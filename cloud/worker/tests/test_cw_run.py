#!/usr/bin/env python3
"""Ask #2369 (S2-S4): cw-run starts a command detached, polls it with bounded waits, and writes progress lines. Offline, real processes, dummy commands."""
import os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..", "cw-run")


class Run(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        self.addCleanup(t.cleanup)
        self.d = os.path.realpath(t.name)
        self.runs = os.path.join(self.d, "runs"); self.prog = os.path.join(self.d, "progress.txt")
        self.env = {"PATH": "/usr/bin:/bin", "CW_RUNS_DIR": self.runs, "CW_PROGRESS_FILE": self.prog, "CW_RUN_TICK": "300"}
        self.env["CW_RUN_SERVER_PASSWORD_FILE"] = os.path.join(self.runs, "no-server-password")   # no local server here: the wake step (ask #2410) stays out of these tests

    def cw(self, *a, timeout=60, **kw):
        env = dict(self.env, **kw.pop("env", {}))
        t0 = time.time()
        r = subprocess.run([sys.executable, TOOL] + list(a), env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=self.d)
        return r, time.time() - t0

    def test_s2_start_returns_at_once_and_waits_are_bounded(self):
        r, took = self.cw("start", "--name", "job", "--", "sh", "-c", "sleep 12; echo done")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(took, 2, "a slow start: the command ran in the foreground")
        self.assertIn("job", r.stdout); self.assertRegex(r.stdout, r"pid \d+"); self.assertIn("cw-run wait job", r.stdout)
        r, took = self.cw("wait", "job", "2")
        self.assertTrue(r.stdout.startswith("running "), r.stdout)
        self.assertTrue(1.5 < took < 6, took)
        r, took = self.cw("wait", "job", "1000")   # the bound is 240; the run ends first
        self.assertEqual(r.stdout.splitlines()[0], "exited 0")
        self.assertEqual(r.stdout.splitlines()[1:], ["done"])
        self.assertEqual(open(os.path.join(self.runs, "job.exit")).read().strip(), "0")
        self.assertEqual(oct(os.stat(self.runs).st_mode & 0o777), "0o700")

    def test_s2_wait_bound_is_240(self):
        src = open(TOOL).read()
        self.assertIn("MAX_WAIT = 240", src)
        self.assertRegex(src, r"secs = min\(secs, MAX_WAIT\)")
        self.assertIn("secs = 120", src)

    def test_s3_status_log_stdin_fork_and_refusals(self):
        self.cw("start", "--name", "three", "--", "sh", "-c", "echo out; echo err >&2; exit 3")
        r, _ = self.cw("wait", "three", "20")
        self.assertEqual(r.stdout.splitlines()[0], "exited 3"); self.assertIn("err", r.stdout)
        # stdin is closed: a read sees end of input at once
        # the caller's own stdin stays OPEN (a pipe) while the child reads: a helper that lets the child inherit it would hang here
        keep = subprocess.Popen([sys.executable, TOOL, "start", "--name", "rd", "--", "sh", "-c", "read x; echo got=[$x]"], env=self.env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=self.d)
        self.addCleanup(lambda: keep.stdin.close())
        keep.wait(timeout=10)
        r, took = self.cw("wait", "rd", "20")
        self.assertIn("exited 0", r.stdout); self.assertIn("got=[]", r.stdout); self.assertLess(took, 10)
        # a child that outlives the command does not hold the start call
        r, took = self.cw("start", "--name", "fk", "--", "sh", "-c", "(sleep 8) & echo parent")
        self.assertLess(took, 2)
        # refusals: one line each
        for args in (("start", "--name", "a/b", "--", "true"), ("start", "--name", "a b", "--", "true"), ("start", "--name", "", "--", "true"), ("wait", "../x")):
            r, _ = self.cw(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
        self.cw("start", "--name", "dup", "--", "sleep", "10")
        r, _ = self.cw("start", "--name", "dup", "--", "true")
        self.assertNotEqual(r.returncode, 0); self.assertEqual(len(r.stderr.strip().splitlines()), 1); self.assertIn("still running", r.stderr)
        r, _ = self.cw("list")
        self.assertIn("dup running", r.stdout); self.assertIn("three exited 3", r.stdout)
        r, _ = self.cw("tail", "three", "1")
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)

    def test_a_hard_rule_is_not_bypassed_by_start(self):
        rules = os.path.join(HERE, "..", "opencode-worker.json")
        env = {"CW_BASE_RULES": rules}
        r, _ = self.cw("start", "--name", "m", "--", "gh", "pr", "merge", "5", env=env)
        self.assertNotEqual(r.returncode, 0); self.assertIn("refuse", r.stderr); self.assertEqual(len(r.stderr.strip().splitlines()), 1)
        r, _ = self.cw("start", "--name", "p", "--", "git", "push", "--force", "origin", "x", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.runs, "m.pid")))
        r, _ = self.cw("start", "--name", "ok", "--", "sh", "-c", "sleep 1; echo done", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_s4_progress_lines(self):
        self.cw("start", "--name", "tk", "--", "sleep", "5", env={"CW_RUN_TICK": "2"})   # ticks at 2 s and 4 s, the end at 5 s: 1 s of margin each way
        r, _ = self.cw("wait", "tk", "30")
        self.assertIn("exited 0", r.stdout)
        time.sleep(0.5)
        lines = open(self.prog).read().splitlines()
        self.assertEqual(len(lines), 3, lines)   # two while it lives, one at the end
        self.assertTrue(all("tk" in x for x in lines)); self.assertIn("exited 0", lines[-1])


if __name__ == "__main__":
    unittest.main()
