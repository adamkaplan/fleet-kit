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

        def late_one(self, minutes, **kwargs):
            now = self.world.clock.now()
            return sb.add_reminder(self.world.state, "w-batch", now - minutes * 60, now - 3600, "ship the widget-api build", **kwargs)["id"]

        def ceilings(self):
            return [e for e in self.world.audit() if e["event"] == "reminder.ceiling"]

        def test_s3_the_bounds_default_to_ten_and_five_and_a_launch_checkpoint_reminder_gets_five(self):
            self.assertEqual((sb.DEFAULTS["reminder_bound_minutes"], sb.DEFAULTS["checkpoint_bound_minutes"]), (10, 5))
            now = self.world.clock.now()
            plain = sb.add_reminder(self.world.state, "w-batch", now, now, "look")
            launch = sb.add_reminder(self.world.state, "w-batch", now, now, "report", condition={"while_running": "w-batch"})
            deadline = dict(plain, deadline=True)
            self.assertEqual([sb.reminder_bound(sb.DEFAULTS, i) for i in (plain, launch, deadline)], [600, 300, 300])

        def test_s9_a_due_reminder_is_a_pending_fact_after_one_pass_and_a_delivery_carries_every_due_one(self):
            self.world.add_agent("w-free", fleet={"role": "coder", "reports_to": "platform", "issue": 20})
            first, second = self.remind("w-free"), self.remind("w-free")
            facts, _ = self.world.engine.collect_facts()
            self.assertEqual(len([f for f in facts if f.recipient == "w-free" and f.key.startswith("reminder:")]), 2)
            del self.world.engine.deliver                       # the real delivery
            self.world.engine.run_pass()
            sent = [e for e in self.world.audit() if e["event"] == "v2.synthetic" and e.get("subject") == "w-free" and e["detail"].get("keys")]
            carried = [k for e in sent for k in e["detail"].get("keys", [])]
            self.assertEqual(len(sent), 1)
            self.assertEqual(len([k for k in carried if k in ("reminder:" + first, "reminder:" + second)]), 2)

        def test_s10_a_reminder_past_its_bound_is_delivered_by_the_next_pass_by_a_queued_wake(self):
            self.late_one(11)
            del self.world.engine.deliver
            self.world.engine.run_pass()
            self.assertEqual(len(self.ceilings()), 1)           # control: the unmutated code
            self.assertEqual(self.ceilings()[0]["detail"]["delivery"], "queued wake")
            self.assertTrue([e for e in self.world.audit() if e["event"] == "v2.synthetic" and e.get("subject") == "w-batch"])

        def test_s10_a_mutant_that_leaves_it_waiting_is_caught(self):
            self.late_one(11)
            del self.world.engine.deliver
            with mock.patch.object(sb.ReminderFacts, "ceiling", lambda self, plan, now: []):
                self.world.engine.run_pass()
                with self.assertRaisesRegex(AssertionError, r"^0 != 1"):
                    self.assertEqual(len(self.ceilings()), 1)

        def long_lane(self, name, issue):
            self.world.add_agent(name, fleet={"role": "coder", "reports_to": "platform", "issue": issue})
            self.world.v2.start_turn(self.world.sessions[name], "task", fleet=True, lasts=86400, steps=3)
            self.world.engine.run_pass()
            self.world.tick(30 * 60)                             # a turn of 3x the bound (10 minutes)

        def sent_to(self, name):
            return [e for e in self.world.audit() if e["event"] == "v2.synthetic" and e.get("subject") == name and e["detail"].get("keys")]

        def steered(self, name):
            return len([e for e in self.sent_to(name) if e["detail"]["delivery"] == "steer"])

        def steer_world(self, name="s-long", issue=30):
            self.long_lane(name, issue)
            self.world.engine.config["steer_after_minutes"] = 0
            now = self.world.clock.now()
            sb.add_reminder(self.world.state, name, now - 11 * 60, now - 3600, "look at the build")
            self.world.engine.__dict__.pop("deliver", None)      # the real delivery

        def test_s4_a_due_reminder_in_a_turn_of_three_bounds_is_steered_not_queued(self):
            self.steer_world()
            self.world.engine.run_pass()
            self.assertEqual((self.steered("s-long"), len(self.sent_to("s-long"))), (1, 1))   # 1 of 1: queue count 0

        def test_s4_a_mutant_that_waits_for_the_turn_to_end_is_caught(self):
            self.steer_world()
            self.world.engine.run_pass()
            self.assertEqual(self.steered("s-long"), 1)          # control: the unmutated code
            self.steer_world("s-long2", 31)
            with mock.patch.object(sb, "reminder_bound", lambda config, item: 10 ** 9):   # the mutant: no bound, so the reminder waits for the turn
                self.world.engine.run_pass()
                with self.assertRaisesRegex(AssertionError, r"^0 != 1"):
                    self.assertEqual(self.steered("s-long2"), 1)

        def test_s6_a_real_open_permission_request_holds_the_due_reminder(self):
            self.steer_world()
            self.world.herdr.panes[self.world.panes["s-long"]]["status"] = "blocked"
            self.world.v2.ask_permission(self.world.sessions["s-long"])
            self.world.engine.run_pass()
            self.assertEqual(len(self.sent_to("s-long")), 0)
            holds = [e for e in self.lines() if e["detail"]["reason"].startswith("hold:") and "s-long" in e["detail"]["reason"]]
            self.assertEqual(len(holds), 1)

        def test_s7_status_counts_per_lane_the_reminders_delivered_later_than_their_bound(self):
            import json, tempfile
            base = 1_800_000_000
            stamp = lambda e: sb.iso(e)
            rows, number = [], 0
            for lane, late, ontime in (("lane-a", 3, 4), ("lane-b", 1, 3)):
                for minutes in [25] * late + [2] * ontime:
                    number += 1
                    due = base + number * 60
                    rows.append({"ts": stamp(due - 60), "event": "reminder.add", "subject": lane, "detail": {"id": "r%d" % number, "at": stamp(due)}})
                    for extra in (0, 60):   # only the FIRST delivery line counts
                        rows.append({"ts": stamp(due + minutes * 60 + extra), "event": "v2.synthetic", "subject": lane, "detail": {"keys": ["reminder:r%d" % number]}})
            with tempfile.TemporaryDirectory() as tmp:
                with open(tmp + "/audit.jsonl", "w") as handle:
                    handle.write("\n".join(json.dumps(r) for r in rows) + "\n")
                now = base + 3600
                counts = sb.late_reminder_counts(tmp, {}, now)
                lines = sb.late_reminder_lines(tmp, {}, now)
            self.assertEqual(counts["lane-a"], 3)
            self.assertEqual(counts["lane-b"], 1)
            self.assertEqual(len(lines), 2)
    return TestReminderWaiting
