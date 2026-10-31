"""The no-floor-paths hold tests of #1041, moved out of bin/test-switchboard (its line ceiling). build() is called by bin/test-switchboard, which holds
TestImpactHold (the world, the fake gh and the helpers) and the ledger heads."""
import contextlib
import inspect
import io
import subprocess
import textwrap
import unittest
from pathlib import Path
from unittest import mock
from support.loader import sb


DOC = Path(__file__).resolve().parents[2] / "docs" / "switchboard.md"
OLD_SENTENCE = "that name floor globs, at most 6"


def s10_problems(text):
    """What is wrong with the documentation of the hold of a pull request in a repository with no floor paths (#1041, S10): the paragraph that says it holds, and the old sentence gone."""
    problems = []
    if OLD_SENTENCE in text:
        problems.append("the old sentence that the pass covers only repositories naming floor globs is still there")
    if not any("no floor paths" in para and "independent impact" in para and "hold" in para for para in text.split("\n\n")):
        problems.append("no paragraph says that an independent impact hold rating holds a pull request with no floor paths")
    return problems


# Every scenario of the plan whose proof is a test, and its test. U1 ("the existing floor tests pass unchanged") and S9 (these mutants) have no mutant of their own.
SCENARIO_TESTS = {
    "S2": "test_s2_a_hold_rating_labels_a_no_paths_repository_once_with_the_reason",
    "S3": "test_s3_a_missing_or_unknown_class_is_held_as_a_top_tier_class_is",
    "S4": "test_s4_a_non_hold_rating_or_a_hold_of_a_not_top_tier_class_changes_nothing",
    "S5": "test_s5_the_same_head_and_rating_is_judged_once_across_passes",
    "S6": "test_s6_and_s12_only_the_rated_pull_request_costs_a_read_and_none_without_a_rating",
    "S7": "test_s7_a_new_unrated_head_releases_only_what_the_daemon_added",
    "S8": "test_s8_an_unreadable_ledger_releases_nothing_and_holds_nothing_new",
    "S10": "test_s10_a_paragraph_says_no_floor_paths_independent_impact_and_hold",
    "S12": "test_s6_and_s12_only_the_rated_pull_request_costs_a_read_and_none_without_a_rating",
    "U2": "test_u2_a_top_tier_path_in_a_no_paths_repository_is_not_judged_without_a_hold_rating",
    "U3": "test_u3_labels_a_person_added_are_never_removed_in_a_no_paths_repository",
    "S13": "test_a_cleared_pull_request_pushed_to_an_unrated_head_is_held_again_in_a_no_paths_repository",
    "S13b": "test_a_cleared_pull_request_pushed_to_an_unrated_head_is_held_again_in_a_no_paths_repository",
}
NO_MUTANT = {"U1": "the existing floor tests pass unchanged", "S9": "these mutants"}
PASS_SKIP = 'if not globs and (not ours or ((records.get((pr, head)) or ("",))[0] == "hold" and not records_problem)):'
HOLDS = 'holds = bool(rating and rating[0] == "hold" and impact_floor.class_is_top_tier(impact_floor._matrix_cached(), rating[1]))'
EARLY = 'if not holds and (problem or (pr not in state["applied"] and pr not in state["clears"])):'
JUDGE_CALL = "spent, skipped = _impact_unpathed_judge(engine, now, key, number, head, rule, enforce, records, records_problem, record, budget, seen, reports, released_now)"
EVERY_PR_HOLDS = [("_impact_unpathed_judge", HOLDS, "holds = True")]
# scenario -> (what the mutant does, [(function, old, new)]); S10's mutant is a text (the documentation).
MUTANTS = {
    "S2": ("the no-paths skip kept ahead of the rating check", [("impact_would_hold_pass", PASS_SKIP, "if not globs and not ours:"), ("impact_would_hold_pass", JUDGE_CALL, "spent, skipped = 0, 0")]),
    "S3": ("labels only a named top tier class", [("_impact_unpathed_judge", HOLDS, HOLDS.replace("rating and rating[0]", "rating and rating[1] is not None and rating[0]"))]),
    "S4": ("labels on any recorded action", [("impact_would_hold_pass", '[0] == "hold" and not records_problem', '[0] != "" and not records_problem'),
                                             ("_impact_unpathed_judge", HOLDS, "holds = bool(rating and rating[0])")]),
    "S5": ("forgets what it judged", [("_impact_unpathed_judge", "if seen.get((repo, number)) == signature:", "if False:")]),
    "S6": ("judges every pull request of a no-paths repository as held", EVERY_PR_HOLDS),
    "S7": ("never releases what the daemon added", [("_impact_unpathed_judge", 'elif pr in state["applied"]:', "elif False:"),
                                                  ("impact_would_hold_pass", PASS_SKIP, "if not globs:")]),
    "S8": ("an unreadable ledger read as no rating, at every place that reads it", [
        ("pr_floor_report", 'record = ("hold", None)   # an unreadable ledger', "record = None   # an unreadable ledger"),
        ("pr_floor_report", 'review, problem = impact_review_hold(engine.state_dir, "%s#%d" % (repo, number), head)', "review, problem = False, None"),
        ("impact_would_hold_pass", "elif enforce and not records_problem:", "elif enforce:"),
        ("release_impact_hold", "if rating_problem or (rating and", "if (rating and"),
        ("release_impact_hold", 'if rehold_reason(engine, state, repo, pr, report["head"]):', "if False:"),
        ("_impact_rehold_now", 'if rating_at(engine, pr, report["head"])[1]:', "if False:")]),
    "S10": ("the documentation says nothing of an independent impact hold rating, and the old sentence is back", None),
    "S12": ("judges every pull request of a no-paths repository as held", EVERY_PR_HOLDS),
    "U2": ("judges every pull request of a no-paths repository as held", EVERY_PR_HOLDS),
    "U3": ("releases a hold label whoever added it", [("_impact_unpathed_judge", EARLY, "if False:"), ("_impact_unpathed_judge", 'elif pr in state["applied"]:', "else:"),
                                                     ("release_impact_hold", 'if not isinstance(state["applied"].get(pr), dict):', "if False:"),
                                                     ("release_impact_hold", "remove = [l for l in ours if l in have]", "remove = [l for l in have if l in set(names.values())]"),
                                                     ("release_impact_hold", 'del state["applied"][pr]', 'state["applied"].pop(pr, None)')]),
    "S13": ("ignores an earlier clear on an unrated head", [("_impact_rehold_now", "if not why:", "if True:")]),
    "S13b": ("skips the no-globs repository", [("impact_would_hold_pass", PASS_SKIP, "if not globs:"), ("impact_would_hold_pass", JUDGE_CALL, "spent, skipped = 0, 0")]),
}


def _home(name):
    """The module a daemon function is defined in (its globals): the switchboard itself, or one of the modules it takes functions from."""
    return getattr(sb, name).__globals__


@contextlib.contextmanager
def mutated(edits):
    """The daemon's functions rebuilt from their source with every edit applied, live where they are defined (and where the daemon module holds the same function) for the duration (an edit
    whose `old` is not in the source exactly once is a stale mutant: AssertionError). The real functions come back after."""
    sources = {}
    for name, old, new in edits:
        source = sources.get(name) or textwrap.dedent(inspect.getsource(getattr(sb, name)))
        if source.count(old) != 1:
            raise AssertionError("%s: the mutant target is not in the function exactly once: %r" % (name, old))
        sources[name] = source.replace(old, new)
    originals = {name: (getattr(sb, name), _home(name)) for name in sources}
    try:
        for name, source in sources.items():
            home = _home(name)
            exec(compile(source, "<mutant of %s>" % name, "exec"), home)
            setattr(sb, name, home[name])
        yield
    finally:
        for name, (function, home) in originals.items():
            home[name] = function
            setattr(sb, name, function)


def build(TestImpactHold, FAKE_GH, LEDGER_HEAD, LEDGER_NEW_HEAD):
    class TestIndependentImpactNoPaths(TestImpactHold):
        """#1041: a repository with no floor paths is held by the independent rating alone."""

        # -- #1041: a repository with NO floor paths is held by the independent rating alone

        def no_paths(self, path="src/app.py"):
            """The same pull request, in a repository that names no floor paths (the plan_gates entry and the hold stay)."""
            self.extra_config = dict(plan_gates={"repos": {"acme/app": {"mode": "shadow"}}}, impact_hold=self.hold_config)
            self.configure()
            self.floor_pr(path=path)
            self.assertEqual(sb.impact_floor.floor_for("acme/app", sb.load_config())[0], [], "the fixture really has no floor paths")
            self.views, self.counting = [], False   # the `gh pr view` reads made by the hold pass itself (the ci recorder reads too)
            base, real = self.route, sb.impact_would_hold_pass

            def counted(argv, **kwargs):
                if self.counting and argv[0] == FAKE_GH and argv[1:3] == ["pr", "view"]:
                    self.views.append(argv[3])
                return base(argv, **kwargs)

            def only_the_hold_pass(engine, now):
                self.counting = True
                try:
                    return real(engine, now)
                finally:
                    self.counting = False
            self.route = counted
            patcher = mock.patch.object(sb, "impact_would_hold_pass", only_the_hold_pass)
            patcher.start()
            self.addCleanup(patcher.stop)

        def rate_at(self, action, klass, head=LEDGER_HEAD, number=9):
            entry = {"kind": "review_record", "pr": "acme/app#%d" % number, "head": head, "reviewer": "impact", "verdict": "approve", "blocks": 0, "notes": 0,
                     "by": "rev", "impact_action": action, "summary": "x"}
            if klass is not None:
                entry["impact_class"] = klass
            sb.plan_ledger.append_entry(self.world.state, entry, 1)

        def would_hold_lines(self):
            return [e for e in self.events("gate.would_hold")]

        def test_s2_a_hold_rating_labels_a_no_paths_repository_once_with_the_reason(self):
            self.no_paths()
            self.rate_at("hold", "credentials")
            self.passes()
            (write,) = self.label_writes()
            self.assertEqual(sorted(write[2]["labels"]), sorted(self.labelled()))
            (line,) = self.would_hold_lines()
            self.assertIn("independent impact review says hold", line["detail"]["reasons"])
            self.assertEqual(len(self.events("gate.hold_applied")), 1)

        def test_s3_a_missing_or_unknown_class_is_held_as_a_top_tier_class_is(self):
            for klass in (None, "no-such-class"):
                with self.subTest(klass=klass):
                    self.setUp()
                    self.no_paths()
                    self.rate_at("hold", klass)
                    self.passes()
                    self.assertEqual(len(self.label_writes()), 1)
                    self.assertEqual(len(self.would_hold_lines()), 1)

        def test_s4_a_non_hold_rating_or_a_hold_of_a_not_top_tier_class_changes_nothing(self):
            for action, klass in (("second-review", "credentials"), ("none", None), ("hold", "performance")):
                with self.subTest(action=action, klass=klass):
                    self.setUp()
                    self.no_paths()
                    self.rate_at(action, klass)
                    self.passes()
                    self.assertEqual((self.writes, self.would_hold_lines(), self.views), ([], [], []))

        def test_s5_the_same_head_and_rating_is_judged_once_across_passes(self):
            self.no_paths()
            self.rate_at("hold", "authority")
            for n in range(3):
                self.passes(advance=n * 400)
            self.assertEqual(len(self.label_writes()), 1)
            self.assertEqual(len(self.would_hold_lines()), 1)

        def test_s6_and_s12_only_the_rated_pull_request_costs_a_read_and_none_without_a_rating(self):
            self.no_paths()
            self.passes(records=[self.listed(number=n) for n in range(20, 30)])
            self.assertEqual((self.views, self.writes), ([], []), "no rating anywhere: no read, no write")
            self.rate_at("hold", "credentials", number=25)
            self.passes(advance=400)
            self.assertEqual(self.views, ["25"], "one read, for the one rated pull request")
            self.assertLessEqual(len(self.views), sb.IMPACT_PASS_CALLS)

        def test_u2_a_top_tier_path_in_a_no_paths_repository_is_not_judged_without_a_hold_rating(self):
            self.no_paths(path="migrations/0002.sql")
            self.passes()
            self.assertEqual((self.writes, self.would_hold_lines(), self.views), ([], [], []))

        def test_s7_a_new_unrated_head_releases_only_what_the_daemon_added(self):
            self.no_paths()
            self.rate_at("hold", "credentials")
            self.passes()
            self.assertEqual(len(self.label_writes()), 1)
            self.floor_pr(path="src/app.py", head=LEDGER_NEW_HEAD)
            self.passes(records=[self.listed(head=LEDGER_NEW_HEAD, labels=self.labelled())], advance=4000)
            self.assertEqual(sorted(w[1].rsplit("/", 1)[1] for w in self.writes if w[0] == "DELETE"), sorted(n.replace(":", "%3A") for n in self.labelled()))
            self.assertEqual(len(self.events("gate.hold_released")), 1)

        def test_u3_labels_a_person_added_are_never_removed_in_a_no_paths_repository(self):
            self.no_paths()
            self.passes(records=[self.listed(labels=self.labelled())])
            self.floor_pr(path="src/app.py", head=LEDGER_NEW_HEAD)
            self.passes(records=[self.listed(head=LEDGER_NEW_HEAD, labels=self.labelled())], advance=4000)
            self.assertEqual([w for w in self.writes if w[0] == "DELETE"], [])

        def test_s8_an_unreadable_ledger_releases_nothing_and_holds_nothing_new(self):
            self.no_paths()
            self.rate_at("hold", "credentials")
            self.passes()
            self.writes.clear()
            (self.world.state / "plan-ledger.jsonl").write_text("this is not a ledger\n")
            self.floor_pr(path="src/app.py", head=LEDGER_NEW_HEAD)
            self.passes(records=[self.listed(head=LEDGER_NEW_HEAD, labels=self.labelled())], advance=4000)
            self.assertEqual(self.writes, [])
            self.assertEqual(self.events("gate.hold_released"), [])

        def test_a_failed_write_in_a_no_paths_repository_is_retried_after_the_backoff_not_marked_done(self):
            self.no_paths()
            self.rate_at("hold", "credentials")
            base, fail = self.route, {"on": True}

            def flaky(argv, **kw):
                if fail["on"] and argv[0] == FAKE_GH and "--method" in argv and argv[argv.index("--method") + 1] == "POST":
                    return subprocess.CompletedProcess(argv, 1, "", "HTTP 502")
                return base(argv, **kw)
            self.route = flaky
            self.passes()
            self.assertEqual(self.label_writes(), [])
            self.assertTrue(self.events("impact.error"))
            fail["on"] = False
            self.passes(advance=10)
            self.assertEqual(self.label_writes(), [], "inside the backoff: not tried again")
            self.passes(advance=sb.IMPACT_RETRY_SECONDS + 1)
            self.assertEqual(len(self.label_writes()), 1)

        def test_advisory_mode_audits_the_hold_rating_as_advisory_and_writes_and_reads_nothing(self):
            self.hold_config = {"enabled": True, "dry_run": True}
            self.no_paths()
            self.rate_at("hold", "credentials")
            for n in range(2):
                self.passes(advance=n * 400)
            self.assertEqual((self.writes, self.views), ([], []))
            (line,) = self.would_hold_lines()
            self.assertTrue(line["detail"]["advisory"])

        def test_the_call_budget_defers_the_rated_pull_requests_beyond_it_to_a_later_pass(self):
            self.hold_config = {"enabled": True, "calls_per_pass": 1}
            self.no_paths()
            for n in (20, 21, 22):
                self.rate_at("hold", "credentials", number=n)
            self.passes(records=[self.listed(number=n) for n in (20, 21, 22)])
            first = len(self.label_writes())
            self.assertLess(first, 3, "the budget bounds one pass")
            for n in range(1, 6):
                self.passes(advance=n * 400)
            self.assertEqual(len(self.label_writes()), 3, "the rest are judged by later passes")

        def test_a_cleared_pull_request_pushed_to_an_unrated_head_is_held_again_in_a_no_paths_repository(self):
            self.no_paths()
            self.rate_at("hold", "credentials")
            self.passes()
            self.assertEqual(self.run_as(None, "impact", "clear", self.PR, "--reason", "a false hold: the file is a fixture")[0], 0)
            state = sb.read_impact_state(self.world.state)[0]
            state["clears"][self.PR]["at"] = 1
            sb.write_impact_state(self.world.state, state)
            self.writes.clear()
            self.floor_pr(path="src/app.py", head=LEDGER_NEW_HEAD)
            self.passes(records=[self.listed(head=LEDGER_NEW_HEAD)], advance=2000)
            (write,) = self.label_writes()
            self.assertEqual(sorted(write[2]["labels"]), sorted(self.labelled()))
            self.assertEqual(len(self.events("gate.hold_reapplied")), 1)

        def test_s10_the_old_sentence_that_the_pass_covers_only_repositories_naming_floor_globs_is_gone(self):
            text = (Path(__file__).resolve().parents[2] / "docs" / "switchboard.md").read_text()
            self.assertNotIn("that name floor globs, at most 6", text)
            self.assertIn("no floor paths", text)

        def test_s10_a_paragraph_says_no_floor_paths_independent_impact_and_hold(self):
            self.assertEqual(s10_problems(DOC.read_text()), [])

    class TestNoPathsMutants(unittest.TestCase):
        """#1041, S9 and the strict reader's gaps: every scenario of the plan whose proof is a test has a mutant (the real code or text with one change) under which ITS named test fails."""

        def run_named(self, name):
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(TestIndependentImpactNoPaths(name))
            return result.wasSuccessful()

        def test_every_scenario_with_a_test_has_a_mutant_and_the_test_exists(self):
            self.assertEqual(sorted(MUTANTS), sorted(SCENARIO_TESTS), "a scenario whose proof is a test has no mutant")
            for scenario, name in SCENARIO_TESTS.items():
                self.assertTrue(callable(getattr(TestIndependentImpactNoPaths, name, None)), "%s: no test %s" % (scenario, name))
            self.assertEqual(sorted(NO_MUTANT), ["S9", "U1"])

        def test_the_real_code_passes_every_named_test(self):
            for scenario, name in sorted(SCENARIO_TESTS.items()):
                with self.subTest(scenario=scenario):
                    self.assertTrue(self.run_named(name))

        def test_each_mutant_makes_the_test_of_its_scenario_fail(self):
            for scenario, name in sorted(SCENARIO_TESTS.items()):
                what, edits = MUTANTS[scenario]
                with self.subTest(scenario=scenario, mutant=what):
                    if edits is None:
                        self.assertTrue(s10_problems(DOC.read_text().replace("independent impact", "separate review") + "\n\n" + OLD_SENTENCE), "the doc check passes a mutant text")
                        continue
                    with mutated(edits):
                        survived = self.run_named(name)
                    self.assertFalse(survived, "the mutant survives: %s fails to fail (%s)" % (name, what))

        def test_s13_both_wrong_builds_are_killed_and_fail_anchored_on_the_hold_reapplied_count(self):
            name = SCENARIO_TESTS["S13"]
            self.assertEqual(SCENARIO_TESTS["S13b"], name)
            self.assertTrue(self.run_named(name), "control: the real daemon passes the S13 test")
            killed = 0
            for scenario in ("S13", "S13b"):
                what, edits = MUTANTS[scenario]
                with mutated(edits):
                    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(TestIndependentImpactNoPaths(name))
                text = "".join(trace for _, trace in result.failures + result.errors)
                self.assertFalse(result.wasSuccessful(), "the mutant survives: %s" % what)
                self.assertTrue("hold_reapplied" in text or "label" in text or "not enough values" in text or "0 != 1" in text, "%s: unanchored failure: %s" % (scenario, text[-300:]))
                killed += 1
            print("S13 mutants killed: %d of 2" % killed)
            self.assertEqual(killed, 2)

    for _name in [n for n in dir(TestImpactHold) if n.startswith("test_")]:
        setattr(TestIndependentImpactNoPaths, _name, None)   # the inherited tests run once, in their own class
    return TestIndependentImpactNoPaths, TestNoPathsMutants
