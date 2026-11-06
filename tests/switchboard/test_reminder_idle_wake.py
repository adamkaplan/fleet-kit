"""#2243: a due reminder of a lane that is not in a turn reaches it in the next pass, and a ceiling wake is delivered by the pass that writes it."""
from support.loader import sb
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE

PASS = 30   # seconds between two passes of the test world


def build(PR4Base):
    class TestReminderIdleWake(PR4Base):
        def setUp(self):
            super().setUp()
            config = {"steer_after_minutes": 600}
            self.world = self.make_world(config=config)
            self.write_config(dict({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR}, **config))
            self.world.add_agent("w-idle", fleet={"role": "coder", "reports_to": "platform", "issue": 10})
            self.world.add_agent("w-other", fleet={"role": "coder", "reports_to": "platform", "issue": 11})

        def stale(self):
            """herdr reads 'working' for the lane while v2 has no turn: the status is stale."""
            self.world.herdr.panes[self.world.panes["w-idle"]]["status"] = "working"
            self.world.engine.run_pass()   # the daemon records the first sight of 'working'
            self.world.tick(700)           # longer ago than the reminder bound (10 minutes)

        def run_passes(self, count, due):
            """Runs `count` passes, other traffic every pass; returns {reminder id: the pass number it was delivered in (1 = the first pass at or after due)}."""
            found, ids = {}, dict(due)
            for number in range(1, count + 1):
                self.world.fact("w-other", "other:%d" % number, "traffic %d" % number)
                self.world.tick(PASS)
                self.world.engine.run_pass()
                for event in self.world.audit():
                    if event["event"] == "v2.synthetic" and event.get("subject") == "w-idle":
                        for key in event["detail"].get("keys", []):
                            for name, item_id in ids.items():
                                if key == "reminder:" + item_id:
                                    found.setdefault(name, number)
            return found

        def two_due(self):
            now = self.world.clock.now()
            return {"first": sb.add_reminder(self.world.state, "w-idle", now + 10, now - 600, "roll the build")["id"],
                    "second": sb.add_reminder(self.world.state, "w-idle", now + 250, now - 600, "check the roll")["id"]}

        def deliveries(self):
            return [e["detail"] for e in self.world.audit() if e["event"] == "v2.synthetic" and e.get("subject") == "w-idle" and "keys" in e["detail"]]

        def test_s2_idle_lane_reminders_delivered_in_the_next_pass(self):
            self.stale()
            found = self.run_passes(40, self.two_due())
            self.assertEqual(sorted(found), ["first", "second"])
            self.assertLessEqual(found["first"], 1)
            self.assertLessEqual(found["second"], 250 // PASS + 1)
            self.assertEqual([d["in_turn"] for d in self.deliveries()], [False, False])

        def test_s3_ceiling_wake_delivery_starts_a_turn(self):
            self.stale()
            now = self.world.clock.now()
            sb.add_reminder(self.world.state, "w-idle", now - 700, now - 3600, "roll the build")
            self.run_passes(1, {})
            events = [e for e in self.world.audit() if e.get("subject") == "w-idle"]
            self.assertTrue([e for e in events if e["event"] == "reminder.ceiling"])
            sent = self.deliveries()
            self.assertEqual(len(sent), 1)
            self.assertFalse(sent[0]["in_turn"])
            self.assertEqual(sent[0]["delivery"], "queue")
            self.assertTrue(sent[0]["resume"])
            self.assertFalse(sent[0].get("busy"))

        def test_s4_stale_working_reminders_are_a_new_turn(self):
            self.stale()
            self.run_passes(40, self.two_due())
            sent = self.deliveries()
            self.assertEqual(len(sent), 2)
            self.assertEqual([d["in_turn"] for d in sent], [False, False])
            self.assertEqual([d["delivery"] for d in sent], ["queue", "queue"])

        def test_young_working_status_keeps_queue(self):
            self.world.herdr.panes[self.world.panes["w-idle"]]["status"] = "working"
            self.world.engine.run_pass()   # the daemon records the first sight of 'working'
            self.world.tick(PASS)          # 30 seconds old, far under the reminder bound; session.active lags
            now = self.world.clock.now()
            sb.add_reminder(self.world.state, "w-idle", now - 5, now - 60, "roll the build")
            self.run_passes(1, {})
            sent = self.deliveries()
            self.assertEqual(len(sent), 1)
            self.assertTrue(sent[0]["in_turn"])

        def test_reminder_mixed_with_other_facts_is_a_new_turn_when_stale(self):
            self.stale()
            now = self.world.clock.now()
            sb.add_reminder(self.world.state, "w-idle", now - 5, now - 600, "roll the build")
            self.world.fact("w-idle", "other:1", "traffic for the lane")
            self.run_passes(1, {})
            sent = self.deliveries()
            self.assertEqual(len(sent), 1)
            self.assertFalse(sent[0]["in_turn"])
            self.assertIn("reminder:", " ".join(sent[0]["keys"]))
            self.assertIn("other:1", sent[0]["keys"])

        def test_real_turn_keeps_queue(self):
            self.world.v2.start_turn(self.world.sessions["w-idle"], "work", fleet=False, lasts=10 ** 7)
            self.stale()
            now = self.world.clock.now()
            sb.add_reminder(self.world.state, "w-idle", now - 5, now - 60, "roll the build")
            self.run_passes(1, {})
            sent = self.deliveries()
            self.assertEqual(len(sent), 1)
            self.assertTrue(sent[0]["in_turn"])
        def zero_world(self):
            """the default: steer_after_minutes 0, so no steering and (before #2243-b) no turn-start file."""
            config = {"steer_after_minutes": 0}
            self.world = self.make_world(config=config)
            self.write_config(dict({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR}, **config))
            self.world.add_agent("w-idle", fleet={"role": "coder", "reports_to": "platform", "issue": 10})
            self.world.add_agent("w-other", fleet={"role": "coder", "reports_to": "platform", "issue": 11})

        def test_setting_zero_stale_working_is_a_new_turn(self):
            self.zero_world()
            self.stale()
            now = self.world.clock.now()
            sb.add_reminder(self.world.state, "w-idle", now - 5, now - 600, "roll the build")
            self.run_passes(1, {})
            sent = self.deliveries()
            self.assertEqual(len(sent), 1)
            self.assertFalse(sent[0]["in_turn"])
            self.assertFalse(sent[0].get("busy"))

        def test_setting_zero_real_turn_still_queues_and_never_steers(self):
            self.zero_world()
            self.world.v2.start_turn(self.world.sessions["w-idle"], "work", fleet=False, lasts=10 ** 7)
            self.stale()
            now = self.world.clock.now()
            sb.add_reminder(self.world.state, "w-idle", now - 5, now - 60, "roll the build")
            self.run_passes(1, {})
            sent = self.deliveries()
            self.assertEqual(len(sent), 1)
            self.assertTrue(sent[0]["in_turn"])
            self.assertEqual(sent[0]["delivery"], "queue")
            self.assertFalse(sent[0].get("busy"))
    return TestReminderIdleWake
