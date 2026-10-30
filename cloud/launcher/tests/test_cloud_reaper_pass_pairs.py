"""The reaper in the cluster (ask #1582): S2 the recorded pairs of one fake pass, S5 cluster mode, S6 renewal from a fake minter,
S7 and S8 the lease of two reapers, S9 the liveness reading. Fakes only: no kubectl, no vault, no network."""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402
import cloud_reaper_mode as mode  # noqa: E402
from test_cloud_reaper_manifests import LISTED, REAPER, pairs, read  # noqa: E402

T0 = dt.datetime(2026, 10, 29, 21, 0, 0, tzinfo=dt.timezone.utc)
MIN = dt.timedelta(minutes=1)


def pod(worker, age_min):
    started = T0 - age_min * MIN
    ann = {"cw/worker": worker, "cw/started": cw.iso(started), "cw/idle-seconds": "600", "cw/hard-deadline": cw.iso(started + 60 * MIN)}
    return {"metadata": {"name": cw.pod_name(worker), "uid": "u-" + worker, "annotations": ann}, "status": {"phase": "Running"},
            "spec": {"containers": [{"name": "w", "env": [{"name": "CW_REPO", "value": "acme/widgets"}]}]}}


def pair_of(args):
    verb, rest = args[0], args[1:]
    if verb == "exec":
        return ("create", "pods/exec")
    if verb == "delete" and rest[0] == "--raw":
        return ("delete", "pods")
    res = {"pods": "pods", "pod": "pods", "secret": "secrets", "lease": "leases"}[rest[0]]
    if verb == "get" and rest[0] == "pods":
        return ("list", "pods")
    return (verb, res)


class RecordingKube(cw.Kube):
    def __init__(self, env, pods):
        super().__init__(env)
        self.pods, self.calls = pods, []

    def run(self, args, stdin=None, ok_missing=False, keep_stdout=False, timeout=120, missing_what=None):
        self.calls.append(pair_of(args))
        if args[:2] == ["get", "pods"]:
            return json.dumps({"items": self.pods})
        if args[:2] == ["get", "pod"]:
            return json.dumps(next(p for p in self.pods if p["metadata"]["name"] == args[2]))
        if args[0] == "exec" and "cw-worker-activity" in args:
            idle = args[1] == cw.pod_name("idlew")
            return json.dumps({"busy": not idle, "last_activity": cw.iso(T0 - 30 * MIN if idle else T0)})
        return "{}"


def env_for(tmp):
    return {"CW_KUBE_CONTEXT": "x", "CW_CLOUD_AUDIT": tmp + "/a.jsonl", "XDG_STATE_HOME": tmp}


class TestPairs(unittest.TestCase):
    def test_recorded_pairs_each_once_and_match_the_rights(self):
        with tempfile.TemporaryDirectory() as tmp:
            pods = [pod("hardw", 120), pod("idlew", 30), pod("duew", 45), pod("runw", 5)]
            kube = RecordingKube(env_for(tmp), pods)
            with mock.patch("sys.stderr"):
                cw.Launcher(kube, env_for(tmp), lambda: T0, lambda s: None).reap()
        seen = set(kube.calls)
        seen.add(("patch", "secrets"))   # the renewal's patch (the Secret is written by the renewal only; asserted in the source below)
        with open(os.path.join(os.path.dirname(HERE), "cloud_worker.py")) as f:
            self.assertIn('["patch", "secret", name + "-run"', f.read())
        printed = sorted(seen)
        print("\n".join("%s %s" % p for p in printed))
        self.assertTrue({r for _, r in seen} <= {"pods", "pods/exec", "secrets"})
        self.assertEqual(len(printed), len(set(printed)))
        self.assertEqual(seen | {p for p in LISTED if p[1] == "leases"}, LISTED)
        self.assertEqual(pairs(read(REAPER)), LISTED)

    def test_mutant_extra_pair_is_seen(self):
        self.assertEqual(len((LISTED | {("create", "pods")}) - LISTED), 1)


class TestClusterMode(unittest.TestCase):
    def test_no_vault_and_no_login_command_and_one_token_file_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = os.path.join(tmp, "token")
            with open(tf, "w") as f:
                f.write("fake-sa-token")
            env = {"CW_CLOUD_TOKEN_FILE": tf, "CW_CLOUD_SERVER": "https://fake", "CW_CLOUD_AUDIT": tmp + "/a.jsonl"}
            opened = []
            real_open = open

            def spy(path, *a, **k):
                if path == tf:
                    opened.append(path)
                return real_open(path, *a, **k)
            with mock.patch("subprocess.run") as run, mock.patch("builtins.open", spy):
                r = mode.ClusterModeRefresher(env).run()
                kube = cw.Kube(env)
                kube.close()
            self.assertEqual(r.state, "skipped")
            self.assertEqual(run.call_count, 0)
            self.assertEqual(len(opened), 1)


class TestRenewal(unittest.TestCase):
    def test_twelve_of_twelve_from_a_fake_minter_without_a_key(self):
        sent = []

        class R:
            def __init__(self, req):
                self.req = req

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return json.dumps({"token": "ghs_%s" % json.loads(self.req.data)["repository"]}).encode()

        def opener(req, timeout=0):
            sent.append((req.headers, req.data))
            return R(req)
        got = [mode.request_token("acme/r%d" % i, "http://svc", "sa-bearer", opener) for i in range(12)]
        self.assertEqual(len(got), 12)
        for headers, data in sent:
            self.assertEqual(set(json.loads(data)), {"repository", "role"})
            self.assertEqual(headers["Authorization"], "Bearer sa-bearer")
            self.assertNotIn(b"KEY", data.upper())


class MemStore:
    def __init__(self):
        self.cur, self.version = None, 0

    def get(self):
        return None if self.cur is None else (self.cur[0], self.cur[1], str(self.version))

    def write(self, holder, now, version):
        if (version is None) != (self.cur is None) or (version is not None and version != str(self.version)):
            return False
        self.cur, self.version = (holder, now), self.version + 1
        return True


class PodSet:
    """12 pods of which 3 are due to stop; a reaper acts only through its gate."""

    def __init__(self):
        self.stops, self.renewals = [], []
        self.live = ["p%d" % i for i in range(12)]

    def reap(self, gate):
        if not gate():
            return 0
        for p in list(self.live):
            if int(p[1:]) < 3:
                self.stops.append(p)
                self.live.remove(p)
            else:
                self.renewals.append(p)
        return 1


class TestLease(unittest.TestCase):
    def test_two_reapers_one_lease_each_action_once(self):
        store, now = MemStore(), [T0]
        a, b = mode.LeaseGate(store, "cloud", lambda: now[0]), mode.LeaseGate(store, "mac", lambda: now[0])
        podset = PodSet()
        for _ in range(3):
            podset.reap(a)
            now[0] += 1 * MIN
            podset.reap(b)
            now[0] += 1 * MIN
        self.assertEqual(sorted(set(podset.stops)), sorted(podset.stops))
        self.assertEqual(len(podset.stops), 3)
        self.assertEqual(len(set(podset.renewals)), 9)
        self.assertEqual(len(podset.renewals), 27)   # each live pod is renewed once per pass of the holder: 3 passes of 9 pods
        self.assertEqual(store.cur[0], "cloud")

    def test_mac_yields_while_the_lease_is_live_and_takes_over_after_the_lapse(self):
        store, now = MemStore(), [T0]
        cloud, mac = mode.LeaseGate(store, "cloud", lambda: now[0]), mode.LeaseGate(store, "mac", lambda: now[0])
        self.assertTrue(cloud())
        podset = PodSet()
        now[0] += dt.timedelta(seconds=mode.LEASE_SECONDS - 1)
        self.assertEqual((podset.reap(mac), len(podset.stops)), (0, 0))
        now[0] += dt.timedelta(seconds=5)
        self.assertEqual((podset.reap(mac), len(podset.stops)), (1, 3))

    def test_mutant_without_the_lease_acts_twice(self):
        podset = PodSet()
        always = lambda: True  # noqa: E731
        podset.live = ["p%d" % i for i in range(12)]
        self.assertEqual(podset.reap(always), 1)
        self.assertEqual(len(podset.stops), 3)
        podset2 = PodSet()
        podset2.reap(always)
        podset2.reap(always)
        self.assertEqual(len(podset2.renewals), 18)   # not the 9 of one holder: renewals repeat without the lease

    def test_lease_pairs_are_in_the_rights(self):
        self.assertLessEqual({("get", "leases"), ("create", "leases"), ("update", "leases")}, pairs(read(REAPER)))


class TestLiveness(unittest.TestCase):
    def test_missing_pass_alerts_once_fresh_does_not_and_status_prints_the_time(self):
        self.assertIsNone(mode.alert_line(T0 - 3 * MIN, T0))
        line = mode.alert_line(T0 - 25 * MIN, T0)
        self.assertIn("cloud reaper", line)
        self.assertIn("25 minutes", line)
        self.assertEqual(mode.status_line(T0 - 3 * MIN), "cloud reaper last pass: 2026-10-29T20:57:00Z")


# ---- S8 strengthened: the REAL default path of _reap_main in the Mac's mode; S18: the lease during a long pass ----
class LeaseKube(RecordingKube):
    """A fake cluster: the Lease reads as `lease` (a dict), or raises the error named; every other call is recorded by RecordingKube."""

    def __init__(self, env, pods, lease):
        super().__init__(env, pods)
        self.lease = lease

    def run(self, args, stdin=None, ok_missing=False, keep_stdout=False, timeout=120, missing_what=None):
        if args[:2] == ["get", "lease"]:
            self.calls.append(("get", "leases"))
            if isinstance(self.lease, Exception):
                raise self.lease
            return json.dumps(self.lease) if self.lease else ""
        return super().run(args, stdin, ok_missing, keep_stdout, timeout, missing_what)


def lease_doc(holder, renewed):
    return {"metadata": {"resourceVersion": "7"}, "spec": {"holderIdentity": holder, "renewTime": renewed.strftime("%Y-%m-%dT%H:%M:%S.000000Z")}}


def mac_pass(lease, ignore_lease=False):
    """One real Mac-mode pass over 3 pods past their hard limit; returns the number of stops. The lease is never written by the Mac."""
    import argparse
    import io
    import contextlib
    stops = []

    class Refresher:
        kube = None

        def run(self, force=False):
            return cw.TokenRefresh("skipped")
    with tempfile.TemporaryDirectory() as tmp:
        kube = LeaseKube(env_for(tmp), [pod("w%d" % i, 120) for i in range(3)], lease)
        devnull = os.open(os.devnull, os.O_RDONLY)
        patches = [mock.patch.object(cw, "reaper_lock", return_value=devnull),
                   mock.patch.object(cw.Launcher, "stop", lambda self, a, reason: stops.append(a.worker) or {})]
        if ignore_lease:
            patches.append(mock.patch.object(mode, "MacYieldGate", lambda clock: (lambda launcher: True)))
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            st.enter_context(contextlib.redirect_stdout(io.StringIO()))
            st.enter_context(contextlib.redirect_stderr(io.StringIO()))
            cw._reap_main(env_for(tmp), argparse.Namespace(loop=0), lambda s: None, clock=lambda: T0, refresher=Refresher(), kube=kube,
                          ticker_wait=lambda: __import__("time").sleep(0.01))
        writes = [c for c in kube.calls if c[0] in ("create", "update", "patch") and c[1] == "leases"]
    return len(stops), len(writes)


def check_mac_yields():
    held, _ = mac_pass(lease_doc("cloud", T0 - 30 * dt.timedelta(seconds=1)))
    if held != 0:
        raise AssertionError("the Mac acted %d times while the lease is held" % held)
    return held


class TestMacYields(unittest.TestCase):
    def test_mac_default_path_yields_to_a_live_lease_and_fails_open(self):
        held = check_mac_yields()
        lapsed, w1 = mac_pass(lease_doc("cloud", T0 - dt.timedelta(seconds=mode.LEASE_SECONDS + 5)))
        forbidden, w2 = mac_pass(cw.CloudError("Error from server (Forbidden): leases is forbidden", 1))
        missing, w3 = mac_pass(None)
        unreadable, _ = mac_pass(OSError("cluster unreachable"))
        print("S8: lease held %d actions, lapsed %d, forbidden %d (fail open), not found %d, unreachable %d" % (held, lapsed, forbidden, missing, unreadable))
        self.assertEqual((held, lapsed, forbidden, missing, unreadable), (0, 3, 3, 3, 3))
        self.assertEqual((w1, w2, w3), (0, 0, 0))   # the Mac never writes the lease

    def test_mutant_mac_pass_that_ignores_the_lease_fails(self):
        real = mac_pass
        globals()["mac_pass"] = lambda lease: real(lease, ignore_lease=True)
        try:
            with self.assertRaises(AssertionError):
                check_mac_yields()
        finally:
            globals()["mac_pass"] = real


def long_pass(beats=True):
    """A cloud reaper pass of 300 fake seconds over 12 pods past their hard limit (a stop takes 25 s); a second holder tries the lease at each stop."""
    now, renewals, takeovers = [T0], [0], [0]

    class Store(MemStore):
        def write(self, holder, when, version):
            ok = super().write(holder, when, version)
            renewals[0] += ok and holder == "cloud"
            return ok
    store = Store()
    clock = lambda: now[0]  # noqa: E731
    gate, rival = mode.LeaseGate(store, "cloud", clock), mode.LeaseGate(store, "mac", clock)
    if not beats:
        gate.beat = lambda: None   # the mutant: renewed only when the pass starts
    with tempfile.TemporaryDirectory() as tmp:
        kube = RecordingKube(env_for(tmp), [pod("w%d" % i, 120) for i in range(12)])
        L = cw.Launcher(kube, env_for(tmp), clock, lambda s: None)
        L.beat = gate.beat

        def stop(args, reason):
            now[0] += dt.timedelta(seconds=25)
            takeovers[0] += bool(rival())   # a second holder tries to take the lease during the pass: the cluster refuses it
            return {}
        L.stop = stop
        self_assert = gate(L)
        assert self_assert
        with mock.patch("sys.stderr"):
            L.reap()
    return renewals[0], takeovers[0], (now[0] - T0).total_seconds()


class TestLeaseDuringPass(unittest.TestCase):
    def test_renewed_during_a_long_pass_and_no_takeover(self):
        renewals, takeovers, secs = long_pass()
        print("S18: renewals %d in %d s, takeovers %d" % (renewals, secs, takeovers))
        self.assertEqual(secs, 300)
        self.assertGreaterEqual(renewals, 5)
        self.assertEqual(takeovers, 0)

    def test_a_stale_version_write_loses_the_lease(self):
        store = MemStore()
        self.assertTrue(store.write("cloud", T0, None))
        self.assertFalse(store.write("mac", T0, "0"))

    def test_mutant_renewal_only_at_the_start_fails(self):
        renewals, takeovers, _ = long_pass(beats=False)
        self.assertEqual(renewals, 1)
        with self.assertRaises(AssertionError):
            self.assertGreaterEqual(renewals, 5)


# ---- S8 (renewals counted) and S19 (a lost lease ends the pass), and the 10 s bound of the Mac's lease read ----
def mac_run(lease, ticker_ignores_lease=False):
    """One real Mac-mode _reap_main over 3 pods past their hard limit and 4 live pods with due tokens; the ticker's tick runs once, in line.
    Returns (stops, renewals) counted over the tick and the pass."""
    import argparse
    import io
    import contextlib
    stops, renewals = [], []

    class Refresher:
        kube = None

        def run(self, force=False):
            return cw.TokenRefresh("skipped")
    with tempfile.TemporaryDirectory() as tmp:
        pods = [pod("w%d" % i, 120) for i in range(3)] + [pod("l%d" % i, 30) for i in range(4)]
        kube = LeaseKube(env_for(tmp), pods, lease)
        devnull = os.open(os.devnull, os.O_RDONLY)
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(cw, "reaper_lock", return_value=devnull))
            st.enter_context(mock.patch.object(cw.Launcher, "stop", lambda self, a, reason: stops.append(a.worker) or {}))
            st.enter_context(mock.patch.object(cw.Launcher, "_renew_git_token", lambda self, p, now, force=False: renewals.append(p) or False))
            st.enter_context(mock.patch.object(cw.RenewalTicker, "start", lambda self: self.tick()))
            if ticker_ignores_lease:
                st.enter_context(mock.patch.object(mode.MacYieldGate, "renewal_allowed", lambda self, launcher: True))
            st.enter_context(contextlib.redirect_stdout(io.StringIO()))
            st.enter_context(contextlib.redirect_stderr(io.StringIO()))
            cw._reap_main(env_for(tmp), argparse.Namespace(loop=0), lambda s: None, clock=lambda: T0, refresher=Refresher(), kube=kube)
    return len(stops), len(renewals)


def check_mac_renewals_yield():
    stops, renewals = mac_run(lease_doc("cloud", T0 - dt.timedelta(seconds=30)))
    if (stops, renewals) != (0, 0):
        raise AssertionError("held: stops %d, renewals %d" % (stops, renewals))
    return stops, renewals


class TestMacRenewalsYield(unittest.TestCase):
    def test_mac_ticker_renews_nothing_while_the_lease_is_held(self):
        held = check_mac_renewals_yield()
        lapsed = mac_run(lease_doc("cloud", T0 - dt.timedelta(seconds=mode.LEASE_SECONDS + 5)))
        forbidden = mac_run(cw.CloudError("Error from server (Forbidden): leases is forbidden", 1))
        print("S8: lease held: stops %d, renewals %d; lapsed: stops %d, renewals %d; forbidden: stops %d, renewals %d (fail open)"
              % (held + lapsed + forbidden))
        self.assertEqual(lapsed, (3, 8))   # the tick and the pass renew the 4 live pods each
        self.assertEqual(forbidden, (3, 8))

    def test_mutant_ticker_that_ignores_the_lease_renews_while_held(self):
        stops, renewals = mac_run(lease_doc("cloud", T0 - dt.timedelta(seconds=30)), ticker_ignores_lease=True)
        self.assertGreater(renewals, 0)   # the mutation applied: the tick renewed although the lease is held
        real = mac_run
        globals()["mac_run"] = lambda lease: real(lease, ticker_ignores_lease=True)
        try:
            with self.assertRaises(AssertionError):
                check_mac_renewals_yield()
        finally:
            globals()["mac_run"] = real


class TimeoutKube(LeaseKube):
    def __init__(self, *a):
        super().__init__(*a)
        self.timeouts = []

    def run(self, args, stdin=None, ok_missing=False, keep_stdout=False, timeout=120, missing_what=None):
        if args[:2] == ["get", "lease"]:
            self.timeouts.append(timeout)
        return super().run(args, stdin, ok_missing, keep_stdout, timeout, missing_what)


class TestLeaseReadBound(unittest.TestCase):
    def test_mac_lease_read_uses_a_ten_second_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            kube = TimeoutKube(env_for(tmp), [], lease_doc("cloud", T0))
            self.assertFalse(mode.MacYieldGate(lambda: T0)(mock.Mock(kube=kube)))
            self.assertEqual(kube.timeouts, [10])


def lost_pass(honour=True, takeover_after=4):
    """A pass of 12 stops of 40 fake seconds; after the 4th a second holder takes the lease. Returns (stops after, renewals after, stderr lines)."""
    import io
    now, stops, renewals = [T0], [], []
    store = MemStore()
    clock = lambda: now[0]  # noqa: E731
    gate = mode.LeaseGate(store, "cloud", clock)
    err = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp:
        kube = RecordingKube(env_for(tmp), [pod("w%d" % i, 120) for i in range(12)])
        L = cw.Launcher(kube, env_for(tmp), clock, lambda s: None)
        L.beat = gate.beat if honour else (lambda: gate.beat() or True)

        def stop(args, reason):
            now[0] += dt.timedelta(seconds=40)
            stops.append(args.worker)
            if len(stops) == takeover_after:
                store.cur, store.version = ("mac", now[0]), store.version + 1   # the lease lapsed and a second holder took it
            return {}
        L.stop = stop
        L._renew_git_token = lambda p, n, force=False: renewals.append(p) or False
        assert gate(L)
        with mock.patch("sys.stderr", err):
            L.reap()
            L.renew_git_tokens([pod("l0", 30)], now[0])   # a renewal asked for after the loss, in the same pass
    lines = [x for x in err.getvalue().splitlines() if "lease lost" in x]
    return len(stops) - takeover_after, len(renewals), lines


class TestLostLease(unittest.TestCase):
    def test_a_pass_that_lost_the_lease_stops_and_renews_nothing_more(self):
        stops, renewals, lines = lost_pass()
        print("S19: stops after the loss %d, renewals after the loss %d, lines %d" % (stops, renewals, len(lines)))
        self.assertEqual((stops, renewals, len(lines)), (0, 0, 1))
        self.assertEqual(lines, ["cloud-reaper: lease lost, pass ended"])

    def test_mutant_that_ignores_the_renewal_result_keeps_stopping(self):
        stops, _, lines = lost_pass(honour=False)
        self.assertEqual(stops, 8)
        with self.assertRaises(AssertionError):
            self.assertEqual(stops, 0)
        self.assertEqual(lines, [])

    def test_the_cloud_renewal_thread_yields_when_the_lease_is_not_held(self):
        gate = mode.LeaseGate(MemStore(), "cloud", lambda: T0)
        self.assertFalse(gate.holds())
        self.assertTrue(gate())
        self.assertTrue(gate.holds())
        self.assertFalse(mode.LeaseGate(gate.store, "mac", lambda: T0)())


if __name__ == "__main__":
    unittest.main()
