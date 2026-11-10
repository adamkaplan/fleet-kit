"""#1771 S3-S7, U1: the --no-commit mark (annotation cw/no-commit) and the minute-40 wip note.

Offline: a fake cluster, the real Launcher.send_wip_notes on a scratch audit file. The mutant (a scratch copy of the source, the edit asserted
applied) is caught by RUNNING the test; the control runs first on an unedited copy.
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
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as real  # noqa: E402
import test_cloud_stack  # noqa: E402
from test_cloud_stack import CASES, build  # noqa: E402

T0 = dt.datetime(2026, 11, 1, 9, 0, 0, tzinfo=dt.timezone.utc)
MODULE_ENV = "CW_TEST_NO_COMMIT_MODULE"
ANCHOR = "a marked pod was sent the wip note"
CONTINUE_LINE = "                    continue   # launched not to commit: no note, no annotation, no audit line"


def load_module(path):
    spec = importlib.util.spec_from_file_location("cloud_worker_nocommit_scratch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def target_module():
    path = os.environ.get(MODULE_ENV)
    return load_module(path) if path else real


class FakeKube:
    def __init__(self):
        self.marks = {}

    def annotate(self, name, uid, **kv):
        self.marks.setdefault(name, {}).update({"cw/" + k.replace("_", "-"): v for k, v in kv.items()})

    def scrub(self, text, limit=300):
        return str(text)[:limit]


def pod(name, extra=None, age=40):
    ann = {"cw/worker": "w" + name, "cw/started": real.iso(T0 - dt.timedelta(minutes=age))}
    ann.update(extra or {})
    return {"metadata": {"name": name, "uid": "u-" + name, "annotations": ann}, "status": {"phase": "Running"}}


class Scene:
    def __init__(self, tmp):
        self.kube, self.sent, self.audit_file = FakeKube(), [], os.path.join(tmp, "audit.jsonl")
        scene, mod = self, target_module()

        class L(mod.Launcher):
            def send_note(self, name, text):
                scene.sent.append((name, text))

            def _cost_or_zero(self, p):
                return 0.0
        self.launcher = L(self.kube, {"CW_CLOUD_AUDIT": self.audit_file, "XDG_STATE_HOME": tmp}, lambda: T0, lambda s: None)

    def run_pass(self, pods):
        for p in pods:   # what the cluster would hold after the last pass
            p["metadata"]["annotations"].update(self.kube.marks.get(p["metadata"]["name"], {}))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.launcher.send_wip_notes(pods, T0)
        return err.getvalue()

    def audit(self):
        if not os.path.exists(self.audit_file):
            return []
        with open(self.audit_file) as f:
            return [json.loads(x) for x in f if x.strip()]


class S3Annotation(unittest.TestCase):
    def test_set_with_the_flag_absent_without(self):
        on = build(test_cloud_stack._args(no_commit=True))["metadata"]["annotations"]
        off = build(test_cloud_stack._args())["metadata"]["annotations"]
        self.assertEqual(on["cw/no-commit"], "true")
        self.assertNotIn("cw/no-commit", off)

    def test_default_pod_stays_byte_identical_to_the_fixture(self):   # U1
        with open(os.path.join(HERE, "fixtures", "default_pod.json")) as f:
            stored = f.read()
        built = {k: build(a, n) for k, (a, n) in CASES.items()}
        self.assertEqual(json.dumps(built, indent=2, sort_keys=True) + "\n", stored)


class NoteTests(unittest.TestCase):
    def test_s4_marked_pod_gets_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene(tmp)
            p = pod("m1", {"cw/no-commit": "true"})
            s.run_pass([p])
            s.run_pass([p])
            self.assertEqual([t for _, t in s.sent if t == real.wip_note("wm1")], [], ANCHOR)
            self.assertNotIn("cw/wip-note", s.kube.marks.get("m1", {}))
            self.assertEqual([a for a in s.audit() if a["action"] == "wip-note"], [])

    def test_s5_unmarked_pod_gets_the_note_as_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene(tmp)
            p = pod("u1")
            s.run_pass([p])
            s.run_pass([p])
            self.assertEqual(s.sent, [("u1", real.wip_note("wu1"))])
            self.assertEqual(s.kube.marks["u1"]["cw/wip-note"], "sent")
            self.assertEqual([a["action"] for a in s.audit()], ["wip-note"])

    def test_s6_unreadable_values_are_unmarked_with_one_line(self):
        for value in ("yes", "", 7):
            with tempfile.TemporaryDirectory() as tmp:
                s = Scene(tmp)
                p = pod("x1", {"cw/no-commit": value})
                errs = [s.run_pass([p]) for _ in range(3)]
                self.assertEqual(s.sent, [("x1", real.wip_note("wx1"))], repr(value))
                lines = [a for a in s.audit() if "no-commit-unreadable" in a["action"]]
                self.assertEqual(len(lines), 1, repr(value))
                self.assertEqual(errs, ["", "", ""], repr(value))


class Mutant(unittest.TestCase):
    def run_variant(self, edit):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
                src = f.read()
            self.assertEqual(src.count(CONTINUE_LINE), 1)
            path = os.path.join(tmp, "cloud_worker_nc.py")
            with open(path, "w") as f:
                f.write(src.replace(CONTINUE_LINE, edit))
            env = dict(os.environ, **{MODULE_ENV: path})
            return subprocess.run([sys.executable, "-m", "unittest", "test_no_commit_mark.NoteTests.test_s4_marked_pod_gets_nothing"],
                                  cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=90)

    def test_control_then_mutant(self):
        control = self.run_variant(CONTINUE_LINE)   # unedited copy: passes
        self.assertEqual(control.returncode, 0, control.stdout)
        mutant = self.run_variant("                    pass")   # the marked pod is sent the note
        print("mutant: exit %s" % mutant.returncode)
        self.assertEqual(mutant.returncode, 1, mutant.stdout)
        self.assertIn(ANCHOR, mutant.stdout)


if __name__ == "__main__":
    unittest.main()
