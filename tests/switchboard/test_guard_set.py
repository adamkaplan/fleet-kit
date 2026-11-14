"""The guard set (#2055): docs/guard-set.json lists the guard classes, tools/guard-set prints them.

Run with `python3 bin/test-switchboard TestGuardSet`. Standard library only.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "guard-set"
LIST = REPO / "docs" / "guard-set.json"
NAMED = ["TestHygiene", "TestSupportRebinding", "TestArchitectureDocument", "TestTheBinSetClassesAreAllInTheTable",
         "TestV2LightGuards", "TestImpactHold", "TestEveryTestFileIsWired"]


_MOD = []


def _tool():   # tools/guard-set, loaded once
    if not _MOD:
        loader = importlib.machinery.SourceFileLoader("guard_set_under_test", str(TOOL))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        _MOD.append(module)
    return _MOD[0]


def run(*args):
    if "--run" not in args:   # the listing and the check run in this process; --run starts the classes it lists, so it keeps a child
        out, err, code = io.StringIO(), io.StringIO(), 0
        before = os.getcwd()
        os.chdir(REPO)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = _tool().main(list(args))
                except SystemExit as stop:
                    code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
        finally:
            os.chdir(before)
        return subprocess.CompletedProcess([sys.executable, str(TOOL), *args], code or 0, out.getvalue(), err.getvalue())
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
        # in-process: the real test_wired_tests module runs the guard's test with its paths pointed at a tree of links to the real files (so the parse caches hold); no child run per step
        import test_wired_tests as wired
        with tempfile.TemporaryDirectory() as d:
            tree = Path(d).resolve()
            (tree / "bin").mkdir()   # only what the guard reads: the runner and the test directory
            (tree / "bin" / "test-switchboard").symlink_to(REPO / "bin" / "test-switchboard")
            (tree / "tests" / "switchboard").mkdir(parents=True)
            for path in (REPO / "tests" / "switchboard").glob("*.py"):
                (tree / "tests" / "switchboard" / path.name).symlink_to(path)

            def guard_run():
                result = unittest.TestResult()
                orig_problems = wired.problems   # the runner's text is read again by the guard: hand it the cached reduced text (the loader lines) instead of a full-file walk
                with mock.patch.object(wired, "TESTS", tree / "tests" / "switchboard"), mock.patch.object(wired, "RUNNER", tree / "bin" / "test-switchboard"), \
                        mock.patch.object(wired, "problems", side_effect=lambda td, src, ex: orig_problems(td, wired.runner_loader_source() if len(src) > 100000 else src, ex)):
                    wired.TestEveryTestFileIsWired("test_the_real_directory_is_wired").run(result)
                return result, "".join(t for _, t in result.failures + result.errors)
            c, text = guard_run()
            self.assertTrue(c.wasSuccessful(), text)  # the control passes on the unmutated tree
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
        def go(source):   # in-process: the tool's own text (the control) or the mutated text, loaded as a module under the real path (so REPO is the real one) and run
            loader = importlib.machinery.SourceFileLoader("guard_set_scan_mutant", str(TOOL))
            loader.get_data = lambda path: source.encode()
            module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
            loader.exec_module(module)
            out, err, code, before = io.StringIO(), io.StringIO(), 0, os.getcwd()
            os.chdir(REPO)
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        code = module.main(["--check"])
                    except SystemExit as stop:
                        code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
            finally:
                os.chdir(before)
            return subprocess.CompletedProcess(["guard-set", "--check"], code or 0, out.getvalue(), err.getvalue())
        control = go(text)
        self.assertEqual(control.returncode, 0, control.stdout + control.stderr)  # the control passes first
        r = go(text.replace(needle, "", 1))
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
