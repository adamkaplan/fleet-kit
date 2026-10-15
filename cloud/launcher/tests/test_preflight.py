"""Offline tests for cloud/launcher/preflight.py (S1, U2): a loopback stub for the GitHub API; no network."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PREFLIGHT = os.path.join(os.path.dirname(HERE), "preflight.py")
REPO = "acme-org/widget-service"
TOKEN = "ghs_synthetic" + "Qw8Rt2" * 6
FULL = ["pull_request", "non_fast_forward", "deletion"]


class Stub(http.server.BaseHTTPRequestHandler):
    rules = []
    protected = False
    fail = None  # a path suffix that answers 500, or "badjson"
    methods = []

    def log_message(self, *a):
        pass

    def reply(self, code, body):
        out = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        Stub.methods.append("GET")
        path = urlparse(self.path).path
        if Stub.fail == "badjson":
            return self.reply(200, b"<html>")
        if Stub.fail == "huge":
            return self.reply(200, b" " * 1_100_000 + b"{}")
        if Stub.fail and path.endswith(Stub.fail):
            return self.reply(500, {})
        if path == "/repos/" + REPO:
            return self.reply(200, {"default_branch": "main"})
        if path == "/repos/%s/rules/branches/main" % REPO:
            return self.reply(200, [{"type": t} for t in Stub.rules])
        if path == "/repos/%s/branches/main" % REPO:
            return self.reply(200, {"name": "main", "protected": Stub.protected})
        self.reply(404, {})

    def do_POST(self):
        Stub.methods.append("POST")
        self.reply(405, {})

    do_PUT = do_PATCH = do_DELETE = do_POST


class Preflight(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Stub.rules, Stub.protected, Stub.fail, Stub.methods = list(FULL), False, None, []

    def run_pf(self, *args, **env):
        e = dict(os.environ, CW_GITHUB_API_BASE=self.base, CW_PREFLIGHT_TOKEN_CMD="echo " + TOKEN)
        e.update(env)
        return subprocess.run([sys.executable, PREFLIGHT, REPO, *args], env=e, capture_output=True, text=True, timeout=60)

    def test_fully_protected_passes_and_only_reads(self):
        p = self.run_pf()
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("preflight ok: deletion, non_fast_forward, pull_request", p.stdout)
        self.assertEqual(set(Stub.methods), {"GET"})
        self.assertNotIn(TOKEN, p.stdout + p.stderr)

    def test_merge_queue_counts_as_refusing_direct_pushes(self):
        Stub.rules = ["merge_queue", "non_fast_forward", "deletion"]
        self.assertEqual(self.run_pf().returncode, 0)

    def test_each_missing_rule_fails_and_is_named(self):
        cases = [([], "pull_request or merge_queue"),
                 (["non_fast_forward", "deletion"], "pull_request or merge_queue"),
                 (["required_status_checks", "non_fast_forward", "deletion"], "pull_request or merge_queue"),
                 (["pull_request", "deletion"], "non_fast_forward"), (["pull_request", "non_fast_forward"], "deletion")]
        for rules, named in cases:
            Stub.rules = rules
            p = self.run_pf()
            self.assertEqual(p.returncode, 1, rules)
            self.assertIn("missing a " + named, p.stdout)
            self.assertNotIn("preflight ok", p.stdout)

    def test_classic_only_fails_closed_unless_accepted(self):
        Stub.rules, Stub.protected = [], True
        p = self.run_pf()
        self.assertEqual(p.returncode, 1)
        self.assertIn("classic protection: review requirement not readable with this token", p.stdout)
        self.assertIn("--accept-classic", p.stdout)
        q = self.run_pf("--accept-classic")
        self.assertEqual(q.returncode, 0)
        self.assertIn("WARNING: --accept-classic", q.stdout)

    def test_accept_classic_does_not_rescue_an_unprotected_branch(self):
        Stub.rules, Stub.protected = [], False
        self.assertEqual(self.run_pf("--accept-classic").returncode, 1)

    def test_unreadable_answers_exit_2(self):
        for fail in ("badjson", "/rules/branches/main", "/branches/main", "widget-service"):
            Stub.fail = fail
            p = self.run_pf()
            self.assertEqual(p.returncode, 2, fail)
            self.assertNotIn("preflight ok", p.stdout)
        Stub.fail = None
        self.assertEqual(self.run_pf(CW_PREFLIGHT_TOKEN_CMD="false").returncode, 2)

    def test_a_huge_answer_or_a_full_page_of_rules_exits_2(self):
        Stub.rules = ["x%d" % i for i in range(100)]
        self.assertEqual(self.run_pf().returncode, 2)
        Stub.fail = "huge"
        self.assertEqual(self.run_pf().returncode, 2)

    def test_bad_usage_and_non_loopback_base(self):
        self.assertEqual(subprocess.run([sys.executable, PREFLIGHT], capture_output=True).returncode, 2)
        self.assertEqual(self.run_pf(CW_GITHUB_API_BASE="http://example.com").returncode, 2)

    def test_a_planted_secret_in_the_token_command_env_appears_nowhere(self):
        planted = "planted-secret-value-9Lm"
        with tempfile.TemporaryDirectory() as d:
            for state in ("pass", "fail"):
                Stub.rules = list(FULL) if state == "pass" else []
                p = self.run_pf(PLANTED_SECRET=planted, CW_PREFLIGHT_TOKEN_CMD="echo " + TOKEN + "; : $PLANTED_SECRET",
                                TMPDIR=d)
                self.assertNotIn(planted, p.stdout + p.stderr)
                self.assertNotIn(TOKEN, p.stdout + p.stderr)
            self.assertEqual(os.listdir(d), [])


if __name__ == "__main__":
    unittest.main()
