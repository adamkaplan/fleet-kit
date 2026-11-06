"""#2269 slice B scenario S2: with CW_PR_LABELS set, cw-gh pr create labels the new pull request with exactly those labels; unset -> no label call;
a label the repository lacks is reported on stderr once and the pull request is still created. A fake GitHub on loopback; the token is synthetic."""
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
LABEL_STEP = "    label_pr(repo, out.get(\"number\"))\n"


class Fake(http.server.BaseHTTPRequestHandler):
    seen = []
    repo_labels = ["needs:captain", "bug"]

    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        Fake.seen.append(("GET", self.path))
        if "/labels" in self.path:
            return self.reply(200, [{"name": x} for x in Fake.repo_labels])
        self.reply(404, {"message": "Not Found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        Fake.seen.append(("POST", self.path, body))
        if self.path.endswith("/pulls"):
            return self.reply(201, {"number": 7, "html_url": "https://example.invalid/pull/7"})
        self.reply(200, [{"name": x} for x in body["labels"]])


def label_posts():
    return [s for s in Fake.seen if s[0] == "POST" and s[1].endswith("/labels")]


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.srv.shutdown)

    def run_create(self, client=CW_GH, **extra):
        Fake.seen = []
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            env = {"PATH": "/usr/bin:/bin", "HOME": d, "GH_TOKEN": "synthetic-" + "Qm7Lx2" * 6,
                   "CW_GH_API_BASE": "http://127.0.0.1:%d" % self.srv.server_port}
            env.update(extra)
            return subprocess.run([sys.executable, client, "pr", "create", "-R", REPO, "--title", "t", "--body", "b", "--head", "cw/x",
                                   "--base", "main"], env=env, capture_output=True, text=True, cwd=d, timeout=60, stdin=subprocess.DEVNULL)

    def test_s2_the_pull_request_is_created_and_labelled(self):
        r = self.run_create(CW_PR_LABELS="needs:captain")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pull/7", r.stdout)
        posts = label_posts()
        self.assertEqual(len(posts), 1, Fake.seen)
        self.assertEqual(posts[0][1], "/repos/%s/issues/7/labels" % REPO)
        self.assertEqual(posts[0][2], {"labels": ["needs:captain"]})

    def test_s2_unset_or_empty_makes_no_label_call(self):
        for extra in ({}, {"CW_PR_LABELS": ""}):
            with self.subTest(extra=extra):
                r = self.run_create(**extra)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(len(label_posts()), 0)
                self.assertFalse([s for s in Fake.seen if "/labels" in s[1]])

    def test_s2_a_missing_label_is_reported_once_and_the_pull_request_stays(self):
        r = self.run_create(CW_PR_LABELS="needs:captain,nope")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.count("no label 'nope'"), 1, r.stderr)
        self.assertEqual(label_posts()[0][2], {"labels": ["needs:captain"]})

    def test_s2_a_mutant_without_the_label_step_fails_with_an_anchored_text(self):
        src = open(CW_GH).read()
        self.assertIn(LABEL_STEP, src)
        self.run_create(CW_PR_LABELS="needs:captain")   # control first: the real client labels
        self.assertEqual(len(label_posts()), 1)
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            mut = os.path.join(d, "cw-gh-mutant")
            open(mut, "w").write(src.replace(LABEL_STEP, "", 1))
            r = self.run_create(client=mut, CW_PR_LABELS="needs:captain")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(label_posts()), 0, "the mutant without the label step applied no label: S2 must fail here")


if __name__ == "__main__":
    unittest.main()
