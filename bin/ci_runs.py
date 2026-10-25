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
import time
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


def _identity(entry, position, repo=""):
    name = entry.get("name") or entry.get("context")
    if not isinstance(name, str) or not name:
        return ("unnamed", position)   # names no check: its own check, never merged with another
    workflow = entry.get("workflowName")
    run = _run_id(entry)
    ident = _cached_identity(entry, run, repo) if run is not None else None   # the cache only (latest_runs did the reads): the stable identity, never the display name
    return (workflow if isinstance(workflow, str) else "", ident, name)


def _run_id(entry):
    """The workflow run id of a check run (from its detailsUrl `.../actions/runs/<id>/...`), or None (a status context, or no url)."""
    url = entry.get("detailsUrl")
    found = re.search(r"/actions/runs/(\d{1,12})(?:/|$)", url) if isinstance(url, str) else None
    return int(found.group(1)) if found else None


UNREADABLE = "IDENTITY_UNREADABLE"   # status given to a run whose workflow identity could not be read: never green (fails closed)
_CACHE = {}   # (owner/repo, run id) -> (workflow id, event); a run's workflow never changes, so one bounded read per distinct run
_PATH = {}   # (owner/repo, run id) -> workflow file path, read with the identity
_FAILED = {}   # (owner/repo, run id) -> monotonic time of a failed read: not retried before FAIL_WINDOW passes
READER = None   # tests (or a caller with its own gh runner) set a function (repo, run id) -> (workflow id, event) or None
READ_CAP = 10   # at most this many gh reads per call; the other distinct runs are marked unreadable (fail closed)
CACHE_MAX = 512   # entries of _CACHE (and of _FAILED); the oldest is dropped first
FAIL_WINDOW = 60   # seconds a failed read is remembered
READ_TIMEOUT = 20   # seconds of one gh read
CAP_STATUS = "IDENTITY_UNREADABLE: more than %d distinct runs, read cap %d per pass" % (READ_CAP, READ_CAP)
_clock = time.monotonic
_REPO = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/actions/runs/")


def _put(table, key, value):
    table.pop(key, None)
    while len(table) >= CACHE_MAX:
        table.pop(next(iter(table)))
    table[key] = value


def _details_repo(entry):
    found = _REPO.search(entry.get("detailsUrl") or "")
    return found.group(1).lower() if found else ""


def own_repo(rollup, repo=None):
    """The pull request's own repository: the one given, else the one most of the rollup's run urls name (a tie names none: all unreadable)."""
    if isinstance(repo, str) and repo:
        return repo.lower()
    counts = {}
    for entry in rollup if isinstance(rollup, list) else []:
        if isinstance(entry, dict) and _details_repo(entry):
            counts[_details_repo(entry)] = counts.get(_details_repo(entry), 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return "" if not ranked or (len(ranked) > 1 and ranked[0][1] == ranked[1][1]) else ranked[0][0]


def _read_run(repo, run):
    """ONE bounded read of a run: (workflow id, event) from `gh api repos/<repo>/actions/runs/<id>`, or None when it cannot be read."""
    import json
    import subprocess
    try:
        done = subprocess.run(["gh", "api", "repos/%s/actions/runs/%d" % (repo, run), "--jq", "{w: .workflow_id, e: .event, p: .path}"],
                              capture_output=True, text=True, timeout=READ_TIMEOUT)
        data = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if isinstance(data, dict) and type(data.get("w")) is int and isinstance(data.get("e"), str) and re.fullmatch(r"[a-z_]{1,40}", data["e"]):
        if isinstance(data.get("p"), str) and re.fullmatch(r"\.github/workflows/[\w.-]{1,100}", data["p"]):
            _put(_PATH, (repo, run), data["p"])
        return (data["w"], data["e"])
    return None


def _cached_identity(entry, run, repo):
    """The identity already read for this run of the PR's own repo, or None. Never reads."""
    return _CACHE.get((repo, run)) if repo and _details_repo(entry) == repo else None


def _identity_of_run(entry, run, repo, budget=None):
    """(workflow id, event) of the run behind `entry` in the PR's own `repo`; None when unreadable (the caller fails closed): a url naming
    another repo, a failed read inside the negative-cache window, or no read left in `budget` (a one-item list, decremented per gh read)."""
    if not repo or _details_repo(entry) != repo:
        return None
    key = (repo, run)
    if key in _CACHE:
        return _CACHE[key]
    failed = _FAILED.get(key)
    if failed is not None and _clock() - failed < FAIL_WINDOW:
        return None
    if budget is not None:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
    got = None
    try:
        got = (READER or _read_run)(repo, run)
    except Exception:
        got = None
    if not (isinstance(got, tuple) and len(got) == 2):
        _put(_FAILED, key, _clock())
        return None
    _put(_CACHE, key, got)
    return got


def _attempt(entry):
    """The run attempt named in a details url (`.../runs/<id>/attempts/<n>`), 0 when it names none: attempts of one run id share the id, the higher attempt is the later."""
    found = re.search(r"/attempts/(\d{1,4})(?:/|$)", entry.get("detailsUrl") or "")
    return int(found.group(1)) if found else 0


def _order(entry, run):
    """Order of runs of one workflow: created_at (or the start time), then run attempt, then run id. A missing time is "" (the old rule)."""
    t = entry.get("created_at") or entry.get("createdAt") or entry.get("started_at") or entry.get("startedAt") or ""
    return (t if isinstance(t, str) else "", _attempt(entry), run)


def latest_runs(rollup, repo=None):
    """`rollup` without the checks of an OLDER run of a workflow. A workflow is its STABLE identity (the workflow id read from the run), never
    its display name (the PR author writes that: a second workflow could take the real one's name). Runs are grouped by (workflow id, event):
    a newer run of another event never hides a red older one (the fail-closed choice). The latest of a group counts (created_at, then run attempt, then the highest run id). A run whose identity cannot be read (another repo, a failed read, over the read cap) is kept and marked UNREADABLE (never
    green). Entries with no run id (status contexts) are kept."""
    if not isinstance(rollup, list):
        return []
    repo = own_repo(rollup, repo)
    budget = [READ_CAP]
    known = []
    top = {}
    capped = set()
    for entry in rollup:
        run = _run_id(entry) if isinstance(entry, dict) else None
        ident = _identity_of_run(entry, run, repo, budget) if run is not None else None
        if run is not None and ident is None and budget[0] <= 0 and (repo, run) not in _FAILED and _details_repo(entry) == repo:
            capped.add((repo, run))
        known.append((run, ident))
        if ident is not None:
            top[ident] = max(top.get(ident, _order(entry, run)), _order(entry, run))
    out = []
    for entry, (run, ident) in zip(rollup, known):
        if run is None:
            out.append(entry)
        elif ident is None:
            out.append(dict(entry, status=CAP_STATUS if (repo, run) in capped else UNREADABLE, conclusion=""))
        elif _order(entry, run) >= top[ident]:
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
    repo = own_repo(rollup)
    rollup = latest_runs(rollup, repo)   # only the latest run of each workflow counts
    groups = {}
    for position, entry in enumerate(rollup):
        if isinstance(entry, dict):
            groups.setdefault(_identity(entry, position, repo), []).append((position, entry))
    keep = set()
    for runs in groups.values():
        pool = [(position, entry) for position, entry in runs if _unfinished(entry)] or runs   # any unfinished run makes the check pending
        key = (lambda item: (_started(item[1]),)) if pool is not runs else (lambda item: _rank(item[1]))
        best = max(key(item) for item in pool)
        keep.update(position for position, entry in pool if key((position, entry)) == best)   # a tie keeps every tied run
    return [entry for position, entry in enumerate(rollup) if position in keep]


CHECK_PAGES = 5   # pages of the Checks API read for one commit; a longer list is REFUSED (never read as whole)
CHECK_PAGE_SIZE = 100
ROLLUP_CAP = 100   # gh's statusCheckRollup is one page of this size: a rollup that reaches it may be cut, so it is never green


def cap_why():
    return "more than %d check runs (%d pages of %d) or a page failed: the list is not read as whole" % (CHECK_PAGES * CHECK_PAGE_SIZE, CHECK_PAGES, CHECK_PAGE_SIZE)


def read_check_runs(api, repo, sha):
    """(runs, refusal) for the check runs of `sha`: `api(path)` answers the decoded page or None. Up to CHECK_PAGES pages; a failed page, or a list
    longer than the bound, gives refusal=cap_why() (the caller records nothing)."""
    runs = []
    for page in range(1, CHECK_PAGES + 1):
        data = api("repos/%s/commits/%s/check-runs?per_page=%d&page=%d" % (repo, sha, CHECK_PAGE_SIZE, page))
        got = data.get("check_runs") if isinstance(data, dict) else None
        if not isinstance(got, list):
            return [], cap_why()
        runs.extend(got)
        total = data.get("total_count")
        if len(got) < CHECK_PAGE_SIZE and (not isinstance(total, int) or len(runs) >= total):
            return runs, None
        if isinstance(total, int) and len(got) >= CHECK_PAGE_SIZE and total < len(runs):
            return [], cap_why()   # GitHub's own count is below the runs read: not whole
        if isinstance(total, int) and len(runs) >= total:
            return runs, None
    return [], cap_why()


def _stamp(r):
    """Ordering of two check runs of one name inside one workflow run (a rerun of a job): newest started_at, completed_at, then check-run id."""
    return (str(r.get("started_at") or ""), str(r.get("completed_at") or ""), r.get("id") if isinstance(r.get("id"), int) else 0)


def latest_check_runs(runs, repo):
    """The Checks API's `runs` ({name, status, conclusion, details_url, app}) without the checks of an OLDER run of a workflow (the same reading
    as `latest_runs`: the one function, fed the url of each check run), and, inside one workflow run, only the NEWEST check run of each
    (run id, name, app): a rerun of one job leaves two check runs of one name under one run id."""
    entries = [dict(r, detailsUrl=r.get("details_url") or r.get("html_url") or "") if isinstance(r, dict) else r for r in runs or []]
    kept = latest_runs(entries, repo)
    best = {}
    for e in kept:
        run = _run_id(e) if isinstance(e, dict) else None
        if run is not None:
            app = e.get("app") if isinstance(e.get("app"), dict) else {}
            key = (run, e.get("name"), app.get("slug"))
            if key not in best or _stamp(e) > _stamp(best[key]):
                best[key] = e
    out = []
    for e in kept:
        if isinstance(e, dict):
            run = _run_id(e)
            if run is not None:
                app = e.get("app") if isinstance(e.get("app"), dict) else {}
                if best[(run, e.get("name"), app.get("slug"))] is not e:
                    continue
            out.append(dict({k: v for k, v in e.items() if k != "detailsUrl"}, **({"status": "unreadable", "conclusion": None} if e.get("status") in (UNREADABLE, CAP_STATUS) else {})))
    return out


def missing_workflows(rollup, expected, repo=None):
    """["no run for workflow X"] for each EXPECTED workflow (`plan_gates.repos.<repo>.ci_workflows`: a workflow ID or its `.github/workflows/<file>` path,
    NEVER the display name, which the PR's author writes) with no check at the head. Entries carry `detailsUrl` (the rollup's, or a check run's
    details_url); a run whose identity was not read matches nothing. An empty rollup misses all."""
    entries = [e for e in (rollup if isinstance(rollup, list) else []) if isinstance(e, dict)]
    repo = own_repo(entries, repo)
    have = set()
    for e in entries:
        run = _run_id(e)
        ident = _cached_identity(e, run, repo) if run is not None else None
        if ident is not None:
            have.add(str(ident[0]))
            if _PATH.get((repo, run)):
                have.add(_PATH[(repo, run)])
    return ["no run for workflow %s" % _clean(w) for w in (expected or []) if str(w) not in have]


def missing_workflows_of_checks(runs, expected, repo):
    """The same, for the Checks API's list (the item path): one function, the entries given the url the rollup calls detailsUrl."""
    return missing_workflows([dict(r, detailsUrl=r.get("details_url") or r.get("html_url") or "") for r in runs or [] if isinstance(r, dict)], expected, repo)
