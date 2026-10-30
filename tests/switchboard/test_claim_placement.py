"""A CLAIMS: line that is present in a pull request body but not read is refused, naming its line number and place (ask #1617).

The readers are fixtures of bin/test-switchboard (HOST is that module, set by its loader block). Neutral names (acme).
The mutants are the real source with one line replaced; each must fail `problems`, the one check the real source passes.
"""
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bin"))
import brief_advisory  # noqa: E402
import plan_ledger  # noqa: E402

HOST = None   # set by bin/test-switchboard
SCEN = [{"id": i, "proof": "test", "evaluator": "ci", "claim": "pr"} for i in ("S1", "S2", "S3", "U1")]
PLACES = (   # body, line number, place
    ("PLAN: acme/app#7\n\nCLAIMS: S1", 3, "after a blank line"),
    ("PLAN: acme/app#7\ncanon: n/a docs\nCLAIMS: S1", 3, "after another opening line"),
    ("CLAIMS: S1\nPLAN: acme/app#7", 1, "above the `PLAN:` line"),
    ("no plan here\nCLAIMS: S9", 2, "no `PLAN:` line"),
)
HIDDEN = (
    ("PLAN: acme/app#7\n```\nCLAIMS: S1\n```", 3, "code fence"),
    ("PLAN: acme/app#7\n~~~\nCLAIMS: S1\n~~~", 3, "code fence"),
    ("PLAN: acme/app#7\n<!--\nCLAIMS: S1\n-->", 3, "HTML comment"),
    ("PLAN: acme/app#7\n<!--\nCLAIMS: S1", 3, "HTML comment"),
)
TWO = (   # body, the line that must be named, the well placed line that must not be
    ("PLAN: acme/app#7\nCLAIMS: S1\nnotes\nCLAIMS: S2", 4, 2),
    ("PLAN: acme/app#7\n\nCLAIMS: S1\nCLAIMS: S2", 3, None),
)
SAME = (None, "", "PLAN: acme/app#7", "PLAN: acme/app#7\ncanon: n/a docs", "Notes\nPLAN: x and CLAIMS: S1", "PLAN: x\n    CLAIMS", "PLAN: x\nCLAIMS", "no plan at all")
WELL = ("PLAN: acme/app#7\nCLAIMS: S1", "PLAN: acme/app#7\n  claim line follows\n".replace("  claim line follows\n", "CLAIMS: S2 S3"), "```\nPLAN: acme/x#1\n```\nPLAN: acme/app#7\nCLAIMS: S1\n")


def problems(pl, ba):
    """What the module pair gets wrong; [] when it is right."""
    bad = []
    for body, line, place in PLACES + HIDDEN:
        claimed, _, error = pl.pr_scenarios(SCEN, body)
        if claimed or not error or "line %d" % line not in error or place not in error or "directly under the `PLAN:` line" not in error:
            bad.append("placement: %r" % body)
        if place in ("code fence", "HTML comment") and "inline" not in (error or ""):
            bad.append("fix: %r" % body)
    for body, named, well in TWO:
        error = pl.pr_scenarios(SCEN, body)[2]
        if not error or "line %d" % named not in error or (well and "line %d" % well in error):
            bad.append("two: %r" % body)
    for body, ids in zip(WELL, (["S1"], ["S2", "S3"], ["S1"])):
        got = pl.pr_scenarios(SCEN, body)
        if got[2] is not None or [s["id"] for s in got[0]] != ids:
            bad.append("well placed: %r" % body)
    for body in SAME:
        if pl.pr_scenarios(SCEN, body)[2] is not None or len(pl.pr_scenarios(SCEN, body)[0]) != 4:
            bad.append("same: %r" % body)
    if ba.rationale_defects("MERGE: captain\nPLAN: x\nCLAIMS: S1\nRationale: one sentence here.".split("\n")) != []:
        bad.append("opening")
    if len(ba.rationale_defects("MERGE: captain\nPLAN: x\nCLAIMS: S1\nBody text".split("\n"))) != 1:
        bad.append("missing rationale")
    return bad


def mutant(path, old, new):
    text = Path(REPO, path).read_text()
    assert text.count(old) == 1, old
    mod = types.ModuleType(Path(path).stem + "_mutant")
    mod.__file__ = str(REPO / path)
    exec(compile(text.replace(old, new), path, "exec"), mod.__dict__)
    return mod, text.replace(old, new) != text


def callers():
    """(file, function) of every call of pr_claim or pr_scenarios in bin/, found by search."""
    found = set()
    for path in sorted(REPO.glob("bin/*")):
        if path.is_dir() or path.name.startswith("test") or path.suffix not in ("", ".py"):
            continue
        lines = path.read_text(errors="replace").split("\n")
        for n, text in enumerate(lines):
            if re.search(r"pr_claim\(|pr_scenarios\(", text) and not text.lstrip().startswith(("def ", "#", '"""', "`")):
                at = n
                while at > 0 and not lines[at].startswith("def "):
                    at -= 1
                found.add((path.name, re.match(r"def (\w+)", lines[at]).group(1)))
    return found


class TestClaimPlacement(unittest.TestCase):
    def test_s2_four_visible_placements_are_refused_with_the_line_and_the_place(self):
        for body, line, place in PLACES:
            with self.subTest(body=body):
                claimed, left, error = plan_ledger.pr_scenarios(SCEN, body)
                self.assertEqual((claimed, left), ([], []))
                self.assertIn("line %d" % line, error)
                self.assertIn(place, error)
                self.assertIn("must stand directly under the `PLAN:` line", error)

    def test_s3_a_fence_or_a_comment_is_refused_and_says_to_write_the_example_inline(self):
        for body, line, place in HIDDEN:
            with self.subTest(body=body):
                error = plan_ledger.pr_scenarios(SCEN, body)[2]
                self.assertIn("line %d" % line, error)
                self.assertIn("inside a %s" % place if place.startswith("code") else place, error)
                self.assertIn("not at the start of a line", error)

    def test_s4_the_second_or_the_first_misplaced_line_is_named_never_the_well_placed_one(self):
        for body, named, well in TWO:
            with self.subTest(body=body):
                error = plan_ledger.pr_scenarios(SCEN, body)[2]
                self.assertIn("line %d" % named, error)
                if well:
                    self.assertNotIn("line %d" % well, error)

    def test_s6_bodies_without_a_line_at_line_start_and_well_placed_ones_equal_the_baseline(self):
        every = [s["id"] for s in SCEN]
        for body in SAME:
            with self.subTest(body=body):
                self.assertEqual(plan_ledger.pr_scenarios(SCEN, body), (SCEN, [], None))
                self.assertEqual(plan_ledger.pr_claim(body, SCEN), (None, None))
        self.assertEqual([s["id"] for s in plan_ledger.pr_scenarios(SCEN, WELL[1])[0]], ["S2", "S3"])
        self.assertEqual(every, ["S1", "S2", "S3", "U1"])

    def test_u2_a_body_whose_first_plan_lines_are_fenced_examples_claims_only_s1_with_no_error(self):
        body = "```\nPLAN: acme/other#9\nCLAIMS: nonsense\n```\nPLAN: acme/app#7\nCLAIMS: S1\n"
        # a fenced CLAIMS line is itself refused (S3); the fenced PLAN examples alone are read past
        body = "```\nPLAN: acme/other#9\n```\n~~~\nPLAN: acme/other#8\n~~~\nPLAN: acme/app#7\nCLAIMS: S1\n"
        claimed, left, error = plan_ledger.pr_scenarios(SCEN, body)
        self.assertEqual(([s["id"] for s in claimed], error), (["S1"], None))

    def test_s7_the_opening_check_skips_the_claim_line(self):
        self.assertEqual(brief_advisory.rationale_defects(["MERGE: captain", "PLAN: acme/app#7", "CLAIMS: S1", "Rationale: one sentence here."]), [])
        self.assertEqual(len(brief_advisory.rationale_defects(["MERGE: captain", "PLAN: acme/app#7", "CLAIMS: S1", "Cause first."])), 1)

    def test_u3_the_check_is_inside_pr_claim_and_pr_scenarios_is_still_read_three_times(self):
        text = (REPO / "bin" / "plan_ledger.py").read_text()
        self.assertEqual(text.count("pr_scenarios("), 3)
        self.assertRegex(text, r"(?s)def pr_claim\(.*?misplaced_claim\(body\)")
        self.assertEqual(problems(plan_ledger, brief_advisory), [])

    def test_s9_the_documentation_states_the_four_things(self):
        docs = (REPO / "docs" / "switchboard.md").read_text()
        at = docs.index("**A `CLAIMS:` line that is not read is refused (#1617).**")
        part = docs[at:at + 3000]
        for statement in ("directly under the `PLAN:` line", "after a blank line", "line number", "write the example inline, not at the start of a line", "skips the claim line", "fails closed"):
            self.assertIn(statement, part)


class TestClaimMutants(unittest.TestCase):
    def test_s8_each_of_four_mutants_is_applied_and_caught(self):
        pl = "bin/plan_ledger.py"
        cases = ((pl, "    if misplaced:\n        return None, misplaced", "    if False:\n        return None, misplaced"),
                 (pl, "        if at == read:\n            continue", "        if False:\n            continue"),
                 (pl, "        if at == read:\n            continue", "        if at == read or read is not None:\n            continue"),
                 ("bin/brief_advisory.py", "PLAN|MERGE|canon|CLAIMS", "PLAN|MERGE|canon"))
        for path, old, new in cases:
            with self.subTest(mutant=new):
                mod, applied = mutant(path, old, new)
                self.assertTrue(applied)
                pair = (mod, brief_advisory) if path == pl else (plan_ledger, mod)
                self.assertNotEqual(problems(*pair), [])


class TestClaimReaders(unittest.TestCase):
    """S5: every reader found by search refuses a body whose claim line is misplaced."""

    BODY = "PLAN: acme/app#7\n\nCLAIMS: S1"

    def setUp(self):
        if HOST is None:
            self.skipTest("the readers are fixtures of bin/test-switchboard")
    READERS = {("plan_ledger.py", "pr_scenarios"), ("plan_ledger.py", "covered_ids"), ("plan_ledger.py", "claim_union"), ("plan_ledger.py", "closed_unclaimed"),
               ("plan_ledger.py", "evaluate_gates"), ("fleet-switchboard", "_scenario_of"), ("fleet-switchboard", "cmd_verdict"), ("fleet-switchboard", "claim_union_refusal"),
               ("fleet-switchboard", "unclaimed_verdict"), ("fleet-switchboard", "claim_union_note"), ("fleet-switchboard", "ci_record_core")}

    def test_the_readers_found_by_search_are_the_readers_tested(self):
        self.assertEqual(callers(), self.READERS)

    def fixture(self, cls, name):
        fx = getattr(HOST, cls)(name)
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        return fx

    def test_every_reader_refuses_and_names_the_line(self):
        fx = self.fixture("TestPrClaimLine", "test_s3_the_gate_counts_exactly_the_claimed_scenarios_and_leaves_the_rest_to_the_epic")
        fx.pr["body"] = self.BODY
        row = fx.g1()   # evaluate_gates
        self.assertEqual(row["status"], "missing")
        self.assertTrue(any("line 3" in m and "after a blank line" in m for m in row["missing"]), row)
        code, _, err = fx.prove("coder", "S1")   # _scenario_of
        self.assertNotEqual(code, 0)
        self.assertIn("line 3", err)
        code, _, err = fx.verdict("cibot", "S1", "ci")   # cmd_verdict
        self.assertNotEqual(code, 0)
        self.assertIn("line 3", err)
        code, report, _ = fx.gates()   # claim_union_note
        self.assertNotIn("claim_union", report)
        ux = self.fixture("TestClaimUnionAtLastMerge", "test_s2_two_prs_claiming_s1_and_s2_of_three_leave_s3_and_the_union_is_pure")
        ux.pr["body"] = self.BODY
        self.assertIsNotNone(ux.refusal([("merged", "S1 S2 S3")]))   # claim_union_refusal
        self.assertIsNotNone(ux.refusal([("open", "S2 S3 U1")], failing=True))   # unclaimed_verdict (the list fails)

    def test_the_pure_readers_refuse_too(self):
        self.assertEqual(plan_ledger.covered_ids(SCEN, [self.BODY]), set())
        applies, uncovered, last = plan_ledger.claim_union(SCEN, self.BODY, [("merged", "PLAN: x\nCLAIMS: S2 S3 U1")])
        self.assertEqual((applies, sorted(uncovered), last), (True, ["S1"], True))
        self.assertEqual(plan_ledger.closed_unclaimed(SCEN, [("merged", "PLAN: x\nCLAIMS: S2 S3 U1")], [(5, self.BODY)]), (["S1"], [5]))
