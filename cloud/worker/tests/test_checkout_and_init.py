"""#1460: the checkout directory the entrypoint makes (S2, U3) and the one new allowance of the git wrapper (S3-S5), and the
sample step of cloud/tests/test-cloud (S7). Offline; synthetic values only."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.join(HERE, "..", "..")
GUARD = os.path.join(CLOUD, "worker", "guard.sh")
ENTRYPOINT = os.path.join(CLOUD, "worker", "entrypoint.sh")
REFUSAL = "refused: git configuration from the environment (GIT_CONFIG_*): the repository's rules are the guard; this wrapper is only a convenience\n"


class Init(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.d = os.path.realpath(t.name)
        self.tmp = os.path.join(self.d, "tmp")
        self.out = os.path.join(self.d, "out")   # outside the temporary directory
        os.mkdir(self.tmp); os.mkdir(self.out)

    def guard(self, *args, cwd=None, env_cfg=True):
        env = {"PATH": "/usr/bin:/bin", "CW_REAL_GIT": "/bin/true", "TMPDIR": self.tmp}
        if env_cfg:
            env["GIT_CONFIG_GLOBAL"] = os.path.join(self.d, "gc")
        return subprocess.run(["sh", GUARD, "git"] + list(args), env=env, cwd=cwd, capture_output=True, text=True)

    def test_s3_init_under_tmpdir_runs(self):
        for args in (["init", os.path.join(self.tmp, "a")], ["init", "-q", os.path.join(self.tmp, "b")], ["-C", os.path.join(self.tmp, "c"), "init"]):
            r = self.guard(*args)
            self.assertEqual((r.returncode, r.stderr), (0, ""), args)

    def test_s3_real_git_creates_the_repository_through_the_wrapper(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("no git")
        target = os.path.join(self.tmp, "real")
        self.assertEqual(self.guard("init", "-q", target).returncode, 0)
        subprocess.run([git, "init", "-q", target], check=True, capture_output=True, env={"PATH": "/usr/bin:/bin", "HOME": self.d})
        self.assertTrue(os.path.exists(os.path.join(target, ".git")))

    def test_s4_init_elsewhere_is_refused_and_nothing_is_made(self):
        sib = self.tmp + "2"; os.mkdir(sib)
        self.addCleanup(shutil.rmtree, sib, True)
        link = os.path.join(self.tmp, "link"); os.symlink(self.out, link)
        forms = [["init", os.path.join(self.out, "w")],
                 ["init", "/home/x"],
                 ["init", os.path.join(self.tmp, "..", "out", "x")],
                 ["init", os.path.join(link, "x")],
                 ["init", os.path.join(sib, "x")],
                 ["init", self.tmp],
                 ["init", ""]]
        for args in forms:
            r = self.guard(*args)
            self.assertEqual((r.returncode, r.stderr), (3, REFUSAL), args)
        self.assertEqual(os.listdir(self.out), [])

    def test_s5_every_other_form_is_refused_with_the_base_message(self):
        t = os.path.join(self.tmp, "a")
        for args in (["clone", "https://github.com/x/y", t], ["push", "origin", "x"], ["config", "user.x", "1"], ["init"], ["init", "-q"],
                     ["init", "--template=/x", t], ["init", t, t + "b"], ["-C", t, "config", "user.x", "1"], ["-c", "a.b=c", "init", t]):
            r = self.guard(*args, cwd=self.tmp)
            self.assertEqual((r.returncode, r.stderr), (3, REFUSAL), args)

    def test_u1_without_environment_configuration_nothing_changes(self):
        self.assertEqual(self.guard("init", os.path.join(self.out, "w"), env_cfg=False).returncode, 0)   # as at the base: not a refused form
        self.assertEqual(self.guard("push", "origin", "main", env_cfg=False).returncode, 3)


class Checkout(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.d = os.path.realpath(t.name)
        self.work = os.path.join(self.d, "work"); os.mkdir(self.work)
        os.chmod(self.work, 0o2777)   # as the platform makes the mount

    def start(self, work):
        env = {"PATH": "/usr/bin:/bin", "HOME": self.d, "WORKER_DOCKER": "0", "CW_RUN_DIR": os.path.join(self.d, "norun"),
               "CW_TMP_DIR": os.path.join(self.d, "tmpfs"), "CW_WORK_DIR": work}
        os.mkdir(env["CW_TMP_DIR"])
        return subprocess.run(["sh", ENTRYPOINT, "sh", "-c", "umask"], env=env, capture_output=True, text=True, timeout=60)

    def test_s2_the_checkout_directory_has_the_runners_permissions(self):
        r = self.start(self.work)
        self.assertEqual(r.returncode, 0, r.stderr)
        st = os.stat(os.path.join(self.work, "repo"))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o755)
        self.assertFalse(st.st_mode & stat.S_ISGID)
        self.assertEqual(st.st_uid, os.getuid())
        self.assertEqual(r.stdout.strip(), "0022")

    def test_u3_a_checkout_that_cannot_be_made_refuses_the_start(self):
        open(os.path.join(self.work, "repo"), "w").close()   # a file where the directory goes
        r = self.start(self.work)
        self.assertEqual(r.returncode, 1)
        lines = r.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("worker:"))
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
