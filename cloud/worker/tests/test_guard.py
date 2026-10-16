"""Slice #637 (S3, U1): the pod holds the same rules a local agent has. The pod-side wrapper refuses six kinds of command
(exit 3, one line, no argument value in it), passes the rest unchanged, and the generated opencode config denies the same six.
This is a convenience layer: the repository's rules are the guard. Offline: a temp tree, stand-in gh and git."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "..")
ENTRYPOINT = os.path.join(WORKER, "entrypoint.sh")
REPO = "acme-org/widget-service"
SECRET = "planted-Zq8Zq8Zq8-secret"
TAIL = "the repository's rules are the guard; this wrapper is only a convenience"

# (category, the phrase the refusal gives, the command)
SIX = [
    ("merge", "merging a pull request", ["gh", "pr", "merge", "12", "--squash"]),
    ("merge", "merging a pull request", ["gh", "api", "-X", "PUT", "repos/%s/pulls/12/merge" % REPO]),
    ("force-push", "force-pushing", ["git", "push", "--force", "origin", "feature-x"]),
    ("force-push", "force-pushing", ["git", "push", "-f", "origin", "feature-x"]),
    ("force-push", "force-pushing", ["git", "push", "--force-with-lease", "origin", "feature-x"]),
    ("force-push", "force-pushing", ["git", "push", "origin", "+feature-x"]),
    ("default-branch", "pushing to the default branch", ["git", "push", "origin", "HEAD:main"]),
    ("default-branch", "pushing to the default branch", ["git", "push", "origin", "main"]),
    ("default-branch", "pushing to the default branch", ["git", "push", "origin", "HEAD:refs/heads/master"]),
    ("default-branch", "pushing to the default branch", ["git", "-C", "/work", "push", "origin", "HEAD:trunk"]),   # CW_DEFAULT_BRANCH=trunk
    ("issue-write", "writing issues", ["gh", "issue", "create", "--title", "t"]),
    ("issue-write", "writing issues", ["gh", "issue", "edit", "3", "--add-label", "bug"]),
    ("issue-write", "writing issues", ["gh", "issue", "close", "3"]),
    ("issue-write", "writing issues", ["gh", "issue", "comment", "3", "--body", "x"]),
    ("label-write", "writing labels", ["gh", "label", "create", "bug"]),
    ("label-write", "writing labels", ["gh", "label", "edit", "bug"]),
    ("label-write", "writing labels", ["gh", "label", "delete", "bug"]),
    ("settings", "changing repository settings", ["gh", "repo", "edit", "--visibility", "public"]),
    ("settings", "changing repository settings", ["gh", "repo", "delete", REPO]),
    ("settings", "changing repository settings", ["gh", "repo", "archive", REPO]),
    ("settings", "changing repository settings", ["gh", "api", "--method", "PATCH", "repos/%s" % REPO]),
    ("settings", "changing repository settings", ["gh", "api", "-XDELETE", "repos/%s/branches/main/protection" % REPO]),
    ("settings", "changing repository settings", ["gh", "api", "repos/%s/rulesets" % REPO, "-f", "name=x"]),
    ("settings", "changing repository settings", ["gh", "api", "repos/%s/hooks" % REPO, "-f", "name=web"]),
    ("settings", "changing repository settings", ["gh", "api", "-X", "PUT", "repos/%s/actions/secrets/KEY" % REPO]),
]
SIX_CATEGORIES = {"merge", "force-push", "default-branch", "issue-write", "label-write", "settings"}

ORDINARY = [
    ["gh", "pr", "create", "--title", "t", "--body", "b"],
    ["git", "push", "origin", "feature-x"],
    ["git", "push", "-u", "origin", "HEAD:feature-x"],
    ["gh", "pr", "view", "12"],
    ["gh", "api", "repos/%s/pulls/12" % REPO],
    ["gh", "pr", "comment", "12", "--body", "merge force main"],
    ["git", "status"],
]


def read(path):
    with open(path) as f:
        return f.read()


class Guard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        cls.addClassCleanup(t.cleanup)
        d = os.path.realpath(t.name)
        cls.run_dir, cls.tmpfs, cls.home, cls.realbin, cls.share = [os.path.join(d, x) for x in ("run", "tmpfs", "home", "realbin", "share")]
        for x in (cls.run_dir, cls.tmpfs, cls.home, cls.realbin, cls.share):
            os.mkdir(x)
        for src, name in (("guard.sh", "guard.sh"), ("opencode-worker.json", "opencode-worker.json"), ("agents/worker.md", "worker.md")):
            shutil.copy(os.path.join(WORKER, src), os.path.join(cls.share, name))
        with open(os.path.join(cls.run_dir, "git-token"), "w") as f:
            f.write(SECRET)
        # stand-ins that say what they ran: the real binaries are never reached by a refused command
        for tool in ("gh", "git"):
            with open(os.path.join(cls.realbin, tool), "w") as f:
                f.write('#!/bin/sh\n'
                        'if [ "$1" = symbolic-ref ]; then\n  case "$*" in *origin/HEAD*) echo "${STANDIN_ORIGIN:-origin/main}" ;; *) echo "${STANDIN_BRANCH:-feature-x}" ;; esac\n  exit 0\nfi\n'
                        'echo "real %s ran: $*"\nexit 0\n' % tool)
            os.chmod(os.path.join(cls.realbin, tool), 0o755)
        cls.env = {"PATH": cls.realbin + ":/usr/bin:/bin", "HOME": cls.home, "WORKER_DOCKER": "0", "CW_RUN_DIR": cls.run_dir,
                   "CW_TMP_DIR": cls.tmpfs, "CW_SHARE_DIR": cls.share, "CW_DEFAULT_BRANCH": "trunk", "GIT_CONFIG_NOSYSTEM": "1"}
        r = subprocess.run(["sh", ENTRYPOINT, "true"], env=cls.env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        cls.bin = os.path.join(cls.tmpfs, "bin")
        cls.conf = json.loads(read(os.path.join(cls.tmpfs, "config", "opencode", "opencode.json")))

    def call(self, cmd, **extra):
        env = dict(self.env, PATH=self.bin + ":" + self.env["PATH"], **extra)
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)

    # -- S3: the wrapper --
    def test_s3_each_of_the_six_is_refused_with_exit_3_and_one_line(self):
        self.assertEqual({c for c, _, _ in SIX}, SIX_CATEGORIES)
        for cat, what, cmd in SIX:
            with self.subTest(cmd=" ".join(cmd[:4])):
                r = self.call(cmd)
                self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
                self.assertEqual(r.stdout, "")
                self.assertEqual(r.stderr, "refused: %s: %s\n" % (what, TAIL))
                self.assertNotIn("real ", r.stdout + r.stderr)   # the real binary never ran

    def test_s3_ordinary_commands_pass_through_unchanged(self):
        for cmd in ORDINARY:
            with self.subTest(cmd=" ".join(cmd[:3])):
                r = self.call(cmd)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.splitlines()[-1].split(" ran: ")[1], " ".join(cmd[1:]))

    def test_s3_a_push_with_no_refspec_follows_the_current_branch(self):
        for cmd in (["git", "push"], ["git", "push", "origin"], ["git", "push", "origin", "HEAD"]):
            with self.subTest(cmd=" ".join(cmd)):
                self.assertEqual(self.call(cmd, STANDIN_BRANCH="main").returncode, 3)
                self.assertEqual(self.call(cmd, STANDIN_BRANCH="trunk").returncode, 3)   # CW_DEFAULT_BRANCH
                self.assertEqual(self.call(cmd, STANDIN_BRANCH="feature-x").returncode, 0)
        # the branch origin/HEAD names is a default branch too
        self.assertEqual(self.call(["git", "push"], STANDIN_BRANCH="develop", STANDIN_ORIGIN="origin/develop").returncode, 3)
        self.assertEqual(self.call(["git", "push", "origin", "develop"], STANDIN_ORIGIN="origin/develop").returncode, 3)
        self.assertEqual(self.call(["git", "push", "origin", "develop"]).returncode, 0)

    def test_s3_more_forms_are_refused_and_value_taking_options_do_not_hide_the_path(self):
        more = [
            ["gh", "pr", "edit", "3", "--add-label", "x"], ["gh", "pr", "edit", "3", "--remove-label=x"],
            ["gh", "api", "graphql", "-f", "query=mutation { mergePullRequest(input: {}) { clientMutationId } }"],
            ["gh", "api", "graphql", "-f", "query=mutation { updateRef(input: {}) { clientMutationId } }"],
            ["git", "push", "--all", "origin"], ["git", "push", "--mirror"], ["git", "push", "-fu", "origin", "feature-x"],
            ["gh", "api", "-X", "POST", "-H", "Accept: application/vnd.github+json", "repos/%s/issues" % REPO, "-f", "title=t"],
            ["gh", "api", "--jq", ".id", "-X", "POST", "repos/%s/labels" % REPO],
            ["gh", "api", "-f", "name=web", "repos/%s/hooks" % REPO],
            ["gh", "api", "--input", "b.json", "-X", "PUT", "repos/%s/pulls/12/merge" % REPO],
            ["gh", "api", "-X", "POST", "repos/%s/merges" % REPO],
            ["gh", "-R", REPO, "pr", "merge", "3"],
        ]
        for cmd in more:
            with self.subTest(cmd=" ".join(cmd[:5])):
                r = self.call(cmd)
                self.assertEqual(r.returncode, 3, r.stderr)
                self.assertEqual(len(r.stderr.splitlines()), 1)
        for cmd in (["gh", "api", "-H", "Accept: x", "repos/%s/pulls/12" % REPO], ["git", "push", "origin", "feature/maintenance"]):
            self.assertEqual(self.call(cmd).returncode, 0, cmd)   # the wrapper does not over-refuse a read or a branch named like main

    def test_s3_an_unsafe_cw_model_refuses_the_start(self):
        r = subprocess.run(["sh", ENTRYPOINT, "echo", "started"], env=dict(self.env, CW_TMP_DIR=self.tmpfs + "-3", CW_MODEL='a","permission":"allow'),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("started", r.stdout)
        self.assertFalse(os.path.exists(self.tmpfs + "-3"))

    def test_u1_the_refusal_line_never_contains_an_argument_value(self):
        for cmd in (["gh", "issue", "comment", "3", "--body", SECRET], ["git", "push", "--force", "origin", SECRET],
                    ["gh", "api", "-X", "PATCH", "repos/%s" % REPO, "-f", "token=" + SECRET], ["git", "push", "origin", "+" + SECRET]):
            r = self.call(cmd)
            self.assertEqual(r.returncode, 3)
            self.assertNotIn(SECRET, r.stdout + r.stderr)
            self.assertNotIn(REPO, r.stdout + r.stderr)
            self.assertEqual(len((r.stdout + r.stderr).splitlines()), 1)

    def test_s3_the_gh_wrapper_still_reads_the_token_at_call_time_and_does_not_leave_it_in_the_environment(self):
        with open(os.path.join(self.realbin, "gh"), "w") as f:
            f.write('#!/bin/sh\necho "token length ${#GH_TOKEN}"\n')
        r = self.call(["gh", "pr", "view"])
        self.assertEqual(r.stdout.strip(), "token length %d" % len(SECRET))
        with open(os.path.join(self.realbin, "gh"), "w") as f:   # restore the stand-in
            f.write('#!/bin/sh\necho "real gh ran: $*"\nexit 0\n')

    def test_s3_a_missing_guard_file_refuses_the_start(self):
        os.remove(os.path.join(self.share, "guard.sh"))
        try:
            r = subprocess.run(["sh", ENTRYPOINT, "echo", "started"], env=dict(self.env, CW_TMP_DIR=self.tmpfs + "-2"),
                               capture_output=True, text=True, timeout=60)
        finally:
            shutil.copy(os.path.join(WORKER, "guard.sh"), os.path.join(self.share, "guard.sh"))
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("started", r.stdout)
        self.assertNotIn(SECRET, r.stdout + r.stderr)

    # -- S3: the agent definition --
    def deny(self, bash):
        return [p for p, v in bash.items() if v == "deny"]

    def test_s3_the_generated_config_denies_the_six_for_the_shell_tool_in_the_default_agent(self):
        self.assertNotIn("default_agent", self.conf)   # a default naming an agent this OpenCode may not register could break the start
        glob = self.conf["permission"]["bash"]
        agent = self.conf["agent"]["worker"]["permission"]["bash"]
        self.assertEqual(glob, agent)   # equality: the same rules, however the agent is chosen
        self.assertEqual(glob["*"], "allow")   # a deny rule comes AFTER the catch-all: the last matching rule wins
        self.assertEqual(list(glob).index("*"), 0)
        denies = self.deny(glob)
        self.assertTrue(denies)
        self.assertTrue(all(v in ("allow", "deny") for v in glob.values()))
        # one table row per category: a command of that category is matched by at least one deny pattern (opencode's `*` matches any text)
        def match(pat, cmdline):
            return re.fullmatch(".*".join(re.escape(x) for x in pat.split("*")), cmdline, re.S) is not None
        for cat, _, cmd in SIX:
            line = " ".join(cmd)
            if "trunk" in line:
                continue   # the configured default is the wrapper's: the config names main and master (README)
            with self.subTest(cmd=line):
                self.assertTrue(any(match(p, line) for p in denies), line)
        self.assertEqual({c for c, _, _ in SIX}, SIX_CATEGORIES)
        for cmd in ORDINARY[:4]:   # and the ordinary ones are not denied
            line = " ".join(cmd)
            self.assertFalse(any(match(p, line) for p in denies), line)

    def test_s3_the_pod_holds_the_worker_agent_definition_with_the_stop_rules_in_prose(self):
        text = read(os.path.join(self.tmpfs, "config", "opencode", "agents", "worker.md"))
        self.assertTrue(text.startswith("---\n"))
        for phrase in ("Never merge", "default branch", "Never force-push", "issues, labels or repository settings", "secret", "stop and report"):
            self.assertIn(phrase, text)
        self.assertNotIn("bypass", text.split("---", 2)[0])

    def test_the_agent_definition_and_readme_say_the_wrapper_is_not_the_guard(self):
        for path in (os.path.join(WORKER, "guard.sh"), os.path.join(WORKER, "..", "README.md")):
            text = read(path)
            self.assertIn("by its path", text, path)
            self.assertRegex(text, r"(?i)not the guard|only a convenience", path)


if __name__ == "__main__":
    unittest.main()
