"""#1166: a worker can comment on an issue and do nothing else to it. The ask 1166 rows of cloud/worker/guard-table.tsv run through the shell
guard layer (guard.sh plus the patterns of opencode-worker.json, last match wins) and through the client (cw-gh against a loopback fake API).
The comment is accepted (exactly one POST); every denied verb and path is refused by its row before any request; a mutant that opens one denied
verb, and a mutant that opens the whole issues path, each make a row disagree. Offline; the token is synthetic; files only under the temp dir."""
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(WORKER))
TABLE = os.path.join(WORKER, "guard-table.tsv")
GUARD = os.path.join(WORKER, "guard.sh")
CONF = os.path.join(WORKER, "opencode-worker.json")
CW_GH = os.path.join(WORKER, "cw-gh")
TOKEN = "synthetic-" + "Qm7Lx2" * 6
REPO = "acme-org/widget-service"
QUOTE = "deny every other issue write by tool policy"
VERBS = ("edit", "create", "close", "reopen", "lock", "unlock", "delete", "transfer")
sys.path.insert(0, os.path.join(ROOT, "cloud", "launcher"))


def read(path):
    with open(path) as f:
        return f.read()


def rows():
    """the ask 1166 block of the table: between its marker and the ask 1200 marker"""
    text = read(TABLE)
    block = text.split("# ---- ask 1166 rows: add below this line ----\n", 1)[1].split("# ---- ask 1200 rows", 1)[0]
    out = []
    for line in block.splitlines():
        if line and not line.startswith("#"):
            out.append(dict(zip(("id", "kind", "tool", "method", "path", "why", "example"), line.split("\t"))))
    return out


def conf_decision(conf, line):
    bash = conf["permission"]["bash"]
    res = "allow"
    for pat, v in bash.items():   # opencode: the last matching rule wins
        if re.fullmatch(".*".join(re.escape(x) for x in pat.split("*")), line, re.S):
            res = v
    return res


def guard_result(example, guard=GUARD, conf=None):
    """(denied, stderr): denied by the shell guard or by the permission patterns"""
    r = subprocess.run(["sh", guard, "gh"] + example.split(), capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
    if r.returncode == 3:
        return True, r.stderr
    assert r.returncode == 0, r.stderr
    return conf_decision(conf or json.loads(read(CONF)), "gh " + example) == "deny", ""


class Fake(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        Fake.seen.append((self.command, self.path))
        out = json.dumps({"id": 1, "html_url": "https://example.test/c1"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_POST = do_PATCH = do_PUT = do_DELETE = do_GET


class IssueWrites(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.addClassCleanup(cls.srv.shutdown)

    def client(self, example, script=CW_GH):
        Fake.seen = []
        env = {"PATH": "/usr/bin:/bin", "CW_GH_API_BASE": self.base, "GH_TOKEN": TOKEN, "HOME": "/nonexistent"}
        return subprocess.run([sys.executable, script] + example.split(), capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)

    def mutant(self, name, guard_edit, conf_drop=(), conf_edit=()):
        """a copy of the guard and the patterns with one change: returns (guard path, conf)"""
        g = read(GUARD)
        new = guard_edit(g)
        self.assertNotEqual(new, g, name)
        path = os.path.join(self.tmp.name, name + ".sh")
        with open(path, "w") as f:
            f.write(new)
        conf = json.loads(read(CONF))
        bash = {k: v for k, v in conf["permission"]["bash"].items() if k not in conf_drop}
        for k, v in conf_edit:
            bash = {(v if kk == k else kk): vv for kk, vv in bash.items()}
        conf["permission"]["bash"] = bash
        return path, conf

    # S3
    def test_s3_the_comment_is_accepted_by_the_guard_and_the_client_with_one_post_each(self):
        body = os.path.join(self.tmp.name, "body.txt")
        with open(body, "w") as f:
            f.write("report")
        for example in ("issue comment 9 -R %s --body-file %s" % (REPO, body), "api -X POST repos/%s/issues/9/comments" % REPO,
                        "api --method POST repos/%s/issues/9/comments" % REPO):
            with self.subTest(example):
                self.assertEqual(guard_result(example)[0], False)
                p = self.client(example)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(Fake.seen, [("POST", "/repos/%s/issues/9/comments" % REPO)])
                self.assertNotIn(TOKEN, p.stdout + p.stderr)

    # S4
    def test_s4_every_denied_row_and_form_is_refused_by_name_before_any_request(self):
        extra = ["api -X POST repos/%s/issues/7/comments -f body=x" % REPO, "api -X POST repos/%s/issues/7/comments -F body=x" % REPO,
                 "api -X POST repos/%s/issues/7/comments --input f" % REPO, "api -X POST repos/%s/issues/7/comments --field body=x" % REPO,
                 "api repos/%s/issues/7/comments -f body=x" % REPO, "api graphql -f query=x", "api -X PATCH repos/%s/issues/7" % REPO,
                 "api -X POST repos/%s/issues/7/labels" % REPO, "api -X DELETE repos/%s/issues/7/comments" % REPO,
                 "api -X POST repos/%s/issues" % REPO, "api -X POST repos/%s/issues/7/assignees" % REPO]
        cases = [(r["id"], r["example"], r["why"]) for r in rows() if r["kind"] == "denied"] + [("api-write-form", e, "writing") for e in extra]
        self.assertGreaterEqual(len(cases), 15 + len(extra))
        for rid, example, why in cases:
            with self.subTest(rid, example=example):
                denied, err = guard_result(example)
                self.assertTrue(denied, "guard: " + rid)
                if err:
                    self.assertTrue(err.startswith("refused: " + why.split()[0]), err)
                    self.assertEqual(len(err.splitlines()), 1)
                    self.assertNotIn(REPO, err)
                p = self.client(example)
                self.assertEqual(p.returncode, 2, (rid, p.stderr))
                self.assertEqual(Fake.seen, [], rid)

    def test_the_table_has_every_row_of_the_plan_and_the_two_programs_agree_on_each(self):
        want = {"issue-comment-create", "issue-edit", "issue-create", "issue-close", "issue-reopen", "issue-lock", "issue-unlock", "issue-delete",
                "issue-transfer", "issue-pin", "issue-labels", "issue-assignees", "comment-patch", "comment-delete", "api-method-flag", "api-write-other"}
        ids = {r["id"] for r in rows()}
        self.assertTrue(want <= ids, want - ids)
        for r in rows():
            self.assertEqual(guard_result(r["example"])[0], r["kind"] == "denied", r["id"])
            self.assertEqual(self.client(r["example"]).returncode == 2, r["kind"] == "denied", r["id"])

    # S6: a mutant that opens one denied verb, in the guard and in its patterns, makes that row disagree with the table
    def test_s6_each_verb_mutant_makes_its_row_fail(self):
        by = {r["id"]: r for r in rows()}
        for v in VERBS:
            with self.subTest(v):
                def edit(g, v=v):
                    s = g.replace('"issue %s"|' % v, "", 1)
                    return s if s != g else g.replace('|"issue %s"' % v, "", 1)
                drop = ("gh issue %s*" % v, "gh * issue %s*" % v)
                path, conf = self.mutant("m-" + v, edit, conf_drop=drop)
                row = by["issue-" + v]
                self.assertEqual(guard_result(row["example"])[0], True)   # the real guard refuses
                self.assertEqual(guard_result(row["example"], path, conf)[0], False, "FAIL: " + row["id"])   # the mutant opens it: the row fails

    def test_s6_a_mutant_that_opens_the_whole_issues_path_fails(self):
        by = {r["id"]: r for r in rows()}
        row = {"id": "issues-write-post", "example": "api -X POST repos/%s/issues/7" % REPO}
        path, conf = self.mutant("m-all", lambda g: g.replace("/issues/[0-9]+/comments$'", "/issues'", 1),
                                 conf_edit=(("gh api -X POST repos/*/issues/*/comments", "gh api -X POST repos/*/issues*"),))
        self.assertEqual(guard_result(row["example"])[0], True)
        self.assertEqual(guard_result(row["example"], path, conf)[0], False)
        self.assertIn("comment-patch", by)

    def test_s6_a_client_that_opens_a_denied_verb_is_caught_by_the_no_request_check(self):
        src = read(CW_GH)
        old = '("issue", "comment"): issue_comment,'
        self.assertIn(old, src)
        path = os.path.join(self.tmp.name, "cw-gh-mutant")
        with open(path, "w") as f:
            f.write(src.replace(old, old + ' ("issue", "lock"): issue_comment,', 1))
        p = self.client("issue lock 7 -R %s --body x" % REPO, script=path)
        self.assertEqual((p.returncode, len(Fake.seen)), (0, 1))   # the mutant makes a request where the table says none
        p = self.client("issue lock 7 -R %s --body x" % REPO)
        self.assertEqual((p.returncode, len(Fake.seen)), (2, 0))

    # S2
    def test_s2_the_worker_token_grew_by_exactly_issues_write(self):
        import app_token
        before = {"contents": "write", "pull_requests": "write", "metadata": "read"}
        grown = {k: v for k, v in app_token.EXPECTED_PERMISSIONS.items() if before.get(k) != v}
        self.assertEqual(grown, {"issues": "write"})
        self.assertEqual(len(app_token.EXPECTED_PERMISSIONS), len(before) + 1)

    # S7
    def test_s7_the_docs_quote_the_owners_decision_once(self):
        text = read(os.path.join(ROOT, "docs", "cloud-worker-interface.md"))
        self.assertEqual(len([l for l in text.splitlines() if QUOTE in l]), 1)
        self.assertIn("allow the comment alone", text)

    # U2: the denied rows that are not issue writes stay denied
    def test_u2_merge_and_default_branch_pushes_stay_refused(self):
        for example in ("pr merge 7", "api -X PUT repos/%s/pulls/7/merge" % REPO):
            self.assertEqual(guard_result(example)[0], True, example)
        for args in (["push", "--force", "origin", "x"], ["push", "origin", "HEAD:refs/heads/trunk-x", "+y"]):
            r = subprocess.run(["sh", GUARD, "git"] + args, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
            self.assertEqual(r.returncode, 3, args)


if __name__ == "__main__":
    unittest.main()
