"""#1962: `pr-state` names the jobs of a run in progress (the failed job with its failed step, the jobs still running), read once per run in progress.

REAL: the recorder's step (`pr_state_jobs` inside `ci_record_pass`), the view, the line printer and `pr_reading.read_jobs`.
FAKE: the `gh` runner and the jobs answers (the route below). The two mutants are textual replacements applied to a copy of the real source, each asserted applied
(`text.count(old) == 1`); the unmutated copy is run first as the control. The fixtures are those of bin/test-switchboard (HOST, set by its loader block)."""
import json
import re
import subprocess
from pathlib import Path
from unittest import mock

HOST = None   # set by bin/test-switchboard
ROOT = Path(__file__).resolve().parents[2]
ANCHOR = "pr-state names the failed job of a run in progress from the jobs answer"
URL = "https://github.com/acme/app/actions/runs/%d/job/1"


def job(name, conclusion, steps=()):
    return {"name": name, "status": "completed" if conclusion else "in_progress", "conclusion": conclusion,
            "steps": [{"name": n, "status": "completed", "conclusion": c} for n, c in steps]}


FIVE = [job("unit", "success"), job("lint", "failure", [("checkout", "success"), ("run ruff", "failure")]), job("mac", None), job("win", None), job("docs", "success")]
CLEAN = [job("unit", "success"), job("mac", None)]


def load():
    host = HOST
    LedgerBase, TestCiRecordPass, FAKE_GH, FakeHub, sb = host.LedgerBase, host.TestCiRecordPass, host.FAKE_GH, host.FakeHub, host.sb

    class TestPrStateJobs(LedgerBase):
        """#1962: the jobs of a run in progress, on fakes (see the module's text for what is real)."""

        extra_config = {"plan_gates": {"repos": {"acme/app": {"mode": "shadow"}}}}
        listed, engine, run_pass, go, pr_state = (TestCiRecordPass.listed, TestCiRecordPass.engine, TestCiRecordPass.run_pass, TestCiRecordPass.go, TestCiRecordPass.pr_state)

        def setUp(self):
            super().setUp()
            self.configure(evaluators={"ci": ["switchboard"], "reviewer": ["rev"], "tester": ["tst"]})
            self.hub = FakeHub({"acme/app": [self.listed(9)]})
            self.t = 100000.0
            self.jobs, self.asked, self.jobs_down = {}, [], False
            runs = sb.load_module("ci_runs")   # the identity of a run is read through this hook (the workflow id of the run), a FAKE here: it is not a jobs read
            patch = mock.patch.object(runs, "READER", lambda repo, run: (int(run), "push"))
            patch.start()
            self.addCleanup(patch.stop)
            runs._CACHE.clear()
            runs._FAILED.clear()

        def route(self, argv, **kwargs):
            found = re.search(r"/actions/runs/(\d+)/(jobs|logs)", argv[2]) if argv[0] == FAKE_GH and argv[1] == "api" else None
            if found:
                self.asked.append(argv[2])
                if found.group(2) != "jobs" or self.jobs_down or int(found.group(1)) not in self.jobs:
                    return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")
                return subprocess.CompletedProcess(argv, 0, json.dumps({"jobs": self.jobs[int(found.group(1))]}), "")
            return super().route(argv, **kwargs)

        def setup_world(self, jobs, completed_run=True):
            self.jobs = {100: jobs}
            rollup = [{"name": "build", "workflowName": "ci", "status": "IN_PROGRESS", "conclusion": "", "detailsUrl": URL % 100}]
            if completed_run:
                rollup.append({"name": "lint", "workflowName": "lint", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": URL % 101})
                self.jobs[101] = [job("x", "success")]
            self.pr["statusCheckRollup"] = rollup
            self.go()

        def job_lines(self, text):
            return [l for l in text.splitlines() if l.startswith("  job ")]

        def check_world(self):
            """The scenarios S2 and S3, as assertions over the real recorder and command."""
            self.setup_world(FIVE)
            code, out, calls = self.pr_state(self.PR)
            lines = self.job_lines(out)
            self.assertEqual(len(lines), 3, "%s: 1 failed + 2 running = 3 lines, not %r" % (ANCHOR, lines))
            self.assertTrue(any("lint: failure (failed step: run ruff)" in l for l in lines), "%s: the failed step is named: %r" % (ANCHOR, lines))
            self.assertEqual(len([l for l in lines if l.endswith(": running")]), 2, ANCHOR)
            self.assertEqual(calls, [], "%s: the command makes no GitHub call" % ANCHOR)
            self.assertEqual([a for a in self.asked if "/runs/100/jobs" in a].__len__(), 1, "%s: one jobs request for the run in progress" % ANCHOR)
            self.assertEqual([a for a in self.asked if "/runs/101/" in a], [], "%s: no request for a completed run" % ANCHOR)
            self.assertEqual(len(self.asked), 1, "%s: one request in all, not %r" % (ANCHOR, self.asked))
            view = json.loads(self.pr_state(self.PR, "--json")[1])
            self.assertEqual(len(view["jobs"]), 3, ANCHOR)

        def test_pr_state_names_the_failed_job_of_a_run_in_progress_from_the_jobs_answer(self):
            self.check_world()

        def test_a_run_with_no_failed_job_prints_no_job_line_and_the_text_is_the_same_as_without_jobs(self):
            self.setup_world(CLEAN, completed_run=False)
            code, out, calls = self.pr_state(self.PR)
            self.assertEqual((self.job_lines(out), len(self.asked)), ([], 1))
            self.assertEqual(len(out.strip().splitlines()), 1, "a head with no failed job prints its one line")
            self.assertNotIn("jobs", json.loads(self.pr_state(self.PR, "--json")[1]))

        def test_a_jobs_answer_that_cannot_be_read_says_so_in_one_line(self):
            self.jobs_down = True
            self.setup_world(FIVE)
            code, out, calls = self.pr_state(self.PR)
            self.assertEqual([l for l in out.splitlines() if l.startswith("  jobs: ")], ["  jobs: jobs not readable"])
            self.assertEqual(self.job_lines(out), [])

        def test_a_completed_run_is_read_for_no_jobs(self):
            self.jobs = {101: [job("x", "failure")]}
            self.pr["statusCheckRollup"] = [{"name": "lint", "workflowName": "lint", "status": "COMPLETED", "conclusion": "FAILURE", "detailsUrl": URL % 101}]
            self.go()
            self.assertEqual(self.asked, [])
            self.assertEqual(self.job_lines(self.pr_state(self.PR)[1]), [])

        def test_the_jobs_stay_when_the_list_is_read_again_and_go_with_a_moved_head(self):
            self.setup_world(FIVE)
            self.go(advance=sb.CI_RECORD_LIST_TTL + 1)
            self.assertEqual(len(self.job_lines(self.pr_state(self.PR)[1])), 3)
            view = sb.pr_state_view(self.PR, {"written_at": self.t, "prs": {self.PR: {"state": "open", "head": "b" * 40, "examined_head": "b" * 40, "jobs_head": "a" * 40,
                                    "jobs": [{"name": "lint", "conclusion": "failure"}], "checks": [], "at": self.t}}}, [], self.t)
            self.assertNotIn("jobs", view)

        # -- the mutants: a textual replacement in a copy of the real source, the control first
        def mutant(self, path, old, new, name, whole=False):
            text = (ROOT / "bin" / path).read_text()
            self.assertEqual(text.count(old), 1, "the mutation must apply")
            return text.replace(old, new)

        def function_of(self, text, name):
            found = re.search(r"^def %s\(.*?(?=^\S)" % name, text, re.S | re.M)
            self.assertIsNotNone(found)
            return found.group(0)

        def run_with(self, source, name, owner):
            namespace = dict(vars(owner))
            exec(compile(source, "<mutant %s>" % name, "exec"), namespace)
            return mock.patch.object(owner, name, namespace[name])

        def reading_with(self, old, new):
            reading = sb.load_module("pr_reading")
            text = self.function_of((ROOT / "bin" / "pr_reading.py").read_text(), "read_jobs")
            if old:
                self.assertEqual(text.count(old), 1, "the mutation must apply")
                text = text.replace(old, new)
            return self.run_with(text, "read_jobs", reading)

        def recorder_with(self, old, new):
            text = self.function_of((ROOT / "bin" / "fleet-switchboard").read_text(), "pr_state_jobs")
            if old:
                self.assertEqual(text.count(old), 1, "the mutation must apply")
                text = text.replace(old, new)
            return self.run_with(text, "pr_state_jobs", sb)

        def fails_with_the_anchor(self, patched):
            with patched:
                with self.assertRaises(AssertionError) as caught:
                    self.check_world()
            self.assertIn(ANCHOR, str(caught.exception))

        def fresh(self):
            self.__dict__.pop("engine_cached", None)
            self.setUp()

        def test_the_controls_pass_on_the_unmutated_copies(self):
            with self.reading_with("", ""):
                self.check_world()
            self.fresh()
            with self.recorder_with("", ""):
                self.check_world()

        def test_mutant_1_the_log_path_instead_of_the_jobs_path_fails_the_check(self):
            with self.reading_with("", ""):
                self.check_world()   # the control first
            self.fresh()
            self.fails_with_the_anchor(self.reading_with("actions/runs/%s/jobs?filter=latest&per_page=100", "actions/runs/%s/logs?filter=latest&per_page=100"))

        def test_mutant_2_every_runs_jobs_read_fails_the_check(self):
            with self.recorder_with("", ""):
                self.check_world()   # the control first
            self.fresh()
            self.fails_with_the_anchor(self.recorder_with('check.get("status") == "IN_PROGRESS" else None', "True else None"))

    return TestPrStateJobs
