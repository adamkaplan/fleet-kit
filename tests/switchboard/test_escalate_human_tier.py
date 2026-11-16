"""#2485: `decisions escalate` on a pull request held by the merge-captain label puts it in the owner's tier for the head it lists, and says `escalated` only when
the tier read back is the owner's.

THE CAUSE (from the fixture of the case, `test_control_*`): the listed row is the Chief of Staff's (`cos`) until the escalation record holds a key for the EXACT head the row
lists (`captain:<repo>#<n>@<head>`). The command wrote its key for the head it read from `gh pr view` NOW, not for the head of the row it was given (the list's head, which
trails a push). When the two differ the key never matches the row, the row stays `cos` (and the daemon's prune forgets the key as `another head`), yet the command printed
`escalated ... waiting on you`: it never read the tier back. The panel's reader is not at fault: it shows the tier the file carries. Synthetic names only.
The classes use the runner's BaselineWorld (found by its file name, as test_batch_carry does)."""
import json
import os
import sys
import time
import unittest
from unittest import mock
from support.core import Base
from support.loader import sb
from support.orders_and_adapters import HEAD_A, HEAD_B, pr_view_json


REPO = "acme-org/widget-service"


def _host():
    for module in list(sys.modules.values()):
        if str(getattr(module, "__file__", "") or "").endswith("test-switchboard") and hasattr(module, "BaselineWorld"):
            return module
    return None


HOST = _host()


@unittest.skipIf(HOST is None, "needs the runner's BaselineWorld")
class TestEscalateHeldPullRequestTier(HOST.BaselineWorld if HOST is not None else Base):
    """A held pull request listed on its labels alone, escalated by the command, read back from the list the daemon derives and from the owner's view of it."""

    def setUp(self):
        super().setUp()
        self.hold = {"name": sb.label_names(self.world.config)["merge-captain"]}
        self.engine = self.world.engine
        self.engine.config["plan_gates"] = {"repos": {REPO: {"mode": "shadow"}}}
        self.engine.ci_record_state = sb.CiRecordState()
        self.engine.gh_runner = self.route
        self.who("cos")

    def list_pull_requests(self, *numbers, head=HEAD_A):
        records = [{"number": n, "title": "Add the widget", "state": "open", "head": {"sha": head}, "created_at": sb.iso(self.world.clock.now() - 3600),
                    "labels": [self.hold], "draft": False} for n in numbers]
        self.engine.ci_record_state.lists[REPO] = (self.world.clock.now(), records)

    def derive(self):
        self.engine.discover(self.world.clock.now())
        self.engine.collect_facts()
        return sb.derive_decisions(self.engine, self.world.clock.now())[0]

    def row(self, number=7):
        (found,) = [d for d in self.derive() if d.get("number") == number and d.get("kind") in ("captain", "notice")]
        return found

    def publish(self, *rows):
        """The daemon's file, as the command reads it (the command's own derivation does not see a pull request held on labels alone)."""
        path = self.world.state / sb.DECISIONS_FILE
        path.write_text(json.dumps({"decisions": list(rows), "errors": [], "written_at": sb.iso(time.time()), "generation": 1, "daemon_pid": 1}))
        stamp = self.world.clock.now()
        os.utime(path, (stamp, stamp))

    def marks(self):
        return sorted(sb.read_escalated(self.world.state))

    def owners_rows(self, number=7):
        """What the owner's panel selects from the derived list: the human view."""
        human = sb.decisions_view({"role": "human"}, {})
        return [d for d in sb.select_decisions(self.derive(), human) if d.get("number") == number]

    def case(self, listed_head=HEAD_A, now_head=HEAD_A, number=7):
        """A held pull request listed at `listed_head`, published to the file, whose pull request reads at `now_head` when the command looks."""
        self.list_pull_requests(number, head=listed_head)
        row = self.row(number)
        self.publish(row)
        self.prs[number] = pr_view_json(labels=[self.hold], headRefOid=now_head, body="")
        return row

    def escalate(self, row):
        return self.cmd("cos", "decisions", "escalate", row["id"], "--reason", "the owner decides")

    def test_s2_an_escalated_held_pull_request_is_in_the_human_tier_and_in_the_panels_rows_once(self):
        row = self.case()
        self.assertEqual((row["tier"], row["kind"]), ("cos", "captain"))
        self.assertEqual(self.owners_rows(), [], "not escalated: not the owner's")
        got = self.escalate(row)
        self.assertEqual(got["code"], 0, got)
        self.assertIn("escalated", got["out"])
        self.assertEqual(self.marks(), ["captain:%s#7@%s" % (REPO, HEAD_A)])
        self.world.tick(5)
        self.assertEqual(self.row()["tier"], "human")
        (shown,) = self.owners_rows()
        self.assertEqual((shown["url"], shown["head"]), ("https://github.com/%s/pull/7" % REPO, HEAD_A))
        self.assertEqual(shown["review_evidence"], "no notice recorded: held on its labels alone", "the reviewer line")
        self.assertEqual(len([d for d in self.derive() if d.get("number") == 7]), 1, "one row")
        self.engine.run_pass()   # a pass of the daemon keeps it
        self.assertEqual(self.row()["tier"], "human")
        self.assertEqual(self.marks(), ["captain:%s#7@%s" % (REPO, HEAD_A)])
        self.assertTrue(self.audit_events("decisions.escalate"))

    def test_s1_the_cause_a_key_for_a_head_the_row_does_not_list_leaves_the_row_in_the_cos_tier(self):
        """The control: what the code did before, written as it did (the key of the head read now, not of the head the row lists, and `escalated` without reading back)."""
        row = self.case(listed_head=HEAD_A, now_head=HEAD_B)
        held = {"id": row["id"], "kind": "captain", "tier": "cos", "repo": REPO, "number": 7, "head": HEAD_B}   # what the old escalate_notice built: the head read NOW
        line = sb.escalate_decision(self.engine, self.route, held, "the owner decides", "cos")
        self.assertIn("escalated", line, "the old command said so")
        self.list_pull_requests(7, head=HEAD_A)
        self.assertEqual(self.row()["tier"], "cos", "S1/S5: the escalated item stays in the Chief of Staff's tier")
        self.assertEqual(self.owners_rows(), [], "and the owner's rows do not have it")

    def test_s3_a_pull_request_that_moved_since_it_was_listed_is_not_escalated_and_says_why(self):
        row = self.case(listed_head=HEAD_A, now_head=HEAD_B)
        got = self.escalate(row)
        self.assertEqual(got["code"], 2, got)
        self.assertNotIn("escalated pr", got["out"])
        self.assertEqual(got["out"], "")
        self.assertIn("moved to a new head since it was listed", got["err"])
        self.assertEqual((self.marks(), self.audit_events("decisions.escalate")), ([], []))
        self.world.tick(5)
        self.assertEqual(self.row()["tier"], "cos")

    def test_s3_an_escalation_the_record_does_not_hold_is_not_called_escalated(self):
        row = self.case()
        with mock.patch.object(sb, "mark_escalated", lambda *args: None):   # the write is lost
            got = self.escalate(row)
        self.assertEqual(got["code"], 1, got)
        self.assertEqual(got["out"], "")
        self.assertIn("could not be placed in the owner's tier", got["err"])
        self.assertEqual(self.marks(), [])
        self.assertEqual(self.row()["tier"], "cos")

    def test_s4_a_new_head_returns_it_to_the_cos_tier_and_one_never_escalated_stays_there(self):
        row = self.case()
        other = self.row_for_other()
        self.assertEqual(self.escalate(row)["code"], 0)
        self.world.tick(5)
        self.assertEqual((self.row(7)["tier"], self.row(8)["tier"]), ("human", "cos"))
        self.list_pull_requests(7, 8, head=HEAD_B)
        self.assertEqual((self.row(7)["tier"], self.row(7)["head"], self.row(8)["tier"]), ("cos", HEAD_B, "cos"), "an escalation of one head does not carry to the next")
        self.assertEqual(self.owners_rows(7), [])
        self.assertEqual(other["tier"], "cos")

    def row_for_other(self):
        self.list_pull_requests(7, 8)
        return self.row(8)

    def test_pc1_an_escalation_changes_no_hold_and_no_label(self):
        row = self.case()
        before = (self.labels(), self.writes())
        self.assertEqual(self.escalate(row)["code"], 0)
        self.assertEqual((self.labels(), self.writes()), before)
