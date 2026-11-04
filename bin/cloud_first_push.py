"""The first-push notice (#2057) and the state line of a cloud send: a part of bin/cloud_worker.py (the one that loads it and calls it).

It holds the code that file needs only for those two things, so that file stays within its size row. `bind(deps)` hands it the names of the cloud
worker module it works with (the launcher's status read, the one-line scrub, the deadline rule) and the switchboard's own namespace.
"""
import datetime
import json
import os
import re
import subprocess
import sys

D = {}   # what the cloud worker module hands over, set by bind()


def bind(deps):
    D.clear()
    D.update(deps)


def _watch_first_push(state, name, args, repo, branch, brief, moment):
    """Record the launch's watch (the pod, the orchestrator it reports to, the repository, the branch and the checkpoint time from --checkpoint or the
    brief's CHECKPOINT line, the data the local launch's reminders use) for the first-push notice. No orchestrator or no checkpoint: no watch. Never raises."""
    try:
        if not args.reports_to:
            return
        flag = getattr(args, "checkpoint", None)
        if flag is not None:
            at = D["_cp_when"](flag, moment)
        else:
            parsed = D["parse_checkpoint"](brief)
            at = moment + parsed[0] if parsed else None
        if at is not None:
            record_first_push_watch(state, name, args.reports_to, repo, branch, at, moment)
    except Exception as err:
        sys.stderr.write("%s: no first-push watch for %s (%s)\n" % (D["PROG"], name, D["_one"](str(err), 100)))


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
    D["blocked_update"](state_dir, FIRST_PUSH_FILE, add)


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
    watch = D["_read_small"](state_dir, FIRST_PUSH_FILE) if state_dir is not None else {}
    if not watch:
        return []
    now, told, reports = engine.now(), [], D["_read_small"](state_dir, D["LAST_REPORT_FILE"])
    runner = getattr(engine, "first_push_runner", None) or subprocess.run

    def close(pod, why):
        D["blocked_update"](state_dir, FIRST_PUSH_FILE, lambda state: dict(state, **{pod: dict(state[pod], closed=why)}) if isinstance(state.get(pod), dict) else None)
    for pod, entry in sorted(watch.items()):
        at, boss = entry.get("at"), entry.get("boss")
        if entry.get("closed") or not D["_is_number"](at) or at > now or not isinstance(boss, str) or not boss:
            continue
        last = (reports.get(pod) or {}).get("at")
        if D["_is_number"](last) and D["_is_number"](entry.get("launched")) and last >= entry["launched"]:
            close(pod, "reported")
            continue
        pushed = first_push_read(runner, entry.get("repo"), entry.get("branch"))
        if pushed is None:
            continue
        if pushed:
            close(pod, "pushed")
            continue
        text = "no first push: %s has pushed no branch and made no report by its checkpoint (%s)" % (D["_plain"](pod, 60), D["iso"](at))
        engine.send("switchboard", boss, text)
        close(pod, "noticed")
        told.append(pod)
        if engine.audit is not None:
            engine.audit.append(engine.actor, "first_push.notice", pod, {"to": boss, "checkpoint": D["iso"](at)})
    return told


_STATE_TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def _state_time(value):
    """A launcher time as written, or None: only a plain UTC time is ever printed (never another value of the launcher's answer)."""
    return value if isinstance(value, str) and _STATE_TIME.match(value) else None


def send_state(runner, name, record, cluster, clock=None):
    """(state line, warning or None) for a send: the launcher's own `status` read ONCE. The warning is set for a pod past its hard deadline,
    a retired or unknown pod, a pod that is not running and a read that failed; the send still prompts the endpoint, only `delivered` changes."""
    code, body, note = D["launcher_status_record"](name, cluster)
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
