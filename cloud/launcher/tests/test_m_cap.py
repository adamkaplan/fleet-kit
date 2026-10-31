#!/usr/bin/env python3
"""Offline tests: the cap of shape-M cloud workers per lane (ask #1640).

Cause: the launch had no limit on the workers of a shape or of a lane, and the record held no shape; one lane's nine 4-CPU pods filled the node pool.
Run directly: python3 cloud/launcher/tests/test_m_cap.py
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("FL" + "EET_SWITCHBOARD_UNDER_TEST", "1")
sys.path.insert(0, str(ROOT / "cloud" / "worker" / "tests"))
import stub_support  # noqa: E402
_loader = importlib.machinery.SourceFileLoader("cw_test_cloud_worker_mod", str(ROOT / "bin" / "test-cloud-worker"))
_spec = importlib.util.spec_from_loader("cw_test_cloud_worker_mod", _loader)
tcw = importlib.util.module_from_spec(_spec)
sys.modules["cw_test_cloud_worker_mod"] = tcw
_loader.exec_module(tcw)

FUTURE, PAST = "2099-01-01T00:00:00Z", "2001-01-01T00:00:00Z"
OLD_ONLY = "M_CAP_SCRATCH_OLD"   # set only in the scratch copy of the code before the change


class Cap(tcw.Base):
    def put(self, name, lane="lane-a", shape="M", hard=FUTURE):
        record = {"worker": name, "url": self.remote.url, "runtime_id": "rt-" + name, "idle_deadline": hard, "hard_deadline": hard, "reports_to": lane}
        if shape:
            record["shape"] = shape
        path = self.state / "cloud" / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        for d in (self.state, path.parent):
            d.chmod(0o700)
        path.write_text(json.dumps(record))
        path.chmod(0o600)

    def ask(self, name, lane="lane-a", shape="M"):
        brief = self.home / "brief.txt"
        brief.write_text("Build the thing.\n")
        return self.main("launch", name, "--cloud", "--agent", "coder", "--repo", "acme-org/widget-service", "--branch", "main", "--role", "coder",
                         "--reports-to", lane, "--shape", shape, "--brief-file", str(brief))

    def calls(self):
        return len(self.stub_calls()) + len(self.jsonl("kubectl.log")) + len(self.jsonl("login.log"))

    def shapes(self):
        return {p.stem: json.loads(p.read_text()).get("shape") for p in (self.state / "cloud").glob("*.json")}

    def cap_refused(self, err):
        return "already holds" in err


class TestScenarios(Cap):
    def test_s2_the_code_before_the_change_accepts_a_sixth_launch(self):
        if not os.environ.get(OLD_ONLY):
            self.skipTest("runs in the scratch copy of the code before the change (TestScratch)")
        for n in range(1, 6):
            self.put("coder%d" % n)
        code, out, err = self.ask("coder6")
        records = [p for p in (self.state / "cloud").glob("*.json") if "shape" in json.loads(p.read_text()) and p.stem == "coder6"]
        print("S2: launches accepted for a lane holding 5 records %d, written records with a shape key %d" % (code == 0, len(records)))
        self.assertEqual(code, 0, err)
        self.assertEqual(records, [])

    def test_s3_a_fifth_shape_m_launch_stops_at_the_cap_with_no_call(self):
        for n in range(1, 5):
            self.put("coder%d" % n)
        code, out, err = self.ask("coder5")
        lines = [l for l in err.splitlines() if "already holds" in l and "lane-a" in l]
        named = [n for n in ("coder1", "coder2", "coder3", "coder4") if n in (lines or [""])[0]]
        print("S3: refusal exit %d, lines naming the lane %d, workers named %d, token minter calls %d, launcher calls %d, cluster calls %d"
              % (code, len(lines), len(named), len(self.jsonl("login.log")), len(self.stub_calls()), len(self.jsonl("kubectl.log"))))
        self.assertEqual(code, 2, err)
        self.assertEqual(len(lines), 1, err)
        self.assertIn("holds 4 shape-M", lines[0])
        self.assertIn("coder1, coder2, coder3, coder4", lines[0])
        self.assertIn("retire one", lines[0])
        self.assertIn("--shape S", lines[0])
        self.assertEqual(self.calls(), 0)

    def test_s4_other_shapes_names_and_lanes_proceed_as_before(self):
        for n in range(1, 5):
            self.put("coder%d" % n)
        for n in range(5, 8):
            self.put("coder%d" % n, lane="lane-c")
        self.put("sidekick", lane="lane-c", shape="S")   # a record of another shape must not count toward lane-c
        results = []
        results.append(self.ask("coder8", shape="S"))                 # another shape, full lane
        results.append(self.ask("coder1"))                             # a name that has a record
        results.append(self.ask("coder9", lane="lane-b"))              # another lane, 0 records
        results.append(self.ask("coder10", lane="lane-c"))             # 3 shape-M records and one shape-S record: the 4th shape M is accepted
        refused = sum(1 for _, _, e in results if self.cap_refused(e))
        print("S4: launches proceeding as before %d of 4, refusals by the cap %d" % (4 - refused, refused))
        self.assertEqual(refused, 0, [e for _, _, e in results])
        self.assertEqual(results[0][0], 0, results[0][2])
        self.assertEqual(results[2][0], 0, results[2][2])
        self.assertEqual(results[3][0], 0, results[3][2])

    def test_s5_ended_shapeless_and_retired_records_are_not_counted(self):
        for n in range(1, 4):
            self.put("coder%d" % n)
        self.put("coder4", hard=PAST)
        self.put("coder5", shape=None)
        self.put("coder6")
        (self.state / "cloud" / "coder6.json").unlink()   # a retired worker: no file
        H = tcw.sb.load_module("cloud_worker").bind(tcw.sb.__dict__)
        count = len(H.lane_m_workers(self.state, "lane-a"))
        code, out, err = self.ask("coder7")
        print("S5: non-counted records %d of 3, launches accepted %d of 1" % (3 if count == 3 else 0, code == 0))
        self.assertEqual(count, 3)
        self.assertEqual(code, 0, err)

    def test_s6_the_record_has_the_shape_and_the_count_reads_no_cluster(self):
        c1 = self.ask("coder1", shape="M")
        c2 = self.ask("coder2", lane="lane-b", shape="S")
        shapes = self.shapes()
        self.assertEqual((c1[0], c2[0]), (0, 0), (c1[2], c2[2]))
        self.stub_dir.joinpath("kubectl.log").unlink(missing_ok=True)
        self.stub_dir.joinpath("login.log").unlink(missing_ok=True)
        self.stub_dir.joinpath("calls.log").unlink(missing_ok=True)
        H = tcw.sb.load_module("cloud_worker").bind(tcw.sb.__dict__)
        H.lane_m_workers(self.state, "lane-a")
        print("S6: records with shape M %d, records with shape S %d, cluster calls by the count %d"
              % (list(shapes.values()).count("M"), list(shapes.values()).count("S"), self.calls()))
        self.assertEqual(shapes, {"coder1": "M", "coder2": "S"})
        self.assertEqual(self.calls(), 0)

    def test_s6_a_launch_without_a_shape_writes_no_shape_key(self):
        brief = self.home / "brief.txt"
        brief.write_text("x\n")
        code, out, err = self.launch("coder3")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.shapes(), {"coder3": None})


class TestDocs(unittest.TestCase):
    SENTENCES = ("because a lane's batch of 4-CPU pods once filled the cluster and left every other lane's launches unplaceable",
                 "retire one (retire NAME) or launch a smaller shape (--shape S)",
                 "a launch still in flight has no record yet; two launches of one lane at the same moment can pass together; "
                 "records written before this change hold no shape")

    def test_s7_the_three_documents_say_the_cap_the_text_and_the_limits(self):
        found = 0
        for rel in ("docs/cloud-operator-quickstart.md", "docs/cloud-worker-interface.md", "cloud/launcher/README.md"):
            text = (ROOT / rel).read_text()
            self.assertIn("MAX_M_PER_LANE", text, rel)
            for s in self.SENTENCES:
                self.assertIn(s, text, rel)
        text = (ROOT / "docs/cloud-operator-quickstart.md").read_text()
        found = sum(1 for s in self.SENTENCES if s in text)
        print("S7: %d of 3 sentences found" % found)
        self.assertEqual(found, 3)


class TestScratch(unittest.TestCase):
    """Scratch copies OUTSIDE the repository (a temporary directory), each run in a child process by stub_support.start."""

    def child(self, edits, test, old=False):
        src = (ROOT / "bin" / "cloud_worker.py").read_text()
        applied = 0
        for old_text, new_text in edits:
            self.assertEqual(src.count(old_text), 1, "the line to change is not as expected: " + old_text)
            src = src.replace(old_text, new_text, 1)
            applied += 1
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        shutil.copytree(ROOT / "bin", os.path.join(d, "bin"), ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "cloud" / "worker" / "tests", os.path.join(d, "cloud", "worker", "tests"), ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "cloud" / "launcher", os.path.join(d, "cloud", "launcher"), ignore=shutil.ignore_patterns("__pycache__", "tests"), dirs_exist_ok=True)
        os.makedirs(os.path.join(d, "cloud", "launcher", "tests"), exist_ok=True)
        shutil.copy(__file__, os.path.join(d, "cloud", "launcher", "tests"))
        shutil.copy(ROOT / "cloud" / "launcher" / "tests" / "fake_opencode_export.sh", os.path.join(d, "cloud", "launcher", "tests"))
        Path(d, "bin", "cloud_worker.py").write_text(src)
        env = dict(os.environ, TMPDIR=d)
        if old:
            env[OLD_ONLY] = "1"
        argv = [sys.executable, os.path.join(d, "cloud", "launcher", "tests", Path(__file__).name), "TestScenarios." + test]
        p = stub_support.start(self, argv, cwd=d, env=env, stdout=open(os.path.join(d, "out.txt"), "w"), stderr=open(os.path.join(d, "err.txt"), "w"))
        code = p.wait(timeout=90)
        return applied, code, Path(d, "out.txt").read_text() + Path(d, "err.txt").read_text()

    def test_s2_the_old_code_accepts_the_sixth_launch(self):
        edits = [('    if args.shape == "M":   # the per-lane cap', '    if False:   # the per-lane cap'),
                 ("            if isinstance(args.shape, str) and args.shape:\n                record[\"shape\"]", "            if False:\n                record[\"shape\"]")]
        applied, code, out = self.child(edits, "test_s2_the_code_before_the_change_accepts_a_sixth_launch", old=True)
        print(out.strip().splitlines()[0] if "S2:" in out else out[-300:])
        self.assertEqual((applied, code), (2, 0), out[-800:])
        self.assertIn("S2: launches accepted for a lane holding 5 records 1, written records with a shape key 0", out)

    def test_s8_a_mutant_that_never_stops_a_launch_fails_the_cap_scenario(self):
        applied, code, out = self.child([("            if len(held) >= MAX_M_PER_LANE:", "            if False:")], "test_s3_a_fifth_shape_m_launch_stops_at_the_cap_with_no_call")
        print("S8: mutant never-stops exit %d, mutations applied %d of 1" % (code, applied))
        self.assertEqual((applied, code), (1, 1), out[-800:])

    def test_s8_a_mutant_that_counts_every_shape_fails_the_other_shapes_scenario(self):
        applied, code, out = self.child([('record.get("shape") != "M" or ', "")], "test_s4_other_shapes_names_and_lanes_proceed_as_before")
        print("S8: mutant counts-every-shape exit %d, mutations applied %d of 1" % (code, applied))
        self.assertEqual((applied, code), (1, 1), out[-800:])


if __name__ == "__main__":
    unittest.main()
