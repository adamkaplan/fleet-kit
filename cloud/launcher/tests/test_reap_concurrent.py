"""#1661: the stops of one reaper pass run at most four at a time, and each stop has its own limit.

Offline: a fake clock, a fake cluster whose end hook blocks until the test releases it (the test moves the fake clock), the real Launcher.reap.
The four mutants run in child processes on scratch copies of the source outside the repository (the edit asserted applied).
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, LAUNCHER)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(LAUNCHER)), "cloud", "worker", "tests"))
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(LAUNCHER)), "bin"))
import cloud_worker as real  # noqa: E402
import cloud_end_alert  # noqa: E402
import record_json  # noqa: E402
import stub_support  # noqa: E402

T0 = dt.datetime(2026, 10, 30, 9, 0, 0, tzinfo=dt.timezone.utc)
HOOK_SECONDS = 100   # eight in a row: 800 seconds; two waves of four: 200
MODULE_ENV = "CW_TEST_REAP_CONCURRENT_MODULE"
SCRUB_SET = 1000
SCRUB_RUNS = 2000
PHRASES = ("four at a time", "its own limit", "tried again at the next pass")


def target_module():
    path = os.environ.get(MODULE_ENV)
    if not path:
        return real
    import importlib.util
    spec = importlib.util.spec_from_file_location("cloud_worker_scratch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def pod_of(worker, name):
    return {"metadata": {"name": name, "uid": "uid-" + name, "annotations": {"cw/worker": worker, "cw/started": real.iso(T0)}},
            "status": {"phase": "Running"}}


class Cluster:
    """The fake cluster: the end hook blocks on its own event until the test lets it go."""

    def __init__(self, pods, hold):
        self.pods, self.hold, self.lock = pods, hold, threading.Lock()
        self.calls, self.deletes, self.annotations, self.hooks = [], [], [], 0
        self.running, self.highest, self.above, self.finished, self.held = 0, 0, 0, 0, []

    def list_pods(self):
        return list(self.pods)

    def get_pod(self, name):
        with self.lock:
            self.calls.append("get_pod")
        return next((p for p in self.pods if p["metadata"]["name"] == name), None)

    def scrub(self, text, limit=300):
        return str(text)[:limit]

    def add_secrets(self, *a):
        pass

    def exec(self, name, *cmd, keep_stdout=False, timeout=120):
        with self.lock:
            self.calls.append("exec " + cmd[0])
        if cmd[0] != "cw-end-hook":
            return "{}"
        ev = threading.Event()
        with self.lock:
            self.hooks += 1
            self.running += 1
            self.highest = max(self.highest, self.running)
            self.above += self.running > 4
            self.held.append(ev)
        if self.hold:
            ev.wait(30)
        with self.lock:
            self.running -= 1
            self.finished += 1
        return json.dumps({"pushed": True, "wip_branch": name + "/wip"})

    def annotate(self, name, uid, **kv):
        with self.lock:
            self.annotations.append(name)

    def delete(self, name, uid=None):
        with self.lock:
            self.deletes.append(name)

    def release_all(self):
        with self.lock:
            held, self.held = self.held, []
        for ev in held:
            ev.set()


class NoRecord:
    root = None

    def record(self, *a, **k):
        return ""

    def add_hook(self, *a, **k):
        pass


def build(mod, tmp, n_ending, n_live=0, hold=True):
    clock = Clock()
    names = {}
    pods = []
    for i in range(n_ending + n_live):
        w = "coder%d" % i
        names[w] = real.pod_name(w)
        pods.append(pod_of(w, names[w]))
    cluster = Cluster(pods, hold)
    env = {"CW_CLOUD_AUDIT": os.path.join(tmp, "audit.jsonl"), "XDG_STATE_HOME": tmp}
    events = {"renewals": [], "reads": []}

    class L(mod.Launcher):
        def hard_deadline(self, pod):
            return T0 - dt.timedelta(minutes=1) if int(real.ann(pod, "worker")[5:]) < n_ending else T0 + dt.timedelta(hours=5)

        def activity(self, name):
            events["reads"].append(name)
            return (True, T0)

        def cost(self, pod):
            return 0.0

        def renew_git_tokens(self, pods, now):
            events["renewals"].append(cluster.finished)
            return []

    launcher = L(cluster, env, clock, lambda s: None)
    launcher.recorder = NoRecord()
    return launcher, cluster, clock, env, events


def audit_lines(env):
    try:
        with open(env["CW_CLOUD_AUDIT"]) as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


def wait_for(cond, what, seconds=10):
    end = time.time() + seconds
    while not cond():
        if time.time() > end:
            raise AssertionError("timed out waiting for " + what)
        time.sleep(0.002)


def run_pass(launcher, cluster, clock, total, cap, on_full=None, advance=HOOK_SECONDS):
    """One pass on its own thread; the test lets the hooks go in waves of `cap`, moving the fake clock by `advance` each wave."""
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("out", launcher.reap()), daemon=True)
    t.start()
    waves = 0
    while cluster.finished < total and t.is_alive():
        want = min(cap, total - cluster.finished)
        wait_for(lambda: len(cluster.held) >= want or not t.is_alive(), "the hooks to run")
        if not cluster.held:
            break
        if on_full:
            on_full()
        clock.now += dt.timedelta(seconds=advance)
        waves += 1
        before = cluster.finished
        n = len(cluster.held)
        cluster.release_all()
        wait_for(lambda: cluster.finished >= before + n, "the hooks to end")
    t.join(10)
    assert not t.is_alive(), "the pass did not end"
    return box["out"], waves


def comparable(env):
    return sorted(({k: v for k, v in l.items() if k != "ts"} for l in audit_lines(env)), key=lambda l: l["worker"])


class Concurrent(unittest.TestCase):
    def eight(self, parallel=None):
        mod = target_module()
        tmp = tempfile.mkdtemp()
        L, cluster, clock, env, events = build(mod, tmp, 8)
        if parallel:
            mod.REAP_PARALLEL = parallel
        try:
            out, waves = run_pass(L, cluster, clock, 8, parallel or mod.REAP_PARALLEL)
        finally:
            if parallel:
                mod.REAP_PARALLEL = 4
        return mod, L, cluster, clock, env, out, waves

    def test_S2_U3_eight_stops_four_at_a_time(self):
        mod, L, cluster, clock, env, out, waves = self.eight()
        print("S2: stops done %d of 8, highest running at once %d, waves %d (sequential would be 8)" % (len(audit_lines(env)), cluster.highest, waves))
        print("U3: highest running at once %d, moments above 4: %d" % (cluster.highest, cluster.above))
        self.assertEqual((len(audit_lines(env)), cluster.highest, waves), (8, 4, 2))
        self.assertEqual(cluster.above, 0)

    def test_S3_each_stop_keeps_its_line_result_and_place(self):
        _, L, cluster, clock, env, out, waves = self.eight()
        mod = real
        tmp2 = tempfile.mkdtemp()
        L2, c2, clock2, env2, _ = build(mod, tmp2, 8)
        mod.REAP_PARALLEL = 1
        try:
            out2, _ = run_pass(L2, c2, clock2, 8, 1)
        finally:
            mod.REAP_PARALLEL = 4
        lines, lines2 = comparable(env), comparable(env2)
        same_lines = sum(a == b for a, b in zip(lines, lines2))
        same_results = sum(L.reap_results[w] == L2.reap_results[w] for w in L2.reap_results)
        order = [o[0] for o in out] == ["coder%d" % i for i in range(8)] and out == out2
        print("S3: audit lines equal %d of 8, results equal %d of 8, pod order kept %s" % (same_lines, same_results, "yes" if order else "no"))
        self.assertEqual((same_lines, same_results, order), (8, 8, True))
        self.assertTrue(all(L.reap_results[w]["wip_branch"] == real.pod_name(w) + "/wip" for w in L.reap_results))

    def test_S6_the_pass_ends_under_the_pass_limit(self):
        mod, L, cluster, clock, env, out, waves = self.eight()
        took = int((clock() - T0).total_seconds())
        L.note_slow_pass(took)
        slow = [l for l in audit_lines(env) if l["action"] == mod.SLOW_PASS_ACTION]
        stops = [l for l in audit_lines(env) if l["action"] == "stop"]
        print("S6: pass seconds %d (under 600), audit lines %d, slow-pass lines %d" % (took, len(stops), len(slow)))
        self.assertLess(took, 600)
        self.assertGreater(8 * HOOK_SECONDS, 600)
        self.assertEqual((len(stops), len(slow)), (8, 0))

    def test_S5_the_renewal_and_the_read_do_not_wait_for_the_stops(self):
        mod = target_module()
        L, cluster, clock, env, events = build(mod, tempfile.mkdtemp(), 4, n_live=1)
        seen = {}

        def full():
            seen.update(renewals=len(events["renewals"]), before=events["renewals"][0], reads=len(events["reads"]), running=cluster.running)
        run_pass(L, cluster, clock, 4, 4, on_full=full)
        print("S5: renewals before the first stop ended %d of 1, activity reads of the live pod %d of 1, stops still running %d of 4" % (
            seen["renewals"] if seen["before"] == 0 else 0, seen["reads"], seen["running"]))
        self.assertEqual((seen["renewals"], seen["before"], seen["reads"], seen["running"]), (1, 0, 1, 4))

    def test_S4_a_stop_past_its_limit_is_abandoned_and_tried_again(self):
        mod = target_module()
        L, cluster, clock, env, events = build(mod, tempfile.mkdtemp(), 1)
        box = {}

        def one_pass():
            t = threading.Thread(target=lambda: box.setdefault("out", L.reap()), daemon=True)
            t.start()
            wait_for(lambda: cluster.hooks >= box.get("n", 0) + 1, "the hook to start")
            clock.now += dt.timedelta(seconds=mod.REAP_STOP_SECONDS)
            t.join(5)
            alive = t.is_alive()
            box["n"] = cluster.hooks
            box.pop("out", None)
            return alive
        try:
            alive = one_pass()
            lines = [l for l in audit_lines(env) if l["action"] == "stop"]
            self.assertFalse(alive, "the pass was held by the stop")
            self.assertEqual(len(lines), 1)
            self.assertFalse(lines[0]["ok"])
            self.assertEqual(lines[0]["reason"], "stop hit its limit: %d seconds (limit %d)" % (mod.REAP_STOP_SECONDS, mod.REAP_STOP_SECONDS))
            self.assertEqual(lines[0]["worker"], "coder0")
            one_pass()
            print("S4: limit audit lines %d, delete calls %d, annotation writes %d, stop attempts after 2 passes %d" % (
                len(lines), len(cluster.deletes), len(cluster.annotations), cluster.hooks))
        finally:
            cluster.release_all()
            time.sleep(0.2)
            cluster.release_all()
        time.sleep(0.2)
        self.assertEqual((len(cluster.deletes), len(cluster.annotations), cluster.hooks), (0, 0, 2))

    def test_U2_one_pod_and_no_pod_passes(self):
        mod = target_module()
        L, cluster, clock, env, _ = build(mod, tempfile.mkdtemp(), 1)
        out, _ = run_pass(L, cluster, clock, 1, 1)
        got1 = (out, comparable(env), cluster.calls, L.reap_results)
        L0, c0, clock0, env0, _ = build(mod, tempfile.mkdtemp(), 0)
        out0 = L0.reap()
        got0 = (out0, comparable(env0), c0.calls, L0.reap_results)
        want1 = ([("coder0", "hard limit", 0)],
                 [{"action": "stop", "cause": "hard", "actor": got1[1][0]["actor"], "cost_estimate_usd": 0.0, "ok": True, "reason": "hard limit",
                   "runtime_id": "pod/" + real.pod_name("coder0"), "worker": "coder0", "refresh": "none"}],
                 ["get_pod", "exec cw-end-hook"],
                 {"coder0": {"pushed": True, "wip_branch": real.pod_name("coder0") + "/wip", "skipped": [], "record": ""}})
        want0 = ([], [], [], {})
        same = (got1 == want1) + (got0 == want0)
        print("U2: one-pod and no-pod passes equal the base %d of 2" % same)
        self.assertEqual((got1, got0), (want1, want0))

    def test_S10_the_concurrent_stops_carry_the_stop_cause(self):
        mod = target_module()
        L, cluster, clock, env, events = build(mod, tempfile.mkdtemp(), 2, hold=False)
        for pod, rid in zip(cluster.pods, ("run-a0", "run-b1")):
            pod["metadata"]["annotations"]["cw/run-id"] = rid
        for name in ("delete_pod", "delete_secret", "delete_run_secret", "get_secret"):   # a pod with a run id also removes its Secrets
            setattr(cluster, name, lambda *a, **k: None)
        L.hard_deadline = lambda pod: T0 - dt.timedelta(minutes=1) if real.ann(pod, "worker") == "coder1" else T0 + dt.timedelta(hours=5)
        L.activity = lambda name: (False, T0 - dt.timedelta(seconds=700))   # idle past the default 600 seconds
        seen, lock, cls = {}, threading.Lock(), type(L)
        old = cls.stop

        def recording(self, ns, reason, *a, **k):
            with lock:
                seen[ns.worker] = ns
            return old(self, ns, reason, *a, **k)
        cls.stop = recording
        try:
            L.reap()
        finally:
            cls.stop = old
        causes = [getattr(seen[w], "cause", None) for w in ("coder0", "coder1")]
        ok = (len(seen), causes) == (2, ["idle", "hard"])
        lines = {l["worker"]: l.get("cause") for l in audit_lines(env) if l["action"] == "stop"}
        audited = sum(lines.get(w) == c for w, c in (("coder0", "idle"), ("coder1", "hard")))
        scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, scratch, True)
        audit, cursor, sent = os.path.join(scratch, "audit.jsonl"), os.path.join(scratch, "cursor.json"), []
        open(audit, "w").close()
        for w in ("coder0", "coder1"):
            with open(os.path.join(scratch, w + ".json"), "w") as f:
                json.dump({"worker": w, "reports_to": "coder-lead"}, f)   # the boss record, as test_cloud_end_alert's World writes it
        scan = lambda: cloud_end_alert.scan_audit(audit, cursor, scratch, {}, lambda to, text: sent.append((to, text)), record_json.parse_bounded)
        scan()   # the first pass over an empty file stores the cursor at its end
        with open(env["CW_CLOUD_AUDIT"]) as src, open(audit, "a") as dst:
            dst.write(src.read())
        scan()
        alerted = sum(len([t for to, t in sent if w in t and word in t]) == 2 for w, word in (("coder0", "idle limit"), ("coder1", "hard limit")))
        runs = [getattr(seen[w], "run_id", None) for w in ("coder0", "coder1")]
        pods = {real.ann(p, "worker"): real.ann(p, "run-id") for p in cluster.pods}
        same_run = sum(runs[i] == pods.get(w) and runs[i] is not None for i, w in enumerate(("coder0", "coder1")))
        print("S10: stops with the right cause %d of 2, audit lines naming it %d of 2, end alerts naming it %d of 2" % (2 if ok else 0, audited, alerted))
        print("S10: run id equals the pod annotation %d of 2" % same_run)
        self.assertEqual((len(seen), causes), (2, ["idle", "hard"]))
        self.assertEqual((audited, alerted, same_run), (2, 2, 2))

    def test_S10_a_failed_pod_stops_through_the_pool_with_cause_pod_failed(self):
        mod = target_module()
        L, cluster, clock, env, events = build(mod, tempfile.mkdtemp(), 1, hold=False)
        cluster.pods[0]["status"] = {"phase": "Failed"}
        for name in ("delete_secret", "delete_run_secret", "get_secret"):
            setattr(cluster, name, lambda *a, **k: None)
        out = L.reap()
        causes = [l.get("cause") for l in audit_lines(env) if l["action"] == "stop"]
        print("S10b: a Failed pod through the pool ends with cause %s, reaped %d of 1, hook runs %d" % (causes, len(out), cluster.hooks))
        self.assertEqual((causes, out, cluster.hooks), (["pod_failed"], [("coder0", "hard limit", 0)], 0))


class ScrubSnapshot(unittest.TestCase):
    def test_S9_scrub_survives_a_set_that_grows_under_it(self):
        mod = target_module()
        planted = "PLANTED-known-secret-value-0001"
        known = {planted} | {"early-known-%07d-xx" % i for i in range(SCRUB_SET)}
        text = "log line with " + planted + " inside"
        errors, removed, done = [], 0, threading.Event()

        def adder():
            for i in range(5000):
                known.add("late-added-%07d-yy" % i)
                time.sleep(0)   # let the scrubbing thread run between two adds
            done.set()
        t = threading.Thread(target=adder, daemon=True)
        t.start()
        for _ in range(SCRUB_RUNS):
            try:
                removed += planted not in mod.scrub(text, known)
            except RuntimeError as e:
                errors.append(str(e))
        t.join()
        print("S9: scrubs %d of %d, RuntimeError %d, planted value removed %d of %d"
              % (SCRUB_RUNS, SCRUB_RUNS, len(errors), removed, SCRUB_RUNS))
        self.assertEqual(errors, [])
        self.assertEqual(removed, SCRUB_RUNS)


def mutant(directory, name, edits):
    with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
        src = f.read()
    for old, new in edits:
        assert src.count(old) == 1, old   # the mutation is applied, or the test stops here
        src = src.replace(old, new)
    d = os.path.join(directory, name)
    os.makedirs(d)
    path = os.path.join(d, "cloud_worker_scratch.py")
    with open(path, "w") as f:
        f.write(src)
    return path


class Mutants(unittest.TestCase):
    def child(self, name, edits, test):
        with tempfile.TemporaryDirectory() as tmp:
            path = mutant(tmp, name, edits)   # outside the repository
            p = stub_support.start(self, [sys.executable, "-m", "unittest", "test_reap_concurrent.Concurrent." + test], cwd=HERE,
                                   env=dict(os.environ, **{MODULE_ENV: path}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            out = p.communicate(timeout=90)[0]
        self.assertEqual(p.returncode, 1, out)
        return 1

    def test_the_four_mutants_are_caught(self):
        n = [
            self.child("m1", [("REAP_PARALLEL = 4 ", "REAP_PARALLEL = 1 ")], "test_S2_U3_eight_stops_four_at_a_time"),
            self.child("m2", [("REAP_PARALLEL = 4 ", "REAP_PARALLEL = 8 ")], "test_S2_U3_eight_stops_four_at_a_time"),
            self.child("m3", [("elif took >= REAP_STOP_SECONDS:", "elif False:")], "test_S4_a_stop_past_its_limit_is_abandoned_and_tried_again"),
            self.child("m4", [("                    dead.set()\n", "                    dead.set()\n                    self.kube.delete(pod_name(jobs[i][1]))\n")],
                       "test_S4_a_stop_past_its_limit_is_abandoned_and_tried_again"),
        ]
        print("S7: mutants caught %d of 4, mutations applied 4 of 4" % sum(n))
        self.assertEqual(sum(n), 4)

    def test_S9_the_mutant_that_iterates_the_live_set_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = mutant(tmp, "m9", [("for v in tuple(secrets_known) if v", "for v in secrets_known if v")])
            p = stub_support.start(self, [sys.executable, "-m", "unittest", "test_reap_concurrent.ScrubSnapshot"], cwd=HERE,
                                   env=dict(os.environ, **{MODULE_ENV: path}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            out = p.communicate(timeout=90)[0]
        seen = "Set changed size during iteration" in out
        print("S9 mutant: failed with Set changed size during iteration %d of 1, mutation applied True" % (p.returncode == 1 and seen))
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("Set changed size during iteration", out)

    def test_S10_the_mutant_without_the_cause_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = 'run_id=nonce,\n                                                      cause="hard" if reason == "hard limit" else "idle"), reason)'
            path = mutant(tmp, "m10", [(old, "run_id=nonce), reason)")])
            p = stub_support.start(self, [sys.executable, "-m", "unittest", "test_reap_concurrent.Concurrent.test_S10_the_concurrent_stops_carry_the_stop_cause"],
                                   cwd=HERE, env=dict(os.environ, **{MODULE_ENV: path}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            out = p.communicate(timeout=90)[0]
        print("S10 mutant: failed %d of 1, mutation applied True" % (p.returncode == 1))
        self.assertEqual(p.returncode, 1, out)


class Docs(unittest.TestCase):
    def test_S8_the_readme_says_the_rule(self):
        with open(os.path.join(LAUNCHER, "README.md")) as f:
            text = f.read()
        found = sum(p in text for p in PHRASES)
        print("S8: phrases found %d of 3" % found)
        self.assertEqual(found, 3)


if __name__ == "__main__":
    unittest.main()
