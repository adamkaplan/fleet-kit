"""The counting tool of the test split (S13a of #1092): tools/arch-count-movable on a synthetic entry of 3 classes, and a mutant that stops after its first round."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(ROOT, "tools")
ENTRY = '''def stays():
    return 1


class A:
    def f(self):
        return stays()


class B:
    def f(self):
        return A()


class C:
    pass
'''


class TestCountMovable(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        os.makedirs(os.path.join(self.dir, "bin"))
        os.makedirs(os.path.join(self.dir, "tests", "switchboard", "support"))
        open(os.path.join(self.dir, "bin", "test-switchboard"), "w").write(ENTRY)

    def run_tool(self, tool):
        done = subprocess.run([sys.executable, tool, self.dir], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_the_tool_prints_the_fixpoint_of_the_synthetic_entry(self):
        self.assertEqual(self.run_tool(os.path.join(TOOLS, "arch-count-movable")), "classes 3 movable (fixpoint) 1 blocked 2 rounds 3")

    def test_a_tool_that_stops_after_its_first_round_counts_a_different_number(self):
        text = open(os.path.join(TOOLS, "arch-count-movable")).read()
        needle = "            left = [c for c in left if c not in blocked]\n"
        self.assertIn(needle, text)
        scratch = os.path.join(self.dir, "mutant")
        os.makedirs(scratch)
        shutil.copy(os.path.join(TOOLS, "arch-test-move"), scratch)
        mutant = os.path.join(scratch, "arch-count-movable")
        open(mutant, "w").write(text.replace(needle, needle + "            break\n"))
        printed = self.run_tool(mutant)
        self.assertEqual(printed, "classes 3 movable (fixpoint) 2 blocked 1 rounds 1")
        self.assertNotEqual(printed, "classes 3 movable (fixpoint) 1 blocked 2 rounds 3")


if __name__ == "__main__":
    unittest.main()
