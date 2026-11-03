"""The first-push notice (#2057): a cloud pod with no pushed branch and no report by its launch's checkpoint is named once to its orchestrator."""
import argparse
import subprocess
import tempfile
import types
import unittest
from support.loader import sb

TOKEN = "tok-SECRET-value-9f3"
BASE = 1_000_000.0
MAIN, WORK = "a" * 40, "b" * 40


def listing(branch_head, main=MAIN):
    lines = ["%s\tHEAD" % main] + (["%s\trefs/heads/feature" % branch_head] if branch_head else [])
    return subprocess.CompletedProcess([], 0, "\n".join(lines) + "\n", "")


class TestFirstPushNotice(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock, self.sent, self.reads, self.answer = BASE + 1900, [], [], listing(None)

        def runner(argv, **kw):
            self.reads.append(argv)
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
        self.engine = types.SimpleNamespace(state_dir=self.dir.name, now=lambda: self.clock, audit=None, actor="t", first_push_runner=runner,
                                            send=lambda frm, to, text: self.sent.append((frm, to, text)))
        sb.record_first_push_watch(self.dir.name, "coder7", "lead", "acme/widget-api", "feature", BASE + 1800, BASE)

    def report(self, at):
        sb._write_small(self.dir.name, sb.LAST_REPORT_FILE, {"coder7": {"at": at}})

    def tick(self):
        return sb.first_push_tick(self.engine)

    def test_a_pod_with_no_push_and_no_report_at_its_checkpoint_is_noticed_once(self):
        self.assertEqual(self.tick(), ["coder7"])
        self.assertEqual(len(self.sent), 1)
        frm, to, text = self.sent[0]
        self.assertEqual(to, "lead")
        self.assertIn("no first push", text)
        self.assertIn("coder7", text)
        self.assertIn(sb.iso(BASE + 1800), text)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(self.reads[0][:3], ["git", "ls-remote", "https://github.com/acme/widget-api.git"])
        self.assertEqual(len(self.reads), 1)   # one read of the remote

    def test_before_the_checkpoint_nothing_is_read_or_sent(self):
        self.clock = BASE + 100
        self.assertEqual(self.tick(), [])
        self.assertEqual((self.sent, self.reads), ([], []))

    def test_a_pod_that_pushed_a_pod_that_reported_and_a_pod_already_noticed_get_none_on_a_second_pass(self):
        self.answer = listing(WORK)   # the branch head differs from HEAD: coder7 pushed
        self.assertEqual(self.tick(), [])
        sb.record_first_push_watch(self.dir.name, "coder8", "lead", "acme/widget-api", "feature", BASE + 1800, BASE)
        sb._write_small(self.dir.name, sb.LAST_REPORT_FILE, {"coder8": {"at": BASE + 600}})   # coder8 reported
        self.assertEqual(self.tick(), [])
        self.answer = listing(None)
        sb.record_first_push_watch(self.dir.name, "coder9", "lead", "acme/widget-api", "feature", BASE + 1800, BASE)
        self.assertEqual(self.tick(), ["coder9"])   # the noticed pod
        self.assertEqual(len(self.sent), 1)
        for _ in range(2):   # the second pass: none of the three gets a notice
            self.assertEqual(self.tick(), [])
        self.assertEqual(len(self.sent), 1, "a second pass sends no second notice")

    def test_a_branch_equal_to_head_is_no_push_and_a_report_before_the_launch_is_not_a_report(self):
        self.answer = listing(MAIN)
        self.report(BASE - 50)
        self.assertEqual(self.tick(), ["coder7"])

    def test_a_report_since_the_launch_means_no_notice_and_no_read(self):
        self.report(BASE + 600)
        self.assertEqual(self.tick(), [])
        self.assertEqual((self.sent, self.reads), ([], []))

    def test_a_failed_or_timed_out_read_raises_no_notice_and_is_retried(self):
        for bad in (subprocess.TimeoutExpired(["git"], 20), OSError("x"), subprocess.CompletedProcess([], 128, "", "fatal: " + TOKEN)):
            self.answer = bad
            self.assertEqual(self.tick(), [])
        self.assertEqual(self.sent, [])
        self.answer = listing(None)
        self.assertEqual(self.tick(), ["coder7"])   # the next pass tells it
        self.assertEqual(len(self.sent), 1)

    def test_a_launch_records_its_watch_from_the_checkpoint_flag_or_the_brief(self):
        module = sb.load_module("cloud_worker").bind(vars(sb))
        args = argparse.Namespace(reports_to="lead", checkpoint="30m")
        module._watch_first_push(self.dir.name, "coder1", args, "acme/widget-api", "feature", "", BASE)
        brief = "CHECKPOINT: report at 20 minutes; overdue at 40 minutes"
        module._watch_first_push(self.dir.name, "coder2", argparse.Namespace(reports_to="lead", checkpoint=None), "acme/widget-api", "feature", brief, BASE)
        module._watch_first_push(self.dir.name, "coder3", argparse.Namespace(reports_to=None, checkpoint="30m"), "acme/widget-api", "feature", "", BASE)
        watch = sb._read_small(self.dir.name, sb.FIRST_PUSH_FILE)
        self.assertEqual(watch["coder1"]["at"], BASE + 1800)
        self.assertEqual(watch["coder2"]["at"], BASE + 1200)
        self.assertNotIn("coder3", watch)
        self.assertEqual(watch["coder1"]["boss"], "lead")


if __name__ == "__main__":
    unittest.main()
