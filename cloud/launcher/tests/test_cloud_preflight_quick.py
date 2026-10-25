#!/usr/bin/env python3
"""Offline tests of the quick mode of bin/cloud-pr-preflight, its shared helper bin/cloud_quick.py and the default of
bin/cloud-prepush (ask #1299). Throwaway repositories under the system temporary directory only."""
import importlib.machinery
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
TD = "cloud/launcher/tests/"
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_NOSYSTEM="1")
for _k in ("CLOUD_PREFLIGHT_MODE", "CLOUD_PREPUSH_PREFLIGHT"):
    ENV.pop(_k, None)
PASS = "import sys\nsys.exit(0)\n"
FAIL = "import sys\nsys.exit(1)\n"
SECTIONS = ("== 1. completeness scan", "== 2.", "== 3. existing test files")


def git(cwd, *a):
    p = subprocess.run(["git"] + list(a), cwd=cwd, capture_output=True, text=True, env=ENV)
    assert p.returncode == 0, (a, p.stderr)


def put(repo, rel, text):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def make_repo(tmp, branch_test=PASS):
    """Base: a.txt, test_reads_a (names a.txt), test_other, test_other2 (name nothing). Branch: edits a.txt."""
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "trunk")
    put(repo, "a.txt", "one\n")
    put(repo, TD + "test_reads_a.py", "# reads a.txt\n" + branch_test.replace("1", "0"))
    put(repo, TD + "test_other.py", PASS)
    put(repo, TD + "test_other2.py", PASS)
    put(repo, "bin/test-cloud-launcher", "#!/bin/sh\n# tests/test_reads_a.py tests/test_other.py tests/test_other2.py\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feat")
    put(repo, "a.txt", "one NEW\n")
    if branch_test == FAIL:
        put(repo, TD + "test_reads_a.py", "# reads a.txt\n" + FAIL)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    return repo


def tool_dir(tmp, helper_text=None):
    d = os.path.join(tmp, "tool")
    os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(BIN, "cloud-pr-preflight"), d)
    if helper_text is None:
        shutil.copy(os.path.join(BIN, "cloud_quick.py"), d)
    else:
        with open(os.path.join(d, "cloud_quick.py"), "w") as f:
            f.write(helper_text)
    return os.path.join(d, "cloud-pr-preflight")


def run_pf(repo, script, *args):
    env = dict(ENV, CLOUD_PREFLIGHT_BASE="trunk", TMPDIR=os.path.dirname(repo))
    p = subprocess.run([sys.executable, script] + list(args), cwd=repo, capture_output=True, text=True, env=env, timeout=300)
    return p.returncode, p.stdout


def checks(out):
    """The test files run in section 2 (rows of the table)."""
    rows = re.findall(r"^(\S+\.py\S*)\s+\S+\s+\S+\s+", out, re.M)
    return [r for r in rows if r != "file"]


def assert_quick_is_smaller(case, repo, script):
    _, full = run_pf(repo, script)
    _, quick = run_pf(repo, script, "--quick")
    case.assertLess(len(checks(quick)), len(checks(full)), quick)
    case.assertIn(TD + "test_reads_a.py", checks(quick))


class QuickMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quick-pf-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_s2_quick_prints_the_same_three_sections_and_fewer_checks(self):
        repo = make_repo(self.tmp)
        script = tool_dir(self.tmp)
        rc, quick = run_pf(repo, script, "--quick")
        self.assertEqual(rc, 0, quick)
        for s in SECTIONS:
            self.assertIn(s, quick)
        self.assertEqual(checks(quick), [TD + "test_reads_a.py"])
        self.assertIn("RESULT: PASS", quick)

    def test_s3_exit_statuses_equal_those_of_the_full_mode(self):
        for kind, want in ((PASS, 0), (FAIL, 1)):
            with self.subTest(kind=want):
                tmp = tempfile.mkdtemp(prefix="quick-pf-", dir=self.tmp)
                repo = make_repo(tmp, kind)
                script = tool_dir(tmp)
                self.assertEqual(run_pf(repo, script)[0], want)
                self.assertEqual(run_pf(repo, script, "--quick")[0], want)

    def test_s4_u2_the_default_mode_selects_every_test_file(self):
        repo = make_repo(self.tmp)
        rc, full = run_pf(repo, tool_dir(self.tmp))
        self.assertEqual(rc, 0, full)
        self.assertEqual(checks(full), [TD + "test_other.py", TD + "test_other2.py", TD + "test_reads_a.py"])
        self.assertIn("== 2. every test file, one at a time", full)
        self.assertNotIn("quick mode", full)

    def test_s5_one_list_pairs_resolving_and_equal_to_the_prepush_list(self):
        spec = importlib.util.spec_from_file_location("cloud_quick_x", os.path.join(BIN, "cloud_quick.py"))
        q = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(q)
        loader = importlib.machinery.SourceFileLoader("cloud_prepush_x", os.path.join(BIN, "cloud-prepush"))
        sp = importlib.util.spec_from_loader("cloud_prepush_x", loader)
        pp = importlib.util.module_from_spec(sp)
        sys.path.insert(0, BIN)
        try:
            loader.exec_module(pp)
        finally:
            sys.path.remove(BIN)
        self.assertEqual(list(pp.SCAN_TESTS), list(q.SCAN_TESTS))
        self.assertTrue(q.SCAN_TESTS)
        for pair in q.SCAN_TESTS:
            self.assertEqual(len(pair), 2)
            self.assertNotEqual(pair[0], "bin/test-switchboard" if not pair[1] else None)
            if not pair[1]:  # only a whole script that takes no selector may omit it
                self.assertFalse(pair[0].endswith(".py"), pair)
            self.assertTrue(q.selector_resolves(ROOT, pair), pair)
        self.assertFalse(q.selector_resolves(ROOT, (q.SCAN_TESTS[0][0], "TestNoSuchClassAnywhere")))
        # the preflight imports the same module, not a copy of the list
        with open(os.path.join(BIN, "cloud-pr-preflight")) as f:
            text = f.read()
        self.assertIn("cloud_quick.SCAN_TESTS", text)
        self.assertNotIn("SCAN_TESTS = ", text)

    def test_s8_a_small_change_selects_fewer_checks_in_quick_than_in_full(self):
        repo = make_repo(self.tmp)
        assert_quick_is_smaller(self, repo, tool_dir(self.tmp))

    def test_s9_a_non_empty_change_with_nothing_selected_is_not_clean(self):
        repo = make_repo(self.tmp)
        git(repo, "checkout", "-q", "trunk")
        git(repo, "checkout", "-q", "-b", "feat2")
        put(repo, "c.txt", "unread\n")
        git(repo, "add", "c.txt")
        git(repo, "commit", "-q", "-m", "unread")
        with open(os.path.join(BIN, "cloud_quick.py")) as f:
            text = f.read()
        empty = re.sub(r"SCAN_TESTS = \[.*?\n\]\n", "SCAN_TESTS = []\n", text, flags=re.S)
        self.assertNotEqual(empty, text)
        script = tool_dir(self.tmp, empty)
        rc, out = run_pf(repo, script, "--quick")
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT RUN: quick mode selected no check", out)
        self.assertNotIn("RESULT: PASS", out)

    def test_s10_a_mutant_that_runs_the_full_set_under_quick_is_refused(self):
        repo = make_repo(self.tmp)
        with open(os.path.join(BIN, "cloud_quick.py")) as f:
            text = f.read()
        mutant = text.replace('picked = [(r, None) for r in readers(root, tracked, changed)]',
                              'picked = [(r, None) for r in readers(root, tracked, changed or set(), lambda rel: True)]')
        mutant = mutant.replace("names = {os.path.basename(c) for c in changed}", "names = {''}")
        self.assertTrue(mutant != text)
        script = tool_dir(self.tmp, mutant)
        with self.assertRaises(AssertionError):
            assert_quick_is_smaller(self, repo, script)
        # and the genuine helper passes the same check
        assert_quick_is_smaller(self, repo, tool_dir(self.tmp))


class PrepushDefault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quick-pp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_pp(self, **env):
        self.tmp = tempfile.mkdtemp(prefix="quick-pp-", dir=self.tmp)
        repo = make_repo(self.tmp)
        d = os.path.join(self.tmp, "pp")
        os.makedirs(d)
        shutil.copy(os.path.join(BIN, "cloud-prepush"), d)
        put(d, "cloud-pr-preflight", "import os, sys\nprint('MODE=%s ARGS=%s' % (os.environ.get('CLOUD_PREFLIGHT_MODE'), sys.argv[1:]))\n"
                                      "sys.exit(1)\n")
        e = dict(ENV, TMPDIR=self.tmp, **env)
        p = subprocess.run([sys.executable, os.path.join(d, "cloud-prepush"), "trunk"], cwd=repo, capture_output=True,
                           text=True, env=e, timeout=120)
        return p

    def test_s6_default_is_quick_and_says_so(self):
        p = self.run_pp()
        self.assertIn("preflight mode: quick", p.stderr)
        self.assertIn("MODE=quick", p.stdout)

    def test_s7_full_asks_for_no_quick_and_a_bad_value_is_refused(self):
        p = self.run_pp(CLOUD_PREPUSH_PREFLIGHT="full")
        self.assertIn("preflight mode: full", p.stderr)
        self.assertIn("MODE=None ARGS=[]", p.stdout)
        p = self.run_pp(CLOUD_PREPUSH_PREFLIGHT="fast")
        self.assertEqual(p.returncode, 2, p.stderr)


def make_shell_reader_repo(tmp):
    """Base: bin/tool.txt and a shell suite bin/test-thing that names it. Branch: edits bin/tool.txt."""
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "trunk")
    put(repo, "bin/tool.txt", "one\n")
    put(repo, "bin/test-thing", "#!/bin/sh\n# reads tool.txt\nexit 0\n")
    put(repo, TD + "test_other.py", PASS)
    put(repo, "bin/test-cloud-launcher", "#!/bin/sh\n# tests/test_other.py\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feat")
    put(repo, "bin/tool.txt", "two\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    return repo


def both_tools(tmp, preflight_text=None):
    """One directory holding the real preflight (or a mutant of it), the helper and the pre-push command."""
    script = tool_dir(tmp)
    d = os.path.dirname(script)
    shutil.copy(os.path.join(BIN, "cloud-prepush"), d)
    if preflight_text is not None:
        with open(script, "w") as f:
            f.write(preflight_text)
    return script, os.path.join(d, "cloud-prepush")


def run_both(repo, script, prepush, tmp, **env):
    rc1, out1 = run_pf(repo, script, "--quick")
    e = dict(ENV, TMPDIR=tmp, **env)
    p = subprocess.run([sys.executable, prepush, "trunk"], cwd=repo, capture_output=True, text=True, env=e, timeout=300)
    return (rc1, out1), (p.returncode, p.stdout + p.stderr)


def not_run_lines(out):
    return sorted(l for l in out.splitlines() if l.startswith("NOT RUN: no check") or l.endswith("cannot run in quick"))


class OneReaderDecision(unittest.TestCase):
    """The preflight and the pre-push command ask the same function what a reader is and what no reader means."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quick-one-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_m1_the_same_change_gives_the_same_verdict_and_reason_in_both(self):
        repo = make_shell_reader_repo(self.tmp)
        script, prepush = both_tools(self.tmp)
        (rc1, out1), (rc2, out2) = run_both(repo, script, prepush, self.tmp)
        self.assertEqual((rc1, rc2), (1, 1), out1 + out2)
        self.assertEqual(not_run_lines(out1), not_run_lines(out2))
        self.assertIn("NOT RUN: no check that can run in quick reads bin/tool.txt", out1)
        self.assertIn("NOT RUN: bin/test-thing cannot run in quick", out1)
        self.assertNotIn("the preflight did not pass", out2)
        self.assertNotIn("RESULT: PASS", out1)

    def test_m1_a_change_nothing_reads_gives_the_same_reason_in_both(self):
        repo = make_shell_reader_repo(self.tmp)
        git(repo, "checkout", "-q", "trunk")
        git(repo, "checkout", "-q", "-b", "feat2")
        put(repo, "c.txt", "unread\n")
        git(repo, "add", "c.txt")
        git(repo, "commit", "-q", "-m", "unread")
        script, prepush = both_tools(self.tmp)
        (rc1, out1), (rc2, out2) = run_both(repo, script, prepush, self.tmp)
        self.assertEqual((rc1, rc2), (1, 1))
        self.assertEqual(not_run_lines(out1), not_run_lines(out2))
        self.assertIn("NOT RUN: no check reads c.txt", out1)

    def test_m1_mutant_a_separate_reader_rule_in_the_preflight_is_caught(self):
        repo = make_shell_reader_repo(self.tmp)
        with open(os.path.join(BIN, "cloud-pr-preflight")) as f:
            text = f.read()
        mutant = text.replace("every = cloud_quick.readers(root, tracked, changed_files)",
                              "every = cloud_quick.readers(root, tracked, changed_files, lambda r: r in set(tests_in(root)))")
        self.assertTrue(mutant != text)
        script, prepush = both_tools(self.tmp, mutant)
        (rc1, out1), (rc2, out2) = run_both(repo, script, prepush, self.tmp)
        self.assertNotEqual(not_run_lines(out1), not_run_lines(out2))

    def test_m2_the_bound_says_how_many_files_were_selected_and_how_many_ran(self):
        repo = make_repo(self.tmp)
        put(repo, TD + "test_slow_a.py", "# reads a.txt\nimport time\ntime.sleep(3)\n")
        put(repo, TD + "test_slow_b.py", "# reads a.txt\nimport time\ntime.sleep(3)\n")
        put(repo, "bin/test-cloud-launcher", "#!/bin/sh\n# tests/test_reads_a.py tests/test_other.py tests/test_other2.py tests/test_slow_a.py tests/test_slow_b.py\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "slow")
        env = dict(ENV, CLOUD_PREFLIGHT_BASE="trunk", TMPDIR=self.tmp, CLOUD_PREFLIGHT_QUICK_TOTAL="1")
        p = subprocess.run([sys.executable, tool_dir(self.tmp), "--quick"], cwd=repo, capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertRegex(p.stdout, r"NOT RUN: quick run exceeded 1 s \(selected 3 files, ran 2; stopped before ")

    def test_m3_a_bad_quick_total_is_refused_in_one_line_and_only_in_quick(self):
        repo = make_repo(self.tmp)
        script = tool_dir(self.tmp)
        for bad in ("abc", "0", "-5", "", "1.5"):
            with self.subTest(value=bad):
                env = dict(ENV, CLOUD_PREFLIGHT_BASE="trunk", TMPDIR=self.tmp, CLOUD_PREFLIGHT_QUICK_TOTAL=bad)
                p = subprocess.run([sys.executable, script, "--quick"], cwd=repo, capture_output=True, text=True, env=env, timeout=120)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertEqual(len((p.stdout + p.stderr).strip().splitlines()), 1)
                self.assertIn("CLOUD_PREFLIGHT_QUICK_TOTAL must be a whole number of seconds (1 or more)", p.stdout)
                self.assertNotIn("Traceback", p.stderr)
                full = subprocess.run([sys.executable, script], cwd=repo, capture_output=True, text=True, env=env, timeout=120)
                self.assertEqual(full.returncode, 0, full.stdout + full.stderr)

    def test_m3_mutant_a_bare_int_at_import_is_caught(self):
        repo = make_repo(self.tmp)
        with open(os.path.join(BIN, "cloud-pr-preflight")) as f:
            text = f.read()
        mutant = text.replace("QUICK_TOTAL = QUICK_TOTAL_DEFAULT\n", 'QUICK_TOTAL = int(os.environ.get("CLOUD_PREFLIGHT_QUICK_TOTAL", "240"))\n')
        self.assertTrue(mutant != text)
        script = tool_dir(self.tmp)
        with open(script, "w") as f:
            f.write(mutant)
        env = dict(ENV, CLOUD_PREFLIGHT_BASE="trunk", TMPDIR=self.tmp, CLOUD_PREFLIGHT_QUICK_TOTAL="abc")
        p = subprocess.run([sys.executable, script], cwd=repo, capture_output=True, text=True, env=env, timeout=120)
        self.assertNotEqual(p.returncode, 0)  # the mutant breaks the full mode: the test above would fail on it


if __name__ == "__main__":
    unittest.main()
