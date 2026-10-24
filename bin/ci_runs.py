"""The NEWEST run of each check decides the CI verdict (ask #534).

gh's `statusCheckRollup` lists EVERY run at a pull request's head, so a red run that was re-run green stays in the list beside its green
re-run. Reading "any entry failed" made such a head look failing for ever (in `pr-state`, the gate's checks row and the notices). `newest`
keeps, for each check, only the run that decides; every reader of the rollup goes through it.

- A check is identified by its workflow and its name (a status context: its context, no workflow): two workflows that each have a check
  named `test` are two checks, and a red one is never hidden by the other's green.
- A check with ANY unfinished run is PENDING, whatever older completed runs say: the unfinished run (the newest started, if several) is
  the one kept. So an older run that finishes after a newer one started can never hide the newer, still running one.
- Among COMPLETED runs, the newest STARTED decides (the completion time stands in for a run with no start time): an older attempt that
  finishes late, even red, does not override a newer run that started after it. Runs that tie on start are ordered by completion (the one
  completing later); runs that still tie are ALL kept, so any failing one fails.
- An entry with no time at all is older than one with a time. An entry that names no check is its own check. Nothing is ever read greener
  by guessing.
"""

import re
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


def _unfinished(entry):
    """True for a run that has not completed: its status says so, or it is a status context still pending or expected."""
    if entry.get("status") not in (None, "COMPLETED"):
        return True
    return str(entry.get("conclusion") or entry.get("state") or "").upper() in ("PENDING", "EXPECTED") and not entry.get("conclusion")


def _started(entry):
    """When the run started (the completion time stands in when it has no start time), or -inf for an entry with no time at all."""
    stamp = _time(entry.get("startedAt"))
    if stamp is None:
        stamp = _time(entry.get("completedAt"))
    return float("-inf") if stamp is None else stamp


def _rank(entry):
    """The order of two COMPLETED runs of one check: newest started first, then the one completing later."""
    completed = _time(entry.get("completedAt"))
    return (_started(entry), float("-inf") if completed is None else completed)


def _identity(entry, position):
    name = entry.get("name") or entry.get("context")
    if not isinstance(name, str) or not name:
        return ("unnamed", position)   # names no check: its own check, never merged with another
    workflow = entry.get("workflowName")
    run = _run_id(entry)
    ident = _identity_of_run(entry, run) if run is not None else None   # cached: the stable identity, never the author's display name
    return (workflow if isinstance(workflow, str) else "", ident, name)


def _run_id(entry):
    """The workflow run id of a check run (from its detailsUrl `.../actions/runs/<id>/...`), or None (a status context, or no url)."""
    url = entry.get("detailsUrl")
    found = re.search(r"/actions/runs/(\d{1,12})(?:/|$)", url) if isinstance(url, str) else None
    return int(found.group(1)) if found else None


UNREADABLE = "IDENTITY_UNREADABLE"   # status given to a run whose workflow identity could not be read: never green (fails closed)
_CACHE = {}   # (owner/repo, run id) -> (workflow id, event) or None; a run's workflow never changes, so one bounded read per distinct run
READER = None   # tests (or a caller with its own gh runner) set a function (repo, run id) -> (workflow id, event) or None


def _read_run(repo, run):
    """ONE bounded read of a run: (workflow id, event) from `gh api repos/<repo>/actions/runs/<id>`, or None when it cannot be read."""
    import json
    import subprocess
    try:
        done = subprocess.run(["gh", "api", "repos/%s/actions/runs/%d" % (repo, run), "--jq", "{w: .workflow_id, e: .event}"],
                              capture_output=True, text=True, timeout=20)
        data = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if isinstance(data, dict) and isinstance(data.get("w"), int) and isinstance(data.get("e"), str):
        return (data["w"], data["e"])
    return None


def _identity_of_run(entry, run):
    """(workflow id, event) of the run behind `entry`, cached; None when unreadable (the caller fails closed)."""
    found = re.search(r"github\.com/([\w.-]+/[\w.-]+)/actions/runs/", entry.get("detailsUrl") or "")
    repo = found.group(1) if found else ""
    key = (repo, run)
    if key not in _CACHE:
        got = None
        try:
            got = (READER or _read_run)(repo, run) if repo else None
        except Exception:
            got = None
        if not (isinstance(got, tuple) and len(got) == 2):
            return None   # an unreadable identity is never cached as readable
        _CACHE[key] = got
    return _CACHE[key]


def latest_runs(rollup):
    """`rollup` without the checks of an OLDER run of a workflow. A workflow is its STABLE identity (the workflow id read from the run), never
    its display name (the PR author writes that: a second workflow could take the real one's name). Runs are grouped by (workflow id, event):
    a newer run of another event never hides a red older one (the fail-closed choice). The highest run id of a group counts (ids grow with
    creation). A run whose identity cannot be read is kept and marked UNREADABLE (never green). Entries with no run id (status contexts) are kept."""
    if not isinstance(rollup, list):
        return []
    known = []
    top = {}
    for entry in rollup:
        run = _run_id(entry) if isinstance(entry, dict) else None
        ident = _identity_of_run(entry, run) if run is not None else None
        known.append((run, ident))
        if ident is not None:
            top[ident] = max(top.get(ident, run), run)
    out = []
    for entry, (run, ident) in zip(rollup, known):
        if run is None:
            out.append(entry)
        elif ident is None:
            out.append(dict(entry, status=UNREADABLE, conclusion=""))
        elif run >= top[ident]:
            out.append(entry)
    return out


def _clean(text, limit=60):
    """A workflow name safe to show: control characters dropped, bounded, quoted."""
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(text))
    return '"%s"' % (text[:limit] + ("..." if len(text) > limit else ""))


def not_green_runs(rollup):
    """[(workflow, run id, state)] of the latest runs that are not complete and green, for a refusal that names them. The workflow name is
    author-controlled text: it comes out sanitised, quoted and length-limited (or "" for a status context)."""
    bad = []
    for e in newest(rollup):
        word = str(e.get("conclusion") or e.get("state") or "").upper()
        if _unfinished(e) or word not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            name = e.get("workflowName")
            bad.append((_clean(name) if isinstance(name, str) and name else "", _run_id(e), (word or str(e.get("status") or "pending")).lower()))
    return bad


def newest(rollup):
    """`rollup` (gh's statusCheckRollup) with only the run that decides for each check, in the original order. Anything that is not a list
    gives []; an entry that is not a dict is dropped (as every reader of the rollup already skipped it)."""
    if not isinstance(rollup, list):
        return []
    rollup = latest_runs(rollup)   # only the latest run of each workflow counts
    groups = {}
    for position, entry in enumerate(rollup):
        if isinstance(entry, dict):
            groups.setdefault(_identity(entry, position), []).append((position, entry))
    keep = set()
    for runs in groups.values():
        pool = [(position, entry) for position, entry in runs if _unfinished(entry)] or runs   # any unfinished run makes the check pending
        key = (lambda item: (_started(item[1]),)) if pool is not runs else (lambda item: _rank(item[1]))
        best = max(key(item) for item in pool)
        keep.update(position for position, entry in pool if key((position, entry)) == best)   # a tie keeps every tied run
    return [entry for position, entry in enumerate(rollup) if position in keep]
