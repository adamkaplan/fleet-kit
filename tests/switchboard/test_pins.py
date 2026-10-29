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

    def refuse_option_bases(self):
        out = Path(self.dir) / "scratch-out.txt"
        for base in ("--output=%s" % out, "-U5", "--no-index"):
            self.calls.clear()
            code, lines, err = self.pins(base=base)
            self.assertNotEqual(code, 0, base)
            self.assertIn("pins: the base is a ref, not an option", err)
            self.assertEqual(lines, [])
            self.assertEqual(self.calls, [], "no git call before the refusal")
            self.assertFalse(out.exists(), "no file written")

    def test_a_base_that_looks_like_an_option_is_refused(self):
        self.refuse_option_bases()

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

    def test_a_finder_without_the_option_refusal_is_caught(self):
        original = sb.pins_base_problem
        with mock.patch.object(sb, "pins_base_problem", lambda base: None):
            self.assertIsNot(sb.pins_base_problem, original, "the mutant is applied")
            with self.assertRaises(AssertionError):
                TestPins.refuse_option_bases(self)

    def test_a_finder_that_lists_the_added_tests_fails(self):
        real = sb.pins_listing
        self.run_mutant("pins_listing", lambda files, hits, read_lines: real({}, hits, read_lines))


class TestPinsReview(PinsRepo):
    """The strict read's six findings (cut terms, deleted files, bounded scans, blank messages, header look-alikes, subdirectory runs)."""

    def commit(self, msg="more"):
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", msg)

    def many_terms(self):
        self.write("wide.py", "".join('def widget_fn_%03d(): return "message number %03d here"\n' % (i, i) if i < 302 else "def widget_fn_%03d(): pass\n" % i for i in range(305)))
        self.commit()
        self.write("wide.py", "")

    def check_cut_line(self):
        self.many_terms()
        code, lines, _ = self.pins()
        self.assertEqual(code, 0)
        self.assertEqual([l for l in lines if l.startswith("cut:")], ["cut: 5 names / 2 messages not searched"])

    def test_cut_terms_are_said_with_their_counts(self):   # M1
        self.check_cut_line()

    def test_a_finder_that_cuts_terms_silently_is_caught(self):   # M1 mutant
        with mock.patch.object(sb, "pins_term_cut", lambda files: (0, 0)):
            with self.assertRaises(AssertionError):
                self.check_cut_line()

    def delete_file(self, runner=None):
        self.git("rm", "-q", "acme.py")
        with captured() as (out, err):
            code = sb.cmd_pins(argparse.Namespace(base="HEAD"), runner or self.runner, lambda: 0.0)
        return code, self.listed(out.getvalue().splitlines())

    def test_a_deleted_file_lists_the_tests_naming_its_functions(self):   # M2
        code, found = self.delete_file()
        self.assertEqual(code, 0)
        self.assertEqual(len(found), 2, found)
        self.assertTrue(any("TestAcme::test_total  matched compute_widget_total" in l for l in found), found)

    def test_a_finder_that_skips_a_deleted_file_is_caught(self):   # M2 mutant: the old side's path is hidden, as before the fix
        def blind(argv, **kwargs):
            done = self.runner(argv, **kwargs)
            if argv[1] == "diff":
                done.stdout = done.stdout.replace("--- a/acme.py", "--- /dev/null")
            return done
        _, found = self.delete_file(blind)
        self.assertEqual(found, [])

    def test_a_scan_per_hit_is_bounded_by_the_file_not_the_hits(self):   # L1
        lines = ["class TestBig(unittest.TestCase):"]
        for i in range(20):   # twenty long tests, a hundred hits in each: a rescan walks back through the test for every hit
            lines += ["    def test_n%04d(self):" % i] + ["        pass  # filler"] * 2499
        self.assertEqual(len(lines), 50001)
        hits = [("tests/big.py", 1 + 2500 * t + 25 * h, "x", "compute_widget_total") for t in range(20) for h in range(1, 101)]
        self.assertEqual(len(hits), 2000)
        count = [0]

        class Counting:
            def __init__(self, real):
                self.real = real

            def match(self, text):
                count[0] += 1
                return self.real.match(text)
        with mock.patch.object(sb, "PINS_ANY_DEF_RE", Counting(sb.PINS_ANY_DEF_RE)), mock.patch.object(sb, "PINS_CLASS_RE", Counting(sb.PINS_CLASS_RE)), \
                mock.patch.object(sb, "PINS_TEST_DEF_RE", Counting(sb.PINS_TEST_DEF_RE)):
            listing = sb.pins_listing({}, hits, lambda path: lines)
        self.assertEqual(len(listing), 20)
        self.assertLessEqual(count[0], 4 * len(lines), count[0])

    def test_a_finder_that_rescans_per_hit_is_caught(self):   # L1 mutant
        def rescan(lines, number, index=None):
            test, test_line, indent = None, None, None
            for i in range(min(number, len(lines)) - 1, -1, -1):
                m = sb.PINS_ANY_DEF_RE.match(lines[i])
                if m and (indent is None or len(m.group(1)) < indent):
                    if sb.PINS_TEST_DEF_RE.match(lines[i]):
                        return "TestBig", sb.PINS_TEST_DEF_RE.match(lines[i]).group(2), i + 1
                    indent = len(m.group(1))
            return "-", None, None
        with mock.patch.object(sb, "pins_enclosing", rescan):
            with self.assertRaises(AssertionError):
                self.test_a_scan_per_hit_is_bounded_by_the_file_not_the_hits()

    def test_a_blank_quoted_message_is_no_term(self):   # L2
        self.assertEqual(sb.pins_messages_of('x = "            "'), [])
        self.assertEqual(sb.pins_messages_of("x = '                '"), [])
        self.assertEqual(sb.pins_messages_of('x = "a real message here"'), ["a real message here"])

    def test_the_docs_say_a_changed_file_lists_its_own_test(self):   # L2 docs
        text = (REPO / "docs" / "switchboard.md").read_text()
        body = re.findall(r"^## `pins`.*?(?=^## |\Z)", text, re.S | re.M)[0]
        self.assertIn("A test in a file the diff changes lists itself", body)

    def test_a_removed_line_that_looks_like_a_header_is_content(self):   # L3
        self.write("acme.py", "def compute_widget_total(count):\n-- print(\"a lost message here\")\n    return count * 2\n")
        self.commit()
        self.write("acme.py", "def compute_widget_total(count):\n    return count * 2\n")
        files = sb.pins_read_diff("HEAD", self.runner)
        self.assertEqual(sorted(files), ["acme.py"])
        self.assertIn("a lost message here", files["acme.py"]["messages"])

    def test_a_removed_double_dash_line_does_not_end_the_file(self):   # L3, the line itself: `-- x` is removed text, `--- x` its diff line
        self.write("acme.py", "def alpha_removed_name(a):\n    pass\n-- b/other.py\n+++ b/zz.py\ndef beta_removed_name(a):\n    pass\n")
        self.commit()
        self.write("acme.py", "")
        files = sb.pins_read_diff("HEAD", self.runner)
        self.assertEqual(sorted(files), ["acme.py"])
        self.assertTrue({"alpha_removed_name", "beta_removed_name"} <= files["acme.py"]["names"], files)

    def test_a_run_from_a_subdirectory_lists_what_the_root_lists(self):   # L4
        self.write("acme.py", BASE_SRC.replace("count * 2", "count * 3"))
        _, root_lines, _ = self.pins()
        (Path(self.dir) / "pkg" / "deep").mkdir(parents=True)
        os.chdir(Path(self.dir) / "pkg" / "deep")
        _, sub_lines, _ = self.pins()
        self.assertEqual(len(self.listed(root_lines)), 1, root_lines)
        self.assertEqual(sub_lines, root_lines)


if __name__ == "__main__":
    unittest.main()
