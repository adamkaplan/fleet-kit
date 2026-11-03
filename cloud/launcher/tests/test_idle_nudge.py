"""The idle nudge: one fixed line to a stalled builder pod (send_idle_nudges, idle_nudge_due). Stub pods, no cluster."""
import datetime as dt
import inspect
import io
import contextlib
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
CONTROL_SRC = textwrap.dedent(inspect.getsource(cw.Launcher.send_idle_nudges))
SKIP_RESULTS = '            if seen != "no":\n                continue\n'
MARK_CALL = "            self.kube.annotate(name, (pod.get(\"metadata\") or {}).get(\"uid\"), **{IDLE_NUDGE_MARK: \"sent\"})\n"


def pod(role="worker", idle="3600", marked=False):
    a = {"cw/worker": "w1", "cw/idle-seconds": idle, "cw/role": role}
    if marked:
        a["cw/idle-nudge"] = "sent"
    return {"metadata": {"name": "w1-pod", "uid": "u1", "annotations": a}, "status": {"phase": "Running"}}


class Stub:
    def __init__(self, busy=False, idle_minutes=20, results="no"):
        self.sent, self.audits, self.marks, self.execs = [], [], [], []
        self.busy, self.idle_minutes, self.results = busy, idle_minutes, results
        self.kube = types.SimpleNamespace(annotate=lambda n, u, **kv: self.marks.append(kv), exec=self._exec, scrub=lambda e, n=120: str(e)[:n])

    def _exec(self, name, *cmd, **kw):
        self.execs.append(cmd)
        if self.results == "fail":
            raise cw.CloudError("exec failed")
        return self.results + "\n"

    def activity(self, name):
        return self.busy, NOW - dt.timedelta(minutes=self.idle_minutes)

    def send_note(self, name, text):
        self.sent.append(text)

    def audit(self, *a):
        self.audits.append(a)

    def _cost_or_zero(self, p):
        return 0


def run(fn, stub, p):
    with contextlib.redirect_stderr(io.StringIO()):
        fn(stub, [p], NOW)


def mutant(old, new):
    src = CONTROL_SRC.replace(old, new)
    applied = src != CONTROL_SRC
    assert applied, "the mutation was not applied"
    ns = {}
    exec(compile(src, "<mutant>", "exec"), cw.__dict__, ns)
    return ns["send_idle_nudges"]


class TestIdleNudge(unittest.TestCase):
    fn = staticmethod(cw.Launcher.send_idle_nudges)

    def test_due_pod_gets_one_line_one_audit_one_mark_and_no_second(self):
        s = Stub()
        run(self.fn, s, pod())
        self.assertEqual(s.sent, [cw.IDLE_NUDGE])
        self.assertEqual(len(s.audits), 1)
        self.assertEqual(s.audits[0][2], "idle-nudge")
        self.assertEqual(s.marks, [{cw.IDLE_NUDGE_MARK: "sent"}])
        run(self.fn, s, pod(marked=True))   # the next pass sees the annotation
        self.assertEqual(len(s.sent), 1, "%d lines sent where 1 is required" % len(s.sent))

    def test_not_due_cases_send_nothing(self):
        cases = {"results exists": (Stub(results="yes"), pod()), "orchestrator": (Stub(), pod(role="orchestrator")),
                 "busy": (Stub(busy=True), pod()), "idle under 15 minutes": (Stub(idle_minutes=14), pod()),
                 "limit not above 15 minutes": (Stub(), pod(idle="900")), "already marked": (Stub(), pod(marked=True)),
                 "results check cannot run": (Stub(results="fail"), pod())}
        for label, (s, p) in cases.items():
            run(self.fn, s, p)
            self.assertEqual((s.sent, s.audits, s.marks), ([], [], []), "a line was sent: " + label)

    def test_the_text_holds_no_name_and_no_token_form(self):
        self.assertNotRegex(cw.IDLE_NUDGE, r"gh[pousr]_|github_pat_|[A-Za-z0-9]{40}")
        self.assertIn("/work/results.txt", cw.IDLE_NUDGE)

    def test_due_function(self):
        t = NOW - dt.timedelta(minutes=15)
        self.assertTrue(cw.idle_nudge_due(False, t, NOW, 901, "", "worker", False))
        self.assertFalse(cw.idle_nudge_due(False, None, NOW, 901, "", "worker", False))


class TestMutants(unittest.TestCase):
    def test_control_passes(self):
        s = Stub(results="yes")
        run(cw.Launcher.send_idle_nudges, s, pod())
        self.assertEqual(s.sent, [])

    def test_no_op_mutation_fails_the_computation(self):
        with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
            mutant("send_idle_nudges", "send_idle_nudges")

    def test_pass_that_ignores_the_results_file_is_caught(self):
        s = Stub(results="yes")
        run(mutant(SKIP_RESULTS, ""), s, pod())
        self.assertEqual(len(s.sent), 1, "the mutant must send where the control sends 0")

    def test_pass_that_sends_without_marking_is_caught(self):
        s = Stub()
        run(mutant(MARK_CALL, ""), s, pod())
        self.assertEqual(s.marks, [], "the mutant must leave the pod unmarked where the control marks it")


if __name__ == "__main__":
    unittest.main()
