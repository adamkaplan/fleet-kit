#!/usr/bin/env python3
"""The launcher's stop of a pod in the Failed phase deletes it, whatever its kept mark (ask #1619, scenario S9, and the old shape of S2).

Offline, against the fake kubectl beside this file; the end hook is counted, not run.
Run directly: python3 cloud/launcher/tests/test_stop_failed_pod.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_k1 as k1  # noqa: E402
import test_cloud_worker as base  # noqa: E402
cw = base.cw


class Failed(k1.K1Base):
    def setUp(self):
        super().setUp()
        self.hooks = []
        real = cw.Launcher.run_hook
        cw.Launcher.run_hook = lambda launcher, name: self.fake_hook(launcher, name)
        self.addCleanup(setattr, cw.Launcher, "run_hook", real)

    def fake_hook(self, launcher, name):
        self.hooks.append(name)
        return {"pushed": False, "wip_branch": None, "note": "nothing to push"}

    def made(self, worker, phase, kept):
        self.assertEqual(self.create_with_secrets(worker)[0], 0)
        st = self.st()
        pod = st["pods"][cw.pod_name(worker)]
        pod["status"]["phase"] = phase
        if kept:
            pod["metadata"]["annotations"]["cw/kept"] = "true"
        self.write_state(st)

    def remaining(self):
        return [name for name, pod in self.st()["pods"].items() if pod["status"]["phase"] == "Failed"]


class TestRetireFailedPod(Failed):
    def test_s9_retires_as_stated(self):
        self.made("worker-a", "Failed", kept=False)
        self.made("worker-b", "Failed", kept=True)
        self.made("coder7", "Running", kept=False)
        done = 0
        for worker in ("worker-a", "worker-b"):
            code, out, err = self.run_cli("stop", worker)
            done += code == 0 and "pod was Failed; end hook not run; deleted" in err and self.pod(worker) is None and cw.pod_name(worker) not in self.hooks
        code, out, err = self.run_cli("stop", "coder7")   # a running pod is stopped as before: its end hook runs, then the pod goes
        done += code == 0 and self.hooks == [cw.pod_name("coder7")] and self.pod("coder7") is None
        print("S9: retires as stated %d of 3, Failed pods remaining %d" % (done, len(self.remaining())))
        self.assertEqual(done, 3)
        self.assertEqual(self.remaining(), [])
        self.assertEqual(self.st()["secrets"], {})

    def test_the_kept_mark_does_not_keep_a_succeeded_pod_either(self):
        self.made("coder7", "Succeeded", kept=True)
        self.assertEqual(self.run_cli("stop", "coder7")[0], 0)
        self.assertIsNone(self.pod())
        self.assertEqual(self.hooks, [])

    def test_a_kept_pod_that_is_running_still_runs_the_end_hook(self):
        self.made("coder7", "Running", kept=True)
        self.assertEqual(self.run_cli("stop", "coder7")[0], 0)
        self.assertEqual(self.hooks, [cw.pod_name("coder7")])


class Probe(Failed):
    def fake_hook(self, launcher, name):   # a dead container: nothing can run in it
        raise cw.CloudError("container not found")

    def test_probe_kept_failed_pod(self):   # run only in a scratch copy of the code before the change
        self.made("coder7", "Failed", kept=True)
        self.run_cli("stop", "coder7")
        print("PROBE remaining=%d" % len(self.remaining()))


class TestOldShape(unittest.TestCase):
    def test_s2_the_old_stop_leaves_a_kept_failed_pod(self):
        import test_launch_idempotent as li
        old = ("cloud/launcher/cloud_worker.py", 'if phase in ("Failed", "Succeeded"):   # whatever',
               'if phase in ("Failed", "Succeeded") and ann(pod, "kept") != "true":   # whatever')
        applied, status, out = li.run_child(self, "test_stop_failed_pod.py", "Probe.test_probe_kept_failed_pod", [old])
        shown = "PROBE remaining=1" in out
        print("S2: Failed kept pod remaining after retire %d (the launcher before the change, scratch copy; mutation applied %d)" % (shown, applied))
        self.assertEqual(applied, 1)
        self.assertTrue(shown, out[-800:])


if __name__ == "__main__":
    unittest.main()
