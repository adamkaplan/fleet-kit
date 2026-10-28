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


import re  # noqa: E402
import test_cloud_status_summary as ss  # noqa: E402


class S8StatusLine(ss.Base):
    """S8: the switchboard's status line for a launcher answer whose pod_failed field is hostile or not text (ask #1442, proof of the first head)."""
    INPUTS = (("escape", "OOM\x1b[31mred", True), ("bell", "OOM\x07ring", True), ("newline", "OOM\nkilled\tagain", True),
              ("number", 137, False), ("object", {}, False), ("list", ["OOMKilled"], False))

    def line_for(self, value):
        self.record("coder7", hard=ss.ALIVE)
        original = ss.CLOUD.run_launcher

        def fake(runner, command, worker, *rest, **kw):
            if command == "status":
                return 0, {"phase": "Failed", "pod_failed": value, "cost_estimate_usd": 0}, ""
            return original(runner, command, worker, *rest, **kw)
        with mock.patch.object(ss.CLOUD, "run_launcher", fake), mock.patch.object(ss.CLOUD, "_answers", lambda *a, **k: False):
            lines, rows = self.status()
        return [line for line in lines if "coder7" in line], rows

    def test_six_inputs_print_one_clean_line_and_never_raise(self):
        for label, value, is_text in self.INPUTS:
            with self.subTest(label):
                mine, rows = self.line_for(value)
                self.assertEqual(len(mine), 1, mine)
                self.assertNotRegex(mine[0], r"[\x00-\x1f\x7f-\x9f]")
                self.assertEqual(len(rows), 1)
                if is_text:
                    self.assertIn("pod failed: OOM", mine[0])
                    self.assertNotIn("does not answer", mine[0])
                else:   # the line printed for a launcher answer it cannot read
                    self.assertIn("launcher status unavailable", mine[0])
                    self.assertNotIn("pod failed:", mine[0])

    def test_the_text_is_scrubbed_again_and_cut(self):
        mine, _ = self.line_for("note " * 100 + " " + PLANT)
        self.assertNotIn(PLANT, mine[0])
        self.assertIn("pod failed: note", mine[0])
        self.assertLessEqual(len(mine[0].split("pod failed: ")[1].split("; stops")[0]), 160)


class S9Launcher(Fixture):
    """S9: a foreign pod is untouched, an unwritable record is reported once and written when it can be, the stored event text is cut."""

    def setUp(self):
        super().setUp()
        self.told = getattr(cw, "FAILED_UNWRITTEN_TOLD", set())
        self.told.clear()
        self.addCleanup(self.told.clear)

    def test_a_labelled_pod_without_the_worker_annotation_is_never_touched(self):
        self.fail("ownd", CASES["oomer"][0])
        st = self.st()
        name = cw.pod_name("ownd")
        foreign = json.loads(json.dumps(st["pods"][name]))
        foreign["metadata"]["name"] = "foreign-pod"
        foreign["metadata"]["annotations"].pop("cw/worker")
        st["pods"]["foreign-pod"] = foreign
        self.write_state(st)
        for minutes in (0, 11, 11):
            self.clock.advance(minutes=minutes)
            self.reap()
        self.assertIn("foreign-pod", self.st()["pods"])
        self.assertEqual([r["worker"] for r in self.records()], ["ownd"])
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            self.assertNotIn("foreign-pod", f.read())   # never read (no get, no events), never deleted

    def test_an_unwritable_record_keeps_the_pod_is_told_once_and_is_written_when_it_can_be(self):
        self.fail("stuck", CASES["oomer"][0])
        real, calls = cw.run_record.write_private, []

        def refuse(*a, **k):
            calls.append(1)
            if len(calls) <= 3:
                raise OSError("disk full")
            return real(*a, **k)
        passes = []
        with mock.patch.object(cw.run_record, "write_private", refuse):
            for _ in range(4):
                self.clock.advance(minutes=11)
                passes.append(self.reap())
        self.assertEqual([len(t) for t in passes], [1, 0, 0, 1])
        self.assertTrue(passes[0][0][1].startswith("record not written"), passes[0])
        self.assertTrue(passes[3][0][1].startswith("pod failed:"), passes[3])
        self.assertEqual(len(self.records()), 1)
        self.assertIsNotNone(self.pod("stuck"))   # in place for the 3 passes (11 minutes apart) and the 4th

    def test_a_long_event_text_of_words_is_cut_to_300_characters(self):
        self.fail("wordy", {"phase": "Failed"})
        st = self.st()
        for ev in st["events"]:
            ev["message"] = " ".join("word%d" % i for i in range(100))[:500]
        self.write_state(st)
        self.reap()
        events = self.records()[0]["events"]
        self.assertTrue(events)
        self.assertTrue(any(len(e) > 100 for e in events), events)   # words survive the scrub, so the cap is what cuts them
        self.assertTrue(all(len(e) <= 300 for e in events))


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

    def test_the_two_limits_of_the_second_head_are_stated(self):   # S8 and S9
        for path in DOC_FILES:
            t = doc(path)
            for phrase in ("every control character removed", "never an error", "carries a worker annotation", "`record not written`"):
                self.assertIn(phrase, t, path)


if __name__ == "__main__":
    unittest.main()
