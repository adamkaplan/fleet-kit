"""The create's wait says why a pod is pending (#1939): the three cases of the cluster, on a fake cluster and a fake clock.

No cloud and no real time: kubectl is the fake beside this file for the writes, the reads of the pod, its events and the pod list are
scripted by the fake time, and the clock only moves when the launcher sleeps. The old fixed wait is run as a mutant (S6)."""
import ast
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402
from test_cloud_worker import Clock, T0  # noqa: E402

LIMIT, OUTER, POD_COUNT = 300, 900, 13
NOT_TRIGGER = {"reason": "NotTriggerScaleUp", "message": "pod didn't trigger scale-up: 1 max node group size reached"}
TRIGGERED = {"reason": "TriggeredScaleUp", "message": "pod triggered scale-up: [{pool 1->2 (max: 3)}]"}
FAILED_SCHED = {"reason": "FailedScheduling", "message": "0/13 nodes are available: 12 Insufficient cpu"}
OLD_TEXT = "timed out waiting for the worker"
WRITES = ("apply", "create", "patch", "label", "annotate", "replace", "delete", "exec")
CASE_TEXT = "the create did not tell the cluster's case"


def load_copy(path):
    spec = importlib.util.spec_from_file_location("cw_copy_" + str(abs(hash(path))), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class World:
    """One create against a fake cluster. `module` is the launcher under test (the real one or a scratch copy)."""

    def __init__(self, module, ready_at=None, unschedulable=True, events=(), refused=False):
        self.cw, self.ready_at, self.unschedulable, self.events, self.refused = module, ready_at, unschedulable, list(events), refused
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.state = os.path.join(d, "kube.json")
        with open(self.state, "w") as f:
            json.dump({"pods": {}, "never_ready": True}, f)
        self.env = {"CW_KUBECTL": os.path.join(HERE, "fake_kubectl"), "FAKE_KUBE_STATE": self.state,
                    "CW_CLOUD_AUDIT": os.path.join(d, "audit.jsonl"), "CW_CLOUD_IMAGE": "registry.example.internal/worker:1",
                    "CW_TOKEN_ENDPOINT": "https://tokens.example.internal/mint", "CW_CLOUD_DOMAIN": "example.internal",
                    "CW_KUBE_CONTEXT": "acme-cw", "CW_ALLOWED_REPOS": "a/b,acme-org/widget-service",
                    "FAKE_KUBE_CALLS": os.path.join(d, "calls.jsonl")}
        self.lock_root = os.path.join(d, "locks")
        self.clock = Clock()
        self.first = None   # the fake time of the first read of the pod
        self.last = None
        self.reads = []

    def fake_now(self):
        return (self.clock() - T0).total_seconds()

    def get_pod(self, kube, name):
        with open(self.state) as f:
            if name not in json.load(f)["pods"]:
                return None
        self.first = self.fake_now() if self.first is None else self.first
        self.last = self.fake_now()
        ready = self.ready_at is not None and self.fake_now() - self.first >= self.ready_at
        conds = [{"type": "Ready", "status": "True"}] if ready else (
            [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}] if self.unschedulable else [])
        return {"metadata": {"name": name, "uid": "u1"}, "status": {"phase": "Pending" if not ready else "Running", "conditions": conds}}

    def pod_events(self, kube, name):
        self.reads.append("events")
        if self.refused:
            raise self.cw.CloudError("kubectl get: Forbidden\nsecond line of the refusal that must not be printed")
        return [dict(e, involvedObject={"kind": "Pod", "name": name}) for e in self.events]

    def list_pods_all(self, kube):
        self.reads.append("pods")
        return [{"metadata": {"name": "p%d" % i}} for i in range(POD_COUNT)]

    def create(self):
        out, err = io.StringIO(), io.StringIO()
        old_root = self.cw.REAPER_LOCK_ROOT
        self.cw.REAPER_LOCK_ROOT = self.lock_root
        K = self.cw.Kube
        try:
            with mock.patch.object(K, "get_pod", lambda k, n: self.get_pod(k, n)), \
                    mock.patch.object(K, "pod_events", lambda k, n: self.pod_events(k, n)), \
                    mock.patch.object(K, "list_pods_all", lambda k: self.list_pods_all(k)), \
                    redirect_stdout(out), redirect_stderr(err):
                code = self.cw.main(["create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main"],
                                    self.env, self.clock, lambda s: self.clock.advance(seconds=s))
        finally:
            self.cw.REAPER_LOCK_ROOT = old_root
        self.code, self.out, self.err = code, out.getvalue(), err.getvalue()
        return self

    def span(self):
        return self.last - self.first

    def audit(self):
        with open(self.env["CW_CLOUD_AUDIT"]) as f:
            return [json.loads(x) for x in f]

    def calls(self):
        with open(self.env["CW_CLOUD_AUDIT"].replace("audit.jsonl", "calls.jsonl")) as f:
            return [c[c.index("-n") + 2:] if "-n" in c else c for c in map(json.loads, f)]

    def pods_left(self):
        with open(self.state) as f:
            return json.load(f)["pods"]

    def close(self):
        self.tmp.cleanup()


def check_s2(module):
    w = World(module, events=[FAILED_SCHED, NOT_TRIGGER]).create()
    try:
        lines = w.err.strip().splitlines()
        ok = (w.span() == 0 and w.code != 0 and len(lines) == 1 and "max node group size reached" in lines[0]
              and str(POD_COUNT) in lines[0] and OLD_TEXT not in w.err and not w.pods_left())
        assert ok, "%s: span %s code %s err %r" % (CASE_TEXT, w.span(), w.code, w.err)
    finally:
        w.close()


def check_s3(module):
    w = World(module, ready_at=420, events=[FAILED_SCHED, TRIGGERED]).create()
    try:
        assert w.code == 0 and w.span() == 420, "%s: ready at 420 gave code %s at %s: %r" % (CASE_TEXT, w.code, w.span(), w.err)
    finally:
        w.close()
    w = World(module, ready_at=12, events=[FAILED_SCHED, TRIGGERED]).create()
    try:
        assert w.code == 0 and w.span() == 12, "%s: control gave code %s at %s" % (CASE_TEXT, w.code, w.span())
    finally:
        w.close()
    w = World(module, events=[FAILED_SCHED, TRIGGERED]).create()
    try:
        assert w.code == module.EXIT_TIMEOUT and w.span() == OUTER and "waiting for a node" in w.err and len(w.err.strip().splitlines()) == 1, \
            "%s: never scheduled gave code %s at %s: %r" % (CASE_TEXT, w.code, w.span(), w.err)
    finally:
        w.close()


def check_s4(module):
    w = World(module, events=[FAILED_SCHED]).create()
    try:
        assert w.code == module.EXIT_TIMEOUT and w.span() == LIMIT and OLD_TEXT in w.err and "events could not be read" not in w.err, \
            "%s: no autoscaler event gave code %s at %s: %r" % (CASE_TEXT, w.code, w.span(), w.err)
    finally:
        w.close()
    w = World(module, ready_at=12, unschedulable=False).create()
    try:
        assert w.code == 0 and w.span() == 12 and not w.reads, "%s: a starting pod gave code %s at %s, reads %s" % (CASE_TEXT, w.code, w.span(), w.reads)
    finally:
        w.close()


def copy_with_old_wait(workdir):
    """A scratch copy of the launcher in which the wait is the old fixed wait; returns (module path, original text, changed text, removed text)."""
    for n in os.listdir(os.path.dirname(HERE)):
        if n.endswith(".py"):
            shutil.copy(os.path.join(os.path.dirname(HERE), n), workdir)
    path = os.path.join(workdir, "cloud_worker.py")
    original = open(path).read()
    removed = "scaling, unread = self._why_pending(name, pod, scaling)"
    changed = mutate(original, removed, "scaling, unread = False, False")
    with open(path, "w") as f:
        f.write(changed)
    return path, original, changed, removed


def mutate(original, removed, added):
    assert original.count(removed) == 1, "the text to mutate must occur once"
    return original.replace(removed, added)


def applied(original, changed, removed):
    """The mutation took: computed from the two texts."""
    return changed != original and removed in original and removed not in changed


class S2Refusal(unittest.TestCase):
    def test_s2_the_autoscaler_refuses_and_the_create_ends_at_once_with_one_line(self):
        w = World(cw, events=[FAILED_SCHED, NOT_TRIGGER]).create()
        self.addCleanup(w.close)
        lines = w.err.strip().splitlines()
        print("S2 fake time %s of %s, code %s, line: %s" % (w.span(), LIMIT, w.code, lines))
        self.assertEqual(w.span(), 0)
        self.assertNotEqual(w.code, 0)
        self.assertEqual(len(lines), 1)
        self.assertIn("max node group size reached", lines[0])
        self.assertIn(str(POD_COUNT), lines[0])
        self.assertNotIn(OLD_TEXT, w.err)
        self.assertEqual(w.pods_left(), {})
        # the cleanup and the audit line are those of today's failed create (the timeout of S4 is the reference)
        ref = World(cw, events=[FAILED_SCHED]).create()
        self.addCleanup(ref.close)
        keys = lambda x: [(a["action"], a["ok"]) for a in x.audit()]
        self.assertEqual(keys(w), keys(ref))
        self.assertEqual([c for c in w.calls() if c[0] == "delete"], [c for c in ref.calls() if c[0] == "delete"])


class S3Scaling(unittest.TestCase):
    def test_s3_a_scale_up_under_way_keeps_the_create_waiting(self):
        w = World(cw, ready_at=420, events=[FAILED_SCHED, TRIGGERED]).create()
        self.addCleanup(w.close)
        print("S3 ready at fake %s, code %s" % (w.span(), w.code))
        self.assertEqual((w.code, w.span()), (0, 420))
        c = World(cw, ready_at=12, events=[FAILED_SCHED, TRIGGERED]).create()
        self.addCleanup(c.close)
        print("S3 control ready at fake %s" % c.span())
        self.assertEqual((c.code, c.span()), (0, 12))

    def test_s3_never_scheduled_ends_at_the_outer_bound(self):
        w = World(cw, events=[FAILED_SCHED, TRIGGERED]).create()
        self.addCleanup(w.close)
        print("S3 never scheduled ends at fake %s, code %s, line %s" % (w.span(), w.code, w.err.strip()))
        self.assertEqual((w.code, w.span()), (cw.EXIT_TIMEOUT, OUTER))
        self.assertEqual(len(w.err.strip().splitlines()), 1)
        self.assertIn("waiting for a node", w.err)


class S4Today(unittest.TestCase):
    def test_s4_unschedulable_without_an_autoscaler_event_is_today(self):
        w = World(cw, events=[FAILED_SCHED]).create()
        self.addCleanup(w.close)
        print("S4 no autoscaler event ends at fake %s: %s" % (w.span(), w.err.strip()))
        self.assertEqual((w.code, w.span()), (cw.EXIT_TIMEOUT, LIMIT))
        self.assertIn(OLD_TEXT, w.err)
        self.assertNotIn("events could not be read", w.err)

    def test_s4_a_starting_pod_returns_without_reading_events(self):
        w = World(cw, ready_at=12, unschedulable=False).create()
        self.addCleanup(w.close)
        print("S4 starting pod returns at fake %s" % w.span())
        self.assertEqual((w.code, w.span(), w.reads), (0, 12, []))


class S5Refused(unittest.TestCase):
    def test_s5_events_refused_is_today_with_one_more_line(self):
        w = World(cw, events=[NOT_TRIGGER], refused=True).create()
        self.addCleanup(w.close)
        print("S5 refused ends at fake %s: %r" % (w.span(), w.err))
        self.assertEqual((w.code, w.span()), (cw.EXIT_TIMEOUT, LIMIT))
        self.assertIn(OLD_TEXT, w.err)
        self.assertEqual(w.err.count("events could not be read"), 1)
        self.assertEqual(len(w.err.strip().splitlines()), 1)
        self.assertNotIn("second line of the refusal", w.err)


class S6Mutant(unittest.TestCase):
    def test_s6_the_old_fixed_wait_fails_s2_and_s3_and_the_control_passes(self):
        passed = []
        for check in (check_s2, check_s3, check_s4):
            check(cw)
            passed.append(check.__name__)
        print("S6 control passes %d of 3" % len(passed))
        self.assertEqual(passed, ["check_s2", "check_s3", "check_s4"])   # every check ran and passed
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work)
        path, original, changed, removed = copy_with_old_wait(work)
        self.assertTrue(applied(original, changed, removed))
        mutant = load_copy(path)
        failed = {}
        for check in (check_s2, check_s3, check_s4):
            try:
                check(mutant)
            except AssertionError as e:
                failed[check.__name__] = str(e)
        print("S6 the copy fails %s" % sorted(failed))
        self.assertEqual(sorted(failed), ["check_s2", "check_s3"])
        for msg in failed.values():
            self.assertIn(CASE_TEXT, msg)

    def test_s6_a_no_op_mutation_fails_the_computation(self):
        original = "alpha beta\n"
        changed = mutate(original, "beta", "beta")
        self.assertFalse(applied(original, changed, "beta"))
        print("S6 the no-op mutation fails the computation 1 of 1")
        self.assertTrue(applied(original, mutate(original, "beta", "gamma"), "beta"))


WALL_CALLS = {"sleep", "time", "monotonic", "perf_counter", "now", "utcnow", "today", "time_ns"}


def scan(source):
    """Findings of a scan of a test file: a call of the wall clock or of a sleep, a thread that is not a daemon."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in WALL_CALLS:
                found.append("a test decides by the wall clock (line %d)" % node.lineno)
            if name == "Thread" and not any(k.arg == "daemon" and getattr(k.value, "value", None) is True for k in node.keywords):
                found.append("a thread that is not a daemon (line %d)" % node.lineno)
    return found


class S8Scan(unittest.TestCase):
    ANCHOR = "        self.assertEqual(w.pods_left(), {})\n"

    def test_s8_this_file_has_no_wall_clock_and_no_plain_thread(self):
        with open(os.path.abspath(__file__)) as fh:
            source = fh.read()
        found = scan(source)
        print("S8 findings in this file: %d" % len(found))
        self.assertEqual(found, [])
        self.assertEqual(source.count(self.ANCHOR), 1)
        changed = mutate(source, self.ANCHOR, self.ANCHOR + "        self.assertLess(time.monotonic() - 0, 5)\n")
        added = "        self.assertLess(time.monotonic() - 0, 5)\n"
        self.assertTrue(changed != source and added in changed and added not in source)
        mutant = scan(changed)
        print("S8 the copy fails: %s" % mutant)
        self.assertTrue(any("a test decides by the wall clock" in m for m in mutant))

    def test_s8_a_thread_without_daemon_is_found(self):
        self.assertEqual(len(scan("import threading\nthreading.Thread(target=f)\n")), 1)
        self.assertEqual(scan("import threading\nthreading.Thread(target=f, daemon=True)\n"), [])


class S9Reads(unittest.TestCase):
    def test_s9_reads_only_and_nothing_synthetic_in_the_line(self):
        token = "ghp_" + "Ab1" * 12
        node = "aks-nodepool1-12345678-vmss000000"
        message = "pod didn't trigger scale-up: 1 max node group size reached %s %s\nsecond line\nthird line %s" % (token, node, token)
        w = World(cw, events=[FAILED_SCHED, {"reason": "NotTriggerScaleUp", "message": message}]).create()
        self.addCleanup(w.close)
        lines = w.err.strip().splitlines()
        print("S9 reads %s, line: %s" % (sorted(set(w.reads)), lines))
        self.assertEqual(len(lines), 1)
        for value in (token, node, "third line"):
            self.assertNotIn(value, w.err + w.out)
        self.assertLessEqual(len(cw._one_line(message * 10)), 120)
        verbs = [c[0] for c in w.calls()]
        after_apply = verbs[len(verbs) - 1 - verbs[::-1].index("create"):][1:]   # everything after the pod was made
        print("S9 kubectl verbs after the pod was made: %s" % after_apply)
        self.assertTrue(all(v in ("get", "delete") for v in after_apply))
        self.assertTrue({"events", "pods"} <= set(w.reads))   # both are read; a further read is allowed


if __name__ == "__main__":
    unittest.main()
