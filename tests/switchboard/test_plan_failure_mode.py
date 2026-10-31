"""The plan check's `no-failure-mode` warning (ask #1455): a plan that reads or produces something and has no scenario about a missing, corrupt, unreadable or absent input."""
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bin"))
import plan_check  # noqa: E402

DONE = "(1) the verb prints a summary"
BASE = """## Plan (scenarios)

Done when: %s

The verb reads three files and writes a feed.

PC-1: the summary keeps its shape

```yaml
scenarios:
  - id: S1
    covers: DW-1
    text: Given three files, when the verb runs, then the summary has exactly 3 lines
    proof: test
    pass: the summary has exactly 3 lines and the first line starts with "summary"
    discriminates: a stub that prints an empty summary returns 0 lines
    evaluator: ci
  - id: S2
    covers: PC-1
    text: Given a long feed, when the verb runs, then the feed is capped at 50 entries
    proof: test
    pass: the feed holds at most 50 entries and exactly 1 header line
    discriminates: a build with no cap writes 51 entries
    evaluator: ci
  - id: U1
    covers: DW-1
    text: The existing listing is unchanged
    proof: test
    pass: the existing listing test passes with exactly 0 edits
    discriminates: a change that renames the listing returns a different name
    evaluator: ci
```
"""
PINNED_HASH = "sha256:6cb50bf03dbe865e24ee38e553296cf71b9f33e4dc360fc52a48554395fab532"
FAILURE = """  - id: S3
    covers: DW-1
    text: Given one input file that is unreadable, when the verb runs, then the output says so
    proof: test
    pass: the output names the unreadable file and the last good output is kept byte for byte
    discriminates: a build that crashes on the file prints a traceback
    evaluator: ci
"""


def plan(extra=""):
    return (BASE % DONE).replace("    evaluator: ci\n```\n", "    evaluator: ci\n" + extra + "```\n")


def run(text):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "plan.md"
        path.write_text(text)
        return subprocess.run([sys.executable, str(REPO / "bin" / "fleet-switchboard"), "plan", "check", "--file", str(path), "--done-when", DONE],
                              capture_output=True, text=True)


class TestNoFailureMode(unittest.TestCase):
    def test_s2_a_reading_plan_with_shape_scenarios_only_warns_once(self):
        out = run(plan())
        lines = out.stdout.splitlines()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(lines[0], "plan check: PASS")
        self.assertEqual(len([l for l in lines if l.startswith("WARN: no-failure-mode:")]), 1, out.stdout)

    def test_s3_a_failure_scenario_or_no_trigger_word_means_no_warning(self):
        self.assertNotIn("no-failure-mode", run(plan(FAILURE)).stdout)
        quiet = plan().replace("reads three files and writes a feed", "prints a summary line").replace("long feed", "long list").replace("the feed", "the list")
        self.assertNotIn("no-failure-mode", run(quiet).stdout)

    def test_s4_the_warning_does_not_change_the_plan_hash(self):
        self.assertEqual(plan_check.check(plan(), DONE)["plan_hash"], PINNED_HASH)
        self.assertIn("plan hash: %s" % PINNED_HASH, run(plan()).stdout)

    def test_s1_s5_the_documents_carry_the_lines(self):
        for path in ("docs/plan-template.md", "skills/fleet-planning/SKILL.md"):
            self.assertIn("missing, corrupt or unreadable", (REPO / path).read_text(), path)
            self.assertIn("the input is missing, corrupt or unreadable: the output says so and the last good output is kept", (REPO / path).read_text(), path)
        text = (REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text()
        self.assertIn("quoted heredoc", text)
        self.assertIn("backtick", text)

    def test_every_trigger_word_warns_and_every_failure_word_clears_it(self):
        """One case per member of the two tables, derived from the code (R-TEST-5)."""
        quiet = plan().replace("reads three files and writes a feed", "prints a summary line").replace("long feed", "long list").replace("the feed", "the list")
        failure_words = [w for w in re.findall(r"[a-z]+", plan_check.FAILURE_SCENARIO_RE.pattern) if w not in ("b", "bb")]
        self.assertEqual(sorted(failure_words), sorted(["missing", "corrupt", "corrupted", "unreadable", "absent"]))
        for word in plan_check.FAILURE_TRIGGER_WORDS:
            with self.subTest(trigger=word):
                warned = quiet.replace("prints a summary line", "the verb will %s it" % word)
                self.assertEqual(run(warned).stdout.count("no-failure-mode"), 1, word)
                for fw in failure_words:
                    with self.subTest(trigger=word, failure=fw):
                        self.assertIsNone(plan_check.failure_mode_warning([{"text": "an input that is %s" % fw}], "the verb will %s it" % word))

    def test_a_plan_with_trigger_words_and_no_scenarios_has_no_warning(self):
        self.assertIsNone(plan_check.failure_mode_warning([], "the verb reads three files"))

    def _no_trigger_plan(self):
        return plan().replace("The verb reads three files and writes a feed.", "The verb prints a summary.").replace("feed", "list")

    def test_a_trigger_word_only_in_a_pass_line_does_not_warn_and_its_control_does(self):
        quiet = self._no_trigger_plan()
        self.assertEqual(run(quiet).stdout.count("no-failure-mode"), 0)   # nothing at all speaks of reading
        in_pass = quiet.replace("the first line starts with", "the verdict quotes what it read: the first line starts with")
        self.assertEqual(run(in_pass).stdout.count("no-failure-mode"), 0)   # anchored: the word is only in a pass line
        in_text = quiet.replace("then the summary has exactly 3 lines", "then the summary has exactly 3 lines it read")
        self.assertEqual(run(in_text).stdout.count("no-failure-mode"), 1)   # control: the same word in a scenario text
        for key in ("discriminates", "proof", "evaluator", "covers", "claim"):
            with self.subTest(field=key):
                got = plan_check.failure_mode_warning([{"id": "S1", "text": "the summary has 3 lines", key: "the verb reads files"}], "The verb prints a summary.")
                self.assertIsNone(got)

    def test_a_scenario_text_that_reads_an_input_warns_without_a_failure_scenario(self):
        got = plan_check.failure_mode_warning([{"id": "S1", "text": "the verb reads three files"}], "The verb prints a summary.")
        self.assertEqual(got["code"], "no-failure-mode")


    def _warn_count(self, module, text):
        return len([w for w in module.check(text, DONE)["warnings"] if w["code"] == "no-failure-mode"])

    def _only_in_other_field(self, field, word="missing"):
        base = plan()
        old = {"pass": "the summary has exactly 3 lines and the first line starts with", "discriminates": "a stub that prints an empty summary returns 0 lines"}[field]
        self.assertIn(old, base)
        return base.replace(old, old + " (" + word + " nothing)", 1)

    def test_a_failure_word_only_in_a_pass_or_discriminates_line_still_warns_and_its_control_does_not(self):
        for field in ("pass", "discriminates"):
            with self.subTest(field=field):
                got = run(self._only_in_other_field(field)).stdout.count("no-failure-mode")
                self.assertEqual(got, 1)   # anchored: the failure word is only in a verdict line
                for word in ("missing", "unreadable"):
                    control = plan().replace("then the summary has exactly 3 lines", "then the summary has exactly 3 lines even when a file is " + word, 1)
                    self.assertEqual(run(control).stdout.count("no-failure-mode"), 0, word)   # control: in a scenario text
                print("warn %d / control 0 (%s)" % (got, field))

    def _mutant(self):
        src = (REPO / "bin" / "plan_check.py").read_text()
        real = 'FAILURE_SCENARIO_RE.search(str(s.get("text", "")))'
        self.assertEqual(src.count(real), 1)
        mutated = src.replace(real, 'FAILURE_SCENARIO_RE.search(" ".join(str(v) for v in s.values()))')
        import importlib.util
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "plan_check_mutant.py"
        path.write_text(mutated)
        spec = importlib.util.spec_from_file_location("plan_check_mutant", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_mutant_that_searches_all_fields_is_caught_and_the_real_module_passes(self):
        mutant = self._mutant()
        caught = passes = 0
        for field in ("pass", "discriminates"):
            text = self._only_in_other_field(field)
            expected = 1
            self.assertEqual(self._warn_count(plan_check, text), expected)   # control: the real module warns once
            passes += 1
            got = self._warn_count(mutant, text)
            self.assertEqual((expected, got), (1, 0))   # the mutant has 0 occurrences where 1 is expected
            caught += 1
        print("mutant caught %d of %d, control passes %d of %d" % (caught, 2, passes, 2))


if __name__ == "__main__":
    unittest.main()
