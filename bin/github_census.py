"""github_census: which KINDS of process spend the GitHub calls the switchboard's records do not count (#1501).

Every 5 minutes the daemon writes one `github.sample` audit line per account: the real used count, the recorded count and the number of live
agent sessions per model provider. When the real used count rose, in the last 5 minutes, by more than OUTSIDE_THRESHOLD calls beyond the recorded
count, one `github.census` audit line is written (at most one per 5 minutes) with the outside count and, per kind of process that holds a
connection to api.github.com or has a GitHub token variable in its environment, a kind name and a count.

Read only. It makes no GitHub call (the real usage figure is the one the daemon already read), reads the machine's process table and writes
one audit line. A line NEVER holds an environment value, a command line, an argument, a token or an address: the table reader keeps only the
process's short name, the NAMES of its GitHub variables and whether a peer is a GitHub address; the census keeps the short name and a count.
A failure is recorded once and never stops the pass. Python 3.9+, standard library only; loaded by path from beside bin/fleet-switchboard.
"""

import collections
import ipaddress
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time

WINDOW = 300                # seconds: the sample interval, the census interval and the window of the outside count
OUTSIDE_THRESHOLD = 100     # calls in the window
MAX_KINDS = 20
TOKEN_VAR = re.compile(r"^(GH|GITHUB)_[A-Z0-9_]*TOKEN$")
KIND_OK = re.compile(r"[^A-Za-z0-9._+-]")
GITHUB_HOST = "api.github.com"
MAX_PROCESSES = 5000        # processes scanned per census, by either reader (the rest are not read)
MAX_ROWS = 20000            # rows read from the output of ps or lsof (the rest is dropped)
SUBPROCESS_TIMEOUT = 10     # seconds each of ps and lsof may run: a hang is one github.census_failed line
GITHUB_RANGES = tuple(ipaddress.ip_network(n) for n in ("140.82.0.0/16", "192.30.252.0/22", "185.199.108.0/22"))   # IPv4 only
PS_ARGV = ["/bin/ps", "-axo", "comm="]                          # command NAME column only: never a flag that prints environments or arguments
LSOF_ARGV = ["/usr/sbin/lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED"]
MAX_BYTES = 4 << 20         # bytes read from the output of ps or lsof (then the child is killed and reaped)
DNS_TIMEOUT = 3             # seconds the lookup of api.github.com may take: a miss is one github.census_failed line


class ProcUnavailable(OSError):
    """Linux without /proc/net: recorded as one github.census_failed line naming "no /proc/net" (never the macOS reader)."""


def kind_of(comm):
    """A process kind: its short name, reduced to a safe, short word. Never a path or an argument."""
    name = os.path.basename(str(comm or "").strip())
    return (KIND_OK.sub("_", name)[:32]) or "unknown"


def in_github_range(peer):
    """True when `peer` is an IPv4 address in a GitHub range (140.82.0.0/16, 192.30.252.0/22, 185.199.108.0/22). IPv6 and anything else: False."""
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return address.version == 4 and any(address in net for net in GITHUB_RANGES)


def census(table, github_addresses):
    """{"kinds": [{"kind", "count", "via"}], "processes": n} from a process table: a list of dicts with `comm`, `environ` (name -> value),
    `peers` (remote addresses) and possibly `cmdline`. Only `comm`, the NAMES in `environ` and the match of `peers` are read: nothing else is."""
    counts = collections.OrderedDict()
    for proc in table or []:
        if not isinstance(proc, dict):
            continue
        via = []
        if any(isinstance(n, str) and TOKEN_VAR.match(n) for n in (proc.get("environ") or {})):
            via.append("env")
        if any(peer in github_addresses or in_github_range(peer) for peer in (proc.get("peers") or [])):
            via.append("connection")
        if not via:
            continue
        cell = counts.setdefault(kind_of(proc.get("comm")), {"count": 0, "via": set()})
        cell["count"] += 1
        cell["via"].update(via)
    rows = [{"kind": k, "count": c["count"], "via": sorted(c["via"])} for k, c in counts.items()]
    rows.sort(key=lambda r: (-r["count"], r["kind"]))
    if len(rows) > MAX_KINDS:
        rows = rows[:MAX_KINDS - 1] + [{"kind": "other", "count": sum(r["count"] for r in rows[MAX_KINDS - 1:]), "via": []}]
    return {"kinds": rows, "processes": sum(r["count"] for r in rows)}


def github_addresses():
    """The addresses api.github.com resolves to now (a name lookup, not a GitHub call). Empty when it cannot be resolved."""
    out = []

    def look():
        try:
            out.append({info[4][0] for info in socket.getaddrinfo(GITHUB_HOST, 443, proto=socket.IPPROTO_TCP)})
        except OSError:
            out.append(set())
    worker = threading.Thread(target=look, daemon=True)
    worker.start()
    worker.join(DNS_TIMEOUT)
    if not out:
        raise TimeoutError("lookup of %s took longer than %d s" % (GITHUB_HOST, DNS_TIMEOUT))
    return out[0]


def _peer(hexaddr):
    raw = bytes.fromhex(hexaddr)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    words = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
    return socket.inet_ntop(socket.AF_INET6, words)


def _socket_peers(proc_root):
    """{socket inode: remote address} of the established TCP connections of the machine."""
    found = {}
    for name in ("tcp", "tcp6"):
        try:
            with open(os.path.join(proc_root, "net", name)) as handle:
                rows = handle.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            cols = row.split()
            if len(cols) > 9 and cols[3] == "01":
                try:
                    found[cols[9]] = _peer(cols[2].split(":")[0])
                except (ValueError, OSError):
                    continue
    return found


LSOF_ESCAPE = re.compile(r"\\x([0-9a-fA-F]{2})")


def _run_text(argv):
    """The text output of a read-only command, with a timeout, a minimal environment, at most MAX_BYTES bytes (then the child is killed and reaped)
    and at most MAX_ROWS rows. Raises on a hang, a missing command or a failure with no output (one github.census_failed line)."""
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin:/usr/sbin"})
    timed_out = []

    def expire():
        timed_out.append(True)
        child.kill()
    timer = threading.Timer(SUBPROCESS_TIMEOUT, expire)
    timer.start()
    try:
        raw = child.stdout.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raw = raw[:MAX_BYTES].rsplit(b"\n", 1)[0]   # the cut line is dropped
            child.kill()
    finally:
        timer.cancel()
        child.stdout.close()
        child.wait()
    if timed_out:
        raise subprocess.TimeoutExpired(argv, SUBPROCESS_TIMEOUT)
    text = raw.decode("utf-8", "replace")
    if child.returncode not in (0, 1) and not text.strip():   # lsof exits 1 for "no match"
        raise OSError("%s exited %d" % (os.path.basename(argv[0]), child.returncode))
    return text.splitlines()[:MAX_ROWS]


def parse_ps(lines):
    """The kinds from `ps -axo comm=` text: each line is the FULL PATH of an executable (spaces possible): its basename, whole line, never split."""
    return [kind_of(line) for line in lines[:MAX_PROCESSES] if line.strip()]


def parse_lsof(lines):
    """{pid: (command, [GitHub peers], cut)} from `lsof -nP -iTCP -sTCP:ESTABLISHED` text. COMMAND is truncated to 9 characters, spaces are `\\x20`;
    IPv6 peers are in brackets and never counted. Only the command, the pid (to group) and a peer that is in a GitHub range are kept."""
    found = collections.OrderedDict()
    for line in lines:
        cols = line.split()
        if len(cols) < 9 or cols[0] == "COMMAND" or not cols[1].isdigit() or "(ESTABLISHED)" not in line:
            continue
        link = next((c for c in cols if "->" in c), None)
        if link is None:
            continue
        peer = link.split("->", 1)[1]
        if peer.startswith("["):
            continue
        peer = peer.rsplit(":", 1)[0]
        if cols[1] not in found and len(found) >= MAX_PROCESSES:
            continue
        cell = found.setdefault(cols[1], (LSOF_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), cols[0]), [], len(cols[0]) >= 9))
        if in_github_range(peer):
            cell[1].append(peer)
    return found


def read_table_macos():
    """The macOS table (no /proc): kinds from ps, connections from lsof. There is NO environment-name signal here (`ps eww` would print values and is
    never run). The lsof name is cut to 9 characters: it is widened to the full name from ps when exactly one ps name starts with it."""
    names = parse_ps(_run_text(PS_ARGV))
    table = []
    for command, peers, cut in parse_lsof(_run_text(LSOF_ARGV)).values():
        full = sorted({n for n in names if n.startswith(kind_of(command))}) if cut else []
        table.append({"comm": full[0] if len(full) == 1 else command, "environ": {}, "peers": peers})
    return table


def read_table(proc_root="/proc"):
    """The real process table, reduced at the source: `comm`, the NAMES of the GitHub token variables (their values are never kept: each is
    the empty string) and the remote addresses of the connections. The command line is never opened. Raises OSError when the table cannot be listed.
    Chosen by sys.platform: linux reads /proc (no /proc/net: ProcUnavailable, never the macOS reader); darwin and any other platform use ps and lsof. At most MAX_PROCESSES processes are scanned."""
    if not sys.platform.startswith("linux"):
        return read_table_macos()
    if not os.path.isdir(os.path.join(proc_root, "net")):
        raise ProcUnavailable("no /proc/net")
    peers = _socket_peers(proc_root)
    table = []
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        if len(table) >= MAX_PROCESSES:
            break
        base = os.path.join(proc_root, entry)
        try:
            with open(os.path.join(base, "comm")) as handle:
                comm = handle.read().strip()
        except OSError:
            continue   # gone, or not ours
        row = {"comm": comm, "environ": {}, "peers": []}
        try:
            with open(os.path.join(base, "environ"), "rb") as handle:
                for item in handle.read(1 << 20).split(b"\0"):
                    name = item.split(b"=", 1)[0].decode("ascii", "ignore")
                    if TOKEN_VAR.match(name):
                        row["environ"][name] = ""
        except OSError:
            pass
        try:
            for fd in os.listdir(os.path.join(base, "fd")):
                target = os.readlink(os.path.join(base, "fd", fd))
                if target.startswith("socket:[") and target[8:-1] in peers:
                    row["peers"].append(peers[target[8:-1]])
        except OSError:
            pass
        table.append(row)
    return table


def sessions_by_provider(engine):
    """{provider: live agent sessions} from the daemon's remembered session records (no new read). {} when there are none."""
    counts = {}
    try:
        for info, at, _status in list(engine.reads.sessions.values()):
            model = info.get("model") if isinstance(info, dict) else None
            provider = model.get("providerID") if isinstance(model, dict) else None
            provider = kind_of(provider) if isinstance(provider, str) and provider else "unknown"
            counts[provider] = counts.get(provider, 0) + 1
    except Exception:
        return {}
    return counts


def run(engine, readings, recorded, now):
    """One look per usage read (the daemon calls it with what it just read; no GitHub call here). `readings` {account: (used, remaining, limit, reset)},
    `recorded` {account: calls recorded in the same window}. Never raises."""
    try:
        audit = getattr(engine, "audit", None)
        state = engine.__dict__.setdefault("census_state", {"seen": {}, "sample_at": now, "census_at": None, "failed": False}) \
            if hasattr(engine, "__dict__") else {"seen": {}, "sample_at": now, "census_at": None, "failed": False}
        if audit is None:
            return
        actor = getattr(engine, "actor", "switchboard")
        worst = None
        for account, (used, _remaining, _limit, reset) in sorted(readings.items()):
            history = state["seen"].setdefault(account, collections.deque())
            while history and history[0][0] <= now - WINDOW:
                history.popleft()
            rec = recorded.get(account, 0)
            if history and history[0][3] == reset and used >= history[0][1]:
                outside = (used - history[0][1]) - max(0, rec - history[0][2])
                if outside > OUTSIDE_THRESHOLD and (worst is None or outside > worst[1]):
                    worst = (account, outside)
            history.append((now, used, rec, reset))
        if now - state["sample_at"] >= WINDOW:
            state["sample_at"] = now
            providers = sessions_by_provider(engine)
            for account, (used, _r, _l, _t) in sorted(readings.items()):
                audit.append(actor, "github.sample", account, {"used": used, "recorded": recorded.get(account, 0), "sessions": providers})
        if worst is not None and (state["census_at"] is None or now - state["census_at"] >= WINDOW):
            state["census_at"] = now
            try:
                found = census(read_table(), github_addresses())   # looked up at call time: a test replaces them
                state["failed"] = False
            except Exception as err:
                if not state["failed"]:
                    state["failed"] = True
                    detail = {"error": type(err).__name__}
                    if isinstance(err, ProcUnavailable):
                        detail["reason"] = "no /proc/net"
                    audit.append(actor, "github.census_failed", worst[0], detail)
                return
            audit.append(actor, "github.census", worst[0], {"outside": worst[1], "window_seconds": WINDOW,
                                                            "kinds": found["kinds"], "processes": found["processes"]})
    except Exception:
        return
