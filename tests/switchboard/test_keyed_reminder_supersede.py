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
            named = {c["id"] for c in (other_key, other_to, unkeyed, newest)}   # the delivered copy included
            dropped = [e["detail"].get("dropped") or e["detail"].get("id") for e in self.world.audit() if e["event"] in ("reminder.drop_superseded", "reminder.drop_in_flight")]
            self.assertEqual([d for d in dropped if d in named], [], msg="s3: no drop audit line names a copy that must be untouched or the delivered copy")
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

        def source(self):
            return next(s for s in self.world.engine.sources if isinstance(s, sb.ReminderFacts))

        def mutated_run(self, owner, name, old, new, check, rx):
            """The control first (the unmutated check passes), then ONE textual replacement in a copy of the method's source, asserted applied;
            the check must then fail with a message matching the anchored `rx`."""
            import inspect
            import textwrap
            check()
            src = textwrap.dedent(inspect.getsource(getattr(owner, name)))
            self.assertEqual(src.count(old), 1, msg="mutant: the control source has the text once")
            mutated = src.replace(old, new)
            self.assertNotEqual(mutated, src, msg="mutant: the mutation was applied")
            space = dict(getattr(owner, name).__globals__)
            exec(mutated, space)
            real = getattr(owner, name)
            setattr(owner, name, space[name])
            try:
                with self.assertRaisesRegex(AssertionError, rx):
                    check()
            finally:
                setattr(owner, name, real)

        t = 0   # the world's clock offset, as this test has moved it

        def arm(self, key, to="platform"):
            """Two older copies and a newest one of `key`, all due after a tick; returns (older, newest)."""
            base = self.t + 10
            older = [self.add("b", base + 1, base + 10, key=key, to=to), self.add("c", base + 2, base + 11, key=key, to=to)]
            newest = self.add("a", base + 9, base + 20, key=key, to=to)
            self.world.tick(base + 100 - self.t)
            self.t = base + 100
            return older, newest

        def two_plans_one_pass(self, key):
            """One pass reads the facts once; two plans of one recipient deliver from them: the first sweeps, the second sees the swept copies."""
            older, newest = self.arm(key)
            source = self.source()
            facts = source()
            source.newest_first(facts)
            self.assertEqual(source.recheck_delivery(next(f for f in facts if f.key == "reminder:" + newest["id"])), None, msg="low1: the newest is delivered")
            second = {f.key: source.recheck_delivery(f) for f in source.newest_first(facts)}
            self.assertEqual([second["reminder:" + i["id"]] for i in older], ["swept", "swept"], msg="low1: a copy swept by the first plan reads swept in the second")

        def test_low1_two_plans_of_one_pass_read_swept_in_the_second(self):
            self.two_plans_one_pass("k")

        def test_low1_mutant_clearing_in_the_sort_helper_fails_after_the_control(self):
            real = sb.ReminderFacts.newest_first

            def clearing(self_, facts):
                self_.swept.clear()
                return real(self_, facts)
            self.two_plans_one_pass("k")   # the control
            sb.ReminderFacts.newest_first = clearing
            try:
                with self.assertRaisesRegex(AssertionError, r"low1: a copy swept by the first plan"):
                    self.two_plans_one_pass("y")
            finally:
                sb.ReminderFacts.newest_first = real

        def pass_sweeping(self, key):
            self.world.tick(500)   # the last turn of the lane has ended
            self.t += 500
            self.world.v2.start_turn(self.world.sessions["platform"], "fleet task", fleet=True, lasts=400)
            older, newest = self.arm(key)
            self.world.engine.run_pass()
            return {i["id"] for i in older}

        def two_consecutive_passes(self, k1, k2):
            first = self.pass_sweeping(k1)
            reported_first = {l["detail"]["dropped"] for l in self.drops()}
            self.assertEqual(reported_first, first, msg="s5: pass one reports its own ids")
            self.skip = len(self.drops()) + self.skip
            second = self.pass_sweeping(k2)
            reported_second = {l["detail"]["dropped"] for l in self.drops()}
            self.assertEqual(reported_second, second, msg="s5: the second pass reports only its own ids")
            gone = sb.Fact("reminder:" + sorted(first)[0], "platform", "reminder", "x", EPOCH)   # an id of pass one, cancelled after pass two began
            self.skip += len(self.drops())   # what this run reported is not read again by the next run in the same world
            self.assertEqual(self.source().recheck_delivery(gone), "cancelled", msg="s5: pass one's ids are not remembered as swept")

        def test_s5_the_second_pass_does_not_report_the_first_passes_ids(self):
            self.two_consecutive_passes("k", "z")

        def test_s5_mutant_without_the_reset_fails_after_the_control(self):
            import inspect
            real = sb.ReminderFacts.__call__

            def keeping(self_):
                kept = set(self_.swept)
                found = real(self_)
                self_.swept |= kept   # the pass does not reset what the last one swept
                return found
            self.assertIn("self.swept.clear()", inspect.getsource(real), msg="s5: the control source has the reset")
            self.two_consecutive_passes("k", "z")   # the control
            sb.ReminderFacts.__call__ = keeping
            try:
                with self.assertRaisesRegex(AssertionError, r"s5: pass one's ids are not remembered as swept"):
                    self.two_consecutive_passes("p", "q")
            finally:
                sb.ReminderFacts.__call__ = real

        def accumulate_check(self, tag):
            """Two deliveries of one pass (two keys): the swept sets accumulate."""
            o1, n1 = self.arm(tag + "1")
            o2, n2 = self.arm(tag + "2")
            source = self.source()
            facts = source()
            for f in source.newest_first(facts):
                if f.key in ("reminder:" + n1["id"], "reminder:" + n2["id"]):
                    source.recheck_delivery(f)
            self.assertEqual(source.swept, {i["id"] for i in o1 + o2}, msg="low5: the swept set accumulates over the deliveries of a pass")

        def test_low5_swept_accumulates_over_two_deliveries_of_a_pass(self):
            self.accumulate_check("a")

        def test_low5_mutant_replacing_the_union_fails_after_the_control(self):
            self.mutated_run(sb.ReminderFacts, "drop_older_copies", "self.swept |= gone", "self.swept = set(gone)",
                             lambda: self.accumulate_check("m"), r"(?s)low5: the swept set accumulates over the deliveries of a pass$")

        def keep_newer_check(self, tag):
            """The delivered copy is the older one (its newer sibling is still in the future): the newer copy of the same key and recipient is kept."""
            base = self.t + 10
            delivered = self.add("a", base + 9, base + 20, key=tag)
            newer = self.add("b", base + 30, base + 5000, key=tag)
            self.world.v2.start_turn(self.world.sessions["platform"], "fleet task", fleet=True, lasts=400)
            self.world.tick(base + 100 - self.t)
            self.t = base + 100
            self.world.engine.run_pass()
            self.assertIn(delivered["id"], self.delivered_ids(), msg="low6: the due copy is delivered")
            self.assertIn(newer["id"], [i["id"] for i in sb.read_reminders(self.world.state)], msg="low6: a copy newer than the delivered one is kept")
            self.world.tick(500)
            self.t += 500

        def test_low6_a_newer_copy_of_the_same_key_and_recipient_is_kept(self):
            self.keep_newer_check("n")

        def test_low6_mutant_not_equal_for_less_than_fails_after_the_control(self):
            self.mutated_run(sb.ReminderFacts, "drop_older_copies", "< mark", "!= mark",
                             lambda: self.keep_newer_check("o"), r"(?s)low6: a copy newer than the delivered one is kept$")

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
