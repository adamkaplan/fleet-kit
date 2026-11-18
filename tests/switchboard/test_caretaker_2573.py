import datetime as dt
import importlib.util
import json
import os
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("caretaker", os.path.join(ROOT, "bin", "switchboard", "caretaker.py"))
caretaker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(caretaker)

CUTOFF = "2026-11-04T12:00:00Z"
OLD = "2026-10-01T00:00:00Z"


class TestCaretaker2573(unittest.TestCase):
    def test_classify_skips_fresh_and_protected_issues(self):
        fresh = {"title": "cw-worker thing", "updatedAt": CUTOFF, "labels": []}
        self.assertEqual(caretaker.classify_issue(fresh, CUTOFF)[0], "skip")
        for suffix in (":charter", ":orchestrator", ":parked", ":awaiting-user", ":awaiting-cos"):
            issue = {"title": "x", "updatedAt": OLD, "labels": [{"name": "widget" + suffix}]}
            expected = "skip_parked" if suffix == ":parked" else "skip"
            self.assertEqual(caretaker.classify_issue(issue, CUTOFF)[0], expected, suffix)

    def test_classify_closes_obsolete_and_raises_aging_to_owner_or_cos(self):
        for title in ("Fix cw-worker restart", "Clean the integration branch"):
            action, reason = caretaker.classify_issue({"title": title, "updatedAt": OLD, "labels": []}, CUTOFF)
            self.assertEqual(action, "close")
            self.assertTrue(reason)
        owned = {"title": "Tidy docs", "updatedAt": OLD, "labels": [{"name": "lane:widget-lead"}]}
        self.assertEqual(caretaker.classify_issue(owned, CUTOFF), ("raise", "widget-lead"))
        self.assertEqual(caretaker.classify_issue({"title": "Tidy", "updatedAt": OLD, "labels": []}, CUTOFF), ("raise", "cos"))

    def test_run_caretaker_closes_when_apply_true_and_summarizes(self):
        calls = []
        issues = [
            {"number": 1, "title": "cw-worker leftovers", "updatedAt": OLD, "labels": []},
            {"number": 2, "title": "Tidy docs", "updatedAt": OLD, "labels": [{"name": "lane:widget-lead"}]},
            {"number": 3, "title": "New", "updatedAt": "2026-11-17T00:00:00Z", "labels": []},
        ]

        def runner(argv, **kw):
            calls.append(argv)
            out = json.dumps(issues) if argv[1:3] == ["issue", "list"] else ""
            return subprocess.CompletedProcess(argv, 0, out, "")

        res = caretaker.run_caretaker(
            config={"github_repos": ["acme-org/widget-service"], "gh_users": {}},
            apply_changes=True, days=14,
            now_dt=dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc), runner=runner)
        self.assertEqual(res["closed"], ["acme-org/widget-service#1"])
        self.assertEqual(res["raised"], ["acme-org/widget-service#2->widget-lead"])
        self.assertEqual(res["errors"], [])
        self.assertEqual(len(res["summary"].splitlines()), 1)
        self.assertIn("widget-lead", res["summary"])
        verbs = [c[2] for c in calls if c[1] == "issue"]
        self.assertEqual(verbs.count("close"), 1)
        self.assertFalse(any("--add-assignee" in c or "edit" in c for c in calls))

    def test_wildcard_user_other_owner_parked_and_counts(self):
        calls = []
        issues = [
            {"number": 1, "title": "a", "updatedAt": "2026-09-01T00:00:00Z", "labels": [], "author": {"login": "someone-else"}},
            {"number": 2, "title": "b", "updatedAt": OLD, "labels": [{"name": "agent:firstmate"}]},
            {"number": 3, "title": "c", "updatedAt": OLD, "labels": [{"name": "parked"}]},
            {"number": 4, "title": "d", "updatedAt": OLD, "labels": [{"name": "widget:parked"}]},
            {"number": 5, "title": "e", "updatedAt": OLD, "labels": []},
        ]

        def runner(argv, **kw):
            calls.append(argv)
            out = json.dumps(issues) if argv[1:3] == ["issue", "list"] else "tok"
            return subprocess.CompletedProcess(argv, 0, out, "")

        res = caretaker.run_caretaker(
            config={"github_repos": ["acme/api"], "gh_users": {"acme/*": "bot"},
                    "caretaker_other_owners": ["Someone-Else"]},
            now_dt=dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc), runner=runner)
        self.assertIn(["gh", "auth", "token", "--user", "bot"], calls)
        self.assertEqual(res["skipped_other"], ["acme/api#1", "acme/api#2"])
        self.assertEqual(res["skipped_parked"], ["acme/api#3", "acme/api#4"])
        self.assertEqual(res["raised"], ["acme/api#5->cos"])
        self.assertIn("skipped 2 (other owner)", res["summary"])
        self.assertIn("acme/api: 5 open (oldest 2026-09-01T00:00:00Z)", res["summary"])
        lst = next(c for c in calls if c[1:3] == ["issue", "list"])
        self.assertIn("1000", lst)


if __name__ == "__main__":
    unittest.main()
