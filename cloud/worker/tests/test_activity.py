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
sys.path.insert(0, HERE)
import stub_support  # noqa: E402


class Activity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.pwfile = os.path.join(self.tmp.name, "pw")
        with open(self.pwfile, "w") as f:
            f.write("synthetic-password\n")
        self.log = os.path.join(self.tmp.name, "stub.log")

    def serve(self, mode="ok"):
        port = stub_support.free_port()
        p = stub_support.start(self, [sys.executable, STUB, str(port)], port=port, env=dict(os.environ, STUB_MODE=mode, STUB_LOG=self.log), stderr=subprocess.PIPE, text=True)
        for _ in range(50):
            if p.poll() is not None:
                self.fail("stub exited %s at start (python %s, port %s): %s" % (p.returncode, sys.version.split()[0], port, p.stderr.read().strip()))
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return port
            except OSError:
                time.sleep(0.1)
        p.kill()
        self.fail("stub did not start in 5s (python %s, port %s): %s" % (sys.version.split()[0], port, p.stderr.read().strip()))

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

    def test_a_stub_that_dies_at_start_is_reported_at_once_with_its_error(self):
        t = time.time()
        with self.assertRaises(AssertionError) as cm:
            self.serve("die")
        self.assertLess(time.time() - t, 2)
        self.assertEqual(str(cm.exception).count("planted stub error"), 1)

    def test_missing_password_file_exits_1(self):
        p = self.run_script(self.serve(), CW_ACTIVITY_PASSWORD_FILE=os.path.join(self.tmp.name, "absent"))
        self.assertEqual((p.returncode, p.stdout), (1, ""))

    # a live child of pid 1 counts as busy, read from a stub /proc; the server says idle
    def tree(self, procs, cmd=b"opencode\0serve\0--port\0" + b"4096\0"):
        root = os.path.join(self.tmp.name, "proc")
        os.makedirs(root, exist_ok=True)
        for pid, (state, ppid) in procs.items():
            os.makedirs(os.path.join(root, str(pid)), exist_ok=True)
            with open(os.path.join(root, str(pid), "stat"), "w") as f:
                f.write("%d (a b) %s %d 1 1 0\n" % (pid, state, ppid))
        if 1 in procs:
            with open(os.path.join(root, "1", "cmdline"), "wb") as f:
                f.write(cmd)
        return root

    def busy(self, root, mode="ok", script=None, marker="1"):
        port = self.serve(mode)
        e = dict(os.environ, CW_ACTIVITY_PORT=str(port), CW_ACTIVITY_PASSWORD_FILE=self.pwfile, CW_ACTIVITY_PROC=root)
        if marker is None:
            e.pop("FL" + "EET_SWITCHBOARD_UNDER_TEST", None)
        else:
            e["FL" + "EET_SWITCHBOARD_UNDER_TEST"] = marker
        p = subprocess.run([sys.executable, script or SCRIPT], env=e, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["busy"]

    def test_live_child_with_idle_server_is_busy(self):
        root = self.tree({1: ("S", 0), 50: ("S", 1), 51: ("S", 50)})
        self.assertIs(self.busy(root), True)

    def test_gate_pid1_is_init_so_a_live_child_does_not_count(self):
        root = self.tree({1: ("S", 0), 50: ("S", 1)}, cmd=b"/sbin/init\0")
        self.assertIs(self.busy(root), False)  # the server's answer only
        self.assertIs(self.busy(root, "busy-forever"), True)

    def test_the_stub_setting_alone_is_ignored_without_the_test_marker(self):
        root = self.tree({1: ("S", 0), 50: ("S", 1)})
        code = "import sys; sys.path.insert(0, %r); import activity; print(activity._proc_root())" % os.path.dirname(SCRIPT)
        for marker, want in ((None, "/proc"), ("0", "/proc"), ("1", root)):
            e = dict(os.environ, CW_ACTIVITY_PROC=root)
            e.pop("FL" + "EET_SWITCHBOARD_UNDER_TEST", None)
            if marker:
                e["FL" + "EET_SWITCHBOARD_UNDER_TEST"] = marker
            p = subprocess.run([sys.executable, "-c", code], env=e, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.stdout.strip(), want, p.stderr)

    def test_no_child_and_idle_server_is_not_busy(self):
        self.assertIs(self.busy(self.tree({1: ("S", 0)})), False)

    def test_agent_and_server_only_is_not_busy(self):
        self.assertIs(self.busy(self.tree({1: ("S", 0), 7: ("S", 0)})), False)  # 7 is outside pid 1: not a descendant

    def test_a_zombie_child_does_not_count(self):
        self.assertIs(self.busy(self.tree({1: ("S", 0), 50: ("Z", 1)})), False)

    def test_unreadable_proc_gives_the_servers_answer(self):
        gone = os.path.join(self.tmp.name, "no-such-proc")
        self.assertIs(self.busy(gone), False)
        self.assertIs(self.busy(gone, "busy-forever"), True)

    def has_child(self, procs):
        sys.path.insert(0, os.path.dirname(SCRIPT))
        try:
            import activity
        finally:
            sys.path.pop(0)
        return activity.has_live_child(self.tree(procs))

    def test_a_child_in_another_subtree_of_pid_1_counts(self):
        me = os.getpid()
        self.assertIs(self.has_child({1: ("S", 0), 60: ("S", 1), me: ("S", 60), 70: ("S", 1)}), True)

    def test_a_process_in_the_tools_own_call_subtree_does_not_count(self):
        me = os.getpid()
        self.assertIs(self.has_child({1: ("S", 0), 60: ("S", 1), me: ("S", 60), 61: ("S", 60), 62: ("S", 61)}), False)

    def test_a_tool_outside_pid_1_counts_every_descendant(self):
        me = os.getpid()
        self.assertIs(self.has_child({1: ("S", 0), me: ("S", 0), 70: ("S", 1)}), True)

    def test_mutant_without_the_child_check_fails_the_long_command_case(self):
        with open(SCRIPT) as f:
            real = f.read()
        old = '"busy": is_busy(active) or has_live_child(),'
        self.assertEqual(real.count(old), 1)
        mutant = real.replace(old, '"busy": is_busy(active),')
        self.assertNotEqual(mutant, real)  # applied, computed from the two texts: a no-op fails here
        path = os.path.join(self.tmp.name, "mutant_activity.py")
        with open(path, "w") as f:
            f.write(mutant)
        root = self.tree({1: ("S", 0), 50: ("S", 1)})
        self.assertIs(self.busy(root, script=SCRIPT), True)  # control: the real tool
        self.assertIs(self.busy(root, script=path), False, "mutant ignoring the child must report busy false (the long command would be ended)")


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
