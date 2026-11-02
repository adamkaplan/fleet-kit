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


if __name__ == "__main__":
    unittest.main()
