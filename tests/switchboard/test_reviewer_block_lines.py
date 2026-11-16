"""Ask #2512 and #2521: the test and impact reviewer agents tell their reviewers to write the block lines the impact brief reads.

Text only. The three line names come from the PARSER (bin/switchboard/review_laws.py: MUTANTS_RUN_LINE, MUTANTS_SURVIVED_LINE, BLAST_RADIUS_LINE, read here from the
part's own source); no copy of a name lives in this file. The agent files, the review skill and the review laws text are compared with them, so a renamed
parser constant fails the tie. Run with `python3 bin/test-switchboard TestReviewerBlockLines TestReviewerBlockLinesControl`. Standard library only."""
import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TEST_AGENT = REPO / "agents" / "reviewers" / "reviewer-test.md"
IMPACT_AGENT = REPO / "agents" / "reviewers" / "reviewer-impact.md"
SKILL = REPO / "skills" / "fleet-review" / "SKILL.md"
LAWS = REPO / "docs" / "review-laws" / "REVIEW_LAWS.md"
ROLE_FILES = {"test": TEST_AGENT, "impact": IMPACT_AGENT}

PARSER = REPO / "bin" / "switchboard" / "review_laws.py"
BLAST = re.compile(r"[ -~]{1,80}")   # the format the parser reads for a blast radius: one line of 1 to 80 printable ASCII characters


def parser_names(source=None):
    """(run, survived, blast): the three block-line names the parser holds as constants, read from its source (the part is a fragment of the daemon, so it is read, not imported)."""
    tree = ast.parse(PARSER.read_text() if source is None else source)
    wanted = ("MUTANTS_RUN_LINE", "MUTANTS_SURVIVED_LINE", "BLAST_RADIUS_LINE")
    space = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in wanted:
            space[node.targets[0].id] = ast.literal_eval(node.value)
    return tuple(space[w] for w in wanted)


RUN, SURVIVED, BLAST_NAME = parser_names()


def line_re(names):
    return re.compile(r"^(%s): ?(.*)$" % "|".join(re.escape(n) for n in names))


def fenced_blocks(text):
    return re.findall(r"^```\n(.*?)^```$", text, re.M | re.S)


def block_lines(text, role, names=None):
    """{name: [values]} of the lines the agent file's fenced verdict blocks hold for `role` (a line is `name: value` at the start of a line)."""
    found = {}
    for block in fenced_blocks(text):
        if not block.startswith("verdict:"):
            continue
        for raw in block.splitlines():
            m = line_re(names or parser_names()).match(raw)
            if m:
                found.setdefault(m.group(1), []).append(m.group(2))
    return found


def derived_names(texts, names=None):
    """The line names every agent file carries as a block line, derived from the files themselves, never listed here."""
    out = {}
    for role, text in texts.items():
        out[role] = sorted(block_lines(text, role, names))
    return out


def example_problems(found, names):
    """[problem] with the example values of one agent file: the format the parser reads."""
    out = []
    run, survived, blast = (found.get(n) for n in names)
    if run is not None or survived is not None:
        if not (run and survived and re.fullmatch(r"\d+", run[0]) and re.fullmatch(r"\d+", survived[0]) and int(survived[0]) <= int(run[0])):
            out.append("the mutant lines of the example are not digits with survivors not above runs")
    if blast is not None and not BLAST.fullmatch(blast[0]):
        out.append("the blast-radius example is not 1 to 80 printable ASCII characters")
    return out


def count_problems(texts, names=None):
    """[problem]: a role whose file does not hold each of its required line names exactly once as a block line."""
    names = names or parser_names()
    required = {"test": names[:2], "impact": names[2:]}
    out = []
    names_all = names
    for role, required_names in required.items():
        found = block_lines(texts[role], role, names)
        for name in required_names:
            if len(found.get(name, [])) != 1:
                out.append("%s: %s appears %d times as a block line, not once" % (role, name, len(found.get(name, []))))
        for other in set(names_all) - set(required_names):
            if other in found:
                out.append("%s: carries %s, which is the other role's line" % (role, other))
        out.extend("%s: %s" % (role, p) for p in example_problems(found, names_all))
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

    def test_the_parser_names_are_three_distinct_names_the_parser_reads_through_its_constants(self):
        self.assertEqual(len(set(parser_names())), 3)
        text = PARSER.read_text()
        for const in ("MUTANTS_RUN_LINE", "MUTANTS_SURVIVED_LINE", "BLAST_RADIUS_LINE"):
            self.assertIn("line.startswith(%s + " % const, text)
        for name in parser_names():   # the parser holds each name once, as its constant
            self.assertEqual(len(re.findall(r'"%s' % re.escape(name), text)), 1, name)

    def test_the_names_of_the_agent_files_are_the_names_of_the_parser_the_skill_and_the_laws_each_said_once(self):
        names = sorted(n for ns in derived_names(read_all()).values() for n in ns)
        self.assertEqual(names, sorted(parser_names()))
        for path in (SKILL, LAWS, REPO / "REVIEW_LAWS.md"):
            with self.subTest(file=str(path.relative_to(REPO))):
                self.assertEqual(sentence_problems(path.read_text(), parser_names()), [])

    def test_the_agent_paragraph_names_each_line_inline_too_so_the_instruction_is_there(self):
        for role, name in (("test", RUN), ("test", SURVIVED), ("impact", BLAST_NAME)):
            with self.subTest(role=role, name=name):
                self.assertIn("`%s: " % name, read_all()[role])

    def test_the_root_laws_copy_names_the_lines_like_the_starter_copy(self):
        self.assertEqual(sum("`%s: " % RUN in l for l in (REPO / "REVIEW_LAWS.md").read_text().splitlines()), 1)


class TestReviewerBlockLinesControl(unittest.TestCase):
    """The control and the mutants: the checks above fail on the definitions as they were, and on each planted drift."""
    def strip(self, text):
        rx = line_re(parser_names())
        return "\n".join(l for l in text.splitlines() if not rx.match(l)) + "\n"

    def renamed_parser(self, const):
        source = PARSER.read_text()
        old = re.search(r'^%s = "([^"]+)"' % const, source, re.M).group(1)
        return old, source.replace('%s = "%s"' % (const, old), '%s = "%s-renamed"' % (const, old), 1)

    def test_definitions_without_the_lines_fail_the_check_with_three_problems(self):
        problems = count_problems({role: self.strip(text) for role, text in read_all().items()})
        self.assertEqual(len(problems), 3, problems)

    def test_the_current_definitions_pass_so_the_control_is_not_vacuous(self):
        self.assertEqual(count_problems(read_all()), [])

    def test_S2_a_renamed_parser_constant_fails_the_tie_for_the_agents_the_skill_and_the_laws(self):
        for const in ("MUTANTS_RUN_LINE", "MUTANTS_SURVIVED_LINE", "BLAST_RADIUS_LINE"):
            with self.subTest(const=const):
                old, source = self.renamed_parser(const)
                names = parser_names(source)
                self.assertNotIn(old, names)
                self.assertNotEqual(count_problems(read_all(), names), [])
                self.assertNotEqual(sentence_problems(SKILL.read_text(), names), [])
                self.assertNotEqual(sentence_problems(LAWS.read_text(), names), [])

    def test_S3_a_renamed_line_in_an_agent_file_fails_the_tie(self):
        texts = read_all()
        for role, name in (("test", RUN), ("test", SURVIVED), ("impact", BLAST_NAME)):
            with self.subTest(name=name):
                drifted = dict(texts, **{role: texts[role].replace("\n%s: " % name, "\n%s_x: " % name, 1)})
                self.assertIn("%s: %s appears 0 times as a block line, not once" % (role, name), count_problems(drifted))

    def test_U1_control_the_old_local_copy_form_passes_a_parser_rename_and_the_new_form_does_not(self):
        old, source = self.renamed_parser("BLAST_RADIUS_LINE")
        local_copy = (RUN, SURVIVED, old)   # what the old test held: a literal copy, blind to the parser
        self.assertEqual(count_problems(read_all(), local_copy), [])   # the copy and the agents agree with each other, so it passes
        self.assertNotEqual(count_problems(read_all(), parser_names(source)), [])   # reading the parser's names, the tie fails

    def test_a_line_written_twice_a_renamed_line_and_a_bad_example_each_fail_by_name(self):
        texts = read_all()
        twice = dict(texts, test=texts["test"].replace("%s: 4\n" % RUN, "%s: 4\n%s: 4\n" % (RUN, RUN), 1))
        self.assertIn("test: %s appears 2 times as a block line, not once" % RUN, count_problems(twice))
        survivors = dict(texts, test=texts["test"].replace("%s: 1\n" % SURVIVED, "%s: 9\n" % SURVIVED, 1))
        self.assertEqual(len(count_problems(survivors)), 1)
        wide = dict(texts, impact=re.sub(r"\n%s: .*" % BLAST_NAME, "\n%s: " % BLAST_NAME + "x" * 81, texts["impact"], count=1))
        self.assertEqual(len(count_problems(wide)), 1)

    def test_a_skill_that_names_a_line_twice_or_in_a_shared_sentence_is_seen(self):
        names = sorted(parser_names())
        text = SKILL.read_text()
        self.assertEqual(sentence_problems(text, names), [])
        twice = text.replace("`%s: <one line>`" % BLAST_NAME, "`%s: <one line>` and `%s: x`" % (BLAST_NAME, BLAST_NAME), 1)
        self.assertEqual(len(sentence_problems(twice, names)), 1)
        dropped = text.replace("`%s: N`" % RUN, "the first line", 1)
        self.assertEqual(sentence_problems(dropped, names), ["%s is named 0 times" % RUN])
        shared = text.replace("`%s: M`" % SURVIVED, "`%s: M` and `%s: N`" % (SURVIVED, RUN), 1)
        self.assertEqual(len(sentence_problems(shared, names)), 2)


if __name__ == "__main__":
    unittest.main()
