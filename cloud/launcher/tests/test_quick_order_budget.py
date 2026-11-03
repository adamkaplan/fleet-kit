#!/usr/bin/env python3
"""Ask #1733: the quick preflight orders its checks (by content first, cheapest first), leaves to CI the checks that do not fit
the budget, and says so in its last line. Throwaway repositories under the system temporary directory only; the budget cases
run on a fake clock and no test reads or waits on real time."""
import importlib.machinery
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import unittest

import test_cloud_preflight_quick as Q
import test_quick_left_to_ci as L

ROOT, BIN, TD = Q.ROOT, Q.BIN, Q.TD
SECS = {"test_a_c60.py": 60, "test_m_c20.py": 20, "test_z_c5.py": 5, "test_b_n2.py": 2, "test_y_n1.py": 1}
ORDER = ["test_z_c5.py", "test_m_c20.py", "test_a_c60.py", "test_y_n1.py", "test_b_n2.py"]
LEFT3 = ["test_a_c60.py", "test_y_n1.py", "test_b_n2.py"]


def read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def mutate(text, old, new):
    return text.replace(old, new), old in text


def make(tmp, failing=False):
    """a.txt changed on the branch; three tests read it by content (5, 20, 60 s recorded), two only name it (1, 2 s)."""
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    Q.git(repo, "init", "-q", "-b", "trunk")
    Q.put(repo, "a.txt", "one\n")
    for n in SECS:
        if "_c" in n:
            code = "1 if 'NEW' in open('a.txt').read()" if failing and n == "test_z_c5.py" else "open('a.txt').read() and 0"
            body = "import sys\nsys.exit(%s)\n" % (code + " else 0" if failing and n == "test_z_c5.py" else code)
        else:
            body = "# names a.txt in a comment only\nimport sys\nsys.exit(0)\n"
        Q.put(repo, TD + n, body)
    Q.put(repo, "bin/test-cloud-launcher", "#!/bin/sh\n# " + " ".join("tests/" + n for n in SECS) + "\n")
    Q.put(repo, TD + "quick_seconds.list", "".join("%s%s %d\n" % (TD, n, s) for n, s in sorted(SECS.items())))
    Q.git(repo, "add", ".")
    Q.git(repo, "commit", "-q", "-m", "base")
    Q.git(repo, "checkout", "-q", "-b", "feat")
    Q.put(repo, "a.txt", "one NEW\n")
    Q.git(repo, "add", "-A")
    Q.git(repo, "commit", "-q", "-m", "change")
    return repo


def order_of(out):
    m = re.search(r"^(?:cloud-prepush: )?quick order: (.*)$", out, re.M)
    return [os.path.basename(x) for x in m.group(1).split(", ")] if m else []


def check_order(out):
    if order_of(out) != ORDER:
        raise AssertionError("quick order wrong: %r" % order_of(out))


def check_left3(out):
    last = L.last_line(out)
    if not (last.startswith("left to CI (not run in quick): ") and all(n in last for n in LEFT3)):
        raise AssertionError("left to CI names missing: %r" % last)


def check_failed(rc):
    if rc != 1:
        raise AssertionError("a failing check ended %s" % rc)


def load(path):
    sys.path.insert(0, BIN)
    loader = importlib.machinery.SourceFileLoader("pre_" + str(abs(hash(path))), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quick-order-")
        self.addCleanup(shutil.rmtree, self.tmp, True)


class S3Order(Base):
    def test_s3_the_order_and_the_alphabetical_mutant(self):
        repo = make(self.tmp)
        rc, out = L.run_pf(repo, L.tools(self.tmp), "--quick")
        check_order(out)
        h, applied = mutate(read("bin/cloud_quick.py"), "return sorted(picks, key=key)", "return sorted(picks)")
        self.assertTrue(applied)
        _, out = L.run_pf(repo, L.tools(self.tmp, "m", helper=h), "--quick")
        with self.assertRaisesRegex(AssertionError, "quick order"):
            check_order(out)

    def test_s3_the_prepush_command_uses_the_same_order(self):
        repo = make(self.tmp)
        rc, out, err = L.run_pp(repo, L.tools(self.tmp), CLOUD_PREFLIGHT_QUICK_TOTAL="60")
        self.assertEqual(order_of(err), ORDER, err)


class Fake:
    def __init__(self, fail=()):
        self.now, self.fail, self.started = 0.0, set(fail), []

    def clock(self):
        return self.now

    def run(self, root, rel, tmo, sel=None):
        self.started.append(rel)
        self.now += SECS[rel]
        return 1 if rel in self.fail and os.path.basename(root) == "branch" else 0

    def cut(self, root, rel, tmo, sel=None):
        self.started.append(rel)
        self.now += tmo
        return "timeout"


class FakeClock(Base):
    def go(self, total, fail=(), cut=False):
        m = load(os.path.join(BIN, "cloud-pr-preflight"))
        m.QUICK_TOTAL = total
        for d in ("branch", "default"):
            os.mkdir(os.path.join(self.tmp, d))
            for n in SECS:
                open(os.path.join(self.tmp, d, n), "w").close()
        f = Fake(fail)
        lines, skipped = [], []
        res = m.run_files(ORDER, {}, os.path.join(self.tmp, "branch"), os.path.join(self.tmp, "default"), 100, True,
                          clock=f.clock, run=f.cut if cut else f.run, out=lines.append, secs=SECS, skipped=skipped)
        return m, f, lines, res, skipped

    def test_s4_checks_start_while_they_fit_and_the_rest_is_left(self):
        m, f, lines, (verdicts, failed, not_run), skipped = self.go(30)
        self.assertEqual(f.started, ORDER[:2])
        self.assertEqual(skipped, ORDER[2:])
        self.assertEqual(len(skipped), 3)
        self.assertEqual(f.started.count("test_a_c60.py"), 0)
        self.assertFalse(failed)
        self.assertEqual(not_run, [])

    def test_s4_end_to_end_status_0_and_the_last_line(self):
        repo = make(self.tmp)
        rc, out = L.run_pf(repo, L.tools(self.tmp), "--quick", CLOUD_PREFLIGHT_QUICK_TOTAL="30")
        self.assertEqual(rc, 0, out)
        check_left3(out)
        self.assertIn("RESULT: PASS (quick mode; left to CI: 3)", out)
        for n in ("test_z_c5.py", "test_m_c20.py"):
            self.assertRegex(out, r"(?m)^%s%s\s" % (re.escape(TD), re.escape(n)))
        self.assertNotRegex(out, r"(?m)^%stest_a_c60.py\s" % re.escape(TD))

    def test_s5_budget_spent_while_a_check_runs_is_not_run(self):
        m, f, lines, (verdicts, failed, not_run), skipped = self.go(30, cut=True)
        self.assertEqual(m.BUDGET_STATUS, 3)
        self.assertEqual(not_run[0], ORDER[0])
        self.assertFalse(failed)
        self.assertEqual(skipped, [])

    def test_s5_a_failing_check_names_the_check_and_not_the_budget(self):
        m, f, lines, (verdicts, failed, not_run), skipped = self.go(1000, fail=(ORDER[0],))
        self.assertTrue(failed)
        self.assertEqual([v[0] for v in verdicts if v[3].startswith("FAIL")], [ORDER[0]])
        self.assertEqual(not_run, [])
        self.assertEqual([ln for ln in lines if "budget" in ln], [])


class S6Mutants(Base):
    def test_s6_control_and_the_two_mutants(self):
        repo = make(self.tmp)
        rc, out = L.run_pf(repo, L.tools(self.tmp), "--quick", CLOUD_PREFLIGHT_QUICK_TOTAL="30")
        check_left3(out)
        h, a1 = mutate(read("bin/cloud_quick.py"), 'return "left to CI (not run in quick): " + ', 'return "" + ')
        self.assertTrue(a1)
        _, out = L.run_pf(repo, L.tools(self.tmp, "m", helper=h), "--quick", CLOUD_PREFLIGHT_QUICK_TOTAL="30")
        with self.assertRaisesRegex(AssertionError, "left to CI names missing"):
            check_left3(out)
        tmp2 = tempfile.mkdtemp(prefix="quick-fail-", dir=self.tmp)
        repo2 = make(tmp2, failing=True)
        rc, out = L.run_pf(repo2, L.tools(tmp2), "--quick")
        check_failed(rc)
        self.assertIn("test_z_c5.py", out)
        p, a2 = mutate(read("bin/cloud-pr-preflight"), "failed |= loop_failed", "failed |= False")
        self.assertTrue(a2)
        rc, out = L.run_pf(repo2, L.tools(tmp2, "m", preflight=p), "--quick")
        with self.assertRaisesRegex(AssertionError, "a failing check ended 0"):
            check_failed(rc)


class S7Full(Base):
    def test_s7_the_full_mode_says_nothing_of_left_to_ci_and_a_failure_ends_1(self):
        repo = make(self.tmp, failing=True)
        rc, out = L.run_pf(repo, L.tools(self.tmp))
        self.assertEqual(rc, 1, out)
        self.assertEqual(out.count("left to CI"), 0)
        self.assertEqual(order_of(out), [])


class S10Statuses(Base):
    def test_s10_not_clean_fixtures_keep_their_statuses(self):
        repo = Q.make_shell_reader_repo(self.tmp)
        rc, out = L.run_pf(repo, L.tools(self.tmp), "--quick")
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT RUN: no check that can run in quick reads", out)
        tmp2 = tempfile.mkdtemp(prefix="quick-heavy-", dir=self.tmp)
        repo2 = Q.make_repo(tmp2)
        heavy = re.search(r"HEAVY\s*=\s*[\[\(\{]\s*[\"']([^\"']+)[\"']", read("bin/cloud_quick.py")).group(1)
        Q.put(repo2, heavy, "# reads a.txt\n" + Q.PASS)
        Q.git(repo2, "add", "-A")
        Q.git(repo2, "commit", "-q", "-m", "heavy")
        rc, out = L.run_pf(repo2, L.tools(tmp2), "--quick")
        self.assertEqual(rc, 1, out)
        self.assertIn("skipped: memory-heavy " + heavy, out)


class S8Docs(unittest.TestCase):
    def paragraph(self, text):
        m = re.search(r"<!-- preflight-modes:begin -->(.*?)<!-- preflight-modes:end -->", text, re.S)
        self.assertIsNotNone(m)
        return m.group(1)

    def check(self, para):
        for ph in ("by content", "cheapest first", "left to CI", "NOT RUN (budget)", "status 3", "last line"):
            if ph not in para:
                raise AssertionError("the phrase %r is missing from the preflight-modes paragraph; left to CI" % ph)

    def test_s8_each_document_says_the_order_the_skip_and_the_spent_budget(self):
        n = 0
        for rel in ("cloud/README.md", "docs/cloud-operator-quickstart.md"):
            para = self.paragraph(read(rel))
            self.check(para)
            n += 1
            with self.assertRaisesRegex(AssertionError, "left to CI"):
                self.check(para.replace("left to CI", "elsewhere"))
        self.assertEqual(n, 2)


class S9List(unittest.TestCase):
    def check(self, text):
        bad, seen = [], []
        for line in text.splitlines():
            parts = line.split(" ")
            if len(parts) != 2 or not re.fullmatch(r"[0-9]+", parts[1]) or parts[0] in seen \
                    or not os.path.isfile(os.path.join(ROOT, parts[0])):
                bad.append(line)
            seen.append(parts[0])
        if seen != sorted(seen):
            bad.append("not sorted")
        if bad:
            raise AssertionError("quick_seconds problems: %r" % bad)

    def test_s9_the_real_list_and_two_bad_copies(self):
        real = read("cloud/launcher/tests/quick_seconds.list")
        self.check(real)
        first = real.splitlines()[0]
        with self.assertRaisesRegex(AssertionError, "quick_seconds"):
            self.check(real + "cloud/launcher/tests/test_no_such_file.py 5\n")
        with self.assertRaisesRegex(AssertionError, "quick_seconds"):
            self.check(real.replace(first, first.split(" ")[0] + " -3", 1))


if __name__ == "__main__":
    unittest.main()
