"""A Done when written as a numbered list under its label is read whole by parse_intent (#2078)."""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from support.core import Base
from support.loader import captured, sb
from support.scenario_roles import FAKE_GH
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE

REPO = Path(__file__).resolve().parents[2]
ONE = "(1) a (2) b c (3) d"
LIST = "## Intent\nDo it.\nDone when:\n1. a\n2. b\n   - c\n\n3. d\n"
SAY = "a Done when written as a numbered list is read whole"
FIRST = ("skills/fleet-charter/SKILL.md", "skills/fleet-planning/SKILL.md", "agents/opencode/chief-of-staff.md",
         "agents/copilot/chief-of-staff.agent.md", "docs/switchboard.md")

ONE_SCENARIO_TEXT = ("  - id: S%d\n    covers: DW-%d\n    text: a scenario that is long enough\n    proof: test\n"
            "    pass: exit status is 0 and count equals 3\n    discriminates: a stub shows count equals 0 and fails\n    evaluator: ci\n")


def body(numbering, tail=""):
    marks = {"dot": ("%d. ",), "paren": ("%d) ",), "round": ("(%d) ",)}[numbering][0]
    return "## Intent\nDo it.\nDone when:\n" + "".join(marks % n + t + "\n" for n, t in ((1, "a"), (2, "b"), (3, "d"))) + tail


class TestDoneWhenListCommands(Base):
    """The real commands read a numbered list and the one-line form alike (#2078 S4, S5, S6)."""
    ONE_LINE = "## Intent\nDo it.\nDone when: (1) a (2) b (3) d\n"

    def setUp(self):
        super().setUp()
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "gh": FAKE_GH, "intent_repo": "acme/app"})
        self.issue = {}

    def runner(self, argv, **kwargs):
        path = argv[2]
        if re.search(r"/issues/7$", path):
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.issue), "")
        if "/comments" in path:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")

    def with_body(self, text):
        self.issue = {"number": 7, "state": "open", "title": "t", "body": text, "updated_at": "2026-01-01T00:00:00Z"}

    def run_main(self, *argv):
        with captured() as (out, err):
            code = sb.main(list(argv), runner=self.runner, now=lambda: 1000.0)
        return code, out.getvalue(), err.getvalue()

    def both(self, fn):
        got = []
        for text in (self.ONE_LINE, body("dot")):
            self.with_body(text)
            got.append(fn())
        return got

    def test_s4_intent_command_wake_header_ask_row_and_ask_refusal_are_equal(self):
        one, many = self.both(lambda: self.run_main("intent", "acme/app#7"))
        self.assertEqual(one[0], 0, one[2])
        self.assertEqual(one, many)
        self.assertIn("Done when: (1) a (2) b (3) d", one[1])

        def header():
            reader = sb.intent_reader_for(sb.load_config(), self.runner, lambda: 1000.0)
            engine = type("E", (), {"intents": reader, "repos_in_play": lambda self: None})()
            return sb.section_resolver(engine, None)(7, "acme/app")
        one, many = self.both(header)
        self.assertEqual(one, many)
        self.assertIn("Done when: (1) a (2) b (3) d", " ".join(one[1]))

        def row():
            issue = sb.IssueIntent.from_api(self.issue)
            return sb.ask_row("fleet", issue, None), sb.ask_refusal(dict(self.issue, parent=3), "acme/app#7")
        one, many = self.both(row)
        self.assertEqual(one, many)
        self.assertIn("(1) a (2) b (3) d", one[0])
        self.assertIsNone(one[1])

    def test_s5_the_plan_check_command_reads_both_forms_the_same(self):
        import tempfile
        plan = Path(tempfile.mkdtemp(dir=str(self.home))) / "plan.md"
        plan.write_text("".join(ONE_SCENARIO_TEXT % (n, n) for n in (1, 2)).join(("scenarios:\n", "")).join(("```yaml\n", "```\n")))
        got = []
        for done in ("(1) a (2) b (3) d", sb.parse_intent(body("dot"))[1]):
            got.append(self.run_main("plan", "check", "--file", str(plan), "--done-when", done))
        self.assertEqual(got[0], got[1])
        self.assertIn(got[0][0], (0, 1), got[0])
        self.assertEqual(got[0][0], 1, got[0])
        self.assertIn("DW-3", got[0][1])

    def test_s6_the_existing_parse_intent_bodies_keep_their_values(self):
        cases = (("Intro\n\n## Intent\nFix the agents.\nDone when: both answer.\n\n## Notes\nx\n", ("Fix the agents.", "both answer.")),
                 ("no section at all", (None, None)), ("## Intent\nJust this\n", ("Just this", None)),
                 ("## Intent\nDone when: later\n", (None, "later")), ("## Intent\n\n## Next\nDone when: not mine\n", (None, None)),
                 (None, (None, None)), ("### intent  \r\n- Make it work\r\n- **Done when:** it works\r\n", ("- Make it work", "it works")),
                 ("```\n## Intent\nnot this\n```\n## Intent\nthis\n```\n## Other\n```\nDone when: x\n## After\nno\n", ("this", "x")),
                 ("## Intent\nMake the check readable.\nDone when: (1) first part; (2) second part; (3) third part.\n\n## Facts\nThe old list (2) and (3).\n",
                  ("Make the check readable.", "(1) first part; (2) second part; (3) third part.")))
        for text, value in cases:
            self.assertEqual(sb.parse_intent(text), value, text)


class TestDoneWhenList(TestDoneWhenListCommands):
    def test_s2_three_clauses_a_sub_bullet_and_a_blank_line(self):
        value = sb.parse_intent(LIST)[1]
        self.assertEqual(value, ONE)
        self.assertEqual(len(re.findall(r"\(\d+\)", value)), 3)
        self.assertEqual(sb.parse_intent("## Intent\nDo it.\nDone when: " + ONE + "\n"), ("Do it.", ONE))

    def test_s3_the_three_numberings_read_the_same_and_the_list_ends(self):
        values = {sb.parse_intent(body(n))[1] for n in ("dot", "paren", "round")}
        self.assertEqual(values, {"(1) a (2) b (3) d"})
        self.assertEqual(sb.parse_intent(body("dot", "\n## Next\n4. e\n"))[1], "(1) a (2) b (3) d")
        self.assertEqual(sb.parse_intent(body("dot", "A paragraph.\n4. e\n"))[1], "(1) a (2) b (3) d")

    def test_s4_the_readers_print_the_same_for_both_forms(self):
        def issue(text):
            return sb.IssueIntent.from_api({"number": 2, "state": "open", "title": "t", "body": text})
        one, many = issue("## Intent\nDo it.\nDone when: (1) a (2) b (3) d\n"), issue(body("dot"))
        self.assertEqual(sb.ask_lines(one), sb.ask_lines(many))
        self.assertEqual(one.done_when, many.done_when)
        self.assertEqual(sb.ask_lines(many)[1], "Done when: (1) a (2) b (3) d")

    def test_s5_plan_check_on_the_list_form(self):
        sys.path.insert(0, str(REPO / "bin"))
        import plan_check
        done = sb.parse_intent(body("dot"))[1]
        self.assertEqual(plan_check.clauses_of(done), ["a", "b", "d"])
        self.assertEqual(plan_check.clauses_of(done), plan_check.clauses_of("(1) a (2) b (3) d"))

    def test_s5_plan_check_passes_a_copy_and_names_a_dropped_or_merged_clause(self):
        sys.path.insert(0, str(REPO / "bin"))
        import plan_check
        done = sb.parse_intent(body("dot"))[1]
        clauses = plan_check.clauses_of(done)
        self.assertTrue(plan_check.verify_declared(["a", "b", "d"], done))
        self.assertIsNone(plan_check.first_difference(["a", "b", "d"], clauses, done))
        dropped = plan_check.first_difference(["a", "b"], clauses, done)
        merged = plan_check.first_difference(["a b", "d"], clauses, done)
        self.assertIn("DW-3", dropped)
        self.assertIn("DW-1", merged)
        self.assertFalse(plan_check.verify_declared(["a", "b"], done))

    def test_s6_bodies_with_text_after_the_label_read_as_before(self):
        self.assertEqual(sb.parse_intent("## Intent\nI.\nDone when: x\n1. y\n"), ("I.", "x"))
        self.assertEqual(sb.parse_intent("## Intent\nDone when: later\n"), (None, "later"))
        self.assertEqual(sb.parse_intent("## Intent\nI.\nDone when:\nplain\n"), ("I.", None))

    def test_u2_unchanged_forms(self):
        self.assertEqual(sb.parse_intent("## Intent\nJust this\n"), ("Just this", None))
        self.assertEqual(sb.parse_intent("### intent  \r\n- Make it work\r\n- **Done when:** it works\r\n"), ("- Make it work", "it works"))
        self.assertEqual(sb.parse_intent("## Intent\nI\n```\nDone when:\n1. a\n```\n"), ("I", None))
        self.assertEqual(sb.parse_intent("## Intent\r\nI\r\nDone when:\r\n1. a\r\n2. b\r\n"), ("I", "(1) a (2) b"))

    def test_s7_the_wording_places_say_it(self):
        text_of = lambda n: " ".join((REPO / n).read_text().split())
        for name in ("skills/fleet-charter/SKILL.md", "skills/fleet-planning/SKILL.md", "docs/switchboard.md"):
            self.assertIn("numbered list", text_of(name), name)
        planning = text_of("skills/fleet-planning/SKILL.md")
        self.assertIn("`## Intent` before `Done when:`, which may be a numbered list", planning)
        doc = (REPO / "docs/switchboard.md").read_text().splitlines()
        hits = [i + 1 for i, line in enumerate(doc) if "numbered list" in line]
        self.assertGreaterEqual(len(hits), 2)
        for number in (112, 3847):
            self.assertIn("numbered list", doc[number - 1], "docs/switchboard.md line %d" % number)
        places = (("skills/fleet-charter/SKILL.md", 97, "Done when (a line, or a numbered list under its label), once."),
                  ("docs/switchboard.md", 112, "a Done-when (a line, or a numbered list under its label)"),
                  ("docs/switchboard.md", 3847, "a Done-when (a line or a numbered list)"),
                  ("skills/fleet-planning/SKILL.md", 53, "which may be a numbered list"))
        for name, number, said in places:   # the four places that say a list is accepted, written once, by file and line text
            self.assertIn(said, (REPO / name).read_text().splitlines()[number - 1], "%s line %d" % (name, number))
        for name in ("agents/opencode/chief-of-staff.md", "agents/copilot/chief-of-staff.agent.md"):
            text = (REPO / name).read_text()
            self.assertIn("one Intent and one Done when in your principal's terms, never widened (see\n", text, name)
            self.assertNotIn("one Done-when line", text, name)

    def test_s7_the_mutant_reading_only_the_first_list_line_is_caught(self):
        src = (REPO / "bin" / "fleet-switchboard").read_text()
        needle = "                listing.append(clause.group(1))\n                continue\n"
        self.assertIn(needle, src)
        self.assertEqual(self.run_copy(src)[0], 0, "the unmutated control must pass first")
        mutant = src.replace(needle, "                listing.append(clause.group(1))\n                break\n", 1)
        self.assertEqual(self.run_copy(src)[0], 0)
        code, out = self.run_copy(mutant)
        self.assertNotEqual(code, 0)
        self.assertIn(SAY, out)

    def run_copy(self, source):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            import shutil
            shutil.copytree(REPO / "bin", Path(tmp) / "bin", ignore=shutil.ignore_patterns("__pycache__"))
            copy = Path(tmp) / "bin" / "fleet-switchboard"
            copy.write_text(source)
            code = ("import importlib.machinery,importlib.util,sys\nl=importlib.machinery.SourceFileLoader('m',%r)\n"
                    "s=importlib.util.spec_from_loader('m',l);m=importlib.util.module_from_spec(s);l.exec_module(m)\n"
                    "ok=m.parse_intent(%r)[1]==%r\nprint('' if ok else %r)\nsys.exit(0 if ok else 1)\n" % (str(copy), LIST, ONE, SAY))
            run = subprocess.run([sys.executable, "-c", code], cwd=str(copy.parent), capture_output=True, text=True)
            return run.returncode, run.stdout
