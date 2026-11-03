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
        for n in range(51):
            self.world.tick(1)
            self.assertEqual(self.cli("platform", "msg%d" % n)[0], 0)
        rows = self.held()
        self.assertEqual(len(rows), 50)
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

    def test_s5_unknown_and_doubly_claimed_recipients_are_refused_with_status_3(self):
        self.assertEqual(self.cli("nobody", "hi")[0], sb.EXIT_HELD)
        self.world.add_agent("platform")   # a second session claims the same name
        code, _, err = self.cli("platform", "hi")
        self.assertEqual(code, sb.EXIT_HELD, err)
        self.assertIn("not sent", err)
        self.assertEqual(self.held(), [])

    def test_s5_a_recipient_with_another_hold_reason_is_refused(self):
        self.status("platform", "weird")
        code, _, err = self.cli("platform", "hi")
        self.assertEqual(code, sb.EXIT_HELD, err)
        self.assertEqual(self.held(), [])

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
