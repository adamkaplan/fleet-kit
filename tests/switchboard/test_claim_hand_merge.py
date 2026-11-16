"""A merged sibling with no recorded claim, merged by hand, does not block the ask's later pull requests (ask #2454, rewritten for ask #2465: no date cutoff, its own CLAIMS line is read).

The fixtures are those of TestClaimUnionAtLastMerge in bin/test-switchboard, found by file name as test_batch_carry.py does. Neutral names (acme).
"""
import datetime
import json
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UNTIL = 1794412648   # 2026-11-11T15:57:28Z, the removed cutoff: merges on both sides of it now read alike
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

    def judge(self, merged_at, claims="S2 U1 S3", edited=None):
        """The merge gate's refusal for a last pull request claiming S1, with ONE hand-merged sibling (no recorded claim) merged at `merged_at`, whose own body (GraphQL) says `claims` (None: no line)."""
        self.fx.merged_iso = lambda after=0: merged_at
        pull = {"body": "PLAN: acme/app#7\n" + ("CLAIMS: %s\n" % claims if claims is not None else "") + "canon: x", "mergedAt": merged_at, "lastEditedAt": edited}
        real = self.fx.route
        def route(argv, **kw):
            if "graphql" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"data": {"repository": {"pullRequest": pull}}}), "")
            return real(argv, **kw)
        self.fx.route = route
        try:
            return self.fx.refusal([("merged", "S2")], record=False)
        finally:
            self.fx.route = real

    # OLD test_s2_...does_not_block...: assertIsNone(judge("2026-11-10T02:31:43Z")) read the sibling as 'no claim line'. NEW: the same merge time passes because its own CLAIMS line covers the plan.
    def test_s2_a_sibling_merged_by_hand_with_no_recorded_claim_does_not_block_a_later_pull_request(self):
        self.assertIsNone(self.judge("2026-11-10T02:31:43Z"))   # the merge time of #2324
        self.assertEqual([e for e in self.fx.ledger() if e.get("kind") == "waiver"], [])   # no waiver was needed
        self.assertIn("S3", self.judge("2026-11-10T02:31:43Z", claims="S2 U1") or "")   # NEW: the line is what counts, not 'every scenario'

    # OLD test_s3_...after_the_end_of_the_hold...still_blocks: four merge times after the cutoff blocked. NEW: every merge time blocks when the body has no line; none is date-bound.
    def test_s3_a_sibling_with_no_claims_line_still_blocks_at_any_merge_time(self):
        for when in (iso(UNTIL + 1), "2026-11-12T04:00:00Z", "2026-11-10T02:31:43Z", iso(OLD_CUTOFF + 1)):
            why = self.judge(when, claims=None)
            self.assertIsNotNone(why, when)
            self.assertIn("no recorded claim", why, when)
        for when in ("not a time", "2026-11-10"):   # an unreadable merge time is unknown even with a valid line (OLD assertion kept)
            self.assertIn("no recorded claim", self.judge(when) or "", when)

    # OLD test_the_boundary_is_at_or_before...: assertIsNone(judge(UNTIL)) / assertIn(refusal, judge(UNTIL+1)) / CLAIM_HAND_MERGE_UNTIL == UNTIL. NEW: no boundary at the old instant; the older cutoff is unchanged.
    def test_there_is_no_boundary_at_the_old_instant_and_the_older_cutoff_is_unchanged(self):
        self.assertIsNone(self.judge(iso(UNTIL)))
        self.assertIsNone(self.judge(iso(UNTIL + 1)))   # was refused: it now reads its own line
        self.assertIn("no recorded claim", self.judge(iso(UNTIL + 1), claims=None) or "")
        self.assertIsNone(self.judge(iso(OLD_CUTOFF - 1), claims=None))   # the older cutoff: legacy, no claim line (OLD assertion kept)
        self.assertFalse(hasattr(self.sb, "CLAIM_HAND_MERGE_UNTIL"))
        self.assertGreater(UNTIL, self.sb.CLAIM_LEDGER_SINCE)

    def test_a_recorded_claim_of_a_sibling_in_the_window_is_read_as_a_normal_claim(self):
        self.fx.merged_iso = lambda after=0: "2026-11-10T02:31:43Z"   # inside the window (the clock of the fixture is later)
        sb = self.sb   # the claim line is written AT the time of the gate, before the merge (the fixture clock is later than the window)
        sb.plan_ledger.append_checked(self.fx.world.state, 1794000000, lambda have, seq: {"kind": "claim", "pr": "acme/app#100", "head": "a" * 40, "claim_text": "S2 U1"})
        why = self.fx.refusal([("merged", "S2 U1")], record=False)
        self.assertIsNotNone(why)
        self.assertIn("S3", why)   # recorded and narrowing: S3 stays uncovered, not read as no claim line

    # OLD test_s5_control: patched CLAIM_HAND_MERGE_UNTIL to the older cutoff and expected the block. NEW: patches the body reading to the old behaviour (unknown) and expects the block.
    def test_s5_control_without_the_rule_the_sibling_blocks(self):
        def old(ctx, runner, now, number, merged):
            raise self.sb.ClaimUnknown("merged pull request #%s has no recorded claim at or before its merge: its claim is unknown" % number)
        with mock.patch.object(self.sb, "_merged_body_claim", old):
            why = self.judge("2026-11-10T02:31:43Z")
        self.assertIsNotNone(why)
        self.assertIn("no recorded claim", why)

    # OLD test_the_constant_is_defined_once_and_read_by_one_rule: the constant and its rule occurred once. NEW: neither is in the source.
    def test_the_cutoff_constant_and_its_rule_are_gone(self):
        src = (REPO / "bin" / "fleet-switchboard").read_text()
        self.assertEqual(src.count("CLAIM_HAND_MERGE_UNTIL"), 0)
        self.assertEqual(src.count(str(UNTIL)), 0)
        self.assertEqual(iso(UNTIL), "2026-11-11T15:57:28Z")
