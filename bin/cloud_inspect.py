"""The inspect command of the cloud launcher: what a pod of the caller is doing now (ask #2115, read-only).

A part of bin/cloud_worker.py (the one that loads it and re-exports every name defined here); it holds the code that file needs so that it stays within
its size row. The text of each function is the text that stood in bin/cloud_worker.py: the only change is that a name which stayed behind there is read
as `D["name"]`, and `D` reads that file's own globals at each call, so a name patched on bin/cloud_worker.py reaches this code.
"""
import json

D: object   # the names that stayed in bin/cloud_worker.py, set by its loader before this file runs


INSPECT_SECONDS = 60   # the bound of the launcher's one read (one exec, or the fallback's four)


def bind(namespace):
    """Load the ended-pod lines of the status (ask #2343, bin/cloud_ended_pods.py): that part wraps the summary line of bin/cloud_worker.py."""
    namespace["_load_part"]("cloud_ended_pods")


def _inspect_short(body):
    """ONE line for a notice: 'NAME: running TOOL for Ns | last: TOOL STATUS | idle Ns | branch B, N unpushed, dirty D | last push P'."""
    def word(value, limit=60):
        return D["_one"](str(value), limit) if value not in (None, "") else "?"
    flight, calls, un = body.get("in_flight"), body.get("last_calls") or [], body.get("unpushed")
    doing = "running %s for %ss" % (word(flight.get("tool")), word(flight.get("seconds"))) if isinstance(flight, dict) else "no call in flight"
    last = "last: %s %s" % (word(calls[0].get("tool")), word(calls[0].get("status"))) if calls and isinstance(calls[0], dict) else "last: none read"
    idle = "idle %ss" % word(body.get("idle_seconds")) if body.get("idle_seconds") is not None else "idle ?"
    git = "branch %s, %s unpushed, dirty %s" % (word(body.get("branch")), word(un.get("count")) if isinstance(un, dict) else "?", word(body.get("dirty")))
    return "%s: %s | %s | %s | %s | last push %s" % (word(body.get("worker"), 80), doing, last, idle, git, word(body.get("last_push"), 100))


@D["refusals"]
def cmd_inspect(args, runner, now):
    """`inspect NAME [--short]`: the pod's live state (the call in flight, the last calls, the last message, the idle seconds, git, the last push and
    error), read-only, through the launcher's `inspect`. Only the pod's BOSS may read it (its worker record's `reports_to`): any other caller is refused
    and the refusal audited. Every text was scrubbed by the launcher before it came here."""
    LaunchError = D["H"]["LaunchError"]
    if not D["valid_name"](args.name):
        raise LaunchError("name the worker: `%s inspect <name>`" % D["H"]["PROG"])
    record = D["route"](args.name)
    if record is None:
        raise LaunchError("%s has no cloud record" % args.name)
    engine, state, _ = D["H"]["build_engine"](runner, now, "cli")
    kind = D["H"]["gates_caller_kind"](runner, now)
    if not (kind[0] == "agent" and record.get("reports_to") and kind[1] == record.get("reports_to")):
        D["_audit"](engine, "cloud.inspect_refused", args.name, {"why": D["_one"](str(kind[1]), 120)})
        raise LaunchError("refused: only %s's own boss may inspect it" % args.name)
    try:
        with D["Cluster"]() as cluster:
            code, body, note = D["run_launcher"](runner, "inspect", args.name, (), INSPECT_SECONDS, cluster=cluster)
    except D["CloudError"] as err:
        raise LaunchError("%s: nothing was read" % err) from None
    if code != 0 or not isinstance(body, dict):
        raise LaunchError("the pod was not read (%s)" % D["_reason"](code, note))
    if getattr(args, "short", False):
        print(_inspect_short(body))
        return D["H"]["EXIT_OK"]
    for key in ("source", "in_flight", "last_calls", "last_text", "idle_seconds", "branch", "unpushed", "dirty", "last_push", "last_error", "missing"):
        print("%s: %s" % (key, json.dumps(body.get(key), sort_keys=True)))
    return D["H"]["EXIT_OK"]
