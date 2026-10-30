"""The cloud branch head message tests of #1407, moved out of bin/test-switchboard (its line ceiling)."""
import tempfile
import types
import unittest
from support.loader import sb


class TestCloudBranchHeadTold(unittest.TestCase):
    """#1407: the finding table names the sender of the cloud branch head message; the real sender tells the lane once per new head."""

    def test_a_pushed_head_tells_the_lane_once_and_the_same_head_again_tells_nobody(self):
        with tempfile.TemporaryDirectory() as state:
            old, new = "a" * 40, "b" * 40
            text = sb.cloud_push_told(state, "pod-1", "work/one", "lane-a", old, new, 1000.0)
            self.assertEqual(text, "pod pod-1 pushed work/one head bbbbbbbb")
            self.assertEqual([r.get("text") if isinstance(r, dict) else r for r in sb.read_reminders(state)][-1:], [text])
            self.assertIsNone(sb.cloud_push_told(state, "pod-1", "work/one", "lane-a", old, new, 1001.0))

    def test_a_retired_worker_is_dropped_from_the_workers_whose_branch_is_read_at_the_next_read(self):
        with tempfile.TemporaryDirectory() as state:
            reg = sb.load_module("registry")
            reg.upsert(state, "pod-1", {"session": "ses_pod-1", "role": "coder"})
            discovery = types.SimpleNamespace(problems=[], agents={"lane-a": types.SimpleNamespace(fleet={"role": "orchestrator"})})
            record = {"worker": "pod-1", "repo": "acme/widget", "branch": "work/one", "reports_to": "lane-a", "hard_deadline": "2099-01-01T00:00:00Z"}
            self.assertEqual([w[0] for w in sb._cloud_branch_workers(state, discovery, [record], 1000.0)], ["pod-1"])
            reg.retire(state, "pod-1", "x", "finished", session="ses_pod-1")
            self.assertEqual(sb._cloud_branch_workers(state, discovery, [record], 1000.0), [])   # retire: the registry's word, read at the next read
