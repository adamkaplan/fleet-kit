"""Ask #2512: the test and impact reviewer agents tell their reviewers to write the block lines the impact brief reads.

Text only. The line names are DERIVED from the agent definitions; the review skill and the review laws text must name the same three lines once each.
The parser of ask #2509 (bin/switchboard/review_laws.py `parse_review_block`) reads `mutants-run:`, `mutants-survived:` and `blast-radius:`; it holds no named
constant for them yet, so this file keeps a tiny local copy of its format below. Replace it by the parser's names after #2511 (the pull request that adds the parser) is merged.
Run with `python3 bin/test-switchboard TestReviewerBlockLines TestReviewerBlockLinesControl`. Standard library only."""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TEST_AGENT = REPO / "agents" / "reviewers" / "reviewer-test.md"
IMPACT_AGENT = REPO / "agents" / "reviewers" / "reviewer-impact.md"
SKILL = REPO / "skills" / "fleet-review" / "SKILL.md"
LAWS = REPO / "docs" / "review-laws" / "REVIEW_LAWS.md"
ROLE_FILES = {"test": TEST_AGENT, "impact": IMPACT_AGENT}

# replace by the parser's names after #2511: a copy of the format the parser reads (digits only, survivors not above runs; one line of 1 to 80 printable ASCII characters)
LINE = re.compile(r"^(mutants-run|mutants-survived|blast-radius): ?(.*)$")
BLAST = re.compile(r"[ -~]{1,80}")


def fenced_blocks(text):
    return re.findall(r"^```\n(.*?)^```$", text, re.M | re.S)


def block_lines(text, role):
    """{name: [values]} of the lines the agent file's fenced verdict blocks hold for `role` (a line is `name: value` at the start of a line)."""
    found = {}
    for block in fenced_blocks(text):
        if not block.startswith("verdict:"):
            continue
        for raw in block.splitlines():
            m = LINE.match(raw)
            if m:
                found.setdefault(m.group(1), []).append(m.group(2))
    return found


def derived_names(texts):
    """The line names every agent file carries as a block line, derived from the files themselves, never listed here."""
    names = {}
    for role, text in texts.items():
        names[role] = sorted(block_lines(text, role))
    return names


def example_problems(found):
    """[problem] with the example values of one agent file: the format the parser reads."""
    out = []
    run, survived, blast = found.get("mutants-run"), found.get("mutants-survived"), found.get("blast-radius")
    if run is not None or survived is not None:
        if not (run and survived and re.fullmatch(r"\d+", run[0]) and re.fullmatch(r"\d+", survived[0]) and int(survived[0]) <= int(run[0])):
            out.append("the mutant lines of the example are not digits with survivors not above runs")
    if blast is not None and not BLAST.fullmatch(blast[0]):
        out.append("the blast-radius example is not 1 to 80 printable ASCII characters")
    return out


def count_problems(texts):
    """[problem]: a role whose file does not hold each of its required line names exactly once as a block line."""
    required = {"test": ("mutants-run", "mutants-survived"), "impact": ("blast-radius",)}
    out = []
    for role, names in required.items():
        found = block_lines(texts[role], role)
        for name in names:
            if len(found.get(name, [])) != 1:
                out.append("%s: %s appears %d times as a block line, not once" % (role, name, len(found.get(name, []))))
        for other in {"mutants-run", "mutants-survived", "blast-radius"} - set(names):
            if other in found:
                out.append("%s: carries %s, which is the other role's line" % (role, other))
        out.extend("%s: %s" % (role, p) for p in example_problems(found))
    return out


def sentence_problems(text, names):
    """[problem]: a line name that a text does not name once, in one sentence of its own (one line, no other line name in that sentence)."""
    out = []
    for name in names:
        lines = [l for l in text.splitlines() if "`%s: " % name in l]
        if len(lines) != 1 or len(re.findall(r"`%s: " % re.escape(name), lines[0])) != 1:
            out.append("%s is named %d times" % (name, len(lines)))
            continue
        sentences = [s for s in re.split(r"(?<=\.)\s+(?=[A-Z])", lines[0]) if "`%s: " % name in s]
        if len(sentences) != 1 or [n for n in names if "`%s: " % n in sentences[0]] != [name]:
            out.append("%s shares its sentence" % name)
    return out


def read_all():
    return {role: path.read_text() for role, path in ROLE_FILES.items()}


class TestReviewerBlockLines(unittest.TestCase):
    def test_each_agent_definition_holds_its_line_names_exactly_once_with_a_valid_example(self):
        self.assertEqual(count_problems(read_all()), [])

    def test_the_names_of_the_agent_files_are_the_names_of_the_skill_and_the_laws_each_said_once(self):
        names = sorted(n for ns in derived_names(read_all()).values() for n in ns)
        self.assertEqual(len(names), len(set(names)), names)
        for path in (SKILL, LAWS, REPO / "REVIEW_LAWS.md"):
            with self.subTest(file=str(path.relative_to(REPO))):
                self.assertEqual(sentence_problems(path.read_text(), names), [])

    def test_the_agent_paragraph_names_each_line_inline_too_so_the_instruction_is_there(self):
        for role, name in (("test", "mutants-run"), ("test", "mutants-survived"), ("impact", "blast-radius")):
            with self.subTest(role=role, name=name):
                self.assertIn("`%s: " % name, read_all()[role])

    def test_the_root_laws_copy_names_the_lines_like_the_starter_copy(self):
        self.assertEqual(sum("`mutants-run: " in l for l in (REPO / "REVIEW_LAWS.md").read_text().splitlines()), 1)


class TestReviewerBlockLinesControl(unittest.TestCase):
    """The control and the mutants: the checks above fail on the definitions as they were, and on each planted drift."""
    def strip(self, text):
        return "\n".join(l for l in text.splitlines() if not LINE.match(l)) + "\n"

    def test_definitions_without_the_lines_fail_the_check_with_three_problems(self):
        problems = count_problems({role: self.strip(text) for role, text in read_all().items()})
        self.assertEqual(len(problems), 3, problems)

    def test_the_current_definitions_pass_so_the_control_is_not_vacuous(self):
        self.assertEqual(count_problems(read_all()), [])

    def test_a_line_written_twice_a_renamed_line_and_a_bad_example_each_fail_by_name(self):
        texts = read_all()
        twice = dict(texts, test=texts["test"].replace("mutants-run: 4\n", "mutants-run: 4\nmutants-run: 4\n", 1))
        self.assertIn("test: mutants-run appears 2 times as a block line, not once", count_problems(twice))
        renamed = dict(texts, impact=texts["impact"].replace("\nblast-radius: ", "\nblast_radius: ", 1))
        self.assertIn("impact: blast-radius appears 0 times as a block line, not once", count_problems(renamed))
        survivors = dict(texts, test=texts["test"].replace("mutants-survived: 1\n", "mutants-survived: 9\n", 1))
        self.assertEqual(len(count_problems(survivors)), 1)
        wide = dict(texts, impact=re.sub(r"\nblast-radius: .*", "\nblast-radius: " + "x" * 81, texts["impact"], count=1))
        self.assertEqual(len(count_problems(wide)), 1)

    def test_a_skill_that_names_a_line_twice_or_in_a_shared_sentence_is_seen(self):
        names = sorted(n for ns in derived_names(read_all()).values() for n in ns)
        text = SKILL.read_text()
        self.assertEqual(sentence_problems(text, names), [])
        twice = text.replace("`blast-radius: <one line>`", "`blast-radius: <one line>` and `blast-radius: x`", 1)
        self.assertEqual(len(sentence_problems(twice, names)), 1)
        dropped = text.replace("`mutants-run: N`", "the first line", 1)
        self.assertEqual(sentence_problems(dropped, names), ["mutants-run is named 0 times"])
        shared = text.replace("`mutants-survived: M`", "`mutants-survived: M` and `mutants-run: N`", 1)
        self.assertEqual(len(sentence_problems(shared, names)), 2)


if __name__ == "__main__":
    unittest.main()
