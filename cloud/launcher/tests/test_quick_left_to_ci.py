#!/usr/bin/env python3
"""Ask #1730: the quick preflight leaves checks that cannot run in quick to CI and ends 0 when the checks that ran pass.
Throwaway repositories under the system temporary directory only; every child is started by the standard library's blocking run function and ends before the test goes on."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import test_cloud_preflight_quick as Q

ROOT, BIN, TD, ENV = Q.ROOT, Q.BIN, Q.TD, Q.ENV
LEFT = "left to CI (not run in quick): bin/test-thing"
NOTRUN = "NOT RUN: bin/test-thing cannot run in quick"


def read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def mutate(text, old, new):
    """Returns (mutated text, applied): the mutation is counted, never assumed."""
    return text.replace(old, new), old in text


def make(tmp, failing=False):
    """The fixture of the quick-mode tests: a.txt read by one passing (or failing) test file and by one whole suite."""
    repo = Q.make_repo(tmp, Q.FAIL if failing else Q.PASS)
    Q.put(repo, "bin/test-thing", "#!/bin/sh\n# reads a.txt\nexit 0\n")
    Q.git(repo, "add", "-A")
    Q.git(repo, "commit", "-q", "-m", "suite")
    return repo


def tools(tmp, sub="t", helper=None, preflight=None, prepush=None):
    """A directory holding the preflight, the helper and the pre-push command, each optionally replaced by a mutant."""
    d = os.path.join(tmp, sub)
    os.makedirs(d)
    for name, text in (("cloud-pr-preflight", preflight), ("cloud_quick.py", helper), ("cloud-prepush", prepush)):
        if text is None:
            shutil.copy(os.path.join(BIN, name), d)
        else:
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
    for extra in os.listdir(BIN):
        if extra.startswith("record_json") and extra.endswith(".py"):
            shutil.copy(os.path.join(BIN, extra), d)
    return d


def run_pf(repo, d, *args, **env):
    e = dict(ENV, CLOUD_PREFLIGHT_BASE="trunk", TMPDIR=os.path.dirname(repo), **env)
    p = subprocess.run([sys.executable, os.path.join(d, "cloud-pr-preflight")] + list(args), cwd=repo,
                       capture_output=True, text=True, env=e, timeout=300)
    return p.returncode, p.stdout


def run_pp(repo, d, *args, **env):
    e = dict(ENV, TMPDIR=os.path.dirname(repo), **env)
    p = subprocess.run([sys.executable, os.path.join(d, "cloud-prepush")] + list(args or ("trunk",)), cwd=repo,
                       capture_output=True, text=True, env=e, timeout=300)
    return p.returncode, p.stdout, p.stderr


def run_bg(repo, d, tmp, **env):
    """The background run and its --read."""
    res = os.path.join(tempfile.mkdtemp(prefix="bg-", dir=tmp), "res")
    rc, out, err = run_pp(repo, d, "--background", "trunk", CLOUD_PREPUSH_RESULT=res, **env)
    assert rc == 0, err
    for _ in range(80):
        r = run_pp(repo, d, "--read", os.path.join(res, "result.json"))
        if r[0] != 4:
            return r
        time.sleep(1)
    return r


def last_line(out):
    lines = [l for l in out.splitlines() if l.strip()]
    return lines[-1] if lines else ""


def all_three(self, repo, d, tmp, **env):
    pf = run_pf(repo, d, "--quick", **env)
    pp = run_pp(repo, d, **env)
    bg = run_bg(repo, d, tmp, **env)
    return {"preflight": (pf[0], pf[1]), "prepush": (pp[0], pp[1] + pp[2]), "background": (bg[0], bg[1] + bg[2])}, \
        {"preflight": pf[1], "prepush": pp[1], "background": bg[1]}


def check_left(out_by_name):
    """The anchored checks of S4: the left-to-CI line is the last non-empty line of stdout."""
    for name, out in out_by_name.items():
        if last_line(out) != LEFT:
            raise AssertionError("left to CI line missing in %s: %r" % (name, last_line(out)))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quick-left-")
        self.addCleanup(shutil.rmtree, self.tmp, True)


class S2Passing(Base):
    def test_s2_the_three_runs_end_0_and_say_what_was_left(self):
        repo = make(self.tmp)
        both, outs = all_three(self, repo, tools(self.tmp), self.tmp)
        n = 0
        for name, (rc, out) in both.items():
            with self.subTest(run=name):
                self.assertEqual(rc, 0, out)
                self.assertEqual(out.count(NOTRUN), 1, out)
                self.assertNotIn("the preflight did not pass", out)
                n += 1
        self.assertEqual(n, 3)
        self.assertIn("RESULT: PASS (quick mode; left to CI: 1)", outs["preflight"])
        check_left({"preflight": outs["preflight"], "prepush": outs["prepush"]})
        self.assertIn(LEFT, outs["background"])
        print("S2 runs %d, statuses %s" % (n, [v[0] for v in both.values()]))


class S4Mutant(Base):
    def test_s4_control_and_the_copy_without_the_line(self):
        repo = make(self.tmp)
        _, outs = all_three(self, repo, tools(self.tmp), self.tmp)
        check_left({"preflight": outs["preflight"], "prepush": outs["prepush"]})
        h, applied = mutate(read("bin/cloud_quick.py"),
                            'return "left to CI (not run in quick): " + ', 'return "" + ')
        self.assertTrue(applied)
        _, outs = all_three(self, repo, tools(self.tmp, "m", helper=h), self.tmp)
        with self.assertRaisesRegex(AssertionError, "left to CI line missing"):
            check_left({"preflight": outs["preflight"]})
        with self.assertRaisesRegex(AssertionError, "left to CI line missing"):
            check_left({"prepush": outs["prepush"]})
        print("S4 mutation applied %s" % applied)


def check_failed(results):
    for name, (rc, out) in results.items():
        if rc != 1 or "test_reads_a.py" not in out:
            raise AssertionError("a failing check ended %s in %s" % (rc, name))


class S5Failing(Base):
    def run_two(self, repo, d):
        pf = run_pf(repo, d, "--quick")
        pp = run_pp(repo, d)
        return {"preflight": pf, "prepush": (pp[0], pp[1] + pp[2])}

    def test_s5_failing_check_ends_1_and_the_mutant_is_caught(self):
        repo = make(self.tmp, failing=True)
        check_failed(self.run_two(repo, tools(self.tmp)))
        pre, a1 = mutate(read("bin/cloud-pr-preflight"), "failed |= loop_failed", "failed |= False")
        pp, a2 = mutate(read("bin/cloud-prepush"), "return 1 if diffs else 0", "return 0")
        self.assertTrue(a1 and a2)
        with self.assertRaisesRegex(AssertionError, "a failing check ended 0"):
            check_failed(self.run_two(repo, tools(self.tmp, "m", preflight=pre, prepush=pp)))
        print("S5 mutations applied %s %s" % (a1, a2))


class S6NotClean(Base):
    def test_s6_three_fixtures_stay_not_clean(self):
        # first: only a whole suite reads the change
        repo = Q.make_shell_reader_repo(self.tmp)
        rc, out = run_pf(repo, tools(self.tmp), "--quick")
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT RUN: no check that can run in quick reads bin/tool.txt", out)
        # second: a memory-heavy test is selected
        tmp2 = tempfile.mkdtemp(prefix="quick-heavy-", dir=self.tmp)
        repo2 = Q.make_repo(tmp2)
        spec = read("bin/cloud_quick.py")
        m = re.search(r"HEAVY\s*=\s*[\[\(\{]\s*[\"']([^\"']+)[\"']", spec)
        self.assertIsNotNone(m)
        heavy = m.group(1)
        Q.put(repo2, heavy, "# reads a.txt\n" + Q.PASS)
        Q.put(repo2, "bin/test-cloud-launcher", "#!/bin/sh\n# tests/test_reads_a.py tests/test_other.py tests/test_other2.py\n")
        Q.git(repo2, "add", "-A")
        Q.git(repo2, "commit", "-q", "-m", "heavy")
        rc, out = run_pf(repo2, tools(tmp2), "--quick")
        self.assertEqual(rc, 1, out)
        self.assertIn("skipped: memory-heavy " + heavy, out)
        # third: a spent budget with a reader that cannot run
        tmp3 = tempfile.mkdtemp(prefix="quick-budget-", dir=self.tmp)
        repo3 = make(tmp3)
        Q.put(repo3, TD + "test_slow_a.py", "# reads a.txt\nimport time\ntime.sleep(3)\n")
        Q.put(repo3, "bin/test-cloud-launcher", "#!/bin/sh\n# tests/test_reads_a.py tests/test_other.py tests/test_other2.py tests/test_slow_a.py\n")
        Q.git(repo3, "add", "-A")
        Q.git(repo3, "commit", "-q", "-m", "slow")
        rc, out = run_pf(repo3, tools(tmp3), "--quick", CLOUD_PREFLIGHT_QUICK_TOTAL="1")
        self.assertEqual(rc, 3, out)
        self.assertIn("NOT RUN (budget)", out)
        print("S6 statuses 1 1 3")


class S7Full(Base):
    def test_s7_the_full_mode_is_unchanged(self):
        n = 0
        for failing, want in ((False, 0), (True, 1)):
            tmp = tempfile.mkdtemp(prefix="quick-full-", dir=self.tmp)
            repo = make(tmp, failing)
            d = tools(tmp)
            rc, out = run_pf(repo, d)
            self.assertEqual(rc, want, out)
            self.assertNotIn("left to CI", out)
            rc, out, err = run_pp(repo, d, CLOUD_PREPUSH_PREFLIGHT="full")
            self.assertNotIn("left to CI", out + err)
            n += 1
        self.assertEqual(n, 2)
        print("S7 fixtures %d" % n)


class S8Docs(unittest.TestCase):
    PHRASES = ("leaves to CI the whole `bin/test-*` suites", "`NOT RUN: <test> cannot run in quick`",
               "`left to CI (not run in quick)", "exit status 0 when every check that ran passed")

    @staticmethod
    def paragraph(text):
        for line in text.splitlines():
            if line.startswith("- quick mode, left to CI"):
                return line
        return ""

    def check(self, text):
        para = self.paragraph(text)
        for ph in self.PHRASES:
            if ph not in para:
                raise AssertionError("left to CI: phrase missing: " + ph)
        return len(self.PHRASES)

    def test_s8_both_documents_say_it_and_a_copy_without_it_fails(self):
        n = 0
        for rel in ("cloud/README.md", "docs/cloud-operator-quickstart.md"):
            n += self.check(read(rel))
        self.assertEqual(n, 8)
        text, applied = mutate(read("cloud/README.md"), "a worker sees exit status 0 when every check that ran passed", "a worker sees")
        self.assertTrue(applied)
        with self.assertRaisesRegex(AssertionError, "left to CI"):
            self.check(text)
        print("S8 phrases %d" % n)


class S9Differ(Base):
    def verdicts(self, repo, pre_dir, pp_dir):
        a = run_pf(repo, pre_dir, "--quick")[0]
        b = run_pp(repo, pp_dir)[0]
        if a != b:
            raise AssertionError("the two commands decide differently: %s and %s" % (a, b))
        return a

    def test_s9_a_second_copy_of_the_rule_is_caught(self):
        repo = make(self.tmp)
        good = tools(self.tmp)
        self.assertEqual(self.verdicts(repo, good, good), 0)
        pre, applied = mutate(read("bin/cloud-pr-preflight"), "failed |= bool(cannot) and not left", "failed |= bool(cannot)")
        self.assertTrue(applied)
        bad = tools(self.tmp, "m", preflight=pre)
        with self.assertRaisesRegex(AssertionError, "the two commands decide differently"):
            self.verdicts(repo, bad, good)
        print("S9 mutation applied %s" % applied)


class S10Scope(unittest.TestCase):
    FILES = {"bin/cloud-pr-preflight", "bin/cloud-prepush", "bin/cloud_quick.py", "cloud/README.md",
             "docs/cloud-operator-quickstart.md", "cloud/launcher/tests/test_quick_left_to_ci.py",
             "bin/test-cloud-launcher.chain", "cloud/launcher/tests/chain_unmeasured.list",
             "cloud/launcher/tests/test_cloud_preflight_quick.py", "cloud/launcher/tests/test_cloud_quick_scans_run.py"}

    def git(self, *a):
        return subprocess.run(["git"] + list(a), cwd=ROOT, capture_output=True, text=True, env=ENV)

    def test_s10_the_diff_is_the_listed_files(self):
        base = None
        for ref in ("origin/integration", "integration"):
            p = self.git("merge-base", ref, "HEAD")
            if p.returncode == 0:
                base = p.stdout.strip()
                break
        if base is None:
            self.skipTest("no integration branch in this checkout")
        names = set(self.git("diff", "--name-only", base, "HEAD").stdout.split())
        if "cloud/launcher/tests/test_quick_left_to_ci.py" not in names:
            self.skipTest("the change is not committed here")
        self.assertEqual(names - self.FILES, set())
        removed = 0
        for f in names:
            if "/tests/" in f and f.endswith(".py"):
                for l in self.git("diff", "-U0", base, "HEAD", "--", f).stdout.splitlines():
                    if l.startswith("-") and not l.startswith("---"):
                        removed += 1
        self.assertEqual(removed, 2)
        print("S10 files %d removed test lines %d" % (len(names), removed))


if __name__ == "__main__":
    unittest.main()
