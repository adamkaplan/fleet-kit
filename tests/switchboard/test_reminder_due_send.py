"""#2243 amendment 2: a due reminder is handed once to the ordinary send path, so it is delivered within one pass of its due time.
Reproduces two cases of a real fleet (6 November 2026, 18:20Z): (a) a due reminder logged 'invariant broken: a due reminder of a lane neither held
nor cancelled is not a pending fact' and was not delivered; (b) a due reminder of a lane whose status reads working got `reminder.ceiling`
'queued wake' lines and no delivery for 28 minutes."""
from unittest import mock

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

        # ---- replacements of the tests that pinned the removed wait logic (each names what it replaces)

        def test_a_blocked_lane_keeps_its_reminder_and_gets_it_once_unblocked(self):
            """replaces test_s6_a_real_open_permission_request_holds_the_due_reminder and test_each_path_writes_one_line_with_its_reason...:
            a lane that cannot be written to is held as a send to it is; the reminder is not lost, and is delivered in the first pass after it can be."""
            self.world.herdr.panes[self.world.panes["w-lane"]]["status"] = "blocked"
            self.world.v2.ask_permission(self.world.sessions["w-lane"])
            now = self.world.clock.now()
            sb.add_reminder(self.world.state, "w-lane", now + 5, now - 60, "roll the build")
            self.world.tick(PASS)
            self.world.engine.run_pass()
            self.assertEqual(self.deliveries(), [])
            self.assertEqual(len(sb.read_reminders(self.world.state)), 1)

        def test_a_reminder_long_past_due_is_sent_at_once_with_no_wait_line(self):
            """replaces test_s10_a_reminder_past_its_bound_is_delivered_by_the_next_pass_by_a_queued_wake, its mutant, and
            test_thirty_minutes_later_a_waiting_reminder_is_said_again: no ceiling, no waiting line; delivered in the pass."""
            now = self.world.clock.now()
            item = sb.add_reminder(self.world.state, "w-lane", now - 3000, now - 3600, "roll the build")
            self.assertEqual(self.delivered_pass(1, item["id"]), 1)
            self.assertEqual(self.audit_events("reminder.ceiling", "reminder.waiting"), [])
            self.assertEqual(sb.read_reminders(self.world.state), [])

        def test_an_unreadable_condition_is_checked_once_and_the_reminder_goes_out_marked(self):
            """replaces test_s11_an_unreadable_condition_past_its_bound..., test_s12/s14/s15 (the ceiling line, once per id): the condition is
            read once at the due time; one that cannot be read does not hold the reminder, and no ceiling line is written."""
            from unittest import mock
            now = self.world.clock.now()
            item = sb.add_reminder(self.world.state, "w-lane", now + 5, now - 60, "check the widget", condition={"while_running": "w-lane"})
            with mock.patch.object(sb.ReminderFacts, "verdict", lambda self, item: None):
                number = self.delivered_pass(1, item["id"])
            self.assertEqual(number, 1)
            self.assertEqual(len(self.audit_events("reminder.condition_unreadable")), 1)
            self.assertEqual(self.audit_events("reminder.ceiling"), [])

        def test_a_due_reminder_in_a_long_turn_goes_in_the_pass_it_is_due(self):
            """replaces test_s4_a_due_reminder_in_a_turn_of_three_bounds_is_steered_not_queued and its mutant: the send path's own steering
            decision (#1540) applies; the reminder is delivered in the pass, never held back for the turn to end."""
            self.world.v2.start_turn(self.world.sessions["w-lane"], "work", fleet=True, lasts=86400, steps=3)
            self.world.engine.run_pass()
            self.world.tick(30 * 60)
            now = self.world.clock.now()
            item = sb.add_reminder(self.world.state, "w-lane", now + 5, now - 60, "roll the build")
            self.assertEqual(self.delivered_pass(1, item["id"]), 1)
            self.assertTrue(self.deliveries()[0]["in_turn"])

        def test_a_gone_lane_keeps_its_reminder_unsent(self):
            now = self.world.clock.now()
            sb.add_reminder(self.world.state, "w-gone", now + 5, now - 60, "nobody home")
            self.world.tick(PASS)
            self.world.engine.run_pass()
            self.assertEqual(self.deliveries("w-gone"), [])
            self.assertEqual(len(sb.read_reminders(self.world.state)), 1)

        def test_a_keyed_reminder_replaced_by_a_newer_one_is_sent_once(self):
            now = self.world.clock.now()
            sb.add_reminder(self.world.state, "w-lane", now + 5, now - 600, "old text", sender="w-other", key="roll")
            new = sb.add_reminder(self.world.state, "w-lane", now + 6, now - 300, "new text", sender="w-other", key="roll")
            self.assertEqual(self.delivered_pass(2, new["id"]), 1)
            self.assertEqual(len(self.deliveries()), 1)
            self.assertEqual(sb.read_reminders(self.world.state), [])

        def wake_class_reminder(self):
            now = self.world.clock.now()
            return sb.add_repeat_reminder(self.world.state, "w-lane", now, now, "Load alert: orch is overloaded", "load-alert", "orch", "2|stalled", wake_class=sb.WAKE_LOAD)

        def test_release_leaves_a_wake_class_reminder_for_its_lane_s_one_wake_message(self):
            """a load alert is a member of the lane's wake message: `release` sends nothing for it and keeps it, and `collect_facts` hands it on as exactly one fact"""
            item = self.wake_class_reminder()
            self.world.tick(PASS)
            self.assertEqual(self.world.engine.sources[[type(x) for x in self.world.engine.sources].index(sb.ReminderFacts)].release(self.world.engine.discover(self.world.clock.now()), self.world.clock.now()), [])
            self.assertEqual(self.deliveries(), [])
            self.assertEqual([r["id"] for r in sb.read_reminders(self.world.state)], [item["id"]])
            self.assertEqual([f.key for f in self.world.engine.collect_facts()[0]], ["reminder:" + item["id"]])

        def test_a_wake_class_reminder_is_delivered_once_then_pruned_with_an_audit_line(self):
            item = self.wake_class_reminder()
            self.world.tick(PASS)
            self.world.engine.run_pass()
            self.assertEqual(len(self.deliveries()), 1)
            self.assertEqual(sb.read_reminders(self.world.state), [], "forgotten by the end of the pass that saw its key delivered")
            self.assertEqual([e["detail"]["ids"] for e in self.audit_events("reminder.prune")], [[item["id"]]])
            self.world.tick(PASS)
            self.world.engine.run_pass()
            self.assertEqual(len(self.deliveries()), 1)

        def test_a_non_wake_reminder_is_no_pending_fact_and_a_crash_in_its_removal_is_reported_and_sends_nothing_twice(self):
            """the removal that follows the send fails once: the error names the release step, the reminder stays, the next pass sends nothing again and the store is emptied"""
            sb.add_reminder(self.world.state, "w-lane", self.world.clock.now(), self.world.clock.now() - 60, "roll the build")
            self.assertEqual(self.world.engine.collect_facts()[0], [])
            real, calls = sb.remove_reminders, []
            def once(*a, **k):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("killed")
                return real(*a, **k)
            self.world.tick(PASS)
            with mock.patch.object(sb, "remove_reminders", once):
                report = self.world.engine.run_pass()
            self.assertIn(("reminders", "release"), [(e["subject"], e["step"]) for e in report["errors"]])
            self.assertEqual(len(self.deliveries()), 1)
            self.world.tick(PASS)
            self.assertEqual(self.world.engine.run_pass()["sent"], [])
            self.assertEqual((len(self.deliveries()), sb.read_reminders(self.world.state)), (1, []))
    return TestReminderDueSend
