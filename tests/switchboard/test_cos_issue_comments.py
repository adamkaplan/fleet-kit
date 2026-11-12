"""Ask #2445: a comment by someone else on an issue the Chief of Staff authored reaches the Chief of Staff (S2 to S4, U1).

Neutral names only. The Chief of Staff's account is read from `gh_users` for the repository (acct-a here, acct-b on another), never typed into the code.
GithubRig is set by bin/test-switchboard after the import."""
import importlib
from unittest import mock
from support.core import Base
from support.loader import sb

GithubRig = None   # set by bin/test-switchboard after the import
_roles = importlib.import_module("support.scenario_roles")
SCRATCH = _roles.GH_REPO

AGENTS = {"chief": {"role": "orchestrator", "charter": 1},
          "coder": {"role": "coder", "issue": 12, "reports_to": "chief"}}


class TestCosIssueComments(Base):
    ACCOUNT = "acct-a"

    def setUp(self):
        super().setUp()
        self.start({"gh_users": {SCRATCH: self.ACCOUNT}})

    def start(self, config):
        self.rig = GithubRig(self, agents=AGENTS, config=config)
        self.gh, self.hub, self.world = self.rig.gh, self.rig.hub, self.rig.world
        self.hub.accounts.token_for = lambda account: "x"   # no real `gh auth token`
        for number in (1, 12):
            self.gh.issue(number)
        self.gh.issue(30, body="Plan for the widget.", title="widget plan")["user"]["login"] = self.ACCOUNT
        self.hub.tick()

    def told(self):
        return [(f.key, f.recipient, f.summary) for f in self.hub.facts()]

    def audit(self, event):
        return [e for e in self.world.audit() if e["event"] == event]

    def test_s2_another_persons_comment_reaches_the_chief_once_and_its_own_does_not(self):
        self.gh.comment(30, "please look at ghp_%s now" % ("a" * 36), 5001, login="reviewer-1")
        self.gh.comment(30, "my own note", 5002, login=self.ACCOUNT)
        (one,) = self.told()
        self.assertEqual(one[:2], ("github.comment:5001", "chief"))
        self.assertIn("@reviewer-1 commented on #30", one[2])
        self.assertIn("untrusted", one[2])
        self.assertNotIn("a" * 36, one[2])
        self.assertLessEqual(len(one[2]), 600)
        self.assertEqual(self.audit("github.comment.chief_unknown"), [])

    def test_s2_the_account_is_the_one_of_the_repository_not_a_fixed_login(self):
        self.start({"gh_users": {SCRATCH: "acct-b"}})
        self.gh.issues[30]["user"]["login"] = "acct-b"
        self.gh.comment(30, "hello", 5003, login="acct-a")
        self.assertEqual([t[:2] for t in self.told()], [("github.comment:5003", "chief")])
        self.gh.issues[31] = dict(self.gh.issues[30], number=31, id=9031, user={"login": "acct-a"})
        self.gh.comment(31, "hello", 5004, login="reviewer-1")
        self.assertEqual([t[0] for t in self.told()], ["github.comment:5003"])

    def test_s3_a_repeated_delivery_routes_once(self):
        for _ in range(3):
            self.gh.comment(30, "hello", 5005, login="reviewer-1")
        self.assertEqual([t[0] for t in self.told()], ["github.comment:5005"] * 1)
        self.assertEqual(sum(1 for _ in self.told()), 1)

    def test_s3_an_unreadable_author_routes_nothing_and_audits_once(self):
        issue = dict(self.gh.issues[30])
        issue["user"] = None
        self.gh.comment(30, "hello", 5006, forward=False)
        comment = self.gh.comments[-1]
        for n in range(2):
            self.gh.send("issue_comment", {"action": "created", "issue": issue, "comment": comment}, delivery="d-%d" % n)
        self.assertEqual(self.told(), [])
        self.assertEqual(len(self.audit("github.comment.chief_unknown")), 1)

    def test_s3_a_failed_read_of_the_issue_routes_nothing_and_audits_once(self):
        self.gh.comment(30, "hello", 5007, login="reviewer-1", forward=False)
        item = sb._comment_item(SCRATCH, self.gh.comments[-1], None, self.world.clock.now())
        self.hub.queue[item.key] = item
        self.gh.api_down = True
        self.hub.facts()
        self.assertEqual(self.told(), [])
        self.assertEqual(len(self.audit("github.comment.chief_unknown")), 1)

    def test_s3_no_account_and_no_signature_routes_nothing_and_audits_as_unowned(self):
        self.start({})
        self.gh.comment(30, "hello", 5008, login="reviewer-1")
        self.assertEqual(self.told(), [])
        self.assertEqual(self.audit("github.comment.chief_unknown"), [])
        self.assertEqual([e["detail"]["keys"][0] for e in self.audit("github.unrouted")], ["github.comment:5008"])

    def test_s4_control_the_current_routing_sends_the_first_case_to_nobody(self):
        with mock.patch.object(sb.GitHubHub, "_chief_route", lambda hub, item, agents, now: (None, None, False)):
            self.gh.comment(30, "hello", 5009, login="reviewer-1")
            self.assertEqual(self.told(), [])
        self.gh.comment(30, "hello again", 5010, login="reviewer-1")
        self.assertEqual([t[1] for t in self.told()], ["chief"])

    def test_u1_a_comment_on_a_lanes_own_issue_still_goes_to_the_lane(self):
        self.gh.comment(12, "a question", 5011, login="reviewer-1")
        self.assertEqual([t[:2] for t in self.told()], [("github.comment:5011", "coder")])

    def test_u1_a_pull_request_comment_is_not_routed_to_the_chief(self):
        self.gh.pull(40)
        self.gh.pulls[40]["user"]["login"] = self.ACCOUNT
        issue = dict(self.gh.pulls[40], pull_request={"url": "x"})
        self.gh.send("issue_comment", {"action": "created", "issue": issue, "comment": {"id": 5012, "body": "x", "user": {"login": "reviewer-1"}}})
        self.assertEqual(self.told(), [])
