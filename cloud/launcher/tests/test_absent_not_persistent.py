"""Fix forward of #1532: an absent pod of a worker the launcher does not know as persistent is stopped with NO StatefulSet read (a kube that answers
Forbidden to every such read changes nothing); an absent persistent worker still reads strictly and refuses on Forbidden; a mutant that reads for every
absent pod fails the first check with an anchored text. Offline."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "worker", "tests"))
import test_persistent_orchestrator as po  # noqa: E402

NAME, WORKER, POD = po.NAME, po.WORKER, po.POD
FORBIDDEN_LINE = po.S7Lookup.FORBIDDEN % (NAME, "cw") + "\n"
MUTANT_OLD = "            if pod is None and self._known_persistent(worker):\n                # ONE strict read"
MUTANT_NEW = "            if pod is None:\n                # ONE strict read"


class AbsentNotPersistent(po.PBase):
    sts_reads, plain_worker, stop_with = po.OneRead.sts_reads, po.OneRead.plain_worker, po.OneRead.stop_with
    setUp_again, reset_cluster = po.OneRead.setUp_again, po.OneRead.reset_cluster

    def check_plain_forbidden(self):
        self.plain_worker()
        code, out, err = self.stop_with(dict(err=FORBIDDEN_LINE))
        self.assertEqual(len(self.sts_reads()), 0, "a workload read happened for a worker that is not persistent")
        self.assertEqual(code, 0, err)
        self.assertIn("pod_gone", out)
        self.assertFalse({NAME + "-pw", NAME + "-run"} & set(self.st()["secrets"]))

    def test_control_absent_plain_pod_stops_with_forbidden_and_no_read(self):
        self.check_plain_forbidden()

    def test_absent_persistent_worker_still_reads_and_refuses_on_forbidden(self):
        self.launch()
        pods = dict(self.st()["pods"])
        pods.pop(POD, None)
        self.patch_state(pods=pods)
        code, out, err = self.stop_with(dict(err=FORBIDDEN_LINE))
        self.assertEqual(code, 1, err)
        self.assertEqual(len(self.sts_reads()), 1)
        self.assertIn("workload unreadable", err)
        self.assertTrue(self.st().get("sts"))

    def test_mutant_reading_for_every_absent_pod_fails_the_first_check(self):
        self.check_plain_forbidden()   # control first: the real source passes
        self.mod = po.mutant(MUTANT_OLD, MUTANT_NEW)
        with open(po.SOURCE) as f:
            self.assertEqual(f.read().count(MUTANT_OLD), 1)   # applied: the target is there once and the module was built from the changed text
        with self.assertRaises(AssertionError) as caught:
            self.setUp_again()
            self.check_plain_forbidden()
        text = str(caught.exception)
        self.assertIn("a workload read happened for a worker that is not persistent", text)


if __name__ == "__main__":
    unittest.main()
