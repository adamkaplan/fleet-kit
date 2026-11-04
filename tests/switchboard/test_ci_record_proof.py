"""`gates ci-record` records the proof of each CI scenario as well as its verdict (#2091)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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
            """Copy the tree, run S2 on the unmutated copy (passes), then on a copy without the proof write (fails with the anchored text in its own output)."""
            root = Path(__file__).resolve().parents[2]
            case = "test_S2_three_ci_scenarios"
            with tempfile.TemporaryDirectory() as tmp:
                copy = Path(tmp) / "tree"
                shutil.copytree(root, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))
                env = dict(os.environ, FLEET_SWITCHBOARD_UNDER_TEST="1")

                def run():
                    return subprocess.run([sys.executable, str(copy / "bin" / "test-switchboard"), case], cwd=str(copy), env=env, capture_output=True, text=True, timeout=600)
                clean = run()
                self.assertEqual(clean.returncode, 0, "the unmutated copy passes S2: " + (clean.stdout + clean.stderr)[-600:])
                core = copy / "bin" / "fleet-switchboard"
                text = core.read_text()
                head, sep, tail = text.partition("    for scenario in wanted:   # the proof of each ci scenario")
                self.assertTrue(sep and "plan_ledger.append_once(" in tail, "the proof write is where the mutant expects it")
                core.write_text(head + sep + tail.replace("plan_ledger.append_once(", "(lambda *a: None)(", 1))
                mutant = run()
                self.assertNotEqual(mutant.returncode, 0, "the mutant fails S2")
                self.assertIn("ci-record records the proof and the verdict", mutant.stdout + mutant.stderr)

    return TestCiRecordProof
