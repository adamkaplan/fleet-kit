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


class TestPlanHashMarkerStored(unittest.TestCase):
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


class TestPlanHashMarkerTally(unittest.TestCase):
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
                with self.assertRaisesRegex(AssertionError, "an approval at the old hash is not read as bound"):
                    self.assertEqual(self.scenario(msb.triage_pass, "old"), [], "an approval at the old hash is not read as bound")

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
            strip = lambda state, now, build: real_append(state, now, lambda e, s: {k: v for k, v in build(e, s).items() if k != "plan_hashes"})
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
            new, old = rnd["plan_hashes"]
            self.assertEqual(self.tally(review, rnd, [old] * 5)["tally"], "consensus")

        def test_verdicts_at_the_old_and_at_the_new_hash_count_together_and_the_consensus_is_reached(self):
            review, rnd = self.closing_round()
            new, old = rnd["plan_hashes"]
            self.assertEqual(len(rnd["plan_hashes"]), 2)
            done = self.tally(review, rnd, [old, old, new, old, new])
            self.assertEqual(done["tally"], "consensus", done)
            (consensus,) = [e for e in self.ledger() if e["kind"] == "consensus"]
            self.assertEqual(consensus["plan_hash"], new, "the consensus is recorded at the new hash")
            status = json.loads(self.review("orch", "status", review, "--json")[1])
            self.assertEqual((status["consensus"], status["next"]), (True, "the consensus is recorded"))

        def test_a_verdict_at_the_hash_of_an_edited_text_is_not_counted(self):
            review, rnd = self.closing_round()
            new, old = rnd["plan_hashes"]
            edited = sb.plan_check.plan_hash(self.FINAL + "\nan edit\n")
            done = self.tally(review, rnd, [old, old, new, old, edited])
            self.assertNotEqual(done["tally"], "consensus", done)
            self.assertEqual([e for e in self.ledger() if e["kind"] == "consensus"], [])

        def test_status_reads_a_consensus_recorded_at_the_old_hash_and_the_other_commands_accept_the_round(self):
            review, rnd = self.closing_round()
            new, old = rnd["plan_hashes"]
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
            new, old = rnd["plan_hashes"]
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
                with self.assertRaisesRegex(AssertionError, "the tally counts the three verdicts together"):
                    self.assertEqual(json.loads(out.getvalue())["tally"], "consensus", "the tally counts the three verdicts together")

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
            rows.append(("tick",) + self.review("orch", "tick")[::2])
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
                self.inject(self.lane_entry(got["review"], rnd, lane, old if lane != rnd["lanes"][-1] else got["plan_hash"]))
            before = len(self.ledger())
            rows = self.six_commands(got["review"], rnd)
            self.assertEqual([r[0] for r in rows], ["verdict", "status", "tick", "decide", "respond", "closing"])
            for name, code, err in rows:
                with self.subTest(command=name):
                    self.assertEqual(code, 2, err)
                    self.assertIn(self.PREDATES, err)
                    self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertEqual(len(self.ledger()), before, "0 ledger lines added")
            session = [e["session"] for e in self.ledger() if e["kind"] == "lane_session"][0]
            self.assertIn("is a reviewer lane", sb.review_lane_send_problem({"plan_review": {"enabled": True}}, session, "orch"))

        def test_the_control_a_round_started_after_the_change_is_accepted_by_the_commands_and_its_lane_send_check_answers_as_before(self):
            got = self.start_round()
            self.assertEqual(self.kinds("round")[-1]["plan_hashes"], [got["plan_hash"], sb.plan_check.plan_hash_legacy(host.LEDGER_PLAN + "\n" + MARKER)])
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
                with self.assertRaisesRegex(AssertionError, "the round is refused with the line"):
                    self.assertIn(self.PREDATES, err.getvalue(), "the round is refused with the line")

    only_own_tests(TestPlanHashMarkerRounds)
    return TestPlanHashMarkerWake, TestPlanHashMarkerRounds
