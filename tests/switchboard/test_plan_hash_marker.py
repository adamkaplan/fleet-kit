"""A plan comment has one hash with or without the trailing marker (#1713): S2, S3, S5, S6, S7, S8, S9, S10, S11 and U4 of the plan.

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


class TestPlanHashMarkerPure(unittest.TestCase):
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
        self.assertEqual(self.pc.plan_hashes(self.signed), (self.pc.plan_hash(self.signed), self.pc.plan_hash_collapsed(self.signed), self.pc.plan_hash_legacy(self.signed)))
        self.assertEqual(len(self.pc.plan_hashes(self.plain)), 2)

    def test_one_edited_word_and_a_marker_line_elsewhere_change_the_hash_and_whitespace_does_not(self):
        base = self.pc.plan_hash(self.signed)
        edited = self.pc.plan_hash(self.signed.replace("size: S", "size: M"))
        elsewhere = self.pc.plan_hash(plan_text() + MARKER + "\nmore text\n")
        spaced = self.pc.plan_hash(self.signed.replace("size: S", "size: S  \t"))
        self.assertEqual([edited != base, elsewhere != base, spaced == base, self.pc.plan_hash(self.signed.replace("size: S", "size:   S")) != base, self.pc.plan_hash(self.signed.replace("size: S", " size: S")) != base], [True, True, True, True, True])

    def test_only_a_marker_in_the_exact_form_on_the_last_non_blank_line_is_removed(self):
        for odd in ("<!-- fleet-switchboard:from=a b -->", "<!-- fleet-switchboard:from= -->", "text " + MARKER):
            text = self.plain + odd + "\n"
            self.assertEqual(self.pc.plan_hash_collapsed(text), self.pc.plan_hash_legacy(text), odd)

    def test_an_approval_at_the_old_hash_and_at_the_new_hash_is_accepted_and_one_of_an_edit_is_not(self):
        for value in self.pc.plan_hashes(self.signed):
            verdict = self.pa.evaluate(value.split(":")[1], comments_of(self.signed), DONE_WHEN, self.pc)
            self.assertTrue(verdict["ok"], (value, verdict["line"]))
        edited = self.signed.replace("size: S", "size: M")
        for value in self.pc.plan_hashes(self.signed):
            verdict = self.pa.evaluate(value.split(":")[1], comments_of(edited), DONE_WHEN, self.pc)
            self.assertFalse(verdict["ok"])


class TestPlanHashMarkerStored(unittest.TestCase):
    """S3, S8, S10: a record at the old hash, at the new hash, at an edit and at an older revision."""

    KINDS = ("approval", "proof", "verdict", "consensus", "ask")

    def setUp(self):
        self.pc, self.pl = real("plan_check"), real("plan_ledger")
        self.signed = plan_text() + "\n" + MARKER + "\n"
        self.hashes = self.pc.plan_hashes(self.signed)

    def entries(self, plan_hash):
        return [{"kind": k, "plan_hash": plan_hash, "head": self.pl.ask_head(plan_hash)} for k in self.KINDS]

    def test_five_kinds_at_two_hashes_read_as_the_live_plan_and_five_of_an_edit_read_as_stale(self):
        bound = 0
        for value in self.hashes:
            for entry in self.pl.as_current(self.entries(value), self.hashes):
                bound += entry["plan_hash"] == self.hashes[0] and entry["head"] == self.pl.ask_head(self.hashes[0])
        self.assertEqual(bound, 5 * len(self.hashes))
        edited = self.pc.plan_hashes(self.signed.replace("size: S", "size: M"))
        stale = sum(e["plan_hash"] != self.hashes[0] for v in edited for e in self.pl.as_current(self.entries(v), self.hashes))
        self.assertEqual(stale, 5 * len(edited))

    def test_a_binding_of_an_older_revision_stays_as_it_is_and_reads_as_stale(self):
        older = self.pc.plan_hashes(plan_text("older: yes") + "\n" + MARKER + "\n")
        for value in older:
            out = self.pl.as_current(self.entries(value), self.hashes)
            self.assertEqual([e["plan_hash"] for e in out], [value] * 5)
            self.assertEqual(self.pc.canonical_hash(value, self.hashes), value)
        self.assertEqual(self.pc.canonical_hash(self.hashes[1], self.hashes), self.hashes[0])

    def test_the_ledger_entries_themselves_are_never_rewritten(self):
        entries = self.entries(self.hashes[1])
        self.pl.as_current(entries, self.hashes)
        self.assertEqual({e["plan_hash"] for e in entries}, {self.hashes[1]})

    def test_a_text_with_no_marker_has_one_hash_and_no_mapping(self):
        pair = self.pc.plan_hashes(plan_text())
        entries = self.entries(pair[0])
        self.assertEqual(self.pl.as_current(entries, pair), entries)


class TestPlanHashMarkerTally(unittest.TestCase):
    """S9: three verdicts of one round at the old and at the new hash count together once read through the round's pair."""

    def setUp(self):
        self.pc, self.pl, self.rv = real("plan_check"), real("plan_ledger"), real("plan_review")
        self.signed = plan_text() + "\n" + MARKER + "\n"
        self.new, _, self.old = self.pc.plan_hashes(self.signed)
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
        pl = _ph_mutate("plan_ledger", "        if entry.get(\"plan_hash\") in olds or entry.get(\"head\") in old_heads:", "        if False:")
        with self.assertRaisesRegex(AssertionError, "an old-head record reads as stale"):
            self.check(real("plan_check"), real("plan_approval"), pl)

    def test_a_mutant_that_compares_the_raw_strings_in_a_round_counts_two_of_three(self):
        pc, pl, rv = real("plan_check"), _ph_mutate("plan_ledger", "    if not hashes or len(hashes) < 2:\n        return entries\n", "    return entries\n"), real("plan_review")
        new, _, old = pc.plan_hashes(self.signed)
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


# ---------------------------------------------------------------------------------------------------- the daemon and the commands, run for real

def _entry_copy(directory, relative, old, new):
    """The whole entry laid out in `directory` with `old` replaced by `new` in the file `relative` (a source COPY; asserted applied), loaded as a module."""
    import shutil
    from support.loader import _load
    directory = Path(directory) / "bin"
    shutil.copytree(str(BIN), str(directory))
    path = Path(directory) / relative
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == 1, "the mutation target is not found exactly once: %r" % old
    path.write_text(source.replace(old, new), encoding="utf-8")
    assert path.read_text(encoding="utf-8") != source, "the mutation was not applied"
    return _load("t1713_entry_%d" % abs(hash(str(directory))), Path(directory) / "fleet-switchboard")


def build():
    host = HOST   # set by bin/test-switchboard: the fixtures of that file
    import json
    import io
    import os
    import tempfile
    from unittest import mock
    from support.loader import captured, sb

    def only_own_tests(cls):
        for name in dir(cls):
            if name.startswith("test") and name not in cls.__dict__:
                setattr(cls, name, None)   # the inherited tests are run by their own class, not again here
        return cls

    class TestPlanHashMarkerWake(host.TestPlanWake):
        """S2: the daemon's plan-wake pass over a plan comment posted plainly and the same text edited in place with the marker."""

        def hashes_announced(self):
            return [re.search(r"(sha256:[0-9a-f]{64})", text).group(1) for who, text in self.lines() if who == "orch"]

        def test_a_plain_post_and_an_edit_in_place_with_the_marker_give_one_hash_over_six_reads(self):
            pc, pa = real("plan_check"), real("plan_approval")
            record = self.post(3, self.plan())
            self.go(3)
            record["body"] += "\n" + MARKER
            record["updated_at"] = sb.iso(self.world.clock.now())
            self.go(3)
            self.add(4, parent=2)
            signed = self.post(4, self.plan(), agent="fleet-registry")
            self.go(3)
            self.assertTrue(signed["body"].rstrip().endswith(MARKER))
            announced = self.hashes_announced()
            reads = list(announced)
            for body in (record["body"].replace("\n" + MARKER, ""), record["body"]):
                reads.append(pc.check(body, host.GOOD_DONE_WHEN)["plan_hash"])
                reads.append(pa.evaluate(pc.plan_hash(body).split(":")[1], [{"id": 1, "body": body}], host.GOOD_DONE_WHEN, pc)["plan_hash"])
            self.assertEqual(len(announced), 2, "one announcement per plan: the edit in place with the marker gives no second one for #3")
            self.assertEqual(len(set(reads)), 1, reads)

        def scenario(self, run, consensus_hash):
            self.post(3, self.plan(), agent="fleet-registry")
            body = self.comments[-1]["body"]
            h = {"old": sb.plan_check.plan_hash_legacy(body), "new": sb.plan_check.plan_hash(body), "other": "sha256:" + "0" * 64}[consensus_hash]
            sb.plan_ledger.append_entry(self.world.state, {"kind": "consensus", "result": "pass", "pr": "-", "scenario": "-", "plan_hash": h}, self.world.clock.now())
            self.world.tick(1)
            self.engine.triage.units.clear() if getattr(self.engine, "triage", None) is not None else None
            run(self.engine, [], self.world.clock.now(), ())
            return self.to("orch")

        def test_control_a_consensus_at_another_hash_does_not_bind_the_plan(self):
            self.assertEqual(len(self.scenario(sb.triage_pass, "other")), 1)

        def test_control_a_consensus_at_the_new_hash_binds_the_plan(self):
            self.assertEqual(self.scenario(sb.triage_pass, "new"), [])

        def test_an_approval_recorded_at_the_old_hash_is_read_as_bound_by_the_wake(self):
            self.assertEqual(self.scenario(sb.triage_pass, "old"), [], "an approval at the old hash is not read as bound")

        def test_a_mutant_that_compares_the_wakes_stored_hash_without_canonical_hash_fails(self):
            with tempfile.TemporaryDirectory() as tmp:
                msb = _entry_copy(tmp, "fleet-switchboard",
                                  "check_module.canonical_hash(plan_hash, hashes) in {check_module.canonical_hash(h, hashes) for h in consensus}", "plan_hash in consensus")
                with self.assertRaisesRegex(AssertionError, r"sha256:[0-9a-f]{64}"):   # the announcement the mutated wake sends (its text names the plan hash)
                    self.assertEqual(self.scenario(msb.triage_pass, "old"), [])

    only_own_tests(TestPlanHashMarkerWake)

    class TestPlanHashMarkerRounds(host.TestPlanReviewCommands):
        """S9, S11: the commands of a review round, each run through the entry with the fixtures of TestPlanReviewCommands."""
        PREDATES = "this round predates the hash pair: start a new round"

        def signed_plan(self):
            self.plan_issue["body"] = host.LEDGER_PLAN + "\n" + MARKER

        def start_round(self, legacy=False):
            """A review round on the marker-bearing plan; `legacy` drops `plan_hashes` from the entries it writes (a round of before the change)."""
            self.signed_plan()
            real_append = sb.plan_ledger.append_checked
            legacy_hash = sb.plan_check.plan_hash_legacy(host.LEDGER_PLAN + "\n" + MARKER)

            def before_the_change(e, s):
                """The entry as the code before this change wrote it: the OLD hash in `plan_hash`, no `plan_hashes`."""
                entry = {k: v for k, v in build_entry(e, s).items() if k != "plan_hashes"}
                if entry.get("kind") == "round" and entry.get("plan_hash"):
                    entry["plan_hash"] = legacy_hash
                return entry

            def strip(state, now, build):
                nonlocal build_entry
                build_entry = build
                return real_append(state, now, before_the_change)
            build_entry = None
            with mock.patch.object(sb.plan_ledger, "append_checked", strip if legacy else real_append):
                code, got, err = self.start()
            self.assertEqual(code, 0, err)
            return got

        def inject(self, entry):
            return sb.plan_ledger.append_entry(self.world.state, entry, self.world.clock.now())

        def lane_entry(self, review, rnd, lane, plan_hash, verdict="approve"):
            return {"kind": "lane", "lineage": rnd["lineage"], "review": review, "round": rnd["round"], "lane": lane, "verdict": verdict,
                    "plan_hash": plan_hash, "objections": [], "by": "planrev-test"}

        def closing_round(self, marker=True):
            got = self.start_round()
            self.finish_round(got, {})
            body = self.FINAL + ("\n" + MARKER if marker else "")
            code, out, err = self.close(got["review"], ref=self.final_plan(body=body))
            self.assertEqual(code, 0, err)
            return got["review"], self.kinds("round")[-1]

        def tally(self, review, rnd, hashes):
            """The closing round's lanes but the last report at the given hashes (ledger lines as an earlier build wrote them); the last reports through the command."""
            lanes = rnd["lanes"]
            for lane, h in zip(lanes[:-1], hashes):
                self.inject(self.lane_entry(review, rnd, lane, h))
            code, out, err = self.verdict_current(review, lanes[-1])
            self.assertEqual(code, 0, err)
            return json.loads(out)

        def test_the_control_all_verdicts_at_the_old_hash_and_a_text_with_no_marker_reach_consensus(self):
            review, rnd = self.closing_round()
            new, *older = rnd["plan_hashes"]
            old = older[-1]
            self.assertEqual(self.tally(review, rnd, [old] * 5)["tally"], "consensus")

        def test_verdicts_at_the_old_and_at_the_new_hash_count_together_and_the_consensus_is_reached(self):
            review, rnd = self.closing_round()
            new, *older = rnd["plan_hashes"]
            old = older[-1]
            self.assertEqual(len(rnd["plan_hashes"]), 3)
            done = self.tally(review, rnd, [old, old, new, old, new])
            self.assertEqual(done["tally"], "consensus", done)
            (consensus,) = [e for e in self.ledger() if e["kind"] == "consensus"]
            self.assertEqual(consensus["plan_hash"], new, "the consensus is recorded at the new hash")
            status = json.loads(self.review("orch", "status", review, "--json")[1])
            self.assertEqual((status["consensus"], status["next"]), (True, "the consensus is recorded"))

        def test_a_verdict_at_the_hash_of_an_edited_text_is_not_counted(self):
            review, rnd = self.closing_round()
            new, *older = rnd["plan_hashes"]
            old = older[-1]
            edited = sb.plan_check.plan_hash(self.FINAL + "\nan edit\n")
            done = self.tally(review, rnd, [old, old, new, old, edited])
            self.assertNotEqual(done["tally"], "consensus", done)
            self.assertEqual([e for e in self.ledger() if e["kind"] == "consensus"], [])

        def test_status_reads_a_consensus_recorded_at_the_old_hash_and_the_other_commands_accept_the_round(self):
            review, rnd = self.closing_round()
            new, *older = rnd["plan_hashes"]
            old = older[-1]
            self.inject({"kind": "consensus", "result": "pass", "pr": "-", "scenario": "-", "lineage": rnd["lineage"], "review": review, "plan_hash": old, "by": "plan-review"})
            self.assertTrue(json.loads(self.review("orch", "status", review, "--json")[1])["consensus"], "status: an old-hash consensus is not read as bound")
            before = len(self.ledger())
            code, out, err = self.review("orch", "tick", "--json")
            self.assertEqual(code, 0, err)
            self.assertEqual(len(self.ledger()), before)
            self.assertIsNone(sb.review_lane_send_problem({"plan_review": {"enabled": True}}, "nobody", "orch"))

        def test_decide_runs_on_a_round_that_has_its_pair(self):
            self.signed_plan()
            review, got = self.spent_review()
            code, out, err = self.review("cos", "decide", review, "--objection", "L2/O1", "--override", "--reason", "out of scope for this plan, accepted by the boss", "--json")
            self.assertEqual(code, 0, err)

        def test_a_mutant_that_compares_the_raw_strings_of_a_round_fails_the_tally(self):
            review, rnd = self.closing_round()
            new, *older = rnd["plan_hashes"]
            old = older[-1]
            with tempfile.TemporaryDirectory() as tmp:
                msb = _entry_copy(tmp, "switchboard/plan_review.py", "    return plan_ledger.as_current(entries, pair), dict(rnd, plan_hash=load_plan_check().canonical_hash(rnd[\"plan_hash\"], pair))\n",
                                  "    return entries, rnd\n")
                for lane, h in zip(rnd["lanes"][:-1], [old, old, new, old, new]):
                    self.inject(self.lane_entry(review, rnd, lane, h))
                pane = [e["pane"] for e in self.ledger() if e["kind"] == "lane_session" and e["review"] == review and e["round"] == rnd["round"] and e["lane"] == rnd["lanes"][-1]][-1]
                os.environ["HERDR_PANE_ID"] = pane
                with captured() as (out, err), mock.patch.object(msb, "owner_terminal_problem", lambda *a, **k: None), \
                        mock.patch.object(sys, "stdin", io.StringIO(self.block(rnd["lanes"][-1], rnd["plan_hash"]))):
                    code = msb.main(["plan", "review", "verdict", review, "--lane", rnd["lanes"][-1], "--json"], runner=self.route, now=self.world.clock.now,
                                    wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
                with self.assertRaisesRegex(AssertionError, r"!= 'consensus'"):   # the assertion on the tally the mutated command printed
                    self.assertEqual(json.loads(out.getvalue())["tally"], "consensus")

        # -- S9: the entry's decide and the two open-reading places, on a round that holds verdicts at both forms

        def escalated_signed(self, obj_lane="L2"):
            """spent_review on the marker-bearing plan: each revision keeps the marker; the lanes but the objector report at the OLD hash (injected) on
            the last round and at the NEW one before. Returns (review, round entry)."""
            self.signed_plan()
            code, got, err = self.start()
            self.assertEqual(code, 0, err)
            review = got["review"]
            obj = ["- id: O1; severity: blocking; state: open; text: a stub passes"]
            for number in (8, 9, 10, 11):
                rnd = [e for e in self.ledger() if e["kind"] == "round" and e["review"] == review][-1]
                new, old = rnd["plan_hashes"]
                for i, lane in enumerate(rnd["lanes"]):
                    if lane == obj_lane:
                        self.assertEqual(self.verdict_current(review, lane, "iterate", obj)[0], 0)
                    else:
                        self.inject(self.lane_entry(review, rnd, lane, old if i % 2 else new))
                self.plan_issue2 = {"number": number, "state": "open", "title": "p", "body": self.PLAN2 + "\nrevision %d\n" % number + MARKER,
                                    "updated_at": "2026-01-01T00:00:00Z", "parent_issue_url": "https://api.github.com/repos/acme/app/issues/7"}
                code, out, err = self.review("orch", "respond", review, "--plan-ref", "acme/app#%d" % number, "--table", self.table("%s/O1: reject, why" % obj_lane), "--json")
                self.assertEqual(code, 0, err)
                if "escalated" in json.loads(out):
                    return review, [e for e in self.ledger() if e["kind"] == "round" and e["review"] == review][-1]
            self.fail("it never escalated")

        def override(self, review):
            return self.review("cos", "decide", review, "--objection", "L2/O1", "--override", "--reason", "out of scope for this plan, accepted by the boss", "--json")

        def consensus_entries(self, review):
            return [e for e in self.ledger() if e["kind"] == "consensus" and e.get("review") == review]

        def test_decide_through_the_entry_on_a_round_with_verdicts_at_both_forms_reaches_the_consensus_once_at_the_new_hash(self):
            review, rnd = self.escalated_signed()
            new, old = rnd["plan_hashes"]
            code, out, err = self.override(review)
            self.assertEqual(code, 0, err)
            (consensus,) = self.consensus_entries(review)
            self.assertEqual((consensus["plan_hash"], consensus.get("overridden_by") is not None), (new, True))

        def test_decide_control_a_consensus_of_the_edited_text_does_not_stand_for_the_round_and_one_is_written(self):
            review, rnd = self.escalated_signed()
            edited = sb.plan_check.plan_hash(self.FINAL + "\nan edit\n")
            self.inject({"kind": "consensus", "result": "pass", "pr": "-", "scenario": "-", "lineage": rnd["lineage"], "review": review, "plan_hash": edited, "by": "plan-review"})
            self.assertEqual(self.override(review)[0], 0)
            self.assertEqual([e["plan_hash"] for e in self.consensus_entries(review)], [edited, rnd["plan_hashes"][0]])

        def test_decide_counts_a_consensus_at_the_old_hash_as_reached_and_writes_no_second(self):
            review, rnd = self.escalated_signed()
            new, old = rnd["plan_hashes"]
            self.inject({"kind": "consensus", "result": "pass", "pr": "-", "scenario": "-", "lineage": rnd["lineage"], "review": review, "plan_hash": old, "by": "plan-review"})
            self.assertEqual(self.override(review)[0], 0)
            self.assertEqual(len(self.consensus_entries(review)), 1, "decide counts the old-hash consensus as the round's")

        def test_a_mutant_that_compares_the_raw_strings_in_decide_writes_a_second_consensus(self):
            review, rnd = self.escalated_signed()
            new, old = rnd["plan_hashes"]
            self.inject({"kind": "consensus", "result": "pass", "pr": "-", "scenario": "-", "lineage": rnd["lineage"], "review": review, "plan_hash": old, "by": "plan-review"})
            with tempfile.TemporaryDirectory() as tmp:
                msb = _entry_copy(tmp, "switchboard/plan_review.py", "        entries, rnd = _canon_round(entries, rnd)   # #1713: both sides in the round's own form\n", "")
                os.environ["HERDR_PANE_ID"] = self.world.panes["cos"]
                with captured() as (out, err), mock.patch.object(msb, "owner_terminal_problem", lambda *a, **k: None), mock.patch.object(sys, "stdin", io.StringIO("")):
                    msb.main(["plan", "review", "decide", review, "--objection", "L2/O1", "--override", "--reason", "out of scope for this plan, accepted by the boss", "--json"],
                             runner=self.route, now=self.world.clock.now, wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
                with self.assertRaisesRegex(AssertionError, r"2 != 1"):   # the mutated decide wrote a second consensus
                    self.assertEqual(len(self.consensus_entries(review)), 1)

        def test_the_tick_and_the_lane_send_check_read_a_round_with_verdicts_at_both_forms_as_open(self):
            got = self.start_round()
            rnd = self.kinds("round")[-1]
            new, old = rnd["plan_hashes"]
            lanes = rnd["lanes"]
            self.inject(self.lane_entry(got["review"], rnd, lanes[0], old))
            self.inject(self.lane_entry(got["review"], rnd, lanes[1], new))
            before = len(self.ledger())
            code, out, err = self.review("orch", "tick", "--json")
            self.assertEqual(code, 0, err)
            done = json.loads(out)
            self.assertEqual(done.get("skipped", []), [], "the tick handles the round")
            self.assertTrue(all(e.get("review") == got["review"] for e in self.ledger()[before:]))
            session = [e["session"] for e in self.ledger() if e["kind"] == "lane_session"][0]
            self.assertIn("is a reviewer lane", sb.review_lane_send_problem({"plan_review": {"enabled": True}}, session, "orch"), "the send check reads the round as open")

        # -- S11: a round without a pair

        def refused(self, code, err, before):
            self.assertEqual(code, 2, err)
            self.assertEqual(err.strip().splitlines()[-1].split(": ", 1)[-1], self.PREDATES)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertEqual(len(self.ledger()), before, "nothing was recorded")

        def six_commands(self, review, rnd):
            """Each command of the round on `rnd`: (name, code, err); the closing outcome is the function the verdict command ends in."""
            rows = []
            lane = rnd["lanes"][0]
            pane = [e["pane"] for e in self.ledger() if e["kind"] == "lane_session" and e["review"] == review and e["lane"] == lane][-1]
            rows.append(("verdict",) + self.review(pane, "verdict", review, "--lane", lane, stdin=self.block(lane, rnd["plan_hash"]))[::2])
            rows.append(("status",) + self.review("orch", "status", review)[::2])
            rows.append(("close",) + self.review("orch", "close", review, "--plan-ref", self.final_plan(), "--json")[::2])   # the same arguments as the existing close tests
            rows.append(("decide",) + self.review("cos", "decide", review, "--objection", "L2/O1", "--accept", "--reason", "the objection is right, revise the plan")[::2])
            rows.append(("respond",) + self.review("orch", "respond", review, "--plan-ref", "acme/app#8", "--table", self.table("L2/O1: accept"))[::2])
            engine, state, _ = sb.build_engine(self.route, self.world.clock.now, "cli", writable=True, socket_call=self.world.herdr.socket)
            try:
                sb._closing_outcome(engine, state, sb.review_config(sb.load_config()), sb.load_config(), self.route, self.world.clock.now, review)
                rows.append(("closing", 0, ""))
            except sb.ReviewError as err:
                rows.append(("closing", 2, str(err)))
            return rows

        def test_a_round_that_predates_the_pair_is_refused_by_each_of_the_six_commands_and_the_lane_send_check_holds_it_open(self):
            got = self.start_round(legacy=True)
            rnd = self.kinds("round")[-1]
            self.assertNotIn("plan_hashes", rnd)
            old = sb.plan_check.plan_hash_legacy(host.LEDGER_PLAN + "\n" + MARKER)
            for lane in rnd["lanes"]:
                self.inject(self.lane_entry(got["review"], rnd, lane, old if lane != rnd["lanes"][-1] else got["plan_hash"]))   # got["plan_hash"] is the NEW hash: a verdict at each form
            before = len(self.ledger())
            rows = self.six_commands(got["review"], rnd)
            self.assertEqual([r[0] for r in rows], ["verdict", "status", "close", "decide", "respond", "closing"])
            for name, code, err in rows:
                with self.subTest(command=name):
                    self.assertEqual(code, 2, err)
                    self.assertIn(self.PREDATES, err)
                    self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertEqual(len(self.ledger()), before, "0 ledger lines added")
            code, out, err = self.review("orch", "tick", "--json")
            self.assertEqual(code, 0, err)
            self.assertIn("skipped: %s: this round predates the hash pair" % got["review"], out)
            self.assertEqual(len(self.ledger()), before)
            session = [e["session"] for e in self.ledger() if e["kind"] == "lane_session"][0]
            self.assertIn("is a reviewer lane", sb.review_lane_send_problem({"plan_review": {"enabled": True}}, session, "orch"))

        def two_rounds(self):
            """A round without the pair (rv1) and a round with it (rv2), both with silent lanes; the clock moves past the lane timeout."""
            legacy = self.start_round(legacy=True)
            self.plan_issue2["parent_issue_url"] = "https://api.github.com/repos/acme/app/issues/9"   # one open review per ask: the second round serves another ask
            code, valid, err = self.start("orch", "acme/app#8", "acme/app#9")
            self.assertEqual(code, 0, err)
            self.assertTrue(self.kinds("round")[-1].get("plan_hashes"))
            self.assertNotEqual(legacy["review"], valid["review"])
            self.world.clock.t += 1000
            return legacy, valid

        def tick_over(self, module, legacy, valid):
            os.environ["HERDR_PANE_ID"] = self.world.panes["orch"]
            with captured() as (out, err), mock.patch.object(module, "owner_terminal_problem", lambda *a, **k: None), mock.patch.object(sys, "stdin", io.StringIO("")):
                code = module.main(["plan", "review", "tick", "--json"], runner=self.route, now=self.world.clock.now,
                                   wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
            return code, out.getvalue(), err.getvalue()

        def test_the_tick_over_a_round_without_the_pair_and_a_round_with_it_skips_the_first_and_handles_the_second(self):
            legacy, valid = self.two_rounds()
            before = len(self.ledger())
            code, out, err = self.tick_over(sb, legacy, valid)
            self.assertEqual(code, 0, err)
            done = json.loads(out)
            self.assertEqual(done["skipped"], ["skipped: %s: this round predates the hash pair" % legacy["review"]])
            self.assertEqual(sorted(done["relaunched"]), sorted(valid["lanes"]), "the round with its pair is handled")
            added = self.ledger()[before:]
            self.assertTrue(added and all(e["review"] == valid["review"] for e in added), "nothing is recorded for the skipped round")

        def test_a_mutant_tick_that_raises_on_the_round_without_the_pair_handles_none_of_the_two(self):
            legacy, valid = self.two_rounds()
            with tempfile.TemporaryDirectory() as tmp:
                msb = _entry_copy(tmp, "switchboard/plan_review.py",
                                  '            done["skipped"].append("skipped: %s: this round predates the hash pair" % review)\n            continue\n',
                                  '            raise ReviewError("boom")\n')
                before = len(self.ledger())
                try:
                    code, out, err = self.tick_over(msb, legacy, valid)
                except Exception as exc:
                    code, out, err = 2, "", repr(exc)
                relaunched = json.loads(out)["relaunched"] if out.strip().startswith("{") else []
                with self.assertRaisesRegex(AssertionError, "boom"):   # the text the mutated tick raised, shown by the assertion on its stderr
                    self.assertEqual(err.strip(), "")
                self.assertEqual(len(self.ledger()), before)

        def test_the_control_a_round_started_after_the_change_is_accepted_by_the_commands_and_its_lane_send_check_answers_as_before(self):
            got = self.start_round()
            self.assertEqual(self.kinds("round")[-1]["plan_hashes"], [got["plan_hash"], sb.plan_check.plan_hash_collapsed(host.LEDGER_PLAN + "\n" + MARKER), sb.plan_check.plan_hash_legacy(host.LEDGER_PLAN + "\n" + MARKER)])
            self.assertEqual(self.review("orch", "status", got["review"])[0], 0)
            self.assertEqual(self.review("orch", "tick")[0], 0)
            self.assertEqual(self.lane_verdict(got, "L1")[0], 0)

        def test_a_mutant_that_treats_a_round_without_a_pair_as_having_the_raw_hash_is_not_refused(self):
            got = self.start_round(legacy=True)
            with tempfile.TemporaryDirectory() as tmp:
                msb = _entry_copy(tmp, "switchboard/plan_review.py", '        raise ReviewError("this round predates the hash pair: start a new round")\n', "        return ()\n")
                with captured() as (out, err), mock.patch.object(msb, "owner_terminal_problem", lambda *a, **k: None), mock.patch.object(sys, "stdin", io.StringIO("")):
                    os.environ["HERDR_PANE_ID"] = self.world.panes["orch"]
                    code = msb.main(["plan", "review", "status", got["review"]], runner=self.route, now=self.world.clock.now,
                                    wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
                with self.assertRaisesRegex(AssertionError, "0 != 2"):   # the mutated status ran: exit 0 where the refusal exits 2
                    self.assertEqual(code, 2)

    only_own_tests(TestPlanHashMarkerRounds)
    return TestPlanHashMarkerWake, TestPlanHashMarkerRounds


def build_late():
    """The classes whose host classes are defined late in bin/test-switchboard (bound by a line placed after them)."""
    host = HOST
    import json
    from unittest import mock
    from support.loader import sb

    def only_own_tests(cls):
        for name in dir(cls):
            if name.startswith("test") and name not in cls.__dict__:
                setattr(cls, name, None)
        return cls

    class TestPlanHashMarkerAsk(host.TestAskIssueSubject):
        """S8 through the real `prove`, `verdict` and `gates` on an ask issue whose plan comment ends with the marker (the fixtures of TestAskIssueSubject)."""

        def setUp(self):
            super().setUp()
            pc = real("plan_check")
            text = host.LEDGER_PLAN + "\n" + MARKER
            self.pair = pc.plan_hashes(text)
            self.new, self.old = self.pair
            self.edited = pc.plan_hash(text.replace("Commissioning works.", "Commissioning works, edited."))
            self.hash = self.new

        def plan(self):
            return dict(super().plan(), plan_hashes=self.pair)

        def g1(self):
            report = json.loads(self.run_cmd("cmd_gates", json=True)[2])
            return next(g for g in report["gates"] if g["gate"] == "G1")

        def recorded_at(self, plan_hash):
            self.hash = plan_hash
            self.record_all()
            self.hash = self.new

        def test_control_records_at_the_new_hash_read_as_bound(self):
            self.recorded_at(self.new)
            self.assertIn("3 of 3 claimed scenarios proven and judged", self.g1()["detail"])

        def test_records_at_the_old_hash_and_its_head_read_as_bound_through_gates(self):
            self.recorded_at(self.old)
            self.assertEqual({e["plan_hash"] for e in self.entries()}, {self.old})
            self.assertIn("3 of 3 claimed scenarios proven and judged", self.g1()["detail"])

        def test_records_at_the_hash_of_an_edited_text_read_as_stale(self):
            self.recorded_at(self.edited)
            self.assertIn("void: plan changed", self.run_cmd("cmd_gates")[2])
            self.assertNotIn("3 of 3 claimed scenarios proven and judged", self.g1()["detail"])

        def test_a_mutant_that_reads_the_ask_head_without_as_current_fails_through_gates(self):
            self.recorded_at(self.old)
            with mock.patch.object(sb.load_plan_ledger(), "as_current", lambda entries, hashes: entries):   # the real module (sb.plan_ledger is a lazy proxy): the read maps nothing, so the head is compared with the new one only
                with self.assertRaisesRegex(AssertionError, "3 of 3 claimed scenarios proven and judged"):
                    self.assertIn("3 of 3 claimed scenarios proven and judged", self.g1()["detail"])

    only_own_tests(TestPlanHashMarkerAsk)

    class TestPlanHashMarkerLaunch(host.TestLaunchGate):
        """The G0b read of a consensus record, through the real `launch`."""

        def signed(self):
            self.plan_issue["body"] = host.LEDGER_PLAN + "\n" + MARKER
            return sb.plan_check.plan_hashes(self.plan_issue["body"])

        def would_refuse(self, plan_hash):
            self.record_consensus(plan_hash)
            code, out, err = self.launch()
            self.assertEqual(code, 0, err)
            return self.events("gate.would_refuse")

        def test_a_consensus_at_the_new_hash_is_quiet(self):
            self.assertEqual(self.would_refuse(self.signed()[0]), [])

        def test_a_consensus_at_the_old_hash_is_quiet_through_launch(self):
            self.assertEqual(self.would_refuse(self.signed()[1]), [], "a consensus at the old hash is not read as bound")

        def test_a_consensus_at_the_hash_of_an_edited_text_is_not_quiet(self):
            pair = self.signed()
            self.assertEqual(len(self.would_refuse(sb.plan_check.plan_hash(self.plan_issue["body"].replace("Commissioning works.", "Commissioning works, edited.")))), 1)

    only_own_tests(TestPlanHashMarkerLaunch)
    return TestPlanHashMarkerAsk, TestPlanHashMarkerLaunch
