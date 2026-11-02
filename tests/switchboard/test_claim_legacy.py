"""A merged sibling merged before the claim ledger existed is legacy: read as a sibling with no claim line (ask #1690).

The fixtures are those of TestClaimUnionAtLastMerge in bin/test-switchboard (HOST is that module, set by its loader line). Neutral names (acme).
"""
import re
import sys
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOST = None   # set by bin/test-switchboard
CUTOFF = 1793310807
INSTANT = "2026-10-29T21:53:27Z"
ISO = "%Y-%m-%dT%H:%M:%SZ"


def iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(ISO)


class TestClaimLegacy(unittest.TestCase):
    def setUp(self):
        if HOST is None:
            self.skipTest("the fixtures are those of bin/test-switchboard")
        self.sb = HOST.sb
        self.fx = HOST.TestClaimUnionAtLastMerge("test_a_merge_that_is_not_the_asks_last_is_not_refused")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.body("S1")

    def judge(self, merged_at, claim="S2 U1"):
        """The merge gate's refusal for a last pull request claiming S1, with ONE merged sibling (no recorded claim) merged at `merged_at`."""
        self.fx.merged_iso = lambda after=0: merged_at
        return self.fx.refusal([("merged", claim)], record=False)

    def test_s2_a_legacy_sibling_passes_without_a_waiver_and_counts_as_no_claim_line(self):
        self.assertIsNone(self.judge(iso(CUTOFF - 86400 * 30)))   # no claim line: it claims S3 too, so nothing is left uncovered
        self.assertEqual([e for e in self.fx.ledger() if e.get("kind") == "waiver"], [])

    def test_s2_a_recorded_claim_is_kept_for_a_sibling_merged_before_the_cutoff(self):
        self.fx.merged_iso = lambda after=0: iso(self.fx.world.clock.now() + after)
        self.assertIn("S3", self.fx.refusal([("merged", "S2 U1")]))   # a recorded claim narrows: S3 stays uncovered

    def test_the_shape_of_1675_a_sibling_merged_on_25_october_with_no_recorded_claim_passes_with_no_waiver(self):
        self.assertIsNone(self.judge("2026-10-25T17:57:00Z"))
        self.assertIn("no recorded claim", self.judge("2026-10-30T00:00:00Z"))   # the same shape after the cutoff is still refused

    def test_s3_after_the_cutoff_or_unreadable_the_sibling_is_still_unknown(self):
        for when in (iso(CUTOFF + 86400 * 30), "not a time", "2026-10-29"):
            why = self.judge(when)
            self.assertIsNotNone(why, when)
            self.assertIn("no recorded claim", why, when)

    def test_s4_the_boundary_is_at_or_before(self):
        self.assertIsNone(self.judge(iso(CUTOFF)))
        self.assertIsNone(self.judge(iso(CUTOFF - 1)))
        self.assertIn("no recorded claim", self.judge(iso(CUTOFF + 1)))

    def test_s5_the_cutoff_is_defined_once_read_by_one_rule_and_documented(self):
        src = (REPO / "bin" / "fleet-switchboard").read_text()
        self.assertEqual(len(re.findall(r"(?m)^CLAIM_LEDGER_SINCE = %d\b.*#" % CUTOFF, src)), 1)
        self.assertEqual(len(re.findall(r"CLAIM_LEDGER_SINCE", src)), 2)   # the definition and the one reading rule
        self.assertEqual(src.count(str(CUTOFF)), 1)
        self.assertEqual(self.sb.CLAIM_LEDGER_SINCE, CUTOFF)
        self.assertEqual(iso(CUTOFF), INSTANT)
        docs = (REPO / "docs" / "switchboard.md").read_text()
        self.assertEqual(docs.count(INSTANT), 1)
        self.assertEqual(docs.count(str(CUTOFF)), 1)

    def checks(self):
        self.test_s2_a_legacy_sibling_passes_without_a_waiver_and_counts_as_no_claim_line()
        self.test_s3_after_the_cutoff_or_unreadable_the_sibling_is_still_unknown()
        self.test_s4_the_boundary_is_at_or_before()

    def test_s6_two_mutants_are_each_applied_and_caught(self):
        self.checks()
        for name, value in (("the cutoff ignored", float("inf")), ("the cutoff 30 days too late", CUTOFF + 86400 * 30)):
            with mock.patch.object(self.sb, "CLAIM_LEDGER_SINCE", value):
                self.assertEqual(self.sb.CLAIM_LEDGER_SINCE, value, name)   # applied
                with self.assertRaises(AssertionError, msg=name):
                    self.checks()

    def test_s6_three_source_copy_mutants_are_each_applied_once_and_caught_by_the_check_naming_the_rule(self):
        """The entry is copied with ONE replaced text (asserted to occur once); the copy's plan_peers is the one the gate runs."""
        import importlib.machinery
        import importlib.util
        import shutil
        import tempfile
        from tests.switchboard.support.loader import copy_entry
        rule = "if not found and merged is not None and merged <= CLAIM_LEDGER_SINCE:"
        mutants = (
            ("the rule deleted", rule, "if False:", "has no recorded claim at or before its merge"),
            ("the cutoff ignored", "merged <= CLAIM_LEDGER_SINCE:", "merged <= CLAIM_LEDGER_SINCE or True:", "unexpectedly None"),
            ("the cutoff 30 days late", "CLAIM_LEDGER_SINCE = %d " % CUTOFF, "CLAIM_LEDGER_SINCE = %d " % (CUTOFF + 2592000), "unexpectedly None"),
        )
        for name, old, new, anchor in mutants:
            with self.subTest(name):
                directory = Path(tempfile.mkdtemp(prefix="legacy-mutant-"))
                self.addCleanup(shutil.rmtree, str(directory), True)
                for module in (REPO / "bin").glob("*.py"):
                    shutil.copy(str(module), str(directory / module.name))
                copy_entry(directory)
                text = (directory / "fleet-switchboard").read_text()
                self.assertEqual(text.count(old), 1, "the mutant applies exactly once")
                for label, source in (("control", text), (name, text.replace(old, new, 1))):
                    (directory / "fleet-switchboard").write_text(source)
                    loader = importlib.machinery.SourceFileLoader("sb_legacy_%s_%d" % (label.replace(" ", "_"), id(directory)), str(directory / "fleet-switchboard"))
                    copy = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
                    with mock.patch.object(sys, "dont_write_bytecode", True):   # a cached bytecode of the same size and second would hide the mutant
                        loader.exec_module(copy)
                    copy.ClaimUnknown = self.sb.ClaimUnknown   # the gate catches the host's class
                    with mock.patch.object(self.sb, "plan_peers", copy.plan_peers):
                        if label == "control":
                            self.checks()
                            continue
                        with self.assertRaises(AssertionError) as caught:
                            self.checks()
                        self.assertIn(anchor, str(caught.exception), name)

    def test_s7_the_docs_state_the_cutoff_its_reason_and_the_two_cases(self):
        docs = (REPO / "docs" / "switchboard.md").read_text()
        item = [p for p in docs.split("\n- **") if p.startswith("A sibling merged before the claim ledger")]
        self.assertEqual(len(item), 1)
        text = item[0].split("\n- **")[0]
        for part in (INSTANT, str(CUTOFF), "first `claim` line", "no claim line", "CU waiver"):
            self.assertIn(part, text)
        self.assertNotIn("A sibling merged before this feature", docs)
