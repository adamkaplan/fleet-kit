"""#2051: a send to a blocked agent is held and delivered when it unblocks, not refused. The real engine over the fake herdr, v2 and registry endpoint."""
import contextlib
import inspect
import textwrap
from unittest import mock

from support.core import Base
from support.herdr_world import World
from support.loader import captured, sb
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE

HELD_LINE = "held for platform: platform is blocked; it will be delivered when it unblocks"
ANCHOR = "a send to a blocked agent is held, never refused"


class TestHeldSends(Base):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("platform", "coder"))
        self.panes = self.world.herdr.panes
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})

    def status(self, name, status):
        self.panes[self.world.panes[name]]["status"] = status

    def cli(self, *argv):
        with captured() as (out, err):
            code = sb.main(["send", "--from", "coder"] + list(argv), runner=self.world.route, now=self.world.clock.now,
                           wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
        return code, out.getvalue(), err.getvalue()

    def held(self, route=None):
        return sb.held_rows(self.world.state, route)

    def endpoint_world(self, state="blocked"):
        self.registry = sb.load_module("registry")
        self.set_state(state)
        self.posts = []

        def fake_deliver(client, url, sender, name, text, post=None, **kw):
            self.posts.append((sender, name, text, kw.get("key")))
            return True, ""
        patch = mock.patch.object(sb.load_module("endpoint_send"), "deliver", fake_deliver)
        patch.start()
        self.addCleanup(patch.stop)

    def set_state(self, state):
        self.registry.upsert(self.world.state, "cloudy", {"session": None, "kind": "orchestrator", "role": "orchestrator",
                                                          "endpoint": "https://pod.example.invalid/channel", "state": state}, now=1)

    def blocked_pane_scenario(self):
        self.status("platform", "blocked")
        code, out, err = self.cli("platform", "please", "look")
        self.assertEqual(code, 0, "%s: status %s, %s" % (ANCHOR, code, err))
        self.assertEqual(self.world.admissions("platform"), [], ANCHOR)
        self.assertEqual(len(self.held()), 1, ANCHOR)
        sent_at = self.held()[0][1]["at"]
        self.world.tick(30)
        self.world.engine.run_pass()
        self.assertEqual(self.world.admissions("platform"), [], "still blocked: nothing delivered")
        self.status("platform", "idle")
        self.world.engine.run_pass()
        (admitted,) = self.world.admissions("platform")
        self.assertEqual(len(self.held()), 0, "a delivered send is no longer held")
        return admitted, sent_at

    def test_s2_a_send_to_a_blocked_pane_agent_is_held_and_delivered_once_on_unblock(self):
        admitted, sent_at = self.blocked_pane_scenario()
        self.assertIn("send from coder: please look", admitted["text"])
        self.assertIn(sb.clock_utc(sent_at), admitted["text"])   # its own sent time, not the delivery time
        self.world.tick(5)
        self.world.engine.run_pass()
        self.assertEqual(len(self.world.admissions("platform")), 1, "delivered twice")

    def test_s2_working_also_releases_it(self):
        self.status("platform", "blocked")
        self.cli("platform", "hi")
        self.status("platform", "working")
        self.world.tick(2)
        self.world.engine.run_pass()
        self.assertEqual(len(self.world.admissions("platform")), 1)

    def test_s3_a_send_to_a_blocked_endpoint_agent_is_held_and_delivered_once(self):
        self.endpoint_world()
        code, _, err = self.cli("cloudy", "ping")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.posts, len(self.held("endpoint"))), ([], 1))
        self.world.engine.run_pass()
        self.assertEqual(self.posts, [])
        self.set_state("idle")
        self.world.tick(3)
        self.world.engine.run_pass()
        self.world.engine.run_pass()
        self.assertEqual(len(self.posts), 1)
        sender, name, text, _ = self.posts[0]
        self.assertEqual((sender, name), ("coder", "cloudy"))
        self.assertIn("ping", text)
        self.assertEqual(self.held(), [])

    def test_s4_the_command_says_it_is_held_in_one_line_and_exits_0(self):
        self.status("platform", "blocked")
        code, out, err = self.cli("platform", "hello")
        lines = [l for l in err.splitlines() if "platform" in l and "held" in l and "unblocks" in l]
        self.assertEqual((code, lines), (0, ["%s: %s" % (sb.PROG, HELD_LINE)]), err)

    def test_s5_the_cap_is_50_and_the_oldest_is_told_it_was_dropped(self):
        self.status("platform", "blocked")
        for n in range(sb.HELD_SENDS_KEEP + 1):
            self.world.tick(1)
            self.assertEqual(self.cli("platform", "msg%d" % n)[0], 0)
        rows = self.held()
        self.assertEqual(len(rows), sb.HELD_SENDS_KEEP)
        self.assertNotIn("msg0", [r["text"] for _, r in rows])
        drops = [a for a in self.world.admissions("coder") if "was dropped" in a["text"]]
        self.assertEqual(len(drops), 1)
        self.assertIn("platform", drops[0]["text"])
        self.assertIn("msg0", drops[0]["text"])

    def test_s5_a_send_older_than_24_hours_is_dropped_and_its_sender_told(self):
        self.status("platform", "blocked")
        self.cli("platform", "old", "news")
        self.world.clock.t += 24 * 3600 + 5
        self.world.engine.run_pass()
        self.assertEqual(self.held(), [])
        self.assertEqual(self.world.admissions("platform"), [])
        drops = [a for a in self.world.admissions("coder") if "was dropped" in a["text"]]
        self.assertEqual(len(drops), 1)
        self.assertIn("old news", drops[0]["text"])

    def test_s5_unknown_recipient_is_refused_with_status_3_and_a_doubly_claimed_one_is_held(self):
        # #1400 (a): the conflict half. OLD: code == sb.EXIT_HELD, "not sent" in err, nothing held. NEW: code 0, one send queued.
        self.assertEqual(self.cli("nobody", "hi")[0], sb.EXIT_HELD)
        self.world.add_agent("platform")   # a second session claims the same name
        code, _, err = self.cli("platform", "hi")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.held()), 1)

    def test_s5_a_recipient_with_another_hold_reason_is_held_too(self):   # #1400 (a): OLD: refused with EXIT_HELD, nothing held. NEW: queued, exit 0
        self.status("platform", "weird")
        code, _, err = self.cli("platform", "hi")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.held()), 1)
        self.assertEqual(self.world.admissions("platform"), [])
        self.status("platform", "idle")
        self.world.engine.run_pass()
        self.assertEqual(len(self.world.admissions("platform")), 1)
        self.assertEqual(self.held(), [])

    def test_a_problem_and_a_conflict_hold_a_send_too_and_a_gone_pane_still_refuses(self):
        obs = mock.Mock(status="idle", conflict=["s2"], problem=None, name="platform", session_id="s1")
        self.assertTrue(sb.held_send_applies(obs))
        obs = mock.Mock(status="idle", conflict=[], problem="v2 unreadable", name="platform", session_id="s1")
        self.assertTrue(sb.held_send_applies(obs))
        self.assertFalse(sb.held_send_applies(mock.Mock(status="idle", conflict=[], problem=None, name="platform", session_id="s1")))
        self.assertFalse(sb.held_send_applies(None))

    def test_u1_an_idle_and_a_working_agent_get_the_send_at_once(self):
        self.assertEqual(self.cli("platform", "one")[0], 0)
        self.assertEqual(len(self.world.admissions("platform")), 1)
        self.world.tick(1)
        self.world.v2.start_turn(self.world.sessions["platform"], "task", fleet=True, lasts=60, steps=3)
        self.world.tick(5)
        self.assertEqual(self.cli("platform", "two")[0], 0)
        self.assertEqual(len(self.world.admissions("platform")), 2)
        self.assertEqual(self.held(), [])

    @contextlib.contextmanager
    def mutant(self, owner, name, old, new):
        source = textwrap.dedent(inspect.getsource(getattr(owner, name)))
        self.assertIn(old, source, "the mutation did not apply to %s" % name)
        original = getattr(owner, name)
        scope = {}
        exec(compile(source.replace(old, new, 1), "<mutant %s>" % name, "exec"), vars(sb), scope)
        setattr(owner, name, scope[name])
        try:
            yield
        finally:
            setattr(owner, name, original)

    def test_s6_the_control_passes(self):
        self.blocked_pane_scenario()

    def test_s6_a_mutant_that_refuses_as_today_is_caught_by_the_anchored_text(self):
        old = "return hold_send_fact(self, fact, now)"
        with self.mutant(sb.Switchboard, "send_fact", old, 'return {"sent": False, "action": "hold", "reason": plan.reason}'):
            with self.assertRaises(AssertionError) as caught:
                self.blocked_pane_scenario()
        self.assertIn(ANCHOR, str(caught.exception))


    # ---- #2051 review round: the endpoint route's own limits, order, edges and the sender who cannot be told

    def cli_as(self, sender, *argv):
        with captured() as (out, err):
            code = sb.main(["send", "--from", sender] + list(argv), runner=self.world.route, now=self.world.clock.now,
                           wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
        return code, out.getvalue(), err.getvalue()

    def drops_to(self, name):
        return [a for a in self.world.admissions(name) if "was dropped" in a["text"]]

    def endpoint_scenario(self):
        self.endpoint_world()
        code, _, err = self.cli("cloudy", "ping")
        self.assertEqual(code, 0, "%s: status %s, %s" % (ANCHOR, code, err))
        self.assertEqual(len(self.held("endpoint")), 1, ANCHOR)
        sent_at = self.held("endpoint")[0][1]["at"]
        self.world.tick(30)
        self.world.engine.run_pass()
        self.assertEqual(self.posts, [], "still blocked: nothing posted")
        self.set_state("idle")
        self.world.engine.run_pass()
        self.assertEqual(len(self.posts), 1, "a held endpoint send is delivered when it unblocks")
        return self.posts[0][2], sent_at

    def test_s6_the_endpoint_control_passes(self):
        text, sent_at = self.endpoint_scenario()
        self.assertTrue(text.startswith("(sent %s) " % sb.iso(sent_at)), "no (sent <time>) prefix: %s" % text)

    def test_s6_a_mutant_that_refuses_on_the_endpoint_route_is_caught_by_the_anchored_text(self):
        old = "if held:\n"
        new = 'if held:\n        sys.stderr.write("%s: not sent to %s: held\\n" % (PROG, args.name))\n        return EXIT_HELD\n'
        with self.mutant(sb, "_send_by_registry", old, new):
            with self.assertRaises(AssertionError) as caught:
                self.endpoint_scenario()
        self.assertIn(ANCHOR, str(caught.exception))

    def test_s6_a_mutant_that_never_delivers_the_held_endpoint_send_is_caught(self):
        with self.mutant(sb, "held_sends_tick", 'if found[0] != "endpoint" or', 'if True or'):
            with self.assertRaises(AssertionError) as caught:
                self.endpoint_scenario()
        self.assertIn("a held endpoint send is delivered when it unblocks", str(caught.exception))

    def test_s6_a_mutant_that_drops_the_sent_time_is_caught(self):
        with self.mutant(sb, "held_sends_tick", '"(sent %s) %s" % (iso(row["at"]), row["text"])', 'row["text"]'):
            with self.assertRaises(AssertionError) as caught:
                self.test_s6_the_endpoint_control_passes()
        self.assertIn("(sent ", str(caught.exception))

    def test_s3_the_endpoint_post_carries_the_sends_own_time_not_the_deliverys(self):
        text, sent_at = self.endpoint_scenario()
        self.assertTrue(text.startswith("(sent %s) " % sb.iso(sent_at)), text)
        self.assertNotIn(sb.iso(self.world.clock.now()), text)
        self.assertTrue(text.endswith("ping"), text)

    def test_s5_endpoint_cap_is_50_and_the_oldest_is_told_it_was_dropped(self):
        self.endpoint_world()
        for n in range(sb.HELD_SENDS_KEEP + 1):
            self.world.tick(1)
            self.assertEqual(self.cli("cloudy", "msg%d" % n)[0], 0)
        rows = self.held("endpoint")
        self.assertEqual(len(rows), sb.HELD_SENDS_KEEP)
        self.assertNotIn("msg0", [r["text"] for _, r in rows])
        (drop,) = self.drops_to("coder")
        self.assertIn("cloudy", drop["text"])
        self.assertIn("msg0", drop["text"])

    def test_s5_an_endpoint_send_older_than_24_hours_is_dropped_and_its_sender_told(self):
        self.endpoint_world()
        self.cli("cloudy", "old", "news")
        self.world.clock.t += 24 * 3600 + 5
        self.world.engine.run_pass()
        self.assertEqual((self.held(), self.posts), ([], []))
        (drop,) = self.drops_to("coder")
        self.assertIn("old news", drop["text"])

    def test_s5_a_gone_endpoint_recipient_is_refused_with_status_3_and_nothing_held(self):
        self.endpoint_world()
        self.registry.retire(self.world.state, "cloudy", "test", "gone")
        code, _, err = self.cli("cloudy", "hi")
        self.assertEqual(code, sb.EXIT_HELD, err)
        self.assertEqual((self.held(), self.posts), ([], []))

    def test_d1_two_held_sends_on_the_pane_route_arrive_once_in_one_message_the_ordinary_batch_lists_newest_first(self):
        self.status("platform", "blocked")
        self.cli("platform", "first-one")
        self.world.tick(2)
        self.cli("platform", "second-one")
        self.status("platform", "idle")
        self.world.engine.run_pass()
        self.world.engine.run_pass()
        (admitted,) = self.world.admissions("platform")   # pinned as it is: the ordinary batch format, not a held-send order
        text = admitted["text"]
        self.assertTrue(0 <= text.find("second-one") < text.find("first-one"), text)
        self.assertEqual(self.held(), [])

    def test_d1_two_held_sends_go_oldest_first_on_the_endpoint_route(self):
        self.endpoint_world()
        self.cli("cloudy", "first-one")
        self.world.tick(2)
        self.cli("cloudy", "second-one")
        self.set_state("idle")
        self.world.engine.run_pass()
        self.assertEqual([p[2].split(") ", 1)[1] for p in self.posts], ["first-one", "second-one"])

    def test_d2_a_send_to_oneself_while_blocked_is_held_like_any_other(self):
        self.status("platform", "blocked")
        code, _, err = self.cli_as("platform", "platform", "note", "to", "self")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.held()), 1)
        self.status("platform", "idle")
        self.world.engine.run_pass()
        (admitted,) = self.world.admissions("platform")
        self.assertIn("note to self", admitted["text"])

    def test_d3_unblocking_in_the_middle_of_a_pass_delivers_the_endpoint_send_in_that_pass_and_the_pane_send_in_the_next(self):
        self.endpoint_world()
        self.status("platform", "blocked")
        self.cli("cloudy", "to-endpoint")
        self.cli("platform", "to-pane")
        real = sb.Switchboard.render
        engine = self.world.engine

        def render(this, *a, **kw):
            result = real(this, *a, **kw)
            self.set_state("idle")
            self.status("platform", "idle")   # unblocked after the pass read both
            return result
        with mock.patch.object(sb.Switchboard, "render", render):
            engine.run_pass()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.world.admissions("platform"), [])
        self.world.engine.run_pass()
        self.assertEqual(len(self.world.admissions("platform")), 1)
        self.world.engine.run_pass()
        self.assertEqual((len(self.posts), len(self.world.admissions("platform"))), (1, 1))

    def test_d4_a_daemon_restart_rereads_the_store_and_delivers_each_held_send_once(self):
        self.endpoint_world()
        self.status("platform", "blocked")
        self.cli("cloudy", "to-endpoint")
        self.cli("platform", "to-pane")
        self.world.engine.run_pass()
        self.set_state("idle")
        self.status("platform", "idle")
        fresh = self.world.new_engine()
        fresh.run_pass()
        self.world.new_engine().run_pass()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.world.admissions("platform")), 1)
        self.assertEqual(self.held(), [])

    def test_d5_a_pane_send_older_than_24_hours_is_still_delivered_when_the_recipient_has_just_unblocked(self):
        self.status("platform", "blocked")
        self.cli("platform", "ancient")
        self.world.clock.t += 24 * 3600 + 5
        self.status("platform", "idle")
        self.world.engine.run_pass()   # the pane delivery step runs before the expiry in held_sends_tick: pinned
        (admitted,) = self.world.admissions("platform")
        self.assertIn("ancient", admitted["text"])
        self.assertEqual((self.held(), self.drops_to("coder")), ([], []))

    def audit_events(self, event):
        return [e for e in self.world.audit() if e["event"] == event]

    def test_d6_a_sender_with_no_pane_is_told_only_by_the_audit_line_when_its_send_is_dropped(self):
        self.status("platform", "blocked")
        for n in range(sb.HELD_SENDS_KEEP + 1):
            self.world.tick(1)
            self.assertEqual(self.cli_as("script", "platform", "msg%d" % n)[0], 0)
        (line,) = self.audit_events("held.drop")
        self.assertEqual((line["subject"], line["detail"]["from"]), ("platform", "script"))
        self.assertEqual(self.world.admissions("coder"), [])

    def test_d6_a_post_that_fails_after_release_is_audited_and_a_paneless_sender_is_not_told(self):
        self.endpoint_world()
        self.cli_as("script", "cloudy", "ping")
        self.set_state("idle")
        patch = mock.patch.object(sb.load_module("endpoint_send"), "deliver", lambda *a, **k: (False, "the channel said no"))
        patch.start()
        self.addCleanup(patch.stop)
        self.world.engine.run_pass()
        (line,) = self.audit_events("held.undelivered")
        self.assertEqual(line["subject"], "cloudy")
        self.assertIn("the channel said no", line["detail"]["reason"])
        self.assertEqual(self.held(), [])
        self.assertEqual(self.world.admissions("coder"), [])


    # ---- #1400: a send queued while the pane is held after a provider error, delivered once the hold ends, and the bounded resume

    def provider_error_scenario(self):
        sid = self.world.sessions["platform"]
        self.world.v2.sessions[sid]["outcome"] = "failed"   # the turn ended on a provider error; herdr shows the pane blocked
        self.status("platform", "blocked")
        code, _, err = self.cli("platform", "after", "the", "error")
        self.assertEqual(code, 0, "%s: status %s, %s" % (ANCHOR, code, err))
        self.assertEqual(len(self.held()), 1, ANCHOR)
        self.world.tick(30)
        self.world.engine.run_pass()
        self.assertEqual(self.world.admissions("platform"), [], "still on hold: nothing delivered")
        self.status("platform", "idle")
        self.world.engine.run_pass()
        (admitted,) = self.world.admissions("platform")
        self.assertIn("after the error", admitted["text"])
        return sid

    def test_b_a_queued_send_is_delivered_and_an_undelivered_wake_after_a_provider_error_gets_one_bounded_resume(self):
        sid = self.provider_error_scenario()
        v2 = self.world.v2
        self.world.v2.sessions[sid]["turn"] = None
        # the wake sits in the inbox, absent from the transcript: the #1337 recovery resumes the idle agent once
        # the resume rules out an open request first: this fake v2 has no such list, so it answers "none open" (nothing is open here)
        v2.op_session_permission_list = lambda params, body: []
        v2.op_session_form_list = lambda params, body: []
        wake_key = self.world.admissions("platform")[0]["keys"][0]
        v2.sessions[sid]["messages"] = [m for m in v2.sessions[sid]["messages"] if wake_key not in str(m)]
        self.assertFalse([m for m in v2.sessions[sid]["messages"] if wake_key in str(m)], "the wake is absent from the transcript")
        # The fake v2 consumes the admitted wake into the transcript, so the real flow cannot leave it in the inbox. The inbox
        # below IS BUILT BY HAND, always, from the real admission (no "if"): the undelivered state is the test's own setup.
        self.assertEqual(v2.sessions[sid]["inbox"], [], "the fake consumed the wake: the inbox is rebuilt by hand below")
        admitted = self.world.admissions("platform")[0]
        v2.sessions[sid]["inbox"].append({"id": admitted["id"], "sessionID": sid, "text": admitted["text"], "delivery": "queue", "resume": False,
                                          "metadata": {"fleet": {"from": "switchboard", "keys": admitted["keys"]}},
                                          "time": {"created": v2.clock.ms(admitted["at"])}})
        self.world.tick(16 * 60)
        self.world.engine.run_pass()
        resumes = [e for e in self.audit_events("undelivered.resume")]
        self.assertEqual(len(resumes), 1, "one bounded resume")
        self.world.tick(60)
        self.world.engine.run_pass()
        self.assertEqual(len(self.audit_events("undelivered.resume")), 1, "never a second resume in the incident")
        self.assertIn("platform", str(resumes[0]), "the resume's audit line names the recipient")

    # ---- #1400 (c): mutants "delivered twice" and "out of order" on the endpoint route; each control first

    def endpoint_two_sends(self, passes=2):
        self.endpoint_world()
        self.cli("cloudy", "first-one")
        self.world.tick(2)
        self.cli("cloudy", "second-one")
        self.set_state("idle")
        for _ in range(passes):
            self.world.engine.run_pass()
        order = [p[2].split(") ", 1)[1] for p in self.posts]
        self.assertEqual(len(self.posts), 2, "a held send is delivered once: delivered twice or not at all: %s" % order)
        self.assertEqual(order, ["first-one", "second-one"], "held sends are delivered in the order they were sent")

    def test_c_control_two_held_sends_are_delivered_once_each_in_order(self):
        self.endpoint_two_sends()

    def test_c_mutant_delivered_twice_is_caught_by_the_anchored_text(self):
        self.test_c_control_two_held_sends_are_delivered_once_each_in_order()
        old = "release_held_sends(engine.state_dir, recipient, [row[\"key\"]])"
        with self.mutant(sb, "held_sends_tick", old, "None"):
            with self.assertRaises(AssertionError) as caught:
                self.endpoint_two_sends()
        self.assertIn("a held send is delivered once", str(caught.exception))

    def test_c_mutant_out_of_order_is_caught_by_the_anchored_text(self):
        self.test_c_control_two_held_sends_are_delivered_once_each_in_order()
        old = "key=lambda pair: (pair[1][\"at\"], pair[1][\"key\"]))"
        with self.mutant(sb, "held_rows", old, old[:-1] + ", reverse=True)"):
            with self.assertRaises(AssertionError) as caught:
                self.endpoint_two_sends()
        self.assertIn("held sends are delivered in the order they were sent", str(caught.exception))
