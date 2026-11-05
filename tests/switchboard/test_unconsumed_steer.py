"""The #1844 tests: a delivered queue-mode message a working lane has not consumed is steered, `pending` lists it, and a re-armed keyed
reminder removes the older queued copy. build() is called by bin/test-switchboard, which holds Base and TestSteerToWorking."""
import json
import os
import re
import textwrap
from unittest import mock
from support.loader import captured, sb
from support.wire_fakes import EPOCH, FakeHTTP

SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "fleet-switchboard")
STEER_CALL = 'self.v2.inbox_update(observation.session_id, item.get("id"), "steer")'
READ_CALL = 'record, reason = load_module("unconsumed_record").read(self.state_dir, load_module("record_json").parse_bounded)'
CANCEL_CALL = 'self.v2.inbox_cancel(observation.session_id, item.get("id"))'


def method_text(name):
    """The source of one Switchboard method, cut out of the daemon file."""
    with open(SOURCE, encoding="utf-8") as handle:
        text = handle.read()
    found = re.search(r"^    def %s\(.*?(?=^    def |^    # -- )" % name, text, re.S | re.M)
    assert found, name
    return textwrap.dedent(found.group(0))


def compiled(name, old=None, new=None):
    """The method as a function in the daemon module's namespace; with `old`, ONE textual replacement, asserted applied."""
    source = method_text(name)
    if old is not None:
        assert source.count(old) == 1, "the mutation did not apply: %r" % old
        source = source.replace(old, new)
    space = {}
    exec(compile(source, "<%s>" % name, "exec"), sb.__dict__, space)
    return space[name]


def build(Base, TestSteerToWorking):
    class TestUnconsumedSteer(Base):
        """#1844: steer a queue-mode message left unconsumed for the bound, once; list it in `pending`; cancel the older queued copy of a re-armed key."""

        build, work, send, admission = (TestSteerToWorking.build, TestSteerToWorking.work, TestSteerToWorking.send,
                                        TestSteerToWorking.admission)

        def setUp(self):
            super().setUp()
            self.build({})
            self.attach_cancel()

        def attach_cancel(self):
            """The fake service has no `session.inbox.cancel`; it is added to this instance here (the support files stay as they are)."""
            v2 = self.world.v2

            def cancel(params, body):
                session = v2._session(params)
                item = next((i for i in session["inbox"] if i["id"] == params.get("inboxID")), None)
                if item is None:
                    raise FakeHTTP(404, "NotFound", "no such pending input")
                v2._write("session.inbox.cancel", session["id"], item["id"])
                session["inbox"].remove(item)
            v2.op_session_inbox_cancel = cancel

        def writes(self, op):
            return [w for w in self.world.v2.writes if w["op"] == op]

        def inbox(self, name="platform"):
            return self.world.v2.sessions[self.world.sessions[name]]["inbox"]

        def lines(self, event):
            return [e for e in self.world.audit() if e["event"] == event]

        def queue_message(self, text):
            self.assertTrue(self.world.engine.send("coder", "platform", text)["sent"])
            return self.inbox()[-1]

        def minutes(self, count):
            self.world.tick(count * 60)

        def pass_(self):
            return self.world.engine.run_pass()

        # -- S2 and S3

        def scenario_steer(self):
            self.work(lasts=3600, steps=3)
            first, second = self.queue_message("first: look at the build"), self.queue_message("second: and the logs")
            self.minutes(15)
            self.inbox().remove(second)   # the lane consumed the second item at 15 minutes
            self.minutes(4)
            self.pass_()
            self.assertEqual(len(self.writes("session.inbox.update")), 0, "a message was steered before the bound")
            self.minutes(2)
            self.pass_()
            self.assertEqual(len(self.writes("session.inbox.update")), 1, "unconsumed message was not steered")
            self.assertEqual(self.writes("session.inbox.update")[0]["id"], first["id"])
            (line,) = self.lines("unconsumed.steer")
            self.assertEqual(line["detail"]["inbox_id"], first["id"])
            self.assertGreaterEqual(line["detail"]["age_seconds"], 20 * 60)
            self.pass_()
            self.assertEqual(len(self.lines("unconsumed.steer")), 1)
            self.assertEqual(len(self.writes("session.inbox.update")), 1)
            self.assertEqual([w["id"] for w in self.writes("session.inbox.update")], [first["id"]])

        def test_s2_an_unconsumed_queue_message_is_steered_once_with_one_audit_line(self):
            self.scenario_steer()

        def test_s3_control_the_unmutated_step_passes(self):
            with mock.patch.object(sb.Switchboard, "steer_unconsumed", compiled("steer_unconsumed")):
                self.scenario_steer()

        def test_s3_the_mutant_that_never_steers_fails_with_the_anchored_text(self):
            mutant = compiled("steer_unconsumed", STEER_CALL, "pass")
            with mock.patch.object(sb.Switchboard, "steer_unconsumed", mutant):
                with self.assertRaises(AssertionError) as caught:
                    self.scenario_steer()
            self.assertIn("unconsumed message was not steered", str(caught.exception))

        # -- S4

        def test_s4_a_lane_holding_a_request_is_not_steered_and_the_setting_at_0_steers_nothing(self):
            self.work(lasts=7200, steps=3)
            self.queue_message("held case")
            self.minutes(60)
            self.world.herdr.panes[self.world.panes["platform"]]["status"] = "blocked"
            self.pass_()
            self.assertEqual(len(self.writes("session.inbox.update")), 0)
            self.build({"unconsumed_steer_minutes": 0})
            self.attach_cancel()
            self.work(lasts=7200, steps=3)
            self.queue_message("setting off")
            self.minutes(60)
            self.pass_()
            self.assertEqual(len(self.writes("session.inbox.update")), 0)

        def test_s4_the_same_lane_with_no_request_and_the_default_setting_is_steered(self):
            self.work(lasts=7200, steps=3)
            self.queue_message("control")
            self.minutes(60)
            self.pass_()
            self.assertEqual(len(self.writes("session.inbox.update")), 1)

        # -- S5 and S6

        def remind(self, to, text, key=None, sender="coder"):
            sb.add_reminder(self.world.state, to, self.world.clock.now(), self.world.clock.now(), text, sender=sender, key=key)
            self.pass_()
            self.world.tick(2)

        def keyed(self, name="platform"):
            return [i for i in self.inbox(name) if any(k.startswith("reminder:") for k in sb._fleet_keys(i))]

        def two_working_lanes(self):
            self.work(lasts=3600, steps=3)
            self.world.v2.start_turn(self.world.sessions["coder"], "task", fleet=True, lasts=3600, steps=3)
            self.world.tick(5)

        def scenario_rearm(self):
            self.work(lasts=3600, steps=3)
            self.remind("platform", "check the build", key="K")
            (older,) = self.keyed()
            self.remind("platform", "check the build again", key="K")
            copies = [i for i in self.keyed() if "again" in i["text"] or i["id"] == older["id"]]
            self.assertEqual(len(copies), 1, "the older queued copy of the key was not removed")
            self.assertNotIn(older["id"], [i["id"] for i in self.inbox()])
            self.assertEqual([w["id"] for w in self.writes("session.inbox.cancel")], [older["id"]])

        def test_s5_a_re_armed_key_removes_the_older_unconsumed_copy(self):
            self.scenario_rearm()

        def test_s5_control_the_unmutated_step_passes(self):
            with mock.patch.object(sb.Switchboard, "supersede_queued_reminders", compiled("supersede_queued_reminders")):
                self.scenario_rearm()

        def test_s5_the_mutant_that_never_cancels_leaves_two_copies_with_the_anchored_text(self):
            mutant = compiled("supersede_queued_reminders", CANCEL_CALL, "pass")
            with mock.patch.object(sb.Switchboard, "supersede_queued_reminders", mutant):
                with self.assertRaises(AssertionError) as caught:
                    self.scenario_rearm()
            self.assertIn("the older queued copy of the key was not removed", str(caught.exception))

        def test_s6_a_consumed_copy_another_key_another_lane_and_an_unkeyed_one_are_not_touched(self):
            self.two_working_lanes()
            self.remind("platform", "consumed copy", key="K")
            (consumed,) = self.keyed()
            self.inbox().remove(consumed)   # the lane consumed it
            self.remind("platform", "other key", key="K2")
            self.remind("platform", "no key at all")
            self.remind("coder", "same key, another lane", key="K")
            before = {name: sorted(i["id"] for i in self.inbox(name)) for name in ("platform", "coder")}
            self.assertEqual((len(before["platform"]), len(before["coder"])), (2, 1))
            self.remind("platform", "re-armed", key="K")
            self.assertEqual(self.writes("session.inbox.cancel"), [])   # 0 of 4 touched
            self.assertEqual(sorted(i["id"] for i in self.inbox("coder")), before["coder"])
            self.assertTrue(set(before["platform"]) <= {i["id"] for i in self.inbox("platform")})
            self.assertEqual(len(self.inbox("platform")), 3)

        def scenario_restart(self, restart):
            self.work(lasts=7200, steps=3)
            plain = self.queue_message("plain")
            self.remind("platform", "check the build", key="K")
            (older,) = self.keyed()
            self.minutes(21)
            self.pass_()
            self.assertEqual([w["id"] for w in self.writes("session.inbox.update")].count(plain["id"]), 1)
            restart()   # a daemon restart: a new engine over the same state directory
            self.remind("platform", "check the build again", key="K")
            self.assertEqual([w["id"] for w in self.writes("session.inbox.cancel")], [older["id"]], "the older copy was not cancelled after the restart")
            for _ in range(3):
                self.minutes(1)
                self.pass_()
            self.assertEqual([w["id"] for w in self.writes("session.inbox.update")].count(plain["id"]), 1, "msg=the item was steered a second time")

        def new_engine(self):
            self.world.engine = self.world.new_engine()

        def test_u4_a_restart_neither_steers_again_nor_forgets_the_older_copy(self):
            self.scenario_restart(self.new_engine)
            self.assertTrue(os.path.exists(os.path.join(str(self.world.state), "unconsumed.json")))

        def test_u4_control_the_unmutated_loader_passes(self):
            with mock.patch.object(sb.Switchboard, "_record_load", compiled("_record_load")):
                self.scenario_restart(self.new_engine)

        def test_u4_the_mutant_that_keeps_the_record_in_memory_only_fails_with_the_anchored_text(self):
            mutant = compiled("_record_load", READ_CALL, 'record, reason = {"steered": [], "reminders": {}}, None')
            with mock.patch.object(sb.Switchboard, "_record_load", mutant):
                with self.assertRaises(AssertionError) as caught:
                    self.scenario_restart(self.new_engine)
            self.assertIn("the older copy was not cancelled after the restart", str(caught.exception))

        def test_u4_an_unreadable_record_reads_as_empty_and_is_audited_once_after_five_passes(self):
            self.work(lasts=7200, steps=3)
            os.mkdir(os.path.join(str(self.world.state), "unconsumed.json"))
            self.new_engine()
            for _ in range(5):
                self.pass_()
                self.world.tick(5)
            self.queue_message("needs the record")
            self.minutes(21)
            for _ in range(5):
                self.pass_()
                self.world.tick(5)
            found = [e for e in self.lines("unconsumed_record.error") if e["detail"]["phase"] == "read"]
            self.assertEqual([e["detail"]["reason"] for e in found], ["unreadable"])

        # -- S7

        def main(self, *argv):
            with captured() as (out, err):
                code = sb.main(list(argv), runner=self.world.route, now=self.world.clock.now,
                               wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
            return code, out.getvalue(), err.getvalue()

        def test_s7_pending_lists_each_delivered_unconsumed_message_with_its_age(self):
            self.world.tick(61 * 60)
            sid = self.world.sessions["platform"]
            for age, key in ((25, "send:coder:aaa"), (40, "send:coder:bbb"), (61, "send:coder:ccc")):
                self.world.v2.sessions[sid]["inbox"].append({
                    "id": "inb_%s" % key[-3:], "sessionID": sid, "type": "synthetic", "text": "x", "resume": True, "delivery": "queue",
                    "metadata": {"fleet": {"keys": [key], "from": "switchboard"}},
                    "time": {"created": self.world.clock.ms(self.world.clock.now() - age * 60)}})
            code, out, err = self.main("pending", "platform")
            self.assertEqual(code, 0, err)
            found = [line for line in out.splitlines() if "not consumed" in line]
            self.assertEqual(len(found), 3, out)
            for age, key in ((25, "aaa"), (40, "bbb"), (61, "ccc")):
                self.assertEqual(len([l for l in found if "%d min" % age in l and key in l]), 1, out)
            code, out, err = self.main("pending", "coder")
            self.assertEqual(len([line for line in out.splitlines() if "not consumed" in line]), 0, out)

        # -- U2

        def test_u2_a_refused_steer_is_audited_once_for_the_item_and_not_repeated(self):
            self.work(lasts=7200, steps=3)
            item = self.queue_message("refused")

            def refuse(params, body):
                raise FakeHTTP(409, "Conflict", "Pending input cannot change to steer")
            self.world.v2.op_session_inbox_update = refuse
            self.minutes(21)
            for _ in range(5):
                self.pass_()
                self.world.tick(5)
            refusals = [e for e in self.lines("unconsumed.refused") if e["detail"]["inbox_id"] == item["id"]]
            self.assertEqual(len(refusals), 1, json.dumps(refusals))
            self.assertEqual(self.lines("unconsumed.steer"), [])

        # -- the absent record (U4) and light mode

        def scenario_absent(self):
            self.work(lasts=7200, steps=3)
            self.assertFalse(os.path.exists(os.path.join(str(self.world.state), "unconsumed.json")))
            self.new_engine()
            self.queue_message("needs the record")
            self.minutes(21)
            for _ in range(5):
                self.pass_()
                self.world.tick(5)
            self.assertEqual(len(self.lines("unconsumed_record.error")), 0, "msg=an absent record was audited")

        def test_u4_an_absent_record_is_empty_and_writes_nothing_to_the_audit_after_five_passes(self):
            self.scenario_absent()

        def test_u4_control_the_unmutated_loader_passes_on_the_absent_record(self):
            with mock.patch.object(sb.Switchboard, "_record_load", compiled("_record_load")):
                self.scenario_absent()

        def test_u4_the_mutant_that_audits_an_absent_file_fails_with_the_anchored_text(self):
            mutant = compiled("_record_load", 'reason != "missing" and ', "")
            with mock.patch.object(sb.Switchboard, "_record_load", mutant):
                with self.assertRaises(AssertionError) as caught:
                    self.scenario_absent()
            self.assertIn("msg=an absent record was audited", str(caught.exception))

        def test_light_mode_the_three_methods_make_no_session_read(self):
            import types
            self.build({"v2_light": True})
            engine = self.world.engine
            observation = types.SimpleNamespace(name="platform", session_id=self.world.sessions["platform"], status="working",
                                                conflict=[], problem=None)
            calls = []
            with mock.patch.object(engine.v2, "inbox_list", lambda *a, **k: calls.append(a) or []):
                self.assertEqual(engine.steer_unconsumed(observation, engine.now()), [])
                self.assertEqual(engine.supersede_queued_reminders(observation, []), [])
                self.assertEqual(engine.unconsumed_lines({"session": observation.session_id}), [])
            self.assertEqual(calls, [], "msg=a session read under v2_light")

        # -- the hand mutants (each: control first, the mutation asserted applied, one anchored text)

        def kills(self, name, old, new, scenario, anchor):
            """The control passes; the one-replacement mutant fails with `anchor` in its failure."""
            with mock.patch.object(sb.Switchboard, name, compiled(name)):
                scenario()
            with mock.patch.object(sb.Switchboard, name, compiled(name, old, new)):
                with self.assertRaises(AssertionError) as caught:
                    scenario()
            self.assertIn(anchor, str(caught.exception))

        def lane(self, status="working", problem=None, conflict=None):
            import types
            return types.SimpleNamespace(name="platform", session_id=self.world.sessions["platform"], status=status,
                                         conflict=conflict or [], problem=problem)

        def old_item(self, minutes=60):
            self.build({})
            self.attach_cancel()
            self.work(lasts=7200, steps=3)
            item = self.queue_message("old")
            self.minutes(minutes)
            return item

        def steered_ids(self):
            return [w["id"] for w in self.writes("session.inbox.update")]

        def scenario_held(self):
            self.old_item()
            for case in ({"problem": "a real permission request"}, {"conflict": ["ses_other"]}):
                engine = self.world.engine
                engine.steer_unconsumed(self.lane(**case), engine.now())
            self.assertEqual(self.steered_ids(), [], "a held lane was steered")

        def scenario_idle(self):
            self.old_item()
            for status in ("idle", "blocked"):
                engine = self.world.engine
                engine.steer_unconsumed(self.lane(status=status), engine.now())
            self.assertEqual(self.steered_ids(), [], "a lane that is not working was steered")

        def scenario_already_steer(self):
            item = self.old_item()
            item["delivery"] = "steer"
            engine = self.world.engine
            engine.steer_unconsumed(self.lane(), engine.now())
            tried = self.steered_ids() + self.lines("unconsumed.steer") + self.lines("unconsumed.refused")
            self.assertEqual(tried, [], "an item already steer was steered again")

        def test_mutant_1_without_the_hold_a_held_lane_is_steered(self):
            self.kills("steer_unconsumed", "hold_reason(observation) or ", "", self.scenario_held, "a held lane was steered")

        def test_mutant_2_without_the_working_check_an_idle_lane_is_steered(self):
            self.kills("steer_unconsumed", ' or observation.status != "working"', "", self.scenario_idle,
                       "a lane that is not working was steered")

        def test_mutant_3_without_the_queue_check_a_steer_item_is_steered_again(self):
            self.kills("steer_unconsumed", 'and item.get("delivery") == "queue" ', "", self.scenario_already_steer,
                       "an item already steer was steered again")

        def crash_scenario(self):
            item = self.old_item()
            engine = self.world.engine
            with mock.patch.object(engine.v2, "inbox_update", side_effect=RuntimeError("crash")):
                try:
                    engine.steer_unconsumed(self.lane(), engine.now())
                except RuntimeError:
                    pass
            self.new_engine()   # a new process over the same state directory
            engine = self.world.engine
            engine.steer_unconsumed(self.lane(), engine.now())
            self.assertEqual(self.steered_ids(), [], "the item was steered again after a crash before the call")
            return item

        def test_mutant_4_without_the_save_before_the_steer_call_a_crash_repeats_the_steer(self):
            self.kills("steer_unconsumed", "self._record_save()   # before inbox_update: a restart never repeats the try", "pass",
                       self.crash_scenario, "the item was steered again after a crash before the call")

        def rearm_chain(self, crash_cancel):
            """K delivered, K re-armed (its cancel crashes when asked), a new engine, K re-armed again."""
            self.build({})
            self.attach_cancel()
            self.work(lasts=7200, steps=3)
            self.remind("platform", "first", key="K")
            if crash_cancel:
                with mock.patch.object(self.world.engine.v2, "inbox_cancel", side_effect=RuntimeError("crash")):
                    self.remind("platform", "second", key="K")
            else:
                self.remind("platform", "second", key="K")
            self.new_engine()
            self.remind("platform", "third", key="K")

        def scenario_save_before_cancel(self):
            self.rearm_chain(True)
            self.assertEqual([i["text"] for i in self.keyed() if "third" in i["text"] or "second" in i["text"] or "first" in i["text"]].__len__(),
                             1, "a copy whose cancel crashed was forgotten")

        def test_mutant_5_without_the_save_before_the_cancel_a_crashed_copy_is_forgotten(self):
            self.kills("supersede_queued_reminders", "self._record_save()   # the new ids are recorded before the cancel call", "pass",
                       self.scenario_save_before_cancel, "a copy whose cancel crashed was forgotten")

        def scenario_save_after_cancel(self):
            self.rearm_chain(False)
            with open(os.path.join(str(self.world.state), "unconsumed.json"), encoding="utf-8") as handle:
                held = json.load(handle)["reminders"]
            self.assertEqual(len(held), 1, "a cancelled copy stays in the record")

        def test_mutant_6_without_the_save_after_a_cancel_the_record_keeps_a_cancelled_copy(self):
            self.kills("supersede_queued_reminders", "                " + "self._record_save()\n", "                " + "pass\n",
                       self.scenario_save_after_cancel, "a cancelled copy stays in the record")

        def scenario_any(self):
            self.build({})
            self.attach_cancel()
            self.work(lasts=7200, steps=3)
            self.remind("platform", "first", key="K")
            (older,) = self.keyed()
            mixed = {"id": "inb_mixed", "sessionID": self.world.sessions["platform"], "type": "synthetic", "text": "x", "resume": True,
                     "delivery": "queue", "metadata": {"fleet": {"keys": [sb._fleet_keys(older)[0], "lab.fact:9"], "from": "switchboard"}},
                     "time": {"created": self.world.clock.ms()}}
            self.inbox().append(mixed)
            self.remind("platform", "second", key="K")
            self.assertIn("inb_mixed", [i["id"] for i in self.inbox()], "an item with one unrelated key was cancelled")
            self.assertNotIn(older["id"], [i["id"] for i in self.inbox()])

        def test_mutant_7_with_any_an_item_holding_one_older_key_is_cancelled(self):
            self.kills("supersede_queued_reminders", "all(k in older for k in keys)", "any(k in older for k in keys)",
                       self.scenario_any, "an item with one unrelated key was cancelled")

        def scenario_self_copy(self):
            import types
            self.build({})
            self.attach_cancel()
            self.work(lasts=7200, steps=3)
            self.remind("platform", "first", key="K")
            (copy,) = self.keyed()
            engine = self.world.engine
            ident = sb._fleet_keys(copy)[0][len("reminder:"):]
            now = self.world.clock.now()
            sb.write_reminders(self.world.state, [{"id": ident, "to": "platform", "at": now, "created": now, "text": "first",
                                                   "issue": None, "from": "coder", "key": "K"}])   # the same reminder, carried again
            engine.supersede_queued_reminders(self.lane(), [types.SimpleNamespace(key="reminder:" + ident)])
            self.assertEqual(self.writes("session.inbox.cancel"), [], "the re-armed copy itself was cancelled")
            self.assertIn(copy["id"], [i["id"] for i in self.inbox()])

        def test_mutant_8_without_the_new_key_exclusion_the_copy_itself_is_cancelled(self):
            self.kills("supersede_queued_reminders", "v == ident and k != new_key", "v == ident", self.scenario_self_copy,
                       "the re-armed copy itself was cancelled")

        def scenario_fresh(self):
            import types
            self.build({})
            self.attach_cancel()
            self.work(lasts=7200, steps=3)
            self.remind("platform", "first", key="K")
            second = sb.add_reminder(self.world.state, "platform", self.world.clock.now(), self.world.clock.now(), "second",
                                     sender="coder", key="K")
            engine = self.world.engine
            seen = []
            real = engine.reads.inbox
            with mock.patch.object(engine.reads, "inbox", lambda sid, now, fresh=False: seen.append(fresh) or real(sid, now, fresh=fresh)):
                engine.supersede_queued_reminders(self.lane(), [types.SimpleNamespace(key="reminder:%s" % second["id"])])
            self.assertEqual(seen, [True], "the inbox was not read afresh before the cancel")

        def test_mutant_9_without_fresh_a_cached_inbox_is_trusted(self):
            self.kills("supersede_queued_reminders", "self.reads.inbox(observation.session_id, self.now(), fresh=True) or []",
                       "self.reads.inbox(observation.session_id, self.now()) or []", self.scenario_fresh,
                       "the inbox was not read afresh before the cancel")

        def scenario_bound(self):
            self.old_item(minutes=0)
            engine = self.world.engine
            now = self.world.clock.now()
            exact, younger = self.inbox()[-1], self.queue_message("one second younger")
            exact["time"]["created"], younger["time"]["created"] = self.world.clock.ms(now - 1200), self.world.clock.ms(now - 1199)
            self.assertEqual(engine.steer_unconsumed(self.lane(), now), [exact["id"]], "the bound is not inclusive or not exact")

        def test_mutant_10_with_a_looser_bound_the_item_exactly_at_it_is_not_steered(self):
            self.kills("steer_unconsumed", "now - _created(item) >= bound", "now - _created(item) > bound + 1", self.scenario_bound,
                       "the bound is not inclusive or not exact")

        # -- Medium 2 and Low 4

        def scenario_non_finite(self):
            self.build({})
            self.attach_cancel()
            sid = self.world.sessions["platform"]
            self.world.tick(3600)
            for name, created in (("nan", float("nan")), ("inf", float("inf")), ("ok", self.world.clock.ms(self.world.clock.now() - 1500))):
                self.world.v2.sessions[sid]["inbox"].append({
                    "id": "inb_" + name, "sessionID": sid, "type": "synthetic", "text": "x", "resume": True, "delivery": "queue",
                    "metadata": {"fleet": {"keys": ["send:coder:" + name], "from": "switchboard"}}, "time": {"created": created}})
            try:
                code, out, err = self.main("pending", "platform")
            except Exception as problem:
                self.fail("pending raised on a non-finite created: %r" % problem)
            self.assertEqual(code, 0, err)
            self.assertEqual(len([l for l in out.splitlines() if "not consumed" in l]), 1, out)
            self.assertEqual(len([l for l in out.splitlines() if ":ok" in l and "25 min" in l]), 1, out)

        def test_pending_skips_a_non_finite_created_and_lists_the_good_item(self):
            self.scenario_non_finite()

        def test_mutant_without_the_finite_check_pending_raises(self):
            self.kills("unconsumed_lines", " and math.isfinite(_created(item))", "", self.scenario_non_finite,
                       "pending raised on a non-finite created")

        def scenario_bad_id(self):
            good = self.old_item()
            self.inbox().insert(0, dict(good, id=["not", "a", "string"]))
            self.inbox().insert(0, dict(good, id={"k": 1}))
            engine = self.world.engine
            try:
                steered = engine.steer_unconsumed(self.lane(), engine.now())
            except TypeError as problem:
                self.fail("an item with an unhashable id broke the pass: %r" % problem)
            self.assertEqual(steered, [good["id"]], "the good item was not steered")

        def test_an_item_with_an_unhashable_id_is_skipped_and_the_good_one_is_steered(self):
            self.scenario_bad_id()

        def test_mutant_without_the_id_type_check_an_unhashable_id_breaks_the_pass(self):
            self.kills("steer_unconsumed", 'isinstance(item.get("id"), str) and ', "", self.scenario_bad_id,
                       "an item with an unhashable id broke the pass")

        # -- the four Lows of a strict read (tests only)

        def add_item(self, name, created, delivery="queue"):
            sid = self.world.sessions["platform"]
            item = {"id": "inb_" + name, "sessionID": sid, "type": "synthetic", "text": "x", "resume": True, "delivery": delivery,
                    "metadata": {"fleet": {"keys": ["send:coder:" + name], "from": "switchboard"}}, "time": {"created": created}}
            self.world.v2.sessions[sid]["inbox"].append(item)
            return item

        def scenario_infinite_created(self):
            self.build({})
            self.attach_cancel()
            self.work(lasts=7200, steps=3)
            self.minutes(60)
            for name, value in (("ninf", float("-inf")), ("pinf", float("inf")), ("nan", float("nan"))):
                self.add_item(name, value)
            engine = self.world.engine
            try:
                steered = engine.steer_unconsumed(self.lane(), engine.now())
            except OverflowError as problem:
                self.fail("a non-finite created escaped the pass: %r" % problem)
            self.assertEqual(steered, [], "a non-finite created was steered")
            self.assertEqual(self.steered_ids(), [], "a non-finite created was steered")

        def test_low1_a_non_finite_created_steers_nothing_and_raises_nothing(self):
            self.scenario_infinite_created()

        def test_low1_mutant_without_the_finite_check_the_pass_raises(self):
            self.kills("steer_unconsumed", " and math.isfinite(_created(item))", "",
                       self.scenario_infinite_created, "a non-finite created escaped the pass")

        def cap_engine_case(self, cap):
            """501 distinct steered ids and 501 distinct reminder ids on one engine whose cap is `cap`."""
            import types
            self.build({})
            self.attach_cancel()
            self.work(lasts=7200, steps=3)
            self.minutes(60)
            created = self.world.clock.ms(self.world.clock.now() - 3600)
            for n in range(501):
                self.add_item("c%03d" % n, created)
            engine = self.world.engine
            with mock.patch.object(sb.Switchboard, "UNCONSUMED_CAP", cap):
                engine.steer_unconsumed(self.lane(), engine.now())
                self.assertNotIn("inb_c000", engine.unconsumed_seen, "the steered map is not capped at 500")
                self.assertEqual(len(engine.unconsumed_seen), 500, "the steered map is not capped at 500")
                now = self.world.clock.now()
                ids = [sb.add_reminder(self.world.state, "platform", now, now, "r%d" % n, sender="coder", key="K%d" % n)
                       for n in range(501)]
                ids = [i["id"] if isinstance(i, dict) else i for i in ids]
                engine.supersede_queued_reminders(self.lane(), [types.SimpleNamespace(key="reminder:%s" % i) for i in ids])
                self.assertNotIn("reminder:%s" % ids[0], engine.reminder_memory, "the reminder map is not capped at 500")
                self.assertEqual(len(engine.reminder_memory), 500, "the reminder map is not capped at 500")

        def test_low2_the_daemon_cap_is_500_after_501_ids_the_oldest_is_gone(self):
            self.cap_engine_case(sb.Switchboard.UNCONSUMED_CAP)

        def test_low2_mutant_cap_5000_keeps_the_oldest(self):
            with open(SOURCE, encoding="utf-8") as handle:
                text = handle.read()
            old = "    UNCONSUMED_CAP = 500\n"
            self.assertEqual(text.count(old), 1, "the mutation did not apply")
            self.assertEqual(sb.Switchboard.UNCONSUMED_CAP, 500, "the control cap is not 500")
            self.cap_engine_case(sb.Switchboard.UNCONSUMED_CAP)   # control first
            mutated = int(old.replace("500", "5000").split("=")[1])
            with self.assertRaises(AssertionError) as caught:
                self.cap_engine_case(mutated)
            self.assertIn("is not capped at 500", str(caught.exception))

        def scenario_cached_consumed(self):
            self.build({"v2_cache_seconds": 120})   # the memory on: the test world has it off
            self.attach_cancel()
            self.work(lasts=7200, steps=3)
            item = self.queue_message("old")
            self.minutes(60)
            engine = self.world.engine
            now = engine.now()
            self.assertEqual([i["id"] for i in engine.reads.inbox(self.world.sessions["platform"], now)], [item["id"]])   # the cache holds it
            self.inbox().remove(item)   # the lane consumed it since
            attempts = []
            real = engine.v2.inbox_update
            with mock.patch.object(engine.v2, "inbox_update", lambda *a, **k: attempts.append(a) or real(*a, **k)):
                steered = engine.steer_unconsumed(self.lane(), now)   # the fake answers 404 for a consumed item: the attempt is what counts
            self.assertEqual((steered, attempts), ([], []), "a consumed item was steered from the cached inbox")

        def test_low4_a_consumed_item_still_in_the_cache_is_not_steered(self):
            self.scenario_cached_consumed()

        def test_low4_mutant_without_the_fresh_read_the_consumed_item_is_steered(self):
            self.kills("steer_unconsumed", "found = stale(self.reads.inbox(observation.session_id, now, fresh=True))",
                       "found = stale(self.reads.inbox(observation.session_id, now))", self.scenario_cached_consumed,
                       "a consumed item was steered from the cached inbox")

    return TestUnconsumedSteer
