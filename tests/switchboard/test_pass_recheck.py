"""#1542: a sweep fact, a triage digest and a plan notice are re-checked at delivery against their own pass's present state. World is set by bin/test-switchboard."""
import contextlib
import sys
import types
import unittest
from unittest import mock
from pathlib import Path
from support.core import Base
from support.loader import sb

from support.herdr_world import World

SWEEP_KEY = "sweep:0123456789:4"
TRIAGE_KEY = "triage:abcdef0123:4"
PLAN_KEY = "plan:acme/api#3:h1h1h1h1:platform"
PAIRS = [("S1", "acme/api#1"), ("S2", "acme/api#2")]


class TestPassRecheck(Base):
    """S2 to S8 of #1542, with the real fact sources answering for their own passes."""

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("platform", "coder"))
        self.sweep = types.SimpleNamespace(facts={})
        self.triage = types.SimpleNamespace(facts={})
        self.wake = types.SimpleNamespace(facts={})
        host = types.SimpleNamespace(sweep=self.sweep, triage=self.triage, plan_wake=self.wake)
        self.world.engine.hub = types.SimpleNamespace(present_record=lambda *a: {})
        for source in self.world.engine.sources:   # the world's own three fact sources, answering for the fake passes
            if getattr(source, "__name__", "") in ("sweep_facts", "triage_facts", "plan_wake_facts"):
                source.bind(host)

    def make(self, key, kind, age=60, summary="a pass fact"):
        self.world.fact("platform", key, summary, kind=kind, created=self.world.clock.now() - age, batch=False, issue=3 if kind == "plan" else None,
                        **({"repo": "acme/api"} if kind == "plan" else {}))

    def deliver(self):
        self.world.tick(1)
        engine = self.world.engine
        now = self.world.clock.now()
        plan = engine.plan("platform", [f for f in engine.collect_facts()[0] if f.kind in ("sweep", "triage", "plan")], engine.discover(now), now)
        self.assertIn(plan.action, ("wake", "note"))
        return engine.deliver(plan)

    def texts(self):
        return [a["text"] for a in self.world.admissions("platform")]

    def dropped(self):
        return [e for e in self.world.audit() if e["event"] == "stale.dropped"]

    def hold(self, state, key, present):
        state.fact_pairs, state.present = {key: PAIRS}, set(present)

    def test_s2_a_sweep_fact_whose_findings_are_all_gone_is_dropped_with_one_audit_line(self):
        self.hold(self.sweep, SWEEP_KEY, [("S1", "acme/api#9")])
        self.make(SWEEP_KEY, "sweep", summary="sweep: 2 findings")
        outcome = self.deliver()
        self.assertEqual(self.texts(), [], msg="a resolved sweep fact was delivered")
        self.assertEqual([(e["detail"]["kind"], e["detail"]["fact"]) for e in self.dropped()], [("sweep", SWEEP_KEY)], msg="not one audit line for the drop")
        self.assertFalse(outcome["sent"])

    def test_s3_one_finding_still_present_keeps_the_whole_fact(self):
        self.hold(self.sweep, SWEEP_KEY, [("S2", "acme/api#2")])
        self.make(SWEEP_KEY, "sweep", summary="sweep: 2 findings: acme/api#1 S1, acme/api#2 S2")
        self.deliver()
        self.assertEqual(len(self.texts()), 1, msg="a fact with a finding still present was not delivered")
        self.assertIn("sweep: 2 findings: acme/api#1 S1, acme/api#2 S2", self.texts()[0])
        self.assertEqual(self.dropped(), [])

    def test_s4_a_triage_digest_with_no_item_left_is_dropped(self):
        self.hold(self.triage, TRIAGE_KEY, [("T1", "acme/api#77")])
        self.make(TRIAGE_KEY, "triage", summary="triage: 2 findings")
        self.deliver()
        self.assertEqual(self.texts(), [], msg="a resolved triage digest was delivered")
        self.assertEqual(len(self.dropped()), 1)

    def test_s5_an_absent_state_keeps_the_fact_marked_unverified_and_writes_no_audit_line(self):
        self.make(SWEEP_KEY, "sweep", summary="sweep: 2 findings")   # the pass has not run: no `present`
        self.deliver()
        self.assertEqual(len(self.texts()), 1, msg="an absent state dropped the fact")
        self.assertIn("[unverified: its present state could not be read]", self.texts()[0])
        self.assertEqual(self.dropped(), [])

    def test_s6_a_superseded_plan_notice_is_dropped_and_the_current_one_is_delivered(self):
        self.wake.newest = {("acme/api", 3): "h2h2h2h2"}
        self.make(PLAN_KEY, "plan", summary="plan posted on #3: old")
        self.deliver()
        self.assertEqual(self.texts(), [], msg="a superseded plan notice was delivered")
        self.assertEqual(len(self.dropped()), 1)

    def test_s6_the_current_plan_notice_is_delivered(self):
        self.wake.newest = {("acme/api", 3): "h1h1h1h1"}
        self.make(PLAN_KEY, "plan", summary="plan posted on #3: current")
        self.deliver()
        self.assertEqual(len(self.texts()), 1, msg="the current plan notice was not delivered")

    def test_s7_a_plan_state_that_cannot_be_read_keeps_the_notice_marked(self):
        self.make(PLAN_KEY, "plan", summary="plan posted on #3: x")   # no plan seen by the pass yet
        self.deliver()
        self.assertEqual(len(self.texts()), 1, msg="an unreadable plan state dropped the notice")
        self.assertIn("[unverified", self.texts()[0])

    def test_s8_an_old_plan_notice_carries_the_one_age_mark_and_a_fresh_one_does_not(self):
        self.wake.newest = {("acme/api", 3): "h1h1h1h1"}
        self.make(PLAN_KEY, "plan", age=90 * 60, summary="plan posted on #3: old")
        self.deliver()
        self.assertIn("(delayed: sent", self.texts()[0].splitlines()[0], msg="a 90-minute plan notice has no age mark")

    def test_s8_a_plan_notice_made_nine_minutes_ago_is_unchanged(self):
        self.wake.newest = {("acme/api", 3): "h1h1h1h1"}
        self.make(PLAN_KEY, "plan", age=9 * 60, summary="plan posted on #3: fresh")
        self.deliver()
        self.assertNotIn("delayed: sent", self.texts()[0].splitlines()[0], msg="a fresh plan notice carries an age line")


class TestPassRecheckMutants(unittest.TestCase):
    """S9: three wrong builds, each caught by a test above (control first, the textual replacement asserted applied)."""

    def mutant(self, module, old, new):
        text = self.source(module)
        self.assertIn(old, text, msg="the mutation target is not in %s" % module)
        self.assertNotEqual(text.replace(old, new, 1), text, msg="the mutation was not applied")
        ns = dict(vars(sb))
        exec(compile(text.replace(old, new, 1), module + ".py", "exec"), ns)
        return sb, ns

    @staticmethod
    def source(module):
        return (Path(sb.__file__).resolve().parent / "switchboard" / (module + ".py")).read_text()

    def test_the_three_mutation_targets_exist_once(self):
        reminders = self.source("reminders")
        decision = self.source("decision")
        self.assertEqual(reminders.count("            if why:\n                dropped.append((fact, mine[0], why))"), 1, msg="the resolved-finding target moved")
        self.assertEqual(reminders.count("now != expected else"), 1, msg="the plan-hash target moved")
        self.assertEqual(decision.count('OWN_LATE_MARK_KINDS = ("reminder",)'), 1, msg="the age-scope target moved")

    def run_case(self, name, patches):
        suite = unittest.defaultTestLoader.loadTestsFromName(name, sys.modules[__name__])
        result = unittest.TestResult()
        with contextlib.ExitStack() as stack:
            for target, attr, value in patches:
                stack.enter_context(mock.patch.object(target, attr, value))
            suite.run(result)
        return result

    def test_a_resolved_finding_delivered_is_caught(self):
        mod, ns = self.mutant("reminders", "            if why:\n                dropped.append((fact, mine[0], why))", "            if False:\n                dropped.append((fact, mine[0], why))")
        self.assertTrue(self.run_case("TestPassRecheck.test_s2_a_sweep_fact_whose_findings_are_all_gone_is_dropped_with_one_audit_line", []).wasSuccessful(), msg="control failed")
        result = self.run_case("TestPassRecheck.test_s2_a_sweep_fact_whose_findings_are_all_gone_is_dropped_with_one_audit_line", [(sb, "judge_delivery", ns["judge_delivery"])])
        self.assertFalse(result.wasSuccessful(), msg="a resolved finding delivered went unnoticed")

    def test_a_superseded_plan_notice_delivered_is_caught(self):
        mod, ns = self.mutant("reminders", "now != expected else", "False else")
        name = "TestPassRecheck.test_s6_a_superseded_plan_notice_is_dropped_and_the_current_one_is_delivered"
        self.assertTrue(self.run_case(name, []).wasSuccessful(), msg="control failed")
        result = self.run_case(name, [(sb, "judge_delivery", ns["judge_delivery"]), (sb, "pass_stale", ns["pass_stale"])])
        self.assertFalse(result.wasSuccessful(), msg="a superseded plan notice delivered went unnoticed")

    def test_a_plan_notice_with_no_age_is_caught(self):
        mod, ns = self.mutant("decision", 'OWN_LATE_MARK_KINDS = ("reminder",)', 'OWN_LATE_MARK_KINDS = ("reminder", "plan")')
        name = "TestPassRecheck.test_s8_an_old_plan_notice_carries_the_one_age_mark_and_a_fresh_one_does_not"
        self.assertTrue(self.run_case(name, []).wasSuccessful(), msg="control failed")
        result = self.run_case(name, [(sb, "in_late_scope", ns["in_late_scope"])])
        self.assertFalse(result.wasSuccessful(), msg="a plan notice with no age went unnoticed")

