"""The send state and the first-push notice of the cloud launch (ask #2057).

A part of bin/cloud_worker.py (the one that loads it through `_first_push()`); it holds the code that file needs for the state line of a send and for
the notice of a pod that made no first push by its launch's checkpoint, so that file stays within its size row. The text of each function is the text
that stood in bin/cloud_worker.py: a name that stayed behind there is read as `D["name"]` (that file's own global AT THE CALL, so a patch of it there
reaches this code) and `H` is the switchboard's namespace the cloud worker module holds. Both are set by its accessor before each use.
"""
import json
import os
import re
import subprocess
import sys

H = {}   # the switchboard's own namespace (the same dict the cloud worker module holds), set by `_first_push()`
D: object   # the names that stayed in bin/cloud_worker.py, set by `_first_push()`


def _watch_first_push(state, name, args, repo, branch, brief, moment):
    """Record the launch's watch (the pod, the orchestrator it reports to, the repository, the branch and the checkpoint time from --checkpoint or the
    brief's CHECKPOINT line, the data the local launch's reminders use) for the first-push notice. No orchestrator or no checkpoint: no watch. Never raises."""
    try:
        if not args.reports_to:
            return
        flag = getattr(args, "checkpoint", None)
        if flag is not None:
            at = H["_cp_when"](flag, moment)
        else:
            parsed = H["parse_checkpoint"](brief)
            at = moment + parsed[0] if parsed else None
        if at is not None:
            record_first_push_watch(state, name, args.reports_to, repo, branch, at, moment)
    except Exception as err:
        sys.stderr.write("%s: no first-push watch for %s (%s)\n" % (H["PROG"], name, D["_one"](str(err), 100)))


# ---- the first-push notice (#2057): a cloud pod that has neither pushed a branch nor reported by its launch's checkpoint is named once to its orchestrator.
# Its own record (a watch per pod, written at launch) and its own pass: NOT a reminder item.
FIRST_PUSH_FILE = "first-push-watch.json"
FIRST_PUSH_TIMEOUT = 20   # seconds for the one read of the remote
FIRST_PUSH_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}")


def record_first_push_watch(state_dir, pod, boss, repo, branch, checkpoint_at, launched):
    """Remember the launch's watch: the pod, the orchestrator to tell, the repository and branch, the checkpoint time. One entry per pod."""
    def add(state):
        state[pod] = {"boss": boss, "repo": repo, "branch": branch, "at": float(checkpoint_at), "launched": float(launched)}
        return state
    H["blocked_update"](state_dir, FIRST_PUSH_FILE, add)


def first_push_read(runner, repo, branch):
    """True when the branch exists on the remote and its head differs from HEAD's (the default branch's), False when it is absent or equal,
    None when the read failed or timed out (no verdict: tried again at the next pass). ONE `git ls-remote`, a read; no token is in the command."""
    if not (isinstance(repo, str) and FIRST_PUSH_REPO.fullmatch(repo) and isinstance(branch, str) and re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch)):
        return None
    argv = ["git", "ls-remote", "https://github.com/%s.git" % repo, "HEAD", "refs/heads/" + branch]
    try:
        done = runner(argv, capture_output=True, text=True, timeout=FIRST_PUSH_TIMEOUT, stdin=subprocess.DEVNULL,
                      env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    heads = {}
    for line in (done.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            heads[parts[1]] = parts[0]
    if "HEAD" not in heads:
        return None
    head = heads.get("refs/heads/" + branch)
    return bool(head and head != heads["HEAD"])


def first_push_tick(engine, discovery=None):
    """One pass: for every watched pod whose checkpoint has passed and which is not closed, ONE `no first push` notice to its orchestrator when it
    has neither reported (last-reports.json, since the launch) nor pushed. A pod that reported or pushed is closed without a notice; a noticed pod
    is closed, so the notice is once per pod. A failed read says nothing and is retried. Returns [pod] told."""
    state_dir = getattr(engine, "state_dir", None)
    watch = H["_read_small"](state_dir, FIRST_PUSH_FILE) if state_dir is not None else {}
    if not watch:
        return []
    now, told, reports = engine.now(), [], H["_read_small"](state_dir, H["LAST_REPORT_FILE"])
    runner = getattr(engine, "first_push_runner", None) or subprocess.run

    def close(pod, why):
        H["blocked_update"](state_dir, FIRST_PUSH_FILE, lambda state: dict(state, **{pod: dict(state[pod], closed=why)}) if isinstance(state.get(pod), dict) else None)
    for pod, entry in sorted(watch.items()):
        at, boss = entry.get("at"), entry.get("boss")
        if entry.get("closed") or not H["_is_number"](at) or at > now or not isinstance(boss, str) or not boss:
            continue
        last = (reports.get(pod) or {}).get("at")
        if H["_is_number"](last) and H["_is_number"](entry.get("launched")) and last >= entry["launched"]:
            close(pod, "reported")
            continue
        pushed = first_push_read(runner, entry.get("repo"), entry.get("branch"))
        if pushed is None:
            continue
        if pushed:
            close(pod, "pushed")
            continue
        text = "no first push: %s has pushed no branch and made no report by its checkpoint (%s)" % (H["_plain"](pod, 60), H["iso"](at))
        engine.send("switchboard", boss, text)
        close(pod, "noticed")
        told.append(pod)
        if engine.audit is not None:
            engine.audit.append(engine.actor, "first_push.notice", pod, {"to": boss, "checkpoint": H["iso"](at)})
    return told


_STATE_TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def _state_time(value):
    """A launcher time as written, or None: only a plain UTC time is ever printed (never another value of the launcher's answer)."""
    return value if isinstance(value, str) and _STATE_TIME.match(value) else None


def launcher_status_record(name, cluster, timeout=None):
    """(exit code, the one record, one-line note) of the launcher's `status NAME --json`, which prints a LIST of records (run_launcher keeps only a
    dict, so it cannot read it). The launcher is started as run_launcher starts it (its own login, no shell, its own session); a list of any other
    length, a non-list or a failed run is no record (None). Never raises."""
    timeout = D["STATUS_SECONDS"] if timeout is None else timeout
    try:
        argv = D["launcher_argv"]() + ["status", name, "--json"]
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=cluster.env() if cluster is not None else None, start_new_session=True)
        try:
            out, err = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            D["_kill_group"](child)
            child.communicate()
            return -1, None, "the launcher did not finish status within %s s" % timeout
    except (OSError, D["CloudError"]) as err:
        return -1, None, D["_one"](str(err), 100)
    note = D["_one"](cluster.clean((err or b"").decode("utf-8", "replace"), 200) if cluster is not None else (err or b"").decode("utf-8", "replace"), 200)
    if child.returncode != 0:
        return child.returncode, None, note
    try:   # bounded like the record readers: the depth scan first, then the decode (not a record file, so not one of parse_bounded's counted sites)
        text = (out or b"").decode("utf-8", "replace")
        if D["nesting_too_deep"](text):
            raise ValueError("too deep")
        data = json.JSONDecoder().decode(text)
    except (ValueError, RecursionError):
        return 0, None, "the launcher's answer was not JSON"
    return 0, (data[0] if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) else None), note


def send_state(runner, name, record, cluster, clock=None):
    """(state line, warning or None) for a send: the launcher's own `status` read ONCE. The warning is set for a pod past its hard deadline,
    a retired or unknown pod, a pod that is not running and a read that failed; the send still prompts the endpoint, only `delivered` changes."""
    code, body, note = launcher_status_record(name, cluster)
    if code != 0 or not isinstance(body, dict):
        retired = "unknown worker" in (note or "")
        line = "state of %s: running=unknown idle_deadline=unknown hard_deadline=%s last_event=unknown" % (name, record.get("hard_deadline") or "unknown")
        if retired:
            return line, "%s is retired or unknown to the launcher" % name
        return line, "the state of %s could not be read (%s)" % (name, D["_one"](D["_reason"](code, note) if code != 0 else "no answer", 100))
    deadlines = body.get("deadlines") if isinstance(body.get("deadlines"), dict) else {}
    idle, hard = _state_time(deadlines.get("idle")), _state_time(deadlines.get("hard"))
    last = _state_time(body.get("idle_since"))
    running = body.get("phase") == "Running"
    line = "state of %s: running=%s idle_deadline=%s hard_deadline=%s last_event=%s" % (
        name, "yes" if running else "no", idle or "unknown", hard or "unknown", last or "unknown")
    past = D["_ended"](record, clock) or D["_ended"]({"hard_deadline": hard}, clock)
    if past:
        return line, "%s is past its hard deadline" % name
    if not running:
        return line, "%s is not running" % name
    return line, None
