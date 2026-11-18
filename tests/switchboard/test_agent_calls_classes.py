"""The hourly lanes show each class the daemon's counter writes under its own label (#1787)."""
import json
import os
import tempfile
import unittest

from support.loader import sb

agent_calls = sb.agent_calls if hasattr(sb, "agent_calls") else __import__("agent_calls")


def hour_lane(delta, now=7200.0):
    with tempfile.TemporaryDirectory() as d:
        agent_calls.fold(d, now, {"acct": delta})
        hours = json.load(open(os.path.join(d, agent_calls.HOURS_FILE)))["hours"]
    bucket = next(iter(hours.values()))
    return bucket["daemon"]["acct"], bucket["lanes"]["daemon"]["acct"]


class TestClassLabels(unittest.TestCase):
    def test_each_class_keeps_its_own_label_and_the_total_is_unchanged(self):
        total, lane = hour_lane({"api repo": 5, "api merge-gate": 3, "api unknown": 2})
        self.assertEqual(total, 10)
        self.assertEqual(sum(lane.values()), 10)
        self.assertEqual(lane, {"api repo": 5, "api merge-gate": 3, "api unknown": 2},
                         "merge-gate and unknown are folded into repo: each class must show under its own label")

    def test_a_class_never_written_is_reduced_without_raising(self):
        total, lane = hour_lane({"api never-written-class": 4})
        self.assertEqual((total, lane), (4, {"api repo": 4}))


if __name__ == "__main__":
    unittest.main()
