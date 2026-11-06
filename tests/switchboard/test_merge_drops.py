"""#2159: a merge resolution never silently drops a line of the tip. Fixture git repositories (neutral names); S2, S3, S4 and U1 of the plan.

HOST is bin/test-switchboard (set by its loader block): `HOST.sb` is the entry module."""
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HOST = None   # set by bin/test-switchboard


def git(cwd, *argv):
    done = subprocess.run(["git", "-C", str(cwd)] + list(argv), capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


class MergeDropsBase(unittest.TestCase):
    def setUp(self):
        if HOST is None:
            self.skipTest("the entry is loaded by bin/test-switchboard")
        self.sb = HOST.sb
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        git(self.dir, "init", "-q", "-b", "integration")
        self.write("app.py", "".join("line %d\n" % n for n in range(1, 11)))
        self.write("other.py", "keep one\nkeep two\n")
        self.commit("base")

    def write(self, name, text):
        (self.dir / name).write_text(text)

    def commit(self, msg):
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", msg)

    def tip_moves(self):
        """The tip adds a line `fact_sources HeldSendFacts` to app.py after the branch left it."""
        git(self.dir, "checkout", "-q", "integration")
        self.write("app.py", "".join("line %d\n" % n for n in range(1, 6)) + "HeldSendFacts(state_dir)\n" + "".join("line %d\n" % n for n in range(6, 11)))
        self.commit("tip adds a line")

    def run_cmd(self, branch="pr"):
        args = types.SimpleNamespace(git_dir=str(self.dir), tip="integration", branch=branch)
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(str(x) for x in a))):
            code = self.sb.cmd_merge_drops(args, subprocess.run, None)
        return code, out


class TestMergeDrops(MergeDropsBase):
    def merge_dropping(self, message="merge the tip"):
        git(self.dir, "checkout", "-q", "-b", "pr", "integration~1")
        self.write("other.py", "keep one\nkeep two\nown addition\n")
        self.commit("the change")
        git(self.dir, "merge", "-q", "--no-commit", "integration")
        self.write("app.py", "".join("line %d\n" % n for n in range(1, 11)))   # the resolution loses the tip's line
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", message)

    def test_s2_a_merge_that_drops_a_tip_line_is_listed(self):
        self.tip_moves()
        self.merge_dropping()
        code, out = self.run_cmd()
        self.assertEqual(code, 1, out)
        self.assertEqual(len(out), 1, out)
        self.assertIn("app.py", out[0])
        self.assertIn("HeldSendFacts(state_dir)", out[0])

    def test_a_pure_revert_of_a_tip_commit_on_a_branch_on_the_tip_passes(self):
        self.tip_moves()
        git(self.dir, "checkout", "-q", "-b", "pr")
        git(self.dir, "revert", "--no-edit", "integration")
        self.assertEqual(self.run_cmd(), (0, []))

    def test_a_branch_on_the_tip_that_merged_an_older_side_and_dropped_a_tip_line_is_refused(self):
        git(self.dir, "checkout", "-q", "-b", "old")
        self.write("other.py", "keep one\nkeep two\nown addition\n")
        self.commit("older side")
        git(self.dir, "checkout", "-q", "integration")
        self.tip_moves()
        git(self.dir, "checkout", "-q", "-b", "pr")
        git(self.dir, "merge", "-q", "--no-commit", "--no-ff", "old")
        self.write("app.py", "".join("line %d\n" % n for n in range(1, 11)))
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", "merge the older side")
        code, out = self.run_cmd()
        self.assertEqual(code, 1, out)
        self.assertIn("HeldSendFacts(state_dir)", out[0])

    def test_s3_a_removal_of_the_branch_itself_passes(self):
        git(self.dir, "checkout", "-q", "-b", "pr")
        self.write("other.py", "keep one\n")   # the pull request means to remove `keep two`
        self.commit("remove a line")
        self.assertEqual(self.run_cmd()[0], 0)

    def test_s3_a_hunk_named_in_the_merge_message_passes(self):
        self.tip_moves()
        self.merge_dropping("merge the tip; drops app.py: HeldSendFacts(state_dir) on purpose")
        self.assertEqual(self.run_cmd()[0], 0)

    def test_s3_a_branch_behind_the_tip_is_not_judged_for_the_tip_s_newer_lines(self):
        self.tip_moves()
        git(self.dir, "checkout", "-q", "-b", "pr", "integration~1")
        self.write("other.py", "keep one\nkeep two\nown addition\n")
        self.commit("the change")
        self.assertEqual(self.run_cmd()[0], 0)

    def test_a_branch_behind_a_tip_that_reindents_and_moves_lines_is_clean(self):
        git(self.dir, "checkout", "-q", "-b", "pr")
        self.write("other.py", "keep one\nkeep two\nown addition\n")
        self.commit("the change")
        git(self.dir, "checkout", "-q", "integration")
        self.write("app.py", "    line 1\n" + "".join("line %d\n" % n for n in range(3, 11)) + "line 2\n")
        self.commit("tip re-indents and moves")
        self.assertEqual(self.run_cmd(), (0, []))

    def test_a_second_dropped_return_none_is_flagged_when_the_branch_removed_only_one(self):
        two = "def a():\n    return None\ndef b():\n    return None\nz = 1\n"
        self.write("app.py", two)
        self.commit("two returns")
        git(self.dir, "checkout", "-q", "-b", "pr")
        self.write("app.py", "def a():\n    return None\ndef b():\n    pass\nz = 1\n")   # the branch removes one `return None`
        self.commit("own removal")
        git(self.dir, "checkout", "-q", "integration")
        self.write("app.py", two + "def c():\n    return None\n")
        self.commit("tip adds a function")
        git(self.dir, "checkout", "-q", "pr")
        git(self.dir, "merge", "-q", "--no-commit", "integration")
        self.write("app.py", "def a():\n    return None\ndef b():\n    pass\nz = 1\n")   # the merge drops the tip's own new function, whole
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", "merge the tip")
        code, out = self.run_cmd()
        self.assertEqual(code, 1, out)
        self.assertEqual(len(out), 1, out)
        self.assertIn("def c()", out[0])

    def test_the_branchs_own_removal_behind_the_tip_is_not_a_drop(self):
        git(self.dir, "checkout", "-q", "-b", "pr")
        self.write("app.py", "".join("line %d\n" % n for n in range(1, 11) if n != 3))   # the branch's own removal
        self.commit("own removal")
        self.tip_moves()   # the tip keeps line 3 and moves on
        self.assertEqual(self.run_cmd(), (0, []))

    def test_the_branchs_own_removal_made_in_a_merge_resolution_is_not_a_drop(self):
        git(self.dir, "checkout", "-q", "-b", "pr")
        self.write("other.py", "keep one\nkeep two\nown addition\n")
        self.commit("the change")
        self.tip_moves()
        git(self.dir, "branch", "-f", "mid", "integration")
        git(self.dir, "checkout", "-q", "integration")   # the tip moves on again after the branch took `mid`
        self.write("third.py", "tip tail\n")
        self.commit("tip moves again")
        git(self.dir, "checkout", "-q", "pr")
        git(self.dir, "merge", "-q", "--no-commit", "mid")
        self.write("app.py", "".join("line %d\n" % n for n in range(1, 6)) + "HeldSendFacts(state_dir)\n" + "".join("line %d\n" % n for n in range(6, 11) if n != 8))   # the port removes line 8: the PR's own change
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", "merge the tip; port")
        self.assertEqual(self.run_cmd()[0], 0)

    def test_a_line_the_branch_modified_then_merged_the_tip_is_not_a_drop(self):
        git(self.dir, "checkout", "-q", "-b", "pr")
        self.write("app.py", "".join(("line 3 extended, extra=None\n" if n == 3 else "line %d\n" % n) for n in range(1, 11)))   # a modified line: a removal and an addition
        self.commit("modify line 3")
        self.tip_moves()
        git(self.dir, "checkout", "-q", "pr")
        git(self.dir, "merge", "-q", "-m", "merge the tip", "integration")
        self.assertEqual(self.run_cmd(), (0, []))   # the tip is wholly merged in: the head and the tip share it

    def test_a_tip_line_the_branch_extended_in_a_conflict_resolution_is_not_a_drop(self):
        git(self.dir, "checkout", "-q", "-b", "pr")
        self.write("app.py", "".join(("line 3 mine\n" if n == 3 else "line %d\n" % n) for n in range(1, 11)))
        self.commit("modify line 3")
        git(self.dir, "checkout", "-q", "integration")
        self.write("app.py", "".join(("line 3 tip\n" if n == 3 else "line %d\n" % n) for n in range(1, 11)))
        self.commit("tip modifies line 3")
        git(self.dir, "checkout", "-q", "pr")
        git(self.dir, "merge", "-q", "--no-commit", "integration") if False else subprocess.run(["git", "-C", str(self.dir), "merge", "-q", "--no-commit", "integration"], capture_output=True)
        self.write("app.py", "".join(("line 3 tip, extra=None\n" if n == 3 else "line %d\n" % n) for n in range(1, 11)))   # the PR's extension of the tip's line
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", "merge the tip")
        self.assertEqual(self.run_cmd(), (0, []))

    def test_a_tip_line_added_after_the_branchs_last_merge_is_not_a_drop(self):
        git(self.dir, "checkout", "-q", "-b", "pr")
        self.write("app.py", "".join(("line 3 extended\n" if n == 3 else "line %d\n" % n) for n in range(1, 11)))
        self.commit("modify line 3")
        self.tip_moves()
        git(self.dir, "checkout", "-q", "pr")
        git(self.dir, "merge", "-q", "-m", "merge the tip", "integration")
        git(self.dir, "checkout", "-q", "integration")   # the tip moves after the re-merge, in the file the branch touched
        self.write("app.py", "".join("line %d\n" % n for n in range(1, 6)) + "HeldSendFacts(state_dir)\n" + "".join("line %d\n" % n for n in range(6, 11)) + "with cloud.Cluster() as cluster:\n")
        self.commit("tip moves again")
        self.assertEqual(self.run_cmd(), (0, []))

    def test_the_founding_case_a_resolution_rewriting_a_tip_line_that_drops_an_element_is_refused(self):
        self.write("f.py", "a = 1\nfact_sources = [Held(s), Other(s), Third(s)]\nb = 2\n")
        self.write("g.py", "g = 1\n")
        self.commit("f and g")
        git(self.dir, "checkout", "-q", "-b", "pr", "HEAD")
        self.write("g.py", "g = 2\n")
        self.commit("the PR's own change to g")
        git(self.dir, "checkout", "-q", "integration")
        self.write("f.py", "a = 1\nfact_sources = [Held(s), Other(s), Third(s)]\nb = 3\n")
        self.commit("tip modifies f elsewhere")
        git(self.dir, "checkout", "-q", "pr")
        git(self.dir, "merge", "-q", "--no-commit", "integration")
        self.write("f.py", "a = 1\nfact_sources = [Other(s), Third(s)]\nb = 3\n")
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", "merge the tip")
        code, out = self.run_cmd()
        self.assertEqual(code, 1)
        self.assertIn("f.py", out[0])
        self.assertIn("fact_sources = [Held(s)", out[0])

    def test_a_conflicting_merge_says_resolve_first(self):
        git(self.dir, "checkout", "-q", "-b", "pr")
        self.write("app.py", "mine\n")
        self.commit("mine")
        git(self.dir, "checkout", "-q", "integration")
        self.write("app.py", "theirs\n")
        self.commit("theirs")
        code, out = self.run_cmd()
        self.assertEqual((code, out), (2, ["conflicts, resolve first"]))

    def test_u1_a_merge_that_keeps_the_tip_whole_is_not_refused(self):
        self.tip_moves()
        git(self.dir, "checkout", "-q", "-b", "pr", "integration~1")
        self.write("other.py", "keep one\nkeep two\nown addition\n")
        self.commit("the change")
        git(self.dir, "merge", "-q", "-m", "merge the tip", "integration")
        code, out = self.run_cmd()
        self.assertEqual((code, out), (0, []))


class TestMergeDropsStep(MergeDropsBase):
    def refusal(self):
        git(self.dir, "update-ref", "refs/heads/base", "integration")
        local = str(self.dir)

        def runner(argv, **kw):
            if argv[:1] == ["git"] and "fetch" in argv:   # the fetch reads the fixture, never the network
                argv = argv[:argv.index("fetch") + 2] + [local, "+refs/heads/base:refs/tips/base", "+refs/heads/pr:refs/tips/head"]
            return subprocess.run(argv, **kw)
        gh = lambda argv, own=True: (0, '{"baseRefName": "base", "headRefOid": "x"}', "")
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        with mock.patch.object(self.sb, "merge_gate_gh", lambda config, run, repo: gh):
            return self.sb.merge_drops_refusal(runner, {}, "acme/api", 7, state.name)

    def test_s4_the_merge_step_refuses_a_pull_request_that_drops_a_tip_line_naming_the_file(self):
        TestMergeDrops.tip_moves(self)
        TestMergeDrops.merge_dropping(self)
        text = self.refusal()
        self.assertIsNotNone(text)
        self.assertEqual(text.count("app.py"), 1, text)

    def test_u1_the_merge_step_refuses_nothing_for_a_clean_merge(self):
        TestMergeDrops.tip_moves(self)
        git(self.dir, "checkout", "-q", "-b", "pr", "integration~1")
        self.write("other.py", "keep one\nkeep two\nown addition\n")
        self.commit("the change")
        git(self.dir, "merge", "-q", "-m", "merge the tip", "integration")
        self.assertIsNone(self.refusal())
