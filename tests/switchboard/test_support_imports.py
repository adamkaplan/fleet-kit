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
            for x in tree.body if isinstance(x, (ast.Import, ast.ImportFrom)) for a in x.names]


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
