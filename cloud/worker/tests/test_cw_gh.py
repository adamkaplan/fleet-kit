"""#1031: cw-gh, the pod's own GitHub client that replaces the real gh. Offline: a loopback stub of the API; no network, no credential
(the token is a synthetic value assembled at run time). One call row per verb of the client's VERBS table (a completeness test checks it)."""
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CW_GH = os.path.join(os.path.dirname(HERE), "cw-gh")
REPO = "acme-org/widget-service"
TOKEN = "synthetic-" + "Kq7Vm2" * 6   # no real credential shape


def load():
    spec = importlib.util.spec_from_loader("cw_gh", importlib.machinery.SourceFileLoader("cw_gh", CW_GH))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


MOD = load()
SHA = "a" * 40


def zip_bytes():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("build/1_step.txt", "build ok\n")
        z.writestr("lint/1_step.txt", "lint failed\n")
    return b.getvalue()


class Stub(http.server.BaseHTTPRequestHandler):
    seen = []       # (method, path, body, auth)
    logs_host = ""  # the base URL a logs redirect points at
    other_seen = []

    def log_message(self, *a):
        pass

    def answer(self, code, obj, extra=None):
        out = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def handle_any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        auth = self.headers.get("Authorization")
        if self.headers.get("X-Role") == "other" or self.path.startswith("/download"):
            Stub.other_seen.append((self.path, auth))
            return self.answer(200, zip_bytes())
        Stub.seen.append((self.command, self.path, json.loads(body) if body else None, auth))
        p = self.path.split("?")[0]
        if p.endswith("/boom"):
            return self.answer(401, {"message": "Bad credentials"})
        if p == "/repos/%s" % REPO:
            return self.answer(200, {"full_name": REPO, "default_branch": "main", "html_url": "https://x/r", "private": True})
        if p == "/repos/%s/pulls" % REPO:
            if self.command == "POST":
                return self.answer(201, {"html_url": "https://x/pull/9"})
            return self.answer(200, [{"number": 9, "title": "t", "head": {"ref": "feat"}, "state": "open", "merged_at": None}])
        if p == "/repos/%s/pulls/9" % REPO:
            return self.answer(200, {"number": 9, "title": "t", "state": "open", "html_url": "https://x/pull/9", "body": "b", "node_id": "PR_abc123",
                                     "head": {"ref": "feat", "sha": SHA}, "base": {"ref": "main"}, "user": {"login": "bot"}})
        if p == "/repos/%s/pulls/9/files" % REPO:
            return self.answer(200, [{"filename": "a.py"}])
        if p == "/repos/%s/issues/9/comments" % REPO:
            return self.answer(201, {"html_url": "https://x/pull/9#c1"})
        if p == "/repos/%s/commits/%s/check-runs" % (REPO, SHA):
            return self.answer(200, {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]})
        if p == "/repos/%s/commits/%s/status" % (REPO, SHA):
            return self.answer(200, {"statuses": []})
        if p == "/repos/%s/issues/3" % REPO:
            return self.answer(200, {"title": "i", "state": "open", "html_url": "https://x/i/3", "labels": [], "body": "ib"})
        if p == "/repos/%s/issues" % REPO:
            return self.answer(200, [{"number": 3, "title": "i", "state": "open"}, {"number": 9, "title": "pr", "state": "open", "pull_request": {}}])
        if p == "/repos/%s/actions/runs" % REPO:
            return self.answer(200, {"workflow_runs": [{"id": 5, "name": "ci", "status": "completed", "conclusion": "success", "head_branch": "feat"}]})
        if p == "/repos/%s/actions/runs/5" % REPO:
            return self.answer(200, {"id": 5, "name": "ci", "status": "completed", "conclusion": "failure", "html_url": "https://x/run/5"})
        if p == "/repos/%s/actions/runs/5/jobs" % REPO:
            return self.answer(200, {"jobs": [{"name": "build", "status": "completed", "conclusion": "success"},
                                              {"name": "lint", "status": "completed", "conclusion": "failure"}]})
        if p == "/repos/%s/actions/runs/5/logs" % REPO:
            return self.answer(302, b"", {"Location": Stub.logs_host + "/download/logs.zip"})
        if p == "/graphql":
            return self.answer(200, {"data": {}})
        self.answer(404, {"message": "Not Found"})

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = handle_any


# DATA: one row per verb of VERBS: (argv after cw-gh, the (method, path) the stub must see FIRST for that verb's own call, text the output has)
CALLS = {
    ("pr", "create"): (["pr", "create", "-R", REPO, "--title", "T", "--body", "B", "--head", "feat", "--base", "main"], ("POST", "/repos/%s/pulls" % REPO), "https://x/pull/9"),
    ("pr", "view"): (["pr", "view", "9", "-R", REPO], ("GET", "/repos/%s/pulls/9" % REPO), "title:\tt"),
    ("pr", "list"): (["pr", "list", "-R", REPO], ("GET", "/repos/%s/pulls?state=open&per_page=30" % REPO), "9\tt\tfeat\topen"),
    ("pr", "comment"): (["pr", "comment", "9", "-R", REPO, "--body", "hello"], ("POST", "/repos/%s/issues/9/comments" % REPO), "https://x/pull/9#c1"),
    ("pr", "checks"): (["pr", "checks", "9", "-R", REPO], ("GET", "/repos/%s/pulls/9" % REPO), "ci\tsuccess"),
    ("pr", "diff"): (["pr", "diff", "9", "-R", REPO, "--name-only"], ("GET", "/repos/%s/pulls/9/files?per_page=100" % REPO), "a.py"),
    ("pr", "ready"): (["pr", "ready", "9", "-R", REPO], ("GET", "/repos/%s/pulls/9" % REPO), "ready for review"),
    ("repo", "view"): (["repo", "view", REPO], ("GET", "/repos/%s" % REPO), "default branch:\tmain"),
    ("issue", "view"): (["issue", "view", "3", "-R", REPO], ("GET", "/repos/%s/issues/3" % REPO), "title:\ti"),
    ("issue", "list"): (["issue", "list", "-R", REPO], ("GET", "/repos/%s/issues?state=open&per_page=100" % REPO), "3\ti\topen"),
    ("run", "view"): (["run", "view", "5", "-R", REPO], ("GET", "/repos/%s/actions/runs/5" % REPO), "job\tlint\tcompleted\tfailure"),
    ("run", "list"): (["run", "list", "-R", REPO], ("GET", "/repos/%s/actions/runs?per_page=20" % REPO), "5\tci\tcompleted\tsuccess\tfeat"),
    ("run", "logs"): (["run", "logs", "5", "-R", REPO], ("GET", "/repos/%s/actions/runs/5/logs" % REPO), "lint failed"),
    ("api", ""): (["api", "repos/%s" % REPO], ("GET", "/repos/%s" % REPO), "full_name"),
}


class Client(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
        Stub.logs_host = cls.base

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Stub.seen, Stub.other_seen = [], []

    def run_gh(self, *args, token=TOKEN, **env):
        e = {"PATH": os.environ.get("PATH", ""), "CW_GH_API_BASE": self.base, "HOME": "/nonexistent"}
        if token:
            e["GH_TOKEN"] = token
        e.update(env)
        return subprocess.run([sys.executable, CW_GH, *args], capture_output=True, text=True, env=e, timeout=60, stdin=subprocess.DEVNULL)

    def test_the_call_table_covers_exactly_the_verbs(self):
        self.assertEqual(set(CALLS), set(MOD.VERBS))

    def test_each_verb_makes_its_call_and_prints_its_result(self):
        for key, (argv, first, text) in CALLS.items():
            with self.subTest(verb=key):
                Stub.seen = []
                p = self.run_gh(*argv)
                self.assertEqual(p.returncode, 0, (p.stdout, p.stderr))
                self.assertIn(text, p.stdout)
                self.assertIn(first, [(m, pth) for m, pth, _, _ in Stub.seen][:3], Stub.seen)
                self.assertNotIn(TOKEN, p.stdout + p.stderr)
                self.assertTrue(all(a == "Bearer " + TOKEN for _, _, _, a in Stub.seen))

    def test_pr_create_sends_the_given_fields_and_is_never_interactive(self):
        self.run_gh("pr", "create", "-R", REPO, "--title", "T", "--body", "B", "--head", "feat", "--base", "main", "--draft")
        sent = [b for m, pth, b, _ in Stub.seen if m == "POST"][0]
        self.assertEqual(sent, {"title": "T", "body": "B", "head": "feat", "base": "main", "draft": True})
        p = self.run_gh("pr", "create", "-R", REPO, "--head", "feat")   # no title, no body: refused, never a prompt
        self.assertEqual(p.returncode, 2)

    def test_pr_ready_sends_the_ready_mutation_for_the_pull_request_node(self):
        self.run_gh("pr", "ready", "9", "-R", REPO)
        gql = [b for m, pth, b, _ in Stub.seen if pth == "/graphql"][0]
        self.assertIn("markPullRequestReadyForReview", gql["query"])
        self.assertEqual(gql["variables"], {"id": "PR_abc123"})

    def test_a_branch_name_finds_its_pull_request(self):
        p = self.run_gh("pr", "view", "feat", "-R", REPO)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(any("head=acme-org%3Afeat" in pth for _, pth, _, _ in Stub.seen))

    def test_the_issue_list_leaves_pull_requests_out(self):
        p = self.run_gh("issue", "list", "-R", REPO)
        self.assertNotIn("pr\topen", p.stdout)

    def test_logs_failed_prints_only_the_failed_jobs_and_the_download_gets_no_token(self):
        p = self.run_gh("run", "view", "5", "-R", REPO, "--log-failed")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("lint failed", p.stdout)
        self.assertNotIn("build ok", p.stdout)
        self.assertEqual(Stub.other_seen, [("/download/logs.zip", None)])   # no Authorization header went to the download host

    def test_every_verb_the_client_lacks_exits_2_with_one_line(self):
        gone = [["issue", "comment", "3", "-b", "x"], ["issue", "create"], ["issue", "edit", "3"], ["issue", "close", "3"], ["pr", "merge", "9"],
                ["pr", "review", "9"], ["pr", "edit", "9"], ["pr", "close", "9"], ["label", "list"], ["release", "create"], ["workflow", "run"],
                ["extension", "install", "x"], ["alias", "set", "a", "b"], ["auth", "status"], ["repo", "edit"], ["secret", "set", "X"],
                ["run", "rerun", "5"], ["config", "get", "x"], ["", ]]
        for argv in gone:
            with self.subTest(argv=argv):
                p = self.run_gh(*argv)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn("is not available in this pod", p.stderr)
                self.assertEqual(Stub.seen, [])   # nothing was sent

    def test_api_is_get_only_with_no_body_graphql_or_host(self):
        bad = [["api", "-X", "POST", "repos/x/y"], ["api", "--method", "PATCH", "repos/x/y"], ["api", "repos/x/y", "-f", "a=b"],
               ["api", "repos/x/y", "-F", "a=b"], ["api", "repos/x/y", "--input", "f"], ["api", "graphql"], ["api", "/graphql"],
               ["api", "https://api.github.com/repos/x/y"], ["api", "repos/x/../y"], ["api", "repos/%2e%2e/y"], ["api", "a", "b"],
               ["api", "repos/x/y", "-H", "X: y"]]
        for argv in bad:
            with self.subTest(argv=argv):
                p = self.run_gh(*argv)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertEqual(Stub.seen, [])

    def test_an_http_error_names_the_status_and_never_the_token(self):
        p = self.run_gh("api", "repos/%s/boom" % REPO)
        self.assertEqual(p.returncode, 1)
        self.assertIn("HTTP 401", p.stderr)
        self.assertNotIn(TOKEN, p.stdout + p.stderr)

    def test_no_token_means_no_call(self):
        p = self.run_gh("repo", "view", REPO, token=None)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(Stub.seen, [])

    def test_a_non_loopback_api_base_is_refused_so_the_token_cannot_be_sent_elsewhere(self):
        for base in ("http://example.com", "https://example.com", "http://127.0.0.1@example.com"):
            p = self.run_gh("repo", "view", REPO, CW_GH_API_BASE=base)
            self.assertEqual(p.returncode, 2, base)
            self.assertNotIn(TOKEN, p.stdout + p.stderr)

    def test_an_unknown_option_is_refused_not_ignored(self):
        p = self.run_gh("pr", "view", "9", "-R", REPO, "--jq", ".x")
        self.assertEqual(p.returncode, 2)

    def test_the_client_source_holds_no_merge_review_or_issue_write_call(self):
        src = open(CW_GH).read()
        for needle in ("/merge", "enablePullRequestAutoMerge", "mergePullRequest", "/reviews", "PATCH", '"PUT"', '"DELETE"'):
            self.assertNotIn(needle, src, needle)
        # the only writes are the pull request create, the pull request comment and the ready mutation
        posts = [l.strip() for l in src.splitlines() if 'request("POST"' in l]
        self.assertEqual(len(posts), 3, posts)


if __name__ == "__main__":
    unittest.main()
