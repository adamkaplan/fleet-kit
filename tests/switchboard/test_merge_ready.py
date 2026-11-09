"""`fleet-switchboard merge-ready <owner/repo#N>` (ask #2265): the facts four wrong admin merges got wrong, read in one place. GET only; every fixture is faked, no real GitHub call."""
import json
import os
import subprocess
from pathlib import Path
from unittest import mock
from support.core import Base
from support.loader import captured, sb
from support.wire_fakes import FAKE_HERDR, FAKE_OPENCODE
from support.herdr_world import World

REPO = "acme/widgets"
PR = "%s#7" % REPO
HEAD, OLD = "b" * 40, "a" * 40
HOLD = "needs:captain"
REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeGitHub:
    """A fake `gh`: every call is recorded; a pull request with every fact right unless a test changes one."""

    def __init__(self):
        self.base, self.default, self.labels = "main", "main", [HOLD]
        self.reviews, self.events = [], []
        self.checks = [{"name": "test", "conclusion": "SUCCESS"}]
        self.required = {"contexts": ["test"], "checks": [{"context": "test"}]}
        self.rules = []   # the rules that apply to the base branch (rulesets)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        words = [a for a in argv[1:] if not a.startswith("-")]
        if words[:2] == ["pr", "view"]:
            answer = {"baseRefName": self.base, "headRefOid": HEAD, "labels": [{"name": n} for n in self.labels], "statusCheckRollup": self.checks,
                      "reviews": [], "title": "t", "state": "OPEN", "body": ""}
        elif words and words[0] == "api":
            path = next(a for a in argv if a.startswith("repos/"))
            if path == "repos/%s" % REPO:
                answer = {"default_branch": self.default}
            elif path.endswith("/reviews"):
                answer = self.reviews
            elif path.endswith("/events"):
                answer = self.events
            elif path.endswith("/required_status_checks"):
                if self.required is None:   # a repository with rulesets, no branch protection
                    return subprocess.CompletedProcess(argv, 1, "", "gh: Branch not protected (HTTP 404)")
                answer = self.required
            elif "/rules/branches/" in path:
                answer = self.rules
            else:
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 404")
        else:
            return subprocess.CompletedProcess(argv, 1, "", "not a read")
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")

    def writes(self):
        return [c for c in self.calls if "--method" in c or "-X" in c or "-f" in c or "-F" in c or "--field" in c or "--input" in c
                or [a for a in c[1:] if not a.startswith("-")][:2] in (["pr", "merge"], ["pr", "edit"], ["pr", "comment"], ["pr", "review"], ["label", "create"])]


def review(state, commit=OLD, user="rev", body="", rid=1):
    return {"id": rid, "state": state, "commit_id": commit, "user": {"login": user}, "body": body}


class TestMergeReady(Base):
    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("coder",))
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "captain_labels": {REPO: HOLD}})
        self.gh = FakeGitHub()

    def runner(self, argv, **kwargs):
        return self.gh(argv, **kwargs) if os.path.basename(argv[0]) == "gh" else self.world.route(argv, **kwargs)

    def run_ready(self, *extra):
        with captured() as (out, err):
            try:
                code = sb.main(["merge-ready", PR] + list(extra), runner=self.runner, now=self.world.clock.now, wait=lambda sock, seconds: False,
                               socket_call=self.world.herdr.socket)
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def refused(self, text):
        code, out, err = self.run_ready()
        self.assertEqual(code, 1, (out, err))
        lines = [l for l in out.splitlines() if text in l]
        self.assertEqual(len(lines), 1, "one line names the fact %r: %r" % (text, out))
        return out

    def test_2320_a_land_repository_has_no_captain_rule_and_the_control_still_needs_the_label(self):
        self.gh.labels = []
        self.refused("captain: the required captain label %s is missing" % HOLD)
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "captain_labels": {REPO: HOLD}, "land_repos": ["acme/*"]})
        code, out, err = self.run_ready()
        self.assertEqual(code, 0, (out, err))
        self.assertIn("no captain rule", out)

    def test_s3_every_fact_right_says_ready_and_exits_0(self):
        code, out, err = self.run_ready()
        self.assertEqual((code, out.strip()), (0, "READY " + PR + " (base main: the default branch)"), err)

    def test_s2_a_pull_request_into_another_base_is_refused(self):
        self.gh.base = "release"
        self.refused("is into release, not the default branch main")

    def test_s2_a_block_lifted_by_the_same_reviewers_later_approval_is_ready_and_named(self):
        self.gh.reviews = [review("CHANGES_REQUESTED", OLD, "rev", rid=1), review("APPROVED", HEAD, "rev", rid=2)]
        code, out, err = self.run_ready()
        self.assertEqual(code, 0, (out, err))
        self.assertIn("rev's block at aaaaaaaa lifted by their later approval at bbbbbbbb", out)

    def test_s2_an_approval_whose_body_blocks_does_not_lift_the_earlier_block(self):
        self.gh.reviews = [review("CHANGES_REQUESTED", OLD, "rev", rid=1), review("APPROVED", HEAD, "rev", body="BLOCK: still wrong", rid=2)]
        self.refused("rev requested changes or blocked at aaaaaaaa")   # the earlier block is named; old code lifted it
        code, out, err = self.run_ready()
        self.assertNotIn("lifted", out)
        self.assertIn("rev requested changes or blocked at bbbbbbbb", out)   # the approval itself counts as a block at its commit

    def test_s2_a_block_followed_only_by_another_reviewers_approval_is_refused(self):
        self.gh.reviews = [review("CHANGES_REQUESTED", OLD, "a", rid=1), review("APPROVED", HEAD, "b", rid=2)]
        self.refused("a requested changes or blocked at aaaaaaaa (an earlier head than bbbbbbbb)")

    def test_s2_a_block_whose_later_approval_was_dismissed_is_refused(self):
        self.gh.reviews = [review("CHANGES_REQUESTED", OLD, "rev", rid=1), review("DISMISSED", HEAD, "rev", rid=2)]
        self.gh.events = [{"event": "review_dismissed", "actor": {"login": "maint"}, "dismissed_review": {"review_id": 2, "state": "approved"}}]
        self.refused("rev requested changes or blocked at aaaaaaaa")

    def test_s2_a_block_and_an_approval_at_the_same_commit_is_refused(self):
        self.gh.reviews = [review("CHANGES_REQUESTED", OLD, "rev", rid=1), review("APPROVED", OLD, "rev", rid=2)]
        self.refused("rev requested changes or blocked at aaaaaaaa")

    def test_s2_a_block_never_followed_by_an_approval_is_refused(self):
        self.gh.reviews = [review("COMMENTED", OLD, "rev", body="BLOCK: no", rid=1)]
        self.refused("rev requested changes or blocked at aaaaaaaa")

    def test_s2_a_block_written_in_a_comment_review_is_refused(self):
        self.gh.reviews = [review("COMMENTED", HEAD, "bot", body="BLOCK: the base is wrong")]
        self.refused("bot requested changes or blocked")

    def test_s2_a_dismissed_review_is_refused_and_names_who_dismissed_it(self):
        self.gh.reviews = [review("DISMISSED", OLD, "rev", rid=5)]
        self.gh.events = [{"event": "review_dismissed", "actor": {"login": "maint"}, "dismissed_review": {"review_id": 5}}]
        self.refused("rev's review at aaaaaaaa was dismissed by maint")

    def test_s2_a_dismissed_approval_is_ready_with_a_note(self):
        self.gh.reviews = [review("DISMISSED", OLD, "rev", rid=5)]
        self.gh.events = [{"event": "review_dismissed", "actor": {"login": "github-actions"}, "dismissed_review": {"review_id": 5, "state": "approved"}}]
        code, out, err = self.run_ready()
        self.assertEqual((code, out.strip()), (0, "READY %s (base main: the default branch) (rev's approval at aaaaaaaa was dismissed by github-actions (not a refusal))" % PR), err)

    def test_s2_a_dismissed_request_for_changes_is_refused_naming_the_dismisser(self):
        self.gh.reviews = [review("DISMISSED", OLD, "rev", rid=5)]
        self.gh.events = [{"event": "review_dismissed", "actor": {"login": "maint"}, "dismissed_review": {"review_id": 5, "state": "changes_requested"}}]
        self.refused("rev's review at aaaaaaaa was dismissed by maint")

    def test_s2_a_dismissed_approval_whose_body_blocks_is_refused(self):
        self.gh.reviews = [review("DISMISSED", OLD, "rev", body="BLOCK: no", rid=5)]
        self.gh.events = [{"event": "review_dismissed", "actor": {"login": "maint"}, "dismissed_review": {"review_id": 5, "state": "approved"}}]
        self.refused("dismissed by maint")

    def test_s2_required_checks_are_read_from_the_rulesets_when_there_is_no_branch_protection(self):
        self.gh.required = None
        self.gh.rules = [{"type": "pull_request"}, {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "test"}, {"context": "lint"}]}}]
        self.refused("required check lint has no result")
        self.gh.checks = [{"name": "test", "conclusion": "SUCCESS"}, {"name": "lint", "conclusion": "SUCCESS"}]
        code, out, err = self.run_ready()
        self.assertEqual((code, out.strip()), (0, "READY " + PR + " (base main: the default branch)"), err)
        self.gh.checks = [{"name": "test", "conclusion": "FAILURE"}, {"name": "lint", "conclusion": "SUCCESS"}]
        self.refused("required check test is FAILURE")

    def test_s2_neither_protection_nor_rulesets_is_a_note_not_a_refusal(self):
        self.gh.required, self.gh.rules = None, []
        code, out, err = self.run_ready()
        self.assertEqual((code, out.strip()), (0, "READY %s (base main: the default branch) (no required checks configured: main has no branch protection and no ruleset rule)" % PR), err)

    def test_s2_a_ruleset_read_that_errors_is_refused_naming_the_error(self):
        self.gh.required = None
        inner = self.gh
        self.runner = lambda argv, **kw: (subprocess.CompletedProcess(argv, 1, "", "HTTP 500") if any("/rules/branches/" in a for a in argv)
                                          else inner(argv, **kw)) if os.path.basename(argv[0]) == "gh" else self.world.route(argv, **kw)
        self.refused("the required checks could not be read")

    def test_s2_a_configured_merge_base_is_what_the_pull_request_is_compared_with_and_is_named(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "captain_labels": {REPO: HOLD}, "merge_base": {REPO: "integration"}})
        self.refused("is into main, not the configured merge base integration")
        self.gh.base = "integration"
        code, out, err = self.run_ready()
        self.assertEqual((code, out.strip()), (0, "READY %s (base integration: the configured merge_base)" % PR), err)

    def test_s2_a_merge_base_of_another_repository_changes_nothing(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "captain_labels": {REPO: HOLD}, "merge_base": {"acme/other": "integration"}})
        code, out, err = self.run_ready()
        self.assertEqual((code, out.strip()), (0, "READY %s (base main: the default branch)" % PR), err)

    def test_u1_a_malformed_merge_base_is_refused_by_the_config(self):
        for bad in ([], {"a/b": ""}, {"a/b": 3}, {"r%d/x" % i: "b" for i in range(21)}):
            with self.assertRaises((SystemExit, sb.ConfigError), msg=repr(bad)):
                self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR, "merge_base": bad})
                self.run_ready()

    def test_s2_a_missing_captain_label_is_refused(self):
        self.gh.labels = []
        self.refused("the required captain label needs:captain is missing")

    def test_s2_a_repository_without_a_captain_rule_is_ready_and_says_so(self):
        self.write_config({"opencode": FAKE_OPENCODE, "herdr": FAKE_HERDR})
        self.gh.labels = []
        code, out, err = self.run_ready()
        self.assertEqual((code, out.strip()), (0, "READY " + PR + " (base main: the default branch) (no captain rule)"), err)

    def test_s2_a_required_check_that_is_red_or_missing_is_refused(self):
        self.gh.checks = [{"name": "test", "conclusion": "FAILURE"}]
        self.refused("required check test is FAILURE")
        self.gh.checks = []
        self.refused("required check test has no result")

    def test_s2_a_required_check_list_that_cannot_be_read_is_refused(self):
        inner = self.gh

        def broken(argv, **kwargs):
            if any(a.endswith("/required_status_checks") for a in argv):
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 403")
            return inner(argv, **kwargs)
        self.runner = lambda argv, **kw: broken(argv, **kw) if os.path.basename(argv[0]) == "gh" else self.world.route(argv, **kw)
        self.refused("the required checks could not be read")

    def test_s4_no_run_makes_a_write_with_or_without_override(self):
        self.gh.base, self.gh.labels = "release", []
        self.run_ready()
        self.run_ready("--override", "the owner: fix forward")
        self.gh.base, self.gh.labels = "main", [HOLD]
        self.run_ready()
        self.assertGreater(len(self.gh.calls), 0)
        self.assertEqual(self.gh.writes(), [], "write calls")

    def test_the_override_records_one_audit_line_prints_overridden_and_exits_6(self):
        self.gh.base = "release"
        code, out, err = self.run_ready("--override", "the owner: fix forward after the revert")
        self.assertEqual(code, 6, (out, err))
        self.assertTrue(out.startswith("OVERRIDDEN by the owner: base: "), out)
        lines = [x for x in self.world.audit() if x["event"] == "merge_ready.override"]
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["subject"], lines[0]["detail"]["who"], lines[0]["detail"]["reason"]), (PR, "the owner", "fix forward after the revert"))
        self.assertTrue(any("not the default branch" in f for f in lines[0]["detail"]["failed"]))

    def test_an_override_with_an_empty_who_or_reason_is_refused_and_writes_nothing(self):
        for text in ("", ": reason", "who:", "who: ", "no colon at all"):
            code, out, err = self.run_ready("--override", text)
            self.assertEqual(code, 2, (text, out, err))
        self.assertEqual([x for x in self.world.audit() if x["event"] == "merge_ready.override"], [])
        self.assertEqual(self.gh.calls, [], "nothing is read before the override is judged")

    def test_an_override_on_a_pull_request_that_is_ready_records_nothing(self):
        code, out, err = self.run_ready("--override", "the owner: just in case")
        self.assertEqual((code, out.strip()), (0, "READY " + PR + " (base main: the default branch)"))
        self.assertEqual([x for x in self.world.audit() if x["event"] == "merge_ready.override"], [])

    def test_s5_the_coordination_skill_and_the_switchboard_document_tell_a_lane_to_run_merge_ready_before_an_admin_merge(self):
        for name in ("skills/fleet-coordination/SKILL.md", "docs/switchboard.md"):
            text = (REPO_ROOT / name).read_text()
            lines = [l for l in text.splitlines() if "merge-ready" in l and "admin merge" in l]
            self.assertTrue(lines, "%s has no line telling a lane to run merge-ready before an admin merge" % name)
