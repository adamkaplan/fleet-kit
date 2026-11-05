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
from support.fake_hubs import FakeTriageHub, sweep_issue, tri_issue
from support.helper_fakes import FakeSweepHub

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
        state.fact_pairs, state.present = {key: (PAIRS, False) if state is self.sweep else PAIRS}, set(present)

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


class TestHiddenFindings(RealPasses):
    """Sixth amendment (3): a sweep fact that hides findings ("+N more") is never dropped by the re-check."""

    def two_findings_one_shown(self):
        self.engine.config["sweep"]["max_findings"] = 1   # the real queue function shows one and hides one ("+1 more")
        self.hub.open = {1: sweep_issue(1, "intro\n## Decision required\nPick one"), 2: sweep_issue(2, "intro\n## Decision required\nPick two")}
        self.sweep_pass()
        (summary, _), = self.engine.sweep.facts.values()
        self.assertIn("(+1 more)", summary, msg="the real sweep did not hide a finding")
        self.hub.open = {}   # both resolved, and the next pass read everything
        self.engine.sweep.confirmed.clear()
        self.sweep_pass()
        self.assertEqual(self.engine.sweep.present, set())
        return summary

    def test_a_sweep_fact_that_hides_findings_is_kept_whole_even_when_the_shown_one_is_gone(self):
        summary = self.two_findings_one_shown()
        self.deliver()
        self.assertEqual(self.dropped(), [], msg="a fact that hides findings was dropped")
        self.assertEqual(len(self.texts()), 1)
        self.assertIn(summary, self.texts()[0])
        self.assertNotIn("[unverified", self.texts()[0])

    def test_a_sweep_fact_that_hides_nothing_and_whose_findings_are_gone_is_dropped(self):   # the control
        self.hub.open = {1: sweep_issue(1, "intro\n## Decision required\nPick one")}
        self.sweep_pass()
        self.assertNotIn("more)", list(self.engine.sweep.facts.values())[0][0])
        self.hub.open = {}
        self.engine.sweep.confirmed.clear()
        self.sweep_pass()
        self.deliver()
        self.assertEqual(self.texts(), [])
        self.assertEqual(len(self.dropped()), 1)


class TestMissingCases(RealPasses):
    """Sixth amendment (5): the cases the reader found missing. Each is a rule of the re-check with a mutant in TestPassRecheckMutants."""

    UNVERIFIED = "[unverified: its present state could not be read]"

    def put_sweep(self, key):
        """A fact with a key the real queue function never makes, in the REAL sweep state."""
        state = sb.sweep_state(self.engine)
        state.facts[key] = ("sweep: odd", self.world.clock.now() - 60)
        state.present = set()

    def test_a_sweep_fact_whose_key_does_not_parse_is_delivered_as_made(self):
        self.put_sweep("sweep:not-a-digest")
        self.deliver()
        self.assertEqual(self.dropped(), [])
        self.assertEqual(len(self.texts()), 1)
        self.assertNotIn("[unverified", self.texts()[0], msg="an unparsed key was judged")

    def test_a_fact_of_a_kind_the_table_does_not_know_is_delivered_as_made(self):
        wake = self.engine.plan_wake = sb.PlanWakeState(self.world.state)
        wake.newest[("acme/api", 3)] = "h2h2h2h2"
        subject = types.SimpleNamespace(key="plan:acme/api#3:h1h1h1h1:cos", kind="other", summary="x")
        self.assertIsNone(sb.pass_subject(subject), msg="a kind that is not in the table was given a subject")
        facts = [sb.Fact("plan:acme/api#3:h1h1h1h1:cos", "cos", "other", "an odd fact", self.world.clock.now() - 60, sender="switchboard")]
        kept, dropped = sb.judge_delivery(facts, self.world.clock.now(), lambda subject: {"hash": "h2h2h2h2"}, lambda *a: None)
        self.assertEqual((kept, dropped), (facts, []))

    def test_a_sweep_fact_is_offered_to_the_chief_of_staff_only(self):
        self.world.add_agent("platform", fleet={})
        self.engine.discover(self.world.clock.now())
        self.hub.open = {1: sweep_issue(1, "intro\n## Decision required\nPick one")}
        self.sweep_pass()
        self.assertEqual({(f.recipient, f.kind) for f in self.engine.collect_facts()[0] if f.kind == "sweep"}, {("cos", "sweep")})

    def test_a_triage_digest_with_an_empty_state_after_a_restart_is_kept_marked_unverified(self):
        state = self.engine.triage = sb.TriageState()   # a restart: memory rebuilt, no pass has run
        state.facts["triage:abcdef0123:4"] = ("triage: 1 finding: acme/api#7 T1", self.world.clock.now() - 60)
        state.fact_pairs["triage:abcdef0123:4"] = [("T1", "acme/api#7")]
        self.assertIsNone(getattr(state, "present", None))
        self.deliver()
        self.assertEqual(self.dropped(), [], msg="an empty state after a restart dropped a digest")
        self.assertEqual(len(self.texts()), 1)
        self.assertIn(self.UNVERIFIED, self.texts()[0])

    def test_an_equal_hash_keeps_the_plan_notice_and_an_unknown_hash_is_no_evidence(self):
        subject = ("plan", "acme/api", 3, "h1h1h1h1")
        self.assertEqual(sb.pass_stale(subject, {"hash": "h1h1h1h1"}), "")
        self.assertEqual(sb.pass_stale(subject, {"hash": None}), "", msg="an unknown hash dropped a notice")
        self.assertEqual(sb.pass_stale(subject, {"hash": 7}), "", msg="an unknown hash dropped a notice")
        self.assertNotEqual(sb.pass_stale(subject, {"hash": "h2h2h2h2"}), "", msg="a different hash kept a notice")


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
        self.assertEqual(reminders.count("why = pass_stale(subject, record) if mine else"), 1, msg="the resolved-finding target moved")
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
        mod, ns = self.mutant("reminders", "why = pass_stale(subject, record) if mine else", "why = \"\" if mine else")
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
        text = (Path(sb.__file__).resolve().parent / "switchboard" / "sweep.py").read_text()
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

    def test_a_recheck_that_ignores_hidden_findings_is_caught(self):
        old = 'or record.get("hidden"):'
        reminders = (Path(sb.__file__).resolve().parent / "switchboard" / "reminders.py").read_text()
        self.assertEqual(reminders.count(old), 1, msg="the mutation target moved")
        self.assertNotEqual(reminders.replace(old, ":", 1), reminders, msg="the mutation was not applied")
        ns = dict(vars(sb))
        exec(compile(reminders.replace(old, ":", 1), "reminders.py", "exec"), ns)
        name = "TestHiddenFindings.test_a_sweep_fact_that_hides_findings_is_kept_whole_even_when_the_shown_one_is_gone"
        self.assertTrue(self.run_case(name, []).wasSuccessful(), msg="control failed")
        result = self.run_case(name, [(sb, "pass_stale", ns["pass_stale"])])
        self.assertFalse(result.wasSuccessful(), msg="a fact that hides findings dropped went unnoticed")

    def mutate_run(self, module_file, old, new, name, patches):
        text = (Path(sb.__file__).resolve().parent / module_file).read_text()
        self.assertEqual(text.count(old), 1, msg="the mutation target moved")
        self.assertNotEqual(text.replace(old, new, 1), text, msg="the mutation was not applied")
        ns = dict(vars(sb))
        exec(compile(text.replace(old, new, 1), module_file, "exec"), ns)
        self.assertTrue(self.run_case(name, []).wasSuccessful(), msg="control failed")
        return self.run_case(name, [(target, attr, ns[attr_name]) for target, attr, attr_name in patches])

    def test_judging_an_unparsed_sweep_key_is_caught(self):
        result = self.mutate_run("switchboard/reminders.py", 'if fact.kind == "sweep" and SWEEP_KEY_RE.match(fact.key):', 'if fact.kind == "sweep":',
                                 "TestMissingCases.test_a_sweep_fact_whose_key_does_not_parse_is_delivered_as_made", [(sb, "pass_subject", "pass_subject")])
        self.assertFalse(result.wasSuccessful(), msg="judging an unparsed key went unnoticed")

    def test_judging_a_kind_the_table_does_not_know_is_caught(self):
        result = self.mutate_run("switchboard/reminders.py", 'if fact.kind == "plan" else None', 'if True else None',
                                 "TestMissingCases.test_a_fact_of_a_kind_the_table_does_not_know_is_delivered_as_made", [(sb, "pass_subject", "pass_subject")])
        self.assertFalse(result.wasSuccessful(), msg="judging an unknown kind went unnoticed")

    def test_an_empty_triage_state_read_as_cleared_is_caught(self):
        text = (Path(sb.__file__).resolve().parent / "switchboard" / "alert_labels.py").read_text()
        old = 'present, pairs = getattr(state, "present", None), getattr(state, "fact_pairs", {}).get(subject[3])\n        return None if present is None or pairs is None else {"pairs": [list(p) for p in pairs], "present"'
        self.assertEqual(text.count(old), 1, msg="the mutation target moved")
        new = old.replace('getattr(state, "present", None), getattr', 'getattr(state, "present", None) or set(), getattr')
        self.assertNotEqual(new, old, msg="the mutation was not applied")
        ns = dict(vars(sb))
        exec(compile(text.replace(old, new, 1), "alert_labels.py", "exec"), ns)
        name = "TestMissingCases.test_a_triage_digest_with_an_empty_state_after_a_restart_is_kept_marked_unverified"
        self.assertTrue(self.run_case(name, []).wasSuccessful(), msg="control failed")
        result = self.run_case(name, [(sb.TriageFacts, "present_record", ns["TriageFacts"].present_record)])
        self.assertFalse(result.wasSuccessful(), msg="an empty state read as cleared went unnoticed")

    def test_an_unknown_hash_read_as_a_newer_plan_is_caught(self):
        result = self.mutate_run("switchboard/reminders.py", "isinstance(now, str) and now != expected", "now != expected",
                                 "TestMissingCases.test_an_equal_hash_keeps_the_plan_notice_and_an_unknown_hash_is_no_evidence", [(sb, "pass_stale", "pass_stale")])
        self.assertFalse(result.wasSuccessful(), msg="an unknown hash read as a newer plan went unnoticed")

    def test_offering_a_sweep_fact_to_every_agent_is_caught(self):
        text = (Path(sb.__file__).resolve().parent / "switchboard" / "sweep.py").read_text()
        start, end = text.index("class SweepFacts:"), len(text)
        old = '(o.fleet or {}).get("role") == "chief-of-staff")'
        body = text[start:end]
        self.assertEqual(body.count(old), 1, msg="the mutation target moved")
        mutated = body.replace(old, '(o.fleet or {}).get("role") != "nobody")')
        self.assertNotEqual(mutated, body, msg="the mutation was not applied")
        ns = dict(vars(sb))
        exec(compile(mutated, "sweep_facts.py", "exec"), ns)
        name = "TestMissingCases.test_a_sweep_fact_is_offered_to_the_chief_of_staff_only"
        self.assertTrue(self.run_case(name, []).wasSuccessful(), msg="control failed")
        result = self.run_case(name, [(sb.SweepFacts, "__call__", ns["SweepFacts"].__call__)])
        self.assertFalse(result.wasSuccessful(), msg="a sweep fact offered to every agent went unnoticed")

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


class TestTriageHiddenGuard(RealPasses):
    """Seventh amendment (1) and (2): the triage digest that hides items is kept; the expiry and the drop forget both maps. REAL triage_pass and queue functions."""

    def setUp(self):
        RealPasses.setUp(self)
        self.triage_world()

    def triage_world(self):
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
        self.add(10, created=2, labels=["kind"])

    add, go, make_digest = TestUnreadUnitKeepsTheDigest.add, TestUnreadUnitKeepsTheDigest.go, TestUnreadUnitKeepsTheDigest.make_digest

    def shown_and_hidden(self, cap):
        self.add(11, created=3, labels=["kind"])
        self.add(12, created=4, labels=["kind"])
        self.engine.config["triage"]["max_findings"] = cap
        digests = self.make_digest()
        shown = self.engine.triage.fact_pairs[digests[0]]
        return digests[0], shown

    def clear_shown(self, shown):
        for _rule, ref in shown:
            self.hub.open["acme/api"].pop(int(ref.rpartition("#")[2]), None)
        self.go(passes=3, tick=sb.TRIAGE_TTL + 1)
        self.assertIsNotNone(self.engine.triage.present, msg="the pass did not read every unit")

    def test_a_digest_that_hides_items_is_kept_when_every_shown_item_has_cleared(self):
        key, shown = self.shown_and_hidden(1)
        self.assertIn("(+", self.engine.triage.facts[key][0], msg="the real digest hid nothing")
        self.assertTrue(self.engine.triage.fact_hidden[key])
        self.clear_shown(shown)
        self.deliver()
        self.assertEqual(self.dropped(), [], msg="a digest that hides items was dropped")
        self.assertTrue(any("(+" in t for t in self.texts()), msg="the digest was not delivered")

    def test_a_digest_that_hides_nothing_is_dropped_when_every_item_has_cleared(self):   # the control
        key, shown = self.shown_and_hidden(20)
        self.assertFalse(self.engine.triage.fact_hidden[key])
        for number in (10, 11, 12):
            self.hub.open["acme/api"].pop(number, None)
        self.go(passes=3, tick=sb.TRIAGE_TTL + 1)
        self.deliver()
        self.assertIn("triage", [e["detail"]["kind"] for e in self.dropped()])

    def test_the_other_owner_note_marks_the_digest_hidden(self):
        state = sb.triage_state(self.engine)
        sb.triage_queue_fact(state, [("T1", "acme/api#5")], self.world.clock.now(), dict(sb.TRIAGE_DEFAULTS), other_owner=2)
        (key,) = [k for k in state.facts]
        self.assertTrue(state.fact_hidden[key])

    def test_the_expiry_and_the_drop_leave_no_entry_in_either_map(self):
        state, cfg, now = sb.triage_state(self.engine), dict(sb.TRIAGE_DEFAULTS), self.world.clock.now()
        sb.triage_queue_fact(state, [("T1", "acme/api#5")], now, cfg)
        (old,) = list(state.facts)
        sb.triage_queue_fact(state, [("T1", "acme/api#6")], now + cfg["realarm_hours"] * 7200 + 1, cfg)
        self.assertNotIn(old, state.facts)
        self.assertNotIn(old, state.fact_pairs)
        self.assertNotIn(old, state.fact_hidden)
        (new,) = list(state.facts)
        source = [s for s in self.engine.sources if getattr(s, "__name__", "") == "triage_facts"][0]
        source.drop_stale([new])
        self.assertEqual((state.facts, state.fact_pairs, state.fact_hidden), ({}, {}, {}))

    def test_the_sweep_expiry_forgets_the_pairs_of_the_fact_it_deletes(self):
        state, cfg, now = sb.sweep_state(self.engine), dict(sb.SWEEP_DEFAULTS), self.world.clock.now()
        sb.sweep_queue_fact(state, [("S10", "acme/api#5")], now, cfg)
        (old,) = list(state.facts)
        sb.sweep_queue_fact(state, [("S10", "acme/api#6")], now + cfg["realarm_hours"] * 7200 + 1, cfg)
        self.assertNotIn(old, state.facts)
        self.assertNotIn(old, state.fact_pairs)


class TestPlanNewestForgotten(RealPasses):
    """Seventh amendment (2): a plan comment that disappears clears `newest`. The REAL plan_wake_pass."""

    def test_a_vanished_plan_comment_clears_the_present_hash(self):
        wake = sb.plan_wake_state(self.engine)
        wake.newest[("acme/api", 3)] = "oldhash"
        wake.newest[("acme/other", 4)] = "keep"
        state = types.SimpleNamespace(units={("acme/api", "issues"): {"data": []}, ("acme/api", "comments"): {"data": []}})
        sb.plan_wake_pass(self.engine, state, ["acme/api"], self.world.clock.now())
        self.assertNotIn(("acme/api", 3), wake.newest)
        self.assertEqual(wake.newest.get(("acme/other", 4)), "keep")


class TestTriageHiddenMutants(unittest.TestCase):
    def test_a_recheck_that_ignores_the_digest_mark_is_caught(self):
        helper = TestPassRecheckMutants()
        text = (Path(sb.__file__).resolve().parent / "switchboard" / "alert_labels.py").read_text()
        old = '"hidden": bool(getattr(state, "fact_hidden", {}).get(subject[3]))'
        self.assertEqual(text.count(old), 1, msg="the mutation target moved")
        self.assertNotEqual(text.replace(old, '"hidden": False', 1), text, msg="the mutation was not applied")
        ns = dict(vars(sb))
        exec(compile(text.replace(old, '"hidden": False', 1), "alert_labels.py", "exec"), ns)
        name = "TestTriageHiddenGuard.test_a_digest_that_hides_items_is_kept_when_every_shown_item_has_cleared"
        self.assertTrue(helper.run_case(name, []).wasSuccessful(), msg="control failed")
        result = helper.run_case(name, [(sb.TriageFacts, "present_record", ns["TriageFacts"].present_record)])
        self.assertFalse(result.wasSuccessful(), msg="a digest that ignores the mark went unnoticed")


class TestPlanKeyHash(unittest.TestCase):
    """Eighth amendment (6). Real: `pass_subject` and the real `Fact` shape of a plan notice, the key built as the daemon builds it
    (`"plan:%s:%s" % (key, name)` with the hash `sha256:<hex>`). Fake: nothing."""

    HASH = "sha256:1246ede5201b" + "0" * 52

    def fact(self, key):
        return types.SimpleNamespace(kind="plan", key=key, recipient="cos")

    def test_a_plan_key_with_sha256_keeps_its_whole_hash(self):
        key = "plan:%s:%s" % ("acme/api#3:" + self.HASH, "orch")
        self.assertEqual(sb.PLAN_KEY_RE.match(key).group(3), self.HASH)
        self.assertEqual(sb.pass_subject(self.fact(key)), ("plan", "acme/api", 3, self.HASH))

    def test_the_old_stand_in_keys_still_match(self):
        self.assertEqual(sb.pass_subject(self.fact("plan:acme/api#3:h1h1h1h1:cos")), ("plan", "acme/api", 3, "h1h1h1h1"))

    def test_a_mutant_that_restores_the_old_pattern_is_caught(self):
        helper = TestPassRecheckMutants()
        name = "TestPlanKeyHash.test_a_plan_key_with_sha256_keeps_its_whole_hash"
        old = 'PLAN_KEY_RE = re.compile(r"^plan:([^#:\\s]+/[^#:\\s]+)#([0-9]+):(\\S+):[^:\\s]+$")'
        new = 'PLAN_KEY_RE = re.compile(r"^plan:([^#:\\s]+/[^#:\\s]+)#([0-9]+):([^:\\s]+):")'
        result = helper.mutate_run("switchboard/reminders.py", old, new, name, [(sb, "pass_subject", "pass_subject")])
        self.assertFalse(result.wasSuccessful(), msg="the old pattern went unnoticed")


class TestPlanWakeMutants(unittest.TestCase):
    """Seventh (3): the REAL plan_wake_pass with the line that records the present hash replaced by `pass` (the test is TestPlanWake's, wired by bin/test-switchboard)."""

    def test_a_plan_pass_that_does_not_record_the_present_hash_is_caught(self):
        name = "TestPlanWake.test_the_plan_pass_records_the_present_hash_so_a_replaced_plan_notice_is_dropped_and_the_new_one_kept"
        text = (Path(sb.__file__).resolve().parent / "switchboard" / "plan_notice.py").read_text()
        start = text.index("def plan_wake_pass(")
        end = text.index("\n\n\n", start)
        # the present hash is recorded twice: for the delivery re-check (`newest`, #1542) and for the pass's own drop (`current`, #1784); either alone keeps a replaced notice out, so the mutant removes both
        olds = ("wake.newest[(repo, number)] = plan_hash", "wake.current[(repo, number)] = plan_hash")
        mutated = text[start:end]
        for old in olds:
            self.assertEqual(mutated.count(old), 1, msg="the mutation target moved")
            mutated = mutated.replace(old, "pass", 1)
        ns = dict(vars(sb))
        exec(compile(mutated, "plan_wake_pass.py", "exec"), ns)
        helper = TestPassRecheckMutants()
        self.assertTrue(helper.run_case(name, []).wasSuccessful(), msg="control failed")
        self.assertFalse(helper.run_case(name, [(sb, "plan_wake_pass", ns["plan_wake_pass"])]).wasSuccessful(), msg="a pass that records no hash went unnoticed")


class TestSeenAllConditions(RealPasses):
    """Seventh amendment (4), as corrected by the Eighth (7). REAL: `sweep_decisions` (the whole sweep pass), the real queue function, the real fact source and the real
    delivery. FAKE: the GitHub hub; in the second case also `sweep_gate_reading` (a gate read that fails: the one thing a hub cannot say).
    The conditions of `seen_all` that these pin are `not defer` and `not state.s5_unchecked`. A third, `whole_list`, is covered by `defer` and has no test of its own:
    in `sweep_decisions`, `whole_list = notices_whole or "*" not in defer`, so `whole_list` is false only when the notice read is not whole AND "*" is in `defer`;
    `defer` is then non-empty and `not defer` already makes `seen_all` false. A mutant that replaces `whole_list` by `True` cannot change `seen_all`: it is equivalent."""

    UNVERIFIED = "[unverified: its present state could not be read]"

    def cleared_fact_then(self, errors=(), extra=(), gate=None):
        self.hub.open = {1: sweep_issue(1, "intro\n## Decision required\nPick one")}
        self.sweep_pass()
        self.assertEqual(len(self.engine.sweep.facts), 1, msg="the real sweep made no fact")
        self.hub.open = {600: sweep_issue(600, "MERGE: captain", pr=True)} if extra else {}   # the finding is gone
        self.engine.sweep.confirmed.clear()
        self.world.tick(sb.SWEEP_TTL + 1)
        now = self.world.clock.now()
        entries, found = sb.decisions_from_notices(self.engine, now)
        with contextlib.ExitStack() as stack:
            if gate:
                stack.enter_context(mock.patch.object(sb, "sweep_gate_reading", gate))
            sb.sweep_decisions(self.engine, list(entries) + list(extra), now, list(found) + list(errors))

    def test_a_pass_that_deferred_a_repository_keeps_its_fact_marked_unverified(self):
        self.cleared_fact_then(errors=[{"source": "github", "error": "acme/api: HTTP 502"}])
        self.assertTrue(self.engine.sweep.deferred_since, msg="the pass did not defer the repository")
        self.assertIsNone(self.engine.sweep.present)
        self.deliver()
        self.assertEqual(self.dropped(), [], msg="a fact was dropped on a pass that deferred a repository")
        self.assertEqual(len(self.texts()), 1)
        self.assertIn(self.UNVERIFIED, self.texts()[0])

    def test_a_pass_with_a_held_pull_request_unchecked_keeps_its_fact_marked_unverified(self):
        held = {"id": "pr:acme/api#600", "kind": "captain", "tier": "cos", "repo": "acme/api", "number": 600, "title": "t",
                "since": sb.iso(self.world.clock.now() - 10 * 3600)}
        self.cleared_fact_then(extra=[held], gate=lambda engine, repo, number, now: (None, None, None, "HTTP 502"))
        self.assertEqual(self.engine.sweep.s5_unchecked, 1, msg="the pass did not leave the held pull request unchecked")
        self.assertIsNone(self.engine.sweep.present)
        self.deliver()
        self.assertEqual(self.dropped(), [], msg="a fact was dropped on a pass that left a pull request unchecked")
        self.assertEqual(len(self.texts()), 1)
        self.assertIn(self.UNVERIFIED, self.texts()[0])

    def test_the_control_a_pass_with_neither_condition_drops_the_cleared_fact(self):
        self.cleared_fact_then()
        self.assertEqual(self.engine.sweep.present, set())
        self.deliver()
        self.assertEqual(self.texts(), [])
        self.assertEqual([e["detail"]["kind"] for e in self.dropped()], ["sweep"])


class TestSeenAllMutants(unittest.TestCase):
    """Each condition replaced by True in the real `sweep_decisions` (control first, mutation asserted applied, one mutant per condition)."""

    def caught(self, old, new, name):
        text = (Path(sb.__file__).resolve().parent / "switchboard" / "sweep.py").read_text()
        start = text.index("def sweep_decisions(")
        end = text.index("\n\n\ndef sweep_queue_fact")
        self.assertEqual(text[start:end].count(old), 1, msg="the mutation target moved")
        mutated = text[start:end].replace(old, new, 1)
        self.assertNotEqual(mutated, text[start:end], msg="the mutation was not applied")
        ns = dict(vars(sb))
        exec(compile(mutated, "sweep_decisions.py", "exec"), ns)
        helper = TestPassRecheckMutants()
        self.assertTrue(helper.run_case(name, []).wasSuccessful(), msg="control failed")
        return helper.run_case(name, [(sb, "sweep_decisions", ns["sweep_decisions"])])

    def test_a_seen_all_that_ignores_the_deferral_is_caught(self):
        result = self.caught("and not defer and", "and True and", "TestSeenAllConditions.test_a_pass_that_deferred_a_repository_keeps_its_fact_marked_unverified")
        self.assertFalse(result.wasSuccessful(), msg="a pass that deferred a repository and set present went unnoticed")

    def test_a_seen_all_that_ignores_an_unchecked_pull_request_is_caught(self):
        result = self.caught("and not state.s5_unchecked", "and True", "TestSeenAllConditions.test_a_pass_with_a_held_pull_request_unchecked_keeps_its_fact_marked_unverified")
        self.assertFalse(result.wasSuccessful(), msg="a pass that left a pull request unchecked and set present went unnoticed")


class TestSweepOnRealPass(RealPasses):
    """Seventh amendment (5), the sweep. REAL: sweep_decisions, the queue function, the fact source, deliver. FAKE: the GitHub hub and the clock. Beside the S3 and S8 stand-ins."""

    def two_findings(self):
        self.hub.open = {1: sweep_issue(1, "intro\n## Decision required\nPick one"), 2: sweep_issue(2, "intro\n## Decision required\nPick two")}
        self.sweep_pass()
        ((key, (text, _)),) = list(self.engine.sweep.facts.items())
        self.assertEqual(len(self.engine.sweep.fact_pairs[key][0]), 2, msg="the real fact does not list both findings")
        return text

    def test_a_fact_with_one_finding_still_present_is_delivered_whole(self):   # S3
        text = self.two_findings()
        del self.hub.open[1]
        self.engine.sweep.confirmed.clear()
        self.sweep_pass(tick=sb.SWEEP_TTL + 1)
        self.deliver()
        self.assertEqual(self.dropped(), [])
        self.assertEqual(len(self.texts()), 1)
        self.assertIn(text, self.texts()[0], msg="the fact was trimmed or not delivered whole")

    def test_a_fact_delivered_ninety_minutes_late_carries_the_age_mark(self):   # S8
        self.two_findings()
        self.world.tick(90 * 60)
        self.deliver()
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("delayed: sent", self.texts()[0])


class TestTriageOnRealPass(RealPasses):
    """Seventh amendment (5), the triage. REAL: triage_pass, its queue function, the fact source, deliver. FAKE: the GitHub hub and the clock."""

    def setUp(self):
        RealPasses.setUp(self)
        TestTriageHiddenGuard.triage_world(self)

    add, go, make_digest = TestUnreadUnitKeepsTheDigest.add, TestUnreadUnitKeepsTheDigest.go, TestUnreadUnitKeepsTheDigest.make_digest

    def test_a_digest_with_one_item_still_present_is_delivered_whole(self):   # S3 for the triage
        self.add(11, created=3, labels=["kind"])
        (key,) = self.make_digest()
        text = self.engine.triage.facts[key][0]
        self.assertGreater(len(self.engine.triage.fact_pairs[key]), 1, msg="the real digest lists one item only")
        del self.hub.open["acme/api"][10]
        self.go(passes=3, tick=sb.TRIAGE_TTL + 1)
        self.deliver()
        self.assertEqual(self.dropped(), [])
        self.assertIn(text, self.texts()[0])

    def test_a_digest_delivered_ninety_minutes_late_carries_the_age_mark(self):   # S8
        self.make_digest()
        self.world.tick(90 * 60)
        self.deliver()
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("delayed: sent", self.texts()[0])
