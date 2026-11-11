"""#2437: the running save keeps the pod's own history. The save commit has ONE parent (the pod's HEAD) and names the earlier save in a `Previous-Save:` line;
a plain push when the remote tip is an ancestor of the new commit, else one --force-with-lease push naming the tip read in the same call, to the worker's
own <worker>/wip reference only. Real git in scratch repositories (a bare remote and a pod clone; HOST_URL is a file:// prefix). The mutant (a scratch copy of
end_hook.py with the second parent restored) must fail the shape test with the anchored text, its unmutated control passing first."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_wip_root import END_HOOK, GIT_ENV, Scratch, git, load_end_hook  # noqa: E402

MODULE_ENV = "CW_TEST_WIP_REBASABLE_MODULE"
ANCHOR = "the save made a merge commit"
PARENT_LINE = "        parents = [head] if head else [wip]"
WIP = "refs/heads/coder7/wip"


def refs(remote):
    return git(remote, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()


class Rebasable(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.s = Scratch(self.tmpdir.name)
        self.mod = load_end_hook(os.environ.get(MODULE_ENV) or END_HOOK, self.tmpdir.name)
        self.s.write("a.txt", "a\n")
        self.base = self.s.commit("base")

    def message(self, rev):
        return git(self.s.remote, "log", "-n", "1", "--format=%B", rev)

    def three_commits_saved_rebased_saved(self):
        s = self.s
        for n in "123":
            s.write("f%s.txt" % n, n + "\n")
            s.commit("pod %s" % n)
        s.write("scratch.txt", "x\n")
        self.assertTrue(s.save(self.mod)["saved"])
        first = s.wip()
        git(s.work, "checkout", "-q", "-b", "newbase", self.base)   # the default branch moved on: a commit on another file
        s.write("n.txt", "n\n")
        newbase = s.commit("new base")
        git(s.work, "checkout", "-q", "base")
        git(s.work, "rebase", "-q", newbase)
        s.write("scratch.txt", "y\n")
        self.assertTrue(s.save(self.mod)["saved"])
        return first, newbase

    def test_s2_the_first_save_has_head_as_its_one_parent_and_no_trailer_and_the_second_names_the_first(self):
        s = self.s
        head = git(s.work, "rev-parse", "HEAD").strip()
        s.write("b.txt", "1\n")
        self.assertTrue(s.save(self.mod)["saved"])
        first = s.wip()
        self.assertEqual(s.parents(first), [head])
        self.assertNotIn("Previous-Save", self.message(first))
        s.write("b.txt", "2\n")
        self.assertTrue(s.save(self.mod)["saved"])
        second = s.wip()
        self.assertEqual(s.parents(second), [head])
        self.assertEqual([x for x in self.message(second).splitlines() if x.startswith("Previous-Save")], ["Previous-Save: " + first])
        self.assertEqual(len(first), 40)

    def test_s3_three_commits_saved_rebased_and_saved_again_hold_each_commit_once(self):
        s = self.s
        first, newbase = self.three_commits_saved_rebased_saved()
        top = s.wip()
        self.assertEqual(len(s.parents(top)), 1, ANCHOR)
        pod = git(s.remote, "rev-list", "--count", "--grep=^WIP:", "--invert-grep", "%s..%s" % (newbase, top)).strip()
        self.assertEqual(pod, "3", "%s: %s pod commits since the new base" % (ANCHOR, pod))
        subjects = git(s.remote, "log", "--format=%s", top).splitlines()
        self.assertEqual(sorted(x for x in subjects if x.startswith("pod")), ["pod 1", "pod 2", "pod 3"], ANCHOR)
        # a person's rebase of the branch onto a still newer base applies the 3 commits (and the save on top) without a conflict
        clone = os.path.join(self.tmpdir.name, "clone")
        git(self.tmpdir.name, "clone", "-q", s.remote, clone)
        git(clone, "checkout", "-q", "-b", "mine", "origin/coder7/wip")
        git(clone, "checkout", "-q", "-b", "newer", newbase)
        with open(os.path.join(clone, "z.txt"), "w") as f:
            f.write("z\n")
        git(clone, "add", "-A")
        git(clone, "commit", "-q", "-m", "newer base")
        git(clone, "checkout", "-q", "mine")
        r = subprocess.run(["git", "rebase", "newer"], cwd=clone, env=dict(os.environ, **GIT_ENV), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        applied = git(clone, "rev-list", "--count", "--grep=^WIP:", "--invert-grep", "newer..HEAD").strip()
        self.assertEqual(applied, "3", ANCHOR)

    def test_s3_the_second_save_after_a_rebase_is_pushed_with_a_lease_and_is_not_a_fast_forward_of_the_first(self):
        s = self.s
        first, _ = self.three_commits_saved_rebased_saved()
        self.assertNotEqual(s.wip(), first)
        self.assertEqual(subprocess.run(["git", "merge-base", "--is-ancestor", first, s.wip()], cwd=s.remote).returncode, 1)
        self.assertIn("Previous-Save: " + first, self.message(s.wip()))

    def test_s4_a_lease_failure_prints_one_line_changes_nothing_and_tries_once(self):
        s = self.s
        first, _ = self.three_commits_saved_rebased_saved()
        # a third save whose remote tip is not an ancestor (a rebase again), the remote tip moving between the read and the push
        git(s.work, "commit", "-q", "--amend", "-m", "pod 3 again")
        s.write("scratch.txt", "z\n")
        tip = s.wip()
        other = git(s.remote, *("commit-tree", git(s.remote, "rev-parse", tip + "^{tree}").strip(), "-m", "someone else's save"), ).strip()
        pushes = []

        def move(a, k):
            if a and "push" in a:
                pushes.append(list(a))
                git(s.remote, "update-ref", WIP, other)
        before = refs(s.remote)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            r = s.save(self.mod, move)
        self.assertEqual(len(pushes), 1, "no second push is tried")
        self.assertTrue(any(x.startswith("--force-with-lease=" + WIP + ":" + tip) for x in pushes[0]), pushes)
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertFalse(r["saved"])
        after = refs(s.remote)
        self.assertEqual(s.wip(), other)
        self.assertEqual([x for x in after if not x.startswith(WIP)], [x for x in before if not x.startswith(WIP)])

    def test_s5_only_the_workers_own_wip_reference_changes(self):
        s = self.s
        first_head = git(s.work, "rev-parse", "HEAD").strip()
        s.write("b.txt", "1\n")
        s.save(self.mod)
        git(s.remote, "update-ref", "refs/heads/main", first_head)
        git(s.remote, "update-ref", "refs/heads/feature", first_head)
        for step in range(2):
            before = dict(x.split(" ") for x in refs(s.remote))
            if step == 0:
                s.write("b.txt", "2\n")   # a plain push
            else:
                git(s.work, "commit", "-q", "--amend", "-m", "rewritten")   # a forced one
                s.write("b.txt", "3\n")
            self.assertTrue(s.save(self.mod)["saved"])
            after = dict(x.split(" ") for x in refs(s.remote))
            self.assertEqual([k for k in after if after.get(k) != before.get(k)], [WIP])
            self.assertEqual(set(after), set(before))

    def test_s6_a_secret_looking_file_is_not_saved_and_the_other_files_are(self):
        s = self.s
        s.write(".env", "TOKEN=x\n")
        s.write("ok.txt", "ok\n")
        self.assertTrue(s.save(self.mod)["saved"])
        names = git(s.remote, "ls-tree", "-r", "--name-only", WIP).split()
        self.assertIn("ok.txt", names)
        self.assertNotIn(".env", names)


def mutant_copy(directory):
    with open(END_HOOK, encoding="utf-8") as f:
        src = f.read()
    assert src.count(PARENT_LINE) == 1
    lines = src.split("\n")
    i = next(n for n, x in enumerate(lines) if x.startswith(PARENT_LINE))
    lines[i] = "        parents = [x for x in dict.fromkeys([wip, head]) if x]"
    out = "\n".join(lines)
    assert out != src, "the mutation is a no-op"
    path = os.path.join(directory, "end_hook_mutant.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    return path


class Mutant(unittest.TestCase):
    def run_test(self, path):
        return subprocess.run([sys.executable, "-m", "unittest", "test_wip_rebasable.Rebasable.test_s3_three_commits_saved_rebased_and_saved_again_hold_each_commit_once"],
                              cwd=HERE, env=dict(os.environ, **{MODULE_ENV: path}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)

    def test_s7_a_save_with_the_second_parent_fails_with_the_anchor(self):
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
