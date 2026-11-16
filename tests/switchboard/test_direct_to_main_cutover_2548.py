"""#2548 cutover: new pull requests target the default branch (`main`) directly; the `integration` branch is retired.

Derived from the repository: every skill and agent file, and the switchboard document, is read from disk. Run with `python3 bin/test-switchboard TestDirectToMainCutover`.
Standard library only.
"""
import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE / "support"))
from loader import sb  # noqa: E402

STALE = (re.compile(r"gh\s+pr\s+create[^\n]*--base[ =]+integration\b"),
         re.compile(r"--base[ =]+integration\b"),
         re.compile(r"--tip\s+origin/integration\b"),
         re.compile(r"\b(?:open|target|merge)\w*\s+(?:a\s+)?(?:pull request|PR)s?\s+(?:in)?to\s+`?(?:origin/)?integration\b", re.I))


def instruction_files():
    found = sorted(REPO.glob("skills/**/SKILL.md")) + sorted(REPO.glob("agents/**/*.md"))
    return [p for p in found if p.is_file()]


def stale_lines(text):
    return [line for line in text.splitlines() if any(p.search(line) for p in STALE)]


class TestDirectToMainCutover(unittest.TestCase):
    def test_the_files_are_derived_and_not_empty(self):
        names = {p.parent.name for p in instruction_files() if p.name == "SKILL.md"}
        self.assertIn("fleet-coordination", names)
        self.assertIn("fleet-charter", names)
        self.assertGreater(len(instruction_files()), 3)

    def test_no_skill_or_agent_tells_a_builder_to_target_integration(self):
        for path in instruction_files():
            with self.subTest(file=str(path.relative_to(REPO))):
                self.assertEqual(stale_lines(path.read_text()), [])

    def test_the_stale_pattern_catches_the_old_instructions(self):
        for line in ("gh pr create --draft --base integration --title x", "run merge-drops --tip origin/integration --branch HEAD",
                     "Open a pull request into `integration` first"):
            with self.subTest(line=line):
                self.assertEqual(stale_lines(line), [line])
        self.assertEqual(stale_lines("gh pr create --base main"), [])

    def test_the_coordination_skill_says_main_directly(self):
        text = (REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text()
        self.assertIn("--tip origin/main", text)
        self.assertIn("`integration` branch is retired", text)
        self.assertIn("targets the default branch (`main`) directly", text)

    def test_the_switchboard_document_says_integration_is_retired(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        head = "## Batching: the integration branch and the batch pull request (#834)"
        self.assertEqual(text.count(head), 1)
        first = text.split(head, 1)[1].lstrip().split("\n\n", 1)[0]
        for phrase in ("Retired (#2548)", "retired", "target `main`", "directly", "no daily batch hour", "no batch freeze", "historical"):
            self.assertIn(phrase, first)

    def test_the_tip_watch_reads_the_default_branch(self):
        sys.path.insert(0, str(REPO / "bin"))
        import tip_watch
        self.assertEqual(tip_watch.BRANCH, "main")

    def test_an_unset_integration_branch_is_the_default_and_an_invalid_one_is_refused(self):
        self.assertIsNone(sb.TRIAGE_DEFAULTS["integration_branch"])
        sb.validate_triage_config({"triage": {}}, "cfg")
        self.refuses_every_bad_value()

    def refuses_every_bad_value(self):
        for bad in (7, ["stage"], "", True):
            with self.assertRaises(sb.ConfigError, msg=repr(bad)):
                sb.validate_triage_config({"triage": {"integration_branch": bad}}, "cfg")

    def test_a_refused_config_keeps_the_last_good_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"triage": {"integration_branch": None}}))
            good = sb.load_config(str(path))
            kept = copy.deepcopy(good)
            path.write_text(json.dumps({"triage": {"integration_branch": 7}}))
            with self.assertRaises(sb.ConfigError):
                sb.load_config(str(path))
            self.assertEqual(good, kept)
            self.assertIsNone(good["triage"]["integration_branch"])

    def test_a_mutant_that_accepts_any_integration_branch_is_caught(self):
        with mock.patch.object(sb, "validate_triage_config", lambda config, source: None):
            with self.assertRaises(AssertionError):
                self.refuses_every_bad_value()


if __name__ == "__main__":
    unittest.main()
