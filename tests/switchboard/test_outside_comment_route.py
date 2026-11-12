"""Ask #2484: an outside person's comment is never routed to nobody (S2 to S5, S6 control, U1).

The case: an issue of a watched repository, signed in its body by `<!-- fleet-switchboard:from=lane-a -->`, an outside person comments;
the lane never registered the issue, so the ordinary links find no owner. Neutral names only. GithubRig is set by bin/test-switchboard."""
import importlib
from unittest import mock
from support.core import Base
from support.loader import sb

GithubRig = None   # set by bin/test-switchboard after the import
_roles = importlib.import_module("support.scenario_roles")
SCRATCH = _roles.GH_REPO
FLEET = "acct-a"
MARK = "<!-- fleet-switchboard:from=%s -->"

ONE_CHIEF = {"chief": {"role": "orchestrator", "charter": 1},
             "lane-a": {"role": "coder", "issue": 12, "reports_to": "chief"}}
TWO_CHIEFS = dict(ONE_CHIEF, chief2={"role": "orchestrator", "charter": 2})


class TestOutsideCommentRoute(Base):
    def rig(self, agents=ONE_CHIEF, config=None):
        self.rig_ = GithubRig(self, agents=agents, config=config if config is not None else {"gh_users": {SCRATCH: FLEET}})
        self.gh, self.hub, self.world = self.rig_.gh, self.rig_.hub, self.rig_.world
        self.hub.accounts.token_for = lambda account: "x"
        for number in (1, 12):
            self.gh.issue(number)
        self.hub.tick()

    def issue(self, number, signer, author):
        issue = self.gh.issue(number, body="Work for the widget.\n\n" + (MARK % signer if signer else ""), title="widget work")
        issue["user"]["login"] = author

    def told(self):
        return [(f.key, f.recipient, f.summary) for f in self.hub.facts()]

    def audit(self, event):
        return [e for e in self.world.audit() if e["event"] == event]

    def test_s2_a_lane_signed_issue_routes_the_outside_comment_to_that_lane_once(self):
        self.rig()
        self.issue(30, "lane-a", FLEET)
        for _ in range(3):
            self.gh.comment(30, "a note with ghp_%s inside" % ("b" * 36), 6001, login="outsider-1")
        got = self.told()
        self.assertEqual([t[:2] for t in got], [("github.comment:6001", "lane-a")])
        self.assertIn("@outsider-1 commented on #30", got[0][2])
        self.assertNotIn("b" * 36, got[0][2])
        self.assertLessEqual(len(got[0][2]), 700)
        self.assertEqual(self.audit("github.comment.chief_unknown"), [])
        self.assertEqual([e["detail"]["routed_by"] for e in self.audit("github.routed")][-1:], ["marker"])

    def test_s3_an_unknown_agent_marker_routes_to_the_chief_not_the_named_agent(self):
        self.rig()
        self.issue(31, "lane-zz", FLEET)
        self.gh.comment(31, "hello", 6002, login="outsider-1")
        got = self.told()
        self.assertEqual([t[:2] for t in got], [("github.comment:6002", "chief")])
        self.assertIn("an issue the Chief of Staff authored", got[0][2])   # the fleet account is the Chief's account here
        self.assertEqual([e["detail"]["routed_by"] for e in self.audit("github.routed")], ["chief"])

    def test_s3_a_marker_in_an_issue_of_a_non_fleet_account_is_not_trusted(self):
        self.rig()
        self.issue(32, "lane-a", "outsider-2")
        self.gh.comment(32, "hello", 6003, login="outsider-1")
        self.assertEqual([t[:2] for t in self.told()], [("github.comment:6003", "chief")])
        self.assertEqual(len(self.audit("github.comment.chief_unknown")), 1)

    def test_s3_a_bot_login_of_the_configuration_is_a_fleet_identity(self):
        self.rig(config={"gh_users": {SCRATCH: FLEET}, "github_bot_logins": ["widget-bot[bot]"]})
        self.issue(33, "lane-a", "widget-bot[bot]")
        self.gh.comment(33, "hello", 6004, login="outsider-1")
        self.assertEqual([t[:2] for t in self.told()], [("github.comment:6004", "lane-a")])

    def test_s4_an_ambiguous_chief_case_is_delivered_to_a_chief_and_audited_once(self):
        self.rig(agents=TWO_CHIEFS)
        self.issue(34, "lane-zz", FLEET)
        for key in (6005, 6006, 6006, 6006):   # the same comment delivered again is routed once
            self.gh.comment(34, "hello %d" % key, key, login="outsider-1")
        got = self.told()
        self.assertEqual([t[1] for t in got], ["chief", "chief"])
        self.assertIn("not one lane", got[0][2])
        self.assertIn("chief, chief2", got[0][2])
        self.assertEqual(len(self.audit("github.comment.chief_unknown")), 2)   # once per comment
        self.assertEqual(self.audit("github.unrouted"), [])

    def test_s4_an_unreadable_author_is_delivered_to_the_chief(self):
        self.rig()
        self.issue(35, "lane-a", FLEET)
        issue = dict(self.gh.issues[35])
        issue["user"] = None
        self.gh.comment(35, "hello", 6010, forward=False)
        self.gh.send("issue_comment", {"action": "created", "issue": issue, "comment": self.gh.comments[-1]})
        self.assertEqual([t[:2] for t in self.told()], [("github.comment:6010", "chief")])

    def test_s5_the_chiefs_own_comment_is_not_routed(self):
        self.rig()
        self.issue(36, "lane-a", FLEET)
        self.gh.comment(36, "my own note", 6011, login=FLEET)
        self.assertEqual(self.told(), [])
        self.gh.comment(36, "an outsider", 6012, login="outsider-1")   # the same issue does route another person's comment
        self.assertEqual([t[0] for t in self.told()], ["github.comment:6012"])

    def test_s5_without_an_account_a_fleet_logins_comment_is_not_routed(self):
        self.rig(config={"gh_users": {}, "github_bot_logins": ["widget-bot[bot]"]})
        self.issue(38, "lane-a", "widget-bot[bot]")
        self.gh.comment(38, "a bot's note", 6016, login="widget-bot[bot]")
        self.assertEqual(self.told(), [])

    def test_s3_an_unknown_agent_from_a_non_chief_fleet_author_falls_back_to_the_chief(self):
        self.rig(config={"gh_users": {SCRATCH: FLEET}, "github_bot_logins": ["widget-bot[bot]"]})
        self.issue(39, "lane-zz", "widget-bot[bot]")
        self.gh.comment(39, "hello", 6017, login="outsider-1")
        got = self.told()
        self.assertEqual([t[:2] for t in got], [("github.comment:6017", "chief")])
        self.assertIn("no lane could be decided", got[0][2])
        self.assertEqual(len(self.audit("github.comment.chief_unknown")), 1)

    @staticmethod
    def old_chief_route(hub, item, agents, now):
        """The _chief_route of integration before ask #2484, as it decided: no marker route, no fallback; an ambiguous or absent Chief lane audits and drops."""
        facts = item.issue_facts
        if facts is None or facts["pr"]:
            return None, None, False
        account = str(hub.accounts.account_for(item.repo) or "").lower()
        if not account or facts["author"].lower() != account or str(item.sender or "").lower() == account:
            return None, None, False
        mine = [n for n in sorted(agents) if sb._is_orchestrator(agents[n])]
        if len(mine) != 1:
            hub._chief_audit_once(item, "the Chief of Staff lane is not one lane")
            return None, None, True
        return mine[0], "chief", False

    def test_s6_control_the_current_routing_sends_the_first_case_to_nobody(self):
        self.rig(agents=TWO_CHIEFS)   # the real case: more than one Chief-of-Staff lane
        self.issue(37, "lane-a", FLEET)
        with mock.patch.object(sb.GitHubHub, "_chief_route", self.old_chief_route):
            self.gh.comment(37, "hello", 6013, login="outsider-1")
            self.assertEqual(self.told(), [])
            self.assertEqual(len(self.audit("github.comment.chief_unknown")), 1)
        self.gh.comment(37, "hello again", 6014, login="outsider-1")
        self.assertEqual([t[1] for t in self.told()], ["lane-a"])

    def test_s6_control_the_ambiguous_case_reaches_nobody_in_the_old_routing(self):
        self.rig(agents=TWO_CHIEFS)
        self.issue(40, "lane-zz", FLEET)
        with mock.patch.object(sb.GitHubHub, "_chief_route", self.old_chief_route):
            self.gh.comment(40, "hello", 6018, login="outsider-1")
            self.assertEqual(self.told(), [])
        self.gh.comment(40, "hello again", 6019, login="outsider-1")
        self.assertEqual([t[1] for t in self.told()], ["chief"])

    def test_u1_a_comment_on_a_lanes_registered_issue_still_goes_to_the_lane_not_the_marker(self):
        self.rig()
        self.gh.issues[12]["body"] = MARK % "chief"
        self.gh.issues[12]["user"]["login"] = FLEET
        self.gh.comment(12, "a question", 6015, login="outsider-1")
        self.assertEqual([t[:2] for t in self.told()], [("github.comment:6015", "lane-a")])
