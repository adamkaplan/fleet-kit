"""#2092: tools/pin-scan prints the other lines a plan's edits would break (S2) and runs scanning checks on a scratch copy (S3). Run by bin/test-switchboard."""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from support.paths import REPO

TOOL = REPO / "tools" / "pin-scan"


def tree_digest(root):
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob("*")):
        if ".git" in p.parts or not p.is_file():
            continue
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def git(root, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=root, check=True, capture_output=True)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        conf = "| row-one | 1 |\n| row-two | 2 |\n| row-three | 3 |\n"
        self.files = {
            "conf.txt": conf,
            "docs.md": "Intro.\nThe line | row-three | 3 | is pinned here.\n",
            "pin.py": 'DIGEST = "%s"\n' % hashlib.sha256(conf.encode()).hexdigest(),
            "notes.md": "The conf table\nhas 3 rows today.\n",
            "test_mut.py": 'mutate("conf.txt", "| row-three | 3 |", "x")\n',
            "clean.txt": "nothing to see here at all\n",
            "runner.txt": "wired: test_a\n",
            "support.py": "def helper():\n    pass\n",
            "test_a.py": "def other():\n    pass\n",
            "check_wired.py": "import sys\nfor n, l in enumerate(open('runner.txt'), 1):\n    if not l.startswith('wired: '):\n        print('runner.txt:%d: shape refused' % n); sys.exit(1)\n",
            "check_rebind.py": "import re, sys\nfor n, l in enumerate(open('test_a.py'), 1):\n    m = re.match(r'def (\\w+)', l)\n    if m and m.group(1) in open('support.py').read().replace('def helper', ''):\n        print('test_a.py:%d: rebinding' % n); sys.exit(1)\n",
        }
        for k, v in self.files.items():
            (self.root / k).write_text(v)
        git(self.root, "init", "-q")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "fixture")

    def run_tool(self, edits, *extra):
        p = Path(self.tmp.name) / "edits.json"
        p.write_text(json.dumps(edits))
        r = subprocess.run([str(TOOL), "--plan", str(p), "--root", str(self.root), *extra], capture_output=True, text=True, timeout=90)
        return r.returncode, r.stdout


class TestPinScanSearch(Fixture):
    EDIT = {"file": "conf.txt", "old": "| row-three | 3 |", "new": "| row-three | 3 |\n| row-four | 4 |"}

    def test_s2_the_four_planted_kinds_and_not_the_clean_file(self):
        code, out = self.run_tool([self.EDIT], "--no-checks")
        self.assertEqual(code, 0, out)
        got = {tuple(l.split(": ", 2)[:2]) for l in out.splitlines() if ": " in l and not l.startswith("found")}
        self.assertEqual(got, {("docs.md:2", "quote"), ("pin.py:1", "digest"), ("notes.md:2", "count"), ("test_mut.py:1", "mutant")}, out)
        self.assertNotIn("clean.txt", out)
        self.assertNotIn("conf.txt:", out)

    def test_s2_an_old_text_absent_from_its_file_is_refused(self):
        code, out = self.run_tool([dict(self.EDIT, old="not there at all")], "--no-checks")
        self.assertEqual(code, 2)


class TestPinScanWords(Fixture):
    def test_the_same_words_in_another_punctuation_are_found_and_the_edited_files_own_lines_are_not(self):
        (self.root / "prose.md").write_text("The set is contents write, pull requests write, issues read and metadata read.\n")
        git(self.root, "add", "prose.md")
        git(self.root, "commit", "-qm", "prose")
        old = '"contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read"'
        (self.root / "conf.txt").write_text("set = {%s}\nmutate(\"conf.txt\", '%s', 'x')\n" % (old, old))
        git(self.root, "commit", "-qam", "conf")
        code, out = self.run_tool([{"file": "conf.txt", "old": "set = {%s}" % old, "new": "set = {}"}], "--no-checks")
        self.assertIn("prose.md:1: quote-words", out)
        self.assertIn("conf.txt:2: mutant", out)
        self.assertNotIn("conf.txt:1:", out)


class TestPinScanModes(Fixture):
    def test_a_bare_file_and_an_unbuilt_diff_mode_are_refused(self):
        for args in (["x.json"], ["--diff", "a...b"]):
            r = subprocess.run([str(TOOL), "--root", str(self.root), *args], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2, r.stdout)


class TestPinScanChecks(Fixture):
    CHECKS = ["--check", "wired=python3 check_wired.py", "--check", "rebind=python3 check_rebind.py"]

    def test_s3_two_planted_failures_are_printed_and_the_real_tree_is_unchanged(self):
        edits = [{"file": "runner.txt", "old": "wired: test_a\n", "new": "wired: test_a\nbad shape test_b\n"},
                 {"file": "support.py", "old": "def helper():", "new": "def other():\n    pass\ndef helper():"}]
        before = tree_digest(self.root)
        code, out = self.run_tool(edits, *self.CHECKS)
        self.assertEqual(code, 0, out)
        self.assertIn("FAIL wired: runner.txt:2", out)
        self.assertIn("FAIL rebind: test_a.py:1", out)
        self.assertEqual(before, tree_digest(self.root))
        wt = subprocess.run(["git", "worktree", "list"], cwd=self.root, capture_output=True, text=True).stdout
        self.assertEqual(len(wt.splitlines()), 1, wt)

    def test_s3_a_harmless_edit_prints_no_failure(self):
        code, out = self.run_tool([{"file": "clean.txt", "old": "nothing", "new": "something"}], *self.CHECKS)
        self.assertNotIn("FAIL", out)


if __name__ == "__main__":
    unittest.main()
