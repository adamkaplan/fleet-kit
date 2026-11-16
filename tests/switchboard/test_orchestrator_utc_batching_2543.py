"""#2543 / #2545: an orchestrator's non-urgent events ride synchronized :00/:30 UTC buckets; github.pr_green; _chief_lanes picks only the Chief of Staff.

Pure functions and stubs: no world, no state path."""
import time
import types
import unittest

from support.loader import sb
from support.wire_fakes import EPOCH

# five minutes past a full hour (08:05:00 UTC), a time that is in no bucket (EPOCH, 1_800_000_000, is itself a multiple of 1800 and of 3600)
BASE = EPOCH - EPOCH % 3600 + 300
BATCH = 90


def seen(role, name="widget-lead"):
    return sb.Observation(name, "ses_example", "w1:p1", "idle", {"name": name, "role": role})


def fact(key, kind, created=BASE, recipient="widget-lead", **extra):
    return sb.Fact(key, recipient, kind, extra.pop("summary", "s %s" % key), created, **extra)


def plan(facts, role="orchestrator", now=BASE, name="widget-lead"):
    return sb.decide(name, facts, seen(role, name), now, BATCH, 600)


def hhmm(epoch):
    return time.strftime("%H:%M:%S", time.gmtime(epoch))


class TestOrchestratorUtcBatching(unittest.TestCase):
    """S1: the four tiers, the jitter, the flush, DELAYED_AFTER from the bucket target."""

    def test_the_jitter_is_deterministic_and_between_0_and_45_seconds(self):
        names = ["widget-lead", "gadget-lead", "ses_example-a", "x"]
        for name in names:
            self.assertEqual(sb.orchestrator_jitter(name), sb.orchestrator_jitter(name))
            self.assertTrue(0 <= sb.orchestrator_jitter(name) <= 45)
        self.assertGreater(len({sb.orchestrator_jitter("lane-%d" % n) for n in range(40)}), 5)

    def test_tier_0_wakes_at_once(self):
        for f in (fact("send:a", "send", batch=False, sender="boss"), fact("r:1", "reminder"), fact("report:1", "report", batch=False),
                  fact("intent.edit:1", "intent.edit")):
            self.assertEqual(plan([f]).action, "wake", f.kind)

    def test_tier_1_waits_for_the_half_hour_plus_jitter(self):
        jit = sb.orchestrator_jitter("widget-lead")
        for kind, key in (("github.review", "github.review:1"), ("github.check", "github.check:1"), ("github.conflict", "github.conflict:1"),
                          ("github.pr_green", "github.pr_green:1"), ("github.comment", "github.comment:1")):
            p = plan([fact(key, kind, sender="alice")])
            self.assertEqual(p.action, "wait", kind)
            self.assertEqual(hhmm(p.ready_at), "08:30:%02d" % jit)
        self.assertEqual(plan([fact("github.check:1", "github.check")], now=BASE + 1500 + jit - 1).action, "wait")
        self.assertEqual(plan([fact("github.check:1", "github.check")], now=BASE + 1500 + jit).action, "wake")

    def test_a_fact_in_the_last_second_of_a_boundary_waits_for_the_next(self):
        jit = sb.orchestrator_jitter("widget-lead")
        p = plan([fact("github.check:2", "github.check", created=BASE + 1500 + jit + 1)], now=BASE + 1500 + jit + 2)
        self.assertEqual(p.ready_at, BASE + 3300 + jit)

    def test_tier_2_waits_for_the_hour(self):
        jit = sb.orchestrator_jitter("widget-lead")
        for f in (fact("github.stage:1", "github.stage"),
                  fact("github.reviewcomment:1", "github.comment", sender="review-bot[bot]")):
            p = plan([f])
            self.assertEqual(p.action, "wait")
            self.assertEqual(hhmm(p.ready_at), "09:00:%02d" % jit)

    def test_a_human_review_comment_is_tier_1(self):
        self.assertEqual(sb.fact_tier(fact("github.reviewcomment:2", "github.comment", sender="alice")), 1)

    def test_routine_issue_churn_is_tier_3_and_an_awaiting_label_or_a_close_is_tier_1(self):
        self.assertEqual(sb.fact_tier(fact("github.issue:1", "github.issue", summary="issue #4 edited: t")), 3)
        self.assertEqual(sb.fact_tier(fact("github.issue:2", "github.issue", summary="issue #4 closed: t")), 1)
        self.assertEqual(sb.fact_tier(fact("github.issue:3", "github.issue", summary="issue #4 label alice:awaiting-answer added: t")), 1)

    def test_tier_3_never_makes_a_standalone_wake(self):
        for kind in ("github.pr", "github.subissue", "github.issue"):
            self.assertIsNone(plan([fact("%s:1" % kind, kind, summary="issue #4 edited: t")], now=BASE + 7200), kind)
        self.assertIsNone(plan([fact("x", "github.pr", rider=True)], now=BASE + 7200))

    def test_any_wake_carries_every_pending_tier(self):
        pending = [fact("send:a", "send", batch=False, sender="boss"), fact("github.stage:1", "github.stage"),
                   fact("github.check:1", "github.check"), fact("github.pr:1", "github.pr")]
        p = plan(pending)
        self.assertEqual(p.action, "wake")
        self.assertEqual({f.key for f in p.pending}, {f.key for f in pending})

    def test_the_chief_of_staff_and_workers_keep_the_batch_window(self):
        for role in ("chief-of-staff", "coder", "tester"):
            f = fact("github.check:1", "github.check")
            self.assertEqual(plan([f], role=role).action, "wait")
            self.assertEqual(plan([f], role=role).ready_at, BASE + BATCH, role)
            self.assertEqual(plan([f], role=role, now=BASE + BATCH).action, "wake", role)

    def test_delayed_after_is_measured_from_the_bucket_target(self):
        jit = sb.orchestrator_jitter("widget-lead")
        stage = fact("github.stage:1", "github.stage")   # created 08:05, held to 09:00 + jitter
        p = plan([stage], now=BASE + 3300 + jit)
        self.assertEqual(p.action, "wake")
        self.assertIsNone(sb.delayed_since(p.pending, BASE + 3300 + jit + 5))   # 55 minutes old, but on time for its bucket
        self.assertIsNotNone(sb.delayed_since(p.pending, BASE + 3300 + jit + sb.DELAYED_AFTER + 5))   # late from the target
        plain = fact("alert:1", "alert", created=BASE)
        self.assertIsNotNone(sb.delayed_since([plain], BASE + sb.DELAYED_AFTER + 5))   # no bucket: measured from the arrival, as before


def lanes(role="orchestrator"):
    return types.SimpleNamespace(problems=[], agents={"widget-lead": seen(role)})


class TestOrchestratorLaneGate(unittest.TestCase):
    """The messages the daemon sends itself (push_watch, pod_silence) follow the same tiers."""

    def test_quiet_items_wait_for_the_hour_and_feature_pushes_are_dropped(self):
        jit = sb.orchestrator_jitter("widget-lead")
        d = lanes()
        self.assertEqual(sb.lane_gate(d, "widget-lead", 2, BASE + 3300 + jit - 1, BASE), "wait")
        self.assertEqual(sb.lane_gate(d, "widget-lead", 2, BASE + 3300 + jit, BASE), "go")
        self.assertEqual(sb.lane_gate(d, "widget-lead", 3, BASE, BASE), "drop")
        self.assertEqual(sb.lane_gate(d, "widget-lead", 0, BASE, BASE), "go")

    def test_a_lane_that_is_not_an_orchestrator_is_never_gated(self):
        for role in ("chief-of-staff", "coder"):
            self.assertEqual(sb.lane_gate(lanes(role), "widget-lead", 3, BASE, BASE), "go")
        self.assertEqual(sb.lane_gate(None, "widget-lead", 3, BASE, BASE), "go")

    def test_a_quiet_pod_with_a_live_detached_job_is_not_told(self):
        d = lanes()
        orig = sb.jobs_active_for
        try:
            sb.jobs_active_for = lambda name, root=None: name == "w-1"
            self.assertEqual(sb.lane_gate(d, "widget-lead", 2, BASE + 99999, BASE, "w-1"), "wait")
            self.assertEqual(sb.lane_gate(d, "widget-lead", 2, BASE + 99999, BASE, "w-2"), "go")
        finally:
            sb.jobs_active_for = orig


class TestPrGreenEvent(unittest.TestCase):
    """S3: github.pr_green once per (repo, pr, head sha), only when open and all checks finished green."""

    def held(self, ci="green", state="open", head="a" * 40):
        return {"acme-org/widget-service#7": {"at": 0, "facts": {"state": state, "ci": ci, "head": head}}}

    def test_green_open_pr_is_noted_once_per_head(self):
        told = set()
        found = sb.pr_green_notes(self.held(), told)
        self.assertEqual(found, [("acme-org/widget-service", 7, "a" * 40)])
        told.add(found[0])
        self.assertEqual(sb.pr_green_notes(self.held(), told), [])
        self.assertEqual(len(sb.pr_green_notes(self.held(head="b" * 40), told)), 1)

    def test_pending_failing_none_or_closed_is_not_green(self):
        for kw in ({"ci": "pending"}, {"ci": "failing"}, {"ci": "none"}, {"state": "closed"}, {"state": "merged"}, {"head": None}):
            self.assertEqual(sb.pr_green_notes(self.held(**kw), set()), [], kw)
        self.assertEqual(sb.pr_green_notes({"bad": {"facts": None}, "acme-org/widget-service#x": {"facts": {"state": "open", "ci": "green", "head": "h"}}}, set()), [])

    def test_the_hub_queues_one_item_and_a_second_pass_queues_none(self):
        hub = types.SimpleNamespace(engine=types.SimpleNamespace(notice_facts=self.held()), _green=set(), queue={}, lock=_NoLock())
        sb.GitHubHub.note_pr_green(hub, BASE)
        self.assertEqual(list(hub.queue), ["github.pr_green:acme-org/widget-service#7:%s" % ("a" * 40)])
        item = next(iter(hub.queue.values()))
        self.assertEqual((item.kind, item.numbers, item.lookups, item.actionable), ("github.pr_green", [7], [("pr", 7)], True))
        hub.queue.clear()
        sb.GitHubHub.note_pr_green(hub, BASE + 60)
        self.assertEqual(hub.queue, {})

    def test_the_item_is_a_tier_1_wake_for_an_orchestrator(self):
        f = fact("github.pr_green:r#7:h", "github.pr_green")
        self.assertEqual(sb.fact_tier(f), 1)


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestChiefLanesRoleFilter(unittest.TestCase):
    """S4: only role == "chief-of-staff"."""

    def test_only_the_chief_of_staff_is_returned(self):
        agents = {"cos": seen("chief-of-staff", "cos"), "widget-lead": seen("orchestrator"), "w-1": seen("coder", "w-1"), "t-1": seen("tester", "t-1")}
        self.assertEqual(sb._chief_lanes(agents), ["cos"])
        self.assertEqual(sb._chief_lanes({"widget-lead": seen("orchestrator")}), [])

    def test_the_hub_uses_it(self):
        agents = {"cos": seen("chief-of-staff", "cos"), "widget-lead": seen("orchestrator")}
        hub = types.SimpleNamespace(discover=lambda: types.SimpleNamespace(agents=agents, problems=[]))
        self.assertEqual(sb.GitHubHub._chiefs_of_staff(hub), ["cos"])
