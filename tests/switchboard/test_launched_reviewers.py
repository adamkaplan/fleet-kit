"""#1869: a reviewer seat is accepted by its launch record, like a tester seat. build() gives the tests the fixtures of bin/test-switchboard (HOST).
Every test runs the REAL commands against a fake world; the mutant tests run the guards on a SOURCE COPY."""
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from support.loader import sb
from support.paths import REPO
from support.herdr_world import set_retired

EXTRA = """  - id: S5
    covers: DW-2
    text: Given a second chat window, when the reply arrives, then the footer shows the agent state
    proof: screenshot
    pass: the footer shows the word "Ready" after the reply
    discriminates: a build that leaves the footer blank shows an empty footer in the screenshot
    evaluator: reviewer
  - id: S6
    covers: DW-2
    text: Given a second commissioned agent, when the owner sends a chat message, then it answers
    proof: live
    pass: the second reply arrives within 5 s and is longer than 0 characters
    discriminates: a stub that returns a canned empty reply fails
    evaluator: tester
"""
OWNER = """  - id: S7
    covers: DW-2
    text: Given the released build, when the owner opens it, then the owner confirms it
    proof: live
    pass: the owner confirms within 5 s that the build is "Ready"
    discriminates: a build that does not start is not confirmed
    evaluator: owner
"""
SID = {"tester": "S2", "reviewer": "S3"}


def build():
    host = HOST   # set by bin/test-switchboard: the fixtures of that file

    class Fixture(host.LedgerBase):
        def setUp(self):
            super().setUp()
            add = self.world.add_agent
            add("orch", fleet={"role": "orchestrator", "repo": "acme/app", "issue": 7})
            add("orch-web", fleet={"role": "orchestrator", "repo": "acme/web", "issue": 8})
            self.configure(evaluators={"reviewer": ["rev"], "tester": ["tst"], "owner": ["own"], "ci": ["cibot"]})
            for sid in ("S2", "S3"):
                self.assertEqual(self.prove("coder", sid)[0], 0)

        def seat(self, name, role="reviewer", repo="acme/app", boss="orch", by="orch", issue=7, **more):
            fleet = {"role": role, "repo": repo, "reports_to": boss, "issue": issue}
            if by is not None:
                fleet["launched_by"] = by
            fleet.update(more)
            self.world.add_agent(name, fleet=fleet)
            return name

        def tweaked(self, sessions=None, roles=None):
            """The real discovery, then the fake world's session and role map adjusted for the one thing a case needs (two agents in one session; a seat that is a coder)."""
            real = sb.launched_testers_for

            def wrapper(ctx, role="tester"):
                launch, got, fleets = real(ctx, role)
                got, fleets = dict(got), {k: dict(v) for k, v in fleets.items()}
                for name, value in (sessions or {}).items():
                    got[name] = got[value] if value in got else value
                for name, value in (roles or {}).items():
                    fleets[name]["role"] = value
                return launch, got, fleets
            return mock.patch.object(sb, "launched_testers_for", wrapper)

        def bound(self):
            ih = sb.load_module("impact_hold")
            return mock.patch.multiple(ih, load_module=sb.load_module, plan_ledger=sb.plan_ledger, create=True)

        def verdicts(self):
            return [e for e in self.ledger() if e["kind"] == "verdict"]

        def audited(self, who):
            return [e["detail"] for e in self.world.audit() if e["event"] == "plan.verdict" and e["detail"]["by"] == who]

        def case(self, name, role):
            """One independence case for one role through the real verdict command: (exit code, stderr, seat name)."""
            seat = "%s-%s" % (name, role)
            other = "tester" if role == "reviewer" else "reviewer"
            tweak = {}
            if name == "role":
                self.seat(seat, role=other)
            elif name == "repo":
                self.seat(seat, role=role, repo="acme/web", boss="orch-web", by="orch-web")
            elif name == "boss":
                self.seat(seat, role=role, boss="coder", by="coder")
            elif name == "launcher":
                self.seat(seat, role=role, by="coder")
            elif name == "unrecorded_launcher":
                self.seat(seat, role=role, by=None)
            elif name == "retired":
                self.seat(seat, role=role)
                set_retired(self.world.state, {seat: {"at": 1, "by": "orch", "reason": "done", "session": self.world.sessions[seat], "auto": False}})
            elif name == "coder_session":
                self.seat(seat, role=role)
                self.world.add_agent("cc-" + role, fleet={"role": "coder", "repo": "acme/app", "issue": 7})
                tweak = {"sessions": {"cc-" + role: seat}}
            elif name == "coder_name":
                self.seat(seat, role=role)
                tweak = {"roles": {seat: "coder"}}
            elif name == "writer_proof":
                self.seat(seat, role=role)
                got = self.run_as(seat, "prove", {"tester": "S1", "reviewer": "U1"}[role], "--pr", self.PR, "--evidence", "ran the suite as %s: 12 passed" % seat, "--result", "pass")
                self.assertEqual(got[0], 0, got)
                self.assertIn(seat, [e["by"] for e in self.ledger() if e["kind"] == "proof"])
            elif name == "writer_notice":
                self.seat(seat, role=role)
                self.assertEqual(self.run_as(seat, "notice", "pr", self.PR, "ready")[0], 0)
            elif name == "writer_session":
                self.seat(seat, role=role)
                tweak = {"sessions": {"coder": seat}}
            else:
                self.seat(seat, role=role)
            with self.tweaked(**tweak):
                code, _, err = self.verdict(seat, SID[role], role)
            return code, err, seat

    class TestLaunchedReviewerRule(Fixture):
        """S2, S3, S4, S5, S9, U4, U7: the rule of the launch record for a reviewer, through the real verdict command."""

        def test_s2_each_false_condition_refuses_with_its_reason_and_the_all_true_seat_and_the_configured_seat_record(self):
            registered = "not registered to record reviewer verdicts"
            expected = {"role": registered, "repo": registered, "boss": registered, "launcher": registered, "retired": registered,
                        "coder_session": "is not a different agent from cc-reviewer, a coder launched on work item #7"}
            for name, reason in expected.items():
                with self.subTest(case=name):
                    code, err, seat = self.case(name, "reviewer")
                    self.assertEqual(code, 2)
                    self.assertIn(reason, err)
                    self.assertEqual([e["by"] for e in self.verdicts() if e["by"] == seat], [])
            self.assertEqual(len(expected), 6)
            code, err, seat = self.case("ok", "reviewer")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.verdict("rev", "S3", "reviewer")[0], 0)
            self.assertEqual(sorted(e["by"] for e in self.verdicts()), ["ok-reviewer", "rev"])

        def test_s3_the_same_cases_give_the_same_outcome_and_words_for_a_tester_and_a_reviewer(self):
            names = ("writer_proof", "writer_notice", "writer_session", "coder_name", "coder_session", "retired", "launcher")
            self.assertEqual(len(names), 7)
            same = 0
            for name in names:
                got = {}
                for role in ("tester", "reviewer"):
                    code, err, seat = self.case(name, role)
                    got[role] = (code, err.replace(seat, "SEAT").replace("cc-" + role, "CC").replace(role, "ROLE"))
                    self.assertEqual(code, 2, (name, role, err))
                with self.subTest(case=name):
                    self.assertEqual(got["tester"], got["reviewer"])
                same += got["tester"] == got["reviewer"]
                self.assertEqual(got["reviewer"][0], 2, name)
            self.assertEqual(same, 7)
            self.assertEqual(self.verdicts(), [])
            code, err, seat = self.case("writer_proof", "reviewer")
            self.assertIn("wrote this PR", err)

        def test_s4_the_audit_line_of_a_launched_reviewer_carries_the_launch_record_and_a_configured_one_none(self):
            self.assertEqual(self.case("ok", "reviewer")[0], 0)
            self.assertEqual(self.verdict("rev", "S3", "reviewer")[0], 0)
            launched, configured = self.audited("ok-reviewer"), self.audited("rev")
            self.assertEqual((len(launched), len(configured)), (1, 1))
            self.assertEqual({k: launched[0]["launch_record"][k] for k in ("name", "role", "repo", "reports_to")},
                             {"name": "ok-reviewer", "role": "reviewer", "repo": "acme/app", "reports_to": "orch"})
            self.assertNotIn("launch_record", configured[0])
            lt = sb.load_launch_testers()
            record = {"ok-reviewer": launched[0]["launch_record"]}
            cfg = {"reviewer": ["rev"]}
            self.assertEqual((lt.source_of("ok-reviewer", cfg, record, "reviewer"), lt.source_of("rev", cfg, record, "reviewer")), ("launch record", "config"))
            self.assertEqual([e["right"] for e in self.verdicts()], ["launch record", "config"])

        def test_s5_ci_and_owner_never_come_from_a_launch_record_and_the_configuration_is_not_touched(self):
            before = self.config_file.read_bytes()
            self.plan_issue["body"] = host.LEDGER_PLAN.rsplit("```\n", 1)[0] + OWNER + "```\n"
            self.assertEqual(self.prove("coder", "S7")[0], 0)
            self.seat("lci", role="ci")
            self.seat("lown", role="owner")
            self.seat("lrev")
            for who in ("lci", "lown", "lrev"):
                for sid, ev in (("S1", "ci"), ("S7", "owner")):
                    code, _, err = self.verdict(who, sid, ev)
                    self.assertNotEqual(code, 0, (who, ev))
                    self.assertIn("not registered to record ci verdicts" if ev == "ci" else "only the owner, from the owner's own terminal", err)
            self.assertEqual(self.verdicts(), [])
            self.assertEqual(sb.load_launch_testers().evaluators({"ci": ["cibot"]}, {"lci": {}}, "reviewer").get("ci"), ["cibot"])
            self.assertEqual(self.config_file.read_bytes(), before)

        def carry(self, who):
            ih = sb.load_module("impact_hold")
            head, pr = "a" * 40, "acme/app#9"
            sb.plan_ledger.append_entry(self.world.state, {"kind": "review_record", "pr": pr, "head": head, "reviewer": "code", "verdict": "approve", "blocks": 0, "notes": 0,
                                                           "by": who, "summary": "", "by_role": "reviewer", "carry_confirmed": True}, self.world.clock.now())
            engine = types.SimpleNamespace(state_dir=self.world.state, config=self.world.config)
            with self.bound(), mock.patch.object(ih, "read_pr_json", lambda *a: {}, create=True), mock.patch.object(ih, "read_pr_notices", lambda *a: [], create=True), \
                    mock.patch.object(ih, "pr_facts_from", lambda raw: {"author": "shared"}, create=True), mock.patch.object(ih, "notice_senders", lambda item: [], create=True):
                return ih.carry_confirmer(engine, "acme/app", 9)(head)

        def test_s5_s9_the_confirmation_of_a_clear_carry_and_the_preflight_read_the_configured_list_only(self):
            self.world.config["plan_evaluators"] = {"reviewer": ["rev"]}
            self.seat("lrev")
            cc = sb.load_module("clear_carry")
            with self.assertRaises(cc.CarryVoid) as raised:
                self.carry("lrev")
            self.assertIn("no confirmation at this head by a registered reviewer", str(raised.exception))
            self.assertIn("ledger entry", self.carry("rev"))   # the control: a configured reviewer's confirmation counts
            blocking, notes = sb.plan_ledger.preflight({"plan_evaluators": {"ci": ["cibot"], "tester": ["tst"]}}, "acme/app", cos_ready=lambda: None)
            self.assertTrue(any("no agent is registered for `reviewer`" in n for n in notes))

        def test_u4_the_old_calls_of_the_module_give_their_tester_results_and_the_constant_names_two(self):
            lt = sb.load_launch_testers()
            fleets = {"orch": {"role": "orchestrator", "repo": "acme/app"}, "t1": {"role": "tester", "repo": "acme/app", "reports_to": "orch", "launched_by": "orch"},
                      "r1": {"role": "reviewer", "repo": "acme/app", "reports_to": "orch", "launched_by": "orch"}}
            repo_of = lambda f: f.get("repo")
            self.assertEqual(lt.launched(fleets, {"t1": "s"}, "acme/app", repo_of),
                             {"t1": {"name": "t1", "role": "tester", "repo": "acme/app", "reports_to": "orch", "issue": None, "session": "s"}})
            self.assertEqual(sorted(lt.launched(fleets, {}, "acme/app", repo_of, None, "reviewer")), ["r1"])
            self.assertEqual(lt.evaluators({"tester": ["tst"], "reviewer": ["r"]}, {"t1": {}}), {"tester": ["tst", "t1"], "reviewer": ["r"]})
            self.assertEqual(lt.source_of("tst", {"tester": ["tst"], "reviewer": ["tst"]}, {}), "config")
            self.assertEqual(lt.source_of("t1", {"reviewer": ["t1"]}, {"t1": {}}), "launch record")   # the tester list was asked: not the reviewer list
            self.assertIn("a tester cannot record a verdict", lt.lane_refusal("t1", {"t1": {}}, {"t1"}, {}))
            self.assertIn("a reviewer cannot record a verdict", lt.lane_refusal("r1", {"r1": {}}, {"r1"}, {}, None, "reviewer"))
            self.assertEqual(lt.LAUNCHABLE, ("tester", "reviewer"))
            self.assertNotIn("ci", lt.LAUNCHABLE)
            self.assertNotIn("owner", lt.LAUNCHABLE)

        def test_u7_the_four_configured_seats_record_and_the_configuration_bytes_are_equal(self):
            seats = ("rev-1", "rev-2", "rev-3", "rev-4")
            self.configure(evaluators={"reviewer": list(seats), "tester": ["tst"], "owner": ["own"], "ci": ["cibot"]})
            before = self.config_file.read_bytes()
            for seat in seats:
                self.world.add_agent(seat)
                self.assertEqual(self.verdict(seat, "S3", "reviewer")[0], 0, seat)
            self.assertEqual(sorted(e["by"] for e in self.verdicts()), list(seats))
            self.assertEqual({e["right"] for e in self.verdicts()}, {"config"})
            self.assertEqual(self.config_file.read_bytes(), before)

    class TestLaunchedReviewerGates(Fixture):
        """S11, U9: `gates` shows where the right of each verdict came from; the ledger accepts the field."""

        def setUp(self):
            super().setUp()
            self.plan_issue["body"] = host.LEDGER_PLAN.rsplit("```\n", 1)[0] + EXTRA + "```\n"
            for sid in ("S5", "S6"):
                code, _, err = self.prove("coder", sid)
                self.assertEqual(code, 0, err)
            self.seat("lrev")
            self.seat("ltst", role="tester")

        def record(self):
            self.assertEqual(self.verdict("lrev", "S3", "reviewer")[0], 0)
            self.assertEqual(self.verdict("ltst", "S2", "tester")[0], 0)
            self.assertEqual(self.verdict("rev", "S5", "reviewer")[0], 0)
            first = self.verdicts()[0]
            old = {k: first[k] for k in ("pr", "plan_ref", "plan_hash", "head", "result", "evidence")}
            sb.plan_ledger.append_entry(self.world.state, dict(old, kind="verdict", scenario="S6", by="tst", evaluator="tester"), self.world.clock.now())   # recorded before the change: no `right`

        def test_s11_gates_prints_the_seat_and_the_source_of_each_verdict_in_text_and_json(self):
            self.record()
            code, report, err = self.gates()
            self.assertIsNotNone(report, err)
            lines = report["verdict_lines"]
            self.assertEqual(len(lines), 4)
            self.assertEqual([l for l in lines if l.endswith("launch record")], ["verdict: S2: tester verdict #%d by ltst, launch record" % self.seq("S2"),
                                                                                 "verdict: S3: reviewer verdict #%d by lrev, launch record" % self.seq("S3")])
            self.assertEqual([l for l in lines if l.endswith(", config")], ["verdict: S5: reviewer verdict #%d by rev, config" % self.seq("S5")])
            self.assertEqual([l for l in lines if l.endswith("source not recorded")], ["verdict: S6: tester verdict #%d by tst, source not recorded" % self.seq("S6")])
            code, out, err = self.run_as("coder", "gates", self.PR)
            self.assertIn(code, (0, 1), err)   # 1: the pull request is not ready, which is not what is read here
            printed = [l.strip() for l in out.splitlines() if l.strip().startswith("verdict: ")]
            self.assertEqual(printed, lines)

        def seq(self, sid):
            return next(e["seq"] for e in self.verdicts() if e["scenario"] == sid)

        def test_u9_a_second_recording_reads_already_recorded_the_ledger_is_valid_and_an_older_verdict_is_shown_as_before(self):
            self.record()
            before = self.verdicts()
            code, out, err = self.verdict("lrev", "S3", "reviewer")
            self.assertEqual(code, 0, err)
            self.assertIn("already recorded", out)
            self.assertEqual(self.verdicts(), before)
            self.assertEqual([e["by"] for e in self.verdicts() if e["scenario"] == "S3"], ["lrev"])
            entries, problem = sb.plan_ledger.read_ledger(self.world.state)
            self.assertIsNone(problem)
            self.assertIn("right", before[0])
            self.assertNotIn("right", self.verdicts()[-1])
            code, report, _ = self.gates()
            self.assertEqual([l for l in report["verdict_lines"] if "source not recorded" in l].__len__(), 1)

    class TestLaunchedReviewerItems(host.LedgerBase):
        """U5: the item path stays tester-only."""

        def setUp(self):
            super().setUp()
            self.fx = importlib.import_module("test_cases_09").TestItemVerdicts("test_s1_a_registered_tester_records_a_verdict_for_its_item")
            self.fx.setUp()
            self.addCleanup(self.fx.doCleanups)
            self.fx.sessions["r1"] = "ses_r1"
            self.fx.fleets["r1"] = {"role": "reviewer", "repo": self.fx.REPO, "reports_to": "orch", "launched_by": "orch", "issue": self.fx.N}

        def test_u5_the_item_path_refuses_a_launched_reviewer_and_records_a_launched_tester(self):
            self.fx.register("r1")
            code, why = self.fx.verdict("r1")
            self.assertIsNone(code)
            self.assertIn("r1 is not a tester launched by an orchestrator", why)
            self.assertEqual(self.fx.verdicts(), [])
            self.fx.register("t1")
            self.assertEqual(self.fx.verdict("t1"), (sb.EXIT_OK, None))
            got = self.fx.verdicts()
            self.assertEqual([(e["by"], e["evaluator"], e["right"]) for e in got], [("t1", "tester", "launch record")])

    class TestLaunchedReviewerRelay(unittest.TestCase):
        """S6, U6: the cloud relay judges a verdict by the configured list alone, and reads no launch record."""

        PR, REPO, HEAD = "acme-org/widget-service#7", "acme-org/widget-service", "a" * 40
        CODER, TESTER, REVIEWER = "widget-api-coder", "widget-api-tester", "widget-api-reviewer"

        def setUp(self):
            self.relay = sb.load_module("cloud_relay_daemon")
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            case = self
            self.reads = []

            class Backend:
                head, author, senders = case.HEAD, None, ()

                def live_head(self, pr):
                    return self.head

                def plan(self, pr):
                    return {"source": "acme-org/widget-service#1", "hash": "sha256:" + "b" * 64,
                            "scenarios": {"S1": {"evaluator": "tester", "claim": "pr"}, "S2": {"evaluator": "reviewer", "claim": "pr"}}}

                def pr_facts(self, pr):
                    return {"author": self.author, "notice_senders": self.senders}

                def evaluators(self, evaluator):
                    return {"tester": [case.TESTER], "reviewer": [case.REVIEWER]}.get(evaluator, [])

                def holder_of(self, repo, issue):
                    return case.CODER

                def pr_issues(self, pr):
                    return {1}

                def gates(self, pr):
                    return {"ready": False}

                def report(self, pod, args):
                    pass

                def audit(self, kind, pr, detail):
                    pass

                def __getattr__(self, name):   # a read of a launch record or of the running agents is a read this backend must never be asked for
                    if re.search(r"launch|agent|discover|registry|retired|fleet|session", name):
                        case.reads.append(name)
                        raise AssertionError("the relay read %s" % name)
                    raise AttributeError(name)
            self.backend = Backend()
            self.queue, self.results, self.n = [], {}, 0
            self.ex = self.relay.Executor(self.tmp.name, self.backend, lambda: 1000)

        def run_one(self, pod, command, **args):
            self.n += 1
            rid = "%032x" % self.n
            call = {"call_id": "c%d" % self.n, "command": command, "pr": self.PR, "args": args, "head": self.HEAD}
            self.queue.append({"id": rid, "pod": pod, "repo": self.REPO, "issue": 1, "call": call})
            self.relay.pass_once(self.ex, lambda: {"requests": list(self.queue)}, lambda r, body: (self.results.__setitem__(r, body), self.queue.clear()))
            return self.results[rid]

        def go(self):
            lt = sb.load_module("launch_testers")
            with mock.patch.object(lt, "launched", side_effect=lambda *a, **k: self.reads.append("launched")), \
                    mock.patch.object(lt, "evaluators", side_effect=lambda *a, **k: self.reads.append("evaluators")):
                for sid in ("S1", "S2"):
                    got = self.run_one(self.CODER, "prove", scenario=sid, result="pass", evidence="ran it")
                    self.assertEqual(got["status"], 200, got)
                off = self.run_one("widget-api-launched-reviewer", "verdict", scenario="S2", result="pass", evidence="saw it")
                on = self.run_one(self.REVIEWER, "verdict", scenario="S2", result="pass", evidence="saw it")
                tester = self.run_one(self.TESTER, "verdict", scenario="S1", result="pass", evidence="saw it")
                stranger = self.run_one("widget-api-launched-tester", "verdict", scenario="S1", result="pass", evidence="saw it")
            return off, on, tester, stranger

        def test_s6_u6_a_reviewer_off_the_list_is_refused_a_configured_one_recorded_and_no_launch_record_is_read(self):
            off, on, tester, stranger = self.go()
            self.assertEqual(off["status"], 403)
            self.assertIn("widget-api-launched-reviewer is not registered to record reviewer verdicts", json.dumps(off))
            self.assertEqual(on["status"], 200)
            self.assertEqual(tester["status"], 200)
            self.assertEqual(stranger["status"], 403)
            self.assertIn("not registered to record tester verdicts", json.dumps(stranger))
            self.assertEqual(self.reads, [], "0 reads of a launch record or of the running agents")
            entries, problem = sb.plan_ledger.read_ledger(self.tmp.name)
            self.assertIsNone(problem)
            self.assertEqual([(e["by"], e["evaluator"]) for e in entries if e["kind"] == "verdict"], [(self.REVIEWER, "reviewer"), (self.TESTER, "tester")])

    class TestLaunchedReviewerDocs(unittest.TestCase):
        """S6, S8: what the documentation says (the plan's own claims about its text)."""

        def paragraph(self):
            text = (REPO / "docs" / "switchboard.md").read_text()
            start = text.index("Testers from the launch record (#372)")
            return text, text[start:].split("\n\n")[0]

        def test_s8_the_paragraph_names_both_roles_and_the_two_that_never_come_and_the_old_sentence_is_gone(self):
            text, para = self.paragraph()
            self.assertEqual(len(re.findall(r"`reviewer`, `ci` and `owner` never come from a launch record", text)), 0)
            self.assertIn("for `tester` and `reviewer`", para)
            self.assertIn("`ci` and `owner` never come from a launch record", para)
            self.assertIn("a reviewer launched with `--role reviewer` counts under the same conditions", para)

        def test_s6_the_documentation_says_in_one_sentence_why_the_relay_stays_list_only(self):
            text = (REPO / "docs" / "switchboard.md").read_text()
            self.assertEqual(len([s for s in re.split(r"(?<=\.)\s+", text) if "reads no agent's launch record" in s and "relay daemon holds the configuration parts" in s]), 1)
            self.assertIn("testers and reviewers from the launch record", (REPO / "docs" / "architecture.md").read_text())

    class TestLaunchedReviewerGuards(Fixture):
        """The two guards the mutants below must break: each asserts the check's own refusal text FIRST."""

        def test_guard_a_seat_whose_launch_was_not_run_by_its_boss_is_refused(self):
            seat = self.seat("forged", by="coder")
            code, _, err = self.verdict(seat, "S3", "reviewer")
            self.assertIn("forged is not registered to record reviewer verdicts", err)
            self.assertEqual(code, 2)

        def test_guard_the_writer_of_the_pull_request_is_refused(self):
            seat = self.seat("writer")
            self.assertEqual(self.prove(seat, "S1")[0], 0)
            code, _, err = self.verdict(seat, "S3", "reviewer")
            self.assertIn("writer wrote this PR", err)
            self.assertEqual(code, 2)

    class TestLaunchedReviewerMutants(unittest.TestCase):
        """S7: two mutants on a SOURCE COPY of the repository, each applied once, each run beside its control."""

        GUARD = "TestLaunchedReviewerGuards.test_guard_"
        MUTANTS = (("launched_by", "test_guard_a_seat_whose_launch_was_not_run_by_its_boss_is_refused", "forged is not registered to record reviewer verdicts",
                    (("bin/launch_testers.py", 'if fleet.get("launched_by") != boss:', 'if False and fleet.get("launched_by") != boss:'),)),
                   ("writer", "test_guard_the_writer_of_the_pull_request_is_refused", "writer wrote this PR",
                    (("bin/plan_ledger.py", '    if caller in authors:\n        return False, ("%s wrote this PR', '    if False:\n        return False, ("%s wrote this PR'),
                     ("bin/launch_testers.py", "if tester in (authors or ()):", "if False:"))))

        def copy(self):
            tmp = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, tmp, True)
            shutil.copytree(REPO, tmp / "repo", ignore=shutil.ignore_patterns(".git", "__pycache__"), symlinks=True)
            return tmp / "repo"

        def run_guard(self, root, name):
            env = dict(os.environ, FLEET_HEAVY="off")
            proc = subprocess.run([sys.executable, str(root / "bin" / "test-switchboard"), "TestLaunchedReviewerGuards." + name], capture_output=True, text=True, env=env, cwd=str(root), timeout=600)
            return proc.returncode, proc.stdout + proc.stderr

        def test_s7_each_mutant_is_applied_once_fails_its_named_guard_with_the_text_of_the_check_and_its_control_passes(self):
            if os.environ.get("FLEETKIT_MUTANT_COPY"):
                self.skipTest("inside a source copy")
            for label, guard, anchor, edits in self.MUTANTS:
                with self.subTest(mutant=label):
                    root = self.copy()
                    control_code, control_out = self.run_guard(root, guard)   # the control FIRST: the unmutated copy passes
                    self.assertEqual(control_code, 0, control_out[-1500:])
                    self.assertIn("1 passed", control_out)
                    for path, old, new in edits:
                        source = (root / path).read_text()
                        self.assertEqual(source.count(old), 1, "%s: applied once" % path)
                        (root / path).write_text(source.replace(old, new))
                    code, out = self.run_guard(root, guard)
                    self.assertNotEqual(code, 0, "the mutant survived")
                    self.assertIn(guard, out)
                    self.assertIn(anchor, out)

    return (TestLaunchedReviewerRule, TestLaunchedReviewerGates, TestLaunchedReviewerItems, TestLaunchedReviewerRelay, TestLaunchedReviewerDocs,
            TestLaunchedReviewerGuards, TestLaunchedReviewerMutants)
