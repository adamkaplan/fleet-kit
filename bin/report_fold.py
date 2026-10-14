"""Folding a report into an earlier send (#352, part of #130).

A worker that messages its boss and then reports on the same issue would wake the boss twice for one piece of news. The
report is not dropped: it is delivered in full, as a note (no second wake), and its text says it follows the earlier message.

This module is pure: it reads and returns plain values and touches no file, GitHub or agent. The daemon's `send` records
what it sent (`record`), and `report` asks `follows` before it plans the delivery.
"""

WINDOW_SECONDS = 600   # a report within this long after a delivered send, to the same boss on the same issue, folds into it
KEEP = 8               # recipients remembered per sender


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def record(entry, recipient, repo, issue, now):
    """`entry` (one sender's record) with its last send to `recipient` on `repo`#`issue` at `now`. A send naming no issue is not recorded:
    there is nothing for a report to fold into. At most KEEP recipients are kept (the oldest go)."""
    entry = dict(entry or {})
    if not isinstance(issue, int) or isinstance(issue, bool):
        return entry
    seen = {k: v for k, v in (entry.get("send_on") or {}).items() if isinstance(k, str) and isinstance(v, dict) and _number(v.get("at"))}
    seen[recipient] = {"repo": repo if isinstance(repo, str) else None, "issue": issue, "at": int(now)}
    for old in sorted(seen, key=lambda name: seen[name]["at"])[:max(0, len(seen) - KEEP)]:
        seen.pop(old, None)
    entry["send_on"] = seen
    return entry


def follows(entry, recipient, repo, issue, now, window=WINDOW_SECONDS):
    """When the earlier send this report folds into was made (epoch seconds), or None: there is none to the same boss on the same issue
    within `window` seconds. A repo recorded on both sides must match; a missing one does not exclude."""
    if not isinstance(issue, int) or isinstance(issue, bool):
        return None
    found = ((entry or {}).get("send_on") or {}).get(recipient)
    if not isinstance(found, dict) or found.get("issue") != issue or not _number(found.get("at")):
        return None
    if found.get("repo") and repo and found["repo"] != repo:
        return None
    age = now - found["at"]
    return found["at"] if 0 <= age <= window else None


def marked(summary, sent_at, now):
    """The report line with a mark that it follows the earlier message and wakes nobody again; its state stays first (`question: ...`)."""
    minutes = max(0, int((now - sent_at) // 60))
    return "%s [follows its message %d min ago]" % (summary, minutes)
