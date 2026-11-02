"""#2017: a loss alerted in one pass is remembered by a fresh load of the module (the way the daemon loads bin/lost_worker.py at every pass).
REAL: bin/lost_worker.py (loaded afresh by path), its state file lost-workers.json, and `lost_worker_tick` of bin/fleet-switchboard.
FAKE: the engine's send, its audit object, the clock, the launcher audit file."""
import json
import os
import unittest
from unittest import mock

from support.loader import sb
from test_lost_worker import NOW, Rig, load

WHY = "a loss alerted in one pass is remembered by a fresh load of the module"


def keys(rig):
    with open(os.path.join(rig.dir, "lost-workers.json")) as handle:
        return [k for k in json.load(handle).get("alerted", {}) if k.startswith("pod_no_stop:")]


class TestLostWorkerRemembers(unittest.TestCase):
    def test_a_fresh_load_alerts_nothing_for_a_loss_the_pass_before_alerted(self):
        r = Rig(load())
        r.created("w1")
        first = load().run_pass(r.sb, r.engine, NOW, r.alive, r.launcher)
        self.assertEqual(len(first), 1, WHY)
        self.assertEqual(len(keys(r)), 1, WHY + ": the key is not in the saved table after pass 1")
        second = load().run_pass(r.sb, r.engine, NOW + 60, r.alive, r.launcher)   # a new module load: no _PROC_ALERTED
        self.assertEqual(len(second), 0, WHY)

    def test_the_real_tick_called_twice_sends_nothing_the_second_time(self):
        r = Rig(load())
        r.created("w1")
        r.engine.state_dir = r.dir
        r.engine.now = lambda: NOW
        with mock.patch.dict(os.environ, {"CW_CLOUD_AUDIT": r.launcher}):
            sb.lost_worker_tick(r.engine, None)
            one = len(r.engine.sent)
            sb.lost_worker_tick(r.engine, None)
        self.assertGreaterEqual(one, 1, WHY)
        self.assertEqual(len(r.engine.sent), one, WHY)


if __name__ == "__main__":
    unittest.main()
