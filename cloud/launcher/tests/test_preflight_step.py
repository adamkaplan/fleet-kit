"""Ask #2184: a launch's preflight that hits its time limit names the step it was in and the seconds, and a vault that did not answer says so.
Offline: the vault is a shell command (a stub that hangs), the GitHub API a loopback stub; the limits are short test limits."""
import os, re, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import test_cloud_allowlist as allow  # noqa: E402
import test_preflight as tp  # noqa: E402
import app_token  # noqa: E402
cw = allow.cw
REPO = "acme-org/widget-service"
LAUNCHER = os.path.dirname(HERE)
STEP_LINES = ('        print("step: token mint", file=sys.stderr, flush=True)\n',
              '        print("step: repository read", file=sys.stderr, flush=True)\n')
TIP_TEXT = "refused: the repository preflight for %s did not pass: preflight: WARN open action required_status_checks: no required_status_checks rule: a merge is bounded by no passing check | preflight: FAIL on main: missing a non_fast_forward rule (force-push must be refused) | " \
           "preflight: FAIL on main: missing a deletion rule (branch deletion must be refused); nothing was created"


def mutate(src, old, new):
    """The text with `old` replaced by `new`, computed: refused unless old is there exactly once and the result differs."""
    if src.count(old) != 1 or old == new:
        raise ValueError("the mutation does not apply or changes nothing")
    return src.replace(old, new)


class Base(allow.Allow):
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
        self.addCleanup(mock.patch.stopall)

    def refusal(self, test_env, seconds=2, program=None):
        patches = [mock.patch.object(cw, "PREFLIGHT_TEST_ENV", dict(test_env, CW_GITHUB_API_BASE=self.base)),
                   mock.patch.object(cw, "PREFLIGHT_SECONDS", seconds)]
        if program:
            patches.append(mock.patch.object(cw, "PREFLIGHT_PROGRAM", program))
        for p in patches:
            p.start()
        try:
            with self.assertRaises(cw.CloudError) as c:
                cw.check_repo_preflight(REPO, {})
            return str(c.exception)
        finally:
            mock.patch.stopall()


class Hangs(Base):
    HANG = {"CW_APP_ID_CMD": "sleep 20"}

    def test_a_vault_that_never_answers_names_the_step_the_seconds_and_the_host(self):
        msg = self.refusal(self.HANG)
        self.assertEqual(msg, "refused: the repository preflight for %s timed out after 2 s at token mint; "
                              "the key vault is unreachable from this host; nothing was created" % REPO)

    def test_a_hang_after_the_mint_names_the_repository_read_and_not_the_host(self):
        msg = self.refusal({"CW_PREFLIGHT_TOKEN_CMD": "echo " + tp.TOKEN, "CW_GITHUB_API_BASE": "http://127.0.0.1:9"}, program=self.slow_after_mint())
        self.assertEqual(msg, "refused: the repository preflight for %s timed out after 2 s at repository read; nothing was created" % REPO)

    def slow_after_mint(self):
        d = tempfile.mkdtemp(prefix="pf-step-")
        self.addCleanup(shutil.rmtree, d, True)
        for n in os.listdir(LAUNCHER):
            if n.endswith(".py"):
                shutil.copy(os.path.join(LAUNCHER, n), d)
        p = os.path.join(d, "preflight.py")
        with open(p) as f:
            src = f.read()
        with open(p, "w") as f:   # the hang itself: the read after the step line waits
            f.write(mutate(src, "        repo = get(base", "        time.sleep(30)\n        repo = get(base"))
        return p


class Unreachable(Base):
    def test_a_vault_command_that_cannot_run_says_its_reason(self):
        err = OSError(101, "Network is unreachable")
        with mock.patch.dict(os.environ, {"CW_APP_ID_CMD": "true"}), mock.patch.object(app_token.subprocess, "run", side_effect=err):
            with self.assertRaises(SystemExit):
                app_token.vault("CW_APP_ID_CMD", "cw-app-id")
        self.assertEqual(app_token.LAST_FAILURE["reason"], "the key vault could not be reached: Network is unreachable")
        self.assertTrue(app_token.LAST_FAILURE["transient"])

    def test_a_vault_command_that_times_out_says_60_s(self):
        with mock.patch.dict(os.environ, {"CW_APP_ID_CMD": "true"}), mock.patch.object(app_token.subprocess, "run", side_effect=subprocess.TimeoutExpired("sh", 60)):
            with self.assertRaises(SystemExit):
                app_token.vault("CW_APP_ID_CMD", "cw-app-id")
        self.assertEqual(app_token.LAST_FAILURE["reason"], "the key vault did not answer within 60 s")

    def test_the_reason_is_short_and_scrubbed(self):
        with mock.patch.dict(os.environ, {"CW_APP_ID_CMD": "true"}), mock.patch.object(app_token.subprocess, "run", side_effect=OSError(5, "bad\nline " + "x" * 200 + " ghp_ABCDEF")):
            with self.assertRaises(SystemExit):
                app_token.vault("CW_APP_ID_CMD", "cw-app-id")
        reason = app_token.LAST_FAILURE["reason"]
        self.assertNotIn("\n", reason)
        self.assertLessEqual(len(reason), 100)
        self.assertNotIn("ghp_", reason)


class Answers(Base):
    def test_an_answering_vault_and_a_failing_repository_keep_the_tip_text(self):
        tp.Stub.rules = ["pull_request"]
        msg = self.refusal({"CW_PREFLIGHT_TOKEN_CMD": "echo " + tp.TOKEN}, seconds=60)
        self.assertEqual(msg, TIP_TEXT % REPO)


class Mutant(Base):
    def mutant_program(self, drop):
        d = tempfile.mkdtemp(prefix="pf-mutant-")
        self.addCleanup(shutil.rmtree, d, True)
        for n in os.listdir(LAUNCHER):
            if n.endswith(".py"):
                shutil.copy(os.path.join(LAUNCHER, n), d)
        p = os.path.join(d, "preflight.py")
        with open(p) as f:
            src = f.read()
        for line in drop:
            src = mutate(src, line, "")
        with open(p, "w") as f:
            f.write(src)
        return p

    def test_the_control_prints_the_step_first(self):
        msg = self.refusal(Hangs.HANG)
        self.assertIn("at token mint", msg)

    def test_without_the_step_the_launch_cannot_name_it(self):
        msg = self.refusal(Hangs.HANG, program=self.mutant_program(STEP_LINES))
        self.assertNotIn("token mint", msg)
        self.assertNotIn("unreachable from this host", msg)
        self.assertEqual(msg, "refused: the repository preflight for %s timed out after 2 s at its start; nothing was created" % REPO)

    def test_a_mutation_that_changes_nothing_or_does_not_apply_is_refused(self):
        src = open(os.path.join(LAUNCHER, "preflight.py")).read()
        self.assertTrue(mutate(src, STEP_LINES[0], "") != src)
        with self.assertRaises(ValueError):
            mutate(src, STEP_LINES[0], STEP_LINES[0])
        with self.assertRaises(ValueError):
            mutate(src, "no such text anywhere", "x")


if __name__ == "__main__":
    unittest.main()
