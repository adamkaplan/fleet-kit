"""`ci-queue` (#1961): the Actions queue measured from a fixture of runs and jobs. The command is inlined in bin/switchboard/reminders.py; bin/test-switchboard
passes HandoffCase to build() (the fixtures of that file), and the class reads `sb`, the loaded entry, never sys.modules[__name__]."""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from support.loader import captured, sb

T0 = datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp()
NOW = T0 + 24 * 3600
REPO = Path(__file__).resolve().parents[2]
GATE = ("test (ubuntu-latest)", "test-cloud (ubuntu-latest)", "test (macos-latest)", "test-cloud (macos-latest)")
UBU, CLOUD, MAC, MACC = GATE
SHIFT = 775   # minutes: the busy hour sits at 23:00 of the window
TOKEN = "ghp_SECRETTOKENVALUE0123456789"


def at(minute):
    return None if minute is None else datetime.fromtimestamp(T0 + minute * 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def job(name, created, started, completed):
    return {"name": name, "created_at": at(created), "started_at": at(started), "completed_at": at(completed)}


def run(ident, workflow, event, sha, created):
    return {"id": ident, "name": workflow, "event": event, "head_sha": sha, "created_at": at(created)}


def fixture():
    """Six runs: waits 0, 120 and 300 s; jobs 3, 5, 5, 9, 5, 3; the test workflow holds 412 job-minutes; 7 jobs are queued at once (a quarter before
    the end of the window, where S15 adds a run) and 20 run at once. Runs 2 to 4 are shifted by SHIFT minutes."""
    k = SHIFT
    runs = [run(1, "test", "pull_request", "aa", 1395), run(2, "test", "pull_request", "bb", 598 + k), run(3, "test", "pull_request", "bb", 595 + k),
            run(4, "nightly", "schedule", "cc", 600 + k), run(5, "test", "push", "dd", 1379), run(6, "test", "pull_request", "ee", 1395)]
    jobs = {
        1: [job("plan", 1395, 1395, 1396), job(UBU, 1396, 1396, 1406), job(CLOUD, 1396, 1396, 1416)],
        2: [job("plan", 598 + k, 600 + k, 601 + k), job(UBU, 601 + k, 601 + k, 609 + k), job(CLOUD, 601 + k, 601 + k, 608 + k),
            job(MAC, 601 + k, 610 + k, 623 + k), job(MACC, 601 + k, 612 + k, 623 + k)],
        3: [job("plan", 595 + k, 600 + k, 601 + k), job(UBU, 601 + k, 601 + k, 611 + k), job(CLOUD, 601 + k, 601 + k, 611 + k),
            job(MAC, 601 + k, 620 + k, 665 + k), job(MACC, 601 + k, 620 + k, 665 + k)],
        4: [job("plan", 600 + k, 600 + k, 601 + k)] + [job(n, 601 + k, 601 + k, 631 + k) for n in (UBU, CLOUD, MAC, MACC)]
           + [job(n, 601 + k, 620 + k, 631 + k) for n in (UBU, CLOUD, MAC)] + [job("stale-report", 601 + k, 601 + k, 656 + k)],
        5: [job("plan", 1379, 1379, 1381), job(UBU, 1381, 1381, 1440), job(CLOUD, 1381, 1381, 1439), job(MAC, 1381, 1381, 1421), job(MACC, 1381, 1381, 1421)],
        6: [job("plan", 1395, 1395, 1396), job(UBU, 1396, 1396, 1406), job(CLOUD, 1396, 1396, 1416)],
    }
    return runs, jobs


def build(HandoffCase, FAKE_GH):
    class TestCiQueue(HandoffCase):
        """S2, S3, S5, S6, S7, S11, S15 of #1961 and the pin of cmd_remind (U2)."""

        def measure(self, runs=None, jobs=None, **kwargs):
            base_runs, base_jobs = fixture()
            kwargs.setdefault("gate_names", GATE)
            kwargs.setdefault("docs_only", {3})
            return sb.ci_queue_measure(runs or base_runs, jobs or base_jobs, NOW, 24, **kwargs)

        def test_s2_the_queue_wait_is_created_to_started_and_the_jobs_are_counted_by_run(self):
            report = self.measure()
            self.assertEqual(len(report["runs"]), 6)
            self.assertEqual(sorted({r["wait_seconds"] for r in report["runs"]}), [0, 120, 300])
            self.assertEqual({r["id"]: r["wait_seconds"] for r in report["runs"]}, {1: 0, 2: 120, 3: 300, 4: 0, 5: 0, 6: 0})
            self.assertEqual({r["id"]: r["jobs"] for r in report["runs"]}, {1: 3, 2: 5, 3: 5, 4: 9, 5: 5, 6: 3})
            self.assertEqual([r for r in report["runs"] if r["id"] == 1][0]["job_names"], {"plan": 1, UBU: 1, CLOUD: 1})
            self.assertEqual([r["run_seconds"] for r in report["runs"] if r["id"] == 1], [21 * 60])

        def test_s3_job_minutes_by_workflow_and_the_peak_of_jobs_queued_at_once(self):
            report = self.measure()
            self.assertEqual(report["job_minutes_by_workflow"]["test"], 412)
            self.assertEqual(report["peak_queued"], 7)

        def test_s5_the_limit_is_the_observed_ceiling_and_the_published_one_is_not_readable(self):
            report = self.measure()
            self.assertEqual(report["observed_ceiling"], 20)
            text = "\n".join(sb.ci_queue_lines(report))
            self.assertIn('"observed" ceiling of 20 concurrent jobs', text)
            self.assertIn('published limit: "not readable by a lane"', text)

        def test_s6_each_kind_of_candidate_carries_its_job_minutes_per_day(self):
            found = {c["kind"]: c["job_minutes_per_day"] for c in self.measure()["candidates"]}
            self.assertEqual(found, {"cut": 55, "dedupe": 40, "move": 90})
            self.assertEqual(len(self.measure()["candidates"]), 3)

        def test_s7_only_a_job_no_gate_reads_is_a_cut_candidate(self):
            runs = [run(1, "test", "pull_request", "aa", 0)]
            jobs = {1: [job(UBU, 0, 0, 5), job("stale-report", 0, 0, 7)]}
            cut = [c for c in self.measure(runs, jobs, docs_only=())["candidates"] if c["kind"] == "cut"]
            self.assertEqual([(c["what"], c["job_minutes_per_day"]) for c in cut], [("stale-report", 7)])

        def test_s15_a_run_still_queued_at_the_end_waits_until_now_and_counts_in_the_peak(self):
            runs, jobs = fixture()
            runs.append(run(7, "test", "pull_request", "ff", 23 * 60))
            jobs[7] = [job("plan", 23 * 60, 23 * 60, None) | {"started_at": None}]
            report = self.measure(runs, jobs)
            self.assertEqual({r["id"]: r["wait_seconds"] for r in report["runs"]}[7], 3600)
            self.assertEqual(report["peak_queued"], 8)

        def pure(self, old=None, new=None):
            """The pure measuring functions of reminders.py, compiled from its text (optionally with ONE edit): a mutant of the counting, never of the file."""
            text = (REPO / "bin" / "switchboard" / "reminders.py").read_text()
            body = text[text.index("def _ci_time("):text.index("def ci_queue_lines(")]
            if old is not None:
                self.assertEqual(body.count(old), 1, "the mutant's anchor is not in the code: " + old)
                body = body.replace(old, new)
            space = {"datetime": datetime, "CI_QUEUE_GATE_FEEDERS": sb.CI_QUEUE_GATE_FEEDERS}
            exec(compile(body, "ci_queue_mutant", "exec"), space)
            return space["ci_queue_measure"]

        def probe(self, measure):
            runs, jobs = fixture()
            runs.append(run(7, "test", "pull_request", "ff", 23 * 60))
            jobs[7] = [dict(job("plan", 23 * 60, None, None))]
            report = measure(runs, jobs, NOW, 24, GATE, {3})
            return {r["id"]: r["wait_seconds"] for r in report["runs"]}, report["peak_queued"]

        def test_mutant_control_the_unmutated_text_measures_as_the_class_does(self):
            waits, peak = self.probe(self.pure())
            self.assertEqual((waits[2], waits[3], waits[7], peak), (120, 300, 3600, 8))

        def test_mutant_a_wait_that_ignores_created_is_not_the_queue_wait(self):
            waits, _ = self.probe(self.pure("wait = (first if first is not None else now) - created", "wait = 0"))
            with self.assertRaisesRegex(AssertionError, "^0 != 120$"):
                self.assertEqual(waits[2], 120)

        def test_mutant_a_job_not_started_is_dropped_from_the_peak(self):
            _, peak = self.probe(self.pure("queued.append((_ci_time(job.get(\"created_at\")) or created, job_start))",
                                           "job_start is not None and queued.append((_ci_time(job.get(\"created_at\")) or created, job_start))"))
            with self.assertRaisesRegex(AssertionError, "^7 != 8$"):
                self.assertEqual(peak, 8)

        def pure_lines(self, old=None, new=None):
            """ci_queue_measure and ci_queue_lines compiled from the text, with ONE optional edit (a mutant of the output)."""
            text = (REPO / "bin" / "switchboard" / "reminders.py").read_text()
            body = text[text.index("def _ci_time("):text.index("def ci_queue_get(")]
            if old is not None:
                self.assertEqual(body.count(old), 1, "the mutant's anchor is not in the code: " + old)
                body = body.replace(old, new)
            space = {"datetime": datetime, "CI_QUEUE_GATE_FEEDERS": sb.CI_QUEUE_GATE_FEEDERS}
            exec(compile(body, "ci_queue_mutant", "exec"), space)
            return space

        def text_of(self, space):
            runs, jobs = fixture()
            return "\n".join(space["ci_queue_lines"](space["ci_queue_measure"](runs, jobs, NOW, 24, GATE, {3})))

        def test_mutant_s5_the_inferred_20_printed_as_published_is_caught_control_first(self):
            self.assertIn('"observed" ceiling of 20', self.text_of(self.pure_lines()))   # control
            text = self.text_of(self.pure_lines('runner limit: "observed" ceiling of %d', 'runner limit: published ceiling of %d'))
            with self.assertRaisesRegex(AssertionError, "'\"observed\" ceiling of 20' not found in"):
                self.assertIn('"observed" ceiling of 20', text)

        def test_mutant_s6_a_candidate_without_minutes_is_caught_control_first(self):
            def found(space):
                runs, jobs = fixture()
                return {c["kind"]: c["job_minutes_per_day"] for c in space["ci_queue_measure"](runs, jobs, NOW, 24, GATE, {3})["candidates"]}
            self.assertEqual(found(self.pure_lines()), {"cut": 55, "dedupe": 40, "move": 90})   # control
            mutant = found(self.pure_lines('"what": "%d run(s) on a head that already had a run" % repeat_runs, "job_minutes_per_day": int(round(repeat_minutes * scale))',
                                           '"what": "%d run(s) on a head that already had a run" % repeat_runs, "job_minutes_per_day": 0'))
            with self.assertRaisesRegex(AssertionError, "^\\{'cut': 55, 'dedupe': 0, 'move': 90\\} != \\{'cut': 55, 'dedupe': 40, 'move': 90\\}\\n"):
                self.assertEqual(mutant, {"cut": 55, "dedupe": 40, "move": 90})

        def test_mutant_s7_listing_every_job_as_a_cut_is_caught_control_first(self):
            runs = [run(1, "test", "pull_request", "aa", 0)]
            jobs = {1: [job(UBU, 0, 0, 5), job("stale-report", 0, 0, 7)]}

            def cuts(space):
                return [c["what"] for c in space["ci_queue_measure"](runs, jobs, NOW, 24, GATE, ())["candidates"] if c["kind"] == "cut"]
            self.assertEqual(cuts(self.pure_lines()), ["stale-report"])   # control
            mutant = cuts(self.pure_lines("if name not in gate_names and name not in CI_QUEUE_GATE_FEEDERS]", "if True]"))
            with self.assertRaisesRegex(AssertionError, "^2 != 1$"):
                self.assertEqual(len(mutant), 1)

        def test_mutant_s11_a_post_is_caught_control_first(self):
            def sent(space_get):
                seen = []

                def runner(argv, **kwargs):
                    seen.append(list(argv))
                    return subprocess.CompletedProcess(argv, 0, "{}", "")
                space_get(runner, "gh", "repos/o/r/actions/runs", 5)
                return sum(1 for argv in seen if {"-X", "--method"} & set(argv))
            text = (REPO / "bin" / "switchboard" / "reminders.py").read_text()
            body = text[text.index("def ci_queue_get("):text.index("def ci_queue_fetch(")]
            self.assertEqual(body.count('[gh, "api", path]'), 1)

            def compiled(source):
                space = {"json": json}
                exec(compile(source, "ci_queue_get_mutant", "exec"), space)
                return space["ci_queue_get"]
            self.assertEqual(sent(compiled(body)), 0)   # control
            posts = sent(compiled(body.replace('[gh, "api", path]', '[gh, "api", "-X", "POST", path]')))
            with self.assertRaisesRegex(AssertionError, "^1 != 0$"):
                self.assertEqual(posts, 0)

        def test_docs_report_sentences_about_the_printed_words_are_true(self):
            text = (REPO / "docs" / "ci-queue-report.md").read_text()
            printed = "\n".join(sb.ci_queue_lines(self.measure()))
            self.assertNotIn("prints the reopened-run count", text)
            for words in ("repeat runs on one head", "draft push runs"):
                self.assertIn('"%s"' % words, text)
                self.assertIn(words, printed)

        def test_docs_report_states_the_reopened_events_and_the_draft_runs(self):
            text = (REPO / "docs" / "ci-queue-report.md").read_text()
            self.assertIn("59 reopened events on 38 distinct pull requests", text)
            self.assertIn("NOT computed here", text)
            self.assertIn("Draft-push runs: 0 counted (the run list carries no draft flag: not countable by a lane), 0 job-minutes", text)

        def test_docs_report_says_why_the_duplicate_checkout_is_not_counted(self):
            text = (REPO / "docs" / "ci-queue-report.md").read_text()
            self.assertIn("not counted by a lane", text)
            self.assertIn("the command does not read them", text)

        def test_docs_report_every_spend_row_has_a_price_and_a_minutes_cell(self):
            lines = (REPO / "docs" / "ci-queue-report.md").read_text().split("## Spend options", 1)[1].splitlines()
            rows = [l.strip("|").split("|") for l in lines if l.startswith("|") and not l.startswith("|---") and "Option" not in l]
            self.assertTrue(rows)
            for option, price, minutes in rows:
                self.assertRegex(price, r"\d|not readable by a lane")
                self.assertRegex(minutes, r"\d+ job-minutes|not readable by a lane")
                if "run-minutes" in minutes:
                    self.assertIn("not job-minutes", minutes)
            self.assertEqual((REPO / "docs" / "ci-queue-report.md").read_text().count("decided by the owner"), 1)
            self.assertIn("tester's live measure", (REPO / "docs" / "ci-queue-report.md").read_text())

        def serve(self, requests):
            runs, jobs = fixture()

            def route(argv, **kwargs):
                if argv[0] != FAKE_GH:
                    return self.world.route(argv, **kwargs)
                requests.append(list(argv))
                path = next(a for a in argv[2:] if a.startswith("repos/"))
                if "/jobs" in path:
                    body = {"jobs": jobs[int(path.split("/runs/")[1].split("/")[0])]}
                elif "/actions/runs" in path:
                    body = {"workflow_runs": runs}
                else:
                    body = {"files": [{"filename": "docs/x.md"}]}
                return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
            return route

        def test_s11_the_command_sends_only_gets_and_prints_no_token(self):
            requests = []
            self.paths = self.home / "paths.txt"
            self.paths.write_text("# list\nshims/**\n")
            with captured() as (out, err):
                code = sb.main(["ci-queue", "--repo", "o/r", "--until", at(24 * 60), "--paths-file", str(self.paths), "--json"], runner=self.serve(requests), now=lambda: NOW,
                               socket_call=self.world.herdr.socket)
            self.assertEqual(code, 0, err.getvalue())
            self.assertTrue(requests)
            for argv in requests:
                self.assertEqual(argv[1], "api")
                self.assertFalse({"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"} & set(argv), argv)
            self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
            self.assertEqual(json.loads(out.getvalue())["peak_queued"], 7)

        def gate_legs(self, literal):
            import importlib.util
            spec = importlib.util.spec_from_file_location("merge_gate_for_ci_queue", REPO / "bin" / "merge_gate.py")
            gate = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(gate)
            return tuple(gate.UBUNTU_LEGS) + tuple(gate.MAC_LEGS) == tuple(literal)

        def test_the_literal_gate_legs_equal_merge_gates(self):
            self.assertTrue(self.gate_legs(sb.CI_QUEUE_GATE_LEGS))

        def test_mutant_one_changed_literal_leg_is_caught_control_first(self):
            self.assertTrue(self.gate_legs(sb.CI_QUEUE_GATE_LEGS))   # control
            mutant = (sb.CI_QUEUE_GATE_LEGS[0].replace("ubuntu", "ubunto"),) + tuple(sb.CI_QUEUE_GATE_LEGS[1:])
            with self.assertRaisesRegex(AssertionError, "^False is not true$"):
                self.assertTrue(self.gate_legs(mutant))

        def test_u2_cmd_remind_and_its_attributes_are_the_lines_before_the_new_code(self):
            text = (REPO / "bin" / "switchboard" / "reminders.py").read_text()
            head = text.split("\n\n\n# ---- ci-queue", 1)[0]
            self.assertTrue(head.endswith("cmd_remind.ensure_on_use = True   # the daemon is alive when it runs\ncmd_remind.header_cache = True    # #1520"))
            self.assertEqual(text.count("def cmd_remind("), 1)

        def test_s2_an_unexpanded_matrix_placeholder_is_never_a_cut_candidate(self):
            runs = [run(1, "test", "pull_request", "aa", 0)]
            jobs = {1: [job("shards (${{ matrix.shard }}/8)", 0, 0, 5), job("matrix.cloud", 0, 0, 5), job("stale-report", 0, 0, 7)]}
            space = self.measure(runs, jobs, docs_only=())
            cut = [c["what"] for c in space["candidates"] if c["kind"] == "cut"]
            self.assertEqual(cut, ["stale-report"])

        WORKFLOW = ("name: test\njobs:\n  plan:\n    runs-on: x\n  shards:\n    name: shards (${{ matrix.shard }}/3)\n    strategy:\n      matrix:\n"
                    "        shard: [1, 2, 3]\n  test:\n    name: ${{ matrix.name }}\n    strategy:\n      matrix:\n        include: ${{ fromJSON(x) }}\n")

        def gate_jobs(self):
            folder = self.home / "wf"
            folder.mkdir(exist_ok=True)
            (folder / "test.yml").write_text(self.WORKFLOW)
            return folder, sb.ci_queue_workflow_jobs(folder)

        def test_s2_the_jobs_of_a_workflow_are_read_with_the_matrix_expanded_and_no_placeholder(self):
            _, names = self.gate_jobs()
            self.assertEqual(sorted(names), ["plan", "shards (1/3)", "shards (2/3)", "shards (3/3)"])

        def test_s2_s3_a_matrix_job_a_gate_reads_is_no_cut_and_a_job_no_gate_reads_is_one_with_its_minutes(self):
            _, names = self.gate_jobs()
            runs = [run(1, "test", "pull_request", "aa", 0)]
            jobs = {1: [job("shards (%d/3)" % i, 0, 0, 10) for i in (1, 2, 3)] + [job("shards (${{ matrix.shard }}/3)", 0, 0, 4), job("stale-report", 0, 0, 7)]}
            space = self.measure(runs, jobs, docs_only=(), gate_names=GATE + names)
            cut = [c for c in space["candidates"] if c["kind"] == "cut"]
            self.assertEqual(len([c for c in cut if "shards" in c["what"] or "matrix" in c["what"]]), 0)
            self.assertEqual([(c["what"], c["job_minutes_per_day"]) for c in cut], [("stale-report", 7)])
    return TestCiQueue
