"""The guard of unused imports in the support modules (#1806): every import of tests/switchboard/support is used, or is listed with its reader."""
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

GUARD_DIR = os.path.dirname(os.path.abspath(__file__))
SUPPORT = os.path.join(GUARD_DIR, "support")

# Each entry: (file, name, reader). A re-exported name that another file reads. Empty now.
EXCEPTIONS = []


def imported_names(tree):
    return [(a.asname or (a.name.split(".")[0] if isinstance(x, ast.Import) else a.name))
            for x in ast.walk(tree) if isinstance(x, (ast.Import, ast.ImportFrom)) for a in x.names]


def check_text(fname, text, exceptions=()):
    """Failures of one module text: an unused imported name, or an exception entry with no reader."""
    failures = []
    allowed = set()
    for entry_file, entry_name, reader in exceptions:
        if entry_file != fname:
            continue
        if not reader:
            failures.append("%s: exception entry for %s has no reader" % (fname, entry_name))
        else:
            allowed.add(entry_name)
    tree = ast.parse(text)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    for name in imported_names(tree):
        if name not in used and name not in allowed:
            failures.append("%s: imported name %s is never used" % (fname, name))
    failures += duplicate_bindings(fname, tree)
    return failures


def scope_imports(scope):
    """The import statements of one scope, not those of a nested function or class."""
    found = []
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop(0)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
        pending[0:0] = list(ast.iter_child_nodes(node))
    return found


_MATCH = getattr(ast, "Match", None)


def branch_paths(scope):
    """For each statement of one scope (by id), the list of (control statement, branch) it sits in."""
    paths = {}

    def walk(node, path):
        for field, value in ast.iter_fields(node):
            items = value if isinstance(value, list) else [value]
            for i, child in enumerate(items):
                if not isinstance(child, ast.AST) or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    continue
                inner = path
                if isinstance(node, ast.If) and field in ("body", "orelse"):
                    inner = path + [(id(node), field)]
                elif isinstance(node, ast.Try) and field == "body":
                    inner = path + [(id(node), "body")]
                elif isinstance(node, ast.Try) and field == "handlers":
                    inner = path + [(id(node), "handler%d" % i)]
                elif isinstance(node, ast.Try) and field in ("orelse", "finalbody"):
                    inner = path + [(id(node), "try-" + field)]
                elif _MATCH is not None and isinstance(node, _MATCH) and field == "cases":
                    inner = path + [(id(node), "case%d" % i)]
                paths[id(child)] = inner
                walk(child, inner)

    walk(scope, [])
    return paths


def exclusive(path_a, path_b):
    """True when the two paths sit in mutually exclusive branches of one if or try."""
    branches = dict(path_a)
    for node, branch in path_b:
        other = branches.get(node)
        if other is not None and other != branch:
            return _exclusive_pair(other, branch)
    return False


def _exclusive_pair(one, two):
    if one.startswith("case") and two.startswith("case"):
        return True
    if "try-orelse" in (one, two) and (one.startswith("handler") or two.startswith("handler")):
        return True
    if one.startswith("handler") or two.startswith("handler"):
        return all(b == "body" or b.startswith("handler") for b in (one, two))
    return {one, two} == {"body", "orelse"}


def duplicate_bindings(fname, tree):
    """A name bound by more than one import statement of one scope: reported once, with the first binding."""
    failures = []
    scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    for scope in scopes:
        paths = branch_paths(scope)
        bound = {}
        for stmt in scope_imports(scope):
            for alias in stmt.names:
                name = alias.asname or (alias.name.split(".")[0] if isinstance(stmt, ast.Import) else alias.name)
                bound.setdefault(name, []).append(stmt)
        for name, stmts in bound.items():
            if len(stmts) < 2:
                continue
            if not any(not exclusive(paths[id(a)], paths[id(b)]) for i, a in enumerate(stmts) for b in stmts[i + 1:]):
                continue
            texts = [ast.unparse(s) for s in stmts]
            submodules = (all(isinstance(s, ast.Import) and not any(a.asname for a in s.names) for s in stmts)
                          and len(set(texts)) == len(texts))
            if submodules:
                continue
            failures.append("%s: %s is bound twice; the first import (line %d: %s) is never used"
                            % (fname, name, stmts[0].lineno, texts[0]))
    return failures


def check_directory(directory, exceptions=()):
    """Reads every .py file of the directory listing; returns (failures, files read)."""
    failures, read = [], []
    for fname in sorted(os.listdir(directory)):
        if fname.endswith(".py"):
            read.append(fname)
            with open(os.path.join(directory, fname)) as handle:
                failures += check_text(fname, handle.read(), exceptions)
    return failures, read


FAKE = "import json\nimport os\n\nprint(os.name)\n"


class TestFakeModule(unittest.TestCase):
    def test_unused_name_is_reported(self):
        failures = check_text("fake.py", FAKE)
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("fake.py", failures[0])
        self.assertIn("json", failures[0])

    def test_exception_with_reader_passes(self):
        self.assertEqual(check_text("fake.py", FAKE, [("fake.py", "json", "test_x reads it")]), [])

    def test_exception_without_reader_is_refused(self):
        failures = check_text("fake.py", FAKE, [("fake.py", "json", "")])
        self.assertTrue(any("has no reader" in f for f in failures), failures)
        self.assertEqual(len([f for f in failures if "has no reader" in f]), 1)


class TestNestedImports(unittest.TestCase):
    def test_unused_imports_below_the_top_level_are_reported(self):
        text = "import os\n\ndef f():\n    import json\n    return os.name\n\ntry:\n    import csv\nexcept ImportError:\n    pass\n"
        failures = check_text("fake.py", text)
        self.assertEqual(len(failures), 2, failures)
        self.assertIn("fake.py", failures[0])
        self.assertIn("json", failures[0])
        self.assertIn("fake.py", failures[1])
        self.assertIn("csv", failures[1])
        used = ("import os\n\ndef f():\n    import json\n    return os.name, json.dumps, csv.reader\n\n"
                "try:\n    import csv\nexcept ImportError:\n    pass\nprint(csv.reader)\n")
        self.assertEqual(check_text("fake.py", used), [])


RULE = "the duplicate-binding rule reported %d failure(s), expected %d: %s"


class TestDuplicateBindings(unittest.TestCase):
    def test_a_name_bound_twice_is_reported_once(self):
        failures = check_text("fake.py", "from a import x\nfrom b import x\nprint(x)\n")
        self.assertEqual(len(failures), 1, RULE % (len(failures), 1, failures))
        self.assertIn("fake.py", failures[0])
        self.assertIn("x", failures[0])
        self.assertIn("from a import x", failures[0])

    def test_the_same_import_twice_is_a_duplicate(self):
        failures = check_text("fake.py", "from a import x\nfrom a import x\nprint(x)\n")
        self.assertEqual(len(failures), 1, RULE % (len(failures), 1, failures))

    def test_submodules_of_one_package_are_one_binding(self):
        text = "import importlib.machinery\nimport importlib.util\nprint(importlib.machinery, importlib.util)\n"
        self.assertEqual(check_text("fake.py", text), [])

    def test_a_try_except_importerror_fallback_is_one_binding(self):
        failures = check_text("fake.py", "try:\n    import a\nexcept ImportError:\n    import b as a\nprint(a)\n")
        self.assertEqual(len(failures), 0, RULE % (len(failures), 0, failures))

    def test_an_if_else_pair_is_one_binding(self):
        failures = check_text("fake.py", "if c:\n    from a import x\nelse:\n    from b import x\nprint(x)\n")
        self.assertEqual(len(failures), 0, RULE % (len(failures), 0, failures))

    def test_two_imports_in_one_branch_are_a_duplicate(self):
        failures = check_text("fake.py", "if c:\n    from a import x\n    from b import x\nprint(x)\n")
        self.assertEqual(len(failures), 1, RULE % (len(failures), 1, failures))
        self.assertIn("fake.py", failures[0])
        self.assertIn("x", failures[0])
        self.assertIn("from a import x", failures[0])
        failures = check_text("fake.py", "try:\n    import a\n    import a\nexcept ImportError:\n    pass\nprint(a)\n")
        self.assertEqual(len(failures), 1, RULE % (len(failures), 1, failures))

    def test_a_try_else_against_its_handler_is_one_binding(self):
        text = "try:\n    pass\nexcept ImportError:\n    import b as a\nelse:\n    import a\nprint(a)\n"
        failures = check_text("fake.py", text)
        self.assertEqual(len(failures), 0, RULE % (len(failures), 0, failures))

    @unittest.skipIf(_MATCH is None, "ast.Match needs Python 3.10 or newer")
    def test_the_arms_of_a_match_are_one_binding(self):
        text = "match v:\n    case 1:\n        import a\n    case 2:\n        import b as a\nprint(a)\n"
        failures = check_text("fake.py", text)
        self.assertEqual(len(failures), 0, RULE % (len(failures), 0, failures))

    def test_a_try_body_and_its_else_are_a_duplicate(self):
        text = "try:\n    import a\nexcept ImportError:\n    pass\nelse:\n    import a\nprint(a)\n"
        failures = check_text("fake.py", text)
        self.assertEqual(len(failures), 1, RULE % (len(failures), 1, failures))
        self.assertIn("fake.py", failures[0])
        self.assertIn("a", failures[0])
        self.assertIn("line 2", failures[0])

    @unittest.skipIf(_MATCH is None, "ast.Match needs Python 3.10 or newer")
    def test_two_imports_in_one_case_arm_are_a_duplicate(self):
        text = "match v:\n    case 1:\n        import a\n        import b as a\nprint(a)\n"
        failures = check_text("fake.py", text)
        self.assertEqual(len(failures), 1, RULE % (len(failures), 1, failures))


class TestMutant(unittest.TestCase):
    def run_copy(self, path):
        return subprocess.run([sys.executable, "-m", "unittest", "-q", "test_support_imports_mutant.TestFakeModule"],
                              cwd=os.path.dirname(path), capture_output=True, text=True)

    def test_mutant_check_fails_the_guard(self):
        with open(os.path.abspath(__file__)) as handle:
            source = handle.read()
        scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, scratch, True)
        path = os.path.join(scratch, "test_support_imports_mutant.py")
        with open(path, "w") as handle:
            handle.write(source)
        control = self.run_copy(path)
        self.assertEqual(control.returncode, 0, control.stderr)
        mutant = source.replace("def check_text(", "def _real_check_text(", 1)
        mutant = mutant.replace("\nFAKE = ", "\ndef check_text(fname, text, exceptions=()):\n    return []\n\n\nFAKE = ", 1)
        with open(path, "w") as handle:
            handle.write(mutant)
        done = self.run_copy(path)
        self.assertNotEqual(done.returncode, 0, "the mutant passed")
        self.assertIn("test_unused_name_is_reported", done.stderr)
        self.assertIn("0 != 1", done.stderr)


class TestScratchDirectory(unittest.TestCase):
    def test_added_file_is_named_and_listing_is_read(self):
        scratch = os.path.join(tempfile.mkdtemp(), "support")
        self.addCleanup(shutil.rmtree, os.path.dirname(scratch), True)
        shutil.copytree(SUPPORT, scratch, ignore=shutil.ignore_patterns("__pycache__"))
        with open(os.path.join(scratch, "added_file.py"), "w") as handle:
            handle.write(FAKE)
        failures, read = check_directory(scratch)
        self.assertEqual(read, sorted(f for f in os.listdir(scratch) if f.endswith(".py")))
        added = [f for f in failures if "added_file.py" in f and "json" in f]
        self.assertEqual(len(added), 1, failures)
        # the copy holds what the real directory holds, plus the one added failure (1 in all once cleaned)
        self.assertEqual(len(failures), len(check_directory(SUPPORT)[0]) + 1)


class TestRealDirectory(unittest.TestCase):
    def test_no_unused_import_in_support(self):
        failures, read = check_directory(SUPPORT, EXCEPTIONS)
        print("support files read: %d, failures: %d" % (len(read), len(failures)))
        self.assertEqual(failures, [], "\n".join(failures))

    def test_exception_list_has_readers(self):
        for entry in EXCEPTIONS:
            self.assertTrue(entry[2], "exception %r has no reader" % (entry,))


if __name__ == "__main__":
    unittest.main()
