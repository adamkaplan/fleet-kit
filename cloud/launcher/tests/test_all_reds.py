"""Ask #2387: bin/test-cloud-launcher runs EVERY entry and lists ALL reds. Offline; scratch trees under the temp dir."""
import os, shutil, subprocess, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SCRIPT = os.path.join(ROOT, "bin", "test-cloud-launcher")
FIXED_LAST = "cloud/worker/tests/test_cloud_prepush_preflight.py"
OLD_EXIT = '{ echo "RESULT: FAIL"; kill $pids 2>/dev/null; exit 1; }'


class AllReds(unittest.TestCase):
    def run_chain(self, names, script_text=None):
        d = tempfile.mkdtemp(prefix="allreds-"); self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "bin")); os.makedirs(os.path.join(d, "cloud/launcher/tests")); os.makedirs(os.path.join(d, "cloud/worker/tests"))
        if script_text is None:
            with open(SCRIPT) as f:
                script_text = f.read()
        with open(os.path.join(d, "bin", "test-cloud-launcher"), "w") as f:
            f.write(script_text)
        os.chmod(os.path.join(d, "bin", "test-cloud-launcher"), 0o755)
        for n in names:
            with open(os.path.join(d, "cloud/launcher/tests", n), "w") as f:
                f.write("import sys, os\nopen(os.path.join(os.environ['CHAIN_LOG'], %r), 'a').write('x')\nsys.exit(int(%r.startswith('test_bad')))\n" % (n, n))
        with open(os.path.join(d, FIXED_LAST), "w") as f:
            f.write("pass\n")
        with open(os.path.join(d, "bin", "test-cloud-launcher.chain"), "w") as f:
            f.write("".join("cloud/launcher/tests/%s\n" % n for n in names) + FIXED_LAST + "\n")
        log = tempfile.mkdtemp(prefix="allreds-log-"); self.addCleanup(shutil.rmtree, log, True)
        p = subprocess.run([os.path.join(d, "bin", "test-cloud-launcher")], cwd=d, env=dict(os.environ, CHAIN_LOG=log), capture_output=True, text=True)
        return p, sorted(os.listdir(log))

    NAMES = ["test_bad1.py", "test_ok2.py", "test_bad3.py"]

    def check_all_reds(self, p, ran):
        lines = p.stdout.split("\n")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("FAILED FILES (2):", lines)
        tail = lines[lines.index("FAILED FILES (2):"):]
        self.assertIn("cloud/launcher/tests/test_bad1.py", tail)
        self.assertIn("cloud/launcher/tests/test_bad3.py", tail)
        self.assertNotIn("cloud/launcher/tests/test_ok2.py", tail)
        self.assertEqual(lines[-2], "RESULT: FAIL")
        self.assertIn("test_bad3.py", ran)

    def test_two_reds_are_both_listed_and_the_last_entry_ran(self):
        p, ran = self.run_chain(self.NAMES)
        self.check_all_reds(p, ran)
        self.assertIn("FAILED: cloud/launcher/tests/test_bad1.py", p.stdout.split("\n"))

    def test_all_passing_prints_pass(self):
        p, _ = self.run_chain(["test_ok1.py", "test_ok2.py"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip().split("\n")[-1], "RESULT: PASS")
        self.assertNotIn("FAILED FILES", p.stdout)

    def test_mutant_with_the_old_early_exit_is_caught(self):
        with open(SCRIPT) as f:
            s = f.read()
        i = s.index('|| { FAILED="$FAILED$e')
        j = s.index("nfail=$((nfail + 1)); }", i) + len("nfail=$((nfail + 1)); }")
        mutant = s[:i] + "|| " + OLD_EXIT + s[j:]
        self.assertNotEqual(mutant, s)
        p, ran = self.run_chain(self.NAMES)  # control first: the real script passes the check
        self.check_all_reds(p, ran)
        p, ran = self.run_chain(self.NAMES, mutant)
        with self.assertRaises(AssertionError):
            self.check_all_reds(p, ran)


if __name__ == "__main__":
    unittest.main()
