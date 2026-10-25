#!/usr/bin/env python3
"""Offline tests of the hardening of bin/cloud-prepush (ask #1284): process-group kill on timeout, the memory-heavy
list, the bound values. Throwaway repositories under the temporary directory only."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SCRIPT = os.path.join(ROOT, "bin", "cloud-prepush")
TD = "cloud/worker/tests/"
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_NOSYSTEM="1")
PASS = "import sys\nsys.exit(0)\n"
OKPF = "import sys\nsys.exit(0)\n"


def git(cwd, *a):
    p = subprocess.run(["git"] + list(a), cwd=cwd, capture_output=True, text=True, env=ENV)
    assert p.returncode == 0, (a, p.stderr)


def put(repo, rel, text, mode=0o755):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def make_repo(tmp, tests):
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "trunk")
    put(repo, "a.txt", "one\n", 0o644)
    for rel, text in tests.items():
        put(repo, rel, text)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feat")
    put(repo, "a.txt", "one NEW\n", 0o644)
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "change")
    return repo


def run(repo, preflight=OKPF, env=None, script_text=None, timeout=60):
    d = tempfile.mkdtemp(prefix="prepush-bin-")
    try:
        target = os.path.join(d, "cloud-prepush")
        if script_text is None:
            shutil.copy(SCRIPT, target)
        else:
            open(target, "w").write(script_text)
        open(os.path.join(d, "cloud-pr-preflight"), "w").write(preflight)
        e = dict(ENV, **(env or {}))
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, target, "trunk"], cwd=repo, capture_output=True, text=True, env=e,
                           timeout=timeout)
        p.elapsed = time.monotonic() - t0
        return p
    finally:
        shutil.rmtree(d, ignore_errors=True)


def spawner(pidfile, tail=""):
    return ("import sys\nfrom subprocess import Popen as start\np = start(['sleep', '77'])\n"
            "open(%r, 'w').write(str(p.pid))\n%s\nimport time\ntime.sleep(77)\n" % (pidfile, tail))


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a killed child nobody reaps stays a zombie: that is dead
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def wait_dead(pid):
    for _ in range(50):
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prepush-hard-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def pid(self, name):
        return int(open(os.path.join(self.tmp, name)).read())


class TestGroupKill(Base):
    def kill_case(self, script_text=None):
        pf = os.path.join(self.tmp, "pf.pid")
        tf = os.path.join(self.tmp, "t.pid")
        out = {}
        # preflight timeout
        repo = make_repo(os.path.join(self.tmp, "r1"), {TD + "test_x.py": "# a.txt\n" + PASS})
        out["pf"] = run(repo, preflight=spawner(pf), env={"CLOUD_PREPUSH_PREFLIGHT_TIMEOUT": "2"},
                        script_text=script_text)
        # per-test timeout
        repo = make_repo(os.path.join(self.tmp, "r2"), {TD + "test_x.py": "# a.txt\n" + spawner(tf)})
        out["test"] = run(repo, env={"CLOUD_PREPUSH_TIMEOUT": "2"}, script_text=script_text)
        return out, pf, tf

    def test_preflight_and_test_children_die(self):
        out, pf, tf = self.kill_case()
        self.assertEqual(out["pf"].returncode, 1)
        self.assertLess(out["pf"].elapsed, 20)
        self.assertIn("timed out", out["pf"].stdout)
        self.assertTrue(wait_dead(self.pid("pf.pid")), "preflight grandchild survived")
        self.assertEqual(out["test"].returncode, 1)
        self.assertLess(out["test"].elapsed, 30)
        self.assertTrue(wait_dead(self.pid("t.pid")), "test grandchild survived")

    def test_mutant_direct_child_only_fails(self):
        src = open(SCRIPT).read()
        mut = src.replace("os.killpg(proc.pid, signal.SIGKILL)", "proc.kill()")
        self.assertNotEqual(mut, src, "mutation did not apply")
        out, pf, tf = self.kill_case(script_text=mut)
        pids = [self.pid("pf.pid"), self.pid("t.pid")]
        survived = [p for p in pids if not wait_dead(p)]
        for p in survived:
            os.kill(p, 9)  # clean up the mutant's orphans
        self.assertTrue(survived, "the mutant should leave an orphan")


class TestHeavy(Base):
    def heavy(self, script_text=None):
        marker = os.path.join(self.tmp, "ran")
        name = TD + "test_root_scanner.py"
        body = "# a.txt\nopen(%r, 'w').write('x')\n" % marker
        repo = make_repo(os.path.join(self.tmp, "r"), {name: body})
        return run(repo, script_text=script_text), marker

    def test_heavy_not_run(self):
        p, marker = self.heavy()
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(p.returncode, 1)
        self.assertIn("skipped: memory-heavy " + TD + "test_root_scanner.py", p.stdout)
        self.assertIn("could not run 1", p.stdout)

    def test_mutant_empty_heavy_runs(self):
        src = open(SCRIPT).read()
        mut = src.replace('HEAVY = ("cloud/worker/tests/test_root_scanner.py",)', "HEAVY = ()")
        self.assertNotEqual(mut, src, "mutation did not apply")
        p, marker = self.heavy(script_text=mut)
        self.assertTrue(os.path.exists(marker))


class TestBounds(Base):
    def test_values(self):
        repo = make_repo(os.path.join(self.tmp, "r"), {TD + "test_x.py": "# a.txt\n" + PASS})
        for v in ("0", "-1", "abc", "1e3", "1.5", " 5", "5\n", "999999999", ""):
            for name in ("CLOUD_PREPUSH_TIMEOUT", "CLOUD_PREPUSH_TOTAL", "CLOUD_PREPUSH_PREFLIGHT_TIMEOUT"):
                p = run(repo, env={name: v})
                self.assertEqual(p.returncode, 2, (name, v, p.stderr))
        self.assertEqual(run(repo, env={"CLOUD_PREPUSH_TIMEOUT": "86400"}).returncode, 0)

    def test_empty_change_exits_before_preflight(self):
        repo = make_repo(os.path.join(self.tmp, "r"), {})
        git(repo, "checkout", "-q", "trunk")
        marker = os.path.join(self.tmp, "pfran")
        p = run(repo, preflight="open(%r, 'w').write('x')\nraise SystemExit(1)\n" % marker)
        self.assertEqual(p.returncode, 0)
        self.assertFalse(os.path.exists(marker))

    def test_preflight_env_has_no_identity_and_own_scratch(self):
        out = os.path.join(self.tmp, "env.txt")
        pf = "import os\nopen(%r, 'w').write(os.environ['HOME'])\n" % out
        tst = "# a.txt\nimport os\nopen(%r, 'a').write(' ' + os.environ['HOME'])\n" % out
        repo = make_repo(os.path.join(self.tmp, "r"), {TD + "test_x.py": tst})
        self.assertEqual(run(repo, preflight=pf).returncode, 0)
        homes = open(out).read().split()
        self.assertEqual(len(homes), 3)
        self.assertEqual(homes[0], homes[1])
        self.assertNotEqual(homes[1], homes[2])


if __name__ == "__main__":
    unittest.main()
