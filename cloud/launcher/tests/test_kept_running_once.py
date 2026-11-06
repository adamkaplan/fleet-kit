"""A kept RUNNING pod whose push fails on an ended token is tried once, marked, and skipped by every later pass; a clean read-only pod
is stopped with no remote call (ask #2236)."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_git_token_renewal as tr  # noqa: E402
import end_hook  # noqa: E402

cw = tr.cw
NEEDS = {"pushed": False, "wip_branch": None, "error": "the git token had ended", "refresh": "needed"}
MARK = "cw/kept-running-tried"
AGAIN = "a kept pod was tried again after its mark"


def run_git(cwd, *a):
    return subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@invalid"] + list(a), check=True,
                          capture_output=True, text=True).stdout


class Counting:
    """A git wrapper that counts the remote verbs and answers them (success, or a refusal) without any network; the rest runs real git."""
    def __init__(self, path, refuse):
        self.path, self.refuse = path, refuse
        with open(path + ".sh", "w") as f:
            f.write('#!/bin/sh\nfor a in "$@"; do case "$a" in fetch|push|ls-remote) echo "$a" >> %s.calls\n'
                    '%s;; esac; done\nexec git "$@"\n' % (path, 'echo "fatal: refused" >&2; exit 1' if refuse else "exit 0"))
        os.chmod(path + ".sh", 0o755)

    def calls(self):
        try:
            with open(self.path + ".calls") as f:
                return f.read().split()
        except OSError:
            return []


class TestHook(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = tmp.name
        seed = self.d + "/seed"
        subprocess.run(["git", "init", "-q", "-b", "main", seed], check=True)
        run_git(seed, "commit", "-q", "--allow-empty", "-m", "one")
        subprocess.run(["git", "clone", "-q", "--bare", seed, self.d + "/remote.git"], check=True)
        subprocess.run(["git", "clone", "-q", self.d + "/remote.git", self.d + "/repo"], check=True)
        self.work = self.d + "/repo"

    def run_hook(self, refuse, n):
        g = Counting(self.d + "/g%d" % n, refuse)
        env = {"CW_WORKER": "w1", "CW_WORKDIR": self.work}
        with mock.patch.object(end_hook, "GIT_BIN", g.path + ".sh"), mock.patch.dict(os.environ, env), \
                mock.patch("builtins.print") as pr:
            code = end_hook.main()
        return code, g.calls(), pr

    def test_clean_read_only_pod_is_stopped_with_no_remote_call(self):
        run_git(self.work, "checkout", "-q", "-b", "scratch")   # a local branch with no remote counterpart, at the starting commit
        code, calls, _ = self.run_hook(True, 1)
        self.assertEqual((code, calls), (0, []))

    def test_a_local_commit_is_pushed_with_a_working_remote(self):
        run_git(self.work, "commit", "-q", "--allow-empty", "-m", "mine")
        code, calls, _ = self.run_hook(False, 2)
        self.assertEqual(code, 0)
        self.assertIn("push", calls)

    def test_a_local_commit_is_kept_with_a_refusing_remote(self):
        run_git(self.work, "commit", "-q", "--allow-empty", "-m", "mine")
        code, calls, _ = self.run_hook(True, 3)
        self.assertEqual(code, 1)


class TestCleanLocal(unittest.TestCase):
    """clean_local must call WORK every kind of unsaved state (a false "clean" loses work) and a truly clean clone clean."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = tmp.name
        seed = self.d + "/seed"
        subprocess.run(["git", "init", "-q", "-b", "main", seed], check=True)
        run_git(seed, "commit", "-q", "--allow-empty", "-m", "one")
        subprocess.run(["git", "clone", "-q", "--bare", seed, self.d + "/remote.git"], check=True)
        subprocess.run(["git", "clone", "-q", self.d + "/remote.git", self.d + "/repo"], check=True)
        self.w = self.d + "/repo"

    def clean(self, fn=None):
        return (fn or end_hook.clean_local)(self.w, None)

    def mutant(self):
        import inspect
        src = inspect.getsource(end_hook.clean_local)
        cut = 'roots.update(out("reflog", "show", "--format=%H", line).split())'
        self.assertIn(cut, src)
        src = src.replace(cut, "pass").replace("roots = set(start)", "roots = set()")
        ns = dict(vars(end_hook))
        exec(src, ns)
        return ns["clean_local"]

    def test_a_truly_clean_clone_is_clean(self):
        self.assertTrue(self.clean())

    def test_each_false_clean_case_is_not_clean(self):
        def detached():
            run_git(self.w, "checkout", "-q", "--detach")
            run_git(self.w, "commit", "-q", "--allow-empty", "-m", "orphan")
            run_git(self.w, "checkout", "-q", "main")

        def stash():
            open(self.w + "/f", "w").write("x")
            run_git(self.w, "add", "f")
            run_git(self.w, "commit", "-q", "-m", "f")
            run_git(self.w, "reset", "-q", "--hard", "origin/main")
            open(self.w + "/g", "w").write("x")
            run_git(self.w, "add", "g")
            run_git(self.w, "stash")

        def worktree():
            run_git(self.w, "worktree", "add", "-q", "--detach", self.d + "/wt")
            open(self.d + "/wt/u", "w").write("x")

        def worktree_commit():
            run_git(self.w, "worktree", "add", "-q", "--detach", self.d + "/wt")
            run_git(self.d + "/wt", "commit", "-q", "--allow-empty", "-m", "in wt")

        def staged():
            open(self.w + "/s", "w").write("x")
            run_git(self.w, "add", "s")

        def untracked():
            open(self.w + "/u", "w").write("x")

        def local_branch():
            run_git(self.w, "checkout", "-q", "-b", "mine")
            run_git(self.w, "commit", "-q", "--allow-empty", "-m", "mine")
            run_git(self.w, "checkout", "-q", "main")
        for name, plant in (("detached orphan commit", detached), ("stash", stash), ("second worktree untracked", worktree),
                            ("second worktree commit", worktree_commit), ("staged file", staged), ("untracked file", untracked),
                            ("local-only branch commit", local_branch)):
            with self.subTest(name):
                self.tearDown_clone()
                plant()
                self.assertFalse(self.clean(), name + " was called clean")

    def tearDown_clone(self):
        subprocess.run("rm -rf %s/repo %s/wt && git clone -q %s/remote.git %s/repo && git -C %s worktree prune"
                       % (self.d, self.d, self.d, self.d, self.w), shell=True, check=True)

    def test_older_stash_entry_holding_the_work_is_not_clean(self):
        run_git(self.w, "commit", "-q", "--allow-empty", "-m", "work")
        open(self.w + "/g", "w").write("x")
        run_git(self.w, "add", "g")
        run_git(self.w, "stash")           # older entry: its parent is the local-only commit
        run_git(self.w, "reset", "-q", "--hard", "origin/main")
        open(self.w + "/h", "w").write("y")
        run_git(self.w, "add", "h")
        run_git(self.w, "stash")           # newest entry: only on the starting commit
        self.assertFalse(self.clean())

    def test_lightweight_and_annotated_tags_on_local_commit_are_not_clean(self):
        for kind in ("light", "annotated"):
            with self.subTest(kind):
                self.tearDown_clone()
                run_git(self.w, "checkout", "-q", "--detach")
                run_git(self.w, "commit", "-q", "--allow-empty", "-m", "tagged")
                run_git(self.w, "tag", *(["-a", "-m", "m"] if kind == "annotated" else []), "t1")
                run_git(self.w, "checkout", "-q", "main")
                run_git(self.w, "reflog", "expire", "--expire=now", "--all")
                self.assertFalse(self.clean())

    def test_a_mutant_without_the_reflog_check_is_caught(self):
        def plant():
            run_git(self.w, "checkout", "-q", "--detach")
            run_git(self.w, "commit", "-q", "--allow-empty", "-m", "orphan")
            run_git(self.w, "checkout", "-q", "main")
        plant()
        self.assertFalse(self.clean(), "control: the real clean_local must see the orphan")
        self.assertTrue(self.clean(self.mutant()), "the mutant must call the orphan clean")   # so the real test would fail on it


class Scen(tr.Harness):
    def count(self):
        n = 0
        for _, _, files in os.walk(self.records):
            n += len(files)
        return n

    def stops(self):
        return [l for l in self.audit() if l["action"] == "stop"]

    def scenario(self):
        L = self.launcher()
        calls = []

        def hook(name):
            calls.append(name)
            return dict(NEEDS)
        L.run_hook = hook
        L.scope_probe = lambda token, repo: [{"full_name": tr.REPO, "permissions": {"pull": True, "push": True, "admin": False}}]
        L._projected = lambda name, token: True
        L.sleep = lambda s: self.clock.advance(seconds=s)
        self.at(hours=4)   # past the hard limit
        self.one_pass(L)
        self.assertEqual((len(self.mints()), len(calls), self.count(), len(self.stops())), (1, 2, 1, 1), "PASS1")
        self.assertEqual(self.st()["pods"][self.name]["metadata"]["annotations"].get(MARK), "true", "MARK")
        for n in (2, 3):
            self.one_pass(L)
            self.assertEqual((len(self.mints()), len(calls), self.count(), len(self.stops())), (1, 2, 1, 1), AGAIN + " (pass %d)" % n)
        self.assertIn(self.name, self.st()["pods"])   # never deleted

    def test_ended_token_kept_running_pod_is_tried_once(self):
        self.scenario()

    def test_a_mutant_without_the_mark_check_is_caught(self):
        orig = cw.ann

        def blind(pod, key, default=None):
            return default if key == "kept-running-tried" else orig(pod, key, default)
        self.scenario()   # control first
        self.tearDown(), self.setUp()
        with mock.patch.object(cw, "ann", blind):
            with self.assertRaises(AssertionError) as cm:
                self.scenario()
        self.assertIn(AGAIN, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
