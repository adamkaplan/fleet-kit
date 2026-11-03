"""The #1969 tests: a pod's pull request is told once to its launching lane. build() is called by bin/test-switchboard, which holds HandoffCase."""
import json
import os
import re
import threading
import types
from pathlib import Path
from support.loader import sb

SOURCE = Path(__file__).resolve().parents[2] / "bin" / "switchboard" / "reminders.py"
MARK = "# ---- a pod's pull request, told once to its launching lane (#1969)"
PODS = [{"worker": "podA", "repo": "acme/api", "branch": "podA/work", "reports_to": "lane1"},
        {"worker": "podB", "repo": "acme/api", "branch": "podB/work"},
        {"worker": "podC", "repo": "acme/api", "branch": "podC/work", "reports_to": "Bad Name!"}]
PLAN = "PLAN: acme/api#1969\n\nthe change"


def build(HandoffCase):
    class TestPodPullRequest(HandoffCase):
        """#1969: an opened pull request from a cloud record's repo and branch sends one line to the record's reports_to."""

        def setUp(self):
            super().setUp()
            self.sent, self.state = [], {}
            self.arm(self.world.engine)

        def arm(self, engine):
            engine.cloud_watch = {"records": [dict(p) for p in PODS]}
            engine.send = lambda sender, to, text, **kw: self.sent.append((to, text)) or {"sent": True}
            self.hub = types.SimpleNamespace(engine=engine, lock=threading.Lock(), pr_events={}, now=engine.now, _audit=lambda *a: None,
                                             present_record=lambda repo, endpoint, number: self.state.get(number, {"state": "open"}))
            engine.hub = self.hub
            self.engine = engine

        def event(self, number, branch, action="opened", body=PLAN, user="someone", repo="acme/api", head_repo=None):
            pr = {"number": number, "body": body, "title": "x", "user": {"login": user}, "state": "open", "head": {"ref": branch, "sha": "a" * 40}}
            if head_repo:
                pr["head"]["repo"] = {"full_name": head_repo}
            sb.GitHubHub.note_pr_event(self.hub, "pull_request", repo, {"action": action, "number": number, "pull_request": pr})

        def lines(self, to="lane1"):
            return [t for r, t in self.sent if r == to]

        def audits(self, kind):
            return [e for e in self.world.audit() if e["event"] == kind]

        def tick(self, tick=None, times=1):
            for _ in range(times):
                (tick or sb.pod_pr_tick)(self.engine, None)

        # the checks, taking the tick so a mutant runs the same ones
        def check_one_line(self, tick=None):
            self.event(7, "podA/work")
            self.tick(tick)
            self.assertEqual(self.sent, [("lane1", "PR opened by podA: acme/api#7 (ask #1969, branch podA/work)")], "exactly 1 send to lane1")
            self.event(7, "podA/work", action="edited")
            self.event(7, "podA/work", action="synchronize")
            self.event(7, "podA/work", action="reopened")
            self.tick(tick, 2)
            self.assertEqual(len(self.lines()), 1, "exactly 1 send to lane1")

        def check_person(self, tick=None):
            self.event(8, "alice/fix")
            self.tick(tick, 2)
            self.assertEqual(self.sent, [], "0 sends for a pull request not opened from a pod's branch")
            self.assertEqual(self.audits("pod_pr.no_lane"), [])

        def mutant(self, old, new):
            text = SOURCE.read_text().split(MARK, 1)[1]
            self.assertEqual(text.count(old), 1, old)
            space = dict(vars(sb))
            exec(compile(text.replace(old, new), "mutant", "exec"), space)
            return space["pod_pr_tick"]

        def test_s2_a_pod_pull_request_sends_one_line_and_edits_send_none(self):
            self.check_one_line()
            self.assertEqual(self.audits("pod_pr.no_lane"), [])

        def test_s4_a_pull_request_not_from_a_pod_sends_nothing(self):
            self.check_person()

        def test_s5_no_lane_or_a_bad_lane_is_audited_and_sends_nothing(self):
            self.event(9, "podB/work")
            self.event(10, "podC/work")
            self.tick(times=2)
            self.assertEqual(self.sent, [])
            found = self.audits("pod_pr.no_lane")
            self.assertEqual(len(found), 2)
            self.assertIn("acme/api#9", json.dumps(found[0]["detail"]))
            self.assertIn("podB", json.dumps(found[0]["detail"]))

        def test_s7_the_ask_is_read_from_a_closes_line_else_unknown(self):
            self.event(11, "podA/work", body="text\nCloses #55\n")
            self.tick()
            self.assertIn("(ask #55,", self.lines()[0])
            self.event(12, "podA/work", body="no reference here")
            self.tick()
            self.assertIn("(ask unknown,", self.lines()[1])

        def test_s8_a_closed_or_merged_pull_request_is_dropped_and_audited_once(self):
            self.state[7] = {"state": "closed"}
            self.state[13] = {"state": "closed", "merged_at": "2026-11-02T00:00:00Z"}
            self.event(7, "podA/work")
            self.tick(times=3)
            self.assertEqual(self.lines(), [])
            self.assertEqual(len(self.audits("pod_pr.dropped")), 1)
            self.event(13, "podA/work")
            self.tick(times=3)
            self.assertEqual(self.lines(), [])
            self.state[7] = {"state": "open"}
            self.event(7, "podA/work")
            self.tick()
            self.assertEqual(self.lines(), [], "never sent later")

        def test_s8_an_unreadable_state_keeps_the_line_for_the_next_pass(self):
            def fail(repo, endpoint, number):
                raise RuntimeError("down")
            self.hub.present_record = fail
            self.event(7, "podA/work")
            self.tick()
            self.assertEqual(self.lines(), [])
            self.hub.present_record = lambda repo, endpoint, number: {"state": "open"}
            self.tick()
            self.assertEqual(len(self.lines()), 1)

        def test_s9_a_restart_does_not_tell_again_and_the_key_is_kept_once(self):
            self.event(7, "podA/work")
            self.tick()
            self.world.engine = self.world.new_engine()
            self.arm(self.world.engine)
            self.event(7, "podA/work")
            self.tick(times=2)
            self.assertEqual(len(self.lines()), 1)
            kept = json.loads((Path(self.engine.state_dir) / sb.POD_PR_FILE).read_text())["told"]
            self.assertEqual(kept, ["pr-opened:acme/api#7"])

        def test_s9_a_crash_between_the_save_and_the_send_loses_the_line_and_never_repeats_it(self):
            def boom(*a, **k):
                raise RuntimeError("crash")
            self.engine.send = boom
            self.event(7, "podA/work")
            with self.assertRaises(RuntimeError):
                self.tick()
            self.arm(self.world.engine)
            self.event(7, "podA/work")
            self.tick()
            self.assertEqual(self.sent, [])

        def test_u2_a_persons_pull_request_with_a_forged_body_sends_nothing(self):
            self.event(14, "alice/fix", body="PLAN: acme/api#1969\npodA", user="podA")
            sb.GitHubHub.note_pr_event(self.hub, "pull_request", "acme/api", {"action": "opened", "pull_request": {"number": 15, "title": "podA/work", "body": PLAN, "head": {"ref": "x"}}})
            self.tick()
            self.assertEqual(self.sent, [])
            self.assertEqual([e for e in self.world.audit() if e["event"].startswith("pod_pr")], [])

        def test_a_fork_with_the_same_branch_name_sends_nothing(self):
            self.event(16, "podA/work", head_repo="mallory/api")
            self.tick()
            self.assertEqual(self.sent, [])

        def test_u4_the_line_is_short_printable_text(self):
            branch = "podA/work\x1b[31m" + "x" * 400
            self.engine.cloud_watch = {"records": [{"worker": "podA", "repo": "acme/api", "branch": branch, "reports_to": "lane1"}]}
            self.event(17, branch)
            self.tick()
            (text,) = self.lines()
            self.assertLess(len(text), 300)
            self.assertRegex(text, r"^[ -~]+$")
            self.assertNotIn("\x1b", text)

        def test_the_tick_runs_in_finish_pass_and_a_raise_is_audited(self):
            calls, quiet = [], (lambda *a, **k: None)
            failed = []
            self.engine._failed = lambda subject, step, err, report: failed.append(step)
            def boom(engine, discovery):
                calls.append(1)
                raise RuntimeError("x")
            from unittest import mock
            with mock.patch.multiple(sb, pod_pr_tick=boom, cloud_watch_tick=quiet, cloud_end_tick=quiet, cloud_turn_tick=quiet, lost_worker_tick=quiet,
                                     health_write=quiet, prune_delivered_reminders=quiet, friction_tick=quiet, load_tick=quiet,
                                     prune_report_riders=quiet, publish_decisions=quiet, write_catching_up=quiet, schedule_judge_warm=quiet):
                sb.finish_pass(self.engine, None, {"errors": []})
            self.assertEqual((calls, failed), ([1], ["pod-pr"]))

        # the mutants, each with its control (the unmutated copy, through the same path) first
        def test_s10_the_unmutated_copy_passes_both_checks(self):
            same = self.mutant("sent.append((lane, text))", "sent.append((lane, text))")
            self.check_one_line(same)
            self.state.clear()
            self.sent.clear()
            self.check_person(same)

        def test_s10_a_mutant_that_sends_twice_fails_with_the_check_text(self):
            twice = self.mutant('engine.send("switchboard", lane, text)', 'engine.send("switchboard", lane, text); engine.send("switchboard", lane, text)')
            with self.assertRaisesRegex(AssertionError, "exactly 1 send to lane1"):
                self.check_one_line(twice)

        def test_s10_a_mutant_that_matches_a_person_fails_with_the_check_text(self):
            person = self.mutant('r.get("branch") == entry["branch"]', "True")
            self.sent.clear()
            with self.assertRaisesRegex(AssertionError, "0 sends for a pull request not opened from a pod's branch"):
                self.check_person(person)

    return TestPodPullRequest
