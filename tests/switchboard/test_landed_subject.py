"""Ask #2373: a landed commit (`--pr <owner/repo>@<sha>`) is a subject of `prove`, `verdict` and `gates` for a repository on the land path.
build(LedgerBase) is called by bin/test-switchboard; the class derives from LedgerBase, whose Base isolates every state path. The fake runner answers the GETs of the commit,
the repository and the compare; nothing else is read or written."""
import json
import subprocess

from support.fake_hubs import LEDGER_HEAD
from support.loader import sb

SHA = "9" * 40
OFF = "b" * 40      # a commit that is not on the default branch
BARE = "c" * 40     # on the default branch, no trailers
HALF = "d" * 40     # only one of the two trailers
LAND_MSG = "Add the thing\n\nbody text\n\nLand-Checks: green\nLand-Council: approved\n"
MESSAGES = {SHA: LAND_MSG, OFF: LAND_MSG, BARE: "Add it\n\nno trailers here\n", HALF: "Add it\n\nLand-Checks: green\n"}
ON_BRANCH = {SHA: "behind", BARE: "identical", HALF: "behind", OFF: "diverged"}


def build(LedgerBase):
    class TestLandedSubject(LedgerBase):
        """S2 to S5 of #2373 and U1 (the trailer match is one function)."""

        extra_config = {"land_repos": ["acme/land"]}
        PLAN = ("--plan", "acme/app#7")

        def setUp(self):
            super().setUp()
            self.base_route = self.route
            self.route = self.land_route
            self.reads = []

        def land_route(self, argv, **kwargs):
            if len(argv) > 2 and argv[1] == "api" and argv[2].startswith("repos/acme/land"):
                path = argv[2]
                self.reads.append(path)
                sha = path.rsplit("/", 1)[-1].split("...")[-1]
                if path.endswith("repos/acme/land"):
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"default_branch": "main"}), "")
                if "/compare/" in path and sha in ON_BRANCH:
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"status": ON_BRANCH[sha]}), "")
                if "/commits/" in path and sha in MESSAGES:
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"sha": sha, "commit": {"message": MESSAGES[sha]}}), "")
                return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")
            return self.base_route(argv, **kwargs)

        def landed(self, sha=SHA, repo="acme/land"):
            return "%s@%s" % (repo, sha)

        def prove_at(self, ref, sid="S1"):
            return self.run_as("coder", "prove", sid, "--pr", ref, "--evidence", "ran the suite: 12 passed", *self.PLAN)

        def verdict_at(self, ref, who="tst", sid="S2", ev="tester"):
            return self.run_as(who, "verdict", sid, "--pr", ref, "--as", ev, "--result", "pass", "--evidence", "judged it: matches the scenario", *self.PLAN)

        def test_S2_prove_and_verdict_are_recorded_against_the_sha_and_the_plan_hash(self):
            ref = self.landed()
            code, out, err = self.prove_at(ref)
            self.assertEqual(code, 0, err)
            self.assertEqual(self.prove_at(ref, "S2")[0], 0)
            code, out, err = self.verdict_at(ref)
            self.assertEqual(code, 0, err)
            rows = self.ledger()
            self.assertEqual({r["kind"] for r in rows}, {"proof", "verdict"})
            for row in rows:
                self.assertEqual((row["pr"], row["head"], row["subject_kind"]), (ref, SHA, "landed"))
                self.assertTrue(row["plan_hash"].startswith("sha256:"))
            code, out, err = self.run_as("coder", "gates", ref, "--json", *self.PLAN)
            report = json.loads(out)
            self.assertEqual(report["plan_hash"], rows[0]["plan_hash"])
            self.assertEqual(report["head"], SHA)

        def test_S2_gates_prints_the_proof_gates_and_says_the_merge_gates_do_not_apply(self):
            code, out, err = self.run_as("coder", "gates", self.landed(), *self.PLAN)
            self.assertIn("G0a", out)
            self.assertIn("G1", out)
            self.assertIn("G2-G8 do not apply", out)
            code, out, err = self.run_as("coder", "gates", self.landed(), "--json", *self.PLAN)
            report = json.loads(out)
            rows = {r["gate"]: r["status"] for r in report["gates"]}
            self.assertEqual((rows["G0a"], rows["G1"]), ("ok", "missing"))
            self.assertTrue(all(status == "n/a" for gate, status in rows.items() if gate not in ("G0a", "G0b", "G0c", "G1")))

        def test_S3_a_sha_without_a_trailer_or_off_the_default_branch_is_refused_and_nothing_is_recorded(self):
            cases = ((BARE, "no trailer Land-Checks, Land-Council"), (HALF, "no trailer Land-Council"), (OFF, "not on the default branch"), ("e" * 40, "sha unknown"))
            for sha, words in cases:
                with self.subTest(sha=sha[:4]):
                    code, out, err = self.prove_at(self.landed(sha))
                    self.assertNotEqual(code, 0)
                    self.assertIn(words, err)
                    self.assertIn(sha[:12], err) if "unknown" not in words else self.assertIn(sha, err)
                    code, out, err = self.verdict_at(self.landed(sha))
                    self.assertNotEqual(code, 0)
                    self.assertIn(words, err)
            self.assertEqual(self.ledger(), [])

        def test_S3_a_repository_that_is_not_on_the_land_path_is_refused_naming_it(self):
            code, out, err = self.prove_at(self.landed(SHA, "acme/app"))
            self.assertNotEqual(code, 0)
            self.assertIn("not a land repository", err)
            self.assertEqual(self.ledger(), [])

        def test_S3_a_short_or_upper_case_sha_is_no_landed_subject(self):
            for ref in ("acme/land@" + SHA[:7], "acme/land@" + HALF.upper()):
                code, out, err = self.prove_at(ref)
                self.assertNotEqual(code, 0, (ref, out, err))
            self.assertEqual((self.ledger(), self.reads), ([], []))

        def test_S4_a_pull_request_subject_is_unchanged_and_carries_no_marker(self):
            code, out, err = self.prove("coder", "S1")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.reads, [])
            (row,) = self.ledger()
            self.assertEqual((row["pr"], "subject_kind" in row), (self.PR, False))

        def test_S4_a_pull_request_of_a_land_repository_is_still_a_pull_request(self):
            self.pr["body"] = "PLAN: acme/app#7\ncanon: updated the docs"
            code, out, err = self.run_as("coder", "prove", "S1", "--pr", "acme/land#9", "--evidence", "ran it: ok")
            self.assertEqual(code, 0, err)
            (row,) = self.ledger()
            self.assertEqual((row["pr"], row["head"], "subject_kind" in row), ("acme/land#9", LEDGER_HEAD, False))
            self.assertEqual([r for r in self.reads if "/commits/" in r or "/compare/" in r], [])

        def test_S5_control_without_the_option_the_old_parser_refuses_a_sha_subject(self):
            # the control: the pull request reader (which every subject took before #2373) refuses a sha reference, so the landed path is what accepts it
            with self.assertRaises(sb.NoticeError):
                sb.parse_pr_ref(self.landed())
            code, out, err = self.run_as("coder", "gates-check", self.landed(), "--for", "merge")
            self.assertNotEqual(code, 0)
            self.assertEqual(self.ledger(), [])

        def test_U1_the_trailer_match_is_one_function_with_the_stated_form(self):
            missing = sb.land_trailers_missing
            self.assertEqual(missing(LAND_MSG), [])
            self.assertEqual(missing("s\n\nland-checks: x\nLAND-COUNCIL:y"), [])
            self.assertEqual(missing("s\n\nLand-Checks:\nLand-Council: ok"), ["Land-Checks"])
            self.assertEqual(missing("Land-Checks: a\nLand-Council: b"), ["Land-Checks", "Land-Council"], "a subject paragraph alone is no trailer block")
            self.assertEqual(missing("s\n\nLand-Checks: a\nLand-Council: b\n\ntext after"), ["Land-Checks", "Land-Council"], "only the last paragraph counts")
            self.assertEqual(missing(None), ["Land-Checks", "Land-Council"])
    return TestLandedSubject
