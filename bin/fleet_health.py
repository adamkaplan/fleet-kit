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
READ_BUDGET_DEFAULT = 30         # GitHub reads an hour for each gh account (the key load_alert.read_budget); a 304 is free
READ_BUDGET_MAX = 60             # a hard ceiling: a key above it is a configuration error
ASKS_FRESH_SECONDS = 3600        # a cached list of asks is re-read at most this often
STALE_MARK_FILE = "asks-stale.json"
STALE_MARKS_MAX = 200            # repositories kept in the stale marks (a delivery names the repository of the daemon's own watch list)
STALE_REREAD_SECONDS = 300       # a repository's asks list marked stale is re-read at most this often
STALE_ISSUE_ACTIONS = ("closed", "reopened", "labeled", "unlabeled", "edited", "deleted", "transferred")
RETRY_SECONDS = 300              # after a failed read the same source is not tried again for this long
ASKS_STALE_SECONDS = 3 * 3600    # a list older than this is not believed: the figure is not recorded
DEFAULTS = {"enabled": False, "stalled_hours": 2.0, "stalled_asks": 2, "backlog_hours": 4.0, "read_budget": READ_BUDGET_DEFAULT}
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
    budget = section["read_budget"]
    if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= READ_BUDGET_MAX:
        raise ValueError("%s.read_budget must be a whole number of GitHub reads an hour for each account, from 1 to %d, got %r" % (where, READ_BUDGET_MAX, budget))
    count = section["stalled_asks"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("%s.stalled_asks must be a whole number of 1 or more, got %r" % (where, count))
    return section


# ---- movement records

_REF_PLAIN = re.compile(r"(?<![\w/&])#(\d{1,7})\b")
_REF_URL = re.compile(r"/issues/(\d{1,7})\b")
_REF_BRANCH = re.compile(r"(?:^|[/_-])(\d{2,7})(?=$|[/_-])")
PARENT_LEVELS = 3                # how far up the parent chain a referenced issue is followed
PARENTS_KEEP = 3000              # resolved parents kept (a parent never changes)


def references(text, branch=None, repo=None):
    """Every issue number a pull request body (and its branch name) refers to: `Closes #N`, `Part of #N`, `Refs #N`, a plain `#N` or an issues URL
    of the same repository. `owner/other#N` of another repository is not this repository's issue. Order kept, no repeats."""
    found = []
    body = text if isinstance(text, str) else ""
    for match in re.finditer(r"([\w.-]+/[\w.-]+)#(\d{1,7})\b", body):   # a number in another repository is never ours
        if repo and match.group(1).lower() != str(repo).lower():
            body = body.replace(match.group(0), " ")
    for pattern, source in ((_REF_PLAIN, body), (_REF_URL, body), (_REF_BRANCH, branch if isinstance(branch, str) else "")):
        for match in pattern.finditer(source):
            n = int(match.group(1))
            if n not in found:
                found.append(n)
    return found


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
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    refs = [n for n in list(closing(pr.get("body"))) + references(pr.get("body"), head.get("ref"), repo) if n != number]
    refs = list(dict.fromkeys(refs))[:MOVES_NUMBERS_MAX - 1]
    return [{"at": now, "repo": repo, "numbers": [number] + refs, "refs": refs, "kind": kind}]


def mark_stale(state_dir, event, payload, repo, now):
    """Mark `repo`'s cached asks list stale when a delivery says an issue was closed, reopened, labelled, unlabelled, edited, deleted or transferred, or
    that a sub-issue list changed: the next pass re-reads it (the hourly re-read stays as the fallback). Returns True when a mark was written."""
    if not isinstance(payload, dict) or not isinstance(repo, str) or not repo:
        return False
    if not (event == "sub_issues" or (event == "issues" and payload.get("action") in STALE_ISSUE_ACTIONS)):
        return False
    with _moves_lock(state_dir):
        path = Path(state_dir) / STALE_MARK_FILE
        try:
            marks = json.loads(path.read_text())
        except (OSError, ValueError):
            marks = {}
        marks = {k: v for k, v in (marks.items() if isinstance(marks, dict) else []) if isinstance(v, (int, float)) and not isinstance(v, bool)}
        marks[repo.lower()] = now
        while len(marks) > STALE_MARKS_MAX:
            marks.pop(min(marks, key=marks.get))
        temp = path.with_name(path.name + ".tmp")
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(marks, handle, sort_keys=True)
        os.replace(str(temp), str(path))
    return True


def stale_since(state_dir, repo):
    """When a delivery last marked `repo`'s asks list stale, or None (a missing or unreadable file marks nothing)."""
    try:
        marks = json.loads((Path(state_dir) / STALE_MARK_FILE).read_text())
    except (OSError, ValueError):
        return None
    value = marks.get(str(repo or "").lower()) if isinstance(marks, dict) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


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


WAIT_LABELS = ("awaiting-user", "awaiting-cos", "awaiting-live", "parked")   # matched by suffix, whatever the owner's prefix
PARKED_MAX_DAYS = 366            # a park reaches at most this far ahead (the same bound as the switchboard's parked_until users)


def ask_wait(ask, now):
    """What an ask is waiting on, by its labels: "owner" (awaiting-user), "cos" (awaiting-cos), "live" (awaiting-live: it waits on a live event), "parked" (a parked label and a valid future date: the
    call site parsed the `Parked until:` line with the switchboard's own parser) or None. awaiting-user wins over awaiting-cos, awaiting-cos over awaiting-live, and all three over parked."""
    labels = [str(l) for l in (ask.get("labels") or [])]
    has = lambda name: any(l == name or l.endswith(":" + name) for l in labels)
    if has("awaiting-user"):
        return "owner"
    if has("awaiting-cos"):
        return "cos"
    if has("awaiting-live"):
        return "live"
    until = ask.get("parked_until")
    if has("parked") and isinstance(until, (int, float)) and not isinstance(until, bool) and now < until <= now + PARKED_MAX_DAYS * 86400.0:
        return "parked"
    if ask.get("via_pr") in ("owner_pr", "cos_pr"):
        return ask["via_pr"]   # a held pull request that serves it waits on the owner (tier human) or on the Chief of Staff (tier cos, not yet escalated)
    if ask.get("timed"):
        return "time"          # a pending reminder due later names it: a time somebody set
    return None


def held_prs(state_dir):
    """[{repo, number, tier, since}] the open held pull requests (kind `captain`) of decisions.json, or [] when it cannot be read: the same file the pass already reads, nothing from GitHub."""
    try:
        listed = json.loads((Path(state_dir) / "decisions.json").read_text()).get("decisions")
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for item in (listed if isinstance(listed, list) else [])[:DECISIONS_MAX]:
        if not isinstance(item, dict) or item.get("closed") or item.get("kind") != "captain":
            continue
        number, repo = item.get("number"), item.get("repo")
        since = _epoch(item.get("since")) if isinstance(item.get("since"), str) else None
        if isinstance(number, int) and not isinstance(number, bool) and isinstance(repo, str) and _REPO_SHAPE.match(repo) and item.get("tier") in ("human", "cos"):
            out.append({"repo": repo, "number": number, "tier": item["tier"], "since": since})
    return out


PR_REFS_MAX = 10      # references kept for one pull request
HELD_LINKS_MAX = 200  # held pull requests resolved in one pass


def resolve_held_links(ctx, state, budget, held, now, problems):
    """{"owner/repo#N" (lower case): {(repo, number), ...}}: for each held pull request, the issues it is linked to, from `ctx.pr_refs` (the strict references the daemon read from the CURRENT body of its notice facts:
    its first-line `PLAN:` link and its `Closes` / `Part of` lines, never prose or a branch name) and each one's parents (the cached, budgeted parent reads the pass already makes). Rebuilt in full every pass, so a
    reference removed from the body is gone at the next pass; the full `owner/repo` is compared, never the name alone."""
    refs = getattr(ctx, "pr_refs", None) or {}
    out, unresolved = {}, []
    for row in held[:HELD_LINKS_MAX]:
        key = "%s#%d" % (row["repo"].lower(), row["number"])
        found = set()
        if row["tier"] == "human" and key not in refs:
            unresolved.append(key)   # #796: its body's references were not read yet
        for ref in (refs.get(key) or [])[:PR_REFS_MAX]:
            if not (isinstance(ref, (list, tuple)) and len(ref) == 2 and isinstance(ref[1], int) and not isinstance(ref[1], bool) and ref[1] > 0):
                continue
            repo = ref[0] if isinstance(ref[0], str) and _REPO_SHAPE.match(ref[0]) and "/" in ref[0] else row["repo"]
            found.add((repo.lower(), ref[1]))
            cur = ref[1]
            for _ in range(PARENT_LEVELS):
                parent, resolved = _parent_of(ctx, state, budget, repo, cur, now, problems)
                if not resolved and row["tier"] == "human" and key not in unresolved:
                    unresolved.append(key)   # #796: a parent read the budget (or a failure) cut: the link to the ask may be missing
                if not resolved or not parent:
                    break
                found.add((repo.lower(), parent))
                cur = parent
        out[key] = found
    ctx.unresolved_held = unresolved   # #796: the human-tier held pull requests whose links are not whole; `tick` reads it
    return out


def serving_waits(asks, held, links, reminders, names, now, stalled_hours):
    """{(repo, number): "owner_pr" | "cos_pr" | "time"} for the asks waiting on a held pull request that serves them (tier human: the owner's wait; tier cos: the Chief of Staff's wait, only while the row is younger than
    `stalled_hours`) or on a pending reminder due later that names them and is for `names` (the orchestrator or a worker of it). Owner beats cos beats time."""
    out = {}
    for ask in asks:
        best = None
        for row in held:
            link = links.get("%s#%d" % (row["repo"].lower(), row["number"]))
            if not link or (str(ask["repo"]).lower(), ask["number"]) not in {tuple(x) for x in link}:   # the full owner/repo and the number: the name of a repository alone is never enough
                continue
            if row["tier"] == "human":
                best = "owner_pr"
                break
            if row["since"] is not None and now - row["since"] < stalled_hours * 3600:
                best = "cos_pr"
        if best is None and any(r.get("issue") == ask["number"] and not isinstance(r.get("issue"), bool) and r.get("to") in names
                                and isinstance(r.get("at"), (int, float)) and r["at"] > now and (not r.get("repo") or str(r["repo"]).lower() == str(ask["repo"]).lower()) for r in reminders):
            best = "time"
        if best:
            out[(ask["repo"], ask["number"])] = best
    return out


def figures(name, info, rows, now, cfg):
    """One orchestrator's figures. `info`: asks [{repo, number, first_seen}] or None (unreadable), busy, idle, messages, decisions."""
    out = {"name": name, "busy": info["busy"], "idle": info["idle"], "messages": info["messages"], "decisions": info["decisions"],
           "cloud": info.get("cloud") or 0, "open_asks": None, "active_asks": None, "stalled": None, "stalled_asks": [], "oldest_stall_hours": None, "moved_1h": None, "moved_backlog": None,
           "waiting_owner": None, "waiting_cos": None, "waiting_live": None, "parked": None, "charter_moves_1h": None,
           "waiting_owner_pr": None, "waiting_time": None}
    asks = info.get("asks")
    if asks is None:
        return out
    last = last_moves(asks, rows)
    out["open_asks"] = len(asks)
    ages, stalled, waits = [], [], {"owner": 0, "cos": 0, "live": 0, "parked": 0, "owner_pr": 0, "cos_pr": 0, "time": 0}
    for ask in asks:
        wait = ask_wait(ask, now)
        if wait:
            waits[wait] += 1   # waiting on someone or deliberately parked: counted by itself, never as stalled
            continue
        at = last[(ask["repo"], ask["number"])]
        at = max(at, ask["first_seen"]) if at is not None else ask["first_seen"]   # never seen to move: unmoved since it was first seen
        ages.append(now - at)
        if now - at >= cfg["stalled_hours"] * 3600:
            stalled.append(ask["number"])
    out["stalled"], out["stalled_asks"] = len(stalled), sorted(stalled)
    out["waiting_owner"], out["waiting_cos"], out["waiting_live"], out["parked"] = waits["owner"] + waits["owner_pr"], waits["cos"] + waits["cos_pr"], waits["live"], waits["parked"]
    out["waiting_owner_pr"], out["waiting_time"] = waits["owner_pr"], waits["time"]
    out["active_asks"] = len(asks) - sum(waits.values())
    out["oldest_stall_hours"] = round(max(ages) / 3600.0, 1) if ages else 0.0
    moved = lambda hours: sum(1 for ask in asks if last[(ask["repo"], ask["number"])] is not None and now - last[(ask["repo"], ask["number"])] < hours * 3600)
    out["moved_1h"], out["moved_backlog"] = moved(1), moved(cfg["backlog_hours"])
    charter = info.get("charter")
    if isinstance(charter, int) and not isinstance(charter, bool):   # pull requests on issues under its charter, with no open ask on the chain: the lane is moving
        hits = {row["numbers"][0] for row in rows if now - row["at"] < 3600 and charter in row["numbers"][1:] and _same_repo(info.get("repo"), row.get("repo"))}
        out["charter_moves_1h"] = len(hits)
    return out


def crossing(fig, cfg):
    """(severity, reasons): 0 and [] when no threshold is crossed. Unreadable asks never cross."""
    if fig["open_asks"] is None:
        return 0, []
    reasons, severity = [], 0
    if fig["stalled"] >= cfg["stalled_asks"]:
        reasons.append("stalled"); severity += fig["stalled"]
    full = fig["busy"] is not None and fig["idle"] is not None and fig["busy"] > 0 and fig["idle"] == 0
    if full and fig.get("moved_backlog") is not None and fig.get("moved_1h") is not None:
        # all coders busy: only when something is stuck (an ask stalled) or open asks clearly outnumber what moves and nothing moved in the last hour
        active = fig["active_asks"] if fig.get("active_asks") is not None else fig["open_asks"]   # asks waiting on someone or parked are not queued work
        outnumbered = active >= 2 * fig["moved_backlog"] and active > fig["moved_backlog"] and fig["moved_1h"] == 0 and not fig.get("charter_moves_1h")
        if fig["stalled"] >= 1 and "stalled" not in reasons:
            reasons.append("busy_stalled"); severity += max(1, fig["stalled"])
        elif outnumbered:
            reasons.append("outnumbered"); severity += max(1, active - fig["moved_backlog"])
    return severity, reasons


def waiting_phrase(fig):
    return "waiting on the owner: %d (via a held PR), on a set time: %d" % (fig.get("waiting_owner_pr") or 0, fig.get("waiting_time") or 0)


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
    if "busy_stalled" in reasons:
        actions.append("split a lane or add coders: all coders are busy and an ask is stalled (%s)" % (asks or "named above"))
    if "outnumbered" in reasons:
        actions.append("split a lane or add coders: all coders are busy, open asks are at least twice those moved in %g h and none moved in the last hour" % cfg["backlog_hours"])
    if fig.get("waiting_owner_pr") or fig.get("waiting_time"):
        text += " Not stalled, waiting: %s." % waiting_phrase(fig)
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


WAITING_LINE = "load: waiting for the first full pass"
WAITING_LINKS = "load: waiting for held PR links"


def mark_waiting(state_dir):
    """#796: the daemon has started and not finished its first full pass: record it for `status`, touching nothing else (figures, episodes). Never raises."""
    try:
        state = _read_state(state_dir)
        if state.get("warming") != WAITING_LINE:
            state["warming"] = WAITING_LINE
            _write_state(state_dir, state)
    except OSError:
        pass


PR_KINDS = ("notice", "captain")   # the entries of decisions.json that are information about a pull request, not a wait
DECISIONS_MAX = 1000             # entries of decisions.json looked at in one pass (the rest are ignored)
_REPO_SHAPE = re.compile(r"[A-Za-z0-9_.-]{1,100}(/[A-Za-z0-9_.-]{1,100})?\Z")


def decision_items(state_dir):
    """([(repo, issue of the ask)], PR notices and holds) of the open entries of decisions.json, or None when it cannot be read.
    A PR heads-up (kind `notice`) or a held pull request (kind `captain`) waits on no orchestrator and is only counted; any other entry waits, and one with no usable ask (a permission request, a question with no issue) is returned with no issue so that it belongs to nobody.
    At most DECISIONS_MAX entries are read, and a repository that is not shaped like `owner/name` is dropped (the chain is then not read).
    The `agent` field is a metadata guess and is not read here."""
    try:
        listed = json.loads((Path(state_dir) / "decisions.json").read_text()).get("decisions")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(listed, list):
        return None
    out, notices = [], 0
    for item in listed[:DECISIONS_MAX]:
        if not isinstance(item, dict) or item.get("closed"):
            continue
        if item.get("kind") in PR_KINDS:
            notices += 1   # a heads-up or a held pull request: information, it waits on no orchestrator
            continue
        ask = item.get("ask")
        repo = item.get("repo")
        usable = isinstance(ask, int) and not isinstance(ask, bool) and ask > 0
        out.append((repo if isinstance(repo, str) and _REPO_SHAPE.match(repo) else "", ask if usable else None))   # no usable ask: a real wait that belongs to nobody
    return out, notices


def charter_agent(body):
    """The agent a charter issue names: the `orchestrator: <name>` line of its yaml block, or None."""
    found = re.search(r"^orchestrator:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)[ \t]*(?:#[^\r\n]*)?\r?$", body if isinstance(body, str) else "", re.M)   # a trailing comment and a CRLF line end are fine
    return found.group(1) if found else None


def charter_retired(body):
    """True only when a charter's yaml says `status: retired`: a body that cannot be read, or says anything else, is not a retired signal."""
    body = body if isinstance(body, str) else ""
    fenced = re.match(r"\s*```ya?ml[ \t]*\r?\n(.*?)^```", body, re.S | re.M)   # a charter opens with its yaml block: a line in a quote or later fence is not its status
    body = fenced.group(1) if fenced else body
    return bool(re.search(r"^status:[ \t]*retired[ \t]*(?:#[^\r\n]*)?\r?$", body if isinstance(body, str) else "", re.M))


class _Budget:
    """The reads of the last hour, counted for each gh account (the repositories one account reads share its count). `ctx` may supply
    `account_of(repo)`, `paused(repo)` and `begin_read()` / `read_cost(token, repo)` (the reads GitHub really billed: a 304 is free); without them every
    repository is one account, nothing is paused and a read costs what the caller says."""

    def __init__(self, ctx, state, now):
        self.ctx, self.now = ctx, now
        self.limit = (ctx.cfg or {}).get("read_budget", READ_BUDGET_DEFAULT)
        kept = []
        for row in state.get("reads", []):
            if isinstance(row, (int, float)) and not isinstance(row, bool):
                row = [row, "default"]   # a row written before the budget was per account
            if isinstance(row, list) and len(row) == 2 and isinstance(row[0], (int, float)) and now - row[0] < 3600:
                kept.append([row[0], str(row[1])])
        self.rows, self.paused_said = kept, set()

    def account(self, repo):
        of = getattr(self.ctx, "account_of", None)
        return str(of(repo)) if of else "default"

    def left(self, account):
        return self.limit - sum(1 for _, a in self.rows if a == account)

    def paused(self, repo, problems):
        """True when the account of `repo` is paused by the rate limit: no read is tried and nothing is counted; said once per account."""
        check = getattr(self.ctx, "paused", None)
        if not check or not check(repo):
            return False
        account = self.account(repo)
        if account not in self.paused_said:
            self.paused_said.add(account)
            problems.append("account %s is paused by the rate limit: its reads wait" % account)
        return True

    def metered(self, repo, default_cost, call):
        """`call()` and charge what it cost, even when it fails (a failed read was still made)."""
        begin, cost_of = getattr(self.ctx, "begin_read", None), getattr(self.ctx, "read_cost", None)
        token = begin() if begin and cost_of else None
        try:
            return call()
        finally:
            cost = cost_of(token, repo) if begin and cost_of else default_cost
            self.rows.extend([self.now, self.account(repo)] for _ in range(cost))

    def to_state(self):
        return self.rows


def _time_up(ctx, what):
    """#494: True when this pass's time budget for the load alert's reads is spent (`ctx.time_up()`, checked between reads; 0 = no limit); what
    was left is noted in `ctx.pending` and the read is made in a later pass (every list kept is read again only when it is old, oldest first)."""
    check = getattr(ctx, "time_up", None)
    if check is None or not check():
        return False
    pending = getattr(ctx, "pending", None)
    if isinstance(pending, list) and len(pending) < 50:
        pending.append(what)
    return True


def _resolve_charters(ctx, state, now, budget):
    """{orchestrator: charter number or None} and problems. The charter is the open issue whose yaml block says `orchestrator: <its name>`
    (one `ctx.read_charters(repo) -> [(number, body)]` per repo, cached an hour; it costs up to 3 reads and needs 2 left in the account's budget), else
    the `charter` of its fleet metadata, unless that issue's own yaml names another agent; an `issue` number of its metadata is never used."""
    cache, problems, out = state.setdefault("charters", {}), [], {}
    repos = sorted({info.get("repo") for info in ctx.orchestrators.values() if info.get("repo")}, key=lambda r: (cache.get(r) or {}).get("at", 0))
    for repo in repos:
        entry = cache.get(repo) or {}
        if (entry.get("at") and now - entry["at"] < ASKS_FRESH_SECONDS) or now - entry.get("tried", -1e9) < RETRY_SECONDS:
            if entry.get("error") and now - entry.get("tried", -1e9) < RETRY_SECONDS:
                problems.append(entry["error"])   # still failing: said every pass, read again only after the retry wait
            continue
        if getattr(ctx, "read_charters", None) is None or budget.paused(repo, problems) or budget.left(budget.account(repo)) < 2:
            continue
        if _time_up(ctx, "charters of %s" % repo):
            continue
        try:
            found = budget.metered(repo, 2, lambda: ctx.read_charters(repo))
        except Exception as err:
            text = "%s: charters not readable (%s)" % (repo, str(err).splitlines()[0][:60] if str(err) else type(err).__name__)
            problems.append(text)
            cache[repo] = dict(entry, tried=now, error=text)
            continue
        named, dead = {}, set()
        for number, body in found:
            agent = charter_agent(body)
            if agent:
                named[number] = agent
                if charter_retired(body):
                    dead.add(number)
        chosen = {a: n for n, a in sorted(named.items(), reverse=True)}   # the charter the agent is known by (the lowest number)
        gone = {a for a in chosen if all(n in dead for n, who in named.items() if who == a)}   # retired only when EVERY open charter naming it says so: a live one left open (a relaunch) keeps it
        cache[repo] = {"at": now, "map": chosen, "named": {str(n): a for n, a in named.items()}, "retired": sorted(gone)}   # the lowest number wins a duplicate
    for name, info in ctx.orchestrators.items():
        entry = cache.get(info.get("repo")) or {}
        mapped, fallback = (entry.get("map") or {}).get(name), info.get("charter")
        if mapped is None and fallback is not None and (entry.get("named") or {}).get(str(fallback), name) != name:
            fallback = None   # the metadata charter's own yaml names another orchestrator: it is not this one's
        out[name] = mapped if mapped is not None else fallback
    return out, problems


def _wait_fields(ask):
    """The few label facts kept with a cached ask: only the labels that matter and the parsed park date."""
    labels = [l for l in (ask.get("labels") or []) if isinstance(l, str) and any(l == w or l.endswith(":" + w) for w in WAIT_LABELS)][:6]
    out = {"labels": labels} if labels else {}
    if isinstance(ask.get("parked_until"), (int, float)) and not isinstance(ask.get("parked_until"), bool):
        out["parked_until"] = ask["parked_until"]
    return out


def _parent_of(ctx, state, budget, repo, number, now, problems):
    """(parent number or None, resolved): a parent never changes, so one read per number is kept for good (a failure is retried after RETRY_SECONDS).
    Unresolved when it cannot be read or the account's budget is spent or paused: nothing is guessed."""
    parents, failed, key = state.setdefault("parents", {}), state.setdefault("parents_failed", {}), "%s#%d" % (repo, number)
    if key in parents:
        return (parents[key] or None), True
    if now - failed.get(key, -1e9) < RETRY_SECONDS or getattr(ctx, "read_parent", None) is None:
        return None, False
    if budget.paused(repo, problems) or budget.left(budget.account(repo)) < 1:
        return None, False
    if _time_up(ctx, "parent of %s" % key):
        return None, False
    try:
        parent = budget.metered(repo, 1, lambda: ctx.read_parent(repo, number))
    except Exception:
        failed[key] = now
        while len(failed) > PARENTS_KEEP:
            failed.pop(next(iter(failed)))
        return None, False
    parents[key] = parent or 0
    while len(parents) > PARENTS_KEEP:
        parents.pop(next(iter(parents)))   # the oldest: it is only read again
    return parent, True


def expand_rows(ctx, state, budget, rows, asks_by_name, charters, now, problems):
    """Attribute each movement to the asks (and charters) on the parent chain of the issues it refers to: a work item whose parent is an ask moves
    that ask, and one whose chain reaches an orchestrator's charter without an ask moves that charter (up to PARENT_LEVELS levels). A row written
    before references were logged carries only the issues it closed: those are followed the same way. Returns (rows with the chain's numbers added,
    the count of movements whose reference could not be resolved)."""
    known = {(a["repo"], a["number"]) for lst in asks_by_name.values() for a in lst}
    for name, number in (charters or {}).items():
        known.add(((ctx.orchestrators.get(name) or {}).get("repo"), number))
    is_stop = lambda repo, n: any(_same_repo(r, repo) and k == n for r, k in known)   # an ask or a charter: the chain ends there
    window = now - max(ctx.cfg.get("stalled_hours", 2.0), ctx.cfg.get("backlog_hours", 4.0)) * 3600   # older movements cannot change a figure
    out, unattributed = [], 0
    for row in rows:
        refs = row["refs"] if isinstance(row.get("refs"), list) else list(row["numbers"][1:])   # an old row: its numbers after the pull request are what it closed
        if not refs or row["at"] < window or not row.get("repo"):
            out.append(row)
            continue
        extra, stuck = [], False
        for ref in refs[:MOVES_NUMBERS_MAX]:
            if not isinstance(ref, int) or is_stop(row["repo"], ref):
                continue
            cur = ref
            for _ in range(PARENT_LEVELS):
                parent, resolved = _parent_of(ctx, state, budget, row["repo"], cur, now, problems)
                if not resolved:
                    stuck = True
                    break
                if not parent:
                    break
                extra.append(parent)
                cur = parent
                if is_stop(row["repo"], cur):
                    break
        unattributed += 1 if stuck else 0
        out.append(dict(row, numbers=list(row["numbers"]) + [n for n in extra if n not in row["numbers"]]) if extra else row)
    return out, unattributed


def attribute_decisions(ctx, state, budget, asks_by_name, charters, now, problems):
    """({orchestrator: waiting decisions}, how many could not be attributed, PR notices and holds, or None when decisions.json cannot be read). A decision belongs to the
    orchestrator whose open ask or charter is on the parent chain of its issue (up to PARENT_LEVELS levels, the same cached and budgeted reads);
    one that cannot be resolved belongs to nobody: the `agent` field of the list is never used."""
    read = decision_items(ctx.state_dir)
    if read is None:
        return None, 0, 0
    items, notices = read
    owner = {}
    for name, lst in asks_by_name.items():
        for ask in lst:
            owner.setdefault(("ask", ask["number"]), []).append((name, ask["repo"]))
    for name, number in (charters or {}).items():
        owner.setdefault(("ask", number), []).append((name, (ctx.orchestrators.get(name) or {}).get("repo")))
    counts, nobody = {}, 0
    for repo, number in items:
        found = None
        cur = number
        for level in range(PARENT_LEVELS + 1):
            if cur is None:
                break
            hits = [n for n, r in owner.get(("ask", cur), []) if _same_repo(r, repo)]
            if hits:
                found = sorted(hits)[0]
                break
            if level == PARENT_LEVELS or not repo:
                break
            parent, resolved = _parent_of(ctx, state, budget, repo, cur, now, problems)
            if not resolved or not parent:
                break
            cur = parent
        if found is None:
            nobody += 1
        else:
            counts[found] = counts.get(found, 0) + 1
    return counts, nobody, notices


def _refresh_asks(ctx, state, now, rows=()):
    """Re-read each asks list at most hourly, the oldest first, within each account's read budget; a failed read keeps the cached list and is told."""
    cache, problems = state.setdefault("asks", {}), []
    budget = _Budget(ctx, state, now)
    charters, more = _resolve_charters(ctx, state, now, budget)
    problems.extend(more)
    state["charter_of"] = {n: c for n, c in charters.items() if c is not None}
    state["charter_retired"] = sorted({a for e in (state.get("charters") or {}).values() if isinstance(e, dict) for a in e.get("retired") or []})   # only a read charter that says retired
    for name in sorted(ctx.orchestrators, key=lambda n: cache.get(n, {}).get("at", 0)):
        info = dict(ctx.orchestrators[name], charter=charters.get(name))
        entry = cache.get(name) or {}
        marked = stale_since(ctx.state_dir, info.get("repo"))
        stale = entry.get("at") and marked is not None and marked > entry["at"] and now - entry["at"] >= STALE_REREAD_SECONDS   # an event said it changed: not the hourly wait
        if (entry.get("at") and now - entry["at"] < ASKS_FRESH_SECONDS and not stale) or now - entry.get("tried", -1e9) < RETRY_SECONDS:
            if entry.get("error") and now - entry.get("tried", -1e9) < RETRY_SECONDS:
                problems.append(entry["error"])
            continue
        if not info.get("charter") or budget.paused(info.get("repo"), problems) or budget.left(budget.account(info.get("repo"))) < 1:
            continue
        if _time_up(ctx, "asks of %s" % name):
            continue
        try:
            found = budget.metered(info.get("repo"), 1, lambda: ctx.read_asks(info["charter"], info.get("repo")))
        except Exception as err:   # an unreadable source is told, not guessed
            text = "%s: asks not readable (%s)" % (name, str(err).splitlines()[0][:80] if str(err) else type(err).__name__)
            problems.append(text)
            cache[name] = dict(entry, tried=now, error=text)
            continue
        seen = {(a["repo"], a["number"]): a["first_seen"] for a in entry.get("list", [])}
        cache[name] = {"at": now, "list": [dict({"repo": a["repo"], "number": a["number"], "first_seen": seen.get((a["repo"], a["number"]), now)}, **_wait_fields(a)) for a in found]}
    for gone in [n for n in cache if n not in ctx.orchestrators]:
        del cache[gone]
    asks_by_name = {n: (cache.get(n) or {}).get("list") or [] for n in ctx.orchestrators}
    charter_numbers = state.get("charter_of") or {}
    rows, unattributed = expand_rows(ctx, state, budget, list(rows), asks_by_name, charter_numbers, now, problems)
    state["unattributed"] = unattributed
    counts, nobody, notices = attribute_decisions(ctx, state, budget, asks_by_name, charter_numbers, now, problems)
    state["decision_counts"], state["decisions_unattributed"], state["pr_notices"] = counts, nobody, notices
    state["cloud_unplaced"] = int(getattr(ctx, "cloud_unplaced", 0) or 0)   # #1097: live cloud workers whose boss is not an orchestrator lane (or has none)
    held = held_prs(ctx.state_dir)
    state["held_links"] = {k: sorted(v) for k, v in resolve_held_links(ctx, state, budget, held, now, problems).items()}
    state["reads"] = budget.to_state()
    return problems, rows


def tick(ctx):
    """One pass. Returns the list of (orchestrator, wake text) sent. Never raises for a source it cannot read."""
    now, state = ctx.now, _read_state(ctx.state_dir)
    before_state = json.loads(json.dumps(state))
    rows = read_moves(ctx.state_dir, now - 24 * 3600)
    audited = audit_moves(ctx.state_dir, now - 24 * 3600)
    rows += audited or []
    problems, rows = _refresh_asks(ctx, state, now, rows)
    held, reminders = held_prs(ctx.state_dir), list(getattr(ctx, "reminders", None) or [])
    unresolved = list(getattr(ctx, "unresolved_held", None) or [])
    if getattr(ctx, "notices_unread", 0):
        unresolved.append("noticed PRs without facts")   # #796: a PR the notice reader skipped or could not read has no row to look at: its links are unknown
    ctx.links_complete = not unresolved
    hold_back = bool(getattr(ctx, "links_gate", False) and unresolved)   # #796: after a start, no alert while a human-tier held PR's links are not whole
    if audited is None:
        problems.append("movement: the audit log cannot be read")
    waiting = state.get("decision_counts")
    if waiting is None:
        problems.append("decisions: decisions.json cannot be read")
    day, hour = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"), datetime.fromtimestamp(now, timezone.utc).strftime("%H")
    series, sent, episodes, latest = state.get("series", {}), [], state.get("episodes", {}), {}
    unchartered = []
    for name in sorted(ctx.orchestrators):
        if name in (state.get("charter_retired") or []):
            continue   # its charter says retired: no figures and no row
        info, entry = ctx.orchestrators[name], (state.get("asks") or {}).get(name)
        asks = None
        if entry and now - entry.get("at", 0) < ASKS_STALE_SECONDS:
            asks = entry["list"]
        elif (state.get("charter_of") or {}).get(name):
            problems.append("%s: no recent list of its open asks" % name) if (name + ": ") not in " ".join(problems) else None
        else:
            unchartered.append(name)
        if asks:
            names = {name} | set(info.get("workers") or [])
            serving = serving_waits(asks, held, state.get("held_links") or {}, reminders, names, now, ctx.cfg["stalled_hours"])
            asks = [dict(a, via_pr=serving[(a["repo"], a["number"])]) if serving.get((a["repo"], a["number"])) in ("owner_pr", "cos_pr")
                    else dict(a, timed=True) if serving.get((a["repo"], a["number"])) == "time" else a for a in asks]
        fig = figures(name, {"asks": asks, "busy": info["busy"], "idle": info["idle"], "messages": info["messages"], "cloud": info.get("cloud") or 0,
                             "decisions": None if waiting is None else waiting.get(name, 0),
                             "charter": (state.get("charter_of") or {}).get(name), "repo": info.get("repo")}, rows, now, ctx.cfg)
        latest[name] = fig
        old = series.get(name)
        if isinstance(old, dict) and old.get("day") != day and old.get("hours"):   # a new UTC day: the finished day stays replayable for a few days
            history = state.setdefault("history", {})
            history.setdefault(old["day"], {})[name] = old["hours"]
            for gone in sorted(history)[:-HISTORY_DAYS]:
                del history[gone]
        mine = old if isinstance(old, dict) and old.get("day") == day else {"day": day, "hours": {}}
        mine["hours"][hour] = {k: fig[k] for k in ("open_asks", "active_asks", "waiting_owner", "waiting_owner_pr", "waiting_time", "waiting_cos", "waiting_live", "parked", "stalled", "stalled_asks", "moved_1h", "moved_backlog", "charter_moves_1h", "busy", "idle", "cloud", "messages", "decisions", "oldest_stall_hours")}
        series[name] = mine
        before = episodes.get(name)
        text, episodes[name] = judge(fig, ctx.cfg, before)
        if hold_back:
            text, episodes[name] = None, before   # nothing judged: the episode memory is exactly as it was
        if text and ctx.cfg["enabled"] and ctx.cos:
            severity, reasons = crossing(fig, ctx.cfg)
            ctx.alert = {"agent": name, "state": "%s|%s" % (severity, ",".join(sorted(reasons)))}   # what the alert is ABOUT, for the daemon's repeat rule
            ctx.wake(text)
            sent.append((name, text))
        elif text:
            episodes[name] = before   # not sent (off, or no Chief of Staff to tell): the episode is still open
    if unchartered:   # no charter issue is recorded in its fleet metadata: its asks cannot be found, said once for all of them
        problems.append("no charter recorded for %s" % ", ".join(unchartered[:6]) + (" and %d more" % (len(unchartered) - 6) if len(unchartered) > 6 else ""))
    state.pop("at", None)
    if hold_back:
        state["warming"] = WAITING_LINKS
    else:
        state.pop("warming", None)
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
    waiting = [state["warming"]] if state.get("warming") in (WAITING_LINE, WAITING_LINKS) else []
    if not isinstance(latest, dict) or not latest:
        return waiting
    num = lambda value: "not recorded" if value is None else str(value)
    out = waiting + ["load        %d orchestrator(s); alert %s" % (len(latest), "on" if state.get("alert") else "off")]
    for name in sorted(latest):
        f = latest[name]
        out.append("  %-12s asks %s, stalled %s, waiting on owner %s, parked %s, waiting on cos %s, waiting live %s, oldest stall %s h, moved 1h %s, charter moves 1h %s; waiting %d msg %s dec; coders %d busy %d idle%s" % (
            name, num(f["open_asks"]), num(f["stalled"]), num(f.get("waiting_owner")), num(f.get("parked")), num(f.get("waiting_cos")), num(f.get("waiting_live")),
            num(f["oldest_stall_hours"]), num(f["moved_1h"]), num(f.get("charter_moves_1h")), f["messages"], num(f["decisions"]), f["busy"], f["idle"], (" +%d\u2601 cloud" % f["cloud"]) if f.get("cloud") else ""))
        if f.get("waiting_owner_pr") or f.get("waiting_time"):
            out[-1] += "; " + waiting_phrase(f)
    if state.get("cloud_unplaced"):
        out.append("  %d live cloud worker(s) with no orchestrator lane (no boss, or a boss that is not an orchestrator)" % state["cloud_unplaced"])
    if state.get("decisions_unattributed"):
        out.append("  %d decision(s) not attributed (the parent of their issue could not be read, or no orchestrator's charter is on its chain)" % state["decisions_unattributed"])
    if state.get("pr_notices"):
        out.append("  PR notices and holds %d (heads-ups and held pull requests: information, never counted as waiting)" % state["pr_notices"])
    if state.get("unattributed"):
        out.append("  %d movement(s) not attributed (a referenced issue's parent could not be read)" % state["unattributed"])
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
