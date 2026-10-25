"""A definition or binding ADDED under a clear-closure name in another file is refused by the clear (#1339, follow-up of #1092 S13).

Real `closure_refusals` and `shadow_refusals` on scratch text pairs and unified diffs; the real `refusals` for the wiring of a part the matrix does not list.
"""
import difflib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bin"))
import impact_floor  # noqa: E402


def clear():
    spec = importlib.util.spec_from_file_location("delegated_clear_shadow", str(REPO / "bin" / "delegated_clear.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RULE = json.loads((REPO / "docs/review-laws/impact-matrix.json").read_text())["owner_hold"]["delegated_clear"]
BASE = "import os\n\n\ndef keep(a):\n    return a\n"


def diff(base, head):
    return "".join(list(difflib.unified_diff(base.splitlines(True), head.splitlines(True), "a/x", "b/x", n=3))[2:])


class TestShadow(unittest.TestCase):
    def setUp(self):
        self.m = clear()
        self.names = self.m.shadow_names(RULE)

    def part(self, path, extra, listed):
        head = BASE + extra
        found = self.m.shadow_refusals(BASE, head, self.names, path)
        if listed:
            found = sorted(set(found) | set(self.m.closure_refusals(BASE, head, diff(BASE, head), RULE["module_roots"].get(path, []), set(), True)))
        return found

    def test_s1_listed_part_refuses_the_added_closure_name(self):
        add = "\n\ndef boss_name(a, b):\n    return a\n"
        self.assertEqual(self.m.closure_refusals(BASE, BASE + add, diff(BASE, BASE + add), ["keep"], set(), True), [], "the check as it was lets it through")
        got = self.part("bin/switchboard/audit.py", add, True)
        self.assertTrue(any("boss_name" in r and "audit.py" in r for r in got), got)

    def test_s2_unlisted_part_refuses_it(self):
        self.assertFalse(impact_floor.matches("bin/switchboard/judge.py", RULE["closure_files"]))
        got = self.part("bin/switchboard/judge.py", "\n\ndef boss_name(a, b):\n    return a\n", False)
        self.assertTrue(any("boss_name" in r and "judge.py" in r for r in got), got)

    def test_s3_binding_added_is_refused_and_an_outside_name_is_not(self):
        binding = next(n for n in ("EXIT_OK", "EXIT_ERROR", "PROG") if n in self.names)
        got = self.part("bin/switchboard/judge.py", "\n%s = 1\n" % binding, False)
        self.assertTrue(any(binding in r for r in got), got)
        self.assertEqual(self.part("bin/switchboard/judge.py", "\n\ndef not_a_closure_name_zz(a):\n    return a\n", False), [])
        self.assertEqual(self.part("bin/switchboard/audit.py", "\n\ndef not_a_closure_name_zz(a):\n    return a\n", True), [])

    def test_a_name_already_there_is_not_added_and_a_syntax_error_is_refused(self):
        self.assertEqual(self.m.shadow_refusals(BASE + "\ndef boss_name(a):\n    return a\n", BASE + "\ndef boss_name(a):\n    return 2\n", self.names, "bin/switchboard/x.py"), [])
        self.assertTrue(self.m.shadow_refusals(BASE, "def (", self.names, "bin/switchboard/x.py"))

    def test_s4_the_entry_keeps_its_baseline_reasons(self):
        add = "\n\ndef boss_name(a, b):\n    return a\n"
        base = "def boss_name(a):\n    return a\n"
        head = base + add
        roots = self.m.daemon_roots(RULE)
        baseline = self.m.closure_refusals(base, head, diff(base, head), roots, set(RULE["clear_entry_points"]), False, RULE["dynamic_actions"])
        self.assertTrue(baseline)
        # the map the clear builds for the entry: the baseline reasons when there are any, else the added-name reason
        self.assertEqual(baseline or self.m.shadow_refusals(base, head, self.names, RULE["daemon_file"]), baseline)

    def test_the_clear_reads_an_unlisted_part_through_its_map(self):
        report = {"owner_decision": "hold", "complete": True, "top_tier_hits": {}, "review_action": "go", "review_class": None, "changed_paths": ["bin/switchboard/judge.py"]}
        files = [{"filename": "bin/switchboard/judge.py", "status": "modified"}]
        reason = ["it adds boss_name in bin/switchboard/judge.py, a name of the clear's closure"]
        self.assertTrue(any("boss_name" in r for r in self.m.refusals(RULE, report, files, impact_floor.matches, {"bin/switchboard/judge.py": reason})))
        self.assertEqual(self.m.refusals(RULE, report, files, impact_floor.matches, {}), [], "a part nobody read is not judged here: the glue (delegated_clear_map) returns a refusal for an unreadable unlisted part")


if __name__ == "__main__":
    unittest.main()
