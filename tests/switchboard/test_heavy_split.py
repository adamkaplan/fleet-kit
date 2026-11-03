"""#2034: bin/heavy_slots.py loads its part bin/heavy_slots_watch.py from its own __file__ and re-exports the 11 names that moved. Five ways load the module, none registers
`heavy_slots` in sys.modules, so the part must never import it. Two mutants on a scratch copy show the two checks bite."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from support.loader import sb
from support.paths import BIN

MOVED = ("Watchdog", "record_stop", "record_lost", "tell_lane", "_held_record", "check_orphans", "_marker_path", "_marker_key", "_reported", "_mark_reported", "_stop_orphan")
NOT_REEXPORTED = "a moved name is not re-exported"
CANNOT_IMPORT = "the part cannot import the module"
CHECK = r'''
import importlib.util, sys
MOVED = %r
spec = importlib.util.spec_from_file_location("heavy_slots_scratch", sys.argv[1])
module = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(module)
except ImportError as error:
    raise AssertionError("the part cannot import the module: %%s" %% error)
bad = [n for n in MOVED if getattr(module, n, None) is None or getattr(module, n) is not getattr(module._part, n)]
if bad:
    raise AssertionError("a moved name is not re-exported: %%s" %% bad)
''' % (MOVED,)


def assert_same_objects(test, module):
    part = module._part
    same = [name for name in MOVED if getattr(module, name) is getattr(part, name)]
    test.assertEqual(len(same), 11, "11 of 11 names identical, got %r" % (same,))


class TestHeavySplit(unittest.TestCase):
    def scratch(self):
        tmp = tempfile.mkdtemp(prefix="split-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for name in ("heavy_slots.py", "heavy_slots_watch.py"):
            shutil.copy(str(BIN / name), os.path.join(tmp, name))
        return tmp

    def run_check(self, tmp):
        empty = tempfile.mkdtemp(prefix="split-cwd-")   # no heavy_slots.py on the path of the child
        self.addCleanup(shutil.rmtree, empty, True)
        return subprocess.run([sys.executable, "-c", CHECK, os.path.join(tmp, "heavy_slots.py")], capture_output=True, text=True, cwd=empty)

    def test_s7_the_switchboard_loader(self):
        assert_same_objects(self, sb.load_heavy_slots())

    def test_s7_the_loader_of_cpu_guard(self):
        import cpu_guard
        spec = importlib.util.spec_from_file_location("heavy_slots", cpu_guard.HEAVY_SLOTS_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert_same_objects(self, module)

    def test_s7_the_loader_of_test_heavy_holders(self):
        import test_heavy_holders
        assert_same_objects(self, test_heavy_holders.load())

    def test_s7_the_loader_of_test_heavy_progress_on_a_scratch_copy(self):
        import test_heavy_progress
        tmp = self.scratch()
        with unittest.mock.patch.dict(os.environ, {"FLEET_TEST_HEAVY_SLOTS": os.path.join(tmp, "heavy_slots.py")}):
            assert_same_objects(self, test_heavy_progress.load_heavy())

    def test_s7_the_loader_of_test_pod_niceness_on_a_scratch_copy(self):
        import test_pod_niceness
        assert_same_objects(self, test_pod_niceness.load(Path(self.scratch()) / "heavy_slots.py"))

    def test_s7_the_unmutated_copy_passes_first(self):
        done = self.run_check(self.scratch())
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_s7_a_mutant_that_drops_the_reexport_fails(self):
        tmp = self.scratch()
        self.assertEqual(self.run_check(tmp).returncode, 0)
        path = os.path.join(tmp, "heavy_slots.py")
        text = open(path).read()
        old = "    globals()[_name] = getattr(_part, _name)\n"
        self.assertEqual(text.count(old), 1)
        open(path, "w").write(text.replace(old, "    pass\n"))
        done = self.run_check(tmp)
        self.assertEqual(done.returncode, 1)
        self.assertIn(NOT_REEXPORTED, done.stderr)

    def test_s7_a_mutant_that_imports_the_module_from_the_part_fails(self):
        tmp = self.scratch()
        self.assertEqual(self.run_check(tmp).returncode, 0)
        path = os.path.join(tmp, "heavy_slots_watch.py")
        text = open(path).read()
        open(path, "w").write(text.replace("import errno\n", "import errno\nimport heavy_slots\n", 1))
        done = self.run_check(tmp)
        self.assertEqual(done.returncode, 1)
        self.assertIn(CANNOT_IMPORT, done.stderr)


if __name__ == "__main__":
    unittest.main()
