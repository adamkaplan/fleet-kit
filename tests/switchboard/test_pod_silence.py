"""Tests of the pod silence watch (ask #2115, bin/pod_silence.py): a fake world, a stub summary, no cluster, no thread."""
import datetime
import json
import tempfile
import time
import types
import unittest
import unittest.mock
from pathlib import Path

from support.loader import sb

REPO = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0
MIN = 60
load_real = sb.load_module


def load(mutant=False, prune_mutant=False):
    text = (REPO / "bin" / "pod_silence.py").read_text()
    applied = False
    if prune_mutant:   # a capped pass prunes too: the told mark of an unread pod is lost
        new = text.replace("    if prune:\n", "    if True:\n", 1)
        applied = new != text
        text = new
    if mutant:   # no once-only state: the told mark is never kept
        new = text.replace('mem["told"] = now\n', 'mem["told"] = None\n', 1)
        applied = new != text
        text = new
    module = types.ModuleType("pod_silence_under_test")
    exec(compile(text, "pod_silence.py", "exec"), module.__dict__)
    return module, applied


def pod(name, report=None, push=None, done=False):
    return {"worker": name, "boss": "lane-a", "last_report": report, "last_push": push, "done": done}


class TestPodSilence(unittest.TestCase):
    def run_passes(self, module, pods, passes=2):
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "s.json")
            first = NOW - 30 * MIN   # first seen 30 minutes ago
            module.scan([dict(p) for p in pods], first, state, lambda to, t: None, lambda n: "x", json.loads)
            for i in range(passes):
                module.scan(pods, NOW + i, state, lambda to, t: sent.append((to, t)), lambda n: "stub summary of " + n, json.loads)
        return sent

    def test_the_four_cases_and_a_second_pass(self):
        module, _ = load()
        pods = [pod("silent"), pod("pushed", push=NOW - 5 * MIN), pod("reported", report=NOW - 5 * MIN), pod("finished", report=NOW - 25 * MIN, done=True)]
        sent = self.run_passes(module, pods, passes=2)
        self.assertEqual([to for to, _ in sent], ["lane-a"])
        self.assertIn("silent", sent[0][1])
        self.assertIn("stub summary of silent", sent[0][1])

    def test_a_retired_pod_is_not_told(self):
        module, _ = load()
        self.assertEqual(self.run_passes(module, [], passes=1), [])

    def test_mutant_without_the_once_only_state_sends_two(self):
        control, applied0 = load()
        self.assertFalse(applied0)
        self.assertEqual(len(self.run_passes(control, [pod("silent")], 2)), 1)   # control first
        mutant, applied = load(mutant=True)
        self.assertTrue(applied)   # applied: computed from the two texts
        self.assertNotEqual(len(self.run_passes(mutant, [pod("silent")], 2)), 1)   # a no-op mutant would fail here
        self.assertEqual(len(self.run_passes(mutant, [pod("silent")], 2)), 2)



def _when(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestPodSilenceTick(unittest.TestCase):
    """The wiring of pod_silence_tick (bin/fleet-switchboard): a stub engine, a stub summary; records are dated by the real clock."""

    def world(self, records, capped=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sent, self.reads = [], []
        return types.SimpleNamespace(state_dir=Path(tmp.name), cloud_watch={"records": records, "capped": capped},
                                     send=lambda frm, to, text: self.sent.append((to, text)))

    def summary(self, name):
        self.reads.append(name)
        return "stub of " + name

    def rec(self, name, **extra):
        base = {"worker": name, "reports_to": "lane-a", "hard_deadline": _when(time.time() + 7200)}
        base.update(extra)
        return base

    def tick(self, engine, ahead=0):
        real = time.time
        with unittest.mock.patch.object(sb.time, "time", lambda: real() + ahead):
            return sb.pod_silence_tick(engine, None, self.summary)

    def test_a_live_pod_is_told_after_the_quiet_period_and_the_boss_is_cleaned(self):
        engine = self.world([self.rec("live", reports_to="lane\x07-a")])
        self.assertEqual(self.tick(engine), [])
        got = self.tick(engine, ahead=21 * MIN)
        self.assertEqual([to for to, _ in self.sent], ["lane-a"])   # _cloud_boss removed the control character
        self.assertEqual(len(got), 1)

    def test_failed_retired_and_past_deadline_records_read_and_send_nothing(self):
        engine = self.world([self.rec("failed"), self.rec("gone", retired=True), self.rec("late", hard_deadline=_when(time.time() - 60))])
        (engine.state_dir / "last-reports.json").write_text(json.dumps({"failed": {"state": "failed", "at": 1}}))
        self.tick(engine)
        self.tick(engine, ahead=30 * MIN)
        self.assertEqual(self.reads, [])
        self.assertEqual(self.sent, [])

    def test_a_pod_the_registry_retired_is_not_told_while_a_live_one_is(self):
        """The view reads the registry: retire the name, then the tick (the control, a live pod, is told)."""
        engine = self.world([self.rec("keep"), self.rec("retired-in-registry")])
        (engine.state_dir / sb.PANE_HEALTH_FILE).write_text(json.dumps({"agents": {}, "states": {}, "missing": {}}))
        (engine.state_dir / sb.RETIRED_FILE).write_text(json.dumps({"retired-in-registry": {"at": 1, "reason": "finished", "session": "s"}}))
        engine.state_dir = str(engine.state_dir)
        self.tick(engine)
        self.tick(engine, ahead=30 * MIN)
        self.assertEqual(self.reads, ["keep"])

    def capped_scenario(self, module=None):
        engine = self.world([self.rec("p1"), self.rec("p2")])
        with unittest.mock.patch.object(sb, "load_module", lambda n: module if (n == "pod_silence" and module) else load_real(n)):
            self.tick(engine)
            self.tick(engine, ahead=21 * MIN)
            self.assertEqual(len(self.sent), 2)
            full = engine.cloud_watch["records"]
            engine.cloud_watch = {"records": [full[1]], "capped": True}   # p1 not read this pass
            self.tick(engine, ahead=22 * MIN)
            engine.cloud_watch = {"records": full, "capped": True}
            self.tick(engine, ahead=23 * MIN)
            self.tick(engine, ahead=50 * MIN)   # past the 20 minutes: without the told mark p1 WOULD be told again
        return [text for _, text in self.sent]

    def test_a_capped_pass_that_omits_a_told_pod_does_not_tell_it_again(self):
        texts = self.capped_scenario()
        self.assertEqual(len(texts), 2, "the told pod was told again: %r" % texts[2:])

    def test_mutant_pruning_on_a_capped_pass_tells_the_omitted_pod_again(self):
        self.assertEqual(len(self.capped_scenario()), 2)   # control first
        control_sent = list(self.sent)
        mutant, applied = load(prune_mutant=True)
        self.assertTrue(applied)
        self.sent = []
        texts = self.capped_scenario(mutant)
        self.assertEqual(len(control_sent), 2)
        self.assertEqual(len(texts), 3)   # a no-op mutant would send 2
        self.assertIn("stub of p1", texts[2])

    def prune_false_run(self, module):
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "s.json")
            sent = []
            module.scan([pod("a")], NOW - 30 * MIN, state, lambda to, t: None, lambda n: "x", json.loads)
            module.scan([pod("a")], NOW, state, lambda to, t: sent.append(t), lambda n: "x of a", json.loads)
            module.scan([pod("b")], NOW + 1, state, lambda to, t: None, lambda n: "x", json.loads, prune=False)
            module.scan([pod("a")], NOW + 2, state, lambda to, t: sent.append(t), lambda n: "x of a", json.loads)
            module.scan([pod("a")], NOW + 2 + 21 * MIN, state, lambda to, t: sent.append(t), lambda n: "x of a", json.loads)   # past the 20 minutes
        return sent

    def test_scan_prune_false_keeps_the_told_mark(self):
        module, _ = load()
        sent = self.prune_false_run(module)
        self.assertEqual(len(sent), 1, "the told pod was told again: %r" % sent[1:])

    def test_mutant_pruning_despite_prune_false_tells_again(self):
        control, applied0 = load()
        self.assertFalse(applied0)
        self.assertEqual(len(self.prune_false_run(control)), 1)   # control first
        mutant, applied = load(prune_mutant=True)
        self.assertTrue(applied)
        sent = self.prune_false_run(mutant)
        self.assertEqual(len(sent), 2)   # a no-op mutant would send 1
        self.assertIn("x of a", sent[1])


BODY = {"worker": "other-pod", "in_flight": None, "last_calls": [], "idle_seconds": 1300, "branch": "b", "unpushed": {"count": 0}, "dirty": False, "last_push": "p"}


class StubCluster:
    def __enter__(self):
        return "CLUSTER"

    def __exit__(self, *a):
        return False


class TestSummaryCluster(unittest.TestCase):
    def summary(self, launcher):
        cloud = load_real("cloud_worker").bind(sb.__dict__)
        fake = types.SimpleNamespace(run_launcher=launcher, _inspect_short=cloud._inspect_short, Cluster=StubCluster)
        fake.bind = lambda g: fake
        with unittest.mock.patch.object(sb, "load_module", lambda n: fake if n == "cloud_worker" else load_real(n)):
            return sb.pod_silence_summary("other-pod")

    def test_summary_passes_an_open_cluster(self):
        seen = []

        def good(runner, command, worker, options=(), timeout=0, cluster=None):
            seen.append(cluster)
            return 0, dict(BODY), ""
        self.assertIn("other-pod: no call in flight", self.summary(good))   # control
        self.assertEqual(seen, ["CLUSTER"])

    def test_a_launcher_given_no_cluster_fails_with_the_anchored_text(self):
        def needs(runner, command, worker, options=(), timeout=0, cluster=None):
            if cluster is None:
                return 1, None, "token unavailable"
            return 0, dict(BODY), ""
        self.assertIn("other-pod", self.summary(needs))
        def mutant(runner, command, worker, options=(), timeout=0, cluster=None):
            return needs(runner, command, worker, options, timeout, None)
        with self.assertRaises(RuntimeError) as ctx:
            self.summary(mutant)
        self.assertEqual(str(ctx.exception).count("the pod was not read (token unavailable)"), 1)


class TestDaemonSummary(unittest.TestCase):
    def test_daemon_reads_another_lanes_pod_in_process_while_the_command_refuses_a_lane(self):
        cloud = load_real("cloud_worker").bind(sb.__dict__)
        calls = []

        def fake_launcher(runner, command, worker, options=(), timeout=0, **kw):
            calls.append((command, worker, tuple(options)))
            return 0, dict(BODY), ""
        fake = types.SimpleNamespace(run_launcher=fake_launcher, _inspect_short=cloud._inspect_short, Cluster=StubCluster)
        fake.bind = lambda g: fake
        with unittest.mock.patch.object(sb, "load_module", lambda n: fake if n == "cloud_worker" else load_real(n)):
            line = sb.pod_silence_summary("other-pod")
        self.assertIn("other-pod: no call in flight", line)
        self.assertEqual(calls, [("inspect", "other-pod", ())])   # the daemon's read has no flag and no caller
        # the command still refuses a lane that is not the boss
        args = types.SimpleNamespace(name="other-pod", short=True)
        H = cloud.H if hasattr(cloud, "H") else None
        self.assertIsNotNone(H)
        audits = []
        with unittest.mock.patch.dict(H, {"gates_caller_kind": lambda r, n: ("agent", "lane-b"), "build_engine": lambda r, n, k: (object(), None, None)}), \
                unittest.mock.patch.object(cloud, "route", lambda n: {"reports_to": "lane-a"}), \
                unittest.mock.patch.object(cloud, "_audit", lambda e, *a: audits.append(a)):
            with self.assertRaises(H["LaunchError"]) as ctx:
                cloud.cmd_inspect(args, None, 0)
        self.assertIn("only other-pod's own boss may inspect it", str(ctx.exception))
        self.assertEqual(audits[0][0], "cloud.inspect_refused")


class TestUnreadableSummary(unittest.TestCase):
    def run_passes(self, module, summary, passes=3):
        sent, audits = [], []
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "s.json")
            module.scan([pod("silent")], NOW - 30 * MIN, state, lambda to, t: None, lambda n: "x", json.loads)
            for i in range(passes):
                module.scan([pod("silent")], NOW + i, state, lambda to, t: sent.append((to, t)), summary, json.loads,
                            audit=lambda w, d: audits.append((w, d)))
        return sent, audits

    def failing(self, name):
        raise RuntimeError("the pod was not read (exit 3)")

    def test_none_and_failing_summaries_send_the_reason_and_one_audit_line_once(self):
        module, _ = load()
        for summary in (lambda n: None, self.failing):
            sent, audits = self.run_passes(module, summary)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0], "lane-a")
            self.assertIn("state unreadable: ", sent[0][1])
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0][0], "silent")
        sent, _ = self.run_passes(module, self.failing)
        self.assertIn("state unreadable: the pod was not read (exit 3)", sent[0][1])

    def test_mutant_that_skips_on_none_fails_with_an_anchored_text(self):
        control, applied0 = load()
        self.assertFalse(applied0)
        sent, _ = self.run_passes(control, lambda n: None)
        self.assertIn("state unreadable: ", sent[0][1])   # control first
        text = (REPO / "bin" / "pod_silence.py").read_text()
        new = text.replace('        if not isinstance(line, str) or not line.strip():\n', '        if not isinstance(line, str) or not line.strip():\n            continue\n', 1)
        self.assertNotEqual(new, text)   # applied
        mutant = types.ModuleType("mutant")
        exec(compile(new, "pod_silence.py", "exec"), mutant.__dict__)
        sent, audits = self.run_passes(mutant, lambda n: None)
        self.assertEqual((sent, audits), ([], []))   # the skip sends nothing: the real module sends one


if __name__ == "__main__":
    unittest.main()
