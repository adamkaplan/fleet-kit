"""Ask #1142: the pod's pre-stop hook (cw-prestop / cw-end-hook --prestop). Fake cluster = run the rendered spec's own command."""
import argparse, datetime as dt, importlib.machinery, importlib.util, os, re, shutil, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402
import end_hook as cw_end_hook  # noqa: E402

LAUNCHER = os.path.dirname(HERE)
PRESTOP = os.path.join(os.path.dirname(LAUNCHER), "worker", "cw-prestop")
HOOK = os.path.join(LAUNCHER, "end_hook.py")


def render(stack=False):
    a = argparse.Namespace(image="img", repo="acme-org/widget-service", branch="main", shape="M", idle="30m",
                           max_hard=None, run_id=None, pull_secret=None, stack=stack)
    now = dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc)
    return cw.render_pod(a, "w1", "w1-abcd", now, now + dt.timedelta(hours=1), {"CW_TOKEN_ENDPOINT": "http://t"})


REPO = "acme-org/widget-service"
URL = "https://github.com/%s.git" % REPO
GIT = "/usr/bin/git"   # the real git: the pod shell's PATH wrapper is not part of the test


def sh(*a, cwd=None, env=None):
    a = (GIT,) + a[1:] if a[0] == "git" else a
    return subprocess.run(a, cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        # the fake remote lives where the host constant's test value points: <t>/<owner>/<name>.git (see seam())
        self.origin, self.work, self.bin = (os.path.join(self.t, n) for n in (REPO + ".git", "repo", "bin"))
        self.evil = os.path.join(self.t, "other-remote", REPO + ".git")   # a second remote the agent would like the push to land in
        os.makedirs(self.bin)
        os.makedirs(os.path.dirname(self.origin))
        os.makedirs(os.path.dirname(self.evil))
        self.helper_dir = os.path.join(self.t, "helper")
        self.helper = os.path.join(self.helper_dir, "git-credential-cw")
        os.makedirs(self.helper_dir)
        # a fake pod helper that records every call; the password is a fake assembled here
        open(self.helper, "w").write("#!/bin/sh\ncat >> %s/helper.log\necho \"$1\" >> %s/helper.log\nprintf 'username=x\\npassword=%s\\n'\n"
                                     % (self.t, self.t, "pw-" + "fake"))
        os.chmod(self.helper, 0o755)
        self.opts = " ".join(cw_end_hook.safe_opts(self.helper))
        self.push_url = "file://%s/%s.git" % (self.t, REPO)
        self.refspec = "--no-verify %s HEAD:refs/heads/w1/wip" % self.push_url
        sh("git", "init", "-q", "--bare", "-b", "main", self.evil)
        sh("git", "init", "-q", "--bare", "-b", "main", self.origin)
        sh("git", "clone", "-q", self.origin, self.work)
        for k, v in (("user.name", "t"), ("user.email", "t@invalid")):
            sh("git", "-C", self.work, "config", k, v)
        open(os.path.join(self.work, "a.txt"), "w").write("a")
        sh("git", "-C", self.work, "add", "-A")
        sh("git", "-C", self.work, "commit", "-qm", "base")
        sh("git", "-C", self.work, "push", "-q", "origin", "HEAD:main")
        # the hook is installed under the names the image gives it
        # origin is named like the pod's repository; the hook never uses it (it pushes to the explicit address, which the seam points at the bare repo)
        sh("git", "-C", self.work, "remote", "set-url", "origin", URL)
        shutil.copy(self.seam(HOOK), os.path.join(self.bin, "cw-end-hook"))
        self.out = os.path.join(self.t, "out")
        # macOS has no GNU timeout: a small stand-in at the front of PATH ('timeout [-k N] SECONDS cmd args' runs cmd args).
        # CW_TEST_FAKE_TIMEOUT=1 forces the stand-in even where GNU timeout exists.
        self.fake_timeout = bool(os.environ.get("CW_TEST_FAKE_TIMEOUT")) or shutil.which("timeout") is None
        if self.fake_timeout:
            fake = os.path.join(self.bin, "timeout")
            open(fake, "w").write('#!/bin/sh\nif [ "$1" = -k ]; then shift 2; fi\nshift\nexec "$@"\n')
            os.chmod(fake, 0o755)

    def seam(self, hook, helper_dir=None, git_bin=None):
        """TEST-ONLY SEAM (documented once, here): a launcher script that imports the hook module from `hook` and patches its host constant
        to a file:// prefix and its helper directory to the fake one. The real hook reads neither from the environment, so the agent cannot."""
        n = len([f for f in os.listdir(self.t) if f.startswith("seam-")])
        path = os.path.join(self.t, "seam-%d.py" % n)
        open(path, "w").write(
            "#!%s\nimport importlib.util, importlib.machinery, sys\nsp = importlib.util.spec_from_loader('end_hook_under_test', importlib.machinery.SourceFileLoader('end_hook_under_test', %r))\n"
            "m = importlib.util.module_from_spec(sp)\nsp.loader.exec_module(m)\nm.HOST_URL = %r\nm.HELPER_DIR = %r\n"
            "import os\nif os.path.exists(%r):\n    m.GIT_BIN = %r\n"   # the logging fake git of a test, when the test made one
            "if %r:\n    m.GIT_BIN = %r\nsys.exit(m.main())\n"
            % (sys.executable, hook, "file://%s/" % self.t, helper_dir or self.helper_dir,
               os.path.join(self.bin, "git"), os.path.join(self.bin, "git"), git_bin, git_bin))
        os.chmod(path, 0o755)
        return path

    def env(self, **kw):
        e = dict(os.environ, PATH=self.bin + ":/usr/bin:/bin", CW_WORKER="w1", CW_WORKDIR=self.work,
                 CW_REPO=REPO, HOME=self.t, CW_PRESTOP_OUT=self.out)
        e.update(kw)
        return e

    def hook(self, budget="20", **kw):
        p = subprocess.run(["sh", PRESTOP, budget], env=self.env(**kw), capture_output=True, text=True, timeout=30)
        p.stdout += open(self.out).read() if os.path.exists(self.out) else ""
        return p

    def dirty(self, name="work.txt"):
        open(os.path.join(self.work, name), "w").write("x")

    def wip(self):
        p = subprocess.run([GIT, "-C", self.origin, "rev-parse", "--verify", "-q", "refs/heads/w1/wip"], capture_output=True, text=True)
        return p.stdout.strip()

    def fake_git(self, body):
        real = GIT
        path = os.path.join(self.bin, "git")
        open(path, "w").write("#!/bin/sh\n" + body.replace("REAL", real))
        os.chmod(path, 0o755)


class Hook(Base):
    def test_pushes_wip_with_a_plain_push_and_leaves_secret_files_out(self):
        self.dirty(); self.dirty(".env")
        self.fake_git('echo "$@" >> %s/calls\nexec REAL "$@"\n' % self.t)
        p = self.hook()
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.wip(), sh("git", "-C", self.work, "rev-parse", "HEAD"))
        self.assertNotIn(".env", sh("git", "-C", self.origin, "ls-tree", "-r", "--name-only", "w1/wip"))
        pushes = [l for l in open(os.path.join(self.t, "calls")) if " push " in " " + l]
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0].strip(), "-C %s %s push %s" % (self.work, self.opts, self.refspec))   # exact: no force, no lease, no other refspec

    def test_clean_tree_or_no_repository_does_nothing(self):
        self.fake_git('echo "$@" >> %s/calls\nexec REAL "$@"\n' % self.t)
        p = self.hook()
        self.assertEqual((p.returncode, p.stdout + p.stderr), (0, ""))
        self.assertFalse([l for l in open(os.path.join(self.t, "calls")) if " push " in " " + l])
        p = self.hook(CW_WORKDIR=os.path.join(self.t, "nothing"))   # no repository: one line now (see EarlyReturns), never a push
        self.assertEqual(p.returncode, 0)
        self.assertEqual(len(p.stdout.strip().splitlines()), 1)

    def test_hanging_push_gives_up_inside_the_budget_with_one_line(self):
        if self.fake_timeout:
            self.skipTest("'timed out' comes from GNU timeout, which is absent (or hidden by CW_TEST_FAKE_TIMEOUT) here")
        self.dirty()
        self.fake_git('case "$*" in *" push "*|"push "*) exec sleep 300;; esac\nexec REAL "$@"\n')
        t0 = time.time()
        p = self.hook(budget="2")
        self.assertLess(time.time() - t0, 6)
        out = (p.stdout + p.stderr).strip().splitlines()
        self.assertEqual((p.returncode, len(out)), (0, 1))
        self.assertIn("timed out", out[0])
        self.assertTrue(os.path.exists(os.path.join(self.work, "work.txt")))

    def test_failure_is_one_line_without_the_url_and_work_stays(self):
        self.dirty()
        self.fake_git('case "$*" in *" push "*) echo "fatal: 401 https://user:tok@github.com/x" >&2; exit 1;; esac\nexec REAL "$@"\n')
        p = self.hook()
        out = (p.stdout + p.stderr).strip().splitlines()
        self.assertEqual((p.returncode, len(out)), (0, 1))
        self.assertNotIn("github.com", out[0])
        self.assertTrue(os.path.exists(os.path.join(self.work, "work.txt")))

    def test_diverged_wip_branch_is_never_overwritten(self):
        other = os.path.join(self.t, "other")
        sh("git", "clone", "-q", self.origin, other)
        sh("git", "-C", other, "-c", "user.name=o", "-c", "user.email=o@invalid", "commit", "-q", "--allow-empty", "-m", "theirs")
        sh("git", "-C", other, "push", "-q", "origin", "HEAD:refs/heads/w1/wip")
        before = self.wip()
        self.dirty()
        p = self.hook()
        self.assertEqual(self.wip(), before)
        self.assertEqual(len((p.stdout + p.stderr).strip().splitlines()), 1)

    def test_a_planted_token_never_reaches_the_output_and_canaries_are_not_read(self):
        tok = "tk-" + "planted" + "-value"
        self.dirty()
        canary = os.path.join(self.t, "server-password")
        open(canary, "w").write("x"); os.chmod(canary, 0)
        p = self.hook(CW_TOKEN=tok)
        self.assertNotIn(tok, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("ermission", p.stdout + p.stderr)


def mutant_hook(dst, how):
    """A copy of the hook that takes the branch from an argument or an environment variable and does not validate."""
    s = open(HOOK).read()
    old = 'os.environ.get("CW_WORKER", "")\n    if not valid_worker(worker):'
    assert old in s
    new = {"arg": '(sys.argv[2:3] or [os.environ.get("CW_WORKER", "")])[0]\n    if False:',
           "env": 'os.environ.get("CW_WIP_NAME") or os.environ.get("CW_WORKER", "")\n    if False:'}[how]
    open(dst, "w").write(s.replace(old, new))


class Refspec(Base):
    """The pushed branch is <own name>/wip only: not from an argument, not from a variable the coder's shell can write."""

    def setUp(self):
        super().setUp()
        self.calls = os.path.join(self.t, "calls")
        self.fake_git('echo "$@" >> %s\nexec REAL "$@"\n' % self.calls)

    def pushes(self):
        if not os.path.exists(self.calls):
            return []
        return [l.strip() for l in open(self.calls) if " push " in " " + l]

    def run_hook(self, hook, args=(), **kw):
        return subprocess.run([self.seam(hook), "--prestop", *args], env=self.env(**kw), capture_output=True, text=True, timeout=30)

    def attacks(self, hook):
        """Every attempt names another branch; whatever is pushed must be exactly the pod's own."""
        for args, kw in ((("main",), {}), (("other/wip",), {}), (("--force", "main"), {}),
                         ((), {"CW_WIP_NAME": "other/wip"}), ((), {"CW_BRANCH": "main", "CW_WIP_BRANCH": "main", "CW_PRESTOP_BRANCH": "other/wip"}),
                         (("main",), {"CW_WIP_NAME": "main"})):
            self.dirty()
            if os.path.exists(self.calls):
                os.remove(self.calls)
            self.run_hook(hook, args, **kw)
            pushed = self.pushes()
            self.assertEqual(len(pushed), 1, (args, kw, pushed))
            self.assertEqual(pushed[0], "-C %s %s push %s" % (self.work, self.opts, self.refspec), (args, kw))
            self.assertEqual(sh("git", "-C", self.origin, "for-each-ref", "--format=%(refname)"), "refs/heads/main\nrefs/heads/w1/wip")
            sh("git", "-C", self.origin, "update-ref", "-d", "refs/heads/w1/wip")
            sh("git", "-C", self.work, "reset", "-q", "--hard", "origin/main")

    def malformed(self, hook):
        for bad in ("a/b", "w1/wip", "a b", "-x", "--force", "..", "a..b", "a/../main", "w1\nmain", "main:main"):
            self.dirty()
            if os.path.exists(self.calls):
                os.remove(self.calls)
            self.run_hook(hook, CW_WORKER=bad)
            self.assertEqual(self.pushes(), [], bad)
            self.assertEqual(sh("git", "-C", self.origin, "for-each-ref", "--format=%(refname)"), "refs/heads/main", bad)

    def test_other_branches_named_by_argument_or_environment_are_never_pushed(self):
        self.attacks(HOOK)

    def test_a_malformed_own_name_is_refused_with_no_push(self):
        self.malformed(HOOK)
        p = self.run_hook(HOOK, CW_WORKER="a/b")
        self.assertEqual(p.returncode, 0)
        self.assertIn("not pushed", p.stdout)

    def test_the_shells_environment_cannot_choose_the_name_through_cw_prestop_either(self):
        self.dirty()
        p = self.hook(CW_WIP_NAME="other/wip", CW_BRANCH="main")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.pushes(), ["-C %s %s push %s" % (self.work, self.opts, self.refspec)])

    def test_a_mutant_taking_the_branch_from_an_argument_or_variable_fails_these_tests(self):
        for how in ("arg", "env"):
            mut = os.path.join(self.t, "mutant-" + how)
            mutant_hook(mut, how)
            for check in (self.attacks, self.malformed):
                with self.subTest(how=how, check=check.__name__):
                    with self.assertRaises(AssertionError):
                        check(mut)
                    for r in sh("git", "-C", self.origin, "for-each-ref", "--format=%(refname)").split():
                        if r != "refs/heads/main":
                            sh("git", "-C", self.origin, "update-ref", "-d", r)
                    sh("git", "-C", self.work, "reset", "-q", "--hard", "origin/main")
                    sh("git", "-C", self.work, "clean", "-qfd")


def mutate(dst, old, new):
    s = open(HOOK).read()
    assert old in s, old
    open(dst, "w").write(s.replace(old, new))


class Hostile(Base):
    """The agent controls the repository's configuration and hooks: none of it may run or choose the push target."""

    def setUp(self):
        super().setUp()
        self.marker = os.path.join(self.t, "marker")
        self.calls = os.path.join(self.t, "calls")
        script = os.path.join(self.t, "evil")
        open(script, "w").write("#!/bin/sh\necho ran >> %s\nexit 1\n" % self.marker)
        os.chmod(script, 0o755)
        open(os.path.join(self.work, ".gitattributes"), "w").write("*.txt diff=evil\n")
        sh("git", "-C", self.work, "add", ".gitattributes"); sh("git", "-C", self.work, "commit", "-qm", "attr")
        sh("git", "-C", self.work, "push", "-q", self.origin, "HEAD:refs/heads/main")
        hooks = os.path.join(self.work, ".git", "hooks")
        for h in ("pre-commit", "pre-push", "post-commit", "commit-msg", "reference-transaction"):
            shutil.copy(script, os.path.join(hooks, h))
        # hostile CONFIGURATION keys are refused outright now (see HostileConfig); this class keeps the hostile hooks and attributes files
        self.hostile_keys = (("core.sshCommand", script), ("core.fsmonitor", script), ("diff.evil.textconv", script),
                             ("remote.origin.pushurl", "https://github.com/other-org/elsewhere.git"),
                             ("url.https://github.com/other-org/.insteadOf", "https://github.com/acme-org/"))
        for f in ("marker", "calls"):
            if os.path.exists(os.path.join(self.t, f)):
                os.remove(os.path.join(self.t, f))
        self.fake_git('echo "$@" >> %s\nexec REAL "$@"\n' % self.calls)

    def pushes(self):
        return [l.strip() for l in open(self.calls) if " push " in " " + l] if os.path.exists(self.calls) else []

    def run_hook(self, hook):
        return subprocess.run([self.seam(hook), "--prestop"], env=self.env(), capture_output=True, text=True, timeout=30)

    def good(self, hook):
        """Targets the explicit URL only, with every option present, and no agent command ran."""
        self.dirty()
        p = self.run_hook(hook)
        self.assertFalse(os.path.exists(self.marker), "an agent hook or command ran")
        pushes = self.pushes()
        self.assertEqual(pushes, ["-C %s %s push %s" % (self.work, self.opts, self.refspec)], p.stdout)
        self.assertIn("-c core.hooksPath=/dev/null", pushes[0])
        self.assertNotIn("other-org", pushes[0])

    def test_hostile_repository_runs_nothing_and_pushes_to_the_explicit_url(self):
        # the hostile part left in the repository is its hooks and attributes (configuration keys are refused: next tests)
        self.good(HOOK)
        self.assertEqual(self.wip(), sh("git", "-C", self.work, "rev-parse", "HEAD"))

    def test_insteadof_in_the_repository_for_our_url_pushes_nothing(self):
        for k, v in self.hostile_keys:
            sh("git", "-C", self.work, "config", k, v)
        self.dirty()
        p = self.run_hook(HOOK)
        self.assertEqual(self.pushes(), [])
        self.assertEqual(self.wip(), "")
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(p.stdout.strip(), "cw-prestop: not pushed (the repository configuration is not a plain one: core.sshcommand); work left in %s and is LOST when the pod ends; unpushed files: work.txt" % self.work)

    def test_origin_pointing_elsewhere_pushes_nothing_and_prints_one_line(self):
        for url in ("https://github.com/other-org/elsewhere.git", "git@github.com:other-org/widget-service.git"):
            sh("git", "-C", self.work, "config", "remote.origin.url", url)
            self.dirty()
            p = self.run_hook(HOOK)
            self.assertEqual(self.pushes(), [])
            self.assertEqual(self.wip(), "")
            self.assertEqual(p.stdout.strip(), "cw-prestop: not pushed (origin points elsewhere); work left in %s and is LOST when the pod ends; unpushed files: work.txt" % self.work)
        sh("git", "-C", self.work, "config", "remote.origin.url", "git@github.com:ACME-ORG/Widget-Service")
        self.good(HOOK)   # same repository after normalising case, scp form and .git

    def test_mutants_are_each_caught(self):
        base = sh("git", "-C", self.work, "rev-parse", "HEAD")
        muts = {"hooksPath": ('"core.hooksPath=/dev/null", ', ""),
                "no-verify": ('"--no-verify", url', "url"),
                "pushurl": ('git(workdir, "push", "--no-verify", url,', 'git(workdir, "push", "--no-verify", "origin",'),
                "origin-check": ('if key in ("remote.origin.url", "remote.origin.pushurl") and norm_repo(value) != repo.lower():', "if False:")}
        for name, (old, new) in muts.items():
            mut = os.path.join(self.t, "mut-" + name)
            mutate(mut, old, new)
            self.assertNotEqual(open(mut).read(), open(HOOK).read(), name)   # the mutation applied
            with self.subTest(mutant=name):
                if name == "origin-check":
                    sh("git", "-C", self.work, "config", "remote.origin.url", "https://github.com/other-org/elsewhere.git")
                    self.dirty()
                    r = self.run_hook(mut)
                    self.assertTrue(self.pushes(), "the mutant should have pushed: " + r.stdout + r.stderr)
                    with self.assertRaises(AssertionError):
                        self.assertEqual(self.pushes(), [])
                else:
                    with self.assertRaises(AssertionError):
                        self.good(mut)
            for f in (self.calls, self.marker):
                if os.path.exists(f):
                    os.remove(f)
            safe = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
            sh("git", "-C", self.work, *safe, "reset", "-q", "--hard", base)
            sh("git", "-C", self.origin, "update-ref", "-d", "refs/heads/w1/wip") if self.wip() else None
            sh("git", "-C", self.work, *safe, "clean", "-qfd")


# GRAMMAR TABLE: every class of git configuration key that can redirect a remote, run a program, change what is sent or to whom, or include
# another file. {HOST} is the host prefix, {OTHER} a second remote, {SCRIPT} a marker-writing script, {INC} a file with a hostile url rule.
HOSTILE_URL = "https://other.example/x/y.git"
CFG_G, CFG_S = "GIT_CONFIG_" + "GLOBAL", "GIT_CONFIG_" + "SYSTEM"
GRAMMAR = {
    "redirect a remote": [
        ("remote.origin.url", HOSTILE_URL), ("remote.origin.pushurl", HOSTILE_URL), ("remote.evil.url", HOSTILE_URL),
        ("remote.evil.pushurl", HOSTILE_URL), ("url.{OTHER}.insteadOf", "{HOST}"), ("url.{OTHER}.pushInsteadOf", "{HOST}"),
        ("branch.main.remote", "evil"), ("branch.main.pushRemote", "evil"), ("remote.pushDefault", "evil"), ("push.default", "current")],
    "run a program": [
        ("core.hooksPath", "{SCRIPT}"), ("core.sshCommand", "{SCRIPT}"), ("core.fsmonitor", "{SCRIPT}"), ("core.askPass", "{SCRIPT}"),
        ("core.gitProxy", "{SCRIPT}"), ("core.pager", "{SCRIPT}"), ("core.editor", "{SCRIPT}"), ("credential.helper", "!{SCRIPT}"),
        ("credential.https://github.com.helper", "!{SCRIPT}"), ("filter.evil.clean", "{SCRIPT}"), ("filter.evil.smudge", "{SCRIPT}"),
        ("filter.evil.process", "{SCRIPT}"), ("diff.evil.textconv", "{SCRIPT}"), ("diff.evil.command", "{SCRIPT}"),
        ("merge.evil.driver", "{SCRIPT}"), ("gpg.program", "{SCRIPT}"), ("sendemail.smtpServer", "{SCRIPT}"), ("alias.push", "!{SCRIPT}"),
        ("protocol.ext.allow", "always"), ("protocol.allow", "always"), ("uploadpack.packObjectsHook", "{SCRIPT}"),
        ("remote.origin.receivepack", "{SCRIPT}"), ("remote.origin.uploadpack", "{SCRIPT}"),
        ("gc.recentObjectsHook", "{SCRIPT}"), ("gc.cruftPacks", "true"), ("gc.auto", "1"), ("pack.packSizeLimit", "1"),
        ("gc.autoDetach", "false"), ("gc.pruneExpire", "now"), ("pack.threads", "1"), ("maintenance.auto", "true"),
        ("receive.autogc", "true"), ("branch.main.merge", "x")],
    "change what is sent or to whom": [
        ("http.proxy", "http://other.example:3128"), ("http.https://github.com/.proxy", "http://other.example:3128"),
        ("http.extraHeader", "X-Evil: 1"), ("http.https://github.com/.extraHeader", "X-Evil: 1"), ("http.followRedirects", "true"),
        ("http.sslCAInfo", "/tmp/evil-ca"), ("http.sslVerify", "false"), ("http.cookieFile", "/tmp/evil-cookies"),
        ("credential.useHttpPath", "false"), ("credential.username", "evil"), ("http.curloptResolve", "github.com:443:127.0.0.1"),
        ("http.proxyAuthMethod", "basic")],
    "include another file": [("include.path", "{INC}"), ("includeIf.gitdir:/.path", "{INC}")],
}
DANGEROUS_ENV = {"GIT_CONFIG": "/dev/null", "GIT_DIR": "/nonexistent", "GIT_WORK_TREE": "/nonexistent", "GIT_INDEX_FILE": "/nonexistent",
                 "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false", "GIT_SSH": "/bin/false", "GIT_SSH_COMMAND": "false",
                 "GIT_EXTERNAL_DIFF": "/bin/false", "GIT_PAGER": "false", "GIT_PROXY_COMMAND": "false", "all_proxy": "http://other.example",
                 "ALL_PROXY": "http://other.example", "http_proxy": "http://other.example", "HTTPS_PROXY": "http://other.example",
                 "https_proxy": "http://other.example", "XDG_CONFIG_HOME": "/tmp/nowhere", "GIT_TERMINAL_PROMPT": "1"}


class Grammar(Base):
    """Completeness of the allow-list against the grammar table, and the reader's named exploits, each with mutants."""

    def setUp(self):
        super().setUp()
        self.marker = os.path.join(self.t, "marker")
        self.calls = os.path.join(self.t, "calls")
        self.envlog = os.path.join(self.t, "envlog")
        self.script = os.path.join(self.t, "script")
        open(self.script, "w").write("#!/bin/sh\necho ran >> %s\nexit 1\n" % self.marker)
        os.chmod(self.script, 0o755)
        self.host = "file://%s/" % self.t
        self.other = "file://%s/other-remote/" % self.t
        self.inc = os.path.join(self.t, "included")
        open(self.inc, "w").write('[url "%s"]\n\tinsteadOf = %s\n\tpushInsteadOf = %s\n' % (self.other, self.host, self.host))
        self.cfg = os.path.join(self.work, ".git", "config")
        self.plain = open(self.cfg).read()
        self.base_head = sh("git", "-C", self.work, "rev-parse", "HEAD")
        self.fake_git('echo "$@" >> %s\n{ echo "ARGS $@"; env | sort; echo "HOMEFILES $(ls -A "$HOME" | wc -l)"; echo; } >> %s\nexec REAL "$@"\n'
                      % (self.calls, self.envlog))

    def fill(self, v):
        return v.replace("{HOST}", self.host).replace("{OTHER}", self.other).replace("{SCRIPT}", self.script).replace("{INC}", self.inc)

    def table(self):
        return [(cls, self.fill(k), self.fill(v)) for cls, rows in GRAMMAR.items() for k, v in rows]

    def calls_list(self):
        return [l.strip() for l in open(self.calls)] if os.path.exists(self.calls) else []

    def reset(self):
        for f in (self.calls, self.envlog, self.marker, os.path.join(self.t, "helper.log")):
            if os.path.exists(f):
                os.remove(f)
        open(self.cfg, "w").write(self.plain)
        for r in sh("git", "-C", self.origin, "for-each-ref", "--format=%(refname)").split():
            if r != "refs/heads/main":
                sh("git", "-C", self.origin, "update-ref", "-d", r)
        sh("git", "-C", self.work, "reset", "-q", "--hard", self.base_head)
        sh("git", "-C", self.work, "clean", "-qfd")

    def other_refs(self):
        return sh("git", "-C", self.evil, "for-each-ref", "--format=%(refname)")

    def go(self, hook, **kw):
        self.reset()
        self.dirty()
        return self.run_hook(hook, **kw)

    def run_hook(self, hook, **kw):
        return subprocess.run([self.seam(hook), "--prestop"], env=self.env(**kw), capture_output=True, text=True, timeout=30)

    def set_local(self, key, value):
        sh("git", "-C", self.work, "config", "--local", "--" + "add", key, value)

    def global_files(self, key, value):
        """The same key in the home file, the global-override file, and the variables: returns environment overrides."""
        home = os.path.join(self.t, "hostile-home")
        os.makedirs(home, exist_ok=True)
        gfile = os.path.join(home, ".git" + "config")
        for f in (gfile, os.path.join(home, "other")):
            open(f, "w").close()
            sh("git", "config", "-f", f, "--" + "add", key, value)
        return {"HOME": home, CFG_G: os.path.join(home, "other"), CFG_S: os.path.join(home, "other"),
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": key, "GIT_CONFIG_VALUE_0": value,
                "GIT_CONFIG_PARAMETERS": "'%s'='%s'" % (key, value)}

    def non_config_calls(self):
        # reading the configuration and listing the untracked file names (ls-files --others: no filter, no hook) are the only calls of a refusal
        return [c for c in self.calls_list() if " config --local " not in c and " ls-files --others " not in c]

    def snapshot(self):
        """argv of every call and the environment of every call without the per-run values."""
        env = [l for l in open(self.envlog).read().splitlines()
               if not l.startswith(("HOME=", "PWD=", "OLDPWD=", "SHLVL=", "_="))]
        return self.calls_list(), env

    # ---- completeness: each key of the table, three ways
    def test_each_key_in_the_repository_config_is_refused_with_nothing_run_or_sent(self):
        n = 0
        for cls, key, value in self.table():
            with self.subTest(cls=cls, key=key):
                self.reset()
                self.set_local(key, value)
                self.dirty()
                p = self.run_hook(HOOK)
                n += 1
                self.assertFalse(os.path.exists(self.marker), "a program of the repository ran")
                self.assertTrue(p.stdout.startswith("cw-prestop: not pushed ("), p.stdout + p.stderr)
                self.assertIn("work left in %s" % self.work, p.stdout)
                self.assertEqual(self.non_config_calls(), [], "git went further than reading the configuration")
                self.assertEqual(self.wip(), "")
                self.assertEqual(self.other_refs(), "")
                self.assertFalse(os.path.exists(os.path.join(self.t, "helper.log")), "the helper was called")
                self.assertTrue(os.path.exists(os.path.join(self.work, "work.txt")))
        self.assertGreater(n, 40)

    def test_the_same_keys_in_home_global_and_environment_change_nothing(self):
        p = self.go(HOOK)
        self.assertEqual(self.wip(), sh("git", "-C", self.work, "rev-parse", "HEAD"), p.stdout + p.stderr)
        base_argv, base_env = self.snapshot()
        for cls, key, value in self.table():
            with self.subTest(cls=cls, key=key):
                self.reset()
                self.dirty()
                p = self.run_hook(HOOK, **self.global_files(key, value))
                self.assertFalse(os.path.exists(self.marker), "a program ran")
                self.assertEqual(self.other_refs(), "", "the push landed in the other remote")
                self.assertEqual(self.wip(), sh("git", "-C", self.work, "rev-parse", "HEAD"), p.stdout + p.stderr)
                self.assertEqual(self.snapshot(), (base_argv, base_env))

    def test_the_environment_the_caller_hands_over_is_dropped_and_pinned_values_are_set(self):
        self.go(HOOK, **DANGEROUS_ENV)
        argv, env = self.snapshot()
        names = {l.split("=", 1)[0] for l in env if "=" in l}
        for bad in list(DANGEROUS_ENV) + ["GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"]:
            self.assertNotIn(bad, names - {"GIT_TERMINAL_PROMPT"})
        for want in (CFG_G + "=/dev/null", CFG_S + "=/dev/null", "GIT_CONFIG_NOSYSTEM=1", "GIT_TERMINAL_PROMPT=0"):
            self.assertIn(want, env)
        self.assertIn("HOMEFILES 0", env)   # an empty private home
        self.assertEqual(self.wip(), sh("git", "-C", self.work, "rev-parse", "HEAD"))

    # ---- the reader's named exploits and the argv pins; each scenario passes on the real hook and fails on its mutant
    def scenario_include(self, hook):   # (a)
        self.reset()
        self.set_local("include.path", self.inc)
        self.dirty()
        p = self.run_hook(hook)
        self.assertEqual(self.other_refs(), "", "the push landed in the other remote")
        self.assertIn("not a plain one: include.path", p.stdout)

    def scenario_local_insteadof(self, hook):
        self.reset()
        self.set_local("url.%s.insteadOf" % self.other, self.host)
        self.set_local("url.%s.pushInsteadOf" % self.other, self.host)
        self.dirty()
        self.run_hook(hook)
        self.assertEqual(self.other_refs(), "", "the push landed in the other remote")

    def scenario_global(self, hook):    # (b)
        self.reset()
        self.dirty()
        env = self.global_files("url.%s.insteadOf" % self.other, self.host)
        sh("git", "config", "-f", env[CFG_G], "url.%s.pushInsteadOf" % self.other, self.host)
        for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_PARAMETERS"):
            del env[k]
        self.run_hook(hook, **env)
        self.assertEqual(self.other_refs(), "", "the push landed in the other remote")
        self.assertEqual(self.wip(), sh("git", "-C", self.work, "rev-parse", "HEAD"))

    def scenario_variables(self, hook):
        self.reset()
        self.dirty()
        self.run_hook(hook, GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="url.%s.insteadOf" % self.other, GIT_CONFIG_VALUE_0=self.host,
                      GIT_CONFIG_KEY_1="url.%s.pushInsteadOf" % self.other, GIT_CONFIG_VALUE_1=self.host)
        self.assertEqual(self.other_refs(), "", "the push landed in the other remote")

    def scenario_credential(self, hook):   # (c)
        self.reset()
        self.set_local("credential.helper", "!" + self.script)
        self.dirty()
        p = self.run_hook(hook)
        self.assertFalse(os.path.exists(self.marker), "the repository's helper ran")
        self.assertFalse(os.path.exists(os.path.join(self.t, "helper.log")), "a helper received something")
        self.assertIn("not a plain one: credential.helper", p.stdout)

    def scenario_transport_keys(self, hook):   # (d)
        for key in ("http.proxy", "http.extraHeader", "core.gitProxy", "core.askPass"):
            self.reset()
            self.set_local(key, self.script)
            self.dirty()
            p = self.run_hook(hook)
            self.assertFalse(os.path.exists(self.marker))
            self.assertEqual((self.wip(), self.non_config_calls()), ("", []), key)
            self.assertIn("not a plain one: " + key.lower(), p.stdout)

    def scenario_pins(self, hook):
        self.go(hook)
        calls = self.non_config_calls()
        self.assertTrue(calls)
        for c in calls:
            self.assertTrue(c.startswith("-C %s %s " % (self.work, self.opts)), c)
        self.assertIn("-c credential.helper= -c credential.helper=%s " % self.helper, calls[0])
        pushes = [c for c in calls if " push " in c]
        self.assertEqual(pushes, ["-C %s %s push %s" % (self.work, self.opts, self.refspec)])

    SCENARIOS = ("include", "local_insteadof", "global", "variables", "credential", "transport_keys", "pins")

    def test_named_exploits_are_all_closed(self):
        for name in self.SCENARIOS:
            with self.subTest(scenario=name):
                getattr(self, "scenario_" + name)(HOOK)

    def test_mutants_are_each_caught(self):
        muts = {
            "allow-list removed": ("local_insteadof", [("if key not in ALLOWED_KEYS and not BRANCH_RE.match(key):", "if False:")]),
            "global config not pinned": ("global", [('"GIT_CONFIG_" + "GLOBAL": "/dev/null"', '"GIT_CONFIG_" + "GLOBAL": os.environ.get("GIT_CONFIG_" + "GLOBAL", "/dev/null")')]),
            "credential reset removed": ("pins", [('"credential.helper=", "credential.helper=" + helper,', '"credential.helper=" + helper,')]),
            "includes not followed": ("include", [('"--includes", ', ""), ('ALLOWED_KEYS = {', 'ALLOWED_KEYS = {"include.path", ')]),
            "environment not scrubbed": ("variables", [("if k in KEEP_ENV}", "if True}")]),
            "explicit URL replaced by origin": ("pins", [('git(workdir, "push", "--no-verify", url,', 'git(workdir, "push", "--no-verify", "origin",')]),
        }
        for name, (scenario, edits) in muts.items():
            mut = os.path.join(self.t, "mut-" + name.replace(" ", "-"))
            s = open(HOOK).read()
            for old, new in edits:
                self.assertIn(old, s, name)
                s = s.replace(old, new)
            self.assertNotEqual(s, open(HOOK).read())   # the mutation applied
            open(mut, "w").write(s)
            with self.subTest(mutant=name):
                with self.assertRaises(AssertionError):
                    getattr(self, "scenario_" + scenario)(mut)

    # ---- the seam and the helper
    def test_the_seam_is_not_reachable_from_the_environment(self):
        self.assertEqual(cw_end_hook.HOST_URL, "https://github.com/")
        self.assertEqual(cw_end_hook.HELPER_DIR, "/run/cw-tmp/bin")
        code = [l for l in open(HOOK).read().splitlines() if not l.lstrip().startswith("#")]
        for name in ("HOST_URL", "HELPER_DIR"):
            self.assertEqual([l for l in code if l.startswith(name + " =")], [l for l in code if l.startswith(name + " =")][:1])
            self.assertFalse([l for l in code if name in l and "environ" in l])
        self.assertEqual(len([l for l in code if "HOST_URL" in l]), 2)   # the definition and the one use
        self.assertFalse([l for l in code if "GIT_BIN" in l and "environ" in l])
        self.assertEqual(cw_end_hook.GIT_BIN, "/usr/bin/git" if os.path.exists("/usr/bin/git") else cw_end_hook.GIT_CANDIDATES[0])

    def test_without_the_pods_helper_nothing_is_pushed(self):
        self.reset()
        self.dirty()
        p = subprocess.run([self.seam(HOOK, helper_dir=os.path.join(self.t, "nowhere")), "--prestop"], env=self.env(), capture_output=True, text=True)
        self.assertIn("the pod's credential helper was not found", p.stdout)
        self.assertEqual((self.wip(), self.non_config_calls()), ("", []))
        self.assertEqual(len(p.stdout.strip().splitlines()), 1)



# THE INERT TABLE: every allow-listed key and why git's own documentation makes it inert (it names no command, no path to run and no URL
# other than the one compared to CW_REPO). git 2.43 documentation, git-config(1) and the sections named. A key whose value is a COMMAND,
# a PATH or a URL is out; remote.origin.url and remote.origin.pushurl are the only URL keys and the hook compares them to CW_REPO.
INERT = {
    "core.repositoryformatversion": ("0", "a number the repository format check reads"),
    "core.filemode": ("true", "a boolean: whether the executable bit is trusted"),
    "core.bare": ("false", "a boolean: whether the repository has a work tree"),
    "core.logallrefupdates": ("true", "a boolean: whether reflogs are written"),
    "core.ignorecase": ("false", "a boolean: case folding of file names"),
    "core.precomposeunicode": ("true", "a boolean: unicode normalisation of file names"),
    "core.symlinks": ("true", "a boolean: whether symbolic links are real"),
    "user.name": ("t", "a plain string written into commits"),
    "user.email": ("t@invalid", "a plain string written into commits"),
    "remote.origin.url": (URL, "the one URL key: compared to CW_REPO, and never used (the hook pushes to its own explicit URL)"),
    "remote.origin.pushurl": (URL, "the other URL key: compared to CW_REPO, and never used"),
    "remote.origin.fetch": ("+refs/heads/*:refs/remotes/origin/*", "a refspec: ANY value is harmless, the hook's fetch names its own refspec and URL"),
    "init.defaultbranch": ("main", "a branch name used only by git init"),
    "receive.denycurrentbranch": ("refuse", "a policy word read by a receiving repository, never by the hook"),
}
INERT_FORMS = {"branch.main.remote": "origin", "branch.main.merge": "refs/heads/main", "branch.a.b.remote": ".", "branch.cw/task.merge": "refs/heads/x"}
KEYS_FIXTURE = os.path.join(HERE, "fixtures", "git-2.43-config-keys.txt")   # `git help -c` of git 2.43.0, checked in as plain text


def instantiate(name):
    """A concrete key from a documentation key name: <placeholders> become x (remote names: evil, never origin)."""
    if name.startswith("remote.<name>."):
        name = name.replace("<name>", "evil", 1)
    return re.sub(r"<[^>]*>", "x", name).replace("*", "x")


class Completeness(Base):
    """The allow-list is CLOSED: every key git documents that is not in the inert table is refused, so a key added later is refused too."""

    def refusal(self, key, value):
        cfg = os.path.join(self.work, ".git", "config")
        with open(cfg, "w") as f:
            f.write(self.plain)
        try:
            sh("git", "-C", self.work, "config", "--local", "--" + "add", key, value)
        except subprocess.CalledProcessError:
            return True   # git itself cannot store this name (a documentation pattern such as gitweb.remote_heads): it can never be in a repository
        try:
            cw_end_hook.config_refusal(self.work, REPO, self.ctx)
        except (cw_end_hook.ConfigRefused, RuntimeError):   # RuntimeError: git cannot read the repository (core.worktree, extensions.*): nothing is pushed either
            return True
        return False

    def setUp(self):
        super().setUp()
        self.plain = open(os.path.join(self.work, ".git", "config")).read()
        self.ctx = cw_end_hook.Ctx(self.helper)
        self.addCleanup(self.ctx.close)
        self.ctx.env["PATH"] = "/usr/bin:/bin"

    def doc_keys(self):
        keys = [l.strip() for l in open(KEYS_FIXTURE) if l.strip() and not l.startswith("#")]
        live = subprocess.run([GIT, "help", "-c"], capture_output=True, text=True)
        if live.returncode == 0 and live.stdout.strip():
            keys += [l.strip() for l in live.stdout.splitlines() if l.strip()]   # a newer git in the image adds its own keys
        return sorted(set(keys))

    def test_the_inert_table_is_exactly_the_allow_list(self):
        self.assertEqual(set(INERT), set(cw_end_hook.ALLOWED_KEYS))
        for key, (_, why) in INERT.items():
            self.assertTrue(why and len(why) < 120, key)

    def test_every_inert_key_is_accepted(self):
        for key, (value, _) in INERT.items():
            with self.subTest(key=key):
                self.assertFalse(self.refusal(key, value))
        for key, value in INERT_FORMS.items():
            with self.subTest(key=key):
                self.assertFalse(self.refusal(key, value))

    def test_every_documented_key_not_in_the_table_is_refused(self):
        keys = self.doc_keys()
        self.assertGreater(len(keys), 800)
        refused = 0
        for name in keys:
            key = instantiate(name)
            if key.lower() in INERT or re.match(r"^branch\.[^.]+\.(remote|merge)$", key.lower()):
                continue
            with self.subTest(key=key):
                self.assertTrue(self.refusal(key, "1"), "an undocumented-as-inert key is accepted: " + key)
                refused += 1
        self.assertGreater(refused, 800)

    def test_the_branch_forms_are_exact(self):
        for key, value in (("branch.main.remote", "evil"), ("branch.main.remote", "https://other.example/x.git"), ("branch.main.merge", "x"),
                           ("branch.main.pushremote", "origin"), ("branch.main.rebase", "true"), ("branch.main.description", "x"),
                           ("branch.autosetupmerge", "true"), ("branch.main.remote.x", "origin")):
            with self.subTest(key=key, value=value):
                self.assertTrue(self.refusal(key, value))

    def write_mut(self, text):
        path = os.path.join(self.t, "mut_eh.py")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_a_wildcard_in_the_allow_list_is_caught(self):
        s = open(HOOK).read()
        for old, new in (("ALLOWED_KEYS = {", 'ALLOWED_KEYS = {"gc.recentobjectshook", '),):
            self.assertIn(old, s)
        mut = s.replace("ALLOWED_KEYS = {", 'ALLOWED_KEYS = {"gc.recentobjectshook", ')
        spec = importlib.util.spec_from_loader("mut_eh", importlib.machinery.SourceFileLoader("mut_eh", self.write_mut(mut)))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertNotEqual(set(INERT), set(m.ALLOWED_KEYS))   # the table test fails on any key added to the list


class EarlyReturns(Base):
    """Medium 1: every refusal and early return prints exactly ONE line with the stable prefix on stdout AND stderr, says the work is LOST,
    and (where git can be asked safely) lists the unpushed file names, at most 50."""

    def one_line(self, p, *needles):
        for stream in (p.stdout, p.stderr):
            lines = stream.strip().splitlines()
            self.assertEqual(len(lines), 1, (p.stdout, p.stderr))
            self.assertTrue(lines[0].startswith("cw-prestop: not pushed ("), lines[0])
            self.assertIn("is LOST when the pod ends", lines[0])
            for n in needles:
                self.assertIn(n, lines[0])
        self.assertEqual(p.stdout, p.stderr)
        self.assertEqual(self.wip(), "")

    def run_main(self, helper_dir=None, **kw):
        return subprocess.run([self.seam(HOOK, helper_dir=helper_dir), "--prestop"], env=self.env(**kw), capture_output=True, text=True)

    def test_each_early_return_prints_one_line(self):
        self.dirty()
        self.one_line(self.run_main(CW_WORKER="a/b"), "worker name")
        self.one_line(self.run_main(CW_REPO="o/r@evil"), "repository name", "work.txt")
        self.one_line(self.run_main(CW_REPO="../r"), "repository name")
        self.one_line(self.run_main(helper_dir=os.path.join(self.t, "nowhere")), "credential helper", "work.txt")
        self.one_line(self.run_main(CW_WORKDIR=os.path.join(self.t, "nothing")), "no git repository")
        sh("git", "-C", self.work, "config", "core.sshCommand", "false")
        self.one_line(self.run_main(), "not a plain one: core.sshcommand", "work.txt")

    def test_a_dot_git_file_is_refused_with_one_line(self):
        # a linked worktree or a separate git directory: .git is a FILE
        work = os.path.join(self.t, "linked")
        sh("git", "-C", self.work, "worktree", "add", "-q", "--detach", work)
        open(os.path.join(work, "w.txt"), "w").write("x")
        self.assertTrue(os.path.isfile(os.path.join(work, ".git")))
        p = self.run_main(CW_WORKDIR=work)
        self.one_line(p, ".git is a file: a linked worktree or a separate git directory is not supported); work left in %s" % work)
        self.assertNotIn("w.txt", p.stdout)   # git is never asked in such a directory

    def test_a_repository_name_starting_with_a_symbol_is_refused(self):
        self.dirty()
        for bad in ("-o/r", "o/-r", ".o/r", "o/.r", "_o/r", "o/_r"):
            with self.subTest(repo=bad):
                self.one_line(self.run_main(CW_REPO=bad), "repository name")
        self.assertTrue(cw_end_hook.REPO_RE.match("acme-org/widget-service"))
        self.assertTrue(cw_end_hook.REPO_RE.match("0rg/9.x_y-z"))

    def test_a_push_failure_prints_one_line_with_the_names(self):
        self.dirty()
        shutil.rmtree(self.origin)
        self.one_line(self.run_main(), "work.txt")

    def test_names_only_and_at_most_fifty(self):
        for i in range(60):
            open(os.path.join(self.work, "f%02d.txt" % i), "w").write("SECRET-CONTENT")
        sh("git", "-C", self.work, "config", "core.sshCommand", "false")
        p = self.run_main()
        self.one_line(p, "(+10 more)")
        self.assertNotIn("SECRET-CONTENT", p.stdout)
        self.assertEqual(p.stdout.count(".txt"), 50)

    def test_the_prefix_line_reaches_the_container_output_through_cw_prestop(self):
        self.dirty()
        sh("git", "-C", self.work, "config", "core.sshCommand", "false")
        p = self.hook()
        self.assertEqual(len(p.stdout.strip().splitlines()), 1)
        self.assertIn("LOST", p.stdout)

    def guard_on_path(self):
        # the pod's guard wrapper first on PATH: refuses a git call whose environment carries git configuration variables
        guard = os.path.join(self.bin, "git")
        with open(guard, "w") as f:
            f.write('#!/bin/sh\nenv | grep -q "^GIT_CONFIG_[A-Z]*=." && { echo "refused: git configuration from the environment" >&2; exit 3; }\nexec %s "$@"\n' % GIT)
        os.chmod(guard, 0o755)

    def test_the_hook_pushes_with_a_refusing_guard_wrapper_first_on_path(self):
        self.guard_on_path()
        self.dirty()
        p = subprocess.run([self.seam(HOOK, git_bin=GIT), "--prestop"], env=self.env(), capture_output=True, text=True)
        self.assertIn("saved work to w1/wip", p.stdout, p.stdout + p.stderr)
        self.assertEqual(self.wip(), sh("git", "-C", self.work, "rev-parse", "HEAD"))
        # the guard really refuses what the hook would send it
        g = subprocess.run([os.path.join(self.bin, "git"), "--version"], env=dict(os.environ, **{CFG_G: "/dev/null"}), capture_output=True, text=True)
        self.assertEqual(g.returncode, 3)

    def test_a_mutant_finding_git_by_path_fails_with_the_guard(self):
        self.guard_on_path()
        self.dirty()
        s = open(HOOK).read()
        self.assertIn("[GIT_BIN, ", s)
        mut = os.path.join(self.t, "mut-pathgit")
        with open(mut, "w") as f:
            f.write(s.replace("[GIT_BIN, ", '["git", '))
        p = subprocess.run([self.seam(mut, git_bin=GIT), "--prestop"], env=self.env(), capture_output=True, text=True)
        self.assertNotIn("saved work", p.stdout)
        self.assertEqual(self.wip(), "")

    def test_a_mutant_that_returns_silently_is_caught(self):
        s = open(HOOK).read()
        mut = os.path.join(self.t, "mut-silent")
        open(mut, "w").write(s.replace('return report(workdir, "the pod\'s credential helper was not found")', "return 0"))
        self.assertNotEqual(open(mut).read(), s)
        self.dirty()
        p = subprocess.run([self.seam(mut, helper_dir=os.path.join(self.t, "nowhere")), "--prestop"], env=self.env(), capture_output=True, text=True)
        with self.assertRaises(AssertionError):
            self.one_line(p)


class AutoGc(Base):
    """The reader's high: a repo-local gc.recentObjectsHook runs a program when an auto-gc fires inside the hook."""

    def gc_repo(self, extra=()):
        script = os.path.join(self.t, "gcscript")
        self.marker = os.path.join(self.t, "gcmarker")
        cfg = os.path.join(self.work, ".git", "config")
        if not hasattr(self, "plain_cfg"):
            self.plain_cfg = open(cfg).read()
            open(script, "w").write("#!/bin/sh\necho ran >> %s\nexit 1\n" % self.marker)
            os.chmod(script, 0o755)
            # loose (unreachable) objects so that an auto-gc has a reason to run: gc.auto=1 fires at more than one loose object in objects/17
            n, d = 0, os.path.join(self.work, ".git", "objects", "17")
            while len(os.listdir(d)) < 3 if os.path.isdir(d) else True:
                n += 1
                subprocess.run([GIT, "-C", self.work, "hash-object", "-w", "--stdin"], input="loose %d" % n, text=True, capture_output=True, check=True)
        with open(cfg, "w") as f:
            f.write(self.plain_cfg)
        if os.path.exists(self.marker):
            os.remove(self.marker)
        for k, v in (("gc.auto", "1"), ("gc.autoDetach", "false"), ("gc.pruneExpire", "now"), ("gc.recentObjectsHook", script)):
            sh("git", "-C", self.work, "config", "--" + "add", k, v)

    def test_gc_keys_in_the_repository_are_refused_and_the_script_never_runs(self):
        for key, value in (("gc.recentObjectsHook", None), ("gc.cruftPacks", "true"), ("pack.packSizeLimit", "1"), ("gc.auto", "1")):
            with self.subTest(key=key):
                self.gc_repo([])
                if value is not None:
                    sh("git", "-C", self.work, "config", "--" + "add", key, value)
                p = subprocess.run([self.seam(HOOK), "--prestop"], env=self.env(), capture_output=True, text=True)
                self.assertFalse(os.path.exists(self.marker), "the gc hook ran")
                self.assertIn("not a plain one: ", p.stdout)
                self.assertEqual(self.wip(), "")

    def test_the_pins_stop_an_auto_gc_even_when_the_allow_list_lets_the_key_through(self):
        # the pins alone: run git with the hook's pinned options inside a repo that has the hostile keys
        self.gc_repo([])
        ctx = cw_end_hook.Ctx(self.helper)
        self.addCleanup(ctx.close)
        ctx.env["PATH"] = "/usr/bin:/bin"
        cw_end_hook.git(self.work, "commit", "--allow-empty", "-qm", "e", ctx=ctx)
        cw_end_hook.git(self.work, "fetch", "--prune", "file://" + self.origin, "+refs/heads/*:refs/remotes/origin/*", ctx=ctx)
        self.assertFalse(os.path.exists(self.marker), "an auto-gc ran inside the hook's git call")
        # and the unpinned call does fire it (the fixture really triggers gc)
        sh("git", "-C", self.work, "commit", "-q", "--allow-empty", "-m", "f")
        self.assertTrue(os.path.exists(self.marker), "the fixture does not trigger an auto-gc: the test proves nothing")

    def variant(self, text, name):
        mut = os.path.join(self.t, "mut-" + name)
        open(mut, "w").write(text)
        self.assertNotEqual(text, open(HOOK).read(), name)
        if os.path.exists(self.marker):
            os.remove(self.marker)
        self.dirty()
        p = subprocess.run([self.seam(mut), "--prestop"], env=self.env(), capture_output=True, text=True)
        self.refused = "not a plain one" in p.stdout
        ran = os.path.exists(self.marker)
        sh("git", "-C", self.work, "reset", "-q", "--hard", self.head)
        sh("git", "-C", self.work, "clean", "-qfd")
        return ran

    def test_mutants_wildcard_restored_and_gc_pin_removed_are_caught(self):
        s = open(HOOK).read()
        check = "if key not in ALLOWED_KEYS and not BRANCH_RE.match(key):"
        pin = '"gc.auto=0", '
        self.assertIn(check, s)
        self.assertIn(pin, s)
        self.gc_repo([])
        self.head = sh("git", "-C", self.work, "rev-parse", "HEAD")
        self.assertFalse(self.variant(s + "\n", "original"), "the real hook ran the gc script")
        self.assertTrue(self.refused)
        wild = s.replace(check, "if key not in ALLOWED_KEYS and not BRANCH_RE.match(key) and not re.match(r'^(gc|pack)[.][^.]+$', key):")
        self.assertFalse(self.variant(wild, "wildcard"), "the pins stop the script even then")
        self.assertFalse(self.refused, "a restored wildcard still refuses gc.recentObjectsHook: the refusal test would not catch it")
        # the allow-list bypassed ALONE: the pins still stop the gc; with the gc.auto pin removed too the script runs
        self.assertFalse(self.variant(s.replace(check, "if False:"), "bypass"), "the pins alone did not stop the auto-gc")
        self.assertTrue(self.variant(s.replace(check, "if False:").replace(pin, "").replace('"maintenance.auto=false", ', "").replace(', "receive.autogc=false"', ""), "bypass-nopin"),
                        "removing the gc pins is not caught")
        # each pin alone is enough (belt and braces): removing ONE leaves the gc stopped
        for one in (pin, '"maintenance.auto=false", ', ', "receive.autogc=false"'):
            self.assertFalse(self.variant(s.replace(check, "if False:").replace(one, ""), "bypass-one"), one)


class LaunchSequence(Base):
    """Fail-closed must not mean fail-always: the config a normal launch writes (clone, task branch, the worker's identity, commit,
    push -u) has ZERO refusals from the allow-list."""

    def test_the_configuration_of_a_normal_clone_is_plain(self):
        for depth in ((), ("--depth", "1")):
            with self.subTest(depth=depth):
                d = tempfile.mkdtemp(dir=self.t)
                work = os.path.join(d, "repo")
                sh("git", "clone", "-q", *depth, "file://" + self.origin, work)
                sh("git", "-C", work, "checkout", "-q", "-b", "cw/task")
                sh("git", "-C", work, "config", "user.name", "worker")
                sh("git", "-C", work, "config", "user.email", "worker@users.noreply.github.com")
                open(os.path.join(work, "n.txt"), "w").write("n")
                sh("git", "-C", work, "add", "n.txt")
                sh("git", "-C", work, "commit", "-qm", "c")
                sh("git", "-C", work, "push", "-q", "-u", "origin", "cw/task")
                keys = sh("git", "-C", work, "config", "--local", "--list", "--name-only").split()
                print("keys of a normal clone (%s): %s" % (depth or "full", sorted(set(keys))))
                sh("git", "-C", work, "remote", "set-url", "origin", URL)   # the pod's clone names the repository's address
                ctx = cw_end_hook.Ctx(self.helper)
                self.addCleanup(ctx.close)
                ctx.env["PATH"] = "/usr/bin:/bin"   # the real git: the pod shell's PATH wrapper is not part of the test
                cw_end_hook.config_refusal(work, REPO, ctx)   # raises on any refusal

    def test_a_refusal_names_the_key_never_the_value(self):
        sh("git", "-C", self.work, "config", "core.sshCommand", "SECRET-VALUE-MARK")
        self.dirty()
        p = subprocess.run([self.seam(HOOK), "--prestop"], env=self.env(), capture_output=True, text=True)
        self.assertIn("not a plain one: core.sshcommand)", p.stdout)
        self.assertNotIn("SECRET-VALUE-MARK", p.stdout + p.stderr)


class Spec(unittest.TestCase):
    def test_every_pod_has_the_hook_and_the_grace(self):
        self.assertLess(cw.PRESTOP_BUDGET_SECONDS, cw.POD_TERMINATION_GRACE_SECONDS)
        for stack in (False, True):
            pod = render(stack)
            self.assertEqual(pod["spec"]["terminationGracePeriodSeconds"], cw.POD_TERMINATION_GRACE_SECONDS)
            lc = pod["spec"]["containers"][0]["lifecycle"]
            self.assertEqual(list(lc), ["preStop"])
            self.assertEqual(lc["preStop"]["exec"]["command"], ["/usr/local/bin/cw-prestop", "45"])

    def test_env_and_volumes_are_unchanged_by_the_hook(self):
        pod = render()
        self.assertEqual(sorted(e["name"] for e in pod["spec"]["containers"][0]["env"]),
                         sorted(["CW_WORKER", "CW_REPO", "CW_BRANCH", "CW_IDLE_SECONDS", "CW_HARD_DEADLINE", "CW_AUDIT_RUNTIME_ID", "CW_TOKEN_ENDPOINT"]))
        self.assertEqual(sorted(v["name"] for v in pod["spec"]["volumes"]), ["pw", "scratch", "tokens"])

    def test_other_lifecycles_are_refused(self):
        for lc in ({"postStart": {"exec": {"command": ["x"]}}},
                   {"preStop": {"sleep": {"seconds": 1}}},
                   {"preStop": {"exec": {"command": ["/bin/sh"]}}}):
            for stack in (False, True):
                pod = render(stack)
                pod["spec"]["containers"][0]["lifecycle"] = lc
                self.assertTrue(cw.forbidden_pod_fields(pod))

    def test_the_refusal_text_names_the_recovery_route(self):
        self.assertIn("recover read-only", cw.RECOVERY_ROUTE)
        src = open(os.path.join(LAUNCHER, "cloud_worker.py")).read()
        self.assertIn("RECOVERY_ROUTE)", src)


class FakeCluster(Base):
    def test_each_end_runs_the_specs_own_command(self):
        cmd = render()["spec"]["containers"][0]["lifecycle"]["preStop"]["exec"]["command"]
        cmd = ["sh", PRESTOP] + cmd[1:]   # the image path is /usr/local/bin; here the script file stands in
        for end in ("deadline", "delete", "drain"):
            with self.subTest(end=end):
                sh("git", "-C", self.work, "push", "-q", self.origin, "--delete", "w1/wip") if self.wip() else None
                self.dirty(end + ".txt")
                subprocess.run(cmd, env=self.env(), check=True, capture_output=True, timeout=30)
                self.assertEqual(self.wip(), sh("git", "-C", self.work, "rev-parse", "HEAD"))


if __name__ == "__main__":
    unittest.main()
