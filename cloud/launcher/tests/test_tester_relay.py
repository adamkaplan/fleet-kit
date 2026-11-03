"""Ask #2026, scenario S4: the hub's relay and the daemon's check with a stub backend and three registered pods.
A tester named for pull request A, a worker, and a tester that is the recorded author of pull request B each call verdict and prove.
The hub is the real one (bin/cloud_hub.py, bin/cloud_relay.py); the daemon side is the real Executor of bin/cloud_relay_daemon.py with a stub backend."""
import hashlib
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import cloud_hub  # noqa: E402
import cloud_relay  # noqa: E402
import cloud_relay_daemon as daemon  # noqa: E402
import plan_ledger  # noqa: E402

REPO, HEAD = "acme-org/widget-service", "a" * 40
A, B = REPO + "#12", REPO + "#13"
TESTER_A, WORKER, TESTER_B, CODER = "tst-a", "wrk-1", "tst-b", "coder-x"
SECRETS = {TESTER_A: "s-ta", WORKER: "s-w", TESTER_B: "s-tb", "daemon": "s-d"}
POD_ISSUE = {TESTER_A: 7, WORKER: 7, TESTER_B: 8}   # A is linked to issue 7, B to issue 8
LINKED = {A: {7}, B: {8}}
AUTHOR = {A: CODER, B: TESTER_B}   # the recorded author of each pull request


class Backend:
    def __init__(self):
        self.audits = []

    def live_head(self, pr):
        return HEAD

    def plan(self, pr):
        return {"source": REPO + "#1", "hash": "sha256:" + "b" * 64, "scenarios": {"S1": {"evaluator": "tester", "claim": "pr"}}}

    def pr_facts(self, pr):
        return {"author": AUTHOR[pr], "notice_senders": (AUTHOR[pr],)}   # the sender of the pull request's notice: the writer the daemon knows

    def evaluators(self, evaluator):
        return [TESTER_A, TESTER_B]   # the names the owner lists for testers: the worker is not among them

    def holder_of(self, repo, issue):
        return {7: TESTER_A, 8: CODER}.get(issue)

    def pr_issues(self, pr):
        return LINKED[pr]

    def audit(self, kind, pr, detail):
        self.audits.append(kind)


def sha(v):
    return hashlib.sha256(v.encode()).hexdigest()


class Rig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        os.mkdir(os.path.join(t, "state"))
        self.state = os.path.join(t, "state")
        with open(os.path.join(t, "secrets.json"), "w") as f:
            json.dump({k: sha(v) for k, v in SECRETS.items()}, f)
        with open(os.path.join(t, "agents.json"), "w") as f:
            json.dump({p: {"url": "http://127.0.0.1:1", "session": "s", "password_file": "x", "repo": REPO, "issue": i} for p, i in POD_ISSUE.items()}, f)
        with open(os.path.join(t, "decisions.json"), "w") as f:
            f.write('{"decisions": [], "errors": []}')
        agents = os.path.join(t, "agents.json")
        relay = cloud_relay.Relay(lambda: cloud_hub._read_json(agents, {}), wait=5.0)
        self.server = cloud_hub.make_hub(("127.0.0.1", 0), cloud_hub.make_secrets_reviewer(os.path.join(t, "secrets.json"), "hub-pods"),
                                         cloud_hub.Inbox(agents), [].append, os.path.join(t, "decisions.json"), relay=relay, daemon="daemon",
                                         namespace="hub-pods")
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.executor = daemon.Executor(self.state, Backend(), lambda: 1000)
        self.n = 0

    def http(self, method, path, who, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers={"Authorization": "Bearer " + SECRETS[who]})
            r = conn.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            conn.close()

    def call(self, pod, command, pr):
        """The pod's call through the hub, the daemon's Executor answering it once; returns (status, body)."""
        self.n += 1
        out = []
        body = {"call_id": "c%d" % self.n, "command": command, "pr": pr, "head": HEAD, "args": {"scenario": "S1", "result": "pass", "evidence": "saw it", "command": "x"}}
        thread = threading.Thread(target=lambda: out.append(self.http("POST", "/relay", pod, body)), daemon=True)
        thread.start()
        for _ in range(500):
            if daemon.pass_once(self.executor, lambda: self.http("GET", "/relay/pending", "daemon")[1],
                                lambda rid, b: self.http("POST", "/relay/result/" + rid, "daemon", b)):
                break
            time.sleep(0.02)
        thread.join(20)
        self.assertEqual(len(out), 1, "the pod's call got no answer")
        return out[0]

    def ledger(self):
        entries, problem = plan_ledger.read_ledger(self.state)
        self.assertIsNone(problem)
        return entries

    def refused(self, pod, command, pr, phrase):
        before = self.ledger()
        status, body = self.call(pod, command, pr)
        self.assertEqual(status, 403, (pod, command, pr, body))
        self.assertIn(phrase, body["error"], (pod, command, pr))
        self.assertEqual(self.ledger(), before)

    def case_worker_refused(self):
        self.refused(WORKER, "verdict", A, "not registered to record tester verdicts")
        self.refused(WORKER, "prove", A, "proof only from the pod holding the work item")


class TestTesterRelay(Rig):
    def test_s4_five_cases(self):
        # case 1: the tester named for A is accepted for A (verdict, then proof)
        for command in ("verdict", "prove"):
            status, body = self.call(TESTER_A, command, A)
            self.assertEqual(status, 200, body)
        # case 2: the same tester is refused for B (not linked to its issue), both commands
        self.refused(TESTER_A, "verdict", B, "pr not linked to the pod's issue")
        self.refused(TESTER_A, "prove", B, "pr not linked to the pod's issue")
        # case 3: the worker is refused
        self.case_worker_refused()
        # case 4: the tester that is the recorded author of B is refused
        self.refused(TESTER_B, "verdict", B, "author cannot record a verdict on its own work")
        self.refused(TESTER_B, "prove", B, "proof only from the pod holding the work item")
        # case 5: the accepted verdict reached the daemon's ledger under the pod's own name with the evaluator tester
        entries = self.ledger()
        self.assertEqual([(e["kind"], e["by"]) for e in entries], [("verdict", TESTER_A), ("proof", TESTER_A)])
        self.assertEqual(entries[0]["evaluator"], "tester")
        self.assertEqual(entries[0]["pr"], A)
        print("S4: 5 of 5 cases judged as stated (accepted for A, refused for B, worker refused, author refused, recorded by %s as tester)" % entries[0]["by"])

    def test_s4_a_relay_that_records_every_pod_as_tester_is_caught(self):
        self.case_worker_refused()   # the control first: the real rule refuses the worker
        with mock.patch.object(plan_ledger, "may_record_verdict", lambda *a, **k: (True, None)):   # the mutant: every pod may record
            with self.assertRaisesRegex(AssertionError, r"200 != 403 : \(.wrk-1., .verdict."):
                self.case_worker_refused()


if __name__ == "__main__":
    unittest.main()
