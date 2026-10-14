"""triage_digest: the stale digest's memory and rules (#349). Pure rules plus three small private files.

Each stale finding surfaces once, then again only on a backoff (1 day after the first showing, 3 after the second, 7 after the
third; from the fourth on, weekly) or when the item changed (a comment, a push, a label or a state change). Parked items (a parked
label with a valid date still ahead) and items a person marked 'seen, leave' are skipped; a parked label with no valid date is
flagged once. A lost or corrupt memory file means nothing was seen: every item surfaces once, never silence.

Files in the state directory (all private, mode 0600, bounded): triage-seen.json (what surfaced and when), triage-mute.json
(the 'seen, leave' list, written by `fleet-switchboard triage`) and triage-cost.json (characters delivered per day).
Nothing here reads or writes GitHub. Python 3.9+, standard library only; loaded by path from beside bin/fleet-switchboard.
"""

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

SEEN_FILE = "triage-seen.json"
MUTE_FILE = "triage-mute.json"
COST_FILE = "triage-cost.json"
BACKOFF_DAYS = (1, 3, 7)   # after the 1st, 2nd and 3rd showing
WEEKLY_DAYS = 7
SEEN_MAX = 5000
MUTE_MAX = 1000
COST_DAYS = 14
REF_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_json(path):
    """A JSON object from `path`, or None when missing, unreadable or not an object."""
    try:
        raw = json.loads(open(path).read())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def write_json(path, body):
    """Private atomic write; returns False when the disk refused (a digest never fails for that)."""
    text = json.dumps(body, sort_keys=True) + "\n"
    try:
        if os.path.exists(path) and open(path).read() == text:
            return True
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".triage-")
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def sig(item):
    """What a change is: the last update, the labels and the state."""
    if item is None:
        return ""
    return "%d|%s|%s" % (int(item["updated"]), ",".join(sorted(str(l) for l in item["labels"])), item["state"])


def load_seen(state_dir):
    raw = read_json(os.path.join(state_dir, SEEN_FILE)) if state_dir else None
    items = raw.get("items") if raw else None
    seen = {}
    if isinstance(items, dict):   # corrupt or foreign shapes are dropped: those items simply surface once more
        for key, rec in items.items():
            if isinstance(key, str) and isinstance(rec, dict) and _number(rec.get("at")) and isinstance(rec.get("n"), int) \
                    and not isinstance(rec.get("n"), bool) and isinstance(rec.get("sig"), str):
                seen[key] = {"sig": rec["sig"], "at": float(rec["at"]), "n": max(1, rec["n"]), "flagged": bool(rec.get("flagged"))}
    return seen


def load_meta(state_dir):
    """The digest's cadence memory, kept in the same file: {"known", "swept", "last"}. A missing or corrupt file is NOT known: the digest
    warms up again. A file written before the cadence existed has no `swept` and counts as swept (an upgrade does not re-warm)."""
    raw = read_json(os.path.join(state_dir, SEEN_FILE)) if state_dir else None
    if raw is None or not isinstance(raw.get("items"), dict):
        return {"known": False, "swept": False, "last": None}
    last = raw.get("last")
    return {"known": True, "swept": bool(raw.get("swept", True)), "last": float(last) if _number(last) else None}


def write_seen(state_dir, seen, meta=None):
    if state_dir:
        meta = meta or {"swept": True, "last": None}
        write_json(os.path.join(state_dir, SEEN_FILE), {"v": 1, "items": seen, "swept": bool(meta.get("swept")), "last": meta.get("last")})


WARMUP_MAX_SECONDS = 1800   # a sweep that cannot finish (a list that never reads) ends its warm-up after this: the digest must never stay silent for good


def gate(meta, now, unchecked, interval_seconds, warming_since=None):
    """(may a digest go out now, the state `status` shows). Warm-up: nothing goes out while any finding is unchecked; the first pass with none
    ends it (`meta["swept"]` is set, the caller saves it). Then at most one digest per `interval_seconds` from the last delivered one
    (0 = no cap). A held digest drops nothing: the caller leaves its memory untouched, so the same items are due next time."""
    if not meta["swept"]:
        if unchecked > 0 and not (warming_since is not None and now - warming_since >= WARMUP_MAX_SECONDS):
            return False, {"state": "warming", "unchecked": int(unchecked)}
        meta["swept"] = True
    if interval_seconds > 0 and meta["last"] is not None and now - meta["last"] < interval_seconds:
        return False, {"state": "held", "next_at": meta["last"] + interval_seconds}
    return True, {"state": "ready"}


def digest_line(digest, now, interval_minutes=60):
    """The `status` line for the digest's cadence, or None."""
    if not isinstance(digest, dict):
        return None
    state = digest.get("state")
    if state == "warming":
        return "digest warming up: %d unchecked; nothing is delivered until every finding has been checked once" % int(digest.get("unchecked") or 0)
    if state == "held" and _number(digest.get("next_at")):
        return "digest: the next digest may go out in %s (at most one per %s)" % (age_text(max(0, int(digest["next_at"] - now))), interval_text(interval_minutes))
    if state == "ready":
        return "digest ready: one goes out when something qualifies (at most one per %s)" % interval_text(interval_minutes)
    return None


def interval_text(minutes):
    minutes = int(minutes) if _number(minutes) else 60
    return "hour" if minutes == 60 else "%d minutes" % minutes if minutes % 60 else "%d hours" % (minutes // 60)


def age_text(seconds):
    return "%ds" % seconds if seconds < 120 else "%dm" % (seconds // 60) if seconds < 7200 else "%dh" % (seconds // 3600)


def load_mutes(state_dir):
    """{ref: epoch it was marked} (a malformed entry is ignored)."""
    raw = read_json(os.path.join(state_dir, MUTE_FILE)) if state_dir else None
    muted = raw.get("muted") if raw else None
    if not isinstance(muted, dict):
        return {}
    return {ref: float(rec["at"]) for ref, rec in muted.items() if isinstance(rec, dict) and _number(rec.get("at"))}


def mute_command(state_dir, action, ref, now):
    """seen-leave | unmute | muted -> (exit code, text). A mute holds while the item has not changed since `now`."""
    muted = load_mutes(state_dir)
    if action == "muted":
        lines = ["%s  seen, leave since %s" % (r, datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                 for r, at in sorted(muted.items())]
        return 0, "\n".join(lines) or "no item is muted"
    if not REF_RE.fullmatch(ref or ""):
        return 2, "the item must be OWNER/REPO#N, got %r" % (ref,)
    if action == "seen-leave":
        if ref not in muted and len(muted) >= MUTE_MAX:
            return 2, "%d items are muted already; unmute some first" % len(muted)
        muted[ref] = now
        text = "%s: muted until it changes" % ref
    else:
        text = "%s: %s" % (ref, "unmuted" if muted.pop(ref, None) is not None else "was not muted")
    if not write_json(os.path.join(state_dir, MUTE_FILE), {"v": 1, "muted": {r: {"at": at} for r, at in sorted(muted.items())}}):
        return 1, "could not write the mute file"
    return 0, text


def select(seen, items, mutes, ours, now, backoff=True, alarmed=None, realarm_seconds=86400.0, parked_max_days=366):
    """([(rule, ref)] to show in this pass's digest, [ref] parked without a valid date and not flagged before).
    `items` maps ref to {"updated", "labels", "state", "parked_until"}. `backoff` false is the fixed re-alarm."""
    alarmed = alarmed or {}
    due, invalid = [], []
    for rule, ref in ours:
        item = items.get(ref)
        if ref in mutes and (item is None or item["updated"] <= mutes[ref]):
            continue   # 'seen, leave': quiet until the item changes
        rec = seen.get("%s %s" % (rule, ref))
        if item is not None:
            until = item.get("parked_until")
            awaiting = any("awaiting" in str(l) for l in item["labels"])
            if until is not None and now < until <= now + parked_max_days * 86400.0 and not awaiting:
                continue   # parked until a date still ahead
            if until is None and any(str(l).endswith(":parked") for l in item["labels"]) and "PARKED " + ref not in seen:
                if ref not in invalid:
                    invalid.append(ref)   # flagged once; the item itself is judged as not parked and listed like any other
        if not backoff:
            if now - alarmed.get((rule, ref), 0) >= realarm_seconds:
                due.append((rule, ref))
        elif rec is None or rec["sig"] != sig(item):
            due.append((rule, ref))
        elif rec["n"] <= len(BACKOFF_DAYS):
            if now - rec["at"] >= BACKOFF_DAYS[rec["n"] - 1] * 86400:
                due.append((rule, ref))
        elif now - rec["at"] >= WEEKLY_DAYS * 86400:
            due.append((rule, ref))
    return sorted(due), sorted(invalid)


def commit(seen, items, shown, invalid, now):
    """Remember what the digest listed (`shown`, only those), and that the invalid parks were flagged; bound the memory."""
    for rule, ref in shown:
        key, current = "%s %s" % (rule, ref), sig(items.get(ref))
        old = seen.get(key)
        same = old is not None and old["sig"] == current
        seen[key] = {"sig": current, "at": now, "n": min(old["n"] + 1, len(BACKOFF_DAYS) + 1) if same else 1,
                     "flagged": bool(old and old["flagged"])}
    for ref in invalid:
        seen["PARKED " + ref] = {"sig": "", "at": now, "n": 1, "flagged": True}   # the once-only flag
    for key in sorted(seen, key=lambda k: seen[k]["at"])[:max(0, len(seen) - SEEN_MAX)]:
        del seen[key]


def prune(seen, candidates):
    """Forget items that are no longer findings: one that returns is new and surfaces once."""
    live = {"%s %s" % pair for pair in candidates} | {"PARKED " + ref for _, ref in candidates}
    for key in [k for k in seen if k not in live]:
        del seen[key]


def day_of(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


def cost_read(state_dir):
    return read_json(os.path.join(state_dir, COST_FILE)) if state_dir else None


def cost_record(state_dir, now, text):
    """Count one delivered message and its characters for today."""
    if not state_dir or not isinstance(text, str):
        return
    raw = cost_read(state_dir) or {}
    days = raw.get("days") if isinstance(raw.get("days"), dict) else {}
    today = day_of(now)
    row = days.get(today) if isinstance(days.get(today), dict) else {}
    days[today] = {"messages": int(row.get("messages", 0)) + 1, "chars": int(row.get("chars", 0)) + len(text)}
    for old in sorted(days)[:-COST_DAYS]:
        del days[old]
    since = raw.get("since") if isinstance(raw.get("since"), str) else today
    write_json(os.path.join(state_dir, COST_FILE), {"v": 1, "since": since, "days": days})


def cost_lines(cost, today):
    """Seven lines ending `today` (YYYY-MM-DD): characters delivered and tokens estimated at chars/4. A day before the first
    record, or no record at all, is 'not recorded'."""
    try:
        end = datetime.strptime(today, "%Y-%m-%d")
    except (TypeError, ValueError):
        return ["      triage     cost per day: not recorded"]
    since = cost.get("since") if isinstance(cost, dict) else None
    days = cost.get("days") if isinstance(cost, dict) and isinstance(cost.get("days"), dict) else {}
    lines = []
    for back in range(6, -1, -1):
        day = (end - timedelta(days=back)).strftime("%Y-%m-%d")
        if not isinstance(since, str) or day < since:
            lines.append("      triage     cost %s: not recorded" % day)
            continue
        row = days.get(day) if isinstance(days.get(day), dict) else {}
        chars = int(row.get("chars", 0))
        lines.append("      triage     cost %s: %d message(s), %d chars, ~%d tokens (chars/4)" % (day, int(row.get("messages", 0)), chars, chars // 4))
    return lines
