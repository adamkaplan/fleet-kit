"""The #1563 reminder-ttl tests, moved out of bin/test-switchboard (its line ceiling). PR4Base lives in the entry file: bin/test-switchboard passes it to build()."""
import json
from support.loader import captured, sb
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE


def build(PR4Base):
    class TestReminderTtlMore(PR4Base):
        """#1563: `remind --ttl` drops a reminder too old to matter, audited."""

        def setUp(self):
            super().setUp()
            self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        def remind(self, to="platform", delay=600, text="look at the build", **kwargs):
            return sb.add_reminder(self.world.state, to, self.world.clock.now() + delay, self.world.clock.now(), text, **kwargs)
        def reminder_facts(self):
            return self.source(sb.ReminderFacts)()
        def keyed(self, sender="a", key="k", **kw):
            return self.remind(sender=sender, key=key, **kw)
        def drops(self):
            return [e for e in self.world.audit() if e["event"] == "reminder.drop_ttl"]
        def arm(self, *argv):
            with captured() as (out, err):
                code = sb.main(["remind"] + list(argv), self.world.route, self.world.clock.now, lambda s: None)
            return code, err.getvalue()
        def test_s3_a_copy_past_its_ttl_is_dropped_and_audited_once(self):
            item = self.remind(delay=10, ttl=1800)
            self.world.tick(10 + 45 * 60)
            self.assertEqual(self.reminder_facts(), [])
            self.assertEqual(sb.read_reminders(self.world.state), [])
            (drop,) = self.drops()
            self.assertIn(item["id"], json.dumps(drop))
            self.assertIn("1800", json.dumps(drop))
            self.assertIn("due", json.dumps(drop))
        def test_s4_a_copy_within_its_ttl_is_made(self):
            self.remind(delay=10, ttl=1800)
            self.world.tick(10 + 10 * 60)
            self.assertEqual(len(self.reminder_facts()), 1)
            self.assertEqual(self.drops(), [])
        def test_s5_a_copy_queued_within_its_ttl_is_not_offered_after_it(self):
            self.remind(delay=10, ttl=1800)
            self.world.tick(10 + 10 * 60)
            self.assertEqual(len(self.reminder_facts()), 1)
            self.world.tick(40 * 60)
            self.assertEqual(self.reminder_facts(), [])
        def test_s7_only_the_item_past_its_ttl_is_dropped(self):
            self.keyed("a", delay=10, ttl=60)
            self.keyed("b", delay=10)
            self.keyed("a", key="j", delay=10)
            self.world.tick(3600)
            self.assertEqual(len(self.reminder_facts()), 2)
            self.assertEqual(len(self.drops()), 1)
            self.assertEqual(len(sb.read_reminders(self.world.state)), 2)
        def test_s2_the_ttl_is_stored_in_seconds_and_listed_and_absent_without_it(self):
            code, err = self.arm("platform", "30m", "--ttl", "30m", "--from", "coder", "with")
            self.assertEqual(code, 0, err)
            code, err = self.arm("platform", "30m", "--from", "coder", "without")
            self.assertEqual(code, 0, err)
            items = {i["text"]: i for i in sb.read_reminders(self.world.state)}
            self.assertEqual(items["with"]["ttl"], 1800)
            self.assertNotIn("ttl", items["without"])
            with captured() as (out, err):
                sb.main(["remind", "--list", "--from", "coder"], self.world.route, self.world.clock.now, lambda s: None)
            self.assertEqual([l for l in out.getvalue().splitlines() if "ttl 30m" in l and "with" in l].__len__(), 1)
        def test_s6_a_bad_ttl_is_refused_with_one_line_and_24h_is_accepted(self):
            for bad in ("25h", "abc", "0"):
                with self.subTest(ttl=bad):
                    code, err = self.arm("platform", "1h", "--ttl", bad, "--from", "coder", "x")
                    self.assertEqual(code, 2)
                    self.assertEqual(len(err.strip().splitlines()), 1)
            self.assertEqual(sb.read_reminders(self.world.state), [])
            code, err = self.arm("platform", "1h", "--ttl", "24h", "--from", "coder", "x")
            self.assertEqual(code, 0, err)
            self.assertEqual(len(sb.read_reminders(self.world.state)), 1)
    return TestReminderTtlMore
