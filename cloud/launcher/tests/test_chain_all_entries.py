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

    FIXTURE = os.path.join(HERE, "fixtures", "test-cloud-launcher.before-1547")   # frozen copy of the script at integration 81d35e7c

    def green_pair(self):
        mask = lambda t: re.sub(r"^(TIME \S+) \d+s$", r"\1 Ns", t, flags=re.M)
        names = ["test_a.py", "test_b.py", "test_cw_names.py", "test_d.py"]
        d = self.tree(names, par=["test_b.py", "test_d.py"], tests=("test_a.py", "test_b.py", "test_d.py"))
        self.entry(d, "test_cw_names.py", "import sys\nprint('ARGS', sys.argv[1:])\n")
        args = ["-v", "-k", "x"]
        def run(script_text):
            self.script(d, script_text)
            os.chmod(os.path.join(d, "bin", "test-cloud-launcher"), 0o755)
            log = tempfile.mkdtemp(prefix="chain-log-"); self.addCleanup(shutil.rmtree, log, True)
            return subprocess.run([os.path.join(d, "bin", "test-cloud-launcher")] + args, cwd=d, capture_output=True, text=True,
                                  env=dict(os.environ, CHAIN_LOG=log))
        return mask, run, d

    def test_green_output_is_the_old_output_with_time_seconds_masked(self):
        mask, run, d = self.green_pair()
        old = run(base.slurp(self.FIXTURE))
        new = run(base.slurp(base.SCRIPT))
        self.assertEqual(old.returncode, 0, old.stdout)
        self.assertEqual(new.returncode, 0, new.stdout)
        ol, nl = mask(old.stdout).split("\n"), mask(new.stdout).split("\n")
        self.assertEqual(len(ol), len(nl))
        # the serial/parallel interleaving is by time: compare as sorted lines
        diff = sum(1 for x, y in zip(sorted(ol), sorted(nl)) if x != y)
        print("S6: differing lines %d of %d" % (diff, len(nl)))
        self.assertEqual(diff, 0)
        self.assertIn("ARGS ['-v', '-k', 'x']", old.stdout)
        self.assertIn("ARGS ['-v', '-k', 'x']", new.stdout)

    def test_s6_hiding_mutant_is_caught_by_the_failing_chain_check(self):
        def failing(script_text):
            d = self.tree(["test_bad1.py", "test_ok.py"])
            self.script(d, script_text)
            os.chmod(os.path.join(d, "bin", "test-cloud-launcher"), 0o755)
            log = tempfile.mkdtemp(prefix="chain-log-"); self.addCleanup(shutil.rmtree, log, True)
            p = subprocess.run([os.path.join(d, "bin", "test-cloud-launcher")], cwd=d, capture_output=True, text=True,
                               env=dict(os.environ, CHAIN_LOG=log))
            res = [l for l in p.stdout.split("\n") if l.startswith("RESULT:")]
            return p.returncode, res[-1] if res else None
        cur = base.slurp(base.SCRIPT)
        self.assertEqual(failing(base.slurp(self.FIXTURE)), (1, "RESULT: FAIL"))
        self.assertEqual(failing(cur), (1, "RESULT: FAIL"))
        hide = cur.replace('echo "RESULT: FAIL"\n  exit 1\nfi\necho "RESULT: PASS"', 'echo "RESULT: PASS"\n  exit 0\nfi\necho "RESULT: PASS"')
        self.assertNotEqual(hide, cur, "the mutation was not applied")
        self.assertNotEqual(failing(hide), (1, "RESULT: FAIL"))


class Signals(Chain):
    HANG = "#!/bin/sh\necho $$ > \"$CHAIN_LOG/pids\"\nsh -c 'echo $$ >> \"$CHAIN_LOG/pids\"; exec sleep 300'\n"

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

    def hang_run(self, wrapper, mutate=None):
        d = self.tree([], tests=(), raw="bin/test-hang\n" + base.FIXED_LAST + "\n")
        self.entry(d, "../../../bin/test-hang", self.HANG)
        if mutate:   # a scratch copy under the temp dir, outside every scanned directory
            src = base.slurp(os.path.join(d, "bin", "test-cloud-launcher"))
            new = mutate(src)
            self.assertNotEqual(new, src, "the mutation was not applied")
            self.script(d, new)
        os.chmod(os.path.join(d, "bin", "test-hang"), 0o755)
        log = tempfile.mkdtemp(prefix="chain-log-"); self.addCleanup(shutil.rmtree, log, True)
        p = stub_support.start(self, wrapper + [os.path.join(d, "bin", "test-cloud-launcher")], cwd=d,
                               env=dict(os.environ, CHAIN_LOG=log), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p, log

    def pids_of(self, log):
        f = os.path.join(log, "pids")
        for _ in range(100):
            text = open(f).read() if os.path.exists(f) else ""
            if len(text.split()) == 2:
                return [int(x) for x in text.split()]
            time.sleep(0.1)
        self.fail("the hung entry did not start")

    def gone(self, pids):
        for _ in range(100):
            if not any(self.alive(x) for x in pids):
                return True
            time.sleep(0.1)
        return False

    def term_problems(self, mutate=None):
        p, log = self.hang_run([], mutate)
        pids = self.pids_of(log)
        p.send_signal(signal.SIGTERM)
        try:
            out = p.communicate(timeout=60)[0]
        except subprocess.TimeoutExpired:
            p.kill(); out = p.communicate()[0]
        out = out.decode() if isinstance(out, bytes) else (out or "")
        bad = []
        if p.returncode != 143:
            bad.append("exit status %s" % p.returncode)
        if "recursion" in out:
            bad.append("recursion")
        if not self.gone(pids):
            bad.append("processes left")
        return bad

    def test_terminate_signal_stops_the_entry_and_its_descendants(self):
        bad = self.term_problems()
        print("TERM run problems:", bad)
        self.assertEqual(bad, [])

    def test_mutant_killing_its_own_shell_in_the_handler_is_caught(self):
        old = 'trap "" TERM INT; for p in $pids; do killtree "$p"; done; exit 143'
        bad = self.term_problems(lambda s: s.replace(old, 'for p in $pids; do killtree "$p"; done; killtree $$; exit 143'))
        self.assertNotEqual(bad, [])

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
