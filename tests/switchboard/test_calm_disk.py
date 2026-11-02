"""Asks #1739 and #1891: no test reads the real disk. S2: a class that reports 13 GB free at the process level still sees the base class's calm reading;
a scratch base without the calm block shows the real reading (the mutant); a test that patches its own 7 GB reading still reaches the guard."""
import collections
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support.core import Base
from support.herdr_world import World
from support.loader import sb
from support.paths import REPO

GB = 10 ** 9
CORE = REPO / "tests" / "switchboard" / "support" / "core.py"
BLOCK = '''        calm_disk = mock.patch.object(sb.shutil, "disk_usage", lambda path: collections.namedtuple("usage", "total used free")(1000 * 10 ** 9, 762 * 10 ** 9, 238 * 10 ** 9))
        calm_disk.start()
        self.addCleanup(calm_disk.stop)
'''
ANCHOR = "a test read the real disk"
SEEN = {}


def reading(free):
    return lambda path: collections.namedtuple("usage", "total used free")(500 * GB, 500 * GB - free, free)


class Measure:
    """One pass of the real engine in a fake world with the default configuration: (free GB read, disk.low events, disk.low keys)."""

    def measure(self):
        world = World(self, agents=("coder",), config={})
        world.add_agent("cos", fleet={"role": "chief-of-staff"})
        engine = world.engine
        engine.discover(world.clock.now())
        found, problems = engine.collect_facts()
        self.assertEqual(problems, [])
        free = sb.disk_free(engine.state_dir)[0] // GB
        events = [e for e in world.audit() if e["event"] == "disk.low"]
        keys = [f.key for f in found if str(f.key).startswith("disk.low")]
        return free, len(events), len(keys)

    def check(self):
        free, events, keys = self.measure()
        if free < 100 or events or keys:
            self.fail("%s: %d GB free, %d disk.low event, %d disk.low keys" % (ANCHOR, free, events, keys))
        return free, events, keys


class Pinned:
    """A class-level patch of the real reading at 13 GB, started BELOW the base's own per-test patch."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.real = mock.patch.object(shutil, "disk_usage", reading(13 * GB))
        cls.real.start()

    @classmethod
    def tearDownClass(cls):
        cls.real.stop()
        super().tearDownClass()


def scratch_base(text):
    path = Path(tempfile.mkdtemp(prefix="acme-core-")) / "core_scratch.py"
    path.write_text(text)
    spec = importlib.util.spec_from_file_location("core_scratch", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    shutil.rmtree(str(path.parent), True)
    return module.Base


class TestCalmDisk(Pinned, Measure, Base):
    def test_a_control_a_calm_reading_under_a_real_one_of_13_gb(self):
        free, events, keys = self.check()
        SEEN.update(free=free, events=events, keys=keys)
        self.assertEqual(free, 238)

    def test_b_mutant_a_base_without_the_calm_block_reads_the_real_disk(self):
        text = CORE.read_text()
        self.assertEqual(text.count(BLOCK), 1)
        mutated = text.replace(BLOCK, "")
        self.assertNotEqual(mutated, text)
        self.assertNotIn("calm_disk", mutated)
        Mutant = type("Mutant", (Pinned, Measure, scratch_base(mutated)), {"test_check": lambda self: self.check()})
        result = unittest.TestResult()
        unittest.TestSuite([Mutant("test_check")]).run(result)
        self.assertEqual((len(result.failures), len(result.errors)), (1, 0), result.errors)
        self.assertIn(ANCHOR, result.failures[0][1])
        self.assertIn("1 disk.low event", result.failures[0][1])
        applied = mutated != text and BLOCK in text and BLOCK not in mutated
        self.assertTrue(applied, "the mutation was not applied")
        SEEN.update(mutant=len(result.failures), mutated=applied)

    def test_d_control_a_no_op_mutation_fails_the_applied_check(self):
        text = CORE.read_text()
        mutated = text.replace(BLOCK, BLOCK)
        applied = mutated != text and BLOCK in text and BLOCK not in mutated
        with self.assertRaises(AssertionError) as caught:
            self.assertTrue(applied, "the mutation was not applied")
        self.assertIn("the mutation was not applied", str(caught.exception))

    def test_c_a_test_that_patches_its_own_7_gb_reading_finds_one_disk_low(self):
        with mock.patch.object(sb.shutil, "disk_usage", reading(7 * GB)):
            free, events, keys = self.measure()
        self.assertEqual((free, events), (7, 1))
        SEEN.update(own=events)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if len(SEEN) == 6:
            print("S2: calm reading %d GB, disk.low events %d, keys %d; control 1 of 1, mutant %d of 1 with the text (1 disk.low event), own 7 GB reading finds %d; mutation applied %s"
                  % (SEEN["free"], SEEN["events"], SEEN["keys"], SEEN["mutant"], SEEN["own"], SEEN["mutated"]))


if __name__ == "__main__":
    unittest.main()
