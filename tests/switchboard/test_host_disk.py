"""#2081: no switchboard test reads the free disk of the host it runs on (S2 to S5, U1). World is set by bin/test-switchboard after the import."""
import importlib.util
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from support.core import Base
from support.loader import sb
from support.paths import BIN, REPO

GB = 10 ** 9
SMALL = shutil._ntuple_diskusage(1000 * GB, 995 * GB, 5 * GB)
CORE = REPO / "tests" / "switchboard" / "support" / "core.py"
NAMED = ("TestStoppedNews", "TestShadowClassifier", "TestCosOrdersFact", "TestV2LightGuards", "TestForegroundScenarios", "TestOfflineScenarios", "TestDecorationIsAFact")
ANCHOR = "a test world never reads the host's free disk"


def ample_room(case):
    """The one check of S2 and S5: with the host at 5 GB, a world the support built sees ample room and a pass raises no disk fact."""
    case.assertGreaterEqual(shutil.disk_usage(str(case.home)).free, 400 * GB, ANCHOR)


def load_core(name, text):
    path = Path(case_tmp()) / (name + ".py")
    path.write_text(text)
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def case_tmp():
    import tempfile
    return tempfile.mkdtemp(prefix="hostdisk-")


def run_check(core_module):
    """Run the check on a Base of the given support module, with the host faked at 5 GB; return the unittest result."""
    class Probe(core_module.Base):
        def runTest(self):
            ample_room(self)
    host = mock.patch.object(shutil, "disk_usage", lambda path: SMALL)
    host.start()
    try:
        return unittest.TextTestRunner(stream=open(os.devnull, "w")).run(Probe())
    finally:
        host.stop()


class TestHostDisk(unittest.TestCase):
    """#2081: a test world never reads the host's free disk."""

    def test_a_test_world_sees_ample_room_when_the_host_has_5_gb(self):   # S2
        host = mock.patch.object(shutil, "disk_usage", lambda path: SMALL)
        host.start()
        self.addCleanup(host.stop)

        class Probe(Base):
            def runTest(self):
                ample_room(self)
                self.assertEqual(sb.disk_guard_settings({}), {"min_free_gb": 50, "hard_free_gb": 10})
                self.assertIsNotNone(sb.disk_free(self.home))
                self.assertGreaterEqual(sb.disk_free(self.home)[0], 400 * GB)
        host.stop()
        host.start()
        result = unittest.TextTestRunner(stream=open(os.devnull, "w")).run(Probe())
        self.assertEqual((result.failures, result.errors), ([], []))

    def test_the_mutant_that_reads_the_real_disk_again_is_caught(self):   # S5
        text = CORE.read_text()
        control = run_check(load_core("hostdisk_control", text))
        self.assertEqual((len(control.failures), len(control.errors), control.testsRun), (0, 0, 1), "the control must pass first")
        lines = text.splitlines(keepends=True)
        start = next(i for i, l in enumerate(lines) if "ample = mock.patch.object(shutil" in l)
        self.assertTrue("ample.start()" in lines[start + 1] and "addCleanup(ample.stop)" in lines[start + 2])
        mutant_text = "".join(lines[:start] + lines[start + 3:])
        self.assertNotEqual(mutant_text, text)
        mutant = run_check(load_core("hostdisk_mutant", mutant_text))
        self.assertEqual(len(mutant.failures), 1, mutant.errors)
        self.assertIn(ANCHOR, mutant.failures[0][1])

    def test_the_disk_guard_settings_are_as_they_were(self):   # U1
        self.assertEqual(sb.disk_guard_settings({}), {"min_free_gb": 50, "hard_free_gb": 10})
        self.assertEqual(sb.disk_guard_settings({"disk_guard": {"min_free_gb": 80}}), {"min_free_gb": 80, "hard_free_gb": 10})
        self.assertEqual(sb.disk_guard_settings({"disk_guard": {"min_free_gb": 80, "hard_free_gb": 20}}), {"min_free_gb": 80, "hard_free_gb": 20})
        self.assertIsNone(sb.disk_guard_settings({"disk_guard": False}))

    def test_the_named_group_passes_with_the_host_at_5_gb(self):   # S3
        site = case_tmp()
        Path(site, "sitecustomize.py").write_text("import shutil\n_u = shutil._ntuple_diskusage(1000 * 10 ** 9, 995 * 10 ** 9, 5 * 10 ** 9)\nshutil.disk_usage = lambda path: _u\n")
        env = dict(os.environ, PYTHONPATH=site + os.pathsep + os.environ.get("PYTHONPATH", ""), FLEET_HEAVY="off")
        done = subprocess.run([sys.executable, str(BIN / "test-switchboard")] + list(NAMED), env=env, capture_output=True, text=True, timeout=840)
        tail = done.stdout[-400:]
        self.assertIn(" 0 failed, 0 errored", tail, tail)
