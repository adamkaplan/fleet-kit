"""The no-floor-paths hold tests of #1041, moved out of bin/test-switchboard (its line ceiling). build() is called by bin/test-switchboard, which holds
TestImpactHold (the world, the fake gh and the helpers) and the ledger heads."""
import subprocess
from pathlib import Path
from unittest import mock
from support.loader import sb


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

    for _name in [n for n in dir(TestImpactHold) if n.startswith("test_")]:
        setattr(TestIndependentImpactNoPaths, _name, None)   # the inherited tests run once, in their own class
    return TestIndependentImpactNoPaths
