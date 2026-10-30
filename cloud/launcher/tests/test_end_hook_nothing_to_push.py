"""#1579: the end hook decides 'nothing to push' from local references only.

A scratch git repository whose origin is a bare repository that refuses every push (its pre-receive hook counts the attempts). The hook is run
as a program. The mutant (a scratch copy that skips the push when there IS an unpushed commit) must fail the unpushed-commit test.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
HOOK_ENV = "CW_TEST_END_HOOK"   # a scratch copy of the hook, for the mutant run
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=dict(os.environ, **GIT_ENV), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True).stdout


def mutant_copy(directory):
    """The real hook with the nothing-to-push test inverted: it skips the push exactly when there IS an unpushed commit."""
    with open(os.path.join(LAUNCHER, "end_hook.py")) as f:
        src = f.read()
    old = '"HEAD", "--not", "--remotes", check=False, ctx=ctx).stdout.strip() == "":'
    assert src.count(old) == 1
    path = os.path.join(directory, "end_hook.py")
    with open(path, "w") as f:
        f.write(src.replace(old, old.replace('== ""', '!= ""')))
    return path


def local_only_copy(directory):
    """The real hook deciding from the local references WITHOUT the fetch first (refresh_refs always reports a failure without running git)."""
    with open(os.path.join(LAUNCHER, "end_hook.py")) as f:
        src = f.read()
    old = "    refreshed = refresh_refs(workdir, url if prestop else \"origin\", ctx)\n"
    assert src.count(old) == 1
    path = os.path.join(directory, "end_hook.py")
    with open(path, "w") as f:
        f.write(src.replace(old, "    refreshed = False\n"))
    return path


DRIVER = (
    "import importlib.util, sys\n"
    "sp = importlib.util.spec_from_file_location('h', sys.argv[1]); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)\n"
    "m.HOST_URL = sys.argv[2]; m.HELPER_DIR = sys.argv[3]\n"
    "sys.exit(m.prestop_main())\n")


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        self.bare, self.work, self.log = os.path.join(t, "origin.git"), os.path.join(t, "work"), os.path.join(t, "attempts")
        git(t, "init", "-q", "--bare", self.bare)
        git(t, "init", "-q", "-b", "base", self.work)
        git(self.work, "remote", "add", "origin", self.bare)
        with open(os.path.join(self.work, "a.txt"), "w") as f:
            f.write("a\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "first")
        git(self.work, "push", "-q", "origin", "base")   # the remote holds the first commit; origin/base tracks it
        hook = os.path.join(self.bare, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho attempt >> %s\nexit 1\n" % self.log)   # from now on the remote refuses every push, after counting it
        os.chmod(hook, 0o755)

    def attempts(self):
        try:
            with open(self.log) as f:
                return len(f.read().split())
        except OSError:
            return 0

    def hook(self):
        env = dict(os.environ, CW_WORKDIR=self.work, CW_WORKER="coder7", **GIT_ENV)
        path = os.environ.get(HOOK_ENV) or os.path.join(LAUNCHER, "end_hook.py")
        env.pop(HOOK_ENV, None)
        return subprocess.run([sys.executable, path], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)


class S7NothingToPush(Scratch):
    def test_a_clean_tree_with_no_unpushed_commit_skips_the_push(self):
        p = self.hook()
        res = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual((p.returncode, self.attempts(), res["pushed"]), (0, 0, False))

    def test_a_clean_tree_result_says_nothing_to_push(self):
        sys.path.insert(0, LAUNCHER)
        import end_hook
        res = end_hook.run(self.work, "coder7")
        self.assertIsInstance(res, end_hook.NothingToPush)
        self.assertEqual(self.attempts(), 0)

    def test_one_unpushed_commit_tries_the_push_and_the_failure_is_reported(self):
        with open(os.path.join(self.work, "b.txt"), "w") as f:
            f.write("b\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "second")
        p = self.hook()
        res = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual((p.returncode, self.attempts(), res["pushed"]), (1, 1, False))   # one attempt; the failure is reported; the stop keeps the pod
        self.assertTrue(res["error"])

    def test_a_dirty_tree_is_saved_and_pushed_as_before(self):
        with open(os.path.join(self.work, "c.txt"), "w") as f:
            f.write("c\n")
        p = self.hook()
        self.assertEqual((p.returncode, self.attempts()), (1, 1))


class S7Fetch(Scratch):
    """The prestop path: HOST_URL is a file:// prefix, the remote is a bare repository o/r.git under it, origin names the repository on the host."""
    def setUp(self):
        super().setUp()
        t = self.tmp.name
        os.makedirs(os.path.join(t, "o"))
        self.remote = os.path.join(t, "o", "r.git")
        os.rename(self.bare, self.remote)
        self.bare = self.remote
        git(self.work, "remote", "set-url", "origin", "https://github.com/o/r.git")
        git(self.work, "fetch", "-q", self.remote, "+refs/heads/*:refs/remotes/origin/*")
        helper_dir = os.path.join(t, "bin")
        os.makedirs(helper_dir)
        h = os.path.join(helper_dir, "git-credential-cw")
        with open(h, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(h, 0o755)
        self.helper_dir, self.driver = helper_dir, os.path.join(t, "driver.py")
        with open(self.driver, "w") as f:
            f.write(DRIVER)

    def prestop(self, hook_path=None):
        env = dict(os.environ, CW_WORKDIR=self.work, CW_WORKER="coder7", CW_REPO="o/r", **GIT_ENV)
        path = hook_path or os.environ.get(HOOK_ENV) or os.path.join(LAUNCHER, "end_hook.py")
        env.pop(HOOK_ENV, None)
        return subprocess.run([sys.executable, self.driver, path, "file://" + self.tmp.name + "/", self.helper_dir], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90)

    def lines(self, p):
        return [l for l in p.stdout.splitlines() if l.startswith("cw-prestop:")]

    def test_a_clean_tree_with_a_working_fetch_says_nothing_to_push(self):
        p = self.prestop()
        print("count (a): attempts=%d lines=%r exit=%d" % (self.attempts(), self.lines(p), p.returncode))
        self.assertEqual((p.returncode, self.attempts(), self.lines(p)), (0, 0, ["cw-prestop: nothing to push"]))

    def test_a_refused_fetch_decides_from_local_references_in_one_line(self):
        os.rename(self.remote, self.remote + ".moved")   # the remote is unreachable: the fetch fails
        p = self.prestop()
        print("count (b): attempts=%d lines=%r exit=%d" % (self.attempts(), self.lines(p), p.returncode))
        self.assertEqual((p.returncode, len(self.lines(p))), (0, 1))
        self.assertIn("local references", self.lines(p)[0])
        self.assertFalse(os.path.exists(self.log))

    def test_a_stale_remote_tracking_ref_after_the_remote_branch_is_deleted_tries_the_push(self):
        git(self.remote, "update-ref", "-d", "refs/heads/base")   # deleted after the last fetch: origin/base is STALE
        p = self.prestop()
        print("count (c): attempts=%d exit=%d" % (self.attempts(), p.returncode))
        self.assertEqual(self.attempts(), 1)

    def test_one_unpushed_commit_against_a_remote_that_blocks_every_push_keeps_the_pod(self):
        with open(os.path.join(self.work, "b.txt"), "w") as f:
            f.write("b\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "second")
        p = self.prestop()
        print("count (d): attempts=%d exit=%d" % (self.attempts(), p.returncode))
        self.assertEqual(self.attempts(), 1)
        self.assertTrue(any("not pushed" in l for l in self.lines(p)))

    def test_no_line_carries_the_marker_token(self):
        marker = "MARKER-TOKEN-0123"
        with open(os.path.join(self.helper_dir, "git-credential-cw"), "w") as f:
            f.write("#!/bin/sh\necho password=%s\n" % marker)
        os.rename(self.remote, self.remote + ".moved")
        p = self.prestop()
        self.assertEqual((p.stdout + p.stderr).count(marker), 0)


class S8HookMutant(unittest.TestCase):
    def test_the_unpushed_commit_test_fails_on_the_skip_mutant(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = mutant_copy(tmp)   # asserts the edit applied
            env = dict(os.environ, **{HOOK_ENV: path})
            p = subprocess.run([sys.executable, "-m", "unittest",
                                "test_end_hook_nothing_to_push.S7NothingToPush.test_one_unpushed_commit_tries_the_push_and_the_failure_is_reported"],
                               cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
        self.assertEqual(p.returncode, 1, p.stdout)


    def test_the_stale_fetch_test_fails_on_the_local_only_mutant(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = local_only_copy(tmp)   # asserts the edit applied
            env = dict(os.environ, **{HOOK_ENV: path})
            p = subprocess.run([sys.executable, "-m", "unittest",
                                "test_end_hook_nothing_to_push.S7Fetch.test_a_stale_remote_tracking_ref_after_the_remote_branch_is_deleted_tries_the_push"],
                               cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
        self.assertEqual(p.returncode, 1, p.stdout)


class S9Docs(unittest.TestCase):
    def test_the_readme_states_the_four_sentences(self):
        with open(os.path.join(LAUNCHER, "README.md")) as f:
            text = " ".join(f.read().split())
        phrases = ("at least every 10 minutes (`RENEW_CADENCE_SECONDS`)",
                   "writes one line on the reaper's own log and one audit line (action `slow-reap-pass`) with its seconds",
                   "runs today on the operator's Mac as a launchd job, so a stalled reaper is seen within one pass",
                   "fetches first and falls back to the local references only when the fetch fails",
                   "holds the reaper's renewal lock for as long as the call's own timeout (120 seconds)")
        self.assertEqual([p for p in phrases if p not in text], [])


if __name__ == "__main__":
    unittest.main()
