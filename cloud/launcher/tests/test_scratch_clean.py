#!/usr/bin/env python3
"""Tests of the scratch cleanup of the fixtures of bin/test-cloud-worker (#1595): a child left running is ended, a stubborn child is killed,
the directory is removed, and RepoRootClean judges only what its own classes leave."""
import importlib.machinery
import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
sys.path.insert(0, str(ROOT / "cloud" / "worker" / "tests"))
import stub_support  # noqa: E402
_loader = importlib.machinery.SourceFileLoader("cw_tests_of_the_worker_scratch", str(ROOT / "bin" / "test-cloud-worker"))
cwt = importlib.util.module_from_spec(importlib.util.spec_from_loader("cw_tests_of_the_worker_scratch", _loader))
sys.modules["cw_tests_of_the_worker_scratch"] = cwt
_loader.exec_module(cwt)

AGENT_TEST = HERE / "test_orchestrator_agent.py"
STUBBORN = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)  # %s"
CHILD = "import time; time.sleep(300)  # %s"


def root_entries():
    return subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, capture_output=True, text=True).stdout


def alive(pid):
    return cwt._alive(pid)


def run_case(case):
    with open(os.devnull, "w") as null:
        return unittest.TextTestRunner(stream=null).run(unittest.defaultTestLoader.loadTestsFromTestCase(case))


class LateCleanups:
    """Takes the cleanups stub_support.start registers, so they run after the fixture's own cleanup (which is what is under test), not before it."""
    def __init__(self):
        self.calls = []

    def addCleanup(self, fn, *args):
        self.calls.append((fn, args))

    def run(self):
        for fn, args in reversed(self.calls):
            fn(*args)


class ScratchCleanup(unittest.TestCase):
    def _run(self, argv_of):
        record = {}
        late = LateCleanups()
        self.addCleanup(late.run)

        class ChildLeft(cwt.Base):
            def test_leaves_a_child(self):
                p = stub_support.start(late, argv_of(self.home), cwd=str(self.home))
                record["pid"] = p.pid
                record["home"] = str(self.home)
                record["p"] = p
                record["alive_before"] = alive(p.pid)
                time.sleep(0.5)
        result = run_case(ChildLeft)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        record["p"].poll()   # reap: a zombie is not alive
        entries = [l for l in root_entries().splitlines() if os.path.basename(record["home"]) in l]
        return alive(record["pid"]), entries, record

    def test_s3_a_running_child_is_ended_and_the_directory_removed(self):
        before = root_entries()
        gone, entries, rec = self._run(lambda home: [sys.executable, "-c", CHILD % home])
        print("S3: children alive %d, directory entries %d" % (int(gone), len(entries)))
        self.assertFalse(gone)
        self.assertEqual(entries, [])
        self.assertFalse(os.path.exists(rec["home"]))
        self.assertEqual(root_entries(), before)

    def test_s3_the_child_is_alive_before_cleanup(self):
        gone, entries, rec = self._run(lambda home: [sys.executable, "-c", CHILD % home])
        print("S3: child alive before cleanup %s" % rec["alive_before"])
        self.assertTrue(rec["alive_before"])

    def test_s3_the_bare_cleanup_leaves_the_child_alive(self):
        src = (ROOT / "bin" / "test-cloud-worker").read_text()
        mutant = src.replace("self.addCleanup(end_children_and_remove, tmp, self.home)", "self.addCleanup(tmp.cleanup)", 1)
        applied = mutant != src
        old_bin = "BIN = Path(__file__).resolve().parent\n"
        mutant = mutant.replace(old_bin, "BIN = Path(%r)\n" % str(ROOT / "bin"), 1)   # the copy lives outside the repository: its BIN is the real bin directory
        applied = applied and old_bin not in mutant and ("BIN = Path(%r)" % str(ROOT / "bin")) in mutant
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        copy = Path(scratch.name) / "zz_s3_mutant.py"
        script = (
            "import importlib.util, os, sys, time, unittest\n"
            "sys.path.insert(0, %r)\n"
            "import stub_support\n"
            "spec = importlib.util.spec_from_file_location('zz_s3_mutant', %r)\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "rec = {}\n"
            "class Late:\n"
            "    def addCleanup(self, fn, *a): pass\n"
            "class Case(m.Base):\n"
            "    def test_child(self):\n"
            "        p = stub_support.start(Late(), [sys.executable, '-c', %r %% self.home], cwd=str(self.home))\n"
            "        rec['p'] = p\n"
            "        time.sleep(0.5)\n"
            "r = unittest.TextTestRunner(stream=open(os.devnull, 'w')).run(unittest.defaultTestLoader.loadTestsFromTestCase(Case))\n"
            "rec['p'].poll()\n"
            "left = m._alive(rec['p'].pid)\n"
            "if left: rec['p'].kill(); rec['p'].wait()\n"
            "sys.exit(1 if (left or not r.wasSuccessful()) else 0)\n"
        ) % (str(HERE), str(copy), CHILD)
        try:
            copy.write_text(mutant)
            p = subprocess.run([sys.executable, "-c", script], cwd=str(HERE), capture_output=True, text=True)
        finally:
            scratch.cleanup()
        print("S3 mutant (the old bare cleanup): exit %d, mutation applied %s" % (p.returncode, applied))
        self.assertTrue(applied)
        self.assertEqual(p.returncode, 1, p.stderr[-800:])

    def test_s4_a_child_that_ignores_the_end_signal_is_killed(self):
        self.assertEqual(cwt.CHILD_END_WAIT_SECONDS, 15)
        gone, entries, rec = self._run(lambda home: [sys.executable, "-c", STUBBORN % home])
        print("S4: children alive %d, directory entries %d, bounded wait %d seconds" % (int(gone), len(entries), cwt.CHILD_END_WAIT_SECONDS))
        self.assertFalse(gone)
        self.assertEqual(entries, [])
        self.assertFalse(os.path.exists(rec["home"]))


class RepoRootJudgement(unittest.TestCase):
    # Finding (read, not assumed): nothing in bin/test-cloud-launcher.parallel (the 3 parallel lanes) judges the repository root's status:
    # test_cloud_worker.py U1Scope diffs tracked paths and is skipped without CW_CHECK_SCOPE; test_stub_cleanup.py walks only cloud/*/tests and
    # bin/test-cloud-worker; no step of .github/workflows/test.yml reads the root's status. Only test_orchestrator_agent.py RepoRootClean
    # judges it ('git status'), and it is a serial-loop entry, as this file is. Even so the planted directory and the leaker's file are written
    # into a temporary git repository, never the real root, and the copy of RepoRootClean is pointed at that repository: no run can overlap another.
    def _copy_run(self, mutate=None, plant=False):
        src = AGENT_TEST.read_text()
        with tempfile.TemporaryDirectory() as scratch, tempfile.TemporaryDirectory() as fake_root:
            subprocess.run(["git", "init", "-q", fake_root], check=True, capture_output=True)
            if plant:
                planted = Path(fake_root) / ("zz-planted-%d-%d" % (os.getpid(), int(time.time())))
                planted.mkdir()
                (planted / "file").write_text("x")
            out = mutate(src, fake_root) if mutate else src
            applied = out != src or mutate is None   # a mutant must change the text; the unmutated run is judged by the rewrites below
            old_here = "HERE = os.path.dirname(os.path.abspath(__file__))\n"
            out = out.replace(old_here, "HERE = %r\n" % str(HERE), 1)   # the copy lives outside the repository: HERE (and so LAUNCHER, ROOT) is the real path
            applied = applied and old_here not in out and ("HERE = %r" % str(HERE)) in out
            old_status = '"--untracked-files=all"], cwd=ROOT,'
            out2 = out.replace(old_status, '"--untracked-files=all"], cwd=%r,' % fake_root, 1)   # the judged root is the temporary repository
            applied = applied and out2 != out
            mod = "zzscratchcopy%d" % os.getpid()
            (Path(scratch) / (mod + ".py")).write_text(out2)
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(HERE)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])))
            p = subprocess.run([sys.executable, "-m", "unittest", mod + ".RepoRootClean"], cwd=scratch, env=env, capture_output=True, text=True)
        return applied, p

    def test_s5_a_planted_directory_does_not_fail_it(self):
        applied, p = self._copy_run(plant=True)
        print("S5: planted directory, exit %d" % p.returncode)
        self.assertTrue(applied)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])

    def _leaker(self, src, root):
        # a class of its own suite that leaves a file in the judged root
        return src.replace("        for cls in (Orchestrator, Entrypoint):\n",
                           "        open(os.path.join(%r, 'zz-leak-%%d' %% os.getpid()), 'w').write('x')\n        for cls in (Orchestrator, Entrypoint):\n" % root, 1)

    def test_s5_a_file_left_by_its_own_classes_fails_it(self):
        applied, p = self._copy_run(self._leaker)
        print("S5: mutant exit %d, mutation applied %s" % (p.returncode, applied))
        self.assertTrue(applied)
        self.assertEqual(p.returncode, 1, p.stderr[-800:])

    def test_s6_the_first_assertion_catches_the_mutant(self):
        applied, p = self._copy_run(self._leaker)
        print("S6: 1 of 1 mutants exit %d, 1 of 1 mutations applied %s" % (p.returncode, applied))
        self.assertTrue(applied)
        self.assertIn("scratch left in the repository root", p.stderr)
        self.assertEqual(p.returncode, 1)


class ZombieChild(unittest.TestCase):
    def _killed_zombie_gone(self, p):
        p.send_signal(signal.SIGKILL)   # ended, never waited for by the test: a zombie until _alive reaps it; the helper's cleanup is registered for the end
        end = time.time() + 5
        while time.time() < end and alive(p.pid):
            time.sleep(0.05)
        return not alive(p.pid)

    def test_s9_a_zombie_child_counts_gone_and_a_live_child_alive(self):
        live = stub_support.start(self, ["sleep", "300"], cwd=tempfile.gettempdir())
        live_ok = alive(live.pid) is True
        live.kill()
        live.wait()
        live_ok = live_ok and alive(live.pid) is False
        zombie = stub_support.start(self, ["sleep", "300"], cwd=tempfile.gettempdir())
        gone = self._killed_zombie_gone(zombie)
        print("zombie counted gone: %s, live child alive: %s" % (gone, live_ok))
        self.assertTrue(gone)
        self.assertTrue(live_ok)

    def test_s9_a_zombie_child_counts_gone_without_proc(self):
        real_open = open
        zombie = stub_support.start(self, ["sleep", "300"], cwd=tempfile.gettempdir())

        def no_proc(path, *a, **k):
            if str(path).startswith("/proc/"):
                raise OSError("unreadable")
            return real_open(path, *a, **k)
        with mock.patch("builtins.open", no_proc):
            gone = self._killed_zombie_gone(zombie)
        print("zombie counted gone without /proc: %s" % gone)
        self.assertTrue(gone)

    def test_s9_the_mutant_that_trusts_the_signal_alone_fails(self):
        src = (ROOT / "bin" / "test-cloud-worker").read_text()
        m = re.search(r"def _alive\(pid\):\n.*?\n\n\n", src, re.S)
        real = m.group(0)
        mutant = real[:real.index("    try:   # our own child")] + "    return True\n"
        applied = mutant != real
        zombie = stub_support.start(self, ["sleep", "300"], cwd=tempfile.gettempdir())
        zombie.send_signal(signal.SIGKILL)   # a zombie: the mutant process (not its parent) asks about it
        time.sleep(1)
        script = ("import os, subprocess, sys\n" + mutant +
                  "assert not _alive(int(sys.argv[1])), 'zombie counted alive'\n")
        p = subprocess.run([sys.executable, "-c", script, str(zombie.pid)], capture_output=True, text=True)
        print("S9: mutant exit %d, mutation applied %s" % (p.returncode, applied))
        self.assertTrue(applied)
        self.assertEqual(p.returncode, 1, p.stderr[-800:])


class Places(unittest.TestCase):
    def test_s2_every_place_that_makes_a_scratch_directory_under_the_root(self):
        out = subprocess.run(["git", "grep", "-l", "-E", "SourceFileLoader.*test-cloud-worker", "--", "bin", "cloud"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        found = sorted((set(out) | {"bin/test-cloud-worker"}) - {"cloud/launcher/tests/test_scratch_clean.py"})   # this file is the test itself
        print("S2: places: " + ", ".join(found))
        # the plan named five; shapes and stub_cleanup do not load Base (shapes uses test_cloud_worker's, stub_cleanup only reads the file)
        self.assertEqual(found, sorted(["bin/test-cloud-worker", "bin/test-cloud-retired", "cloud/launcher/tests/test_orchestrator_hub_session.py"]))
        print("S2: the evidence decides: the test judged the whole root (certain); the fixture's children are the probable origin")


class Docs(unittest.TestCase):
    def test_s8_the_documentation_says_where_the_scratch_goes(self):
        text = " ".join((ROOT / "cloud" / "launcher" / "README.md").read_text().lower().split())
        phrases = ("the fixtures of `bin/test-cloud-worker` make their scratch directory under the repository root",
                   "the launcher tests make theirs in the platform's temporary directory",
                   "at cleanup the fixture ends every child process that names its directory (end signal, wait at most 15 seconds, kill) and then removes the directory")
        found = [p for p in phrases if p in text]
        print("S8: %d of 3 sentences found" % len(found))
        self.assertEqual(len(found), 3)


if __name__ == "__main__":
    unittest.main()
