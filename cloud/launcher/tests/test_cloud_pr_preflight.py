#!/usr/bin/env python3
"""Tests for bin/cloud-pr-preflight (ask #1195): temporary repositories, each check passing and failing, and a mutant per check."""
import os, subprocess, sys, tempfile, shutil, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SCRIPT = os.path.join(ROOT, "bin", "cloud-pr-preflight")
PASS = "import sys\nsys.exit(0)\n"
FAIL = "import sys\nsys.exit(1)\n"
CHAIN = '#!/bin/sh\nEXCEPTIONS=""\n# tests/test_a.py tests/test_b.py\n'


def sh(cwd, *a):
    subprocess.run(a, cwd=cwd, check=True, capture_output=True)


class Repo:
    def __init__(self):
        self.d = tempfile.mkdtemp(prefix="pf-test-")
        sh(self.d, "git", "init", "-q", "-b", "main")
        sh(self.d, "git", "config", "user.email", "t@example.com")
        sh(self.d, "git", "config", "user.name", "t")
        os.makedirs(os.path.join(self.d, "bin"))
        os.makedirs(os.path.join(self.d, "cloud/launcher/tests"))
        os.makedirs(os.path.join(self.d, "cloud/worker/tests"))
        self.put("bin/test-cloud-launcher", CHAIN)
        self.put("cloud/launcher/tests/test_a.py", PASS)
        self.put("cloud/worker/tests/test_b.py", PASS)
        self.commit("base")
        sh(self.d, "git", "checkout", "-q", "-b", "feat")

    def put(self, rel, text):
        p = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def commit(self, m):
        sh(self.d, "git", "add", "-A")
        sh(self.d, "git", "commit", "-q", "-m", m)

    def run(self, script=SCRIPT, **env):
        e = dict(os.environ, CLOUD_PREFLIGHT_TIMEOUT="5", CLOUD_PREFLIGHT_BASE="main")
        e.update(env)
        p = subprocess.run([sys.executable, script], cwd=self.d, env=e, capture_output=True, text=True)
        return p.returncode, p.stdout

    def status(self):
        return subprocess.run(["git", "status", "--porcelain"], cwd=self.d, capture_output=True, text=True).stdout

    def close(self):
        shutil.rmtree(self.d, ignore_errors=True)


def mutant(old, new):
    src = open(SCRIPT).read()
    assert old in src, old
    fd, path = tempfile.mkstemp(suffix=".py")
    os.write(fd, src.replace(old, new).encode()); os.close(fd)
    return path


class T(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.close)

    def test_clean_passes_and_writes_nothing(self):
        before = set(os.listdir(tempfile.gettempdir()))
        rc, out = self.r.run()
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: PASS", out)
        self.assertEqual(self.r.status(), "")
        self.assertEqual([x for x in set(os.listdir(tempfile.gettempdir())) - before if x.startswith("cloud-preflight-")], [])

    def test_unchained_new_file_named(self):
        self.r.put("cloud/launcher/tests/test_new.py", PASS); self.r.commit("new")
        rc, out = self.r.run()
        self.assertNotEqual(rc, 0)
        self.assertIn("NOT RUN: cloud/launcher/tests/test_new.py", out)

    def test_exception_listed_file_ok(self):
        self.r.put("bin/test-cloud-launcher", CHAIN.replace('""', '"test_new.py:slow"'))
        self.r.put("cloud/worker/tests/test_new.py", PASS); self.r.commit("new")
        self.assertEqual(self.r.run()[0], 0)

    def test_branch_only_failure_named(self):
        self.r.put("cloud/launcher/tests/test_a.py", FAIL); self.r.commit("break")
        rc, out = self.r.run()
        self.assertNotEqual(rc, 0)
        self.assertIn("FAIL: branch exit 1, default exit 0", out)

    def test_same_failure_is_environment_only(self):
        self.r.put("cloud/worker/tests/test_b.py", FAIL); self.r.commit("x")
        sh(self.r.d, "git", "checkout", "-q", "main"); self.r.put("cloud/worker/tests/test_b.py", FAIL)
        self.r.commit("base fails too"); sh(self.r.d, "git", "checkout", "-q", "-b", "f2")
        self.r.put("cloud/launcher/tests/test_a.py", PASS + "# x\n")
        self.r.commit("touch")
        rc, out = self.r.run()
        self.assertIn("environment-only", out)
        self.assertNotIn("FAIL: branch", out)

    def test_timeout_fails_unless_allowed(self):
        self.r.put("cloud/launcher/tests/test_a.py", "import time\ntime.sleep(30)\n"); self.r.commit("slow")
        rc, out = self.r.run(CLOUD_PREFLIGHT_TIMEOUT="1")
        self.assertNotEqual(rc, 0)
        self.assertIn("NOT RUN: timed out", out)

    def test_changed_existing_test_listed(self):
        self.r.put("cloud/worker/tests/test_b.py", PASS + "# edited\nx = 1\n"); self.r.commit("edit")
        rc, out = self.r.run()
        self.assertNotEqual(rc, 0)
        self.assertIn("cloud/worker/tests/test_b.py  +2 -0", out)

    def test_new_test_file_not_listed_as_changed(self):
        self.r.put("cloud/launcher/tests/test_c.py", PASS)
        self.r.put("bin/test-cloud-launcher", CHAIN + "# tests/test_c.py\n"); self.r.commit("c")
        rc, out = self.r.run()
        self.assertEqual(rc, 0, out)

    # mutants: each makes a check pass wrongly; the tests above must then fail
    def test_mutant_scan_ignores_unchained(self):
        self.r.put("cloud/launcher/tests/test_new.py", PASS); self.r.commit("new")
        m = mutant("failed |= bool(bad)", "pass"); self.addCleanup(os.remove, m)
        rc, out = self.r.run(script=m)
        self.assertEqual(rc, 0)  # the mutant wrongly passes ...
        self.assertNotEqual(self.r.run()[0], 0)  # ... the real command does not

    def test_mutant_comparison_ignores_branch_failure(self):
        # a failure with no edited existing file: the default branch's own test is broken by a changed helper
        self.r.put("cloud/launcher/helper.txt", "x"); self.r.put("cloud/launcher/tests/test_a.py", PASS)
        self.r.commit("h")
        sh(self.r.d, "git", "checkout", "-q", "main"); self.r.put("cloud/launcher/tests/test_a.py", "import os,sys\nsys.exit(0 if not os.path.exists('cloud/launcher/helper.txt') else 1)\n")
        self.r.commit("dep"); sh(self.r.d, "git", "checkout", "-q", "-b", "f3"); self.r.put("cloud/launcher/helper.txt", "x"); self.r.commit("h2")
        m = mutant('v = "FAIL: branch exit %s, default exit %s" % (b, d); failed = True',
                   'v = "FAIL: branch exit %s, default exit %s" % (b, d)'); self.addCleanup(os.remove, m)
        self.assertEqual(self.r.run(script=m)[0], 0)
        self.assertNotEqual(self.r.run()[0], 0)

    def test_mutant_changed_list_omits_edit(self):
        self.r.put("cloud/worker/tests/test_b.py", PASS + "x = 1\n"); self.r.commit("edit")
        m = mutant("if path in base_tests or path in (set()):", "if False:"); self.addCleanup(os.remove, m)
        self.assertEqual(self.r.run(script=m)[0], 0)
        self.assertNotEqual(self.r.run()[0], 0)


if __name__ == "__main__":
    unittest.main()
