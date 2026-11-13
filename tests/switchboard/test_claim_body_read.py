"""A merged sibling with no recorded claim reads the CLAIMS line of its own body as merged (ask #2465).

Scenarios S2 to S5 of the plan. The fixtures are those of TestClaimUnionAtLastMerge in bin/test-switchboard (found by file name, as test_claim_hand_merge.py does);
the GraphQL read of the sibling's own body is answered here. Neutral names (acme). Plan: per pull request S1, S2, S3, U1.
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
MERGED = "2026-11-12T02:11:10Z"
BEFORE = "2026-11-12T02:00:00Z"
AFTER = "2026-11-12T02:11:23Z"


def _host():
    for module in list(sys.modules.values()):
        if str(getattr(module, "__file__", "") or "").endswith("test-switchboard") and hasattr(module, "TestClaimUnionAtLastMerge"):
            return module
    return None


def body(claims):
    return "PLAN: acme/app#7\n" + ("CLAIMS: %s\n" % claims if claims is not None else "") + "canon: updated the docs"


class TestClaimBodyRead(unittest.TestCase):
    def setUp(self):
        self.host = _host()
        if self.host is None:
            self.skipTest("the fixtures are those of bin/test-switchboard")
        self.sb = self.host.sb
        self.fx = self.host.TestClaimUnionAtLastMerge("test_a_merge_that_is_not_the_asks_last_is_not_refused")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.body("S1")   # the pull request under judgement claims S1; the sibling (#100, merged at MERGED, no ledger claim) must cover the rest
        self.fx.merged_iso = lambda after=0: MERGED
        self.asked = []

    def gate(self, claims="S2 U1 S3", edited=None, merged_at=MERGED, raw=None, drop=None, fail=False):
        """The merge gate's refusal for the last pull request, with ONE hand-merged sibling whose own body (read by GraphQL) says `claims`."""
        pull = {"body": body(claims), "mergedAt": merged_at, "lastEditedAt": edited}
        pull.update(raw or {})
        for key in drop or ():
            del pull[key]
        real = self.fx.route
        def route(argv, **kw):
            if "graphql" in argv:
                self.asked.append(list(argv))
                if fail:
                    return subprocess.CompletedProcess(argv, 1, "", "boom")
                return subprocess.CompletedProcess(argv, 0, json.dumps({"data": {"repository": {"pullRequest": pull}}}), "")
            return real(argv, **kw)
        self.fx.route = route
        try:
            return self.fx.refusal([("merged", "S2")], record=False)
        finally:
            self.fx.route = real

    def test_s2_a_hand_merged_sibling_with_a_valid_line_is_counted_and_one_with_no_line_fails_closed(self):
        self.assertIsNone(self.gate("S2 U1 S3"))   # counted: S1 + S2 S3 U1 cover the plan, no waiver
        self.assertEqual([e for e in self.fx.ledger() if e.get("kind") == "waiver"], [])
        why = self.gate("S2 U1")   # counted as written, not as 'every scenario': S3 stays uncovered
        self.assertIsNotNone(why)
        self.assertIn("S3", why)
        self.assertIn("no recorded claim", self.gate(None) or "")   # no CLAIMS line: unknown, fails closed
        self.assertIn("no recorded claim", self.gate("S2 U1", fail=True) or "")   # an unreadable body: unknown
        self.assertEqual(self.asked[0][self.asked[0].index("-F") + 1], "number=100")

    def test_s3_an_identifier_the_plan_lacks_or_an_empty_line_fails_closed(self):
        for claims in ("S2 U1 S3 S9", "S9", ""):
            self.assertIn("no recorded claim", self.gate(claims) or "", claims)

    def test_s3_a_claim_line_after_a_blank_line_is_refused_as_in_every_claim_reader(self):
        misplaced = "PLAN: acme/app#7\n\nCLAIMS: S2 U1 S3"   # the line must stand directly under the PLAN line (#1617)
        self.assertIn("no recorded claim", self.gate(raw={"body": misplaced}) or "")

    def test_s3_a_body_edited_after_the_merge_does_not_widen_the_claim(self):
        for edited in (AFTER, MERGED, "not a time", 7):   # after, at the same second, unreadable: the edit is not proven to precede the merge
            self.assertIn("no recorded claim", self.gate("S2 U1 S3", edited=edited) or "", edited)
        self.assertIn("no recorded claim", self.gate("S2 U1 S3", drop=["lastEditedAt"]) or "")   # no edit time in the answer
        self.assertIsNone(self.gate("S2 U1 S3", edited=BEFORE))   # an edit proven to precede the merge reads as merged
        self.assertIn("S3", self.gate("S2 U1", edited=BEFORE) or "")

    def test_s3_a_merge_time_that_differs_or_a_body_that_is_not_text_fails_closed(self):
        self.assertIn("no recorded claim", self.gate(merged_at=BEFORE) or "")
        self.assertIn("no recorded claim", self.gate(raw={"body": None}) or "")
        self.assertIn("no recorded claim", self.gate(raw={"mergedAt": None}) or "")

    def test_s4_a_recorded_ledger_claim_is_preferred_over_the_body(self):
        later = self.host.TestClaimUnionAtLastMerge.merged_iso(self.fx)   # a merge time after the fixture clock, so the claim recorded now is at or before it
        self.fx.merged_iso = lambda after=0: later
        self.fx.record(100, "S2 U1")
        why = self.gate("S2 U1 S3", merged_at=later)   # the body is wider; the ledger claim wins, so S3 stays uncovered
        self.assertIsNotNone(why)
        self.assertIn("S3", why)
        self.assertEqual(self.asked, [])   # the body was not even read

    def test_s5_the_cutoff_is_gone_and_no_pull_request_number_is_in_the_reading(self):
        src = (REPO / "bin" / "fleet-switchboard").read_text()
        self.assertEqual(src.count("CLAIM_HAND_MERGE_UNTIL"), 0)
        self.assertFalse(hasattr(self.sb, "CLAIM_HAND_MERGE_UNTIL"))
        docs = (REPO / "docs" / "switchboard.md").read_text()
        self.assertEqual(docs.count("CLAIM_HAND_MERGE_UNTIL"), 0)
        function = src[src.index("def _merged_body_claim"):src.index("def plan_peers")]
        self.assertEqual(re.findall(r"#\d{3,5}\b(?!\))", re.sub(r"\"\"\".*?\"\"\"", "", function, flags=re.S)), [])   # no number outside the docstring
        self.assertEqual(src.count("CLAIM_BODY_QUERY ="), 1)

    def test_s5_controls_the_gate_without_the_body_reading_refuses_the_same_sibling(self):
        def old(ctx, runner, now, number, merged):   # the gate as it stood: a merged sibling with no recorded claim is unknown
            raise self.sb.ClaimUnknown("merged pull request #%s has no recorded claim at or before its merge: its claim is unknown" % number)
        with mock.patch.object(self.sb, "_merged_body_claim", old):
            self.assertIn("no recorded claim", self.gate("S2 U1 S3") or "")   # the valid line counts for nothing on the current gate: the control

    def test_s5_controls_a_reading_that_trusts_any_line_is_caught(self):
        def trusts_the_line(ctx, runner, now, number, merged):   # no check against the plan
            return "PLAN: x#1\nCLAIMS: S2 U1 S3"
        with mock.patch.object(self.sb, "_merged_body_claim", trusts_the_line):
            with self.assertRaises(AssertionError):
                self.test_s3_an_identifier_the_plan_lacks_or_an_empty_line_fails_closed()
