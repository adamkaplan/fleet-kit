"""A cloud pod that ends Failed leaves its reason and a record (ask #1442): scenarios S1 to S6 and U1.

Offline: the fake kubectl and a fake clock of the other launcher tests. Every fixture is synthetic; the token-shaped string is built at run time.
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
PLANT = "ghp_" + "Zq81LmN4pR7tVw2Y9aBc"   # two halves: no complete token-shaped string sits in this file

CASES = {   # worker: (the pod's status, the reason the record must give, the exit code)
    "oomer": ({"phase": "Failed", "containerStatuses": [{"name": "worker", "state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]},
              "OOMKilled (exit 137)", 137),
    "evictd": ({"phase": "Failed", "reason": "Evicted", "message": "The node was low on resource: memory."}, "Evicted", None),
    "exiter": ({"phase": "Failed", "containerStatuses": [{"name": "worker", "state": {"terminated": {"reason": "Error", "exitCode": 1}}}]},
                "Error (exit 1)", 1),
}


class Fixture(base.Base):
    def setUp(self):
        super().setUp()
        self.runs = os.path.join(self.tmp.name, "runs")
        self.env["CW_RUN_RECORD_DIR"] = self.runs

    def fail(self, worker, status, kept=False):
        self.create(worker)
        st = self.st()
        pod = st["pods"][cw.pod_name(worker)]
        pod["status"] = status
        if kept:
            pod["metadata"].setdefault("annotations", {})["cw/kept"] = "true"
        evs = st.setdefault("events", [])
        name = cw.pod_name(worker)
        for i in range(14):
            evs.append({"reason": "Ev%d" % i, "message": "event %d " % i + "x" * 500 + (" " + PLANT if i == 12 else ""),
                        "involvedObject": {"kind": "Pod", "name": name}})
        self.write_state(st)

    def records(self):
        out = []
        if os.path.isdir(self.runs):
            for n in sorted(os.listdir(self.runs)):
                with open(os.path.join(self.runs, n, "run.json")) as f:
                    out.append(json.load(f))
        return out

    def activity_reads(self):
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            return sum(1 for line in f if "cw-worker-activity" in line)

    def reap(self):
        return cw.Launcher(cw.Kube(self.env), self.env, self.clock).reap()


class S2Records(Fixture):
    def test_each_failed_pod_gets_a_record_with_its_reason(self):
        for w, (status, _, _) in CASES.items():
            self.fail(w, status)
        self.reap()
        recs = {r["worker"]: r for r in self.records()}
        self.assertEqual(sorted(recs), sorted(CASES))
        for w, (_, reason, code) in CASES.items():
            r = recs[w]
            self.assertEqual(r["exit_reason"], "pod failed")
            self.assertEqual(r["pod_failed_reason"], reason)
            self.assertEqual(r["exit_code"], code)
            self.assertLessEqual(len(r["events"]), 10)
            self.assertTrue(r["events"])
            self.assertTrue(all(len(e) <= 300 for e in r["events"]))
            self.assertNotIn(PLANT, json.dumps(r))
        for n in os.listdir(self.runs):
            with open(os.path.join(self.runs, n, "run.json")) as f:
                self.assertNotIn(PLANT, f.read())

    def test_a_pod_with_no_container_reason_names_its_last_event(self):
        self.fail("quiet", {"phase": "Failed"})
        self.reap()
        self.assertEqual(self.records()[0]["pod_failed_reason"], "Ev13")


class S3Status(Fixture):
    def test_status_says_pod_failed_with_the_reason(self):
        for w, (status, reason, _) in CASES.items():
            self.fail(w, status)
        for w, (_, reason, _) in CASES.items():
            code, out, err = self.run_cli("status", w)
            self.assertEqual(code, 0, err)
            rec = json.loads(out)[0]
            self.assertEqual(rec["status_line"], "pod failed: " + reason)
            self.assertTrue(rec["status_line"].startswith("pod failed:"))
            self.assertLessEqual(len(rec["status_line"]), 200)
            self.assertNotIn("does not answer", out)
        self.assertEqual(len(self.records()), 3)

    def test_the_reason_is_cut_to_160_characters(self):
        self.fail("longr", {"phase": "Failed", "reason": "slow " * 80})
        code, out, _ = self.run_cli("status", "longr")
        self.assertEqual(len(json.loads(out)[0]["pod_failed"]), 160)

    def test_the_switchboard_line_is_wired(self):
        with open(os.path.join(REPO_ROOT, "bin", "cloud_worker.py")) as f:
            self.assertIn("pod failed: %s", f.read())


class S4Passes(Fixture):
    def test_two_passes_record_once_and_report_once(self):
        self.fail("oomer", CASES["oomer"][0])
        first = self.reap()
        self.assertEqual(first, [("oomer", "pod failed: OOMKilled (exit 137)", 0)])
        reads = self.activity_reads()
        self.assertEqual(reads, 0)
        self.assertEqual(self.reap(), [])
        self.assertEqual(self.activity_reads(), reads)   # 0 activity reads in pass 2
        self.assertEqual(len(self.records()), 1)
        self.assertIsNotNone(self.pod("oomer"))

    def test_deleted_after_ten_minutes_unless_kept(self):
        self.fail("plain", CASES["oomer"][0])
        self.fail("kepto", CASES["oomer"][0], kept=True)
        self.reap()
        self.clock.advance(minutes=9)
        self.reap()
        self.assertIsNotNone(self.pod("plain"))
        self.clock.advance(minutes=2)
        self.assertEqual(self.reap(), [])
        self.assertIsNone(self.pod("plain"))
        self.assertIsNotNone(self.pod("kepto"))
        self.assertEqual(len(self.records()), 2)


class S5Mutant(Fixture):
    def test_a_mutant_that_skips_the_record_fails_the_tests(self):
        applied = []

        def skip(*a, **k):
            applied.append(1)
        for name, cls in (("S2", S2Records), ("S4", S4Passes)):
            with self.subTest(name):
                failed = 0
                with mock.patch.object(cw.run_record, "write_private", skip):
                    for t in unittest.defaultTestLoader.loadTestsFromTestCase(cls):
                        res = unittest.TestResult()
                        t.run(res)
                        failed += len(res.failures) + len(res.errors)
                self.assertGreater(failed, 0)
        self.assertTrue(applied, "the mutation was not applied")


DOC_FILES = ("cloud/launcher/README.md", "docs/cloud-worker-interface.md")


def doc(path):
    with open(os.path.join(REPO_ROOT, path)) as f:
        text = f.read()
    return " ".join(text[text.index("A pod that ends Failed (ask #1442)"):].split())


class S1S6Docs(unittest.TestCase):
    def test_the_cause_is_stated(self):   # S1: 4 of 4
        for path in DOC_FILES:
            t = doc(path)
            for phrase in ('"does not answer"', "`activity unreadable`", "two hours", "No run record was written", "`get_pod` and `pod_events`"):
                self.assertIn(phrase, t, path)

    def test_the_record_and_how_a_lane_reads_it(self):   # S6: 3 of 3 and the reading
        for path in DOC_FILES:
            t = doc(path)
            for phrase in ("the reason, the exit code and the events", "exit reason `pod failed`", "`status` prints `pod failed: <reason>`", "ten minutes"):
                self.assertIn(phrase, t, path)


if __name__ == "__main__":
    unittest.main()
