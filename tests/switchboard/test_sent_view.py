"""The `sent` tests of #1550, moved out of bin/test-switchboard (its line ceiling). World is set by bin/test-switchboard after the import."""
import inspect
import json
import os
import re
from pathlib import Path
from unittest import mock
from support.core import Base
from support.loader import captured, sb
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE

REPO = Path(__file__).resolve().parents[2]
World = None   # set by bin/test-switchboard: World lives in the entry file


class TestSentView(Base):
    """#1550: `sent` lists the caller's own recent messages with their sent, delivered and picked-up times, from the sent-keys ledger, the audit log and the
    recipient's transcript. It reads only: no GitHub call, no write, and no message text in its output."""
    LINE = re.compile(r"^\S+  sent (\d\d:\d\dZ)  delivered (\d\d:\d\dZ|not yet|unknown)  picked up (\d\d:\d\dZ|not yet|unknown)$")
    TEXT = "PLANTED-TEXT-OF-A-MESSAGE-1550"

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("platform", "coder"))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.ledger = {}
        self.base = self.world.clock.now()

    def main(self, *argv):
        with captured() as (out, err):
            code = sb.main(list(argv), runner=self.world.route, now=self.world.clock.now,
                           wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
        return code, out.getvalue(), err.getvalue()

    def message(self, sender, recipient, sent, delivered="at", ok=True, session=None):
        """One message in the ledger and the audit log. `sent` and `delivered` are epoch seconds; delivered None writes the intent line alone, "none" no audit line."""
        key = "send:%s:%016x" % (sender, len(self.ledger) + 1)
        self.ledger[key] = (session or self.world.sessions[recipient], sent)
        if delivered == "none":
            return key, None
        call = "call%04d" % len(self.ledger)
        lines = [{"ts": sb.iso(sent), "actor": "cli", "event": "v2.synthetic", "subject": recipient, "detail": {"keys": [key], "delivery": "queue"}, "phase": "intent", "call": call}]
        if delivered is not None:
            lines.append({"ts": sb.iso(delivered), "actor": "cli", "event": "v2.synthetic", "subject": recipient, "detail": None, "phase": "result", "call": call, "ok": ok})
        with open(str(self.world.audit_path), "a") as handle:
            for line in lines:
                handle.write(json.dumps(line, sort_keys=True) + "\n")
        return key, delivered

    def write_ledger(self):
        sessions = {}
        for key, (session, at) in self.ledger.items():
            sessions.setdefault(session, {})[key] = at
        (self.world.state / sb.SENT_KEYS_FILE).write_text(json.dumps({"sessions": sessions, "unknown": {}}) + "\n")

    def turn(self, name, at, kind="assistant", text="x"):
        sid = self.world.sessions[name]
        self.world.v2.sessions[sid]["messages"].append({"id": "msg_%d_%d" % (at, len(self.world.v2.sessions[sid]["messages"])), "type": kind, "time": {"created": int(at * 1000)}, "text": text})

    def run_sent(self, *argv, sender="coder"):
        self.write_ledger()
        return self.main("sent", "--from", sender, *argv)

    def lines(self, out):
        return [line for line in out.splitlines() if line not in (sb.SENT_NOTE, "no messages")]

    def test_s2_the_newest_twenty_of_twenty_five_each_with_its_three_fields(self):
        for n in range(25):
            self.message("coder", "platform", self.base - 10000 + n * 10, delivered=self.base - 10000 + n * 10 + 1)
        self.turn("platform", self.base - 100)
        code, out, err = self.run_sent()
        self.assertEqual(code, 0, err)
        lines = self.lines(out)
        self.assertEqual(len(lines), 20)
        self.assertEqual(sum(1 for line in lines if self.LINE.match(line)), 20, lines)
        self.assertEqual(self.LINE.match(lines[0]).group(1), sb.sent_clock(self.base - 10000 + 24 * 10), "newest first")
        self.assertIn(sb.SENT_NOTE, out)
        self.assertEqual(len(self.lines(self.run_sent("--limit", "5")[1])), 5)
        for bad in ("0", "21", "-1"):
            self.assertEqual(self.run_sent("--limit", bad)[0], sb.EXIT_USAGE, bad)

    def test_s3_a_message_of_another_sender_is_never_listed(self):
        self.world.add_agent("elsewhere")
        self.message("coder", "platform", self.base - 50, delivered=self.base - 49)
        self.message("someone", "elsewhere", self.base - 40, delivered=self.base - 39)
        self.message("coder2", "elsewhere", self.base - 30, delivered=self.base - 29)   # a sender whose name starts with the caller's
        code, out, err = self.run_sent()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.lines(out)), 1)
        self.assertEqual(sum(1 for line in self.lines(out) if line.startswith("elsewhere")), 0)
        self.assertTrue(self.lines(out)[0].startswith("platform"))

    def test_s4_no_result_line_reads_not_yet_for_delivered_and_picked_up(self):
        self.message("coder", "platform", self.base - 50, delivered=None)
        self.turn("platform", self.base - 10)   # a turn that began after the send: it is not a pick-up of a message that was not delivered
        code, out, _ = self.run_sent()
        match = self.LINE.match(self.lines(out)[0])
        self.assertEqual((code, match.group(2), match.group(3)), (0, "not yet", "not yet"))

    def test_a_message_older_than_the_audit_tail_reads_unknown(self):
        self.message("coder", "platform", self.base - 50, delivered="none")
        match = self.LINE.match(self.lines(self.run_sent()[1])[0])
        self.assertEqual((match.group(2), match.group(3)), ("unknown", "unknown"))

    def test_s5_picked_up_is_the_first_turn_at_or_after_the_delivery(self):
        delivered = self.base - 100
        self.message("coder", "platform", delivered - 5, delivered=delivered)
        for at in (delivered - 60, delivered + 30, delivered + 90):
            self.turn("platform", at)
        self.turn("platform", delivered + 10, kind="user")
        code, out, _ = self.run_sent()
        self.assertEqual(self.LINE.match(self.lines(out)[0]).group(3), sb.sent_clock(delivered + 30))
        self.assertNotEqual(sb.sent_clock(delivered + 30), sb.sent_clock(delivered + 90))
        self.assertNotEqual(sb.sent_clock(delivered + 30), sb.sent_clock(delivered - 60))

    def test_s5_a_turn_before_the_delivery_alone_is_not_yet(self):
        self.message("coder", "platform", self.base - 105, delivered=self.base - 100)
        self.turn("platform", self.base - 160)
        self.assertEqual(self.LINE.match(self.lines(self.run_sent()[1])[0]).group(3), "not yet")

    def test_s5_a_full_page_that_starts_after_the_delivery_is_unknown_not_a_guess(self):
        delivered = self.base - 5000
        self.message("coder", "platform", delivered - 5, delivered=delivered)
        for n in range(sb.READ_LIMIT):
            self.turn("platform", delivered + 100 + n)
        self.assertEqual(self.LINE.match(self.lines(self.run_sent()[1])[0]).group(3), "unknown")

    def test_s6_an_unreadable_recipient_session_is_unknown_and_the_command_succeeds(self):
        self.message("coder", "platform", self.base - 50, delivered=self.base - 49, session="ses_gone")
        code, out, err = self.run_sent()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.LINE.match(self.lines(out)[0]).group(3), "unknown")
        self.assertEqual(self.LINE.match(self.lines(out)[0]).group(2), sb.sent_clock(self.base - 49))

    def test_the_read_goes_through_the_daemons_budget_and_a_spent_one_is_unknown(self):
        for n in range(3):
            self.message("coder", "platform", self.base - 50 - n, delivered=self.base - 49 - n)
        self.turn("platform", self.base - 10)
        code, out, _ = self.run_sent()
        reads = [c for c in self.world.v2.calls if c[2] == "session.message.list"]
        self.assertEqual(len(reads), 1, "one read per distinct recipient session, not one per message")
        self.world.v2.calls.clear()
        with mock.patch.object(sb.V2Reads, "may_fill", lambda self, now: False):
            code, out, _ = self.run_sent()
        self.assertEqual([c for c in self.world.v2.calls if c[2] == "session.message.list"], [])
        self.assertEqual({self.LINE.match(line).group(3) for line in self.lines(out)}, {"unknown"})

    def test_s7_no_github_call_no_write_and_no_message_text(self):
        self.message("coder", "platform", self.base - 50, delivered=self.base - 49)
        self.turn("platform", self.base - 10, text=self.TEXT)
        self.turn("platform", self.base - 9, kind="user", text=self.TEXT)
        self.write_ledger()
        before = {path.name: path.read_bytes() for path in self.world.state.iterdir() if path.is_file()}
        calls = []
        route = self.world.route
        self.world.route = lambda argv, **kw: (calls.append(argv[0]), route(argv, **kw))[1]   # route() fails on any binary but the two fakes: a gh call is an error
        code, out, err = self.main("sent", "--from", "coder")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(set(calls) - {FAKE_OPENCODE, FAKE_HERDR}), [], "no GitHub call")
        self.assertEqual(self.world.v2.writes, [])
        self.assertEqual({path.name: path.read_bytes() for path in self.world.state.iterdir() if path.is_file()}, before, "no file written or changed")
        self.assertEqual(out.count(self.TEXT) + err.count(self.TEXT), 0, "0 characters of a message text in the output")
        self.assertEqual(self.world.audit_path.read_text().count(self.TEXT), 0)

    def test_the_caller_is_found_as_send_finds_it_and_from_is_lab_only(self):
        self.message("coder", "platform", self.base - 50, delivered=self.base - 49)
        self.write_ledger()
        os.environ["HERDR_PANE_ID"] = self.world.panes["coder"]
        code, out, err = self.main("sent")
        self.assertEqual((code, len(self.lines(out))), (0, 1), err)
        os.environ["HERDR_PANE_ID"] = self.world.panes["platform"]
        self.assertEqual(len(self.lines(self.main("sent")[1])), 0, "another agent sees none of it")
        os.environ.pop(sb.LAB_ENV)
        code, _, err = self.main("sent", "--from", "coder")
        self.assertEqual(code, sb.EXIT_USAGE)
        self.assertIn("only for the Lab", err)

    def mutant(self, name, old, new):
        source = inspect.getsource(getattr(sb, name))
        self.assertIn(old, source)
        scope = {}
        exec(source.replace(old, new), sb.__dict__, scope)
        return mock.patch.object(sb, name, scope[name])

    def test_s8_a_listing_that_ignores_the_sender_fails_the_two_sender_test(self):
        self.test_s3_a_message_of_another_sender_is_never_listed()
        self.setUp()
        with self.mutant("sent_rows", "wanted.fullmatch(key)", 'key.startswith("send:")'):
            with self.assertRaises(AssertionError):
                self.test_s3_a_message_of_another_sender_is_never_listed()

    def test_s8_a_pickup_earlier_than_the_delivery_fails_the_pickup_tests(self):
        self.test_s5_picked_up_is_the_first_turn_at_or_after_the_delivery()
        self.setUp()
        self.test_s5_a_turn_before_the_delivery_alone_is_not_yet()
        self.setUp()
        with self.mutant("sent_pickup", "later = [at for at in turns if at >= delivered]", "later = list(turns)"):
            with self.assertRaises(AssertionError):
                self.test_s5_a_turn_before_the_delivery_alone_is_not_yet()
        self.setUp()
        with self.mutant("sent_pickup", "min(later) if later", "max(later) if later"):
            with self.assertRaises(AssertionError):
                self.test_s5_picked_up_is_the_first_turn_at_or_after_the_delivery()

    def test_an_empty_ledger_prints_no_messages_and_the_note(self):
        code, out, err = self.run_sent()
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["no messages", sb.SENT_NOTE])

    def test_a_sender_with_a_colon_is_matched_whole_and_a_prefix_sender_is_not(self):
        self.message("a:b", "platform", self.base - 50, delivered=self.base - 49)
        self.message("a", "platform", self.base - 40, delivered=self.base - 39)
        self.assertEqual(len(self.lines(self.run_sent(sender="a:b")[1])), 1)
        self.assertEqual(len(self.lines(self.run_sent(sender="a")[1])), 1)
        self.ledger["send:a:b:zzzz"] = (self.world.sessions["platform"], self.base - 30)   # not `<16 hex>`: no sender's
        self.assertEqual(len(self.lines(self.run_sent(sender="a")[1])), 1)

    def test_the_documentation_names_the_read_cap_and_the_failed_delivery(self):
        text = " ".join((REPO / "docs" / "switchboard.md").read_text().split())
        for phrase in ("8th recipient", "delivered unknown", "ok=false"):
            self.assertIn(phrase, text)

    def test_the_documentation_says_what_picked_up_means(self):
        text = " ".join((REPO / "docs" / "switchboard.md").read_text().split())
        for phrase in ("first turn", "not proof that the text was understood", "v2_reads_per_minute", "sent-keys.json", "v2.synthetic", "message_list"):
            self.assertIn(phrase, text)
