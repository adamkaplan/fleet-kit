"""The quick run's time bound clamps each file's timeout; a change no check reads is decided before any run."""
import importlib.machinery, importlib.util, os, shutil, subprocess, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PRE = os.path.join(ROOT, "bin", "cloud-pr-preflight")
PUSH = os.path.join(ROOT, "bin", "cloud-prepush")


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def git(cwd, *a):
    subprocess.run(["git"] + list(a), cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Mini(unittest.TestCase):
    """A small repository: a base commit and a branch commit that changes one file."""

    def make(self, files, changed_path):
        tmp = tempfile.mkdtemp(prefix="quick-bounds-")
        self.addCleanup(shutil.rmtree, tmp, True)
        r = os.path.join(tmp, "r")
        os.makedirs(r)
        git(r, "init", "-q")
        git(r, "config", "user.name", "t")
        git(r, "config", "user.email", "t@users.noreply.github.com")
        for rel, text in files.items():
            write(os.path.join(r, rel), text)
        shutil.copy(os.path.join(ROOT, "bin", "cloud_quick.py"), os.path.join(r, "cloud_quick.py"))
        for rel in files:
            git(r, "add", rel)
        git(r, "add", "cloud_quick.py")
        git(r, "commit", "-qm", "base")
        git(r, "branch", "basebr")
        write(os.path.join(r, changed_path), "changed = 1\n")
        git(r, "add", changed_path)
        git(r, "commit", "-qm", "change")
        return r

    def run_pre(self, r, script, total, extra_env=None):
        d = os.path.join(r, "bin")
        os.makedirs(d, exist_ok=True)
        shutil.copy(script, os.path.join(d, "cloud-pr-preflight"))
        shutil.copy(os.path.join(ROOT, "bin", "cloud_quick.py"), os.path.join(d, "cloud_quick.py"))
        env = dict(os.environ, CLOUD_PREFLIGHT_QUICK_TOTAL=str(total), CLOUD_PREFLIGHT_BASE="basebr",
                   PYTHONDONTWRITEBYTECODE="1")
        env.update(extra_env or {})
        t = time.monotonic()
        p = subprocess.run([sys.executable, os.path.join(d, "cloud-pr-preflight"), "--quick"], cwd=r, env=env,
                           capture_output=True, text=True, timeout=140)
        return p, time.monotonic() - t

    def mutated(self, old, new):
        with open(PRE) as f:
            src = f.read()
        self.assertIn(old, src)
        out = os.path.join(tempfile.mkdtemp(prefix="quick-mut-"), "pre")
        self.addCleanup(shutil.rmtree, os.path.dirname(out), True)
        write(out, src.replace(old, new))
        return out




class TestClamp(Mini):
    def setUp(self):
        self.r = self.make({"lib.py": "x = 0\n", "cloud/launcher/tests/test_slow.py": "import time\n# reads lib.py\ntime.sleep(20)\n",
                            "bin/test-cloud-launcher": "tests/test_slow.py\n"}, "lib.py")

    def test_total_never_exceeds_bound_plus_grace(self):
        p, secs = self.run_pre(self.r, PRE, 4)
        self.assertLess(secs, 4 + 8, p.stdout)
        self.assertIn("NOT RUN (budget)", p.stdout)
        self.assertEqual(p.returncode, 3)
        self.assertNotIn("RESULT: PASS", p.stdout)

    def test_mutant_without_clamp_fails(self):
        m = self.mutated("tmo = min(timeout, max(1, int(remaining))) if quick else timeout", "tmo = timeout")
        with open(m) as f:
            m1 = f.read()
        m2 = m1.replace("tmo = min(timeout, max(1, int(QUICK_TOTAL - (clock() - t_start))))", "tmo = timeout")
        self.assertNotEqual(m2, m1)  # the second clamp is removed too
        write(m, m2)
        p, secs = self.run_pre(self.r, m, 4)
        self.assertGreaterEqual(secs, 4 + 8)


class TestNoReader(Mini):
    def setUp(self):
        marker = os.path.join(tempfile.mkdtemp(prefix="quick-marker-"), "started")
        self.addCleanup(shutil.rmtree, os.path.dirname(marker), True)
        self.marker = marker
        scan = "import os\nopen(os.environ['QUICK_MARKER'], 'w').close()\n"
        self.r = self.make({"bin/test-cloud-neutral-names": scan, "bin/test-cloud-launcher": "x\n",
                            "cloud/launcher/tests/test_x.py": "pass\n"}, "docs_only.md")
        self.env = {"QUICK_MARKER": marker}

    def test_no_reader_is_decided_before_any_run(self):
        p, secs = self.run_pre(self.r, PRE, 240, self.env)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("NOT RUN: no check reads", p.stdout)
        self.assertFalse(os.path.exists(self.marker), "a scan was started")
        self.assertLess(secs, 5)

    def test_mutant_deciding_after_the_scans_fails(self):
        m = self.mutated("return 1  # decided from the reader picks alone: no scan is started", "pass")
        p, secs = self.run_pre(self.r, m, 240, self.env)
        self.assertTrue(os.path.exists(self.marker))

    def test_prepush_decides_before_the_preflight(self):
        pp = load(PUSH, "pp_bounds")
        called = []
        pp.preflight = lambda *a, **k: called.append(1) or (True, [])
        rc = pp.stage(self.r, "basebr", "basebr", ["docs_only.md"], self.r, self.r, 10, 10, time.monotonic(), 10, "quick")
        self.assertEqual(rc, 1)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
