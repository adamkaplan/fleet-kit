#!/usr/bin/env python3
"""Offline tests of ask #1525: the lane's send bridges a restart of the hub's pod by retrying for a stated window.

S2 a send that fails five times then succeeds; S3 a hub that never answers; S4 the documented limit of a restart
(the remembered keys are forgotten); S5 the pod's relay command keeps one attempt; S6 the documentation sentences;
mutants run a scratch copy of the client in a child process.
"""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BIN = os.path.join(ROOT, "bin")
CLIENT = os.path.join(BIN, "cloud_client.py")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeHub:
    """Counts deliveries by key; `restart()` forgets the remembered keys, as a restart of the pod does."""

    def __init__(self, fail_first=0):
        self.fail_first, self.requests, self.keys, self.deliveries = fail_first, [], [], 0
        self.seen = set()

    def restart(self):
        self.seen = set()

    def __call__(self, request):
        body = request["body"]
        self.requests.append(request)
        self.keys.append(body.get("key"))
        if len(self.requests) <= self.fail_first:
            return {"status": 0, "error": "ConnectionRefusedError"}
        if body.get("key") in self.seen:
            return {"status": 200, "duplicate": True}
        self.seen.add(body.get("key"))
        self.deliveries += 1
        return {"status": 200}


def child(source, code):
    """Run `code` against a scratch copy of the client held in a child process; returns the exit status."""
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "cloud_client.py"), "w") as f:
            f.write(source)
        return subprocess.run([sys.executable, "-c", code], cwd=d, capture_output=True, text=True, timeout=60).returncode


SCRIPT_S2 = """
import cloud_client as c, sys
open('t','w').write('tok')
n=[0]; keys=set(); dl=[0]
def post(r):
    n[0]+=1; keys.add(r['body'].get('key'))
    if n[0] <= 5: return {'status':0,'error':'x'}
    dl[0]+=1; return {'status':200}
ok,_=c.send('a','b','hi',post,token_path='t',sleep=lambda s: None)
sys.exit(0 if ok and dl[0]==1 and n[0]==6 and len(keys)==1 else 1)
"""
SCRIPT_S3 = """
import cloud_client as c, sys
open('t','w').write('tok')
ok,line=c.send('a','b','hi',lambda r: {'status':0,'error':'x'},token_path='t',sleep=lambda s: None)
sys.exit(1 if ok else 0)
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.client = load(CLIENT, "cloud_client_under_test")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token = os.path.join(self.tmp.name, "token")
        with open(self.token, "w") as f:
            f.write("tok-secret\n")
        self.waits = []

    def send(self, post, **kw):
        return self.client.send("worker-a", "boss-a", "hello", post, token_path=self.token, sleep=self.waits.append, **kw)


class S2Bridged(Base):
    def test_send_is_bridged_on_the_sixth_try(self):
        hub = FakeHub(fail_first=5)
        ok, line = self.send(hub)
        keys = set(hub.keys)
        print("S2: deliveries %d, tries %d, keys %d, waits %s" % (hub.deliveries, len(hub.requests), len(keys), sum(self.waits)))
        self.assertTrue(ok, line)
        self.assertEqual((hub.deliveries, len(hub.requests), len(keys)), (1, 6, 1))
        self.assertEqual(self.waits, [0.5, 1.0, 2.0, 4.0, 8.0])
        self.assertEqual(sum(self.waits), 15.5)
        self.assertEqual(self.client.RETRY_WINDOW_SECONDS, 15.5)

    def test_report_is_bridged_too(self):
        hub = FakeHub(fail_first=5)
        ok, line = self.client.report("worker-a", "boss-a", "done", hub, token_path=self.token, sleep=self.waits.append)
        self.assertTrue(ok, line)
        self.assertEqual((hub.deliveries, len(hub.requests), len(set(hub.keys))), (1, 6, 1))

    def test_mutant_three_tries_fails_the_scenario(self):
        src = open(CLIENT).read()
        self.assertEqual(src.count("RETRIES = 6\nMAX_RETRIES = 6"), 1)
        mutant = src.replace("RETRIES = 6\nMAX_RETRIES = 6", "RETRIES = 3\nMAX_RETRIES = 3")
        self.assertEqual(child(src, SCRIPT_S2), 0)
        self.assertEqual(child(mutant, SCRIPT_S2), 1)


class S3Exhausted(Base):
    def test_never_answers_ends_after_six_tries_with_one_line(self):
        before = sorted(os.listdir(self.tmp.name))
        hub = FakeHub(fail_first=10 ** 6)
        ok, line = self.send(hub)
        after = sorted(os.listdir(self.tmp.name))
        print("S3: tries %d, lines %d, files %d" % (len(hub.requests), len(line.splitlines()), len(after) - len(before)))
        self.assertFalse(ok)
        self.assertEqual(len(hub.requests), 6)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("cloud channel unavailable", line)
        self.assertIn("after 6 tries", line)
        self.assertEqual(before, after)

    def test_command_line_send_exits_1(self):
        cli = load(os.path.join(BIN, "cloud_cli.py"), "cloud_cli_under_test")
        real = cli.cloud_client.send
        cli.cloud_client.send = lambda *a, **k: real(*a, sleep=self.waits.append, **k)
        hub = FakeHub(fail_first=10 ** 6)
        out = io.StringIO()
        env = {"FLEET_AGENT_NAME": "worker-a", "FLEET_CLOUD_CHANNEL_URL": "http://127.0.0.1:1/message",
               "FLEET_CLOUD_TOKEN_FILE": self.token}
        with contextlib.redirect_stdout(out):
            code = cli.main(["send", "boss-a", "hello"], env, post=hub)
        self.assertEqual(code, 1)
        self.assertEqual(len(hub.requests), 6)
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        self.assertNotIn("tok-secret", out.getvalue())

    def test_mutant_success_after_exhausting_fails_the_scenario(self):
        src = open(CLIENT).read()
        old = 'return False, "cloud channel unavailable'
        self.assertEqual(src.count(old), 1)
        mutant = src.replace(old, 'return True, "cloud channel unavailable')
        self.assertEqual(child(src, SCRIPT_S3), 0)
        self.assertEqual(child(mutant, SCRIPT_S3), 1)


class S4Restart(Base):
    def test_restart_forgets_keys_so_a_retry_is_delivered_twice(self):
        """The documented limit of a restart (docs/cloud-channel.md): the keys are held in memory only, so a retry
        that reaches the new hub after a restart is delivered a second time."""
        hub = FakeHub()
        self.send(hub, key="k1")
        hub.restart()
        self.send(hub, key="k1")
        with_restart = hub.deliveries
        calm = FakeHub()
        self.send(calm, key="k1")
        self.send(calm, key="k1")
        print("S4: with restart %d, without restart %d" % (with_restart, calm.deliveries))
        self.assertEqual((with_restart, calm.deliveries), (2, 1))


class S5Relay(Base):
    def test_relay_makes_one_attempt_and_prints_the_call_id(self):
        cli = load(os.path.join(BIN, "cloud_cli.py"), "cloud_cli_relay_under_test")
        attempts = []

        class Refusing:
            def open(self, req, timeout=None):
                attempts.append(req.full_url)
                raise ConnectionRefusedError("refused")

        cli.OPENER = Refusing()
        env = {"FLEET_CLOUD_CHANNEL_URL": "http://127.0.0.1:1/message", "FLEET_CLOUD_TOKEN_FILE": self.token}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["relay", "report", "--pr", "o/r#1", "--call-id", "call-77"], env)
        printed = out.getvalue()
        print("S5: attempts %d, call id printed, status %d" % (len(attempts), code))
        self.assertEqual((len(attempts), code), (1, 1))
        self.assertIn("call-77", printed)
        self.assertNotIn("tok-secret", printed)


class S6Docs(unittest.TestCase):
    def test_five_sentences_are_documented(self):
        with open(os.path.join(ROOT, "docs", "cloud-channel.md")) as f:
            text = " ".join(f.read().split())
        phrases = [
            "A restart of the hub's pod loses the remembered dedupe keys, the relay queue, a report claimed and not yet delivered, and the decisions file on the pod's volume.",
            "The lane's send retries for a window of 15 seconds (six tries, waits of 0.5, 1, 2, 4 and 8 seconds) with the same key, so a restart of the hub's pod shorter than that is bridged.",
            "When the hub stays down longer, the send ends with one line that says the channel is unavailable and nothing is queued on disk: the lane sends again.",
            "a message whose first delivery reached the agent and whose retry reaches the new hub after a restart is delivered twice, because the keys are held in memory only.",
            "A second replica is REJECTED for now",
        ]
        self.assertEqual([p for p in phrases if p in text], phrases)
        self.assertIn("measured by a tester after the merge", text)
        for word in ("100m CPU", "128Mi", "pod disruption budget", "replicas"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
