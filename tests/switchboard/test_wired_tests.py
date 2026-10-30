"""Every tests/switchboard/test_*.py is wired into bin/test-switchboard (#1618).

A file is wired when bin/test-switchboard (1) imports its module and (2) binds at least one of its TestCase classes at module level.
A module imported with no class bound runs nothing: the same harm as no import. The files come from the directory, never from a list.
A file that must stay unwired is a row of EXEMPT_FILES (the only exemption list) with a reason of at least 20 characters.
Run with `python3 bin/test-switchboard TestEveryTestFileIsWired TestWiredCheckPlanted`. Standard library only.
"""
import ast
import shutil
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "bin" / "test-switchboard"
TESTS = REPO / "tests" / "switchboard"
MIN_REASON = 20

# file name -> reason (at least 20 characters). Today empty: all files are wired.
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


def loader_facts(source):
    """(modules imported, {module: classes bound at module level}) read from the runner's source by ast."""
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
                if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id in alias:
                    bound.setdefault(alias[sub.value.id], set()).add(sub.attr)
    return imported, bound


def problems(tests_dir, source, exempt, known=None):
    """The wiring failures, one message each. `known` is only for the mutant: a list of files to check instead of the directory."""
    imported, bound = loader_facts(source)
    out = []
    for name, reason in exempt.items():
        if not isinstance(reason, str) or len(reason.strip()) < MIN_REASON:
            out.append("%s: exemption reason under %d characters" % (name, MIN_REASON))
    files = known if known is not None else sorted(p.name for p in Path(tests_dir).glob("test_*.py"))
    for name in files:
        if name in exempt:
            continue
        mod = name[:-3]
        if mod not in imported:
            out.append("%s: not imported by bin/test-switchboard" % name)
        elif not (test_classes(Path(tests_dir) / name) & bound.get(mod, set())):
            out.append("%s: no class bound at module level by bin/test-switchboard" % name)
    return out


def mutant_problems(tests_dir, source, exempt):
    """MUTANT: skips the directory read and checks only the files it already knows (the ones the runner imports)."""
    imported, _ = loader_facts(source)
    return problems(tests_dir, source, exempt, known=sorted(m + ".py" for m in imported if (Path(tests_dir) / (m + ".py")).exists()))


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

    def test_s6_the_mutant_that_skips_the_directory_misses_the_planted_file(self):
        self.plant()
        self.assertEqual(mutant_problems(self.tmp, self.source, {}), [])   # the S3 test would fail with it
        self.assertNotEqual(problems(self.tmp, self.source, {}), [])

    def test_s6_the_mutant_misses_the_removed_import(self):
        lines = [l for l in self.source.splitlines(True) if 'import_module("test_size_ceilings")' not in l]
        self.assertEqual(mutant_problems(self.tmp, "".join(lines), {}), [])   # the S5 test would fail with it
        self.assertNotEqual(problems(self.tmp, "".join(lines), {}), [])


if __name__ == "__main__":
    unittest.main()
