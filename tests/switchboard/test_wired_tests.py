"""Every tests/switchboard/test_*.py is wired into bin/test-switchboard (#1618).

A file is wired when bin/test-switchboard (1) imports its module and (2) binds EVERY one of its TestCase classes at module level. (except a base class, whose tests run through its subclass, and a class listed in EXEMPT_FILES as file.py::ClassName with a reason)
A module imported with no class bound runs nothing: the same harm as no import. The files come from the directory, never from a list.
A file that must stay unwired is a row of EXEMPT_FILES (the only exemption list) with a reason of at least 20 characters.
Run with `python3 bin/test-switchboard TestEveryTestFileIsWired TestWiredCheckPlanted`. Standard library only.
"""
import ast
import importlib
import shutil
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "bin" / "test-switchboard"
TESTS = REPO / "tests" / "switchboard"
MIN_REASON = 20

# file name or file.py::ClassName -> reason (at least 20 characters). Today empty: every class is bound or a base.
EXEMPT_FILES = {}


def test_classes(path):
    """The module-level classes of a test file that are TestCases (a test_ method or a base named *TestCase)."""
    out = set()
    for node in ast.parse(Path(path).read_text()).body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = [ast.unparse(b) for b in node.bases]
        if any(b.endswith("TestCase") for b in bases) or any(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_") for n in node.body):
            out.add(node.name)
    return out


def _imports(node):
    """The module name when `node` is importlib.import_module("m") or (name := importlib.import_module("m")), else None."""
    if isinstance(node, ast.NamedExpr):
        node = node.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
            and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        return node.args[0].value
    return None


def loader_facts(source):
    """(modules imported, {module: names bound at module level}) read from the runner's source by ast.

    A module-level assignment whose right side is a CALL on the imported module (`X = alias.build(Base)` or `X = importlib.import_module("m").build(Base)`)
    binds the module too: the assigned target names are recorded for it, and `problems` accepts the file (see call_bound).
    Known limit: a bare assignment of a non-class attribute (`X = alias.CONSTANT`) is NOT claimed to be handled; only a call is read as building a class.
    """
    tree = ast.parse(source)
    alias = {}      # variable or import name -> module
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name)
                alias[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
              and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            imported.add(node.args[0].value)
    for node in ast.walk(tree):
        if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name) and _imports(node.value):
            alias[node.target.id] = _imports(node.value)
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) \
                and node.value.func.attr == "import_module" and node.value.args and isinstance(node.value.args[0], ast.Constant):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    alias[t.id] = node.value.args[0].value
    bound = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            bound.setdefault(node.module, set()).update(a.name for a in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            for sub in ast.walk(node.value):
                if not isinstance(sub, ast.Attribute):
                    continue
                mod = alias.get(sub.value.id) if isinstance(sub.value, ast.Name) else _imports(sub.value)
                if mod:
                    bound.setdefault(mod, set()).add(sub.attr)
    for mod, names in call_bound(source).items():
        bound.setdefault(mod, set()).update(names)
    return imported, bound


def call_bound(source):
    """{module: target names} of module-level assignments whose value is a call on the module (an attribute call on its alias or on import_module(...))."""
    tree = ast.parse(source)
    alias = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.NamedExpr)) and _imports(node.value):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(t, ast.Name):
                    alias[t.id] = _imports(node.value)
        elif isinstance(node, ast.Import):
            for a in node.names:
                alias[a.asname or a.name] = a.name
    out = {}
    for node in tree.body:
        if not (isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)):
            continue
        base = node.value.func.value
        mod = alias.get(base.id) if isinstance(base, ast.Name) else _imports(base)
        if mod and node.value.func.attr != "import_module":
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                out.setdefault(mod, set()).update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
    return out


def problems(tests_dir, source, exempt):
    """The wiring failures, one message each."""
    imported, bound = loader_facts(source)
    built = call_bound(source)
    out = []
    for name, reason in exempt.items():
        if not isinstance(reason, str) or len(reason.strip()) < MIN_REASON:
            out.append("%s: exemption reason under %d characters" % (name, MIN_REASON))
    files = sorted(p.name for p in Path(tests_dir).glob("test_*.py"))
    for name in files:
        if name in exempt:
            continue
        mod = name[:-3]
        if mod not in imported:
            out.append("%s: not imported by bin/test-switchboard" % name)
        elif built.get(mod) and not test_classes(Path(tests_dir) / name):
            continue    # the class is built by a call on the module: no module-level ClassDef to match
        elif not (test_classes(Path(tests_dir) / name) & bound.get(mod, set())):
            out.append("%s: no class bound at module level by bin/test-switchboard" % name)
    out.extend(class_problems(tests_dir, source, exempt))
    return out


def class_problems(tests_dir, source, exempt):
    """#1847: one message per module-level TestCase class that the runner does not bind by name.

    A base class (another class of the same file derives from it) is not required; a class is exempt by the key file.py::ClassName
    (its reason is read by `problems`); a file name key exempts the whole file. A file the runner does not import is reported by `problems`.
    """
    imported, bound = loader_facts(source)
    out = []
    for path in sorted(Path(tests_dir).glob("test_*.py")):
        name, mod = path.name, path.stem
        if name in exempt or mod not in imported:
            continue
        classes = test_classes(path)
        bases = set()
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.ClassDef):
                bases.update(ast.unparse(b).split(".")[-1] for b in node.bases)
        for cls in sorted(classes - bound.get(mod, set()) - bases):
            if "%s::%s" % (name, cls) not in exempt:
                out.append("%s: class %s is not bound at module level by bin/test-switchboard" % (name, cls))
    return out





class TestEveryTestFileIsWired(unittest.TestCase):
    def test_the_real_directory_is_wired(self):
        files = list(TESTS.glob("test_*.py"))
        self.assertGreaterEqual(len(files), 21)
        self.assertEqual(problems(TESTS, RUNNER.read_text(), EXEMPT_FILES), [])

    def test_the_exemption_list_is_the_only_one(self):
        tree = ast.parse(Path(__file__).read_text())
        dicts = [t.id for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict)
                 for t in n.targets if isinstance(t, ast.Name)]
        self.assertEqual(dicts, ["EXEMPT_FILES"])

    def test_the_real_reasons_are_long_enough(self):
        for name, reason in EXEMPT_FILES.items():
            self.assertGreaterEqual(len(reason.strip()), MIN_REASON, name)

    def test_s6_the_documentation_sentence_names_classes_the_base_rule_and_the_key(self):
        lines = [l for l in (REPO / "docs" / "switchboard.md").read_text().splitlines() if "file.py::ClassName" in l and "#1847" in l]
        self.assertEqual(len(lines), 1, lines)
        for item in ("every test class", "base class", "file.py::ClassName"):
            self.assertIn(item, lines[0])

    def test_s8_the_loader_line_binds_by_explicit_name(self):
        lines = [l for l in RUNNER.read_text().splitlines() if "TestMacosReader" in l]
        self.assertEqual(lines, ["TestMacosReader = _census.TestMacosReader   # #1847"])
        self.assertNotIn("globals", lines[0])

    def test_u3_no_test_of_this_change_reads_a_diff_against_a_ref(self):
        import inspect
        mine = [(TestEveryTestFileIsWired, n) for n in dir(TestEveryTestFileIsWired) if n.startswith(("test_s6", "test_s8", "test_u3"))]
        mine += [(TestWiredCheckPlanted, n) for n in dir(TestWiredCheckPlanted) if n.startswith(("test_s2_", "test_s3_a_planted_file_with", "test_s3_a_listed", "test_s3_a_removed_b", "test_s3_s7", "test_s7"))]
        mine += [(TestMutantIsCaught, n) for n in dir(TestMutantIsCaught) if n.startswith("test_s4_the_one")]
        self.assertEqual(len(mine), 11, mine)
        src = "".join(inspect.getsource(getattr(c, n)) for c, n in mine)
        for word in ("sub" + "process", "git" + " diff", "origin" + "/integration", "merge" + "-base"):
            self.assertNotIn(word, src)

class TestWiredCheckPlanted(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wired-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in TESTS.glob("test_*.py"):
            shutil.copy(p, self.tmp / p.name)
        self.source = RUNNER.read_text()

    def plant(self):
        (self.tmp / "test_planted.py").write_text("import unittest\n\nclass TestP(unittest.TestCase):\n    def test_a(self):\n        pass\n")

    def test_s3_a_planted_file_nothing_imports_fails(self):
        self.plant()
        out = problems(self.tmp, self.source, {})
        self.assertEqual(len(out), 1, out)
        self.assertIn("test_planted.py", out[0])
        self.assertIn("not imported", out[0])

    def test_s4_a_listed_file_with_a_reason_passes(self):
        self.plant()
        self.assertEqual(problems(self.tmp, self.source, {"test_planted.py": "a planted helper that holds no test"}), [])

    def test_s4_a_short_reason_fails_naming_the_entry(self):
        self.plant()
        out = problems(self.tmp, self.source, {"test_planted.py": "too short"})
        self.assertEqual(len(out), 1, out)
        self.assertIn("test_planted.py", out[0])

    def test_s5_a_removed_import_fails_naming_the_file(self):
        lines = [l for l in self.source.splitlines(True) if 'import_module("test_size_ceilings")' not in l]
        self.assertEqual(len(lines), len(self.source.splitlines(True)) - 1)
        out = problems(self.tmp, "".join(lines), {})
        self.assertTrue(any("test_size_ceilings.py" in m and "not imported" in m for m in out), out)

    def test_s5_an_import_with_no_class_bound_fails(self):
        lines = [l for l in self.source.splitlines(True) if not l.startswith("TestTheCheck, TestSizeMutants")]
        self.assertEqual(len(lines), len(self.source.splitlines(True)) - 1)
        out = problems(self.tmp, "".join(lines), {})
        self.assertTrue(any("test_size_ceilings.py" in m and "no class bound" in m for m in out), out)

    def test_one_line_loader_is_accepted(self):
        one = 'import importlib\nA, B = (_m := importlib.import_module("test_x")).A, _m.B\n'
        self.assertIn("test_x", loader_facts(one)[0])
        self.assertEqual(loader_facts(one)[1]["test_x"], {"A", "B"})
        plain = 'import importlib\nA, B = importlib.import_module("test_x").A, importlib.import_module("test_x").B\n'
        self.assertEqual(loader_facts(plain)[1]["test_x"], {"A", "B"})
        (self.tmp / "test_x.py").write_text("import unittest\n\nclass A(unittest.TestCase):\n    pass\n")
        only = {p.name: "a helper that holds no test of its own" for p in self.tmp.glob("test_*.py") if p.name != "test_x.py"}
        self.assertEqual(problems(self.tmp, one, only), [])
        self.assertTrue(any("test_x.py" in m and "not imported" in m for m in problems(self.tmp, "import importlib\n", only)))
        self.assertTrue(any("test_x.py" in m and "no class bound" in m for m in problems(self.tmp, "import importlib\nimportlib.import_module('test_x')\n", only)))

    def test_a_class_built_by_a_call_on_the_module_is_accepted(self):
        (self.tmp / "test_x.py").write_text("def build(base):\n    return base\n")
        only = {p.name: "a helper that holds no test of its own" for p in self.tmp.glob("test_*.py") if p.name != "test_x.py"}
        src = 'import importlib\n_x = importlib.import_module("test_x")\nTestX = _x.build(Base)\n'
        self.assertEqual(problems(self.tmp, src, only), [])
        out = problems(self.tmp, 'import importlib\n_x = importlib.import_module("test_x")\n', only)
        self.assertEqual(len(out), 1, out)
        self.assertIn("test_x.py", out[0])

    def test_s3_a_planted_file_with_one_of_two_classes_bound_fails_naming_the_second(self):
        (self.tmp / "test_planted.py").write_text(TWO_CLASSES)
        out = problems(self.tmp, self.source + LOADER_TA, {})
        self.assertEqual(len(out), 1, out)
        self.assertIn("test_planted.py", out[0])
        self.assertIn("TestB", out[0])
        self.assertNotIn("TestA", out[0])

    def test_s3_a_listed_class_with_a_reason_passes(self):
        (self.tmp / "test_planted.py").write_text(TWO_CLASSES)
        self.assertEqual(problems(self.tmp, self.source + LOADER_TA, {"test_planted.py::TestB": LONG}), [])

    def test_s3_a_removed_binding_of_a_real_class_fails_naming_it(self):
        line = "TestMacosReader = _census.TestMacosReader   # #1847\n"
        self.assertEqual(self.source.count(line), 1)
        out = problems(self.tmp, self.source.replace(line, ""), {})
        self.assertEqual(len(out), 1, out)
        self.assertIn("test_github_census.py", out[0])
        self.assertIn("TestMacosReader", out[0])

    def test_s3_s7_a_base_class_is_not_reported(self):
        (self.tmp / "test_planted.py").write_text(BASE_AND_SUB)
        self.assertEqual(problems(self.tmp, self.source + "\nfrom test_planted import TestSub\n", {}), [])
        self.assertEqual(test_classes(self.tmp / "test_planted.py"), {"Base", "TestSub"})

    def test_s7_a_short_reason_fails_naming_the_key(self):
        (self.tmp / "test_planted.py").write_text(TWO_CLASSES)
        out = problems(self.tmp, self.source + LOADER_TA, {"test_planted.py::TestB": "ten chars!"})
        self.assertEqual(len(out), 1, out)
        self.assertIn("test_planted.py::TestB", out[0])
        self.assertIn("under", out[0])

    def test_s7_a_file_name_key_keeps_its_meaning(self):
        (self.tmp / "test_planted.py").write_text(TWO_CLASSES)
        self.assertEqual(problems(self.tmp, self.source, {"test_planted.py": LONG}), [])

    def test_s2_the_real_directory_has_no_unbound_class(self):
        files = list(TESTS.glob("test_*.py"))
        classes = [c for f in files for c in test_classes(f)]
        self.assertGreaterEqual(len(classes), 276)
        self.assertEqual(class_problems(TESTS, RUNNER.read_text(), EXEMPT_FILES), [])

# Both lines are built from halves so that each appears once, in `problems`, never as one literal in this file.
MUTATED_LINE = "    files = sorted(m + " + '".py" for m in imported if (Path(tests_dir) / (m + ".py")).exists())'
ORIGINAL_LINE = "    files = sorted(p.name for p in Path(tests_dir)." + 'glob("test_*.py"))'
SKIPPED = ("test_s3_a_planted_file_nothing_imports_fails", "test_s5_a_removed_import_fails_naming_the_file")
NO_CLASS = ("test_s5_an_import_with_no_class_bound_fails",)
# The second mutant skips the class-bound check (a directory read alone cannot hide an imported file with no class bound).
CLASS_ORIGINAL = "        elif not (test_classes(Path(tests_dir) / name) & " + 'bound.get(mod, set())):'
CLASS_MUTATED = "        elif False and (test_classes(Path(tests_dir) / name) & " + 'bound.get(mod, set())):'


def run_named_cases(module, names):
    """Run the named TestWiredCheckPlanted tests of `module` in-process: {name: None if it passed, else its failure message}."""
    import io
    out = {}
    for name in names:
        result = unittest.TestResult()
        module.TestWiredCheckPlanted(name).run(result)
        bad = result.failures + result.errors
        out[name] = None if not bad else [l for l in bad[0][1].splitlines() if l.strip()][-1]
    return out


class TestMutantIsCaught(unittest.TestCase):
    """S6: a checker that skips the directory read must FAIL the S3 and S5 tests (run against a scratch copy outside the repository)."""
    def load_mutant(self, target, mutated_line):
        import importlib.util
        tmp = Path(tempfile.mkdtemp(prefix="wired-mutant-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        original = Path(__file__).read_text()
        self.assertEqual(original.count(target), 1)
        mutated = original.replace(target, mutated_line)
        self.assertNotEqual(mutated, original)
        copy = tmp / "test_wired_mutant.py"
        copy.write_text(mutated)
        text = copy.read_text()                       # read back
        self.assertEqual(text.count(mutated_line), 1)
        self.assertEqual(text.count(target), 0)
        self.assertNotIn(str(REPO), str(copy))
        spec = importlib.util.spec_from_file_location("wired_mutant", copy)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.REPO, mod.RUNNER, mod.TESTS = REPO, RUNNER, TESTS
        return mod

    def test_the_mutant_fails_each_s3_and_s5_test(self):
        got = run_named_cases(self.load_mutant(ORIGINAL_LINE, MUTATED_LINE), SKIPPED)
        for name, message in got.items():
            self.assertIsNotNone(message, "the mutant PASSED %s" % name)
            self.assertNotIn("test_planted.py", message)      # the planted file is not named
            self.assertNotIn("not imported", message)         # the diagnosing text is missing
        self.assertNotIn("test_size_ceilings.py", " ".join(got.values()))

    def test_the_class_check_mutant_fails_the_no_class_test(self):
        got = run_named_cases(self.load_mutant(CLASS_ORIGINAL, CLASS_MUTATED), NO_CLASS)
        message = got[NO_CLASS[0]]
        self.assertIsNotNone(message, "the mutant PASSED")
        self.assertNotIn("no class bound", message)

    def test_s4_the_one_class_per_file_mutant_fails_the_planted_test_with_its_text(self):
        import sys
        name = "test_s3_a_planted_file_with_one_of_two_classes_bound_fails_naming_the_second"
        this = sys.modules[__name__]
        self.assertEqual(run_named_cases(this, [name]), {name: None})      # the control: the unmutated check
        got = run_named_cases(self.load_mutant(CLASS_RULE_ORIGINAL, CLASS_RULE_MUTATED), [name])
        self.assertEqual(got, {name: "AssertionError: 0 != 1 : []"})

    def test_the_real_module_passes_the_same_tests(self):
        import sys
        this = sys.modules[__name__]
        self.assertEqual(run_named_cases(this, SKIPPED + NO_CLASS), {n: None for n in SKIPPED + NO_CLASS})


# #1847: the class check. Halves keep each mutated line once in this file.
CLASS_RULE_ORIGINAL = "        for cls in sorted(classes - " + "bound.get(mod, set()) - bases):"
CLASS_RULE_MUTATED = "        for cls in sorted(set() if classes & " + "bound.get(mod, set()) else classes - bases):"
TWO_CLASSES = "import unittest\n\nclass TestA(unittest.TestCase):\n    def test_a(self):\n        pass\n\nclass TestB(unittest.TestCase):\n    def test_b(self):\n        pass\n"
BASE_AND_SUB = "import unittest\n\nclass Base(unittest.TestCase):\n    def test_a(self):\n        pass\n\nclass TestSub(Base):\n    def test_b(self):\n        pass\n"
LOADER_TA = "\nfrom test_planted import TestA\n"
LONG = "a class another runner executes"


if __name__ == "__main__":
    unittest.main()
