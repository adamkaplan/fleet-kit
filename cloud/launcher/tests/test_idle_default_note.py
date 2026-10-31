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
_FAKE_OLD = '"hard_deadline": "2099-10-14T14:00:00Z"}'
assert bint.STUB.count(_FAKE_OLD) == 1   # the fake launcher's answer, given the two keys of the real one (bin/test-cloud-worker is not edited)
bint.STUB = bint.STUB.replace(_FAKE_OLD, '"hard_deadline": "2099-10-14T14:00:00Z", "idle_seconds": 1800, "hard_seconds": 7200}')
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


def fmt_seconds(n):
    """The expected text of a limit, written apart from the note's own code."""
    return "%dh" % (n // 3600) if n % 3600 == 0 else "%dm" % (n // 60) if n % 60 == 0 else "%ds" % n


def follow_problem(note, answer):
    """The note must print the limits of the launcher's answer (None when it does)."""
    m = NOTE.match(note)
    if not m or (m.group(1), m.group(3)) != (fmt_seconds(answer["idle_seconds"]), fmt_seconds(answer["hard_seconds"])):
        return "the note did not follow the launcher: %r" % note
    return None


def default_problem(case):
    """The create check of the default: the pod's idle-seconds annotation."""
    got = case.pod()["metadata"]["annotations"]["cw/idle-seconds"]
    return None if got == "1800" else "idle-seconds is not 1800: %s" % got


LAUNCHER_PATH = os.path.join(ROOT, "cloud", "launcher", "cloud_worker.py")
CREATE_ARGS = ["create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main"]


def mutated_launcher(replacements):
    """The launcher code in memory with each (old, new) text replaced; each old text must occur once. Returns (module, count applied)."""
    with open(LAUNCHER_PATH) as f:
        source = f.read()
    applied = 0
    for old, new in replacements:
        assert source.count(old) == 1, old
        source = source.replace(old, new)
        applied += 1
    module = types.ModuleType("mutant_cloud_worker")
    module.__file__ = LAUNCHER_PATH
    exec(compile(source, LAUNCHER_PATH, "exec"), module.__dict__)
    return module, applied


IDLE_DEFAULT = 'c.add_argument("--idle", default="30m")'
HARD_DEFAULT = 'c.add_argument("--hard", default="2h")'


class FollowsTheLauncher(lbase.Base):
    def answer_of(self, module):
        out = io.StringIO()
        with redirect_stdout(out):
            code = module.main(CREATE_ARGS, self.env, self.clock, lambda s: self.clock.advance(seconds=s))
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def note_of(self, answer, key_free=False):
        module = sb_module(self)
        record = {k: answer.get(k) for k in ("idle_deadline", "hard_deadline", "idle_seconds", "hard_seconds")}
        return module._limits_note(types.SimpleNamespace(idle=None, hard=None), record)

    def test_S7_the_note_follows_the_launcher_and_a_literal_default_does_not(self):
        real = self.answer_of(cw)
        self.assertEqual((real["idle_seconds"], real["hard_seconds"]), (1800, 7200))
        n1 = self.note_of(real)
        self.assertIn("idle limit 30m (default)", n1)
        self.assertIn("hard limit 2h (default)", n1)
        self.assertIsNone(follow_problem(n1, real))
        self.setUp()   # a fresh fake cluster for the changed launcher
        changed, applied = mutated_launcher([(IDLE_DEFAULT, 'c.add_argument("--idle", default="45m")'), (HARD_DEFAULT, 'c.add_argument("--hard", default="3h")')])
        ans = self.answer_of(changed)
        n2 = self.note_of(ans)
        followed = int("idle limit 45m (default)" in n2) + int("hard limit 3h (default)" in n2)
        # the literal mutant: a copy of the note function with the default texts put back
        with open(os.path.join(ROOT, "bin", "cloud_worker.py")) as f:
            src = f.read()
        start = src.index("def _limits_note(")
        body = src[start:src.index("\n\n\ndef _registry_note", start)]
        key = "applied(record.get(key))"
        self.assertEqual(body.count(key), 1)
        ns = dict(sb_module(self).__dict__)
        exec(body.replace(key, '("30m" if key == "idle_seconds" else "2h")'), ns)
        lit = ns["_limits_note"](types.SimpleNamespace(idle=None, hard=None), {k: ans.get(k) for k in ("idle_deadline", "hard_deadline", "idle_seconds", "hard_seconds")})
        problem = follow_problem(lit, ans)
        failed = int(problem is not None and "the note did not follow the launcher" in problem)
        control = int(follow_problem(n2, ans) is None)
        bare = {k: v for k, v in ans.items() if k not in ("idle_seconds", "hard_seconds")}
        n3 = self.note_of(bare)
        unread = int("idle limit not reported" in n3) + int("hard limit not reported" in n3)
        print("S7: the unchanged launcher read %s and %s, the changed launcher read %s and %s (%d of 2 followed), the literal mutant failed %d of 1 with the anchored text, control passed %d of 1, %d of 2 limits read not reported without the keys, mutations applied %s" % (
            fmt_seconds(real["idle_seconds"]), fmt_seconds(real["hard_seconds"]), fmt_seconds(ans["idle_seconds"]), fmt_seconds(ans["hard_seconds"]), followed, failed, control, unread, applied))
        self.assertEqual((followed, failed, control, unread, applied), (2, 1, 1, 2, 2))


class RealChecksCatchTheMutants(SwitchboardSide):
    def test_S4_the_print_nothing_mutant_fails_the_real_note_check(self):
        control = int(note_problem(self.run_launch()) is None)
        module = sb_module(self)
        with mock.patch.object(module, "_limits_note", lambda args, record: ""):
            code, out, err = self.launch_with("--cloud", name="coder8")
        self.assertEqual(code, 0, err)
        caught = note_problem(err)
        got = int(caught is not None and caught.startswith("expected one note line"))
        self.S4 = (got, control)
        self.assertEqual((got, control), (1, 1))
        RealChecksCatchTheMutants.print_line = (got, control)


class ZDefaultMutantFailsTheRealCheck(lbase.Base):   # sorts after RealChecksCatchTheMutants, whose result it prints
    def test_S6_the_10m_default_fails_the_real_default_check(self):
        code, out, err = self.run_cli(*CREATE_ARGS)
        self.assertEqual(code, 0, err)
        control = int(default_problem(self) is None)
        self.setUp()
        mutant, applied = mutated_launcher([(IDLE_DEFAULT, 'c.add_argument("--idle", default="10m")')])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mutant.main(CREATE_ARGS, self.env, self.clock, lambda s: self.clock.advance(seconds=s)), 0)
        problem = default_problem(self)
        caught = int(problem is not None and problem.startswith("idle-seconds is not 1800"))
        print("S4 mutants: print-nothing caught %d of 1 with expected one note line, control %d of 1; S6 mutant: 10m default caught %d of 1 with idle-seconds is not 1800, control %d of 1" % (
            RealChecksCatchTheMutants.print_line[0] if hasattr(RealChecksCatchTheMutants, "print_line") else 0,
            RealChecksCatchTheMutants.print_line[1] if hasattr(RealChecksCatchTheMutants, "print_line") else 0, caught, control))
        self.assertEqual((caught, control, applied), (1, 1, 1))


class LauncherDiff(unittest.TestCase):
    def test_S8_the_launcher_file_differs_from_integration_by_the_default_and_the_answer_line(self):
        import subprocess
        try:
            out = subprocess.run(["git", "-C", ROOT, "diff", "-U0", "origin/integration", "--", "cloud/launcher/cloud_worker.py"],
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as err:
            out = None
            why = str(err)
        if out is None or out.returncode != 0:
            why = why if out is None else (out.stderr.strip() or "git diff failed")
            print("S8: SKIPPED, not a git checkout with origin/integration: %s" % why)
            self.skipTest("no git checkout with origin/integration")
        lines = out.stdout.splitlines()
        removed = [l for l in lines if l.startswith("-") and not l.startswith("---")]
        added = [l for l in lines if l.startswith("+") and not l.startswith("+++")]
        fallback = [l for l in removed + added if '"idle-seconds", "600"' in l and "#" in l]
        fallback_changed = len([l for l in removed if '"idle-seconds", "600"' in l])
        print("S8: launcher file diff vs integration: %d removed lines, %d added lines, fallback lines changed %d" % (len(removed), len(added), fallback_changed))
        self.assertEqual((len(removed), len(added), fallback_changed, len(fallback)), (1, 2, 0, 0))


if __name__ == "__main__":
    unittest.main()
