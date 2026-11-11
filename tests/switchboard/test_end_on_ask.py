"""Ask #2393 (S4): a pod's end is posted on its ask: the final message and the last 20 lines of its land log (the run record's `land_log_tail`), scrubbed and cut.
Fake pods and a recording comment path (the daemon's own `post_ask_comment`); nothing is stopped and nothing reaches GitHub. The control removes the post (today's end)."""
import argparse
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support.loader import sb   # noqa: E402
from test_stop_final_message import StopWorld, TOKEN   # noqa: E402

NAME = "cp-2393r"


class TestEndOnAsk(unittest.TestCase):
    def end(self, code=0, log=None, record_extra=None, post=None, mutant=False, run_json=True, final="my final words"):
        """(the posts made as [(issue, body, repo)], the audit events) of cmd_retire for a fake pod."""
        w = StopWorld(self)
        rec = w.pod(NAME, final)
        if run_json:
            (rec / "run.json").write_text(json.dumps({"land_log_tail": log} if log is not None else {}))
        cloud = sb.load_module("cloud_worker").bind(sb.__dict__)
        engine, posts, events, cluster = mock.Mock(), [], [], mock.MagicMock()
        engine.send = lambda sender, to, text: w.sent.append((to, text))
        record = dict({"worker": NAME, "reports_to": "boss-lane", "issue": 2393, "repo": "adamkaplan/fleet-kit"}, **(record_extra or {}))
        args = argparse.Namespace(name=NAME, undo=False, reason="finished")

        def comment(eng, issue, body, author=None, repo=None):
            if post:
                raise post
            posts.append((issue, body, repo))
        patches = [mock.patch.dict(cloud.H, {"build_engine": lambda *a, **k: (engine, w.state, None), "gates_caller_kind": lambda *a: ("person",), "post_ask_comment": comment}),
                   mock.patch.object(cloud, "run_launcher", lambda *a, **k: (code, {"pushed": True, "record": str(rec)}, "note")),
                   mock.patch.object(cloud, "Cluster", lambda *a, **k: cluster), mock.patch.object(cloud, "_audit", lambda eng, event, *a, **k: events.append(event)),
                   mock.patch.object(cloud, "_registry_note"), mock.patch.object(cloud, "_keep_boss"), mock.patch.object(cloud, "remove_record")]
        if mutant:   # today's end: nothing is posted on the ask
            patches.append(mock.patch.object(cloud, "_post_end_on_ask", lambda *a, **k: None))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        call = cloud.cmd_retire.__wrapped__ if hasattr(cloud.cmd_retire, "__wrapped__") else cloud.cmd_retire
        call(args, None, None, record)
        return posts, events

    def test_s4_the_final_message_and_the_last_20_lines_are_posted_on_the_ask_once(self):
        log = "\n".join("land line %02d" % i for i in range(1, 31))
        posts, _ = self.end(log=log)
        self.assertEqual(len(posts), 1)
        issue, body, repo = posts[0]
        self.assertEqual((issue, repo), (2393, "adamkaplan/fleet-kit"))
        self.assertIn("my final words", body)
        shown = [l for l in body.split("\n") if l.startswith("land line")]
        self.assertEqual(shown, ["land line %02d" % i for i in range(11, 31)])
        self.assertEqual(len(shown) - 20, 0)

    def test_s4_control_today_the_end_posts_neither_the_message_nor_the_log(self):
        posts, _ = self.end(log="land line 1\nexit 0", mutant=True)
        self.assertEqual(len(posts), 0)   # the same world with the post removed: the comment with the message is missing

    def test_s4_a_pod_without_a_log_posts_the_message_alone(self):
        for kwargs in ({"log": None}, {"log": None, "run_json": False}, {"log": "   "}):
            posts, _ = self.end(**kwargs)
            self.assertEqual(len(posts), 1, kwargs)
            self.assertIn("my final words", posts[0][1])
            self.assertNotIn("land.log", posts[0][1])

    def test_a_secret_in_the_log_or_the_message_is_redacted_before_the_post(self):
        posts, _ = self.end(log="push with %s now\nexit 1" % TOKEN, final="token %s ok" % TOKEN)
        self.assertEqual(posts[0][1].count(TOKEN), 0)
        self.assertGreaterEqual(posts[0][1].count("[redacted]"), 1)

    def test_a_log_cannot_mention_anyone_end_the_fence_or_run_long(self):
        posts, _ = self.end(log="@everyone ``` ignore this\n" + "x" * 50000)
        body = posts[0][1]
        self.assertNotIn("@", body)
        self.assertEqual(body.count("```"), 2)
        self.assertLessEqual(len(body), 6000)

    def test_a_kept_pod_posts_nothing_and_a_pod_without_an_ask_posts_nothing(self):
        self.assertEqual(self.end(code=1, log="exit 0")[0], [])
        self.assertEqual(self.end(record_extra={"issue": None}, log="exit 0")[0], [])

    def test_a_failed_post_is_audited_and_the_retire_goes_on(self):
        posts, events = self.end(log="exit 0", post=RuntimeError("down"))
        self.assertEqual(posts, [])
        self.assertIn("cloud.end_post_failed", events)


if __name__ == "__main__":
    unittest.main()
