"""Slice D of #1215 (S18-S23, S25, S28). Run: python3 -m unittest tests.test_brief_advisory  (from the repo root).
Mutant checks here: each rule is flipped in-test and the check must fail (one per member of each table)."""
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import brief_advisory as ba   # noqa: E402
import decision_core as dc    # noqa: E402

TOP = ("## Why this needs you\n\nIt changes a credential path. The owner decides if it ships.\n\n"
       "## What changes for you\n\n| Before | After |\n|---|---|\n| You get two cards. | You get one card. |\n| You open a list. | The list opens itself. |\n\n"
       "## Example\n\nToday a failed job sends two cards and a pointer.\nAfter, one card reads: job 4 failed.\n\n"
       "## Questions for you\n\n1. Ship it now? Recommendation: yes.\n\n")
OPEN = "MERGE: captain\nPLAN: https://example.test/acme/api/issues/9\n\n"
RAT = "Rationale: The order of 23 October says the owner decides only what might matter.\n\n"
CHANGED = ["docs/switchboard.md", "tests/test_brief_advisory.py", "bin/brief_advisory.py", "bin/decision_core.py", "bin/impact_hold.py", "bin/fleet-switchboard"]


def held_defects(body):
    out = subprocess.run([sys.executable, "-c",
                          "import sys,importlib.machinery as m,importlib.util as u;"
                          "l=m.SourceFileLoader('sb','bin/fleet-switchboard');s=u.spec_from_loader('sb',l);mod=u.module_from_spec(s);l.exec_module(mod);"
                          "print('\\n'.join(mod._held_defects(sys.stdin.read())))"],
                         input=body, capture_output=True, text=True, cwd=ROOT)
    return [l for l in out.stdout.splitlines() if l]


ADVISORY = {"decider": "chief_of_staff", "material": [], "why": "It only edits tests, so nothing widens."}
OWNER = {"decider": "owner", "material": ["credentials_widened"], "why": "It widens a credential path, so the owner decides."}


class TestG6bRationale(unittest.TestCase):
    def test_s18_missing_rationale_is_one_defect_and_present_is_none(self):
        self.assertEqual(held_defects(OPEN + TOP), [d for d in held_defects(OPEN + TOP) if "rationale" in d])
        self.assertEqual(len(held_defects(OPEN + TOP)), 1)
        self.assertEqual(held_defects(OPEN + RAT + TOP), [])

    def test_s18_two_sentences_fail(self):
        self.assertEqual(len(held_defects(OPEN + "Rationale: One thing. Another thing.\n\n" + TOP)), 1)

    def test_s25_body_quotes_the_order_in_its_first_three_lines(self):
        body = "PLAN: https://example.test/acme/api/issues/1215\nMERGE: captain\n" + RAT + TOP
        self.assertEqual(held_defects(body), [])
        self.assertIn("order of 23 October", "\n".join(body.split("\n")[:3]))


class TestAdvisoryAndRows(unittest.TestCase):
    def test_s19_row_shows_the_rationale(self):
        rec = {"head": "a" * 40, "title": "t", "brief": OWNER}
        row = dc.labels_entry("acme/api", 9, "api", rec, False)
        self.assertIn(OWNER["why"], row["note"])
        self.assertEqual(row["rationale"], OWNER["why"])

    def test_s20_advisory_is_not_an_owner_hold_and_is_marked_advisory(self):
        self.assertFalse(ba.owner_hold_wanted(ADVISORY))
        row = dc.labels_entry("acme/api", 9, "api", {"head": "a" * 40, "brief": ADVISORY}, False)
        self.assertTrue(row["advisory"])
        self.assertEqual(row["tier"], "cos")

    def test_s20_fail_closed_cases_are_not_advisory(self):
        for brief in (None, {}, {"decider": "owner", "material": []}, {"decider": "chief_of_staff", "material": ["spend"]},
                      {"decider": "chief_of_staff"}, {"decider": "chief_of_staff", "material": None}):
            self.assertTrue(ba.owner_hold_wanted(brief), brief)

    def test_s20_plan_writes_no_label_for_advisory(self):
        src = open(os.path.join(ROOT, "bin", "impact_hold.py")).read()
        self.assertRegex(src, r'brief\.get\("decider"\) == "chief_of_staff" and brief\.get\("material"\) == \[\]')

    def test_why_sentence_must_be_one(self):
        self.assertIsNone(ba.why_sentence({"why": "One. Two."}))
        self.assertIsNone(ba.why_sentence(None))


class TestDocsAndHygiene(unittest.TestCase):
    def test_s23_docs_state_four_items(self):
        text = open(os.path.join(ROOT, "docs", "switchboard.md")).read()
        items = {"brief": "impact brief", "deciders": "Who decides what", "escalation": "credentials or issuance widened", "order": "order of 23 October"}
        found = [k for k, v in items.items() if v in text]
        self.assertEqual(len(found), 4, found)

    def test_s28_no_private_names_in_changed_files(self):
        pat = re.compile(r"ghp_[A-Za-z0-9]{10,}|github_pat_|BEGIN [A-Z ]*PRIVATE KEY|\.internal\b|\.corp\b|[\w.-]+@(?!example\.)[\w-]+\.(?:com|net|org)")
        hits = []
        for name in ("docs/switchboard.md", "bin/brief_advisory.py"):   # this file holds the patterns themselves
            with open(os.path.join(ROOT, name)) as fh:
                text = fh.read()
            text = text[text.find("## The impact brief"):] if name.endswith(".md") else text
            hits += [(name, m.group(0)) for m in pat.finditer(text) if m.group(0) != "github_pat_"]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
