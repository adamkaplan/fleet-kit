"""Ask #2072: the launcher's `status NAME --json` prints a LIST with one record per pod; run_launcher returns it for `status` only, status_lines reads
its first record and cloud_turn_activity/scan_turns see the turn end. Stub launchers print the REAL shape (a program found by FLEET_CLOUD_WORKER_BIN)."""
import importlib.util
import io
import json
import os
import shutil
import stat
import tempfile
import types
import unittest
import warnings
from unittest import mock

from support.loader import sb
from support.paths import BIN

RECORD = {"worker": "w1", "runtime_id": "pod/w1", "phase": "Running", "idle_since": None, "deadlines": {}, "cost_estimate_usd": 1.25}
CLUSTER = BIN / "cloud_cluster.py"
OLD = 'return child.returncode, body if isinstance(body, dict) else None, D["_one"]'
NEW_PREFIX = "return child.returncode, body if isinstance(body, dict) or (command"


def mutate_text(text, old, new):
    """The text with `old` (asserted to occur once) replaced by `new`; the mutation is applied only when the two texts differ AND the result differs."""
    assert text.count(old) == 1, "the text to mutate does not occur once"
    out = text.replace(old, new)
    applied = out != text
    assert applied, "the mutation was not applied"
    return out


class Launcher(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="status-shape-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cw = sb.load_module("cloud_worker").bind(vars(sb))

    def stub(self, payload, code=0):
        path = os.path.join(self.dir, "launcher")
        with open(path, "w") as f:
            f.write("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\nexit %d\n" % (json.dumps(payload), code))
        os.chmod(path, 0o755)
        return mock.patch.dict(os.environ, {self.cw.LAUNCHER_ENV: path})

    def call(self, command, payload, runner=None):
        with self.stub(payload):
            return self.cw.run_launcher(None, command, "w1", (), 30)[:2]


class TestRunLauncherShapes(Launcher):
    def test_status_list_of_dicts_is_returned(self):
        self.assertEqual(self.call("status", [RECORD]), (0, [RECORD]))

    def test_status_other_shapes_stay_none(self):
        for payload in ([], [1], [RECORD, 2], "x", 3):
            self.assertEqual(self.call("status", payload), (0, None), payload)

    def test_status_dict_as_before(self):
        self.assertEqual(self.call("status", RECORD), (0, RECORD))

    def test_each_dict_caller_gets_none_for_a_list(self):
        for command in ("create", "snapshot", "stop", "hub-session"):
            self.assertEqual(self.call(command, [RECORD]), (0, None), command)

    def test_create_snapshot_stop_hubsession_callers_unchanged(self):
        # the callers read `body` of create and snapshot; a list there is None, as a non-dict was before
        for command in ("create", "snapshot"):
            self.assertIsNone(self.call(command, [RECORD])[1], command)
        for command in ("stop", "hub-session"):
            self.assertEqual(self.call(command, [RECORD])[0], 0, command)


class FakeCluster:
    def __init__(self, timeout=None):
        pass

    def __enter__(self):
        return self

    def close(self):
        pass

    def env(self):
        return None

    @staticmethod
    def clean(text, limit=300):
        return text


class CtxCluster(FakeCluster):
    def __exit__(self, *exc):
        return False


class TestDictCallersUnchanged(Launcher):
    """The dict-only rule holds PER CALLER: only `status` may return a list. For each caller that expects a dict or ignores the body, a stub answers a
    LIST for the caller's own command and then None (the old non-dict case): run_launcher still returns None for the list, and the caller's observable
    result (return value, printed lines, exit code, audit lines) is IDENTICAL in the two runs. A caller that cannot run without a cluster, a vault and
    a hub (create, hub-session) is tested by its closest function: the body check and the exit code that caller reads."""

    def both(self, command, run):
        """The caller's observable result with a list answer and with None; run_launcher's body for the list is None."""
        with self.stub([RECORD]):
            self.assertIsNone(self.cw.run_launcher(None, command, "w1", (), 30)[1], command)
        results = []
        for payload in ([RECORD], None):
            with self.stub(payload):
                results.append(run())
        return results

    def observe(self, call):
        out, err = io.StringIO(), io.StringIO()
        try:
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = ("returned", call())
        except Exception as exc:
            result = ("raised", type(exc).__name__, str(exc))
        return result, out.getvalue(), err.getvalue()

    mutant_text = None

    def setUp(self):
        super().setUp()
        if self.mutant_text:
            module = types.ModuleType("cloud_cluster_mutant")
            module.D = self.cw._Stayed()
            exec(compile(self.mutant_text, "mutant", "exec"), module.__dict__)
            patch = mock.patch.object(self.cw, "run_launcher", module.run_launcher)
            patch.start()
            self.addCleanup(patch.stop)

    def callers_suite(self, text):
        suite = unittest.TestSuite()
        for name in ("test_create_caller_unchanged", "test_stop_caller_unchanged", "test_retire_stop_caller_unchanged", "test_snapshot_caller_unchanged"):
            case = TestDictCallersUnchanged(name)
            case.mutant_text = text
            suite.addTest(case)
        return suite

    def test_mutant_every_command_may_return_a_list_fails_a_caller_test(self):
        old, new = '(command == "status" and isinstance(body, list)', '(isinstance(body, list)'
        full = CLUSTER.read_text()
        control = unittest.TextTestRunner(stream=io.StringIO()).run(self.callers_suite(full))
        self.assertTrue(control.wasSuccessful(), "control: the unmutated code passes the caller tests")
        text = mutate_text(full, old, new)
        self.assertNotEqual(text, full, "the mutation was not applied")
        run = unittest.TextTestRunner(stream=io.StringIO()).run(self.callers_suite(text))
        failed = run.failures + run.errors
        self.assertGreaterEqual(len(failed), 1, "no caller test failed for the mutant")
        self.assertIn("is not None : stop", "".join(m for _, m in failed))

    def test_a_no_op_caller_mutation_fails_the_computation(self):
        with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
            mutate_text(CLUSTER.read_text(), "isinstance(body, list)", "isinstance(body, list)")


    def test_create_caller_unchanged(self):
        def run():
            code, body, _ = self.cw.run_launcher(None, "create", "w1", (), 30)
            return self.observe(lambda: (code, self.cw._record_from_create("w1", body)))
        listed, none = self.both("create", run)
        self.assertEqual(listed, none, "the dict-only rule: a list answer changed the caller")
        self.assertEqual(listed[0][:2], ("raised", "CloudError"))

    def test_hub_session_caller_unchanged(self):
        def run():
            code, _, note = self.cw.run_launcher(None, "hub-session", "w1", ["--session", "s"], 30)
            return self.observe(lambda: (code, note))
        listed, none = self.both("hub-session", run)
        self.assertEqual(listed, none, "the dict-only rule: a list answer changed the caller")
        self.assertEqual(listed[0], ("returned", (0, "")))

    def test_stop_caller_unchanged(self):
        audits = []
        def run():
            del audits[:]
            with mock.patch.object(self.cw, "_audit", lambda *a: audits.append(a[1:])):
                result = self.observe(lambda: self.cw._stop_after_failure(None, None, "w1", "run12345", "why", CtxCluster()))
            return result, list(audits)
        listed, none = self.both("stop", run)
        self.assertEqual(listed, none, "the dict-only rule: a list answer changed the caller")
        self.assertEqual(listed[0][0], ("returned", True))

    def test_retire_stop_caller_unchanged(self):
        audits = []
        cw = self.cw
        def run():
            del audits[:]
            args = types.SimpleNamespace(name="w1", undo=False, reason="done")
            H = dict(cw.H, build_engine=lambda *a: (None, self.dir, None), gates_caller_kind=lambda *a: ("operator",), PROG="prog", EXIT_ERROR=1, EXIT_OK=0)
            with mock.patch.dict(cw.H, H), mock.patch.object(cw, "Cluster", CtxCluster), mock.patch.object(cw, "_may_retire", lambda *a: True), \
                    mock.patch.object(cw, "_audit", lambda *a: audits.append(a[1:])), mock.patch.object(cw, "_registry_note", lambda *a: None), \
                    mock.patch.object(cw, "_keep_boss", lambda *a: None), mock.patch.object(cw, "remove_record", lambda *a: None):
                result = self.observe(lambda: cw.cmd_retire(args, None, None, dict(RECORD)))
            return result, list(audits)
        listed, none = self.both("stop", run)
        self.assertEqual(listed, none, "the dict-only rule: a list answer changed the caller")
        self.assertEqual(listed[0][0], ("returned", 0))

    def test_snapshot_caller_unchanged(self):
        def run():
            return self.observe(lambda: self.cw._cluster_snapshot(None, CtxCluster(), 30))
        listed, none = self.both("snapshot", run)
        self.assertEqual(listed, none, "the dict-only rule: a list answer changed the caller")
        self.assertEqual(listed[0][:2], ("raised", "CloudError"))


class TestStatusLines(Launcher):
    def lines(self, payload):
        cw = self.cw
        patches = [mock.patch.object(cw, "Cluster", FakeCluster), mock.patch.object(cw, "list_records", lambda state: [dict(RECORD, hard_deadline="x")]),
                   mock.patch.object(cw, "_cluster_snapshot", lambda *a: {}), mock.patch.object(cw, "_summary_line", lambda *a: "summary"),
                   mock.patch.object(cw, "_ended", lambda record: False), mock.patch.object(cw, "_answers", lambda *a: False)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with self.stub(payload):
            return "\n".join(cw.status_lines(None, self.dir, 30)[0])

    def test_list_shape_shows_phase_and_cost(self):
        text = self.lines([RECORD])
        self.assertIn("phase Running, about $1.25 so far", text)
        self.assertNotIn("unavailable", text)

    def test_dict_shape_shows_phase_and_cost(self):
        self.assertIn("phase Running, about $1.25 so far", self.lines(RECORD))

    def test_other_shape_is_unavailable(self):
        for payload in ([], [3], "x"):
            self.assertIn("launcher status unavailable", self.lines(payload), payload)


class TestTurnEnd(Launcher):
    def activity(self, payload):
        with self.stub(payload):
            return sb.cloud_turn_activity("w1")

    def test_activity_from_the_real_list_shape(self):
        self.assertEqual(self.activity([RECORD]), (True, 0))
        done = self.activity([dict(RECORD, idle_since="2026-11-03T10:00:00Z")])
        self.assertEqual(done, (False, 1793700000.0))

    def scan(self, payload_for, cw_patch=None):
        """busy: 0 notices; turn ended: 1; the next scan: 0 more."""
        sent, memory, state, audit = [], {}, os.path.join(self.dir, "s.json"), os.path.join(self.dir, "a.jsonl")
        mod = sb.load_module("cloud_end_alert")
        parse = sb.load_module("record_json").parse_bounded
        counts = []
        for now, payload in ((1000.0, [RECORD]), (1100.0, [dict(RECORD, idle_since="2026-11-03T10:00:00Z")]), (1200.0, [dict(RECORD, idle_since="2026-11-03T10:00:00Z")]),
                             (1300.0, [dict(RECORD, idle_since="2026-11-03T10:00:00Z")])):
            with self.stub(payload):
                mod.scan_turns([("w1", "coder-lead")], sb.cloud_turn_activity, state, audit, lambda to, text: sent.append((to, text)), parse, now, memory)
            counts.append(len(sent))
        return counts, sent

    def test_scan_turns_tells_the_boss_once(self):
        counts, sent = self.scan(None)
        self.assertEqual(counts[0], 0, "busy: no notice")
        self.assertEqual(counts[-1], 1, "a turn end is one notice")
        self.assertEqual(counts[-1], counts[-2], "the next scan tells no more")
        self.assertEqual({to for to, _ in sent}, {"coder-lead"})

    def mutant_run_launcher(self, old, new):
        text = mutate_text(CLUSTER.read_text(), old, new)
        module = types.ModuleType("cloud_cluster_mutant")
        module.D = self.cw._Stayed()
        exec(compile(text, "mutant", "exec"), module.__dict__)
        return module.run_launcher

    def line(self):
        full = CLUSTER.read_text()
        start = full.index(NEW_PREFIX)
        return full[start:full.index("\n", start)]

    def test_mutant_restores_the_dict_only_return(self):
        new = self.line()
        dict_only = OLD + new.split('D["_one"]', 1)[1]
        # the control first: the text as it is (one occurrence of the new line, replaced by itself plus a comment that changes the text) passes
        with mock.patch.object(self.cw, "run_launcher", self.mutant_run_launcher(new, new + "  # control")):
            control = self.scan(None)[0]
        self.assertEqual(control[-1], 1, "control: the unmutated code tells the boss once")
        with mock.patch.object(self.cw, "run_launcher", self.mutant_run_launcher(new, dict_only)):
            got = self.scan(None)[0]
        self.assertEqual(got[-1], 0, "the dict-only return: the turn end is never seen (0 notices)")

    def test_a_no_op_mutation_fails_the_computation(self):
        with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
            mutate_text(CLUSTER.read_text(), self.line(), self.line())


if __name__ == "__main__":
    unittest.main()
