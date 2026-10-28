"""`fleet-switchboard preflight-pr --body PR`: the description against the plan's cause-first reviewer scenario (ask of 28 Oct, plan S1-S10, U1, U2).

The three recorded shapes are SYNTHETIC (neutral names): the why block first, places without line numbers, fewer than N places."""
import contextlib
import importlib.machinery
import importlib.util
import io
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
ENTRY = REPO / "bin" / "fleet-switchboard"
PART = REPO / "bin" / "switchboard" / "body_check.py"


def load_entry():
    loader = importlib.machinery.SourceFileLoader("sb_body_check_t", str(ENTRY))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("sb_body_check_t", loader))
    loader.exec_module(module)
    return module


SB = load_entry()


def plan(rule, text="Given the description, when read, then it names the cause first in the first section", evaluator="reviewer", claim="pr"):
    return ("## Plan (scenarios): demo\n\n```yaml\nscenarios:\n  - id: S1\n    covers: DW-1\n    text: %s\n    proof: screenshot\n    pass: %s\n"
            "    discriminates: a description that opens with the change fails\n    evaluator: %s\n    claim: %s\n```\n" % (text, rule, evaluator, claim))


PLACES = "\n".join("- `src/mod_%d.py:%d` the place" % (i, 10 + i) for i in range(1, 7))
GOOD = "PLAN: acme-org/demo#7\n\n## The cause\n\nThe places:\n%s\n\n## Why\n\nbefore and after\n\n## The change\n\nwe edit it\n" % PLACES
WHY_FIRST = "PLAN: acme-org/demo#7\n\n<!-- pr-explainer:begin -->\nbefore and after\n<!-- pr-explainer:end -->\n\n## The cause\n\n%s\n" % PLACES
HEADING_ORDER = "PLAN: acme-org/demo#7\n\n## Why\n\nbefore and after\n\n## The cause\n\n%s\n" % PLACES
NO_LINES = "PLAN: acme-org/demo#7\n\n## The cause\n\n- `src/mod_a.py` the place\n- `src/mod_b.py` the place\n- `src/mod_c.py` the place\n"
FEW = "PLAN: acme-org/demo#7\n\n## The cause\n\n- `src/mod_a.py:12` the place\n- `src/mod_b.py:30` the place\n\n## The change\n\nedit\n"
LINE_RULE = "the first section names at least 4 places with function and line"


class Run:
    """The command with its readers faked; every call a fake sees is kept, so a non-GET call or a write can be counted."""

    def __init__(self, body, plan_text, fail=None):
        self.calls, self.fail = [], fail
        self.body, self.plan_text = body, plan_text

    def __call__(self):
        def read_pr_json(engine, repo, number, extra_fields=""):
            self.calls.append("GET pr")
            if self.fail == "pr":
                raise SB.NoticeError("gh pr view failed: boom")
            return {"body": self.body}

        def fetch_plan(reader, repo, number, comment_id=None):
            self.calls.append("GET plan")
            if self.fail == "plan":
                raise SB.PlanInputError("cannot read plan")
            return self.plan_text, "comment", "", []

        args = SB.argparse.Namespace(body="acme-org/demo#9", merged=False)
        out = io.StringIO()
        cfg = {"gh": "gh", "call_timeout_seconds": 5, "intent_repo": "acme-org/demo"}
        with mock.patch.object(SB, "read_pr_json", read_pr_json), mock.patch.object(SB, "fetch_plan", fetch_plan), \
                mock.patch.object(SB, "build_engine", lambda *a, **k: (object(), None, None)), mock.patch.object(SB, "load_config", lambda: cfg), \
                mock.patch.object(SB, "IntentReader", lambda *a, **k: object()), mock.patch.object(SB, "GhAccounts", lambda *a, **k: object()), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("no process may run")), contextlib.redirect_stdout(out):
            code = SB.cmd_preflight_pr(args, lambda *a, **k: (_ for _ in ()).throw(AssertionError("no runner call")), lambda: 0)
        return code, out.getvalue()


def run(body, rule=LINE_RULE, **kw):
    return Run(body, plan(rule, **kw.pop("plan_kw", {})), **kw)()


class TestBodyCheck(unittest.TestCase):
    def test_s2_the_why_block_first_is_refused_by_name(self):
        for shape in (WHY_FIRST, HEADING_ORDER):
            code, out = run(shape)
            self.assertEqual(code, 1)
            self.assertEqual(out.count("the cause section is not first"), 1, out)

    def test_s3_places_without_a_line_are_refused_with_the_counts(self):
        code, out = run(NO_LINES)
        self.assertEqual(code, 1)
        self.assertIn("places with a file and line: found 0 of at least 4", out)
        self.assertNotIn("the cause section is not first", out)

    def test_fewer_than_n_places_is_refused(self):
        code, out = run(FEW)
        self.assertEqual(code, 1)
        self.assertIn("places with a file and line: found 2 of at least 4", out)

    def test_s5_good_passes_and_each_bad_shape_has_its_own_item(self):
        self.assertEqual(run(GOOD)[0], 0)
        items = set()
        for bad in (WHY_FIRST, NO_LINES, FEW):
            code, out = run(bad)
            self.assertEqual(code, 1)
            items.add(re.findall(r"REFUSED: (.*)", out)[0])
        self.assertEqual(len(items), 3)

    def test_s6_a_build_that_skips_the_count_check_fails_the_s3_test(self):
        with mock.patch.object(SB, "body_rule_count", lambda rule: (None, None)):
            code, out = run(NO_LINES)
        self.assertNotIn("found 0 of at least 4", out)   # the mutant: the S3 assertion above would fail

    def test_s4_the_count_forms_and_the_no_count_rule(self):
        self.assertEqual(SB.body_rule_count("on 28 Oct the section names at least 4 places"), (4, "at least"))
        self.assertEqual(SB.body_rule_count("the first section names the 3 cases (count equals 3)"), (3, "count equals"))
        self.assertEqual(SB.body_rule_count("the first section names the cause, as on 28 Oct"), (None, None))
        code, out = run(GOOD, rule="the first section is the cause, seen on 28 Oct")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("not checked"), 1)
        self.assertIn("not checked: the pass rule has no count", out)
        code, out = run(WHY_FIRST, rule="the first section is the cause, seen on 28 Oct")
        self.assertIn("the cause section is not first", out)

    def test_count_equals_is_exact_and_items_count_without_lines(self):
        rule = "the first section names the cases (count equals 3)"
        three = "PLAN: acme-org/demo#7\n\n## The cause\n\nSee #101, #102 and #103.\n"
        self.assertEqual(run(three, rule=rule)[0], 0)
        code, out = run(three + "and #104\n", rule=rule)
        self.assertEqual(code, 1)
        self.assertIn("named items: found 4 of count equals 3", out)

    def test_s7_a_failed_read_is_not_checked_and_writes_nothing(self):
        for where in ("pr", "plan"):
            runner = Run(GOOD, plan(LINE_RULE), fail=where)
            code, out = runner()
            self.assertNotEqual(code, 0)
            self.assertEqual(out.count("not checked"), 1, out)
            self.assertEqual([c for c in runner.calls if not c.startswith("GET")], [])

    def test_s9_a_plan_with_no_cause_first_scenario_is_not_checked(self):
        for other in (plan("the table lists 2 rows", text="Given x, then y"), plan(LINE_RULE, evaluator="ci"), plan(LINE_RULE, claim="epic")):
            code, out = Run(GOOD, other)()
            self.assertNotEqual(code, 0)
            self.assertEqual(out.count("not checked"), 1)
            self.assertIn("no cause-first scenario", out)

    def test_u2_only_gets_are_made(self):
        runner = Run(GOOD, plan(LINE_RULE))
        self.assertEqual(runner()[0], 0)
        self.assertEqual(runner.calls, ["GET pr", "GET plan"])

    def test_s8_s10_the_code_is_in_a_part_file_and_the_entry_gains_little(self):
        self.assertTrue(PART.is_file())
        self.assertIn("switchboard/body_check.py", (REPO / "bin" / "fleet-switchboard.parts").read_text().split())
        text = (ENTRY).read_text()
        self.assertNotIn("def body_rule_count", text)
        self.assertLessEqual(text.count("cmd_body_check"), 1)
        self.assertEqual(len(re.findall(r"preflight-pr --body", (REPO / "docs" / "switchboard.md").read_text())) >= 1, True)

    def test_u1_without_body_the_command_is_unchanged(self):
        args = SB.argparse.Namespace(body=None, merged=False)
        with mock.patch.object(SB, "cmd_body_check", side_effect=AssertionError("must not run")), contextlib.redirect_stderr(io.StringIO()):
            code = SB.cmd_preflight_pr(args, lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""), lambda: 0)
        self.assertEqual(code, SB.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
