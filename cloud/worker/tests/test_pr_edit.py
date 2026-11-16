"""The pod's client edits a pull request body (`pr edit N --body-file F`) with no pod-side policy check: one PATCH, whatever the number or
the body. The token's scope and the repository's rules decide what is allowed. Also: the stub server the record test reuses, and the
image properties that remain (cw-gh never follows a redirect, the credential helper answers only for https://github.com, no guard layer).
Offline; the token is synthetic."""
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
CW_GH = os.path.join(WORKER, "cw-gh")
TOKEN = "synthetic-" + "Qm7Lx2" * 6
REPO = "acme-org/widget-service"
BRANCH = "cw/own-branch"


class H(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def reply(self, code, obj, extra=None):
        out = json.dumps(obj).encode()
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        H.seen.append(("GET", self.path))
        if self.path.startswith("/redir"):
            return self.reply(302, {}, {"Location": "http://127.0.0.1:1/elsewhere"})
        self.reply(200, {"ok": True})

    def do_PATCH(self):
        ln = int(self.headers.get("Content-Length") or 0)
        H.seen.append(("PATCH", self.path, json.loads(self.rfile.read(ln))))
        self.reply(200, {"html_url": "https://example.invalid/pr"})

    do_POST = do_PUT = do_DELETE = do_PATCH


def patches():
    return [x for x in H.seen if x[0] != "GET"]


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
        cls.tmp = tempfile.TemporaryDirectory(dir=os.path.realpath("/tmp"))
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.work = os.path.join(cls.tmp.name, "work")
        os.mkdir(cls.work)
        subprocess.run(["git", "init", "-q", cls.work], check=True)
        subprocess.run(["git", "-C", cls.work, "remote", "add", "origin", "https://github.com/%s.git" % REPO], check=True)
        cls.addClassCleanup(cls.srv.shutdown)

    def client(self, *args):
        H.seen = []
        env = {"PATH": "/usr/bin:/bin", "CW_GH_API_BASE": self.base, "GH_TOKEN": TOKEN, "HOME": "/nonexistent"}
        r = subprocess.run([sys.executable, CW_GH] + list(args), capture_output=True, text=True, env=env, cwd=self.work, timeout=60, stdin=subprocess.DEVNULL)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        return r

    def body(self, text):
        p = os.path.join(self.tmp.name, "body")
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_pr_edit_makes_one_patch_with_the_body_file_text(self):
        for n in ("7", "8"):   # any number: no own-PR record or branch check any more
            r = self.client("pr", "edit", n, "--body-file", self.body("PLAN: x\n\nnew text\n"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(patches(), [("PATCH", "/repos/%s/pulls/%s" % (REPO, n), {"body": "PLAN: x\n\nnew text\n"})])

    def test_pr_edit_needs_a_number_and_a_body(self):
        for args in (("pr", "edit", "--body-file", self.body("x")), ("pr", "edit", "7")):
            r = self.client(*args)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(patches(), [])

    def test_api_is_get_only_because_nothing_else_is_implemented(self):
        r = self.client("api", "-X", "POST", "repos/%s/issues" % REPO)
        self.assertEqual(r.returncode, 2)
        self.assertIn("only GET is implemented", r.stderr)
        self.assertEqual(H.seen, [])
        r = self.client("api", "repos/%s/issues/3" % REPO)   # no policy refusal of an issue path
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_api_takes_no_host_so_the_token_stays_on_the_api_host(self):
        for p in ("https://example.invalid/x", "//example.invalid/x"):
            r = self.client("api", p)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(H.seen, [])

    def test_the_client_never_follows_a_redirect(self):
        r = self.client("api", "redir")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([x for x in H.seen if x[0] == "GET"], [("GET", "/redir")])


class ImageProperties(unittest.TestCase):
    def read(self, p):
        with open(os.path.join(WORKER, p)) as f:
            return f.read()

    def test_cw_gh_has_no_redirect_follower(self):
        src = self.read("cw-gh")
        self.assertIn("class NoRedirect(urllib.request.HTTPRedirectHandler)", src)
        self.assertIn("build_opener(NoRedirect)", src)

    def test_the_credential_helper_answers_only_for_https_github(self):
        src = self.read("entrypoint.sh")
        self.assertIn('[ "\\$proto" = https ] && [ "\\$host" = github.com ] || exit 0', src)

    def test_no_guard_layer_is_left(self):
        self.assertFalse(os.path.exists(os.path.join(WORKER, "guard.sh")))
        self.assertFalse(os.path.exists(os.path.join(WORKER, "guard-table.tsv")))
        for p in ("Dockerfile", "entrypoint.sh"):
            self.assertIsNone(re.search(r"guard\.sh|guard-table|cw-guard", self.read(p)), p)


if __name__ == "__main__":
    unittest.main()
