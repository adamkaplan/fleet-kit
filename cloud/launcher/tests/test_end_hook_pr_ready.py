"""#2145: the end hook marks a finished pod's draft pull request ready (S2, S3) and the end notice says so (S4). A fake hub, a scratch repository."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "..", "bin"))
import end_hook  # noqa: E402
import cloud_end_alert as cea  # noqa: E402
import record_json  # noqa: E402

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"}


class Hub:
    def __init__(self, pr):
        self.pr, self.readies, self.comments = pr, [], []

    def find(self, branch):
        return self.pr

    def ready(self, n):
        self.readies.append(n)

    def comment(self, n, text):
        self.comments.append((n, text))


class Broken(Hub):
    def ready(self, n):
        raise RuntimeError("boom")


def repo():
    d = tempfile.mkdtemp()
    run = lambda *a: subprocess.run(["git", *a], cwd=d, env=dict(os.environ, **ENV), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
    run("init", "-q", "-b", "feat")
    run("commit", "-q", "--allow-empty", "-m", "x")
    return d, run("rev-parse", "HEAD")


def notice(note):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "run.json"), "w") as f:
        json.dump({"end_hook": {"pushed": False, "wip_branch": None, "head_on": None, "pr_note": note}}, f)
    return cea.pr_line(d, record_json.parse_bounded)


class TestPrReady(unittest.TestCase):
    def test_S2_marks_ready_and_comments_once(self):
        d, sha = repo()
        hub = Hub({"number": 7, "isDraft": True, "headRefOid": sha})
        res = end_hook.with_pr(d, {"pushed": False, "wip_branch": None}, hub, "/review")
        self.assertEqual((len(hub.readies), len(hub.comments)), (1, 1))
        self.assertEqual(res["pr_note"], "PR #7 marked ready, review requested")

    def test_S2_no_configured_comment_posts_none(self):
        d, sha = repo()
        hub = Hub({"number": 7, "isDraft": True, "headRefOid": sha})
        res = end_hook.with_pr(d, {"pushed": False}, hub, "")
        self.assertEqual((len(hub.readies), len(hub.comments)), (1, 0))
        self.assertIn("marked ready", res["pr_note"])

    def test_S3_other_head_or_unpushed_is_not_ready(self):
        d, sha = repo()
        h1 = Hub({"number": 7, "isDraft": True, "headRefOid": "0" * 40})
        n1 = end_hook.pr_note(d, {"pushed": False}, h1, "/review")
        h2 = Hub({"number": 7, "isDraft": True, "headRefOid": sha})
        n2 = end_hook.pr_note(d, {"pushed": True, "wip_branch": "w/wip"}, h2, "/review")
        self.assertEqual(len(h1.readies) + len(h2.readies), 0)
        self.assertEqual(len(h1.comments) + len(h2.comments), 0)
        self.assertIn("head is not the pushed commit", n1)
        self.assertIn("unpushed work", n2)

    def test_S4_every_case_has_a_notice_line(self):
        d, sha = repo()
        cases = [(Hub(None), {"pushed": False}), (Hub({"number": 1, "isDraft": False, "headRefOid": sha}), {"pushed": False}),
                 (Broken({"number": 1, "isDraft": True, "headRefOid": sha}), {"pushed": False}),
                 (Hub({"number": 1, "isDraft": True, "headRefOid": sha}), {"pushed": False}),
                 (Hub(None), {"pushed": True}), (Hub(None), {"pushed": False, "no_repository": True})]
        missing = 0
        for hub, res in cases:
            line = notice(end_hook.pr_note(d, res, hub, "/review"))
            if not (line.startswith("PR #") or line.startswith("PR not marked ready: ")) or "no answer recorded" in line:
                missing += 1
        self.assertEqual(missing, 0)
        self.assertIn("the call failed", end_hook.pr_note(d, {"pushed": False}, cases[2][0], "/r"))

    def test_notice_without_a_recorded_note_says_so(self):
        self.assertEqual(cea.pr_line("", record_json.parse_bounded), "PR not marked ready: no answer recorded")


FAKE_GH = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$1 $2" in
  "pr list") printf '[{"number": 9, "draft": %s, "head": {"ref": "feat", "sha": "%s"}, "state": "open"}]\\n' "$FAKE_DRAFT" "$FAKE_SHA" ;;
esac
"""


class TestMainRealHub(unittest.TestCase):
    """main() with hub=None: the REAL GhHub against a fake cw-gh on PATH that prints REST rows (draft, head.sha), as the pod's client does."""
    def go(self, draft, comment):
        from unittest import mock
        import io, contextlib
        d, sha = repo()
        bindir = tempfile.mkdtemp()
        log = os.path.join(bindir, "log")
        with open(os.path.join(bindir, "gh"), "w") as f:
            f.write(FAKE_GH)
        os.chmod(os.path.join(bindir, "gh"), 0o755)
        env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "CW_WORKDIR": d, "CW_WORKER": "w", "FAKE_LOG": log, "FAKE_DRAFT": draft, "FAKE_SHA": sha}
        if comment:
            env["CW_REVIEW_COMMENT"] = comment
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(end_hook, "run", lambda w, n: {"pushed": False, "wip_branch": None}), contextlib.redirect_stdout(out):
            rc = end_hook.main()
        calls = open(log).read().splitlines() if os.path.exists(log) else []
        return rc, json.loads(out.getvalue()), calls

    def test_draft_is_marked_ready_and_comment_posted(self):
        rc, res, calls = self.go("true", "/review please")
        self.assertEqual(rc, 0)
        self.assertEqual(res["pr_note"], "PR #9 marked ready, review requested")
        self.assertIn("pr ready 9", calls)
        self.assertIn("pr comment 9 --body /review please", calls)

    def test_no_comment_configured_posts_none(self):
        rc, res, calls = self.go("true", "")
        self.assertIn("marked ready, no review comment is configured", res["pr_note"])
        self.assertEqual([c for c in calls if c.startswith("pr comment")], [])

    def test_not_a_draft_is_left_alone(self):
        rc, res, calls = self.go("false", "/review")
        self.assertIn("is not a draft", res["pr_note"])
        self.assertEqual([c for c in calls if c.startswith("pr ready")], [])


class TestReviewCommentSource(unittest.TestCase):
    def test_launcher_reads_per_repo_comment_default_none(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("launcher_cloud_worker", os.path.join(os.path.dirname(HERE), "cloud_worker.py"))
        cw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cw)
        d = tempfile.mkdtemp()
        p = os.path.join(d, "c.json")
        with open(p, "w") as f:
            json.dump({"acme/widget": "/review", "acme/bad": "x\ny"}, f)
        self.assertEqual(cw.review_comment("Acme/Widget", p), "/review")
        self.assertEqual(cw.review_comment("acme/other", p), "")
        self.assertEqual(cw.review_comment("acme/bad", p), "")
        self.assertEqual(cw.review_comment("acme/widget", os.path.join(d, "none.json")), "")


if __name__ == "__main__":
    unittest.main()
