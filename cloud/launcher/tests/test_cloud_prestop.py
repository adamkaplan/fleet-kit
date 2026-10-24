"""Ask #1142: the pod's pre-stop hook (cw-prestop / cw-end-hook --prestop). Fake cluster = run the rendered spec's own command."""
import argparse, datetime as dt, os, shutil, subprocess, sys, tempfile, time, unittest

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
        self.origin, self.work, self.bin = (os.path.join(self.t, n) for n in ("origin.git", "repo", "bin"))
        os.makedirs(self.bin)
        sh("git", "init", "-q", "--bare", "-b", "main", self.origin)
        sh("git", "clone", "-q", self.origin, self.work)
        for k, v in (("user.name", "t"), ("user.email", "t@invalid")):
            sh("git", "-C", self.work, "config", k, v)
        open(os.path.join(self.work, "a.txt"), "w").write("a")
        sh("git", "-C", self.work, "add", "-A")
        sh("git", "-C", self.work, "commit", "-qm", "base")
        sh("git", "-C", self.work, "push", "-q", "origin", "HEAD:main")
        # the hook is installed under the names the image gives it
        # the fake remote: origin is named like the pod's repository and the (global) git configuration maps that github URL to the local bare repo
        sh("git", "-C", self.work, "remote", "set-url", "origin", URL)
        self.gitconfig = os.path.join(self.t, ".gitconfig")
        open(self.gitconfig, "w").write('[url "%s"]\n\tinsteadOf = %s\n' % (self.origin, URL))
        shutil.copy(HOOK, os.path.join(self.bin, "cw-end-hook"))
        self.out = os.path.join(self.t, "out")
        # macOS has no GNU timeout: a small stand-in at the front of PATH ('timeout [-k N] SECONDS cmd args' runs cmd args).
        # CW_TEST_FAKE_TIMEOUT=1 forces the stand-in even where GNU timeout exists.
        self.fake_timeout = bool(os.environ.get("CW_TEST_FAKE_TIMEOUT")) or shutil.which("timeout") is None
        if self.fake_timeout:
            fake = os.path.join(self.bin, "timeout")
            open(fake, "w").write('#!/bin/sh\nif [ "$1" = -k ]; then shift 2; fi\nshift\nexec "$@"\n')
            os.chmod(fake, 0o755)

    def env(self, **kw):
        e = dict(os.environ, PATH=self.bin + ":/usr/bin:/bin", CW_WORKER="w1", CW_WORKDIR=self.work,
                 CW_REPO=REPO, HOME=self.t, GIT_CONFIG_GLOBAL=self.gitconfig, CW_PRESTOP_OUT=self.out)
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
        for bad in ("force", "lease", "+", " -f"):
            self.assertNotIn(bad.replace("+", "+r"), pushes[0].replace("+refs", "+r").replace("--no-verify", ""))

    def test_clean_tree_or_no_repository_does_nothing(self):
        self.fake_git('echo "$@" >> %s/calls\nexec REAL "$@"\n' % self.t)
        p = self.hook()
        self.assertEqual((p.returncode, p.stdout + p.stderr), (0, ""))
        self.assertFalse([l for l in open(os.path.join(self.t, "calls")) if " push " in " " + l])
        p = self.hook(CW_WORKDIR=os.path.join(self.t, "nothing"))
        self.assertEqual((p.returncode, p.stdout + p.stderr), (0, ""))

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


REFSPEC = "--no-verify %s HEAD:refs/heads/w1/wip" % URL
OPTS = " ".join(cw_end_hook.SAFE_OPTS)


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
        return subprocess.run([sys.executable, hook, "--prestop", *args], env=self.env(**kw), capture_output=True, text=True, timeout=30)

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
            self.assertEqual(pushed[0], "-C %s %s push %s" % (self.work, OPTS, REFSPEC), (args, kw))
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
        self.assertEqual(self.pushes(), ["-C %s %s push %s" % (self.work, OPTS, REFSPEC)])

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
        for k, v in (("core.sshCommand", script), ("core.fsmonitor", script), ("diff.evil.textconv", script),
                     ("remote.origin.pushurl", "https://github.com/other-org/elsewhere.git"),
                     ("url.https://github.com/other-org/.insteadOf", "https://github.com/acme-org/")):
            sh("git", "-C", self.work, "config", k, v)
        for f in ("marker", "calls"):
            if os.path.exists(os.path.join(self.t, f)):
                os.remove(os.path.join(self.t, f))
        self.fake_git('echo "$@" >> %s\nexec REAL "$@"\n' % self.calls)

    def pushes(self):
        return [l.strip() for l in open(self.calls) if " push " in " " + l] if os.path.exists(self.calls) else []

    def run_hook(self, hook):
        return subprocess.run([sys.executable, hook, "--prestop"], env=self.env(), capture_output=True, text=True, timeout=30)

    def good(self, hook):
        """Targets the explicit URL only, with every option present, and no agent command ran."""
        self.dirty()
        p = self.run_hook(hook)
        self.assertFalse(os.path.exists(self.marker), "an agent hook or command ran")
        pushes = self.pushes()
        self.assertEqual(pushes, ["-C %s %s push %s" % (self.work, OPTS, REFSPEC)], p.stdout)
        self.assertIn("-c core.hooksPath=/dev/null", pushes[0])
        self.assertNotIn("other-org", pushes[0])

    def test_hostile_repository_runs_nothing_and_pushes_to_the_explicit_url(self):
        # origin itself is hostile-free here (same repository): the hostile part is hooks, commands, pushurl, insteadOf
        sh("git", "-C", self.work, "config", "--unset-all", "url.https://github.com/other-org/.insteadOf")
        self.good(HOOK)
        self.assertEqual(self.wip(), sh("git", "-C", self.work, "rev-parse", "HEAD"))

    def test_insteadof_in_the_repository_for_our_url_pushes_nothing(self):
        self.dirty()
        p = self.run_hook(HOOK)
        self.assertEqual(self.pushes(), [])
        self.assertEqual(self.wip(), "")
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(p.stdout.strip(), "cw-prestop: not pushed (origin points elsewhere); work left in %s" % self.work)

    def test_origin_pointing_elsewhere_pushes_nothing_and_prints_one_line(self):
        sh("git", "-C", self.work, "config", "--unset-all", "url.https://github.com/other-org/.insteadOf")
        for url in ("https://github.com/other-org/elsewhere.git", "git@github.com:other-org/widget-service.git"):
            sh("git", "-C", self.work, "config", "remote.origin.url", url)
            self.dirty()
            p = self.run_hook(HOOK)
            self.assertEqual(self.pushes(), [])
            self.assertEqual(self.wip(), "")
            self.assertEqual(p.stdout.strip(), "cw-prestop: not pushed (origin points elsewhere); work left in %s" % self.work)
        sh("git", "-C", self.work, "config", "remote.origin.url", "git@github.com:ACME-ORG/Widget-Service")
        self.good(HOOK)   # same repository after normalising case, scp form and .git

    def test_mutants_are_each_caught(self):
        sh("git", "-C", self.work, "config", "--unset-all", "url.https://github.com/other-org/.insteadOf")
        base = sh("git", "-C", self.work, "rev-parse", "HEAD")
        muts = {"hooksPath": ('"-c", "core.hooksPath=/dev/null", ', ""),
                "no-verify": ('"--no-verify", url', "url"),
                "pushurl": ('git(workdir, "push", "--no-verify", url,', 'git(workdir, "push", "--no-verify", "origin",'),
                "origin-check": ("if cur and norm_repo(cur) != repo.lower():", "if False:")}
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
