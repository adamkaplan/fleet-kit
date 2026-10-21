"""Offline tests for cloud/launcher/preflight.py (S1, U2): a loopback stub for the GitHub API; no network."""
import contextlib
import hashlib
import io
import http.server
import shutil
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
    org_hint = None  # retry | remaining | body: rate-limit signs on the /orgs/ answer
    org_code = None  # status (or "drop") answered for any /orgs/ GET
    org_extra = []  # extra organization-sourced rules (ruleset 9) on top of the repository's own
    org_types = []  # rule types sourced from unreadable-able organization ruleset 9
    bypass = []  # bypass_actors answered on ruleset 7
    auto_merge = False  # allow_auto_merge answered on the repository

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
            if Stub.org_hint:
                out = b'{"message": "You have exceeded a secondary rate limit"}' if Stub.org_hint == "body" else b"{}"
                self.send_response(Stub.org_code)
                if Stub.org_hint == "retry":
                    self.send_header("Retry-After", "60")
                if Stub.org_hint == "remaining":
                    self.send_header("X-RateLimit-Remaining", "0")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            return self.reply(Stub.org_code, {})
        if Stub.fail == "badjson":
            return self.reply(200, b"<html>")
        if Stub.fail == "huge":
            return self.reply(200, b" " * 1_100_000 + b"{}")
        if Stub.fail and path.endswith(Stub.fail):
            return self.reply(Stub.fail_code, {})
        if path == "/repos/" + REPO:
            return self.reply(200, {"default_branch": Stub.default, "allow_auto_merge": Stub.auto_merge})
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
            return self.reply(200, {"id": 7, "target": Stub.target, "enforcement": Stub.enforcement, "conditions": cond,
                                    "bypass_actors": Stub.bypass})
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
        Stub.org_code, Stub.org_types, Stub.fail_code, Stub.org_extra, Stub.org_hint = None, [], 500, [], None
        Stub.bypass, Stub.auto_merge = [], False
        Other.seen = []
        self.dir = tempfile.mkdtemp()
        # a private copy of the program: its repo-approvals.json is the only approvals file there is
        self.copy = os.path.join(self.dir, "launcher")
        os.makedirs(self.copy)
        for n in ("preflight.py", "app_token.py", "token_mint.py", "repo-approvals.json"):
            shutil.copy(os.path.join(os.path.dirname(PREFLIGHT), n), self.copy)
        self.prog = os.path.join(self.copy, "preflight.py")

    def approvals(self, content, raw=False):
        """Write the private copy's repo-approvals.json; returns its path."""
        path = os.path.join(self.copy, "repo-approvals.json")
        with open(path, "w") as f:
            f.write(content if raw else json.dumps(content))
        return path

    def run_pf(self, *args, **env):
        e = dict(os.environ, CW_GITHUB_API_BASE=self.base, CW_PREFLIGHT_TOKEN_CMD="echo " + TOKEN)
        e.update(env)
        return subprocess.run([sys.executable, self.prog, REPO, *args], env=e, capture_output=True, text=True, timeout=60)

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

    KEY = hashlib.sha256(REPO.lower().encode()).hexdigest()
    ENTRY = {"approved_by": "the-owner", "approved_at": "2026-10-16", "reason": "protected by the org ruleset", "pr": "12"}
    LOUD = ("preflight ok: protection NOT VERIFIED (organization ruleset 9 unreadable); owner-approved by "
            "the-owner on 2026-10-16 (pr #12): protected by the org ruleset")

    def test_a_403_on_an_org_ruleset_is_not_counted_and_the_repo_own_rules_can_pass(self):
        Stub.org_extra = ["deletion"]  # an unreadable organization ruleset on top of the repository's own three
        Stub.org_code = 403
        p = self.run_pf()
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("organization ruleset 9: not readable with this token, not counted", p.stdout)
        self.assertIn("preflight ok: deletion, non_fast_forward, pull_request", p.stdout)

    def test_S1_the_shipped_file_is_empty_and_an_org_only_repo_fails_with_the_pointer(self):
        with open(os.path.join(os.path.dirname(PREFLIGHT), "repo-approvals.json")) as f:
            self.assertEqual(json.load(f), {})
        self.org_only()
        p = self.run_pf()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("not readable with this token, not counted", p.stdout)
        self.assertIn("an owner approval entry is needed", p.stdout)

    def test_S1_a_valid_entry_passes_loudly_and_matches_the_lowercased_name(self):
        self.org_only()
        self.approvals({self.KEY: self.ENTRY})
        p = self.run_pf()
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn(self.LOUD, p.stdout)

    def test_S2_absent_malformed_incomplete_or_other_repo_fails_safe(self):
        self.org_only()
        os.remove(os.path.join(self.copy, "repo-approvals.json"))
        self.assertEqual(self.run_pf().returncode, 2)  # absent
        for raw in ("{not json", "[]", ""):
            self.approvals(raw, raw=True)
            self.assertEqual(self.run_pf().returncode, 2, raw)
        for field in self.ENTRY:  # each field missing or empty
            for bad in (None, " ", 5):
                e = dict(self.ENTRY)
                e.pop(field) if bad is None else e.update({field: bad})
                self.approvals({self.KEY: e})
                self.assertEqual(self.run_pf().returncode, 2, (field, bad))
        self.approvals({self.KEY: "x"})
        self.assertEqual(self.run_pf().returncode, 2)
        # an entry for another repository (or the unhashed name) is ignored: an ordinary failure, exit 1
        self.approvals({hashlib.sha256(b"acme-org/other").hexdigest(): self.ENTRY, REPO: self.ENTRY})
        self.assertEqual(self.run_pf().returncode, 1)

    def test_duplicate_keys_are_refused_in_every_shape(self):
        self.org_only()
        e = json.dumps(self.ENTRY)
        bad = json.dumps(dict(self.ENTRY, reason=""))
        other = json.dumps(dict(self.ENTRY, approved_by="someone-else"))
        for name, raw in (
                ("repo hash twice", '{"%s": %s, "%s": %s}' % (self.KEY, e, self.KEY, other)),
                ("field twice", '{"%s": {"approved_by": "a", "approved_by": "b", "approved_at": "2026-10-16", '
                                '"reason": "r", "pr": "1"}}' % self.KEY),
                ("malformed first, valid last", '{"%s": %s, "%s": %s}' % (self.KEY, bad, self.KEY, e))):
            self.approvals(raw, raw=True)
            p = self.run_pf()
            self.assertEqual(p.returncode, 2, name)
            self.assertNotIn("NOT VERIFIED", p.stdout)

    def test_a_deeply_nested_file_is_unreadable_not_a_traceback(self):
        self.org_only()
        self.approvals("[" * 65000, raw=True)
        p = self.run_pf()
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("Traceback", p.stderr)

    def test_control_and_bidi_characters_in_a_field_refuse_the_entry(self):
        self.org_only()
        for ch in ("\x00", "\x1b", "\x7f", "\x85", "\x9b", "\u2028", "\u2029", "\u202a", "\u202e", "\u2066", "\u2069"):
            for field in ("reason", "approved_by"):
                self.approvals({self.KEY: dict(self.ENTRY, **{field: "ab" + ch + "cd"})})
                p = self.run_pf()
                self.assertEqual(p.returncode, 2, (field, ch))
                self.assertNotIn(ch, p.stdout)

    def test_a_symlink_to_the_program_reads_the_checkouts_file_not_one_beside_the_link(self):
        self.org_only()
        self.approvals({})  # the checkout's own file grants nothing
        other = os.path.join(self.dir, "elsewhere")
        os.makedirs(other)
        with open(os.path.join(other, "repo-approvals.json"), "w") as f:
            json.dump({self.KEY: self.ENTRY}, f)  # a grant beside the link
        link = os.path.join(other, "preflight.py")
        os.symlink(self.prog, link)
        self.prog = link
        p = self.run_pf()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertNotIn("NOT VERIFIED", p.stdout)

    def test_a_403_with_a_sign_of_a_rate_limit_is_refused_a_plain_one_is_not_counted(self):
        self.approvals({self.KEY: self.ENTRY})
        for hint in ("retry", "remaining", "body"):
            self.org_only(403)
            Stub.org_hint = hint
            p = self.run_pf()
            self.assertEqual(p.returncode, 2, (hint, p.stdout))
            self.assertNotIn("not counted", p.stdout)
            self.assertNotIn("60", p.stdout)
        Stub.org_hint = None
        p = self.run_pf()
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("not counted", p.stdout)

    def test_S3_the_loud_line_is_printed_on_every_run(self):
        self.org_only()
        self.approvals({self.KEY: self.ENTRY})
        for _ in range(3):
            self.assertIn(self.LOUD, self.run_pf().stdout)

    def test_S4_an_approval_rescues_no_visible_missing_rule_and_no_other_problem(self):
        self.approvals({self.KEY: self.ENTRY})
        Stub.rules = ["pull_request", "deletion"]  # non_fast_forward visibly missing, no unreadable rule carries it
        Stub.org_types, Stub.org_code = ["deletion"], 403
        self.assertEqual(self.run_pf().returncode, 1)
        Stub.rules, Stub.org_types = [], []  # an unprotected repository
        self.assertEqual(self.run_pf().returncode, 1)
        Stub.rules, Stub.enforcement = ["pull_request", "deletion"], "evaluate"  # a readable ruleset not enforced
        Stub.org_types = ["non_fast_forward"]
        self.assertEqual(self.run_pf().returncode, 1)
        Stub.rules, Stub.enforcement = ["non_fast_forward", "deletion"], "active"  # no review rule anywhere
        Stub.org_types = []
        self.assertEqual(self.run_pf().returncode, 1)

    def test_S5_no_override_path_an_environment_setting_a_flag_or_a_file_elsewhere_is_ignored(self):
        self.org_only()
        outside = os.path.join(self.dir, "elsewhere.json")
        with open(outside, "w") as f:
            json.dump({self.KEY: self.ENTRY, REPO: self.ENTRY}, f)
        for env in ({"CW_REPO_APPROVALS": outside}, {"CW_REPO_APPROVALS": "1"}, {"CW_APPROVALS": outside}):
            p = self.run_pf(**env)
            self.assertEqual(p.returncode, 1, p.stdout)
            self.assertNotIn("NOT VERIFIED", p.stdout)
        for flag in ("--approve", "--approvals=" + outside, "--accept-org"):
            self.assertEqual(self.run_pf(flag).returncode, 2)  # not an option: usage, exit 2
        with open(PREFLIGHT) as f:
            self.assertNotIn("CW_REPO_APPROVALS", f.read())

    def test_S6_the_approvals_file_is_a_top_tier_credentials_path_in_the_impact_matrix(self):
        matrix = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "docs", "review-laws", "impact-matrix.json")
        with open(matrix) as f:
            paths = json.load(f)["owner_hold"]["top_tier_paths"]["credentials"]
        self.assertEqual(paths.count("cloud/launcher/repo-approvals.json"), 1)

    def test_S4_only_a_403_on_an_org_ruleset_is_not_readable(self):
        self.approvals({self.KEY: self.ENTRY})
        for code in (404, 500, "drop"):
            self.org_only(code)
            p = self.run_pf()
            self.assertEqual(p.returncode, 2, (code, p.stdout))
            self.assertNotIn("not counted", p.stdout)
        Stub.org_code, Stub.org_types, Stub.org = None, [], False
        Stub.fail = "/rulesets/7"
        self.assertEqual(self.run_pf().returncode, 2)

    def test_a_403_on_the_repository_own_ruleset_or_rules_endpoint_exits_2(self):
        Stub.fail, Stub.fail_code = "/rulesets/7", 403
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


# ---- #1031: the new checks and the one WARN-vs-BLOCK table ----
sys.path.insert(0, os.path.dirname(PREFLIGHT))
import preflight as pf  # noqa: E402

CLEAN = FULL + ["required_status_checks"]   # no open action


def _open_bypass():
    Stub.bypass = [{"actor_id": 1, "actor_type": "Integration", "bypass_mode": "always"}]


def _open_auto_merge():
    Stub.auto_merge = True


def _open_no_status_checks():
    Stub.rules = list(FULL)


def _open_queue():
    Stub.rules = CLEAN + ["merge_queue"]


# DATA: one row per key of preflight.CHECK_MODES: how to make that one action open on the stub (a completeness test checks the keys)
OPENERS = {"bypass_actor": _open_bypass, "auto_merge": _open_auto_merge, "required_status_checks": _open_no_status_checks,
           "merge_queue_change": _open_queue}
EXPECTED_MODES = {"bypass_actor": "WARN", "auto_merge": "WARN", "required_status_checks": "WARN", "merge_queue_change": "WARN"}


class OpenActions(Preflight):
    def setUp(self):
        super().setUp()
        Stub.rules = list(CLEAN)

    def test_the_mode_table_is_pinned_and_complete(self):
        self.assertEqual(pf.CHECK_MODES, EXPECTED_MODES)   # a flip to BLOCK fails here: it is the owner's reviewed change
        self.assertEqual(set(OPENERS), set(pf.CHECK_MODES))
        self.assertEqual(set(pf.OPEN_ACTION_TEXT), set(pf.CHECK_MODES))
        self.assertTrue(set(pf.CHECK_MODES.values()) <= {"WARN", "BLOCK"})

    def test_nothing_open_prints_no_warning(self):
        p = self.run_pf()
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertNotIn("WARN", p.stdout)

    def test_each_open_action_warns_once_names_itself_and_the_launch_continues(self):
        for key, opener in OPENERS.items():
            with self.subTest(key=key):
                Stub.rules, Stub.bypass, Stub.auto_merge = list(CLEAN), [], False
                opener()
                p = self.run_pf()
                self.assertEqual(p.returncode, 0, p.stdout)
                lines = p.stdout.splitlines()
                self.assertEqual([l for l in lines if "WARN open action" in l and (" %s:" % key) in l].__len__(), 1, p.stdout)
                self.assertEqual(sum("WARN open action" in l for l in lines), 1, p.stdout)
                self.assertTrue(lines[-1].startswith("preflight: preflight ok") and ("WARN open: " + key) in lines[-1], lines[-1])

    def test_an_app_bypass_actor_is_the_only_bypass_that_warns(self):
        Stub.bypass = [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
        p = self.run_pf()
        self.assertNotIn("WARN", p.stdout)

    def test_a_bypass_actor_of_a_ruleset_that_does_not_cover_the_branch_is_not_counted(self):
        _open_bypass()
        Stub.include = ["refs/heads/other"]
        p = self.run_pf()
        self.assertNotIn("WARN open action bypass_actor", p.stdout)

    def run_blocked(self, key):
        """pf.main in-process with ONE key switched to BLOCK; returns (exit code, output)."""
        old, saved, out = dict(pf.CHECK_MODES), dict(os.environ), io.StringIO()
        os.environ.update(CW_GITHUB_API_BASE=self.base, CW_PREFLIGHT_TOKEN_CMD="echo " + TOKEN)
        try:
            pf.CHECK_MODES[key] = "BLOCK"
            with contextlib.redirect_stdout(out):
                rc = pf.main([REPO])
        finally:
            pf.CHECK_MODES.clear()
            pf.CHECK_MODES.update(old)
            os.environ.clear()
            os.environ.update(saved)
        return rc, out.getvalue()

    def test_each_key_set_to_block_fails_naming_the_action_and_nothing_else_changes(self):
        for key, opener in OPENERS.items():
            with self.subTest(key=key):
                Stub.rules, Stub.bypass, Stub.auto_merge = list(CLEAN), [], False
                opener()
                rc, text = self.run_blocked(key)
                self.assertEqual(rc, 1, text)
                self.assertIn("FAIL on main: open action %s:" % key, text)
                self.assertNotIn("preflight ok", text)
                self.assertEqual(text.count("FAIL"), 1, text)   # only the flipped key blocks

    def test_an_owner_approval_never_rescues_a_blocking_open_action(self):
        for key, opener in OPENERS.items():
            with self.subTest(key=key):
                Stub.rules, Stub.bypass, Stub.auto_merge = list(CLEAN), [], False
                Stub.org_types, Stub.org_code = list(FULL), 403
                self.approvals({self.KEY: self.ENTRY})
                opener()
                if key == "required_status_checks":
                    Stub.rules = list(FULL)
                saved = pf.APPROVALS
                pf.APPROVALS = os.path.join(self.copy, "repo-approvals.json")
                try:
                    rc, text = self.run_blocked(key)
                finally:
                    pf.APPROVALS = saved
                self.assertEqual(rc, 1, text)
                self.assertNotIn("NOT VERIFIED", text)
                self.assertIn("open action %s:" % key, text)

    def test_a_bypass_actor_of_a_ruleset_that_is_not_enforced_is_not_counted_and_every_ruleset_id_is_named(self):
        _open_bypass()
        Stub.enforcement = "evaluate"
        self.assertNotIn("WARN open action bypass_actor", self.run_pf().stdout)
        Stub.enforcement = "active"
        Stub.rule_ids = {"deletion": 8}
        Stub.extra = {8: {"id": 8, "target": "branch", "enforcement": "active", "bypass_actors": [{"actor_type": "Integration", "actor_id": 2}],
                          "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}}}
        text = self.run_pf().stdout
        self.assertIn("bypass actor of ruleset 7, 8", text)

    def test_what_blocks_today_still_blocks_with_every_new_check_a_warning(self):
        Stub.rules = ["non_fast_forward", "deletion", "required_status_checks"]
        p = self.run_pf()
        self.assertEqual(p.returncode, 1)
        self.assertIn("missing a pull_request or merge_queue", p.stdout)


for _n in dir(Preflight):   # reuse the fixture only: the inherited tests already run in Preflight
    if _n.startswith("test_") and _n not in OpenActions.__dict__:
        setattr(OpenActions, _n, None)


if __name__ == "__main__":
    unittest.main()
