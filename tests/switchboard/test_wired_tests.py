"""Every tests/switchboard/test_*.py is wired into bin/test-switchboard (#1618).

A file is wired when bin/test-switchboard (1) imports its module and (2) binds EVERY one of its TestCase classes at module level (except a base class, whose tests run through a bound subclass, and a class listed in EXEMPT_FILES as file.py::ClassName with a reason).
A module imported with no class bound runs nothing: the same harm as no import. The files come from the directory, never from a list.
A file that must stay unwired is a row of EXEMPT_FILES (the only exemption list) with a reason of at least 20 characters.
Run with `python3 bin/test-switchboard TestEveryTestFileIsWired TestWiredCheckPlanted`. Standard library only.
"""
import ast
import functools
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
    for node in parse_cached(Path(path).read_text()).body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = [ast.unparse(b) for b in node.bases]
        if any(b.endswith("TestCase") for b in bases) or any(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_") for n in node.body):
            out.add(node.name)
    return out


@functools.lru_cache(maxsize=None)
def parse_cached(source):
    """ast.parse of a source text, once per text (the 75k-line runner parses in ~2.4 s; nothing mutates the tree)."""
    return ast.parse(source)


def _imports(node):
    """The module name when `node` is importlib.import_module("m") or (name := importlib.import_module("m")), else None."""
    if isinstance(node, ast.NamedExpr):
        node = node.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
            and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        return node.args[0].value
    return None


def direct_attributes(value):
    """The Attribute nodes a value binds directly (#1847): the value itself when it is an Attribute, or those of a Tuple or List (recursively); nothing for any other expression."""
    if isinstance(value, ast.Attribute):
        yield value
    elif isinstance(value, (ast.Tuple, ast.List)):
        for element in value.elts:
            yield from direct_attributes(element)


@functools.lru_cache(maxsize=None)
def loader_facts(source):
    """(modules imported, {module: names bound at module level}) read from the runner's source by ast.

    A module-level assignment whose right side is a CALL on the imported module (`X = alias.build(Base)` or `X = importlib.import_module("m").build(Base)`)
    binds the module too: the assigned target names are recorded for it, and `problems` accepts the file (see call_bound).
    Known limit: a bare assignment of a non-class attribute (`X = alias.CONSTANT`) is NOT claimed to be handled; only a call is read as building a class.
    """
    tree = parse_cached(source)
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
            for sub in direct_attributes(node.value):
                if not isinstance(sub, ast.Attribute):
                    continue
                mod = alias.get(sub.value.id) if isinstance(sub.value, ast.Name) else _imports(sub.value)
                if mod:
                    bound.setdefault(mod, set()).add(sub.attr)
    for mod, names in call_bound(source).items():
        bound.setdefault(mod, set()).update(names)
    return imported, bound


@functools.lru_cache(maxsize=None)
def call_bound(source):
    """{module: target names} of module-level assignments whose value is a call on the module (an attribute call on its alias or on import_module(...))."""
    tree = parse_cached(source)
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


def discovery_on(source):
    """#2206: True when the runner's loader lines call the self-registration (support.discovery.bind_discovered)."""
    return "bind_discovered(" in source


def discovered(classes, source):
    """#2206: the classes of a file the runner binds itself: those named Test* when the self-registration is in the loader lines."""
    return {c for c in classes if c.startswith("Test")} if discovery_on(source) else set()


def problems(tests_dir, source, exempt):
    """The wiring failures, one message each (a file is imported by a loader line or by the self-registration, #2206)."""
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
        if discovery_on(source) and not parses(Path(tests_dir) / name):
            out.append("%s: does not parse, so the runner cannot discover its classes" % name)
        elif mod not in imported and not discovery_on(source):
            out.append("%s: not imported by bin/test-switchboard" % name)
        elif built.get(mod) and not test_classes(Path(tests_dir) / name):
            continue    # the class is built by a call on the module: no module-level ClassDef to match
        elif not (test_classes(Path(tests_dir) / name) & (bound.get(mod, set()) | discovered(test_classes(Path(tests_dir) / name), source))):
            out.append("%s: no class bound at module level by bin/test-switchboard" % name)
    out.extend(discovery_gaps(tests_dir, source, exempt, files))
    out.extend(class_problems(tests_dir, source, exempt))
    return out


def parses(path):
    """True when the file is Python the runner can read (#2206: a file that does not parse is not discovered)."""
    try:
        parse_cached(Path(path).read_text())
        return True
    except SyntaxError:
        return False


def discovery_gaps(tests_dir, source, exempt, files):
    """#2206: the classes the self-registration cannot reach, one message each (only when the loader uses it).

    A Test* class name defined by two files and bound by no loader line: the runner binds the first, the second is lost (named with both files).
    A TestCase class in a .py file of the directory outside the test_*.py pattern: never discovered (named by file).
    """
    if not discovery_on(source):
        return []
    out = []
    owners = {}
    bound = loader_facts(source)[1]
    for path in (Path(tests_dir) / n for n in files):
        if path.name in exempt or not parses(path):
            continue
        for cls in test_classes(path):
            if cls.startswith("Test") and cls not in bound.get(path.stem, set()):      # a class a loader line binds by name is not left to discovery
                owners.setdefault(cls, []).append(path.name)
    for cls, files in sorted(owners.items()):
        if len(files) > 1:
            out.append("class %s is defined by %s: the runner binds only the first, the others are lost" % (cls, ", ".join(files)))
    for path in sorted(Path(tests_dir).glob("*.py")):
        if path.name.startswith("test_") or path.name in exempt or not parses(path):
            continue
        if test_classes(path):
            out.append("%s: holds TestCase classes but is outside the test_*.py pattern: never discovered" % path.name)
    return out


def stale_key(tests_dir, key):
    """True when an exemption key names no file, or (file.py::ClassName) a class that is not a module-level TestCase class of that file."""
    name, _, cls = key.partition("::")
    path = Path(tests_dir) / name
    if not (name.startswith("test_") and path.is_file()):
        return True
    return bool(cls) and cls not in test_classes(path)


def class_problems(tests_dir, source, exempt):
    """#1847: one message per module-level TestCase class that the runner does not bind by name.

    A base class is not required only when a class that derives from it (directly or through other classes of the file) is bound;
    otherwise it is reported with its subclasses. A class defined more than once at module level is reported with the count.
    A class is exempt by the key file.py::ClassName (its reason is read by `problems`); a file name key exempts the whole file.
    An exemption key that names no file, or no TestCase class of the file, is reported. A file the runner does not import is reported by `problems`.
    """
    imported, bound = loader_facts(source)
    out = []
    for key in sorted(exempt):
        if stale_key(tests_dir, key):
            out.append("%s: exemption key names no file or no module-level TestCase class of it" % key)
    for path in sorted(Path(tests_dir).glob("test_*.py")):
        name, mod = path.name, path.stem
        if name in exempt or (mod not in imported and not discovery_on(source)):
            continue
        if not parses(path):
            continue
        classes = test_classes(path)
        bound_here = bound.get(mod, set()) | discovered(classes, source)
        parents = {}
        counts = {}
        for node in parse_cached(path.read_text()).body:
            if isinstance(node, ast.ClassDef):
                parents[node.name] = [ast.unparse(b).split(".")[-1] for b in node.bases]
                counts[node.name] = counts.get(node.name, 0) + 1
        for cls in sorted(classes):
            if counts[cls] > 1:
                out.append("%s: class %s defined %d times: only the last runs" % (name, cls, counts[cls]))
        hidden = set()      # the ancestors of a bound class: their tests run through it
        for cls in classes & bound_here:
            todo = list(parents.get(cls, []))
            while todo:
                parent = todo.pop()
                if parent not in hidden:
                    hidden.add(parent)
                    todo.extend(parents.get(parent, []))
        for cls in sorted(classes - bound_here - hidden):
            if "%s::%s" % (name, cls) not in exempt:
                below = sorted(c for c in classes if c != cls and cls in _lineage_of(c, parents))
                out.append("%s: class %s is not bound at module level by bin/test-switchboard or discovered (named Test*)%s"
                           % (name, cls, " (subclasses: %s)" % ", ".join(below) if below else ""))
    return out


def _lineage_of(cls, parents):
    seen, todo = set(), list(parents.get(cls, []))
    while todo:
        parent = todo.pop()
        if parent not in seen:
            seen.add(parent)
            todo.extend(parents.get(parent, []))
    return seen


class TestEveryTestFileIsWired(unittest.TestCase):
    def test_the_real_directory_is_wired(self):
        files = list(TESTS.glob("test_*.py"))
        self.assertGreaterEqual(len(files), 21)
        self.assertEqual(problems(TESTS, RUNNER.read_text(), EXEMPT_FILES), [])
        self.assertEqual(problems(TESTS, runner_loader_source(), EXEMPT_FILES), [])     # the reduced text the planted tests use gives the same verdict

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
        lines = [l for l in RUNNER.read_text().splitlines() if "TestMacosReader" in l and l.startswith("TestMacosReader")]
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

def loader_lines(source):
    """The runner's module-level statements that are not a def or class (its loader lines), lines kept verbatim: what `problems` reads of the 75k-line runner, at a fraction of the parse cost."""
    lines = source.splitlines(True)
    keep = []
    for node in parse_cached(source).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            keep.extend(lines[node.lineno - 1:node.end_lineno])
    return "".join(keep)


LOADER_SOURCE = []      # one entry: the reduced runner text, built once per process (a mutant module copy is handed this one)


def runner_loader_source():
    if not LOADER_SOURCE:
        LOADER_SOURCE.append(loader_lines(RUNNER.read_text()))
    return LOADER_SOURCE[0]


class TestWiredCheckPlanted(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wired-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in TESTS.glob("test_*.py"):
            shutil.copy(p, self.tmp / p.name)
        self.source = runner_loader_source()

    def plant(self):
        (self.tmp / "test_planted.py").write_text("import unittest\n\nclass TestP(unittest.TestCase):\n    def test_a(self):\n        pass\n")

    def test_s3_a_planted_file_that_does_not_parse_fails_naming_it(self):
        (self.tmp / "test_planted.py").write_text("import unittest\n\nclass TestP(unittest.TestCase)\n    def test_a(self):\n        pass\n")
        out = problems(self.tmp, self.source, {})
        self.assertEqual(len(out), 1, out)
        self.assertIn("test_planted.py", out[0])
        self.assertIn("does not parse", out[0])

    def test_s4_a_listed_file_with_a_reason_passes(self):
        self.plant()
        self.assertEqual(problems(self.tmp, self.source, {"test_planted.py": "a planted helper that holds no test"}), [])

    def test_s4_a_short_reason_fails_naming_the_entry(self):
        self.plant()
        out = problems(self.tmp, self.source, {"test_planted.py": "too short"})
        self.assertEqual(len(out), 1, out)
        self.assertIn("test_planted.py", out[0])

    def test_s5_a_class_name_defined_by_two_files_fails_naming_it(self):
        for n in ("test_planted.py", "test_planted_two.py"):
            (self.tmp / n).write_text(PLANTED)
        out = problems(self.tmp, self.source, {})
        self.assertEqual(len(out), 1, out)
        self.assertIn("class TestP is defined by test_planted.py, test_planted_two.py", out[0])

    def test_s3_a_file_outside_the_pattern_fails_naming_it(self):
        (self.tmp / "planted_checks.py").write_text(PLANTED)
        out = problems(self.tmp, self.source, {})
        self.assertEqual(len(out), 1, out)
        self.assertIn("planted_checks.py", out[0])
        self.assertIn("outside the test_*.py pattern", out[0])

    def test_s5_a_file_whose_classes_are_not_named_test_fails(self):
        (self.tmp / "test_planted.py").write_text(PLANTED.replace("TestP", "CheckP"))
        out = problems(self.tmp, self.source, {})
        self.assertTrue(any("test_planted.py: no class bound" in m for m in out), out)
        self.assertTrue(any("class CheckP " in m for m in out), out)

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

    def test_s3_a_planted_file_with_one_class_not_named_test_fails_naming_it(self):
        (self.tmp / "test_planted.py").write_text(TWO_CLASSES.replace("TestB", "CheckB"))
        out = problems(self.tmp, self.source, {})
        self.assertEqual(len(out), 1, out)
        self.assertIn("test_planted.py", out[0])
        self.assertIn("CheckB", out[0])
        self.assertNotIn("TestA", out[0])

    def test_s3_a_listed_class_with_a_reason_passes(self):
        (self.tmp / "test_planted.py").write_text(TWO_CLASSES)
        self.assertEqual(problems(self.tmp, self.source + LOADER_TA, {"test_planted.py::TestB": LONG}), [])

    def test_s3_a_removed_binding_without_discovery_fails_naming_it(self):
        line = "TestMacosReader = _census.TestMacosReader   # #1847\n"
        self.assertEqual(self.source.count(line), 1)
        self.assertEqual(self.source.count("bind_discovered("), 1)
        out = problems(self.tmp, self.source.replace(line, "").replace("bind_discovered(", "bind_nothing("), {})
        out = [o for o in out if not o.startswith("test_loader_merge.py:")]   # a file only discovery reaches (#2206) is unimported once discovery is off: that is the point
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

    def lone(self, text, name="test_planted.py"):
        """A directory holding only the planted file (#1847)."""
        d = Path(tempfile.mkdtemp(prefix="wired-lone-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / name).write_text(text)
        return d

    def test_s9_a_bound_subclass_hides_its_base_and_an_unbound_one_is_reported_with_it(self):
        d = self.lone(BASE_AND_SUB)
        self.assertEqual(len(class_problems(d, "from test_planted import TestSub\n", {})), 0)
        out = class_problems(d, "import test_planted\n", {})
        self.assertEqual(len(out), 2, out)
        self.assertIn("class Base ", out[0])
        self.assertIn("TestSub", out[0])
        self.assertIn("class TestSub ", out[1])
        chain = self.lone(CHAIN)
        bound_leaf = class_problems(chain, "from test_planted import Leaf\n", {})
        self.assertEqual(len(bound_leaf), 0, bound_leaf)
        out = class_problems(chain, "import test_planted\n", {})
        self.assertEqual(len(out), 3, out)

    def test_s10_stale_exemption_keys_are_reported_by_name(self):
        d = self.lone(TWO_CLASSES)
        keys = {"test_gone.py::TestX": LONG, "test_planted.py::TestNope": LONG, "test_nofile.py": LONG, "test_planted.py::TestB": LONG}
        out = problems(d, "from test_planted import TestA\n", keys)
        self.assertEqual(len(out), 3, out)
        for key in ("test_gone.py::TestX", "test_planted.py::TestNope", "test_nofile.py"):
            self.assertEqual(len([m for m in out if m.startswith(key + ": exemption key")]), 1, out)
        self.assertEqual([m for m in out if "TestB" in m], [])

    def test_s11_a_class_defined_twice_is_reported_with_the_count(self):
        twice = self.lone(TWO_CLASSES.replace("TestB", "TestA"))
        out = problems(twice, "from test_planted import TestA\n", {})
        self.assertEqual(len(out), 1, out)
        self.assertIn("class TestA defined 2 times: only the last runs", out[0])
        once = self.lone(TWO_CLASSES)
        self.assertEqual(problems(once, "from test_planted import TestA, TestB\n", {}), [])

    def test_s12_only_the_direct_forms_bind_a_class(self):
        runner = ('import importlib\nm = importlib.import_module("test_planted")\nA = m.TestA\nB, C = (m.TestB, m.TestC)\n'
                  'D = m.build()\nE = m.TestE.helper\nF = [m.TestF][0]\n')
        names = ["TestA", "TestB", "TestC", "D", "TestE", "TestF"]
        d = self.lone("import unittest\n\n" + "".join(
            "class %s(unittest.TestCase):\n    def test_x(self):\n        pass\n\n" % n for n in names))
        bound = sorted(loader_facts(runner)[1]["test_planted"])
        self.assertEqual(len(bound), 4, bound)
        self.assertEqual(bound, ["D", "TestA", "TestB", "TestC"])
        out = problems(d, runner, {})
        self.assertEqual(len(out), 2, out)
        self.assertIn("class TestE ", out[0])
        self.assertIn("class TestF ", out[1])
        self.assertEqual(problems(TESTS, RUNNER.read_text(), EXEMPT_FILES), [])

    def test_s2_the_real_directory_has_no_unbound_class(self):
        files = list(TESTS.glob("test_*.py"))
        classes = [c for f in files for c in test_classes(f)]
        self.assertGreaterEqual(len(classes), 276)
        self.assertEqual(class_problems(TESTS, RUNNER.read_text(), EXEMPT_FILES), [])

# Both lines are built from halves so that each appears once, in `problems`, never as one literal in this file.
MUTATED_LINE = "    files = sorted(m + " + '".py" for m in imported if (Path(tests_dir) / (m + ".py")).exists())'
ORIGINAL_LINE = "    files = sorted(p.name for p in Path(tests_dir)." + 'glob("test_*.py"))'
SKIPPED = ("test_s3_a_planted_file_that_does_not_parse_fails_naming_it", "test_s5_a_class_name_defined_by_two_files_fails_naming_it")
NO_CLASS = ("test_s5_a_file_whose_classes_are_not_named_test_fails",)
# The second mutant skips the class-bound check (a directory read alone cannot hide an imported file with no class bound).
CLASS_ORIGINAL = "        elif not (test_classes(Path(tests_dir) / name) & (" + 'bound.get(mod, set()) | discovered(test_classes(Path(tests_dir) / name), source))):'
CLASS_MUTATED = "        elif False and (test_classes(Path(tests_dir) / name) & (" + 'bound.get(mod, set()) | discovered(test_classes(Path(tests_dir) / name), source))):'


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
    """S6: a checker that skips the directory read must FAIL the planted unparsable-file and duplicate-name tests (run against a scratch copy outside the repository)."""
    _controls = {}

    @classmethod
    def setUpClass(cls):
        cls.original_text = Path(__file__).read_text()      # one read per class

    def control(self, name):
        """The unmutated check's result for `name`, run once per class and shared by every test that needs it."""
        import sys
        this = sys.modules[__name__]
        if name not in self._controls:
            self._controls[name] = run_named_cases(this, [name])[name]
        return self._controls[name]

    def load_mutant(self, target, mutated_line):
        import importlib.util
        tmp = Path(tempfile.mkdtemp(prefix="wired-mutant-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        original = self.original_text
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
        mod.LOADER_SOURCE = LOADER_SOURCE or [runner_loader_source()]
        return mod

    def test_the_mutant_fails_each_s3_and_s5_test(self):
        got = run_named_cases(self.load_mutant(ORIGINAL_LINE, MUTATED_LINE), SKIPPED)
        for name, message in got.items():
            self.assertIsNotNone(message, "the mutant PASSED %s" % name)
            self.assertNotIn("test_planted.py", message)      # the planted file is not named
            self.assertNotIn("does not parse", message)       # the diagnosing text is missing
            self.assertNotIn("is defined by", message)

    def test_the_class_check_mutant_fails_the_no_class_test(self):
        got = run_named_cases(self.load_mutant(CLASS_ORIGINAL, CLASS_MUTATED), NO_CLASS)
        message = got[NO_CLASS[0]]
        self.assertIsNotNone(message, "the mutant PASSED")
        self.assertNotIn("no class bound", message)

    def test_s4_the_one_class_per_file_mutant_fails_the_planted_test_with_its_text(self):
        name = "test_s3_a_planted_file_with_one_class_not_named_test_fails_naming_it"
        self.assertIsNone(self.control(name))      # the control: the unmutated check
        got = run_named_cases(self.load_mutant(CLASS_RULE_ORIGINAL, CLASS_RULE_MUTATED), [name])
        self.assertEqual(got, {name: "AssertionError: 0 != 1 : []"})

    def mutant_run(self, name, original, mutated):
        self.assertIsNone(self.control(name))      # the control: the unmutated check
        return run_named_cases(self.load_mutant(original, mutated), [name])[name]

    def test_s9_to_s12_each_mutant_fails_with_its_text(self):
        S9 = "test_s9_a_bound_subclass_hides_its_base_and_an_unbound_one_is_reported_with_it"
        rows = [
            ("s9 always-exempt base", S9, S9_ORIGINAL, S9_MUTATED,
             "AssertionError: 1 != 2 : ['test_planted.py: class TestSub is not bound at module level by bin/test-switchboard or discovered (named Test*)']"),
            # a base is hidden only by a DIRECT bound subclass, never through a chain
            ("s9 direct-only base", S9, S9_DIRECT_ORIGINAL, S9_DIRECT_MUTATED,
             "AssertionError: 1 != 0 : ['test_planted.py: class Base is not bound at module level by bin/test-switchboard or discovered (named Test*) (subclasses: Leaf, Mid)']"),
            ("s10 skip stale keys", "test_s10_stale_exemption_keys_are_reported_by_name", S10_ORIGINAL, S10_MUTATED,
             "AssertionError: 0 != 3 : []"),
            ("s11 count once", "test_s11_a_class_defined_twice_is_reported_with_the_count", S11_ORIGINAL, S11_MUTATED,
             "AssertionError: 0 != 1 : []"),
            ("s12 read every attribute", "test_s12_only_the_direct_forms_bind_a_class", S12_ORIGINAL, S12_MUTATED,
             "AssertionError: 7 != 4 : ['D', 'TestA', 'TestB', 'TestC', 'TestE', 'TestF', 'build']"),
        ]
        for label, name, original, mutated, expected in rows:
            with self.subTest(label):
                self.assertEqual(self.mutant_run(name, original, mutated), expected)

    def test_the_real_module_passes_the_same_tests(self):
        self.assertEqual({n: self.control(n) for n in SKIPPED + NO_CLASS}, {n: None for n in SKIPPED + NO_CLASS})


# #1847: the class check. Halves keep each mutated line once in this file.
CLASS_RULE_ORIGINAL = "        for cls in sorted(classes - " + "bound_here - hidden):"
CLASS_RULE_MUTATED = "        for cls in sorted(set() if classes & " + "bound_here else classes - hidden):"
TWO_CLASSES = "import unittest\n\nclass TestA(unittest.TestCase):\n    def test_a(self):\n        pass\n\nclass TestB(unittest.TestCase):\n    def test_b(self):\n        pass\n"
BASE_AND_SUB = "import unittest\n\nclass Base(unittest.TestCase):\n    def test_a(self):\n        pass\n\nclass TestSub(Base):\n    def test_b(self):\n        pass\n"
S9_ORIGINAL = "        for cls in sorted(classes - " + "bound_here - hidden):"
S9_DIRECT_ORIGINAL = "                    hidden.add(parent)\n                    todo.extend(parents.get(parent, []))\n        for cls in sorted("
S9_DIRECT_MUTATED = "                    hidden.add(parent)\n        for cls in sorted("
S9_MUTATED = "        for cls in sorted(classes - " + "bound_here - {b for v in parents.values() for b in v}):"
S10_ORIGINAL = "        if stale_" + "key(tests_dir, key):"
S10_MUTATED = "        if False and stale_" + "key(tests_dir, key):"
S11_ORIGINAL = "            if counts[cls] " + "> 1:"
S11_MUTATED = "            if min(counts[cls], 1) " + "> 1:"
S12_ORIGINAL = "            for sub in direct_" + "attributes(node.value):"
S12_MUTATED = "            for sub in ast." + "walk(node.value):"
CHAIN = ("import unittest\n\nclass Base(unittest.TestCase):\n    def test_a(self):\n        pass\n\n"
         "class Mid(Base):\n    def test_b(self):\n        pass\n\nclass Leaf(Mid):\n    def test_c(self):\n        pass\n")
PLANTED = "import unittest\n\nclass TestP(unittest.TestCase):\n    def test_a(self):\n        pass\n"
LOADER_TA = "\nfrom test_planted import TestA\n"
LONG = "a class another runner executes"


if __name__ == "__main__":
    unittest.main()
