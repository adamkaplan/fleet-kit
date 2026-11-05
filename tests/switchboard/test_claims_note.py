"""A pull request whose `CLAIMS:` line leaves scenarios of its plan out is told once per body (ask #1716).

The fixtures are those of bin/test-switchboard (HOST is that module, set by its loader block): the plan wake world, with a routing hub whose
`route_of` is the real one. Neutral names (acme/api). The mutants patch one function and must fail the very checks the real code passes.
"""
import contextlib
import json
import random
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
        self.assertEqual(self.sb.plan_ledger.omitted_claims(self.sb.plan_check.parse_scenarios(self.sb.plan_check.scenario_block(HOST.LEDGER_PLAN)), body, self.sb.plan_ledger.claim_counts(self.sb.plan_check.parse_scenarios(self.sb.plan_check.scenario_block(HOST.LEDGER_PLAN)), [body] + [o for _, o in others])), ["S3", "U1"])


class NoGithubReadChecks:
    """(mixed into TestClaimsNoteSkips, whose loader line is the one registered) PC-2 on the REAL path: a real GitHubHub routes the notice and its GitHub reads fail the test on any call."""

    def real_hub(self, fx, calls):
        sb = self.sb
        hub = sb.GitHubHub(fx.engine.config, fx.world.state, sb.AuditLog(fx.world.audit_path), lambda *a, **k: None, None, fx.world.clock.now)
        def refuse(name):
            def call(*a, **k):
                calls.append((name, a, sorted(k)))
                raise AssertionError("GitHub read %s%r" % (name, a))
            return call
        hub._read_one, hub.gh_api = refuse("_read_one"), refuse("gh_api")
        hub.config, hub.watches, hub.state_dir, hub._notices = fx.engine.config, {}, fx.engine.state_dir, {}
        hub._load_notices = lambda: {}
        return hub

    def checks(self, fx):
        with mock.patch.object(self.sb, "claims_note_pass", lambda *a: None):
            fx.go()
        calls = self.calls = []
        fx.engine.hub = self.real_hub(fx, calls)
        real_plan = self.sb.claims_note_plan
        def plan(engine, state, repos, prepo, pnumber, cache):   # the plan of the other repository is held and reads as pass
            return real_plan(engine, state, repos, "acme/api" if prepo == "acme/other" else prepo, pnumber, cache)
        with mock.patch.object(self.sb, "claims_note_plan", plan):
            self.sb.claims_note_pass(fx.engine, fx.engine.triage, ["acme/api"], fx.world.clock.now())
        self.assertIsInstance(fx.engine.hub, self.sb.GitHubHub)
        self.assertEqual([t.split(":")[0] for t in self.notices(fx)], ["PR acme/api#%d" % n for n in (10, 11, 7, 9)], "the notice routed and sent")   # closing keyword, Refs, Part of, the PLAN link alone
        lines = [w for w in self.skipped(fx) if w.startswith("no recipient")]
        self.assertEqual(len(lines), 1, "the one whose plan issue is in another repository has no lane to reach (the audit says so)")
        self.assertIn("plan issue in another repository: not used for routing", lines[0])
        self.assertEqual(calls, [], "GitHub calls on the routing path")

    def fixture(self):
        fx = self.world()
        self.pull(fx, 7, LINK + "CLAIMS: S1")             # names a closing issue
        self.pull(fx, 8, "PLAN: acme/other#3\nCLAIMS: S1")   # its plan issue is in another repository
        self.pull(fx, 9, "Refs #3\nPLAN: acme/api#3\nCLAIMS: S1")      # no closing keyword
        self.pull(fx, 10, "Part of #3\nPLAN: acme/api#3\nCLAIMS: S1")
        self.pull(fx, 11, "PLAN: acme/api#3\nCLAIMS: S1")            # only the plan link
        return fx

    def test_the_real_route_of_reads_no_github_for_the_pull_requests_the_pass_builds(self):
        self.checks(self.fixture())

    def test_a_mutant_that_adds_a_lookup_the_hub_must_resolve_is_caught_and_the_control_passes(self):
        real, applied = self.sb.GitHubItem, []
        def mutant(*args):
            item = real(*args)
            item.lookups = [("pr", args[8][0])]   # the hub must now read the pull request
            applied.append(1)
            return item
        with mock.patch.object(self.sb, "GitHubItem", mutant):
            with self.assertRaisesRegex(AssertionError, r"the notice routed and sent"):
                self.checks(self.fixture())
        self.assertTrue(applied)
        self.assertEqual(self.calls[0][0], "_read_one")   # the mutant made the hub read GitHub
        self.checks(self.fixture())   # the control


class TestClaimsNoteSkips(NoGithubReadChecks, ClaimsNoteBase):
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

    def unavailable_checks(self, fx):
        for number in (7, 8, 9):
            self.pull(fx, number, LINK + "CLAIMS: S1")
        self.hold(fx)
        real = fx.hub.route_of
        def route(item, agents, now):
            if item.numbers == [8]:
                raise self.sb.GitHubUnavailable("read of issues/3 failed recently\nretry later")
            return real(item, agents, now)
        fx.hub.route_of = route
        self.run_pass(fx)
        self.assertEqual(sorted(t.split(":")[0] for t in self.notices(fx)), ["PR acme/api#7", "PR acme/api#9"], "the first and third announced")
        saved = json.loads((Path(fx.engine.state_dir) / self.sb.CLAIMS_NOTE_FILE).read_text())["announced"]
        self.assertEqual(sorted(k.split(":")[0] for k in saved), ["acme/api#7", "acme/api#9"], "the state file holds both keys")
        self.run_pass(fx)
        lines = self.skipped(fx)
        self.assertEqual(len(lines), 1, "one audit line names the second")
        self.assertTrue(lines[0].startswith("GitHub unavailable: read of issues/3"), lines)

    def test_one_failed_read_skips_that_pull_request_and_not_the_state_write(self):
        self.unavailable_checks(self.world())

    def test_a_mutant_that_lets_the_exception_escape_fails_and_the_control_passes(self):
        src = (REPO / "bin" / "switchboard" / "claims_note.py").read_text()
        self.assertIn("except GitHubUnavailable as err:", src)
        start = src.index("def claims_note_pass(")
        body = src[start:src.index("\n\n\ndef ", start)]
        mutated = body.replace("except GitHubUnavailable as err:", "except KeyboardInterrupt as err:")
        self.assertNotEqual(mutated, body)   # applied
        original = self.sb.claims_note_pass
        namespace = original.__globals__
        exec(mutated, namespace)   # the pass again, with the per-pull-request catch gone; rebinds the name in the part's namespace
        mutant = namespace["claims_note_pass"]
        applied = []
        try:
            with mock.patch.object(self.sb, "claims_note_pass", mutant):
                applied.append(1)
                with self.assertRaisesRegex(AssertionError, r"the first and third announced"):
                    self.unavailable_checks(self.world())
        finally:
            namespace["claims_note_pass"] = original
            self.sb.claims_note_pass = original
        self.assertTrue(applied)
        self.unavailable_checks(self.world())   # the control

    def test_the_notices_the_hub_holds_are_kept_and_a_hub_with_none_gets_the_files(self):
        held, filed = {("acme/api", 5): {"from": "coder"}}, {("acme/api", 6): {"from": "coder"}}
        for start, expect in ((held, held), ({}, filed)):
            fx = self.world()
            self.pull(fx, 7, LINK + "CLAIMS: S1")
            self.hold(fx)
            fx.hub._notices, fx.hub._load_notices = dict(start), lambda: dict(filed)
            self.run_pass(fx)
            self.assertEqual(fx.hub._notices, expect)

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


class ScaleChecks:
    """(mixed into TestClaimsNoteMutants, whose loader line is the one registered) S8, S9, S10 of #1716: the pass counts the claims of a plan once; the readings are counted in the namespace the code calls."""

    def held(self, fx, n):
        for i in range(n):
            self.pull(fx, 100 + i, LINK + "CLAIMS: S1")   # every body omits S2, S3 and U1
        with mock.patch.object(self.sb, "claims_note_pass", lambda *a: None):
            fx.go()

    def one_pass(self, fx):
        self.sb.claims_note_pass(fx.engine, fx.engine.triage, ["acme/api"], fx.world.clock.now())

    def sent(self, fx):
        return len([e for e in fx.world.audit() if e["event"] == "claims_note.sent"])

    def errors(self, fx):
        return len([e for e in fx.world.audit() if e["event"] == "claims_note.error"])

    def readings(self, fx):
        """The readings of a body (calls of pr_scenarios) in one pass, counted in the namespace the code calls: plan_ledger is a proxy to it."""
        namespace, calls = self.sb.plan_ledger.omitted_claims.__globals__, []
        real = namespace["pr_scenarios"]
        namespace["pr_scenarios"] = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        try:
            self.one_pass(fx)
        finally:
            namespace["pr_scenarios"] = real
        return len(calls)

    def scale_checks(self, sizes):
        done = {}
        for n in sizes:
            fx = self.world()
            self.held(fx, n)
            first = self.readings(fx)
            self.assertLessEqual(first, 3 * n, "readings per body at N=%d" % n)
            self.assertEqual(self.sent(fx), n, "notices sent at N=%d" % n)
            self.assertEqual((self.readings(fx), self.sent(fx)), (0, n), "second pass readings and notices at N=%d" % n)
            done[n] = first
        if len(done) == 2:
            self.assertEqual(done[1000], 5 * done[200], "ratio of readings for a ratio of 5 in size")

    @contextlib.contextmanager
    def mutated(self, name, old, new, where=None):
        """The function `name` again from a SOURCE COPY with `old` replaced by `new` (asserted found exactly once), bound in its own namespace, restored afterwards."""
        path = REPO / "bin" / ("switchboard/claims_note.py" if where is None else where)
        src = path.read_text()
        start = src.index("def %s(" % name)
        body = src[start:src.index("\n\n\ndef ", start)] if "\n\n\ndef " in src[start:] else src[start:]
        self.assertEqual(body.count(old), 1, "the mutation is applied exactly once")
        namespace = (self.sb.plan_ledger.omitted_claims if where else self.sb.claims_note_pass).__globals__
        original = namespace[name]
        exec(body.replace(old, new), namespace)
        try:
            yield
        finally:
            namespace[name] = original

    def test_s8_a_pass_reads_each_body_at_most_three_times_sends_every_notice_and_a_second_pass_reads_none(self):
        self.scale_checks((200, 1000))

    def test_s8_a_mutant_that_reads_the_others_again_for_each_examination_fails_beside_its_control(self):
        self.scale_checks((200,))   # the control
        with self.mutated("claims_note_ids", "return plan_ledger.omitted_claims(scenarios, body, counts[key])",
                          "return (lambda _: plan_ledger.omitted_claims(scenarios, body, counts[key]))([plan_ledger.covered_ids(scenarios, [o['body']]) for g in (opened, merged) for o in g if o is not record and isinstance(o.get('body'), str)])"):
            with self.assertRaisesRegex(AssertionError, r"readings per body at N=200"):
                self.scale_checks((200,))

    def test_s8_a_mutant_that_builds_the_counts_for_each_examination_fails_beside_its_control(self):
        self.scale_checks((200,))   # the control
        with self.mutated("claims_note_ids", "if key not in counts:", "if True:"):
            with self.assertRaisesRegex(AssertionError, r"readings per body at N=200"):
                self.scale_checks((200,))

    def fixtures(self):
        rng, out = random.Random(1716), []
        for _ in range(300):
            ids = ["S%d" % i for i in range(1, rng.randint(3, 8) + 1)]
            scenarios = [{"id": i, "claim": "pr"} for i in ids] + [{"id": "E1", "claim": "epic"}]
            def body():
                kind = rng.choice(("none", "refused", "full", "partial", "partial", "same"))
                if kind == "none":
                    return "PLAN: acme/api#3"
                if kind == "refused":
                    return "PLAN: acme/api#3\nCLAIMS: S99"
                if kind == "full":
                    return "PLAN: acme/api#3\nCLAIMS: " + " ".join(ids)
                return "PLAN: acme/api#3\nCLAIMS: " + " ".join(rng.sample(ids, rng.randint(1, len(ids)) if kind == "partial" else 1))
            mine = body()
            others = [body() for _ in range(rng.randint(0, 6))]
            if others and rng.random() < 0.3:
                others[0] = mine   # a body that covers the same ids as another
            out.append((scenarios, mine, others))
        return out

    def counts_match(self):
        ledger, wrong = self.sb.plan_ledger, 0
        for scenarios, mine, others in self.fixtures():
            named = ledger.omitted_claims(scenarios, mine, ledger.claim_counts(scenarios, [mine] + others))
            _, left, error = ledger.pr_scenarios(scenarios, mine)   # as claim_union_note reads them
            covered = ledger.covered_ids(scenarios, others)
            expected = [] if error or not left else [x["id"] for x in left if x["id"] not in covered]
            wrong += named != expected
        self.assertEqual(300 - wrong, 300, "fixtures with equal id lists, of 300")

    def test_s9_the_counts_and_claim_union_note_name_the_same_ids_in_300_of_300_fixtures(self):
        self.counts_match()

    def test_s9_a_mutant_that_reads_a_count_of_one_as_no_cover_fails_beside_its_control(self):
        self.counts_match()   # the control
        old = 'not counts.get(s.get("id"), 0)'
        with self.mutated("omitted_claims", old, 'counts.get(s.get("id"), 0) <= 1', where="plan_ledger.py"):   # an id that exactly one other body covers is read as covered by nobody
            with self.assertRaisesRegex(AssertionError, r"fixtures with equal id lists, of 300"):
                self.counts_match()

    def corrupt_checks(self, restart):
        fx = self.world()
        self.held(fx, 30)
        target = Path(fx.engine.state_dir) / self.sb.CLAIMS_NOTE_FILE
        target.write_text("\x00garbage{{{")
        fx.engine.claims_note = None
        seen = []
        for number in (1, 2, 3):
            if restart:
                fx.engine.claims_note = None
            before = self.sent(fx)
            self.one_pass(fx)
            seen.append(self.sent(fx) - before)
            if number == 1:
                try:
                    valid = "announced" in json.loads(target.read_text())
                except ValueError:
                    valid = False
                self.assertTrue(valid, "the file is valid after the first pass")
        self.assertEqual(seen, [30, 0, 0], "notices in the 3 passes")
        self.assertEqual(self.errors(fx), 1, "claims_note.error lines")

    def test_s10_a_corrupt_file_is_audited_once_and_30_notices_go_once_on_one_engine_and_with_restarts(self):
        self.corrupt_checks(False)
        self.corrupt_checks(True)

    def test_s10_a_mutant_that_does_not_rewrite_the_file_after_a_corrupt_read_fails_beside_its_control(self):
        self.corrupt_checks(True)   # the control
        with self.mutated("claims_note_pass", "if sent or old:", "if (sent or old) and not note.problem:"):
            with self.assertRaisesRegex(AssertionError, r"the file is valid after the first pass"):
                self.corrupt_checks(True)


class TestClaimsNoteMutants(ScaleChecks, ClaimsNoteBase):
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
            with self.assertRaisesRegex(AssertionError, r"\ALists differ: \[\['S1', 'S2', 'S3', 'U1'\]\] != \[\['S3', 'U1'\]\]"):
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
            with self.assertRaisesRegex(AssertionError, r"\ALists differ: \[\d+\.\d+\] != \[\d+\.\d+\]"):
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
