"""#2381: the reaper's save never writes a parentless (root) commit. A pod with no commit and no earlier save saves nothing and pushes nothing; the next save
with a readable HEAD has HEAD as parent, and one after an earlier save has both. Offline: a scratch pod (a git repository with no commit at first) and a bare
host repository whose pre-receive hook counts the pushes it receives (HOST_URL is a file:// prefix). The mutant (a scratch copy of end_hook.py without the guard)
must fail the no-commit test with the anchored text, its unmutated control passing first."""
import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
END_HOOK = os.path.join(LAUNCHER, "end_hook.py")
MODULE_ENV = "CW_TEST_WIP_ROOT_MODULE"
ANCHOR = "a parentless commit was written"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"}
GUARD = '''        if not head and not wip:   # no commit yet and no earlier save: the commit would have no parent (a root); save nothing until the pod has a commit
            return {"saved": False, "tree": tree, "wip_branch": branch, "note": "no commit yet: nothing saved"}
'''


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=dict(os.environ, **GIT_ENV), check=check, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True).stdout


def load_end_hook(path, seam):
    spec = importlib.util.spec_from_file_location("end_hook_wiproot", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HOST_URL = "file://" + seam + "/"
    mod.HELPER_DIR = os.path.join(seam, "bin")
    return mod


class Scratch:
    """A pod checkout with NO commit and a bare remote o/r.git that counts the pushes it receives."""
    worker = "coder7"

    def __init__(self, tmp):
        self.tmp = tmp
        os.makedirs(os.path.join(tmp, "o"))
        self.remote, self.work = os.path.join(tmp, "o", "r.git"), os.path.join(tmp, "work")
        self.pushes = os.path.join(tmp, "pushes.log")
        git(tmp, "init", "-q", "--bare", self.remote)
        hook = os.path.join(self.remote, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho push >> %s\nexit 0\n" % self.pushes)
        os.chmod(hook, 0o755)
        git(tmp, "init", "-q", "-b", "base", self.work)
        git(self.work, "remote", "add", "origin", "https://github.com/o/r.git")
        os.makedirs(os.path.join(tmp, "bin"))
        helper = os.path.join(tmp, "bin", "git-credential-cw")
        with open(helper, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(helper, 0o755)
        self.write("a.txt", "a\n")   # an untracked file: the tree is not empty

    def write(self, name, text):
        with open(os.path.join(self.work, name), "w") as f:
            f.write(text)

    def commit(self, msg):
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", msg)
        return git(self.work, "rev-parse", "HEAD").strip()

    def count_pushes(self):
        if not os.path.exists(self.pushes):
            return 0
        with open(self.pushes) as f:
            return len(f.read().split())

    def wip(self):
        return git(self.remote, "rev-parse", "-q", "--verify", "refs/heads/%s/wip" % self.worker, check=False).strip() or None

    def parents(self, rev):
        return git(self.remote, "rev-list", "--parents", "-n", "1", rev).split()[1:]

    def save(self, mod, git_fn=None):
        """The real snapshot(), in this process, with its git calls optionally passed through a recording function."""
        mod_git = mod.git
        if git_fn:
            mod.git = lambda *a, **k: (git_fn(a, k), mod_git(*a, **k))[1]
        ctx = mod.Ctx(os.path.join(self.tmp, "bin", "git-credential-cw"))
        env_before = dict(os.environ)
        os.environ.update(GIT_ENV)
        try:
            return mod.snapshot(self.work, self.worker, "o/r", ctx)
        finally:
            mod.git = mod_git
            os.environ.clear()
            os.environ.update(env_before)
            ctx.close()


class WipRoot(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.s = Scratch(self.tmpdir.name)
        path = os.environ.get(MODULE_ENV) or END_HOOK
        self.mod = load_end_hook(path, self.tmpdir.name)

    def test_s2_a_pod_with_no_commit_saves_nothing_and_pushes_nothing(self):
        s = self.s
        r = s.save(self.mod)
        if s.count_pushes() or s.wip():
            root = s.wip() and s.parents(s.wip()) == []
            self.fail("%s: %d push(es) received, root=%s" % (ANCHOR, s.count_pushes(), root))
        self.assertEqual((r["saved"], r["wip_branch"], r["note"]), (False, "coder7/wip", "no commit yet: nothing saved"))
        self.assertEqual(set(r), {"saved", "tree", "wip_branch", "note"})
        self.assertEqual(s.count_pushes(), 0)

    def test_s3_the_first_save_after_a_commit_has_head_as_parent_and_the_next_has_both(self):
        s = self.s
        self.assertFalse(s.save(self.mod)["saved"])   # no commit yet: nothing
        head = s.commit("first")
        s.write("b.txt", "b\n")
        self.assertTrue(s.save(self.mod)["saved"])
        first = s.wip()
        self.assertEqual((s.count_pushes(), s.parents(first)), (1, [head]))
        s.write("b.txt", "bb\n")
        self.assertTrue(s.save(self.mod)["saved"])
        second = s.wip()
        self.assertEqual((s.count_pushes(), sorted(s.parents(second))), (2, sorted([first, head])))
        self.assertEqual(len(s.parents(second)), 2)

    def test_s3_an_earlier_save_with_an_unborn_head_later_is_a_parent_when_head_is_unreadable(self):
        s = self.s
        head = s.commit("first")
        s.write("b.txt", "b\n")
        self.assertTrue(s.save(self.mod)["saved"])
        first = s.wip()
        git(s.work, "update-ref", "-d", "refs/heads/base")   # HEAD unborn again, an earlier save exists
        s.write("b.txt", "c\n")
        self.assertTrue(s.save(self.mod)["saved"])
        self.assertEqual(s.parents(s.wip()), [first])
        self.assertNotIn(head, s.parents(s.wip()))

    def test_s4_every_function_that_calls_commit_tree_passes_a_parent(self):
        with open(END_HOOK, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        callers = set()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and any(isinstance(n, ast.Constant) and n.value == "commit-tree" for n in ast.walk(fn)):
                callers.add(fn.name)
        self.assertEqual(callers, {"snapshot", "ff_over_snapshots"}, "a new path calls commit-tree: cover it here")
        # snapshot(): a recording git over a no-commit pod, a first save and a second save
        calls = []
        record = lambda a, k: calls.append(list(a))
        s = self.s
        s.save(self.mod, record)
        s.commit("first")
        s.write("b.txt", "b\n")
        s.save(self.mod, record)
        s.write("b.txt", "c\n")
        s.save(self.mod, record)
        # ff_over_snapshots(): a save, then a commit that leaves wip not an ancestor of HEAD, then the end hook's step
        s.write("d.txt", "d\n")
        s.save(self.mod, record)
        saved = os.path.join(s.work, ".git", self.mod.SNAP_SAVED_FILE)
        self.assertTrue(os.path.isfile(saved))
        s.commit("second")
        ctx = self.mod.Ctx(os.path.join(s.tmp, "bin", "git-credential-cw"))
        mod_git = self.mod.git
        self.mod.git = lambda *a, **k: (record(a, k), mod_git(*a, **k))[1]
        try:
            self.assertTrue(self.mod.ff_over_snapshots(s.work, s.worker, "file://" + s.tmp + "/o/r.git", ctx))
        finally:
            self.mod.git = mod_git
            ctx.close()
        trees = [c for c in calls if "commit-tree" in c]
        self.assertEqual(len(trees), 4)   # three saves with a commit and the end hook's step
        for c in trees:
            rest = c[c.index("commit-tree") + 2:]
            ps = [rest[i + 1] for i, x in enumerate(rest) if x == "-p" and i + 1 < len(rest)]
            self.assertTrue(ps, "%s: %r" % (ANCHOR, c))
        # the end hook's step: both parents, always
        ff = trees[-1]
        self.assertEqual(ff.count("-p"), 2)
        self.assertEqual(len(s.parents(s.wip())), 2)
        for rev in git(s.remote, "rev-list", "--grep=^WIP:", "refs/heads/coder7/wip").split():   # the saves only (the pod's own first commit is its root)
            self.assertTrue(s.parents(rev), ANCHOR)


def mutant_copy(directory):
    with open(END_HOOK, encoding="utf-8") as f:
        src = f.read()
    assert src.count(GUARD) == 1
    out = src.replace(GUARD, "")
    assert out != src, "the mutation is a no-op"
    path = os.path.join(directory, "end_hook_mutant.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    return path


class Mutant(unittest.TestCase):
    def run_test(self, path):
        return subprocess.run([sys.executable, "-m", "unittest", "test_wip_root.WipRoot.test_s2_a_pod_with_no_commit_saves_nothing_and_pushes_nothing"],
                              cwd=HERE, env=dict(os.environ, **{MODULE_ENV: path}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)

    def test_s5_a_save_without_the_guard_fails_with_the_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = os.path.join(tmp, "control.py")
            with open(END_HOOK, encoding="utf-8") as f, open(ctl, "w", encoding="utf-8") as g:
                g.write(f.read())
            c = self.run_test(ctl)   # the control first: the unmutated copy passes
            self.assertEqual(c.returncode, 0, c.stdout)
            p = self.run_test(mutant_copy(tmp))
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn(ANCHOR, p.stdout)


if __name__ == "__main__":
    unittest.main()
