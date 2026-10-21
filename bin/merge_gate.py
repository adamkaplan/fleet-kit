"""The fleet's own merge queue (#962): merge into a gated branch only when the pull request's last green run tested the branch as it is now,
and one merge into that branch at a time.

Two pull requests, each green on CI that ran against an older default branch, can merge minutes apart and fail together. The gate closes that:

- `read_facts` reads, and decide() judges: the branch tip now, the branch tip the pull request's last CI run was computed on, equal or not, the
  run's result at the current head, the latest completed run on the branch, and whether a merge is allowed now and why. Nothing here writes.
- When the branch moved, `gate_pass` re-runs THAT pull request's CI on the current merge ref (the smallest safe way is documented on `rerun`: a close
  and reopen, head and labels untouched), waits up to a stated limit and passes only on green. A red or timed-out run blocks and names the failing job.
- A pass is recorded for one exact head and branch tip, and the lock of the repository and branch is taken: one merge at a time. The lock names its
  holder and since when; a lock whose lease ran out, or whose holder process is gone, is recovered; a second merger is told who holds it.
- `check_merge` is what the `gh` shim asks before `gh pr merge`: a person at the owner's own terminal passes, so does a pull request with a logged
  waiver of the chief of staff; any other caller needs the pass for this exact head and tip AND the lock. While the branch's latest completed run is red,
  nothing merges into it except what a waiver names.

Every failure to read is a refusal that says why (fail closed). The functions take the clock, `gh`, the sleeper and the process probe as arguments so
a test fakes them. Python 3.9 standard library only; `pr_reading` (the shared pull-request reader) is handed in by the caller.
"""
import fcntl
import json
import os
import re
import tempfile
import time
from pathlib import Path

CONFIG_KEY = "merge_gate"   # the module declares its own configuration key (bin/fleet-switchboard config_declarations)
CONFIG_DEFAULT = None       # None: nothing is enforced and nothing is added to the configuration
WAIT_MINUTES = 45           # the longest the gate waits for the re-run
LOCK_MINUTES = 20           # a lock is recovered when its holder has held it longer than this without merging
HOLDER_GRACE = 120          # seconds a lock is kept when its holder process is gone (a shell that ran the gate has exited; the merge follows)
POLL_SECONDS = 20
GATE_PASSED = "gate pass and lock held by"   # the reason of an allowed merge that used the lock: the shim releases it when the merge returns
WAIVER_GATE = "MG"          # the gate id a logged waiver names (`waive --gates MG`)
STATE_DIR = "merge-gate"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
HOLD_WORDS = ("merge-captain", "hold-")   # a label that holds the pull request for the owner (by suffix, whatever the prefix)


def validate(raw, source="config"):
    """`merge_gate`: absent = nothing is enforced. Else {"repos": {"owner/name": {"branches": ["main"]} or null}, "wait_minutes": n, "lock_minutes": n}.
    A listed repository without `branches` gates its default branch only; the integration branch is gated only when listed. Raises ValueError."""
    if raw is None:
        return
    bad = ValueError("%s: 'merge_gate' must be {\"repos\": {\"owner/name\": {\"branches\": [\"main\"]}}, \"wait_minutes\": n, \"lock_minutes\": n}, got %r" % (source, raw))
    if not isinstance(raw, dict) or set(raw) - {"repos", "wait_minutes", "lock_minutes"}:
        raise bad
    repos = raw.get("repos", {})
    if not isinstance(repos, dict):
        raise bad
    for name, entry in repos.items():
        if not isinstance(name, str) or not REPO_RE.match(name) or entry not in (None, {}) and not (
                isinstance(entry, dict) and set(entry) == {"branches"} and isinstance(entry["branches"], list) and entry["branches"]
                and all(isinstance(b, str) and BRANCH_RE.match(b) for b in entry["branches"])):
            raise bad
    for key in ("wait_minutes", "lock_minutes"):
        value = raw.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 1440):
            raise bad


def settings(config):
    """{"repos": {lower-cased repo: [branches] or None}, "wait": seconds, "lock": seconds} from the configuration (never raises)."""
    raw = config.get(CONFIG_KEY) if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    repos = {}
    for name, entry in (raw.get("repos") if isinstance(raw.get("repos"), dict) else {}).items():
        branches = entry.get("branches") if isinstance(entry, dict) else None
        repos[str(name).lower()] = list(branches) if isinstance(branches, list) else None
    def minutes(key, default):
        value = raw.get(key)
        return (value if isinstance(value, (int, float)) and not isinstance(value, bool) and 1 <= value <= 1440 else default) * 60
    return {"repos": repos, "wait": minutes("wait_minutes", WAIT_MINUTES), "lock": minutes("lock_minutes", LOCK_MINUTES)}


def listed(conf, repo):
    return repo.lower() in conf["repos"]


def is_gated(conf, repo, branch, default_branch):
    """True when `branch` of `repo` is gated: the repository is listed and the branch is in its `branches`, or (no `branches`) is its default branch."""
    if not listed(conf, repo):
        return False
    branches = conf["repos"][repo.lower()]
    return branch in branches if branches is not None else branch == default_branch


def is_held(labels):
    return any(any(word in str(label).lower() for word in HOLD_WORDS) for label in labels or [])


# ------------------------------------------------------------------------------------------------ facts and the decision

def read_facts(reading, gh, repo, number, conf):
    """The facts of one pull request, read now. Never raises: a read that failed is in `problems` and the decision refuses on it."""
    facts = {"repo": repo, "number": number, "problems": [], "head": None, "branch": None, "tip": None, "run_base": None, "runs": [], "pr_result": "none",
             "failing": [], "branch_runs": [], "branch_result": "unknown", "default_branch": None, "state": None, "labels": [], "gated": False}
    try:
        pr = reading.read_pr(gh, repo, number)
        facts.update(head=pr["head"], branch=pr["base"], state=pr["state"], labels=pr["labels"])
        facts["default_branch"] = reading.read_default_branch(gh, repo)
        facts["gated"] = is_gated(conf, repo, pr["base"], facts["default_branch"])
        facts["tip"] = reading.read_tip(gh, repo, pr["base"])
        runs = reading.read_pr_runs(gh, repo, number, pr["head"])
        facts["runs"] = runs
        results = [reading.run_result(r) for r in runs]
        facts["pr_result"] = "none" if not runs else "pending" if "pending" in results else "red" if "red" in results else "green"
        if facts["pr_result"] == "red":
            for run in runs:
                if reading.run_result(run) == "red":
                    facts["failing"] += ["%s / %s" % (run["workflow"], job) for job in reading.read_failing_jobs(gh, repo, run["id"])]
        bases = {r["base"] for r in runs if r["base"]}
        # the oldest base among the deciding runs: a pull request is as current as its least current run
        if runs and all(r["base"] for r in runs):
            facts["run_base"] = min((r for r in runs), key=lambda r: r["created"] or 0)["base"] if len(bases) > 1 else next(iter(bases))
        branch_runs = reading.read_branch_runs(gh, repo, pr["base"], {r["workflow_id"] for r in runs} or None)
        facts["branch_runs"] = branch_runs
        facts["branch_result"] = "unknown" if not branch_runs else "red" if any(r["result"] == "red" for r in branch_runs) else "green"
    except reading.ReadError as err:
        facts["problems"].append(str(err))
    return facts


def decide(facts, waived=False):
    """(allowed, reason, need): whether a merge is allowed now and why in ONE line; `need` is "rerun" when the branch moved and a re-run is the way
    forward, else None. Fail closed: an unread, unknown or pending state never allows. A waiver (a logged one for this head) allows and says so."""
    if waived:
        return True, "waived: a logged waiver names this head", None
    if facts["problems"]:
        return False, "unreadable: %s" % facts["problems"][0], None
    if facts["state"] != "OPEN":
        return False, "the pull request is %s, not open" % (facts["state"] or "in an unknown state").lower(), None
    if not facts["gated"]:
        return True, "branch %s is not gated: no gate pass is needed" % facts["branch"], None
    if facts["branch_result"] == "red":
        red = [r["workflow"] for r in facts["branch_runs"] if r["result"] == "red"]
        return False, "the latest completed run on %s is red (%s): nothing merges into it until it is fixed, except what a waiver names" % (facts["branch"], ", ".join(red[:3])), None
    if facts["pr_result"] == "none":
        return False, "no CI run at the head %s: nothing was tested" % facts["head"][:8], None
    if facts["pr_result"] == "red":
        return False, "CI is red at the head %s: failing %s" % (facts["head"][:8], ", ".join(facts["failing"][:3])), None
    if facts["pr_result"] == "pending":
        return False, "CI is still running at the head %s: wait for it" % facts["head"][:8], None
    if not facts["run_base"]:
        return False, "the branch tip the last run was computed on is not recorded: re-run its CI", "rerun"
    if facts["run_base"] != facts["tip"]:
        return False, "%s moved after the last green run (tested on %s, tip now %s): its CI must run again on the current merge ref" % (
            facts["branch"], facts["run_base"][:8], facts["tip"][:8]), "rerun"
    return True, "green at the head %s on the current tip %s" % (facts["head"][:8], facts["tip"][:8]), None


def block(facts, allowed, reason, waived=False):
    """The one block `merge-gate` prints."""
    short = lambda s: s[:8] if s else "unknown"
    lines = ["merge-gate %s#%d" % (facts["repo"], facts["number"]),
             "  branch %s: %s" % (facts["branch"] or "unknown", "gated" if facts["gated"] else "not gated"),
             "  branch tip now:           %s" % short(facts["tip"]),
             "  CI computed on tip:       %s" % short(facts["run_base"]),
             "  equal:                    %s" % ("yes" if facts["tip"] and facts["tip"] == facts["run_base"] else "no"),
             "  CI result at head %s: %s%s" % (short(facts["head"]), facts["pr_result"], (" (" + ", ".join(facts["failing"][:3]) + ")") if facts["failing"] else ""),
             "  latest run on branch:     %s" % (", ".join("%s %s at %s" % (r["workflow"], r["result"], short(r["sha"])) for r in facts["branch_runs"][:3]) or facts["branch_result"]),
             "  merge allowed now:        %s: %s" % ("yes" if allowed else "no", reason)]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ the re-run

def rerun(gh, repo, number, labels, allow_held):
    """Run that pull request's CI again on the CURRENT merge ref, changing neither its head nor its labels. Returns None, or one line saying why not.

    The smallest safe way found: close the pull request and reopen it at once. A pull request's `reopened` event starts its CI on a merge ref GitHub
    computes fresh from the branch tip; the head commit is untouched and so are the labels, so no review, proof or clear (each bound to the head) is
    voided. What does NOT work: `gh run rerun` (it reuses the run's OLD merge ref, so the moved branch is never tested), a `workflow_dispatch` run (it
    tests the head alone, not the combination) and `update-branch` or a push (they change the head). A held pull request is not closed by the gate: its
    owner's clear is bound to the head, and the person who owns it says first, so the gate blocks with the command to run (`--rerun-held`)."""
    if is_held(labels) and not allow_held:
        return ("the branch moved and this pull request is held for the owner: the gate does not close and reopen it by itself; once its orchestrator agrees, "
                "run `fleet-switchboard merge-gate %s#%d --pass --rerun-held`" % (repo, number))
    code, _, err = gh(["api", "-X", "PATCH", "repos/%s/pulls/%d" % (repo, number), "-f", "state=closed"])
    if code != 0:
        return "could not close the pull request to recompute its merge ref (%s): nothing changed" % (" ".join((err or "").split())[:80] or "exit %d" % code)
    for _ in range(3):
        code, _, err = gh(["api", "-X", "PATCH", "repos/%s/pulls/%d" % (repo, number), "-f", "state=open"])
        if code == 0:
            return None
    return "the pull request was closed to recompute its merge ref and COULD NOT be reopened: run `gh pr reopen %d --repo %s` now" % (number, repo)


def wait_for_run(reading, gh, conf, repo, number, since, facts, clock, sleep, wait_seconds=None):
    """Wait for the run started after `since` on the current tip. Returns (facts, why): facts read last, and a reason when it did not end green."""
    limit = clock() + (conf["wait"] if wait_seconds is None else wait_seconds)
    while True:
        latest = read_facts(reading, gh, repo, number, conf)
        fresh = [r for r in latest["runs"] if (r["created"] or 0) >= since - 2]   # a run started by the re-run (the clock of GitHub and ours may differ by seconds)
        if not latest["problems"] and fresh and latest["pr_result"] in ("green", "red"):
            return latest, None
        if clock() >= limit:
            return latest, "no finished CI run on the current merge ref within %d minutes: nothing was allowed" % ((wait_seconds or conf["wait"]) // 60)
        sleep(POLL_SECONDS)


# ------------------------------------------------------------------------------------------------ the lock and the pass record

def _dir(state, create):
    path = Path(state) / STATE_DIR
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def lock_name(repo, branch):
    return re.sub(r"[^A-Za-z0-9._-]", "_", "%s@%s" % (repo.lower(), branch)) + ".lock.json"


class Guard:
    """One short critical section over the state directory (a file lock), so two mergers never read and write the lock at the same moment."""

    def __init__(self, state):
        self.path = _dir(state, True) / ".guard"

    def __enter__(self):
        self.handle = open(self.path, "a+")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def _atomic_write(path, data):
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, sort_keys=True)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def read_json(path):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, ValueError, OSError):
        return True
    return True


def lock_stale(lock, now, lease, alive):
    """(stale, why): the lease ran out, or the holder's process is gone and the grace after the pass is over."""
    since = lock.get("since") if isinstance(lock.get("since"), (int, float)) else 0
    if now - since > lease:
        return True, "held longer than %d minutes without a merge" % (lease // 60)
    pid = lock.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and now - since > HOLDER_GRACE and not alive(pid):
        return True, "its holder is gone"
    return False, ""


def read_lock(state, repo, branch):
    return read_json(_dir(state, False) / lock_name(repo, branch))


def acquire(state, repo, branch, holder, pid, pr, now, lease, alive=pid_alive):
    """(ok, lock, said): take the lock of the repository and branch. The same holder may take it again (a retry). A stale lock of another holder is recovered
    (`said` says so). A live one is not: `lock` is it and `said` tells the second merger who holds it and when to retry."""
    with Guard(state):
        path = _dir(state, True) / lock_name(repo, branch)
        current = read_json(path)
        recovered = ""
        if current is not None and current.get("holder") != holder:
            stale, why = lock_stale(current, now, lease, alive)
            if not stale:
                retry = int(max(60, current.get("since", now) + lease - now) // 60)
                return False, current, "%s holds the merge lock on %s %s since %s (for %s): retry in about %d minutes, or when it is released" % (
                    current.get("holder"), repo, branch, time.strftime("%H:%M:%SZ", time.gmtime(current.get("since", 0))), current.get("pr"), retry)
            recovered = "recovered a stale lock of %s (%s)" % (current.get("holder"), why)
        lock = {"holder": holder, "since": now, "pid": pid, "pr": pr, "repo": repo, "branch": branch}
        _atomic_write(path, lock)
        return True, lock, recovered


def release(state, repo, branch, holder):
    """Drop the lock when `holder` holds it. True when it was released."""
    with Guard(state):
        path = _dir(state, True) / lock_name(repo, branch)
        current = read_json(path)
        if current is not None and current.get("holder") == holder:
            try:
                path.unlink()
            except OSError:
                return False
            return True
    return False


PASSES = "passes.json"


def record_pass(state, repo, number, head, tip, branch, by, now):
    with Guard(state):
        path = _dir(state, True) / PASSES
        data = read_json(path) or {}
        data = {k: v for k, v in data.items() if isinstance(v, dict) and now - v.get("at", 0) < 86400}   # a pass is a day old at most
        data["%s#%d" % (repo.lower(), number)] = {"head": head, "tip": tip, "branch": branch, "by": by, "at": now}
        _atomic_write(path, data)


def read_pass(state, repo, number):
    return (read_json(_dir(state, False) / PASSES) or {}).get("%s#%d" % (repo.lower(), number))


# ------------------------------------------------------------------------------------------------ the pass and the check

def gate_pass(reading, gh, conf, state, repo, number, holder, pid, clock, sleep, allow_held=False, alive=pid_alive, wait_seconds=None):
    """Run the gate for a merger: read, re-run when the branch moved, wait, and on green take the lock and record the pass. Returns (ok, lines)."""
    lines = []
    facts = read_facts(reading, gh, repo, number, conf)
    allowed, reason, need = decide(facts)
    if need == "rerun" and facts["gated"] and not facts["problems"]:
        problem = rerun(gh, repo, number, facts["labels"], allow_held)
        if problem:
            return False, [problem]
        lines.append("re-running CI of %s#%d on the current merge ref (head and labels unchanged)" % (repo, number))
        facts, why = wait_for_run(reading, gh, conf, repo, number, clock(), facts, clock, sleep, wait_seconds)
        if why:
            return False, lines + [why]
        allowed, reason, need = decide(facts)
    if not allowed:
        return False, lines + [reason]
    if not facts["gated"]:
        return True, lines + [reason]
    lease = conf["lock"]
    ok, lock, said = acquire(state, repo, facts["branch"], holder, pid, "%s#%d" % (repo, number), clock(), lease, alive)
    if not ok:
        return False, lines + [said]
    if said:
        lines.append(said)
    try:   # the tip is read once more with the lock held: a pass is for the tip at this moment
        tip = reading.read_tip(gh, repo, facts["branch"])
        head = reading.read_pr(gh, repo, number)["head"]
    except reading.ReadError as err:
        release(state, repo, facts["branch"], holder)
        return False, lines + ["unreadable: %s" % err]
    if tip != facts["tip"] or head != facts["head"] or tip != facts["run_base"]:
        release(state, repo, facts["branch"], holder)
        return False, lines + ["the branch or the head moved while the gate ran: run it again"]
    record_pass(state, repo, number, head, tip, facts["branch"], holder, clock())
    return True, lines + ["gate passed for %s#%d at head %s on tip %s; the lock on %s is yours until the merge returns (`merge-gate --release` frees it early)" % (
        repo, number, head[:8], tip[:8], facts["branch"])]


def check_merge(reading, gh, conf, state, repo, number, caller, person, waived_head, clock, alive=pid_alive):
    """What the shim asks before `gh pr merge`: (allowed, one line, head to bind the merge to or None).
    `person`: the caller is the owner's own terminal (passes). `waived_head`: a callable head -> bool, true when a logged waiver of the chief of staff names this
    pull request at this head. `caller` is the agent's name (the lock holder)."""
    facts = read_facts(reading, gh, repo, number, conf)
    if facts["problems"]:
        return False, "merge of %s#%d refused: %s (the gate fails closed)" % (repo, number, facts["problems"][0]), None
    if not facts["gated"]:
        return True, "branch %s is not gated" % facts["branch"], facts["head"]
    if person:
        return True, "the owner's own terminal", facts["head"]
    if waived_head(facts["head"]):
        return True, "a logged waiver names this pull request at this head", facts["head"]
    if facts["state"] != "OPEN":
        return False, "merge of %s#%d refused: the pull request is not open" % (repo, number), None
    if facts["branch_result"] == "red":
        return False, "merge of %s#%d refused: the latest completed run on %s is red, so nothing merges into it except what a logged waiver of the chief of staff names" % (
            repo, number, facts["branch"]), None
    got = read_pass(state, repo, number)
    why = ("run `fleet-switchboard merge-gate %s#%d --pass` first" % (repo, number))
    if got is None:
        return False, "merge of %s#%d refused: no gate pass recorded; %s" % (repo, number, why), None
    if got.get("head") != facts["head"] or got.get("branch") != facts["branch"] or got.get("tip") != facts["tip"]:
        return False, "merge of %s#%d refused: the gate pass is stale (it was for head %s on tip %s; now head %s on tip %s); %s" % (
            repo, number, (got.get("head") or "?")[:8], (got.get("tip") or "?")[:8], facts["head"][:8], facts["tip"][:8], why), None
    lock = read_lock(state, repo, facts["branch"])
    stale = lock_stale(lock, clock(), conf["lock"], alive)[0] if lock else True
    if lock is None or stale or lock.get("holder") != caller:
        who = "nobody holds" if lock is None or stale else "%s holds" % lock.get("holder")
        return False, "merge of %s#%d refused: %s the merge lock on %s; %s" % (repo, number, who, facts["branch"], why), None
    return True, "%s %s" % (GATE_PASSED, caller), facts["head"]
