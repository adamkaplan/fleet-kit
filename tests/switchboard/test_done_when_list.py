"""A Done when written as a numbered list under its label is read whole by parse_intent (#2078)."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

from support.loader import sb

REPO = Path(__file__).resolve().parents[2]
ONE = "(1) a (2) b c (3) d"
LIST = "## Intent\nDo it.\nDone when:\n1. a\n2. b\n   - c\n\n3. d\n"
SAY = "a Done when written as a numbered list is read whole"
FIRST = ("skills/fleet-charter/SKILL.md", "skills/fleet-planning/SKILL.md", "agents/opencode/chief-of-staff.md",
         "agents/copilot/chief-of-staff.agent.md", "docs/switchboard.md")


def body(numbering, tail=""):
    marks = {"dot": ("%d. ",), "paren": ("%d) ",), "round": ("(%d) ",)}[numbering][0]
    return "## Intent\nDo it.\nDone when:\n" + "".join(marks % n + t + "\n" for n, t in ((1, "a"), (2, "b"), (3, "d"))) + tail


class TestDoneWhenList(unittest.TestCase):
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
        for name in FIRST:
            text = " ".join((REPO / name).read_text().split())
            self.assertRegex(text, r"numbered list", name)
        self.assertEqual(sum("numbered list" in " ".join((REPO / n).read_text().split()) for n in FIRST), 5)
        for name in ("docs/switchboard.md",):
            self.assertGreaterEqual((REPO / name).read_text().count("numbered list"), 2)

    def test_s7_the_mutant_reading_only_the_first_list_line_is_caught(self):
        src = (REPO / "bin" / "fleet-switchboard").read_text()
        needle = "                listing.append(clause.group(1))\n                continue\n"
        self.assertIn(needle, src)
        self.assertEqual(self.run_copy(src), 0, "the unmutated control must pass first")
        mutant = src.replace(needle, "                listing.append(clause.group(1))\n                break\n", 1)
        self.assertNotEqual(self.run_copy(mutant), 0, SAY)

    def run_copy(self, source):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            import shutil
            shutil.copytree(REPO / "bin", Path(tmp) / "bin", ignore=shutil.ignore_patterns("__pycache__"))
            copy = Path(tmp) / "bin" / "fleet-switchboard"
            copy.write_text(source)
            code = ("import importlib.machinery,importlib.util,sys\nl=importlib.machinery.SourceFileLoader('m',%r)\n"
                    "s=importlib.util.spec_from_loader('m',l);m=importlib.util.module_from_spec(s);l.exec_module(m)\n"
                    "sys.exit(0 if m.parse_intent(%r)[1]==%r else 1)\n" % (str(copy), LIST, ONE))
            return subprocess.run([sys.executable, "-c", code], cwd=str(copy.parent), capture_output=True).returncode
