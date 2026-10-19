"""#665: the operator driver runs the repository preflight after the allow-list and before any token is minted (S1, S2, S3, U1).
Offline: the real preflight.py against a loopback stub of the GitHub API; the fake kubectl and a marker file count the calls."""
import io, os, pwd, subprocess, sys, unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_cloud_allowlist as allow  # noqa: E402
import test_preflight as tp  # noqa: E402
import run_task  # noqa: E402
cw = allow.cw
REPO = "acme-org/widget-service"


class Wired(allow.Allow):
    @classmethod
    def setUpClass(cls):
        tp.Preflight.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        tp.Preflight.tearDownClass.__func__(cls)

    def setUp(self):
        super().setUp()
        tp.Stub.rules, tp.Stub.protected, tp.Stub.fail, tp.Stub.methods, tp.Stub.redirect = list(tp.FULL), False, None, [], None
        tp.Stub.review_count, tp.Stub.enforcement, tp.Stub.include, tp.Stub.conditions = 1, "active", ["~DEFAULT_BRANCH"], "ok"
        tp.Stub.exclude, tp.Stub.target, tp.Stub.default, tp.Stub.rule_ids, tp.Stub.extra, tp.Stub.org, tp.Stub.no_id = [], "branch", "main", {}, {}, False, False
        tp.Stub.org_code, tp.Stub.org_types, tp.Stub.fail_code, tp.Stub.org_extra, tp.Stub.org_hint = None, [], 500, [], None
        self.env["CW_ALLOWED_REPOS"] = REPO
        # the loopback stub reaches the real program ONLY through the library's test hook, never through the caller's environment
        hook = mock.patch.object(cw, "PREFLIGHT_TEST_ENV", {"CW_GITHUB_API_BASE": self.base, "CW_PREFLIGHT_TOKEN_CMD": "echo " + tp.TOKEN})
        hook.start()
        self.addCleanup(hook.stop)
        self.mint = os.path.join(self.tmp.name, "mint-ran")
        self.env["CW_GIT_TOKEN_CMD"] = "touch " + self.mint   # a mint leaves its mark
        self.driver = mock.patch.object(run_task, "Driver").start()   # the driver proper: a fake that proves whether it was reached
        self.driver.return_value.run.return_value = 0
        self.addCleanup(mock.patch.stopall)

    def drive(self, repo=REPO, **env):
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            code = run_task.main(["coder7", repo, "main", os.path.join(self.tmp.name, "brief.txt")], dict(self.env, **env))
        return code, out.getvalue(), err.getvalue()

    def nothing_made(self):
        self.driver.assert_not_called()
        self.assertFalse(os.path.exists(self.mint), "a mint ran")
        self.assertEqual(self.calls(), [], "a cluster call was made")


class S1Refuses(Wired):
    def test_three_of_three_refusal_kinds_mint_nothing_create_nothing_and_show_the_message(self):
        for kind, setup, message in (("protection missing", lambda: setattr(tp.Stub, "rules", ["pull_request"]), "missing a non_fast_forward rule"),
                                     ("unreadable answer", lambda: setattr(tp.Stub, "fail", "badjson"), "cannot read"),
                                     ("repository not found", lambda: setattr(tp.Stub, "fail", "/repos/" + REPO), "cannot read")):
            with self.subTest(kind):
                tp.Stub.rules, tp.Stub.fail = list(tp.FULL), None
                setup()
                code, out, err = self.drive()
                self.assertEqual(code, 2, err)
                self.assertIn("did not pass", err)
                self.assertIn(message, err)
                self.assertNotIn("preflight ok", out + err)
                self.nothing_made()


class S2Passes(Wired):
    def test_a_passing_preflight_prints_one_result_line_and_reaches_the_driver(self):
        code, out, err = self.drive()
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count("preflight: preflight ok"), 1, out)
        self.driver.return_value.run.assert_called_once()


class S3FailClosed(Wired):
    def test_a_missing_killed_or_silent_program_refuses_and_mints_nothing(self):
        for kind, argv in (("missing", ["/nonexistent/preflight"]), ("killed", ["sh", "-c", "kill -9 $$"]), ("empty output", ["true"]),
                           ("timed out", ["sleep", "5"]), ("non-zero, no message", ["sh", "-c", "exit 3"])):
            with self.subTest(kind), mock.patch.object(cw, "PREFLIGHT_ARGV", argv), mock.patch.object(cw, "PREFLIGHT_SECONDS", 1):
                code, out, err = self.drive()
                self.assertEqual(code, 2, err)
                self.assertIn("refused", err)
                self.assertNotIn("preflight ok", out)
                self.nothing_made()


class U1AllowListFirst(Wired):
    def test_an_off_list_repository_is_refused_before_the_preflight_is_called(self):
        mark = os.path.join(self.tmp.name, "pf-ran")
        with mock.patch.object(cw, "PREFLIGHT_ARGV", ["sh", "-c", "touch " + mark]):
            for repo in ("acme-org/widget-service-2", "other-org/x"):
                code, out, err = self.drive(repo)
                self.assertEqual(code, 2)
                self.assertIn("is not on the cloud allow-list", err)
        self.assertFalse(os.path.exists(mark), "the preflight was called")
        self.nothing_made()


class EnvironmentCannotRedirectTheGuard(Wired):
    def test_a_loopback_api_base_and_token_command_in_the_callers_environment_are_ignored(self):
        tp.Stub.rules = list(tp.FULL)   # the stub would ACCEPT the repository: it must never be asked
        with mock.patch.object(cw, "PREFLIGHT_TEST_ENV", {}):
            for env in ({}, {"CW_VAULT_SUBSCRIPTION": "", "CW_VAULT_NAME": ""}):
                code, out, err = self.drive(CW_GITHUB_API_BASE=self.base, CW_PREFLIGHT_TOKEN_CMD="echo " + tp.TOKEN, **env)
                self.assertEqual(code, 2, err)
                self.assertIn("did not pass", err)
                self.assertNotIn("preflight ok", out + err)
                self.assertEqual(tp.Stub.methods, [], "the stub was contacted")
                self.nothing_made()

    def test_the_child_gets_only_the_allow_list_a_fixed_path_and_a_private_home(self):
        env = {"PATH": "/tmp/evil", "HOME": "/home/evil", "LC_ALL": "C", "CW_GITHUB_API_BASE": "http://127.0.0.1:1", "CW_PREFLIGHT_TOKEN_CMD": "x",
               "CW_GIT_TOKEN_CMD": "y", "GH_TOKEN": "z", "GITHUB_TOKEN": "z", "HTTPS_PROXY": "p", "CW_VAULT_NAME": "v", "PYTHONPATH": "/tmp/evil",
               "TMPDIR": "/tmp/evil", "CW_OPENSSL": "relative-openssl", "AZURE_CONFIG_DIR": "relative"}
        with mock.patch.object(cw, "PREFLIGHT_TEST_ENV", {}):
            got = cw._preflight_env(env, "/private/home")
        self.assertEqual(got["PATH"], cw.PREFLIGHT_PATH)
        self.assertEqual((got["HOME"], got["TMPDIR"]), ("/private/home", "/private/home"))
        self.assertEqual(got["AZURE_CONFIG_DIR"], os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".azure"))   # from the password database, never $HOME
        self.assertEqual({k: got[k] for k in got if k not in ("PATH", "HOME", "TMPDIR", "AZURE_CONFIG_DIR")}, {"LC_ALL": "C", "CW_VAULT_NAME": "v"})

    def test_an_absolute_existing_azure_config_dir_is_kept_and_an_absolute_openssl_too(self):
        cfg = self.tmp.name
        ossl = "/bin/sh"
        with mock.patch.object(cw, "PREFLIGHT_TEST_ENV", {}):
            got = cw._preflight_env({"AZURE_CONFIG_DIR": cfg, "CW_OPENSSL": ossl}, "/h")
        self.assertEqual((got["AZURE_CONFIG_DIR"], got["CW_OPENSSL"]), (cfg, ossl))


class PoisonedEnvironment(Wired):
    """HOME with a usercustomize.py, a PYTHONPATH with a sitecustomize.py and fake python/gh/az/sh on PATH: none of it may run, at BOTH entry points."""
    FAKE = "#!/bin/sh\ntouch \"$SENTINEL\"\necho preflight ok: deletion, non_fast_forward, pull_request\nexit 0\n"

    def poison(self):
        root = self.tmp.name
        self.sentinel = os.path.join(root, "fake-ran")
        home, pp, bindir = (os.path.join(root, d) for d in ("poison-home", "poison-pp", "poison-bin"))
        for d in (home, pp, bindir):
            os.makedirs(d)
        code = "import sys, os\nif any(a.endswith('preflight.py') for a in sys.argv):\n    open(%r, 'w').close()\n    print('preflight: preflight ok: deletion, non_fast_forward, pull_request')\n    os._exit(0)\n" % self.sentinel
        probe = subprocess.run([sys.executable, "-c", "import site; print(site.getusersitepackages())"], capture_output=True, text=True,
                               env={"HOME": home, "PATH": os.environ.get("PATH", "")}).stdout.strip()
        os.makedirs(probe, exist_ok=True)
        with open(os.path.join(probe, "usercustomize.py"), "w") as f:
            f.write(code)
        with open(os.path.join(pp, "sitecustomize.py"), "w") as f:
            f.write(code)
        for name in ("python", "python3", "gh", "az", "sh", "openssl"):
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write(self.FAKE)
            os.chmod(p, 0o755)
        return {"HOME": home, "PYTHONPATH": pp, "PATH": bindir + ":" + os.environ.get("PATH", ""), "SENTINEL": self.sentinel}

    def check_refused(self, out, err):
        self.assertNotIn("preflight ok", out + err)
        self.assertFalse(os.path.exists(self.sentinel), "a fake from the caller's environment ran")

    def test_the_driver_ignores_it(self):
        env = self.poison()
        with mock.patch.object(cw, "PREFLIGHT_TEST_ENV", {}):
            code, out, err = self.drive(**env)
        self.assertEqual(code, 2, err)
        self.assertIn("did not pass", err)
        self.check_refused(out, err)
        self.nothing_made()

    def test_launch_cloud_ignores_it(self):   # `launch --cloud` calls the same function with the caller's environment
        env = dict(self.poison(), **{"CW_ALLOWED_REPOS": REPO})
        with mock.patch.object(cw, "PREFLIGHT_TEST_ENV", {}):
            with self.assertRaises(cw.CloudError) as cm:
                cw.check_repo_preflight(REPO, env)
        self.check_refused("", str(cm.exception))

    def test_the_legitimate_route_still_works_with_the_environment_scrubbed(self):
        env = self.poison()
        tp.Stub.rules = list(tp.FULL)
        code, out, err = self.drive(**env)   # the stub route is injected through the hook; the poisoned caller environment is not read
        self.assertEqual(code, 0, err)
        self.assertIn("preflight ok", out + err)
        self.assertFalse(os.path.exists(self.sentinel))

    def test_the_child_command_is_isolated_and_the_home_is_removed_afterwards(self):
        seen = {}
        real = subprocess.run

        def spy(argv, **kw):
            seen["argv"], seen["env"], seen["cwd"] = argv, kw["env"], kw.get("cwd")
            seen["empty"] = os.listdir(kw["env"]["HOME"]) == []
            seen["mode"] = os.stat(kw["env"]["HOME"]).st_mode & 0o777
            return real(argv, **kw)
        with mock.patch.object(cw.subprocess, "run", spy):
            self.drive()
        self.assertEqual(seen["argv"][:3], [os.path.realpath(sys.executable), "-I", "-S"])
        self.assertTrue(os.path.isabs(seen["argv"][0]))
        self.assertEqual(seen["env"]["PATH"], cw.PREFLIGHT_PATH)
        self.assertTrue(seen["empty"])
        self.assertEqual(seen["mode"], 0o700)
        self.assertFalse(os.path.exists(seen["env"]["HOME"]), "the private home was not removed")


class Shared(unittest.TestCase):
    def test_both_entry_points_call_the_one_function(self):
        self.assertIs(run_task.cw.check_repo_preflight, cw.check_repo_preflight)


if __name__ == "__main__":
    unittest.main()
