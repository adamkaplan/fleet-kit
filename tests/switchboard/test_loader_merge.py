"""Two branches that each add a test file merge without conflict (#2206, S2): the loader of bin/test-switchboard self-registers test classes.

Today's form (one loader line per test file, added at the end of the loader lines) conflicts when two branches each add one; the new form (no loader line) does not.
Both are tried in a scratch git repository outside the working tree with `git merge-tree --write-tree`. Standard library only.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[2] / "bin" / "test-switchboard"
ANCHOR = "def main(argv):"
PLANTED = "import unittest\n\n\nclass %s(unittest.TestCase):\n    def test_a(self):\n        pass\n"


def git(cwd, *args, check=True):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=check, capture_output=True, text=True)


def conflicts(out):
    return len([l for l in out.splitlines() if l.startswith("CONFLICT")])


class TestTwoBranchesAddingATestFile(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="loader-merge-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.text = RUNNER.read_text()
        self.assertEqual(self.text.count(ANCHOR), 1)

    def merge(self, base_text, edit):
        """CONFLICT count and the merged file names of two branches made by edit(branch_name, runner_text) -> runner_text."""
        git(self.repo, "init", "-q", "-b", "base")
        (self.repo / "bin").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "bin" / "test-switchboard").write_text(base_text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        for name in ("a", "b"):
            git(self.repo, "checkout", "-q", "-b", name, "base")
            (self.repo / "tests").mkdir(exist_ok=True)
            (self.repo / "tests" / ("test_new_%s.py" % name)).write_text(PLANTED % ("TestNew" + name.upper()))
            (self.repo / "bin" / "test-switchboard").write_text(edit(name, base_text))
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-qm", "add " + name)
        res = git(self.repo, "merge-tree", "--write-tree", "--name-only", "a", "b", check=False)
        tree = res.stdout.splitlines()[0]
        files = git(self.repo, "ls-tree", "-r", "--name-only", tree).stdout.split() if res.returncode == 0 else []
        return conflicts(res.stdout), files

    def test_s2_the_runner_discovers_so_two_added_test_files_do_not_conflict(self):
        self.assertIn("bind_discovered(", self.text)
        count, files = self.merge(self.text, lambda name, text: text)
        self.assertEqual(count, 0)
        self.assertIn("tests/test_new_a.py", files)
        self.assertIn("tests/test_new_b.py", files)

    def test_s2_today_s_form_one_loader_line_per_file_conflicts(self):
        old = "\n".join(l for l in self.text.splitlines() if "bind_discovered(" not in l) + "\n"
        line = 'TestNew%s = importlib.import_module("test_new_%s").TestNew%s\n\n\n'
        edit = lambda name, text: text.replace(ANCHOR, line % (name.upper(), name, name.upper()) + ANCHOR)
        count, _ = self.merge(old, edit)
        self.assertGreaterEqual(count, 1)
