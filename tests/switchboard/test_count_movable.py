"""tools/arch-count-movable counts the classes that can move whole, by the fixpoint of the dry run (S13a of #1092).

A synthetic entry of 3 classes: `Free` moves; `Needs` uses `Stuck`, which uses `glue`, a name that stays in the entry. The first round blocks `Stuck`, the second
`Needs` (it uses a class that is now outside the plan), the third is clean. A mutant of the tool that stops after its first round counts one class too many.
Standard library only."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "arch-count-movable"
ENTRY = '''#!/usr/bin/env python3
import unittest

glue = {"stays": True}


class Free(unittest.TestCase):
    def test_a(self):
        self.assertTrue(True)


class Stuck(unittest.TestCase):
    def test_a(self):
        self.assertTrue(glue)


class Needs(unittest.TestCase):
    def test_a(self):
        self.assertTrue(Stuck)
'''


class TestCountMovable(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "test-switchboard").write_text(ENTRY)
        (self.tmp / "tools").mkdir()
        shutil.copy(REPO / "tools" / "arch-test-move", self.tmp / "tools" / "arch-test-move")
        (self.tmp / "tests" / "switchboard" / "support").mkdir(parents=True)
        (self.tmp / "tests" / "switchboard" / "support" / "__init__.py").write_text("")

    def run_tool(self, tool):
        done = subprocess.run([sys.executable, str(tool), str(self.tmp)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_the_tool_prints_the_fixpoint_and_the_one_round_mutant_does_not(self):
        self.assertEqual(self.run_tool(TOOL), "classes 3 movable (fixpoint) 1 blocked 2 rounds 3")   # the control
        source = TOOL.read_text()
        self.assertIn("        if not blocked:\n", source)
        mutant = self.tmp / "mutant-count"
        mutant.write_text(source.replace("        if not blocked:\n", "        if True:\n", 1))
        text = self.run_tool(mutant)
        self.assertNotEqual(text, "classes 3 movable (fixpoint) 1 blocked 2 rounds 3")
        self.assertIn("movable (fixpoint) 2 ", text)   # one class too many

    def test_it_writes_nothing_into_the_tree(self):
        before = sorted(str(p) for p in self.tmp.rglob("*"))
        self.run_tool(TOOL)
        self.assertEqual(sorted(str(p) for p in self.tmp.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
