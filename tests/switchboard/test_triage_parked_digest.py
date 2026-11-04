"""#2090: a pending triage digest fact is judged again before it is delivered: a validly parked item is never repeated, an invalid park is still flagged once.
The class borrows the setup of TestTriageDigest from the host file (bin/test-switchboard), passed to build() without re-running its tests."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def build(Base, Digest, sb):
    class TestTriageParkedDigest(Base):
        DAY = 86400
        COS, USER = Digest.COS, Digest.USER
        setUp = Digest.setUp
        pr_runner, add, bound, passes = Digest.pr_runner, Digest.add, Digest.bound, Digest.passes

        def mentions(self, ref):
            return sum(ref in f.summary for f in self.bound()() if f.kind == "triage")

        def reread(self, number, **kw):
            self.add(number, created=5, updated=0.1, **kw)
            self.engine.triage.units.clear()
            sb.triage_pass(self.engine, [], self.world.clock.now(), ())

        def future(self):
            return "Parked until: %s" % sb.iso(self.world.clock.now() + 30 * self.DAY)[:10]

        def test_s2_a_validly_parked_item_is_never_repeated(self):
            self.add(150, created=5, updated=4, labels=["osuser:parked"], body="no date yet")
            self.add(151, created=5, updated=4, labels=["kind"])
            self.passes(0)
            self.assertEqual(self.mentions("api#150"), 1)   # control: the made digest names it as an invalid park
            self.reread(150, labels=["osuser:parked"], body=self.future())
            mentions = 0
            for _ in range(2):
                self.world.tick(3600)
                mentions += self.mentions("api#150")
            self.assertEqual(mentions, 0, "a validly parked item is never repeated")
            self.assertTrue(any("api#151" in v[0] for v in self.engine.triage.facts.values()))

        def test_s2_a_pending_digest_of_only_parked_items_ends(self):
            self.add(150, created=5, updated=4, labels=["kind"])
            self.passes(0)
            self.reread(150, labels=["osuser:parked", "kind"], body=self.future())
            self.assertEqual([k for k in self.engine.triage.facts if "summary" not in k], [])
            self.assertEqual([f for f in self.bound()() if f.kind == "triage" and "summary" not in f.key], [])

        def test_s3_an_invalid_park_is_flagged_once(self):
            self.add(152, created=5, updated=4, labels=["osuser:parked"], body="no date yet")
            self.passes(2)
            audit = [e for e in self.world.audit() if e["event"] == "triage.parked_invalid"]
            self.assertEqual(len(audit), 1)

        def test_s3_an_expired_park_is_judged_as_an_unparked_item(self):
            self.add(153, created=5, updated=4, labels=["osuser:parked"], body="Parked until: 2020-01-01")
            self.passes(0)
            self.assertEqual(self.mentions("api#153"), 1)

        def records(self, ref):
            st = self.engine.triage
            keys = [k for k in (st.seen or {}) if k.endswith(ref)]
            return len(keys) + sum(p[1].endswith(ref) for p in st.alarmed) + sum(p[1].endswith(ref) for p in st.reasons)

        def test_s2_the_remembered_records_of_a_validly_parked_item_are_forgotten_in_the_same_pass(self):
            self.add(150, created=5, updated=4, labels=["osuser:parked"], body="no date yet")
            self.passes(0)
            self.assertGreater(self.records("api#150"), 0)   # control: the made digest remembered it (PARKED, T3, alarmed)
            self.reread(150, labels=["osuser:parked"], body=self.future())
            self.assertEqual(self.records("api#150"), 0)

        def test_s3_a_fact_of_only_an_invalid_park_has_a_basis_and_is_judged_again(self):
            self.add(154, created=5, updated=4, labels=["osuser:parked"], body="no date yet")
            self.passes(0)
            only = [k for k in self.engine.triage.facts if k.startswith("triage:parked:")]
            for k in only:
                self.assertIn(k, self.engine.triage.basis)
            self.reread(154, labels=["osuser:parked"], body=self.future())
            self.assertEqual([k for k in self.engine.triage.facts if k.startswith("triage:parked:")], [])

        def test_s3_a_held_fact_whose_park_was_valid_is_judged_again_when_it_expires_or_the_line_goes(self):
            for body in ("Parked until: 2020-01-01", "no date at all"):
                with self.subTest(body=body):
                    self.setUp()
                    self.add(155, created=5, updated=4, labels=["kind"])
                    self.passes(0)
                    self.reread(155, labels=["osuser:parked", "kind"], body=self.future())
                    self.assertEqual(self.mentions("api#155"), 0)
                    self.reread(155, labels=["osuser:parked", "kind"], body=body)
                    self.world.tick(3 * self.DAY)
                    self.passes(0)
                    self.assertGreaterEqual(self.mentions("api#155"), 1)

        def test_s4_the_rebuilt_text_keeps_the_kinds_of_the_items_not_shown(self):
            state = sb.TriageState()
            state.facts["k"] = ("old", 1.0)
            state.basis["k"] = ([("T3", "acme/api#1"), ("T3", "acme/api#2")], 3, [], 0, ["T3", "T5"])
            items = {"acme/api#2": {"labels": ["osuser:parked"], "parked_until": 1e12, "state": "open"}}
            with mock.patch.object(sb, "triage_digest_items", lambda s: items), mock.patch.object(sb, "triage_validly_parked", lambda i, n: i is not None):
                sb.triage_rejudge_facts(state, 1.0)
            self.assertIn("T5 = ", state.facts["k"][0])
            self.assertIn("(+1 more)", state.facts["k"][0])

        def test_s4_mutant_without_the_rejudge_call_the_s2_test_fails_on_its_anchored_text(self):
            repo = Path(__file__).resolve().parents[2]
            test = "TestTriageParkedDigest.test_s2_a_validly_parked_item_is_never_repeated"
            call = "    triage_rejudge_facts(state, now)   # a digest still waiting"
            with tempfile.TemporaryDirectory() as tmp:
                copy = Path(tmp) / "copy"
                shutil.copytree(repo, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))
                run = lambda: subprocess.run([sys.executable, "bin/test-switchboard", test], cwd=copy, capture_output=True, text=True, timeout=600)
                clean = run()
                self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)   # the unmutated copy passes
                path = copy / "bin" / "switchboard" / "triage.py"
                text = path.read_text()
                self.assertEqual(text.count(call), 1)
                path.write_text(text.replace(call, "    pass   # a digest still waiting"))
                mutant = run()
                self.assertNotEqual(mutant.returncode, 0)
                self.assertIn("a validly parked item is never repeated", mutant.stdout + mutant.stderr)

        def test_u1_the_moved_text_builder_is_byte_identical(self):
            state = sb.TriageState()
            state.reasons[("T3", "acme/api#1")] = "why"
            shown = [("T3", "acme/api#1"), ("T5", "acme/api#2")]
            kinds, total, other, invalid = ["T3", "T5"], 5, 2, ["acme/api#9"]
            old = "triage: %d finding%s: %s%s. %s. Resolve, route or escalate each; the triage changes nothing on GitHub." % (
                total, "" if total == 1 else "s", ", ".join("%s %s%s" % (ref, rule, " (%s)" % state.reasons[(rule, ref)] if (rule, ref) in state.reasons else "") for rule, ref in shown),
                " (+%d more)" % (total - len(shown)) if total > len(shown) else "",
                "; ".join("%s = %s" % (k, sb.TRIAGE_KINDS[k]) for k in kinds))
            old += " Also %d other-owner flag(s): not ours, nothing to do." % other
            old += " Parked without a valid `Parked until: YYYY-MM-DD` line (treated as not parked): %s." % ", ".join(invalid)
            self.assertEqual(sb.triage_digest_text(state, total, shown, kinds, other, invalid), old)

    return TestTriageParkedDigest
