"""The idle nudge reaches a stack pod: every kubectl exec names the worker container (a stack pod's default container is not the worker)."""
import contextlib
import datetime as dt
import inspect
import io
import os
import sys
import textwrap
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_git_token_renewal as tr  # noqa: E402

cw = tr.cw
NOW = dt.datetime(2026, 11, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
EXEC_SRC = textwrap.dedent(inspect.getsource(cw.Kube.exec))


def stack_pod():
    a = {"cw/worker": "w1", "cw/idle-seconds": "3600", "cw/stack": "true"}
    return {"metadata": {"name": "w1-pod", "uid": "u1", "annotations": a}, "status": {"phase": "Running"},
            "spec": {"containers": [{"name": "db"}, {"name": "worker"}]}}


def single_pod():
    p = stack_pod()
    del p["metadata"]["annotations"]["cw/stack"]
    p["spec"]["containers"] = [{"name": "worker"}]
    return p


def make_exec(src=None):
    """A Kube whose run() records argv; its exec answers like a container: only -c worker has /work/results.txt state."""
    calls = []
    kube = cw.Kube.__new__(cw.Kube)

    def run(args, *a, **kw):
        calls.append(list(args))
        head = args[:args.index("--")]
        if "-c" in head and head[head.index("-c") + 1] == "worker":
            return "no\n" if "sh" in args else ""
        return ""   # the default (service) container: no /work, prints nothing
    kube.run = run
    fn = cw.Kube.exec
    if src is not None:
        ns = {}
        exec(compile(src, "<mutant>", "exec"), cw.__dict__, ns)
        fn = ns["exec"]
    kube.exec = types.MethodType(fn, kube)
    kube.annotate = lambda n, u, **kv: None
    kube.scrub = lambda e, n=120: str(e)[:n]
    return kube, calls


class Launch:
    def __init__(self, kube):
        self.kube, self.sent, self.audits = kube, [], []

    def activity(self, name):
        return False, NOW - dt.timedelta(minutes=16)

    def send_note(self, name, text):
        self.sent.append(text)
        self.kube.exec(name, "python3", "-c", "x", text)

    def audit(self, *a):
        self.audits.append(a)

    def _cost_or_zero(self, p):
        return 0


def nudge(pod, src=None):
    kube, calls = make_exec(src)
    s = Launch(kube)
    with contextlib.redirect_stderr(io.StringIO()):
        cw.Launcher.send_idle_nudges(s, [pod], NOW)
    return s, calls


class TestStackNudge(unittest.TestCase):
    def test_control_single_container_pod_nudged(self):
        s, calls = nudge(single_pod())
        self.assertEqual(len(s.audits), 1)

    def test_stack_pod_nudged_once_and_exec_names_worker(self):
        s, calls = nudge(stack_pod())
        self.assertEqual([a[2] for a in s.audits], ["idle-nudge"])
        self.assertEqual(s.sent, [cw.IDLE_NUDGE])
        self.assertTrue(calls and all(c[c.index("-c") + 1] == "worker" and c.index("-c") < c.index("--") for c in calls), calls)

    def test_mutant_without_container_name_skips_stack_pod(self):
        src = EXEC_SRC.replace('"-c", WORKER_CONTAINER, ', "")
        self.assertNotEqual(src, EXEC_SRC, "the mutation was not applied")
        s, calls = nudge(stack_pod(), src)
        self.assertEqual(s.audits, [], "the mutant must skip the stack pod where the control nudges it")
        self.assertTrue(calls and all("-c" not in c[:c.index("--")] for c in calls), calls)


if __name__ == "__main__":
    unittest.main()
