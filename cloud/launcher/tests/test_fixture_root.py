#!/usr/bin/env python3
"""Test of scenario S9 of #1595: the entries that load the fixture of bin/test-cloud-worker run at the same time as the entry that judges the repository root,
and the root shows no untracked entry at any judgement."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
sys.path.insert(0, str(ROOT / "cloud" / "worker" / "tests"))
import stub_support  # noqa: E402

ROUNDS = 10
WAVE_SIZE = 5
PY = sys.executable
TINY_LOADER = (
    "import importlib.machinery, importlib.util, os, sys, unittest\n"
    "sys.path.insert(0, %r)\n"
    "loader = importlib.machinery.SourceFileLoader('zz_s9_tiny', %r)\n"
    "spec = importlib.util.spec_from_file_location('zz_s9_tiny', %r, loader=loader)\n"
    "m = importlib.util.module_from_spec(spec)\n"
    "sys.modules['zz_s9_tiny'] = m; spec.loader.exec_module(m)\n"
    "class Case(m.Base):\n"
    "    def test_trivial(self):\n"
    "        self.assertTrue(True)\n"
    "r = unittest.TextTestRunner(stream=open(os.devnull, 'w')).run(unittest.defaultTestLoader.loadTestsFromTestCase(Case))\n"
    "sys.exit(0 if r.wasSuccessful() else 1)\n"
) % (str(HERE), str(ROOT / "bin" / "test-cloud-worker"), str(ROOT / "bin" / "test-cloud-worker"))
ROUND_TIMEOUT = 300   # a hang guard only, never what the test judges
JUDGE = [PY, "-m", "unittest", "test_orchestrator_agent.RepoRootClean"]


def status(root):
    return subprocess.run(["git", "status", "--short", "--untracked-files=all"], cwd=root, capture_output=True, text=True).stdout


class FixtureRoot(unittest.TestCase):
    def _wave(self, size):
        procs = []
        for _ in range(size):
            tiny = stub_support.start(self, [PY, "-c", TINY_LOADER], cwd=str(HERE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            judge = stub_support.start(self, JUDGE, cwd=str(HERE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append((tiny, judge))
        deadline = time.monotonic() + ROUND_TIMEOUT   # hang guard for the whole wave
        rounds = []
        timed_out = False
        for tiny, judge in procs:
            codes = []
            for p in (tiny, judge):
                try:
                    codes.append(p.wait(timeout=max(0.1, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    codes.append(-1)
            rounds.append(codes)
        if timed_out:
            for tiny, judge in procs:
                for p in (tiny, judge):
                    if p.poll() is None:
                        p.kill()
                        p.wait()
            rounds = [[-1, -1] for _ in rounds]   # the wave's rounds count as failures
        return rounds

    def test_s9_no_untracked_entry_in_the_root_while_the_loaders_run(self):
        t0 = time.monotonic()
        before = status(ROOT)
        failures = 0
        untracked = 0
        for wave in range(ROUNDS // WAVE_SIZE):
            rounds = self._wave(WAVE_SIZE)
            now = status(ROOT)
            if [l for l in now.splitlines() if l.startswith("??") and l not in before.splitlines()]:
                untracked += WAVE_SIZE   # a new '??' entry counts once for each round of the wave
            failures += sum(1 for codes in rounds if any(c != 0 for c in codes))
        print("S9: rounds %d, failures %d, untracked entries in the root %d of %d judgements" % (ROUNDS, failures, untracked, ROUNDS))
        print("S9 waves: %d of %d rounds, seconds %d" % (ROUNDS // WAVE_SIZE, WAVE_SIZE, time.monotonic() - t0))
        self.assertEqual(failures, 0)
        self.assertEqual(untracked, 0)

    def test_s9_the_old_directory_shows_an_untracked_entry(self):
        src = (ROOT / "bin" / "test-cloud-worker").read_text()
        old_line = "tmp = tempfile.TemporaryDirectory()"
        mutant = src.replace(old_line, "tmp = tempfile.TemporaryDirectory(dir=FAKE_ROOT)", 1)
        mutant = mutant.replace("BIN = Path(__file__).resolve().parent\n", "BIN = Path(%r)\n" % str(ROOT / "bin"), 1)
        applied = mutant != src and "tmp = tempfile.TemporaryDirectory(dir=FAKE_ROOT)" in mutant
        with tempfile.TemporaryDirectory() as scratch, tempfile.TemporaryDirectory() as fake_root:
            subprocess.run(["git", "init", "-q", fake_root], check=True, capture_output=True)
            copy = Path(scratch) / "zz_s9_mutant.py"
            copy.write_text(mutant)
            script = (
                "import importlib.util, os, sys, unittest\n"
                "sys.path.insert(0, %r)\n"
                "spec = importlib.util.spec_from_file_location('zz_s9_mutant', %r)\n"
                "m = importlib.util.module_from_spec(spec)\n"
                "m.FAKE_ROOT = %r\n"
                "sys.modules['zz_s9_mutant'] = m; spec.loader.exec_module(m)\n"
                "seen = []\n"
                "class Case(m.Base):\n"
                "    def test_loader_runs(self):\n"
                "        seen.append([n for n in os.listdir(%r) if n != '.git'])\n"
                "unittest.TextTestRunner(stream=open(os.devnull, 'w')).run(unittest.defaultTestLoader.loadTestsFromTestCase(Case))\n"
                "sys.exit(1 if any(seen) else 0)\n"
            ) % (str(HERE), str(copy), fake_root, fake_root)
            p = subprocess.run([PY, "-c", script], cwd=str(HERE), capture_output=True, text=True)
        print("S9 mutant (the old directory): exit %d, mutation applied %s" % (p.returncode, applied))
        self.assertTrue(applied)
        self.assertEqual(p.returncode, 1, p.stderr[-800:])


if __name__ == "__main__":
    unittest.main()
