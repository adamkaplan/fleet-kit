"""The `ci-failures` tests of #1968. build() is called by bin/test-switchboard, which holds HandoffCase (its fakes: the home, the config, the runner)."""
import json
import re
import subprocess
from support.loader import captured, sb
from support.scenario_roles import FAKE_GH

REPO = "acme/web"
HEAD = "a" * 40
TOKEN = "ghp_" + "Q" * 36


def build(HandoffCase):
    class TestCiFailures(HandoffCase):
        """#1968: one plain line per failed job, read-only, bounded, scrubbed."""

        def setUp(self):
            super().setUp()
            self.gh = self.serve
            self.requests = []
            self.jobs = {}      # run id -> list of job dicts
            self.logs = {}      # job id -> text, or None for a 403
            self.runs = [{"id": 11, "workflow_id": 1, "name": "ci", "status": "completed", "conclusion": "failure", "head_sha": HEAD,
                          "created_at": "2026-11-02T10:00:00Z", "pull_requests": []}]

        def serve(self, argv, **kwargs):
            path = argv[-1]
            self.requests.append(("GET" if argv[1] == "api" and "-X" not in argv else "OTHER", path, argv[0]))
            if path == "repos/%s/pulls/7" % REPO:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"head": {"sha": HEAD}}), "")
            if "/actions/runs?" in path:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"workflow_runs": self.runs}), "")
            m = re.search(r"/actions/runs/(\d+)/jobs", path)
            if m:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"jobs": self.jobs[int(m.group(1))]}), "")
            m = re.search(r"/actions/jobs/(\d+)/logs", path)
            if m:
                text = self.logs.get(int(m.group(1)))
                if text is None:
                    return subprocess.CompletedProcess(argv, 1, "", "HTTP 403")
                return subprocess.CompletedProcess(argv, 0, text, "")
            return subprocess.CompletedProcess(argv, 1, "", "HTTP 404")

        def job(self, jid, name, conclusion="failure", step="Run tests", status="completed"):
            return {"id": jid, "name": name, "status": status, "conclusion": conclusion,
                    "steps": [{"name": "Set up", "conclusion": "success"}, {"name": step, "conclusion": conclusion}]}

        def fail_log(self, *names):
            return "\n".join("2026-11-02T10:01:00.000Z FAIL: %s (m.C)" % n for n in names) + "\nRan 9 tests\n"

        def ci(self):
            with captured() as (out, err):
                code = sb.main(["ci-failures", "%s#7" % REPO], runner=self.route, now=self.world.clock.now, socket_call=self.world.herdr.socket)
            return code, out.getvalue(), err.getvalue()

        def lines(self):
            code, out, err = self.ci()
            self.assertEqual(code, 0, err)
            return out.splitlines()

        def two_failed(self):
            self.jobs[11] = [self.job(1, "unit-a"), self.job(2, "unit-b", step="Run suite"), self.job(3, "lint", "success")]
            self.logs = {1: self.fail_log("test_one", "test_two"), 2: self.fail_log("test_three")}

        def check_two_failed(self, lines):
            self.assertFalse([l for l in lines if "lint" in l], "a passing job was printed as failed")
            self.assertEqual(len(lines), 2, lines)
            self.assertIn("job unit-a, step Run tests: tests: test_one (m.C); test_two (m.C)", lines[0])
            self.assertIn("job unit-b, step Run suite: tests: test_three (m.C)", lines[1])

        def test_s2_two_failed_jobs_print_one_line_each_and_the_passing_job_none(self):
            self.two_failed()
            self.check_two_failed(self.lines())

        def test_s3_a_run_still_going_says_so_beside_the_concluded_failure(self):
            self.runs[0]["status"] = "in_progress"
            self.jobs[11] = [self.job(1, "unit-a"), self.job(2, "unit-b", conclusion=None, status="in_progress")]
            self.logs = {1: self.fail_log("test_one")}
            lines = self.lines()
            self.assertEqual([l for l in lines if "still going" in l].__len__(), 1, lines)
            self.assertEqual([l for l in lines if l.startswith("FAILED job")].__len__(), 1, lines)

        def test_s4_all_green_says_no_failure(self):
            self.jobs[11] = [self.job(1, "a", "success"), self.job(2, "b", "skipped")]
            lines = self.lines()
            self.assertEqual(len(lines), 1, lines)
            self.assertIn("no failure", lines[0])

        def test_s5_an_unreadable_log_does_not_stop_the_next_job(self):
            self.jobs[11] = [self.job(1, "unit-a"), self.job(2, "unit-b")]
            self.logs = {1: None, 2: self.fail_log("test_three")}
            lines = self.lines()
            self.assertEqual(len(lines), 2, lines)
            self.assertIn("unit-a, step Run tests: log not readable", lines[0])
            self.assertIn("tests: test_three (m.C)", lines[1])

        def test_s6_a_token_never_appears_and_the_names_are_cut(self):
            self.jobs[11] = [self.job(1, "unit-a")]
            self.logs = {1: self.fail_log("test_%s_%d" % (TOKEN, 0), *["test_%d" % i for i in range(1, 500)])}
            code, out, err = self.ci()
            self.assertEqual(out.count(TOKEN), 0, out)
            self.assertEqual(len(out.splitlines()), 1)
            self.assertEqual(out.count("test_"), 10 + 0, out)
            self.assertIn("(+490 more)", out)

        def test_s7_the_output_pastes_into_a_brief_and_the_skill_names_the_command(self):
            self.two_failed()
            _, out, _ = self.ci()
            self.assertNotIn("\x1b", out)
            self.assertTrue(all(len(l) <= 400 for l in out.splitlines()))
            skill = (sb.Path(sb.__file__).resolve().parent.parent / "skills" / "fleet-coordination" / "SKILL.md").read_text().splitlines()
            self.assertEqual(len([l for l in skill if "fleet-switchboard ci-failures" in l]), 1)

        def test_s8_and_s11_six_counted_get_requests_through_the_lane_account(self):
            self.runs.append(dict(self.runs[0], id=12, workflow_id=2, name="lint"))
            self.jobs[11] = [self.job(1, "unit-a")]
            self.jobs[12] = [self.job(2, "lint-a")]
            self.logs = {1: self.fail_log("t1"), 2: self.fail_log("t2")}
            self.lines()
            self.assertEqual(len(self.requests), 6, self.requests)
            self.assertLess(len(self.requests), 3 + 2 + 5)
            self.assertEqual({r[0] for r in self.requests}, {"GET"})
            self.assertEqual({r[2] for r in self.requests}, {FAKE_GH})

        def test_s9_the_check_of_s2_fails_on_a_mutant_that_prints_the_passing_job(self):
            self.two_failed()
            self.check_two_failed(self.lines())    # the control: the unmutated command passes first
            real = sb.ci_failure_lines

            def mutant(reading, gh, repo, number, clean):
                return real(reading, gh, repo, number, clean) + ["FAILED job lint, step Run tests: no test names found"]
            sb.ci_failure_lines = mutant
            self.addCleanup(setattr, sb, "ci_failure_lines", real)
            try:
                self.check_two_failed(self.lines())
            except AssertionError as err:
                self.assertIn("a passing job was printed as failed", str(err))
                return
            self.fail("the mutant was not caught")

        def test_s12_only_a_failed_conclusion_is_called_failed(self):
            self.jobs[11] = [self.job(1, "a", "success"), self.job(2, "b", "skipped"), self.job(3, "c", "neutral"), self.job(4, "d", None, status="completed"),
                             self.job(5, "e", "cancelled")]
            lines = self.lines()
            self.assertEqual([l for l in lines if l.startswith("FAILED")], [], lines)
            self.assertEqual(len([l for l in lines if "unknown" in l and l.startswith("job d")]), 1, lines)

        def test_s14_at_most_five_logs_are_read_for_eight_failed_jobs(self):
            self.jobs[11] = [self.job(i, "j%d" % i) for i in range(1, 9)]
            self.logs = {i: self.fail_log("t%d" % i) for i in range(1, 9)}
            lines = self.lines()
            self.assertEqual(len([r for r in self.requests if "/logs" in r[1]]), 5)
            self.assertEqual(len(lines), 8)

        def test_s13_the_command_adds_no_flat_module(self):
            self.assertTrue(hasattr(sb, "add_ci_failures_parsers") and hasattr(sb, "cmd_ci_failures"))

    return TestCiFailures
