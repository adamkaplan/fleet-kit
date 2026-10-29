"""#1510: the reaper retires the registry row of a worker it ends, once (a stop that fails leaves the row).

Offline: a fake cluster with 3 pods (one past its hard limit, one idle, one running), the real Launcher.reap and the registry's own functions on a
scratch state directory. The mutants copy the source tree to a scratch directory, assert the change was applied, and run the behaviour tests there:
a mutant is caught when that run exits 1.
"""
import contextlib
import datetime as dt
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE.parent
ROOT = LAUNCHER.parents[1]
sys.path.insert(0, str(LAUNCHER))
sys.path.append(str(ROOT / "bin"))
import cloud_worker as real  # noqa: E402
import registry  # noqa: E402

IN_MUTANT = os.environ.get("CW1510_REAPER_MUTANT") == "1"
T0 = dt.datetime(2026, 10, 29, 21, 0, 0, tzinfo=dt.timezone.utc)
HARD, IDLE, BUSY = "coderhard", "coderidle", "coderbusy"
SECRET = "ghp_" + "B" * 36
STATE_NAME = "fl" + "eet-switchboard"


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


def pod(worker):
    return {"metadata": {"name": "w-" + worker, "annotations": {"cw/worker": worker, "cw/started": real.iso(T0)}}, "status": {"phase": "Running"}}


class Pass(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, STATE_NAME)
        os.makedirs(self.state, mode=0o700)
        for name in (HARD, IDLE, BUSY):
            got = registry.upsert(self.state, name, {"kind": "cloud", "session": "ses_" + name, "role": "coder", "state": "working"})
            self.assertFalse(got["problem"], got)
        self.fails = set()
        self.kube = FakeKube([pod(HARD), pod(IDLE), pod(BUSY)])
        tmp, fails, kube = self.tmp.name, self.fails, self.kube

        class L(real.Launcher):
            def hard_deadline(self, p):
                return T0 if p["metadata"]["annotations"]["cw/worker"] == HARD else T0 + dt.timedelta(hours=5)

            def activity(self, name):
                return (False, T0 - dt.timedelta(minutes=60)) if name == "w-" + IDLE else (True, T0)

            def cost(self, p):
                return 0.0

            def renew_git_tokens(self, pods, now):
                return []

            def send_wip_notes(self, pods, now):
                pass

            def stop(self, a, reason=None):
                if a.worker in fails:
                    raise real.CloudError("the stop failed")
                kube.pods = [p for p in kube.pods if p["metadata"]["annotations"]["cw/worker"] != a.worker]
                return {}

        self.launcher = L(self.kube, {"CW_CLOUD_AUDIT": os.path.join(tmp, "audit.jsonl"), "XDG_STATE_HOME": tmp}, lambda: T0 + dt.timedelta(minutes=1), lambda s: None)
        patch = mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp})
        patch.start()
        self.addCleanup(patch.stop)
        self.calls = []
        real_retire = registry.retire

        def counting(state, name, by, reason, *a, **k):
            self.calls.append(name)
            return real_retire(state, name, by, reason, *a, **k)
        p2 = mock.patch.object(registry, "retire", counting)
        p2.start()
        self.addCleanup(p2.stop)

    def run_pass(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = self.launcher.reap()
        return out, err.getvalue()

    def rows(self):
        return sorted(registry.read(self.state)["agents"])

    def test_matches_one_retire_after_the_stop_none_after_a_second_pass_and_none_after_a_failed_stop(self):
        self.fails.add(IDLE)   # the idle pod's stop fails; the hard-limit pod's stop works
        out, err = self.run_pass()
        self.assertIn((HARD, "hard limit", 0), out)
        self.assertEqual([r[:2] for r in out if r[0] == IDLE and r[2] != 0], [(IDLE, "idle")])
        after_first = self.calls.count(HARD)
        failed = self.calls.count(IDLE)
        self.run_pass()
        after_second = self.calls.count(HARD)
        print("retire counts: after the stop %d, after a second pass %d, after a failed stop %d" % (after_first, after_second - after_first, failed))
        self.assertEqual((after_first, after_second - after_first, failed), (1, 0, 0))
        self.assertEqual(self.calls, [HARD])
        self.assertEqual(self.rows(), sorted([IDLE, BUSY]))   # the ended worker's row is retired; the failed stop and the running pod keep theirs
        self.assertIn(HARD, registry.read(self.state)["retired"])

    def test_matches_an_ended_idle_worker_is_retired_too_and_a_running_one_is_not(self):
        self.run_pass()
        self.assertEqual(sorted(self.calls), sorted([HARD, IDLE]))
        self.assertEqual(self.rows(), [BUSY])

    def test_matches_a_registry_write_that_fails_gives_one_line_and_the_pod_is_still_ended(self):
        def boom(*a, **k):
            raise RuntimeError("boom https://%s.example.test:4096 %s" % ("worker-aaa", SECRET))
        with mock.patch.object(registry, "retire", boom):
            out, err = self.run_pass()
        self.assertIn((HARD, "hard limit", 0), out)
        lines = [l for l in err.splitlines() if l.startswith("registry: ")]
        self.assertEqual(len(lines), 2, err)   # one line for each ended pod
        for line in lines:
            for bad in (SECRET, "worker-aaa", "example.test", "://"):
                self.assertNotIn(bad, line)
        self.assertEqual([l for l in lines if l.startswith("registry: %s: " % HARD)].__len__(), 1)
        self.assertEqual([p["metadata"]["name"] for p in self.kube.pods], ["w-" + BUSY])

    def test_matches_a_row_that_is_absent_is_harmless(self):
        registry.retire(self.state, HARD, "t", "gone")
        self.calls.clear()
        out, err = self.run_pass()
        self.assertIn((HARD, "hard limit", 0), out)
        self.assertEqual(err, "")


@unittest.skipIf(IN_MUTANT, "a mutant run does not run the mutants")
class Mutants(unittest.TestCase):
    OLD = "                    self.retire_registry_row(worker, reason)   # the stop succeeded: the row is retired, once"
    CASES = (("the retire removed (the row stays behind)", OLD, "                    pass"),
             ("the retire called twice", OLD, OLD + "\n" + OLD))

    def run_copy(self, old, new):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            copy = Path(tmp) / "tree"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", Path(tmp).name))
            target = copy / "cloud/launcher/cloud_worker.py"
            text = target.read_text()
            if old:
                self.assertEqual(text.count(old), 1, "the mutation does not apply once")
                target.write_text(text.replace(old, new))
                self.assertNotEqual(target.read_text(), text, "the mutation was not applied")
            done = subprocess.run([sys.executable, "-m", "unittest", "test_reaper_registry_retire.Pass"], cwd=str(copy / "cloud/launcher/tests"),
                                  env=dict(os.environ, CW1510_REAPER_MUTANT="1"), capture_output=True, text=True, timeout=900)
            return done.returncode

    def test_matches_the_unchanged_copy_passes(self):
        self.assertEqual(self.run_copy(None, None), 0)

    def test_matches_each_mutant_exits_1(self):
        for label, old, new in self.CASES:
            with self.subTest(label):
                code = self.run_copy(old, new)
                print("mutant %s: exit %s" % (label, code))
                self.assertEqual(code, 1, label)


class Docs(unittest.TestCase):
    def test_matches_both_documents_say_the_reaper_retires_the_row_once(self):
        for rel in ("docs/cloud-worker-interface.md", "cloud/launcher/README.md"):
            text = " ".join((ROOT / rel).read_text().split())
            self.assertIn("reaper retires the row of a worker it ends, once; cloud-clear remains for a pod that disappeared without the reaper", text, rel)


if __name__ == "__main__":
    unittest.main()
