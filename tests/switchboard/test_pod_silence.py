"""Tests of the pod silence watch (ask #2115, bin/pod_silence.py): a fake world, a stub summary, no cluster, no thread."""
import json
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0
MIN = 60


def load(mutant=False):
    text = (REPO / "bin" / "pod_silence.py").read_text()
    applied = False
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


if __name__ == "__main__":
    unittest.main()
