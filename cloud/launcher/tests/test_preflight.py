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
    review_count = 1
    enforcement = "active"
    include = ["~DEFAULT_BRANCH"]
    conditions = "ok"  # "ok" uses include; None answers a ruleset without conditions
    redirect_code = 302
    redirect = None  # a Location to answer every GET with, as a 302
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
        if Stub.redirect:
            self.send_response(Stub.redirect_code)
            self.send_header("Location", Stub.redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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
            out = []
            for x in Stub.rules:
                r = {"type": x, "ruleset_id": 7, "ruleset_source_type": "Repository", "ruleset_source": REPO}
                if x == "pull_request":
                    r["parameters"] = {"required_approving_review_count": Stub.review_count}
                out.append(r)
            return self.reply(200, out)
        if path == "/repos/%s/rulesets/7" % REPO:
            cond = {"ref_name": {"include": Stub.include, "exclude": []}} if Stub.conditions == "ok" else None
            return self.reply(200, {"id": 7, "target": "branch", "enforcement": Stub.enforcement, "conditions": cond})
        if path == "/repos/%s/branches/main" % REPO:
            return self.reply(200, {"name": "main", "protected": Stub.protected})
        self.reply(404, {})

    def do_POST(self):
        Stub.methods.append("POST")
        self.reply(405, {})

    do_PUT = do_PATCH = do_DELETE = do_POST


class Other(http.server.BaseHTTPRequestHandler):
    seen = []  # any request that reached the second port

    def log_message(self, *a):
        pass

    def do_GET(self):
        Other.seen.append((self.path, self.headers.get("Authorization")))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


class Preflight(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
        cls.other = http.server.HTTPServer(("127.0.0.1", 0), Other)
        threading.Thread(target=cls.other.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.other.shutdown()

    def setUp(self):
        Stub.rules, Stub.protected, Stub.fail, Stub.methods, Stub.redirect = list(FULL), False, None, [], None
        Stub.review_count, Stub.enforcement, Stub.include, Stub.conditions = 1, "active", ["~DEFAULT_BRANCH"], "ok"
        Other.seen = []

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

    def test_the_review_count_must_be_at_least_one_unless_there_is_a_merge_queue(self):
        Stub.review_count = 0
        p = self.run_pf()
        self.assertEqual(p.returncode, 1)
        self.assertIn("required approving reviews", p.stdout)
        Stub.rules = ["merge_queue", "pull_request", "non_fast_forward", "deletion"]
        self.assertEqual(self.run_pf().returncode, 0)  # the queue stands in for reviews
        Stub.rules, Stub.review_count = list(FULL), 2
        self.assertEqual(self.run_pf().returncode, 0)

    def test_a_ruleset_that_is_not_enforced_or_does_not_cover_the_branch_is_refused(self):
        for enf in ("evaluate", "disabled"):
            Stub.enforcement = enf
            p = self.run_pf()
            self.assertEqual(p.returncode, 1, enf)
            self.assertIn("not enforced (enforcement is '%s')" % enf, p.stdout)
        Stub.enforcement = "active"
        for inc in (["refs/heads/release/*"], ["refs/heads/dev"], []):
            Stub.include = inc
            p = self.run_pf()
            self.assertEqual(p.returncode, 1, inc)
            self.assertIn("does not cover the branch", p.stdout)
        for inc in (["~ALL"], ["refs/heads/main"], ["refs/heads/m*"], ["refs/heads/**"]):
            Stub.include = inc
            self.assertEqual(self.run_pf().returncode, 0, inc)

    def test_an_odd_or_unreadable_ruleset_fails_closed_with_exit_2(self):
        Stub.conditions = None
        self.assertEqual(self.run_pf().returncode, 2)
        Stub.conditions, Stub.fail = "ok", "/rulesets/7"
        self.assertEqual(self.run_pf().returncode, 2)

    def test_classic_only_fails_closed_unless_accepted(self):
        Stub.rules, Stub.protected = ["non_fast_forward", "deletion"], True
        p = self.run_pf()
        self.assertEqual(p.returncode, 1)
        self.assertIn("classic protection: review requirement not readable with this token", p.stdout)
        self.assertIn("--accept-classic", p.stdout)
        q = self.run_pf("--accept-classic")
        self.assertEqual(q.returncode, 0)
        self.assertIn("WARNING: --accept-classic", q.stdout)

    def test_accept_classic_overrides_only_the_review_requirement(self):
        for rules, protected in (([], False), (["non_fast_forward", "deletion"], False), ([], True),
                                 (["pull_request", "deletion"], True), (["pull_request", "non_fast_forward"], True)):
            Stub.rules, Stub.protected = rules, protected
            p = self.run_pf("--accept-classic")
            self.assertEqual(p.returncode, 1, (rules, protected))
            self.assertNotIn("WARNING", p.stdout)
        for fail in ("/branches/main", "/rules/branches/main", "badjson"):
            Stub.rules, Stub.protected, Stub.fail = ["non_fast_forward", "deletion"], True, fail
            self.assertEqual(self.run_pf("--accept-classic").returncode, 2, fail)

    def test_no_redirect_is_followed_whatever_its_status_or_target(self):
        for code in (301, 302, 307, 308):
            for target in ("http://127.0.0.1:%d/leak" % self.other.server_port,  # another host (port)
                           "http://127.0.0.1:%d/leak" % self.srv.server_port,  # the same host
                           "/leak"):  # relative
                Stub.redirect, Stub.redirect_code, Stub.methods = target, code, []
                p = self.run_pf()
                self.assertEqual(p.returncode, 2, (code, target))
                self.assertIn("HTTP %d" % code, p.stdout)
                self.assertIn("redirect", p.stdout)
                self.assertNotIn(TOKEN, p.stdout + p.stderr)
                self.assertEqual(Other.seen, [])
                self.assertEqual(len(Stub.methods), 1, "only the first request was made")

    def test_bad_targets_are_refused(self):
        for bad in ("a/..", "../b", ".", "a/b/c", "a/", "/b", "a/./b"):
            p = subprocess.run([sys.executable, PREFLIGHT, bad], capture_output=True, text=True,
                               env=dict(os.environ, CW_PREFLIGHT_TOKEN_CMD="echo " + TOKEN))
            self.assertEqual(p.returncode, 2, bad)
        self.assertEqual(self.run_pf("--branch", "a/../b").returncode, 2)

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
