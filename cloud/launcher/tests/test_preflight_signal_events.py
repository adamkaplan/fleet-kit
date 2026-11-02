#!/usr/bin/env python3
"""Ask #1709, S10/S11/S13: the signal helper of test_cloud_pr_preflight waits for the started marker, raises when the script
ends first, and starts its child with the default SIGINT. The REAL tests run in CHILD processes in copies outside the repository."""
import glob, os, shutil, signal, subprocess, sys, tempfile, threading, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
TESTFILE = os.path.join(HERE, "test_cloud_pr_preflight.py")
SCRIPT = os.path.join(ROOT, "bin", "cloud-pr-preflight")
WORKER_TESTS = os.path.join(ROOT, "cloud", "worker", "tests")
NEW_SLEEP = 'SLEEP = "import os, time\\nos.environ.get(\\"CLOUD_PREFLIGHT_STARTED\\") and open(os.environ[\\"CLOUD_PREFLIGHT_STARTED\\"], \\"w\\").close()\\ntime.sleep(60)\\n"'
OLD_SLEEP = 'SLEEP = "import time\\ntime.sleep(60)\\n"'
RESTORE = ", preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL)"
RMTREE = "shutil.rmtree(tmp, ignore_errors=True)"
RESULTS = {}


def read(p):
    with open(p) as f:
        return f.read()


def make_tree(testtext=None, scripttext=None):
    """A copy of the files the real tests need, in a temporary directory outside the repository, laid out as in the repository."""
    d = tempfile.mkdtemp(prefix="pf-events-")
    for rel, text in (("bin/cloud-pr-preflight", scripttext if scripttext is not None else read(SCRIPT)),
                      ("cloud/launcher/tests/test_cloud_pr_preflight.py", testtext if testtext is not None else read(TESTFILE))):
        p = os.path.join(d, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
    os.chmod(os.path.join(d, "bin", "cloud-pr-preflight"), 0o755)
    os.makedirs(os.path.join(d, "cloud", "worker", "tests"))
    for n in os.listdir(WORKER_TESTS):
        if n in ("stub_support.py", "stub_server.py"):
            shutil.copy(os.path.join(WORKER_TESTS, n), os.path.join(d, "cloud", "worker", "tests", n))
    doc = os.path.join(d, "docs"); os.makedirs(doc)
    shutil.copy(os.path.join(ROOT, "docs", "cloud-operator-quickstart.md"), doc)   # read at import by the Docs class of the copy
    return d


def run_real(tree, name, sigint_ignored=False, bound=60):
    """Run Signals.<name> of the copy as a child; returns (exit status, output)."""
    kw = {"preexec_fn": lambda: signal.signal(signal.SIGINT, signal.SIG_IGN)} if sigint_ignored else {}
    cwd = os.path.join(tree, "cloud", "launcher", "tests")
    try:
        p = subprocess.run([sys.executable, "-B", "-m", "unittest", "test_cloud_pr_preflight.Signals." + name],
                           cwd=cwd, capture_output=True, text=True, timeout=bound, **kw)
    except subprocess.TimeoutExpired as e:
        return "bound", "TIMEOUT %s" % (e.output or "")
    return p.returncode, p.stdout + p.stderr


def once(text, old, new):
    assert text.count(old) == 1, "old text must occur exactly once: %r (%d)" % (old, text.count(old))
    out = text.replace(old, new)
    assert out != text
    return out


class Events(unittest.TestCase):
    def tree(self, **kw):
        d = make_tree(**kw); self.addCleanup(shutil.rmtree, d, ignore_errors=True); return d

    def test_s10_source_and_script_that_exits_at_once(self):
        src = read(TESTFILE)
        go = src[src.index("    def go(self"):src.index("    def test_sigterm_cleans_up")]
        sleeps, ranges = go.count("time.sleep(0.5)"), go.count("range(100)")
        self.assertEqual((sleeps, ranges), (0, 0))
        tree = self.tree(scripttext="import sys; sys.exit(3)\n")
        rc, out = run_real(tree, "test_sigterm_cleans_up")
        raised = out.count("AssertionError: the preflight script ended before its test file started")
        self.assertIn("ended before its test file started", out)
        self.assertNotEqual(rc, 0)
        self.assertEqual(raised, 1, out)
        RESULTS["S10"] = "S10: go source has %d time.sleep(0.5) and %d range(100), exit-at-once script raises the text %d of 1" % (sleeps, ranges, raised)
        print(RESULTS["S10"])

    def test_s11_mutant_without_the_marker_fails_with_the_early_end_text(self):
        src = read(TESTFILE)
        m = once(once(src, NEW_SLEEP, OLD_SLEEP), 'CLOUD_PREFLIGHT_TIMEOUT="30"', 'CLOUD_PREFLIGHT_TIMEOUT="2"')
        applied = m != src and OLD_SLEEP in m and NEW_SLEEP not in m
        rc_c, out_c = run_real(self.tree(), "test_sigterm_cleans_up")
        control = int(rc_c == 0)
        self.assertEqual(control, 1, out_c)
        rc_m, out_m = run_real(self.tree(testtext=m), "test_sigterm_cleans_up")
        self.assertIn("ended before its test file started", out_m)
        mutant = int(rc_m != 0)
        self.assertEqual(mutant, 1, out_m)
        self.assertTrue(applied)
        RESULTS["S11"] = "S11: control passes %d of 1, mutant fails %d of 1 with the early-end text, mutation applied %s" % (control, mutant, applied)
        print(RESULTS["S11"])

    def test_s13_sigint_condition(self):
        src = read(TESTFILE)
        name = "test_sigint_cleans_up"
        tmp = tempfile.gettempdir()
        before_c = set(os.listdir(tmp))
        rc_c, out_c = run_real(self.tree(), name, sigint_ignored=True)
        left_c = sorted(x for x in set(os.listdir(tmp)) - before_c if x.startswith("cloud-preflight-"))
        for x in left_c:
            shutil.rmtree(os.path.join(tmp, x), ignore_errors=True)
        control_named = left_c[0] if left_c else ""
        self.assertEqual(control_named, "", left_c)
        control = int(rc_c == 0 and control_named == "")
        self.assertEqual(control, 1, out_c)
        m = once(src, RESTORE, "")
        before = set(os.listdir(tempfile.gettempdir()))
        rc_m, out_m = run_real(self.tree(testtext=m), name, sigint_ignored=True)
        left = sorted(x for x in set(os.listdir(tempfile.gettempdir())) - before if x.startswith("cloud-preflight-"))
        for x in left:
            shutil.rmtree(os.path.join(tempfile.gettempdir(), x), ignore_errors=True)
        self.assertNotEqual(rc_m, 0, out_m)
        # the pinned hang guard raises its own text (the script ignores SIGINT and does not end); the temporary copy it leaves is named by the listing
        self.assertIn("AssertionError: the preflight script did not end within 20 seconds after the signal (hung)", out_m)
        self.assertEqual(len(left), 1, left)
        named = left[0] if left else ""
        failure = out_m + "\nLEFTOVER temporary directory: " + named
        self.assertIn("cloud-preflight-", failure)
        self.assertIn(named, failure)
        self.assertTrue(named)
        mutant = int(rc_m != 0 and bool(named) and named in failure)
        s = read(SCRIPT)
        s2 = once(s, RMTREE, "pass")
        rc_s, out_s = run_real(self.tree(scripttext=s2), name)
        nocleanup = int(rc_s != 0)
        self.assertEqual(nocleanup, 1, out_s)
        applied = RESTORE not in m and RMTREE not in s2
        self.assertTrue(applied)
        RESULTS["S13"] = ("S13: control passes %d of 1 with SIGINT ignored and leaves no directory, mutant without the restoration fails %d of 1 with the leftover directory named: %s, "
                          "script without its cleanup fails the real test %d of 1, mutations applied %s" % (control, mutant, named, nocleanup, applied))
        print(RESULTS["S13"])


OLD_LEFTOVERS = (
    ('def leftovers(tmpdir):\n    return [x for x in os.listdir(tmpdir) if x.startswith("cloud-preflight-")]',
     'def leftovers(before):\n    return [x for x in set(os.listdir(tempfile.gettempdir())) - before if x.startswith("cloud-preflight-")]'),
    ('own_tmp = tempfile.mkdtemp(prefix="preflight-tmp-"); self.addCleanup(shutil.rmtree, own_tmp, ignore_errors=True)',
     'before = set(os.listdir(tempfile.gettempdir()))'),
    ('CLOUD_PREFLIGHT_STARTED=marker, TMPDIR=own_tmp)', 'CLOUD_PREFLIGHT_STARTED=marker)'),
    ('leftovers(own_tmp), "no temp copy', 'leftovers(before), "no temp copy'),
    ('        left = leftovers(own_tmp)\n        for x in left:\n            shutil.rmtree(os.path.join(own_tmp, x), ignore_errors=True)',
     '        left = leftovers(before)\n        for x in left:\n            shutil.rmtree(os.path.join(tempfile.gettempdir(), x), ignore_errors=True)'),
)
FOREIGN = "cloud-preflight-foreign"


class SharedTmp(unittest.TestCase):
    """Ask #1709, PC-4 / S19: the leftovers check looks only in the per-test TMPDIR, never the shared /tmp."""
    def plant_when_started(self):
        """A foreign directory appears in the shared /tmp once a signal test has started its script (an event, no clock)."""
        shared = tempfile.gettempdir()
        path = os.path.join(shared, FOREIGN)
        stop = threading.Event()
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        def watch():
            while not stop.is_set():
                if glob.glob(os.path.join(shared, "preflight-marker-*", "started")):
                    os.makedirs(path, exist_ok=True)
                    return
                stop.wait(0.002)
        t = threading.Thread(target=watch, daemon=True); t.start()
        self.addCleanup(t.join); self.addCleanup(stop.set)

    def test_s19_foreign_directory_in_shared_tmp(self):
        src = read(TESTFILE)
        m = src
        for new, old in OLD_LEFTOVERS:
            m = once(m, new, old)
        name = "test_sigterm_cleans_up"
        os.makedirs(os.path.join(tempfile.gettempdir(), FOREIGN), exist_ok=True)   # planted before the run: the control must pass with it present
        self.addCleanup(shutil.rmtree, os.path.join(tempfile.gettempdir(), FOREIGN), ignore_errors=True)
        rc_c, out_c = run_real(make_tree_cleaned(self), name)
        control = int(rc_c == 0)
        self.assertEqual(control, 1, out_c)
        shutil.rmtree(os.path.join(tempfile.gettempdir(), FOREIGN), ignore_errors=True)
        self.plant_when_started()
        rc_n, out_n = run_real(make_tree_cleaned(self), name)
        self.assertEqual(rc_n, 0, out_n)   # the new text ignores the foreign directory that appeared during the run
        shutil.rmtree(os.path.join(tempfile.gettempdir(), FOREIGN), ignore_errors=True)
        self.plant_when_started()
        rc_m, out_m = run_real(make_tree_cleaned(self, testtext=m), name)
        self.assertNotEqual(rc_m, 0, out_m)
        self.assertIn(FOREIGN, out_m)
        mutant = int(rc_m != 0 and FOREIGN in out_m)
        shutil.rmtree(os.path.join(tempfile.gettempdir(), FOREIGN), ignore_errors=True)
        s2 = once(read(SCRIPT), RMTREE, "pass")
        rc_s, out_s = run_real(make_tree_cleaned(self, scripttext=s2), name)
        nocleanup = int(rc_s != 0)
        self.assertEqual(nocleanup, 1, out_s)
        RESULTS["S19"] = "S19: control passes %d of 1 with %s in the shared tmp, mutant with the old leftovers text fails %d of 1 naming it, script without cleanup fails %d of 1" % (control, FOREIGN, mutant, nocleanup)
        print(RESULTS["S19"])


def make_tree_cleaned(case, **kw):
    d = make_tree(**kw); case.addCleanup(shutil.rmtree, d, ignore_errors=True); return d


class HangGuard(unittest.TestCase):
    def test_s10_script_that_never_starts_raises_under_a_fake_clock(self):
        sys.path.insert(0, HERE)
        sys.path.insert(0, os.path.join(ROOT, "cloud", "worker", "tests"))
        import test_cloud_pr_preflight as m
        self.assertEqual(m._clock, __import__("time").monotonic)   # the control: the real clock, the signal tests run with it
        ticks = iter([1000.0])
        self.addCleanup(setattr, m, "_clock", m._clock)
        m._clock = lambda: next(ticks, 1000.0 + m.START_WAIT_LIMIT + 1)
        t = m.Signals("test_sigterm_cleans_up")
        t.setUp()
        self.addCleanup(t.doCleanups)
        script = os.path.join(t.r.d, "sleeper.py")
        with open(script, "w") as f:
            f.write("import time\ntime.sleep(30)\n")
        started = []
        real = m.stub_support.start
        def spy(*a, **kw):
            p = real(*a, **kw); started.append(p); return p
        self.addCleanup(setattr, m.stub_support, "start", real)
        m.stub_support.start = spy
        with self.assertRaises(AssertionError) as c:
            t.go(signal.SIGTERM, script=script)
        self.assertIn("did not start its test file within", str(c.exception))
        dead = started[0].poll() is not None
        self.assertTrue(dead)
        print("S10: a script that never starts raises `did not start its test file within` under a fake clock, 1 of 1; child killed %s" % dead)


if __name__ == "__main__":
    unittest.main()
