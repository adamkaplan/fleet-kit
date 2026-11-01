"""#1579: the reaper's renewal of every due git token no longer waits for the steps before it.

Offline: a fake clock, a fake cluster and fake pods; the real Launcher.reap, renewal loop, RenewalTicker and slow-pass note. The old order is
rebuilt from the real source (a scratch copy, the edit asserted applied); the same copy is the mutant the stall test must fail on.
"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as real  # noqa: E402

T0 = dt.datetime(2026, 10, 29, 21, 0, 0, tzinfo=dt.timezone.utc)
MIN = dt.timedelta(minutes=1)
UNAUTH = "kubectl get: You must be logged in to the server (Unauthorized)"
MODULE_ENV = "CW_TEST_REAPER_MODULE"   # a scratch copy of the code, for the mutant run


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class FakeKube:
    def __init__(self, pods):
        self.pods, self.logged_in = pods, True

    def list_pods(self):
        return list(self.pods)

    def scrub(self, text, limit=300):
        return str(text)[:limit]

    def add_secrets(self, *a):
        pass

    def close(self):
        pass


class Refresher:
    def __init__(self, kube=None):
        self.runs, self.forced, self.kube = 0, 0, kube

    def run(self, force=False):
        self.runs += 1
        if force:
            self.forced += 1
            if self.kube:
                self.kube.logged_in = True
        return types_ns(state="written" if force else "fresh", token="")


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def load_module(path):
    spec = importlib.util.spec_from_file_location("cloud_worker_scratch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def target_module():
    path = os.environ.get(MODULE_ENV)
    return load_module(path) if path else real


def old_order_copy(directory):
    """The real source with the renewal put back as the LAST step and the thread's cadence beyond any pass (the code before #1579)."""
    with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
        src = f.read()
    edits = [("        renewed = self.renew_git_tokens(self._before_hard_limit(pods, now), now)\n", "        renewed = []\n"),
             ("        out.extend(renewed)\n", "        out.extend(self.renew_git_tokens(live, now))\n"),
             ("RENEW_CADENCE_SECONDS = 600 ", "RENEW_CADENCE_SECONDS = 10 ** 9 ")]
    for old, new in edits:
        assert src.count(old) == 1, old
        src = src.replace(old, new)
    path = os.path.join(directory, "cloud_worker_old_order.py")
    with open(path, "w") as f:
        f.write(src)
    return path


def make_pods(n_due_now=6, n_due_later=6):
    """Pods whose token was minted 40 minutes ago (due now) or 20 minutes ago (due in 10 minutes), and one idle pod to stop."""
    pods, minted, iso_t0 = [], {}, real.iso(T0)
    for i in range(n_due_now + n_due_later):
        name = "widget-%02d" % i
        pods.append({"metadata": {"name": name, "annotations": {"cw/worker": "coder%d" % i, "cw/started": iso_t0}}, "status": {"phase": "Running"}})
        minted[name] = T0 - (40 if i < n_due_now else 20) * MIN
    pods.append({"metadata": {"name": "widget-idle", "annotations": {"cw/worker": "coderidle", "cw/started": iso_t0}}, "status": {"phase": "Running"}})
    return pods, minted


def build(mod, clock, tmp, stop=None, wait=None, with_ticker=False, all_due=True):
    pods, minted = make_pods(12, 0) if all_due else make_pods()
    kube = FakeKube(pods)
    env = {"CW_CLOUD_AUDIT": os.path.join(tmp, "audit.jsonl"), "XDG_STATE_HOME": tmp}
    renewed, attempts = {}, []

    class L(mod.Launcher):
        def hard_deadline(self, pod):
            return T0 + 300 * MIN

        def activity(self, name):
            return (False, T0 - 60 * MIN) if name == "widget-idle" else (True, T0)

        def cost(self, pod):
            return 0.0

        def stop(self, args, reason):
            return stop(args, reason) if stop else {}

        def _renew_git_token(self, pod, now, force=False):
            name = pod["metadata"]["name"]
            attempts.append(name)
            if not kube.logged_in:
                raise mod.CloudError(UNAUTH)
            if name not in minted or (now - minted[name]).total_seconds() < mod.GIT_TOKEN_RENEW_AFTER_SECONDS:
                return False
            minted[name] = clock()
            renewed.setdefault(name, clock())   # the FIRST renewal of each pod
            return True

    launcher = L(kube, env, clock, lambda s: None)
    refresher = Refresher(kube)
    ticker = None
    if with_ticker:
        ticker = mod.RenewalTicker(launcher, refresher, clock, wait)
    return launcher, kube, refresher, ticker, renewed, attempts, env


def audit_lines(env):
    try:
        with open(env["CW_CLOUD_AUDIT"]) as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


def stall_scenario(mod, all_due=False):
    """One pass while the stop of the idle pod takes 50 minutes of the fake clock. Returns the measurements."""
    clock = Clock()
    go, done = threading.Semaphore(0), threading.Semaphore(0)

    def wait():
        done.release()
        go.acquire()

    def stop(args, reason):
        done.acquire()   # the thread is parked in its wait
        for _ in range(50):
            clock.now += MIN
            go.release()
            done.acquire()
        return {}

    with tempfile.TemporaryDirectory() as tmp:
        launcher, kube, refresher, ticker, renewed, attempts, env = build(mod, clock, tmp, stop, wait, True, all_due=all_due)
        ticker.start()
        launcher.reap()
        ticker._stop.set()
        go.release()
    due = {n: T0 + (0 if all_due or int(n[-2:]) < 6 else 10) * MIN for n in renewed}
    return {"renewed": renewed, "due": due, "refreshes": refresher.runs, "stop_ends": T0 + 50 * MIN}


class S2OldOrder(unittest.TestCase):
    def test_old_order_renews_nothing_until_the_stop_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = load_module(old_order_copy(tmp))
            r = stall_scenario(old, all_due=True)
        before = [n for n, t in r["renewed"].items() if t < r["stop_ends"]]
        after = [n for n, t in r["renewed"].items() if t >= r["stop_ends"]]
        self.assertEqual((len(before), len(after)), (0, 12))


class S3Stall(unittest.TestCase):
    def test_every_due_token_is_renewed_within_20_minutes_and_login_refreshed(self):
        r = stall_scenario(target_module())
        late = [n for n, t in r["renewed"].items() if t - r["due"][n] > 20 * MIN]
        self.assertEqual((len(r["renewed"]), len(late)), (12, 0))
        self.assertGreaterEqual(r["refreshes"], 2)


class S4RaisingStop(unittest.TestCase):
    def test_a_stop_that_raises_still_leaves_12_renewed(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            def stop(args, reason):
                raise real.CloudError("the stop failed", 3)
            launcher, kube, refresher, ticker, renewed, attempts, env = build(real, clock, tmp, stop)
            out = launcher.reap()
        self.assertEqual(len(renewed), 12)
        self.assertEqual(len([o for o in out if o[1] == "idle" and o[2] != 0]), 1)

    def test_a_stop_that_raises_an_unexpected_error_still_leaves_12_renewed(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            def stop(args, reason):
                raise RuntimeError("boom")
            launcher, kube, refresher, ticker, renewed, attempts, env = build(real, clock, tmp, stop)
            with self.assertRaises(RuntimeError):
                launcher.reap()
        self.assertEqual(len(renewed), 12)


class S5LoginLapse(unittest.TestCase):
    def test_a_lapsed_login_is_refreshed_and_retried_once_in_the_pass(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            launcher, kube, refresher, ticker, renewed, attempts, env = build(real, clock, tmp)
            kube.logged_in = False
            launcher.relogin = lambda: refresher.run(force=True).state == "written"
            launcher.reap()
            lines = audit_lines(env)
        self.assertEqual(refresher.forced, 1)
        self.assertEqual(attempts.count("widget-00"), 2)   # 1 try, 1 retry
        self.assertEqual(len(renewed), 12)
        self.assertEqual([x for x in lines if x["action"] == "renew-git-token" and not x["ok"]], [])


class S6SlowPass(unittest.TestCase):
    def note(self, seconds):
        clock = Clock()
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            launcher, kube, refresher, ticker, renewed, attempts, env = build(real, clock, tmp)
            with contextlib.redirect_stderr(err):
                launcher.note_slow_pass(seconds)
            return err.getvalue().splitlines(), audit_lines(env)

    def test_the_limit_is_a_named_constant(self):
        self.assertEqual(real.SLOW_PASS_SECONDS, 600)

    def test_a_long_pass_writes_one_log_line_and_one_audit_line_with_the_seconds(self):
        log, audit = self.note(601)
        self.assertEqual(len(log), 1)
        self.assertEqual(len(audit), 1)
        self.assertIn("601", audit[0]["reason"])
        self.assertEqual(audit[0]["action"], real.SLOW_PASS_ACTION)

    def test_a_short_pass_writes_nothing(self):
        self.assertEqual(self.note(600), ([], []))

    def test_the_loop_measures_each_pass_with_the_note(self):
        clock, err = Clock(), io.StringIO()
        args = types_ns(loop=0)
        with tempfile.TemporaryDirectory() as tmp:
            launcher, kube, refresher, ticker, renewed, attempts, env = build(real, clock, tmp)

            def slow_reap():
                clock.now += 11 * MIN
                return []
            launcher.reap = slow_reap
            devnull = os.open(os.devnull, os.O_RDONLY)
            with mock.patch.object(real, "reaper_lock", return_value=devnull), mock.patch.object(real, "Launcher", return_value=launcher), \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                real._reap_main(env, args, lambda s: None, clock=clock, refresher=Refresher(kube), kube=kube, ticker_wait=lambda: time.sleep(0.01))
            lines = audit_lines(env)
        self.assertEqual(len([x for x in lines if x["action"] == real.SLOW_PASS_ACTION]), 1)


class S8Mutant(unittest.TestCase):
    def test_the_stall_test_fails_on_the_old_order_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = old_order_copy(tmp)   # asserts every edit applied
            with open(path) as f:
                self.assertIn("RENEW_CADENCE_SECONDS = 10 ** 9", f.read())
            env = dict(os.environ, **{MODULE_ENV: path})
            p = subprocess.run([sys.executable, "-m", "unittest", "test_reaper_renewal.S3Stall"], cwd=HERE, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
        self.assertEqual(p.returncode, 1, p.stdout)


if __name__ == "__main__":
    unittest.main()
