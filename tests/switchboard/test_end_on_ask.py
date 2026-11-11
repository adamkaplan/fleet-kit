"""Ask #2393 (S4): a pod's end is posted on its ask: the final message and the last lines of its land log (the run record's `land_log_tail`), scrubbed and cut.
Fake pods and a recording comment path (the daemon's own `post_ask_comment`); nothing is stopped and nothing reaches GitHub. The control removes the post (today's end)."""
import argparse
import inspect
import tempfile
import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support.loader import sb   # noqa: E402
from test_stop_final_message import StopWorld, TOKEN   # noqa: E402

NAME = "cp-2393r"
N = sb.load_module("cloud_worker").bind(sb.__dict__)._retired().END_LOG_LINES   # the clip under test, never a number of this file


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
            real = (Path(__file__).resolve().parents[2] / "bin" / "cloud_retired.py").read_text()
            old = "        post(engine, issue, end_comment("
            assert real.count(old) == 1
            module = types.ModuleType("cloud_retired_today")
            exec(compile(real.replace(old, "        (lambda *a, **k: None)(engine, issue, end_comment(", 1), "cloud_retired.py", "exec"), module.__dict__)
            module.D = cloud._retired().D   # today's end: the comment is built and never posted
            patches.append(mock.patch.object(cloud._retired(), "post_end", module.post_end))
        call = cloud.cmd_retire.__wrapped__ if hasattr(cloud.cmd_retire, "__wrapped__") else cloud.cmd_retire
        for p in patches:
            p.start()
        try:
            call(args, None, None, record)
        finally:
            for p in reversed(patches):
                p.stop()
        return posts, events

    def test_s4_the_final_message_and_the_last_lines_are_posted_on_the_ask_once(self):
        log = "\n".join("land line %02d" % i for i in range(1, N + 11))
        posts, _ = self.end(log=log)
        self.assertEqual(len(posts), 1)
        issue, body, repo = posts[0]
        self.assertEqual((issue, repo), (2393, "adamkaplan/fleet-kit"))
        self.assertIn("my final words", body)
        shown = [l for l in body.split("\n") if l.startswith("land line")]
        self.assertIn("land line %02d" % (N + 10), shown)
        self.assertLessEqual(len(shown), N)
        self.assertEqual(shown, ["land line %02d" % i for i in range(N + 10 - len(shown) + 1, N + 11)])   # a contiguous suffix of the log, the newest line last
        self.assertNotIn("land line 01", shown)

    def test_s4_control_today_the_end_posts_neither_the_message_nor_the_log(self):
        def complete(posts):   # the S4 predicate: a comment carries the final message and the log
            return [("my final words" in b, "land line 1" in b) for _i, b, _r in posts] == [(True, True)]
        today, _ = self.end(log="land line 1\nexit 0", mutant=True)   # today's end: the post is not made
        now, _ = self.end(log="land line 1\nexit 0")
        self.assertFalse(complete(today))
        self.assertTrue(complete(now))

    def test_the_pods_text_stays_inside_its_fence_and_a_local_path_line_is_dropped(self):
        posts, _ = self.end(final="![x](https://evil.example/a.png) ``` # fake header\n" + "word " * 900)
        body = posts[0][1]
        fence_lines = [l for l in body.split("\n") if l.startswith("```")]
        self.assertTrue(all(l == "```" for l in fence_lines), fence_lines)
        inside = body.split("```")[1]
        self.assertIn("# fake header", inside)
        self.assertIn("evil.example", inside)
        self.assertNotIn("more characters in", body)
        self.assertNotIn("/cloud-last/", body)

    def test_a_short_log_is_posted_whole_and_a_missing_last_text_still_posts_the_log(self):
        log = "\n".join("l%d" % i for i in range(1, 6))
        posts, _ = self.end(log=log, final=None)
        shown = [l for l in posts[0][1].split("\n") if re.fullmatch(r"l\d+", l)]
        self.assertEqual(shown, log.split("\n"))
        self.assertIn("last text not read", posts[0][1])

    def test_a_key_block_in_the_log_is_redacted_whole_even_when_the_cut_drops_its_first_line(self):
        key = "-----BEGIN PRIVATE KEY-----\nQUJDREVGR0hJSktMTU5PUA\nUVJTVFVWV1hZWjAxMjM0NTY3\n-----END PRIVATE KEY-----"
        posts, _ = self.end(log=key + "\n" + "\n".join("l%d" % i for i in range(N - 3)))   # N + 1 lines: the cut drops only the BEGIN line
        self.assertNotIn("QUJDREVGR0hJSktMTU5PUA", posts[0][1])
        self.assertNotIn("UVJTVFVWV1hZWjAxMjM0NTY3", posts[0][1])
        self.assertIn("\nl%d" % (N - 4), posts[0][1])

    def test_a_key_body_whose_first_line_was_clipped_before_the_run_record_is_redacted(self):
        posts, _ = self.end(log="UVJTVFVWV1hZWjAxMjM0NTY3\nQUJDREVGR0hJSktMTU5PUA\n-----END PRIVATE KEY-----\nexit 1")
        self.assertNotIn("QUJDREVGR0hJSktMTU5PUA", posts[0][1])
        self.assertIn("exit 1", posts[0][1])

    def test_the_pods_endpoint_address_is_removed_from_the_log(self):
        posts, _ = self.end(log="curl https://worker-cb-2393-1f2e3d4c.example.internal:4096/x failed\nexit 1")
        self.assertNotIn("worker-cb-2393", posts[0][1])
        self.assertIn("exit 1", posts[0][1])

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
        self.assertTrue(all(l == "```" for l in body.split("\n") if l.startswith("```")))   # the log's own ``` cannot end a fence
        self.assertLess(body.count("x"), 50000)

    def test_a_kept_pod_posts_nothing_and_a_pod_without_an_ask_posts_nothing(self):
        self.assertEqual(self.end(code=1, log="exit 0")[0], [])
        self.assertEqual(self.end(record_extra={"issue": None}, log="exit 0")[0], [])

    def test_a_failed_post_is_audited_and_the_retire_goes_on(self):
        posts, events = self.end(log="exit 0", post=RuntimeError("down"))
        self.assertEqual(posts, [])
        self.assertIn("cloud.end_post_failed", events)


class TestLaunchRecordsTheAsk(unittest.TestCase):
    """The launch is what gives the end post its ask: the cloud record carries `issue` (the real `_launch`, stubbed at its edges as test_first_push_notice does)."""

    def launched(self, issue):
        import test_first_push_notice as fp
        source = inspect.getsource(fp.drive_launch).replace("save_record=lambda *a: None", "save_record=lambda st, rec: SAVED.append(dict(rec))").replace("issue=None", "issue=%r" % issue)
        assert "SAVED.append" in source
        space = dict(vars(fp), SAVED=[])
        exec(compile(source, "drive_launch_issue", "exec"), space)
        with tempfile.TemporaryDirectory() as state:
            space["drive_launch"](fp.CW(), state)
        return space["SAVED"][0]

    def test_a_launch_with_an_ask_records_its_number_and_one_without_records_none(self):
        self.assertEqual(self.launched(2393)["issue"], 2393)
        self.assertIsNone(self.launched(None)["issue"])
        self.assertEqual(self.launched(2393)["repo"], "acme/widget-api")


if __name__ == "__main__":
    unittest.main()
