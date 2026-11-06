"""#2243 amendment 2: a due reminder is handed once to the ordinary send path, so it is delivered within one pass of its due time.
Reproduces two cases of a real fleet (6 November 2026, 18:20Z): (a) a due reminder logged 'invariant broken: a due reminder of a lane neither held
nor cancelled is not a pending fact' and was not delivered; (b) a due reminder of a lane whose status reads working got `reminder.ceiling`
'queued wake' lines and no delivery for 28 minutes."""
from support.loader import sb
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE

PASS = 30   # seconds between two passes of the test world


def build(PR4Base):
    class TestReminderDueSend(PR4Base):
        def setUp(self):
            super().setUp()
            config = {"steer_after_minutes": 600}
            self.world = self.make_world(config=config)
            self.write_config(dict({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR}, **config))
            self.world.add_agent("w-lane", fleet={"role": "coder", "reports_to": "platform", "issue": 10})
            self.world.add_agent("w-other", fleet={"role": "coder", "reports_to": "platform", "issue": 11})

        def deliveries(self, lane="w-lane"):
            return [e["detail"] for e in self.world.audit() if e["event"] == "v2.synthetic" and e.get("subject") == lane and "keys" in e["detail"]]

        def delivered_pass(self, passes, item_id):
            """The number of the pass (1 = the first pass at or after the due time) that delivered the reminder, or None."""
            for number in range(1, passes + 1):
                self.world.fact("w-other", "other:%d" % number, "traffic %d" % number)
                self.world.tick(PASS)
                self.world.engine.run_pass()
                if any("reminder:" + item_id in d["keys"] or any(item_id in str(k) for k in d["keys"]) or "roll the build" in str(d) for d in self.deliveries()):
                    return number
            return None

        def audit_events(self, *names):
            return [e for e in self.world.audit() if e["event"] in names]

        def test_a_reminder_that_falls_due_during_a_pass_is_no_broken_invariant(self):
            """case (a): the reminder came due after the pass read its facts: no 'invariant broken' line, and it is delivered by the next pass."""
            now = self.world.clock.now()
            item = sb.add_reminder(self.world.state, "w-lane", now + PASS // 2, now - 600, "roll the build")
            collect, world = self.world.engine.collect_facts, self.world
            def late_due():
                facts = collect()
                world.tick(PASS)   # the pass is long: the reminder falls due after its facts were read
                return facts
            self.world.engine.collect_facts = late_due
            self.world.fact("w-lane", "other:0", "other traffic for the lane")   # the lane has a plan in this pass
            self.world.engine.run_pass()
            self.world.engine.collect_facts = collect
            self.assertEqual([e for e in self.audit_events("reminder.waiting") if "invariant broken" in e["detail"]["reason"]], [])
            self.world.engine.run_pass()
            self.assertEqual(len(self.deliveries()), 1)

        def test_a_due_reminder_of_a_working_lane_is_delivered_in_one_pass_with_no_ceiling(self):
            """case (b): the status reads working; the reminder is delivered in the first pass after it is due, never by a ceiling line."""
            self.world.herdr.panes[self.world.panes["w-lane"]]["status"] = "working"
            self.world.engine.run_pass()
            self.world.tick(1320)   # 22 minutes of 'working' with no turn of v2's
            now = self.world.clock.now()
            item = sb.add_reminder(self.world.state, "w-lane", now + 5, now - 1800, "roll the build")
            number = self.delivered_pass(2, item["id"])
            self.assertIsNotNone(number)
            self.assertLessEqual(number, 1)
            self.assertEqual(self.audit_events("reminder.ceiling"), [])

        def test_a_due_reminder_of_a_working_lane_never_waits_for_a_ceiling(self):
            """case (b), later: a reminder already 12 minutes past due on a lane in a real turn is delivered in that pass with no ceiling line."""
            self.world.v2.start_turn(self.world.sessions["w-lane"], "work", fleet=False, lasts=10 ** 7)
            self.world.engine.run_pass()
            self.world.tick(700)
            now = self.world.clock.now()
            item = sb.add_reminder(self.world.state, "w-lane", now - 700, now - 3600, "roll the build")
            number = self.delivered_pass(1, item["id"])
            self.assertEqual(number, 1)
            self.assertEqual(self.audit_events("reminder.ceiling"), [])
            self.assertEqual(self.audit_events("reminder.waiting"), [])

        def test_a_real_turn_lane_gets_it_queued_not_lost(self):
            self.world.v2.start_turn(self.world.sessions["w-lane"], "work", fleet=False, lasts=10 ** 7)
            self.world.engine.run_pass()
            now = self.world.clock.now()
            item = sb.add_reminder(self.world.state, "w-lane", now + 5, now - 60, "roll the build")
            self.assertEqual(self.delivered_pass(1, item["id"]), 1)
            (sent,) = self.deliveries()
            self.assertTrue(sent["in_turn"])
            self.assertEqual(sb.read_reminders(self.world.state), [])
    return TestReminderDueSend
