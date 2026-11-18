"""#2543 / #2545: an orchestrator's non-urgent events ride synchronized :00/:30 UTC buckets; github.pr_green; _chief_lanes picks only the Chief of Staff.

Pure functions and stubs: no world, no state path."""
import json
import os
import tempfile
import time
import types
import unittest
import unittest.mock
from pathlib import Path

from support.loader import sb
from support.wire_fakes import EPOCH
import test_push_watch as tpw
import test_pod_silence as tps

# five minutes past a full hour (08:05:00 UTC), a time that is in no bucket (EPOCH, 1_800_000_000, is itself a multiple of 1800 and of 3600)
BASE = EPOCH - EPOCH % 3600 + 300
BATCH = 90
HOUR = EPOCH - EPOCH % 3600


def seen(role, name="widget-lead"):
    return sb.Observation(name, "ses_example", "w1:p1", "idle", {"name": name, "role": role})


def seen_in(role, name, repo=None):
    fleet = {"name": name, "role": role}
    if repo:
        fleet["repo"] = repo
    return sb.Observation(name, "ses_example", "w1:p1", "idle", fleet)


def _make_fact(key, kind, created=BASE, recipient="widget-lead", tier=None, **extra):
    f = sb.Fact(key, recipient, kind, extra.pop("summary", "s %s" % key), created, **extra)
    f.tier = tier
    return f


def plan(facts, role="orchestrator", now=BASE, name="widget-lead"):
    return sb.decide(name, facts, seen(role, name), now, BATCH, 600)


FLEET = {"fleet-account"}
HUB = types.SimpleNamespace(config={"github_bot_logins": ["Review-Helper"]}, _fleet_login=lambda login: str(login or "").lower() in FLEET)


def item(kind, key=None, sender="alice", actionable=True, **extra):
    """A routed GitHub item as the hub holds it (its own fields: the tier never reads the summary)."""
    return types.SimpleNamespace(key=key or "%s:1" % kind, recipient="widget-lead", kind=kind, summary="PR #4 whatever: t", created=BASE, issue=4,
                                 sender=sender, repo="acme-org/widget-service", actionable=actionable, **extra)


def tier_of(**kw):
    return sb.fact_tier(sb.tiered_fact(item(**kw), sb.Fact, HUB))


def hhmm(epoch):
    return time.strftime("%H:%M:%S", time.gmtime(epoch))


class TestOrchestratorUtcBatching(unittest.TestCase):
    """S1: the four tiers, the jitter, the flush, DELAYED_AFTER from the bucket target."""

    def test_the_jitter_is_deterministic_and_between_0_and_45_seconds(self):
        for name in ["widget-lead", "gadget-lead", "ses_example-a", "x"]:
            self.assertEqual(sb.orchestrator_jitter(name), sb.orchestrator_jitter(name))
            self.assertTrue(0 <= sb.orchestrator_jitter(name) <= 45)
        self.assertGreater(len({sb.orchestrator_jitter("lane-%d" % n) for n in range(40)}), 5)

    def test_tier_0_wakes_at_once(self):
        for f in (_make_fact("send:a", "send", batch=False, sender="boss"), _make_fact("r:1", "reminder"), _make_fact("report:1", "report", batch=False),
                  _make_fact("intent.edit:1", "intent.edit")):
            self.assertEqual(plan([f]).action, "wake", f.kind)

    def test_tier_1_waits_for_the_half_hour_plus_jitter(self):
        jit = sb.orchestrator_jitter("widget-lead")
        for kind in ("github.review", "github.check", "github.conflict", "github.pr_green", "github.comment"):
            p = plan([_make_fact("%s:1" % kind, kind, tier=1)])
            self.assertEqual(p.action, "wait", kind)
            self.assertEqual(hhmm(p.ready_at), "08:30:%02d" % jit)
        one = lambda now: plan([_make_fact("github.check:1", "github.check", tier=1)], now=now).action
        self.assertEqual(one(BASE + 1500 + jit - 1), "wait")
        self.assertEqual(one(BASE + 1500 + jit), "wake")

    def test_a_fact_in_the_last_second_of_a_boundary_waits_for_the_next(self):
        jit = sb.orchestrator_jitter("widget-lead")
        p = plan([_make_fact("github.check:2", "github.check", created=BASE + 1500 + jit + 1, tier=1)], now=BASE + 1500 + jit + 2)
        self.assertEqual(p.ready_at, BASE + 3300 + jit)

    def test_tier_2_waits_for_the_hour(self):
        jit = sb.orchestrator_jitter("widget-lead")
        p = plan([_make_fact("github.stage:1", "github.stage", tier=2)])
        self.assertEqual((p.action, hhmm(p.ready_at)), ("wait", "09:00:%02d" % jit))

    def test_every_branch_of_the_tier_table(self):
        table = [
            (dict(kind="github.conflict"), 1), (dict(kind="github.pr_green"), 1), (dict(kind="github.stage"), 2),
            (dict(kind="github.pr", actionable=False), 3), (dict(kind="github.subissue"), 3),
            (dict(kind="github.subissue", sender="fleet-account"), "drop"), (dict(kind="github.pr", sender="fleet-account", actionable=False), "drop"),
            (dict(kind="github.check"), 1), (dict(kind="github.check", actionable=False), 3),   # failed; a passing check only rides
            (dict(kind="github.review"), 1), (dict(kind="github.review", actionable=False), 3),   # changes requested; approved or commented ride
            (dict(kind="github.review", key="github.botreview:helper:abc"), 2),
            (dict(kind="github.comment"), 1), (dict(kind="github.comment", sender="helper[bot]", actionable=False), 3),
            (dict(kind="github.comment", sender="review-helper", actionable=True), 3),   # a configured bot login
            (dict(kind="github.comment", key="github.reviewcomment:9"), 1),   # a human's review comment
            (dict(kind="github.comment", key="github.reviewcomment:9", sender="helper[bot]"), 2),
            (dict(kind="github.comment", key="github.reviewcomment:9", sender="Review-Helper"), 2),
            (dict(kind="github.issue", action="labeled", label="alice:awaiting-answer", actor="alice"), 1),
            (dict(kind="github.issue", action="unlabeled", label="alice:awaiting-answer", actor="alice"), 3),   # removed: not a wake
            (dict(kind="github.issue", action="labeled", label="bug", actor="alice"), 3),
            (dict(kind="github.issue", action="closed", actor="alice"), 1), (dict(kind="github.issue", action="reopened", actor="alice"), 1),
            (dict(kind="github.issue", action="closed", actor="fleet-account"), "drop"),   # our own close
            (dict(kind="github.issue", action="edited", actor="alice"), 3), (dict(kind="github.issue"), 3),
            (dict(kind="alert"), None),
        ]
        for kw, want in table:
            self.assertEqual(tier_of(**kw), want, kw)

    def test_self_echo_issue_and_subissue_events_are_dropped_for_an_orchestrator(self):
        def facts(*items):
            return [sb.tiered_fact(i, sb.Fact, HUB) for i in items]
        own = [item("github.issue", key="github.issue:own", action="closed", actor="fleet-account"),
               item("github.subissue", key="github.subissue:own", sender="fleet-account")]
        ext = [item("github.issue", key="github.issue:ext", action="edited", actor="alice"),
               item("github.subissue", key="github.subissue:ext", sender="alice")]
        send = _make_fact("send:a", "send", batch=False, sender="boss")
        p = plan([send] + facts(*own))
        self.assertEqual(p.action, "wake")
        self.assertEqual([f.key for f in p.pending], ["send:a"])
        p = plan([send] + facts(*own, *ext))
        self.assertEqual(p.action, "wake")
        self.assertEqual(sorted(f.key for f in p.pending), ["github.issue:ext", "github.subissue:ext", "send:a"])
        self.assertIsNone(plan(facts(*own)))
        p = plan(facts(*own), role="chief-of-staff")
        self.assertIsNotNone(p)
        self.assertEqual(len(p.pending), 2)

    def test_a_summary_that_looks_like_a_wake_does_not_make_one(self):
        f = sb.tiered_fact(item("github.issue", action="unlabeled", label="alice:awaiting-answer", actor="alice", ), sb.Fact, HUB)
        f.summary = "issue #4 label alice:awaiting-answer added: closed"
        self.assertEqual(sb.fact_tier(f), 3)

    def test_the_other_kinds_and_a_missing_tier_keep_the_batch_window(self):
        self.assertIsNone(sb.fact_tier(_make_fact("alert:1", "alert")))
        self.assertIsNone(sb.fact_tier(_make_fact("github.check:1", "github.check")))   # no tier recorded: not an orchestrator tier
        p = plan([_make_fact("alert:1", "alert")])
        self.assertEqual((p.action, p.ready_at), ("wait", BASE + BATCH))
        mixed = plan([_make_fact("alert:1", "alert"), _make_fact("github.stage:1", "github.stage", tier=2)])
        self.assertEqual(mixed.ready_at, BASE + BATCH)   # the earliest of an untiered fact and a bucket
        self.assertEqual(sb.orchestrator_ready_at("widget-lead", [_make_fact("alert:1", "alert")], BATCH), BASE + BATCH)

    def test_tier_3_never_makes_a_standalone_wake(self):
        for kind in ("github.pr", "github.subissue", "github.issue"):
            self.assertIsNone(plan([_make_fact("%s:1" % kind, kind, tier=3)], now=BASE + 7200), kind)
        self.assertIsNone(plan([_make_fact("x", "github.pr", rider=True)], now=BASE + 7200))

    def test_any_wake_carries_every_pending_tier(self):
        pending = [_make_fact("send:a", "send", batch=False, sender="boss"), _make_fact("github.stage:1", "github.stage", tier=2),
                   _make_fact("github.check:1", "github.check", tier=1), _make_fact("github.pr:1", "github.pr", tier=3)]
        p = plan(pending)
        self.assertEqual(p.action, "wake")
        self.assertEqual({f.key for f in p.pending}, {f.key for f in pending})

    def test_the_chief_of_staff_and_workers_keep_the_batch_window(self):
        for role in ("chief-of-staff", "coder", "tester"):
            f = _make_fact("github.check:1", "github.check", tier=1)
            self.assertEqual(plan([f], role=role).ready_at, BASE + BATCH, role)
            self.assertEqual(plan([f], role=role, now=BASE + BATCH).action, "wake", role)

    def test_delayed_after_is_measured_from_the_bucket_target(self):
        jit = sb.orchestrator_jitter("widget-lead")
        stage = _make_fact("github.stage:1", "github.stage", tier=2)   # created 08:05, held to 09:00 + jitter
        p = plan([stage], now=BASE + 3300 + jit)
        self.assertEqual(p.action, "wake")
        self.assertIsNone(sb.delayed_since(p.pending, BASE + 3300 + jit + 5))   # 55 minutes old, but on time for its bucket
        self.assertIsNotNone(sb.delayed_since(p.pending, BASE + 3300 + jit + sb.DELAYED_AFTER + 5))   # late from the target
        plain = _make_fact("alert:1", "alert", created=BASE)
        self.assertIsNotNone(sb.delayed_since([plain], BASE + sb.DELAYED_AFTER + 5))   # no bucket: measured from the arrival, as before

    def test_a_tier_0_send_flushes_old_tier_2_and_tier_3_facts_with_no_delayed_flag(self):
        now = BASE + 3000
        stage = _make_fact("github.stage:1", "github.stage", created=now - 25 * 60, tier=2)
        rider = _make_fact("github.pr:1", "github.pr", created=now - 40 * 60, tier=3)
        send = _make_fact("send:a", "send", created=now, batch=False, sender="boss")
        p = plan([stage, rider, send], now=now)
        self.assertEqual(p.action, "wake")
        self.assertEqual(len(p.pending), 3)
        self.assertIsNone(sb.delayed_since(p.pending, now))
        # the same facts to a worker keep the arrival rule: the 40 minute old rider is late there
        old = _make_fact("github.pr:2", "github.pr", created=now - 40 * 60, tier=3)
        w = plan([old, send], role="coder", now=now)
        self.assertIsNotNone(sb.delayed_since(w.pending, now))

    def test_a_fact_held_for_a_blocked_orchestrator_past_its_bucket_is_late_from_the_target(self):
        jit = sb.orchestrator_jitter("widget-lead")
        stage = _make_fact("github.stage:1", "github.stage", tier=2)
        plan([stage], now=BASE)
        self.assertIsNotNone(sb.delayed_since([stage], BASE + 3300 + jit + sb.DELAYED_AFTER + 5))


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
        self.assertEqual(sb.lane_gate(lanes("coder"), "widget-lead", 0, BASE, BASE), "go")

    def test_a_quiet_pod_whose_record_names_a_cw_run_job_is_not_told(self):
        d = lanes()
        self.assertEqual(sb.lane_gate(d, "widget-lead", 2, BASE + 99999, BASE, "w-1", pod_jobs=["build"]), "wait")
        self.assertEqual(sb.lane_gate(d, "widget-lead", 2, BASE + 99999, BASE, "w-1", pod_jobs=[]), "go")

    def test_a_quiet_pod_with_a_live_detached_job_is_not_told(self):
        d = lanes()
        alive = lambda name, root=None: name == "w-1"
        self.assertEqual(sb.lane_gate(d, "widget-lead", 2, BASE + 99999, BASE, "w-1", jobs_alive=alive), "wait")
        self.assertEqual(sb.lane_gate(d, "widget-lead", 2, BASE + 99999, BASE, "w-2", jobs_alive=alive), "go")

    def test_a_fleet_that_was_not_read_waits_whatever_the_lane(self):
        for d in (None, types.SimpleNamespace(problems=["herdr is down"], agents={})):
            self.assertEqual(sb.lane_gate(d, "widget-lead", 2, BASE + 99999, BASE), "wait")
            self.assertEqual(sb.lane_gate(d, "widget-lead", 1, BASE + 99999, BASE), "wait")
            self.assertEqual(sb.lane_gate(d, "widget-lead", 3, BASE, BASE), "wait")
            self.assertEqual(sb.lane_gate(d, "widget-lead", 0, BASE, BASE), "go")


class TestJobsActiveFor(unittest.TestCase):
    """jobs_active_for on a real temporary directory: a live job of the worker, a finished one, another worker's, and a directory that is not private."""

    def plant(self, root, name, starter, pid=None, code=None):
        d = Path(root) / name
        d.mkdir(mode=0o700)
        (d / "job.json").write_text(json.dumps({"name": name, "starter": starter, "pid": pid}))
        if code is not None:
            (d / "exit").write_text("%d\n" % code)

    def make(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "jobs"
        root.mkdir(mode=0o700)
        os.chmod(str(root), 0o700)
        return root

    def test_only_a_live_job_of_that_worker_counts(self):
        root = self.make()
        self.plant(root, "build-live", "w-1", pid=os.getpid())
        self.plant(root, "build-done", "w-2", pid=os.getpid(), code=0)   # finished: the exit file says so
        self.plant(root, "other", "w-3", pid=os.getpid())
        self.assertTrue(sb.jobs_active_for("w-1", root))
        self.assertFalse(sb.jobs_active_for("w-2", root))
        self.assertTrue(sb.jobs_active_for("w-3", root))
        self.assertFalse(sb.jobs_active_for("w-4", root))

    def test_a_job_whose_process_is_gone_is_not_live(self):
        root = self.make()
        self.plant(root, "gone", "w-1", pid=2 ** 22 + 12345)
        self.assertFalse(sb.jobs_active_for("w-1", root))

    def test_a_directory_others_can_write_or_no_directory_is_not_believed(self):
        root = self.make()
        self.plant(root, "build-live", "w-1", pid=os.getpid())
        os.chmod(str(root), 0o777)
        self.assertFalse(sb.jobs_active_for("w-1", root))
        self.assertFalse(sb.jobs_active_for("w-1", root / "missing"))


class TestGatedDaemonSends(unittest.TestCase):
    """pod_silence.scan and push_watch.run_pass with an orchestrator recipient and the real gate."""

    def gate(self, now, role="orchestrator"):
        d = types.SimpleNamespace(problems=[], agents={"lane-a": seen(role, "lane-a")})
        return lambda lane, tier, since, worker=None, jobs=None: sb.lane_gate(d, lane, tier, now[0], since, worker, pod_jobs=jobs)

    def scan_gate(self, now, role="orchestrator"):
        """The gate pod_silence_tick hands to scan: a quiet pod is tier 2."""
        gate = self.gate(now, role)
        return lambda lane, since, worker, jobs=None: gate(lane, 2, since, worker, jobs)

    def test_a_quiet_pod_is_told_to_an_orchestrator_at_its_hour_and_not_before(self):
        module, _ = tps.load()
        now, sent = [tps.NOW], []
        gate = self.scan_gate(now)
        jit = sb.orchestrator_jitter("lane-a")
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "s.json")
            pods = [tps.pod("silent")]
            module.scan([dict(p) for p in pods], tps.NOW - 30 * 60, state, lambda to, t: None, lambda n: "x", json.loads, gate=lambda *a: "go")   # first seen 30 minutes ago
            hour = tps.NOW + jit   # NOW is on the hour: a pod quiet since before it is held to that hour plus the jitter
            for at, told in ((tps.NOW - 1, 0), (hour - 1, 0), (hour, 1), (hour + 60, 1)):
                now[0] = at
                module.scan(pods, at, state, lambda to, t: sent.append(to), lambda n: "stub summary", json.loads, gate=gate)
                self.assertEqual(len(sent), told, at)
            self.assertEqual(sent, ["lane-a"])

    def test_a_quiet_pod_with_a_cw_run_job_waits_and_a_chief_of_staff_lane_is_told_at_once(self):
        module, _ = tps.load()
        for role, jobs, want in (("orchestrator", ["build"], 0), ("chief-of-staff", ["build"], 1), ("orchestrator", [], 0)):
            now, sent = [tps.NOW + 5 * 3600], []
            with tempfile.TemporaryDirectory() as tmp:
                state = str(Path(tmp) / "s.json")
                pods = [dict(tps.pod("silent"), jobs=jobs)]
                module.scan([dict(p) for p in pods], tps.NOW, state, lambda to, t: None, lambda n: "x", json.loads, gate=lambda *a: "go")
                gate = self.scan_gate(now, role)
                if role == "orchestrator" and not jobs:   # no job, an hour long past: told
                    want = 1
                module.scan(pods, now[0], state, lambda to, t: sent.append(to), lambda n: "stub", json.loads, gate=gate)
                self.assertEqual(len(sent), want, (role, jobs))

    def tick_at(self, role, jobs, clock):
        """The real pod_silence_tick (a stub engine, a stub summary), the pod first seen at HOUR + 5 minutes, then ticked at `clock`."""
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            rec = {"worker": "w-a", "reports_to": "lane-a", "hard_deadline": tps._when(HOUR + 86400)}
            if jobs is not None:
                rec["jobs"] = jobs
            engine = types.SimpleNamespace(state_dir=Path(tmp), cloud_watch={"records": [rec], "capped": False},
                                           send=lambda frm, to, text: sent.append((to, text)), audit=None, actor="switchboard")
            discovery = types.SimpleNamespace(problems=[], agents={"lane-a": seen(role, "lane-a")})
            for at in (HOUR + 300, clock):
                with unittest.mock.patch.object(sb.time, "time", lambda at=at: at):
                    sb.pod_silence_tick(engine, discovery, lambda w: "summary for " + w)
        return sent

    def test_the_real_pod_silence_tick_gates_an_orchestrator_boss_by_bucket_and_running_jobs(self):
        jit = sb.orchestrator_jitter("lane-a")
        nextbucket = HOUR + 3600 + jit
        running = [{"state": "running"}]
        self.assertEqual(self.tick_at("orchestrator", None, nextbucket - 1), [])     # quiet since :25, held to the next :00 + jitter
        self.assertEqual([to for to, _ in self.tick_at("orchestrator", None, nextbucket)], ["lane-a"])
        self.assertEqual(self.tick_at("orchestrator", running, nextbucket), [])     # a running job holds it
        self.assertEqual([to for to, _ in self.tick_at("chief-of-staff", running, HOUR + 300 + 21 * 60)], ["lane-a"])   # not an orchestrator: at once
        self.assertEqual(self.tick_at("coder", None, HOUR + 300 + 19 * 60), [])      # and not before its 20 quiet minutes

    def run_push(self, role, steps):
        pw = tpw.load()
        world = tpw.PushWorld()
        world.commits = {tpw.SHA_A: tpw.commit()}
        world.refs = {"w-a/wip": tpw.SHA_A}
        with tempfile.TemporaryDirectory() as tmp:
            launcher = str(Path(tmp) / "launcher.jsonl")
            Path(launcher).touch()
            engine = tpw.Engine(tmp, [tpw.record("w-a", "lane-a")])
            now = [steps[0][0]]
            ns = types.SimpleNamespace(parse_iso=tpw.parse_iso, _one_line=lambda t, n=200: str(t)[:n], parse_bounded=json.loads)
            engine.lane_gate = self.gate(now, role)   # the daemon's Switchboard.lane_gate
            sent_after = []
            for at in [t for t, _ in steps]:
                now[0] = at
                pw.run_pass(ns, engine, at, world, {"interval": 0, "quiet_minutes": 10}, launcher)
                sent_after.append(len(engine.sent))
            return sent_after, [e for e, _, _ in engine.audit.lines], engine.sent

    def test_a_feature_branch_push_is_dropped_for_an_orchestrator_and_its_quiet_item_waits_for_the_hour(self):
        jit = sb.orchestrator_jitter("lane-a")
        hour = tps.NOW - tps.NOW % 3600 + 3600 + jit
        counts, events, sent = self.run_push("orchestrator", [(tps.NOW, 0), (tps.NOW + 700, 0), (hour - 1, 0), (hour, 0)])
        self.assertEqual(counts, [0, 0, 0, 1])   # the push item is dropped (told to nobody); the quiet item goes at the hour
        self.assertEqual(events.count("push_watch.dropped"), 1)
        self.assertNotIn("push_watch.sent", events)
        self.assertEqual(events.count("push_watch.quiet"), 1)
        self.assertIn("quiet", sent[0][1].lower())

    def test_the_same_push_is_told_at_once_to_a_chief_of_staff_lane(self):
        counts, events, _ = self.run_push("chief-of-staff", [(tps.NOW, 0)])
        self.assertEqual(counts, [1])
        self.assertIn("push_watch.sent", events)
        self.assertNotIn("push_watch.dropped", events)


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
        hub = types.SimpleNamespace(engine=types.SimpleNamespace(notice_facts=self.held()), queue={}, lock=_NoLock())
        sb.queue_pr_green(hub, BASE, sb.GitHubItem)
        self.assertEqual(list(hub.queue), ["github.pr_green:acme-org/widget-service#7:%s" % ("a" * 40)])
        item = next(iter(hub.queue.values()))
        self.assertEqual((item.kind, item.numbers, item.lookups, item.actionable), ("github.pr_green", [7], [("pr", 7)], True))
        hub.queue.clear()
        sb.queue_pr_green(hub, BASE + 60, sb.GitHubItem)
        self.assertEqual(hub.queue, {})
        self.assertEqual(hub._green, {("acme-org/widget-service", 7, "a" * 40)})   # made at the first pass, because the stub hub had none

    def test_the_told_record_is_cleared_when_it_passes_20000(self):
        hub = types.SimpleNamespace(engine=types.SimpleNamespace(notice_facts=self.held()), queue={}, lock=_NoLock())
        hub._green = {("acme-org/other-service", n, "c" * 40) for n in range(20001)}
        sb.queue_pr_green(hub, BASE, sb.GitHubItem)
        self.assertEqual(hub._green, {("acme-org/widget-service", 7, "a" * 40)})   # the old record dropped, the new head kept
        self.assertEqual(len(hub.queue), 1)
        hub._green = {("acme-org/other-service", n, "c" * 40) for n in range(20000)}   # at the cap exactly: kept, and the new one added
        hub.queue.clear()
        sb.queue_pr_green(hub, BASE, sb.GitHubItem)
        self.assertEqual(len(hub._green), 20001)

    def test_the_item_is_a_tier_1_wake_for_an_orchestrator(self):
        self.assertEqual(tier_of(kind="github.pr_green", key="github.pr_green:r#7:h", sender=None), 1)


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestChiefLanesRoleFilter(unittest.TestCase):
    """S4: GitHubHub._chief_lanes selects only role == "chief-of-staff", keeping the repo preference."""

    def agents(self, **repos):
        return {"cos": seen_in("chief-of-staff", "cos", repos.get("cos")), "widget-lead": seen_in("orchestrator", "widget-lead", repos.get("lead")),
                "w-1": seen_in("coder", "w-1"), "t-1": seen_in("tester", "t-1")}

    def lanes(self, agents, repo="acme-org/widget-service"):
        hub = types.SimpleNamespace()
        return sb.GitHubHub._chief_lanes(hub, types.SimpleNamespace(repo=repo), agents)

    def test_only_the_chief_of_staff_is_returned(self):
        self.assertEqual(self.lanes(self.agents()), ["cos"])
        self.assertEqual(self.lanes({"widget-lead": seen("orchestrator")}), [])

    def test_an_orchestrator_bound_to_this_repository_is_still_excluded(self):
        agents = self.agents(cos="acme-org/widget-service", lead="acme-org/widget-service")
        self.assertNotIn("widget-lead", self.lanes(agents))
        self.assertEqual(self.lanes({"widget-lead": agents["widget-lead"]}), [])
        self.assertEqual(self.lanes(agents), ["cos"])

    def test_a_lane_bound_to_this_repository_comes_first_and_another_repository_is_left_out(self):
        agents = self.agents(cos="acme-org/widget-service")
        agents["cos-2"] = seen_in("chief-of-staff", "cos-2", None)
        agents["cos-3"] = seen_in("chief-of-staff", "cos-3", "acme-org/other-service")
        self.assertEqual(self.lanes(agents), ["cos"])
        del agents["cos"]
        self.assertEqual(self.lanes(agents), ["cos-2"])

    def test_the_hub_chiefs_of_staff_are_the_same_role(self):
        agents = self.agents()
        hub = types.SimpleNamespace(discover=lambda: types.SimpleNamespace(agents=agents, problems=[]))
        self.assertEqual(sb.GitHubHub._chiefs_of_staff(hub), ["cos"])
