"""#1474: a cloud launch says its idle and hard limits, and the create's idle default is 30 minutes.

S2/S4: the stderr note of `launch --cloud` (stdout stays one JSON line). S6: the create's default, the explicit value and the reaper's
fallback for a pod made before the change. S5: the interface document. A mutant is a copy of the code in memory, never a file."""
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
import test_cloud_worker as lbase  # noqa: E402  (the launcher's fake-cluster base)
cw = lbase.cw


def _load_bin_tests():
    loader = importlib.machinery.SourceFileLoader("idle_note_bin_tests", os.path.join(ROOT, "bin", "test-cloud-worker"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


bint = _load_bin_tests()   # the switchboard side's fake launcher and its Base
NOTE = re.compile(r"^launch --cloud: idle limit (\S+?)( \(default\))?, hard limit (\S+?)( \(default\))?, stops at (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)$")


def note_problem(err):
    """The reason the stderr text is not the note (None when it is right)."""
    lines = [l for l in err.splitlines() if l.startswith("launch --cloud:")]
    if len(lines) != 1:
        return "expected one note line, found %d" % len(lines)
    if not NOTE.match(lines[0]):
        return "the note has the wrong shape: %r" % lines[0]
    return None


class SwitchboardSide(bint.Base):
    def run_launch(self, *extra):
        code, out, err = self.launch_with("--cloud", *extra)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, out)   # stdout is exactly one line
        self.assertEqual(json.loads(lines[0])["cloud"], True)
        return err

    def note(self, err):
        return [l for l in err.splitlines() if l.startswith("launch --cloud:")][0]

    def test_a_launch_without_idle_says_the_default_limits_and_a_stop_time(self):
        err = self.run_launch()
        self.assertIsNone(note_problem(err))
        self.assertEqual(self.note(err), "launch --cloud: idle limit 30m (default), hard limit 2h (default), stops at 2099-10-14T12:10:00Z")

    def test_a_launch_with_idle_says_that_value_and_no_default_beside_it(self):
        err = self.run_launch("--idle", "60m")
        self.assertIsNone(note_problem(err))
        self.assertEqual(self.note(err), "launch --cloud: idle limit 60m, hard limit 2h (default), stops at 2099-10-14T12:10:00Z")

    def test_a_launch_with_both_flags_says_no_default_and_the_note_comes_before_the_json(self):
        err = self.run_launch("--idle", "45m", "--hard", "3h")
        self.assertEqual(self.note(err), "launch --cloud: idle limit 45m, hard limit 3h, stops at 2099-10-14T12:10:00Z")

    def test_the_note_holds_no_address_and_no_brief(self):
        err = self.run_launch()
        text = self.note(err)
        for word in (self.remote.url, "127.0.0.1", "http", "Build the thing", bint.PASSWORD):
            self.assertNotIn(word, text)

    def test_a_build_that_prints_no_note_fails_the_checks(self):
        module = sb_module(self)
        calls = []
        with mock.patch.object(module, "_limits_note", lambda args, record: calls.append(1) or ""):
            err = self.run_launch()
        self.assertEqual(calls, [1])   # the mutation was applied
        self.assertIsNotNone(note_problem(err))

    def test_the_stop_time_is_the_earlier_of_the_two_deadlines(self):
        module = sb_module(self)
        args = types.SimpleNamespace(idle=None, hard=None)
        text = module._limits_note(args, {"idle_deadline": "2026-10-14T14:00:00Z", "hard_deadline": "2026-10-14T12:30:00Z"})
        self.assertTrue(text.endswith("stops at 2026-10-14T12:30:00Z"), text)
        text = module._limits_note(args, {"idle_deadline": None, "hard_deadline": "garbage"})
        self.assertTrue(text.endswith("stops at not reported"), text)


def sb_module(case):
    return bint.sb.load_module("cloud_worker")


class CreateDefault(lbase.Base):
    def create(self, worker="coder7", *extra):
        """A create that adds no idle flag of its own (the test base's helper pins 10m), so the default is what is under test."""
        code, out, err = self.run_cli("create", worker, "--repo", "acme-org/widget-service", "--branch", "main", *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_a_create_without_idle_gives_1800_seconds_and_a_deadline_30_minutes_out(self):
        out = self.create("coder7")
        self.assertEqual(self.pod()["metadata"]["annotations"]["cw/idle-seconds"], "1800")
        self.assertEqual(out["idle_deadline"], cw.iso(lbase.T0 + lbase.dt.timedelta(minutes=30)))

    def test_a_create_with_idle_10m_gives_600_seconds(self):
        out = self.create("coder7", "--idle", "10m")
        self.assertEqual(self.pod()["metadata"]["annotations"]["cw/idle-seconds"], "600")
        self.assertEqual(out["idle_deadline"], cw.iso(lbase.T0 + lbase.dt.timedelta(minutes=10)))

    def test_a_pod_with_no_idle_annotation_is_judged_at_600_seconds(self):
        self.create("coder7")
        state = self.st()
        del state["pods"][cw.pod_name("coder7")]["metadata"]["annotations"]["cw/idle-seconds"]
        self.write_state(state)
        self.set_activity("coder7", False, lbase.T0)
        code, out, _ = self.run_cli("status")
        self.assertEqual(json.loads(out)[0]["deadlines"]["idle"], "2026-10-14T12:10:00Z")

    def test_a_build_that_keeps_10m_fails_the_default_check(self):
        path = os.path.join(ROOT, "cloud", "launcher", "cloud_worker.py")
        with open(path) as f:
            source = f.read()
        good = 'c.add_argument("--idle", default="30m")'
        self.assertEqual(source.count(good), 1)
        mutant = types.ModuleType("mutant_cloud_worker")
        mutant.__file__ = path
        exec(compile(source.replace(good, 'c.add_argument("--idle", default="10m")'), path, "exec"), mutant.__dict__)
        self.assertIn('default="10m"', source.replace(good, 'c.add_argument("--idle", default="10m")'))   # the mutation was applied
        with redirect_stdout(io.StringIO()):
            code = mutant.main(["create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main"], self.env, self.clock,
                               lambda s: self.clock.advance(seconds=s))
        self.assertEqual(code, 0)
        self.assertNotEqual(self.pod()["metadata"]["annotations"]["cw/idle-seconds"], "1800")   # the 30m check would fail on it


class InterfaceDocument(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "docs", "cloud-worker-interface.md")) as f:
            self.text = f.read()

    def test_the_document_says_30m_default_and_where_it_is_read_from(self):
        self.assertIn("`--idle 30m` (default)", self.text)
        self.assertNotIn("`--idle 10m` (default)", self.text)
        self.assertIn("read from the `--idle` default of the launcher's `create`", self.text)


if __name__ == "__main__":
    unittest.main()
