"""`fleet-switchboard pins` (ask #1543): the existing tests that name what the diff changes or removes. Temp git repositories only; the command reads
`git diff` and `git grep` and nothing else, and each mutant is applied, asserted applied and caught."""
import argparse
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from support.loader import captured, sb
from support.paths import REPO

NO_MATCH = "no existing test names a changed definition or message"
BASE_SRC = 'def compute_widget_total(count):\n    return count * 2\n\n\ndef announce():\n    return "the widget exploded badly"\n'
BASE_TEST = ('import unittest\nfrom acme import compute_widget_total, announce\n\n\nclass TestAcme(unittest.TestCase):\n'
             '    def test_total(self):\n        self.assertEqual(compute_widget_total(3), 6)\n\n'
             '    def test_message(self):\n        self.assertEqual(announce(), "the widget exploded badly")\n\n'
             '    def test_unrelated(self):\n        self.assertTrue(True)\n')


class PinsRepo(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.old = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self.old)
        self.git("init", "-q", "-b", "main")
        self.write("acme.py", BASE_SRC)
        self.write("tests/test_acme.py", BASE_TEST)
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "base")
        self.calls = []

    def git(self, *argv):
        subprocess.run(["git"] + list(argv), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, path, text):
        p = Path(self.dir) / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def runner(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.run(argv, **kwargs)

    def pins(self, base="HEAD"):
        with captured() as (out, err):
            code = sb.cmd_pins(argparse.Namespace(base=base), self.runner, lambda: 0.0)
        return code, out.getvalue().splitlines(), err.getvalue()

    def listed(self, lines):
        return [l for l in lines if "::" in l]


class TestPins(PinsRepo):
    def change_function(self):
        self.write("acme.py", BASE_SRC.replace("count * 2", "count * 3"))

    def change_message(self):
        self.write("acme.py", BASE_SRC.replace("the widget exploded badly", "the widget stopped"))

    def add_test_calling(self, n=1):
        extra = "".join("\n    def test_added_%d(self):\n        compute_widget_total(%d)\n" % (i, i) for i in range(n))
        self.write("tests/test_new.py", "import unittest\nfrom acme import compute_widget_total\n\n\nclass TestNew(unittest.TestCase):" + extra)
        self.git("add", "-A")   # staged: a new file is in the diff against HEAD

    def behaviour(self):
        """S2, S3 and S4 in one: the assertions every mutant must fail."""
        self.change_function()
        code, lines, _ = self.pins()
        found = self.listed(lines)
        self.assertEqual(code, 0)
        self.assertEqual(len(found), 1, lines)
        self.assertIn("tests/test_acme.py:7  TestAcme::test_total  matched compute_widget_total", found[0])
        self.assertTrue(lines[-1].startswith("1 existing test "), lines)
        self.git("checkout", "-q", "--", "acme.py")
        self.change_message()
        _, lines, _ = self.pins()
        found = self.listed(lines)
        self.assertEqual(len(found), 1, lines)
        self.assertIn("TestAcme::test_message", found[0])
        self.assertIn("the widget exploded badly", found[0])
        self.git("checkout", "-q", "--", "acme.py")
        self.change_function()
        self.add_test_calling(2)
        _, lines, _ = self.pins()
        self.assertFalse([l for l in lines if "test_new.py" in l], lines)
        self.assertEqual(len(self.listed(lines)), 1, lines)

    def test_a_changed_function_is_found_in_the_test_that_calls_it(self):   # S2
        self.change_function()
        code, lines, _ = self.pins()
        found = self.listed(lines)
        self.assertEqual((code, len(found)), (0, 1), lines)
        self.assertIn("tests/test_acme.py:7  TestAcme::test_total  matched compute_widget_total", found[0])
        self.assertEqual(lines[-1].split()[0], "1")

    def test_a_removed_message_is_found_in_the_test_that_asserts_it(self):   # S3
        self.change_message()
        code, lines, _ = self.pins()
        found = self.listed(lines)
        self.assertEqual((code, len(found)), (0, 1), lines)
        self.assertIn("test_message", found[0])
        self.assertIn("the widget exploded badly", found[0])

    def test_a_test_the_diff_adds_is_not_listed(self):   # S4
        self.change_function()
        self.add_test_calling(1)
        _, lines, _ = self.pins()
        self.assertEqual([l for l in lines if "test_new.py" in l], [])

    def test_twenty_added_tests_are_not_listed(self):   # U2
        self.change_function()
        self.add_test_calling(20)
        _, lines, _ = self.pins()
        self.assertEqual([l for l in lines if "test_new.py" in l or "test_added_" in l], [])
        self.assertEqual(len(self.listed(lines)), 1)

    def test_no_match_prints_the_line_and_exits_zero(self):   # S5
        self.write("other.py", "def lonely_helper_function():\n    return 1\n")
        self.git("add", "-A")
        code, lines, _ = self.pins()
        self.assertEqual((code, lines), (0, [NO_MATCH]))

    def test_an_unreadable_diff_says_so_and_exits_non_zero(self):   # S5
        code, lines, err = self.pins(base="no-such-ref")
        self.assertNotEqual(code, 0)
        self.assertIn("could not read the diff", err)
        self.assertEqual(lines, [])

    def test_the_listing_is_bounded_and_says_it_cut(self):   # S6
        body = "".join("    def test_many_%03d(self):\n        compute_widget_total(1)\n\n" % i for i in range(300))
        self.write("tests/test_many.py", "import unittest\n\n\nclass TestMany(unittest.TestCase):\n" + body)
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "many")
        self.change_function()
        _, lines, _ = self.pins(base="HEAD")
        found = self.listed(lines)
        self.assertEqual(len(found), 200)
        self.assertEqual(len([l for l in lines if l.startswith("cut:")]), 1)
        self.assertIn("301", lines[-1])

    def test_it_reads_only(self):   # U1
        self.change_function()
        self.add_test_calling(1)
        before = sorted(str(p) for p in Path(self.dir).rglob("*") if ".git" not in p.parts)
        stamp = {p: p.stat().st_mtime_ns for p in Path(self.dir).rglob("*") if p.is_file() and ".git" not in p.parts}
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), mock.patch("socket.create_connection", side_effect=AssertionError("network")):
            self.pins()
        self.assertTrue(self.calls)
        self.assertEqual({(c[0], c[1]) for c in self.calls}, {("git", "diff"), ("git", "grep")})
        self.assertEqual(sorted(str(p) for p in Path(self.dir).rglob("*") if ".git" not in p.parts), before)
        self.assertEqual({p: p.stat().st_mtime_ns for p in stamp}, stamp)
        self.assertFalse([c for c in self.calls if "fetch" in c or "pull" in c])

    def test_the_docs_hold_one_section_naming_the_three_statements(self):   # S8
        text = (REPO / "docs" / "switchboard.md").read_text()
        sections = re.findall(r"^## `pins`.*?(?=^## |\Z)", text, re.S | re.M)
        self.assertEqual(len(sections), 1)
        body = sections[0]
        self.assertIn("It only reads", body)
        self.assertIn("What it lists", body)
        self.assertIn("Run the listed classes before you push", body)


class TestPinsMutants(PinsRepo):
    """S7: three mutants of the finder; each is applied, asserted applied and fails the behaviour check."""
    behaviour = TestPins.behaviour
    change_function = TestPins.change_function
    change_message = TestPins.change_message
    add_test_calling = TestPins.add_test_calling

    def run_mutant(self, name, mutant):
        original = getattr(sb, name)
        with mock.patch.object(sb, name, mutant):
            self.assertIsNot(getattr(sb, name), original, "the mutant is applied")
            with self.assertRaises(AssertionError):
                self.behaviour()

    def test_the_unmutated_finder_passes_the_behaviour_check(self):
        self.behaviour()

    def test_a_finder_that_lists_every_test_fails(self):
        def every(names, messages, runner):
            done = subprocess.run(["git", "grep", "-n", "def test_", "--", "tests"], stdout=subprocess.PIPE, universal_newlines=True)
            return [(l.split(":", 2)[0], int(l.split(":", 2)[1]), l.split(":", 2)[2], "x") for l in done.stdout.splitlines()]
        self.run_mutant("pins_search", every)

    def test_a_finder_that_lists_none_fails(self):
        self.run_mutant("pins_search", lambda names, messages, runner: [])

    def test_a_finder_that_lists_the_added_tests_fails(self):
        real = sb.pins_listing
        self.run_mutant("pins_listing", lambda files, hits, read_lines: real({}, hits, read_lines))


if __name__ == "__main__":
    unittest.main()
