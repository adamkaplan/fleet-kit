"""The guard set (#2055): docs/guard-set.json lists the guard classes, tools/guard-set prints them.

Run with `python3 bin/test-switchboard TestGuardSet`. Standard library only.
"""
import json
import shutil
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
            self.assertEqual(sum(1 for l in out if l.startswith("python3 bin/test-switchboard " + c) and l.split()[2] == c), 1, c)
        for l, e in zip(out, data["classes"]):
            self.assertTrue(l.startswith("python3 bin/test-switchboard " + e["class"]), l)
            want = "  # timeout %d" % e["timeout"] if e.get("timeout") else ""
            self.assertEqual(l, "python3 bin/test-switchboard " + e["class"] + want)

    def test_a_class_with_no_loader_line_is_caught_by_the_wired_guard_control_first(self):
        guard = "TestEveryTestFileIsWired"
        with tempfile.TemporaryDirectory() as d:
            tree = Path(d) / "tree"
            shutil.copytree(REPO, tree, ignore=shutil.ignore_patterns(".git", "__pycache__"))

            def guard_run():
                return subprocess.run([sys.executable, "bin/test-switchboard", guard], capture_output=True, text=True, cwd=tree)
            c = guard_run()
            self.assertEqual(c.returncode, 0, c.stdout + c.stderr)  # the control passes on the unmutated copy
            (tree / "tests" / "switchboard" / "test_planted_unloaded.py").write_text(
                "import unittest\n\nclass TestPlantedNoLoader(unittest.TestCase):\n    def test_a(self):\n        pass\n")
            r = guard_run()
            out = r.stdout + r.stderr
            self.assertNotEqual(r.returncode, 0, out)
            self.assertIn("test_the_real_directory_is_wired", out)
            self.assertIn("test_planted_unloaded.py", out)

    def test_documents_carry_the_sentence(self):
        for f in ("docs/plan-template.md", "skills/fleet-coordination/SKILL.md"):
            self.assertIn("run the guard set", (REPO / f).read_text(), f)
        t = (REPO / "docs/plan-template.md").read_text()
        for w in ("copies `bin/`", "light switch", "new module", "matrix path"):
            self.assertIn(w, t)


    def test_the_two_classes_of_the_whole_file_run_are_listed_and_printed(self):
        data = json.loads(LIST.read_text())
        out = run().stdout.splitlines()
        self.assertEqual(len(out), len(data["classes"]) + len(data["extras"]))
        for c in ("TestOwnMarkerNotHonoured", "TestTheEntryGuard"):
            self.assertEqual(sum(1 for l in out if l == "python3 bin/test-switchboard " + c), 1, c)

    def test_a_scan_that_drops_the_second_directory_is_caught_control_first(self):
        needle = ', REPO / "tests" / "test_installed_guard.py"'
        text = TOOL.read_text()
        self.assertIn(needle, text)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "tools").mkdir()
            for n in ("tests", "bin", "docs"):
                (root / n).symlink_to(REPO / n)
            copy = root / "tools" / "guard-set"

            def go():
                return subprocess.run([sys.executable, str(copy), "--check"], capture_output=True, text=True)
            copy.write_text(text)
            control = go()
            self.assertEqual(control.returncode, 0, control.stdout + control.stderr)  # the control passes first
            copy.write_text(text.replace(needle, "", 1))
            r = go()
            self.assertEqual(r.returncode, 1)
            self.assertIn("every class of the guard set exists", r.stdout)
            self.assertIn("TestTheEntryGuard", r.stdout)


if __name__ == "__main__":
    unittest.main()
