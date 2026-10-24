"""The one reading of a pull request that two commands share: `merge-gate` (#962) and the clear packet (#952).

Both need the same few facts about a pull request: its head, the branch it targets, whether it is open, its labels, the branch's tip now and the
CI runs at its head. They are read here once, through a `gh` callable the caller supplies, so a test can fake GitHub and the two commands never hold
two copies of the rules for "which run decides" and "what is the branch tip".

`gh(argv)` runs `gh <argv>` and returns `(returncode, stdout, stderr)`; it never raises. Every function here is read-only. A read that fails raises
`ReadError` with one short line, so a caller says "unreadable" and never fills the gap from memory. Python 3.9 standard library only.
"""
import json
from datetime import datetime, timezone
from urllib.parse import quote

RUN_PAGE = 100
GREEN = ("success", "skipped", "neutral")        # a conclusion that leaves a run green
NOT_A_RESULT = ("cancelled", "skipped", "neutral", "stale")   # a branch run with such a conclusion says nothing about the branch (a superseded run)


class ReadError(Exception):
    """One GitHub read failed or answered something unusable."""


def _json(gh, argv, what):
    code, out, err = gh(argv)
    if code != 0:
        raise ReadError("%s could not be read (%s)" % (what, " ".join((err or "").split())[:100] or "exit %d" % code))
    try:
        return json.loads(out or "")
    except ValueError:
        raise ReadError("%s answered something that is not JSON" % what) from None


def epoch(text):
    """Epoch seconds of an ISO time GitHub wrote, or None."""
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def read_pr(gh, repo, number):
    """{"head", "base", "state", "draft", "labels"} of the pull request."""
    data = _json(gh, ["pr", "view", str(number), "--repo", repo, "--json", "headRefOid,baseRefName,state,isDraft,labels"], "pull request %s#%d" % (repo, number))
    head = data.get("headRefOid") if isinstance(data, dict) else None
    base = data.get("baseRefName") if isinstance(data, dict) else None
    if not isinstance(head, str) or len(head) != 40 or not isinstance(base, str) or not base:
        raise ReadError("pull request %s#%d has no readable head or base" % (repo, number))
    labels = [l.get("name") for l in data.get("labels") or [] if isinstance(l, dict) and isinstance(l.get("name"), str)]
    return {"head": head, "base": base, "state": str(data.get("state") or "").upper(), "draft": bool(data.get("isDraft")), "labels": labels}


def read_default_branch(gh, repo):
    data = _json(gh, ["api", "repos/%s" % repo], "repository %s" % repo)
    name = data.get("default_branch") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name:
        raise ReadError("repository %s has no readable default branch" % repo)
    return name


def read_tip(gh, repo, branch):
    """The sha the branch points to now."""
    data = _json(gh, ["api", "repos/%s/git/ref/heads/%s" % (repo, quote(branch, safe="/"))], "branch %s" % branch)
    sha = ((data.get("object") or {}).get("sha")) if isinstance(data, dict) else None
    if not isinstance(sha, str) or len(sha) != 40:
        raise ReadError("branch %s has no readable tip" % branch)
    return sha


def _runs(gh, repo, query, what):
    data = _json(gh, ["api", "repos/%s/actions/runs?%s&per_page=%d" % (repo, query, RUN_PAGE)], what)
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        raise ReadError("%s answered no run list" % what)
    return [r for r in runs if isinstance(r, dict)]


def _newest_per_workflow(runs):
    """The newest run of each workflow (by creation time, then id): the one that decides for that workflow."""
    best = {}
    for run in runs:
        key = run.get("workflow_id", run.get("name"))
        rank = (epoch(run.get("created_at")) or 0, run.get("id") if isinstance(run.get("id"), int) else 0)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, run)
    return [item[1] for item in best.values()]


def read_pr_runs(gh, repo, number, head):
    """The runs that decide at `head`: the newest of each workflow among the pull-request runs of this pull request. Each is
    {"id", "workflow", "workflow_id", "status", "conclusion", "created", "base"}; `base` is the branch tip GitHub recorded for the run (None when it
    recorded none, as for a run of a fork)."""
    out = []
    for run in _newest_per_workflow([r for r in _runs(gh, repo, "head_sha=%s&event=pull_request" % head, "the runs at the head")
                                     if r.get("head_sha") == head]):
        base = None
        for item in run.get("pull_requests") or []:
            if isinstance(item, dict) and item.get("number") == number:
                sha = (item.get("base") or {}).get("sha") if isinstance(item.get("base"), dict) else None
                base = sha if isinstance(sha, str) and len(sha) == 40 else None
        out.append({"id": run.get("id"), "workflow": str(run.get("name") or "workflow"), "workflow_id": run.get("workflow_id"),
                    "status": run.get("status"), "conclusion": run.get("conclusion"), "created": epoch(run.get("created_at")), "base": base})
    return out


def run_result(run):
    """`green`, `red` or `pending` for one run: only a completed run with a green conclusion is green; anything unknown is red or pending, never green."""
    if run.get("status") != "completed":
        return "pending"
    return "green" if run.get("conclusion") in GREEN else "red"


def read_failing_jobs(gh, repo, run_id):
    """Names of the jobs of the run that did not succeed (at most 5); an unreadable list says so in one name."""
    try:
        data = _json(gh, ["api", "repos/%s/actions/runs/%s/jobs?filter=latest&per_page=100" % (repo, run_id)], "the jobs of run %s" % run_id)
    except ReadError:
        return ["(the failing job could not be read)"]
    jobs = data.get("jobs") if isinstance(data, dict) else None
    names = [str(j.get("name")) for j in jobs or [] if isinstance(j, dict) and j.get("conclusion") in ("failure", "timed_out", "cancelled", "startup_failure")]
    return names[:5] or ["(no failing job named)"]


def read_branch_runs(gh, repo, branch, workflow_ids=None):
    """The latest completed run of each workflow on the branch itself (push runs), skipping a superseded one. With `workflow_ids`, only those
    workflows (the ones that also run for pull requests). Each: {"id", "workflow", "sha", "result"}."""
    runs = [r for r in _runs(gh, repo, "branch=%s&event=push&status=completed" % quote(branch, safe=""), "the runs on branch %s" % branch)
            if r.get("conclusion") not in NOT_A_RESULT and (workflow_ids is None or r.get("workflow_id") in workflow_ids)]
    return [{"id": r.get("id"), "workflow": str(r.get("name") or "workflow"), "sha": r.get("head_sha"),
             "result": "green" if r.get("conclusion") == "success" else "red"} for r in _newest_per_workflow(runs)]
