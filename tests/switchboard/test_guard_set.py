"""The guard set (#2055): docs/guard-set.json lists the guard classes, tools/guard-set prints them.

Run with `python3 bin/test-switchboard TestGuardSet`. Standard library only.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "guard-set"
LIST = REPO / "docs" / "guard-set.json"
NAMED = ["TestHygiene", "TestSupportRebinding", "TestArchitectureDocument", "TestTheBinSetClassesAreAllInTheTable",
         "TestV2LightGuards", "TestImpactHold", "TestEveryTestFileIsWired"]


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=REPO)


class TestGuardSet(unittest.TestCase):
    def test_every_class_of_the_guard_set_exists(self):
        r = run("--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_mutant_adding_a_missing_class_is_caught_control_first(self):
        text = LIST.read_text()
        anchor = '"classes": ['
        self.assertIn(anchor, text)
        with tempfile.TemporaryDirectory() as d:
            control = Path(d) / "control.json"
            control.write_text(text)
            self.assertEqual(run("--check", "--list", str(control)).returncode, 0)  # the control passes first
            mutant = Path(d) / "mutant.json"
            mutant.write_text(text.replace(anchor, anchor + '{"class": "TestNoSuchGuardClassXyz"},', 1))
            r = run("--check", "--list", str(mutant))
            self.assertEqual(r.returncode, 1)
            self.assertIn("every class of the guard set exists", r.stdout)
            self.assertEqual(r.stdout.count("TestNoSuchGuardClassXyz"), 1)

    def test_one_printed_line_per_entry(self):
        data = json.loads(LIST.read_text())
        out = run().stdout.splitlines()
        self.assertEqual(len(out), len(data["classes"]) + len(data["extras"]))
        for c in NAMED:
            self.assertEqual(sum(1 for l in out if l.endswith("bin/test-switchboard " + c)), 1, c)
        for l, e in zip(out, data["classes"]):
            self.assertTrue(l.split("python3 bin/test-switchboard ")[1] == e["class"])

    def test_documents_carry_the_sentence(self):
        for f in ("docs/plan-template.md", "skills/fleet-coordination/SKILL.md"):
            self.assertIn("run the guard set", (REPO / f).read_text(), f)
        t = (REPO / "docs/plan-template.md").read_text()
        for w in ("copies `bin/`", "light switch", "new module", "matrix path"):
            self.assertIn(w, t)


if __name__ == "__main__":
    unittest.main()
