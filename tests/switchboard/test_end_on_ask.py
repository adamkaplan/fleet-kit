"""Ask #2393 (S4): a pod's end is posted on its ask: the final message and each run log's tail (the run record's `end_hook.run_logs.runs`, ask #2432), scrubbed and cut.
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
LIMIT = sb.load_module("cloud_worker").bind(sb.__dict__)._retired().END_LOG_CHARS   # the clip under test, never a number of this file


class TestEndOnAsk(unittest.TestCase):
    def end(self, code=0, log=None, record_extra=None, post=None, mutant=False, run_json=True, final="my final words", run_logs=None, notes=None, visibility=({"private": True},)):
        """(the posts made as [(issue, body, repo)], the audit events) of cmd_retire for a fake pod."""
        w = StopWorld(self)
        rec = w.pod(NAME, final)
        if run_json:
            if run_logs is None and log is not None:   # the merged shape (ask #2432): end_hook.run_logs.runs = [{name, exit, woke, log_tail}]
                run_logs = [{"name": "land.log", "exit": 0, "log_tail": log, "woke": False}]
            shape = {"end_hook": {"run_logs": {"runs": run_logs, "wake_lines": []}}} if isinstance(run_logs, list) else {"end_hook": {"run_logs": run_logs}} if run_logs is not None else {}
            if notes is not None:   # ask #2426: the launcher keeps the pod's /work/.pod-notes.log as run.json's `pod_notes`
                shape["pod_notes"] = notes
            (rec / "run.json").write_text(json.dumps(shape))
        cloud = sb.load_module("cloud_worker").bind(sb.__dict__)
        engine, posts, events, cluster = mock.Mock(), [], [], mock.MagicMock()
        engine.send = lambda sender, to, text: w.sent.append((to, text))
        reads = engine.visibility_reads = []

        def gh_api(path, paginate=True, timeout=None):   # the engine's reader: one bounded read of the repository
            reads.append((path, paginate, timeout))
            if isinstance(visibility, Exception):
                raise visibility
            return list(visibility)
        engine.gh_api = gh_api
        record = dict({"worker": NAME, "reports_to": "boss-lane", "issue": 2393, "repo": "acme/widget-api"}, **(record_extra or {}))
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
            old = "        post(engine, issue, comment,"
            assert real.count(old) == 1
            module = types.ModuleType("cloud_retired_today")
            exec(compile(real.replace(old, "        (lambda *a, **k: None)(engine, issue, comment,", 1), "cloud_retired.py", "exec"), module.__dict__)
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
        self.last_reads = reads
        return posts, events

    def test_s4_the_final_message_and_the_last_lines_are_posted_on_the_ask_once(self):
        count = LIMIT // 8   # lines of 14 characters: the log is far longer than the clip
        log = "\n".join("land line %04d" % i for i in range(1, count + 1))
        posts, _ = self.end(log=log)
        self.assertEqual(len(posts), 1)
        issue, body, repo = posts[0]
        self.assertEqual((issue, repo), (2393, "acme/widget-api"))
        self.assertIn("my final words", body)
        shown = [l for l in body.split("\n") if l.startswith("land line")]
        self.assertIn("land line %04d" % count, shown)
        self.assertEqual(shown, ["land line %04d" % i for i in range(count - len(shown) + 1, count + 1)])   # a contiguous suffix, the newest line last
        self.assertNotIn("land line 0001", shown)
        self.assertLessEqual(len("\n".join(shown)), LIMIT)

    def test_each_run_log_with_a_tail_is_posted_with_its_exit_and_the_others_are_skipped(self):
        logs = [{"name": "build.log", "exit": 2, "log_tail": "compile failed", "woke": False}, {"name": "idle.log", "exit": None, "log_tail": "", "woke": False},
                {"name": "land.log", "exit": None, "log_tail": "still going", "woke": False}, {"name": "none.log", "exit": 0, "woke": False}, "junk", {"log_tail": 5}]
        posts, _ = self.end(run_logs=logs)
        body = posts[0][1]
        self.assertIn("build.log (exit 2)", body)
        self.assertIn("compile failed", body)
        self.assertIn("land.log (still running or no exit file)", body)
        self.assertEqual((body.count("idle.log"), body.count("none.log")), (0, 0))

    def test_a_hostile_name_or_exit_is_neither_shown_nor_trusted(self):
        logs = [{"name": "ghp_" + "A1b2C3d4E5f6G7h8I9j0" + "@everyone/..", "exit": "0\n```x", "log_tail": "one", "woke": False},
                {"name": "a`b\nc", "exit": True, "log_tail": "two", "woke": False}, {"name": "ok.log", "exit": 10 ** 7, "log_tail": "three", "woke": False},
                {"name": "../../etc/passwd", "exit": 3.5, "log_tail": "four", "woke": False}]
        posts, _ = self.end(run_logs=logs)
        body = posts[0][1]
        self.assertNotIn("ghp_", body)
        self.assertNotIn("@", body)
        self.assertTrue(all(l == "```" for l in body.split("\n") if l.startswith("```")))
        self.assertEqual([w for w in ("one", "two", "three", "four") if "\n" + w + "\n" in body + "\n"], ["one", "two", "three", "four"], "one bad entry dropped the others")
        self.assertIn("[run log ok.log (still running or no exit file)", body)
        self.assertNotIn("etc/", body)
        self.assertNotIn("(exit 0", body)

    def test_at_most_the_entry_cap_of_run_logs_is_posted_each_clipped(self):
        cap = sb.load_module("cloud_worker").bind(sb.__dict__)._retired().END_LOG_ENTRIES
        logs = [{"name": "r%d.log" % i, "exit": 0, "log_tail": "line\n" * LIMIT, "woke": False} for i in range(cap * 3)]
        posts, _ = self.end(run_logs=logs)
        body = posts[0][1]
        self.assertEqual(body.count("[run log "), cap)
        self.assertLessEqual(body.count("line"), cap * LIMIT // 5)

    def test_control_the_same_record_without_run_logs_posts_the_message_alone(self):
        with_logs, _ = self.end(log="land line 1")
        without, _ = self.end()
        self.assertEqual(["[run log" in b for _i, b, _r in with_logs], [True])
        self.assertEqual([("[run log" in b, "my final words" in b) for _i, b, _r in without], [(False, True)])

    def test_a_key_block_in_the_final_message_is_redacted_too(self):
        posts, _ = self.end(final="done\n-----BEGIN OPENSSH PRIVATE KEY-----\nQUJDREVGR0hJSktMTU5PUA\n-----END OPENSSH PRIVATE KEY-----\nbye")
        self.assertNotIn("QUJDREVGR0hJSktMTU5PUA", posts[0][1])
        self.assertIn("done", posts[0][1])

    def test_a_malformed_run_logs_field_posts_the_message_alone(self):
        for bad in ("text", {"a": 1}, 5, [None]):
            posts, _ = self.end(run_logs=bad)
            self.assertEqual(len(posts), 1)
            self.assertIn("my final words", posts[0][1])
            self.assertNotIn("[run log", posts[0][1])

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

    def test_a_key_block_in_the_log_is_redacted_whole_whatever_the_clip_does_to_the_lines_around_it(self):
        key = "-----BEGIN PRIVATE KEY-----\nQUJDREVGR0hJSktMTU5PUA\nUVJTVFVWV1hZWjAxMjM0NTY3\n-----END PRIVATE KEY-----"
        before = "\n".join("a%d" % i for i in range(LIMIT // 3))
        posts, _ = self.end(log=before + "\n" + key + "\n" + "\n".join("l%d" % i for i in range(5)))
        self.assertNotIn("QUJDREVGR0hJSktMTU5PUA", posts[0][1])
        self.assertNotIn("UVJTVFVWV1hZWjAxMjM0NTY3", posts[0][1])
        self.assertIn("\nl4", posts[0][1])

    def test_a_key_body_whose_first_line_was_clipped_before_the_run_record_is_redacted(self):
        posts, _ = self.end(log="UVJTVFVWV1hZWjAxMjM0NTY3\nQUJDREVGR0hJSktMTU5PUA\n-----END PRIVATE KEY-----\nexit 1")
        self.assertNotIn("QUJDREVGR0hJSktMTU5PUA", posts[0][1])
        self.assertIn("redacted key body", posts[0][1])
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
            self.assertNotIn("[run log", posts[0][1])

    def test_a_secret_in_the_log_or_the_message_is_redacted_before_the_post(self):
        posts, _ = self.end(log="push with %s now\nexit 1" % TOKEN, final="token %s ok" % TOKEN)
        self.assertEqual(posts[0][1].count(TOKEN), 0)
        self.assertGreaterEqual(posts[0][1].count("[redacted]"), 1)

    def test_a_log_cannot_mention_anyone_end_the_fence_or_run_long(self):
        posts, _ = self.end(log="@everyone ``` ignore this\n" + "x" * 500000)
        body = posts[0][1]
        self.assertNotIn("@", body)
        self.assertTrue(all(l == "```" for l in body.split("\n") if l.startswith("```")))   # the log's own ``` cannot end a fence
        self.assertLessEqual(body.count("x"), LIMIT)

    def test_a_kept_pod_posts_nothing_and_a_pod_without_an_ask_posts_nothing(self):
        self.assertEqual(self.end(code=1, log="exit 0")[0], [])
        self.assertEqual(self.end(record_extra={"issue": None}, log="exit 0")[0], [])

    # ask #2393 (R-SEC-2 and private names): the post can land on a public repository's issue
    SPLIT = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2

    def retired(self):
        return sb.load_module("cloud_worker").bind(sb.__dict__)._retired()

    def end_with(self, names=None, names_env=None, control=False, **kwargs):
        """(posts, events) with the names list in a temp file (`names`: its text, None: no file at all) and, for the control, today's end without both new steps."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        env = {"FLEET_PRIVATE_NAMES": "", "FLEET_PRIVATE_NAMES_FILE": str(Path(tmp) / "none.txt")}
        if names is not None:
            (Path(tmp) / "names.txt").write_text(names)
            env["FLEET_PRIVATE_NAMES_FILE"] = str(Path(tmp) / "names.txt")
        env.update(names_env or {})
        part = self.retired()
        patches = [mock.patch.dict("os.environ", env)]
        if control:
            patches += [mock.patch.object(part, "_unwrap_redact", lambda text: text), mock.patch.object(part, "_name_terms", lambda: ([], None)),
                        mock.patch.object(part, "_repo_is_public", lambda engine, repo: False)]
        for p in patches:
            p.start()
        try:
            return self.end(**kwargs)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_a_token_split_across_a_newline_or_a_backslash_newline_is_redacted(self):
        t = self.SPLIT
        for cut, glue in ((20, "\n"), (12, "\\\n"), (18, "-\n")):
            text = "before\npush with %s%s%s now\nafter" % (t[:cut], glue, t[cut:])
            for where in ({"final": text}, {"log": text}):
                posts, _ = self.end_with(**where)
                body = posts[0][1]
                self.assertNotIn(t[:cut], body, (cut, where))
                self.assertNotIn(t[cut:], body, (cut, where))
                self.assertIn("[redacted]", body)
                self.assertIn("push with", body)
                self.assertIn("now", body)
                self.assertIn("after", body)
            control, _ = self.end_with(final=text, control=True)
            self.assertIn(t[cut:], control[0][1], "control: today's end leaves the split token in")

    def test_a_normal_two_line_text_is_not_mangled(self):
        text = "The build finished.\nAll 12 checks passed, see the notes\nfor details-\nand a long-hyphenated-word\nnext line."
        for where in ({"final": text}, {"log": text}, {"notes": text}):
            posts, _ = self.end_with(**where)
            control, _ = self.end_with(control=True, **where)
            self.assertEqual(posts[0][1], control[0][1], where)   # the same post as today's, word for word
            self.assertIn("long-hyphenated-word", posts[0][1])

    def test_a_private_term_in_the_message_or_log_is_replaced_case_insensitively(self):
        names = "# made-up terms\nacme-secret-org\ncase:ExactOnly\n"
        posts, events = self.end_with(names=names, final="see ACME-Secret-Org/repo and ExactOnly and exactonly", log="log of acme-secret-org")
        body = posts[0][1]
        self.assertNotIn("secret-org", body.lower())
        self.assertNotIn("ExactOnly", body)
        self.assertIn("exactonly", body)
        self.assertGreaterEqual(body.count("[private name]"), 3)
        self.assertNotIn("cloud.end_names_unchecked", events)
        control, _ = self.end_with(names=names, final="see acme-secret-org", control=True)
        self.assertIn("acme-secret-org", control[0][1], "control: today's end posts the term")

    def test_a_list_given_as_text_is_used_and_a_zero_width_entry_does_nothing(self):
        posts, _ = self.end_with(names_env={"FLEET_PRIVATE_NAMES": "x*\nacme-secret-org"}, final="abc acme-secret-org")
        self.assertIn("abc [private name]", posts[0][1])

    def test_a_missing_or_unreadable_or_bad_list_posts_and_the_audit_says_so_once(self):
        for label, kw in (("missing", {"names": None}), ("directory", {"names_env": {"FLEET_PRIVATE_NAMES_FILE": tempfile.gettempdir()}}),
                          ("bad", {"names": "(unclosed\n"}), ("empty", {"names": "# nothing\n"})):
            posts, events = self.end_with(final="words about acme-secret-org", **kw)
            self.assertEqual(len(posts), 1, label)
            self.assertIn("acme-secret-org", posts[0][1], label)
            self.assertEqual(events.count("cloud.end_names_unchecked"), 1, label)

    def test_a_fold_with_an_indent_over_three_lines_or_after_a_very_long_line_is_redacted(self):
        t = self.SPLIT
        cases = {"indent": "x %s\n   %s y" % (t[:20], t[20:]), "three lines": "x %s\n%s\n%s y" % (t[:15], t[15:30], t[30:]),
                 "long line": "p" * 5000 + " %s\n%s" % (t[:20], t[20:]), "four lines": "x %s\n%s\n%s\n%s" % (t[:8], t[8:16], t[16:30], t[30:])}
        for label, text in cases.items():
            for where in ({"final": text}, {"log": text}):
                body = self.end_with(**where)[0][0][1]
                for piece in (t[8:16], t[15:30], t[20:], t[30:]):
                    self.assertNotIn(piece, body, (label, piece))
                self.assertIn("[redacted]", body, label)
            self.assertIn(t[30:], self.end_with(final=text, control=True)[0][0][1], label)

    def test_a_whole_token_followed_by_a_word_on_the_next_line_keeps_the_word(self):
        for text in ("key %s\nafter that\nmore" % self.SPLIT, "key %s\n  later words" % self.SPLIT):
            body = self.end_with(final=text)[0][0][1]
            self.assertNotIn(self.SPLIT[20:], body)
            self.assertIn("after that" if "after" in text else "later words", body)

    def test_one_bad_entry_or_a_cut_entry_does_not_disable_the_others(self):
        posts, events = self.end_with(names="(unclosed\nacme-secret-org\n", final="about acme-secret-org")
        self.assertIn("about [private name]", posts[0][1])
        self.assertEqual(events.count("cloud.end_names_unchecked"), 1)
        part = self.retired()
        text = "acme-secret-org\n" + "z" * (part.NAMES_MAX_BYTES + 10)
        posts, events = self.end_with(names=text, final="about acme-secret-org")
        self.assertIn("about [private name]", posts[0][1])
        self.assertEqual(events.count("cloud.end_names_unchecked"), 1)

    def test_a_short_name_that_grows_the_text_is_cut_again(self):
        posts, _ = self.end_with(names="x\n", final="x" * (LIMIT * 20))
        self.assertLessEqual(len(posts[0][1]), self.retired().END_POST_CHARS)

    def test_more_fold_shapes_are_redacted_and_the_names_list_is_read_once(self):
        t = self.SPLIT
        cases = {"bare cr": "x %s\r%s y" % (t[:20], t[20:]), "form feed": "x %s\x0c%s y" % (t[:20], t[20:]), "blank line": "x %s\n\n%s y" % (t[:20], t[20:]),
                 "trailing space": "x %s  \n%s y" % (t[:20], t[20:]), "quote mark": "x %s\n> %s y" % (t[:20], t[20:]), "table bar": "x %s\n| %s |" % (t[:20], t[20:]),
                 "seven lines": "x " + "\n".join(t[k:k + 6] for k in range(0, 42, 6))}
        for label, text in cases.items():
            for where in ({"final": text}, {"log": text}, {"notes": text}):
                body = self.end_with(**where)[0][0][1]
                self.assertNotIn(t[20:], body, (label, where))
                self.assertNotIn(t[6:12], body, (label, where))
        part = self.retired()
        reads = []
        real = part._name_terms
        with mock.patch.object(part, "_name_terms", lambda: reads.append(1) or real()):
            self.end_with(names="acme-secret-org\n", final="x", log="y")
        self.assertEqual(len(reads), 1)

    def test_a_name_folded_over_a_line_break_and_a_trailing_hyphen_before_prose_are_left_as_they_are(self):
        posts, _ = self.end_with(names="acme-secret-org\n", final="about acme-\nsecret-org and well-\nknown 2nd things")
        self.assertIn("well-?known 2nd things", posts[0][1])

    def test_quoted_and_invisible_folds_many_blank_lines_and_the_cost_of_a_long_text(self):
        t = self.SPLIT
        digitless = "xoxb-123456789012-123456789012-AbCdEfGh"
        body = self.end_with(final="%s\nIjKlMnOpQrStUvWxYz" % digitless)[0][0][1]
        self.assertNotIn("IjKlMnOpQrStUvWxYz", body)
        cases = {"string literal": 'k = "%s" +\n"%s",' % (t[:20], t[20:]), "zero width": "x %s\u200b\n%s y" % (t[:20], t[20:]),
                 "nine blanks": "x %s%s%s y" % (t[:20], "\n" * 10, t[20:])}
        for label, text in cases.items():
            body = self.end_with(final=text)[0][0][1]
            self.assertNotIn(t[20:], body, label)
            self.assertNotIn(t[20:], self.end_with(log=text)[0][0][1], label)
        import time
        part = self.retired()
        for unit in ("a1-\n", "a1\n", "| a1 |\n", "a:\n", "a=b\n", "token=a\n", "ghp_x\nb\n", "http://a:\nb\n"):
            started = time.monotonic()
            part._unwrap_redact(unit * (200000 // len(unit)))
            self.assertLess(time.monotonic() - started, 8, unit)

    def test_a_public_repository_without_a_usable_list_gets_only_the_withheld_line(self):
        line = self.retired().WITHHELD_LINE
        secret = "words about acme-secret-org and my final words"
        for label, kw in (("missing", {"names": None}), ("directory", {"names_env": {"FLEET_PRIVATE_NAMES_FILE": tempfile.gettempdir()}})):
            for public in (({"private": False},), ({},), ()):
                posts, events = self.end_with(final=secret, log="land line", visibility=public, **kw)
                self.assertEqual([b for _i, b, _r in posts], [line], (label, public))
                self.assertEqual(events.count("cloud.end_names_unchecked"), 1, label)
            self.assertEqual(self.last_reads, [("repos/acme/widget-api", False, self.last_reads[0][2])])   # one bounded read
            self.assertTrue(self.last_reads[0][2])
            control, _ = self.end_with(final=secret, control=True, visibility=({"private": False},), **kw)
            self.assertIn("my final words", control[0][1], "control: today's end posts the text")

    def test_visibility_that_cannot_be_read_counts_as_public(self):
        for bad in (RuntimeError("down"), OSError("no gh"), ("junk",)):
            posts, _ = self.end_with(names=None, final="my final words", visibility=bad)
            self.assertEqual([b for _i, b, _r in posts], [self.retired().WITHHELD_LINE], bad)

    def test_a_private_repository_without_a_list_posts_as_today_with_the_note(self):
        posts, events = self.end_with(names=None, final="my final words", visibility=({"private": True},))
        self.assertIn("my final words", posts[0][1])
        self.assertEqual(events.count("cloud.end_names_unchecked"), 1)

    def test_a_usable_list_needs_no_visibility_read(self):
        posts, _ = self.end_with(names="acme-secret-org\n", final="my final words", visibility=RuntimeError("down"))
        self.assertIn("my final words", posts[0][1])
        self.assertEqual(self.last_reads, [])

    def test_the_list_is_read_once_and_never_printed(self):
        posts, events = self.end_with(names="(unclosed-acme-secret\n", final="x")
        self.assertNotIn("unclosed-acme-secret", repr((posts, events)))

    def test_a_failed_post_is_audited_and_the_retire_goes_on(self):
        posts, events = self.end(log="exit 0", post=RuntimeError("down"))
        self.assertEqual(posts, [])
        self.assertIn("cloud.end_post_failed", events)


class TestLaunchRecordsTheAsk(unittest.TestCase):
    """The launch is what gives the end post its ask: the cloud record carries `issue` (the real `_launch`, stubbed at its edges as test_first_push_notice does)."""

    def launched(self, issue):
        import test_first_push_notice as fp
        if not hasattr(fp.PodSessionStub, "model_list"):   # the launch now verifies its model (#1655): the stub serves the default one, as test_land_text does
            fp.PodSessionStub.model_list = lambda self: [dict(zip(("providerID", "id"), fp.sb.CLOUD_DEFAULT_MODEL.split("/", 1)))]
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


class TestNotesAndRunLogsTogether(unittest.TestCase):
    """A pod with BOTH a notes tail (#2426/#2435, in the last message) and run logs: each part is posted once."""
    end = TestEndOnAsk.end   # the same fake pod
    HEADER = "[last lines of /work/.pod-notes.log, untrusted text]"
    LOGS = [{"name": "build.log", "exit": 1, "log_tail": "build tail alpha", "woke": False}, {"name": "land.log", "exit": 0, "log_tail": "land tail beta", "woke": False}]

    def parts(self, body):
        return [body.count(self.HEADER), body.count("build tail alpha"), body.count("land tail beta"), body.count("my final words"), body.count("[run log ")]

    def test_the_notes_header_each_run_log_tail_and_the_final_message_are_posted_once(self):
        posts, _ = self.end(run_logs=self.LOGS, notes="merged branch x\npushed y")
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.parts(posts[0][1]), [1, 1, 1, 1, 2])
        self.assertIn("merged branch x", posts[0][1])

    def test_control_a_post_that_drops_one_part_fails_the_once_check(self):
        posts, _ = self.end(run_logs=self.LOGS, notes="merged branch x")
        body = posts[0][1]
        self.assertEqual(self.parts(body), [1, 1, 1, 1, 2])
        for drop in (self.HEADER, "build tail alpha", "land tail beta", "my final words"):
            self.assertNotEqual(self.parts(body.replace(drop, "")), [1, 1, 1, 1, 2], drop)
        without, _ = self.end(run_logs=self.LOGS)   # no notes in the record: no notes header
        self.assertEqual(self.parts(without[0][1])[0], 0)

    def test_a_secret_shaped_string_and_a_backtick_in_the_notes_come_out_cleaned(self):
        secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        posts, _ = self.end(run_logs=self.LOGS, notes="token " + secret + "\nran `rm -rf x` for @everyone")
        body = posts[0][1]
        self.assertNotIn(secret, body)
        self.assertNotIn("ghp_", body)
        self.assertNotIn("@", body)
        self.assertEqual(self.parts(body), [1, 1, 1, 1, 2])
        notes_part = body.split(self.HEADER, 1)[1]
        self.assertNotIn("`rm", notes_part)
        self.assertIn("'rm -rf x'", notes_part)
