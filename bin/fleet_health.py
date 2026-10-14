"""fleet_health: the load of each orchestrator and the threshold alert to the Chief of Staff (#348).

Every daemon pass `tick` computes, per orchestrator, a load figure from records the switchboard already has (its cached list of open asks,
its coders' states, the movement of each ask, the decisions and reminders waiting on it), keeps an hourly series for the current UTC day
in load.json, and, when the alert is on, wakes the Chief of Staff ONCE per episode when a threshold is crossed. `status` prints one line per
orchestrator from load.json. All decisions live here; bin/fleet-switchboard only supplies the facts (`Context`) and the delivery.
Loaded by path from bin/fleet-switchboard (load_fleet_health), like plan_ledger.

Real movement of an ask: a push, a pull request opened or merged, a plan approved, a report naming it. A comment is not movement.
A figure whose source cannot be read is None (`not recorded`), never zero, and an orchestrator with an unreadable source is never alerted.
"""
import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = "load.json"
MOVES_FILE = "ask-moves.jsonl"
MOVES_KEEP_SECONDS = 24 * 3600
MOVES_READ_BYTES = 4 << 20       # the end of the movement file that is read: it is fed from outside, so it is never read whole
MOVES_NUMBERS_MAX = 20           # issue numbers kept from one pull request body
MOVES_ROWS_MAX = 20000
ARCHIVE_READ_BYTES = 64 << 20    # per audit archive read by the replay
AUDIT_TAIL_BYTES = 8 << 20
HISTORY_DAYS = 3                 # finished days of the hourly series kept for the replay
READ_BUDGET = 10                 # GitHub reads of asks lists in any hour, for the whole fleet
ASKS_FRESH_SECONDS = 3600        # a cached list of asks is re-read at most this often
ASKS_STALE_SECONDS = 3 * 3600    # a list older than this is not believed: the figure is not recorded
DEFAULTS = {"enabled": False, "stalled_hours": 2.0, "stalled_asks": 2, "backlog_hours": 4.0}
_REF = re.compile(r"report on (?:([\w.-]+)/)?([\w.-]*)#(\d+)")


def validate(raw, where):
    """The `load_alert` section: absent or null is off. Raises ValueError with a message."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError("%s must be an object, got %r" % (where, type(raw).__name__))
    section = dict(DEFAULTS, **raw)
    if not isinstance(section["enabled"], bool):
        raise ValueError("%s.enabled must be true or false, got %r" % (where, section["enabled"]))
    for key in ("stalled_hours", "backlog_hours"):
        value = section[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 168:
            raise ValueError("%s.%s must be a number of hours above 0 and at most 168, got %r" % (where, key, value))
    count = section["stalled_asks"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("%s.stalled_asks must be a whole number of 1 or more, got %r" % (where, count))
    return section


# ---- movement records

def move_rows(event, payload, repo, now, closing=lambda text: []):
    """[(repo, numbers, kind)] a webhook delivery says about real movement: a push (a pull request synchronised), a pull request opened or
    merged. `numbers` are the pull request and the issues its body closes. Comments, reviews and labels are not movement."""
    if event != "pull_request" or not isinstance(payload, dict):
        return []
    pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
    number, action = pr.get("number"), payload.get("action")
    if not isinstance(number, int) or isinstance(number, bool):
        return []
    if action == "synchronize":
        kind = "push"
    elif action in ("opened", "reopened"):
        kind = "pr_opened"
    elif action == "closed" and (pr.get("merged") or pr.get("merged_at")):
        kind = "pr_merged"
    else:
        return []
    numbers = ([number] + [n for n in closing(pr.get("body")) if n != number])[:MOVES_NUMBERS_MAX]
    return [{"at": now, "repo": repo, "numbers": numbers, "kind": kind}]


@contextmanager
def _moves_lock(state_dir):
    """One flock for the append (the webhook thread) and the prune's read-and-replace (the daemon pass): a row appended meanwhile is not lost."""
    fd = os.open(str(Path(state_dir) / (MOVES_FILE + ".lock")), os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        yield


def append_moves(state_dir, rows):
    if not rows:
        return
    with _moves_lock(state_dir):
        path = Path(state_dir) / MOVES_FILE
        try:   # a torn last line (a crash mid-append) must not swallow the next row
            with open(str(path), "rb") as tail:
                tail.seek(0, os.SEEK_END)
                torn = tail.tell() > 0 and (tail.seek(-1, os.SEEK_END) or tail.read(1)) != b"\n"
        except OSError:
            torn = False
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write("\n" if torn else "")
            handle.write("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def read_moves(state_dir, since):
    path = Path(state_dir) / MOVES_FILE
    rows = []
    try:
        with open(str(path), "rb") as handle:   # only the end of the file: a torn first line is dropped
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - MOVES_READ_BYTES))
            lines = handle.read().decode("utf-8", "replace").splitlines()[1 if size > MOVES_READ_BYTES else 0:]
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("at"), (int, float)) and row["at"] >= since and isinstance(row.get("numbers"), list):
                rows.append(row)
        rows = rows[-MOVES_ROWS_MAX:]
    except OSError:
        pass
    return rows


def prune_moves(state_dir, now):
    """Drop rows older than a day, when the file has any (a rewrite, atomic, under the same lock as the append)."""
    path = Path(state_dir) / MOVES_FILE
    with _moves_lock(state_dir):
        rows = read_moves(state_dir, now - MOVES_KEEP_SECONDS)
        try:
            with open(str(path), "rb") as handle:   # a bounded count: the file is fed from outside
                stored = len(handle.read(MOVES_READ_BYTES + 1).splitlines())
                oversize = handle.seek(0, os.SEEK_END) > MOVES_READ_BYTES
            if stored <= len(rows) + 200 and not oversize:   # a few hundred stale rows are not worth a rewrite
                return
        except OSError:
            return
        temp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        os.replace(str(temp), str(path))


def _epoch(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def audit_moves(state_dir, since):
    """Movement the audit log holds: a report naming an ask (a delivered report's description) and a plan approved (a review consensus, whose
    subject is `repo#ask`). Only the end of the active file is read; None when it cannot be read."""
    path = Path(state_dir) / "audit.jsonl"
    try:
        with open(str(path), "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - AUDIT_TAIL_BYTES))
            data = handle.read()
            torn = size > AUDIT_TAIL_BYTES
    except OSError:
        return None
    rows = []
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines()):
        if (number == 0 and torn) or ("report on" not in line and "plan.review.consensus" not in line):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        at = _epoch(entry.get("ts")) if isinstance(entry, dict) else None
        if at is None or at < since:
            continue
        detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
        if entry.get("event") == "v2.synthetic" and entry.get("phase") == "intent":
            found = _REF.search(str(detail.get("description") or ""))
            if found:
                rows.append({"at": at, "repo": (found.group(1) + "/" if found.group(1) else "") + found.group(2), "numbers": [int(found.group(3))], "kind": "report"})
        elif entry.get("event") == "plan.review.consensus" and "#" in str(entry.get("subject")):
            repo, _, ask = str(entry["subject"]).rpartition("#")
            if ask.isdigit():
                rows.append({"at": at, "repo": repo, "numbers": [int(ask)], "kind": "plan"})
    return rows


def _same_repo(want, have):
    """A moved row names a repo as `owner/name`, `name` or nothing; an ask's repo is `owner/name`."""
    want, have = str(want or "").lower(), str(have or "").lower()
    return not have or not want or want == have or want.rpartition("/")[2] == have.rpartition("/")[2]


# ---- the figures

def last_moves(asks, rows):
    """{(repo, number): the newest movement time among `rows`, or None}."""
    found = {}
    for ask in asks:
        times = [row["at"] for row in rows if ask["number"] in row["numbers"] and _same_repo(ask["repo"], row.get("repo"))]
        found[(ask["repo"], ask["number"])] = max(times) if times else None
    return found


def figures(name, info, rows, now, cfg):
    """One orchestrator's figures. `info`: asks [{repo, number, first_seen}] or None (unreadable), busy, idle, messages, decisions."""
    out = {"name": name, "busy": info["busy"], "idle": info["idle"], "messages": info["messages"], "decisions": info["decisions"],
           "open_asks": None, "stalled": None, "stalled_asks": [], "oldest_stall_hours": None, "moved_1h": None, "moved_backlog": None}
    asks = info.get("asks")
    if asks is None:
        return out
    last = last_moves(asks, rows)
    out["open_asks"] = len(asks)
    ages, stalled = [], []
    for ask in asks:
        at = last[(ask["repo"], ask["number"])]
        at = max(at, ask["first_seen"]) if at is not None else ask["first_seen"]   # never seen to move: unmoved since it was first seen
        ages.append(now - at)
        if now - at >= cfg["stalled_hours"] * 3600:
            stalled.append(ask["number"])
    out["stalled"], out["stalled_asks"] = len(stalled), sorted(stalled)
    out["oldest_stall_hours"] = round(max(ages) / 3600.0, 1) if ages else 0.0
    moved = lambda hours: sum(1 for ask in asks if last[(ask["repo"], ask["number"])] is not None and now - last[(ask["repo"], ask["number"])] < hours * 3600)
    out["moved_1h"], out["moved_backlog"] = moved(1), moved(cfg["backlog_hours"])
    return out


def crossing(fig, cfg):
    """(severity, reasons): 0 and [] when no threshold is crossed. Unreadable asks never cross."""
    if fig["open_asks"] is None:
        return 0, []
    reasons, severity = [], 0
    if fig["stalled"] >= cfg["stalled_asks"]:
        reasons.append("stalled"); severity += fig["stalled"]
    full = fig["busy"] is not None and fig["idle"] is not None and fig["busy"] > 0 and fig["idle"] == 0
    if full and fig.get("moved_backlog") is not None and fig["open_asks"] > fig["moved_backlog"]:
        reasons.append("backlog"); severity += fig["open_asks"] - fig["moved_backlog"]
    return severity, reasons


def wake_text(fig, reasons, cfg):
    asks = ", ".join("#%d" % n for n in fig["stalled_asks"][:5])
    name = re.sub(r"[\x00-\x1f\x7f]+|\s+", " ", str(fig["name"]))[:60]
    text = ("Load alert: %s is overloaded. %d open ask(s); %d with no real movement in %g h%s; moved %d in the last hour and %d in %g h; "
            "coders %s busy, %s idle; waiting on it: %s message(s), %s decision(s)." % (
                name, fig["open_asks"], fig["stalled"], cfg["stalled_hours"], " (%s)" % asks if asks else "",
                fig["moved_1h"], fig["moved_backlog"], cfg["backlog_hours"], *["not recorded" if fig[k] is None else fig[k] for k in ("busy", "idle", "messages", "decisions")]))
    actions = []
    if "stalled" in reasons:
        actions.append("move the stalled asks (%s) to another lane" % (asks or "named above"))
    if "backlog" in reasons:
        actions.append("split a lane or add coders (all are busy and asks queue faster than they move)")
    return text + " Recommended: " + "; or ".join(actions) + "."


def judge(fig, cfg, previous):
    """(wake text or None, the episode's new state). Once per episode: again only when worse, or after recovery and a new crossing."""
    severity, reasons = crossing(fig, cfg)
    if fig["open_asks"] is None:
        return None, previous   # nothing believed: no change, no wake
    if not severity:
        return None, {"sent": 0}
    if severity > (previous or {}).get("sent", 0):
        return wake_text(fig, reasons, cfg), {"sent": severity}
    return None, previous


# ---- the pass

class Context:
    """What the daemon supplies: `now`, `state_dir`, `cfg` (validated section), `orchestrators` {name: {charter, repo, busy, idle, messages}},
    `read_asks(charter, repo) -> [{repo, number}]` (may raise), `cos` (the Chief of Staff's name or None) and `wake(text)`."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _read_state(state_dir):
    try:
        data = json.loads((Path(state_dir) / STATE_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state_dir, data):
    path = Path(state_dir) / STATE_FILE
    temp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, sort_keys=True, indent=1)
        handle.write("\n")
    os.replace(str(temp), str(path))


def decisions_waiting(state_dir):
    """{agent: open decisions} from decisions.json, or None when it cannot be read."""
    try:
        listed = json.loads((Path(state_dir) / "decisions.json").read_text()).get("decisions")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(listed, list):
        return None
    found = {}
    for item in listed:
        if isinstance(item, dict) and not item.get("closed"):
            found[str(item.get("agent"))] = found.get(str(item.get("agent")), 0) + 1
    return found


def charter_agent(body):
    """The agent a charter issue names: the `orchestrator: <name>` line of its yaml block, or None."""
    found = re.search(r"^orchestrator:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)[ \t]*$", body if isinstance(body, str) else "", re.M)
    return found.group(1) if found else None


def _resolve_charters(ctx, state, now, reads):
    """{orchestrator: charter number or None} and problems. The charter is the open issue whose yaml block says `orchestrator: <its name>`
    (one `ctx.read_charters(repo) -> [(number, body)]` per repo, cached an hour and counted in the read budget as 2), else the `charter` of its
    fleet metadata; an issue number in its metadata is NEVER taken as the charter on its own (it may be another orchestrator's)."""
    cache, problems, out = state.setdefault("charters", {}), [], {}
    repos = sorted({info.get("repo") for info in ctx.orchestrators.values() if info.get("repo")})
    for repo in repos:
        entry = cache.get(repo) or {}
        if entry.get("at") and now - entry["at"] < ASKS_FRESH_SECONDS:
            continue
        if getattr(ctx, "read_charters", None) is None or len(reads) + 2 > READ_BUDGET:
            continue
        reads.extend([now, now])
        try:
            found = ctx.read_charters(repo)
        except Exception as err:
            problems.append("%s: charters not readable (%s)" % (repo, str(err).splitlines()[0][:60] if str(err) else type(err).__name__))
            continue
        cache[repo] = {"at": now, "map": {a: n for n, body in found for a in [charter_agent(body)] if a}}
    for name, info in ctx.orchestrators.items():
        mapped = ((cache.get(info.get("repo")) or {}).get("map") or {}).get(name)
        out[name] = mapped if mapped is not None else info.get("charter")
    return out, problems


def _refresh_asks(ctx, state, now):
    """Re-read at most READ_BUDGET asks lists an hour (the oldest first); a failed read keeps the cached list and is told."""
    cache, reads, problems = state.setdefault("asks", {}), [t for t in state.get("reads", []) if now - t < 3600], []
    charters, more = _resolve_charters(ctx, state, now, reads)
    problems.extend(more)
    state["charter_of"] = {n: c for n, c in charters.items() if c is not None}
    for name in sorted(ctx.orchestrators, key=lambda n: cache.get(n, {}).get("at", 0)):
        info = dict(ctx.orchestrators[name], charter=charters.get(name))
        entry = cache.get(name) or {}
        if entry.get("at") and now - entry["at"] < ASKS_FRESH_SECONDS:
            continue
        if not info.get("charter") or len(reads) >= READ_BUDGET:
            continue
        reads.append(now)
        try:
            found = ctx.read_asks(info["charter"], info.get("repo"))
        except Exception as err:   # an unreadable source is told, not guessed
            problems.append("%s: asks not readable (%s)" % (name, str(err).splitlines()[0][:80] if str(err) else type(err).__name__))
            continue
        seen = {(a["repo"], a["number"]): a["first_seen"] for a in entry.get("list", [])}
        cache[name] = {"at": now, "list": [{"repo": a["repo"], "number": a["number"], "first_seen": seen.get((a["repo"], a["number"]), now)} for a in found]}
    for gone in [n for n in cache if n not in ctx.orchestrators]:
        del cache[gone]
    state["reads"] = reads
    return problems


def tick(ctx):
    """One pass. Returns the list of (orchestrator, wake text) sent. Never raises for a source it cannot read."""
    now, state = ctx.now, _read_state(ctx.state_dir)
    before_state = json.loads(json.dumps(state))
    problems = _refresh_asks(ctx, state, now)
    rows = read_moves(ctx.state_dir, now - 24 * 3600)
    audited = audit_moves(ctx.state_dir, now - 24 * 3600)
    if audited is None:
        problems.append("movement: the audit log cannot be read")
    rows += audited or []
    waiting = decisions_waiting(ctx.state_dir)
    if waiting is None:
        problems.append("decisions: decisions.json cannot be read")
    day, hour = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"), datetime.fromtimestamp(now, timezone.utc).strftime("%H")
    series, sent, episodes, latest = state.get("series", {}), [], state.get("episodes", {}), {}
    unchartered = []
    for name in sorted(ctx.orchestrators):
        info, entry = ctx.orchestrators[name], (state.get("asks") or {}).get(name)
        asks = None
        if entry and now - entry.get("at", 0) < ASKS_STALE_SECONDS:
            asks = entry["list"]
        elif (state.get("charter_of") or {}).get(name):
            problems.append("%s: no recent list of its open asks" % name) if (name + ": ") not in " ".join(problems) else None
        else:
            unchartered.append(name)
        fig = figures(name, {"asks": asks, "busy": info["busy"], "idle": info["idle"], "messages": info["messages"],
                             "decisions": None if waiting is None else waiting.get(name, 0)}, rows, now, ctx.cfg)
        latest[name] = fig
        old = series.get(name)
        if isinstance(old, dict) and old.get("day") != day and old.get("hours"):   # a new UTC day: the finished day stays replayable for a few days
            history = state.setdefault("history", {})
            history.setdefault(old["day"], {})[name] = old["hours"]
            for gone in sorted(history)[:-HISTORY_DAYS]:
                del history[gone]
        mine = old if isinstance(old, dict) and old.get("day") == day else {"day": day, "hours": {}}
        mine["hours"][hour] = {k: fig[k] for k in ("open_asks", "stalled", "stalled_asks", "moved_1h", "moved_backlog", "busy", "idle", "messages", "decisions", "oldest_stall_hours")}
        series[name] = mine
        before = episodes.get(name)
        text, episodes[name] = judge(fig, ctx.cfg, before)
        if text and ctx.cfg["enabled"] and ctx.cos:
            ctx.wake(text)
            sent.append((name, text))
        elif text:
            episodes[name] = before   # not sent (off, or no Chief of Staff to tell): the episode is still open
    if unchartered:   # no charter issue is recorded in its fleet metadata: its asks cannot be found, said once for all of them
        problems.append("no charter recorded for %s" % ", ".join(unchartered[:6]) + (" and %d more" % (len(unchartered) - 6) if len(unchartered) > 6 else ""))
    state.pop("at", None)
    state.update({"series": {n: series[n] for n in latest}, "episodes": {n: episodes.get(n) for n in latest},
                  "latest": latest, "problems": problems, "alert": bool(ctx.cfg["enabled"])})
    if state != before_state:   # an idle pass rewrites nothing
        _write_state(ctx.state_dir, state)
    prune_moves(ctx.state_dir, now)
    return sent


def status_lines(state_dir, now):
    """The `status` lines: one per orchestrator, then what could not be read. Empty until the daemon has made a pass."""
    state = _read_state(state_dir)
    latest = state.get("latest")
    if not isinstance(latest, dict) or not latest:
        return []
    num = lambda value: "not recorded" if value is None else str(value)
    out = ["load        %d orchestrator(s); alert %s" % (len(latest), "on" if state.get("alert") else "off")]
    for name in sorted(latest):
        f = latest[name]
        out.append("  %-12s asks %s, stalled %s (oldest %s h), moved 1h %s; waiting %d msg %s dec; coders %d busy %d idle" % (
            name, num(f["open_asks"]), num(f["stalled"]), num(f["oldest_stall_hours"]), num(f["moved_1h"]),
            f["messages"], num(f["decisions"]), f["busy"], f["idle"]))
    if state.get("problems"):
        out.append("  unreadable: %s" % "; ".join(state["problems"][:3]))
    return out


def replay(state_dir, cfg, day=None):
    """What the alert WOULD have done over a recorded day of the hourly series (the current day, or `day` while it is kept): a list of
    {orchestrator, hour, severity, reasons, text}, one per wake, in time order, by the same rule the daemon applies (once per episode).
    (days, firings): the days that are recorded, and the firings of the one asked for (None when it is not recorded)."""
    state = _read_state(state_dir)
    by_day = {d: dict(names) for d, names in (state.get("history") or {}).items() if isinstance(names, dict)}
    for name, entry in (state.get("series") or {}).items():
        if isinstance(entry, dict) and entry.get("day") and isinstance(entry.get("hours"), dict):
            by_day.setdefault(entry["day"], {})[name] = entry["hours"]
    days = sorted(by_day)
    chosen = day or (days[-1] if days else None)
    if chosen not in by_day:
        return days, None
    firings = []
    for name in sorted(by_day[chosen]):
        previous = None
        for hour in sorted(by_day[chosen][name]):
            fig = dict(by_day[chosen][name][hour], name=name)
            if fig.get("open_asks") is None:
                continue
            text, previous = judge(fig, cfg, previous)
            if text:
                severity, reasons = crossing(fig, cfg)
                firings.append({"orchestrator": name, "hour": "%s %s:00Z" % (chosen, hour), "severity": severity, "reasons": reasons, "text": text})
    firings.sort(key=lambda f: (f["hour"], f["orchestrator"]))
    return days, firings


# ---- a past day rebuilt from the audit log (best effort)

RECONSTRUCT_NOT_RECORDED = ("coders busy and idle, messages waiting and decisions waiting (the audit does not hold them, so the backlog crossing cannot be "
                            "judged); pushes and pull requests (only reports and plan approvals are audited); the real list of open asks, approximated by the asks "
                            "an orchestrator's reports, reminders and workers named in the 24 hours before")
RECONSTRUCT_WINDOW = 24 * 3600


def _audit_files(state_dir, first):
    state, files = Path(state_dir), []
    try:
        for path in sorted(state.glob("audit*.jsonl.gz")):
            try:
                if path.stat().st_mtime >= first:
                    files.append(path)
            except OSError:
                continue
    except OSError:
        pass
    if (state / "audit.jsonl").exists():
        files.append(state / "audit.jsonl")
    return files


def _audit_events(state_dir, first, last, events):
    """The audit entries between two epochs whose event is in `events`, oldest first by file; None when there is no audit file."""
    import gzip
    files = _audit_files(state_dir, first)
    if not files:
        return None
    out = []
    for path in files:
        try:
            with (gzip.open(str(path), "rt", encoding="utf-8", errors="replace") if path.suffix == ".gz" else open(str(path), encoding="utf-8", errors="replace")) as handle:
                for line in handle.read(ARCHIVE_READ_BYTES).splitlines():   # an operator's replay: bounded per file
                    if not any(e in line for e in events):
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    at = _epoch(entry.get("ts")) if isinstance(entry, dict) else None
                    if at is not None and first <= at < last and entry.get("event") in events:
                        out.append((at, entry))
        except (OSError, EOFError, ValueError):
            continue   # an unreadable archive costs its days, not the replay
    out.sort(key=lambda pair: pair[0])
    return out


def reconstruct_day(state_dir, day, cfg=DEFAULTS):
    """{orchestrator: {hour: figures}} for a past UTC `day`, from the audit log and its archives: or None when no audit file exists. Best effort: see
    RECONSTRUCT_NOT_RECORDED. Orchestrators are the agents workers were started to report to; an ask is in play for an hour while an orchestrator's report,
    reminder or worker named it in the 24 h before; it moves when a report or a plan approval names it."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    events = {"v2.session.create", "reminder.add", "v2.synthetic", "plan.review.consensus"}
    found = _audit_events(state_dir, start - RECONSTRUCT_WINDOW, start + 86400, events)
    if found is None:
        return None
    bosses, mentions, moves = set(), [], []   # mentions: (at, boss, repo, number); moves: (at, boss, repo, number)
    for at, entry in found:
        detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
        event = entry.get("event")
        if event == "v2.session.create":
            fleet = detail.get("fleet") if isinstance(detail.get("fleet"), dict) else {}
            if fleet.get("reports_to"):
                bosses.add(str(fleet["reports_to"]))
                if isinstance(fleet.get("issue"), int):
                    mentions.append((at, str(fleet["reports_to"]), "", fleet["issue"]))
        elif event == "reminder.add" and isinstance(detail.get("issue"), int) and detail.get("from"):
            mentions.append((at, str(detail["from"]), str(detail.get("repo") or ""), detail["issue"]))
        elif event == "v2.synthetic" and entry.get("phase") == "intent":
            match = re.search(r"switchboard: ([\w.-]+) report on (?:([\w.-]+)/)?([\w.-]*)#(\d+)", str(detail.get("description") or ""))
            if match:
                repo = (match.group(2) + "/" if match.group(2) else "") + match.group(3)
                moves.append((at, match.group(1), repo, int(match.group(4))))
        elif event == "plan.review.consensus" and "#" in str(entry.get("subject")):
            repo, _, ask = str(entry["subject"]).rpartition("#")
            if ask.isdigit():
                moves.append((at, None, repo, int(ask)))   # a plan approved moves the ask for whoever holds it
    bosses.discard("cos")
    result = {}
    for boss in sorted(bosses):
        mine = [m for m in mentions if m[1] == boss]
        asks = {}
        for at, _, repo, number in sorted(mine):
            asks.setdefault(("", number), []).append(at)   # keyed by number: a worker's record names no repo, a reminder's may
        hours = {}
        for hour in range(24):
            end = start + (hour + 1) * 3600
            present = []
            for (repo, number), seen in asks.items():
                seen = [t for t in seen if end - RECONSTRUCT_WINDOW <= t < end]
                if seen:   # with no pushes in the audit, the baseline is the newest mention of any kind, so a quiet ask is not an alarm for what the audit cannot see
                    present.append((repo, number, max(seen)))
            if not present:
                continue
            rows = [{"at": at, "repo": "", "numbers": [number]} for at, who, repo, number in moves if at < end]   # a worker's report or a plan approval moves the ask it names
            ask_list = [{"repo": repo, "number": number, "first_seen": first} for repo, number, first in present]
            hours["%02d" % hour] = figures(boss, {"asks": ask_list, "busy": None, "idle": None, "messages": None, "decisions": None}, rows, end, cfg)
        if hours:
            result[boss] = hours
    return result


def replay_audit(state_dir, cfg, day):
    """(firings, notes) for a past `day` rebuilt from the audit; firings is None when there is no audit to read."""
    rebuilt = reconstruct_day(state_dir, day, cfg)
    if rebuilt is None:
        return None, RECONSTRUCT_NOT_RECORDED
    firings = []
    for name in sorted(rebuilt):
        previous = None
        for hour in sorted(rebuilt[name]):
            fig = dict(rebuilt[name][hour])
            text, previous = judge(fig, cfg, previous)
            if text:
                severity, reasons = crossing(fig, cfg)
                firings.append({"orchestrator": name, "hour": "%s %s:59Z" % (day, hour), "severity": severity, "reasons": reasons, "text": text})
    firings.sort(key=lambda f: (f["hour"], f["orchestrator"]))
    return firings, RECONSTRUCT_NOT_RECORDED
