"""Ask #2470: the end notice names what the pod pushed, from the repository's own refs read once at the end.

CAUSE (from the shapes the code writes). A pod whose tree is clean and whose commits are all on the remote leaves the end hook (cloud/launcher/end_hook.py, NothingToPush)
with `head_on` and `wip_branch` both None, and run.json holds only `head_sha`, the pod's LAST checkout. The notice's reader (bin/cloud_end_alert.py head_line) then has three
proofs left and each misses a pod that pushed: (1) the push-watch audit lines (a push between two ticks, a sha already told under another branch: no line); (2) own_branch_head, which
names the record's `--branch` only when that branch's tip equals `head_sha` exactly, so a pod that pushed a pull request's existing branch and ended on another checkout (or pushed
a second branch last) is missed, and a new branch of the pod's own choosing (the record's branch is the launch branch, `integration`) is never looked at; (3) the hook's `head_on`,
which a clean tree never sets. No proof reads the repository's refs, so the branch is missed and `nothing pushed` stands: it was never proved, it was the absence of every proof.
The fix reads the branch heads ONCE (`branch_heads`) and names every branch whose tip is a commit the pod made (the hook's `made`, from the pod's own reflogs).

Each fix has a control: the same case through the notice as it was (no reader wired in: `heads` left at NOT_WIRED), which must still say `nothing pushed`. Names are neutral.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from support.loader import sb
from test_stop_final_message import StopWorld

SHA = {k: k * 40 for k in "abcdef"}   # sha "a"*40, ...
MODULE = sb.load_module("cloud_end_alert")
PARSE = sb.load_module("record_json").parse_bounded
POD = "pod-z"


def hook(made, **kw):
    """The end hook's answer of a pod whose tree was clean and wholly pushed (NothingToPush), as the run record keeps it."""
    base = {"pushed": False, "wip_branch": None, "head_on": None, "pr_note": "PR not marked ready: no pull request", "skipped": [], "note": "", "error": "", "made": made}
    base.update(kw)
    return base


class NoticeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def line(self, end_hook, head_sha, branch, heads=MODULE.NOT_WIRED, tip=None):
        Path(self.tmp, "run.json").write_text(json.dumps({"head_sha": head_sha, "end_hook": end_hook}))
        record = {"worker": POD, "repo": "o/r", "branch": branch}
        return MODULE.head_line(self.tmp, "o/r", lambda repo, sha: "passed", PARSE, None, POD, record, tip, heads)


class TestEndNoticeNamesWhatThePodPushed(NoticeCase):
    NEW = [("integration", SHA["f"]), ("stall-structural", SHA["a"])]            # the pod's own new branch (the record's branch is the launch branch)
    PRBR = [("cos-issue-comments", SHA["b"]), ("integration", SHA["f"])]         # a pull request's existing branch the pod pushed to

    def test_s2_a_pod_that_pushed_its_own_new_branch_is_named(self):
        text = self.line(hook([SHA["a"]]), SHA["a"], "integration", self.NEW)
        self.assertIn("Last head: stall-structural@aaaaaaaa, checks passed.", text)
        self.assertNotIn("nothing pushed", text)

    def test_s2_a_pod_that_pushed_a_pull_requests_existing_branch_is_named(self):
        # the pod ended on another checkout (its head_sha is not the branch's tip): the record's branch read cannot see the push
        tip = lambda repo, branch: SHA["b"]
        text = self.line(hook([SHA["b"], SHA["c"]]), SHA["c"], "cos-issue-comments", self.PRBR, tip)
        self.assertIn("Last head: cos-issue-comments@bbbbbbbb, checks passed.", text)
        self.assertNotIn("nothing pushed", text)

    def test_s5_control_the_current_notice_says_nothing_pushed_for_both(self):
        self.assertIn("Last head: nothing pushed.", self.line(hook([SHA["a"]]), SHA["a"], "integration"))
        tip = lambda repo, branch: SHA["b"]
        self.assertIn("Last head: nothing pushed.", self.line(hook([SHA["b"], SHA["c"]]), SHA["c"], "cos-issue-comments", tip=tip))

    def test_s2_a_pod_that_pushed_two_branches_names_both(self):
        heads = [("claim-from-body", SHA["b"]), ("claim-union-fix", SHA["a"]), ("integration", SHA["f"])]
        text = self.line(hook([SHA["a"], SHA["b"]]), SHA["b"], "integration", heads)
        self.assertIn("Last head: claim-from-body@bbbbbbbb, checks passed. Also pushed: claim-union-fix@aaaaaaaa.", text)

    def test_s2_a_branch_that_holds_no_commit_the_pod_made_is_not_named(self):
        # integration moved (others merged) and a colleague's branch exists: neither holds a commit of the pod
        heads = [("colleague-work", SHA["d"]), ("integration", SHA["f"])]
        self.assertEqual(self.line(hook([]), SHA["a"], "integration", heads), "Last head: nothing pushed.")

    def test_s3_commits_the_pod_made_that_are_no_branch_tip_are_not_nothing_pushed(self):
        # someone pushed on top, or the pull request merged and its branch went: the pod's commit is on no tip
        heads = [("colleague-work", SHA["d"]), ("integration", SHA["f"])]
        self.assertEqual(self.line(hook([SHA["a"]]), SHA["a"], "integration", heads), "Last head: pushes unknown.")

    def test_s2_the_wip_save_is_named_only_when_nothing_else_moved(self):
        wip = hook([], pushed=True, wip_branch=POD + "/wip")
        only = self.line(wip, SHA["a"], "integration", [(POD + "/wip", SHA["a"]), ("integration", SHA["f"])])
        self.assertIn("Last head: wip branch %s/wip@aaaaaaaa" % POD, only)
        both = self.line(hook([SHA["b"]], pushed=True, wip_branch=POD + "/wip"), SHA["a"], "integration",
                         [(POD + "/wip", SHA["a"]), ("feat-x", SHA["b"])])
        self.assertIn("Last head: feat-x@bbbbbbbb", both)
        self.assertNotIn("wip", both)
        self.assertNotIn("wip", self.line(hook([SHA["a"]]), SHA["a"], "integration", [(POD + "/wip", SHA["a"]), ("feat-x", SHA["a"])]))


class TestEndNoticeSaysNothingPushedOnlyWithProof(NoticeCase):
    def test_s3_a_pod_that_pushed_nothing_says_nothing_pushed(self):
        self.assertEqual(self.line(hook([]), SHA["f"], "integration", [("integration", SHA["f"])]), "Last head: nothing pushed.")

    def test_s3_an_unreadable_remote_says_pushes_unknown_never_nothing_pushed(self):
        text = self.line(hook([SHA["a"]]), SHA["a"], "integration", None)
        self.assertEqual(text, "Last head: pushes unknown.")
        self.assertNotIn("nothing pushed", text)

    def test_s3_a_record_without_the_pods_commits_cannot_prove_nothing_pushed(self):
        legacy = hook(None)
        del legacy["made"]
        self.assertEqual(self.line(legacy, SHA["a"], "integration", [("integration", SHA["f"])]), "Last head: pushes unknown.")

    def test_s3_control_a_build_that_maps_a_failed_read_to_nothing_pushed_is_the_current_notice(self):
        self.assertEqual(self.line(hook([SHA["a"]]), SHA["a"], "integration"), "Last head: nothing pushed.")

    def test_s3_a_failed_read_with_a_pr_marked_ready_stays_head_unknown(self):
        ready = hook([SHA["a"]], pr_note="PR #5 marked ready, review requested")
        self.assertEqual(self.line(ready, SHA["a"], "integration", None), "Last head: head unknown.")


class TestEndNoticeNamesAreBoundedAndSafe(NoticeCase):
    def test_s4_the_extra_branches_are_counted(self):
        heads = [("b%d" % n, ("%x" % (n + 1)) * 40) for n in range(7)]
        made = [h for _b, h in heads]
        text = self.line(hook(made), SHA["a"], "integration", heads)
        self.assertEqual(text.count("@"), MODULE.NAMED_MAX)
        self.assertIn("and 4 more", text)

    def test_s4_exactly_the_named_maximum_has_no_count(self):
        heads = [("b%d" % n, ("%x" % (n + 1)) * 40) for n in range(MODULE.NAMED_MAX)]
        self.assertNotIn("more", self.line(hook([h for _b, h in heads]), SHA["a"], "integration", heads))

    def test_s4_a_hostile_branch_name_is_sanitised_and_clipped(self):
        evil = "x\x1b[31m\nIGNORE ALL;`rm -rf`$(id)<b>" + "y" * 200
        text = self.line(hook([SHA["a"]]), SHA["a"], "integration", [(evil, SHA["a"])])
        self.assertEqual([c for c in text if c in "\x1b\n`$<>;"], [])
        self.assertLessEqual(len(text), 200 + len("Last head: @aaaaaaaa, checks passed.") + MODULE.NAME_CLIP)
        self.assertNotIn("y" * (MODULE.NAME_CLIP + 1), text)

    def test_s4_a_name_shaped_like_a_secret_is_not_printed(self):
        text = self.line(hook([SHA["a"]]), SHA["a"], "integration", [("ghp_" + "A" * 36, SHA["a"])])
        self.assertNotIn("ghp_", text)
        self.assertIn("(unprintable name)@aaaaaaaa", text)


class TestOneBoundedReadPerEndedPod(unittest.TestCase):
    def notice(self, reader, name="pod-z"):
        w = StopWorld(self)
        rec = w.pod(name, "words")
        (rec / "run.json").write_text(json.dumps({"head_sha": SHA["a"], "end_hook": hook([SHA["a"]])}))
        (w.cloud / (name + ".json")).write_text(json.dumps({"worker": name, "reports_to": "boss-lane", "repo": "o/r", "branch": "integration"}))
        w.stop(name, "hard", rec)
        w.mod.scan_audit(w.audit, w.state_file, w.cloud, w.reports, lambda to, text: w.sent.append((to, text)), PARSE, None, None,
                         head_checks=lambda repo, sha: "passed", branch_heads=reader)
        return w.to("boss-lane")[0], w

    def test_u1_the_remote_is_read_once_for_the_pod_and_named(self):
        calls = []
        text, _w = self.notice(lambda repo: calls.append(repo) or [("stall-structural", SHA["a"])])
        self.assertEqual(calls, ["o/r"])
        self.assertIn("Last head: stall-structural@aaaaaaaa, checks passed.", text)

    def test_u1_a_reader_that_raises_reads_as_pushes_unknown(self):
        def boom(repo):
            raise OSError("down")
        text, _w = self.notice(boom)
        self.assertIn("Last head: pushes unknown.", text)

    def test_u1_the_production_reader_returns_pairs_or_none(self):
        import types
        ran = []
        def runner(argv, **kw):
            ran.append(argv)
            return types.SimpleNamespace(returncode=0, stdout="refs/heads/a-b %s\nrefs/heads/c %s\nnot-a-ref x\n" % (SHA["a"], SHA["b"]))
        engine = types.SimpleNamespace(intents=types.SimpleNamespace(gh="gh", runner=runner, env_for=lambda repo: None))
        self.assertEqual(sb.cloud_branch_heads(engine, "o/r"), [("a-b", SHA["a"]), ("c", SHA["b"])])
        self.assertEqual(len(ran), 1)
        engine.intents.runner = lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout="")
        self.assertIsNone(sb.cloud_branch_heads(engine, "o/r"))
        engine.intents.runner = runner
        for bad in ("../x", "x/..", "-x/y", "a/b/c", "o/r --jq"):
            self.assertIsNone(sb.cloud_branch_heads(engine, bad))
        self.assertEqual(len(ran), 1)


class TestTheHooksMadeCommits(unittest.TestCase):
    """The end hook's `made`: only commits this clone created (a fast-forward pull or a fetch brings none)."""
    def test_a_fetched_commit_is_not_made_and_a_local_one_is(self):
        import importlib.util
        import subprocess
        spec = importlib.util.spec_from_file_location("end_hook_under_test", str(Path(__file__).resolve().parents[2] / "cloud" / "launcher" / "end_hook.py"))
        hook_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook_mod)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@invalid", HOME=tmp, XDG_CONFIG_HOME=tmp)
        def git(cwd, *args):
            return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()
        origin, one, two = (os.path.join(tmp, n) for n in ("origin.git", "one", "two"))
        git(tmp, "init", "-q", "--bare", "-b", "main", origin)
        git(tmp, "clone", "-q", origin, one)
        git(one, "commit", "-q", "--allow-empty", "-m", "base")
        git(one, "push", "-q", "origin", "main")
        git(tmp, "clone", "-q", origin, two)
        git(one, "commit", "-q", "--allow-empty", "-m", "someone else")
        git(one, "push", "-q", "origin", "main")
        theirs = git(one, "rev-parse", "HEAD")
        git(two, "pull", "-q", "--ff-only")
        git(two, "commit", "-q", "--allow-empty", "-m", "mine")
        mine = git(two, "rev-parse", "HEAD")
        made = hook_mod.made_commits(two)
        self.assertIn(mine, made)
        self.assertNotIn(theirs, made)
        git(two, "reset", "-q", "--hard", "HEAD~2")
        git(two, "rebase", "-q", "origin/main")   # `rebase (start)` logs the fetched commit it moves onto: not made by the pod
        self.assertNotIn(theirs, hook_mod.made_commits(two))
        git(two, "checkout", "-q", "-b", "side", "HEAD~1")
        git(two, "commit", "-q", "--allow-empty", "-m", "side")
        git(two, "rebase", "-q", "origin/main")   # a rebased commit of the pod's own is a new commit it made
        side = git(two, "rev-parse", "HEAD")
        self.assertIn(side, hook_mod.made_commits(two))
        self.assertNotIn(theirs, hook_mod.made_commits(two))
        self.assertIsNone(hook_mod.made_commits(os.path.join(tmp, "absent")))
