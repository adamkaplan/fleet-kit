"""#1510 S3 (DW-3): the reaper retires the registry row of a worker it ends, exactly once.

Offline: a fake clock, a fake cluster of 3 pods (past its hard limit, idle, running) and a fake registry holding their rows; the real
Launcher.reap. The mutants are scratch copies of the real source (the edit asserted applied) run in a child process; each must exit 1.
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

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as real  # noqa: E402

T0 = dt.datetime(2026, 10, 29, 21, 0, 0, tzinfo=dt.timezone.utc)
MIN = dt.timedelta(minutes=1)
MODULE_ENV = "CW_TEST_REAPER_ROW_MODULE"
PLANTED = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
ADDRESS = "https://worker-old.example.test:4096"


class FakeKube:
    def __init__(self, pods):
        self.pods = pods

    def list_pods(self):
        return list(self.pods)

    def scrub(self, text, limit=300):
        return str(text).replace(PLANTED, "[scrubbed]")[:limit]

    def add_secrets(self, *a):
        pass

    def close(self):
        pass


class FakeRegistry:
    def __init__(self, names, problem=None, boom=None):
        self.rows, self.retires, self.problem, self.boom = set(names), [], problem, boom

    def retire(self, state_dir, name, by, reason, **kw):
        self.retires.append(name)
        if self.boom:
            raise RuntimeError(self.boom)
        if self.problem:
            return {"problem": self.problem}
        self.rows.discard(name)   # an absent row is harmless
        return {"problem": None}


def target_module():
    path = os.environ.get(MODULE_ENV)
    if not path:
        return real
    spec = importlib.util.spec_from_file_location("cloud_worker_row_scratch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pods():
    out = []
    for worker, started in (("coderold", 0), ("coderidle", 0), ("coderrun", 0)):
        out.append({"metadata": {"name": "pod-" + worker, "annotations": {"cw/worker": worker, "cw/started": real.iso(T0)}}, "status": {"phase": "Running"}})
    return out


def build(mod, tmp, registry, failing=()):
    kube = FakeKube(pods())

    class L(mod.Launcher):
        def hard_deadline(self, pod):
            return T0 - MIN if pod["metadata"]["name"] == "pod-coderold" else T0 + 300 * MIN

        def activity(self, name):
            return (False, T0 - 60 * MIN) if name == "pod-coderidle" else (True, T0)

        def cost(self, pod):
            return 0.0

        def renew_git_tokens(self, pods, now):
            return []

        def send_wip_notes(self, pods, now):
            pass

        def stop(self, args, reason):
            if args.worker in failing:
                raise mod.CloudError("stop failed", 1)
            kube.pods = [p for p in kube.pods if p["metadata"]["annotations"]["cw/worker"] != args.worker]
            return {}

    launcher = L(kube, {"CW_CLOUD_AUDIT": os.path.join(tmp, "audit.jsonl"), "XDG_STATE_HOME": tmp}, lambda: T0)
    launcher.registry = registry
    return launcher


def run_pass(launcher):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        results = launcher.reap()
    return results, [l for l in err.getvalue().splitlines() if "registry:" in l]


class ReaperRow(unittest.TestCase):
    def setUp(self):
        self.mod = target_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_row_retired_once_after_the_stop_and_not_by_a_second_pass(self):
        reg = FakeRegistry(["coderold", "coderidle", "coderrun"])
        launcher = build(self.mod, self.tmp.name, reg)
        res, lines = run_pass(launcher)
        self.assertEqual(sorted((w, c) for w, _, c in res), [("coderidle", 0), ("coderold", 0)])
        for w in ("coderold", "coderidle"):
            print("S3 %s: retire after the stop = %d" % (w, reg.retires.count(w)))
            self.assertEqual(reg.retires.count(w), 1)
        self.assertEqual(reg.retires.count("coderrun"), 0)
        self.assertEqual(reg.rows, {"coderrun"})
        run_pass(launcher)
        for w in ("coderold", "coderidle"):
            print("S3 %s: retire after a second pass = %d (total)" % (w, reg.retires.count(w)))
            self.assertEqual(reg.retires.count(w), 1)
        self.assertEqual(lines, [])

    def test_row_stays_after_a_failed_stop(self):
        reg = FakeRegistry(["coderold", "coderidle", "coderrun"])
        res, _ = run_pass(build(self.mod, self.tmp.name, reg, failing=("coderold", "coderidle")))
        self.assertEqual(sorted(c for _, _, c in res), [1, 1])
        for w in ("coderold", "coderidle"):
            print("S3 %s: retire after a failed stop = %d" % (w, reg.retires.count(w)))
            self.assertEqual(reg.retires.count(w), 0)
        self.assertEqual(reg.rows, {"coderold", "coderidle", "coderrun"})

    def test_failed_row_write_gives_one_line_and_the_pod_is_still_ended(self):
        for reg in (FakeRegistry(["coderold"], problem="refused %s at %s" % (PLANTED, ADDRESS)),
                    FakeRegistry(["coderold"], boom="boom %s at %s" % (PLANTED, ADDRESS))):
            with self.subTest(kind="problem" if reg.problem else "raises"):
                launcher = build(self.mod, self.tmp.name, reg)
                res, lines = run_pass(launcher)
                print("S3 failed retire: %d stderr lines per ended pod" % (len(lines) // 2))
                self.assertEqual(sorted(w for w, _, c in res if c == 0), ["coderidle", "coderold"])
                self.assertEqual(len(lines), 2)   # one line for each of the two ended workers
                for line in lines:
                    self.assertNotIn(PLANTED, line)
                    self.assertNotIn("example.test", line)
                    self.assertIn("coder", line)
                self.assertEqual(launcher.kube.pods[0]["metadata"]["annotations"]["cw/worker"], "coderrun")

    def test_a_scratch_copy_is_a_mutant_the_tests_catch(self):
        if os.environ.get(MODULE_ENV):
            self.skipTest("inside a mutant run")
        with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
            src = f.read()
        call = "                    self.retire_row(worker, reason)"
        mutants = {
            "retire removed": (call + "   # only", "                    pass   # only"),
            "retire called twice": (call + "   # only", call + "\n" + call + "   # only"),
            "retire before the stop": None,
        }
        mutants.pop("retire before the stop")
        for label, (old, new) in mutants.items():
            self.assertEqual(src.count(old), 1, label)
            path = os.path.join(self.tmp.name, "mutant_%s.py" % label.replace(" ", "_"))
            with open(path, "w") as f:
                f.write(src.replace(old, new))
            env = dict(os.environ, **{MODULE_ENV: path})
            p = subprocess.run([sys.executable, "-m", "unittest", "test_reaper_row.ReaperRow.test_row_retired_once_after_the_stop_and_not_by_a_second_pass"],
                               cwd=HERE, env=env, capture_output=True, text=True)
            print("S3 mutant %s: exit status %d" % (label, p.returncode))
            self.assertEqual(p.returncode, 1, label)


if __name__ == "__main__":
    unittest.main()
