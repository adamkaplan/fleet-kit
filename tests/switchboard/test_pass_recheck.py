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
from support.fake_hubs import FakeTriageHub, sweep_issue, tri_issue   # bin/test-switchboard also gives FakeSweepHub (it lives in a test module)
FakeSweepHub = None

SWEEP_KEY = "sweep:0123456789:4"
TRIAGE_KEY = "triage:abcdef0123:4"
PLAN_KEY = "plan:acme/api#3:h1h1h1h1:platform"
PAIRS = [("S1", "acme/api#1"), ("S2", "acme/api#2")]


class TestPassRecheck(Base):
    """S2 to S8 of #1542, with the real fact sources answering for their own passes."""

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("platform", "coder"))
        self.sweep = types.SimpleNamespace(facts={}, fact_pairs={})
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

    def test_s8_a_sweep_item_a_triage_item_and_a_plan_notice_made_ninety_minutes_ago_all_carry_the_one_mark(self):
        self.hold(self.sweep, SWEEP_KEY, [("S1", "acme/api#1")])
        self.hold(self.triage, TRIAGE_KEY, [("S1", "acme/api#1")])
        self.wake.newest = {("acme/api", 3): "h1h1h1h1"}
        for key, kind in ((SWEEP_KEY, "sweep"), (TRIAGE_KEY, "triage"), (PLAN_KEY, "plan")):
            self.make(key, kind, age=90 * 60, summary="a %s item" % kind)
            self.deliver()
        marked = [t for t in self.texts() if "delayed: sent" in t.splitlines()[0]]
        self.assertEqual(len(marked), 3, msg="not all three old items carry the mark: %r" % self.texts())

    def test_s8_the_age_threshold_is_defined_once_in_bin(self):
        import re
        defs = [str(f) for f in Path(sb.__file__).resolve().parent.rglob("*") if f.is_file() and f.suffix in ("", ".py")
                for m in [re.findall(r"^[A-Z_]*(?:DELAYED|LATE|AGE)[A-Z_]*_AFTER\s*=", f.read_text(errors="ignore"), re.M)] for _ in m]
        self.assertEqual(len(defs), 1, msg="age threshold definitions: %r" % defs)


class RealPasses(Base):
    """The REAL sweep and triage passes (their fake GitHub hubs and cases are given by bin/test-switchboard), the REAL queue functions and the REAL fact sources:
    nothing here puts a fact in by hand."""

    COS, USER = "osuser:awaiting-cos", "osuser:awaiting-user"

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=(), config={"github_repos": ["acme/api"]})
        self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
        self.hub = FakeSweepHub()
        self.hub.present_record = lambda *a: {}   # the delivery reader needs a hub with the present-state read; the pass facts never use it
        self.engine = self.world.engine
        self.engine.hub = self.hub
        self.engine.config["sweep"] = dict(sb.SWEEP_DEFAULTS, enabled=True, person_names=["Alex"])
        self.engine.config["awaiting_label"], self.engine.config["awaiting_cos_label"] = self.USER, self.COS
        self.engine.discover(self.world.clock.now())
        self.offered = []

    offered = None

    def sweep_pass(self, tick=1):
        self.world.tick(tick)
        now = self.world.clock.now()
        entries, errors = sb.decisions_from_notices(self.engine, now)
        sb.sweep_decisions(self.engine, list(entries), now, errors)

    def deliver(self):
        """The delivery step for the Chief of Staff, as production runs it (the real plan and the real deliver)."""
        self.world.tick(int(self.engine.batch_seconds) + 1)   # past the batching wait: the fact is due
        engine, now = self.engine, self.world.clock.now()
        facts = [f for f in engine.collect_facts()[0] if f.kind in ("sweep", "triage", "plan")]
        plan = engine.plan("cos", facts, engine.discover(now), now)
        self.offered.append([f.key for f in facts])
        return engine.deliver(plan) if plan is not None and plan.action in ("wake", "note") else None

    def texts(self):
        return [a["text"] for a in self.world.admissions("cos")]

    def dropped(self):
        return [e for e in self.world.audit() if e["event"] == "stale.dropped"]


class TestDropStaleOnce(RealPasses):
    """Sixth amendment (1): a dropped fact is forgotten by its source, so it is not offered again and a second audit line is not written."""

    def raise_sweep_fact(self):
        self.hub.open = {1: sweep_issue(1, "intro\n## Decision required\nPick one")}
        self.sweep_pass()
        self.assertEqual(len(self.engine.sweep.facts), 1, msg="the real sweep made no fact")

    def test_a_dropped_sweep_fact_is_not_offered_again_and_has_one_audit_line_over_two_deliveries(self):
        self.raise_sweep_fact()
        self.hub.open = {}
        self.engine.sweep.confirmed.clear()
        self.sweep_pass()   # the real pass again: the finding is gone
        self.deliver()
        self.deliver()
        self.assertEqual(self.texts(), [], msg="a resolved sweep fact was delivered")
        self.assertEqual([e["detail"]["kind"] for e in self.dropped()], ["sweep"], msg="not exactly one stale.dropped line over two deliveries")
        self.assertEqual([len(keys) for keys in self.offered], [1, 0], msg="the dropped fact was offered again")
        self.assertEqual(self.engine.sweep.facts, {})
        self.assertEqual(self.engine.sweep.fact_pairs, {})

    def test_a_dropped_plan_notice_is_forgotten_by_its_source(self):
        wake = self.engine.plan_wake = sb.PlanWakeState(self.world.state)
        key = "plan:acme/api#3:h1h1h1h1:cos"
        wake.facts[key] = ("plan posted on #3: old", self.world.clock.now() - 60, "cos", "acme/api", 3)
        wake.newest[("acme/api", 3)] = "h2h2h2h2"
        self.deliver()
        self.deliver()
        self.assertEqual(self.texts(), [])
        self.assertEqual([e["detail"]["kind"] for e in self.dropped()], ["plan"], msg="not exactly one stale.dropped line over two deliveries")
        self.assertEqual([len(keys) for keys in self.offered], [1, 0], msg="the dropped notice was offered again")
        self.assertEqual(wake.facts, {})

    def test_a_dropped_triage_digest_is_not_offered_again_and_has_one_audit_line_over_two_deliveries(self):
        engine, now = self.engine, self.world.clock.now()
        state = sb.triage_state(engine)
        sb.triage_queue_fact(state, [("T1", "acme/api#77")], now, dict(sb.TRIAGE_DEFAULTS))   # the real queue function makes the digest
        state.present = {("T1", "acme/api#9")}   # what the real pass holds after it: the listed item is gone
        self.assertEqual(len(state.facts), 1, msg="the real queue function made no digest")
        self.deliver()
        self.deliver()
        self.assertEqual(self.texts(), [], msg="a resolved triage digest was delivered")
        self.assertEqual([e["detail"]["kind"] for e in self.dropped()], ["triage"], msg="not exactly one stale.dropped line over two deliveries")
        self.assertEqual([len(keys) for keys in self.offered], [1, 0], msg="the dropped digest was offered again")


class TestUnreadUnitKeepsTheItem(RealPasses):
    """Sixth amendment (2): a pass that did not see everything it judges holds no present state, so the item is delivered marked unverified, never dropped as cleared."""

    UNVERIFIED = "[unverified: its present state could not be read]"

    def sweep_fact_then_a_pass_that_cannot_read(self, readable):
        self.hub.open = {1: sweep_issue(1, "intro\n## Decision required\nPick one")}
        self.sweep_pass()
        self.assertEqual(len(self.engine.sweep.facts), 1, msg="the real sweep made no fact")
        self.hub.open = {}   # the finding is gone ...
        self.engine.sweep.confirmed.clear()
        if not readable:   # ... and the next pass cannot read the repository to see it
            def down(*args, **kwargs):
                raise sb.GitHubUnavailable("HTTP 502: down")
            self.hub.read_pages = down
        self.sweep_pass(tick=sb.SWEEP_TTL + 1)

    def test_a_sweep_pass_that_could_not_read_a_repository_keeps_its_fact_marked_unverified(self):
        self.sweep_fact_then_a_pass_that_cannot_read(readable=False)
        self.assertTrue(self.engine.sweep.repos["acme/api"].get("error"), msg="the pass did not see the repository as unreadable")
        self.assertIsNone(self.engine.sweep.present)
        self.deliver()
        self.assertEqual(self.dropped(), [], msg="a fact was dropped on a pass that read nothing")
        self.assertEqual(len(self.texts()), 1)
        self.assertIn(self.UNVERIFIED, self.texts()[0])

    def test_a_sweep_pass_that_read_everything_still_drops_the_cleared_fact(self):   # the control of the case above
        self.sweep_fact_then_a_pass_that_cannot_read(readable=True)
        self.assertEqual(self.engine.sweep.present, set())
        self.deliver()
        self.assertEqual(self.texts(), [])
        self.assertEqual([e["detail"]["kind"] for e in self.dropped()], ["sweep"])


class TestUnreadUnitKeepsTheDigest(RealPasses):
    """The same for the triage: the REAL triage_pass, its REAL digest, a unit that cannot be read."""

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=(), config={"github_repos": ["acme/api"]})
        self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
        self.world.add_agent("orch", fleet={"role": "orchestrator", "charter": 1, "repo": "acme/api"})
        self.hub = FakeTriageHub()
        self.hub.present_record = lambda *a: {}
        self.engine = self.world.engine
        self.engine.hub = self.hub
        self.engine.config["awaiting_label"], self.engine.config["awaiting_cos_label"] = self.USER, self.COS
        self.engine.config["triage"] = dict(sb.TRIAGE_DEFAULTS, repos=["acme/api"], our_logins=["bot"], enabled=True)
        self.engine.discover(self.world.clock.now())
        self.hub.open["acme/api"] = {}
        self.add(1, created=30, updated=0.1, labels=["x:orchestrator", "kind"], assignees=["bot"], body="## Decision required\nx")
        self.add(2, created=20, updated=0.1, parent=1, labels=["kind"], body="Next: go")
        self.add(10, created=2, labels=["kind"])   # no assignee, no chain: T1

    def add(self, number, **kw):
        self.hub.open["acme/api"][number] = tri_issue(self.world.clock.now(), number, **kw)

    def go(self, passes=10, tick=1):
        for _ in range(passes):
            self.world.tick(tick)
            sb.decisions_from_notices(self.engine, self.world.clock.now())
            sb.triage_pass(self.engine, [], self.world.clock.now(), ())

    def make_digest(self):
        self.go()
        for key in [k for k in self.engine.triage.facts if "summary" in k]:
            del self.engine.triage.facts[key]   # the daily summary is another fact: not the one under test
        digests = list(self.engine.triage.facts)
        self.assertTrue(digests, msg="the real triage pass made no digest: %r" % sorted(self.engine.triage.facts))
        return digests

    def test_a_triage_pass_with_a_unit_it_could_not_read_keeps_the_digest_marked_unverified(self):
        self.make_digest()
        self.assertIsNotNone(self.engine.triage.present, msg="a whole pass holds no present state")
        del self.hub.open["acme/api"][10]   # the finding is resolved ...
        self.hub.fail = ["/issues/comments"]   # ... and the comments unit cannot be read any more
        self.go(passes=3, tick=sb.TRIAGE_TTL + 1)
        self.assertIsNone(self.engine.triage.present)
        self.deliver()
        self.assertEqual(self.dropped(), [], msg="a digest was dropped on a pass that did not read a unit")
        self.assertEqual(len(self.texts()), 1)
        self.assertIn(self.UNVERIFIED, self.texts()[0])

    UNVERIFIED = "[unverified: its present state could not be read]"

    def test_a_triage_pass_that_read_every_unit_drops_the_cleared_digest(self):   # the control of the case above
        self.make_digest()
        del self.hub.open["acme/api"][10]
        self.go(passes=3, tick=sb.TRIAGE_TTL + 1)
        self.assertEqual(self.engine.triage.present & {("T1", "acme/api#10")}, set())
        self.deliver()
        self.assertEqual(self.texts(), [])
        self.assertEqual([e["detail"]["kind"] for e in self.dropped()], ["triage"])


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

    def test_a_second_age_constant_for_plan_notices_is_caught(self):
        name = "TestPassRecheck.test_s8_the_age_threshold_is_defined_once_in_bin"
        self.assertTrue(self.run_case(name, []).wasSuccessful(), msg="control failed")
        real = Path.read_text
        def second(path, *a, **k):
            text = real(path, *a, **k)
            return text + "\nPLAN_DELAYED_AFTER = 1800\n" if path.name == "decision.py" else text
        result = self.run_case(name, [(Path, "read_text", second)])
        self.assertFalse(result.wasSuccessful(), msg="a second age constant went unnoticed")

    def test_a_sweep_pass_that_sets_present_anyway_is_caught(self):
        text = (Path(sb.__file__).resolve()).read_text()
        start = text.index("def sweep_decisions(")
        end = text.index("\n\n\ndef sweep_queue_fact")
        old = "if seen_all else None"
        self.assertEqual(text[start:end].count(old), 1, msg="the mutation target moved")
        mutated = text[start:end].replace(old, "if True else None")
        self.assertNotEqual(mutated, text[start:end], msg="the mutation was not applied")
        ns = dict(vars(sb))
        exec(compile(mutated, "sweep_decisions.py", "exec"), ns)
        name = "TestUnreadUnitKeepsTheItem.test_a_sweep_pass_that_could_not_read_a_repository_keeps_its_fact_marked_unverified"
        self.assertTrue(self.run_case(name, []).wasSuccessful(), msg="control failed")
        result = self.run_case(name, [(sb, "sweep_decisions", ns["sweep_decisions"])])
        self.assertFalse(result.wasSuccessful(), msg="a pass that sets present after an unread repository went unnoticed")

    def test_a_triage_pass_that_sets_present_anyway_is_caught(self):
        reminders = Path(sb.__file__).resolve().parent / "switchboard" / "triage.py"
        text = reminders.read_text()
        start = text.index("def triage_pass(")
        end = text.index("\n\n\ndef triage_queue_fact")
        old = "if seen_all else None"
        self.assertEqual(text[start:end].count(old), 1, msg="the mutation target moved")
        mutated = text[start:end].replace(old, "if True else None")
        self.assertNotEqual(mutated, text[start:end], msg="the mutation was not applied")
        ns = dict(vars(sb))
        exec(compile(mutated, "triage_pass.py", "exec"), ns)
        name = "TestUnreadUnitKeepsTheDigest.test_a_triage_pass_with_a_unit_it_could_not_read_keeps_the_digest_marked_unverified"
        self.assertTrue(self.run_case(name, []).wasSuccessful(), msg="control failed")
        result = self.run_case(name, [(sb, "triage_pass", ns["triage_pass"])])
        self.assertFalse(result.wasSuccessful(), msg="a pass that sets present after an unread unit went unnoticed")

    def test_a_source_that_does_not_forget_a_dropped_fact_is_caught(self):
        name = "TestDropStaleOnce.test_a_dropped_sweep_fact_is_not_offered_again_and_has_one_audit_line_over_two_deliveries"
        self.assertTrue(self.run_case(name, []).wasSuccessful(), msg="control failed")
        forget = lambda self, keys: None
        self.assertIsNot(sb.SweepFacts.drop_stale, forget)
        result = self.run_case(name, [(sb.SweepFacts, "drop_stale", forget)])
        self.assertFalse(result.wasSuccessful(), msg="a source that never forgets went unnoticed")
        name = "TestDropStaleOnce.test_a_dropped_plan_notice_is_forgotten_by_its_source"
        self.assertTrue(self.run_case(name, []).wasSuccessful(), msg="control failed")
        result = self.run_case(name, [(sb.PlanWakeFacts, "drop_stale", forget)])
        self.assertFalse(result.wasSuccessful(), msg="a plan source that never forgets went unnoticed")
