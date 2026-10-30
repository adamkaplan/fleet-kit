"""#1664: the end hook answers that it has nothing to save when its pod has no checkout.

A work directory that does not exist, or that holds no .git entry, makes the hook answer pushed false with the note "no checkout: nothing to
save" and exit status 0 BEFORE any git call, so the launcher stops and deletes the pod. A checkout whose git fails for another reason still
answers an error (exit status 1) and a checkout with unpushed work is saved. Offline: scratch repositories and a fake launcher stop. The mutants
(scratch copies of the hook OUTSIDE the repository) run this file as a child through stub_support.start.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "worker", "tests"))
import stub_support  # noqa: E402
import test_token_at_limit as tl  # noqa: E402  (its harness: a running worker, the fake cluster, a scripted end hook)

HOOK_ENV = "CW_TEST_END_HOOK"      # a scratch copy of the hook, for the mutant runs
INNER_ENV = "CW_TEST_INNER_RUN"    # set in a mutant's child run: the mutant test is not run again inside it
NOTE = "no checkout: nothing to save"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"}
PLANTED = "ghp_Zk9Wp3Zk9Wp3Zk9Wp3Zk9Wp3Zk9Wp3Zk9Wp3.txt"
SPEC = {"pushed": False, "wip_branch": None, "skipped": [], "note": NOTE}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=dict(os.environ, **GIT_ENV), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=60).stdout


def read_hook():
    with open(os.path.join(LAUNCHER, "end_hook.py")) as f:
        return f.read()


GATE = '    if not has_checkout(workdir):\n        print(json.dumps({"pushed": False, "wip_branch": None, "skipped": [], "note": NO_CHECKOUT_NOTE}))\n        return 0\n'
FAIL = '        print(json.dumps({"pushed": False, "wip_branch": None, "error": str(e)}))\n        return 1\n'


def write_copy(directory, old, new):
    src = read_hook()
    assert src.count(old) == 1, "the mutation did not apply"
    path = os.path.join(directory, "end_hook.py")
    with open(path, "w") as f:
        f.write(src.replace(old, new))
    return path


def base_copy(directory):
    """The hook as it was: no check before git. It is also the build that keeps the pod when there is no checkout (git fails, an error)."""
    return write_copy(directory, GATE, "")


def nothing_for_any_failure_copy(directory):
    """A build that answers nothing to save for ANY git failure."""
    return write_copy(directory, FAIL, '        print(json.dumps({"pushed": False, "wip_branch": None, "skipped": [], "note": NO_CHECKOUT_NOTE}))\n        return 0\n')


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = self.tmp.name

    def hook(self, work, path=None):
        env = dict(os.environ, CW_WORKDIR=work, CW_WORKER="coder7", **GIT_ENV)
        path = path or os.environ.get(HOOK_ENV) or os.path.join(LAUNCHER, "end_hook.py")
        env.pop(HOOK_ENV, None)
        p = subprocess.run([sys.executable, path], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        return p.returncode, json.loads(p.stdout.strip().splitlines()[-1])

    def checkout(self, name, unpushed=False, dirty=False):
        """A repository whose origin is a bare repository holding its first commit; optionally one more unpushed commit or a dirty tree."""
        bare, work = os.path.join(self.t, name + ".git"), os.path.join(self.t, name)
        git(self.t, "init", "-q", "--bare", bare)
        git(self.t, "init", "-q", "-b", "base", work)
        git(work, "remote", "add", "origin", bare)
        with open(os.path.join(work, "a.txt"), "w") as f:
            f.write("a\n")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "first")
        git(work, "push", "-q", "origin", "base")
        if unpushed:
            with open(os.path.join(work, "b.txt"), "w") as f:
                f.write("b\n")
            git(work, "add", "-A")
            git(work, "commit", "-q", "-m", "second")
        if dirty:
            with open(os.path.join(work, "c.txt"), "w") as f:
                f.write("c\n")
        return work

    def corrupt(self, work):
        with open(os.path.join(work, ".git", "index"), "wb") as f:
            f.write(b"not an index at all")
        return work


class S2NoDirectory(Scratch):
    def test_a_pod_whose_work_directory_does_not_exist_has_nothing_to_save(self):
        code, ans = self.hook(os.path.join(self.t, "absent"))
        self.assertEqual((code, ans), (0, SPEC))
        self.assertNotIn("error", ans)
        print("S2: answers 1, pushed false, note %s, error fields 0, exit status 0" % NOTE)


class S3NoGitEntry(Scratch):
    def test_a_directory_without_dot_git_has_nothing_to_save(self):
        d = os.path.join(self.t, "plain")
        os.mkdir(d)
        code, ans = self.hook(d)
        self.assertEqual((code, ans), (0, SPEC))
        print("S3: directory without .git: same note 1 of 1, exit status 0")


class S4OtherFailures(Scratch):
    def test_a_git_failure_in_a_checkout_is_an_error_and_unpushed_work_is_saved(self):
        code, ans = self.hook(self.corrupt(self.checkout("bad")))
        self.assertEqual(code, 1)
        self.assertTrue(ans["error"])
        self.assertNotIn("note", ans)
        self.assertIs(ans["pushed"], False)
        code2, ans2 = self.hook(self.checkout("saving", unpushed=True))
        self.assertEqual((code2, ans2), (0, {"pushed": True, "wip_branch": "coder7/wip", "skipped": []}))
        print("S4: git failure answers an error with exit status 1 1 of 1, checkout with unpushed work saved 1 of 1")


class S5Launcher(tl.Stop):
    def test_the_launcher_deletes_the_pod_and_both_records_carry_the_note(self):
        code, ans = Scratch.hook(self, os.path.join(self.tmp.name, "absent"))
        self.assertEqual(code, 0)
        res, error = self.run_stop([ans])
        self.assertIsNone(error)
        self.assertIsNone(self.pod())   # deleted: no kept mark is left
        line = [l for l in "".join(self.captured).splitlines() if "end hook:" in l]
        record = json.loads(self.record_text())["end_hook"]
        places = int(len(line) == 1 and NOTE in line[0]) + int(record["note"] == NOTE)
        self.assertEqual(places, 2)
        print("S5: deletes 1, kept marks 0, note in audit line and run record %d of 2" % places)


class U2Equal(Scratch):
    def test_a_checkout_answers_as_the_base(self):
        base = base_copy(self.t)
        same = 0
        for name, kw in (("clean", {}), ("unpushed", {"unpushed": True}), ("dirty", {"dirty": True}), ("bad", {"corrupt": True})):
            works = []
            for tag in ("new", "old"):
                w = self.checkout(name + tag, unpushed=kw.get("unpushed", False), dirty=kw.get("dirty", False))
                works.append(self.corrupt(w) if kw.get("corrupt") else w)
            a, b = self.hook(works[0]), self.hook(works[1], base)
            norm = lambda r: json.loads(json.dumps(r).replace(self.t + "/" + name + "new", "W").replace(self.t + "/" + name + "old", "W"))
            same += norm(a) == norm(b)
        self.assertEqual(same, 4)
        print("U2: answers equal the base %d of 4" % same)


class U3Planted(Scratch):
    def test_the_check_never_lists_the_directory(self):
        d = os.path.join(self.t, "plain")
        os.mkdir(d)
        open(os.path.join(d, PLANTED), "w").close()
        w = self.checkout("real")
        open(os.path.join(w, PLANTED), "w").close()
        git(w, "add", "-A")
        git(w, "commit", "-q", "-m", "planted")
        git(w, "push", "-q", "origin", "base")
        seen = sum(PLANTED in json.dumps(self.hook(x)[1]) for x in (d, w, os.path.join(d, "nested")))
        self.assertEqual(seen, 0)
        print("U3: planted name occurrences %d" % seen)


class S6Mutants(Scratch):
    TARGETS = ((base_copy, "S2NoDirectory"), (nothing_for_any_failure_copy, "S4OtherFailures"))

    def child(self, copy, cls):
        scratch = tempfile.mkdtemp(prefix="cw-mutant-")   # outside the repository
        self.addCleanup(shutil.rmtree, scratch, True)
        path = copy(scratch)
        p = stub_support.start(self, [sys.executable, "-m", "unittest", "-q", "test_end_hook_no_checkout." + cls], cwd=HERE,
                               env=dict(os.environ, **{HOOK_ENV: path, INNER_ENV: "1"}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out, _ = p.communicate(timeout=90)
        return path, p.returncode, out

    @unittest.skipIf(os.environ.get(INNER_ENV), "inside a mutant's child run")
    def test_each_mutant_fails_its_test(self):
        caught = applied = 0
        for copy, cls in self.TARGETS:
            path, rc, out = self.child(copy, cls)
            applied += os.path.isfile(path)
            caught += rc == 1
            self.assertEqual(rc, 1, out)
        self.assertEqual((caught, applied), (2, 2))
        print("S6: mutants caught %d of 2, mutations applied %d of 2" % (caught, applied))


class S7Docs(Scratch):
    def test_the_documentation_says_it(self):
        with open(os.path.join(LAUNCHER, "README.md")) as f:
            text = f.read().lower()
        found = sum(p in text for p in ("no checkout", "a new image"))
        self.assertEqual(found, 2)
        print("S7: phrases found %d of 2" % found)


if __name__ == "__main__":
    unittest.main()
