"""The guard set (#2055): docs/guard-set.json lists the guard classes, tools/guard-set prints them.

Run with `python3 bin/test-switchboard TestGuardSet`. Standard library only.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
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
        # in-process: the copy's own test_wired_tests module (loaded from the tree copy) runs the guard's test; no child run per step
        import importlib.util
        with tempfile.TemporaryDirectory() as d:
            tree = (Path(d) / "tree").resolve()   # macOS: /var is /private/var, and the module resolves its own __file__
            (tree / "bin").mkdir(parents=True)   # only what the copy's guard reads: the runner and the test directory
            shutil.copy(REPO / "bin" / "test-switchboard", tree / "bin" / "test-switchboard")
            shutil.copytree(REPO / "tests" / "switchboard", tree / "tests" / "switchboard", ignore=shutil.ignore_patterns("__pycache__"))
            spec = importlib.util.spec_from_file_location("guard_tree_wired", tree / "tests" / "switchboard" / "test_wired_tests.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertEqual(mod.TESTS, tree / "tests" / "switchboard")      # the copy's own paths, not the repository's
            self.assertEqual(mod.RUNNER, tree / "bin" / "test-switchboard")

            def guard_run():
                result = unittest.TestResult()
                mod.TestEveryTestFileIsWired("test_the_real_directory_is_wired").run(result)
                return result, "".join(t for _, t in result.failures + result.errors)
            c, text = guard_run()
            self.assertTrue(c.wasSuccessful(), text)  # the control passes on the unmutated copy
            self.assertGreaterEqual(c.testsRun, 1)     # the named guard test ran (not a skipped-everything pass)
            self.assertEqual(c.skipped, [], "the guard test ran, it was not skipped")   # a skipped guard is a pass that checked nothing
            # since #2210 a Test* class is bound by the runner's self-registration; a class with no loader line AND not named Test* is the one nothing reaches
            (tree / "tests" / "switchboard" / "test_planted_unloaded.py").write_text(
                "import unittest\n\nclass PlantedNoLoader(unittest.TestCase):\n    def test_a(self):\n        pass\n")
            r, out = guard_run()
            self.assertFalse(r.wasSuccessful(), out)
            self.assertIn("test_the_real_directory_is_wired", [t.id().rsplit(".", 1)[-1] for t, _ in r.failures + r.errors])  # the in-process form of the removed assertIn of the test name
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


class TestGuardSetRun(unittest.TestCase):
    """tools/guard-set --run (#2266): fixture classes only; no count of real classes is pinned."""

    def _list(self, d, entries):
        path = Path(d) / "list.json"
        path.write_text(json.dumps({"classes": entries, "extras": []}))
        return path

    def _fx(self, d, name, body):
        script = Path(d) / (name + ".py")
        script.write_text("import sys, time, os\nlog = %r\n" % str(Path(d) / "log.txt")
                          + "open(log, 'a').write('start %s %%f\\n' %% time.time())\n" % name + body
                          + "open(log, 'a').write('end %s %%f\\n' %% time.time())\n" % name)
        return {"class": name, "cmd": "%s %s" % (sys.executable, script)}

    def test_runs_in_order_one_at_a_time_and_names_the_failed_class(self):
        ok = "time.sleep(0.2)\nprint('Ran 3 tests in 0.2s')\n"
        bad = "time.sleep(0.2)\nprint('Ran 2 tests in 0.2s')\nprint('FAILED (failures=1)')\nopen(log, 'a').write('end FxBad %f\\n' % time.time())\nsys.exit(1)\n"
        with tempfile.TemporaryDirectory() as d:
            es = [self._fx(d, "FxA", ok), self._fx(d, "FxBad", bad), self._fx(d, "FxC", ok)]
            r = run("--run", "--list", str(self._list(d, es)))
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertRegex(r.stdout, r"FxA: tests run 3, failed 0, [0-9.]+ s")
            self.assertRegex(r.stdout, r"FxBad: tests run 2, failed 1, [0-9.]+ s")
            self.assertIn("RESULT: FAIL, failed: FxBad", r.stdout)
            self.assertNotIn("FxA (", r.stdout.split("RESULT:")[1])
            events = [l.split() for l in (Path(d) / "log.txt").read_text().splitlines()]
            starts = [e[1] for e in events if e[0] == "start"]
            self.assertEqual(starts, ["FxA", "FxBad", "FxC"])  # in order, the failing one did not stop the next
            alive = 0
            overlaps = 0
            for e in events:   # the log is appended in time order
                alive += 1 if e[0] == "start" else 0
                if e[0] == "start" and alive > 1:
                    overlaps += 1
                if e[0] == "end":
                    alive -= 1
            self.assertEqual(overlaps, 0)

    def test_all_pass_prints_result_pass(self):
        with tempfile.TemporaryDirectory() as d:
            r = run("--run", "--list", str(self._list(d, [self._fx(d, "FxOk", "print('Ran 1 test in 0s')\n")])))
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("RESULT: PASS", r.stdout)

    def test_a_class_past_its_timeout_is_stopped_with_the_reason_and_the_next_runs(self):
        with tempfile.TemporaryDirectory() as d:
            es = [self._fx(d, "FxSlow", "time.sleep(30)\n"), self._fx(d, "FxNext", "print('Ran 1 test in 0s')\n")]
            es[0]["timeout"] = 1
            t0 = time.time()
            r = run("--run", "--list", str(self._list(d, es)))
            self.assertLess(time.time() - t0, 20)
            self.assertEqual(r.returncode, 1)
            self.assertIn("TIMEOUT after 1s (killed)", r.stdout)
            self.assertIn("RESULT: FAIL, failed: FxSlow (timed out)", r.stdout)
            self.assertIn("FxNext: tests run 1, failed 0", r.stdout)   # the class after the timed-out one ran

    def test_documents_say_run_the_tool_and_no_longer_one_by_one(self):
        for f in ("docs/plan-template.md", "skills/fleet-coordination/SKILL.md", "skills/fleet-planning/SKILL.md"):
            t = (REPO / f).read_text()
            self.assertIn("tools/guard-set --run", t, f)
            self.assertNotIn("one class at a time before", t, f)
            self.assertNotIn("runs them one class at a time", t, f)


if __name__ == "__main__":
    unittest.main()
