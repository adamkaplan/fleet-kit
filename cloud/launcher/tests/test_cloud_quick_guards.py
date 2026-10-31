#!/usr/bin/env python3
"""Offline tests of the quick-mode guards (ask #1299 findings): a change no check reads is never clean, one heavy list,
a missing helper fails loudly, and the documented scan table equals SCAN_TESTS. Temp repositories under the system temp dir."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
BIN = os.path.join(ROOT, "bin")
ENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", PYTHONDONTWRITEBYTECODE="1")
PASS = "import sys\nsys.exit(0)\n"


def load_quick(path=None):
    spec = importlib.util.spec_from_file_location("cq_under_test", path or os.path.join(BIN, "cloud_quick.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


REAL = load_quick()


def git(repo, *a):
    subprocess.run(["git"] + list(a), cwd=repo, check=True, capture_output=True, env=ENV)


def put(repo, rel, text):
    p = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def make_repo(tmp, changed, heavy_reader=False):
    """Base holds a stub for every REAL scan file; the branch changes one file that no test names."""
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "trunk")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.invalid")
    for rel, _ in REAL.SCAN_TESTS:
        put(repo, rel, PASS)
    put(repo, "bin/test-cloud-launcher", "#!/bin/sh\n# tests/test_x.py\n")
    put(repo, changed, "one\n")
    if heavy_reader:
        put(repo, REAL.HEAVY[0], "# names a.txt\n" + PASS)
        put(repo, "a.txt", "one\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feat")
    put(repo, changed, "two\n")
    if heavy_reader:
        put(repo, "a.txt", "two\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "change")
    return repo


def tool_dir(tmp, helper_text=None, mutate=None, with_helper=True):
    d = os.path.join(tmp, "tool")
    os.makedirs(d, exist_ok=True)
    for n in ("cloud-pr-preflight", "cloud-prepush"):
        with open(os.path.join(BIN, n)) as f:
            text = f.read()
        if mutate and n == mutate[0]:
            new = text.replace(mutate[1], mutate[2])
            assert new != text, "mutation did not apply"
            text = new
        with open(os.path.join(d, n), "w") as f:
            f.write(text)
    if with_helper:
        with open(os.path.join(BIN, "cloud_quick.py")) as f:
            text = f.read()
        if helper_text:
            new = text.replace(helper_text[0], helper_text[1])
            assert new != text, "mutation did not apply"
            text = new
        with open(os.path.join(d, "cloud_quick.py"), "w") as f:
            f.write(text)
    return d


def run(repo, tool, name, *args):
    env = dict(ENV, CLOUD_PREFLIGHT_BASE="trunk", TMPDIR=os.path.dirname(repo))
    p = subprocess.run([sys.executable, os.path.join(tool, name)] + list(args), cwd=repo, capture_output=True, text=True,
                       env=env, timeout=250)
    return p.returncode, p.stdout, p.stderr


class Guards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quick-guard-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_h1_preflight_unread_change_is_not_clean_for_the_real_scan_list(self):
        for changed in ("bin/widget-tool", "cloud/worker/scan-embedded-roots.py"):
            with self.subTest(changed=changed):
                tmp = tempfile.mkdtemp(dir=self.tmp)
                repo = make_repo(tmp, changed)
                rc, out, _ = run(repo, tool_dir(tmp), "cloud-pr-preflight", "--quick")
                self.assertEqual(rc, 1, out)
                self.assertIn("NOT RUN: no check reads " + changed, out)
                self.assertNotIn("RESULT: PASS", out)

    def test_h1_prepush_unread_change_is_not_clean(self):
        repo = make_repo(self.tmp, "bin/widget-tool")
        rc, out, _ = run(repo, tool_dir(self.tmp), "cloud-prepush", "trunk")
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT RUN: no check reads bin/widget-tool", out)

    def test_h1_mutant_deciding_after_the_scans_are_added_is_caught(self):
        repo = make_repo(self.tmp, "bin/widget-tool")
        tool = tool_dir(self.tmp, mutate=("cloud-pr-preflight", "if changed_files and not reads:", "if changed_files and not names:"))
        rc, out, _ = run(repo, tool, "cloud-pr-preflight", "--quick")
        self.assertEqual(rc, 0, "the mutant must be clean, which the real guard refuses")

    def test_m1_heavy_is_one_list_and_skipped_by_the_preflight(self):
        self.assertEqual(REAL.HEAVY, ("cloud/worker/tests/test_root_scanner.py",))
        repo = make_repo(self.tmp, "a.txt", heavy_reader=True)
        rc, out, _ = run(repo, tool_dir(self.tmp), "cloud-pr-preflight", "--quick")
        self.assertEqual(rc, 1, out)
        self.assertIn("skipped: memory-heavy " + REAL.HEAVY[0], out)

    def test_m1_mutant_with_the_list_emptied_runs_it(self):
        repo = make_repo(self.tmp, "a.txt", heavy_reader=True)
        tool = tool_dir(self.tmp, helper_text=('HEAVY = ("cloud/worker/tests/test_root_scanner.py",)', "HEAVY = ()"))
        rc, out, _ = run(repo, tool, "cloud-pr-preflight", "--quick")
        self.assertNotIn("skipped: memory-heavy", out)

    def test_m2_the_mode_line_says_basename_match_and_the_copies_are_gone(self):
        repo = make_repo(self.tmp, "bin/widget-tool")
        _, out, _ = run(repo, tool_dir(self.tmp), "cloud-pr-preflight", "--quick")
        self.assertIn("checks that read the 1 files (basename match)", out)
        with open(os.path.join(BIN, "cloud-pr-preflight")) as f:
            self.assertNotIn("def select(", f.read())

    def test_m3_a_missing_helper_fails_loudly(self):
        repo = make_repo(self.tmp, "bin/widget-tool")
        rc, out, err = run(repo, tool_dir(self.tmp, with_helper=False), "cloud-pr-preflight", "--quick")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("cloud_quick helper missing", err)
        self.assertNotIn("usage", out)


class Docs(unittest.TestCase):
    def test_h2_the_documented_quick_list_equals_scan_tests_and_each_resolves(self):
        with open(os.path.join(ROOT, "cloud/README.md")) as f:
            text = f.read()
        m = re.search(r"<!-- scan-table:begin -->(.*?)<!-- scan-table:end -->", text, re.S)
        self.assertTrue(m, "scan table missing")
        rows = re.findall(r"^\| `([^`]+)` \| (yes|no) \|", m.group(1), re.M)
        documented = sorted(r for r, q in rows if q == "yes")
        listed = sorted(a if not b else a + "::" + b for a, b in REAL.SCAN_TESTS)
        self.assertEqual(documented, listed)
        self.assertTrue(any(q == "no" for _, q in rows))
        for pair in REAL.SCAN_TESTS:
            self.assertTrue(REAL.selector_resolves(ROOT, pair), pair)


if __name__ == "__main__":
    unittest.main()
