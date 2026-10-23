"""#1083: `cw-gh issue view N --comments`: comments printed after the body, bounded by 50 comments and 20000 bytes with a note, an issue
without comments, one GET to the comments path, and a mutant that drops the bound. Offline: a loopback stub; the token is synthetic."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CW_GH = os.path.join(os.path.dirname(HERE), "cw-gh")
REPO = "acme-org/widget-service"
TOKEN = "synthetic-" + "Hn3Wd8" * 6
NOTE = "[cut: more comments not shown]"


class Stub(http.server.BaseHTTPRequestHandler):
    seen = []
    comments = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Stub.seen.append((self.command, self.path))
        p = self.path.split("?")[0]
        if p == "/repos/%s/issues/3" % REPO:
            out = {"title": "t", "state": "open", "html_url": "u", "labels": [], "body": "the body"}
        elif p == "/repos/%s/issues/3/comments" % REPO:
            out = Stub.comments
        else:
            out = {"message": "Not Found"}
        data = json.dumps(out).encode()
        self.send_response(200 if p.startswith("/repos/%s/issues/3" % REPO) else 404)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = do_PATCH = do_PUT = do_DELETE = do_GET


def comment(i, who="alice", size=20):
    return {"user": {"login": who}, "created_at": "2026-01-0%dT00:00:00Z" % (i % 9 + 1), "body": "c%d " % i + "x" * size}


class Comments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.addClassCleanup(cls.srv.shutdown)

    def run_gh(self, comments, script=CW_GH, *extra):
        Stub.seen, Stub.comments = [], comments
        env = {"PATH": "/usr/bin:/bin", "CW_GH_API_BASE": self.base, "GH_TOKEN": TOKEN, "HOME": "/nonexistent"}
        return subprocess.run([sys.executable, script, "issue", "view", "3", "-R", REPO, "--comments", *extra], capture_output=True, text=True,
                              env=env, timeout=60, stdin=subprocess.DEVNULL)

    def blocks(self, out):
        return [l for l in out.splitlines() if l.startswith("--- comment by ")]

    def test_s2_three_comments_print_author_time_and_body_after_the_body(self):
        p = self.run_gh([comment(1, "alice"), comment(2, "bob"), comment(3, "carol")])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self.blocks(p.stdout)), 3)
        self.assertLess(p.stdout.index("the body"), p.stdout.index("--- comment by alice at 2026-01-02T00:00:00Z"))
        self.assertIn("c2 xxx", p.stdout)
        self.assertNotIn(NOTE, p.stdout)
        self.assertNotIn(TOKEN, p.stdout + p.stderr)

    def test_s3_sixty_comments_of_1kb_are_cut_with_the_note(self):
        p = self.run_gh([comment(i, size=1024) for i in range(60)])
        self.assertEqual(p.returncode, 0, p.stderr)
        n = len(self.blocks(p.stdout))
        self.assertTrue(0 < n <= 50, n)
        self.assertLess(len(p.stdout.encode()), 21000 + 500)
        self.assertEqual(p.stdout.splitlines()[-1], NOTE)

    def test_the_count_bound_alone_cuts_at_50_with_the_note(self):
        p = self.run_gh([comment(i, size=2) for i in range(60)])
        self.assertEqual(len(self.blocks(p.stdout)), 50)
        self.assertEqual(p.stdout.splitlines()[-1], NOTE)

    def test_s4_one_get_to_the_comments_path_and_no_write(self):
        self.run_gh([comment(1)])
        self.assertTrue(all(m == "GET" for m, _ in Stub.seen), Stub.seen)
        self.assertEqual(len([1 for _, pth in Stub.seen if pth.split("?")[0].endswith("/issues/3/comments")]), 1)
        import importlib.machinery
        import importlib.util
        spec = importlib.util.spec_from_loader("cwgh", importlib.machinery.SourceFileLoader("cwgh", CW_GH))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertEqual(sorted(m.VERBS), sorted([("pr", v) for v in ("create", "view", "list", "comment", "checks", "diff", "ready")] + [
            ("repo", "view"), ("issue", "view"), ("issue", "list"), ("issue", "comment"), ("run", "view"), ("run", "list"), ("run", "logs"), ("api", "")]))

    def test_s5_an_issue_without_comments_prints_the_body_and_no_comments(self):
        p = self.run_gh([])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("the body", p.stdout)
        self.assertEqual(self.blocks(p.stdout), [])
        self.assertIn("no comments", p.stdout.splitlines())

    def test_without_the_flag_no_comments_request_is_made(self):
        Stub.seen = []
        env = {"PATH": "/usr/bin:/bin", "CW_GH_API_BASE": self.base, "GH_TOKEN": TOKEN, "HOME": "/nonexistent"}
        subprocess.run([sys.executable, CW_GH, "issue", "view", "3", "-R", REPO], capture_output=True, env=env, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual([p for _, p in Stub.seen], ["/repos/%s/issues/3" % REPO])

    def test_s6_a_mutant_that_drops_the_bound_fails_the_bound_checks(self):
        src = open(CW_GH).read()
        small, big = [comment(i, size=2) for i in range(60)], [comment(i, size=1024) for i in range(60)]
        for n, (old, new, rows) in enumerate((("rows[:MAX_COMMENTS]", "rows", small), ("if used + size > MAX_COMMENT_BYTES:", "if False:", big),
                                              ("if cut:\n        print(CUT_NOTE)", "if False:\n        print(CUT_NOTE)", big))):
            self.assertIn(old, src)
            path = os.path.join(self.tmp.name, "mut%d" % n)
            with open(path, "w") as f:
                f.write(src.replace(old, new, 1))
            p = self.run_gh(rows, script=path)
            ok = len(self.blocks(p.stdout)) <= 50 and len(p.stdout.encode()) < 21500 and p.stdout.splitlines()[-1] == NOTE
            self.assertFalse(ok, old)   # the S3 assertions would FAIL on this mutant


if __name__ == "__main__":
    unittest.main()
