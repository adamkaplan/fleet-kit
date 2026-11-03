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
from unittest import mock

from support.loader import sb
from support.paths import BIN

RECORD = {"worker": "w1", "runtime_id": "pod/w1", "phase": "Running", "idle_since": None, "deadlines": {}, "cost_estimate_usd": 1.25}
CLUSTER = BIN / "cloud_cluster.py"
OLD = 'return child.returncode, body if isinstance(body, dict) else None, D["_one"]'
NEW_PREFIX = "return child.returncode, body if isinstance(body, dict) or (command"


def mutate(text, old, new):
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
        text = mutate(CLUSTER.read_text(), old, new)
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
            mutate(CLUSTER.read_text(), self.line(), self.line())


if __name__ == "__main__":
    unittest.main()
