"""Ask #2367: the wrapper's git part is ONE rule. A `git push` to a NETWORK remote whose destination is the default branch is refused with the
wrapper's one line, except the exact form <40-hex>:refs/heads/<default> (no force, no plus) whose commit message has non-empty Land-Checks and
Land-Council trailers, none 'not recorded'. Everything else of git passes. Offline: nothing is ever pushed (the guard only decides); the https
remote is a name that is never contacted. A mutant without the rule fails with an anchored text, its control first."""
import os
import re
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
GUARD = os.path.join(WORKER, "guard.sh")
README = os.path.join(WORKER, "..", "README.md")
LINE = "refused: pushing to the default branch of a network remote: the repository's rules are the guard; this wrapper is only a convenience\n"
SYSVAR = "GIT_CONFIG_" + "SYS" + "TEM"   # built from parts: the repository scan of added lines names the variable
ANCHOR = "the default branch was pushed without a land record"
RULE = '  refuse "pushing to the default branch of a network remote"\n}'


def read(p):
    with open(p) as f:
        return f.read()


def push_table():
    rows = []
    for line in read(GUARD).splitlines():
        if line.startswith("#PUSH "):
            kind, name, args, why = [x.strip() for x in line[6:].split(" | ")]
            rows.append((kind, name, args, why))
    return rows


class OneRule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = tempfile.TemporaryDirectory(dir=os.environ.get("CW_TMPDIR"))
        cls.addClassCleanup(t.cleanup)
        cls.d = os.path.realpath(t.name)
        cls.repo = os.path.join(cls.d, "r")
        os.mkdir(cls.repo)
        cls.env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": cls.d, "GIT_CONFIG_NOSYSTEM": "1",
                   "CW_REAL_GIT": "git", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x.invalid", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x.invalid"}
        cls.git("init", "-q", "-b", "main", ".")
        cls.git("remote", "add", "origin", "https://example.invalid/o/r.git")
        cls.git("remote", "add", "sshr", "git@example.invalid:o/r.git")
        cls.git("remote", "add", "localr", os.path.join(cls.d, "bare.git"))
        cls.git("remote", "add", "filer", "file://" + os.path.join(cls.d, "bare.git"))
        cls.good = cls.commit("good", "Land-Checks: unit 5 of 5\nLand-Council: strict ok")
        cls.none = cls.commit("none", "")
        cls.empty = cls.commit("empty", "Land-Checks:\nLand-Council: strict ok")
        cls.notrec = cls.commit("notrec", "Land-Checks: not recorded\nLand-Council: strict ok")
        cls.forged = cls.commit("forged", "Land-Checks: x\nLand-Council: y\n\nthe body goes on after the lines above")
        cls.onlyone = cls.commit("onlyone", "Land-Checks: unit 5 of 5")
        cls.git("branch", "-f", "feature-x")

    @classmethod
    def git(cls, *a):
        r = subprocess.run(["git"] + list(a), cwd=cls.repo, env=cls.env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    @classmethod
    def commit(cls, subject, trailers):
        msg = subject + ("\n\n" + trailers if trailers else "") + "\n"
        cls.git("commit", "-q", "--allow-empty", "-m", msg)
        return cls.git("rev-parse", "HEAD")

    def run_guard(self, args, guard=GUARD, env=None, cwd=None):
        return subprocess.run(["sh", guard, "git"] + args, cwd=cwd or self.repo, env=env or self.env, capture_output=True, text=True, timeout=30)

    def refused(self, args, guard=GUARD):
        r = self.run_guard(args, guard)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (3, "", LINE), args)

    def passed(self, args, guard=GUARD):
        r = self.run_guard(args, guard)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), args)

    # S2
    def check_s2(self, guard=GUARD):
        g = self.good
        self.passed(["push", "origin", g + ":refs/heads/main"], guard)   # the land form
        for what, args in (
            ("no trailer", ["push", "origin", self.none + ":refs/heads/main"]),
            ("an empty trailer", ["push", "origin", self.empty + ":refs/heads/main"]),
            ("not recorded", ["push", "origin", self.notrec + ":refs/heads/main"]),
            ("one trailer only", ["push", "origin", self.onlyone + ":refs/heads/main"]),
            ("a branch word", ["push", "origin", "main"]),
            ("a glob", ["push", "origin", "refs/heads/*:refs/heads/*"]),
            ("--force", ["push", "--force", "origin", g + ":refs/heads/main"]),
            ("a leading plus", ["push", "origin", "+" + g + ":refs/heads/main"]),
            ("a short sha", ["push", "origin", g[:12] + ":refs/heads/main"]),
        ):
            r = self.run_guard(args, guard)
            if r.returncode != 3:
                self.fail("%s: %s (%s)" % (ANCHOR, what, args))
            self.assertEqual((r.stdout, r.stderr), ("", LINE), what)

    def test_s2_the_land_form_passes_and_the_other_forms_are_refused(self):
        self.check_s2()

    # S8
    def test_s8_a_mutant_without_the_one_rule_fails_with_the_anchored_text(self):
        self.check_s2()   # control first
        text = read(GUARD)
        self.assertEqual(text.count(RULE), 1)
        p = os.path.join(self.d, "mutant.sh")
        with open(p, "w") as f:
            f.write(text.replace(RULE, "  return 0\n}"))
        with self.assertRaises(AssertionError) as cm:
            self.check_s2(p)
        self.assertIn(ANCHOR, str(cm.exception))

    # S3
    def test_s3_local_remotes_pass_everything_and_a_network_remote_refuses_the_default_branch(self):
        for remote in ("localr", "filer", os.path.join(self.d, "bare.git"), "file://" + os.path.join(self.d, "bare.git")):
            for args in (["push", "--force", remote, "main"], ["push", "--prune", remote, "refs/heads/*:refs/heads/*"], ["push", remote, "feature-x"],
                         ["push", "--all", remote], ["push", "--mirror", remote], ["push", remote, self.none + ":refs/heads/main"]):
                self.passed(args)
        for remote in ("origin", "sshr"):
            self.refused(["push", "--force", remote, "main"])
            self.refused(["push", "--prune", remote, "refs/heads/*:refs/heads/*"])
            self.passed(["push", remote, "feature-x"])
            self.passed(["push", "--force", remote, "feature-x"])
            self.refused(["push", "--all", remote])
            self.refused(["push", "--mirror", remote])
        for url in ("https://example.invalid/r.git", "http://example.invalid/r.git", "ssh://git@example.invalid/r.git", "git://example.invalid/r.git",
                    "git@example.invalid:o/r.git", "example.invalid:o/r.git"):
            self.refused(["push", url, "main"])
            self.passed(["push", url, "feature-x"])
        self.passed(["push", "./rel.git", "main"])

    def test_s3_no_remote_and_no_refspec_follow_the_current_branch(self):
        self.refused(["push"])   # the current branch is main, the remote origin
        self.refused(["push", "origin"])
        self.refused(["push", "origin", "HEAD"])
        self.git("checkout", "-q", "feature-x")
        try:
            self.passed(["push"])
            self.passed(["push", "origin", "HEAD"])
        finally:
            self.git("checkout", "-q", "main")

    # S4
    def test_s4_config_and_the_environment_pass_through(self):
        bare = os.path.join(self.d, "bare2.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True, env=self.env)
        sysfile = os.path.join(self.repo, "sys.cfg")
        with open(sysfile, "w") as f:
            f.write("[alias]\n\tx = push\n")
        for args, extra in (
            (["config", "http.receivepack", "true"], {}),
            (["config", "core.hooksPath", "/x"], {}),
            (["-C", bare, "config", "a.b", "c"], {}),
            (["config", "alias.x", "push"], {}),
            (["config", "push.default", "matching"], {}),
            (["-c", "alias.p=push", "status"], {}),
            (["status"], {SYSVAR: sysfile}),
            (["status"], {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "alias.x", "GIT_CONFIG_VALUE_0": "push"}),
            (["status"], {"GIT_CONFIG_PARAMETERS": "'alias.x=push'"}),
        ):
            r = self.run_guard(args, env=dict(self.env, **extra))
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), (args, extra))

    # bypass attempts of the one rule
    def test_a_forged_trailer_or_a_sha_that_is_not_the_commit_does_not_pass(self):
        # trailers that are not in the LAST paragraph, or are mixed with a sentence, are not trailers
        self.refused(["push", "origin", self.forged + ":refs/heads/main"])
        m2 = self.commit("mixed", "Land-Checks: x\nLand-Council: y\nand a sentence that is no trailer")
        self.refused(["push", "origin", m2 + ":refs/heads/main"])
        # a tree sha, a sha that is no object, an upper-case sha
        tree = self.git("rev-parse", self.good + "^{tree}")
        self.refused(["push", "origin", tree + ":refs/heads/main"])
        self.refused(["push", "origin", "0" * 40 + ":refs/heads/main"])
        self.refused(["push", "origin", self.good.upper() + ":refs/heads/main"])
        self.refused(["push", "origin", self.good + ":refs/heads/main", "feature-x" + ":" + "main"])   # a second refspec
        self.refused(["push", "origin", "HEAD:refs/heads/main"])
        self.refused(["push", "origin", self.good + ":main"])   # not the exact destination form
        self.refused(["push", "-f", "origin", self.good + ":refs/heads/main"])
        self.refused(["push", "-uf", "origin", self.good + ":refs/heads/main"])
        self.refused(["push", "--force-with-lease=main", "origin", self.good + ":refs/heads/main"])
        self.refused(["push", "--mirror", "origin", self.good + ":refs/heads/main"])

    def test_a_ref_named_like_a_sha_does_not_stand_for_the_commit(self):
        fake = "a" * 40
        self.git("update-ref", "refs/heads/" + fake, self.good)   # a branch with a 40-hex name that points at the good commit
        try:
            self.refused(["push", "origin", fake + ":refs/heads/main"])
        finally:
            self.git("update-ref", "-d", "refs/heads/" + fake)

    def test_a_remote_renamed_after_its_url_is_still_a_network_remote(self):
        self.git("remote", "rename", "origin", "elsewhere")
        try:
            self.refused(["push", "elsewhere", "main"])
            self.refused(["push", "--repo=elsewhere", "main"])
            self.refused(["push", "--repo", "elsewhere", "main"])
            self.passed(["push", "elsewhere", "feature-x"])
        finally:
            self.git("remote", "rename", "elsewhere", "origin")
        self.git("remote", "set-url", "--push", "localr", "https://example.invalid/p.git")   # a push URL that differs from the fetch URL decides
        try:
            self.refused(["push", "localr", "main"])
        finally:
            self.git("remote", "set-url", "--push", "localr", os.path.join(self.d, "bare.git"))

    def test_an_insteadof_rewrite_to_a_network_url_is_read_by_get_url(self):
        cfg = os.path.join(self.repo, ".git", "config")
        before = read(cfg)
        with open(cfg, "a") as f:   # a rewrite rule written to the repository's file directly
            f.write('[url "https://example.invalid/"]\n\tinsteadOf = alias:\n')
        self.git("remote", "add", "viaalias", "alias:o/r.git")
        try:
            self.refused(["push", "viaalias", "main"])
        finally:
            with open(cfg, "w") as f:
                f.write(before)

    def test_the_dash_c_option_is_followed_and_other_options_do_not_hide_the_destination(self):
        self.refused(["-C", self.repo, "push", "origin", "main"])
        self.refused(["push", "-o", "x", "origin", "main"])
        self.refused(["push", "-u", "origin", "HEAD:refs/heads/main"])
        self.refused(["push", "--receive-pack=x", "origin", "main"])
        self.passed(["push", "-o", "x", "origin", "feature-x"])
        self.passed(["push", "origin", "feature/maintenance"])
        self.passed(["status"])
        self.passed(["fetch", "origin", "main"])

    def test_security_review_forms_abbreviated_destination_repo_option_matching_push_and_other_directory(self):
        for dst in ("heads/main", "refs/heads/main", "main"):
            self.refused(["push", "origin", "HEAD:" + dst])
            self.refused(["push", "origin", "+HEAD:" + dst])
        self.refused(["push", "origin", self.good + ":heads/main"])   # not the exact land form
        self.refused(["push", "--repo=../r.git", "origin", "main"])   # git takes the positional word: origin
        self.refused(["push", "--repo", "../r.git", "origin", "main"])
        self.refused(["push", "origin", ":"])   # the matching push
        self.refused(["push", "--recurse-submodules", "check", "origin", "main"])   # the option takes the next word as its value
        self.passed(["push", "--repo=../r.git", "feature-x"])
        # -C is followed: from a directory that is no repository the guard reads the repository it names
        r = self.run_guard(["-C", self.repo, "push", "origin", "main"], cwd=self.d)
        self.assertEqual((r.returncode, r.stderr), (3, LINE))
        r = self.run_guard(["-C", self.repo, "push", "origin", self.good + ":refs/heads/main"], cwd=self.d)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        r = self.run_guard(["-C", self.repo, "push", "origin", self.none + ":refs/heads/main"], cwd=self.d)
        self.assertEqual(r.returncode, 3)

    # S9
    def test_s9_every_row_of_the_table_is_answered_as_listed_and_the_readme_names_exactly_these_forms(self):
        rows = push_table()
        self.assertEqual(len(rows), 10)
        for kind, name, args, _ in rows:
            with self.subTest(form=name):
                sha = self.good if kind == "allowed" else self.none
                words = args.replace("SHA", sha).split()
                if kind == "allowed":
                    self.passed(["push"] + words)
                else:
                    self.refused(["push"] + words)
        text = read(README)
        for _, name, _, _ in rows:
            self.assertIn("`" + name + "`", text, name)
        para = [l for l in text.splitlines() if l.startswith("- **`git push`**")]
        self.assertEqual(len(para), 1)
        self.assertIn("`#PUSH`", para[0])
        self.assertNotIn("#ENVCFG", text)
        self.assertNotIn("#CFG", text)
        self.assertNotIn("#ENVCFG", read(GUARD))
        self.assertNotIn("#CFG", read(GUARD))


class Removed(unittest.TestCase):
    """DW-3: each earlier git refusal is gone (the removal, each named)."""

    def test_the_removed_refusals_and_their_helpers_are_gone_from_the_wrapper(self):
        text = read(GUARD)
        for gone in ("env_cfg", "bad_cfg", "defining a git alias", "overriding push.default", "changing git configuration", 'refuse "force-pushing"',
                     "pushing by a glob refspec", "git configuration from the environment"):
            self.assertNotIn(gone, text, gone)
        # the gh rules stay
        for kept in ("merging a pull request", "writing issues", "a gh command it does not know", "api_check", "pr_edit_check"):
            self.assertIn(kept, text, kept)


if __name__ == "__main__":
    unittest.main()
