"""#2544: the 15-minute UTC grid for recurring reminders (`--key`, `--every`), the unkeyed under-15m self-polling chain guard, and the prune of
the reminders of retired workers. build() is called by bin/test-switchboard, which holds PR4Base, captured and the fake binaries."""
import calendar
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
            registry.retire(self.world.state, "gone", "test", "done", session="ses_example1")
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
    return TestDeadWorkerReminderPrune
