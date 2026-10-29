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


class S8HookMutant(unittest.TestCase):
    def test_the_unpushed_commit_test_fails_on_the_skip_mutant(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = mutant_copy(tmp)   # asserts the edit applied
            env = dict(os.environ, **{HOOK_ENV: path})
            p = subprocess.run([sys.executable, "-m", "unittest",
                                "test_end_hook_nothing_to_push.S7NothingToPush.test_one_unpushed_commit_tries_the_push_and_the_failure_is_reported"],
                               cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
        self.assertEqual(p.returncode, 1, p.stdout)


class S9Docs(unittest.TestCase):
    def test_the_readme_states_the_four_sentences(self):
        with open(os.path.join(LAUNCHER, "README.md")) as f:
            text = " ".join(f.read().split())
        phrases = ("at least every 10 minutes (`RENEW_CADENCE_SECONDS`)",
                   "writes one line on the reaper's own log and one audit line (action `slow-reap-pass`) with its seconds",
                   "runs today on the operator's Mac as a launchd job, so a stalled reaper is seen within one pass",
                   "decides from local references only (no network call")
        self.assertEqual([p for p in phrases if p not in text], [])


if __name__ == "__main__":
    unittest.main()
