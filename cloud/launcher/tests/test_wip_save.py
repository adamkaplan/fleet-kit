"""#2304: the reaper saves a running pod's work to <worker>/wip every ten minutes without touching the pod (S2, S3, S5).
Offline: a scratch git repository stands for the pod's /work/repo and a bare repository for the host (HOST_URL is a file:// prefix); the fake kube runs the
real `end_hook.py --snapshot` in the scratch pod, as the launcher's exec does in a real one. The real Launcher.reap drives it.
The mutant (a scratch copy of the launcher without the save step) must fail the changed-pod test with the anchored text."""
import contextlib
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as real  # noqa: E402

T0 = dt.datetime(2026, 11, 7, 9, 0, 0, tzinfo=dt.timezone.utc)
MODULE_ENV = "CW_TEST_WIP_MODULE"
ANCHOR = "a changed pod was not saved"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"}
DRIVER = (
    "import importlib.util, sys\n"
    "sp = importlib.util.spec_from_file_location('h', sys.argv[1]); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)\n"
    "m.HOST_URL = sys.argv[2]; m.HELPER_DIR = sys.argv[3]\n"
    "sys.exit(m.main())\n")


def target_module():
    path = os.environ.get(MODULE_ENV)
    if not path:
        return real
    spec = importlib.util.spec_from_file_location("cloud_worker_wipsave_scratch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=dict(os.environ, **GIT_ENV), check=check, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True).stdout


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Pod:
    """A scratch pod checkout and its bare remote o/r.git."""
    def __init__(self, tmp, worker="coder7"):
        self.tmp, self.worker = tmp, worker
        os.makedirs(os.path.join(tmp, "o"))
        self.remote, self.work = os.path.join(tmp, "o", "r.git"), os.path.join(tmp, "work")
        git(tmp, "init", "-q", "--bare", self.remote)
        git(tmp, "init", "-q", "-b", "base", self.work)
        git(self.work, "remote", "add", "origin", "https://github.com/o/r.git")
        self.write("a.txt", "a\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "first")
        git(self.work, "push", "-q", self.remote, "base")
        git(self.work, "fetch", "-q", self.remote, "+refs/heads/*:refs/remotes/origin/*")
        git(self.work, "branch", "-q", "--set-upstream-to=origin/base", "base")
        os.makedirs(os.path.join(tmp, "bin"))
        helper = os.path.join(tmp, "bin", "git-credential-cw")
        with open(helper, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(helper, 0o755)
        self.driver = os.path.join(tmp, "driver.py")
        with open(self.driver, "w") as f:
            f.write(DRIVER)

    def write(self, name, text):
        with open(os.path.join(self.work, name), "w") as f:
            f.write(text)

    def snapshot(self):
        env = dict(os.environ, CW_WORKDIR=self.work, CW_WORKER=self.worker, CW_REPO="o/r", **GIT_ENV)
        return subprocess.run([sys.executable, self.driver, os.path.join(LAUNCHER, "end_hook.py"), "file://" + self.tmp + "/",
                               os.path.join(self.tmp, "bin"), "--snapshot"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)

    def wip(self):
        out = git(self.remote, "rev-parse", "-q", "--verify", "refs/heads/%s/wip" % self.worker, check=False).strip()
        return out or None

    def count(self):
        return int(git(self.remote, "rev-list", "--count", "--all", "--not", "refs/heads/base").strip()) if self.wip() else 0

    def fingerprint(self):
        """The pod's branch head, its index bytes, its files and its status: all of it must be equal before and after a save."""
        files = {n: sha(os.path.join(self.work, n)) for n in sorted(os.listdir(self.work)) if n != ".git"}
        return (git(self.work, "rev-parse", "refs/heads/base").strip(), sha(os.path.join(self.work, ".git", "index")), files,
                git(self.work, "status", "--porcelain", "--untracked-files=all"))

    def refuse_pushes(self):
        hook = os.path.join(self.remote, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho refused >&2\nexit 1\n")
        os.chmod(hook, 0o755)


class EndHookSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pod = Pod(self.tmp.name)

    def test_s2_a_changed_tree_is_saved_once_and_the_pod_is_untouched(self):
        p = self.pod
        p.write("b.txt", "new file\n")
        p.write("a.txt", "changed\n")
        git(p.work, "add", "a.txt")   # a staged change and an untracked file
        before = p.fingerprint()
        r = p.snapshot()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(json.loads(r.stdout)["saved"])
        self.assertEqual((p.count(), p.fingerprint()), (1, before))
        self.assertEqual(git(p.remote, "show", "refs/heads/coder7/wip:b.txt"), "new file\n")
        self.assertEqual(git(p.remote, "show", "refs/heads/coder7/wip:a.txt"), "changed\n")
        r = p.snapshot()   # nothing changed: nothing written
        self.assertEqual((r.returncode, json.loads(r.stdout)["saved"], p.count(), p.fingerprint()), (0, False, 1, before))
        p.write("b.txt", "newer\n")
        self.assertTrue(json.loads(p.snapshot().stdout)["saved"])
        self.assertEqual(p.count(), 2)

    def test_s2_the_pods_commits_are_reachable_and_a_clean_pod_saves_nothing(self):
        p = self.pod
        self.assertEqual((p.snapshot().returncode, p.wip()), (0, None))   # a clean pod on a pushed commit: nothing
        p.write("c.txt", "c\n")
        git(p.work, "add", "-A")
        git(p.work, "commit", "-q", "-m", "second")   # committed, not pushed, tree clean
        before = p.fingerprint()
        self.assertTrue(json.loads(p.snapshot().stdout)["saved"])
        head = git(p.work, "rev-parse", "HEAD").strip()
        self.assertIn(head, git(p.remote, "rev-list", "refs/heads/coder7/wip").split())
        self.assertEqual(p.fingerprint(), before)

    def test_a_secret_looking_name_cannot_slip_through_as_a_pathspec_or_as_a_tracked_change(self):
        p = self.pod
        p.write(".env", "TOKEN=1\n")
        git(p.work, "add", ".env")
        git(p.work, "commit", "-q", "-m", "tracked secret")
        git(p.work, "push", "-q", p.remote, "base")
        p.write(".env", "TOKEN=2\n")   # a tracked secret-looking file, modified
        p.write(":(exclude)q.key", "k\n")
        p.write(":(top)m.txt", "magic\n")   # not secret-looking: staged by name, taken literally
        p.write("staged.pem", "s\n")
        git(p.work, "add", "-f", "staged.pem")   # a secret-looking file the agent staged itself
        p.write("b.txt", "1\n")
        self.assertEqual(p.snapshot().returncode, 0)
        names = git(p.remote, "ls-tree", "-r", "--name-only", "refs/heads/coder7/wip").split("\n")
        self.assertIn("b.txt", names)
        self.assertNotIn(":(exclude)q.key", names)
        self.assertNotIn("staged.pem", names)
        self.assertIn(":(top)m.txt", names)
        self.assertEqual(git(p.remote, "show", "refs/heads/coder7/wip:.env"), "TOKEN=1\n")

    def test_a_diverged_remote_wip_is_never_forced_over(self):
        p = self.pod
        p.write("b.txt", "1\n")
        p.snapshot()
        other = os.path.join(self.tmp.name, "other2")
        git(self.tmp.name, "clone", "-q", p.remote, other)
        git(other, "checkout", "-q", "coder7/wip")
        git(other, "-c", "user.name=o", "-c", "user.email=o@invalid", "commit", "-q", "--allow-empty", "-m", "theirs")
        git(other, "push", "-q", "origin", "HEAD:refs/heads/coder7/wip")
        theirs = p.wip()
        p.write("b.txt", "2\n")
        r = p.snapshot()   # the old wip head is fetched as the parent: a fast-forward over theirs, or a refusal; theirs stays reachable
        self.assertIn(theirs, git(p.remote, "rev-list", "refs/heads/coder7/wip").split())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_deletions_links_and_the_executable_bit_are_saved_without_git_add(self):
        p = self.pod
        os.remove(os.path.join(p.work, "a.txt"))
        os.symlink("b.txt", os.path.join(p.work, "ln"))
        p.write("run.sh", "#!/bin/sh\n")
        os.chmod(os.path.join(p.work, "run.sh"), 0o755)
        r = p.snapshot()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ls = git(p.remote, "ls-tree", "-r", "refs/heads/coder7/wip")
        self.assertNotIn("a.txt", ls)
        self.assertIn("120000", [l.split()[0] for l in ls.splitlines() if l.endswith("\tln")][0])
        self.assertIn("100755", [l.split()[0] for l in ls.splitlines() if l.endswith("\trun.sh")][0])

    def test_a_decoy_name_cannot_make_the_save_read_another_file(self):
        p = self.pod
        p.write(".env", "SECRET=1\n")
        p.write('"\\056env"', "decoy\n")   # git would unquote this name to .env in a --stdin-paths list
        os.symlink("/etc", os.path.join(p.work, "lnk"))
        r = p.snapshot()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tree = git(p.remote, "ls-tree", "-r", "--name-only", "refs/heads/coder7/wip").split("\n")
        self.assertNotIn(".env", tree)
        self.assertNotIn("SECRET=1", git(p.remote, "grep", "-I", "SECRET", "refs/heads/coder7/wip", check=False))
        os.rename(os.path.join(p.work, "lnk"), os.path.join(p.work, "lnk2"))
        os.mkdir(os.path.join(p.work, "lnk"))
        os.symlink("/etc", os.path.join(p.work, "lnk", "d"))
        os.rename(os.path.join(p.work, "lnk"), os.path.join(p.work, "real"))
        os.symlink("real", os.path.join(p.work, "via"))   # a link on the way: via/d is never read through
        self.assertEqual(p.snapshot().returncode, 0)
        self.assertNotIn("via/d", git(p.remote, "ls-tree", "-r", "--name-only", "refs/heads/coder7/wip").split("\n"))

    def test_a_staged_secret_with_pathspec_magic_in_its_name_is_backed_out(self):
        p = self.pod
        p.write(":(top)x.pem", "s\n")
        git(p.work, "add", "-f", "--", ":(literal):(top)x.pem")
        p.write("b.txt", "1\n")
        self.assertEqual(p.snapshot().returncode, 0)
        names = git(p.remote, "ls-tree", "-r", "--name-only", "refs/heads/coder7/wip").split("\n")
        self.assertIn("b.txt", names)
        self.assertNotIn(":(top)x.pem", names)

    def test_the_pushes_are_never_forced(self):
        p = self.pod
        log = os.path.join(self.tmp.name, "git-args")
        wrapper = os.path.join(self.tmp.name, "gitw")
        with open(wrapper, "w") as f:
            f.write('#!/bin/sh\necho "$@" >> %s\nexec /usr/bin/git "$@"\n' % log)
        os.chmod(wrapper, 0o755)
        with open(p.driver, "w") as f:
            f.write(DRIVER.replace("sys.exit(m.main())", "m.GIT_BIN = %r\nsys.exit(m.main())" % wrapper))
        p.write("b.txt", "1\n")
        self.assertEqual(p.snapshot().returncode, 0)
        pushes = [l for l in open(log).read().splitlines() if " push " in l]
        self.assertEqual(len(pushes), 1)
        for bad in (" -f", "--force", " +", ":+"):
            self.assertNotIn(bad, pushes[0].split("refs/heads")[0] + " " if bad != " +" else pushes[0])

    def test_the_executable_bit_is_the_owners(self):
        p = self.pod
        p.write("m.sh", "x\n")
        os.chmod(os.path.join(p.work, "m.sh"), 0o611)
        self.assertEqual(p.snapshot().returncode, 0)
        ls = git(p.remote, "ls-tree", "-r", "refs/heads/coder7/wip")
        self.assertTrue([l for l in ls.splitlines() if l.endswith("\tm.sh")][0].startswith("100644"))

    def test_a_garbled_marker_file_is_not_a_crash(self):
        p = self.pod
        p.write("b.txt", "1\n")
        open(os.path.join(p.work, ".git", "cw-wip-saved"), "w").close()
        r = p.snapshot()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(json.loads(r.stdout)["saved"])

    def test_an_unborn_head_with_no_wip_branch_is_saved_as_a_root_commit(self):
        t = os.path.join(self.tmp.name, "u")
        os.makedirs(t)
        u = Pod.__new__(Pod)
        u.tmp, u.worker = t, "coder7"
        os.makedirs(os.path.join(t, "o"))
        u.remote, u.work = os.path.join(t, "o", "r.git"), os.path.join(t, "work")
        git(t, "init", "-q", "--bare", u.remote)
        git(t, "init", "-q", "-b", "base", u.work)
        git(u.work, "remote", "add", "origin", "https://github.com/o/r.git")
        os.makedirs(os.path.join(t, "bin"))
        shutil.copy(os.path.join(self.tmp.name, "bin", "git-credential-cw"), os.path.join(t, "bin"))
        u.driver = os.path.join(t, "driver.py")
        shutil.copy(self.pod.driver, u.driver)
        u.write("x.txt", "x\n")
        r = u.snapshot()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(json.loads(r.stdout)["saved"])
        self.assertEqual(git(u.remote, "show", "refs/heads/coder7/wip:x.txt"), "x\n")

    def test_s3_the_new_commit_has_the_old_wip_head_as_its_parent(self):
        p = self.pod
        p.write("b.txt", "1\n")
        p.snapshot()
        old = p.wip()
        p.write("b.txt", "2\n")
        r = p.snapshot()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        parents = git(p.remote, "rev-list", "--parents", "-n", "1", "refs/heads/coder7/wip").split()[1:]
        self.assertEqual(parents[0], old, "a fast-forward: the old wip head is the first parent")
        self.assertEqual(git(p.remote, "merge-base", "--is-ancestor", old, "refs/heads/coder7/wip", check=False), "")

    def test_s3_a_refused_push_is_an_error_and_is_not_marked_saved(self):
        p = self.pod
        p.write("b.txt", "1\n")
        p.refuse_pushes()
        r = p.snapshot()
        self.assertEqual(r.returncode, 1)
        self.assertTrue(json.loads(r.stdout)["error"])
        self.assertIsNone(p.wip())
        os.remove(os.path.join(p.remote, "hooks", "pre-receive"))
        self.assertTrue(json.loads(p.snapshot().stdout)["saved"], "a failed save is tried again")

    def test_a_secret_looking_untracked_file_is_left_out(self):
        p = self.pod
        p.write("b.txt", "1\n")
        p.write("token.key", "not for the remote\n")
        self.assertEqual(p.snapshot().returncode, 0)
        names = git(p.remote, "ls-tree", "-r", "--name-only", "refs/heads/coder7/wip").split()
        self.assertIn("b.txt", names)
        self.assertNotIn("token.key", names)

    def test_the_end_hook_after_a_save_pushes_a_fast_forward(self):
        """After a running save, wip is not an ancestor of the pod's HEAD: the end hook's own push must still be accepted."""
        p = self.pod
        p.write("b.txt", "1\n")
        p.snapshot()
        old = p.wip()
        p.write("b.txt", "2\n")
        env = dict(os.environ, CW_WORKDIR=p.work, CW_WORKER="coder7", CW_REPO="o/r", **GIT_ENV)
        drv = DRIVER.replace("m.main()", "m.prestop_main()")
        with open(p.driver, "w") as f:
            f.write(drv)
        r = subprocess.run([sys.executable, p.driver, os.path.join(LAUNCHER, "end_hook.py"), "file://" + p.tmp + "/", os.path.join(p.tmp, "bin")],
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(p.wip(), old)
        self.assertEqual(git(p.remote, "show", "refs/heads/coder7/wip:b.txt"), "2\n")
        self.assertEqual(git(p.remote, "merge-base", "--is-ancestor", old, p.wip(), check=False), "")

    def test_a_wip_head_that_is_not_our_own_save_is_never_overwritten_by_the_end_hook(self):
        p = self.pod
        p.write("b.txt", "1\n")
        p.snapshot()
        other = os.path.join(self.tmp.name, "other")
        git(self.tmp.name, "clone", "-q", p.remote, other)
        git(other, "checkout", "-q", "coder7/wip")
        git(other, "-c", "user.name=o", "-c", "user.email=o@invalid", "commit", "-q", "--allow-empty", "-m", "theirs")
        git(other, "push", "-q", "origin", "HEAD:refs/heads/coder7/wip")
        theirs = p.wip()
        p.write("b.txt", "2\n")
        env = dict(os.environ, CW_WORKDIR=p.work, CW_WORKER="coder7", CW_REPO="o/r", **GIT_ENV)
        with open(p.driver, "w") as f:
            f.write(DRIVER.replace("m.main()", "m.prestop_main()"))
        subprocess.run([sys.executable, p.driver, os.path.join(LAUNCHER, "end_hook.py"), "file://" + p.tmp + "/", os.path.join(p.tmp, "bin")],
                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        self.assertEqual(p.wip(), theirs)


def kube_pod(name="w1", worker="coder7", role=None, persistent=False):
    ann = {"cw/worker": worker, "cw/started": real.iso(T0 - dt.timedelta(minutes=5)), "cw/hard-deadline": real.iso(T0 + dt.timedelta(hours=2))}
    if role:
        ann["cw/role"] = role
    if persistent:
        ann["cw/persistent"] = "true"
    return {"metadata": {"name": name, "uid": "u-" + name, "annotations": ann},
            "spec": {"containers": [{"env": [{"name": "CW_REPO", "value": "o/r"}]}]}, "status": {"phase": "Running"}}


class FakeKube:
    def __init__(self, pods, pod_by_name):
        self.pods, self.pod_by_name, self.execs, self.answer = pods, pod_by_name, [], None

    def list_pods(self):
        return list(self.pods)

    def annotate(self, *a, **k):
        pass

    def exec(self, name, *args, **kw):
        self.execs.append((name,) + args)
        if args[:2] == ("cw-end-hook", "--snapshot"):
            if self.answer is not None:
                return self.answer
            r = self.pod_by_name[name].snapshot()
            return r.stdout
        return ""

    def scrub(self, text, limit=300):
        return str(text)[:limit]

    def add_secrets(self, *a, **k):
        pass

    def close(self):
        pass


class Scene:
    def __init__(self, pods, pod_by_name, tmp):
        self.mod, self.now = target_module(), T0
        self.kube = FakeKube(pods, pod_by_name)
        self.audit_path = os.path.join(tmp, "audit.jsonl")
        env = {"CW_CLOUD_AUDIT": self.audit_path, "XDG_STATE_HOME": tmp}
        scene = self

        class L(self.mod.Launcher):
            def hard_deadline(self, p):
                return T0 + dt.timedelta(hours=9)

            def activity(self, name):
                return (True, T0)

            def cost(self, p):
                return 0.0

            def send_wip_notes(self, live, now): pass
            def send_final_notes(self, live, now): pass
            def send_idle_nudges(self, live, now, acts): pass
            def renew_git_tokens(self, pods, now): return []

        self.launcher = L(self.kube, env, lambda: scene.now, lambda s: None)

    def reap(self, minutes=0):
        self.now = T0 + dt.timedelta(minutes=minutes)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(self.mod, "run_git_token_cmd", return_value="tok-synthetic"), \
                mock.patch.object(self.mod, "git_token_cmd", return_value=["x"]), mock.patch.object(self.mod, "check_repo_allowed", return_value=None):
            self.launcher.reap()
        return err.getvalue()

    def lines(self):
        try:
            with open(self.audit_path) as f:
                return [json.loads(x) for x in f if x.strip() and json.loads(x).get("action") == "wip-save"]
        except FileNotFoundError:
            return []

    def snapshots(self):
        return [e for e in self.kube.execs if e[1:3] == ("cw-end-hook", "--snapshot")]


class ReaperSave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pod = Pod(self.tmp.name)
        self.scene = Scene([kube_pod()], {"w1": self.pod}, self.tmp.name)

    def test_s2_a_changed_pod_is_saved_once_and_not_again_until_it_changes(self):
        p, s = self.pod, self.scene
        p.write("b.txt", "work\n")
        before = p.fingerprint()
        s.reap(0)
        self.assertEqual(p.count(), 1, ANCHOR)
        self.assertEqual(p.fingerprint(), before, "the pod's branch, index and files are untouched")
        s.reap(5)
        self.assertEqual(len(s.snapshots()), 1, "not asked again before ten minutes")
        s.reap(11)
        self.assertEqual((len(s.snapshots()), p.count()), (2, 1), "asked again, and nothing changed: 0 new commits")
        p.write("b.txt", "more work\n")
        s.reap(22)
        self.assertEqual(p.count(), 2)
        self.assertEqual(p.fingerprint()[:1], before[:1])

    def test_an_orchestrator_and_a_pod_launched_not_to_commit_are_not_saved(self):
        s = Scene([kube_pod("o1", role="orchestrator"), kube_pod("o2", persistent=True)], {}, self.tmp.name)
        s.reap(0)
        self.assertEqual(s.snapshots(), [])

    def test_s3_a_failed_save_is_said_once(self):
        p, s = self.pod, self.scene
        p.write("b.txt", "work\n")
        p.refuse_pushes()
        err1 = s.reap(0)
        err2 = s.reap(11)
        self.assertEqual(len(s.snapshots()), 2, "tried again")
        self.assertEqual(len(s.lines()), 1, "one audit line")
        self.assertEqual((err1.count("was not saved"), err2.count("was not saved")), (1, 0))
        self.assertEqual([x["ok"] for x in s.lines()], [False])
        os.remove(os.path.join(p.remote, "hooks", "pre-receive"))
        s.reap(22)
        self.assertEqual(p.count(), 1)

    def test_an_unreadable_answer_is_a_failure_said_once(self):
        s = self.scene
        s.kube.answer = "not json"
        s.reap(0)
        s.reap(11)
        self.assertEqual(len(s.lines()), 1)


def mutant_copy(directory):
    with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
        src = f.read()
    old = "        wip_save.save_pass(self, live, now, NAME_RE.fullmatch)"
    assert src.count(old) == 1
    path = os.path.join(directory, "cloud_worker_wipsave_mutant.py")
    with open(path, "w") as f:
        f.write(src.replace(old, "        pass #"))
    return path


class Mutant(unittest.TestCase):
    def run_test(self, path):
        return subprocess.run([sys.executable, "-m", "unittest", "test_wip_save.ReaperSave.test_s2_a_changed_pod_is_saved_once_and_not_again_until_it_changes"],
                              cwd=HERE, env=dict(os.environ, **{MODULE_ENV: path}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)

    def test_s5_a_launcher_without_the_save_step_fails_with_the_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = os.path.join(tmp, "control.py")
            with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
                src = f.read()
            with open(ctl, "w") as f:
                f.write(src)
            c = self.run_test(ctl)   # the control first: the unmutated copy passes
            self.assertEqual(c.returncode, 0, c.stdout)
            p = self.run_test(mutant_copy(tmp))
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn(ANCHOR, p.stdout)


if __name__ == "__main__":
    unittest.main()
