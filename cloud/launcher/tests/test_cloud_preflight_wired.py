"""#665: the operator driver runs the repository preflight after the allow-list and before any token is minted (S1, S2, S3, U1).
Offline: the real preflight.py against a loopback stub of the GitHub API; the fake kubectl and a marker file count the calls."""
import io, os, sys, unittest
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

    def test_the_child_gets_only_the_allow_list(self):
        env = {"PATH": "/usr/bin", "HOME": "/home/user", "LC_ALL": "C", "CW_GITHUB_API_BASE": "http://127.0.0.1:1", "CW_PREFLIGHT_TOKEN_CMD": "x",
               "CW_GIT_TOKEN_CMD": "y", "GH_TOKEN": "z", "GITHUB_TOKEN": "z", "HTTPS_PROXY": "p", "CW_VAULT_NAME": "v"}
        with mock.patch.object(cw, "PREFLIGHT_TEST_ENV", {}):
            self.assertEqual(cw._preflight_env(env), {"PATH": "/usr/bin", "HOME": "/home/user", "LC_ALL": "C", "CW_VAULT_NAME": "v"})


class Shared(unittest.TestCase):
    def test_both_entry_points_call_the_one_function(self):
        self.assertIs(run_task.cw.check_repo_preflight, cw.check_repo_preflight)


if __name__ == "__main__":
    unittest.main()
