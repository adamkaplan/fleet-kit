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


def count_re_bindings(source):
    """(line, form) of every use of `re` that runs at import, read from the ast: every module-level statement (nested in module-level
    if/try/with/for/while/match/class bodies, never in a function body) plus the parts of a def that run at import (decorators, defaults,
    annotations) and of a class (bases, keywords, decorators) are read for an import of re (any form), any ast.Name `re`, and a call of
    __import__ / importlib.import_module / import_module with the constant 're' as its FIRST positional argument; these read 'module-level ...', and the designed function-body
    `import re` statements read 'function-local import re'; it does NOT see any binding made by dynamic code: __import__ with a keyword argument or
    with a name built at run time ('r'+'e', a variable holding 're'), globals()['re'] = ..., exec('import re'), import_module('.re', package=...),
    getattr(sys.modules, 're'); the real pins.py has none of these (shown by the control: 0 module-level, 2 function-local)."""
    import ast
    found = []

    def import_forms(node):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "re" or a.name.startswith("re."):
                    yield "import re as %s" % a.asname if a.asname else "import re"
        elif isinstance(node, ast.ImportFrom):
            if node.module == "re" and not node.level:
                for a in node.names:
                    yield "from re import *" if a.name == "*" else "from re import %s%s" % (a.name, " as " + a.asname if a.asname else "")

    def expr_forms(node, label):
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and n.id == "re":
                found.append((n.lineno, "module-level reference to re in " + label))
            elif isinstance(n, ast.NamedExpr) and any(isinstance(m, ast.Name) and m.id == "re" for m in ast.walk(n.value)):
                found.append((n.lineno, "module-level walrus using re"))
            elif isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "re":
                f = n.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name in ("__import__", "import_module"):
                    found.append((n.lineno, "module-level call of %s('re')" % name))

    def exprs(nodes, label):
        for n in nodes:
            if n is not None:
                expr_forms(n, label)

    def local(stmts):
        for node in stmts:
            for form in import_forms(node):
                found.append((node.lineno, "function-local " + form))
            for field in ("body", "orelse", "finalbody"):
                local(getattr(node, field, None) or [])
            for h in getattr(node, "handlers", []):
                local(h.body)
            for c in getattr(node, "cases", []):
                local(c.body)

    def walk(stmts):
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                exprs(node.decorator_list, "def decorator")
                exprs(a.defaults + a.kw_defaults, "def default")
                every = a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]
                exprs([x.annotation for x in every], "def annotation")
                exprs([node.returns], "def return annotation")
                local(node.body)
                continue
            for form in import_forms(node):
                found.append((node.lineno, "module-level " + form))
            label = type(node).__name__
            if isinstance(node, ast.ClassDef):
                exprs(node.decorator_list, "class decorator")
                exprs(node.bases, "class base")
                exprs([k.value for k in node.keywords], "class keyword")
            for field, value in ast.iter_fields(node):
                if field in ("body", "orelse", "finalbody") and isinstance(value, list):
                    walk(value)
                elif field == "handlers":
                    for h in value:
                        exprs([h.type], label)
                        walk(h.body)
                elif field == "cases":
                    for c in value:
                        exprs([c.pattern, c.guard], "match case")
                        walk(c.body)
                elif field in ("decorator_list", "bases", "keywords", "name"):
                    continue
                elif isinstance(value, ast.AST):
                    expr_forms(value, label)
                elif isinstance(value, list):
                    exprs([v for v in value if isinstance(v, ast.AST) and not isinstance(v, ast.stmt)], label)
    walk(ast.parse(source).body)
    return sorted(found)


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
        self.assertIn("run what you edited too", body)
        self.assertIn("cut: search timed out: N of M terms not searched", body)


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
        count = [0]   # compile calls of _pins_re plus every use of a pattern (match, search, finditer, findall), whether through _pins_re or through re directly
        real = re.compile   # the unpatched compile: _pins_re reaches it through counting, so a helper compile is counted once

        class Counted:
            def __init__(self, pattern):
                self.pattern = pattern

            def __getattr__(self, name):
                inner = getattr(self.pattern, name)
                if name not in ("match", "search", "finditer", "findall"):
                    return inner

                def use(*args, **kw):
                    count[0] += 1
                    return inner(*args, **kw)
                return use

        def counting(text):
            count[0] += 1
            return Counted(real(text))

        def compiled(pattern, *args, **kw):   # re.compile used outside the helper: the compile is counted and the object returned counts its uses
            count[0] += 1
            return Counted(real(pattern, *args, **kw))

        def direct(name):
            original = getattr(re, name)

            def use(pattern, *args, **kw):
                count[0] += 1
                return original(pattern, *args, **kw)
            return use
        with mock.patch.object(sb, "_pins_re", counting), mock.patch.object(re, "compile", compiled), mock.patch.multiple(re, **{n: direct(n) for n in ("match", "search", "finditer", "findall")}):
            listing = sb.pins_listing({}, hits, lambda path: lines)
        self.assertEqual(len(listing), 20)
        self.assertLessEqual(count[0], 5 * len(lines), count[0])   # the file, not the 2000 hits: a rescan per hit counts millions

    def test_a_finder_that_rescans_per_hit_is_caught(self):   # L1 mutant
        def rescan(lines, number, index=None):
            test, test_line, indent = None, None, None
            for i in range(min(number, len(lines)) - 1, -1, -1):
                m = sb._pins_re(sb.PINS_ANY_DEF_RE).match(lines[i])
                if m and (indent is None or len(m.group(1)) < indent):
                    if sb._pins_re(sb.PINS_TEST_DEF_RE).match(lines[i]):
                        return "TestBig", sb._pins_re(sb.PINS_TEST_DEF_RE).match(lines[i]).group(2), i + 1
                    indent = len(m.group(1))
            return "-", None, None
        self.test_a_scan_per_hit_is_bounded_by_the_file_not_the_hits()   # control: the unmutated finder passes
        applied = [0]

        def mutated(*args, **kw):
            applied[0] += 1
            return rescan(*args, **kw)
        with mock.patch.object(sb, "pins_enclosing", mutated):
            with self.assertRaises(AssertionError) as caught:
                self.test_a_scan_per_hit_is_bounded_by_the_file_not_the_hits()
        self.assertEqual(applied[0], 2000)   # the mutation was reached once per hit (2000 hits), and only inside the one patch
        self.assertRegex(str(caught.exception), r"^\d+ not less than or equal to 250005 : \d+$")

    def test_a_finder_that_rescans_per_hit_through_re_directly_is_caught(self):   # L1 mutant 2: the pattern used without the compile helper
        def rescan(lines, number, index=None):
            indent = None
            for i in range(min(number, len(lines)) - 1, -1, -1):
                m = re.match(sb.PINS_ANY_DEF_RE, lines[i])
                if m and (indent is None or len(m.group(1)) < indent):
                    if re.match(sb.PINS_TEST_DEF_RE, lines[i]):
                        return "TestBig", re.match(sb.PINS_TEST_DEF_RE, lines[i]).group(2), i + 1
                    indent = len(m.group(1))
            return "-", None, None
        self.test_a_scan_per_hit_is_bounded_by_the_file_not_the_hits()   # control: the unmutated finder passes
        applied = [0]

        def mutated(*args, **kw):
            applied[0] += 1
            return rescan(*args, **kw)
        with mock.patch.object(sb, "pins_enclosing", mutated):
            with self.assertRaises(AssertionError) as caught:
                self.test_a_scan_per_hit_is_bounded_by_the_file_not_the_hits()
        self.assertEqual(applied[0], 2000)
        self.assertRegex(str(caught.exception), r"^\d+ not less than or equal to 250005 : \d+$")

    def test_a_finder_that_rescans_per_hit_through_a_pattern_compiled_outside_the_helper_is_caught(self):   # L1 mutant 3
        def rescan(lines, number, index=None):
            indent = None
            for i in range(min(number, len(lines)) - 1, -1, -1):
                m = re.compile(sb.PINS_ANY_DEF_RE).match(lines[i])
                if m and (indent is None or len(m.group(1)) < indent):
                    if re.compile(sb.PINS_TEST_DEF_RE).match(lines[i]):
                        return "TestBig", re.compile(sb.PINS_TEST_DEF_RE).match(lines[i]).group(2), i + 1
                    indent = len(m.group(1))
            return "-", None, None
        self.test_a_scan_per_hit_is_bounded_by_the_file_not_the_hits()   # control: the unmutated finder passes
        applied = [0]

        def mutated(*args, **kw):
            applied[0] += 1
            return rescan(*args, **kw)
        with mock.patch.object(sb, "pins_enclosing", mutated):
            with self.assertRaises(AssertionError) as caught:
                self.test_a_scan_per_hit_is_bounded_by_the_file_not_the_hits()
        self.assertEqual(applied[0], 2000)
        self.assertRegex(str(caught.exception), r"^\d+ not less than or equal to 250005 : \d+$")

    PINS_SRC = REPO / "bin" / "switchboard" / "pins.py"

    def re_bindings(self, source):
        found = count_re_bindings(source)
        module = [f for f in found if f[1].startswith("module-level")]
        local = [f for f in found if f[1].startswith("function-local")]
        return module, local

    def assert_no_module_level_re(self, source):
        module, local = self.re_bindings(source)
        self.assertEqual(module, [], "module-level binding of re (the scan-bound test cannot see it): " + "; ".join("line %d: %s" % f for f in module))
        self.assertEqual(len(local), 2, "function-local `import re` count is not the designed 2: " + "; ".join("line %d" % f[0] for f in local))

    def test_pins_binds_re_at_no_module_level_and_imports_it_in_two_functions(self):   # guard on the L1 guard
        self.assert_no_module_level_re(self.PINS_SRC.read_text())

    RE_CASES = [
        ("import re", "import re\n", r"module-level import re"),
        ("import re as regex", "import re as regex\n", r"module-level import re as regex"),
        ("from re import compile", "from re import compile\n", r"module-level from re import compile"),
        ("from re import compile as c", "from re import compile as c\n", r"module-level from re import compile as c"),
        ("from re import *", "from re import *\n", r"module-level from re import \*"),
        ("assignment", 'compiled = re.compile("x")\n', r"module-level reference to re in Assign"),
        ("try block", "try:\n    import re\nexcept ImportError:\n    pass\n", r"module-level import re"),
        ("walrus", 'if (w := re.compile("a")):\n    pass\n', r"module-level walrus using re"),
        ("def default", 'def f_default(x=re.compile("a")):\n    pass\n', r"module-level reference to re in def default"),
        ("decorator", "@re.sub\ndef f_decorated():\n    pass\n", r"module-level reference to re in def decorator"),
        ("class base", "class C_base(re.Pattern):\n    pass\n", r"module-level reference to re in class base"),
        ("bare statement", 're.compile("a")\n', r"module-level reference to re in Expr"),
        ("for loop", "for r in [re]:\n    pass\n", r"module-level reference to re in For"),
        ("import in match", "match 1:\n    case 1:\n        import re\n", r"module-level import re"),
        ("__import__", '__import__("re")\n', r"module-level call of __import__\('re'\)"),
        ("import_module", 'import importlib\nimportlib.import_module("re")\n', r"module-level call of import_module\('re'\)"),
    ]

    def test_a_module_level_binding_of_re_is_caught_in_every_form(self):   # L1 guard mutants
        text = self.PINS_SRC.read_text()
        self.assert_no_module_level_re(text)   # control first
        first_def = text.index("\ndef ")
        local_import = "    import re\n"
        cases = self.RE_CASES
        messages = {}
        for name, add, form in cases:
            with self.subTest(name):
                mutant = text[:first_def] + "\n" + add + text[first_def:]
                self.assertEqual(mutant.count("\n" + add), 1, "the mutation is applied exactly once")
                with self.assertRaises(AssertionError) as caught:
                    self.assert_no_module_level_re(mutant)
                messages[name] = str(caught.exception).split("cannot see it): ", 1)[-1]
                self.assertRegex(messages[name], r"line \d+: " + form + r"(?:;|$)")
        forms = {n: f for n, _, f in cases}
        for name, add, form in cases:
            for other, message in messages.items():
                if forms[other] != form:   # try block / match / plain import are the same form
                    with self.subTest("anchor of %s does not match the message of %s" % (name, other)):
                        self.assertNotRegex(message, r"line \d+: " + form + r"(?:;|$)")
        with self.subTest("a third function-local import"):
            at = text.index(local_import)
            mutant = text[:at] + local_import + text[at:]
            self.assertEqual(len(re.findall(r"^    import re$", mutant, re.M)), 3, "applied once")
            with self.assertRaises(AssertionError) as caught:
                self.assert_no_module_level_re(mutant)
            self.assertRegex(str(caught.exception), r"function-local `import re` count is not the designed 2: line \d+; line \d+; line \d+$")

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


class TestPinsStrictRead(PinsRepo):
    """Strict read 2: a big diff, the user's diff settings, a stray line break, a quoted path."""
    def fake(self, diff_out="", grep=None):
        def run(argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            if argv[1] == "diff":
                return subprocess.CompletedProcess(argv, 0, diff_out, "")
            return grep(argv, **kwargs)
        return run

    def run_cmd(self, runner):
        with captured() as (out, err):
            code = sb.cmd_pins(argparse.Namespace(base="HEAD"), runner, lambda: 0.0)
        return code, out.getvalue().splitlines(), err.getvalue()

    def timing_out_after_first(self):
        n = []

        def grep(argv, **kw):
            n.append(1)
            if len(n) > 1:
                raise subprocess.TimeoutExpired(argv, 30)
            return subprocess.CompletedProcess(argv, 0, "tests/t.py:3:    widget_fn_001()\n", "")
        return grep

    def test_a_timed_out_search_cuts_and_exits_zero(self):   # M1
        self.write("tests/t.py", "def test_a(self):\n    pass\n    widget_fn_001()\n")
        diff = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,120 +0,0 @@\n" + "".join("-def widget_fn_%03d(): pass\n" % i for i in range(120))
        code, lines, err = self.run_cmd(self.fake(diff, self.timing_out_after_first()))
        self.assertEqual(code, 0, err)
        self.assertEqual(lines[0], "cut: search timed out: 70 of 120 terms not searched")
        self.assertTrue([l for l in lines if "widget_fn_001" in l], lines)

    def test_a_finder_that_raises_on_a_timeout_is_caught(self):   # M1 mutant: the old behaviour
        def old(names, messages, runner):
            try:
                runner(["git", "grep"], input="")
                return []
            except subprocess.TimeoutExpired as err:
                raise sb.PinsError("could not search the tests: %s" % err)
        with mock.patch.object(sb, "pins_search", old):
            with self.assertRaises(AssertionError):
                self.test_a_timed_out_search_cuts_and_exits_zero()

    def test_601_terms_run_in_13_chunks(self):   # M1
        grep = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "")
        names = ["widget_fn_%04d" % i for i in range(601)]
        sb.pins_search(names, [], self.fake("", grep))
        greps = [c for c in self.calls if c[0][1] == "grep"]
        self.assertEqual(len(greps), 13)
        self.assertTrue(all(len(c[1]["input"].split()) <= 50 and c[1]["timeout"] <= 30 for c in greps))

    def test_the_diff_call_pins_prefixes_and_textconv(self):   # L1 argv
        self.write("acme.py", BASE_SRC.replace("count * 2", "count * 3"))
        self.pins()
        diff = [c for c in self.calls if c[1] == "diff"][0]
        self.assertEqual(diff[:2], ["git", "diff"])
        for opt in ("--src-prefix=a/", "--dst-prefix=b/", "--no-textconv"):
            self.assertIn(opt, diff)

    def test_the_diff_call_turns_quote_path_off(self):   # L3 argv
        seen = {}

        def run(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, "", "")
        sb.pins_read_diff("HEAD", run)
        env = seen["env"]
        pairs = [(env["GIT_CONFIG_KEY_%d" % i], env["GIT_CONFIG_VALUE_%d" % i]) for i in range(int(env["GIT_CONFIG_COUNT"]))]
        self.assertIn(("core.quotePath", "false"), pairs)

    def test_other_prefixes_are_not_a_clean_answer(self):   # L1 headers
        for pre in ("c/", "w/"):
            diff = "diff --git %sm.py %sm.py\n--- %sm.py\n+++ %sm.py\n@@ -1 +0,0 @@\n-def widget_fn_001(): pass\n" % ((pre,) * 4)
            code, lines, err = self.run_cmd(self.fake(diff, lambda a, **k: subprocess.CompletedProcess(a, 1, "", "")))
            self.assertEqual(code, 1)
            self.assertIn("not understood", err)
            self.assertNotIn("no existing test names a changed definition or message", lines)

    def test_a_quoted_path_is_refused_loudly(self):   # L3 headers
        diff = 'diff --git a/m.py b/m.py\n--- a/m.py\n+++ "b/caf\\303\\251.py"\n@@ -1 +0,0 @@\n-def widget_fn_001(): pass\n'
        code, lines, err = self.run_cmd(self.fake(diff, lambda a, **k: subprocess.CompletedProcess(a, 1, "", "")))
        self.assertEqual(code, 1)
        self.assertIn("quoted path", err)

    def test_a_non_ascii_file_name_is_read(self):   # L3 real git
        self.write("caf\u00e9.py", "def widget_fn_001(): pass\n")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "more")
        self.write("caf\u00e9.py", "")
        self.assertIn("caf\u00e9.py", sb.pins_read_diff("HEAD", self.runner))

    def test_a_form_feed_and_a_line_separator_in_a_hunk_line_are_content(self):   # L2
        diff = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,2 +0,0 @@\n-def widget_fn_001(): x = \"a\x0c\u2028 lost message here\"\n-def widget_fn_002(): pass\n"
        files = sb.pins_read_diff("HEAD", self.fake(diff))
        self.assertTrue({"widget_fn_001", "widget_fn_002"} <= files["m.py"]["names"], files)
        self.assertIn("a\x0c\u2028 lost message here", files["m.py"]["messages"])


if __name__ == "__main__":
    unittest.main()
