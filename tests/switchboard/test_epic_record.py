"""The epic block lives in bin/epic_record.py and reads the entry's plan_ledger (#1383, PC-5, PC-6): U5, U6, U7.

HOST is bin/test-switchboard (set by its loader block). The ORACLE is a copy of the pre-move functions (origin/registry/1383:bin/plan_ledger.py, lines 684-757), bound to the plan_ledger's claim_of and evaluator_of.
Neutral names (acme).
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bin"))
import plan_ledger  # noqa: E402

HOST = None   # set by bin/test-switchboard
NAMES = ("EPIC_VIA", "EPIC_STATES", "epic_scenarios", "epic_state", "epic_lines")


def _build_oracle(evaluator_of, claim_of):
    """The pre-move epic block, copied unchanged; returns its names."""
    EPIC_VIA = "epic"
    EPIC_STATES = ("unrecorded", "recorded", "stale")


    def epic_scenarios(plan):
        """The scenarios of a checked plan that are claimed at the epic, in order."""
        return [s for s in (plan or {}).get("scenarios_parsed") or [] if claim_of(s) == "epic"]


    def epic_state(scenario, entries, ref, plan_hash, default_head, authors, contains, changed):
        """One epic-claimed scenario's picture on its owning issue `ref`: {id, evaluator, claim, state, status, head, missing[], void[], why}. `state` is `unrecorded` (no proof and verdict of one commit, a fail, a verdict by a writer, or a record whose commit lacks a
        claimant's merge), `recorded` (at `head`; `current` when it is the default branch's head) or `stale` (a file the proof names changed since, or the change cannot be judged). `contains(head)` -> (True, None) or (False, reason);
        `changed(old, new)` -> the files changed, or None when the compare cannot be judged (it counts as stale); `default_head` None (unreadable) makes every record stale. The newest proof decides the commit; the verdict must be at the same commit."""
        sid, evaluator = scenario["id"], evaluator_of(scenario)
        out = {"id": sid, "evaluator": evaluator, "claim": "epic", "state": "unrecorded", "status": "missing", "head": None, "current": False, "missing": [], "void": [], "why": None}
        mine = [e for e in entries if e.get("pr") == ref and e.get("scenario") == sid and e.get("via") == EPIC_VIA and e.get("kind") in ("proof", "verdict")]
        here = [e for e in mine if e.get("plan_hash") == plan_hash]
        if mine and not here:
            out["void"].append("record #%d void: plan changed" % mine[-1]["seq"])
        proofs = [e for e in here if e["kind"] == "proof"]
        proof = proofs[-1] if proofs else None
        if proof is None:
            out["missing"].append("proof (`prove %s --pr %s`)" % (sid, ref))
        elif proof.get("result") != "pass":
            out["missing"].append("proof says fail")
        verdicts = [e for e in here if e["kind"] == "verdict" and e.get("evaluator") == evaluator and (proof is None or e.get("head") == proof.get("head"))]
        verdict = verdicts[-1] if verdicts else None
        if verdict is None:
            out["missing"].append("%s verdict at %s (`verdict %s --pr %s --as %s`)" % (evaluator, proof["head"][:8] if proof else "the proof's commit", sid, ref, evaluator))
        elif verdict.get("result") != "pass":
            out["missing"].append("%s verdict says fail" % evaluator)
        elif verdict.get("by") in authors:
            out["missing"].append("the verdict is by a writer of the record (not accepted)")
        if out["missing"]:
            return out
        head = proof["head"]
        ok, why = contains(head)
        if not ok:
            out["void"].append("the record at %s is void: %s" % (head[:8], why))
            out["missing"].append("a record at a commit that holds every merged pull request of the plan (%s)" % why)
            return out
        out.update(head=head, state="recorded", status="ok")
        if default_head is None:
            out.update(state="stale", status="missing", why="the default branch's head could not be read")
        elif head == default_head:
            out["current"] = True
        else:
            files = changed(head, default_head)
            named = set(proof.get("files") or ())
            if files is None:
                out.update(state="stale", status="missing", why="the change from %s to %s cannot be judged" % (head[:8], default_head[:8]))
            elif files and (not named or named & set(files)):
                out.update(state="stale", status="missing", why="%s changed since %s" % (", ".join(sorted(named & set(files)) or sorted(files)[:3]), head[:8]))
            else:
                out["why"] = "no file the proof names changed since %s" % head[:8]
        if out["state"] == "stale":
            out["missing"].append("stale: %s (record it again at the default branch's head)" % out["why"])
        return out


    def epic_lines(rows, default_head):
        """The lines `gates` prints for the epic-claimed scenarios: one per scenario, unrecorded, recorded at a head, or stale."""
        lines = []
        for r in rows:
            if r["state"] == "unrecorded":
                text = "unrecorded: %s" % "; ".join(r["missing"] + r["void"])
            elif r["state"] == "stale":
                text = "recorded at %s, STALE: %s" % (r["head"][:8], r["why"])
            else:
                text = "recorded at %s (%s)" % (r["head"][:8], "the default branch's head" if r["current"] else "still valid at %s: %s" % ((default_head or "?")[:8], r["why"]))
            lines.append("  epic %s (%s): %s" % (r["id"], r["evaluator"], text))
        return lines
    return dict(EPIC_VIA=EPIC_VIA, EPIC_STATES=EPIC_STATES, epic_scenarios=epic_scenarios, epic_state=epic_state, epic_lines=epic_lines)


def epic():
    return HOST.sb.load_module("epic_record")


PH, REF, H1, H2 = "p" * 8, "acme/app#5", "1" * 40, "2" * 40
SC = {"id": "E1", "evaluator": "tester", "proof": "test", "claim": "epic"}


def proof(**k):
    return dict({"seq": 1, "pr": REF, "scenario": "E1", "via": "epic", "kind": "proof", "plan_hash": PH, "result": "pass", "head": H1, "files": ["a.py"]}, **k)


def verdict(**k):
    return dict({"seq": 2, "pr": REF, "scenario": "E1", "via": "epic", "kind": "verdict", "plan_hash": PH, "result": "pass", "head": H1, "evaluator": "tester", "by": "acme-tester"}, **k)


YES = lambda head: (True, None)
NO = lambda head: (False, "no merge of acme/app#4")
CASES = {   # name -> (entries, default_head, authors, contains, changed)
    "unrecorded": ([], H1, set(), YES, lambda a, b: []),
    "recorded and current": ([proof(), verdict()], H1, set(), YES, lambda a, b: []),
    "recorded and not current": ([proof(), verdict()], H2, set(), YES, lambda a, b: ["b.py"]),
    "stale": ([proof(), verdict()], H2, set(), YES, lambda a, b: ["a.py"]),
    "stale, compare unreadable": ([proof(), verdict()], H2, set(), YES, lambda a, b: None),
    "stale, head unreadable": ([proof(), verdict()], None, set(), YES, lambda a, b: []),
    "a proof that fails": ([proof(result="fail"), verdict()], H1, set(), YES, lambda a, b: []),
    "a verdict by a writer": ([proof(), verdict(by="acme-writer")], H1, {"acme-writer"}, YES, lambda a, b: []),
    "a record at a head without a merge": ([proof(), verdict()], H1, set(), NO, lambda a, b: []),
    "a record of another plan": ([proof(plan_hash="x"), verdict(plan_hash="x")], H1, set(), YES, lambda a, b: []),
}


class TestEpicRecordPlacement(unittest.TestCase):
    def test_u5_the_names_are_defined_in_epic_record_and_plan_ledger_defines_none(self):
        text = (REPO / "bin" / "epic_record.py").read_text()
        for n in ("EPIC_VIA", "EPIC_STATES"):
            self.assertRegex(text, r"(?m)^%s = " % n)
        for n in ("epic_scenarios", "epic_state", "epic_lines"):
            self.assertRegex(text, r"(?m)^def %s\(" % n)
        pl = (REPO / "bin" / "plan_ledger.py").read_text()
        for n in NAMES + ("epic_record", "importlib"):
            self.assertNotRegex(pl, r"(?m)^(def |class )?%s\b" % n)
            self.assertFalse(hasattr(plan_ledger, n), n)
        self.assertLess(len(pl.splitlines()), 1500)
        for n in NAMES:
            self.assertTrue(hasattr(epic(), n), n)

    def test_u5_the_entry_reaches_the_module_through_load_module(self):
        src = (REPO / "bin" / "fleet-switchboard").read_text()
        self.assertEqual(len(re.findall(r"(?<![`.\w])plan_ledger\.(?:epic_\w+|EPIC_\w+)", src)), 0)
        self.assertGreaterEqual(len(re.findall(r'load_module\("epic_record"\)\.', src)), 7)

    def test_u6_the_floor_holds_a_pull_request_that_edits_only_epic_record(self):
        globs, exclude, force = HOST.sb.impact_floor.floor_for("adamkaplan/fleet-kit")
        for path in ("bin/epic_record.py", "bin/plan_ledger.py"):
            self.assertEqual(HOST.sb.impact_floor.floor_paths([path], globs, exclude, force), [path])
        import json
        m = json.loads((REPO / "docs" / "review-laws" / "impact-matrix.json").read_text())
        self.assertIn("bin/epic_record.py", m["owner_hold"]["top_tier_paths"]["authority"])
        self.assertIn("bin/epic_record.py", m["owner_hold"]["floor"]["adamkaplan/fleet-kit"]["globs"])
        others = json.dumps([m["owner_hold"]["never_carried_paths"],
                             m["owner_hold"]["substance_decider"]["owner_only_paths"]["clear_machinery"], m["owner_hold"]["delegated_clear"]["clear_code_paths"]])
        self.assertEqual(others.count("epic_record"), 0)


class TestEpicRecordMove(unittest.TestCase):
    def setUp(self):
        self.oracle = _build_oracle(plan_ledger.evaluator_of, plan_ledger.claim_of)

    def test_u7_the_fixture_table_has_0_differing_cells(self):
        differ = []
        for name, (entries, default, authors, contains, changed) in CASES.items():
            args = (SC, entries, REF, PH, default, authors, contains, changed)
            if self.oracle["epic_state"](*args) != epic().epic_state(*args):
                differ.append(name)
        rows = [self.oracle["epic_state"](SC, e, REF, PH, d, a, c, ch) for e, d, a, c, ch in CASES.values()]
        self.assertEqual(self.oracle["epic_lines"](rows, H2), epic().epic_lines(rows, H2))
        self.assertEqual(len({r["state"] for r in rows}), 3)
        plan = {"scenarios_parsed": [SC, {"id": "S1", "evaluator": "ci", "proof": "test", "claim": "pr"}, {"id": "M1", "evaluator": "tester", "proof": "measure"}]}
        self.assertEqual(self.oracle["epic_scenarios"](plan), epic().epic_scenarios(plan))
        self.assertEqual([s["id"] for s in epic().epic_scenarios(plan)], ["E1", "M1"])
        self.assertEqual((self.oracle["EPIC_VIA"], self.oracle["EPIC_STATES"]), (epic().EPIC_VIA, epic().EPIC_STATES))
        self.assertEqual(differ, [])

    def test_u7_epic_scenarios_sees_a_patch_of_the_entrys_claim_of(self):
        plan = {"scenarios_parsed": [{"id": "S1", "evaluator": "ci", "proof": "test", "claim": "pr"}]}
        self.assertEqual(epic().epic_scenarios(plan), [])
        with mock.patch.object(HOST.sb.load_module("plan_ledger"), "claim_of", lambda s: "epic"):
            self.assertEqual([s["id"] for s in epic().epic_scenarios(plan)], ["S1"])
        self.assertIs(epic()._plan_ledger(), HOST.sb.load_module("plan_ledger"))

    def test_u7_the_loaded_by_path_copy_is_kept_when_the_entry_has_not_loaded_one(self):
        with mock.patch.dict(sys.modules):
            del sys.modules["plan_ledger"]
            with mock.patch.object(epic(), "_PLAN_LEDGER", None):
                first = epic()._plan_ledger()
                self.assertIs(epic()._plan_ledger(), first)
                self.assertTrue(callable(first.claim_of))

    def test_u7_a_wrong_function_in_epic_record_fails_the_epic_tests(self):
        wrong = {"epic_scenarios": lambda plan: [], "epic_state": lambda scenario, *a: {"id": scenario["id"], "evaluator": "tester", "claim": "epic", "state": "recorded", "status": "ok",
                                                                                    "head": H1, "current": True, "missing": [], "void": [], "why": None},
                 "epic_lines": lambda rows, default_head: []}
        for name, fn in wrong.items():
            with mock.patch.object(epic(), name, fn):
                suite = unittest.defaultTestLoader.loadTestsFromTestCase(HOST.TestEpicRecord)
                result = unittest.TestResult()
                suite.run(result)
            self.assertGreaterEqual(len(result.failures) + len(result.errors), 1, name)
