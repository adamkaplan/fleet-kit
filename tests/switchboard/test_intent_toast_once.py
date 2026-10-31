"""#1726: an edit of an ask's Intent is toasted once per edit. build() is called by bin/test-switchboard, which holds Base and GithubRig."""
import types
from unittest import mock
from support.loader import sb


def build(Base, GithubRig, GH_REPO):
    class TestIntentToastOnce(Base):
        OLD = "## Intent\nAdd /health\nDone when: returns 200\n"

        def setUp(self):
            super().setUp()
            self.rig = GithubRig(self, agents={"platform": {"role": "orchestrator", "charter": 1}, "cos": {"role": "chief-of-staff"}})
            self.gh, self.hub, self.world = self.rig.gh, self.rig.hub, self.rig.world
            for number, parent in ((1, None), (12, 1), (13, 1)):
                self.gh.issue(number, parent=parent) if parent else self.gh.issue(number)
            self.hub.tick()
            self.engine = self.world.engine
            self.engine.intents = types.SimpleNamespace(invalidate=lambda n, repo=None: None)
            self.n = 0

        def edit(self, number):
            self.n += 1
            self.world.tick(1)
            body = "## Intent\nAdd /health\nDone when: returns 200 and %d\n" % self.n
            issue = dict(self.gh.issues[number], body=body, updated_at=sb.iso(self.world.clock.now()))
            self.gh.send("issues", {"action": "edited", "issue": issue, "sender": {"login": "you"}, "changes": {"body": {"from": self.OLD}}})
            return "intent.edit:%s#%d:%s" % (GH_REPO, number, issue["updated_at"])

        def render(self, held=None, discovery=None):
            self.world.tick(5)
            now = self.world.clock.now()
            report = {"toasts": [], "errors": []}
            self.engine.render_toast(discovery or self.engine.discover(now), held or {}, now, report)
            return report

        def shown(self):
            return len(self.world.herdr.toasts)

        def two_pending(self, passes=10):
            """Two pending edits of two asks for the Chief of Staff; the first is acknowledged after the fifth pass."""
            first = self.edit(12)
            self.edit(13)
            reasons = []
            for i in range(passes):
                for t in self.render()["toasts"]:
                    reasons.append(t["reason"])
                if i == 4:
                    self.hub.acknowledge([first])
            return reasons

        def test_s2_two_pending_edits_are_toasted_once_each_over_ten_passes(self):
            reasons = self.two_pending()
            self.assertEqual(self.shown(), 2)
            self.assertEqual(len(set(reasons)), len(reasons), "a reason was toasted again")

        def test_s3_a_later_edit_of_the_same_ask_is_toasted(self):
            self.edit(12)
            self.render()
            self.render()
            self.assertEqual(self.shown(), 1)
            self.edit(12)
            for _ in range(3):
                self.render()
            self.assertEqual(self.shown(), 2)

        def test_s3_a_toast_refused_as_rate_limited_is_owed_and_shown_after_its_retry(self):
            self.edit(12)
            self.world.herdr.toast_replies = ["rate_limited"]
            self.render()
            self.assertEqual(self.shown(), 0)
            for _ in range(3):
                self.render()
            self.assertEqual(self.shown(), 1)

        def once(self, discovery=None, held=None):
            for _ in range(3):
                self.render(held=held, discovery=discovery)
            self.assertEqual(self.shown(), 1)

        def test_s3_a_pane_on_another_program_is_toasted_once(self):
            found = self.engine.discover(self.world.clock.now())
            found.agents["platform"].wrong_program, found.agents["platform"].problem = True, "another program"
            self.once(discovery=found)

        def test_s3_a_blocked_agent_is_toasted_once(self):
            self.world.herdr.panes[self.world.panes["platform"]]["status"] = "blocked"
            self.once()

        def test_s3_held_items_are_toasted_once(self):
            plan = types.SimpleNamespace(reason="not ready", pending=[types.SimpleNamespace(created=0)])
            self.once(held={"platform": plan})

        def test_s3_an_unreachable_agent_is_toasted_once(self):
            self.engine.pass_toasts["platform"] = ("unreachable: gone", "Fleet: platform cannot be reached", "gone")
            self.once()

        def test_s3_a_polled_repository_is_toasted_once(self):
            watch = self.hub.watches[GH_REPO]
            watch.polling, watch.poll_reason, watch.last_error = True, "no admin", ""
            self.once()

        def test_s4_the_one_reason_memory_toasts_a_reason_again_and_is_caught(self):
            self.two_pending()   # the unmutated control passes
            self.assertEqual(self.shown(), 2)
            self.fresh_rig()
            real = sb.Switchboard.render_toast

            def one_slot(engine, discovery, held, now, report):
                real(engine, discovery, held, now, report)
                last = {t["agent"]: t["reason"] for t in report["toasts"] if t["shown"]}
                engine.toasted_intent = {name: ({last[name]} if name in last else set()) for name in set(engine.toasted_intent) | set(last)}
            with mock.patch.object(sb.Switchboard, "render_toast", one_slot):
                with self.assertRaisesRegex(AssertionError, r"^8 != 2"):
                    self.test_s2_two_pending_edits_are_toasted_once_each_over_ten_passes()

        def fresh_rig(self):
            self.setUp()

    return TestIntentToastOnce
