"""A plan comment has one hash with or without the trailing marker (#1713): S2, S3, S5, S6, S7, S8, S9, S10 and U4 of the plan.

Pure: it reads the bin modules and writes nothing. A mutant is applied to a COPY of the source, asserted applied, and run beside its control."""
import hashlib
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BIN = REPO / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

MARKER = "<!-- fleet-switchboard:from=fleet-registry -->"
CLAUSES = ["first clause is stated here", "second clause is stated here"]
DONE_WHEN = "(1) %s (2) %s" % tuple(CLAUSES)


def plan_text(extra=""):
    lines = "\n".join('  DW-%d: "%s"' % (i, c) for i, c in enumerate(CLAUSES, 1))
    return "## Plan (scenarios)\n\nsize: S\n%s\n\n```yaml\nclauses:\n%s\n```\n" % (extra, lines)


def load(name, path=None, source=None, as_name=None):
    spec = importlib.util.spec_from_file_location(as_name or name, str(path or BIN / (name + ".py")))
    module = importlib.util.module_from_spec(spec)
    if source is None:
        spec.loader.exec_module(module)
    else:
        exec(compile(source, str(path or name), "exec"), module.__dict__)
    return module


def real(name):
    return load(name, as_name="t1713_" + name)


def _ph_mutate(name, old, new):
    """The module `name` built from a source copy with `old` replaced by `new`; asserts the mutation applied (never silently a no-op)."""
    source = (BIN / (name + ".py")).read_text(encoding="utf-8")
    assert source.count(old) == 1, "the mutation target is not found exactly once: %r" % old
    mutated = source.replace(old, new)
    assert mutated != source, "the mutation was not applied"
    return load(name, source=mutated, as_name="t1713m_" + name)


def comments_of(text):
    return [{"id": 1, "body": text}]


class TestOneHashWithOrWithoutTheMarker(unittest.TestCase):
    """S2, S5: the hash of a text is the same in both forms; any other edit changes it."""

    def setUp(self):
        self.pc, self.pa = real("plan_check"), real("plan_approval")
        self.plain = plan_text()
        self.signed = self.plain + "\n" + MARKER + "\n"

    def reads(self, text):
        verdict = self.pa.evaluate(self.pc.plan_hash(text).split(":")[1], comments_of(text), DONE_WHEN, self.pc)
        return [self.pc.plan_hash(text), self.pc.plan_hashes(text)[0], verdict["plan_hash"]]

    def test_six_reads_of_one_text_in_two_forms_are_one_hash(self):
        found = set(self.reads(self.plain) + self.reads(self.signed))
        self.assertEqual(len(found), 1, found)

    def test_the_old_formula_gives_two_hashes_for_the_two_forms(self):
        self.assertNotEqual(self.pc.plan_hash_legacy(self.plain), self.pc.plan_hash_legacy(self.signed))
        self.assertEqual(self.pc.plan_hashes(self.signed), (self.pc.plan_hash(self.signed), self.pc.plan_hash_legacy(self.signed)))
        self.assertEqual(len(self.pc.plan_hashes(self.plain)), 1)

    def test_one_edited_word_and_a_marker_line_elsewhere_change_the_hash_and_whitespace_does_not(self):
        base = self.pc.plan_hash(self.signed)
        edited = self.pc.plan_hash(self.signed.replace("size: S", "size: M"))
        elsewhere = self.pc.plan_hash(plan_text() + MARKER + "\nmore text\n")
        spaced = self.pc.plan_hash(self.signed.replace("size: S", "size:   S"))
        self.assertEqual([edited != base, elsewhere != base, spaced == base], [True, True, True])

    def test_only_a_marker_in_the_exact_form_on_the_last_non_blank_line_is_removed(self):
        for odd in ("<!-- fleet-switchboard:from=a b -->", "<!-- fleet-switchboard:from= -->", "text " + MARKER):
            text = self.plain + odd + "\n"
            self.assertEqual(self.pc.plan_hash(text), self.pc.plan_hash_legacy(text), odd)

    def test_an_approval_at_the_old_hash_and_at_the_new_hash_is_accepted_and_one_of_an_edit_is_not(self):
        for value in self.pc.plan_hashes(self.signed):
            verdict = self.pa.evaluate(value.split(":")[1], comments_of(self.signed), DONE_WHEN, self.pc)
            self.assertTrue(verdict["ok"], (value, verdict["line"]))
        edited = self.signed.replace("size: S", "size: M")
        for value in self.pc.plan_hashes(self.signed):
            verdict = self.pa.evaluate(value.split(":")[1], comments_of(edited), DONE_WHEN, self.pc)
            self.assertFalse(verdict["ok"])


class TestStoredBindingsReadAsBound(unittest.TestCase):
    """S3, S8, S10: a record at the old hash, at the new hash, at an edit and at an older revision."""

    KINDS = ("approval", "proof", "verdict", "consensus", "claim", "ask")

    def setUp(self):
        self.pc, self.pl = real("plan_check"), real("plan_ledger")
        self.signed = plan_text() + "\n" + MARKER + "\n"
        self.hashes = self.pc.plan_hashes(self.signed)

    def entries(self, plan_hash):
        return [{"kind": k, "plan_hash": plan_hash, "head": self.pl.ask_head(plan_hash)} for k in self.KINDS]

    def test_six_kinds_at_two_hashes_read_as_the_live_plan_and_six_of_an_edit_read_as_stale(self):
        bound = 0
        for value in self.hashes:
            for entry in self.pl.as_current(self.entries(value), self.hashes):
                bound += entry["plan_hash"] == self.hashes[0] and entry["head"] == self.pl.ask_head(self.hashes[0])
        self.assertEqual(bound, 12)
        edited = self.pc.plan_hashes(self.signed.replace("size: S", "size: M"))
        stale = sum(e["plan_hash"] != self.hashes[0] for v in edited for e in self.pl.as_current(self.entries(v), self.hashes))
        self.assertEqual(stale, 6 * len(edited))

    def test_a_binding_of_an_older_revision_stays_as_it_is_and_reads_as_stale(self):
        older = self.pc.plan_hashes(plan_text("older: yes") + "\n" + MARKER + "\n")
        for value in older:
            out = self.pl.as_current(self.entries(value), self.hashes)
            self.assertEqual([e["plan_hash"] for e in out], [value] * 6)
            self.assertEqual(self.pc.canonical_hash(value, self.hashes), value)
        self.assertEqual(self.pc.canonical_hash(self.hashes[1], self.hashes), self.hashes[0])

    def test_the_ledger_entries_themselves_are_never_rewritten(self):
        entries = self.entries(self.hashes[1])
        self.pl.as_current(entries, self.hashes)
        self.assertEqual({e["plan_hash"] for e in entries}, {self.hashes[1]})

    def test_a_text_with_no_marker_has_one_hash_and_no_mapping(self):
        pair = self.pc.plan_hashes(plan_text())
        entries = self.entries(pair[0])
        self.assertIs(self.pl.as_current(entries, pair), entries)


class TestReviewRoundInBothForms(unittest.TestCase):
    """S9: three verdicts of one round at the old and at the new hash count together once read through the round's pair."""

    def setUp(self):
        self.pc, self.pl, self.rv = real("plan_check"), real("plan_ledger"), real("plan_review")
        self.signed = plan_text() + "\n" + MARKER + "\n"
        self.new, self.old = self.pc.plan_hashes(self.signed)
        self.lanes = ["L1", "L2", "L3"]

    def lanes_at(self, hashes):
        return [{"kind": "lane", "lineage": "o/r#1", "review": "rv1", "round": 1, "lane": l, "verdict": "approve", "plan_hash": h, "objections": []}
                for l, h in zip(self.lanes, hashes)]

    def consensus(self, entries, pair, final=None):
        entries = self.pl.as_current(entries, pair)
        return self.rv.decide_consensus(entries, self.lanes, final or self.new, rounds_left=True)[0]

    def test_two_old_and_one_new_verdict_reach_consensus_through_the_pair_and_not_without_it(self):
        mixed = self.lanes_at([self.old, self.old, self.new])
        self.assertEqual(self.consensus(mixed, (self.new, self.old)), "consensus")
        self.assertNotEqual(self.rv.decide_consensus(mixed, self.lanes, self.new, rounds_left=True)[0], "consensus")

    def test_the_controls_all_old_and_all_new_reach_consensus(self):
        self.assertEqual(self.consensus(self.lanes_at([self.old] * 3), (self.new, self.old)), "consensus")
        self.assertEqual(self.consensus(self.lanes_at([self.new] * 3), (self.new, self.old)), "consensus")

    def test_a_verdict_at_the_hash_of_an_edited_text_does_not_count(self):
        edited = self.pc.plan_hash(self.signed.replace("size: S", "size: M"))
        self.assertNotEqual(self.consensus(self.lanes_at([self.old, self.new, edited]), (self.new, self.old)), "consensus")

    def test_every_stored_vs_stored_command_reads_the_round_in_its_own_form(self):
        source = (BIN / "switchboard" / "plan_review.py").read_text(encoding="utf-8")
        for name in ("review_lane_send_problem", "_closing_outcome", "review_status", "review_respond", "review_close", "review_tick", "review_decide"):
            body = source.split("def %s(" % name, 1)[1].split("\ndef ", 1)[0]
            self.assertIn("_round_pair(", body, name)
        for name in ("review_start", "review_close", "review_respond"):
            body = source.split("def %s(" % name, 1)[1].split("\ndef ", 1)[0]
            self.assertIn('"plan_hashes": list(', body, name)


class TestPlanHashMarkerMutants(unittest.TestCase):
    """S6, S8, S9: a mutant is applied to a copy and asserted applied; its control passes the same check."""

    def setUp(self):
        self.signed = plan_text() + "\n" + MARKER + "\n"

    def check(self, pc, pa, pl):
        """The one check: the two forms hash alike, an approval at the old hash is accepted, an old-head ask record reads as the live one."""
        assert pc.plan_hash(self.signed) == pc.plan_hash(plan_text()), "the marker changes the hash"
        old = pc.plan_hashes(self.signed)[-1]
        assert pa.evaluate(old.split(":")[1], comments_of(self.signed), DONE_WHEN, pc)["ok"], "an approval at the old hash is refused"
        out = pl.as_current([{"kind": "proof", "head": pl.ask_head(old), "plan_hash": old}], pc.plan_hashes(self.signed))
        assert out[0]["head"] == pl.ask_head(pc.plan_hash(self.signed)), "an old-head record reads as stale"

    def test_the_control_passes(self):
        self.check(real("plan_check"), real("plan_approval"), real("plan_ledger"))

    def test_a_mutant_that_hashes_the_marker_fails(self):
        pc = _ph_mutate("plan_check", "    text = _without_trailing_marker(text)\n", "    pass\n")
        with self.assertRaisesRegex(AssertionError, "the marker changes the hash"):
            self.check(pc, real("plan_approval"), real("plan_ledger"))

    def test_a_mutant_that_accepts_no_old_hash_fails(self):
        pc = _ph_mutate("plan_check", "    return hashes[0] if hashes and stored in hashes else stored\n", "    return stored\n")
        with self.assertRaisesRegex(AssertionError, "an approval at the old hash is refused"):
            self.check(pc, real("plan_approval"), real("plan_ledger"))

    def test_a_mutant_that_reads_the_ask_head_at_the_new_form_only_fails(self):
        pl = _ph_mutate("plan_ledger", "        if entry.get(\"plan_hash\") == old or entry.get(\"head\") == old_head:", "        if False:")
        with self.assertRaisesRegex(AssertionError, "an old-head record reads as stale"):
            self.check(real("plan_check"), real("plan_approval"), pl)

    def test_a_mutant_that_compares_the_raw_strings_in_a_round_counts_two_of_three(self):
        pc, pl, rv = real("plan_check"), _ph_mutate("plan_ledger", "    if not hashes or len(hashes) < 2:\n        return entries\n", "    return entries\n"), real("plan_review")
        new, old = pc.plan_hashes(self.signed)
        entries = [{"kind": "lane", "lane": l, "verdict": "approve", "plan_hash": h, "objections": []} for l, h in zip("ABC", (old, old, new))]
        entries = pl.as_current(entries, (new, old))
        self.assertNotEqual(rv.decide_consensus(entries, list("ABC"), new, rounds_left=True)[0], "consensus")


class TestPlanHashMarkerDocumentation(unittest.TestCase):
    """S7: one section names the three statements."""

    def test_the_template_says_what_the_hash_covers(self):
        text = (REPO / "docs" / "plan-template.md").read_text(encoding="utf-8")
        section = text.split("#### The plan hash and the trailing marker (#1713)", 1)[1].split("\n#", 1)[0]
        for phrase in ("whitespace collapsed", "one trailing marker line", "marker line anywhere else", "old hash"):
            self.assertIn(phrase, section.lower())

    def test_switchboard_md_names_the_rule(self):
        text = (REPO / "docs" / "switchboard.md").read_text(encoding="utf-8")
        self.assertIn("(#1713)", text)


class TestPlanHashMarkerNoSizeTest(unittest.TestCase):
    """U4 (#1676): this file never diffs against a ref or counts a pull request's lines."""

    def test_no_such_call(self):
        source = Path(__file__).read_text(encoding="utf-8")
        needle = "git" + " diff"
        self.assertEqual(source.count(needle), 0)
        self.assertEqual(len(re.findall("|".join(("sub" + "process", "num" + "stat", "merge" + "-base")), source)), 0)
