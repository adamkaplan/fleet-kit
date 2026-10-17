"""Slice 4 (#639): the repository allow-list. Scenario S8 and U1. Offline: the fake kubectl records every call."""
import io, json, os, sys, unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_cloud_worker as base  # noqa: E402
import run_task  # noqa: E402
cw = base.cw

LIST = "acme-org/widget-service,acme-org/widget-api"
TOOLING = "acme-org/tooling"


class Allow(base.Base):
    def setUp(self):
        super().setUp()
        self.env["CW_ALLOWED_REPOS"] = LIST
        self.env["CW_TOOLING_REPO"] = TOOLING

    def calls(self):
        p = self.env["FAKE_KUBE_CALLS"]
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x) for x in f.read().splitlines() if x]

    def create_for(self, repo):
        return self.run_cli("create", "coder7", "--repo", repo, "--branch", "main")

    def refused(self, repo):
        code, out, err = self.create_for(repo)
        self.assertEqual(code, 2, err)
        self.assertEqual(self.calls(), [], "no kubectl call at all, so no create")
        self.assertEqual(out, "")
        return err


class S8Launcher(Allow):
    def test_three_off_list_repositories_are_refused_with_exit_2_and_zero_calls(self):
        for repo in (TOOLING, "acme-org/widget-service-2", "other-org/widget-service"):
            self.assertIn("refused: %s is not on the cloud allow-list" % repo, self.refused(repo))

    def test_the_tooling_repository_is_refused_even_when_it_is_listed(self):
        self.env["CW_ALLOWED_REPOS"] = LIST + "," + TOOLING
        self.assertIn("refused: %s is not on the cloud allow-list" % TOOLING, self.refused(TOOLING))

    def test_an_on_list_repository_proceeds_and_the_match_ignores_case(self):
        code, _, err = self.create_for("acme-org/widget-service")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.calls())
        self.env["CW_ALLOWED_REPOS"] = "ACME-ORG/Widget-Service"
        code, _, err = self.run_cli("create", "coder8", "--repo", "acme-org/widget-service", "--branch", "main")
        self.assertEqual(code, 0, err)

    def test_unset_or_empty_list_refuses_fail_closed(self):
        for value in (None, "", "  "):
            if value is None:
                del self.env["CW_ALLOWED_REPOS"]
            else:
                self.env["CW_ALLOWED_REPOS"] = value
            self.refused("acme-org/widget-service")

    def test_a_malformed_entry_is_rejected_whatever_else_is_listed(self):
        for bad in ("acme-org/*", "*/widget-service", "acme-org/../x", "acme-org/..", "acme-org/a b", "acme-org", "acme-org/a/b", "./x", "acme-org/"):
            self.env["CW_ALLOWED_REPOS"] = "acme-org/widget-service," + bad
            err = self.refused("acme-org/widget-service")
            self.assertIn("not OWNER/NAME", err, bad)

    def test_a_malformed_requested_repository_is_refused(self):
        for bad in ("acme-org/*", "acme-org/..", "acme-org/widget-service/x", "acme-org widget"):
            self.refused(bad)

    def test_a_malformed_tooling_setting_refuses(self):
        self.env["CW_TOOLING_REPO"] = "not a repo"
        self.refused("acme-org/widget-service")

    def test_a_trailing_dot_git_is_malformed_in_the_tooling_setting_and_in_the_list(self):
        self.env["CW_TOOLING_REPO"] = "acme-org/tooling.git"
        self.refused("acme-org/widget-service")
        self.env["CW_TOOLING_REPO"] = TOOLING
        self.env["CW_ALLOWED_REPOS"] = LIST + ",acme-org/widget-web.git"
        self.assertIn("not OWNER/NAME", self.refused("acme-org/widget-service"))

    def test_a_trailing_newline_is_not_accepted_anywhere(self):
        for repo in ("acme-org/widget-service\n", "acme-org\n/widget-service"):
            self.refused(repo)
        self.assertIsNone(cw._repo_key("acme-org/widget-service\n"))
        self.assertIsNone(cw._repo_key("acme-org\n/widget-service"))
        self.env["CW_TOOLING_REPO"] = "acme-org/tool\ning"
        self.refused("acme-org/widget-service")
        self.assertFalse(run_task.REPO_RE.fullmatch("acme-org/widget-service\n"))

    def test_a_renamed_repository_is_not_matched_spellings_only(self):
        self.env["CW_ALLOWED_REPOS"] = "acme-org/old-name"
        self.assertIn("not on the cloud allow-list", self.refused("acme-org/new-name"))

    def test_tooling_unset_adds_no_extra_check(self):
        del self.env["CW_TOOLING_REPO"]
        self.assertEqual(self.create_for("acme-org/widget-api")[0], 0)


class S8Driver(Allow):
    def drive(self, repo, **env):
        e = dict(self.env, **env)
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = run_task.main(["coder7", repo, "main", os.path.join(self.tmp.name, "brief.txt")], e)
        return code, err.getvalue()

    def test_the_driver_uses_the_same_function_and_makes_no_call(self):
        self.assertIs(run_task.cw.check_repo_allowed, cw.check_repo_allowed)
        for repo in (TOOLING, "acme-org/widget-service-2", "other-org/x"):
            code, err = self.drive(repo)
            self.assertEqual(code, 2)
            self.assertIn("refused: %s is not on the cloud allow-list" % repo, err)
        code, err = self.drive("acme-org/widget-service", CW_ALLOWED_REPOS="")
        self.assertEqual(code, 2)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
