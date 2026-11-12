"""Ask #2472: the end notice counts a results file as finished only when it is THIS run's (newer than the run's start) and its LAST non-empty line is `STATUS: done`.
Offline: the fake cluster (a pod whose results file has a chosen modification time) and the end alert's own notice text."""
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "bin"))
import test_run_record as trr  # noqa: E402
import cloud_end_alert as alert  # noqa: E402
import run_record as rr  # noqa: E402
import stall_watch as sw  # noqa: E402

OLD, NEW = 1000, 4000000000   # far before and far after any run's start
DONE_LAST = "worked\nSTATUS: done\n"
DONE_FIRST = "STATUS: done\nmore work to do\n"
STALE_TEXT = "results.txt is from an earlier run"


class Own(trr.Record):
    def results(self, content, mtime):
        self.patch_state(report_fs={self.name: {"type": "file", "content": content, "mtime": mtime}})
        p, out = self.run_driver()
        self.assertEqual(p.returncode, 0, out)
        return json.loads(self.read(os.path.join(self.the_record(), "run.json"))).get("results")

    def notice(self, results):
        d = os.path.join(self.tmp.name, "n")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "run.json"), "w") as f:
            json.dump({"results": results} if results is not None else {}, f)
        done = alert.reported_done(d, json.loads)
        return alert.message_text("w1", "idle limit", "now", "nothing unpushed", "r", done=done, note=alert.results_note(d, json.loads))

    def test_s2_a_leftover_file_from_an_earlier_run_is_stale(self):
        found = self.results(DONE_LAST, OLD)
        self.assertEqual((found["state"], found["done"]), ("stale", False))
        text = self.notice(found)
        self.assertIn(STALE_TEXT, text)
        self.assertNotIn("STATUS: done", text, "stale results file counted as this run")

    def test_s3_a_file_written_by_this_run_says_done(self):
        found = self.results(DONE_LAST, NEW)
        self.assertEqual((found["state"], found["done"]), ("done", True))
        self.assertIn("its results file says STATUS: done", self.notice(found))

    def test_s4_a_marker_that_is_not_the_last_line_is_unfinished(self):
        found = self.results(DONE_FIRST, NEW)
        self.assertEqual((found["state"], found["done"]), ("unfinished", False))
        text = self.notice(found)
        self.assertIn("results.txt present but not finished", text)
        self.assertNotIn("STATUS: done", text)

    def test_s5_no_results_file_says_so_and_the_rest_of_the_notice_is_unchanged(self):
        p, out = self.run_driver()
        self.assertEqual(p.returncode, 0, out)
        self.assertIsNone(json.loads(self.read(os.path.join(self.the_record(), "run.json"))).get("results"))
        text = self.notice(None)
        base = alert.message_text("w1", "idle limit", "now", "nothing unpushed", "r")
        self.assertEqual(text, base + " Results: no results file.")

    def test_s6_control_then_mutant_that_ignores_the_start(self):
        def check():   # the S2 check on the rule itself
            self.assertEqual(rr.results_verdict("STATUS: done", OLD, 5000), "stale", "stale results file counted as this run")
        check()   # control: the real rule passes
        real = rr.results_verdict
        rr.results_verdict = lambda line, mtime, start, nocase=False: real(line, None, None, nocase)   # the comparison replaced by true
        try:
            with self.assertRaisesRegex(AssertionError, "stale results file counted as this run"):
                check()
        finally:
            rr.results_verdict = real

    def test_the_stall_watch_shares_the_rule(self):
        data = {"now": 9000, "results": "status: DONE", "results_mtime": OLD, "started": 5000}
        self.assertFalse(sw._results_done(data))
        self.assertTrue(sw._results_done(dict(data, results_mtime=6000)))


for _n in dir(trr.Record):   # the inherited tests are that file's own, not run again here
    if _n.startswith("test") and _n not in Own.__dict__:
        setattr(Own, _n, None)
del _n


if __name__ == "__main__":
    unittest.main()
