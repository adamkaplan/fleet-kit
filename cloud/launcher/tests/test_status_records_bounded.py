#!/usr/bin/env python3
"""The real launcher's `status` for all workers reads the pods four at a time, each with its own bound (ask #1493, S10); the named form stays serial.

Offline: the launcher's own fake kubectl; the activity read is a fake that sleeps (scaled).
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as launcher_tests   # noqa: E402  (the launcher's own fake cluster)


class LauncherRecords(launcher_tests.Base):
    """S10: the real launcher's `status` for all workers reads the pods four at a time, each with its own bound; the named form stays serial."""
    SLEEP = 0.3

    def setUp(self):
        super().setUp()
        self.cw = launcher_tests.cw
        self.calls = []
        self.lock = threading.Lock()

    def pods(self, count):
        started = self.cw.iso(launcher_tests.T0)
        return [{"metadata": {"name": "cw-w%d" % i, "annotations": {"cw/worker": "w%d" % i, "cw/hard-deadline": started, "cw/started": started}},
                 "status": {"phase": "Running"}} for i in range(count)]

    def launcher(self, pods, fail=()):
        launcher = self.cw.Launcher(self.cw.Kube(self.env), self.env, self.clock)
        launcher.kube.list_pods = lambda: pods
        launcher.kube.get_pod = lambda name: pods[0]
        sleep, calls, lock = self.SLEEP, self.calls, self.lock

        def activity(name):
            with lock:
                calls.append(name)
            time.sleep(sleep)
            if name in fail:
                raise RuntimeError("the exec failed")
            return False, launcher_tests.T0
        launcher.activity = activity
        return launcher

    def ask(self, launcher, worker=None):
        return launcher.status(launcher_tests.argparse.Namespace(worker=worker))

    def check_concurrent(self, serial=False):
        pods = self.pods(15)
        launcher = self.launcher(pods)
        with mock.patch.object(self.cw, "STATUS_POD_SECONDS", 1):
            if serial:   # the mutant: the old loop
                self.applied = True
                started = time.monotonic()
                records = [launcher.record(p) for p in pods]
            else:
                started = time.monotonic()
                records = self.ask(launcher)
            took = time.monotonic() - started
        print("launcher 15 pods took %.2f s" % took, file=sys.stderr)
        self.assertLess(took, self.SLEEP * 15 / 4 + 1, took)
        self.assertEqual([r["worker"] for r in records], ["w%d" % i for i in range(15)])
        return records

    def test_s10_fifteen_pods_are_read_four_at_a_time_not_one_after_another(self):
        records = self.check_concurrent()
        self.assertTrue(all(r["idle_since"] is not None for r in records))
        self.assertEqual(len(self.calls), 15)

    def test_s10_a_mutant_with_the_serial_loop_fails_the_timing_test(self):
        self.applied = False
        with self.assertRaises(AssertionError):
            self.check_concurrent(serial=True)
        self.assertTrue(self.applied, "the mutation was not applied")

    def test_s10_a_pod_whose_activity_raises_has_empty_idle_fields_and_the_other_fourteen_are_complete(self):
        pods = self.pods(15)
        records = self.ask(self.launcher(pods, fail=("cw-w3",)))
        self.assertEqual([r["worker"] for r in records], ["w%d" % i for i in range(15)])
        bad = records[3]
        self.assertIsNone(bad["idle_since"])
        self.assertIsNone(bad["deadlines"]["idle"])
        self.assertEqual(bad["deadlines"]["hard"], pods[3]["metadata"]["annotations"]["cw/hard-deadline"])
        self.assertEqual(bad["phase"], "Running")
        self.assertEqual(list(bad), ["worker", "runtime_id", "phase", "idle_since", "deadlines", "cost_estimate_usd"])
        self.assertEqual(len([r for r in records if r["idle_since"] is not None and r["deadlines"]["idle"] is not None]), 14)

    def test_s10_a_pod_over_its_bound_is_answered_without_its_activity(self):
        pods = self.pods(2)
        self.SLEEP = 1.0
        launcher = self.launcher(pods)
        with mock.patch.object(self.cw, "STATUS_POD_SECONDS", 0.1):
            records = self.ask(launcher)
        self.assertEqual([r["idle_since"] for r in records], [None, None])

    def check_total_deadline(self, pod_seconds, total_seconds):
        """S13 scaled: 300 pods whose activity read hangs; the whole answer comes within the total deadline plus one second."""
        pods = self.pods(300)
        self.SLEEP = 30
        launcher = self.launcher(pods)
        with mock.patch.object(self.cw, "STATUS_POD_SECONDS", pod_seconds), mock.patch.object(self.cw, "STATUS_TOTAL_SECONDS", total_seconds):
            started = time.monotonic()
            records = self.ask(launcher)
            took = time.monotonic() - started
        print("launcher 300 hanging pods took %.2f s" % took, file=sys.stderr)
        self.assertLess(took, 2 + 1, took)
        self.assertEqual([r["worker"] for r in records], ["w%d" % i for i in range(300)])
        unknown = [r for r in records if r["idle_since"] is None and r["deadlines"]["idle"] is None]
        self.assertGreaterEqual(len(unknown), 1)
        for r, p in zip(records, pods):
            self.assertEqual(list(r), ["worker", "runtime_id", "phase", "idle_since", "deadlines", "cost_estimate_usd"])
            self.assertEqual(r["runtime_id"], "pod/" + p["metadata"]["name"])
            self.assertEqual(r["phase"], "Running")
            self.assertEqual(r["deadlines"]["hard"], p["metadata"]["annotations"]["cw/hard-deadline"])
        return records

    def test_s13_three_hundred_hanging_pods_are_answered_within_the_total_deadline(self):
        records = self.check_total_deadline(0.2, 2)
        self.assertEqual(len(records), 300)

    def test_s13_a_mutant_without_the_total_deadline_fails(self):
        applied = []
        with self.assertRaises(AssertionError):
            applied.append(True)   # the mutation: each pod bounded only (the total deadline is far away)
            self.check_total_deadline(0.08, 10 ** 6)
        self.assertTrue(applied, "the mutation was not applied")

    def test_s10_the_named_worker_form_is_one_serial_record_as_before(self):
        pods = self.pods(1)
        launcher = self.launcher(pods)
        answer = self.ask(launcher, "w0")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(answer, [launcher.record(pods[0])])

    def test_s10_the_kube_client_keeps_no_state_a_call_writes(self):
        launcher = self.cw.Launcher(self.cw.Kube(self.env), self.env, self.clock)
        before = (dict(launcher.kube.env), set(launcher.kube.known), launcher.kube.kubeconfig, launcher.kube.dir)
        self.assertEqual(self.ask(launcher), [])
        self.assertEqual(before, (dict(launcher.kube.env), set(launcher.kube.known), launcher.kube.kubeconfig, launcher.kube.dir))


if __name__ == "__main__":
    unittest.main()
