"""`gates ci-record` records the proof of each CI scenario as well as its verdict (#2091)."""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support.fake_hubs import LEDGER_HEAD, LEDGER_PLAN

BASE_HEAD, OLD_BASE = "c" * 40, "d" * 40
ALL_CI = LEDGER_PLAN.replace("    proof: live\n    pass: the reply", "    proof: test\n    pass: the reply").replace("evaluator: tester", "evaluator: ci", 1)   # S1, S2 and U1 are ci scenarios


def build(LedgerBase):
    class TestCiRecordProof(LedgerBase):
        """S2 to S4 of #2091: the proof next to the verdict, nothing twice, nothing on a red, unfinished or stale run, the tester and reviewer scenarios untouched."""

        def setUp(self):
            super().setUp()
            self.extra_config = {"plan_gates": {"repos": {"acme/app": {"mode": "shadow"}}}}
            self.configure(evaluators={"ci": ["switchboard"], "reviewer": ["rev"], "tester": ["tst"]})
            self.first_parent = BASE_HEAD   # the merge ref's first parent; the base's current head is BASE_HEAD
            self.base_route = self.route
            self.route = self.merge_route

        def merge_route(self, argv, **kwargs):
            if len(argv) > 2 and argv[1] == "api":
                path = argv[2]
                if path.endswith("/commits/refs/pull/9/merge"):
                    return subprocess.CompletedProcess(argv, 0, json.dumps([{"sha": self.first_parent}, {"sha": LEDGER_HEAD}]), "")
                if path.endswith("/pulls/9"):
                    return subprocess.CompletedProcess(argv, 0, "integration\n", "")
                if path.endswith("/branches/integration"):
                    return subprocess.CompletedProcess(argv, 0, BASE_HEAD + "\n", "")
            return self.base_route(argv, **kwargs)

        def record(self):
            return self.run_as(None, "gates", "ci-record", self.PR)

        def rows(self, kind):
            return [e for e in self.ledger() if e["kind"] == kind]

        def test_S2_three_ci_scenarios_get_a_proof_and_a_verdict_and_g1_misses_none(self):
            self.plan_issue["body"] = ALL_CI
            code, out, err = self.record()
            self.assertEqual(code, 0, err)
            proofs, verdicts = self.rows("proof"), self.rows("verdict")
            ci = {"S1", "S2", "U1"}   # the ci scenarios of ALL_CI
            self.assertLessEqual(ci, {p["scenario"] for p in proofs}, "ci-record records the proof and the verdict")
            self.assertLessEqual(ci, {v["scenario"] for v in verdicts}, "ci-record records the verdict of each ci scenario")
            self.assertTrue(all(p["head"] == LEDGER_HEAD and LEDGER_HEAD[:12] in p["evidence"] and p["by"] == "switchboard" for p in proofs))
            self.assertIn("and the proof of S1, S2, U1", out)
            _, report, _ = self.gates()
            g1 = next(r for r in report["gates"] if r["gate"] == "G1")
            self.assertEqual([m for m in g1["missing"] if m.split(":")[0] in ("S1", "S2", "U1")], [], "ci-record records the proof and the verdict")

        def test_S3_nothing_twice_and_nothing_from_a_red_unfinished_or_stale_run(self):
            self.plan_issue["body"] = ALL_CI
            self.assertEqual(self.record()[0], 0)
            n = len(self.ledger())
            code, out, err = self.record()
            self.assertEqual((code, len(self.ledger())), (0, n), "nothing twice")
            self.pr["headRefOid"] = "e" * 40
            for rollup, word in (([{"conclusion": "FAILURE", "status": "COMPLETED"}], "failing"), ([{"status": "IN_PROGRESS", "conclusion": ""}], "pending")):
                self.pr["statusCheckRollup"] = rollup
                code, out, err = self.record()
                self.assertEqual(len(self.ledger()), n, word)
                self.assertEqual(len([l for l in err.splitlines() if "CI is %s" % word in l]), 1)
            self.pr["statusCheckRollup"] = [{"conclusion": "SUCCESS", "status": "COMPLETED"}]
            self.first_parent = OLD_BASE
            code, out, err = self.record()
            self.assertEqual((code, len(self.ledger())), (0, n), "a stale merge ref records nothing")
            self.assertEqual(len([l for l in out.splitlines() if "stale merge ref" in l]), 1)
            self.first_parent = BASE_HEAD
            self.record()
            self.assertGreater(len(self.ledger()), n, "the fresh merge ref at the new head records")

        def test_S4_the_tester_and_reviewer_scenarios_gain_no_record(self):
            self.assertEqual(self.record()[0], 0)
            touched = {e["scenario"] for e in self.ledger()}
            self.assertLessEqual({"S1", "U1"}, touched, "ci-record records the ci scenarios")
            self.assertTrue(touched.isdisjoint({"S2", "S3"}), "ci-record leaves the tester and reviewer scenarios alone")
            self.assertLessEqual({"S1", "U1"}, {e["scenario"] for e in self.rows("proof")})

        def test_an_unreadable_merge_ref_records_nothing_and_says_why(self):
            self.plan_issue["body"] = ALL_CI
            real = self.route

            def broken(argv, **kwargs):
                if len(argv) > 2 and argv[1] == "api" and argv[2].endswith("/commits/refs/pull/9/merge"):
                    return subprocess.CompletedProcess(argv, 1, "", "HTTP 500 boom")
                return real(argv, **kwargs)
            self.route = broken
            code, out, err = self.record()
            self.assertEqual((code, len(self.ledger())), (0, 0), "an unreadable merge ref records nothing")
            self.assertEqual(len([l for l in out.splitlines() if "merge ref could not be read" in l and "boom" in l]), 1)

        def test_a_missing_merge_ref_records_nothing_and_says_why(self):
            self.plan_issue["body"] = ALL_CI
            real = self.route

            def gone(argv, **kwargs):
                if len(argv) > 2 and argv[1] == "api" and argv[2].endswith("/commits/refs/pull/9/merge"):
                    return subprocess.CompletedProcess(argv, 1, "", "HTTP 404 Not Found")
                return real(argv, **kwargs)
            self.route = gone
            code, out, err = self.record()
            self.assertEqual((code, len(self.ledger())), (0, 0), "a missing merge ref records nothing")
            self.assertEqual(len([l for l in out.splitlines() if "no merge ref" in l]), 1)

        def test_S5_the_proof_write_has_a_mutant_the_S2_case_catches(self):
            """Run S2 in this process on the unmutated daemon (passes), then with `ci_record_core` rebuilt without the proof write (fails with the anchored text in its own output)."""
            from support import loader
            daemon = loader.sb
            case = "test_S2_three_ci_scenarios_get_a_proof_and_a_verdict_and_g1_misses_none"

            def run():
                result = unittest.TestResult()
                type(self)(case).run(result)
                return result, "".join(text for _, text in result.failures + result.errors)
            clean, text = run()
            self.assertTrue(clean.wasSuccessful() and clean.testsRun == 1, "the unmutated daemon passes S2: " + text[-600:])
            source = (Path(__file__).resolve().parents[2] / "bin" / "fleet-switchboard").read_text()
            node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "ci_record_core")
            head, sep, tail = ast.get_source_segment(source, node).partition("    for scenario in wanted:   # the proof of each ci scenario")
            self.assertTrue(sep and "plan_ledger.append_once(" in tail, "the proof write is where the mutant expects it")
            namespace = {}
            exec(compile(head + sep + tail.replace("plan_ledger.append_once(", "(lambda *a: None)(", 1), "<mutant ci_record_core>", "exec"), daemon.__dict__, namespace)
            with mock.patch.object(daemon, "ci_record_core", namespace["ci_record_core"]):
                mutant, text = run()
            self.assertFalse(mutant.wasSuccessful(), "the mutant fails S2")
            self.assertIn("ci-record records the proof and the verdict", text)

    return TestCiRecordProof
