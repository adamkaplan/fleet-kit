"""A merged sibling with no recorded claim, merged by hand before the captain hold ended, does not block the ask's later pull requests (ask #2454).

The fixtures are those of TestClaimUnionAtLastMerge in bin/test-switchboard, found by file name as test_batch_carry.py does. Neutral names (acme).
"""
import datetime
import sys
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UNTIL = 1794412647   # 2026-11-11T15:57:27Z
OLD_CUTOFF = 1793310807
ISO = "%Y-%m-%dT%H:%M:%SZ"


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(ISO)


def _host():
    for module in list(sys.modules.values()):
        if str(getattr(module, "__file__", "") or "").endswith("test-switchboard") and hasattr(module, "TestClaimUnionAtLastMerge"):
            return module
    return None


class TestClaimHandMerge(unittest.TestCase):
    def setUp(self):
        self.host = _host()
        if self.host is None:
            self.skipTest("the fixtures are those of bin/test-switchboard")
        self.sb = self.host.sb
        self.fx = self.host.TestClaimUnionAtLastMerge("test_a_merge_that_is_not_the_asks_last_is_not_refused")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.body("S1")

    def judge(self, merged_at):
        """The merge gate's refusal for a last pull request claiming S1, with ONE merged sibling (no recorded claim; the one of #2324) merged at `merged_at`."""
        self.fx.merged_iso = lambda after=0: merged_at
        return self.fx.refusal([("merged", "S2")], record=False)

    def test_s2_a_sibling_merged_by_hand_with_no_recorded_claim_does_not_block_a_later_pull_request(self):
        self.assertIsNone(self.judge("2026-11-10T02:31:43Z"))   # the merge time of #2324
        self.assertEqual([e for e in self.fx.ledger() if e.get("kind") == "waiver"], [])   # no waiver was needed

    def test_s3_a_sibling_merged_after_the_end_of_the_hold_with_no_recorded_claim_still_blocks(self):
        for when in (iso(UNTIL + 1), "2026-11-12T04:00:00Z", "not a time", "2026-11-10"):
            why = self.judge(when)
            self.assertIsNotNone(why, when)
            self.assertIn("no recorded claim", why, when)

    def test_the_boundary_is_at_or_before_and_the_older_cutoff_is_unchanged(self):
        self.assertIsNone(self.judge(iso(UNTIL)))
        self.assertIn("no recorded claim", self.judge(iso(UNTIL + 1)))
        self.assertIsNone(self.judge(iso(OLD_CUTOFF - 1)))
        self.assertEqual(self.sb.CLAIM_HAND_MERGE_UNTIL, UNTIL)
        self.assertGreater(self.sb.CLAIM_HAND_MERGE_UNTIL, self.sb.CLAIM_LEDGER_SINCE)

    def test_a_recorded_claim_of_a_sibling_in_the_window_is_read_as_a_normal_claim(self):
        self.fx.merged_iso = lambda after=0: iso(self.fx.world.clock.now() + after)
        self.assertIn("S3", self.fx.refusal([("merged", "S2 U1")]))   # recorded and narrowing: S3 stays uncovered, as for any claim

    def test_s5_control_without_the_rule_the_sibling_blocks(self):
        with mock.patch.object(self.sb, "CLAIM_HAND_MERGE_UNTIL", OLD_CUTOFF):   # the gate as it stood: only the older cutoff
            why = self.judge("2026-11-10T02:31:43Z")
        self.assertIsNotNone(why)
        self.assertIn("no recorded claim", why)

    def test_the_constant_is_defined_once_and_read_by_one_rule(self):
        src = (REPO / "bin" / "fleet-switchboard").read_text()
        self.assertEqual(src.count("CLAIM_HAND_MERGE_UNTIL = %d " % UNTIL), 1)
        self.assertEqual(src.count("merged <= CLAIM_HAND_MERGE_UNTIL:"), 1)
        self.assertEqual(src.count(str(UNTIL)), 1)
        self.assertEqual(iso(UNTIL), "2026-11-11T15:57:27Z")
