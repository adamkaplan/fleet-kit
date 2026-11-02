"""#1858: the daemon-thread rule is stated where a person writes a test (scenario S3). Offline; reads text only, starts no thread."""
import ast
import pathlib
import unittest

SRC = pathlib.Path(__file__).with_name("test_stub_cleanup.py")
PHRASE = "is started with daemon=True"
MESSAGE = "the test-writing guidance does not name the daemon rule"


def check(doc):
    if PHRASE not in doc:
        raise AssertionError(MESSAGE)


class ThreadRuleText(unittest.TestCase):
    def doc(self):
        return ast.get_docstring(ast.parse(SRC.read_text()))

    def test_control_then_mutant(self):
        doc = self.doc()
        check(doc)  # control first
        self.assertEqual(doc.count(PHRASE), 1)
        i = doc.index("A thread of a test " + PHRASE)
        j = doc.index("fails this scan.", i) + len("fails this scan.")
        mutant = doc[:i] + doc[j:]
        self.assertNotEqual(mutant, doc)
        with self.assertRaises(AssertionError) as c:
            check(mutant)
        self.assertEqual(str(c.exception), MESSAGE)
        print("S3: control found %d of 1 sentence; mutant applied, fails 1 of 1 with %r" % (doc.count(PHRASE), str(c.exception)))


SKILL = pathlib.Path(__file__).resolve().parents[3] / "skills" / "fleet-coordination" / "SKILL.md"
S2_HEAD = "**A cloud builder starts every test thread as a daemon thread**"
S2_SENTENCES = (
    ("starts every test thread as a daemon thread", "daemon=True"),
    ("python3 -B cloud/launcher/tests/test_stub_cleanup.py TestEverySite",),
)
S2_MESSAGE = "the skill does not name the daemon rule"


def s2_paragraph(text):
    for para in text.split("\n\n"):
        if para.startswith(S2_HEAD):
            return para
    return ""


def s2_found(text):
    para = s2_paragraph(text)
    return sum(1 for need in S2_SENTENCES if all(n in para for n in need))


def s2_check(text):
    if s2_found(text) != len(S2_SENTENCES):
        raise AssertionError(S2_MESSAGE)


class SkillParagraphText(unittest.TestCase):
    def test_s2_control_then_mutant(self):
        text = SKILL.read_text()
        s2_check(text)  # control first
        found = s2_found(text)
        self.assertEqual(found, 2)
        para = s2_paragraph(text)
        self.assertEqual(text.count(para), 1)
        mutant = text.replace(para, "")
        self.assertNotEqual(mutant, text)
        with self.assertRaises(AssertionError) as c:
            s2_check(mutant)
        self.assertEqual(str(c.exception), S2_MESSAGE)
        print("S2: control found %d of 2 sentences; mutant applied, fails 1 of 1 with %r" % (found, str(c.exception)))


if __name__ == "__main__":
    unittest.main()
