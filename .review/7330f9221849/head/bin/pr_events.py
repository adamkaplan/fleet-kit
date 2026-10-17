"""`pr-state` from the forwarder's webhook events (#354, part of #130).

The picture `pr-state` shows (a map "owner/repo#N" -> entry) is kept by the daemon from the open-PR lists it reads and from examinations. A `pull_request`
webhook event the forwarder already delivers says the same things the moment they happen: a PR opened, a push, a label, a draft conversion, a close or a
merge. This module turns one verified event into the entry for its PR, with no GitHub call.

Pure: it reads plain values and returns plain values; it touches no file, GitHub or agent. The daemon's hub keeps the entries it makes, and a pass folds
them into the picture (`fold`).
"""

ACTIONS = ("opened", "reopened", "synchronize", "ready_for_review", "converted_to_draft", "labeled", "unlabeled", "closed", "edited")
KEPT_FROM_EXAMINATION = ("checks", "ci", "mergeable", "gates", "examined_at", "examined_head")   # what an examination found at the head the entry still has
MAX_LABELS = 20


def _plain(value, limit):
    """`value` as one bounded line without control characters, or None when it is not text. Label names are set by anyone who can label."""
    if not isinstance(value, str):
        return None
    return " ".join("".join(" " if ord(c) < 32 or 127 <= ord(c) < 160 else c for c in value).split())[:limit]


def _is_number(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def entry_for(old, event, payload, now):
    """(number, entry) for a verified `pull_request` event, or None when the event is not about a PR this picture keeps (another event, an action it
    does not use, a payload without a PR number). `old` is the entry already held for it, or None. The entry has the shape the list pass writes
    (`state`, `head`, `labels`, `at`, `listed_at`); a head that changed drops what an examination had found."""
    if event != "pull_request" or not isinstance(payload, dict) or payload.get("action") not in ACTIONS:
        return None
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        return None
    number = pr.get("number") if _is_number(pr.get("number")) else payload.get("number")
    if not _is_number(number):
        return None
    head = (pr.get("head") or {}).get("sha") if isinstance(pr.get("head"), dict) else None
    head = _plain(head, 40) if isinstance(head, str) and head else (old or {}).get("head")
    raw = pr.get("labels") if isinstance(pr.get("labels"), list) else None
    if raw is None:
        labels = list((old or {}).get("labels") or [])
    else:
        names = ((l.get("name") if isinstance(l, dict) else l) for l in raw[:MAX_LABELS])
        labels = sorted({n for n in (_plain(name, 60) for name in names) if n})
    if payload["action"] == "closed" or pr.get("state") == "closed":
        state = "merged" if pr.get("merged") is True else "closed"
    else:
        state = "draft" if pr.get("draft") is True else "open"
    entry = {"state": state, "head": head, "labels": labels, "at": now, "listed_at": (old or {}).get("listed_at", now), "via": "event"}
    if state in ("merged", "closed"):
        entry["why"] = "%s by a webhook event" % state
    if old and old.get("head") == head and old.get("checks") is not None:
        entry.update({k: old[k] for k in KEPT_FROM_EXAMINATION if k in old})
    return number, entry


def fold(picture, events, bound):
    """Merge the hub's `events` ({ref: entry}) into `picture` ({ref: entry}): an event entry replaces what the picture holds when it is newer
    (`at`), and keeps the examination of the same head. The picture is cut to its `bound` newest. Returns how many entries changed."""
    changed = 0
    for ref, entry in (events or {}).items():
        old = picture.get(ref)
        if old is not None and old.get("at", 0) >= entry.get("at", 0):
            continue
        merged = dict(entry)
        if old and old.get("head") == entry.get("head") and old.get("checks") is not None:
            merged.update({k: old[k] for k in KEPT_FROM_EXAMINATION if k in old})
        merged["listed_at"] = max(entry.get("listed_at", 0), (old or {}).get("listed_at", 0))
        picture[ref] = merged
        changed += 1
    for ref in sorted(picture, key=lambda r: picture[r].get("at", 0))[:max(0, len(picture) - bound)]:
        picture.pop(ref, None)
    return changed
