#!/usr/bin/env python3
"""Tests for bin/cloud-pr-preflight (ask #1195): temporary repositories, each check passing and failing, and a mutant per check."""
import os, signal, subprocess, sys, tempfile, shutil, time, unittest

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

    def run(self, script=SCRIPT, args=(), **env):
        e = dict(os.environ, CLOUD_PREFLIGHT_TIMEOUT="5", CLOUD_PREFLIGHT_BASE="main")
        e.update(env)
        p = subprocess.run([sys.executable, script, *args], cwd=self.d, env=e, capture_output=True, text=True)
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
        self.assertEqual(rc, 0, out)  # listed for justification, not a failure
        self.assertIn("existing test files edited (justify each", out)
        self.assertIn("cloud/worker/tests/test_b.py  +2 -0", out)

    def test_new_test_file_not_listed_as_changed(self):
        self.r.put("cloud/launcher/tests/test_c.py", PASS)
        self.r.put("bin/test-cloud-launcher", CHAIN + "# tests/test_c.py\n"); self.r.commit("c")
        rc, out = self.r.run()
        self.assertEqual(rc, 0, out)

    def test_expect_flags_stray_addition(self):
        self.r.put("cloud/launcher/tests/test_c.py", PASS)
        self.r.put("bin/test-cloud-launcher", CHAIN + "# tests/test_c.py\n")
        self.r.put("scratch.txt", "x"); self.r.commit("c")
        rc, out = self.r.run(args=["--expect", "cloud/", "--expect", "bin/"])
        self.assertNotEqual(rc, 0)
        self.assertIn("unexpected additions", out)
        self.assertIn("  scratch.txt", out.split("unexpected additions")[1])
        rc, out = self.r.run()
        self.assertEqual(rc, 0, out)
        self.assertIn("  scratch.txt", out)

    def test_expect_inside_prefix_passes(self):
        self.r.put("cloud/launcher/tests/test_c.py", PASS)
        self.r.put("bin/test-cloud-launcher", CHAIN + "# tests/test_c.py\n"); self.r.commit("c")
        self.assertEqual(self.r.run(args=["--expect", "cloud/", "--expect", "bin/"])[0], 0)

    def test_mutant_stray_root_file_missed(self):
        self.r.put("scratch.txt", "x"); self.r.commit("s")
        m = mutant("failed |= bool(stray)", "pass"); self.addCleanup(os.remove, m)
        self.assertEqual(self.r.run(script=m, args=["--expect", "cloud/"])[0], 0)
        self.assertNotEqual(self.r.run(args=["--expect", "cloud/"])[0], 0)

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
        m = mutant("if path in base_tests:", "if False:"); self.addCleanup(os.remove, m)
        self.assertNotIn("test_b.py  +", self.r.run(script=m)[1])
        self.assertIn("test_b.py  +", self.r.run()[1])


def leftovers(before):
    return [x for x in set(os.listdir(tempfile.gettempdir())) - before if x.startswith("cloud-preflight-")]


SLEEP = "import time\ntime.sleep(60)\n"


class Signals(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.close)
        self.r.put("cloud/launcher/tests/test_a.py", SLEEP); self.r.commit("sleepy")

    def go(self, sig, script=SCRIPT):
        before = set(os.listdir(tempfile.gettempdir()))
        e = dict(os.environ, CLOUD_PREFLIGHT_TIMEOUT="30", CLOUD_PREFLIGHT_BASE="main")
        p = subprocess.Popen([sys.executable, script], cwd=self.r.d, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if leftovers(before):
                break
            time.sleep(0.1)
        time.sleep(0.5)
        self.assertTrue(leftovers(before), "no temp copy appeared")
        p.send_signal(sig)
        try:
            rc = p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait(); rc = "hung"
        left = leftovers(before)
        for x in left:
            shutil.rmtree(os.path.join(tempfile.gettempdir(), x), ignore_errors=True)
        return rc, left

    def test_sigterm_cleans_up(self):
        rc, left = self.go(signal.SIGTERM)
        self.assertEqual((rc, left), (143, []))

    def test_sighup_cleans_up(self):
        rc, left = self.go(signal.SIGHUP)
        self.assertEqual((rc, left), (129, []))

    def test_sigint_cleans_up(self):
        rc, left = self.go(signal.SIGINT)
        self.assertNotEqual(rc, 0)
        self.assertEqual(left, [])

    def test_mutant_without_handlers_leaks(self):
        m = mutant("signal.signal(sg, _on_signal)", "pass"); self.addCleanup(os.remove, m)
        rc, left = self.go(signal.SIGTERM, script=m)
        self.assertTrue(left)


class Probes(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.close)

    def chained(self, *names):
        self.r.put("bin/test-cloud-launcher", CHAIN + "".join("# tests/%s\n" % n for n in names))

    def test_scan_unchained_new_file_prints_not_run_and_fails(self):
        self.r.put("cloud/worker/tests/test_z.py", PASS); self.r.commit("z")
        rc, out = self.r.run()
        self.assertNotEqual(rc, 0)
        self.assertIn("NOT RUN: cloud/worker/tests/test_z.py", out)

    def test_expect_deleted_file_listed_not_added(self):
        os.remove(os.path.join(self.r.d, "cloud/worker/tests/test_b.py")); self.r.commit("rm")
        rc, out = self.r.run(args=["--expect", "cloud/"])
        self.assertEqual(rc, 0, out)
        self.assertIn("removed on branch", out)
        self.assertEqual(out.split("added files:")[1].split("unexpected")[0].strip(), "none")

    def test_expect_rename_is_add_plus_delete(self):
        sh(self.r.d, "git", "mv", "cloud/worker/tests/test_b.py", "elsewhere.py"); self.r.commit("mv")
        rc, out = self.r.run(args=["--expect", "cloud/"])
        self.assertNotEqual(rc, 0)
        self.assertIn("  elsewhere.py", out)
        self.assertIn("removed on branch", out)

    def test_expect_path_with_spaces(self):
        self.r.put("docs/a b.txt", "x"); self.r.commit("sp")
        rc, out = self.r.run(args=["--expect", "cloud/"])
        self.assertNotEqual(rc, 0)
        self.assertIn("  docs/a b.txt", out)

    def test_expect_stray_who_at_root(self):
        self.r.put(".who", "x"); self.r.commit("w")
        rc, out = self.r.run(args=["--expect", "cloud/"])
        self.assertNotEqual(rc, 0)
        self.assertIn("  .who", out.split("unexpected additions")[1])

    def test_hostile_file_name_is_not_an_option_or_command(self):
        for name in ("test_-rf.py", "test_a b;touch PWNED.py", "test_x\ny.py"):
            self.r.put("cloud/launcher/tests/" + name, PASS)
        self.chained("test_-rf.py", "test_a b;touch PWNED.py")
        self.r.commit("hostile")
        rc, out = self.r.run()
        self.assertFalse(os.path.exists(os.path.join(self.r.d, "PWNED.py")))
        self.assertFalse(os.path.exists(os.path.join(self.r.d, "PWNED")))
        self.assertIn("test_-rf.py", out)
        # a base name starting with a dash is not read as an option either
        rc, out = self.r.run(CLOUD_PREFLIGHT_BASE="--output=PWNED")
        self.assertFalse(os.path.exists(os.path.join(self.r.d, "PWNED")))
        self.assertNotEqual(rc, 0)

    def test_offline_with_unreachable_proxy(self):
        px = "http://127.0.0.1:9"
        rc, out = self.r.run(HTTP_PROXY=px, HTTPS_PROXY=px, ALL_PROXY=px, http_proxy=px, https_proxy=px, GIT_TERMINAL_PROMPT="0")
        self.assertEqual(rc, 0, out)

    def test_binary_symlink_and_large_files(self):
        self.r.put("cloud/launcher/tests/data.bin", "")
        with open(os.path.join(self.r.d, "cloud/launcher/tests/data.bin"), "wb") as f:
            f.write(bytes(range(256)) * 10)
        os.symlink("test_a.py", os.path.join(self.r.d, "cloud/launcher/tests/test_link.py"))
        with open(os.path.join(self.r.d, "cloud/worker/tests/big.txt"), "wb") as f:
            f.write(b"x" * (20 * 1024 * 1024))
        self.chained("test_link.py")
        self.r.commit("odd")
        rc, out = self.r.run()
        self.assertEqual(rc, 0, out)

    def test_malformed_timeout_refused(self):
        rc, out = self.r.run(CLOUD_PREFLIGHT_TIMEOUT="abc")
        self.assertEqual(rc, 2)
        self.assertEqual(len(out.strip().splitlines()), 1)


class Docs(unittest.TestCase):
    DOC = open(os.path.join(ROOT, "docs", "cloud-operator-quickstart.md")).read()
    H = "## Checking a cloud pull request before the push"

    def section(self):
        self.assertEqual(self.DOC.count(self.H), 1)
        return self.DOC.split(self.H)[1].split("\n## ")[0]

    def test_heading_and_command_named(self):
        self.assertIn("bin/cloud-pr-preflight", self.section())

    def test_red_run_cause_stated_first(self):
        sec = self.section()
        self.assertIn("red run", sec)
        self.assertLess(sec.index("red run"), sec.index("Run `bin/cloud-pr-preflight`"))

    def test_brief_rule_sentence(self):
        self.assertIn("### A rule for every cloud coder brief", self.section())
        self.assertIn("Before any push, run bin/cloud-pr-preflight at the repository root and paste its three sections", self.section())

    def test_chain_names_this_test(self):
        line = [l for l in open(os.path.join(ROOT, "bin", "test-cloud-launcher")) if l.startswith("if python3")]
        self.assertEqual(len(line), 1)
        self.assertIn("cloud/launcher/tests/test_cloud_pr_preflight.py", line[0])


if __name__ == "__main__":
    unittest.main()
