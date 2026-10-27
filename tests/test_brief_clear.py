"""#1215 slice C: the brief-bound clear (pure checks over fixture briefs) and its mutants. Run: python3 -m unittest tests.test_brief_clear  (from the repo root)."""
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import delegated_clear as dc  # noqa: E402

H = "a" * 40
RULE = {"never_path_classes": ["credentials"], "top_tier_review_classes": ["credentials"], "clear_code_paths": ["bin/impact_hold.py"],
        "new_file_globs": ["bin/*.py"], "closure_files": []}
REPORT = {"owner_decision": "hold", "registrar_reasons": [], "complete": True, "top_tier_hits": {"credentials": ["cloud/token_mint.py"]}, "changed_paths": ["cloud/token_mint.py"], "head": H, "pr": "o/r#1"}


def match(path, globs):
    return path in globs


def brief(decider="chief_of_staff", material=(), text="IMPACT BRIEF\nRECOMMENDED DECIDER: Chief of Staff"):
    return {"head": H, "text": text, "digest": hashlib.sha256(text.encode()).hexdigest(), "decider": decider, "material": list(material), "why": "tests only."}


def refuse(floor, given=None, reasons="read the diff, tests only", report=REPORT, files=(), scored=None):
    given = given if given is not None else floor
    digest = given["digest"] if given else ""
    text = given["text"] if given else ""
    return dc.substance_refusals(RULE, report, list(files), match, {}, scored, floor, digest, text, reasons)


class TestBriefBoundClear(unittest.TestCase):
    def test_chief_clears_owner_only_class_on_substance(self):   # S6: the class ceiling no longer refuses
        self.assertEqual(refuse(brief()), [])
        self.assertTrue(dc.refusals(RULE, REPORT, [], match, {}, None))   # the old class ceiling would refuse it

    def test_each_material_risk_escalates(self):   # S7
        for risk in dc.MATERIAL_RISKS:
            with self.subTest(risk=risk):
                out = refuse(brief("chief_of_staff", [risk]))
                self.assertTrue(any("escalate to the owner" in r for r in out), out)

    def test_decider_owner_escalates(self):   # S8 / S27: the brief decides
        self.assertEqual(refuse(brief()), [])
        self.assertTrue(any("escalate to the owner" in r for r in refuse(brief("owner"))))

    def test_no_brief_refused(self):   # S11
        out = dc.substance_refusals(RULE, REPORT, [], match, {}, None, brief(), "", "", "reasons here")
        self.assertTrue(any("no brief" in r for r in out))

    def test_stale_brief_refused_names_both_hashes(self):   # S12
        old, new = brief(text="old brief"), brief(text="new brief")
        out = refuse(new, given=old)
        self.assertEqual(len(out), 1)
        self.assertIn(old["digest"][:8], out[0])
        self.assertIn(new["digest"][:8], out[0])

    def test_unbuildable_floor_brief_refused(self):
        self.assertTrue(refuse(None, given=brief()))

    def test_no_reasons_refused(self):
        self.assertTrue(any("no reasons" in r for r in refuse(brief(), reasons=" ")))

    def test_digest_must_match_text(self):
        b = brief()
        out = dc.substance_refusals(RULE, REPORT, [], match, {}, None, b, "0" * 64, b["text"], "why not")
        self.assertTrue(out)

    def test_machinery_needs_two_reads(self):   # S15: one read (no counted score) refused; the scored path (judge, 2 required) allows
        files = [{"filename": "bin/impact_hold.py", "status": "modified"}]
        rep = dict(REPORT, changed_paths=["bin/impact_hold.py"], top_tier_hits={})
        out = refuse(brief(), files=files, report=rep, scored=None)
        self.assertTrue(any("2 required" in r for r in out), out)
        self.assertEqual(refuse(brief(), files=files, report=rep, scored={"level": "Low", "reasons": [], "runs": [1, 2]}), [])
        one = refuse(brief(), files=files, report=rep, scored={"level": "Low", "reasons": ["1 counted impact rating at aaaaaaaa, 2 required"]})
        self.assertTrue(any("2 required" in r for r in one))

    def test_unreadable_still_refused(self):
        self.assertTrue(refuse(brief(), files=[{"nofile": 1}]))

    def test_kind_table_is_complete(self):   # every refusal kind of the code is lifted or kept on purpose
        src = Path(dc.__file__).read_text()
        kinds = set(__import__("re").findall(r'_item\("([a-z-]+)"', src))
        self.assertEqual(kinds, set(dc.SUBSTANCE_LIFTED) | {"rating", "unreadable", "registrar"})   # registrar (#964) is kept on purpose: it is not lifted by a brief

    def test_lifted_kinds_each_drop_only_with_brief(self):   # one mutant per member: a kind kept would refuse the owner-only class
        for kind in dc.SUBSTANCE_LIFTED:
            with self.subTest(kind=kind):
                self.assertIn(kind, ("class", "review", "machinery", "new-file", "closure"))


class TestMutants(unittest.TestCase):
    """Each mutant is a patched module function; the shipped tests above must fail under it (checked here by running the check the test makes)."""

    def test_a_report_with_no_registrar_answer_is_refused_on_the_brief_path(self):   # the #971 interaction: no answer is never read as clean
        rep = {k: v for k, v in REPORT.items() if k != "registrar_reasons"}
        out = refuse(brief(), report=rep)
        self.assertTrue(any("registrar lint's answer cannot be read" in r for r in out), out)
        self.assertEqual(refuse(brief()), [])   # with the answer (no finding) the brief clears

    def test_mutant_brief_path_accepts_a_report_with_no_registrar_answer(self):
        rep = {k: v for k, v in REPORT.items() if k != "registrar_reasons"}
        real = dc.refusal_items

        def lenient(*a, **k):
            items, tidy = real(*a, **k)
            return [i for i in items if "registrar lint's answer cannot be read" not in i["text"]], tidy
        dc.refusal_items = lenient
        try:
            self.assertEqual(refuse(brief(), report=rep), [])   # the mutant accepts: the test above would fail
        finally:
            dc.refusal_items = real
        self.assertTrue(refuse(brief(), report=rep))


    def test_accept_stale_brief_mutant(self):
        old, new = brief(text="old"), brief(text="new")
        real = dc.brief_refusals
        dc.brief_refusals = lambda f, d, t, r: []
        try:
            self.assertEqual(refuse(new, given=old), [])   # the mutant accepts: test_stale_brief_refused would fail
        finally:
            dc.brief_refusals = real
        self.assertTrue(refuse(new, given=old))

    def test_every_class_chief_mutant(self):
        real = dc.SUBSTANCE_LIFTED
        self.assertEqual(refuse(brief("owner")) != [], True)
        dc.brief_refusals, orig = (lambda f, d, t, r: []), dc.brief_refusals
        try:
            self.assertEqual(refuse(brief("owner")), [])   # mutant clears the owner's: test_decider_owner_escalates fails
        finally:
            dc.brief_refusals = orig
        self.assertIs(real, dc.SUBSTANCE_LIFTED)


if __name__ == "__main__":
    unittest.main()
