"""#1215 slice B: the decider follows the brief's substance. Run from the repo root: python3 -m unittest tests.test_substance_decider"""
import importlib.util, json, re, shutil, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATRIX = json.loads((ROOT / "docs/review-laws/impact-matrix.json").read_text())
SRC = ROOT / "bin/delegated_clear.py"
RISKS = ("credentials_widened", "issuance_widened", "spend", "irreversible", "speaks_for_owner", "authority_widened")


def load(path=SRC):
    spec = importlib.util.spec_from_file_location("dc_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DC = load()
BRIEF = load(ROOT / "bin/impact_brief.py")
PIECES = {"what": "edits tests", "ci": "green", "reads": "2 independent at head"}


def brief(material=(), loosening=(), pieces=None):
    return {"head": "a" * 40, "pieces": dict(PIECES if pieces is None else pieces), "material": list(material), "loosening": list(loosening), "rating": "Low"}


def decide(b, classes, mod=DC, matrix=MATRIX):
    return mod.decide_on_substance(b, classes, mod.substance_rule(matrix))[0]


class Decider(unittest.TestCase):
    def test_S6_owner_only_today_class_without_material_risk_goes_to_chief(self):
        self.assertEqual(decide(brief(), ["authority"]), "chief_of_staff")
        self.assertEqual(decide(brief(), ["identity"]), "chief_of_staff")

    def test_S7_each_material_risk_is_escalated(self):
        for risk in RISKS:
            d, why = DC.decide_on_substance(brief([risk]), ["tests"], DC.substance_rule(MATRIX))
            self.assertEqual(d, "owner", risk)
            self.assertIn("escalate to the owner", why)

    def test_S8_same_label_and_class_differ_by_brief(self):
        a, b = brief(), brief(["credentials_widened"])
        self.assertEqual((a["rating"], b["rating"]), ("Low", "Low"))
        self.assertEqual(decide(a, ["code"]), "chief_of_staff")
        self.assertEqual(decide(b, ["code"]), "owner")

    def test_S9_owner_only_classes_never_go_to_chief(self):
        for cls in ("merge_shim", "clear_machinery", "an_unnamed_class"):
            self.assertEqual(decide(brief(), [cls]), "owner", cls)

    def test_S9_mutant_every_class_chief_is_caught(self):
        # mutant 1: the code skips the owner-only check; mutant 2: the matrix marks every class chief of staff
        text = SRC.read_text()
        self.assertIn('if c in rule["owner_only"]:', text)
        tmp = Path(tempfile.mkdtemp(prefix="dc-")) / "dc_mutant.py"
        self.addCleanup(shutil.rmtree, str(tmp.parent), True)
        tmp.write_text(text.replace('if c in rule["owner_only"]:', 'if False:').replace('if c not in rule["chief_of_staff"]:', 'if False:'))
        mut = load(tmp)
        cleared = [c for c in ("merge_shim", "clear_machinery", "an_unnamed_class") if decide(brief(), [c], mut) != "owner"]
        self.assertEqual(len(cleared), 3)  # the mutant clears all three owner-only/unknown classes; the shipped build clears none
        m = json.loads(json.dumps(MATRIX))
        r = m["owner_hold"]["substance_decider"]
        r["chief_of_staff"] += r["owner_only"]
        r["owner_only"] = []
        self.assertEqual([c for c in ("merge_shim", "clear_machinery", "an_unnamed_class") if decide(brief(), [c], matrix=m) != "owner"], ["merge_shim", "clear_machinery"])

    def test_mutant_decide_by_label_or_class_alone_is_caught(self):
        text = SRC.read_text()
        tmp = Path(tempfile.mkdtemp(prefix="dc-")) / "dc_mutant2.py"
        self.addCleanup(shutil.rmtree, str(tmp.parent), True)
        tmp.write_text(text.replace('if material or any(m not in rule["material_risks"] for m in material):', 'if False:'))
        mut = load(tmp)
        self.assertEqual(decide(brief(["spend"]), ["docs"], mut), "chief_of_staff")  # the mutant ignores the brief ...
        self.assertEqual(decide(brief(["spend"]), ["docs"]), "owner")  # ... the shipped build does not

    def test_S24_unknown_class_and_not_shown_go_to_owner(self):
        self.assertEqual(decide(brief(), ["brand_new_class"]), "owner")
        self.assertEqual(decide(brief(), []), "owner")
        self.assertEqual(decide(brief(pieces={"what": "x", "ci": "not shown"}), ["tests"]), "owner")
        self.assertEqual(decide(brief(loosening=["an accept path"]), ["tests"]), "owner")
        self.assertEqual(decide(None, ["tests"]), "owner")
        self.assertEqual(DC.decide_on_substance(brief(), ["tests"], None)[0], "owner")

    def test_S27_the_brief_decides_not_the_class_name_or_label(self):
        self.assertEqual(decide(brief(), ["authority"]), "chief_of_staff")
        self.assertEqual(decide(brief(["spend"]), ["docs"]), "owner")

    def test_why_is_one_sentence(self):
        for b, c in ((brief(), ["tests"]), (brief(["spend"]), ["tests"]), (brief(), ["credentials"])):
            why = DC.decide_on_substance(b, c, DC.substance_rule(MATRIX))[1]
            self.assertEqual(len(re.findall(r"[.!?](\s|$)", why)), 1, why)

    def test_table_complete_against_matrix(self):
        r = MATRIX["owner_hold"]["substance_decider"]
        self.assertFalse(set(r["owner_only"]) & set(r["chief_of_staff"]))
        oh = MATRIX["owner_hold"]
        classes = set(oh["top_tier_classes"]) | set(oh["other_classes"])
        self.assertLessEqual(classes & set(BRIEF.KNOWN_CLASSES), set(r["owner_only"]) | set(r["chief_of_staff"]))
        self.assertEqual(set(r["owner_only"]) | set(r["chief_of_staff"]), set(BRIEF.KNOWN_CLASSES))   # derived from the code: bin/impact_brief.py
        self.assertEqual(set(r["material_risks"]), set(RISKS))

    def test_backstop_block_untouched(self):
        self.assertIn("never_path_classes", MATRIX["owner_hold"]["delegated_clear"])


if __name__ == "__main__":
    unittest.main()
