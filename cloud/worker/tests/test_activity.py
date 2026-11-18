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
        from unittest import mock
        self.emptyproc = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        self.addCleanup(self.emptyproc.cleanup)
        envpatch = mock.patch.dict(os.environ, {"CW_ACTIVITY_UNDER_TEST": "1", "CW_ACTIVITY_PROC": self.emptyproc.name})
        envpatch.start()
        self.addCleanup(envpatch.stop)

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
        self.assertEqual({k: v for k, v in json.loads(p.stdout).items() if k != "turn"}, {"busy": False, "last_activity": "2023-11-14T22:21:40Z"})   # ask #2443: the `turn` key is asserted on the next line
        self.assertEqual(json.loads(p.stdout)["turn"], {"turn": "unknown", "outcome": None, "finish": None})   # the stub holds no `idle` message: unknown, never a guess

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
        self.assertEqual(json.loads(p.stdout), {"busy": False, "last_activity": "2026-01-02T03:04:05Z", "turn": {"turn": "unknown", "outcome": None, "finish": None}})   # ask #2443: no session: unknown

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
            e.pop("CW_ACTIVITY_UNDER_TEST", None)
        else:
            e["CW_ACTIVITY_UNDER_TEST"] = marker
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
            e.pop("CW_ACTIVITY_UNDER_TEST", None)
            if marker:
                e["CW_ACTIVITY_UNDER_TEST"] = marker
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

    # stub /proc with start times (field 22, ticks since boot), 100 ticks a second; the stub session started at 1700000000 s
    def timed(self, procs, btime=1700000000 - 1000):
        """procs: pid -> (state, ppid, comm, start ticks); the process starts at btime + ticks/100."""
        root = self.tree({pid: (v[0], v[1]) for pid, v in procs.items()})
        with open(os.path.join(root, "stat"), "w") as f:
            f.write("cpu 1 1 1 1\nbtime %d\n" % btime)
        for pid, (state, ppid, comm, ticks) in procs.items():
            with open(os.path.join(root, str(pid), "stat"), "w") as f:
                f.write("%d (%s) %s %d 1 1 0 %s\n" % (pid, comm, state, ppid, " ".join(["0"] * 14 + [str(ticks)])))
            if pid != 1:
                with open(os.path.join(root, str(pid), "cmdline"), "wb") as f:
                    f.write(comm.encode() + b"\0")
        return root

    def child_at(self, procs):
        sys.path.insert(0, os.path.dirname(SCRIPT))
        try:
            import activity
        finally:
            sys.path.pop(0)
        return activity.has_live_child(self.timed(procs), session_start=1700000000)

    def test_a_boot_time_process_older_than_the_session_does_not_count(self):
        # starts at btime + 500 s = 500 s before the session
        self.assertIs(self.child_at({1: ("S", 0, "opencode", 1), 50: ("S", 1, "helper", 50000)}), False)

    def test_a_process_started_after_the_session_began_counts(self):
        self.assertIs(self.child_at({1: ("S", 0, "opencode", 1), 50: ("S", 1, "pytest", 150000)}), True)

    def test_dockerd_started_mid_session_does_not_count(self):
        self.assertIs(self.child_at({1: ("S", 0, "opencode", 1), 50: ("S", 1, "dockerd", 150000), 51: ("S", 1, "containerd", 150000)}), False)

    def test_a_container_process_under_containerd_shim_does_not_count(self):
        self.assertIs(self.child_at({1: ("S", 0, "opencode", 1), 50: ("S", 1, "containerd-shim", 150000), 51: ("S", 50, "nginx", 150001)}), False)

    def test_a_test_run_started_by_a_returned_call_still_counts(self):
        self.assertIs(self.child_at({1: ("S", 0, "opencode", 1), 50: ("S", 1, "sh", 150000), 51: ("S", 50, "python3", 150001)}), True)

    def test_mutant_without_the_daemon_list_fails_the_dockerd_case(self):
        with open(SCRIPT) as f:
            real = f.read()
        old = ' and not any(_is_daemon(proc, p, comm[p]) for p in chain)'
        self.assertEqual(real.count(old), 1)
        mutant = real.replace(old, '')
        self.assertNotEqual(mutant, real)  # applied, computed from the two texts
        path = os.path.join(self.tmp.name, "mutant_daemons.py")
        with open(path, "w") as f:
            f.write(mutant)
        import importlib.util
        root = self.timed({1: ("S", 0, "opencode", 1), 50: ("S", 1, "dockerd", 150000)})
        results = []
        for script in (SCRIPT, path):
            spec = importlib.util.spec_from_file_location("act_%d" % len(results), script)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            results.append(mod.has_live_child(root, session_start=1700000000))
        self.assertIs(results[0], False)  # control: the real tool
        self.assertIs(results[1], True, "mutant without the daemon list must count dockerd (the pod would never idle out)")

    def test_mutant_without_the_child_check_fails_the_long_command_case(self):
        with open(SCRIPT) as f:
            real = f.read()
        old = '"busy": is_busy(active) or has_live_child(session_start=session_start),'
        self.assertEqual(real.count(old), 1)
        mutant = real.replace(old, '"busy": is_busy(active),')
        self.assertNotEqual(mutant, real)  # applied, computed from the two texts: a no-op fails here
        path = os.path.join(self.tmp.name, "mutant_activity.py")
        with open(path, "w") as f:
            f.write(mutant)
        root = self.tree({1: ("S", 0), 50: ("S", 1)})
        self.assertIs(self.busy(root, script=SCRIPT), True)  # control: the real tool
        self.assertIs(self.busy(root, script=path), False, "mutant ignoring the child must report busy false (the long command would be ended)")

    # main() wiring of the session start, on a stub server with two sessions (created 1700000000 s and 1700000400 s) and a stub /proc
    def two_session_busy(self, procs, script=SCRIPT):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/session/active":
                    body = {"data": {}}
                else:
                    body = {"data": [{"id": "a", "time": {"created": 1700000000000, "updated": 1700000100000}},
                                     {"id": "b", "time": {"created": 1700000400000, "updated": 1700000500000}}]}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            root = self.timed(procs)
            e = dict(os.environ, CW_ACTIVITY_PORT=str(srv.server_address[1]), CW_ACTIVITY_PASSWORD_FILE=self.pwfile,
                     CW_ACTIVITY_PROC=root, CW_ACTIVITY_UNDER_TEST="1")
            p = subprocess.run([sys.executable, script], env=e, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)["busy"]
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(5)

    def test_a_child_started_between_two_sessions_counts(self):
        # starts at 1700000200 s: after the oldest session (1700000000 s), before the newest (1700000400 s)
        self.assertIs(self.two_session_busy({1: ("S", 0, "opencode", 1), 50: ("S", 1, "pytest", 120000)}), True)

    def test_main_wires_the_session_start(self):
        boot = {1: ("S", 0, "opencode", 1), 50: ("S", 1, "helper", 50000)}
        after = {1: ("S", 0, "opencode", 1), 50: ("S", 1, "pytest", 150000)}
        self.assertIs(self.two_session_busy(after), True)
        self.assertIs(self.two_session_busy(boot), False)

    def test_mutant_passing_no_session_start_fails_the_boot_time_case(self):
        with open(SCRIPT) as f:
            real = f.read()
        old = "has_live_child(session_start=session_start)"
        self.assertEqual(real.count(old), 1)
        mutant = real.replace(old, "has_live_child()")
        self.assertNotEqual(mutant, real)  # applied, computed from the two texts
        path = os.path.join(self.tmp.name, "mutant_nostart.py")
        with open(path, "w") as f:
            f.write(mutant)
        boot = {1: ("S", 0, "opencode", 1), 50: ("S", 1, "helper", 50000)}
        self.assertIs(self.two_session_busy(boot, SCRIPT), False)  # control: the real tool
        self.assertIs(self.two_session_busy(boot, path), True, "mutant without the session start must count a boot-time process")


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
