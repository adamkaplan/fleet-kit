"""Tests for cloud/worker/cw-pod-inspect: a stub agent server (session and message routes) and a stub git tree under /tmp."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "cw-pod-inspect")
NOW_MS = int(time.time() * 1000)


def messages():
    tool = lambda name, status, start, end=None: {"type": "tool", "tool": name, "state": {"status": status, "time": {"start": start, "end": end}}}
    return [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "user words"}]},
            {"info": {"role": "assistant"}, "parts": [tool("read", "completed", NOW_MS - 60000, NOW_MS - 59000),
                                                      tool("bash", "error", NOW_MS - 50000, NOW_MS - 40000),
                                                      {"type": "text", "text": "x" * 400},
                                                      tool("shell", "running", NOW_MS - 30000)]}]


class Handler(http.server.BaseHTTPRequestHandler):
    override = None

    def do_GET(self):
        if self.path == "/api/session":
            data = [{"id": "s1", "time": {"updated": NOW_MS - 5000}}]
        elif self.path == "/api/session/s1/message":
            data = Handler.override or messages()
        else:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"data": data}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def sh(cwd, *a):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True, check=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")).stdout


class PodInspect(unittest.TestCase):
    def setUp(self):
        # The fixtures' ages are relative to NOW_MS: re-read it per test, or a long whole-directory run ages every call past its bound.
        global NOW_MS
        NOW_MS = int(time.time() * 1000)
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.pw = os.path.join(self.tmp.name, "pw")
        with open(self.pw, "w") as f:
            f.write("synthetic\n")
        self.repo = os.path.join(self.tmp.name, "repo")
        bare = os.path.join(self.tmp.name, "bare.git")
        sh(self.tmp.name, "git", "init", "-q", "--bare", bare)
        sh(self.tmp.name, "git", "init", "-q", "-b", "feat", self.repo)
        sh(self.repo, "git", "remote", "add", "origin", bare)
        with open(os.path.join(self.repo, "a"), "w") as f:
            f.write("1")
        sh(self.repo, "git", "add", "a")
        sh(self.repo, "git", "commit", "-qm", "one")
        sh(self.repo, "git", "push", "-q", "origin", "feat")

    def serve(self):
        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        def stop():
            srv.shutdown()
            srv.server_close()
            t.join(5)
        self.addCleanup(stop)
        return srv.server_address[1]

    def run_it(self, port):
        e = dict(os.environ, CW_ACTIVITY_PORT=str(port), CW_ACTIVITY_PASSWORD_FILE=self.pw, CW_INSPECT_REPO=self.repo)
        p = subprocess.run([sys.executable, SCRIPT], env=e, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(p.stdout.strip().splitlines()), 1)
        return json.loads(p.stdout)

    def commit(self, name):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(name)
        sh(self.repo, "git", "add", name)
        sh(self.repo, "git", "commit", "-qm", name)

    def test_nine_fields_read_right(self):
        self.commit("b")
        self.commit("c")
        with open(os.path.join(self.repo, "d"), "w") as f:
            f.write("d")
        o = self.run_it(self.serve())
        self.assertEqual(o["missing"], [])
        self.assertEqual(o["in_flight"]["tool"], "shell")
        self.assertTrue(28 <= o["in_flight"]["seconds"] <= 120)
        self.assertEqual([c["tool"] for c in o["last_calls"]], ["read", "bash"])
        self.assertEqual([c["status"] for c in o["last_calls"]], ["completed", "error"])
        self.assertTrue(38 <= o["last_calls"][1]["seconds_ago"] <= 130)
        self.assertEqual(o["last_text"], "x" * 300)
        self.assertTrue(3 <= o["idle_seconds"] <= 40)
        self.assertEqual(o["branch"], "feat")
        shas = sh(self.repo, "git", "log", "--format=%h", "-2").split()
        self.assertEqual(o["unpushed"]["count"], 2)
        self.assertEqual(sorted(o["unpushed"]["shas"]), sorted(s[:9] for s in sh(self.repo, "git", "log", "--format=%H", "-2").split()))
        self.assertEqual(len(shas), 2)
        self.assertEqual(o["dirty"], 1)
        self.assertIsInstance(o["last_push"], str)

    def test_a_call_in_flight_is_reported_with_its_age(self):
        o = self.run_it(self.serve())
        self.assertEqual(o["in_flight"]["tool"], "shell")
        self.assertTrue(28 <= o["in_flight"]["seconds"] <= 120, o["in_flight"])
        self.assertNotIn("shell", [c["tool"] for c in o["last_calls"]])

    def test_more_than_ten_calls_are_cut_to_the_last_ten(self):
        many = [{"info": {"role": "assistant"}, "parts": [{"type": "tool", "tool": "t%d" % i, "state": {"status": "completed", "time": {"start": NOW_MS - 100000 + i * 1000, "end": NOW_MS - 100000 + i * 1000}}} for i in range(15)]}]
        Handler.override = many
        self.addCleanup(setattr, Handler, "override", None)
        o = self.run_it(self.serve())
        tools = [c["tool"] for c in o["last_calls"]]
        self.assertLessEqual(len(tools), 10)   # cut
        self.assertGreaterEqual(len(tools), 5)   # not cut to nothing
        self.assertEqual(tools, ["t%d" % i for i in range(15 - len(tools), 15)])   # the last ones, in order
        self.assertIsNone(o["in_flight"])

    def test_dead_server_lists_server_fields_and_exits_zero(self):
        o = self.run_it(1)
        SERVER, GIT = ("in_flight", "last_calls", "last_text", "idle_seconds"), ("branch", "unpushed", "dirty", "last_push")
        for field in SERVER:
            self.assertIn(field, o["missing"])   # every field the dead server owed is listed
        for field in GIT:
            self.assertNotIn(field, o["missing"])   # the git fields were read and are not blamed on the server
        self.assertIsNone(o["in_flight"])
        self.assertEqual(o["branch"], "feat")
        self.assertEqual(o["unpushed"]["count"], 0)

    def test_no_repo_lists_git_fields(self):
        self.repo = os.path.join(self.tmp.name, "nothing")
        o = self.run_it(self.serve())
        for field in ("branch", "unpushed", "dirty", "last_push"):
            self.assertIn(field, o["missing"])   # every git field is listed
        for field in ("in_flight", "last_calls", "last_text", "idle_seconds"):
            self.assertNotIn(field, o["missing"])   # the server fields were read
        self.assertEqual(o["in_flight"]["tool"], "shell")


if __name__ == "__main__":
    unittest.main()
