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


class TotalDeadline(launcher_tests.Base):
    """S13: the read for all workers has ONE total deadline (STATUS_TOTAL_SECONDS), under the switchboard's call cap; no record is dropped."""
    SLEEP = 0.3
    pods, launcher, ask = LauncherRecords.pods, LauncherRecords.launcher, LauncherRecords.ask

    def setUp(self):
        super().setUp()
        self.cw = launcher_tests.cw
        self.calls = []
        self.lock = threading.Lock()

    def hanging(self, count):
        pods = self.pods(count)
        launcher = self.launcher(pods)
        release = threading.Event()
        self.addCleanup(release.set)

        def activity(name):
            release.wait(60)   # far past every bound
            return False, launcher_tests.T0
        launcher.activity = activity
        return pods, launcher

    def check_total(self, count=300, remove_total=False):
        pods, launcher = self.hanging(count)
        answer = []
        with mock.patch.object(self.cw, "STATUS_POD_SECONDS", 0.2), mock.patch.object(self.cw, "STATUS_TOTAL_SECONDS", 1e9 if remove_total else 2):
            if remove_total:
                self.applied = True   # the mutant: each pod bounded alone, no total deadline
            started = time.monotonic()
            thread = threading.Thread(target=lambda: answer.append(self.ask(launcher)), daemon=True)
            thread.start()
            thread.join(3)
            took = time.monotonic() - started
        print("launcher %d hanging pods took %.2f s" % (count, took), file=sys.stderr)
        self.assertTrue(answer, "no answer within 2 + 1 s")
        records = answer[0]
        self.assertEqual([r["worker"] for r in records], ["w%d" % i for i in range(count)])
        self.assertGreaterEqual(len([r for r in records if r["idle_since"] is None and r["deadlines"]["idle"] is None]), 1)
        for record, pod in zip(records, pods):
            self.assertEqual(list(record), ["worker", "runtime_id", "phase", "idle_since", "deadlines", "cost_estimate_usd"])
            self.assertEqual(record["phase"], "Running")
            self.assertEqual(record["deadlines"]["hard"], pod["metadata"]["annotations"]["cw/hard-deadline"])
        return took

    def test_s13_three_hundred_hanging_pods_answer_within_the_total_deadline_with_every_record_in_order(self):
        self.assertLess(self.check_total(), 3)

    def test_s13_a_mutant_without_the_total_deadline_fails_the_three_hundred_pod_test(self):
        self.applied = False
        with self.assertRaises(AssertionError):
            self.check_total(remove_total=True)
        self.assertTrue(self.applied, "the mutation was not applied")

    def test_s13_the_total_deadline_is_a_named_constant_below_the_switchboard_call_cap(self):
        self.assertEqual(self.cw.STATUS_TOTAL_SECONDS, 40)


if __name__ == "__main__":
    unittest.main()
