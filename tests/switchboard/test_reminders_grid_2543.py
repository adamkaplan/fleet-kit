"""#2544: the 15-minute UTC grid for recurring reminders (`--key`, `--every`), the unkeyed under-15m self-polling chain guard, and the prune of
the reminders of retired workers. build() is called by bin/test-switchboard, which holds PR4Base, captured and the fake binaries."""
import calendar
import types
import unittest

from support.loader import sb


class TestReminderGridAndRecurring(unittest.TestCase):
    """The pure rules: grid_when, parse_every, next_tick, self_poll_chain."""
    NOW = calendar.timegm((2030, 1, 2, 5, 7, 31))   # 05:07:31Z

    def test_a_one_off_stays_exact(self):
        for text, delta in (("3m", 180), ("5m", 300), ("15m", 900), ("30m", 1800)):
            self.assertEqual(sb.parse_when(text, self.NOW), self.NOW + delta)
        self.assertEqual(sb.parse_when("2030-01-02T05:20:00Z", self.NOW), calendar.timegm((2030, 1, 2, 5, 20, 0)))

    def test_a_keyed_delay_under_15m_is_refused(self):
        for text in ("1m", "14m", "899s"):
            with self.assertRaises(sb.ReminderError):
                sb.grid_when(text, self.NOW)

    def test_a_keyed_delay_snaps_up_onto_the_grid_and_is_at_least_15m_ahead(self):
        for text in ("15m", "16m", "30m", "1h", "2h", "1d", "37m"):
            at = sb.grid_when(text, self.NOW)
            self.assertEqual(at % 900, 0, text)
            self.assertGreaterEqual(at - self.NOW, 900, text)
            self.assertLess(at - self.NOW, sb.parse_when(text, 0) + 900, text)
        self.assertEqual(sb.grid_when("15m", self.NOW), calendar.timegm((2030, 1, 2, 5, 30, 0)))

    def test_a_fractional_now_on_a_tick_still_lands_15m_ahead(self):
        boundary = calendar.timegm((2030, 1, 2, 6, 0, 0))
        self.assertEqual(sb.grid_when("15m", boundary), boundary + 900)
        self.assertEqual(sb.grid_when("15m", boundary + 0.5), boundary + 1800)
        self.assertGreaterEqual(sb.grid_when("15m", boundary + 0.5) - (boundary + 0.5), 900)

    def test_an_unpruned_due_self_reminder_counts_as_a_wake(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            now = float(self.NOW)
            sb.add_reminder(tmp, "widget-worker", now - 120, now - 600, "poll", sender="widget-worker")   # due, not yet pruned, no record
            self.assertIn("--every", sb.self_poll_chain(tmp, "widget-worker", "widget-worker", now + 300, now, False))
            self.assertIsNone(sb.self_poll_chain(tmp, "widget-other", "widget-other", now + 300, now, False))

    def test_a_keyed_iso_time_must_be_on_the_grid(self):
        self.assertEqual(sb.grid_when("2030-01-02T09:45:00Z", self.NOW), calendar.timegm((2030, 1, 2, 9, 45, 0)))
        for text in ("2030-01-02T09:40:00Z", "2030-01-02T09:45:30Z"):
            with self.assertRaises(sb.ReminderError):
                sb.grid_when(text, self.NOW)

    def test_every_is_a_multiple_of_15m_and_at_least_15m(self):
        self.assertEqual([sb.parse_every(t) for t in ("15m", "30m", "1h", "2h", "1d")], [900, 1800, 3600, 7200, 86400])
        for text in ("10m", "20m", "45s", "0m", "x", "1w"):
            with self.assertRaises(sb.ReminderError):
                sb.parse_every(text)

    def test_the_next_tick_follows_the_schedule_not_the_delivery(self):
        at = calendar.timegm((2030, 1, 2, 6, 0, 0))
        self.assertEqual(sb.next_tick(at, 1800, at + 400), at + 1800)   # delivered late: still on the grid
        self.assertEqual(sb.next_tick(at, 900, at + 2000), at + 2700)   # the daemon was down: the first tick after now

    def test_a_rearm_keeps_the_chain_and_its_condition(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            at = calendar.timegm((2030, 1, 2, 6, 0, 0))
            item = sb.add_reminder(tmp, "widget-worker", at, at - 900, "poll", sender="widget-orch", key="k", every=1800,
                                   condition={"until_closed": "acme-org/widget-api#1"})
            new = sb.rearm_every(tmp, item, at + 5)
            self.assertEqual((new["at"], new["every"], new["key"], new["to"], new["condition"]),
                             (at + 1800, 1800, "k", "widget-worker", {"until_closed": "acme-org/widget-api#1"}))
            self.assertIsNone(sb.rearm_every(tmp, dict(item, every=None), at))

    def test_the_self_polling_chain_guard(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            now = float(self.NOW)
            guard = lambda to, at, keyed=False: sb.self_poll_chain(tmp, "widget-worker", to, now + at, now, keyed)
            self.assertIsNone(guard("widget-worker", 300))     # nothing woke it yet: upfront one-shots pass
            self.assertIsNone(guard("widget-worker", 900))
            sb.note_self_wake(tmp, "widget-worker", now - 60)
            self.assertIn("--every", guard("widget-worker", 300))   # woke from a self-reminder a minute ago
            self.assertIsNone(guard("widget-worker", 900))
            self.assertIsNone(guard("widget-worker", 300, keyed=True))
            self.assertIsNone(guard("widget-peer", 300))       # not a self-reminder
            sb.note_self_wake(tmp, "widget-worker", now - 1000)
            self.assertIsNone(guard("widget-worker", 300))     # the wake is older than 15m


def build(PR4Base, captured, FAKE_OPENCODE, FAKE_HERDR):
    class TestDeadWorkerReminderPrune(PR4Base):
        def setUp(self):
            super().setUp()
            self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})

        def add(self, to, **kw):
            now = self.world.clock.now()
            return sb.add_reminder(self.world.state, to, now + 3600, now, "poll", sender="platform", **kw)

        def arm(self, *argv):
            with captured() as (out, err):
                code = sb.main(["remind"] + list(argv), self.world.route, self.world.clock.now, lambda s: None)
            return code, err.getvalue()

        def test_a_retired_workers_reminders_are_pruned_and_live_ones_stay(self):
            registry = sb.load_module("registry")
            registry.retire(self.world.state, "gone", "test", "done", session="ses_example1", record={"role": "coder"})
            registry.retire(self.world.state, "retired-chief", "test", "done", session="ses_example2", record={"role": "chief-of-staff"})
            dead, chief, live = self.add("gone"), self.add("retired-chief"), self.add("coder")
            self.world.engine.run_pass()
            ids = {i["id"] for i in sb.read_reminders(self.world.state)}
            self.assertEqual(ids, {chief["id"], live["id"]})
            self.assertTrue([e for e in self.world.audit() if e["event"] == "reminder.prune_orphans"])

        def test_an_unknown_name_is_not_pruned(self):
            keep = self.add("never-registered")
            self.world.engine.run_pass()
            self.assertEqual([i["id"] for i in sb.read_reminders(self.world.state)], [keep["id"]])

        def test_the_cli_refuses_short_keyed_and_unkeyed_chains_and_arms_the_grid(self):
            code, err = self.arm("platform", "5m", "--key", "k", "--from", "coder", "x")
            self.assertEqual(code, 2, err)
            self.assertEqual(sb.read_reminders(self.world.state), [])
            code, err = self.arm("platform", "30m", "--key", "k", "--every", "30m", "--from", "coder", "x")
            self.assertEqual(code, 0, err)
            (item,) = sb.read_reminders(self.world.state)
            self.assertEqual((item["at"] % 900, item["every"]), (0, 1800))
            self.assertEqual(self.arm("platform", "30m", "--every", "30m", "--from", "coder", "x")[0], 2)
            self.assertEqual(self.arm("platform", "5m", "--from", "coder", "one-off")[0], 0)
            exact = [i for i in sb.read_reminders(self.world.state) if i["text"] == "one-off"]
            self.assertEqual(exact[0]["at"], self.world.clock.now() + 300)

        def test_an_agent_woken_by_its_own_reminder_cannot_chain_a_short_one(self):
            self.assertEqual(self.arm("coder", "5m", "--from", "coder", "a")[0], 0)
            self.assertEqual(self.arm("coder", "15m", "--from", "coder", "b")[0], 0)   # upfront one-shots pass
            sb.note_self_wake(self.world.state, "coder", self.world.clock.now() - 30)
            code, err = self.arm("coder", "5m", "--from", "coder", "c")
            self.assertEqual(code, 2, err)
            self.assertIn("--key", err)
            self.assertEqual(self.arm("coder", "15m", "--key", "poll", "--from", "coder", "d")[0], 0)

        def deliver(self):
            for _ in range(3):
                self.world.tick(5)
                self.world.engine.run_pass()

        def test_a_delivered_every_reminder_rearms_on_the_grid(self):
            now = self.world.clock.now()
            at = (int(now) // 900 + 1) * 900.0
            sb.add_reminder(self.world.state, "coder", at, now, "poll", sender="platform", key="k", every=1800)
            self.world.tick(at - now + 5)
            self.deliver()
            items = sb.read_reminders(self.world.state)
            self.assertEqual([(i["at"], i["every"], i["key"]) for i in items], [(at + 1800, 1800, "k")])

        def test_a_delivered_self_reminder_blocks_a_short_unkeyed_one_without_a_manual_note(self):
            now = self.world.clock.now()
            sb.add_reminder(self.world.state, "coder", now + 10, now, "poll", sender="coder")
            self.world.tick(15)
            self.deliver()
            self.assertEqual(sb.read_reminders(self.world.state), [])
            self.assertIn("coder", sb.read_record_file(self.world.state / "self-wakes.json"))
            code, err = self.arm("coder", "5m", "--from", "coder", "again")
            self.assertEqual(code, 2, err)
            self.assertIn("--every", err)

        def orphans(self, items, live=(), discovery="ok"):
            if discovery == "ok":
                discovery = types.SimpleNamespace(agents={n: object() for n in live}, problems=[])
            return sb.retired_orphans(self.world.state, discovery, items)

        def retire(self, name, record=None, session="ses_example1"):
            sb.load_module("registry").retire(self.world.state, name, "test", "done", session=session, record=record or {"role": "coder"})

        def test_a_partial_or_missing_discovery_prunes_nothing(self):
            self.retire("gone")
            item = self.add("gone")
            self.assertEqual(self.orphans([item]), [item["id"]])
            self.assertEqual(self.orphans([item], discovery=None), [])
            self.assertEqual(self.orphans([item], discovery=types.SimpleNamespace(agents={}, problems=["herdr unreadable"])), [])

        def test_a_chartered_or_sessionless_retirement_is_kept(self):
            self.retire("chartered", record={"role": "coder", "charter": 7}, session="ses_example3")
            self.retire("sessionless", session=None)
            self.assertEqual(self.orphans([self.add("chartered"), self.add("sessionless")]), [])

        def test_the_chain_check_and_the_write_are_one_step(self):
            now = float(self.world.clock.now())
            sb.note_self_wake(self.world.state, "coder", now - 10)
            with self.assertRaises(sb.ReminderError):
                sb.add_reminder(self.world.state, "coder", now + 300, now, "x", sender="coder", check_chain=True)
            self.assertEqual(sb.read_reminders(self.world.state), [])

        def test_an_unreadable_registry_prunes_nothing(self):
            registry = sb.load_module("registry")
            registry.retire(self.world.state, "gone", "test", "done", session="ses_example1", record={"role": "coder"})
            item = self.add("gone")
            (self.world.state / "registry.json").write_text("{not json")
            self.assertEqual(self.orphans([item]), [])

        def test_a_retired_senders_chain_is_pruned_but_its_one_off_to_a_live_recipient_stays(self):
            sb.load_module("registry").retire(self.world.state, "gone", "test", "done", session="ses_example1", record={"role": "coder"})
            now = self.world.clock.now()
            chain = sb.add_reminder(self.world.state, "coder", now + 900, now, "poll", sender="gone", key="k", every=900)
            one_off = sb.add_reminder(self.world.state, "coder", now + 600, now, "once", sender="gone")
            self.assertEqual(self.orphans([chain, one_off]), [chain["id"]])

        def test_a_retired_name_that_is_live_registered_or_an_orchestrator_is_kept(self):
            registry = sb.load_module("registry")
            for name, record in (("live-one", {"role": "coder"}), ("reg-one", {"role": "coder"}), ("orch-one", {"kind": "orchestrator"})):
                registry.retire(self.world.state, name, "test", "done", session="ses_" + name, record=record)
            registry.upsert(self.world.state, "reg-one", {"pane": "p-reg", "session": "ses_other", "role": "coder"})
            items = [self.add(n) for n in ("live-one", "reg-one", "orch-one")]
            self.assertEqual(self.orphans(items, live=("live-one",)), [])
    return TestDeadWorkerReminderPrune
