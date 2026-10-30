"""The delivery-lag tests of #1402, moved out of bin/test-switchboard (its line ceiling). World is set by bin/test-switchboard after the import."""
import os
import unittest
from support.core import Base
from support.loader import sb
from support.wire_fakes import EPOCH

World = None   # set by bin/test-switchboard: World lives in the entry file


class PresentHub:
    """#1402: the hub's one present-state read, as a fake: records by (endpoint, repo, number); a missing one raises; every read is counted; nothing writes."""

    def __init__(self, records):
        self.records, self.reads = records, []

    def present_record(self, repo, endpoint, number):
        self.reads.append((endpoint, repo, number))
        if (endpoint, repo, number) not in self.records:
            raise sb.GitHubUnavailable("gh api failed")
        return self.records[(endpoint, repo, number)]


class TestDeliveryLag(Base):
    """#1402: an alert or digest delivered late says so, and one no longer true is dropped (S2 to S8, U1, U2)."""

    OPEN_HEAD = "aaaa1111"

    def setUp(self):
        super().setUp()
        self.world = World(self, agents=("platform", "coder"))
        self.hub = PresentHub({("pulls", "acme/api", 7): {"state": "open", "merged": False, "head": {"sha": "newhead22"}},   # the head moved on
                               ("pulls", "acme/api", 8): {"state": "open", "merged": False, "head": {"sha": self.OPEN_HEAD}},   # still true
                               ("issues", "acme/api", 5): {"state": "closed", "labels": [{"name": "alert"}], "body": ""},
                               ("issues", "acme/api", 6): {"state": "open", "labels": [], "body": "Standing line (2026-10-27): waiting for 2026-11-02"},
                               ("issues", "acme/api", 4): {"state": "open", "labels": [{"name": "alert"}], "body": ""}})
        self.world.engine.hub = self.hub

    def make(self, key, age, summary="a thing", kind="alert", **extra):
        self.world.fact("platform", key, summary, kind=kind, created=self.world.clock.now() - age, batch=False, **extra)

    def deliver(self):
        self.world.tick(1)
        engine = self.world.engine
        now = self.world.clock.now()
        plan = engine.plan("platform", engine.collect_facts()[0], engine.discover(now), now)
        self.assertIn(plan.action, ("wake", "note"))
        return engine.deliver(plan)

    def texts(self):
        return [a["text"] for a in self.world.admissions("platform")]

    def dropped(self):
        return [e for e in self.world.audit() if e["event"] == "stale.dropped"]

    def sent_clock(self, age):
        return sb.clock_utc(self.world.clock.now() + 1 - age)

    def test_s2_a_fresh_alert_shows_its_sent_time_first_and_is_not_delayed(self):
        self.make("red-ci:pr-unowned:acme/api#8:%s:ci:platform" % self.OPEN_HEAD, 600)
        self.assertTrue(self.deliver()["sent"])
        (text,) = self.texts()
        first = text.splitlines()[0]
        self.assertIn("sent %s" % self.sent_clock(600), first)
        self.assertEqual(first.count("delayed:"), 0)

    def test_s3_an_alert_older_than_thirty_minutes_is_marked_delayed_in_its_first_line(self):
        self.make("red-ci:pr-unowned:acme/api#8:%s:ci:platform" % self.OPEN_HEAD, 15 * 3600)
        self.assertTrue(self.deliver()["sent"])
        first = self.texts()[0].splitlines()[0]
        self.assertIn("delayed: sent %s" % self.sent_clock(15 * 3600), first)
        self.make("alert:acme/api#4 alert:platform", 1700, issue=4, repo="acme/api")   # 28 minutes: not late
        self.assertTrue(self.deliver()["sent"])
        self.assertNotIn("delayed:", self.texts()[1].splitlines()[0])

    def test_s4_stale_alerts_are_dropped_audited_and_acknowledged_and_an_unreadable_one_is_delivered_unverified(self):
        self.make("red-ci:pr-unowned:acme/api#7:%s:ci:platform" % self.OPEN_HEAD, 7200, "red on the old head")
        self.make("alert:acme/api#5 alert:platform", 7200, "issue five", issue=5, repo="acme/api")
        self.make("alert-standing:acme/api#6:x:platform", 7200, "issue six has no standing line", issue=6, repo="acme/api")
        self.make("red-ci:pr-unowned:acme/api#9:%s:ci:platform" % self.OPEN_HEAD, 7200, "red where the read fails")
        outcome = self.deliver()
        self.assertTrue(outcome["sent"])
        (text,) = self.texts()
        for gone in ("old head", "issue five", "issue six"):
            self.assertNotIn(gone, text)
        self.assertIn("unverified", text)
        self.assertIn("read fails", text)
        self.assertEqual(len(self.dropped()), 3)
        self.assertEqual(sorted(e["detail"]["why"] for e in self.dropped()),
                         ["a standing line is already posted", "the head moved on to newhead22", "the issue is closed"])
        self.assertEqual(outcome["keys"], ["red-ci:pr-unowned:acme/api#9:%s:ci:platform" % self.OPEN_HEAD])

    def test_s4_every_stale_alert_alone_is_dropped_with_no_message(self):
        self.make("red-ci:pr-unowned:acme/api#7:%s:ci:platform" % self.OPEN_HEAD, 7200)
        outcome = self.deliver()
        self.assertEqual((outcome["sent"], outcome["action"]), (False, "dropped"))
        self.assertEqual(self.texts(), [])
        self.assertEqual(len(self.dropped()), 1)

    def test_s5_an_alert_still_true_is_delivered_once_and_acknowledged_with_one_read_and_no_write(self):
        key = "red-ci:pr-unowned:acme/api#8:%s:ci:platform" % self.OPEN_HEAD
        self.make(key, 7200)
        self.make("alert:acme/api#4 alert:platform", 7200, issue=4, repo="acme/api")
        outcome = self.deliver()
        self.assertEqual(len(self.texts()), 1)
        self.assertEqual(sorted(outcome["keys"]), sorted([key, "alert:acme/api#4 alert:platform"]))
        self.assertEqual(self.dropped(), [])
        self.assertEqual(sorted(self.hub.reads), [("issues", "acme/api", 4), ("pulls", "acme/api", 8)])   # U2: one read per subject, and the fake hub has no write
        self.assertFalse(hasattr(self.hub, "write"))

    def test_s4_mutant_a_stale_alert_delivered_is_caught(self):
        os.environ[sb.FAULT_ENV] = "deliver-stale-alert"
        self.make("red-ci:pr-unowned:acme/api#7:%s:ci:platform" % self.OPEN_HEAD, 7200, "red on the old head")
        self.assertTrue(self.deliver()["sent"])
        self.assertIn("old head", self.texts()[0])   # the delivered message the real build would never send: the drop tests above fail under this fault
        self.assertEqual(self.dropped(), [])

    def test_s2_mutant_no_sent_time_is_caught(self):
        os.environ[sb.FAULT_ENV] = "no-sent-time"
        self.make("red-ci:pr-unowned:acme/api#8:%s:ci:platform" % self.OPEN_HEAD, 15 * 3600)
        self.deliver()
        first = self.texts()[0].splitlines()[0]
        self.assertNotIn("sent ", first)
        self.assertNotIn("delayed:", first)

    def digests(self, old_age):
        self.make("triage:aaaaaaaaaa:1", old_age, "triage: the old digest", kind="triage")
        self.make("triage:bbbbbbbbbb:2", 60, "triage: the current digest", kind="triage")

    def test_s6_a_digest_older_than_six_hours_is_replaced_by_the_current_one(self):
        self.digests(7 * 3600)
        self.assertTrue(self.deliver()["sent"])
        (text,) = self.texts()
        self.assertIn("the current digest", text)
        self.assertNotIn("the old digest", text)
        (drop,) = self.dropped()
        self.assertEqual((drop["detail"]["kind"], drop["detail"]["fact"]), ("digest", "triage:aaaaaaaaaa:1"))

    def test_s6_mutant_an_old_digest_kept_is_caught(self):
        os.environ[sb.FAULT_ENV] = "keep-old-digest"
        self.digests(7 * 3600)
        self.deliver()
        self.assertIn("the old digest", self.texts()[0])
        self.assertEqual(self.dropped(), [])

    def test_s7_a_digest_five_hours_old_with_no_newer_one_is_delivered_marked_delayed(self):
        self.make("triage:aaaaaaaaaa:1", 5 * 3600, "triage: the old digest", kind="triage")
        self.assertTrue(self.deliver()["sent"])
        self.assertIn("delayed: sent", self.texts()[0].splitlines()[0])
        self.assertEqual(self.dropped(), [])

    def test_a_digest_over_six_hours_with_no_newer_one_is_delivered_delayed_never_lost(self):
        self.make("triage:aaaaaaaaaa:1", 9 * 3600, "triage: the only digest", kind="triage")
        self.assertTrue(self.deliver()["sent"])
        self.assertIn("delayed: sent", self.texts()[0].splitlines()[0])

    def test_a_merged_wake_takes_the_sent_time_of_its_oldest_member(self):
        old = sb.Fact("triage:aaaaaaaaaa:1", "platform", "triage", "old", EPOCH)
        new = sb.Fact("sweep:x", "platform", "sweep", "new", EPOCH + 5 * 3600)
        (merged,) = [f for f in sb.merge_wake_facts("platform", [old, new]) if getattr(f, "members", None)]
        self.assertEqual((merged.created, sb.sent_at(merged)), (EPOCH + 5 * 3600, EPOCH))
        text, _ = sb.compose("platform", [merged], delivered=EPOCH + 5 * 3600)
        self.assertIn("delayed: sent %s" % sb.clock_utc(EPOCH), text.splitlines()[0])

    def test_every_subject_kind_has_a_reader_and_a_stale_and_a_true_case(self):
        cases = {"pull": [("open", {"state": "open", "head": {"sha": "abc"}}, "abc", ""),
                          ("moved", {"state": "open", "head": {"sha": "def"}}, "abc", "the head moved on to def"),
                          ("closed", {"state": "closed", "head": {"sha": "abc"}}, "abc", "the pull request is closed"),
                          ("merged", {"state": "closed", "merged": True, "head": {"sha": "abc"}}, "abc", "the pull request is merged")],
                 "issue": [("open", {"state": "open", "labels": [{"name": "x"}]}, "x", ""),
                           ("closed", {"state": "closed", "labels": [{"name": "x"}]}, "x", "the issue is closed"),
                           ("unlabelled", {"state": "open", "labels": []}, "x", "the label x is gone")],
                 "standing": [("none", {"state": "open", "body": "nothing"}, None, ""),
                              ("posted", {"state": "open", "body": "Standing line (2026-10-27): waiting for 2026-11-02"}, None, "a standing line is already posted"),
                              ("closed", {"state": "closed", "body": ""}, None, "the issue is closed")]}
        self.assertEqual(sorted(cases), sorted(sb.SUBJECT_ENDPOINT))   # a new subject kind with no row here fails
        fact = sb.Fact("k", "platform", "alert", "x", EPOCH)
        for kind, rows in cases.items():
            for name, record, expected, why in rows:
                self.assertEqual(sb.alert_stale((kind, "acme/api", 1, expected), record, fact, sb.triage_body_standing), why, (kind, name))

    def test_other_facts_are_untouched_and_no_hub_means_no_judgement(self):
        self.world.engine.hub = None
        self.make("red-ci:pr-unowned:acme/api#7:%s:ci:platform" % self.OPEN_HEAD, 7200, "red on the old head")
        self.make("lab.fact:1", 7200, "plain", kind="send")
        self.assertTrue(self.deliver()["sent"])
        self.assertEqual(self.dropped(), [])
        self.assertIn("old head", self.texts()[0])
