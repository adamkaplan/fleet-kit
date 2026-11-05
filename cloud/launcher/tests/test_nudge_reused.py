"""The idle nudge treats /work/results.txt as finished only when it is NEWER than the pod's last user message. Stub pods, no cluster."""
import inspect
import os
import sys
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_idle_nudge as tn  # noqa: E402

cw = tn.cw
CONTROL_SRC = textwrap.dedent(inspect.getsource(cw.Launcher.send_idle_nudges))
OLD_IF = 'if seen != "no" and not results_older_than_prompt(seen):'


def mutant(old, new):
    src = CONTROL_SRC.replace(old, new)
    assert src != CONTROL_SRC, "the mutation was not applied"
    ns = {}
    exec(compile(src, "<mutant>", "exec"), cw.__dict__, ns)
    return ns["send_idle_nudges"]


def pod_named(name):
    p = tn.pod()
    p["metadata"]["name"] = name
    p["metadata"]["annotations"]["cw/worker"] = name.split("-")[0]
    return p


def stub(answer):
    return tn.Stub(results=answer)


def one_pass(fn, s, pods):
    import contextlib
    import io
    with contextlib.redirect_stderr(io.StringIO()):
        fn(s, pods, tn.NOW)


class TestReused(unittest.TestCase):
    fn = staticmethod(cw.Launcher.send_idle_nudges)

    def test_reused_pod_nudged_once_then_not_again(self):
        s = stub("100.0 200.0")   # the file is older than the last prompt
        one_pass(self.fn, s, [tn.pod()])
        self.assertEqual((len(s.sent), len(s.audits)), (1, 1), "a re-used pod was not nudged")
        one_pass(self.fn, s, [tn.pod(marked=True)])
        self.assertEqual(len(s.sent), 1)

    def test_finished_pod_not_nudged(self):
        s = stub("300.0 200.0")
        one_pass(self.fn, s, [tn.pod()])
        self.assertEqual((s.sent, s.audits, s.marks), ([], [], []))

    def test_unreadable_time_not_nudged_and_next_pod_handled(self):
        for answer in ("unreadable", "100.0 unreadable", "garbage", "yes", "fail"):
            s = stub(answer)
            one_pass(self.fn, s, [tn.pod()])
            self.assertEqual((s.sent, s.audits, s.marks), ([], [], []), answer)
        answers = {"a-pod": "fail", "b-pod": "100.0 200.0"}
        s = tn.Stub()

        def _exec(name, *cmd, **kw):
            if answers[name] == "fail":
                raise cw.CloudError("exec failed")
            return answers[name] + "\n"
        s.kube.exec = _exec
        s.activity = lambda name: (False, tn.NOW - tn.dt.timedelta(minutes=20))
        one_pass(self.fn, s, [pod_named("a-pod"), pod_named("b-pod")])
        self.assertEqual(len(s.sent), 1, "the pass did not go on to the next pod")


class TestMutant(unittest.TestCase):
    def test_control_first(self):
        s = stub("100.0 200.0")
        one_pass(cw.Launcher.send_idle_nudges, s, [tn.pod()])
        self.assertEqual(len(s.sent), 1)

    def test_mutant_without_the_comparison_fails(self):
        s = stub("100.0 200.0")
        one_pass(mutant(OLD_IF, 'if seen != "no":'), s, [tn.pod()])
        with self.assertRaisesRegex(AssertionError, "a re-used pod was not nudged"):
            self.assertEqual(len(s.sent), 1, "a re-used pod was not nudged")


if __name__ == "__main__":
    unittest.main()
