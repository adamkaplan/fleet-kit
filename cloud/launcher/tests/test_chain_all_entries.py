"""Ask #1547: the chain runs every entry and names the failed ones. Offline; scratch trees and scratch mutants under the temp dir."""
import os, re, shutil, signal, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "worker", "tests"))
import test_chain_list as base
import stub_support

README = os.path.join(base.ROOT, "cloud", "launcher", "README.md")


class Chain(base.Scratch):
    def tree(self, names, par=(), **kw):
        d = super().tree(names, tests=kw.pop("tests", tuple(names)), **kw)
        with open(os.path.join(d, "bin", "test-cloud-launcher.parallel"), "w") as f:
            f.write("".join("cloud/launcher/tests/%s\n" % n for n in par))
        return d

    def script(self, d, text):
        with open(os.path.join(d, "bin", "test-cloud-launcher"), "w") as f:
            f.write(text)

    def entry(self, d, name, body):
        with open(os.path.join(d, "cloud/launcher/tests", name), "w") as f:
            f.write(body)


class AllEntries(Chain):
    def test_failed_first_and_third_entries_all_run_and_are_named(self):
        names = ["test_bad1.py", "test_ok.py", "test_bad3.py"]
        p, ran = self.run_chain(self.tree(names))
        self.assertEqual(ran, sorted(names))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(len(re.findall(r"^FAILED: ", p.stdout, re.M)), 2)
        closing = re.findall(r"^failed entries: 2: .*$", p.stdout, re.M)
        self.assertEqual(len(closing), 1, p.stdout)
        self.assertIn("test_bad1.py", closing[0]); self.assertIn("test_bad3.py", closing[0])
        self.assertEqual(p.stdout.strip().split("\n")[-1], "RESULT: FAIL")

    def test_green_chain_prints_pass_and_no_failed_list(self):
        p, _ = self.run_chain(self.tree(["test_a.py", "test_b.py"]))
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("failed entries", p.stdout)
        self.assertEqual(p.stdout.strip().split("\n")[-1], "RESULT: PASS")

    def test_broken_lists_run_no_entry(self):
        cases = [
            self.tree(["test_a.py"], tests=("test_a.py", "test_x.py")),
            self.tree(["test_a.py", "test_gone.py"], tests=("test_a.py",)),
            self.tree(["test_a.py", "test_a.py"], tests=("test_a.py",)),
            self.tree(["test_a.py"], tests=("test_a.py",), last=False),
        ]
        for d in cases:
            p, ran = self.run_chain(d)
            self.assertEqual(p.returncode, 1, p.stdout)
            self.assertIn("NOT RUN:", p.stdout)
            self.assertEqual(len(re.findall(r"^RUN ", p.stdout, re.M)), 0)
            self.assertEqual(ran, [])

    def test_failed_serial_entry_does_not_stop_the_parallel_entries(self):
        names = ["test_bad1.py", "test_p1.py", "test_bad2.py"]
        p, ran = self.run_chain(self.tree(names, par=["test_p1.py", "test_bad2.py"]))
        self.assertEqual(ran, sorted(names))
        self.assertIn("RUN cloud/launcher/tests/test_p1.py\n", p.stdout)
        self.assertIn("RUN cloud/launcher/tests/test_bad2.py\n", p.stdout)
        self.assertEqual(len(re.findall(r"^failed entries: 2: ", p.stdout, re.M)), 2 - 1)
        self.assertEqual(p.returncode, 1)

    def test_entry_that_fails_at_once_does_not_stop_the_next_entry(self):
        p, ran = self.run_chain(self.tree(["test_bad.py", "test_next.py"]))
        self.assertIn("test_next.py", ran)

    def test_each_entry_gets_its_arguments(self):
        d = self.tree(["test_cw_names.py"])
        self.entry(d, "test_cw_names.py", "import sys\nprint('ARGS', sys.argv[1:])\n")
        p = subprocess.run([os.path.join(d, "bin", "test-cloud-launcher"), "-v"], cwd=d, capture_output=True, text=True,
                           env=dict(os.environ, CHAIN_LOG=tempfile.mkdtemp(prefix="chain-log-")))
        self.assertIn("ARGS ['-v']", p.stdout)

    def test_green_output_is_the_old_output_with_time_seconds_masked(self):
        old = subprocess.run(["git", "show", "HEAD:bin/test-cloud-launcher"], cwd=base.ROOT, capture_output=True, text=True)
        if old.returncode != 0:
            self.skipTest("no git history")
        mask = lambda t: re.sub(r"^(TIME \S+) \d+s$", r"\1 Ns", t, flags=re.M)
        d = self.tree(["test_a.py", "test_b.py", "test_c.py"], par=["test_b.py"])
        new, _ = self.run_chain(d)
        self.script(d, old.stdout)
        os.chmod(os.path.join(d, "bin", "test-cloud-launcher"), 0o755)
        # the old text is HEAD's: a green chain prints the same lines in both
        oldp, _ = self.run_chain(d)
        self.assertEqual(mask(new.stdout).split("\n")[-3:], mask(oldp.stdout).split("\n")[-3:])
        self.assertEqual(sorted(mask(new.stdout).split("\n")), sorted(mask(oldp.stdout).split("\n")))


class Signals(Chain):
    HANG = ("import os, subprocess, sys\nc = subprocess.Popen(['sleep', '300'])\n"
            "open(os.path.join(os.environ['CHAIN_LOG'], 'pids'), 'w').write('%d %d' % (os.getpid(), c.pid))\n"
            "c.wait()\n")

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        try:   # a defunct process (not yet reaped by its new parent) is no longer running
            with open("/proc/%d/stat" % pid) as f:
                return f.read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return True

    def hang_run(self, wrapper):
        d = self.tree(["test_hang.py"])
        self.entry(d, "test_hang.py", self.HANG)
        log = tempfile.mkdtemp(prefix="chain-log-"); self.addCleanup(shutil.rmtree, log, True)
        p = stub_support.start(self, wrapper + [os.path.join(d, "bin", "test-cloud-launcher")], cwd=d,
                               env=dict(os.environ, CHAIN_LOG=log), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p, log

    def pids_of(self, log):
        f = os.path.join(log, "pids")
        for _ in range(100):
            text = open(f).read() if os.path.exists(f) else ""
            if text.count(" "):
                return [int(x) for x in text.split()]
            time.sleep(0.1)
        self.fail("the hung entry did not start")

    def gone(self, pids):
        for _ in range(100):
            if not any(self.alive(x) for x in pids):
                return True
            time.sleep(0.1)
        return False

    def test_terminate_signal_stops_the_entry_and_its_descendants(self):
        p, log = self.hang_run([])
        pids = self.pids_of(log)
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=20)
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(self.gone(pids), pids)

    def test_external_time_limit_ends_a_hung_entry_and_descendants(self):
        if not shutil.which("timeout"):
            self.skipTest("no timeout command")
        p, log = self.hang_run(["timeout", "3"])
        pids = self.pids_of(log)
        p.wait(timeout=30)
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(self.gone(pids), pids)


class Mutant(Chain):
    def mutant_run(self, text_of):
        names = ["test_bad1.py", "test_ok.py", "test_bad3.py"]
        d = self.tree(names)
        path = os.path.join(d, "bin", "test-cloud-launcher")
        src = base.slurp(path)
        new = text_of(src)
        self.assertNotEqual(new, src, "the mutation was not applied")
        self.script(d, new)
        return self.run_chain(d)

    def test_mutant_restoring_the_stop_at_first_failure_is_caught(self):
        def stop(src):
            return src.replace('wait $sp || failed="$failed $e"', 'wait $sp || { echo "RESULT: FAIL"; exit 1; }')
        p, ran = self.mutant_run(stop)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(ran, ["test_bad1.py"])   # 1 entry ran, where the chain requires 3
        self.assertNotIn("failed entries", p.stdout)

    def test_mutant_that_changes_nothing_is_not_accepted(self):
        with self.assertRaises(AssertionError):
            self.mutant_run(lambda s: s)


class Docs(unittest.TestCase):
    def test_documentation_says_it(self):
        t = base.slurp(README)
        for phrase in ("Every entry runs even when one fails", "`FAILED: <entry>`", "`failed entries: N: <names>`",
                       "completeness checks (`NOT RUN:`) still stop the run before any entry", "time limit"):
            self.assertIn(phrase, t)


if __name__ == "__main__":
    unittest.main()
