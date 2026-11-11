#!/usr/bin/env python3
"""Ask #2410 (S2-S7): when a cw-run command ends, the helper wakes the agent once: one user message to the local server, one progress line, one boss message. Offline: a fake local HTTP server and a fake client."""
import http.server, json, os, re, subprocess, sys, tempfile, threading, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..", "cw-run")
TOKEN = "ghp_" + "A1b2C3d4E5" * 4


class Fake:
    """mode: ok | busy2 (409 twice, then accept) | never (always 409) | nosession"""
    def __init__(self, mode="ok"):
        self.mode, self.messages, self.attempts = mode, [], 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def reply(self, code, obj=None):
                data = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/session":
                    if outer.mode == "nosession":
                        return self.reply(200, {"data": []})
                    return self.reply(200, {"data": [{"id": "ses_old", "time": {"updated": 1}}, {"id": "ses_new", "time": {"updated": 9}}]})
                self.reply(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.attempts += 1
                if self.path != "/api/session/ses_new/prompt":
                    return self.reply(404)
                if outer.mode == "never" or (outer.mode == "busy2" and outer.attempts <= 2):
                    return self.reply(409, {"error": "busy"})
                outer.messages.append(body["text"]); self.reply(200, {"data": {}})

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


class Wake(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        self.addCleanup(t.cleanup)
        self.d = os.path.realpath(t.name)
        self.runs, self.prog = os.path.join(self.d, "runs"), os.path.join(self.d, "progress.txt")
        self.boss = os.path.join(self.d, "boss.txt")
        self.client = os.path.join(self.d, "client.sh")
        self.set_client(0)
        with open(os.path.join(self.d, "pw"), "w") as f:
            f.write("pw\n")

    def set_client(self, rc):
        with open(self.client, "w") as f:
            f.write('#!/bin/sh\nprintf "%%s\\n--\\n" "$*" >> %s\nexit %d\n' % (self.boss, rc))
        os.chmod(self.client, 0o755)

    def fake(self, mode="ok"):
        f = Fake(mode); self.addCleanup(f.close); return f

    def start(self, f, script, name="job", tool=TOOL, gap="0.2", limit="1.5"):
        env = {"PATH": "/usr/bin:/bin", "CW_RUNS_DIR": self.runs, "CW_PROGRESS_FILE": self.prog, "CW_RUN_TICK": "300",
               "CW_RUN_SERVER": f.url, "CW_RUN_SERVER_PASSWORD_FILE": os.path.join(self.d, "pw"), "CW_RUN_CLIENT": self.client,
               "FLEET_BOSS": "boss1", "CW_RUN_TEST": "1", "CW_RUN_WAKE_GAP": gap, "CW_RUN_WAKE_LIMIT": limit}
        r = subprocess.run([sys.executable, tool, "start", "--name", name, "--", "sh", "-c", script], env=env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)

    def finish(self, name="job", secs=20):
        """Wait until the detached wrapper has ended (marker written, wake done: the pid is gone)."""
        end = time.time() + secs
        pid = int(open(os.path.join(self.runs, name + ".pid")).read())
        while time.time() < end:
            try:
                os.kill(pid, 0)
                if "Z" in open("/proc/%d/stat" % pid).read().rsplit(")", 1)[1].split()[0]:
                    break
            except (ProcessLookupError, OSError):
                break
            time.sleep(0.1)
        else:
            self.fail("the wrapper did not end")
        return open(os.path.join(self.runs, name + ".exit")).read().strip()

    def progress(self):
        try:
            return open(self.prog).read().splitlines()
        except OSError:
            return []

    def bosses(self):
        try:
            return [m for m in open(self.boss).read().split("--\n") if m.strip()]
        except OSError:
            return []

    def test_s2_exit_13_one_message_one_line_one_boss_message(self):
        f = self.fake(); self.start(f, "echo a; exit 13")
        self.assertEqual(self.finish(), "13")
        self.assertEqual(len(f.messages), 1)
        self.assertTrue(f.messages[0].startswith("run job exited 13 after "), f.messages[0])
        self.assertIn("exited 13", f.messages[0]); self.assertIn("\na", f.messages[0])
        lines = [l for l in self.progress() if l.startswith("run job exited 13 after ")]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], f.messages[0].splitlines()[0])
        b = self.bosses()
        self.assertEqual(len(b), 1); self.assertIn("send boss1 run job exited 13", b[0]); self.assertIn("\na", b[0])

    def test_s3_secret_redacted_capped_and_failing_boss_blocks_nothing(self):
        self.set_client(1)
        f = self.fake(); self.start(f, "yes 'ab cd ef gh ij kl mn op qr st uv wx yz ab cd ef gh ij kl mn op qr st uv wx yz ab cd ef gh ij kl mn op qr st uv wx yz ab cd ef gh ij kl mn op qr st uv wx yz ab cd ef gh ij kl mn op qr st uv wx yz ab cd ef gh ij kl mn op qr st uv wx yz' | head -n 40; echo token=%s; echo password: \"hunter2 xyz\"; exit 13" % TOKEN)
        self.assertEqual(self.finish(), "13")
        self.assertEqual(len(f.messages), 1)
        self.assertEqual(f.messages[0].count(TOKEN), 0)
        self.assertEqual(len(f.messages[0]), 3000); self.assertTrue(f.messages[0].endswith("..."))
        self.assertIn("[redacted]", f.messages[0])
        self.assertNotIn("hunter2", f.messages[0])
        self.assertEqual(len([l for l in self.progress() if l.startswith("run job exited 13")]), 1)
        b = self.bosses()
        self.assertEqual(len(b), 1); self.assertNotIn(TOKEN, b[0]); self.assertIn("[redacted]", b[0])

    def test_s4_busy_twice_then_accepts(self):
        f = self.fake("busy2"); self.start(f, "exit 13")
        self.assertEqual(self.finish(), "13")
        self.assertEqual(len(f.messages), 1); self.assertEqual(f.attempts, 3)
        self.assertEqual(self.progress().count("wake not delivered"), 0)

    def test_s4_never_accepts_one_not_delivered_line(self):
        f = self.fake("never"); self.start(f, "echo a; exit 13")
        self.assertEqual(self.finish(), "13")
        self.assertEqual(f.messages, []); self.assertGreaterEqual(f.attempts, 2)
        self.assertEqual(self.progress().count("wake not delivered"), 1)
        self.assertEqual(open(os.path.join(self.runs, "job.log")).read().splitlines().count("wake not delivered"), 1)

    def test_s5_no_answer_and_no_session(self):
        f = self.fake(); url = f.url; f.close()   # nothing listens
        f.url = url; self.start(f, "exit 13")
        self.assertEqual(self.finish(), "13")
        self.assertEqual(self.progress().count("wake not delivered"), 1)
        g = self.fake("nosession"); self.start(g, "exit 13", name="job2")
        self.assertEqual(self.finish("job2"), "13")
        self.assertEqual(self.progress().count("wake not delivered"), 2); self.assertEqual(g.messages, [])

    def scenario_second_start(self, tool):
        f = self.fake(); self.start(f, "exit 0", tool=tool)
        self.assertEqual(self.finish(), "0")
        self.assertEqual(len(f.messages), 1, "exit 0 must produce its one message")
        self.start(f, "exit 0", tool=tool)
        self.finish()
        self.assertEqual(len(f.messages), 1, "a run woke the agent twice")

    def test_s6_second_start_same_name_sends_nothing(self):
        self.scenario_second_start(TOOL)
        self.assertTrue(os.path.exists(os.path.join(self.runs, "job.woke")))

    def test_s7_mutant_without_marker_fails_with_anchored_text(self):
        self.scenario_second_start(TOOL)   # control first, unmutated
        src = open(TOOL).read()
        old = "os.O_CREAT | os.O_EXCL | os.O_WRONLY"
        self.assertEqual(src.count(old), 1)
        mutant = os.path.join(self.d, "cw-run-mutant")
        open(mutant, "w").write(src.replace(old, "os.O_CREAT | os.O_WRONLY"))
        self.assertNotEqual(open(mutant).read(), src)
        self.setUp()   # a clean scratch for the mutant
        with self.assertRaises(AssertionError) as cm:
            self.scenario_second_start(mutant)
        self.assertIn("a run woke the agent twice", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
