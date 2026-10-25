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

U = "https://api.github.com/repos/%s" % REPO
# slice-2 review round: (the phrase the refusal gives, the command, also denied by the OpenCode config?). DATA, one row per form.
EXTRA_REFUSED = [
    # M1 aliases (a defined alias would hide the command from the wrapper)
    ("defining or changing a gh alias", ["gh", "alias", "set", "m", "pr merge"], True),
    ("defining or changing a gh alias", ["gh", "alias", "import", "a.yml"], True),
    ("defining or changing a gh alias", ["gh", "alias", "delete", "m"], True),
    ("defining or changing a gh alias", ["gh", "-R", REPO, "alias", "set", "m", "pr merge"], True),
    ("a gh command it does not know (it could be an alias)", ["gh", "m", "5"], False),
    ("a gh command it does not know (it could be an alias)", ["gh", "--repo", REPO, "p", "5"], False),
    ("defining a git alias", ["git", "config", "alias.p", "push --force"], True),
    ("defining a git alias", ["git", "config", "--global", "ALIAS.p", "push --force"], True),
    ("defining a git alias", ["git", "-c", "alias.p=push --force", "p"], True),
    ("defining a git alias", ["git", "--config-env=alias.p=VAR", "p"], False),
    ("defining a git alias", ["git", "--config-env", "alias.p=VAR", "p"], False),
    # M2 a full URL hides the path
    ("merging a pull request", ["gh", "api", U + "/pulls/5/merge", "-X", "PUT"], False),
    ("merging a pull request", ["gh", "api", "https://api.github.com:443/repos/o/r/pulls/5/merge", "-X", "PUT"], False),
    ("merging a pull request", ["gh", "api", "https://user:pw@host.example/repos/o/r/pulls/5/merge?x=1", "-X", "PUT"], False),
    ("merging a pull request", ["gh", "api", "http://h/api/v3/repos/o/r/merges", "-X", "POST"], False),
    ("merging a pull request", ["gh", "api", "//repos/o/r/pulls/5/merge/", "-X", "PUT"], False),
    ("writing issues", ["gh", "api", U + "/issues", "-f", "title=t"], False),
    ("writing labels", ["gh", "api", U + "/labels", "-X", "POST"], False),
    ("a gh api endpoint it cannot read", ["gh", "api", "https://"], False),
    ("a gh api endpoint it cannot read", ["gh", "api", "https:///repos/o/r/pulls/5/merge", "-X", "PUT"], False),
    ("a gh api endpoint it cannot read", ["gh", "api", "repos/o/r/pulls/5/%6derge", "-X", "PUT"], False),
    ("a gh api endpoint it cannot read", ["gh", "api", "repos/o/r/x/../pulls/5/merge", "-X", "PUT"], False),
    ("a gh api endpoint it cannot read", ["gh", "api", "x:y/repos"], False),
    # M3 a GraphQL query that cannot be read here
    ("a GraphQL query it cannot read (from a file or stdin)", ["gh", "api", "graphql", "-F", "query=@q.graphql"], False),
    ("a GraphQL query it cannot read (from a file or stdin)", ["gh", "api", "graphql", "--field", "query=@q.graphql"], False),
    ("a GraphQL query it cannot read (from a file or stdin)", ["gh", "api", "graphql", "-Fquery=@q.graphql"], False),
    ("a GraphQL query it cannot read (from a file or stdin)", ["gh", "api", "graphql", "--input", "q.json"], False),
    ("a GraphQL query it cannot read (from a file or stdin)", ["gh", "api", "graphql", "--input=-"], False),
    ("a GraphQL query it cannot read (from a file or stdin)", ["gh", "api", "graphql"], False),
    ("a GraphQL query it cannot read (from a file or stdin)", ["gh", "api", "graphql", "-f", "query={ viewer { login } }", "--input", "q.json"], False),
    ("a GraphQL query it cannot read (from a file or stdin)", ["gh", "api", "https://api.github.com/graphql", "--input", "-"], False),
    # M4 git push
    ("pushing by a glob refspec", ["git", "push", "origin", "refs/heads/*:refs/heads/*"], False),
    ("pushing by a glob refspec", ["git", "push", "origin", "feature-*"], False),
    ("overriding push.default", ["git", "-c", "push.default=matching", "push", "origin"], True),
    ("overriding push.default", ["git", "config", "push.default", "matching"], True),
    ("changing the gh configuration", ["gh", "config", "set", "git_protocol", "ssh"], False),
    ("defining a git alias", ["git", "config", "alias.p", "status", "--get"], False),
    ("defining a git alias", ["git", "config", "--get", "alias.p", "status"], False),
    ("defining a git alias", ["git", "config", "--add", "alias.p", "status"], False),
    ("defining a git alias", ["git", "config", "--unset", "alias.p"], False),
    ("defining a git alias", ["git", "config", "-l", "alias.p", "x"], False),
    ("defining a git alias", ["git", "config", "--get", "--add", "alias.p", "x"], False),
    ("overriding push.default", ["git", "-c", "PUSH.default=matching", "push"], False),
    ("pushing to the default branch", ["git", "push", "--repo=origin", "trunk"], False),
    ("pushing to the default branch", ["git", "push", "--repo", "origin", "main"], False),
    ("pushing to the default branch", ["git", "push", "--receive-pack=x", "origin", "main"], False),
    ("pushing to the default branch", ["git", "push", "--exec=x", "origin", "main"], False),
    # M5 approving or dismissing a review
    ("approving or dismissing a pull request review", ["gh", "pr", "review", "5", "--approve"], True),
    ("approving or dismissing a pull request review", ["gh", "pr", "review", "-a"], True),
    ("approving or dismissing a pull request review", ["gh", "-R", REPO, "pr", "review", "5", "--request-changes"], True),
    ("approving or dismissing a pull request review", ["gh", "api", "repos/%s/pulls/5/reviews" % REPO, "-X", "POST", "-f", "event=APPROVE"], True),
    ("approving or dismissing a pull request review", ["gh", "api", "repos/%s/pulls/5/reviews" % REPO, "-f", "event=APPROVE"], True),
    ("approving or dismissing a pull request review", ["gh", "api", "-X", "PUT", "repos/%s/pulls/5/reviews/9/dismissals" % REPO], True),
    ("approving or dismissing a pull request review", ["gh", "api", U + "/pulls/5/reviews", "-X", "POST"], True),
    # the cheap lows
    ("writing assignees", ["gh", "pr", "edit", "5", "--add-assignee", "x"], True),
    ("starting workflows or releases", ["gh", "workflow", "run", "ci.yml"], True),
    ("starting workflows or releases", ["gh", "release", "create", "v1"], True),
    ("installing or running a gh extension", ["gh", "extension", "install", "o/gh-x"], True),
    ("starting workflows or releases", ["gh", "api", "repos/%s/dispatches" % REPO, "-X", "POST"], True),
    ("changing repository settings", ["gh", "api", "orgs/acme-org/rulesets", "-X", "POST"], True),
    ("force-pushing or pushing to the default branch", ["gh", "api", "repos/%s/git/refs" % REPO, "-X", "POST", "-f", "ref=refs/heads/x"], True),
    # round 3: short-flag clusters, upper-case paths (table-driven; the refused phrase is part of the row)
    ("merging a pull request", ["gh", "api", "-iX", "PUT", "repos/%s/pulls/5/merge" % REPO], False),
    ("merging a pull request", ["gh", "api", "-iXPUT", "repos/%s/pulls/5/merge" % REPO], False),
    ("merging a pull request", ["gh", "api", "-XPUT", "repos/%s/pulls/5/merge" % REPO], False),
    ("merging a pull request", ["gh", "api", "-i", "repos/%s/pulls/5/merge" % REPO, "-X", "PUT"], False),
    ("a gh api call it cannot read (more than one endpoint)", ["gh", "api", "-pX", "PUT", "repos/%s/pulls/5/merge" % REPO], False),
    ("writing issues", ["gh", "api", "-ifkey=v", "repos/%s/issues" % REPO], False),
    ("writing issues", ["gh", "api", "-iF", "title=t", "repos/%s/issues" % REPO], False),
    ("a gh api option cluster it cannot read", ["gh", "api", "-ik", "repos/%s/issues" % REPO], False),
    ("a gh api option cluster it cannot read", ["gh", "api", "-zX", "PUT", "repos/%s/pulls/5/merge" % REPO], False),
    ("writing issues", ["gh", "api", "-X", "POST", "repos/%s/Issues" % REPO], False),
    ("writing issues", ["gh", "api", "-X", "POST", "repos/%s/ISSUES" % REPO], False),
    ("approving or dismissing a pull request review", ["gh", "api", "-X", "POST", "repos/%s/PULLS/5/REVIEWS" % REPO], False),
    ("writing labels", ["gh", "api", "-X", "POST", "repos/%s/Labels" % REPO], False),
    ("force-pushing or pushing to the default branch", ["gh", "api", "-X", "POST", "REPOS/%s/Git/Refs" % REPO], False),
    ("merging a pull request", ["gh", "api", "-X", "PUT", "Repos/%s/pulls/5/MERGE" % REPO], False),
    ("force-pushing", ["git", "push", "-uf", "origin", "feature-x"], False),
    ("pushing to the default branch", ["git", "push", "-uo", "opt", "origin", "main"], False),
    ("pushing to the default branch", ["git", "push", "-u", "-o", "opt", "origin", "main"], False),
    ("defining a git alias", ["git", "config", "--global", "alias.p", "push -f"], False),
]
EXTRA_ALLOWED = [
    ["gh", "ruleset", "list"],
    ["gh", "alias", "list"],
    ["gh", "pr", "create", "--title", "t"],
    ["gh", "pr", "view", "5"],
    ["gh", "pr", "comment", "5", "--body", "x"],
    ["gh", "api", "graphql", "-f", "query={ viewer { login } }"],
    ["gh", "api", "graphql", "-f", "query=query($o:String!){ repository(owner:$o,name:\"x\"){ id } }", "-F", "o=acme-org"],
    ["gh", "api", U + "/pulls/5"],
    ["gh", "api", "https://api.github.com/repos/o/r/pulls/5/reviews"],
    ["gh", "api", "repos/%s/pulls/5/comments" % REPO, "-f", "body=x"],
    ["gh", "--version"],
    ["gh", "extension", "list"],
    ["git", "push", "origin", "feature-x"],
    ["git", "push", "-u", "origin", "HEAD:feature-x"],
    ["git", "push", "--repo=origin", "feature-x"],
    ["git", "push", "--receive-pack=x", "origin", "feature-x"],
    ["git", "-c", "user.name=x", "commit", "-m", "m"],
    ["git", "config", "user.name", "x"],
    ["git", "push", "origin", "feature/maintenance"],
    ["git", "config", "--get", "alias.p"],
    ["git", "config", "--list"],
    ["git", "config", "-l"],
    ["gh", "config", "get", "git_protocol"],
    ["git", "config", "-l", "--show-origin"],
    ["git", "config", "--get", "--global", "alias.p"],
    ["gh", "api", "-i", "repos/%s/pulls/5" % REPO],
    ["gh", "api", "-iX", "GET", "repos/%s/pulls/5" % REPO],
    ["gh", "api", "-iH", "Accept: x", "repos/%s/pulls/5" % REPO],
    ["gh", "api", "-iq", ".id", "repos/%s/pulls/5" % REPO],
    ["gh", "api", "-X", "POST", "repos/%s/pulls" % REPO, "-f", "title=t"],
    ["git", "push", "-uo", "opt", "origin", "feature-x"],
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
                   "CW_TMP_DIR": cls.tmpfs, "CW_SHARE_DIR": cls.share, "CW_DEFAULT_BRANCH": "trunk", "GIT_CONFIG_NOSYSTEM": "1",
                   "CW_GH_CLIENT": os.path.join(cls.realbin, "gh")}   # the image has no real gh: the stand-in is the client the wrapper runs
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

    def test_review_round_forms_are_refused_by_phrase_and_the_real_binary_never_runs(self):
        for what, cmd, _ in EXTRA_REFUSED:
            with self.subTest(cmd=" ".join(cmd)):
                r = self.call(cmd)
                self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
                self.assertEqual(r.stdout, "")
                self.assertEqual(r.stderr, "refused: %s: %s\n" % (what, TAIL))

    def test_git_environment_configuration_is_refused_and_the_wrapper_leaves_the_workers_own_git_alone(self):
        for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_KEY_0", "GIT_CONFIG_KEY_7", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
            with self.subTest(name=name):
                r = self.call(["git", "push", "origin", "feature-x"], **{name: "1"})
                self.assertEqual(r.returncode, 3, r.stdout)
                self.assertEqual(r.stderr, "refused: git configuration from the environment (GIT_CONFIG_*): %s\n" % TAIL)
                self.assertEqual(self.call(["git", "push", "origin", "feature-x"], **{name: ""}).returncode, 0)   # set but empty: not refused
        self.assertEqual(self.call(["gh", "pr", "view"], GIT_CONFIG_COUNT="1").returncode, 0)   # gh is not git
        self.assertNotIn("GIT_CONFIG_", read(ENTRYPOINT).replace("GIT_CONFIG_NOSYSTEM", ""))   # the entrypoint sets the helper in ~/.gitconfig (a file), never these variables
        self.assertEqual(self.call(["git", "push", "origin", "feature-x"]).returncode, 0)   # GIT_CONFIG_NOSYSTEM (the test's) is not on the list

    def test_review_round_allowed_controls_still_pass_unchanged(self):
        for cmd in EXTRA_ALLOWED:
            with self.subTest(cmd=" ".join(cmd)):
                r = self.call(cmd)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.splitlines()[-1].split(" ran: ")[1], " ".join(cmd[1:]))

    def test_review_round_the_config_denies_what_it_can_express(self):
        glob = self.conf["permission"]["bash"]
        self.assertEqual(glob, self.conf["agent"]["worker"]["permission"]["bash"])
        denies = self.deny(glob)
        def match(pat, line):
            return re.fullmatch(".*".join(re.escape(x) for x in pat.split("*")), line, re.S) is not None
        for _, cmd, in_config in EXTRA_REFUSED:
            if in_config:
                with self.subTest(cmd=" ".join(cmd)):
                    self.assertTrue(any(match(p, " ".join(cmd)) for p in denies), cmd)
        for cmd in EXTRA_ALLOWED:   # and a read or an ordinary call is not denied
            if cmd[:3] in (["gh", "ruleset", "list"], ["gh", "alias", "list"], ["gh", "pr", "create"], ["gh", "pr", "view"], ["git", "push", "-u"]):   # (the config over-refuses a branch named like main; README)
                self.assertFalse(any(match(p, " ".join(cmd)) for p in denies), cmd)

    def test_u1_the_new_refusals_do_not_print_an_argument_value(self):
        for cmd in (["gh", "alias", "set", SECRET, "pr merge"], ["gh", SECRET, "5"], ["gh", "api", "https://" + SECRET + "/repos/o/r/pulls/5/merge", "-X", "PUT"],
                    ["git", "config", "alias." + SECRET, "x"], ["git", "push", "origin", "*" + SECRET]):
            r = self.call(cmd)
            self.assertEqual(r.returncode, 3)
            self.assertNotIn(SECRET, r.stdout + r.stderr)
            self.assertEqual(len((r.stdout + r.stderr).splitlines()), 1)

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


    def test_1019_a_read_of_an_issue_passes_and_a_write_of_the_same_issue_is_refused(self):
        path = "repos/%s/issues/3" % REPO
        for cmd in (["gh", "api", path], ["gh", "api", path + "/comments"], ["gh", "issue", "view", "3"]):
            with self.subTest(read=" ".join(cmd)):
                self.assertEqual(self.call(cmd).returncode, 0, cmd)
        for cmd in (["gh", "api", "-X", "POST", path], ["gh", "api", "-X", "PATCH", path], ["gh", "api", "-X", "POST", path + "/comments"],
                    ["gh", "issue", "comment", "3", "--body", "x"]):
            with self.subTest(write=" ".join(cmd)):
                r = self.call(cmd)
                self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
                self.assertEqual(r.stderr, "refused: writing issues: %s\n" % TAIL)


def cfg_table():
    """the git config table at the top of guard.sh: [(kind, name, args, why)]"""
    rows = []
    for line in read(os.path.join(WORKER, "guard.sh")).splitlines():
        if line.startswith("#CFG "):
            kind, name, args, why = [x.strip() for x in line[5:].split(" | ")]
            rows.append((kind, name, args.split(), why))
    return rows


class GuardConfigForms(unittest.TestCase):
    """#705: every git config write form is refused, from one table; the README names exactly the table's refused forms."""

    @classmethod
    def setUpClass(cls):
        t = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        cls.addClassCleanup(t.cleanup)
        cls.d = os.path.realpath(t.name)
        cls.stub = os.path.join(cls.d, "git")
        with open(cls.stub, "w") as f:
            f.write('#!/bin/sh\necho real git ran\n')
        os.chmod(cls.stub, 0o755)

    def run_guard(self, args, guard=None):
        env = {"PATH": "/usr/bin:/bin", "CW_REAL_GIT": self.stub}
        return subprocess.run(["sh", guard or os.path.join(WORKER, "guard.sh"), "git", "config"] + args, env=env, capture_output=True, text=True, timeout=30)

    def mutant(self, old, new):
        text = read(os.path.join(WORKER, "guard.sh"))
        assert old in text, old
        p = os.path.join(self.d, "mut-%d.sh" % len(os.listdir(self.d)))
        with open(p, "w") as f:
            f.write(text.replace(old, new, 1))
        return p

    def test_s1_s2_every_row_of_the_table_is_answered_as_listed(self):
        rows = cfg_table()
        self.assertTrue([r for r in rows if r[0] == "refused"] and [r for r in rows if r[0] == "allowed"])
        for kind, name, args, _ in rows:
            with self.subTest(name=name):
                r = self.run_guard(args)
                if kind == "refused":
                    self.assertEqual(r.returncode, 3, r.stdout)
                    self.assertEqual(r.stderr, "refused: changing git configuration: %s\n" % TAIL)
                else:
                    self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_identity_exception_is_exactly_two_keys(self):
        for args in (["user.name.x", "v"], ["user.nam", "v"], ["user.name", "a", "b"], ["USER.NAME.x", "v"], ["user.email.x", "v"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_guard(args).returncode, 3)
        for args in (["user.name", "/tmp/include.path"], ["user.email", "push --force"], ["--global", "USER.Name", "x"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_guard(args).returncode, 0)   # a value is just a value
        self.assertEqual(self.run_guard(["user.name", "x", "--add"]).returncode, 3)

    def test_s3_the_readme_names_exactly_the_refused_forms_in_the_table(self):
        names = {n for k, n, _, _ in cfg_table() if k == "refused"}
        para = [l for l in read(os.path.join(WORKER, "..", "README.md")).splitlines() if l.startswith("- **`git config` writes are refused**")]
        self.assertEqual(len(para), 1)
        listed = para[0].split("this list names exactly its refused forms):", 1)[1].split(". The one exception", 1)[0]
        in_readme = set(re.findall(r"`([^`]+)`", listed))
        self.assertEqual(in_readme, names)
        self.assertIn("judged word by word", para[0])
        self.assertIn("user.name", para[0])
        self.assertRegex(para[0], r"convenience, not the guard")

    def test_every_refused_flag_row_has_a_mutant_that_this_table_catches(self):
        text = read(os.path.join(WORKER, "guard.sh"))
        for kind, name, args, _ in cfg_table():
            if kind != "refused" or not name.startswith("-"):
                continue
            with self.subTest(name=name):
                lines = text.splitlines()
                hit = [i for i, l in enumerate(lines) if l.strip().endswith("wr=1 ;;") and name in l.strip().split(")")[0].split("|")]
                self.assertEqual(len(hit), 1, name)
                head, tail = lines[hit[0]].split(")", 1)
                lines[hit[0]] = "|".join(t for t in head.split("|") if t.strip() != name) + ")" + tail
                m = "\n".join(lines).replace("-*) wr=1 ;;", "-*) ;;")   # the catch-all is off too: this member alone must hold
                p = os.path.join(self.d, "mut-%d.sh" % len(os.listdir(self.d)))
                with open(p, "w") as f:
                    f.write(m)
                self.assertEqual(self.run_guard([name, "user.x"], p).returncode, 0, "the mutant must let the form through: %s" % name)

    def test_every_prefix_of_a_write_flag_is_refused(self):
        for kind, name, _, _ in cfg_table():
            if kind == "refused" and name.startswith("--"):
                for n in range(3, len(name)):
                    with self.subTest(flag=name[:n]):
                        self.assertEqual(self.run_guard([name[:n], "user.x"]).returncode, 3)

    def test_every_subcommand_row_and_the_plain_form_have_a_mutant_that_this_table_catches(self):
        for kind, name, args, _ in cfg_table():
            if kind == "refused" and name in ("edit", "set", "unset", "rename-section", "remove-section"):
                with self.subTest(name=name):
                    parts = "edit|set|unset|rename-section|remove-section".split("|")
                    parts.remove(name)
                    p = self.mutant("edit|set|unset|rename-section|remove-section) wr=1", "|".join(parts) + ") wr=1")
                    self.assertEqual(self.run_guard([name], p).returncode, 0)
        p = self.mutant("user.name|user.email) n_ok=1", "*) n_ok=1")
        self.assertEqual(self.run_guard(["user.x", "y"], p).returncode, 0)
        p = self.mutant("-*) wr=1 ;;", "-*) ;;")
        self.assertEqual(self.run_guard(["--uns", "user.x"], p).returncode, 0)

    def test_the_read_forms_and_their_modifiers_still_pass(self):
        for args in (["--get", "--type=bool", "user.x"], ["--bool", "--get", "user.x"], ["--get", "--default", "d", "user.x"], ["get", "user.x"], ["list"], ["--get-regexp", "--global", "x"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_guard(args).returncode, 0)

    READ_MODES = ["--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "--get-color", "--get-colorbool"]

    def write_flags(self):
        return [n for k, n, _, _ in cfg_table() if k == "refused" and n.startswith("-") and n != "an unknown flag or abbreviation"] + ["--uns", "-le", "-el"]

    def test_a_write_flag_with_any_read_mode_is_refused_in_every_position(self):
        flags = self.write_flags()
        self.assertGreater(len(flags), 8)
        for mode in self.READ_MODES:
            for flag in flags:
                for args in ([mode, flag, "user.x"], [flag, mode, "user.x"], [mode, "user.x", flag], [mode, flag], [flag, "user.x", mode]):
                    with self.subTest(args=args):
                        self.assertEqual(self.run_guard(args).returncode, 3)

    def test_a_mutant_that_drops_the_write_flag_check_before_the_read_shortcut_fails_the_pair_test(self):
        p = self.mutant('[ "$wr" = 0 ] && [ "$ok" = 1 ]', '[ "$ok" = 1 ]')
        self.assertEqual(self.run_guard(["--get", "--unset", "user.x"], p).returncode, 0)
        self.assertEqual(self.run_guard(["--list", "--uns"], p).returncode, 0)
        self.assertEqual(self.run_guard(["--get", "--unset", "user.x"]).returncode, 3)

    # ---- #705 review round: parse like git, default deny ----
    REPRO = [   # reproduced against real git: the VALUE looks like a read modifier, everything after the key is a value
        ["core.pager", "--default=x;touch/tmp/p"], ["core.fsmonitor", "--type=x;touch/tmp/p"], ["include.path", "--bool"],
        ["user.x", "--global"], ["core.editor", "--path"], ["user.name", "--global"], ["user.email", "-x"],
    ]

    def test_flag_shaped_values_after_the_key_are_refused_by_name(self):
        for args in self.REPRO:
            with self.subTest(args=args):
                r = self.run_guard(args)
                self.assertEqual(r.returncode, 3)
                self.assertEqual(r.stdout, "")

    ARITY = {"--get": (1, 1), "--get-all": (1, 1), "--get-regexp": (1, 1), "--list": (0, 0), "-l": (0, 0),
             "--get-urlmatch": (1, 2), "--get-color": (1, 2), "--get-colorbool": (1, 2)}
    MODIFIERS = {"--global", "--local", "-z", "--show-origin", "--bool", "--path", "--type=x", "--default=x"}
    WORDS = ["core.pager", "user.name", "include.path", "v", "--global", "--get", "--list", "-l", "--bool", "--default=x", "--unset"]

    def oracle(self, words):
        """the specification, written apart from the wrapper: True when the shape is a read or an identity write"""
        i = 0
        while i < len(words) and words[i].startswith("-"):
            i += 1
        opts, rest = words[:i], words[i:]
        if any(w.startswith("-") for w in rest):
            return False
        modes = [o for o in opts if o in self.ARITY]
        if len(modes) > 1 or any(o not in self.ARITY and o not in self.MODIFIERS for o in opts):
            return False
        if modes:
            lo, hi = self.ARITY[modes[0]]
            return lo <= len(rest) <= hi
        return len(rest) == 1 or (len(rest) == 2 and rest[0] in ("user.name", "user.email"))

    def shapes(self):
        import itertools
        out = []
        for n in (1, 2, 3):
            out += [list(p) for p in itertools.product(self.WORDS, repeat=n)]
        small = ["user.name", "v", "--global", "--get", "--unset"]
        out += [list(p) for p in itertools.product(small, repeat=4)]
        return out

    def test_every_shape_up_to_four_words_is_allowed_only_if_it_is_a_read_or_an_identity_write(self):
        from concurrent.futures import ThreadPoolExecutor
        shapes = self.shapes()
        with ThreadPoolExecutor(8) as ex:
            rcs = list(ex.map(lambda a: self.run_guard(a).returncode, shapes))
        type(self).RCS = dict(zip(map(tuple, shapes), rcs))
        bad = [(a, rc) for a, rc in zip(shapes, rcs) if (rc == 0) != self.oracle(a) or rc not in (0, 3)]
        self.assertEqual(bad[:10], [])
        self.assertTrue(any(rc == 0 for rc in rcs) and any(rc == 3 for rc in rcs))

    def test_the_allowed_shapes_leave_the_configuration_of_real_git_unchanged(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("no git")
        home = os.path.join(self.d, "home"); repo = os.path.join(self.d, "repo")
        os.makedirs(home); os.makedirs(repo)
        env = {"PATH": "/usr/bin:/bin", "HOME": home, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.path.join(home, ".gitconfig")}
        subprocess.run([git, "init", "-q"], cwd=repo, env=env, check=True, capture_output=True)
        subprocess.run([git, "config", "user.x", "1"], cwd=repo, env=env, check=True, capture_output=True)
        files = [os.path.join(repo, ".git", "config"), os.path.join(home, ".gitconfig")]
        before = [read(f) if os.path.exists(f) else None for f in files]
        ran = 0
        for args in self.shapes():
            rc = self.RCS.get(tuple(args)) if hasattr(self, "RCS") else self.run_guard(args).returncode
            i = 0
            while i < len(args) and args[i].startswith("-"):
                i += 1
            if rc == 0 and len(args[i:]) != 2:   # (an identity write is allowed to change the configuration)
                subprocess.run([git, "config"] + args, cwd=repo, env=env, capture_output=True, timeout=30)
                ran += 1
        self.assertGreater(ran, 20)
        self.assertEqual(before, [read(f) if os.path.exists(f) else None for f in files])

    def test_each_rule_of_the_parser_has_a_mutant_that_dies(self):
        p = self.mutant('"") if [ "$pos" = 1 ]; then n_ok=1; elif', '"") if [ "$pos" -ge 1 ]; then n_ok=1; elif')   # the arity of the no-mode form dropped
        self.assertEqual(self.run_guard(["core.pager", "v", "w"], p).returncode, 0)
        p = self.mutant("--get|--get-all|--get-regexp|get) [ \"$pos\" = 1 ] && n_ok=1", "--get|--get-all|--get-regexp|get) n_ok=1")   # a read mode's arity dropped
        self.assertEqual(self.run_guard(["--get", "a", "b"], p).returncode, 0)
        p = self.mutant("case \"$a\" in -*) ok=0 ;; esac", "case \"$a\" in -*) ;; esac")   # options after the key accepted
        self.assertEqual(self.run_guard(["user.name", "--global"], p).returncode, 0)
        p = self.mutant("case \"$a\" in -*) ok=0 ;; esac", "case \"$a\" in --global|--bool|--path|--type=*|--default=*) continue ;; -*) ok=0 ;; esac")   # the old modifier list restored
        for args in (["include.path", "--bool"], ["user.x", "--global"], ["core.editor", "--path"], ["core.pager", "--default=x;t"], ["core.fsmonitor", "--type=x;t"]):
            self.assertEqual(self.run_guard(args, p).returncode, 0, args)
            self.assertEqual(self.run_guard(args).returncode, 3, args)

    def test_a_mutant_that_widens_the_identity_exception_is_caught(self):
        p = self.mutant("user.name|user.email)", "user.*)")
        self.assertEqual(self.run_guard(["user.name.x", "v"], p).returncode, 0)   # the mutant passes it ...
        self.assertEqual(self.run_guard(["user.name.x", "v"]).returncode, 3)      # ... the real wrapper does not

    def test_the_agent_definition_and_config_name_the_same_forms(self):
        text = read(os.path.join(WORKER, "agents", "worker.md"))
        for phrase in ("--add", "--unset", "--edit", "--rename-section", "--remove-section", "include.path", "user.name", "user.email"):
            self.assertIn(phrase, text)
        conf = json.loads(read(os.path.join(WORKER, "opencode-worker.json")))
        denies = [p for p, v in conf["permission"]["bash"].items() if v == "deny"]
        def match(pat, line):
            return re.fullmatch(".*".join(re.escape(x) for x in pat.split("*")), line, re.S) is not None
        for kind, name, args, _ in cfg_table():
            line = "git config " + " ".join(args)
            if kind == "refused" and name not in ("KEY VALUE", "an unknown flag or abbreviation", "a value that looks like an option", "a value word after a read mode's key", "an option after the get or list subcommand"):
                with self.subTest(name=name):
                    self.assertTrue(any(match(p, line) for p in denies), line)
            if kind == "allowed":
                self.assertFalse(any(match(p, line) for p in denies), line)


if __name__ == "__main__":
    unittest.main()
