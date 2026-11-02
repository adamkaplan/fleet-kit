"""#1784: a held plan line for a replaced hash is dropped and counted; a delivered line is not repeated. The fake world is TestPlanWake's (HOST, set by
bin/test-switchboard); mutants are one textual replacement in a copy of the PlanWakeFacts source, never production."""
import copy
import re
import unittest
from pathlib import Path
from unittest import mock

HOST = None   # set by bin/test-switchboard
BIN = Path(__file__).resolve().parents[2] / "bin"


def build():
    sb = HOST.sb

    class TestPlanWakeDrops(HOST.Base):
        """The tests of the plan-wake drop and acknowledge steps."""

        HEAD, MARK = HOST.TestPlanWake.HEAD, HOST.TestPlanWake.MARK
        add, post, plan, go, to = (HOST.TestPlanWake.__dict__[n] for n in ("add", "post", "plan", "go", "to"))

        def setUp(self):   # the world of TestPlanWake.setUp
            HOST.Base.setUp(self)
            self.world = HOST.World(self, agents=(), config={"github_repos": ["acme/api"]})
            self.world.add_agent("cos", fleet={"role": "chief-of-staff"})
            self.world.add_agent("orch", fleet={"role": "orchestrator", "charter": 1, "repo": "acme/api"})
            self.hub = HOST.FakeTriageHub()
            self.engine = self.world.engine
            self.engine.hub = self.hub
            self.engine.config["triage"] = dict(sb.TRIAGE_DEFAULTS, repos=["acme/api"], our_logins=["bot"])
            self.engine.discover(self.world.clock.now())
            self.hub.open["acme/api"] = {}
            self.comments = self.hub.comments.setdefault("acme/api", [])
            self.next_id = 100
            self.add(1, labels=["x:orchestrator"])
            self.add(2, parent=1, body="## Intent\nx\nDone when: " + HOST.GOOD_DONE_WHEN)
            self.add(3, parent=2)

        lines = HOST.TestPlanWake.__dict__["lines"]

        def audit(self, event):
            return [e for e in self.world.audit() if e["event"] == event]

        def class_source(self):
            text = (BIN / "fleet-switchboard").read_text()
            start = text.index("class PlanWakeFacts:")
            return text[start:text.index("\n\n\n# ----", start)]

        def mutant(self, old, new):
            source = self.class_source()
            self.assertEqual(source.count(old), 1)
            changed = source.replace(old, new)
            self.assertNotEqual(changed, source)   # the mutation is applied
            space = dict(sb.__dict__)
            exec(changed, space)
            return space["PlanWakeFacts"]

        def edits(self, count=3):
            record = self.post(3, self.plan())
            self.go()
            for n in range(count - 1):
                record["body"] += "\nedit %d" % n
                record["updated_at"] = sb.iso(self.world.clock.now())
                self.go()
            return record

        def snapshot(self):
            wake = self.engine.plan_wake
            return {k: copy.deepcopy(getattr(wake, k)) for k in ("announced", "facts", "current", "delivered")}

        def fresh(self, snap, cls):
            wake = sb.PlanWakeState(self.engine.state_dir)
            for k, v in snap.items():
                setattr(wake, k, copy.deepcopy(v))
            self.engine.plan_wake = wake
            source = cls()
            source.bind(self.engine)
            self.engine.sources = [source]
            return wake, source

        def offer(self, inbox, name="orch"):
            """One engine delivery: the real collection and dedupe, a transcript that cannot see the keys, then the engine's acknowledgement."""
            facts = [f for f in self.engine.collect_facts()[0] if f.recipient == name]
            with mock.patch.object(self.engine, "delivered_keys", return_value=(set(), True)):
                sent = self.engine._undelivered(name, facts, self.engine.discovery) if facts else []
            inbox.extend(f.summary for f in sent)
            self.engine._acknowledge([f.key for f in sent])
            return sent

        def s2(self, snap, cls):
            wake, _ = self.fresh(snap, cls)
            before = len(self.audit("plan_wake.dropped"))
            inbox = []
            self.offer(inbox)
            self.assertEqual(len(inbox), 1, msg="a held line for a replaced hash is delivered")
            self.assertEqual(wake.dropped, 2, msg="a replaced hash is not counted as dropped")
            return len(self.audit("plan_wake.dropped")) - before, inbox

        def test_s2_s7_three_edits_deliver_the_current_hash_and_drop_two_each_with_an_audit_line(self):
            record = self.edits()
            snap = self.snapshot()
            self.assertEqual(len(snap["facts"]) // 1, 3 * 1)
            audited, inbox = self.s2(snap, sb.PlanWakeFacts)
            self.assertIn(sb.plan_check.plan_hash(record["body"]), inbox[0])
            self.assertEqual(audited, 2)
            events = self.audit("plan_wake.dropped")
            self.assertEqual(len(events), 2)
            for e in events:
                self.assertEqual(sorted(e["detail"]), ["current", "dropped"])
                self.assertEqual(e["detail"]["current"], sb.plan_check.plan_hash(record["body"]))
                self.assertNotEqual(e["detail"]["dropped"], e["detail"]["current"])
            self.go()
            self.assertIn("plan lines: dropped 2 (a replaced hash), repeats not delivered 0", "\n".join(sb.triage_status_lines(sb.triage_summary(self.world.state))))

        def test_s8_two_mutants_fail_with_the_text_of_their_check_and_the_control_passes_first(self):
            self.edits()
            snap = self.snapshot()
            self.s2(snap, sb.PlanWakeFacts)   # the unmutated control first
            offers_all = self.mutant("if current is not None and now_key != own", "if False and now_key != own")
            with self.assertRaisesRegex(AssertionError, r"^3 != 1 : a held line for a replaced hash is delivered"):
                self.s2(snap, offers_all)
            wake, _ = self.fresh(snap, sb.PlanWakeFacts)
            control = []
            self.offer(control)
            self.offer(control)
            self.assertEqual(len(control), 1, msg="a delivered line is offered again")   # the control: delivered once, never again
            no_ack = self.mutant("def acknowledge(self, keys):", "def acknowledge_off(self, keys):")
            wake, _ = self.fresh(snap, no_ack)
            inbox = []
            self.offer(inbox)
            self.offer(inbox)
            with self.assertRaisesRegex(AssertionError, r"^2 != 1 : a delivered line is offered again"):
                self.assertEqual(len(inbox), 1, msg="a delivered line is offered again")

        def test_s3_s4_a_delivered_key_offered_again_is_not_delivered_and_is_counted(self):
            self.post(3, self.plan())
            self.go()
            snap = self.snapshot()
            wake, _ = self.fresh(snap, sb.PlanWakeFacts)
            inbox = []
            self.offer(inbox)
            self.offer(inbox)   # a second pass over the held lines: delivered exactly once
            self.assertEqual(len(inbox), 1, msg="a delivered line is offered again")
            held = dict(snap["facts"])
            for _ in range(3):
                wake.facts.update(held)   # the same key offered again
                self.offer(inbox)
            self.assertEqual(len(inbox), 1, msg="a repeat of a delivered key is delivered")
            self.assertEqual(wake.repeats, 3)
            self.go()
            self.assertIn("repeats not delivered 3", "\n".join(sb.triage_status_lines(sb.triage_summary(self.world.state))))

        def test_s5_a_refused_delivery_stays_owed_and_is_never_dropped(self):
            self.post(3, self.plan())
            self.go()
            wake, _ = self.fresh(self.snapshot(), sb.PlanWakeFacts)
            facts = self.engine.collect_facts()[0]
            self.assertEqual(len(facts), 1)          # offered, and the delivery raised before the acknowledgement: nothing is acknowledged
            inbox = []
            self.offer(inbox)
            self.assertEqual((len(inbox), wake.dropped), (1, 0))

        def test_s6_a_plan_replaced_after_delivery_recalls_nothing(self):
            record = self.post(3, self.plan())
            self.go()
            wake, _ = self.fresh(self.snapshot(), sb.PlanWakeFacts)
            inbox = []
            self.offer(inbox)
            first = sb.plan_check.plan_hash(record["body"])
            record["body"] += "\nreplaced"
            record["updated_at"] = sb.iso(self.world.clock.now())
            self.go()
            self.offer(inbox)
            self.assertEqual(len(inbox), 2, msg="a plan replaced after delivery recalls nothing: the first line stays delivered and the new one is offered")
            self.assertEqual(self.engine.plan_wake.dropped, 0, msg="a plan replaced after delivery recalls nothing: no drop is counted")
            self.assertEqual([e for e in self.audit("plan_wake.dropped") if e["detail"]["dropped"] == first], [], msg="a plan replaced after delivery recalls nothing: no plan_wake.dropped line names the delivered hash")

        def test_s9_an_unknown_current_hash_keeps_the_line(self):
            self.post(3, self.plan())
            self.go()
            snap = self.snapshot()
            snap["current"] = {}
            wake, _ = self.fresh(snap, sb.PlanWakeFacts)
            inbox = []
            self.offer(inbox)
            self.assertEqual((len(inbox), wake.dropped), (1, 0))

        def s12(self, snap, cls, older):
            """older: the recorded current hash was announced BEFORE the held line's own; else AFTER."""
            snap = copy.deepcopy(snap)
            keys = sorted(snap["announced"], key=snap["announced"].get)
            held = {k: v for k, v in snap["facts"].items() if k.startswith("plan:" + keys[0] + ":") or k.startswith("plan:" + keys[-1] + ":")}
            snap["facts"] = held
            repo, number = "acme/api", 3
            snap["current"] = {(repo, number): keys[0 if older else -1].split(":", 1)[1]}
            snap["facts"] = {k: v for k, v in snap["facts"].items() if k.startswith("plan:" + keys[-1 if older else 0] + ":")}
            wake, _ = self.fresh(snap, cls)
            before = len(self.audit("plan_wake.dropped"))
            inbox = []
            self.offer(inbox)
            audited = len(self.audit("plan_wake.dropped")) - before
            if older:
                self.assertEqual((len(inbox), wake.dropped), (1, 0), msg="a line is dropped for a hash that is not newer")
            else:
                self.assertEqual((len(inbox), wake.dropped, audited), (0, 1, 1), msg="S12 a newer-announced hash drops the held line: one dropped, one audit line, none delivered")

        def test_s12_a_drop_needs_a_current_hash_announced_after_the_line_and_a_differs_only_mutant_fails(self):
            self.edits(2)
            snap = self.snapshot()
            self.s12(snap, sb.PlanWakeFacts, True)
            self.s12(snap, sb.PlanWakeFacts, False)
            mutant = self.mutant(" and own in wake.announced and now_key in wake.announced and wake.announced[now_key] > wake.announced[own]", "")
            with self.assertRaisesRegex(AssertionError, r"^Tuples differ: \(0, 1\) != \(1, 0\)(.|\n)*: a line is dropped for a hash that is not newer"):
                self.s12(snap, mutant, True)

        def test_u1_a_current_unapproved_plan_is_announced_with_no_github_read(self):
            reads = []
            self.hub._read_one = lambda *a, **k: reads.append(a)
            self.post(3, self.plan())
            self.go()
            self.assertEqual(len(self.to("orch")), 1)
            seen = len(reads)
            inner, during = sb.plan_wake_pass, []

            def counted(*a, **k):
                before = len(reads)
                inner(*a, **k)
                during.append(len(reads) - before)

            with mock.patch.object(sb, "plan_wake_pass", counted):
                self.go(2)   # the plan is announced already: the pass (record, drop, acknowledge) reads nothing
            self.assertEqual(sum(during), 0)
            self.assertTrue(during)

    return TestPlanWakeDrops
