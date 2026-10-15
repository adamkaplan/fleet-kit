"""Tests for cloud/worker/activity.py (cw-worker-activity) against the loopback stub, and the Dockerfile's command list."""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.join(HERE, "..", "..")
SCRIPT = os.path.join(CLOUD, "worker", "activity.py")
STUB = os.path.join(HERE, "stub_server.py")


class Activity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.pwfile = os.path.join(self.tmp.name, "pw")
        with open(self.pwfile, "w") as f:
            f.write("synthetic-password\n")
        self.log = os.path.join(self.tmp.name, "stub.log")

    def serve(self, mode="ok"):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        p = subprocess.Popen([sys.executable, STUB, str(port)], env=dict(os.environ, STUB_MODE=mode, STUB_LOG=self.log))
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return port
            except OSError:
                time.sleep(0.1)
        self.fail("stub did not start")

    def run_script(self, port, **env):
        e = dict(os.environ, CW_ACTIVITY_PORT=str(port), CW_ACTIVITY_PASSWORD_FILE=self.pwfile)
        e.update(env)
        return subprocess.run([sys.executable, SCRIPT], env=e, capture_output=True, text=True, timeout=30)

    def test_idle_reports_newest_update(self):
        p = self.run_script(self.serve())
        self.assertEqual(p.returncode, 0, p.stderr)
        # 1700000500000 ms = 2023-11-14T22:21:40Z; the stub's active list is empty before a prompt
        self.assertEqual(json.loads(p.stdout), {"busy": False, "last_activity": "2023-11-14T22:21:40Z"})

    def test_busy_when_a_session_is_active(self):
        p = self.run_script(self.serve("busy-forever"))
        self.assertEqual(json.loads(p.stdout)["busy"], True)

    def test_a_list_is_still_accepted(self):
        p = self.run_script(self.serve("list-active"))
        self.assertEqual(json.loads(p.stdout)["busy"], True)

    def test_unexpected_shape_gives_a_reason_on_stderr(self):
        p = self.run_script(self.serve("bad-active"))
        self.assertEqual((p.returncode, p.stdout, p.stderr.strip()), (1, "", "activity: unexpected shape"))

    def test_http_401_gives_a_reason_on_stderr(self):
        p = self.run_script(self.serve("unauthorized"))
        self.assertEqual((p.returncode, p.stdout, p.stderr.strip()), (1, "", "activity: HTTP 401"))

    def test_no_sessions_falls_back_to_pod_start(self):
        p = self.run_script(self.serve("no-sessions"), CW_STARTED="2026-01-02T03:04:05Z")
        self.assertEqual(json.loads(p.stdout), {"busy": False, "last_activity": "2026-01-02T03:04:05Z"})

    def test_unreadable_answer_exits_1_with_no_output(self):
        for port, mode in ((self.serve("bad-list"), "bad-list"), (1, "nothing listening")):
            p = self.run_script(port)
            self.assertEqual((p.returncode, p.stdout), (1, ""), mode)

    def test_missing_password_file_exits_1(self):
        p = self.run_script(self.serve(), CW_ACTIVITY_PASSWORD_FILE=os.path.join(self.tmp.name, "absent"))
        self.assertEqual((p.returncode, p.stdout), (1, ""))


class Dockerfile(unittest.TestCase):
    def test_the_three_commands_are_installed(self):
        with open(os.path.join(CLOUD, "worker", "Dockerfile")) as f:
            text = f.read()
        for src, dst in (("launcher/end_hook.py", "cw-end-hook"), ("launcher/token_mint.py", "cw-token-mint"),
                         ("worker/activity.py", "cw-worker-activity")):
            self.assertRegex(text, r"(?m)^COPY %s /usr/local/bin/%s$" % (re.escape(src), dst))
            self.assertIn("/usr/local/bin/" + dst, text.split("RUN chmod 0755", 1)[1])  # made executable
            self.assertTrue(os.path.exists(os.path.join(CLOUD, src)))
            with open(os.path.join(CLOUD, src)) as f:
                self.assertTrue(f.readline().startswith("#!/usr/bin/env python3"))


if __name__ == "__main__":
    unittest.main()
