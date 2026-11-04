"""The platform class list (#2132): tests/platform-classes.txt names the classes bin/test-switchboard --platform-only runs.

Run with `python3 bin/test-switchboard TestPlatformClassList`. Standard library only.
"""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LIST = REPO / "tests" / "platform-classes.txt"


def listed(path=LIST):
    names = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def unknown(names, repo=REPO):
    sources = [repo / "bin" / "test-switchboard"] + sorted(repo.glob("tests/**/test_*.py")) + sorted(repo.glob("cloud/*/tests/test_*.py"))
    text = "\n".join(p.read_text() for p in sources)
    return [n for n in names if not re.search(r"^class %s\b" % re.escape(n), text, re.M)]


def main(argv):
    missing = unknown(listed(argv[0] if argv else LIST))
    for name in missing:
        print("unknown class: " + name)
    return 1 if missing else 0


class TestPlatformClassList(unittest.TestCase):
    def test_every_listed_class_exists(self):
        self.assertEqual(unknown(listed()), [])

    def test_the_list_is_not_empty_and_has_no_duplicates(self):
        names = listed()
        self.assertTrue(names)
        self.assertEqual(len(names), len(set(names)))

    def test_a_planted_unknown_name_fails_naming_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "l.txt"
            p.write_text("TestPlatformPaths  # x\nTestNoSuchClassPlanted  # y\n")
            self.assertEqual(unknown(listed(p)), ["TestNoSuchClassPlanted"])
            self.assertEqual(main([str(p)]), 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
