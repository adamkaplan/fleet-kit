"""#1215 integration: the real brief builder feeds the real decider and the brief-bound clear; every table of kinds is derived from the code; the four mutants fail.
Run from the repo root: python3 -m unittest tests.test_brief_integration   (also collected by bin/test-switchboard)."""
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import delegated_clear as dc  # noqa: E402
import impact_brief as ib  # noqa: E402
import brief_advisory as ba  # noqa: E402

HEAD, OTHER = "a" * 40, "b" * 40
MATRIX = json.loads((ROOT / "docs/review-laws/impact-matrix.json").read_text())
RULE = MATRIX["owner_hold"]["delegated_clear"]
SUB = dc.substance_rule(MATRIX)


def facts(path="tests/t.py", cls="tests", head=HEAD, **over):
    base = {"files": [{"path": path, "additions": 3, "deletions": 1, "status": "modified"}], "ci": {"state": "green", "head": head},
            "reads": [{"by": "reader-one", "verdict": "pass", "head": head}, {"by": "reader-two", "verdict": "pass", "head": head}], "rating": {"level": "Low"},
            "mutants": "4 of 4", "blast": "low", "head": head, "base": "main", "classes": {path: cls}, "body_claim": None}
    base.update(over)
    return base


def clear(brief, floor=None, path="tests/t.py", cls="tests", scored=None, reasons="read the diff; tests only", files=()):
    floor = floor if floor is not None else brief
    report = {"owner_decision": "hold", "complete": True, "top_tier_hits": {cls: [path]} if cls in MATRIX["owner_hold"]["top_tier_classes"] else {}, "changed_paths": [path], "head": floor["head"], "pr": "o/r#1"}
    out = dc.substance_refusals(RULE, report, list(files), lambda p, g: p in g, {}, scored, floor, brief["digest"] if brief else "", brief["text"] if brief else "", reasons)
    if not out:
        classes = {path: cls}
        out = [] if dc.decide_on_substance(floor, classes, SUB)[0] == "chief_of_staff" else ["escalate to the owner"]
    return out


class TestRealBriefToDecider(unittest.TestCase):
    def test_s2_tests_only_recommends_the_chief_of_staff_in_one_screen(self):
        b = ib.build_brief(facts())
        lines = b["text"].splitlines()
        self.assertLessEqual(len(lines), 24)
        self.assertIn("RECOMMENDED DECIDER: Chief of Staff", lines)
        self.assertEqual(len([l for l in lines if l.startswith("WHY:")]), 1)
        self.assertEqual(clear(b), [])

    def test_s3_s5_credentials_recommend_the_owner_and_follow_the_diff_not_the_text(self):
        b = ib.build_brief(facts("cloud/launcher/token_mint.py", "credentials", body_claim="tests only"))
        self.assertIn("RECOMMENDED DECIDER: owner", b["text"])
        self.assertIn("cloud/launcher/token_mint.py", b["text"])
        self.assertIn("credentials or issuance widened", b["text"])
        self.assertTrue(any("escalate to the owner" in r for r in clear(b, path="cloud/launcher/token_mint.py", cls="credentials")))

    def test_s4_s24_a_missing_piece_or_unknown_class_is_not_shown_and_the_owner(self):
        for over in ({"ci": None}, {"reads": None}):
            b = ib.build_brief(facts(**over))
            self.assertIn("not shown", b["text"])
            self.assertEqual(b["decider"], "owner")
            self.assertTrue(clear(b))
        b = ib.build_brief(facts(cls="a_class_new_in_this_change"))
        self.assertEqual((b["decider"], "not shown" in b["text"]), ("owner", True))

    def test_s6_an_owner_only_class_today_with_no_widening_is_cleared_on_the_substance(self):
        f = facts("cloud/launcher/token_mint.py", "credentials")
        f["files"][0].update(additions=0, deletions=5)   # the diff only removes: nothing widened
        b = ib.build_brief(f)
        self.assertEqual(b["decider"], "chief_of_staff", b["text"])
        self.assertTrue(dc.refusals(RULE, {"owner_decision": "hold", "complete": True, "top_tier_hits": {"credentials": ["cloud/launcher/token_mint.py"]}, "changed_paths": ["cloud/launcher/token_mint.py"], "head": HEAD}, [], lambda p, g: p in g, {}, None))
        self.assertEqual(clear(b, path="cloud/launcher/token_mint.py", cls="credentials"), [])

    def test_s8_s27_the_same_label_and_class_differ_by_the_brief(self):
        good, bad = ib.build_brief(facts()), ib.build_brief(facts("cloud/launcher/token_mint.py", "credentials"))
        self.assertEqual((good["pieces"]["rating"], bad["pieces"]["rating"]), ("level=Low", "level=Low"))
        self.assertEqual((good["decider"], bad["decider"]), ("chief_of_staff", "owner"))
        decided = [dc.decide_on_substance(good, {"tests/t.py": "tests"}, SUB)[0], dc.decide_on_substance(bad, {"x": "credentials"}, SUB)[0]]
        self.assertEqual(decided, ["chief_of_staff", "owner"])

    def test_s7_each_material_risk_is_refused_with_escalate(self):
        for cls, risk in (("credentials", "credentials_widened"), ("issuance", "issuance_widened"), ("spend", "spend"), ("release", "irreversible"), ("owner_voice", "speaks_for_owner")):
            b = ib.build_brief(facts("x/%s.py" % cls, cls))
            self.assertIn(risk, b["material"])
            self.assertTrue(any("escalate to the owner" in r for r in clear(b, path="x/%s.py" % cls, cls=cls)), cls)

    def test_s11_s12_no_brief_or_a_brief_of_another_head_is_refused(self):
        b = ib.build_brief(facts())
        self.assertTrue(any("no brief" in r for r in clear(None, floor=b)))
        old = ib.build_brief(facts(head=OTHER))
        out = clear(old, floor=b)
        self.assertTrue(any("differs" in r and old["digest"][:8] in r and b["digest"][:8] in r for r in out), out)

    def test_s15_a_machinery_path_needs_the_two_reads(self):
        b = ib.build_brief(facts("bin/impact_hold.py", "code"))
        rep = {"owner_decision": "hold", "complete": True, "top_tier_hits": {}, "changed_paths": ["bin/impact_hold.py"], "head": HEAD}
        one = dc.substance_refusals(RULE, rep, [{"filename": "bin/impact_hold.py", "status": "modified"}], lambda p, g: p in g or any(p == x for x in g), {}, None, b, b["digest"], b["text"], "read it")
        self.assertTrue(any("2 required" in r for r in one), one)

    def test_s26_the_brief_is_deterministic_and_fits_a_screen(self):
        a, b = ib.build_brief(facts()), ib.build_brief(facts())
        self.assertEqual(a["text"], b["text"])
        self.assertLessEqual(max(len(l) for l in a["text"].splitlines()), 100)
        self.assertEqual(a["digest"], hashlib.sha256(a["text"].encode()).hexdigest())

    def test_the_real_floor_facts_give_a_brief_for_a_non_top_tier_change(self):
        report = {"top_tier_hits": {}, "head": HEAD, "base": "main", "complete": True, "reads": [], "rating": None}
        b = ib.build_brief(ib.facts_from_report([{"path": "tests/x.py", "additions": 1, "deletions": 0, "changeType": "MODIFIED"}], report))
        self.assertEqual(b["head"], HEAD)
        self.assertEqual(b["decider"], "owner")   # CI, mutants and blast radius are not read from the records yet: not shown, so the owner


class TestTablesDerivedFromTheCode(unittest.TestCase):
    def test_the_material_lists_agree_across_the_modules(self):
        self.assertEqual(set(ib.MATERIAL), set(dc.MATERIAL_RISKS))
        self.assertEqual(set(ib.MATERIAL), set(SUB["material_risks"]))
        self.assertEqual(set(ib.MATERIAL), set(ib.MATERIAL_WORDS))

    def test_the_decider_table_covers_exactly_the_briefs_known_classes(self):
        self.assertEqual(set(SUB["owner_only"]) | set(SUB["chief_of_staff"]), set(ib.KNOWN_CLASSES))
        self.assertFalse(set(SUB["owner_only"]) & set(SUB["chief_of_staff"]))
        self.assertEqual(set(ib.CLASS_RISK) - set(ib.KNOWN_CLASSES), set())
        self.assertLessEqual(set(ib.OWNER_ONLY_CLASSES), set(SUB["owner_only"]))

    def test_every_matrix_top_tier_class_is_known_to_the_brief(self):
        self.assertLessEqual(set(MATRIX["owner_hold"]["top_tier_classes"]), set(ib.KNOWN_CLASSES))

    def test_the_brief_module_is_clear_code_and_never_carried(self):
        self.assertIn("bin/impact_brief.py", RULE["clear_code_paths"])
        self.assertIn("bin/impact_brief.py", MATRIX["owner_hold"]["never_carried_paths"])


class TestDocsAndNames(unittest.TestCase):
    DOC = (ROOT / "docs/switchboard.md").read_text()

    def test_s1_the_cause_names_the_four_places(self):
        cause = self.DOC[self.DOC.index("### Cause (from the recorded case)"):]
        cause = cause[:cause.index("\n\n", 40) + 400]
        for name in ("impact floor", "_apply_impact_hold_locked", "delegated_clear", "impact-matrix.json"):
            self.assertIn(name, cause)

    def test_s23_the_docs_state_four_items(self):
        for item in ("impact brief", "Who decides what", "credentials or issuance widened", "order of 23 October"):
            self.assertIn(item, self.DOC)

    def test_s28_no_private_name_in_the_new_files(self):
        pat = re.compile(r"ghp_[A-Za-z0-9]{10,}|BEGIN [A-Z ]*PRIVATE KEY|[\w.-]+@(?!example\.)[\w-]+\.(?:com|net|org)")
        for name in ("bin/impact_brief.py", "bin/brief_advisory.py", "tests/test_brief_integration.py", "tests/test_brief_clear.py", "tests/test_substance_decider.py"):
            text = (ROOT / name).read_text()
            if name.endswith("integration.py"):
                text = text.replace(pat.pattern, "")
            self.assertEqual(pat.findall(text), [], name)

    def test_s21_the_ten_cases_each_have_a_named_test(self):
        suite = (BIN / "test-switchboard").read_text()
        own = Path(__file__).read_text()
        cases = {"tests_only": "test_s2_tests_only", "credentials": "test_s3_s5_credentials", "not_shown": "test_s4_s24_a_missing_piece",
                 "differing_brief": "test_s11_s12_a_clear_with_no_brief_or_a_stale_brief", "self_clear": "test_s17_a_forged_record_of_the_author",
                 "author_clear": "test_s17_a_forged_record_of_the_author", "one_read": "test_s15_a_machinery_path_needs_the_two_reads",
                 "two_reads": "test_s12_s13_a_low_rating_never_outranks_the_machinery_list", "push_after_clear": "test_s10_s11_a_delegated_clear_at_one_head_is_a_hold_again",
                 "owner_plain_clear": "test_s2_only_the_chief_of_staff_may_use_the_option_and_the_owners_plain_clear_is_unchanged"}
        self.assertEqual(len(cases), 10)
        for case, name in cases.items():
            self.assertTrue(name in suite or name in own, case)


class TestMutants(unittest.TestCase):
    """S22: each mutant changes one place in a COPY of the source and a named test then fails."""

    def mutate(self, name, old, new):
        src = (BIN / name).read_text()
        self.assertIn(old, src, "the mutation anchor moved: " + name)
        return src.replace(old, new, 1)

    def run_suite_copy(self, name, mutated, *tokens):
        tmp = Path(tempfile.mkdtemp(prefix="mut-", dir="/tmp/opencode"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        shutil.copytree(BIN, tmp / "bin", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "docs", tmp / "docs", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "tests", tmp / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        (tmp / "bin" / name).write_text(mutated)
        done = subprocess.run([sys.executable, str(tmp / "bin" / "test-switchboard"), *tokens], capture_output=True, text=True, env={**__import__("os").environ, "FLEET_HEAVY": "off"}, timeout=600)
        return done

    def test_s22_dropping_the_brief_from_the_audit_line_fails_the_audit_test(self):
        mutated = self.mutate("impact_hold.py", 'detail["brief_digest"], detail["brief_text"] = brief["digest"], brief["text"]', "pass")
        done = self.run_suite_copy("impact_hold.py", mutated, "test_s10_the_audit")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("test_s10_the_audit_line_carries", done.stdout + done.stderr)

    def test_s22_accepting_a_stale_brief_fails_the_stale_test(self):
        mutated = self.mutate("delegated_clear.py", 'if brief_digest != floor_brief["digest"] or brief_text != floor_brief.get("text"):', "if False:")
        done = self.run_suite_copy("delegated_clear.py", mutated, "test_s11_s12_a_clear_with_no_brief")
        self.assertNotEqual(done.returncode, 0)

    def test_s22_skipping_the_read_count_clears_a_machinery_path_with_no_reads(self):
        mutated = self.mutate("delegated_clear.py", 'if scored is None and any(i["kind"] in ("machinery", "new-file", "closure") for i in items):', "if False:")
        spec = importlib.util.spec_from_loader("dc_mut", loader=None)
        mod = importlib.util.module_from_spec(spec)
        mod.__dict__["__file__"] = str(BIN / "delegated_clear.py")
        exec(compile(mutated, "dc_mut.py", "exec"), mod.__dict__)
        b = ib.build_brief(facts("bin/impact_hold.py", "code"))
        rep = {"owner_decision": "hold", "complete": True, "top_tier_hits": {}, "changed_paths": ["bin/impact_hold.py"], "head": HEAD}
        args = (RULE, rep, [{"filename": "bin/impact_hold.py", "status": "modified"}], lambda p, g: p in g, {}, None, b, b["digest"], b["text"], "read it")
        self.assertTrue(dc.substance_refusals(*args))
        self.assertEqual(mod.substance_refusals(*args), [], "the mutant clears with no read: TestRealBriefToDecider.test_s15 catches it")

    def test_s22_marking_every_class_chief_of_staff_clears_a_credentials_brief(self):
        mutated = self.mutate("delegated_clear.py", 'if floor_brief.get("decider") != "chief_of_staff" or material:', "if False:")
        spec = importlib.util.spec_from_loader("dc_mut2", loader=None)
        mod = importlib.util.module_from_spec(spec)
        mod.__dict__["__file__"] = str(BIN / "delegated_clear.py")
        exec(compile(mutated, "dc_mut2.py", "exec"), mod.__dict__)
        b = ib.build_brief(facts("x/c.py", "credentials"))
        rep = {"owner_decision": "hold", "complete": True, "top_tier_hits": {"credentials": ["x/c.py"]}, "changed_paths": ["x/c.py"], "head": HEAD}
        args = (RULE, rep, [], lambda p, g: p in g, {}, None, b, b["digest"], b["text"], "read it")
        self.assertTrue(dc.substance_refusals(*args))
        self.assertEqual(mod.substance_refusals(*args), [])


if __name__ == "__main__":
    unittest.main()
