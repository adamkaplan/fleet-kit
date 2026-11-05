"""Tests of the pod silence watch (ask #2115, bin/pod_silence.py): a fake world, a stub summary, no cluster, no thread."""
import datetime
import json
import tempfile
import time
import types
import unittest
import unittest.mock
from pathlib import Path

from support.loader import sb

REPO = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0
MIN = 60
load_real = sb.load_module


def load(mutant=False, prune_mutant=False):
    text = (REPO / "bin" / "pod_silence.py").read_text()
    applied = False
    if prune_mutant:   # a capped pass prunes too: the told mark of an unread pod is lost
        new = text.replace("    if prune:\n", "    if True:\n", 1)
        applied = new != text
        text = new
    if mutant:   # no once-only state: the told mark is never kept
        new = text.replace('mem["told"] = now\n', 'mem["told"] = None\n', 1)
        applied = new != text
        text = new
    module = types.ModuleType("pod_silence_under_test")
    exec(compile(text, "pod_silence.py", "exec"), module.__dict__)
    return module, applied


def pod(name, report=None, push=None, done=False):
    return {"worker": name, "boss": "lane-a", "last_report": report, "last_push": push, "done": done}


class TestPodSilence(unittest.TestCase):
    def run_passes(self, module, pods, passes=2):
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "s.json")
            first = NOW - 30 * MIN   # first seen 30 minutes ago
            module.scan([dict(p) for p in pods], first, state, lambda to, t: None, lambda n: "x", json.loads)
            for i in range(passes):
                module.scan(pods, NOW + i, state, lambda to, t: sent.append((to, t)), lambda n: "stub summary of " + n, json.loads)
        return sent

    def test_the_four_cases_and_a_second_pass(self):
        module, _ = load()
        pods = [pod("silent"), pod("pushed", push=NOW - 5 * MIN), pod("reported", report=NOW - 5 * MIN), pod("finished", report=NOW - 25 * MIN, done=True)]
        sent = self.run_passes(module, pods, passes=2)
        self.assertEqual([to for to, _ in sent], ["lane-a"])
        self.assertIn("silent", sent[0][1])
        self.assertIn("stub summary of silent", sent[0][1])

    def test_a_retired_pod_is_not_told(self):
        module, _ = load()
        self.assertEqual(self.run_passes(module, [], passes=1), [])

    def test_mutant_without_the_once_only_state_sends_two(self):
        control, applied0 = load()
        self.assertFalse(applied0)
        self.assertEqual(len(self.run_passes(control, [pod("silent")], 2)), 1)   # control first
        mutant, applied = load(mutant=True)
        self.assertTrue(applied)   # applied: computed from the two texts
        self.assertNotEqual(len(self.run_passes(mutant, [pod("silent")], 2)), 1)   # a no-op mutant would fail here
        self.assertEqual(len(self.run_passes(mutant, [pod("silent")], 2)), 2)



def _when(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestPodSilenceTick(unittest.TestCase):
    """The wiring of pod_silence_tick (bin/fleet-switchboard): a stub engine, a stub summary; records are dated by the real clock."""

    def world(self, records, capped=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sent, self.reads = [], []
        return types.SimpleNamespace(state_dir=Path(tmp.name), cloud_watch={"records": records, "capped": capped},
                                     send=lambda frm, to, text: self.sent.append((to, text)))

    def summary(self, name):
        self.reads.append(name)
        return "stub of " + name

    def rec(self, name, **extra):
        base = {"worker": name, "reports_to": "lane-a", "hard_deadline": _when(time.time() + 7200)}
        base.update(extra)
        return base

    def tick(self, engine, ahead=0):
        real = time.time
        with unittest.mock.patch.object(sb.time, "time", lambda: real() + ahead):
            return sb.pod_silence_tick(engine, None, self.summary)

    def test_a_live_pod_is_told_after_the_quiet_period_and_the_boss_is_cleaned(self):
        engine = self.world([self.rec("live", reports_to="lane\x07-a")])
        self.assertEqual(self.tick(engine), [])
        got = self.tick(engine, ahead=21 * MIN)
        self.assertEqual([to for to, _ in self.sent], ["lane-a"])   # _cloud_boss removed the control character
        self.assertEqual(len(got), 1)

    def test_failed_retired_and_past_deadline_records_read_and_send_nothing(self):
        engine = self.world([self.rec("failed"), self.rec("gone", retired=True), self.rec("late", hard_deadline=_when(time.time() - 60))])
        (engine.state_dir / "last-reports.json").write_text(json.dumps({"failed": {"state": "failed", "at": 1}}))
        self.tick(engine)
        self.tick(engine, ahead=30 * MIN)
        self.assertEqual(self.reads, [])
        self.assertEqual(self.sent, [])

    def capped_scenario(self, module=None):
        engine = self.world([self.rec("p1"), self.rec("p2")])
        with unittest.mock.patch.object(sb, "load_module", lambda n: module if (n == "pod_silence" and module) else load_real(n)):
            self.tick(engine)
            self.tick(engine, ahead=21 * MIN)
            self.assertEqual(len(self.sent), 2)
            full = engine.cloud_watch["records"]
            engine.cloud_watch = {"records": [full[1]], "capped": True}   # p1 not read this pass
            self.tick(engine, ahead=22 * MIN)
            engine.cloud_watch = {"records": full, "capped": True}
            self.tick(engine, ahead=23 * MIN)
            self.tick(engine, ahead=50 * MIN)   # past the 20 minutes: without the told mark p1 WOULD be told again
        return [text for _, text in self.sent]

    def test_a_capped_pass_that_omits_a_told_pod_does_not_tell_it_again(self):
        texts = self.capped_scenario()
        self.assertEqual(len(texts), 2, "the told pod was told again: %r" % texts[2:])

    def test_mutant_pruning_on_a_capped_pass_tells_the_omitted_pod_again(self):
        self.assertEqual(len(self.capped_scenario()), 2)   # control first
        control_sent = list(self.sent)
        mutant, applied = load(prune_mutant=True)
        self.assertTrue(applied)
        self.sent = []
        texts = self.capped_scenario(mutant)
        self.assertEqual(len(control_sent), 2)
        self.assertEqual(len(texts), 3)   # a no-op mutant would send 2
        self.assertIn("stub of p1", texts[2])

    def prune_false_run(self, module):
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "s.json")
            sent = []
            module.scan([pod("a")], NOW - 30 * MIN, state, lambda to, t: None, lambda n: "x", json.loads)
            module.scan([pod("a")], NOW, state, lambda to, t: sent.append(t), lambda n: "x of a", json.loads)
            module.scan([pod("b")], NOW + 1, state, lambda to, t: None, lambda n: "x", json.loads, prune=False)
            module.scan([pod("a")], NOW + 2, state, lambda to, t: sent.append(t), lambda n: "x of a", json.loads)
            module.scan([pod("a")], NOW + 2 + 21 * MIN, state, lambda to, t: sent.append(t), lambda n: "x of a", json.loads)   # past the 20 minutes
        return sent

    def test_scan_prune_false_keeps_the_told_mark(self):
        module, _ = load()
        sent = self.prune_false_run(module)
        self.assertEqual(len(sent), 1, "the told pod was told again: %r" % sent[1:])

    def test_mutant_pruning_despite_prune_false_tells_again(self):
        control, applied0 = load()
        self.assertFalse(applied0)
        self.assertEqual(len(self.prune_false_run(control)), 1)   # control first
        mutant, applied = load(prune_mutant=True)
        self.assertTrue(applied)
        sent = self.prune_false_run(mutant)
        self.assertEqual(len(sent), 2)   # a no-op mutant would send 1
        self.assertIn("x of a", sent[1])


if __name__ == "__main__":
    unittest.main()
