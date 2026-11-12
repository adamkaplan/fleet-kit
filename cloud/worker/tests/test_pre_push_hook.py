"""Ask #2371: the image's pre-push hook (cloud/worker/pod-hooks/pre-push) decides the default-branch rule from git's own arguments and standard
input, so no command-line form has to be parsed. Driven directly (arguments and input) and by real git against an https URL nothing listens on.
A mutant without the trailer check fails with an anchored text, its control first."""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
HOOKDIR = os.path.join(WORKER, "pod-hooks")
HOOK = os.path.join(HOOKDIR, "pre-push")
README = os.path.join(WORKER, "..", "README.md")
LINE = "refused: pushing the default branch without a land record: the repository's rules are the guard; this hook is only a convenience\n"
ANCHOR = "the default branch was pushed without a land record"
CHECK = '    land_record "$lsha" || refuse\n'
ZERO = "0" * 40
NET = "https://127.0.0.1:9/x.git"


def read(p):
    with open(p) as f:
        return f.read()


class Hook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        cls.addClassCleanup(t.cleanup)
        cls.d = os.path.realpath(t.name)
        cls.repo = os.path.join(cls.d, "r")
        os.mkdir(cls.repo)
        cls.env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": cls.d, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                   "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x.invalid", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x.invalid"}
        cls.env["CW_NOTES_LOG"] = os.path.join(cls.d, "notes.log")   # ask #2426: the hook records pushes; a test never writes the pod's own notes file
        cls.git("init", "-q", "-b", "main", ".")
        cls.base = cls.commit("base", "")
        cls.git("branch", "side")
        cls.git("checkout", "-q", "side")
        cls.sidec = cls.commit("side", "")
        cls.git("checkout", "-q", "main")
        cls.good = cls.commit("good", "Land-Checks: unit 5 of 5\nLand-Council: strict ok")
        cls.none = cls.commit("none", "")
        cls.empty = cls.commit("empty", "Land-Checks:\nLand-Council: strict ok")
        cls.notrec = cls.commit("notrec", "Land-Checks: unit 5\nLand-Council: Not Recorded")
        cls.forged = cls.commit("forged", "Land-Checks: x\nLand-Council: y\n\nthe body goes on after the lines above")
        cls.dupempty = cls.commit("dupempty", "Land-Checks: ok\nLand-Council: ok\nLand-Checks:")
        cls.git("tag", "-a", "-m", "t", "tagged", cls.good)
        cls.tagobj = cls.git("rev-parse", "tagged")

    @classmethod
    def git(cls, *a, cwd=None, env=None):
        r = subprocess.run(["git"] + list(a), cwd=cwd or cls.repo, env=env or cls.env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    @classmethod
    def commit(cls, subject, trailers):
        cls.git("commit", "-q", "--allow-empty", "-m", subject + ("\n\n" + trailers if trailers else "") + "\n")
        return cls.git("rev-parse", "HEAD")

    def run_hook(self, url, lines, hook=HOOK, env=None, cwd=None):
        stdin = "".join("%s %s %s %s\n" % l for l in lines)
        return subprocess.run(["sh", hook, "origin", url], input=stdin, cwd=cwd or self.repo, env=env or self.env, capture_output=True, text=True, timeout=30)

    def line(self, sha, dst="main", remote=None):
        return ("refs/heads/x", sha, "refs/heads/" + dst, remote or self.base)

    def refused(self, url, lines, hook=HOOK, env=None):
        r = self.run_hook(url, lines, hook, env)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (3, "", LINE), (url, lines))

    def passed(self, url, lines, hook=HOOK, env=None):
        r = self.run_hook(url, lines, hook, env)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), (url, lines))

    # S2
    def check_s2(self, hook=HOOK):
        self.passed(NET, [self.line(self.good)], hook)   # the land form
        for what, lines in (
            ("no trailer", [self.line(self.none)]),
            ("an empty trailer", [self.line(self.empty)]),
            ("not recorded in mixed case", [self.line(self.notrec)]),
            ("a trailer in the body", [self.line(self.forged)]),
            ("an empty duplicate", [self.line(self.dupempty)]),
            ("a short object name", [self.line(self.good[:12])]),
            ("a tag object", [self.line(self.tagobj)]),
            ("a non-ancestor remote object", [self.line(self.good, remote=self.sidec)]),
            ("a deletion", [self.line(ZERO)]),
            ("a second ref that is bad", [self.line(self.good), self.line(self.none)]),
        ):
            r = self.run_hook(NET, lines, hook)
            if r.returncode != 3:
                self.fail("%s: %s" % (ANCHOR, what))
            self.assertEqual((r.stdout, r.stderr), ("", LINE), what)

    def test_s2_the_land_form_passes_and_the_other_forms_are_refused(self):
        self.check_s2()

    def test_new_remote_branch_zeros_pass_only_with_the_record(self):
        self.passed(NET, [self.line(self.good, remote=ZERO)])
        self.refused(NET, [self.line(self.none, remote=ZERO)])
        self.refused(NET, [self.line(self.good, remote="1" * 40)])   # a remote object this repository does not have

    # S9
    def test_s9_a_mutant_without_the_trailer_check_fails_with_the_anchored_text(self):
        self.check_s2()   # control first
        text = read(HOOK)
        self.assertEqual(text.count(CHECK), 1)
        p = os.path.join(self.d, "mutant")
        with open(p, "w") as f:
            f.write(text.replace(CHECK, "    :\n"))
        with self.assertRaises(AssertionError) as cm:
            self.check_s2(p)
        self.assertIn(ANCHOR, str(cm.exception))

    # S3
    def test_s3_network_urls_are_refused_and_local_ones_and_feature_branches_pass(self):
        bare = os.path.join(self.d, "bare.git")
        for url in ("https://example.invalid/r.git", "http://example.invalid/r.git", "ssh://git@example.invalid/r.git", "git://example.invalid/r.git",
                    "git@example.invalid:o/r.git", "example.invalid:o/r.git"):
            self.refused(url, [self.line(self.none)])
            self.passed(url, [self.line(self.none, dst="feature-x")])
        for url in (bare, "file://" + bare, "./rel.git", "../rel.git", "rel.git"):
            self.passed(url, [self.line(self.none)])
            self.passed(url, [self.line(ZERO)])
            self.passed(url, [self.line(self.none, dst="feature-x")])

    def test_other_ref_kinds_and_default_names(self):
        self.passed(NET, [("refs/tags/v1", self.none, "refs/tags/v1", ZERO)])
        self.passed(NET, [self.line(self.none, dst="trunk")])   # trunk is not a default here
        self.refused(NET, [self.line(self.none, dst="trunk")], env=dict(self.env, CW_DEFAULT_BRANCH="trunk"))
        self.refused(NET, [self.line(self.none, dst="master")])
        self.passed(NET, [])

    # S4
    def test_s4_the_repositorys_own_hook_runs_after_the_rule(self):
        hooks = os.path.join(self.repo, ".git", "hooks")
        own = os.path.join(hooks, "pre-push")
        try:
            for code in (5, 0):
                with open(own, "w") as f:
                    f.write("#!/bin/sh\ncat > %s\necho \"$1 $2\" >> %s\nexit %d\n" % (os.path.join(self.d, "in.txt"), os.path.join(self.d, "in.txt"), code))
                os.chmod(own, 0o755)
                if os.path.exists(os.path.join(self.d, "in.txt")):
                    os.remove(os.path.join(self.d, "in.txt"))
                r = self.run_hook(NET, [self.line(self.good)])
                self.assertEqual(r.returncode, code)
                got = read(os.path.join(self.d, "in.txt"))
                self.assertIn("refs/heads/x %s refs/heads/main %s" % (self.good, self.base), got)   # the same input and arguments
                self.assertIn("origin " + NET, got)
            os.remove(os.path.join(self.d, "in.txt"))
            self.refused(NET, [self.line(self.none)])   # a refused push never runs it
            self.assertFalse(os.path.exists(os.path.join(self.d, "in.txt")))
            os.chmod(own, 0o644)   # not executable: not run
            self.passed(NET, [self.line(self.good)])
        finally:
            if os.path.exists(own):
                os.remove(own)

    def test_the_hook_is_executable_and_names_no_other_tool(self):
        self.assertTrue(os.access(HOOK, os.X_OK))
        self.assertTrue(read(HOOK).startswith("#!/bin/sh\n"))

    # S5 and S6: real git. Git connects to the remote and reads its refs BEFORE it runs the hook (it needs the remote object names), so a
    # URL nothing listens on ends with git's connection error and the hook never runs; the refusal is shown against a remote that answers:
    # an ssh URL whose "ssh" is a fake that runs the receive side locally (a network URL for the hook, no network).
    def real(self, *a, cwd=None):
        return subprocess.run(["git"] + list(a), cwd=cwd or self.scratch, env=dict(self.env, GIT_SSH_COMMAND=self.fakessh), capture_output=True, text=True, timeout=60)

    def setUp(self):
        name = self.id().split(".")[-1]
        self.scratch = os.path.join(self.d, "scratch-" + name)
        shutil.copytree(self.repo, self.scratch)
        self.git("config", "core.hooksPath", HOOKDIR, cwd=self.scratch)
        self.bare = os.path.join(self.d, "bare-" + name + ".git")
        subprocess.run(["git", "init", "-q", "--bare", self.bare], check=True, env=self.env)
        self.git("push", "-q", self.bare, self.base + ":refs/heads/main", cwd=self.scratch, env=dict(self.env))
        self.fakessh = os.path.join(self.d, "fakessh")
        with open(self.fakessh, "w") as f:
            f.write('#!/bin/sh\nfor last; do :; done\nexec sh -c "$last"\n')
        os.chmod(self.fakessh, 0o755)
        self.sshurl = "ssh://git@127.0.0.1" + self.bare
        self.git("checkout", "-q", "-b", "work", self.none, cwd=self.scratch)

    def git_push(self, remote, spec):
        return self.real("push", remote, spec)

    def bare_main(self):
        return self.git("rev-parse", "refs/heads/main", cwd=self.bare)

    def test_s5_real_git_refuses_the_unapproved_default_branch_push_and_lets_the_approved_form_through(self):
        self.git("remote", "add", "net", self.sshurl, cwd=self.scratch)
        r = self.git_push("net", self.none + ":refs/heads/main")
        self.assertIn(LINE, r.stderr)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.bare_main(), self.base)   # nothing landed
        r = self.git_push("net", self.good + ":refs/heads/main")
        self.assertNotIn("refused:", r.stderr)
        self.assertEqual((r.returncode, self.bare_main()), (0, self.good), r.stderr)
        r = self.git_push("net", self.none + ":refs/heads/feature-x")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_s5_an_https_url_nothing_listens_on_ends_with_the_connection_error(self):
        self.git("remote", "add", "web", NET, cwd=self.scratch)
        r = self.git_push("web", self.none + ":refs/heads/main")
        self.assertRegex(r.stderr, r"(?i)127\.0\.0\.1")
        self.assertNotIn("refused:", r.stderr)   # git failed to connect before it called the hook

    def test_s6_insteadof_and_pushurl_are_seen_by_the_hook(self):
        # a network name rewritten to a local path: the hook sees the local path and the push lands
        self.git("config", "url.%s.insteadOf" % self.bare, "https://rewritten.invalid/r.git", cwd=self.scratch)
        self.git("remote", "add", "re", "https://rewritten.invalid/r.git", cwd=self.scratch)
        r = self.git_push("re", self.none + ":refs/heads/main")
        self.assertEqual((r.returncode, self.bare_main()), (0, self.none), r.stderr)
        self.git("update-ref", "refs/heads/main", self.base, self.none, cwd=self.bare)
        # a local path rewritten to an ssh URL of the same repository: the hook sees the ssh URL and refuses
        self.git("config", "--unset-all", "url.%s.insteadOf" % self.bare, cwd=self.scratch)
        self.git("config", "url.%s.insteadOf" % self.sshurl, self.bare, cwd=self.scratch)
        self.git("remote", "add", "loc", self.bare, cwd=self.scratch)
        r = self.git_push("loc", self.none + ":refs/heads/main")
        self.assertIn(LINE, r.stderr)
        self.assertEqual(self.bare_main(), self.base)
        # a push URL that is ssh though the fetch URL is local
        self.git("config", "--unset-all", "url.%s.insteadOf" % self.sshurl, cwd=self.scratch)
        self.git("config", "remote.loc.pushurl", self.sshurl, cwd=self.scratch)
        r = self.git_push("loc", self.none + ":refs/heads/main")
        self.assertIn(LINE, r.stderr)
        r = self.git_push("loc", self.none + ":refs/heads/feature-x")
        self.assertEqual(r.returncode, 0, r.stderr)

    # the files around the hook
    def test_the_image_and_entrypoint_register_the_hook_and_the_permission_file_holds_no_bypass_rows(self):
        d = read(os.path.join(WORKER, "Dockerfile"))
        self.assertIn("COPY worker/pod-hooks/pre-push /usr/local/lib/cw-hooks/pre-push", d)
        self.assertIn("chown -R root:root /usr/local/lib/cw-hooks && chmod 0755 /usr/local/lib/cw-hooks /usr/local/lib/cw-hooks/pre-push", d)
        e = read(os.path.join(WORKER, "entrypoint.sh")).splitlines()
        i = [n for n, l in enumerate(e) if "git config --global credential.helper" in l]
        self.assertEqual(len(i), 1)
        self.assertTrue(e[i[0] + 1].startswith("  git config --global core.hooksPath /usr/local/lib/cw-hooks"))
        conf = read(os.path.join(WORKER, "opencode-worker.json"))
        for row in ('"git*push*--no-verif*": "deny"', '"*push*--no-verif*": "deny"', '"*core.hooksPath*": "deny"', '"*hooksPath=*": "deny"'):
            self.assertEqual(conf.count(row), 0, row)   # ask #2402: the permission file holds no shell rows

    def test_there_is_no_wrapper_to_parse_a_push(self):
        self.assertFalse(os.path.exists(os.path.join(WORKER, "guard.sh")))   # the guard layer was removed (ask #2402); the hook is the only push rule left
        self.assertIn("pre-push", read(README))


if __name__ == "__main__":
    unittest.main()
