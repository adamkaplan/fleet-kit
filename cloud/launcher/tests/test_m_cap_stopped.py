#!/usr/bin/env python3
"""Offline tests: a shape-M worker whose pod the launcher already stopped is not counted against the lane's cap (ask #2259).

Cause: the count kept a record until its hard deadline, so workers the reaper stopped at their idle limit held slots for up to an hour.
Run directly: python3 cloud/launcher/tests/test_m_cap_stopped.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "cloud" / "launcher" / "tests"))
import test_m_cap as base  # noqa: E402  (loads the switchboard fixture)

tcw, stub_support = base.tcw, base.stub_support
ANCHOR = "a stopped worker still held a shape-M slot"


class Stopped(base.Cap):
    def setUp(self):
        super().setUp()
        self.audit = self.home / "state" / "cw-cloud" / "audit.jsonl"
        patch = mock.patch.dict(os.environ, {"CW_CLOUD_AUDIT": str(self.audit)})
        patch.start()
        self.addCleanup(patch.stop)

    def write_audit(self, *lines):
        self.audit.parent.mkdir(parents=True, exist_ok=True)
        self.audit.write_text("".join(l if isinstance(l, str) else json.dumps(l) + "\n" for l in lines))

    @staticmethod
    def line(name, action, ok=True):
        return {"action": action, "runtime_id": "rt-" + name, "ok": ok, "worker": name}

    def count(self):
        return tcw.sb.load_module("cloud_worker").bind(tcw.sb.__dict__).lane_m_workers(self.state, "lane-a")


class TestScenarios(Stopped):
    def test_s2_three_stopped_of_four_count_one_and_the_launch_goes(self):
        for n in range(1, 5):
            self.put("coder%d" % n)
        self.write_audit(*[self.line("coder%d" % n, "create") for n in range(1, 5)], *[self.line("coder%d" % n, "stop") for n in range(1, 4)])
        held = self.count()
        code, out, err = self.ask("coder5")
        print("S2: counted %d of 1, launch exit %d" % (len(held), code))
        self.assertEqual(held, ["coder4"], ANCHOR)
        self.assertEqual(code, 0, err)

    def test_s3_four_running_count_four_and_the_fifth_is_refused(self):
        for n in range(1, 5):
            self.put("coder%d" % n)
        self.write_audit(*[self.line("coder%d" % n, "create") for n in range(1, 5)])
        held = self.count()
        code, out, err = self.ask("coder5")
        print("S3: counted %d of 4, launch exit %d" % (len(held), code))
        self.assertEqual(len(held), 4)
        self.assertEqual(code, 2, err)

    def test_s4_an_unreadable_stop_record_counts(self):
        for n in range(1, 4):
            self.put("coder%d" % n)
        self.write_audit(self.line("coder1", "create"), self.line("coder1", "stop"), "{not json\n", self.line("coder2", "create"))
        stopped_counted = "coder1" in self.count()
        self.assertFalse(stopped_counted)   # the readable stop frees its worker; the corrupt line is skipped
        self.audit.write_bytes(b"\xff\xfe{")   # corrupt: nothing readable
        corrupt = self.count()
        self.audit.unlink()   # missing
        missing = self.count()
        print("S4: corrupt audit counted %d of 3, missing audit counted %d of 3" % (len(corrupt), len(missing)))
        self.assertEqual(len(corrupt), 3)
        self.assertEqual(len(missing), 3)

    def test_s4_a_failed_stop_or_a_stop_before_the_create_does_not_free(self):
        for n in range(1, 4):
            self.put("coder%d" % n)
        self.write_audit(self.line("coder1", "create"), self.line("coder1", "stop", ok=False),
                         self.line("coder2", "stop"), self.line("coder2", "create"), self.line("coder3", "create"))
        self.assertEqual(self.count(), ["coder1", "coder2", "coder3"])


class TestMutant(unittest.TestCase):
    """A scratch copy of the code OUTSIDE the repository, run in a child process by stub_support.start."""
    TEST = "TestScenarios.test_s2_three_stopped_of_four_count_one_and_the_launch_goes"

    def child(self, edits):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for sub in ("bin", "cloud/worker/tests", "cloud/launcher"):
            shutil.copytree(ROOT / sub, os.path.join(d, sub), ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
        part = Path(d, "bin", "cloud_lane_cap.py")
        src, applied = part.read_text(), 0
        for old, new in edits:
            self.assertEqual(src.count(old), 1, "the line to change is not as expected: " + old)
            src = src.replace(old, new, 1)
            applied += 1
        part.write_text(src)
        env = dict(os.environ, TMPDIR=d)
        argv = [sys.executable, os.path.join(d, "cloud", "launcher", "tests", Path(__file__).name), self.TEST]
        with open(os.path.join(d, "out.txt"), "w") as out_f, open(os.path.join(d, "err.txt"), "w") as err_f:
            p = stub_support.start(self, argv, cwd=d, env=env, stdout=out_f, stderr=err_f)
            code = p.wait(timeout=90)
        return applied, code, Path(d, "out.txt").read_text() + Path(d, "err.txt").read_text()

    def test_s5_control_then_mutant_that_counts_every_record_until_its_hard_deadline(self):
        applied, code, out = self.child([])
        print("S5 control: exit %d of 0 expected" % code)
        self.assertEqual((applied, code), (0, 0), out[-800:])
        applied, code, out = self.child([('            if record.get("runtime_id") not in stopped:', "            if True:")])
        print("S5 mutant: exit %d, mutations applied %d of 1" % (code, applied))
        self.assertEqual((applied, code), (1, 1), out[-800:])
        self.assertIn(ANCHOR, out)


if __name__ == "__main__":
    unittest.main()
