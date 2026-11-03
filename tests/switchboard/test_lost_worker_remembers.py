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
        lane = r.engine.cloud_watch["records"][0]["reports_to"]   # the lane of the loss, from the fixture
        expected = len(dict.fromkeys([x for x in [lane, "chief-of-staff"] if x]))   # as _deliver: deduplicated, None skipped
        self.assertEqual(one, expected, WHY)
        self.assertEqual(len(r.engine.sent), one, WHY)

    # S4: the S2 check run against a loader; the mutants are textual replacements in a COPY of bin/lost_worker.py (REAL detector copy, real state file; FAKE engine/clock).
    @staticmethod
    def _s2(loader):
        r = Rig(loader())
        r.created("w1")
        first = loader().run_pass(r.sb, r.engine, NOW, r.alive, r.launcher)
        assert len(first) == 1, WHY
        assert len(keys(r)) == 1, WHY + ": the key is not in the saved table after pass 1"
        second = loader().run_pass(r.sb, r.engine, NOW + 60, r.alive, r.launcher)
        assert len(second) == 0, WHY

    @staticmethod
    def _copy_loader(tmp, new_for):
        import importlib.util
        import pathlib
        src = pathlib.Path(__file__).resolve().parents[2] / "bin" / "lost_worker.py"
        text = src.read_text()
        text = new_for(text)
        path = os.path.join(tmp, "lost_worker.py")
        with open(path, "w") as handle:
            handle.write(text)

        def loader():
            spec = importlib.util.spec_from_file_location("lost_worker_copy", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        return loader

    def test_control_the_unmutated_copy_passes_the_check(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._s2(self._copy_loader(tmp, lambda t: t))

    def test_each_mutant_fails_the_check_with_the_anchored_text(self):
        import tempfile
        marker = '    alerted = state.setdefault("alerted", {})   # #2017:'
        for name in ("M1", "M2"):
            def mutate(text, name=name):
                lines = text.split("\n")
                hits = [l for l in lines if l.startswith(marker)]
                old = hits[0] + "\n"
                self.assertEqual(text.count(old), 1)
                return text.replace(old, "" if name == "M1" else "    alerted = {}\n")
            with tempfile.TemporaryDirectory() as tmp:
                loader = self._copy_loader(tmp, lambda t: t)
                self._s2(loader)   # control first
                loader = self._copy_loader(tmp, mutate)
                with self.assertRaises(AssertionError) as ctx:
                    self._s2(loader)
                self.assertIn(WHY, str(ctx.exception), name)


if __name__ == "__main__":
    unittest.main()
