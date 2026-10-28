"""Ask #1480: every stop the launcher makes of its own accord carries `cause` (idle, hard, pod_gone, pod_failed); a stop by a person's call carries none.
The vanished-pod check of the reaper's pass writes ONE stop line per vanished worker. Offline: the fake cluster of test_cloud_worker."""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as tcw  # noqa: E402

cw = tcw.cw
T0 = tcw.T0


class Cause(tcw.Base):
    def reap(self):
        return cw.Launcher(cw.Kube(self.env), self.env, self.clock).reap()

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(cw.Launcher, "run_hook", lambda launcher, name: {"pushed": False, "wip_branch": None})
        patch.start()
        self.addCleanup(patch.stop)

    def hook(self):
        pass

    def mutant(self, old, new):
        """The launcher module with `old` replaced by `new` (asserted applied, once)."""
        import types
        with open(cw.__file__) as f:
            text = f.read()
        self.assertEqual(text.count(old), 1, "the mutation did not apply")
        mod = types.ModuleType("cw_mutant")
        mod.__file__ = cw.__file__
        sys.modules["cw_mutant"] = mod
        self.addCleanup(sys.modules.pop, "cw_mutant", None)
        exec(compile(text.replace(old, new), "cw_mutant", "exec"), mod.__dict__)
        mod.Launcher.run_hook = lambda launcher, name: {"pushed": False, "wip_branch": None}
        return mod

    def stops(self):
        return [l for l in self.audit_lines() if l["action"] == "stop"]

    def lose_pod(self, worker="coder7"):
        st = self.st()
        st["pods"].pop(cw.pod_name(worker))
        self.write_state(st)

    def test_s11_the_reaper_idle_stop_carries_idle(self):
        self.create()
        self.hook()
        self.set_activity("coder7", False, self.clock())
        self.clock.advance(minutes=12)
        self.assertEqual([r[:2] for r in self.reap()], [("coder7", "idle")])
        stops = self.stops()
        self.assertEqual([(l["cause"], l["ok"]) for l in stops], [("idle", True)])
        self.assertTrue(stops[0]["reason"].startswith("idle"))

    def test_s11_the_reaper_hard_stop_carries_hard(self):
        self.create("coder7", "--hard", "1h")
        self.hook()
        self.set_activity("coder7", True, T0)
        self.clock.advance(minutes=61)
        self.assertEqual([r[:2] for r in self.reap()], [("coder7", "hard limit")])
        stops = self.stops()
        self.assertEqual([l["cause"] for l in stops], ["hard"])
        self.assertTrue(stops[0]["reason"].startswith("hard limit"))

    def test_s11_a_retire_of_a_gone_pod_carries_pod_gone(self):
        self.create()
        self.lose_pod()
        code, out, err = self.run_cli("stop", "coder7", "--reason", "retire")
        self.assertEqual(code, 0, err)
        self.assertIs(json.loads(out).get("pod_gone"), True)
        self.assertEqual([l["cause"] for l in self.stops()], ["pod_gone"])

    def test_s11_a_failed_pod_carries_pod_failed(self):
        self.create("coder7", "--hard", "1h")
        st = self.st()
        st["pods"][cw.pod_name("coder7")].setdefault("status", {})["phase"] = "Failed"
        self.write_state(st)
        self.clock.advance(minutes=61)
        self.reap()
        self.assertEqual([l["cause"] for l in self.stops()], ["pod_failed"])

    def test_s11_a_persons_stop_by_name_carries_no_cause(self):
        self.create()
        with mock.patch.object(cw.Launcher, "run_hook", lambda self, name: {"pushed": False, "wip_branch": None}):
            code, out, err = self.run_cli("stop", "coder7", "--reason", "idle")
        self.assertEqual(code, 0, err)
        stops = self.stops()
        self.assertEqual(len(stops), 1)
        self.assertNotIn("cause", stops[0])
        self.assertTrue(stops[0]["reason"].startswith("idle"))

    def test_s11_a_failed_end_hook_still_names_the_cause(self):
        self.create("coder7", "--hard", "1h")
        self.set_activity("coder7", True, T0)
        self.clock.advance(minutes=61)
        with mock.patch.object(cw.Launcher, "run_hook", lambda self, name: {"pushed": False, "wip_branch": None, "error": "no remote"}):
            self.reap()
        self.assertEqual([(l["cause"], l["ok"]) for l in self.stops()], [("hard", False)])

    def test_s11_a_vanished_pod_is_written_once_over_three_passes(self):
        self.create()
        self.lose_pod()
        for _ in range(3):
            self.reap()
        stops = self.stops()
        self.assertEqual(len(stops), 1)
        self.assertEqual((stops[0]["cause"], stops[0]["ok"], stops[0]["worker"], stops[0]["runtime_id"], stops[0]["reason"]),
                         ("pod_gone", True, "coder7", "pod/" + cw.pod_name("coder7"), "pod gone; no stop was recorded"))

    def test_s11_a_present_pod_writes_nothing(self):
        self.create()
        for _ in range(3):
            self.reap()
        self.assertEqual(self.stops(), [])

    def test_s11_a_worker_with_a_later_stop_line_writes_nothing_more(self):
        self.create()
        with mock.patch.object(cw.Launcher, "run_hook", lambda self, name: {"pushed": False, "wip_branch": None}):
            self.run_cli("stop", "coder7", "--reason", "done")
        self.assertIsNone(self.pod())
        self.reap()
        self.reap()
        stops = self.stops()
        self.assertEqual(len(stops), 1)
        self.assertNotIn("cause", stops[0])

    def test_s11_a_create_older_than_a_day_is_not_noticed(self):
        self.create()
        self.lose_pod()
        self.clock.advance(hours=25)
        self.reap()
        self.assertEqual(self.stops(), [])

    def test_s11_a_cluster_that_cannot_be_read_writes_nothing(self):
        self.create()
        self.lose_pod()
        self.patch_state(fail_cmd="get")
        with self.assertRaises(Exception):
            self.reap()
        self.assertEqual(self.stops(), [])

    def test_m7_a_mutant_that_writes_the_cause_for_two_of_four_stops_is_caught(self):
        import argparse
        mod = self.mutant('info["cause"] = "pod_gone"\n                if nonce:', 'if nonce:')
        self.create()
        self.lose_pod()
        mod.Launcher(mod.Kube(self.env), self.env, self.clock).stop(argparse.Namespace(worker="coder7", reason="retire", run_id=None))
        self.assertNotIn("cause", self.stops()[0])   # the real code writes pod_gone

    def test_m8_a_vanished_pass_that_writes_on_every_pass_is_caught(self):
        mod = self.mutant('elif rec.get("action") in ("stop", "delete") and rid in open_runs:', 'elif False:')
        self.create()
        self.lose_pod()
        for _ in range(3):
            mod.Launcher(mod.Kube(self.env), self.env, self.clock).reap()
        self.assertEqual(len(self.stops()), 3)   # the real code writes 1


if __name__ == "__main__":
    unittest.main()
