"""#1663: a reaper call answered Unauthorized makes the reaper refresh its login at once (through the refresher's own retry gap) and retry that call once.

Offline: a fake clock, a fake cluster answer (Kube._run replaced), a counted vault read (cluster_token replaced). The mutants are scratch copies
of the real source outside the repository, each edit asserted applied; the same tests run in a child and must fail there."""
import datetime as dt
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, LAUNCHER)
sys.path.insert(0, os.path.join(ROOT, "cloud", "worker", "tests"))
import cloud_worker as real  # noqa: E402
import stub_support  # noqa: E402

MODULE_ENV = "CW_TEST_UNAUTH_MODULE"
T0 = dt.datetime(2026, 10, 30, 12, 0, 0, tzinfo=dt.timezone.utc)
MIN = dt.timedelta(minutes=1)
PLANT = "sk-PLANTED0123456789"
UNAUTH = "You must be logged in to the server (Unauthorized)"


def load_module(path):
    spec = importlib.util.spec_from_file_location("cloud_worker_unauth_scratch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def target():
    path = os.environ.get(MODULE_ENV)
    return load_module(path) if path else real


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class World:
    """A refresher and a Kube of the target module, a counted vault and a scripted cluster."""

    def __init__(self, test, vault_ok=True, answers=None):
        self.cw = target()
        d = tempfile.mkdtemp()
        test.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        os.chmod(d, 0o700)
        self.audit = os.path.join(d, "audit.jsonl")
        env = {"CW_CLOUD_TOKEN_FILE": os.path.join(d, "token"), "CW_CLOUD_AUDIT": self.audit}
        self.clock, self.reads, self.attempts, self.answers = Clock(), 0, 0, list(answers or [])

        def mint(*a, **k):
            self.reads += 1
            if not vault_ok:
                raise self.cw.ClusterLogin("token-command-failed")
            return "tok-%d" % self.reads
        p = mock.patch.object(self.cw, "cluster_token", mint)
        p.start()
        test.addCleanup(p.stop)

        def fake_run(kube, args, *a, **k):
            self.attempts += 1
            answer = self.answers.pop(0) if self.answers else "ok"
            if answer != "ok":
                raise self.cw.CloudError("kubectl %s: %s" % (args[0], answer))
            return "{}"
        p2 = mock.patch.object(self.cw.Kube, "_run", fake_run)
        p2.start()
        test.addCleanup(p2.stop)
        self.refresher = self.cw.ClusterTokenRefresher(env, self.clock)
        self.kube = self.cw.Kube.__new__(self.cw.Kube)
        self.kube.on_unauthorized = lambda: self.refresher.run(unauthorized=True).state == "written"

    def call(self, verb="get"):
        try:
            self.kube.run([verb, "pods"])
            return True
        except self.cw.CloudError:
            return False

    def audit_text(self):
        try:
            with open(self.audit) as f:
                return f.read()
        except OSError:
            return ""


class Scenarios(unittest.TestCase):
    def test_s2_a_renewal_answered_unauthorized_refreshes_and_retries_once(self):
        w = World(self, answers=[UNAUTH])
        done = w.call("patch")
        print("S2: refreshes %d of 1, renewal attempts %d, renewals done %d of 1" % (w.reads, w.attempts, int(done)))
        self.assertEqual((w.reads, w.attempts, done), (1, 2, True))

    def test_s3_five_answers_in_a_minute_read_the_vault_once(self):
        w = World(self, vault_ok=False, answers=[UNAUTH] * 5)
        failed = 0
        for _ in range(5):
            failed += not w.call()
            w.clock.now += dt.timedelta(seconds=10)
        extra = w.attempts - 5
        print("S3: vault reads in the minute %d, calls failed as today %d, retries beyond the first of each call %d" % (w.reads, failed, extra))
        self.assertEqual((w.reads, failed, extra), (1, 5, 0))

    def test_s4_the_refresh_point_is_within_45_minutes(self):
        life = real.parse_duration(real.CLUSTER_TOKEN_DURATION) / 60
        margin = real.CLUSTER_TOKEN_REFRESH_MARGIN_SECONDS / 60
        print("S4: refresh point %d minutes (at most 45), margin %d minutes (at least 15)" % (life - margin, margin))
        self.assertLessEqual(life - margin, 45)
        self.assertGreaterEqual(margin, 15)

    def test_s5_u3_the_audit_line_is_fixed_and_one_line(self):
        w = World(self, answers=["%s %s\nnext\x1b[31m" % (UNAUTH, PLANT)])
        w.call()
        text = w.audit_text()
        lines = [x for x in text.splitlines() if w.cw.REFRESH_ACTION in x]
        print("S5: audit lines %d, planted text occurrences %d" % (len(lines), text.count(PLANT)))
        self.assertEqual((len(lines), text.count(PLANT)), (1, 0))
        self.assertIn("refreshed on unauthorized", lines[0])
        print("U3: newlines %d, escape characters %d" % (lines[0].count("\n"), text.count("\x1b") + text.count("\\u001b")))
        self.assertEqual((text.count("\n"), text.count("\x1b"), text.count("\\u001b")), (1, 0, 0))

    def test_s6_a_pass_without_unauthorized_is_as_the_base(self):
        w = World(self)
        ok = [w.call() for _ in range(2)]
        for minutes in (0, 10, 45):   # the ordinary ticks: the file is missing, fresh, then inside the margin
            w.clock.now = T0 + minutes * MIN
            w.refresher.run()
        lines = len(w.audit_text().splitlines())
        got = (w.reads, w.attempts, lines)
        base = (3, 2, 3)   # measured on the base: vault reads, cluster calls, audit lines
        same = sum(a == b for a, b in zip(got, base))
        print("S6: vault reads, renewals and audit lines equal the base %d of 3" % same)
        self.assertEqual((ok, got), ([True, True], base))

    def test_u2_a_call_answered_twice_is_attempted_twice(self):
        w = World(self, answers=[UNAUTH, UNAUTH, UNAUTH])
        failed = not w.call()
        print("U2: attempts %d, failures %d" % (w.attempts, int(failed)))
        self.assertEqual((w.attempts, failed), (2, True))

    def test_the_end_hooks_exec_and_a_create_are_never_retried(self):
        for verb in ("exec", "create"):
            w = World(self, answers=[UNAUTH, UNAUTH])
            self.assertFalse(w.call(verb))
            self.assertEqual(w.attempts, 1)


class S9Relogin(unittest.TestCase):
    def test_s9_the_relogin_of_the_reaper_loop_honours_the_retry_gap(self):
        w = World(self, vault_ok=False)
        cw = w.cw
        grabbed = []

        class NoTicker:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

            def close(self):
                pass

        def reap(launcher):
            grabbed.append(launcher.relogin)
            launcher.reap_results = {}
            return []
        kube = mock.Mock()
        env = {"CW_CLOUD_NAMESPACE": "cw-s9-%d" % os.getpid()}
        with mock.patch.object(cw, "RenewalTicker", NoTicker), mock.patch.object(cw.Launcher, "reap", reap), \
                mock.patch.object(cw, "reaper_lock", lambda *a, **k: None), mock.patch.object(cw.Launcher, "_catch_signals", staticmethod(lambda: {})):
            cw._reap_main(env, mock.Mock(loop=False), lambda s: None, clock=w.clock, refresher=w.refresher, kube=kube)
        relogin = grabbed[0]
        w.reads = 0   # the pass's own refresh failed; count from here
        w.clock.now += 301 * dt.timedelta(seconds=1)
        first = relogin()
        r1 = w.reads
        w.clock.now += 60 * dt.timedelta(seconds=1)
        w.reads = 0
        second = relogin()
        r2 = w.reads
        w.clock.now += 301 * dt.timedelta(seconds=1)
        w.reads = 0
        relogin()
        r3 = w.reads
        w.clock.now += 60 * dt.timedelta(seconds=1)
        w.reads = 0
        extra_answer = relogin()
        extra_reads = w.reads
        print("S9: vault reads first call %d, second call inside the gap %d, call after the gap %d, inside-gap calls answering not written %d of 2"
              % (r1, r2, r3, sum(1 for x in (second, extra_answer) if x is False)))
        self.assertEqual((r1, r2, r3, first, second), (1, 0, 1, False, False))
        self.assertEqual((extra_reads, extra_answer), (0, False))


def mutate(directory, edits):
    with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
        src = f.read()
    for old, new in edits:
        assert src.count(old) == 1, old
        src = src.replace(old, new)
    path = os.path.join(directory, "cloud_worker.py")
    with open(path, "w") as f:
        f.write(src)
    return path


RETRY_TAIL = "        return self._run(args, *a, **kw)\n\n    def _run("
MUTANTS = {
    "no retry": ([('"unauthorized" in str(e).lower() and self.on_unauthorized()', '"unauthorized" in str(e).lower() and False')],
                 "test_s2_a_renewal_answered_unauthorized_refreshes_and_retries_once"),
    "retry without a limit": ([(RETRY_TAIL, "        for _ in range(5):\n            try:\n                return self._run(args, *a, **kw)\n"
                                "            except CloudError:\n                pass\n        return self._run(args, *a, **kw)\n\n    def _run(")],
                              "test_u2_a_call_answered_twice_is_attempted_twice"),
    "refresh past the vault gap": ([("if not (force and not unauthorized) and self.last_failure", "if not force and self.last_failure")],
                                   "test_s3_five_answers_in_a_minute_read_the_vault_once"),
}


class S7Mutants(unittest.TestCase):
    def test_each_mutant_fails_its_test(self):
        caught = applied = 0
        for name, (edits, test_name) in MUTANTS.items():
            with tempfile.TemporaryDirectory() as tmp:
                path = mutate(tmp, edits)   # asserts every edit applied
                applied += 1
                p = stub_support.start(self, [sys.executable, "-m", "unittest", "test_reaper_unauthorized.Scenarios"], cwd=HERE,
                                       env=dict(os.environ, **{MODULE_ENV: path}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                out = p.communicate(timeout=90)[0]
            if p.returncode == 1 and ("FAIL: %s " % test_name in out or "ERROR: %s " % test_name in out):
                caught += 1
            else:
                print("S7: mutant survived: %s" % name)
        print("S7: mutants caught %d of 3, mutations applied %d of 3" % (caught, applied))
        self.assertEqual((caught, applied), (3, 3))


class S9Mutant(unittest.TestCase):
    def test_the_mutant_with_force_true_fails_s9(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = mutate(tmp, [("r = refresher.run(unauthorized=True)", "r = refresher.run(force=True)")])   # asserts it occurs once
            applied = True
            p = stub_support.start(self, [sys.executable, "-m", "unittest", "test_reaper_unauthorized.S9Relogin"], cwd=HERE,
                                   env=dict(os.environ, **{MODULE_ENV: path}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            out = p.communicate(timeout=90)[0]
        failed = p.returncode == 1 and "FAIL: test_s9_" in out and "(1, 0, 1, False, False)" in out and "second call inside the gap 1" in out
        print("S9 mutant: failed on the second vault read inside the gap %d of 1, mutation applied %s" % (1 if failed else 0, applied))
        self.assertTrue(failed)


class S8Docs(unittest.TestCase):
    def test_the_readme_says_the_rule(self):
        with open(os.path.join(LAUNCHER, "README.md")) as f:
            text = f.read()
        found = sum(p in text for p in ("refresh at once", "every five minutes"))
        print("S8: phrases found %d of 2" % found)
        self.assertEqual(found, 2)


if __name__ == "__main__":
    unittest.main()
