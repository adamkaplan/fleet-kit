"""The `steer_after_minutes` tests of #1540, moved out of bin/test-switchboard (its line ceiling). build() is called by bin/test-switchboard, which holds Base and TestSteerToWorking."""
import json
import os
import types
from support.loader import sb
from support.wire_fakes import EPOCH


def build(Base, TestSteerToWorking):
    class TestSteerLongTurn(Base):
        """#1540: with `steer_after_minutes` on, a peer send and a reminder to a recipient whose turn has lasted longer go in as steer
        (audited busy true with the turn age); off, a young turn or an unknown age: as before."""

        build, work, send, admission = (TestSteerToWorking.build, TestSteerToWorking.work, TestSteerToWorking.send,
                                        TestSteerToWorking.admission)   # the same world, one more class

        def setUp(self):
            super().setUp()
            self.build({})

        def on(self, minutes=30):
            self.build({"steer_after_minutes": minutes})

        def long_turn(self, minutes):
            self.work(lasts=minutes * 60 + 600, steps=3)
            self.world.engine.run_pass()   # the pass that first sees it working records the turn start
            self.world.tick(minutes * 60)

        def intents(self):
            return [e for e in self.world.audit() if e["event"] == "v2.synthetic" and e["phase"] == "intent"]

        def peer(self):
            return self.send(sender="coder")

        def remind(self):
            fact = sb.Fact("reminder:long1", "platform", "reminder", "check the build", EPOCH, batch=False)
            return fact

        def turn_file(self):
            return self.world.engine.state_dir / "turn-start.json"

        def test_a_peer_send_to_a_long_turn_steers_with_busy_and_the_age(self):
            self.on(30)
            self.long_turn(60)
            self.assertTrue(self.peer()["sent"])
            (intent,) = self.intents()
            self.assertEqual((intent["detail"]["delivery"], intent["detail"]["busy"]), ("steer", True))
            self.assertGreaterEqual(intent["detail"]["turn_age_seconds"], 60 * 60)
            self.assertEqual(self.admission()["delivery"], "steer")

        def test_a_reminder_to_a_long_turn_steers(self):
            self.on(30)
            self.long_turn(60)
            plan = self.world.engine.plan("platform", [self.remind()], self.world.engine.discover(self.world.engine.now()), self.world.engine.now())
            self.assertEqual(plan.action, "wake")
            self.assertTrue(self.world.engine.deliver(plan)["sent"])
            (intent,) = self.intents()
            self.assertEqual((intent["detail"]["delivery"], intent["detail"]["busy"]), ("steer", True))
            self.assertIn("turn_age_seconds", intent["detail"])

        def test_the_key_off_queues_a_peer_send_to_a_very_long_turn(self):
            self.build({})
            self.long_turn(600)
            self.assertTrue(self.peer()["sent"])
            (intent,) = self.intents()
            self.assertEqual(intent["detail"]["delivery"], "queue")
            self.assertNotIn("busy", intent["detail"])
            self.assertFalse(self.turn_file().exists())   # off: nothing is read or written

        def test_a_young_turn_queues(self):
            self.on(30)
            self.long_turn(10)
            self.assertTrue(self.peer()["sent"])
            (intent,) = self.intents()
            self.assertEqual(intent["detail"]["delivery"], "queue")
            self.assertNotIn("busy", intent["detail"])

        def test_an_unknown_age_counts_as_young(self):
            self.on(30)
            self.long_turn(60)
            self.turn_file().unlink()   # a restart that lost the record
            self.assertTrue(self.peer()["sent"])
            (intent,) = self.intents()
            self.assertEqual(intent["detail"]["delivery"], "queue")
            self.assertNotIn("busy", intent["detail"])

        def test_an_idle_recipient_is_woken_and_nothing_is_steered(self):
            self.on(30)
            self.assertTrue(self.peer()["sent"])
            (intent,) = self.intents()
            self.assertEqual(intent["detail"]["delivery"], "queue")
            self.assertNotIn("busy", intent["detail"])
            self.assertEqual([c["phase"] for c in self.world.model_calls("platform")], ["turn_start"])

        def test_the_boss_rule_is_unchanged_with_the_key_on(self):
            self.on(30)
            self.work()
            self.assertTrue(self.send()["sent"])
            (intent,) = self.intents()
            self.assertEqual((intent["detail"]["delivery"], intent["detail"]["busy"]), ("steer", True))

        def test_the_predicate_with_the_key_off_never_steers_a_peer_on_age_alone(self):
            decision = sb.steers_into_turn
            working = types.SimpleNamespace(status="working", name="platform", fleet={"reports_to": "cos"})
            peer = [sb.Fact("send:coder:1", "platform", "send", "hi", EPOCH, sender="coder")]
            self.assertFalse(decision({"steer_after_minutes": 0}, working, peer, None, 10 ** 6))
            self.assertFalse(decision({}, working, peer, None, 10 ** 6))
            self.assertTrue(decision({"steer_after_minutes": 30}, working, peer, None, 31 * 60))
            self.assertFalse(decision({"steer_after_minutes": 30}, working, peer, None, 30 * 60))
            self.assertFalse(decision({"steer_after_minutes": 30}, working, peer, None, None))

        def test_a_peer_send_with_a_report_in_one_message_queues_and_a_peer_send_alone_steers(self):   # S11
            decision = sb.steers_into_turn
            working = types.SimpleNamespace(status="working", name="platform", fleet={"reports_to": "cos"})
            peer = sb.Fact("send:coder:1", "platform", "send", "hi", EPOCH, sender="coder")
            report = sb.Fact("report:coder:1", "platform", "report", "done", EPOCH)
            config = {"steer_after_minutes": 30}
            self.assertFalse(decision(config, working, [peer, report], None, 60 * 60))
            self.assertTrue(decision(config, working, [peer], None, 60 * 60))

        def test_an_error_in_the_age_call_is_an_unknown_age_and_queues(self):   # S12
            self.on(30)
            self.long_turn(60)
            turn_start = sb.load_module("turn_start")
            real = turn_start.age

            def broken(*args, **kwargs):
                raise RuntimeError("boom")
            turn_start.age = broken
            try:
                self.assertTrue(self.peer()["sent"])
            finally:
                turn_start.age = real
            (intent,) = self.intents()
            self.assertEqual(intent["detail"]["delivery"], "queue")
            self.assertNotIn("busy", intent["detail"])

        def test_an_unwritable_state_dir_is_audited_once_in_three_passes(self):   # S12
            self.on(30)
            state = self.world.engine.state_dir
            (state / ("turn-start.json.%d.tmp" % os.getpid())).mkdir()   # the temp file cannot be created
            self.work(lasts=4000, steps=3)
            for _ in range(3):
                self.world.engine.run_pass()
                self.world.tick(5)
            self.assertEqual(len([e for e in self.world.audit() if e["event"] == "turn_start.error"]), 1)

        def test_the_first_pass_after_a_daemon_start_discards_the_file(self):   # S9
            self.on(30)
            self.long_turn(60)
            self.assertTrue(self.turn_file().exists())
            self.world.engine.turn_start_fresh = True   # a new process
            self.world.engine.run_pass()
            self.assertEqual(sb.load_module("turn_start").age(self.world.engine.state_dir, self.sid, self.world.engine.now(),
                                                              sb.load_module("record_json").parse_bounded), 0)
            self.assertTrue(self.peer()["sent"])
            (intent,) = self.intents()
            self.assertEqual(intent["detail"]["delivery"], "queue")
            self.assertNotIn("busy", intent["detail"])

        def test_a_file_with_an_old_working_entry_and_a_new_process_is_a_young_turn(self):   # S9
            turn_start = sb.load_module("turn_start")
            parse = sb.load_module("record_json").parse_bounded
            state = self.world.engine.state_dir
            turn_start.note(state, [("s1", "working")], 1000, parse)
            turn_start.note(state, [("s1", "working")], 900000, parse, fresh=True)
            self.assertEqual(turn_start.age(state, "s1", 900000, parse), 0)

        def test_working_idle_working_keeps_resetting_the_age_across_passes(self):   # S9
            self.on(30)
            self.long_turn(60)
            engine = self.world.engine
            engine.run_pass()
            first = sb.load_module("turn_start").age(engine.state_dir, self.sid, engine.now(), sb.load_module("record_json").parse_bounded)
            self.assertGreaterEqual(first, 60 * 60)
            turn_start = sb.load_module("turn_start")
            parse = sb.load_module("record_json").parse_bounded
            turn_start.note(engine.state_dir, [(self.sid, "idle")], engine.now(), parse)
            turn_start.note(engine.state_dir, [(self.sid, "working")], engine.now(), parse)
            self.assertEqual(turn_start.age(engine.state_dir, self.sid, engine.now(), parse), 0)

        def test_the_age_restarts_when_the_status_leaves_working(self):
            turn_start = sb.load_module("turn_start")
            parse = sb.load_module("record_json").parse_bounded
            state = self.world.engine.state_dir
            turn_start.note(state, [("s1", "working")], 1000, parse)
            turn_start.note(state, [("s1", "working")], 2000, parse)
            self.assertEqual(turn_start.age(state, "s1", 2500, parse), 1500)
            turn_start.note(state, [("s1", "idle")], 3000, parse)
            self.assertIsNone(turn_start.age(state, "s1", 3100, parse))
            turn_start.note(state, [("s1", "working")], 4000, parse)
            self.assertEqual(turn_start.age(state, "s1", 4000, parse), 0)
            self.assertEqual(oct(self.turn_file().stat().st_mode & 0o777), oct(0o600))
            self.assertEqual(list(json.loads(self.turn_file().read_text())), ["s1"])

        def test_the_setting_is_validated(self):
            for bad in (-1, "30", True, None):
                with self.assertRaises(sb.ConfigError):
                    sb.validate_config({"steer_after_minutes": bad})
            self.assertEqual(sb.validate_config({})["steer_after_minutes"], 0)
            self.assertEqual(sb.validate_config({"steer_after_minutes": 30})["steer_after_minutes"], 30)

    return TestSteerLongTurn
