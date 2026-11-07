"""The reaper's save of a running pod's work (ask #2304): every SAVE_EVERY_SECONDS, for each running non-orchestrator pod, ONE `kubectl exec` of
`cw-end-hook --snapshot` in the pod, which pushes the pod's commits and files to <worker>/wip as a new fast-forward commit (cloud/launcher/end_hook.py,
`snapshot`) without touching the pod's branch, index or files. A pod whose tree is the one last saved writes nothing. A failure is said once (one audit line
and one line on stderr) until a save works again. State lives on the launcher object, in memory: a restarted reaper starts the clock again.
"""
import json
import sys

SAVE_EVERY_SECONDS = 600
EXEC_TIMEOUT_SECONDS = 60
SAVE_ACTION = "wip-save"
ORCHESTRATOR_ROLE = "orchestrator"


def _ann(pod, key, default=None):
    return ((pod.get("metadata") or {}).get("annotations") or {}).get("cw/" + key, default)


def save_pass(launcher, pods, now, name_ok):
    """One pass over the pods the reaper holds. Returns a list of (pod name, 'saved'|'unchanged'|'failed'), for the tests. Never raises."""
    if not callable(getattr(launcher.kube, "exec", None)):
        return []   # a kube that cannot exec has no pod to reach
    state = launcher.__dict__.setdefault("wip_save_state", {})
    seen, out = set(), []
    for pod in pods:
        try:
            worker, name = _ann(pod, "worker"), pod["metadata"]["name"]
            if (pod.get("status") or {}).get("phase") != "Running" or not name_ok(worker or ""):
                continue
            if _ann(pod, "persistent") is not None or _ann(pod, "role", "worker") == ORCHESTRATOR_ROLE or _ann(pod, "no-commit") == "true":
                continue
            seen.add(name)
            row = state.setdefault(name, {"last": None, "said": None})
            if row["last"] is not None and (now - row["last"]).total_seconds() < SAVE_EVERY_SECONDS:
                continue
            row["last"] = now
            try:
                res = json.loads(launcher.kube.exec(name, "cw-end-hook", "--snapshot", keep_stdout=True, timeout=EXEC_TIMEOUT_SECONDS).strip().splitlines()[-1])
                if res.get("error"):
                    raise RuntimeError(str(res["error"]))
            except Exception as e:
                why = "".join(c if c.isprintable() else "?" for c in launcher.kube.scrub(e, 200)) or type(e).__name__
                if row["said"] != why:
                    row["said"] = why
                    launcher.audit(worker, "pod/" + name, SAVE_ACTION, 0.0, False, "the work was not saved to %s/wip: %s" % (worker, why))
                    print("cloud-worker: the work of %s was not saved to %s/wip: %s" % (name, worker, why), file=sys.stderr)
                out.append((name, "failed"))
                continue
            row["said"] = None
            out.append((name, "saved" if res.get("saved") else "unchanged"))
        except Exception as e:
            print("cloud-worker: the wip save step failed: %s" % launcher.kube.scrub(e, 120), file=sys.stderr)
    for gone in [n for n in state if n not in seen]:
        del state[gone]
    return out
