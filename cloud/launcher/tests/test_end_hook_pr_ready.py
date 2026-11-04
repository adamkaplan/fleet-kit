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


if __name__ == "__main__":
    unittest.main()
