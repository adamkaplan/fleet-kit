#!/usr/bin/env python3
"""Ask #2517 (S5, S6, S9): the land gate at the two places a pod cannot ignore. The pod's pre-push hook refuses a push of the default branch of a land repository without the pod's live
land token; the cw-run wrapper refuses to START or RESTART a land without it. Both ask one command (CW_LAND_GATE_CMD; here a fake switchboard that answers 0 or 3). Unreachable: both
ALLOW and write ONE audit line, and CW_LAND_GATE=enforce turns the refusal on. A repository the switchboard does not gate and a landing that is not a land are untouched.
Offline, real processes, real git. Names are neutral."""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "pod-hooks", "pre-push")
TOOL = os.path.join(HERE, "..", "cw-run")
REFUSAL = "refused: lane-a does not hold the land token of acme-org/widget-service; it is held by lane-b (last touch 12s ago); queue length 2. Run: switchboard land-gate take acme-org/widget-service"
URL = "https://example.invalid/acme-org/widget-service.git"
ZERO = "0" * 40


class Gate(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        self.addCleanup(t.cleanup)
        self.d = os.path.realpath(t.name)
        self.repo = os.path.join(self.d, "r")
        os.mkdir(self.repo)
        self.audit = os.path.join(self.d, "gate-audit.log")
        self.calls = os.path.join(self.d, "calls.log")
        self.env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": self.d, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "FLEET_AGENT_NAME": "lane-a",
                    "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x.invalid", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x.invalid",
                    "CW_NOTES_LOG": os.path.join(self.d, "notes.log"), "CW_LAND_GATE_AUDIT": self.audit,
                    "CW_RUNS_DIR": os.path.join(self.d, "runs"), "CW_PROGRESS_FILE": os.path.join(self.d, "progress.txt"),
                    "CW_RUN_SERVER_PASSWORD_FILE": os.path.join(self.d, "no-server-password")}
        self.git("init", "-q", "-b", "main", ".")
        self.git("remote", "add", "origin", URL)
        self.git("commit", "-q", "--allow-empty", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("commit", "-q", "--allow-empty", "-m", "land\n\nLand-Checks: unit 5 of 5\nLand-Council: strict ok\n")
        self.good = self.git("rev-parse", "HEAD")

    def git(self, *a):
        r = subprocess.run(["git"] + list(a), cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def fake(self, status, text=""):
        """A fake switchboard: records its arguments, prints one line and exits with `status`."""
        path = os.path.join(self.d, "fake-gate-%s" % status)
        with open(path, "w") as f:
            f.write("#!/bin/sh\necho \"$@\" >> %s\n%s\nexit %s\n" % (self.calls, "echo '%s'" % text if text else ":", status))
        os.chmod(path, 0o755)
        self.env["CW_LAND_GATE_CMD"] = path

    def hook(self, url=URL, env=None):
        line = "refs/heads/x %s refs/heads/main %s\n" % (self.good, self.base)
        return subprocess.run(["sh", HOOK, "origin", url], input=line, cwd=self.repo, env=dict(self.env, **(env or {})), capture_output=True, text=True, timeout=30)

    def cw(self, *a, env=None):
        return subprocess.run([sys.executable, TOOL] + list(a), env=dict(self.env, **(env or {})), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, cwd=self.repo)

    def audit_lines(self):
        return open(self.audit).read().splitlines() if os.path.exists(self.audit) else []

    def asked(self):
        return open(self.calls).read().splitlines() if os.path.exists(self.calls) else []


class TestHook(Gate):
    def test_s5_a_push_without_the_token_is_refused_in_the_words_of_the_switchboard_and_with_it_is_allowed(self):
        self.fake(3, REFUSAL)
        r = self.hook()
        self.assertEqual((r.returncode, r.stdout, r.stderr.strip()), (3, "", REFUSAL))   # why, who holds it, how long the queue is
        self.assertEqual(self.asked(), ["acme-org/widget-service --name lane-a"])
        self.fake(0, "lane-a holds the land token of acme-org/widget-service")
        r = self.hook()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.audit_lines(), [])

    def test_s5_control_the_hook_without_the_gate_allows_the_same_push(self):
        text = open(HOOK).read().replace('    land_gate "$rref"\n', "")
        mutant = os.path.join(self.d, "pre-push-mutant")
        open(mutant, "w").write(text)
        self.fake(3, REFUSAL)
        r = subprocess.run(["sh", mutant, "origin", URL], input="refs/heads/x %s refs/heads/main %s\n" % (self.good, self.base), cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)   # the code before the change: nothing stops it

    def test_s6_an_unreachable_switchboard_allows_with_one_audit_line_and_the_word_turns_the_refusal_on(self):
        for broken in (os.path.join(self.d, "no-such-command"), None):
            if broken is None:
                self.fake(7)   # any status but 0 and 3
            else:
                self.env["CW_LAND_GATE_CMD"] = broken
            if os.path.exists(self.audit):
                os.unlink(self.audit)
            r = self.hook()
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
            lines = self.audit_lines()
            self.assertEqual(len(lines), 1, lines)
            self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ land-gate-unchecked acme-org/widget-service refs/heads/main$")
        r = self.hook(env={"CW_LAND_GATE": "enforce"})
        self.assertEqual(r.returncode, 3)
        self.assertIn("could not be checked", r.stderr)

    def test_s6_a_repository_the_switchboard_does_not_gate_and_a_push_of_another_branch_are_untouched(self):
        self.fake(0, "acme-org/docs-site is not on the land list: no gate")
        self.assertEqual(self.hook().returncode, 0)
        self.fake(3, REFUSAL)
        line = "refs/heads/x %s refs/heads/feature %s\n" % (self.good, self.base)
        r = subprocess.run(["sh", HOOK, "origin", URL], input=line, cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(len(self.asked()), 1)   # only the default branch was asked about

    def test_a_url_with_a_trailing_slash_or_dot_git_slash_is_still_asked(self):
        self.fake(3, REFUSAL)
        for url in (URL + "/", URL[:-4] + "/", URL[:-4], "git@example.invalid:acme-org/widget-service.git"):
            self.assertEqual(self.hook(url).returncode, 3, url)
            self.assertEqual(self.asked()[-1], "acme-org/widget-service --name lane-a", url)   # the repository was read right from each form

    def test_s6_a_url_that_names_no_owner_and_repository_is_unchecked_audited_and_refused_under_the_word(self):
        self.fake(3, REFUSAL)
        url = "https://user:secret-value@example.invalid/a/b/c.git"
        r = self.hook(url)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(len(self.audit_lines()), 1)
        self.assertNotIn("secret", open(self.audit).read())
        self.assertEqual(self.asked(), [])
        self.assertEqual(self.hook(url, env={"CW_LAND_GATE": "enforce"}).returncode, 3)

    def test_the_existing_land_record_rule_still_comes_first(self):
        self.fake(3, REFUSAL)
        line = "refs/heads/x %s refs/heads/main %s\n" % (self.base, self.base)   # no Land trailers
        r = subprocess.run(["sh", HOOK, "origin", URL], input=line, cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)
        self.assertIn("without a land record", r.stderr)
        self.assertEqual(self.asked(), [])   # the old rule refused first: the gate was not even asked


class TestWrapper(Gate):
    LAND = ["start", "--name", "land", "--", "sh", "-c", ".spire/tool/bin/spire land > /dev/null 2>&1; echo ran > ran.txt"]

    def ran(self, name="land"):
        # No polling: if the run was started, wait on its own exit marker (cw-run wait); a refused start has no pid file, so there is nothing to wait for.
        if os.path.exists(os.path.join(self.d, "runs", name + ".pid")):
            self.cw("wait", name, "60")
        return os.path.exists(os.path.join(self.repo, "ran.txt"))

    def test_s9_a_start_and_a_restart_without_the_token_are_refused_saying_why_and_with_it_the_land_runs(self):
        self.fake(3, REFUSAL)
        r = self.cw(*self.LAND)   # a start
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn(REFUSAL, r.stderr)
        self.assertIn("the land was not started", r.stderr)
        self.assertFalse(self.ran())
        self.assertFalse(os.path.exists(os.path.join(self.d, "runs", "land.pid")))
        self.assertEqual(self.asked(), ["acme-org/widget-service --name lane-a"])
        self.fake(0, "lane-a holds the land token of acme-org/widget-service")
        self.assertEqual(self.cw(*self.LAND).returncode, 0)   # with the token it runs
        self.assertTrue(self.ran())
        self.cw("wait", "land", "20")
        runs = os.path.join(self.d, "runs")
        before = sorted((n, open(os.path.join(runs, n)).read()) for n in os.listdir(runs) if n.startswith("land.") and n.endswith((".pid", ".exit")))
        os.unlink(os.path.join(self.repo, "ran.txt"))
        self.fake(3, REFUSAL)   # the token is gone (expired, or taken by another): a RESTART of the same name
        r = self.cw(*self.LAND)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn(REFUSAL, r.stderr)
        self.assertFalse(self.ran())   # it did not run again
        after = sorted((n, open(os.path.join(runs, n)).read()) for n in os.listdir(runs) if n.startswith("land.") and n.endswith((".pid", ".exit")))
        self.assertEqual(before, after, "the earlier run's record is untouched by the refused restart")

    def test_s9_control_a_wrapper_without_the_gate_starts_the_land(self):
        text = open(TOOL).read().replace("    land_gate(cmd)\n", "")
        mutant = os.path.join(self.d, "cw-run-mutant")
        open(mutant, "w").write(text)
        self.fake(3, REFUSAL)
        r = subprocess.run([sys.executable, mutant] + self.LAND, env=self.env, capture_output=True, text=True, cwd=self.repo, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(self.ran())   # the code before the change: the land runs past its turn

    def test_s6_unreachable_allows_with_one_audit_line_and_the_word_refuses(self):
        self.env["CW_LAND_GATE_CMD"] = os.path.join(self.d, "no-such-command")
        r = self.cw(*self.LAND)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.audit_lines()), 1)
        self.assertRegex(self.audit_lines()[0], r"land-gate-unchecked acme-org/widget-service start$")
        self.assertTrue(self.ran())
        r = self.cw("start", "--name", "land2", "--", "sh", "-c", ".spire/tool/bin/spire land", env={"CW_LAND_GATE": "enforce"})
        self.assertEqual(r.returncode, 3)
        self.assertIn("could not be checked", r.stderr)

    def test_a_stray_flag_does_not_switch_the_gate_off_and_an_unknown_repository_is_unchecked(self):
        self.fake(3, REFUSAL)
        for cmd in (["sh", "-c", "spire land", "--dry-run"], ["sh", "-c", "spire land --note --dry-run"], ["sh", "-c", "{ spire land; }"]):
            r = self.cw("start", "--name", "x", "--", *cmd)
            self.assertEqual(r.returncode, 3, (cmd, r.stderr))
        self.git("remote", "set-url", "origin", "https://example.invalid/a/b/c/d")
        r = self.cw("start", "--name", "y", "--", "sh", "-c", "spire land")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.audit_lines()), 1)
        r = self.cw("start", "--name", "z", "--", "sh", "-c", "spire land", env={"CW_LAND_GATE": "enforce"})
        self.assertEqual(r.returncode, 3)

    def test_s6_a_command_that_is_not_a_land_is_not_asked(self):
        self.fake(3, REFUSAL)
        for cmd in (["echo", "hello"], ["sh", "-c", "echo spirit lander"], ["sh", "-c", "spire lander"], ["sh", "-c", "spire land-x"]):
            r = self.cw("start", "--name", "other", "--", *cmd)
            self.assertEqual(r.returncode, 0, (cmd, r.stderr))
            self.cw("wait", "other", "5")
        self.assertEqual(self.asked(), [])


if __name__ == "__main__":
    unittest.main()
