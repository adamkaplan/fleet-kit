"""The keyed-reminder supersede tests of #1711: a delivered keyed reminder drops the older copies of the same key and recipient, whatever their sender."""
import json
from support.loader import sb
from support.wire_fakes import EPOCH


def build(PR4Base):
    class TestKeyedReminderSupersede(PR4Base):
        """#1711: at the delivery of a keyed reminder, every other older copy of its key and recipient still in the file is dropped, one audit line each."""

        def add(self, sender, created, at, key="k", to="platform", **kw):
            item = sb.add_reminder(self.world.state, to, EPOCH + at, EPOCH + created, "look at the build", sender=sender, key=key, **kw)
            return item

        skip = 0   # audit lines of an earlier run in the same world (the control of S4)

        def drops(self):
            return [e for e in self.world.audit() if e["event"] == "reminder.drop_superseded"][self.skip:]

        def hold_then_free(self):
            """One lane, busy: the due copies are queued for it and one pass delivers."""
            self.world.v2.start_turn(self.world.sessions["platform"], "fleet task", fleet=True, lasts=400)
            self.world.tick(100)
            self.world.engine.run_pass()

        def delivered_ids(self):
            return [k[len("reminder:"):] for m in self.world.admissions("platform") for k in m["keys"] if k.startswith("reminder:")]

        def setup_s2(self):
            older = [self.add("b", 1, 10), self.add("c", 2, 11), self.add(None, 3, 12), self.add("d", 0, 5000)]
            newest = self.add("a", 9, 20)
            return older, newest

        def check_s2(self):
            older, newest = self.setup_s2()
            self.hold_then_free()
            self.assertEqual(self.delivered_ids(), [newest["id"]])
            lines = self.drops()
            self.assertEqual(len(lines), 4)
            self.assertEqual(sorted(l["detail"]["dropped"] for l in lines), sorted(i["id"] for i in older))
            self.assertTrue(all(l["detail"]["kept"] == newest["id"] and l["detail"]["by"] == "delivered" for l in lines))
            self.assertEqual([i["id"] for i in sb.read_reminders(self.world.state) if i["id"] in {o["id"] for o in older}], [])

        def test_s2_one_delivery_and_four_drops_whatever_the_sender(self):
            self.check_s2()

        def test_s3_delivered_other_key_other_recipient_and_unkeyed_are_untouched(self):
            self.world.add_agent("web", fleet={"role": "coder", "reports_to": "platform", "issue": 3})
            other_key = self.add("a", 1, 10, key="j")
            other_to = self.add("x", 2, 11, to="web")
            unkeyed = sb.add_reminder(self.world.state, "platform", EPOCH + 12, EPOCH + 3, "plain", sender="a")
            gone_already = self.add("b", 0, 9)
            sb.remove_reminders(self.world.state, {gone_already["id"]})   # a copy already delivered is not in the file
            newest = self.add("a", 9, 20)
            self.hold_then_free()
            self.assertEqual(self.drops(), [])
            present = {i["id"] for i in sb.read_reminders(self.world.state)}
            delivered = {k[len("reminder:"):] for m in self.world.admissions("platform") + self.world.admissions("web") for k in m["keys"]}
            self.assertEqual([i["id"] for i in (other_key, other_to, unkeyed, gone_already) if not (i["id"] in present or i["id"] in delivered or i is gone_already)], [])
            self.assertIn(newest["id"], delivered)
            self.assertFalse([e for e in self.world.audit() if e["event"] == "reminder.drop_superseded" and gone_already["id"] in json.dumps(e)])

        def test_s4_a_mutant_that_keeps_the_older_copies_fails_the_check_after_the_unmutated_control(self):
            self.check_s2()   # the control: the unmutated check passes
            self.skip = len([e for e in self.world.audit() if e["event"] == "reminder.drop_superseded"])
            self.world.tick(1000)
            real = sb.ReminderFacts.drop_older_copies
            sb.ReminderFacts.drop_older_copies = lambda self_, item, items: []
            try:
                with self.assertRaisesRegex(AssertionError, r"^4 != 0"):
                    older, newest = self.setup_s2b()
                    self.hold_then_free()
                    self.assertEqual(len(older), len(self.drops()))
            finally:
                sb.ReminderFacts.drop_older_copies = real

        def setup_s2b(self):
            base = 1000
            older = [self.add("b", base + 1, base + 10), self.add("c", base + 2, base + 11), self.add(None, base + 3, base + 12), self.add("d", base, base + 5000)]
            return older, self.add("a", base + 9, base + 20)

    return TestKeyedReminderSupersede
