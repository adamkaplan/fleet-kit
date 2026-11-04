"""`gates ci-record` records the proof of each CI scenario as well as its verdict (#2091)."""
import json
import subprocess

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
            self.assertEqual((len(proofs), len(verdicts)), (3, 3), "ci-record records the proof and the verdict")
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
            self.assertEqual(len(self.ledger()), n + 6, "the fresh merge ref at the new head records")

        def test_S4_the_tester_and_reviewer_scenarios_gain_no_record(self):
            self.assertEqual(self.record()[0], 0)
            touched = {e["scenario"] for e in self.ledger()}
            self.assertEqual(touched, {"S1", "U1"}, "ci-record touches only the ci scenarios")
            self.assertEqual(len(self.rows("proof")), 2)

    return TestCiRecordProof
