"""The cloud send tells the pod's real state (#2057): one state line from the launcher's own `status`, and a warning instead of `delivered`
for a pod past its hard deadline, retired, unknown or unread. The send still prompts the endpoint in every case."""
import argparse
import contextlib
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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestCloudSendState(unittest.TestCase):
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

        def fake_launcher(runner, command, worker, options=(), timeout=0, **kw):
            self.calls.append((command, worker))
            return launcher

        patched = dict(build_engine=lambda *a, **k: (mock.Mock(), "state", config), intent_refusal=lambda *a: None,
                       gates_caller_kind=lambda *a: ("agent", "coder"))
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.dict(module.H, patched), mock.patch.object(module, "run_launcher", fake_launcher), \
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
        text = self.send((0, self.body(), ""))
        self.assertEqual(self.count(text, r"state of coder7: running=yes idle_deadline=%s hard_deadline=%s last_event=2099-01-01T00:00:00Z" % (FUTURE, FUTURE)), 1)
        self.assertEqual(self.count(text, "delivered to coder7"), 1)
        self.assertEqual(self.count(text, "WARNING"), 0)
        self.assertEqual(self.calls, [("status", "coder7")])

    def test_a_busy_pod_shows_its_last_event_as_unknown(self):
        text = self.send((0, self.body(idle_since=None), ""))
        self.assertIn("last_event=unknown", text)
        self.assertEqual(self.count(text, "delivered to"), 1)

    def test_a_dead_pod_is_never_reported_delivered(self):
        cases = {
            "past its hard deadline": self.send((0, self.body(deadlines={"idle": FUTURE, "hard": PAST}), "")),
            "retired": self.send((2, None, "unknown worker coder7"), hard=PAST),
            "unknown": self.send((2, None, "unknown worker coder7")),
            "unread": self.send((1, None, "boom")),
        }
        for name, text in cases.items():
            self.assertEqual(self.count(text, "delivered to"), 0, "a dead pod is never reported delivered: " + name)
            self.assertEqual(self.count(text, "WARNING"), 1, name)
            self.assertEqual(self.count(text, r"state of coder7: running="), 1, name)

    def test_a_record_past_its_hard_deadline_warns_even_when_the_pod_still_answers(self):
        text = self.send((0, self.body(), ""), hard=PAST)
        self.assertEqual((self.count(text, "delivered to"), self.count(text, "WARNING")), (0, 1))

    def test_a_non_dict_body_is_a_failed_read(self):
        text = self.send((0, None, ""))
        self.assertEqual((self.count(text, "delivered to"), self.count(text, "WARNING")), (0, 1))


if __name__ == "__main__":
    unittest.main()
