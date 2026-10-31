"""#1728 part 1: a due reminder the daemon does not deliver in a pass writes one `reminder.waiting` audit line (once per reason, again on a
changed reason or after 30 minutes). One case per candidate path. PR4Base lives in the entry file: bin/test-switchboard passes it to build()."""
from unittest import mock
from support.loader import sb
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE


def build(PR4Base):
    class TestReminderWaiting(PR4Base):
        """Five lanes, each with a due reminder that is not delivered: a batch window, a hold, an engaged note, an unreadable condition, a turn."""

        def setUp(self):
            super().setUp()
            config = {"steer_after_minutes": 600}
            self.world = self.make_world(config=config)
            self.write_config(dict({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR}, **config))
            self.ids = {}
            for number, name in enumerate(("w-batch", "w-hold", "w-note", "w-turn"), start=10):
                self.world.add_agent(name, fleet={"role": "coder", "reports_to": "platform", "issue": number})
            self.block("w-hold")                                     # blocked: a hold
            self.world.v2.start_turn(self.world.sessions["w-turn"], "task", fleet=True, lasts=86400, steps=3)
            self.world.engine.run_pass()                             # the pass that first sees the turn records its start
            self.world.tick(10 * 60)
            self.world.you("w-note")                                 # engaged: a note
            for name in ("w-batch", "w-hold", "w-note", "w-turn"):
                self.ids[name] = self.remind(name)
            self.ids["w-gone"] = self.remind("w-gone", condition={"unless_report_from": "coder", "since": 0})
            collect, born = self.world.engine.collect_facts, self.world.clock.now()

            def batching():
                facts, more = collect()
                for fact in facts:
                    if fact.recipient == "w-batch":
                        fact.batch, fact.created = True, born   # a reminder is never batched by itself: this lane's is, to reach the window
                return facts, more
            self.world.engine.collect_facts = batching
            self.world.engine.deliver = lambda plan, interrupt=False: {"sent": False, "action": plan.action, "reason": "held in the test"}   # a note and a queued wake do reach the lane: here they stay pending, so each pass sees all five

        def remind(self, to, **kwargs):
            now = self.world.clock.now()
            return sb.add_reminder(self.world.state, to, now - 60, now - 3600, "look at the build", **kwargs)["id"]

        def lines(self):
            return [e for e in self.world.audit() if e["event"] == "reminder.waiting"]

        def test_each_path_writes_one_line_with_its_reason_and_a_second_pass_none(self):
            self.world.engine.run_pass()
            lines = self.lines()
            self.assertEqual(len(lines), 5)
            wanted = {"w-batch": "wait: batching until", "w-hold": "hold: w-hold is blocked", "w-note": "note: you prompted",
                      "w-gone": "condition unreadable", "w-turn": "lane in a turn of 10 minutes, queued"}
            for name, reason in wanted.items():
                (line,) = [e for e in lines if e["detail"]["id"] == self.ids[name]]
                self.assertIn(reason, line["detail"]["reason"], name)
                self.assertEqual(line["detail"]["late_min"], 0 if name == "w-batch" else 1)
            self.world.tick(10)
            self.world.engine.run_pass()
            self.assertEqual(len(self.lines()), 5)

        def test_thirty_minutes_later_a_waiting_reminder_is_said_again(self):
            self.world.engine.run_pass()
            self.world.tick(31 * 60)
            self.world.engine.run_pass()
            self.assertGreater(len(self.lines()), 5)

        def test_a_mutant_that_writes_a_line_per_pass_is_caught(self):
            self.world.engine.run_pass()
            self.world.tick(10)
            self.world.engine.run_pass()
            self.assertEqual(len(self.lines()), 5)                   # control: the unmutated code
            with mock.patch.object(sb.ReminderFacts, "says_again", lambda self, item_id, reason, now: True):
                self.world.tick(10)
                self.world.engine.run_pass()
                with self.assertRaisesRegex(AssertionError, r"^10 != 5"):
                    self.assertEqual(len(self.lines()), 5)
    return TestReminderWaiting
