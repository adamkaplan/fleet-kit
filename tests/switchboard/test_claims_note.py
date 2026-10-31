"""A pull request whose `CLAIMS:` line leaves scenarios of its plan out is told once per body (ask #1716).

The fixtures are those of bin/test-switchboard (HOST is that module, set by its loader block): the plan wake world, with a routing hub whose
`route_of` is the real one. Neutral names (acme/api). The mutants patch one function and must fail the very checks the real code passes.
"""
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bin"))

HOST = None   # set by bin/test-switchboard
FIRST = "test_a_work_item_plan_goes_to_the_orchestrator_once_and_an_edit_gives_one_more_with_the_new_hash"
PER_PR = ("S1", "S2", "S3", "U1")   # the per-pull-request scenarios of the plan in HOST.LEDGER_PLAN; S4 is the epic's
LINK = "Closes #3\nPLAN: acme/api#3\n"


class ClaimsNoteBase(unittest.TestCase):
    def setUp(self):
        if HOST is None:
            self.skipTest("the fixtures are those of bin/test-switchboard")
        self.sb = HOST.sb

    def world(self, plan=True):
        fx = HOST.TestPlanWake(FIRST)
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        fx.world.add_agent("coder", fleet={"role": "coder", "issue": 3, "repo": "acme/api"})   # the lane that owns the work item
        fx.engine.discover(fx.world.clock.now())
        hub, sb = fx.hub, self.sb
        hub.config, hub.watches, hub.state_dir, hub._notices = fx.engine.config, {}, fx.engine.state_dir, {}
        hub._load_notices = lambda: {}
        hub.route_of = types.MethodType(sb.GitHubHub.route_of, hub)   # the daemon's own resolver of a pull request's lane, unchanged
        hub._resolve = types.MethodType(sb.GitHubHub._resolve, hub)
        hub.pulls["acme/api"] = []
        if plan:
            fx.post(3, fx.HEAD + HOST.LEDGER_PLAN.split("Done when:", 1)[1].split("\n", 1)[1])
        return fx

    def pull(self, fx, number, body, merged=False):
        if merged:
            record = HOST.tri_issue(fx.world.clock.now(), number, created=0.01, body=body)
            record.update(merged_at=self.sb.iso(fx.world.clock.now() - 600), state="closed")
            fx.hub.pulls["acme/api"].append(record)
        else:
            fx.hub.open["acme/api"][number] = HOST.tri_issue(fx.world.clock.now(), number, created=0.01, body=body, pr=True)

    def edit(self, fx, number, body):
        fx.hub.open["acme/api"][number]["body"] = body

    def notices(self, fx):
        wake = getattr(fx.engine, "plan_wake", None)
        return sorted(text for text, _, name, _, _ in (wake.facts.values() if wake else []) if text.startswith("PR acme/api#"))

    def stamps(self, fx):
        return sorted(made for text, made, _, _, _ in fx.engine.plan_wake.facts.values() if text.startswith("PR acme/api#"))

    def skipped(self, fx):
        return [e["detail"]["why"] for e in fx.world.audit() if e["event"] == "claims_note.skipped"]

    @staticmethod
    def ids_of(text):
        return re.search(r"leaves out ([A-Z0-9, ]+?) of the plan", text).group(1).split(", ")


class TestClaimsNotePass(ClaimsNoteBase):
    def omission_check(self, fx):
        """S2: an omission is noticed once, naming the ids and not the epic's; an edit gives one more naming what is still left."""
        self.pull(fx, 7, LINK + "CLAIMS: S1 S2")
        fx.go()
        first = self.stamps(fx)
        fx.go(2)
        self.assertEqual(self.stamps(fx), first)   # the same fact, queued once: a pass that queued it again would stamp it again
        got = self.notices(fx)
        self.assertEqual([self.ids_of(t) for t in got], [["S3", "U1"]])
        self.assertNotIn("S4", got[0])
        self.edit(fx, 7, LINK + "CLAIMS: S1 S2 S3")
        fx.go(3)
        self.assertEqual(sorted(self.ids_of(t) for t in self.notices(fx)), [["S3", "U1"], ["U1"]])

    def test_s2_an_omission_is_noticed_once_and_an_edit_gives_one_more_naming_what_is_left(self):
        self.omission_check(self.world())

    def test_s2_the_notice_goes_to_the_lane_the_daemon_resolves_for_the_pull_request(self):
        fx = self.world()
        self.pull(fx, 7, LINK + "CLAIMS: S1")
        fx.go()
        self.assertEqual(len(fx.to("coder")), 1)
        self.assertEqual(fx.to("orch") + fx.to("cos"), [x for x in fx.to("orch") + fx.to("cos") if not x.startswith("PR acme/api#")])

    def test_s3_a_full_line_a_completed_line_no_claim_line_and_no_plan_link_are_not_noticed(self):
        cases = {
            "a full line": [(7, LINK + "CLAIMS: S1 S2 S3 U1")],
            "a line completed by another open pull request": [(7, LINK + "CLAIMS: S1 S2"), (8, LINK + "CLAIMS: S3 U1")],
            "a line completed by a merged pull request": [(7, LINK + "CLAIMS: S1 S2")],
            "no claim line": [(7, LINK)],
            "no plan link": [(7, "Closes #3\nCLAIMS: S1")],
        }
        for name, bodies in cases.items():
            with self.subTest(case=name):
                fx = self.world()
                for number, body in bodies:
                    self.pull(fx, number, body)
                if "merged" in name:
                    self.pull(fx, 9, LINK + "CLAIMS: S3 U1", merged=True)
                fx.go(2)
                self.assertEqual(self.notices(fx), [])

    def test_s3_a_pull_request_closed_without_a_merge_claims_nothing(self):
        fx = self.world()
        self.pull(fx, 7, LINK + "CLAIMS: S1 S2")
        self.pull(fx, 9, LINK + "CLAIMS: S3 U1", merged=True)
        fx.hub.pulls["acme/api"][0]["merged_at"] = None
        fx.go()
        self.assertEqual([self.ids_of(t) for t in self.notices(fx)], [["S3", "U1"]])

    def test_s4_three_passes_over_one_body_send_one_notice_and_write_nothing_else(self):
        fx = self.world()
        self.pull(fx, 7, LINK + "CLAIMS: S1 S2")
        before = len(self.sb.plan_ledger.read_ledger(fx.engine.state_dir)[0])
        fx.go(3)
        self.assertEqual(len(self.notices(fx)), 1)
        self.assertEqual(len(self.sb.plan_ledger.read_ledger(fx.engine.state_dir)[0]), before)
        self.assertEqual([c for c in fx.hub.calls if "/labels" in c], [])
        self.assertEqual(len([k for k in fx.engine.claims_note.announced]), 1)
        again = self.world()   # a restart reads the announced keys back: no second notice
        self.pull(again, 7, LINK + "CLAIMS: S1 S2")
        again.go()
        saved = (Path(again.engine.state_dir) / self.sb.CLAIMS_NOTE_FILE).read_text()
        again.engine.claims_note = None
        again.engine.plan_wake.facts.clear()
        again.go()
        self.assertEqual((self.notices(again), (Path(again.engine.state_dir) / self.sb.CLAIMS_NOTE_FILE).read_text()), ([], saved))

    def test_s7_the_pass_and_claim_union_note_name_the_same_ids_through_covered_ids(self):
        body, others = "PLAN: acme/api#3\nCLAIMS: S1 S2", [("open", "PLAN: acme/api#3\nCLAIMS: S2")]
        fx = self.world()
        self.pull(fx, 7, LINK + "CLAIMS: S1 S2")
        self.pull(fx, 8, LINK + "CLAIMS: S2")
        fx.go()
        ids = sorted(i for t in self.notices(fx) if t.startswith("PR acme/api#7:") for i in self.ids_of(t))
        gates = HOST.TestPrClaimLine("test_s3_the_gate_counts_exactly_the_claimed_scenarios_and_leaves_the_rest_to_the_epic")
        gates.setUp()
        self.addCleanup(gates.doCleanups)
        gates.body("S1 S2")
        listed = [dict(number=11, state="open", merged_at=None, body="PLAN: acme/app#7\nCLAIMS: S2")]
        route = gates.route
        gates.route = lambda argv, **kw: (HOST.subprocess.CompletedProcess(argv, 0, HOST.json.dumps({"total_count": 1, "items": [dict(o, pull_request={"merged_at": None}) for o in listed]}), "")
                                          if "search/issues" in " ".join(argv) else route(argv, **kw))
        self.assertEqual(sorted(gates.gates()[1]["unclaimed"]), ids)
        self.assertEqual(ids, ["S3", "U1"])
        source = (REPO / "bin" / "switchboard" / "claims_note.py").read_text()
        self.assertEqual(source.count("covered_ids("), 1)   # the one reading of the claimed ids in the new code (the rest is `omitted_claims`)
        self.assertEqual(len(re.findall(r"pr_scenarios\(|pr_claim\(|claim_text\(", source)), 1)   # no second private reading of the line
        self.assertEqual(self.sb.plan_ledger.omitted_claims(self.sb.plan_check.parse_scenarios(self.sb.plan_check.scenario_block(HOST.LEDGER_PLAN)), body, others), ["S3", "U1"])


class TestClaimsNoteSkips(ClaimsNoteBase):
    def hold(self, fx, drop=None):
        """The units held by a triage pass that has no notice pass, so the pass under test is the only one that runs."""
        with mock.patch.object(self.sb, "claims_note_pass", lambda *a: None):
            fx.go()
        if drop:
            fx.engine.triage.units.pop(drop)

    def run_pass(self, fx):
        reads, raised = len(fx.hub.calls), None
        try:
            self.sb.claims_note_pass(fx.engine, fx.engine.triage, ["acme/api"], fx.world.clock.now())
        except Exception as err:   # never raises
            raised = err
        self.assertIsNone(raised)
        return len(fx.hub.calls) - reads

    def test_u2_a_unit_not_held_a_plan_that_is_not_pass_and_a_refused_line_send_nothing_and_say_why(self):
        cases = []
        fx = self.world()
        self.pull(fx, 7, LINK + "CLAIMS: S1")
        self.hold(fx, drop=("acme/api", "comments"))
        cases.append((fx, "not held"))
        fx = self.world(plan=False)
        fx.post(3, fx.HEAD + "no block here")
        self.pull(fx, 7, LINK + "CLAIMS: S1")
        self.hold(fx)
        cases.append((fx, "does not read as pass"))
        fx = self.world()
        self.pull(fx, 7, LINK + "CLAIMS: S9")
        self.hold(fx)
        cases.append((fx, "refused"))
        for fx, why in cases:
            with self.subTest(why=why):
                made = self.run_pass(fx)
                self.assertEqual((made, self.notices(fx)), (0, []))
                lines = self.skipped(fx)
                self.assertEqual(len(lines), 1, lines)
                self.assertIn(why, lines[0])

    def test_u2_a_pass_that_meets_an_error_is_noted_and_does_not_raise(self):
        fx = self.world()
        self.pull(fx, 7, LINK + "CLAIMS: S1")
        self.hold(fx)
        with mock.patch.object(self.sb.load_plan_ledger(), "omitted_claims", side_effect=ZeroDivisionError("boom")):
            self.assertEqual(self.run_pass(fx), 0)
        self.assertIn("claims_note.error", [e["event"] for e in fx.world.audit()])

    def test_pc3_with_no_lane_the_notice_is_not_sent_and_the_audit_says_so(self):
        fx = self.world()
        fx.hub.route_of = lambda item, agents, now: (None, None)
        self.pull(fx, 7, LINK + "CLAIMS: S1")
        fx.go()
        self.assertEqual(self.notices(fx), [])
        self.assertTrue(any("no recipient" in why for why in self.skipped(fx)))


class TestClaimsNoteText(ClaimsNoteBase):
    def test_u3_the_text_names_the_pull_request_the_plan_the_ids_and_both_fixes_and_the_key_expires_after_14_days(self):
        text = self.sb.claims_note_text("acme/api", 7, "acme/api#3", ["S3", "U1"])
        for item in ("acme/api#7", "acme/api#3", "S3, U1", "add them to the `CLAIMS:` line, or another pull request of the ask claims them"):
            self.assertIn(item, text)
        self.assertIn("as far as the held units show", text)
        fx = self.world()
        self.pull(fx, 7, LINK + "CLAIMS: S1 S2")
        fx.go()
        key = next(iter(fx.engine.claims_note.announced))
        self.assertTrue(key.startswith("acme/api#7:"))
        fx.engine.claims_note.announced[key] = fx.world.clock.now() - 15 * 86400
        fx.go()
        self.assertNotIn(key, fx.engine.claims_note.announced)


class TestClaimsNoteMutants(ClaimsNoteBase):
    """S5: each mutant is applied (asserted), caught by the checks the real code passes, and the unmutated control passes the same checks."""

    def checks(self, fx):
        TestClaimsNotePass("test_s2_an_omission_is_noticed_once_and_an_edit_gives_one_more_naming_what_is_left").omission_check.__func__(self, fx)

    def test_the_control_passes_the_same_checks(self):
        self.checks(self.world())

    def test_a_mutant_that_lists_every_scenario_of_the_plan_whatever_the_line_says_fails(self):
        ledger, applied = self.sb.load_plan_ledger(), []
        def every(scenarios, this_body, others):
            applied.append(1)
            return [s["id"] for s in scenarios if ledger.claim_of(s) == "pr"]
        with mock.patch.object(ledger, "omitted_claims", every):
            with self.assertRaises(AssertionError):
                self.checks(self.world())
        self.assertTrue(applied)

    def test_a_mutant_that_sends_the_notice_on_every_pass_fails(self):
        real, applied = self.sb.claims_note_state, []
        class Forgetful(dict):
            def __contains__(self, key):
                applied.append(1)
                return False
        def mutant(engine):
            state = real(engine)
            if not isinstance(state.announced, Forgetful):
                state.announced = Forgetful(state.announced)
            return state
        with mock.patch.object(self.sb, "claims_note_state", mutant):
            with self.assertRaises(AssertionError):
                self.checks(self.world())
        self.assertTrue(applied)


class TestClaimsNoteDocumentation(unittest.TestCase):
    def test_s6_the_claim_section_says_the_notice_exists_informs_once_per_body_and_skips_no_line_or_no_link(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        sections = re.findall(r"(?m)^- \*\*A notice lists the scenarios a `CLAIMS:` line leaves out[^\n]*", text)
        self.assertEqual(len(sections), 1)
        statements = ("opened or its body is edited", "only informs and is sent once per body", "no `CLAIMS:` line or no plan link gets none")
        self.assertEqual([s for s in statements if s in sections[0]], list(statements))


class TestClaimsNoteScan(unittest.TestCase):
    def test_u6_no_test_of_this_file_measures_the_size_of_a_pull_request(self):
        text = Path(__file__).read_text()
        for pattern in (r"git\s+" + "diff", "orig" + r"in/integration", r"num" + "stat", r"\bwc\s+-l", r"len\(.*splitlines\(\)\)\s*[<>]=?\s*\d{2,}"):
            self.assertIsNone(re.search(pattern, text), pattern)


if __name__ == "__main__":
    unittest.main()
