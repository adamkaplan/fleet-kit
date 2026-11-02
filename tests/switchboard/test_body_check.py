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


def load_entry(path=ENTRY, name="sb_body_check_t"):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
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

    def __init__(self, body, plan_text, fail=None, sb=None, arg="acme-org/demo#9"):
        self.arg = arg
        self.sb = sb or SB
        self.calls, self.fail = [], fail
        self.body, self.plan_text = body, plan_text

    def __call__(self):
        SB = self.sb
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

        args = SB.argparse.Namespace(body=self.arg, merged=False)
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


FENCE_LINES = "\n".join("- `src/mod_%d.py:%d` the place" % (i, 10 + i) for i in range(1, 5))
WHY_BLOCK = "<!-- pr-explainer:begin -->\nbefore and after\n<!-- pr-explainer:end -->\n\n"
FENCED_CAUSE = "PLAN: acme-org/demo#7\n\n" + WHY_BLOCK + "```\n## Cause\n%s\n```\n" % FENCE_LINES
FENCED_CAUSE_TILDE = FENCED_CAUSE.replace("```", "~~~")
FENCED_CAUSE_OPEN = "PLAN: acme-org/demo#7\n\n" + WHY_BLOCK + "```\n## Cause\n%s\n" % FENCE_LINES
REAL_CAUSE_FENCED_WHY = "PLAN: acme-org/demo#7\n\n## Cause\n\n%s\n\n```\n## Why\nbefore\n```\n" % FENCE_LINES
S12_RULE_PLAN = plan("the first section names at least 2 named items", text="Given the line of the description, when read, then the cause is first")
TWO_ITEMS = "PLAN: acme-org/demo#7\n\n## The cause\n\nSee #101 and #102.\n"
FENCED_WHY_BEFORE = "PLAN: acme-org/demo#7\n\n```\n## Why\nbefore\n```\n\n## Cause\n\n%s\n" % FENCE_LINES
MSG = "the cause section is not first"
S3_MSG = "places with a file and line: found 0 of at least 4"


def check_s3(sb=None):
    """The S3 check: the description with 3 places without a line is refused with the counts. Raises AssertionError anchored on its own message."""
    code, out = Run(NO_LINES, plan(LINE_RULE), sb=sb)()
    if S3_MSG not in out or code != 1:
        raise AssertionError("S3 check: '%s' missing (exit %s): %s" % (S3_MSG, code, out.strip()))


def check_s11(sb=None):
    for name, text, refused in (("fenced cause after why", FENCED_CAUSE, True), ("real cause, fenced why", REAL_CAUSE_FENCED_WHY, False),
                                ("tilde fence", FENCED_CAUSE_TILDE, True), ("fence never closed", FENCED_CAUSE_OPEN, True),
                                ("fenced why before the real cause", FENCED_WHY_BEFORE, False)):
        code, out = Run(text, plan(LINE_RULE), sb=sb)()
        if refused and (code != 1 or out.count(MSG) != 1):
            raise AssertionError("S11 check: '%s' missing for %s (exit %s): %s" % (MSG, name, code, out.strip()))
        if not refused and code != 0:
            raise AssertionError("S11 check: %s must pass, exit %s: %s" % (name, code, out.strip()))


def check_s12_form(sb=None):
    code, out = Run(TWO_ITEMS, S12_RULE_PLAN, sb=sb)()
    if code != 0 or "PASS" not in out:
        raise AssertionError("S12 form check: the pass field says items, expected PASS (exit %s): %s" % (code, out.strip()))


def check_s12_empty(sb=None):
    try:
        code, out = Run(GOOD, plan(LINE_RULE), sb=sb, arg="")()
    except Exception as err:   # the old path ran (a runner or process call)
        raise AssertionError("S12 empty check: expected 'not checked', the old path ran: %r" % (err,))
    if code != 1 or out.count("not checked") != 1 or "PASS" in out or "FAIL" in out:
        raise AssertionError("S12 empty check: expected one 'not checked' line and exit 1, got exit %s: %s" % (code, out.strip()))


def source_copy(tmp, edits=()):
    """A copy of bin/ in tmp with each (file under bin/, old, new) edit applied exactly once; returns the loaded entry."""
    import shutil
    for src in [p for p in (REPO / "bin").iterdir() if p.name != "test-switchboard" and (p.is_dir() or p.suffix in ("", ".py", ".parts"))]:
        dst = Path(tmp) / "bin" / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst) if src.is_dir() else shutil.copy(src, dst)
    for rel, old, new in edits:
        target = Path(tmp) / "bin" / rel
        source = target.read_text()
        assert source.count(old) == 1, "mutation not applied exactly once in %s: %d" % (rel, source.count(old))
        target.write_text(source.replace(old, new))
    return load_entry(Path(tmp) / "bin" / "fleet-switchboard", "sb_bc_%s" % abs(hash((str(tmp), str(edits)))))


class TestBodyCheck(unittest.TestCase):
    def mutant_fails(self, check, edits):
        """Control first (the unmutated copy passes the check), then the mutant copy must fail it; returns the failure text."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            check(source_copy(Path(tmp) / "control"))
            with tempfile.TemporaryDirectory() as tmp2:
                mutant = source_copy(Path(tmp2), edits)
                with self.assertRaises(AssertionError) as caught:
                    check(mutant)
        return str(caught.exception)

    def test_s8_the_documentation_sentence_and_the_unit_row_each_once(self):
        doc = (REPO / "docs" / "switchboard.md").read_text()
        sentences = [l for l in doc.splitlines() if "preflight-pr --body" in l]
        self.assertEqual(len(sentences), 1, "docs/switchboard.md: the sentence naming 'preflight-pr --body' occurs %d times, expected 1" % len(sentences))
        arch = (REPO / "docs" / "architecture.md").read_text()
        head, _, known = arch.partition("\n## Known violations")
        unit = [l for l in head.splitlines() if l.startswith("| `switchboard/body_check.py` |")]
        self.assertEqual(len(unit), 1, "docs/architecture.md: the unit-table row of switchboard/body_check.py occurs %d times, expected 1" % len(unit))
        rows = [l for l in known.splitlines() if l.startswith("|") and "switchboard/body_check.py" in l]
        self.assertEqual(len(rows), 0, "docs/architecture.md: %d Known-violations rows name switchboard/body_check.py, expected 0" % len(rows))

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

    def test_s6_a_build_that_skips_the_count_check_fails_the_s3_check(self):
        old = "    if (got < count) if form == \"at least\" else (got != count):\n"
        text = self.mutant_fails(check_s3, [("switchboard/body_check.py", old, "    if False:\n")])
        self.assertIn("S3 check: '%s' missing" % S3_MSG, text)

    def test_s3_the_check_function_passes_on_the_real_build(self):
        check_s3()

    def test_s11_fenced_headings_are_not_read(self):
        check_s11()
        for text in (FENCED_CAUSE, FENCED_CAUSE_TILDE, FENCED_CAUSE_OPEN):
            self.assertEqual(Run(text, plan(LINE_RULE))()[1].count(MSG), 1)
        self.assertEqual(Run(REAL_CAUSE_FENCED_WHY, plan(LINE_RULE))()[0], 0)

    def test_s11_a_mutant_that_reads_headings_inside_fences_fails(self):
        text = self.mutant_fails(check_s11, [("switchboard/body_check.py", "    out, fence = [], None\n", "    return text\n    out, fence = [], None\n")])
        self.assertIn("S11 check: fenced why before the real cause must pass", text)

    def test_s12_the_form_is_read_from_the_pass_field_only(self):
        check_s12_form()

    def test_s12_an_empty_body_argument_is_not_checked(self):
        check_s12_empty()

    def test_s12_a_mutant_reading_the_form_from_the_text_fails(self):
        old = '    if _body_re(r"\\bline\\b", "I").search(str(scenario.get("pass", ""))):\n'
        new = '    if _body_re(r"\\bline\\b", "I").search(str(scenario.get("pass", "")) + " " + str(scenario.get("text", ""))):\n'
        text = self.mutant_fails(check_s12_form, [("switchboard/body_check.py", old, new)])
        self.assertIn("S12 form check: the pass field says items, expected PASS", text)

    def test_s12_a_mutant_testing_the_argument_for_truth_fails(self):
        old = 'getattr(args, "body", None) is not None:'
        text = self.mutant_fails(check_s12_empty, [("fleet-switchboard", old, 'getattr(args, "body", None):')])
        self.assertIn("S12 empty check: expected", text)

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

    def test_u1_without_body_the_command_is_unchanged(self):
        args = SB.argparse.Namespace(body=None, merged=False)
        with mock.patch.object(SB, "cmd_body_check", side_effect=AssertionError("must not run")), contextlib.redirect_stderr(io.StringIO()):
            code = SB.cmd_preflight_pr(args, lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""), lambda: 0)
        self.assertEqual(code, SB.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
