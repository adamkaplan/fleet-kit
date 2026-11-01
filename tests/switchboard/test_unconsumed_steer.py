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

        def test_a_restart_forgets_the_memory_so_the_older_copy_stays_and_the_steer_still_delivers_it(self):
            self.work(lasts=7200, steps=3)
            self.remind("platform", "check the build", key="K")
            (older,) = self.keyed()
            self.world.engine = self.world.new_engine()   # a daemon restart: the memory of delivered reminders is empty
            self.remind("platform", "check the build again", key="K")
            self.assertEqual([i["id"] for i in self.inbox() if i["id"] == older["id"]], [older["id"]])   # the older copy remains: count 1
            self.assertEqual(self.writes("session.inbox.cancel"), [])
            self.minutes(21)
            self.pass_()
            self.assertIn(older["id"], [w["id"] for w in self.writes("session.inbox.update")])

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

    return TestUnconsumedSteer
