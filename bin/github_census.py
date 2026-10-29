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
import os
import re
import socket
import struct
import time

WINDOW = 300                # seconds: the sample interval, the census interval and the window of the outside count
OUTSIDE_THRESHOLD = 100     # calls in the window
MAX_KINDS = 20
TOKEN_VAR = re.compile(r"^(GH|GITHUB)_[A-Z0-9_]*TOKEN$")
KIND_OK = re.compile(r"[^A-Za-z0-9._+-]")
GITHUB_HOST = "api.github.com"


def kind_of(comm):
    """A process kind: its short name, reduced to a safe, short word. Never a path or an argument."""
    name = os.path.basename(str(comm or "").strip())
    return (KIND_OK.sub("_", name)[:32]) or "unknown"


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
        if any(peer in github_addresses for peer in (proc.get("peers") or [])):
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
    try:
        return {info[4][0] for info in socket.getaddrinfo(GITHUB_HOST, 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        return set()


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


def read_table(proc_root="/proc"):
    """The real process table, reduced at the source: `comm`, the NAMES of the GitHub token variables (their values are never kept: each is
    the empty string) and the remote addresses of the connections. The command line is never opened. Raises OSError when the table cannot be listed."""
    peers = _socket_peers(proc_root)
    table = []
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
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
                    audit.append(actor, "github.census_failed", worst[0], {"error": type(err).__name__})
                return
            audit.append(actor, "github.census", worst[0], {"outside": worst[1], "window_seconds": WINDOW,
                                                            "kinds": found["kinds"], "processes": found["processes"]})
    except Exception:
        return
