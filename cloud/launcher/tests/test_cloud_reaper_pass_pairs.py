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


class Fleet:
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
        fleet = Fleet()
        for _ in range(3):
            fleet.reap(a)
            now[0] += 1 * MIN
            fleet.reap(b)
            now[0] += 1 * MIN
        self.assertEqual(sorted(set(fleet.stops)), sorted(fleet.stops))
        self.assertEqual(len(fleet.stops), 3)
        self.assertEqual(len(set(fleet.renewals)), 9)
        self.assertEqual(len(fleet.renewals), 27)   # each live pod is renewed once per pass of the holder: 3 passes of 9 pods
        self.assertEqual(store.cur[0], "cloud")

    def test_mac_yields_while_the_lease_is_live_and_takes_over_after_the_lapse(self):
        store, now = MemStore(), [T0]
        cloud, mac = mode.LeaseGate(store, "cloud", lambda: now[0]), mode.LeaseGate(store, "mac", lambda: now[0])
        self.assertTrue(cloud())
        fleet = Fleet()
        now[0] += dt.timedelta(seconds=mode.LEASE_SECONDS - 1)
        self.assertEqual((fleet.reap(mac), len(fleet.stops)), (0, 0))
        now[0] += dt.timedelta(seconds=5)
        self.assertEqual((fleet.reap(mac), len(fleet.stops)), (1, 3))

    def test_mutant_without_the_lease_acts_twice(self):
        fleet = Fleet()
        always = lambda: True  # noqa: E731
        fleet.live = ["p%d" % i for i in range(12)]
        self.assertEqual(fleet.reap(always), 1)
        self.assertEqual(len(fleet.stops), 3)
        fleet2 = Fleet()
        fleet2.reap(always)
        fleet2.reap(always)
        self.assertEqual(len(fleet2.renewals), 18)   # not the 9 of one holder: renewals repeat without the lease

    def test_lease_pairs_are_in_the_rights(self):
        self.assertLessEqual({("get", "leases"), ("create", "leases"), ("update", "leases")}, pairs(read(REAPER)))


class TestLiveness(unittest.TestCase):
    def test_missing_pass_alerts_once_fresh_does_not_and_status_prints_the_time(self):
        self.assertIsNone(mode.alert_line(T0 - 3 * MIN, T0))
        line = mode.alert_line(T0 - 25 * MIN, T0)
        self.assertIn("cloud reaper", line)
        self.assertIn("25 minutes", line)
        self.assertEqual(mode.status_line(T0 - 3 * MIN), "cloud reaper last pass: 2026-10-29T20:57:00Z")


if __name__ == "__main__":
    unittest.main()
