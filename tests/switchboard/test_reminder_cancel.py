"""The #1401 reminder-cancel tests, moved out of bin/test-switchboard (its line ceiling). World and PR4Base are set by bin/test-switchboard before this file runs (injected into the module, not imported)."""
import json
from pathlib import Path
from unittest import mock
from support.loader import captured, sb
from support.wire_fakes import EPOCH, FAKE_HERDR, FAKE_OPENCODE

World = None     # set by bin/test-switchboard: World lives in the entry file


class TestReminderCancel(PR4Base):
    """#1401: cancel a chain by owner and key; a copy on its way is re-checked at the delivery step."""

    def remind(self, to="platform", delay=600, text="look at the build", **kwargs):
        return sb.add_reminder(self.world.state, to, self.world.clock.now() + delay, self.world.clock.now(),
                               text, **kwargs)

    def reminder_facts(self):
        return self.source(sb.ReminderFacts)()

    def keyed(self, sender="a", key="k", **kw):
        return self.remind(sender=sender, key=key, **kw)

    def cancel_key(self, sender="a", key="k"):
        state = self.world.state
        return sb.cancel_chain(state, sender, key)

    def audit_events(self, event):
        return [e for e in self.world.audit() if e["event"] == event]

    def run_cli(self, *argv):
        with captured() as (out, err):
            code = sb.main(list(argv), self.world.route, self.world.clock.now, lambda s: None)
        return code, out.getvalue(), err.getvalue()

    def test_cancelling_a_chain_drops_the_pending_and_fired_copies_and_writes_one_audit_line(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        base = self.keyed(delay=10)
        sb.write_reminders(self.world.state, [dict(base, id=i, at=EPOCH + at) for i, at in (("p1", 5000), ("f1", 1), ("f2", 2))])
        self.world.tick(100)
        code, out, err = self.run_cli("remind", "--cancel-key", "k", "--from", "a")
        self.assertEqual(code, 0, err)
        self.assertEqual([r for r in sb.read_reminders(self.world.state) if r.get("from") == "a" and r.get("key") == "k"], [])
        self.assertEqual(len(self.audit_events("reminder.cancelled")), 1)

    def test_a_delivered_copy_is_not_recalled_and_a_second_cancel_writes_no_second_line(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        sent = self.keyed(delay=10)
        self.world.tick(10)
        self.assertEqual(len(self.world.engine.run_pass()["sent"]), 1)   # delivered and pruned
        self.keyed(delay=600)
        code, _, err = self.run_cli("remind", "--cancel-key", "k", "--from", "a")
        self.assertEqual(code, 0, err)
        code, _, _ = self.run_cli("remind", "--cancel-key", "k", "--from", "a")
        self.assertEqual(code, 2)
        self.assertEqual(len(self.audit_events("reminder.cancelled")), 1)
        self.assertEqual(len(self.world.admissions("platform")), 1, "no retraction is sent")
        self.assertEqual(self.world.admissions("platform")[0]["keys"], ["reminder:" + sent["id"]])

    def deliver_with(self, race):
        """Run a pass in which `race` lands after the reminder became a fact and before it is delivered."""
        real = self.world.engine.reobserve

        def raced(previous, now):
            race()
            return real(previous, now)

        with mock.patch.object(self.world.engine, "reobserve", raced):
            return self.world.engine.run_pass()

    def test_an_in_flight_copy_superseded_by_a_re_arm_is_not_delivered_and_the_audit_names_both(self):
        old = self.keyed(delay=10)
        self.world.tick(10)
        new = []
        report = self.deliver_with(lambda: new.append(sb.add_reminder(self.world.state, "platform", self.world.clock.now(), self.world.clock.now() + 1, "again", sender="a", key="k")))
        self.assertEqual([m for m in self.world.admissions("platform") if "reminder:" + old["id"] in m["keys"]], [])
        (line,) = self.audit_events("reminder.drop_in_flight")
        self.assertEqual((line["detail"]["dropped"], line["detail"]["kept"]), (old["id"], new[0]["id"]))
        self.assertEqual(report["sent"], [])

    def test_an_in_flight_copy_cancelled_before_delivery_is_not_delivered_and_one_line_names_it(self):
        old = self.keyed(delay=10)
        self.world.tick(10)
        self.deliver_with(lambda: self.cancel_key())
        self.assertEqual(self.world.admissions("platform"), [])
        lines = [e for e in self.world.audit() if old["id"] in json.dumps(e)]
        self.assertEqual([e["event"] for e in lines if e["event"].startswith("reminder.drop")], ["reminder.drop_in_flight"])
        self.assertEqual(lines[-1]["detail"]["kept"], "cancelled")

    def test_another_owner_and_another_key_are_neither_cancelled_nor_dropped(self):
        self.keyed("b", delay=10)
        self.keyed("a", key="j", delay=10)
        mine = self.keyed("a", delay=10)
        self.world.tick(10)
        self.deliver_with(lambda: self.cancel_key())
        keys = sorted(k for m in self.world.admissions("platform") for k in m["keys"])
        self.assertEqual(len(keys), 2)
        self.assertNotIn("reminder:" + mine["id"], keys)
        self.assertEqual(self.audit_events("reminder.drop_in_flight")[0]["detail"]["dropped"], mine["id"])
        self.assertEqual(len(self.audit_events("reminder.drop_in_flight")), 1)

    def planned(self, facts):
        engine = self.world.engine
        discovery = engine.discover(self.world.clock.now())
        return engine.plan("platform", facts, discovery, self.world.clock.now()), discovery

    def test_a_plan_is_decided_again_from_the_facts_left_after_the_recheck_drops_some(self):
        old = self.keyed(delay=10)
        self.world.tick(10)
        (fact,) = [f for f in self.reminder_facts() if f.key == "reminder:" + old["id"]]
        lower = sb.Fact("worker:x:idle", "platform", "worker", "x finished", self.world.clock.now(), batch=True)
        plan, discovery = self.planned([fact, lower])
        self.assertEqual(plan.action, "wake", "the unbatched reminder alone plans the wake")
        alone = sb.decide("platform", [lower], plan.observation, self.world.clock.now(), self.world.engine.batch_seconds,
                          self.world.engine.engaged_seconds)
        self.assertEqual(alone.action, "wait")
        self.cancel_key()
        outcome = self.world.engine.deliver(plan)
        self.assertEqual((outcome["sent"], outcome["action"]), (False, alone.action))
        self.assertEqual(self.world.admissions("platform"), [])
        (line,) = self.audit_events("recheck")
        self.assertEqual((line["detail"]["was"], line["detail"]["now"]), ("wake", "wait"))

    def test_an_unreadable_reminders_file_at_the_recheck_delivers_nothing_loses_nothing_then_delivers_once(self):
        old = self.keyed(delay=10)
        self.world.tick(10)
        path = Path(self.world.state) / "reminders.json"
        good = path.read_text()
        def corrupt():
            path.write_text("not json")
        report = self.deliver_with(corrupt)
        self.assertEqual(self.world.admissions("platform"), [])
        self.assertEqual(report["sent"], [])
        path.write_text(good)
        self.world.engine.run_pass()
        self.world.engine.run_pass()
        sent = [m for m in self.world.admissions("platform") if "reminder:" + old["id"] in m["keys"]]
        self.assertEqual(len(sent), 1)

    def test_a_reminders_file_error_at_the_recheck_defers_with_one_text_free_audit_line(self):
        old = self.keyed(delay=10)
        self.world.tick(10)
        (fact,) = [f for f in self.reminder_facts() if f.key == "reminder:" + old["id"]]
        plan, discovery = self.planned([fact])
        path = Path(self.world.state) / "reminders.json"
        path.write_text(json.dumps({"reminders": [{"bogus": "PLANTED-SECRET-TEXT"}]}))
        outcome = self.world.engine.deliver(plan)
        self.assertEqual(outcome["action"], "deferred")
        self.assertNotIn("PLANTED-SECRET-TEXT", json.dumps(outcome))
        (line,) = self.audit_events("source.error")
        self.assertNotIn("PLANTED-SECRET-TEXT", json.dumps(line))
        self.assertIn("ReminderError", line["detail"]["error"])

    def test_a_sender_less_reminder_passes_the_recheck_and_is_delivered(self):
        item = self.remind(delay=10)
        self.world.tick(10)
        self.world.engine.run_pass()
        self.assertEqual([m["keys"] for m in self.world.admissions("platform")], [["reminder:" + item["id"]]])
        self.assertEqual(self.audit_events("reminder.drop_in_flight"), [])

    def test_an_until_closed_reminder_passes_the_recheck_and_is_delivered(self):
        item = self.remind(delay=10, condition={"until_closed": "o/r#5"})
        self.world.tick(10)
        self.world.engine.run_pass()
        self.assertEqual([m["keys"] for m in self.world.admissions("platform")], [["reminder:" + item["id"]]])
        self.assertEqual(self.audit_events("reminder.drop_in_flight"), [])
