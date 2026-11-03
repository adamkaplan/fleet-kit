"""The cloud send tells the pod's real state (#2057): one state line from the launcher's own `status`, and a warning instead of `delivered`
for a pod past its hard deadline, retired, unknown or unread. The send still prompts the endpoint in every case."""
import argparse
import contextlib
import json
import io
import re
import unittest
from unittest import mock
from support.loader import sb

TOKEN = "tok-SECRET-value-9f3"
FUTURE, PAST = "2099-01-01T00:00:00Z", "2000-01-01T00:00:00Z"


class Probe:
    """A stand-in for Cluster() and tunnel(): they carry a token the printed lines must never show."""
    token_file = TOKEN

    def env(self):
        return {"KUBECONFIG": TOKEN}

    def clean(self, text, limit=300):
        return text.replace(TOKEN, "***")[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TheDeliveredLineMutant:
    """S5: a mutant that restores the delivered line for a dead pod is caught by the anchored text; the control passes first; a no-op fails the computation."""
    SOURCE = sb.load_module("cloud_worker").__file__
    OLD = "    if warning:\n        sys.stderr.write(\"%s: WARNING"
    NEW = "    if False:\n        sys.stderr.write(\"%s: WARNING"
    ANCHOR = "a dead pod is never reported delivered"

    def compute(self, old, new):
        """The mutation computed from the two texts, in its own process: exit 1 when the two texts are identical (a no-op is no mutation)."""
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a, b = tmp + "/a", tmp + "/b"
            open(a, "w").write(old)
            open(b, "w").write(new)
            code = "import sys; sys.exit(0 if open(sys.argv[1]).read() != open(sys.argv[2]).read() else 1)"
            return subprocess.run([sys.executable, "-c", code, a, b]).returncode

    def run_dead_pod_test(self, source):
        import types
        mod = types.ModuleType("cw_send_mutant")
        mod.__dict__["__file__"] = self.SOURCE
        import sys
        sys.modules["cw_send_mutant"] = mod
        try:
            exec(compile(source, "cw_send_mutant", "exec"), mod.__dict__)
            result = self._run_variant(mod)
        finally:
            sys.modules.pop("cw_send_mutant", None)
        return result

    def _run_variant(self, mod):

        class Variant(TestCloudSendState):
            def setUp(inner):
                super().setUp()
                inner.module = mod.bind(vars(sb))
        result = unittest.TestResult()
        Variant("test_a_dead_pod_is_never_reported_delivered").run(result)
        return result

    def test_the_control_passes_the_no_op_fails_the_computation_and_the_real_mutant_is_caught(self):
        source = open(self.SOURCE).read()
        self.assertEqual(source.count(self.OLD), 1)
        control = self.run_dead_pod_test(source)
        self.assertEqual((control.failures, control.errors), ([], []), "control: the unmutated source passes")
        self.assertEqual(self.compute(source, source.replace(self.OLD, self.OLD)), 1, "a no-op mutation fails the computation")
        mutated = source.replace(self.OLD, self.NEW)
        self.assertEqual(self.compute(source, mutated), 0)
        result = self.run_dead_pod_test(mutated)
        self.assertEqual(len(result.failures), 1)
        self.assertIn(self.ANCHOR, result.failures[0][1])


class TestCloudSendState(TheDeliveredLineMutant, unittest.TestCase):
    def setUp(self):
        self.module = sb.load_module("cloud_worker").bind(vars(sb))
        self.prompts, self.calls = [], []

    def body(self, **over):
        body = {"worker": "coder7", "runtime_id": "pod/x", "phase": "Running", "idle_since": "2099-01-01T00:00:00Z",
                "deadlines": {"idle": FUTURE, "hard": FUTURE}, "endpoint": "https://pod.example/?access_token=" + TOKEN}
        body.update(over)
        return body

    def send(self, launcher, hard=FUTURE):
        module = self.module
        self.prompts = []
        record = {"worker": "coder7", "session": "ses_example", "hard_deadline": hard}
        args = argparse.Namespace(name="coder7", issue=None, interrupt=False, text=["hello"])
        config = {"call_timeout_seconds": 5}
        v2 = mock.Mock()
        v2.prompt.side_effect = lambda *a, **k: self.prompts.append(a) or {"id": "msg_1"}

        code, answer, stderr = launcher   # the launcher's real answer: the exit code and the stdout (a LIST of records for `status NAME`)

        class Child:
            returncode = code

            def communicate(self, timeout=None):
                return (answer if isinstance(answer, bytes) else json.dumps(answer).encode()), stderr.encode()

        def fake_popen(argv, **kw):
            self.calls.append((argv[-3], argv[-2]))
            return Child()

        patched = dict(build_engine=lambda *a, **k: (mock.Mock(), "state", config), intent_refusal=lambda *a: None,
                       gates_caller_kind=lambda *a: ("agent", "coder"))
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.dict(module.H, patched), mock.patch.object(module, "launcher_argv", lambda: ["launcher"]), mock.patch.object(module.subprocess, "Popen", fake_popen), \
                mock.patch.object(module, "Cluster", Probe), mock.patch.object(module, "tunnel", lambda *a: Probe()), \
                mock.patch.object(module, "remote_v2", lambda *a: v2), mock.patch.object(module, "_audit", lambda *a: None), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = module.cmd_send(args, None, 0, record)
        self.assertEqual(code, module.H["EXIT_OK"])
        self.assertEqual(len(self.prompts), 1, "the send prompts the endpoint in every case")
        self.assertNotIn(TOKEN, err.getvalue() + out.getvalue())
        return err.getvalue()

    def count(self, text, pattern):
        return len(re.findall(pattern, text, re.M))

    def test_a_running_pod_prints_its_state_and_delivered(self):
        text = self.send((0, [self.body()], ""))
        self.assertEqual(self.count(text, r"state of coder7: running=yes idle_deadline=%s hard_deadline=%s last_event=2099-01-01T00:00:00Z" % (FUTURE, FUTURE)), 1)
        self.assertEqual(self.count(text, "delivered to coder7"), 1)
        self.assertEqual(self.count(text, "WARNING"), 0)
        self.assertEqual(self.calls, [("status", "coder7")])

    def test_a_busy_pod_shows_its_last_event_as_unknown(self):
        text = self.send((0, [self.body(idle_since=None)], ""))
        self.assertIn("last_event=unknown", text)
        self.assertEqual(self.count(text, "delivered to"), 1)

    def test_a_dead_pod_is_never_reported_delivered(self):
        cases = {
            "past its hard deadline": self.send((0, [self.body(deadlines={"idle": FUTURE, "hard": PAST})], "")),
            "retired": self.send((2, b"", "unknown worker coder7"), hard=PAST),
            "unknown": self.send((2, b"", "unknown worker coder7")),
            "unread": self.send((1, b"", "boom " + TOKEN)),
        }
        for name, text in cases.items():
            self.assertEqual(self.count(text, "delivered to"), 0, "a dead pod is never reported delivered: " + name)
            self.assertEqual(self.count(text, "WARNING"), 1, name)
            self.assertEqual(self.count(text, r"state of coder7: running="), 1, name)

    def test_a_record_past_its_hard_deadline_warns_even_when_the_pod_still_answers(self):
        text = self.send((0, [self.body()], ""), hard=PAST)
        self.assertEqual((self.count(text, "delivered to"), self.count(text, "WARNING")), (0, 1))

    def test_a_dict_an_empty_list_or_a_longer_list_is_a_failed_read(self):
        for answer in (self.body(), [], [self.body(), self.body()], b"not json", ["x"]):
            text = self.send((0, answer, ""))
            self.assertEqual((self.count(text, "delivered to"), self.count(text, "WARNING")), (0, 1), repr(answer)[:30])


if __name__ == "__main__":
    unittest.main()
