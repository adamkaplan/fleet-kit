#!/usr/bin/env python3
"""Scenario S8 of #1709: the self-registering test isolates git from the user's configuration.

The one test of tests/switchboard/test_cases_12.py that makes a throwaway repository runs in a CHILD process, in copies of the tree outside the
repository, with a hostile global git configuration (a global pre-commit hook that exits 1) in the child's HOME. The real test passes; a copy
with its isolation removed fails with the hook's text; the same mutant without the hostile configuration passes (a control)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
TEST_NAME = "test_two_branches_that_add_different_commands_touch_no_common_file_and_merge_cleanly"
ISOLATION = 'HOME=work, XDG_CONFIG_HOME=work, GIT_CONFIG_NOSYSTEM="1")'
HOOK_TEXT = "HOSTILE-GLOBAL-HOOK-REFUSES"
RESULTS = {}


def copy_tree(dest):
    ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".mypy_cache", ".pytest_cache")
    for part in ("bin", "scenarios", "tests", "docs", "templates", "skills", "agents", "plugins", "shims", "herdr", "reviews"):
        if (ROOT / part).exists():
            shutil.copytree(ROOT / part, dest / part, ignore=ignore, symlinks=True)
    for top in ROOT.iterdir():
        if top.is_file():
            shutil.copy2(top, dest / top.name)
    os.chmod(dest, 0o700)


def make_home(base, hostile):
    home = base / ("home-hostile" if hostile else "home-empty")
    home.mkdir(mode=0o700)
    assert home.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()), "the hostile file is written only into a temporary home"
    if hostile:
        hooks = home / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\necho %s >&2\nexit 1\n" % HOOK_TEXT)
        hook.chmod(0o755)
        (home / ".gitconfig").write_text("[core]\n\thooksPath = %s\n" % hooks)
    return home


def run_child(tree, home):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home))
    done = subprocess.run([sys.executable, "bin/test-switchboard", "-k", TEST_NAME], cwd=str(tree), env=env,
                          capture_output=True, text=True, timeout=90)
    return done.returncode, done.stdout + done.stderr


class S8Isolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="s8-iso-"))
        cls.addClassCleanup(shutil.rmtree, str(cls.base), True)
        cls.real = cls.base / "real"
        cls.mutant = cls.base / "mutant"
        cls.real.mkdir()
        cls.mutant.mkdir()
        copy_tree(cls.real)
        copy_tree(cls.mutant)
        path = cls.mutant / "tests" / "switchboard" / "test_cases_12.py"
        text = path.read_text()
        cls.applied = text.count(ISOLATION) == 1
        path.write_text(text.replace(ISOLATION, ")") if cls.applied else text)
        cls.hostile = make_home(cls.base, True)
        cls.empty = make_home(cls.base, False)

    def test_mutation_applied(self):
        self.assertTrue(self.applied, "the pinned isolation text must occur exactly once in the copy")
        self.assertNotIn(ISOLATION, (self.mutant / "tests" / "switchboard" / "test_cases_12.py").read_text())
        self.assertIn(ISOLATION, (self.real / "tests" / "switchboard" / "test_cases_12.py").read_text())

    def test_real_passes_hostile(self):
        code, out = run_child(self.real, self.hostile)
        RESULTS["real"] = int(code == 0)
        self.assertEqual(code, 0, out[-1500:])
        self.assertIn("1 run, 1 passed", out)

    def test_mutant_fails_hostile_with_hook_text(self):
        code, out = run_child(self.mutant, self.hostile)
        RESULTS["mutant"] = int(code != 0 and HOOK_TEXT in out)
        self.assertNotEqual(code, 0, out[-1500:])
        self.assertIn(HOOK_TEXT, out)
        self.assertIn("commit", out)

    def test_mutant_passes_without_hostile_configuration(self):
        code, out = run_child(self.mutant, self.empty)
        RESULTS["control"] = int(code == 0)
        self.assertEqual(code, 0, out[-1500:])
        self.assertIn("1 run, 1 passed", out)

    @classmethod
    def tearDownClass(cls):
        if len(RESULTS) == 3:
            line = ("S8: real test passes with the hostile configuration %d of 1, mutant without the isolation fails %d of 1 with the hook text, "
                    "control without the hostile configuration passes %d of 1, mutation applied %s"
                    % (RESULTS["real"], RESULTS["mutant"], RESULTS["control"], cls.applied))
            print(line)


if __name__ == "__main__":
    unittest.main()
