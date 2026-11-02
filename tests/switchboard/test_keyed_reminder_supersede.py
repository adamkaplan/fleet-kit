"""The keyed-reminder supersede tests of #1711: a delivered keyed reminder drops the older copies of the same key and recipient, whatever their sender."""
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
            for name, copy in (("other key", other_key), ("other recipient", other_to), ("unkeyed", unkeyed)):
                self.assertTrue(copy["id"] in present or copy["id"] in delivered, msg="s3: the %s copy was neither kept nor delivered" % name)
            self.assertNotIn(gone_already["id"], present | delivered, msg="s3: the already delivered copy is not brought back")
            self.assertIn(newest["id"], delivered, msg="s3: the newest keyed copy is delivered")

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

        def two_passes(self):
            """Pass one sweeps four copies; pass two sweeps two others; returns the ids of the second pass and the facts source."""
            self.setup_s2()
            self.hold_then_free()
            source = next(s for s in self.world.engine.sources if isinstance(s, sb.ReminderFacts))
            self.assertEqual(len(source.swept), 4, msg="s5: pass one sweeps four copies")
            self.world.tick(1000)
            base = 1000
            older = [self.add("b", base + 1, base + 10, key="z"), self.add("c", base + 2, base + 11, key="z")]
            self.add("a", base + 9, base + 20, key="z")
            self.world.v2.start_turn(self.world.sessions["platform"], "fleet task", fleet=True, lasts=400)
            self.world.tick(1100)
            self.world.engine.run_pass()
            return {i["id"] for i in older}, source

        def test_s5_swept_holds_only_the_ids_of_the_last_pass(self):
            second, source = self.two_passes()
            self.assertEqual(source.swept, second, msg="s5: after two passes swept holds only the second pass's ids")
            self.assertEqual(len(source.swept), 2, msg="s5: two ids")

        def test_s5_mutant_without_the_reset_fails_after_the_control(self):
            import inspect
            import textwrap
            src = textwrap.dedent(inspect.getsource(sb.ReminderFacts.newest_first))
            self.assertIn("self.swept.clear()", src, msg="s5: the control source has the reset")
            mutated = src.replace("self.swept.clear()", "pass")
            self.assertNotEqual(mutated, src, msg="s5: the mutation was applied")
            space = dict(sb.ReminderFacts.newest_first.__globals__)
            exec(mutated, space)
            real = sb.ReminderFacts.newest_first
            sb.ReminderFacts.newest_first = space["newest_first"]
            try:
                with self.assertRaisesRegex(AssertionError, r"s5: after two passes"):
                    self.test_s5_swept_holds_only_the_ids_of_the_last_pass()
            finally:
                sb.ReminderFacts.newest_first = real

        def test_s5_a_copy_dropped_in_the_same_pass_is_audited_as_swept(self):
            self.setup_s2()
            self.hold_then_free()
            self.assertEqual(len(self.drops()), 4, msg="s5: four drop_superseded lines")
            self.assertEqual([e for e in self.world.audit() if e["event"] == "reminder.drop_in_flight"], [], msg="s5: none says cancelled or in flight")

        def setup_s2b(self):
            base = 1000
            older = [self.add("b", base + 1, base + 10), self.add("c", base + 2, base + 11), self.add(None, base + 3, base + 12), self.add("d", base, base + 5000)]
            return older, self.add("a", base + 9, base + 20)

    return TestKeyedReminderSupersede
