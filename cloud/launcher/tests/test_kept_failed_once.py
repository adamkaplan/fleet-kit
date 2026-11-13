"""A kept pod that ended Failed or Succeeded is tried ONCE, marked cw/unreachable, and skipped by every later pass (fix forward, 5 Nov)."""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_failed_pod_record as fpr  # noqa: E402
cw = fpr.cw
NOTE = "kept pod ended: its work is unreachable by exec; the pod is left for a person"


class Scen(fpr.Fixture):
    def exec_calls(self):
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            return sum(1 for line in f if '"exec"' in line)

    def mine(self):
        return [r for r in self.records() if r["worker"] == self.w]

    def lines(self):
        return [x for x in self.audit_lines() if x["worker"] == self.w and x["action"] == "stop"]

    def past_hard(self):
        self.clock.advance(hours=4)   # past the hard deadline (2 h), inside KEPT_GRACE_SECONDS (24 h, #2345): the pod is not yet expired

    def scenario(self, phase, w="keptf"):
        """Three passes over a kept pod of `phase`, past its hard limit; asserts the one-try rule."""
        self.w = w
        self.fail(w, {"phase": phase}, kept=True)
        self.past_hard()
        self.reap()
        self.assertEqual(len(self.mine()), 1, "RECORDS_PASS1")
        self.assertEqual(len(self.lines()), 1, "AUDIT_PASS1")
        self.assertIn(NOTE, self.lines()[0]["reason"])
        self.assertEqual(self.pod(w)["metadata"]["annotations"].get("cw/unreachable"), "true", "MARK")
        execs = self.exec_calls()
        for n in (2, 3):
            self.reap()
            self.assertEqual(len(self.mine()), 1, "RECORDS_PASS%d" % n)
            self.assertEqual(len(self.lines()), 1, "AUDIT_PASS%d" % n)
            self.assertEqual(self.exec_calls(), execs, "EXEC_PASS%d" % n)
        self.assertIsNotNone(self.pod(w))   # never deleted


class Kept(Scen):
    def test_kept_failed_is_tried_once(self):
        self.scenario("Failed")

    def test_kept_succeeded_is_tried_once(self):
        self.scenario("Succeeded", "keptok")

    def test_unkept_failed_is_still_deleted(self):
        self.fail("plainf", {"phase": "Failed"})
        self.reap()
        self.clock.advance(minutes=11)
        self.reap()
        self.assertIsNone(self.pod("plainf"))

    def test_unkept_failed_past_hard_is_still_deleted(self):
        self.fail("plainh", {"phase": "Failed"})
        self.past_hard()
        self.reap()
        self.assertIsNone(self.pod("plainh"))

    def test_unkept_ended_pod_with_the_mark_is_still_deleted(self):
        self.fail("stray", {"phase": "Failed"})
        st = self.st()
        st["pods"][cw.pod_name("stray")]["metadata"].setdefault("annotations", {})["cw/unreachable"] = "true"
        self.write_state(st)
        self.past_hard()
        self.reap()
        self.clock.advance(minutes=11)
        self.reap()
        self.assertIsNone(self.pod("stray"))

    def test_a_mark_that_raises_still_counts_as_the_one_try(self):
        self.w = "boom"
        self.fail("boom", {"phase": "Failed"}, kept=True)
        self.past_hard()
        la = cw.Launcher(cw.Kube(self.env), self.env, self.clock)   # one launcher: the process's own state
        with mock.patch.object(cw.Kube, "annotate", side_effect=cw.CloudError("annotate refused")):
            la.reap()
            self.assertEqual(len(self.mine()), 1)
            self.assertEqual(len(self.lines()), 1)
            la.reap()
            la.reap()
        self.assertEqual(len(self.mine()), 1, "RECORDS_AFTER")
        self.assertEqual(len(self.lines()), 1, "AUDIT_AFTER")

    def test_kept_running_is_still_retried(self):
        self.w = "runner"
        self.create("runner")
        st = self.st()
        st["pods"][cw.pod_name("runner")]["metadata"]["annotations"]["cw/kept"] = "true"
        self.write_state(st)
        self.past_hard()
        self.reap()
        first = len(self.mine())
        self.reap()
        self.assertGreater(len(self.mine()), first)
        self.assertNotIn("cw/unreachable", self.pod("runner")["metadata"]["annotations"])
        self.assertIsNotNone(self.pod("runner"))


class Mutant(Scen):
    def test_a_mutant_without_the_skip_writes_a_record_every_pass(self):
        orig, applied = cw.ann, []

        def blind(pod, key, default=None):
            if key == "unreachable":
                applied.append(1)
                return default
            return orig(pod, key, default)
        self.scenario("Failed", "ctrl")   # control first: the unmutated code passes
        self.assertFalse(applied)
        with mock.patch.object(cw, "ann", blind):
            with self.assertRaises(AssertionError) as cm:
                self.scenario("Failed", "mut")
        self.assertTrue(applied, "the mutation was not applied")
        self.assertTrue("RECORDS_PASS2" in str(cm.exception), str(cm.exception))


if __name__ == "__main__":
    unittest.main()
