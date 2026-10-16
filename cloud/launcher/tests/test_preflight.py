"""Offline tests for cloud/launcher/preflight.py (S1, U2): a loopback stub for the GitHub API; no network."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import unquote, urlparse

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
    exclude = []
    target = "branch"
    default = "main"
    rule_ids = {}  # rule type -> ruleset id (default 7)
    extra = {}  # ruleset id -> its answer, for a second ruleset
    org = False  # answer ruleset 7's rules as Organization-sourced
    no_id = False  # answer rules without a ruleset_id
    redirect_code = 302
    redirect = None  # a Location to answer every GET with, as a 302
    fail_code = 500
    fail = None  # a path suffix that answers 500, or "badjson"
    methods = []
    org_code = None  # status (or "drop") answered for any /orgs/ GET
    org_extra = []  # extra organization-sourced rules (ruleset 9) on top of the repository's own
    org_types = []  # rule types sourced from unreadable-able organization ruleset 9

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
        path = unquote(urlparse(self.path).path)
        if path.startswith("/orgs/") and Stub.org_code:
            if Stub.org_code == "drop":
                self.close_connection = True
                self.connection.close()
                return
            return self.reply(Stub.org_code, {})
        if Stub.fail == "badjson":
            return self.reply(200, b"<html>")
        if Stub.fail == "huge":
            return self.reply(200, b" " * 1_100_000 + b"{}")
        if Stub.fail and path.endswith(Stub.fail):
            return self.reply(Stub.fail_code, {})
        if path == "/repos/" + REPO:
            return self.reply(200, {"default_branch": Stub.default})
        if path.startswith("/repos/%s/rules/branches/" % REPO):
            out = []
            for x in Stub.rules:
                r = {"type": x, "ruleset_id": Stub.rule_ids.get(x, 7), "ruleset_source_type": "Repository", "ruleset_source": REPO}
                if Stub.org:
                    r.update(ruleset_source_type="Organization", ruleset_source="acme-org")
                if x in Stub.org_types:
                    r.update(ruleset_id=9, ruleset_source_type="Organization", ruleset_source="acme-org")
                if Stub.no_id:
                    del r["ruleset_id"]
                if x == "pull_request":
                    r["parameters"] = {"required_approving_review_count": Stub.review_count}
                out.append(r)
            for x in Stub.org_extra:
                out.append({"type": x, "ruleset_id": 9, "ruleset_source_type": "Organization", "ruleset_source": "acme-org"})
            return self.reply(200, out)
        if path in ("/repos/%s/rulesets/7" % REPO, "/orgs/acme-org/rulesets/7"):
            cond = {"ref_name": {"include": Stub.include, "exclude": Stub.exclude}} if Stub.conditions == "ok" else None
            return self.reply(200, {"id": 7, "target": Stub.target, "enforcement": Stub.enforcement, "conditions": cond})
        for rid, body in Stub.extra.items():
            if path == "/repos/%s/rulesets/%d" % (REPO, rid):
                return self.reply(200, body)
        if path.startswith("/repos/%s/branches/" % REPO):
            return self.reply(200, {"name": "x", "protected": Stub.protected})
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
        Stub.exclude, Stub.target, Stub.default, Stub.rule_ids, Stub.extra, Stub.org, Stub.no_id = [], "branch", "main", {}, {}, False, False
        Stub.org_code, Stub.org_types, Stub.fail_code, Stub.org_extra = None, [], 500, []
        Other.seen = []
        self.dir = tempfile.mkdtemp()

    def approvals(self, content, raw=False):
        path = os.path.join(self.dir, "approvals.json")
        with open(path, "w") as f:
            f.write(content if raw else json.dumps(content))
        return path

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

    def covers_exit(self, include, branch, exclude=()):
        Stub.include, Stub.exclude, Stub.default = include, list(exclude), branch
        return self.run_pf()

    def test_github_glob_semantics_star_stops_at_slash_and_double_star_crosses(self):
        cases = [(["refs/heads/release/*"], "release/1", 0), (["refs/heads/release/*/x"], "release/a/b", 1),
                 (["refs/heads/release/*/x"], "release/a/x", 0), (["refs/heads/*"], "a/b", 1), (["refs/heads/*"], "ab", 0),
                 (["refs/heads/**"], "a/b", 0), (["**"], "a/b", 0), (["*"], "a/b", 1), (["refs/heads/release/**"], "release/a/b", 0),
                 (["refs/heads/rel?ase/1"], "release/1", 0), (["refs/heads/rel?ase/1"], "rel/ase/1", 1),
                 (["refs/heads/a.c"], "abc", 1), (["~ALL"], "a/b", 0)]
        for include, branch, code in cases:
            self.assertEqual(self.covers_exit(include, branch).returncode, code, (include, branch))

    def test_a_pattern_not_understood_is_not_covering_and_is_named(self):
        for pat in ("refs/heads/[ab]*", "~SOMETHING", "~", ""):
            p = self.covers_exit([pat], "main")
            self.assertEqual(p.returncode, 1, pat)
            self.assertIn("does not cover the branch", p.stdout)
        p = self.covers_exit(["refs/heads/[ab]*"], "main")
        self.assertIn("a pattern was not understood", p.stdout)

    def test_an_exclude_removes_coverage_and_an_unknown_exclude_removes_it_too(self):
        self.assertEqual(self.covers_exit(["~ALL"], "main", ["refs/heads/main"]).returncode, 1)
        self.assertEqual(self.covers_exit(["~ALL"], "main", ["refs/heads/other"]).returncode, 0)
        p = self.covers_exit(["~ALL"], "main", ["refs/heads/[m]ain"])
        self.assertEqual(p.returncode, 1)
        self.assertIn("not understood", p.stdout)

    def test_default_branch_tilde_only_covers_the_default_branch(self):
        Stub.include, Stub.default = ["~DEFAULT_BRANCH"], "main"
        self.assertEqual(self.run_pf("--branch", "release/1").returncode, 1)
        Stub.default = "release/1"
        self.assertEqual(self.run_pf().returncode, 0)

    def test_a_rule_without_a_ruleset_id_fails_closed(self):
        Stub.no_id = True
        p = self.run_pf()
        self.assertEqual(p.returncode, 2)
        self.assertIn("does not name its ruleset", p.stdout)

    def test_an_organization_ruleset_is_read_from_the_org_path(self):
        Stub.org = True
        self.assertEqual(self.run_pf().returncode, 0)
        Stub.fail = "/rulesets/7"  # a token that cannot read the organization's ruleset fails closed
        self.assertEqual(self.run_pf().returncode, 2)

    def org_only(self, code=403, types=None):
        Stub.org_types = types or list(FULL)
        Stub.org_code = code

    ENTRY = {"approved_by": "the owner", "approved_at": "2026-10-16", "reason": "protected by the org ruleset"}

    def test_a_403_on_an_org_ruleset_is_not_counted_and_the_repo_own_rules_can_pass(self):
        Stub.rules = list(FULL)
        Stub.org_extra = ["deletion"]  # an unreadable organization ruleset on top of the repository's own three
        Stub.org_code = 403
        p = self.run_pf()
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("organization ruleset 9: not readable with this token, not counted", p.stdout)
        self.assertIn("preflight ok: deletion, non_fast_forward, pull_request", p.stdout)

    def test_only_unreadable_org_rules_fail_with_the_approvals_pointer(self):
        self.org_only()
        p = self.run_pf()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("not readable with this token, not counted", p.stdout)
        self.assertIn("an owner approval entry is needed: see CW_REPO_APPROVALS", p.stdout)
        self.assertEqual(self.run_pf(CW_REPO_APPROVALS=self.approvals({"acme-org/other": self.ENTRY})).returncode, 1)

    def test_a_valid_approval_passes_loudly_every_run(self):
        self.org_only()
        f = self.approvals({REPO.upper(): self.ENTRY})
        for _ in range(2):
            p = self.run_pf(CW_REPO_APPROVALS=f)
            self.assertEqual(p.returncode, 0, p.stdout)
            self.assertIn("preflight ok: protection NOT VERIFIED (organization ruleset 9 unreadable); owner-approved by "
                          "the owner on 2026-10-16: protected by the org ruleset", p.stdout)

    def test_a_bad_approvals_file_or_entry_exits_2(self):
        self.org_only()
        for bad in ({"approved_by": "x", "approved_at": "d"}, {**self.ENTRY, "reason": " "}, {**self.ENTRY, "approved_by": 5}, "x"):
            self.assertEqual(self.run_pf(CW_REPO_APPROVALS=self.approvals({REPO: bad})).returncode, 2, bad)
        self.assertEqual(self.run_pf(CW_REPO_APPROVALS=self.approvals("{not json", raw=True)).returncode, 2)
        self.assertEqual(self.run_pf(CW_REPO_APPROVALS=self.approvals("[]", raw=True)).returncode, 2)
        self.assertEqual(self.run_pf(CW_REPO_APPROVALS=os.path.join(self.dir, "none.json")).returncode, 2)
        self.assertEqual(self.run_pf(CW_REPO_APPROVALS=self.dir).returncode, 2)

    def test_the_approvals_file_is_not_read_when_it_is_not_the_reason(self):
        Stub.rules = ["pull_request", "deletion"]  # non_fast_forward is visibly missing and no unreadable rule carries it
        Stub.org_types = ["deletion"]
        Stub.org_code = 403
        f = self.approvals({REPO: self.ENTRY})
        self.assertEqual(self.run_pf(CW_REPO_APPROVALS=f).returncode, 1)
        self.assertEqual(self.run_pf(CW_REPO_APPROVALS=os.path.join(self.dir, "none")).returncode, 1)  # not even read
        Stub.rules, Stub.org_types = [], []
        self.assertEqual(self.run_pf(CW_REPO_APPROVALS=f).returncode, 1)  # an unprotected repo

    def test_an_approval_does_not_rescue_a_readable_ruleset_without_a_rule_or_with_a_problem(self):
        Stub.rules, Stub.enforcement = ["pull_request", "deletion"], "evaluate"
        Stub.org_types, Stub.org_code = ["pull_request"], 403
        self.assertEqual(self.run_pf(CW_REPO_APPROVALS=self.approvals({REPO: self.ENTRY})).returncode, 1)

    def test_only_a_403_on_an_org_ruleset_is_not_readable(self):
        for code in (404, 500, "drop"):
            self.org_only(code)
            p = self.run_pf(CW_REPO_APPROVALS=self.approvals({REPO: self.ENTRY}))
            self.assertEqual(p.returncode, 2, (code, p.stdout))
            self.assertNotIn("not counted", p.stdout)
        Stub.org_code = None
        Stub.org_types = []
        Stub.org = False
        Stub.fail = "/rulesets/7"
        self.assertEqual(self.run_pf().returncode, 2)

    def test_a_403_on_the_repository_own_ruleset_or_rules_endpoint_exits_2(self):
        Stub.fail = "/rulesets/7"
        Stub.fail_code = 403
        self.assertEqual(self.run_pf().returncode, 2)
        Stub.fail = "/rules/branches/main"
        self.assertEqual(self.run_pf().returncode, 2)

    def test_a_ruleset_that_is_not_a_branch_ruleset_fails_closed(self):
        Stub.target = "push"
        self.assertEqual(self.run_pf().returncode, 2)

    def test_an_unknown_enforcement_value_fails_closed(self):
        Stub.enforcement = "bogus"
        self.assertEqual(self.run_pf().returncode, 2)

    def test_two_rulesets_each_judged_and_only_enforced_ones_count(self):
        Stub.rule_ids = {"deletion": 8}
        Stub.extra = {8: {"id": 8, "target": "branch", "enforcement": "disabled",
                          "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}}}}
        p = self.run_pf()
        self.assertEqual(p.returncode, 1)
        self.assertIn("ruleset 8 is not enforced", p.stdout)
        self.assertIn("missing a deletion rule", p.stdout)
        Stub.extra[8]["enforcement"] = "active"
        self.assertEqual(self.run_pf().returncode, 0)

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
