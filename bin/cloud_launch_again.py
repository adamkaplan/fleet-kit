"""The helpers of the idempotent and no-wait launch of the cloud launch (ask #1619).

A part of bin/cloud_worker.py (the one that loads it and re-exports the four helper names); it holds brief_path, _launch_line, _launch_again and
_finish_starting so that file stays within its size row. `H` is the globals dictionary of bin/cloud_worker.py itself (not a copy), set by `bind`:
a name that stayed there is read as `H["name"]` AT THE CALL, so a patch of it on bin/cloud_worker.py reaches this code (and the original
`H` of that file is `H["H"]`). A name defined here and patched on bin/cloud_worker.py does not reach it: patch it on this module.
"""
import json
import os
import sys

BRIEF_MAX = 4_000_000   # the kept text of a no-wait launch's brief (the file read_brief already bounded)
H: dict = {}   # the globals of bin/cloud_worker.py, set by bind


def bind(namespace):
    """Keep the dictionary itself (not a copy) so every read is at the call."""
    global H
    H = namespace


def brief_path(state, name):
    return H["cloud_dir"](state) / (name + ".brief")


def _launch_line(record, **extra):
    """The one JSON line of a launch (name, session, runtime_id, url, cloud) for a record; `extra` adds keys (existing, state)."""
    line = {"name": record["worker"], "session": record.get("session"), "runtime_id": record["runtime_id"], "url": record["url"], "cloud": True}
    if record.get("state") == "starting":
        line["state"] = "starting"
    line.update(extra)
    return line


RETURNED = set()   # names a launch only returned (an existing worker): read and cleared by the entry's `_cmd_launch`


def _launch_again(args, runner, engine, state, record, repo, branch):
    """A launch of a name that has a record. Returns the exit code when the launch is decided here (the worker returned, still starting, finished,
    or refused), or None when the record was cleared (its pod is gone and its hard deadline passed) and a new worker is to be made."""
    if record is None:   # a first launch of the name
        return None
    LaunchError = H["H"]["LaunchError"]
    name = args.name
    if not H["_ended"](record):
        if record.get("state") == "starting":
            return _finish_starting(args, runner, engine, state, record, repo, branch)
        RETURNED.add(name)   # the entry arms no checkpoint reminders for a worker that was only returned (the first launch armed them)
        print("exists: returned worker %s (pod %s)" % (name, record["runtime_id"]), file=sys.stderr)
        print(json.dumps(_launch_line(record, existing=True), sort_keys=True))
        return H["H"]["EXIT_OK"]
    refuse = LaunchError("a cloud worker named %s already exists, past its hard deadline, and its pod is still there (`%s retire %s` ends it)" % (name, H["H"]["PROG"], name))
    if not H["_pod_known"](record):
        raise refuse
    try:
        with H["Cluster"](timeout=H["SNAPSHOT_SECONDS"]) as cluster:
            snapshot = H["_cluster_snapshot"](runner, cluster, H["SNAPSHOT_SECONDS"])
    except H["CloudError"] as err:
        raise LaunchError("a cloud worker named %s already exists and the cluster could not be read (%s); nothing was created" % (name, H["_one"](str(err), 200))) from None
    if H["_pod_of"](record, snapshot) is not None:
        raise refuse
    H["_keep_boss"](state, record)   # exactly what cloud-clear does
    H["remove_record"](state, name)
    H["_audit"](engine, "cloud.cleared", name, {"runtime_id": record.get("runtime_id"), "hard_deadline": record.get("hard_deadline")})
    H["_registry_note"](engine, "retire", state, name, "cleared: the pod is gone and the hard deadline has passed")
    print("cleared %s (pod gone, deadline %s)" % (name, record.get("hard_deadline")), file=sys.stderr)
    return None


def _finish_starting(args, runner, engine, state, record, repo, branch):
    """The same launch command again for a worker in the state starting: read the pod; not ready: say so and create nothing; ready: the session, the
    hub entry and the brief, ONCE. ORDER (a crash must never deliver the brief twice): the session id is saved as soon as it exists (a retry
    makes no second session); the record is marked ready and the brief file removed BEFORE the brief is sent (a crash after that loses the brief, it
    never sends it twice; the failure is said and the brief can be sent with `send`)."""
    LaunchError = H["H"]["LaunchError"]
    name = args.name
    configured = bool((os.environ.get("CW_HUB_CHANNEL_URL") or "").strip() or (os.environ.get("CW_HUB_REGISTRY_SECRET") or "").strip())
    hub = configured or args.agent == "orchestrator"   # the same rule as the launch's own
    recovery = H["VaultRecovery"](H["cloud_dir"](state) / H["VAULT_LOCK_NAME"], lambda detail: H["_audit"](engine, "cloud.vault_check", name, detail))
    try:
        with H["Cluster"](recovery) as cluster:
            pod = H["_pod_of"](record, H["_cluster_snapshot"](runner, cluster, H["SNAPSHOT_SECONDS"])) if H["_pod_known"](record) else None
            phase = (pod or {}).get("phase")
            if pod is None or phase in ("Failed", "Succeeded"):
                raise LaunchError("the pod of %s is %s: `%s retire %s` frees the name" % (name, "gone" if pod is None else phase, H["H"]["PROG"], name))
            if not pod.get("ready"):
                print("still starting: %s" % name, file=sys.stderr)
                print(json.dumps(_launch_line(record), sort_keys=True))
                return H["H"]["EXIT_OK"]
            fleet = H["H"]["fleet_record"](name, args.role, args.reports_to, None if args.issue is None else int(args.issue), args.charter, repo)
            with H["tunnel"](cluster, record) as live:
                v2 = H["remote_v2"](state, live, engine.config["call_timeout_seconds"])
                if not record.get("session"):
                    record["session"] = v2.session_create(args.agent if args.agent == "orchestrator" else H["CLOUD_WORKER_AGENT"], name, args.dir or H["DEFAULT_DIRECTORY"],
                                                          {"fleet": fleet, "cloud": {"runtime_id": record["runtime_id"], "agent": H["CLOUD_WORKER_AGENT"]}})["id"]
                    H["save_record"](state, record)
                if hub:
                    hcode, _, hnote = H["run_launcher"](runner, "hub-session", name, ["--session", record["session"]], 120, cluster=cluster)
                    if hcode != 0:
                        raise H["CloudError"]("the hub entry could not be given the session: %s" % H["_reason"](hcode, hnote))
                brief = None
                try:
                    brief = H["_read_private"](brief_path(state, name), BRIEF_MAX) if brief_path(state, name).exists() else None
                except (OSError, ValueError):
                    brief = None
                record["state"] = "ready"
                H["save_record"](state, record)   # before the brief: see the order above
                try:
                    brief_path(state, name).unlink()
                except OSError:
                    pass
                if brief:
                    v2.prompt(record["session"], H["SETUP_PREAMBLE"] % {"repo": repo, "branch": branch} + brief, {"fleet": {"from": "switchboard", "brief": True}})
    except (H["H"]["V2Error"], H["H"]["V2Unreachable"], H["CloudError"], OSError, KeyError, TypeError) as err:
        raise LaunchError("%s (the worker %s is kept: launch again, or `%s retire %s`)" % (H["_one"](str(err), 200), name, H["H"]["PROG"], name)) from None
    H["_registry_note"](engine, "upsert", state, name, {"session": record["session"], "repo": repo, "role": args.role, "kind": "cloud", "run_id": record.get("run_id"), "state": "working"})
    print(json.dumps(_launch_line(record), sort_keys=True))
    return H["H"]["EXIT_OK"]


def no_wait_options(args):
    """--no-wait (only on request): the launcher requests the pod and returns; the same launch command finishes it later. The create options it adds."""
    if not getattr(args, "no_wait", False):
        return []
    if getattr(args, "stack", False):
        raise H["H"]["LaunchError"]("--no-wait does not combine with --stack (a stack pod's start is its own wait); nothing was created")
    return [H["CREATE_OPTION"]["no_wait"]]


def park(engine, runner, state, name, record, run_id, brief, cluster):
    """The pod is requested: keep what the finishing call needs (the run id, the brief once) and return at once."""
    try:
        record["state"], record["run_id"] = "starting", run_id
        if brief:
            H["_write_private"](brief_path(state, name), brief)
        H["save_record"](state, record)
    except (H["CloudError"], OSError) as err:
        H["remove_record"](state, name)
        H["_stop_after_failure"](engine, runner, name, run_id, "launch: record not stored", cluster)
        raise H["H"]["LaunchError"]("the worker was requested but its record could not be stored (%s); stop was requested" % H["_one"](cluster.clean(str(err), 100), 100)) from None
    print("starting: %s requested; run the same launch command again to finish it, or read %s / status to poll" % (name, name), file=sys.stderr)
    print(json.dumps(_launch_line(record), sort_keys=True))
    return H["H"]["EXIT_OK"]
