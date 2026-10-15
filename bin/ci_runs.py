"""The NEWEST run of each check decides the CI verdict (ask #534).

gh's `statusCheckRollup` lists EVERY run at a pull request's head, so a red run that was re-run green stays in the list beside its green
re-run. Reading "any entry failed" made such a head look failing for ever (in `pr-state`, the gate's checks row and the notices). `newest`
keeps, for each check, only its newest run; every reader of the rollup goes through it.

- A check is identified by its workflow and its name (a status context: its context, no workflow): two workflows that each have a check
  named `test` are two checks, and a red one is never hidden by the other's green.
- The newest run is the one with the latest completion time (the start time while it has not completed: gh gives an unfinished run the
  zero date as its completion time). An entry with no time is OLDER than one with a time, except a run that has not completed: with no
  time it is taken as the newest (a missing time never lets an older green hide a pending run).
- It never makes a verdict greener by guessing: entries that tie for newest are ALL kept (so any failing one still fails), an entry that
  names no check is its own check, and an entry that cannot be read is kept as it is.
"""

from datetime import datetime, timezone

_ZERO_YEAR = 1   # gh's "no completion time" is 0001-01-01T00:00:00Z


def _time(text):
    """Epoch seconds of an ISO time gh wrote (`2026-10-15T16:16:00Z`), or None when there is none (absent, unreadable, or gh's zero date)."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return None if moment.year <= _ZERO_YEAR else moment.timestamp()


def _rank(entry):
    """How new this run is: its completion time, else its start time; a run that has not completed and has no time at all is the newest
    (cautious); a completed run with no time is older than any with a time."""
    stamp = _time(entry.get("completedAt"))
    if stamp is None:
        stamp = _time(entry.get("startedAt"))
    if stamp is not None:
        return stamp
    return float("inf") if entry.get("status") not in (None, "COMPLETED") else float("-inf")


def _identity(entry, position):
    name = entry.get("name") or entry.get("context")
    if not isinstance(name, str) or not name:
        return ("unnamed", position)   # names no check: its own check, never merged with another
    workflow = entry.get("workflowName")
    return (workflow if isinstance(workflow, str) else "", name)


def newest(rollup):
    """`rollup` (gh's statusCheckRollup) with only the newest run of each check, in the original order. Anything that is not a list gives []; an
    entry that is not a dict is dropped (as every reader of the rollup already skipped it)."""
    if not isinstance(rollup, list):
        return []
    groups = {}
    for position, entry in enumerate(rollup):
        if isinstance(entry, dict):
            groups.setdefault(_identity(entry, position), []).append((position, entry))
    keep = set()
    for runs in groups.values():
        best = max(_rank(entry) for _, entry in runs)
        keep.update(position for position, entry in runs if _rank(entry) == best)   # a tie keeps every tied run
    return [entry for position, entry in enumerate(rollup) if position in keep]
