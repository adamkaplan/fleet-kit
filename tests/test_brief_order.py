"""#1215: the order of refusals in the delegated clear: a list that cannot be read whole has no brief to bind, so the real reason (#981) comes before "no brief". Run: python3 -m unittest tests.test_brief_order"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import delegated_clear  # noqa: E402
import impact_floor  # noqa: E402
import impact_hold  # noqa: E402

RULE = {"never_path_classes": [], "top_tier_review_classes": [], "clear_code_paths": [], "new_file_globs": [], "closure_files": []}


def checks(complete, files):
    report = {"owner_decision": "hold", "review_action": "approve", "complete": complete, "top_tier_hits": {}, "changed_paths": [], "head": "a" * 40, "pr": "o/r#1", "unknown": None if complete else "GitHub listed 1 of 3 changed files"}
    got = {"report": report, "rule": RULE, "files": files, "closure": {}}
    with mock.patch.object(impact_hold, "delegated_clear_inputs", return_value=(got, None)), mock.patch.object(impact_hold, "load_module", lambda name: delegated_clear, create=True), \
            mock.patch.object(impact_hold, "impact_floor", impact_floor, create=True):   # the part runs in the entry's namespace: lend it the two names it reads
        return impact_hold.delegated_clear_pr_checks(None, None, "o/r", 1, None, {}, ("", "", "reasons"))


class TestRefusalOrder(unittest.TestCase):
    def test_unreadable_list_is_refused_with_its_own_reason_not_no_brief(self):
        report, why = checks(False, None)
        self.assertIsNone(report)
        self.assertNotIn("no brief", why)

    def test_readable_list_with_no_brief_is_refused_no_brief(self):
        report, why = checks(True, [])
        self.assertIsNone(report)
        self.assertIn("no brief", why)

    def test_a_brief_with_no_info_dict_does_not_raise(self):
        report = {"owner_decision": "hold", "review_action": "approve", "complete": True, "top_tier_hits": {}, "changed_paths": [], "head": "a" * 40, "pr": "o/r#1"}
        got = {"report": report, "rule": RULE, "files": [], "closure": {}}
        with mock.patch.object(impact_hold, "delegated_clear_inputs", return_value=(got, None)), mock.patch.object(impact_hold, "load_module", lambda name: delegated_clear, create=True), \
                mock.patch.object(impact_hold, "impact_floor", impact_floor, create=True), mock.patch.object(impact_hold, "floor_brief", lambda *a: None, create=True), \
                mock.patch.object(delegated_clear, "substance_refusals", lambda *a: ["refused for the test"]):
            result = impact_hold.delegated_clear_pr_checks(None, None, "o/r", 1, None, None, ("d", "t", "r"))
        self.assertEqual(result, (None, "refused for the test"))


if __name__ == "__main__":
    unittest.main()
