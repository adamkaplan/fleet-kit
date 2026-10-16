"""Ownerless, quiet issues of a team repository (#605, part of the triage).

Triage calls an issue `ours` when a fleet account wrote it, is assigned to it, or has a pull request notice. Every other issue of a team repository is an
`other-owner` flag, counted apart and never a finding, so dozens of issues nobody owns and nobody touched for days read as "found 0". This module says which
open ISSUES of such a repository have NO owner (the one rule T1 uses: `is_claimed`), carry no owner label, were not written by the fleet and have been quiet
for `days` or more, and builds the one daily line about them (counts, the age of the oldest, links: no issue text) and the per-repository breakdown of what
the triage could not check, and why.

Pure: plain values in, plain values out. No file, GitHub or agent.
"""
import fnmatch

DEFAULT_OWNER_LABELS = ("agent:*", "owner:*", "crew:*")
DEFAULT_DAYS = 3
DEFAULT_LIMIT = 500
OLDEST = 5
REASONS = ("call budget", "failed read", "not read yet")


def is_claimed(item, ctx):
    """The ONE definition of an issue with an owner that T1 and this summary share: assigned, below a charter, named by an `Owner:` line, owned by a running agent, or
    whose owner cannot be told yet (its parent chain leaves the issues read). `ctx` is the triage context of the repository."""
    number = item["number"]
    return bool(item["assignees"] or number in ctx["chain"] or item.get("owner_body", False) or number in ctx["owned"] or number in ctx["unresolved"])


def claim_label(labels, patterns):
    """True when one of `labels` matches a configured owner-label pattern (`agent:*`, an exact name)."""
    return any(fnmatch.fnmatchcase(str(label).lower(), str(pattern).lower()) for label in labels for pattern in patterns)


def ownerless_stale(items, ctx, ours_logins, patterns, now, days=DEFAULT_DAYS):
    """[(number, quiet_days)] of the open ISSUES (not pull requests) that have no owner, no owner label, were not written by one of `ours_logins`, and were last touched `days`
    or more days ago; the oldest first."""
    found = []
    for number, item in items.items():
        if item.get("pr") or item.get("state") != "open":
            continue
        if is_claimed(item, ctx) or claim_label(item["labels"], patterns) or item["author"] in ours_logins:
            continue
        quiet = (now - item["updated"]) / 86400.0
        if quiet >= days:
            found.append((number, round(quiet, 1)))
    return sorted(found, key=lambda row: (-row[1], row[0]))


def summary_line(repo, rows, read, total, days=DEFAULT_DAYS, cut=False):
    """The ONE line about a repository: how many, how old the oldest is, the five oldest as links, and how much of the list was read. Counts, ages and links only: no title, no text.
    When the list was CUT the read took the least recently updated first, so the count is a lower bound (`at least N`) and the oldest are the true oldest."""
    links = ", ".join("https://github.com/%s/issues/%d" % (repo, number) for number, _ in rows[:OLDEST])
    return "ownerless in %s: %s%d open issue(s) with no owner and no activity for %d day(s) or more; oldest quiet for %d day(s); oldest %d: %s; read %s of %s open item(s)%s" % (
        repo, "at least " if cut else "", len(rows), days, int(rows[0][1]), min(OLDEST, len(rows)), links, read,
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
