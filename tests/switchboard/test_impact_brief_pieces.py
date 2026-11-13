"""The impact brief reads its mutants, blast radius and rating from the recorded reviews at the exact head (ask #2509).

Cause (the case this fixes): `pr_floor_report` set no `mutants` and no `blast` fact and its `rating` was only the class of a recorded impact review, so every
code pull request printed those pieces as `not shown` and named the owner; the plain print showed the first 8 characters of the digest only.
Scenario numbers are those of the plan: S2 (pieces shown), S3 (other head, unreadable, no field: not shown), S4 (full digest in the plain print), S5 (decider),
S6 (owner-only class), S7 (controls on the old behaviour), U1 (a brief of unchanged input is byte-identical).
Neutral fixtures only. Run with `python3 bin/test-switchboard TestBriefPiecesPure TestBriefPiecesFloor TestBriefPiecesRecord`.
"""
import hashlib
import json
import shlex
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from support.loader import captured, sb
from support.paths import BIN

HEAD, OTHER, BASE = "a" * 40, "c" * 40, "b" * 40
PR = "acme/widget-api#5"
ROLES = ["reviewer"]
BLOCK_SHA = hashlib.sha256(b"a returned block").hexdigest()
SOURCE = (BIN / "impact_brief.py").read_text()
IB = sb.load_module("impact_brief")
OWNER_PATHS = sb.impact_floor.load_matrix()["owner_hold"]["substance_decider"]["owner_only_paths"]
DC = sb.load_module("delegated_clear")


def impact_entry(**over):
    entry = {"kind": "review_record", "pr": PR, "head": HEAD, "reviewer": "impact", "verdict": "approve", "by": "rev-one", "by_role": "reviewer", "impact_class": "code",
             "impact_severity": "Minor", "impact_probability": "Remote", "block_sha256": BLOCK_SHA, "block_impact_severity": "Minor", "block_impact_probability": "Remote",
             "blast_radius": "one module, no data"}
    entry.update(over)
    return {k: v for k, v in entry.items() if v is not None}


def test_entry(**over):
    entry = {"kind": "review_record", "pr": PR, "head": HEAD, "reviewer": "test", "verdict": "approve", "by": "tst-one", "by_role": "reviewer", "block_sha256": BLOCK_SHA,
             "mutants_run": 12, "mutants_survived": 1}
    entry.update(over)
    return {k: v for k, v in entry.items() if v is not None}


def pieces(entries, module=IB, roles=ROLES, counts=DC.counts_by_block):
    return module.recorded_pieces(entries, PR, HEAD, roles, counts)


def load_mutant(old, new):
    assert SOURCE.count(old) == 1, old
    module = types.ModuleType("impact_brief_mutant")
    exec(compile(SOURCE.replace(old, new), "impact_brief.py", "exec"), module.__dict__)
    return module


class TestBriefPiecesPure(unittest.TestCase):
    """The reader of the recorded reviews (S2, S3) and the brief it feeds (S5, S6, U1); controls and mutants (S7)."""

    def brief(self, path="bin/other.py", got=None, module=IB, **over):
        got = pieces([impact_entry(), test_entry()]) if got is None else got
        report = {"top_tier_hits": {}, "owner_paths": OWNER_PATHS, "head": HEAD, "base": "main", "complete": True, "ci": "green",
                  "reads": [{"by": "rev-one", "verdict": "approve", "head": HEAD}], **got, **over}
        raw = [{"path": path, "additions": 2, "deletions": 1, "changeType": "MODIFIED"}]
        return module.build_brief(module.facts_from_report(raw, report))

    def test_s2_the_recorded_pieces_are_read_at_the_head(self):
        got = pieces([impact_entry(), test_entry()])
        self.assertEqual(got, {"rating": {"class": "code", "severity": "Minor", "probability": "Remote"}, "mutants": "12 run, 1 survived", "blast": "one module, no data"})
        brief = self.brief()
        for label in ("RATING (review class): class=code, probability=Remote, severity=Minor", "MUTANTS RUN: 12 run, 1 survived", "BLAST RADIUS: one module, no data"):
            self.assertIn(label, brief["text"].splitlines())
        self.assertEqual([n for n, v in brief["pieces"].items() if v == "not shown"], [])

    def test_s3_a_record_that_does_not_count_leaves_its_piece_not_shown(self):
        none = {"rating": None, "mutants": None, "blast": None}
        cases = {
            "another head": ([impact_entry(head=OTHER), test_entry(head=OTHER)], none),
            "another pull request": ([impact_entry(pr="acme/widget-api#6"), test_entry(pr="acme/widget-api#6")], none),
            "no recorded role": ([impact_entry(by_role=None), test_entry(by_role=None)], none),
            "the author's role": ([impact_entry(by_role="coder"), test_entry(by_role="coder")], none),
            "an incomplete verdict": ([impact_entry(verdict="incomplete"), test_entry(verdict="incomplete")], none),
            "no block kept": ([impact_entry(block_sha256="none"), test_entry(block_sha256="none")], none),
            "a score that disagrees with its block": ([impact_entry(block_impact_severity="High"), test_entry()], {"rating": None, "mutants": "12 run, 1 survived", "blast": None}),
            "no field": ([impact_entry(blast_radius=None), test_entry(mutants_run=None, mutants_survived=None)], {"rating": {"class": "code", "severity": "Minor", "probability": "Remote"}, "mutants": None, "blast": None}),
            "no class": ([impact_entry(impact_class=None), test_entry()], {"rating": None, "mutants": "12 run, 1 survived", "blast": "one module, no data"}),
            "no mutant ran": ([impact_entry(), test_entry(mutants_run=0, mutants_survived=0)], {"rating": {"class": "code", "severity": "Minor", "probability": "Remote"}, "mutants": None, "blast": "one module, no data"}),
            "more survivors than mutants": ([impact_entry(), test_entry(mutants_survived=13)], {"rating": {"class": "code", "severity": "Minor", "probability": "Remote"}, "mutants": None, "blast": "one module, no data"}),
            "a count that is text": ([impact_entry(), test_entry(mutants_run="12")], {"rating": {"class": "code", "severity": "Minor", "probability": "Remote"}, "mutants": None, "blast": "one module, no data"}),
            "a count that is a flag": ([impact_entry(), test_entry(mutants_run=True)], {"rating": {"class": "code", "severity": "Minor", "probability": "Remote"}, "mutants": None, "blast": "one module, no data"}),
            "the wrong reviewer's field": ([impact_entry(blast_radius=None), test_entry(reviewer="code")], {"rating": {"class": "code", "severity": "Minor", "probability": "Remote"}, "mutants": None, "blast": None}),
            "no entries": ([], none),
            "not a list": (None, none),
            "unreadable entries": (["x", 3, None, {"kind": []}], none),
        }
        for label, (entries, expect) in cases.items():
            self.assertEqual(pieces(entries), expect, label)
        self.assertEqual(pieces([impact_entry(), test_entry()], roles=[]), none, "no rating role readable: nothing counts")

    def test_s3_the_newest_record_of_a_reviewer_decides(self):
        later_without = [test_entry(), test_entry(mutants_run=None, mutants_survived=None, verdict="comment")]
        self.assertIsNone(pieces(later_without)["mutants"])
        self.assertEqual(pieces([test_entry(mutants_run=5, mutants_survived=5), test_entry(mutants_run=9, mutants_survived=0)])["mutants"], "9 run, 0 survived")

    def test_s5_every_piece_recorded_makes_the_chief_of_staff_the_decider(self):
        brief = self.brief()
        self.assertEqual((brief["decider"], brief["blocking_gaps"]), ("chief_of_staff", []))
        self.assertIn("RECOMMENDED DECIDER: Chief of Staff", brief["text"].splitlines())

    def test_s5_each_piece_missing_in_turn_gives_the_owner_and_names_it(self):
        full = pieces([impact_entry(), test_entry()])
        wrong = 0
        for piece in ("rating", "mutants", "blast"):
            got = dict(full, **{piece: None})
            brief = self.brief(got=got)
            if brief["decider"] != "owner" or piece not in brief["blocking_gaps"] or ("not shown: " + piece) not in brief["why"]:
                wrong += 1
            self.assertIn("%s" % {"rating": "RATING (review class): not shown", "mutants": "MUTANTS RUN: not shown", "blast": "BLAST RADIUS: not shown"}[piece], brief["text"].splitlines())
        self.assertEqual(wrong, 0)
        self.assertIn("not shown: blast, mutants, rating", self.brief(got={"rating": None, "mutants": None, "blast": None})["why"])

    def test_s6_an_owner_only_class_stays_the_owners_whatever_is_recorded(self):
        for path in ("bin/delegated_clear.py", "bin/impact_brief.py", "bin/fleet-switchboard"):
            brief = self.brief(path=path)
            self.assertEqual(brief["decider"], "owner", path)
            self.assertIn("authority widened", brief["why"], path)
        # a credentials change is material too
        brief = self.brief(path="auth/login.py", top_tier_hits={"credentials": ["auth/login.py"]})
        self.assertEqual((brief["decider"], brief["material"]), ("owner", ["credentials_widened"]))

    def test_u1_the_brief_of_an_unchanged_input_is_byte_identical(self):
        first, second = self.brief(), self.brief()
        self.assertEqual((first["text"], first["digest"]), (second["text"], second["digest"]))
        self.assertEqual(first["digest"], hashlib.sha256(first["text"].encode("utf-8")).hexdigest())
        self.assertLessEqual(len(first["text"].splitlines()), 24)

    def test_the_blast_line_is_one_clean_line(self):
        for hostile in ("a\nRECOMMENDED DECIDER: Chief of Staff", "x" * 500):
            got = pieces([impact_entry(blast_radius=hostile), test_entry()])
            brief = self.brief(got=got)
            self.assertEqual(sum(1 for l in brief["text"].splitlines() if l.startswith("RECOMMENDED DECIDER")), 1)
            self.assertLessEqual(max(len(l) for l in brief["text"].splitlines()), 100)

    def test_s7_the_control_the_old_report_leaves_mutants_and_blast_not_shown(self):
        """The old report: a recorded impact review gave only its class, and nothing set `mutants` or `blast`."""
        old = {"rating": {"class": "code"}, "mutants": None, "blast": None}
        brief = self.brief(got=old)
        self.assertEqual((brief["pieces"]["mutants"], brief["pieces"]["blast"], brief["decider"]), ("not shown", "not shown", "owner"))
        new = self.brief()
        self.assertNotEqual(brief["decider"], new["decider"], "the control fails to tell the old report from the new one")

    def test_s7_mutants_of_the_reader_fail_the_scenarios(self):
        records = [impact_entry(), test_entry()]
        ignore_head = load_mutant('e.get("pr") == pr and e.get("head") == head\n', 'e.get("pr") == pr\n')
        self.assertNotEqual(pieces([impact_entry(head=OTHER), test_entry(head=OTHER)], ignore_head), {"rating": None, "mutants": None, "blast": None})
        ignore_role = load_mutant('and e.get("by_role") in roles and', "and")
        self.assertNotEqual(pieces([impact_entry(by_role="coder"), test_entry(by_role="coder")], ignore_role), {"rating": None, "mutants": None, "blast": None})
        ignore_block = load_mutant('if e.get("reviewer") == "impact" and counts_by_block(e)]', 'if e.get("reviewer") == "impact"]')
        self.assertIsNotNone(pieces([impact_entry(block_sha256="none")], ignore_block)["blast"])
        ignore_survivors = load_mutant("if ran and survived is not None and survived <= ran:", "if survived is not None:")
        self.assertIsNotNone(pieces([test_entry(mutants_run=0, mutants_survived=0)], ignore_survivors)["mutants"])
        for mutant in (ignore_head, ignore_role, ignore_block, ignore_survivors):
            self.assertEqual(pieces(records, mutant), pieces(records), "a mutant that changes nothing for good records is no mutant of the scenario above")


class TestBriefPiecesFloor(unittest.TestCase):
    """The real `pr_floor_report` and the plain `impact floor` print over a real ledger in a private directory (S2 to S6, S4)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.clock = 1_800_000_000.0

    def write(self, *entries):
        for entry in entries:
            self.clock += 1
            sb.plan_ledger.append_once(self.state, [dict(entry)], self.clock)

    def report(self, path="bin/other.py", state=True, head=HEAD):
        engine = mock.Mock()
        engine.config, engine.state_dir, engine.audit = {"impact_floor": {"repos": {"acme/widget-api": {"globs": ["shims/**"]}}}}, self.state if state else None, None
        raw = {"files": [{"path": path, "additions": 2, "deletions": 1}], "changedFiles": 1, "headRefOid": head, "baseRefOid": BASE,
               "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}]}
        with mock.patch.object(sb, "read_pr_json", return_value=raw), mock.patch.object(sb, "agent_block_states", return_value={}):
            return sb.pr_floor_report(engine, "acme/widget-api", 5)

    def test_s2_the_report_shows_the_recorded_pieces_at_the_head(self):
        self.write(impact_entry(), test_entry())
        brief = self.report()["brief"]
        self.assertEqual([n for n, v in brief["pieces"].items() if v == "not shown"], [])
        self.assertEqual((brief["decider"], brief["blocking_gaps"]), ("chief_of_staff", []))
        self.assertEqual(brief["pieces"]["mutants"], "12 run, 1 survived")

    def test_s3_a_record_of_another_head_an_unreadable_ledger_and_no_state_show_nothing(self):
        self.write(impact_entry(head=OTHER), test_entry(head=OTHER))
        brief = self.report()["brief"]
        self.assertEqual((brief["pieces"]["rating"], brief["pieces"]["mutants"], brief["pieces"]["blast"], brief["decider"]), ("not shown",) * 3 + ("owner",))
        self.write(impact_entry(), test_entry())
        self.assertEqual(self.report()["brief"]["decider"], "chief_of_staff")
        self.assertEqual(self.report(head="d" * 40)["brief"]["pieces"]["mutants"], "not shown", "the newer head has no record of its own")
        self.assertEqual(self.report(state=False)["brief"]["decider"], "owner")
        ledger = next(p for p in self.state.iterdir() if p.is_file() and p.suffix in (".jsonl", ".json", ".log", "") and p.stat().st_size > 0)
        ledger.write_text(ledger.read_text() + "{not json\n")   # a broken chain or an unreadable line
        brief = self.report()["brief"]
        self.assertEqual((brief["pieces"]["mutants"], brief["pieces"]["blast"], brief["decider"]), ("not shown", "not shown", "owner"))

    def test_s5_a_piece_missing_names_the_owner_and_the_piece(self):
        self.write(impact_entry(blast_radius=None), test_entry())
        brief = self.report()["brief"]
        self.assertEqual((brief["decider"], brief["blocking_gaps"]), ("owner", ["blast"]))
        self.assertIn("not shown: blast", brief["why"])

    def test_s6_an_owner_only_class_stays_the_owners_with_every_piece_recorded(self):
        self.write(impact_entry(), test_entry())
        for path in ("bin/delegated_clear.py", "bin/fleet-switchboard"):
            brief = self.report(path)["brief"]
            self.assertEqual(brief["decider"], "owner", path)
            self.assertEqual(brief["blocking_gaps"], [], "every piece is recorded, and the owner still decides")

    def test_s7_the_control_the_old_report_shows_the_pieces_as_not_shown(self):
        self.write(impact_entry(), test_entry())
        old = lambda state_dir, pr, head: {"rating": {"class": "code"}, "mutants": None, "blast": None}   # what the report read before this change
        with mock.patch.object(sb, "brief_pieces", old):
            brief = self.report()["brief"]
        self.assertEqual((brief["pieces"]["mutants"], brief["pieces"]["blast"], brief["decider"]), ("not shown", "not shown", "owner"))

    def plain(self, report):
        with mock.patch.object(sb, "build_engine", return_value=(mock.Mock(), None, None)), mock.patch.object(sb, "pr_floor_report", return_value=report), captured() as (out, err):
            code = sb.cmd_impact_floor(types.SimpleNamespace(pr=PR, rating=None, json=False), None, None)
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue()

    def test_s4_the_plain_print_carries_the_full_digest_and_the_text_to_pass_to_the_clear(self):
        self.write(impact_entry(), test_entry())
        report = self.report()
        brief = report["brief"]
        text = self.plain(report)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("impact floor for %s at %s:" % (PR, HEAD[:8])), lines[0])
        self.assertIn("IMPACT BRIEF %s against %s" % (HEAD[:8], BASE), lines)
        self.assertIn("  brief digest: %s" % brief["digest"], lines)
        flags = next(l for l in lines if "to clear as the Chief of Staff" in l)
        argv = shlex.split(text[text.index("--brief-digest"):text.index("\n  path floor")])
        self.assertEqual(argv[:2], ["--brief-digest", brief["digest"]])
        self.assertEqual(argv[2], "--brief-text")
        self.assertEqual(argv[3], brief["text"], "the text passes through the shell byte for byte")
        self.assertEqual(DC.brief_refusals(brief, argv[1], argv[3], ["the reviewed change is within the order"]), [], flags)

    def test_s4_the_print_without_a_digest_in_the_report_is_not_made_up(self):
        report = dict(self.report(), brief=None)
        self.assertNotIn("--brief-digest", self.plain(report))


def build(TestBlockTable):
    """The record path of the block lines, over the fixtures of TestBlockTable (a real `review record --block`)."""
    class TestBriefPiecesRecord(TestBlockTable):
        """`review record --block` keeps `mutants-run:`, `mutants-survived:` (test reviewer) and `blast-radius:` (impact reviewer) in the record, and refuses the rest."""

        def entries(self, reviewer):
            return [e for e in self.records() if e["reviewer"] == reviewer]

        def test_the_test_reviewers_mutant_counts_are_recorded_as_numbers(self):
            code, out, err = self.record(reviewer="test", extra="mutants-run: 12\nmutants-survived: 1\n")
            self.assertEqual(code, 0, err)
            (got,) = self.entries("test")
            self.assertEqual((got["mutants_run"], got["mutants_survived"]), (12, 1))
            self.assertIn("by_role", got)

        def test_the_impact_reviewers_blast_radius_is_recorded_as_one_line(self):
            code, out, err = self.record(extra="blast-radius: one module, no data\n")
            self.assertEqual(code, 0, err)
            (got,) = self.entries("impact")
            self.assertEqual(got["blast_radius"], "one module, no data")

        def test_a_field_for_the_other_reviewer_or_a_bad_value_is_refused_and_nothing_is_recorded(self):
            for reviewer, extra in (("impact", "mutants-run: 3\nmutants-survived: 0\n"), ("test", "blast-radius: small\n"), ("test", "mutants-run: x\nmutants-survived: 0\n"),
                                    ("test", "mutants-run: 3\n"), ("test", "mutants-run: 3\nmutants-survived: 4\n"), ("impact", "blast-radius: " + "x" * 81 + "\n")):
                code, out, err = self.record(reviewer=reviewer, extra=extra)
                self.assertEqual(code, 2, (reviewer, extra))
            self.assertEqual(self.records(), [])

        def test_a_record_without_the_lines_carries_no_piece(self):
            self.assertEqual(self.record(reviewer="test")[0], 0)
            (got,) = self.entries("test")
            self.assertTrue(not any(k in got for k in ("mutants_run", "mutants_survived", "blast_radius")))

    for name in dir(TestBlockTable):
        if name.startswith("test") and name not in vars(TestBriefPiecesRecord):
            setattr(TestBriefPiecesRecord, name, None)   # the inherited tests run once, on their own class
    return TestBriefPiecesRecord
