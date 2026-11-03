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

        # -- the printed text of a head with no failed run is the text without this change (U1)
        # OLD_TEXT is a LITERAL: the exact string the printer of origin/integration (before this branch; `git archive origin/integration`, a copy of bin/fleet-switchboard)
        # printed through its own `pr_state_line` for the view of this fixture entry (a pending head, one in-progress check "build", no jobs keys). It is not derived from the new printer.
        OLD_TEXT = 'acme/app#9; open; head aaaaaaaa; CI pending examined 0s ago (build: in_progress); gates NOT (G1 S1: proof (`prove S1`; the ci judges it, a prover records it); ci verdict (`verdict S1 --as ci`; only the ci may record it); G1 S2: proof (`prove S2`; the tester judges it, a prover records it); tester verdict (`verdict S2 --as tester`; only the tester may record it); G1 S3: proof (`prove S3`; the reviewer judges it, a prover records it); reviewer verdict (`verdict S3 --as reviewer`; only the reviewer may record it); G1 U1: proof (`prove U1`; the ci judges it, a prover records it); ci verdict (`verdict U1 --as ci`; only the ci may record it)) [examined 0s ago]; reviews none recorded at this head; labels none; list read 0s ago'

        def printed_for_the_clean_head(self):
            self.setup_world(CLEAN, completed_run=False)
            self.assertEqual(len(self.asked), 1, "the recorder step ran on the pending head")
            return self.pr_state(self.PR)[1]

        def test_a_head_with_no_failed_run_prints_the_text_it_printed_without_the_jobs_keys(self):
            out = self.printed_for_the_clean_head()
            self.assertEqual(out.rstrip("\n"), self.OLD_TEXT, "byte for byte the old printer's text")

        def test_mutant_5_a_printer_that_adds_a_line_for_a_head_without_jobs_fails_the_literal(self):
            # control first: the unmutated printer gives the literal
            self.assertEqual(self.printed_for_the_clean_head().rstrip("\n"), self.OLD_TEXT)
            self.fresh()
            text = self.function_of((ROOT / "bin" / "fleet-switchboard").read_text(), "pr_state_line")
            old = 'if view.get("jobs") is not None:'
            self.assertEqual(text.count(old), 1, "the mutation must apply")
            old2 = 'for j in view["jobs"] if'
            self.assertEqual(text.count(old2), 1, "the mutation must apply")
            text = text.replace(old, "if True:").replace(old2, 'for j in (view.get("jobs") or [{"name": "extra", "conclusion": "none"}]) if')
            with self.run_with(text, "pr_state_line", sb):
                out = self.printed_for_the_clean_head()
            self.assertIn("  job extra: none", out, "the mutant printer added its line")
            self.assertNotEqual(out.rstrip("\n"), self.OLD_TEXT)

        # -- the caps, from the constants
        def many_runs_world(self, runs, failed_per_run):
            self.jobs = {200 + i: [job("j%d" % n, "failure", [("s", "failure")]) for n in range(failed_per_run)] for i in range(runs)}
            self.pr["statusCheckRollup"] = [{"name": "b%d" % i, "workflowName": "w%d" % i, "status": "IN_PROGRESS", "conclusion": "", "detailsUrl": URL % (200 + i)} for i in range(runs)]
            self.go()

        def check_caps(self, lines_anchor, requests_anchor):
            self.many_runs_world(sb.PR_STATE_JOBS_RUNS + 2, sb.PR_STATE_JOBS_MAX + 2)
            lines = self.job_lines(self.pr_state(self.PR)[1])
            self.assertEqual(len(lines), sb.PR_STATE_JOBS_MAX, "%s: not %d lines" % (lines_anchor, len(lines)))
            kept = sb.pr_state_read(self.world.state, self.t)[0]["prs"][self.PR]["jobs"]   # the view cuts at 10 too: the cap of the recorder is read in the entry
            self.assertEqual(len(kept), sb.PR_STATE_JOBS_MAX, "%s: %d lines kept in the entry" % (lines_anchor, len(kept)))
            requests = [a for a in self.asked if "/jobs" in a]
            self.assertEqual(len(requests), sb.PR_STATE_JOBS_RUNS, "%s: not %d requests" % (requests_anchor, len(requests)))

        LINES, REQUESTS = "the cap of lines of jobs holds", "the cap of runs read holds"

        def test_the_caps_of_lines_and_of_runs_hold(self):
            self.check_caps(self.LINES, self.REQUESTS)

        def test_mutant_3_no_cap_of_lines_fails_the_check(self):
            with self.recorder_with("", ""):
                self.check_caps(self.LINES, self.REQUESTS)   # the control first
            self.fresh()
            with self.recorder_with("jobs[:PR_STATE_JOBS_MAX]", "jobs"):
                with self.assertRaises(AssertionError) as caught:
                    self.check_caps(self.LINES, self.REQUESTS)
            self.assertIn(self.LINES, str(caught.exception))

        def test_mutant_4_no_cap_of_runs_fails_the_check(self):
            with self.recorder_with("", ""):
                self.check_caps(self.LINES, self.REQUESTS)   # the control first
            self.fresh()
            with self.recorder_with("runs[:PR_STATE_JOBS_RUNS]", "runs"):
                with self.assertRaises(AssertionError) as caught:
                    self.check_caps(self.LINES, self.REQUESTS)
            self.assertIn(self.REQUESTS, str(caught.exception))

        # -- the real run-identity reader, counted (the only test that leaves ci_runs.READER in place)
        def test_the_real_identity_reader_and_the_jobs_reads_are_counted_together(self):
            """REAL reader (ci_runs.READER, the gh run-identity read), FAKE gh answer: the identity answer below is a fake dict, and so are the jobs answers."""
            runs = sb.load_module("ci_runs")
            identity = []
            real_run = subprocess.run

            def fake_run(argv, *args, **kwargs):
                found = re.search(r"/actions/runs/(\d+)$", str(argv[2])) if isinstance(argv, list) and len(argv) > 2 and argv[0] == FAKE_GH else None
                if not found:
                    return real_run(argv, *args, **kwargs)
                identity.append(int(found.group(1)))
                return subprocess.CompletedProcess(argv, 0, json.dumps({"w": int(found.group(1)), "e": "push", "p": ".github/workflows/ci.yml"}), "")
            patch = mock.patch.object(runs, "READER", None)   # the real reader; the daemon's config load sets runs.GH to the fake gh, whose spawn is answered below
            patch.start()
            self.addCleanup(patch.stop)
            patch = mock.patch("subprocess.run", fake_run)
            patch.start()
            self.addCleanup(patch.stop)
            runs._CACHE.clear()
            runs._FAILED.clear()
            self.setup_world(FIVE)
            in_progress = [a for a in self.asked if "/jobs" in a]
            rollup_runs = {100, 101}   # the runs of the rollup: one in progress, one completed
            self.assertEqual(len(in_progress), 1, "the jobs request per run in progress")
            self.assertEqual(sorted(set(identity)), sorted(rollup_runs), "newest() reads the identity of each run of the rollup")
            self.assertEqual(len(identity), len(set(identity)), "each identity is read once: the later passes are served from the cache")
            self.assertEqual(len(in_progress) + len(identity), 3, "the total GitHub requests for one pending head")
            # a second examination of the same head, past the retry gate: the jobs request again (1), the run identities from the cache (0 more)
            self.go(advance=sb.CI_RECORD_LIST_TTL + 1)
            self.assertEqual(len([a for a in self.asked if "/jobs" in a]) - len(in_progress), 1, "the second examination: the jobs request again, once")
            self.assertEqual(len(identity), 2, "the second examination: 0 more identity reads, served from the cache")

    return TestPrStateJobs
