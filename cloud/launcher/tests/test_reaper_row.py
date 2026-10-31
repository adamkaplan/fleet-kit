"""#1510 S3 (DW-3): the reaper retires the registry row of a worker it ends, exactly once.

Offline: a fake cluster of 3 pods (past its hard limit, idle, running), the registry's own functions on a scratch state directory, the real
Launcher.reap. Two real mutants (a scratch copy of the source, the edit asserted applied) are caught by RUNNING the tests.
"""
import contextlib
import datetime as dt
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as real  # noqa: E402
import registry  # noqa: E402  (the launcher put bin/ on the path)

T0 = dt.datetime(2026, 10, 30, 9, 0, 0, tzinfo=dt.timezone.utc)
MIN = dt.timedelta(minutes=1)
MODULE_ENV = "CW_TEST_ROW_MODULE"
PLANT = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
ENDED = ("coderhard", "coderidle")


class FakeKube:
    def __init__(self, pods):
        self.pods = pods

    def list_pods(self):
        return list(self.pods)

    def scrub(self, text, limit=300):
        return str(text)[:limit]

    def add_secrets(self, *a):
        pass

    def close(self):
        pass


def load_module(path):
    spec = importlib.util.spec_from_file_location("cloud_worker_row_scratch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def target_module():
    path = os.environ.get(MODULE_ENV)
    return load_module(path) if path else real


def mutant_copy(directory, kind):
    with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
        src = f.read()
    call = "                    self.retire_row(worker, reason)"
    new = {"removed": "                    pass", "twice": call + "\n" + call}[kind]
    assert src.count(call + "   #") == 1
    src = src.replace(call + "   #", new + "   #")
    path = os.path.join(directory, "cloud_worker_row_%s.py" % kind)
    with open(path, "w") as f:
        f.write(src)
    return path


def pods():
    stamp = real.iso(T0)
    return [{"metadata": {"name": "w-%s" % w, "annotations": {"cw/worker": "coder" + w, "cw/started": stamp}}, "status": {"phase": "Running"}}
            for w in ("hard", "idle", "run")]


class Scene:
    def __init__(self, mod, tmp, fail_stop=()):
        self.state = os.path.join(tmp, "fl" + "eet-switchboard")
        os.makedirs(self.state)
        for w in ("hard", "idle", "run"):
            registry.upsert(self.state, "coder" + w, {"session": "s" + w, "role": "coder", "repo": "r", "kind": "cloud", "state": "working"})
        self.retires, self.fail_stop = [], set(fail_stop)
        scene = self

        class L(mod.Launcher):
            def hard_deadline(self, pod):
                return T0 - MIN if pod["metadata"]["name"] == "w-hard" else T0 + 300 * MIN

            def activity(self, name):
                return (False, T0 - 60 * MIN) if name == "w-idle" else (True, T0)

            def cost(self, pod):
                return 0.0

            def stop(self, args, reason):
                if args.worker in scene.fail_stop:
                    raise mod.CloudError("stop failed", 1)
                return {}

            def send_wip_notes(self, live, now):
                pass

        self.mod = mod
        env = {"CW_CLOUD_AUDIT": os.path.join(tmp, "audit.jsonl"), "XDG_STATE_HOME": tmp}
        self.launcher = L(FakeKube(pods()), env, lambda: T0, lambda s: None)

    def counted(self, name, **kw):
        orig = registry.retire

        def wrapper(state, n, *a, **k):
            self.retires.append(n)
            return orig(state, n, *a, **k)
        return wrapper

    def pass_(self, retire=None):
        err = io.StringIO()
        with mock.patch.object(registry, "retire", retire or self.counted("x")), contextlib.redirect_stderr(err):
            out = self.launcher.reap()
        return out, err.getvalue()

    def live(self, name):
        return registry.get(self.state, name) is not None


class S3Row(unittest.TestCase):
    def test_one_retire_after_the_stop_none_after_a_second_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene(target_module(), tmp)
            out, _ = s.pass_()
            first = {w: s.retires.count(w) for w in ENDED}
            kept = s.live("coderrun")
            gone = [not s.live(w) for w in ENDED]
            s.launcher.kube.pods = [p for p in s.launcher.kube.pods if p["metadata"]["name"] == "w-run"]   # the ended pods are gone
            s.pass_()
            total = {w: s.retires.count(w) for w in ENDED}
        print("retire counts after the stop: %s; after a second pass: %s" % (first, total))
        self.assertEqual(sorted(w for w, _, c in out if c == 0), sorted(ENDED))
        self.assertEqual(first, {"coderhard": 1, "coderidle": 1})
        self.assertEqual(total, first)
        self.assertEqual(gone, [True, True], "the row of an ended worker is retired")
        self.assertTrue(kept, "the row of the running worker stays")
        self.assertEqual(s.retires.count("coderrun"), 0)

    def test_a_failed_stop_leaves_the_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene(target_module(), tmp, fail_stop=ENDED)
            out, _ = s.pass_()
            counts = {w: s.retires.count(w) for w in ENDED}
            rows = [s.live(w) for w in ENDED]
        print("retire counts after a failed stop: %s" % counts)
        self.assertEqual(counts, {"coderhard": 0, "coderidle": 0})
        self.assertEqual(rows, [True, True])
        self.assertTrue(all(c != 0 for w, _, c in out if w in ENDED))

    def test_a_failed_registry_retire_gives_one_stderr_line_and_the_pod_is_still_ended(self):
        def boom(*a, **k):
            raise OSError("cannot write %s under %s" % (PLANT, a[0]))
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene(target_module(), tmp)
            out, err = s.pass_(retire=boom)
        lines = [x for x in err.splitlines() if x.strip()]
        ours = [x for x in lines if x.startswith("registry: coderhard:")]
        self.assertEqual(len(ours), 1, err)
        self.assertEqual(len([x for x in lines if x.startswith("registry: coderidle:")]), 1, err)
        self.assertNotIn(PLANT, err)
        self.assertEqual(sorted(w for w, _, c in out if c == 0), sorted(ENDED))

    def test_a_registry_problem_text_is_one_line_and_the_pass_goes_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene(target_module(), tmp)
            out, err = s.pass_(retire=lambda *a, **k: {"problem": "locked\nby another"})
        self.assertEqual(len(err.strip().splitlines()), 2, err)
        self.assertEqual(sorted(w for w, _, c in out if c == 0), sorted(ENDED))

    def test_the_retire_of_an_absent_row_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene(target_module(), tmp)
            for w in ENDED:
                registry.retire(s.state, w, "test", "gone already")   # the rows are absent before the pass
            out, err = s.pass_()
        self.assertEqual(sorted(w for w, _, c in out if c == 0), sorted(ENDED))


class Mutants(unittest.TestCase):
    def run_mutant(self, kind):
        with tempfile.TemporaryDirectory() as tmp:
            path = mutant_copy(tmp, kind)
            env = dict(os.environ, **{MODULE_ENV: path})
            p = subprocess.run([sys.executable, "-m", "unittest", "test_reaper_row.S3Row.test_one_retire_after_the_stop_none_after_a_second_pass"],
                               cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
        return p

    def test_mutant_retire_removed_is_caught(self):
        p = self.run_mutant("removed")
        print("mutant retire removed: exit %s" % p.returncode)
        self.assertEqual(p.returncode, 1, p.stdout)

    def test_mutant_retire_called_twice_is_caught(self):
        p = self.run_mutant("twice")
        print("mutant retire twice: exit %s" % p.returncode)
        self.assertEqual(p.returncode, 1, p.stdout)


if __name__ == "__main__":
    unittest.main()
