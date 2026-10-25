"""Every named quick scan must RESOLVE and RUN (exit 0) on a clean tree; a mutant that runs a shell script with python fails."""
import importlib.machinery, importlib.util, os, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TestScansRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="scans-run-")
        cls.wt = os.path.join(cls.tmp, "wt")
        subprocess.run(["git", "worktree", "add", "--detach", cls.wt, "HEAD"], cwd=ROOT, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["git", "worktree", "remove", "--force", cls.wt], cwd=ROOT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "worktree", "prune"], cwd=ROOT)
        os.rmdir(cls.tmp)

    def test_every_scan_entry_runs_clean(self):
        pre = load(os.path.join(ROOT, "bin", "cloud-pr-preflight"), "pre_ok")
        for rel, sel in pre.cloud_quick.SCAN_TESTS:
            with self.subTest(scan=rel, selector=sel):
                self.assertEqual(pre.run_one(self.wt, rel, 140, sel), 0)

    def test_mutant_running_shell_with_python_fails(self):
        src = open(os.path.join(ROOT, "bin", "cloud-pr-preflight")).read()
        old = '["sh", rel]'
        self.assertIn(old, src)
        mut = os.path.join(self.tmp, "mutant.py")
        with open(mut, "w") as f:
            f.write(src.replace(old, "[sys.executable, rel]"))
        m = load(mut, "pre_mut")
        m.cloud_quick  # the helper must still import
        self.assertNotEqual(m.run_one(self.wt, "bin/test-cloud-neutral-names", 60, None), 0)
        os.unlink(mut)

    def test_total_bound_exceeded_is_not_clean(self):
        with open(os.path.join(self.wt, "cloud", "launcher", "cloud_worker.py"), "a") as f:
            f.write("\n# bound probe\n")
        env = dict(os.environ, CLOUD_PREFLIGHT_QUICK_TOTAL="0", CLOUD_PREFLIGHT_BASE="HEAD", PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, os.path.join(self.wt, "bin", "cloud-pr-preflight"), "--quick"], cwd=self.wt,
                           env=env, capture_output=True, text=True, timeout=140)
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOT RUN: quick run exceeded 0 s", r.stdout)
        self.assertNotIn("RESULT: PASS", r.stdout)


if __name__ == "__main__":
    unittest.main()
