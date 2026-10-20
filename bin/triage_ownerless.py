"""Ownerless, quiet issues of a team repository (#605, part of the triage).

Triage calls an issue `ours` when a fleet account wrote it, is assigned to it, or has a pull request notice. Every other issue of a team repository is an
`other-owner` flag, counted apart and never a finding, so dozens of issues nobody owns and nobody touched for days read as "found 0". This module says which
open ISSUES of such a repository have NO owner (the one rule T1 uses: `is_claimed`), carry no owner label, were not written by the fleet and have been quiet
for `days` or more, and builds the one daily line about them (counts, the age of the oldest, links, how many are parked until a future date and so left out: no issue text) and the per-repository breakdown of what
the triage could not check, and why.

Pure: plain values in, plain values out. No file, GitHub or agent.
"""
import fnmatch

DEFAULT_OWNER_LABELS = ("agent:*", "owner:*", "crew:*")
DEFAULT_DAYS = 3
DEFAULT_LIMIT = 500
OLDEST = 5
REASONS = ("call budget", "failed read", "not read yet")


def is_claimed(item, ctx, lanes=None):
    """The ONE definition of an issue with an owner that T1 and this summary share: assigned, below a charter, named by an `Owner:` line, owned by a running agent, or
    whose owner cannot be told yet (its parent chain leaves the issues read). `ctx` is the triage context of the repository. `lanes` (None for T1 and for a repository with no
    label map) makes the `Owner:` line count only when its first name is one of those known lanes."""
    number = item["number"]
    named = item.get("owner_body", False) if lanes is None else lane_named(item, lanes)
    return bool(item["assignees"] or number in ctx["chain"] or named or number in ctx["owned"] or number in ctx["unresolved"])


def claim_label(labels, patterns):
    """True when one of `labels` matches a configured owner-label pattern (`agent:*`, an exact name)."""
    return any(fnmatch.fnmatchcase(str(label).lower(), str(pattern).lower()) for label in labels for pattern in patterns)


def label_owned(labels, label_owners):
    """True when a label of the issue is a key of the repository's `label_owners` map (a lane carries it)."""
    keys = {str(k).lower() for k in (label_owners or {})}
    return any(str(label).lower() in keys for label in labels)


def lane_named(item, lanes):
    """True when the first `Owner:` line of the body names a known lane (`lanes`: lower-case orchestrator names)."""
    name = item.get("owner_name")
    return bool(name) and name.lower() in lanes


def ownerless_split(items, ctx, ours_logins, patterns, now, days=DEFAULT_DAYS, label_owners=None, lanes=(), parked_now=None):
    """([listed], [parked]) rows `(number, quiet_days)`, the oldest first, of the open ISSUES (not pull requests) that have no owner, no owner label, were not written by one of
    `ours_logins`, and were last touched `days` or more days ago. With a non-empty `label_owners` map for the repository, a label of the map owns the issue and an `Owner:` line owns it
    only when it names one of `lanes`; with none, the rule is the one before the map existed. `parked_now(item, now)` is the daemon's ONE reading of "parked until a future date"
    (None: nothing is parked): an issue that would be listed and is parked moves to the second list. This is the only place the parked rule lives; every count, age and link of the
    line derives from the first list and the parked total from the second."""
    found, parked, strict = [], [], bool(label_owners)
    for number, item in items.items():
        if item.get("pr") or item.get("state") != "open":
            continue
        if strict and label_owned(item["labels"], label_owners):
            continue
        if is_claimed(item, ctx, lanes if strict else None) or claim_label(item["labels"], patterns) or item["author"] in ours_logins:
            continue
        quiet = (now - item["updated"]) / 86400.0
        if quiet >= days:
            (parked if parked_now is not None and parked_now(item, now) else found).append((number, round(quiet, 1)))
    order = lambda rows: sorted(rows, key=lambda row: (-row[1], row[0]))
    return order(found), order(parked)


def ownerless_stale(items, ctx, ours_logins, patterns, now, days=DEFAULT_DAYS, label_owners=None, lanes=(), parked_now=None):
    """[(number, quiet_days)] the listed rows of `ownerless_split` (the oldest first)."""
    return ownerless_split(items, ctx, ours_logins, patterns, now, days, label_owners, lanes, parked_now)[0]


def summary_line(repo, rows, read, total, days=DEFAULT_DAYS, cut=False, parked=0):
    """The ONE line about a repository: how many, how old the oldest is, the five oldest as links, and how much of the list was read. Counts, ages and links only: no title, no text.
    When the list was CUT the read took the least recently updated first, so the count is a lower bound (`at least N`) and the oldest are the true oldest."""
    links = ", ".join("https://github.com/%s/issues/%d" % (repo, number) for number, _ in rows[:OLDEST])
    return "ownerless in %s: %s%d open issue(s) with no owner and no activity for %d day(s) or more; oldest quiet for %d day(s); oldest %d: %s; parked %d; read %s of %s open item(s)%s" % (
        repo, "at least " if cut else "", len(rows), days, int(rows[0][1]), min(OLDEST, len(rows)), links, parked, read,
        total if total is not None else "an unknown number", ", least recently updated first" if cut else "")


def reasons_line(repo, reasons, other_owner=0, oldest_seconds=None):
    """The report line of what was not checked for `repo`, with a reason per count (`reasons` {reason: n}); the other-owner share counted apart."""
    parts = ["%d %s" % (reasons[r], r) for r in REASONS if reasons.get(r)]
    if not parts:
        return None
    line = "not checked for %s: %s" % (repo, ", ".join(parts))
    if other_owner:
        line += " (%d of them other-owner flags)" % other_owner
    if oldest_seconds:
        line += "; oldest unchecked for %d hour(s)" % int(oldest_seconds // 3600)
    return line
