"""Ask #2373: a landed commit (`--pr <owner/repo>@<sha>`) is a subject of `prove`, `verdict` and `gates` for a repository on the land path.
build(LedgerBase) is called by bin/test-switchboard; the class derives from LedgerBase, whose Base isolates every state path. The fake runner answers the GETs of the commit,
the repository and the compare; nothing else is read or written."""
import json
import subprocess
from unittest import mock

from support.fake_hubs import LEDGER_HEAD
from support.loader import sb

SHA = "9" * 40
OFF = "b" * 40      # a commit that is not on the default branch
TIP = "4" * 40     # the default branch tip itself, with trailers
BARE = "c" * 40     # on the default branch, no trailers
HALF = "d" * 40     # only one of the two trailers
T = sb.LAND_PROOF_TRAILERS   # the fixtures are built from the one table the code reads
LAND_MSG = "Add the thing\n\nbody text\n\n" + "".join("%s: ok\n" % t for t in T)
MESSAGES = {SHA: LAND_MSG, TIP: LAND_MSG, OFF: LAND_MSG, BARE: "Add it\n\nno trailers here\n", HALF: "Add it\n\n%s: ok\n" % T[0]}
AHEAD = "8" * 40    # a descendant of the default branch's tip
MESSAGES[AHEAD] = LAND_MSG
MINE = "7" * 40     # landed by the agent `tst`, who must not judge it
MESSAGES[MINE] = LAND_MSG
SPOOF = "6" * 40    # the trailer lines sit in prose, not in a trailer block
MESSAGES[SPOOF] = "Add it\n\nThe lines are " + " and ".join("%s: x" % t for t in T) + "\n"
COMMITTED = "5" * 40   # committed by `tst` (the author has no GitHub account)
MESSAGES[COMMITTED] = LAND_MSG
ON_BRANCH = {SHA: "behind", TIP: "identical", BARE: "identical", HALF: "behind", OFF: "diverged", AHEAD: "ahead", MINE: "behind", SPOOF: "behind", COMMITTED: "behind"}
AUTHORS = {MINE: "tst"}
COMMITTERS = {COMMITTED: "tst"}


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
            self.branch = "main"
            self.failing = {}      # a path fragment -> the stderr of a failing read

        def land_route(self, argv, **kwargs):
            if len(argv) > 2 and argv[1] == "api" and argv[2].startswith("repos/acme/land"):
                path = argv[2]
                self.reads.append(path)
                for fragment, text in self.failing.items():
                    if (path == fragment) if fragment.startswith("repos/") else (fragment in path):
                        return subprocess.CompletedProcess(argv, 1, "", text)
                sha = path.rsplit("/", 1)[-1].split("...")[-1]
                if path.endswith("repos/acme/land"):
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"default_branch": self.branch}), "")
                if "/compare/" in path and sha in ON_BRANCH:
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"status": ON_BRANCH[sha]}), "")
                if "/commits/" in path and sha in MESSAGES:
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"sha": sha, "commit": {"message": MESSAGES[sha]}, "author": {"login": AUTHORS[sha]} if sha in AUTHORS else None, "committer": {"login": COMMITTERS.get(sha, "someone")}}), "")
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
            self.assertTrue(all(status == "n/a" for gate, status in rows.items() if gate not in sb.LANDED_GATES))
            self.assertEqual(rows["G4"], "n/a")

        def test_S3_a_sha_without_a_trailer_or_off_the_default_branch_is_refused_and_nothing_is_recorded(self):
            cases = ((BARE, ["no trailer"] + list(T)), (HALF, ["no trailer"] + list(T[1:])), (SPOOF, ["no trailer"] + list(T)), (OFF, ["not on the default branch"]),
                     (AHEAD, ["not on the default branch"]), ("e" * 40, ["sha unknown"]))
            for sha, words in cases:
                with self.subTest(sha=sha[:4]):
                    for run in (self.prove_at, self.verdict_at):
                        code, out, err = run(self.landed(sha))
                        self.assertNotEqual(code, 0)
                        for word in words:
                            self.assertIn(word, err)
                        self.assertIn(sha[:12], err)
            self.assertEqual(self.ledger(), [])

        def test_S3_a_read_that_fails_is_not_called_an_unknown_sha_and_nothing_is_recorded(self):
            for fragment, words in (("/commits/", "could not be read"), ("/compare/", "default branch"), ("repos/acme/land", "default branch")):
                with self.subTest(fragment=fragment):
                    self.failing = {fragment: "gh: Bad Gateway (HTTP 502)"}
                    code, out, err = self.prove_at(self.landed())
                    self.assertNotEqual(code, 0)
                    self.assertIn(words, err)
                    self.assertNotIn("sha unknown", err)
            self.failing = {"/commits/": "gh: Bad Gateway (HTTP 502)\n{\"message\": \"Not Found (HTTP 404) upstream\"}"}
            code, out, err = self.prove_at(self.landed())
            self.assertIn("could not be read", err)
            self.assertNotIn("sha unknown", err)
            self.assertEqual(self.ledger(), [])

        def test_S3_a_head_that_is_not_the_sha_and_a_second_plan_are_refused(self):
            ref = self.landed()
            code, out, err = self.run_as("coder", "prove", "S1", "--pr", ref, "--evidence", "ran: ok", "--head", SHA[:10], *self.PLAN)
            self.assertNotEqual(code, 0)
            self.assertIn("landed sha", err)
            self.assertEqual(self.ledger(), [])
            self.assertEqual(self.prove_at(ref)[0], 0)
            code, out, err = self.run_as("coder", "prove", "S2", "--pr", ref, "--evidence", "ran: ok", "--plan", "acme/app#8")
            self.assertNotEqual(code, 0)
            self.assertIn("acme/app#8", err)
            self.assertEqual([r["scenario"] for r in self.ledger()], ["S1"])

        def test_S3_the_default_branch_tip_itself_is_a_subject(self):
            self.assertEqual(self.prove_at(self.landed(TIP))[0], 0)
            self.assertEqual([r["head"] for r in self.ledger()], [TIP])

        def test_S3_the_spelling_of_the_repository_does_not_make_another_subject(self):
            self.assertEqual(self.prove_at("Acme/Land@" + SHA)[0], 0)
            self.assertEqual({r["pr"] for r in self.ledger()}, {self.landed()})

        def test_S3_the_writer_of_the_commit_may_not_judge_it_and_all_is_at_the_sha(self):
            ref = self.landed(MINE)
            self.assertEqual(self.prove_at(ref)[0], 0)
            code, out, err = self.verdict_at(ref)
            self.assertNotEqual(code, 0)
            self.assertEqual([r["kind"] for r in self.ledger()], ["proof"])
            code, out, err = self.run_as("rev", "verdict", "--all", "--pr", ref, "--as", "reviewer", "--result", "pass", "--evidence", "ran it", "--head", MINE, *self.PLAN)
            self.assertEqual(code, 0, err)
            self.assertEqual({r["head"] for r in self.ledger()}, {MINE})

        def test_S3_a_committer_with_no_linked_author_may_not_judge_the_commit(self):
            ref = self.landed(COMMITTED)
            self.assertEqual(self.prove_at(ref)[0], 0)
            self.assertNotEqual(self.verdict_at(ref)[0], 0)
            self.assertEqual([r["kind"] for r in self.ledger()], ["proof"])

        def test_S3_the_repository_is_lower_cased_at_the_reader(self):
            self.assertEqual(sb.landed_ref("Acme/Land@" + SHA), ("acme/land", SHA))
            self.assertIsNone(sb.landed_ref("acme/land@" + SHA.upper()[:-1]))
            self.assertIsNone(sb.landed_ref("../land@" + SHA))

        def test_S3_a_malformed_landed_reference_is_refused_with_its_form(self):
            code, out, err = self.prove_at("acme/land@" + SHA[:7])
            self.assertNotEqual(code, 0)
            self.assertIn("a landed subject is", err)
            self.assertEqual((self.ledger(), self.reads), ([], []))

        def test_S3_an_excluded_repository_is_refused_before_any_read(self):
            self.extra_config = dict(self.extra_config, excluded_repos=["acme/land"])
            self.configure()
            for run in (self.prove_at, self.verdict_at):
                code, out, err = run(self.landed())
                self.assertNotEqual(code, 0)
                self.assertIn("excluded", err)
            self.assertEqual((self.ledger(), self.reads), ([], []))

        def test_S2_the_default_branch_name_is_quoted_in_the_compare(self):
            self.branch = "rel/1"
            self.assertEqual(self.prove_at(self.landed())[0], 0)
            self.assertIn("repos/acme/land/compare/rel%2F1..." + SHA, self.reads)

        def test_S2_a_landed_commit_whose_scenarios_are_all_proven_and_judged_reads_ready(self):
            ref = self.landed()
            code, out, err = self.run_as("coder", "gates", ref, "--json", *self.PLAN)
            self.assertEqual(code, 1, "ready is false while a scenario is open")
            self.all_proven_at(ref)
            code, out, err = self.run_as("coder", "gates", ref, "--json", *self.PLAN)
            report = json.loads(out)
            self.assertEqual(code, 0, out)
            self.assertTrue(report["ready"])
            rows = {r["gate"]: r["status"] for r in report["gates"]}
            self.assertEqual(rows["G1"], "ok")
            self.assertTrue(all(status in ("ok", "n/a", "untracked") for status in rows.values()))

        def all_proven_at(self, ref):
            for sid in ("S1", "S2", "S3", "U1"):
                self.assertEqual(self.prove_at(ref, sid)[0], 0)
            for who, sid, ev in (("cibot", "S1", "ci"), ("tst", "S2", "tester"), ("rev", "S3", "reviewer"), ("cibot", "U1", "ci")):
                code, out, err = self.verdict_at(ref, who, sid, ev)
                self.assertEqual(code, 0, (who, sid, err))

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

        def test_S5_control_with_the_landed_path_off_today_s_code_refuses_the_sha_subject(self):
            ref = self.landed()
            original = sb._ledger_context
            with mock.patch.object(sb, "_ledger_context", lambda *a, **k: original(*a, **dict(k, landed=False))):   # the commands as they were: a subject is a pull request or nothing
                for run in (lambda: self.prove_at(ref), lambda: self.run_as("coder", "gates", ref, *self.PLAN)):
                    code, out, err = run()
                    self.assertNotEqual(code, 0)
                    self.assertIn(ref, err)   # the refusal names the subject it would not take
                    self.assertIn("not a pull request", err)   # the old reader's words (parse_pr_ref), not the landed path's own
            self.assertEqual((self.ledger(), [r for r in self.reads if "/commits/" in r]), ([], []))
            self.assertEqual(self.prove_at(ref)[0], 0, "the same call with the landed path records")
            self.assertEqual(len(self.ledger()), 1)

        def test_U1_the_trailer_match_is_one_function_with_the_stated_form(self):
            missing = sb.land_trailers_missing
            block = lambda lines: "subject\n\n" + "\n".join(lines) + "\n"
            self.assertEqual(missing(LAND_MSG), [])
            self.assertEqual(missing(block([t.upper() + ": x" for t in T])), [])
            self.assertEqual(missing(block([t.lower() + ":y" for t in T] + ["Signed-off-by: a <a@b>"])), [])
            self.assertEqual(missing(block([T[0] + ":"] + [t + ": ok" for t in T[1:]])), [T[0]], "an empty value is no trailer")
            self.assertEqual(missing(" ".join(T) + "\n" + "\n".join(t + ": a" for t in T)), list(T), "a subject paragraph alone is no trailer block")
            self.assertEqual(missing(block([t + ": a" for t in T]) + "\ntext after\n"), list(T), "only the last paragraph counts")
            self.assertEqual(missing(block(["prose"] + [t + ": a" for t in T])), list(T), "prose in the block makes it no block")
            self.assertEqual(missing(None), list(T))
    return TestLandedSubject
