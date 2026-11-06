"""#2269 slice A: the reaper labels the pull requests a pod recorded, with the labels its launch named (cw/pr-labels). Offline: a fake kube, a fake
GitHub (the add_labels hook), the real Launcher.reap. Scenarios S3 (once), S4 (a foreign number and a missing label), S5 (no option, and the mutant)."""
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
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as real  # noqa: E402

T0 = dt.datetime(2026, 11, 6, 9, 0, 0, tzinfo=dt.timezone.utc)
MODULE_ENV = "CW_TEST_PR_LABEL_MODULE"
HOLD = "needs:captain"
ANCHOR = "a pod pull request was not labelled"


def target_module():
    path = os.environ.get(MODULE_ENV)
    if not path:
        return real
    spec = importlib.util.spec_from_file_location("cloud_worker_prlabel_scratch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeKube:
    def __init__(self, pods, records):
        self.pods, self.records, self.execs = pods, records, []

    def list_pods(self):
        return list(self.pods)

    def annotate(self, name, uid, **kv):
        for p in self.pods:
            if p["metadata"]["name"] == name:
                p["metadata"]["annotations"].update({"cw/%s" % k.replace("_", "-"): str(v) for k, v in kv.items()})

    def exec(self, name, *args):
        self.execs.append(name)
        return self.records.get(name, "")

    def scrub(self, text, limit=300):
        return str(text)[:limit]

    def add_secrets(self, *a, **k):
        pass

    def close(self):
        pass


def pod(name="w1", worker="coder", labels=HOLD):
    ann = {"cw/worker": worker, "cw/started": real.iso(T0 - dt.timedelta(minutes=5)), "cw/hard-deadline": real.iso(T0 + dt.timedelta(hours=2))}
    if labels:
        ann["cw/pr-labels"] = labels
    return {"metadata": {"name": name, "uid": "u-" + name, "annotations": ann},
            "spec": {"containers": [{"env": [{"name": "CW_REPO", "value": "acme/widget"}]}]}, "status": {"phase": "Running"}}


class Scene:
    def __init__(self, pods, records, tmp, github=None):
        self.mod, self.calls = target_module(), []
        self.kube = FakeKube(pods, records)
        self.audit_path = os.path.join(tmp, "audit.jsonl")
        self.env = {"CW_CLOUD_AUDIT": self.audit_path, "XDG_STATE_HOME": tmp}
        self.github = github or (lambda token, repo, n, labels: None)
        mod = self.mod

        class L(mod.Launcher):
            def hard_deadline(self, p):
                return T0 + dt.timedelta(hours=9)

            def activity(self, name):
                return (True, T0)

            def cost(self, p):
                return 0.0

            def send_wip_notes(self, live, now): pass
            def send_final_notes(self, live, now): pass
            def send_idle_nudges(self, live, now, acts): pass
            def renew_git_tokens(self, pods, now): return []

        self.launcher = L(self.kube, self.env, lambda: T0, lambda s: None)
        self.launcher.add_labels = self.call
        self.heads = {}
        self.launcher.pr_head = lambda token, repo, n: self.heads.get(n, ("coder/b", "open"))

    def call(self, token, repo, n, labels):
        self.calls.append((repo, n, list(labels)))
        self.github(token, repo, n, labels)

    def reap(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(self.mod, "run_git_token_cmd", return_value="tok-synthetic"), \
                mock.patch.object(self.mod, "git_token_cmd", return_value=["x"]), mock.patch.object(self.mod, "check_repo_allowed", return_value=None):
            return self.launcher.reap()

    def lines(self):
        try:
            with open(self.audit_path) as f:
                return [json.loads(x) for x in f if x.strip() and json.loads(x).get("action") == "pr-label"]
        except FileNotFoundError:
            return []


class PrLabel(unittest.TestCase):
    def test_s3_a_recorded_pr_is_labelled_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene([pod()], {"w1": "41\n"}, tmp)
            s.reap()
            s.reap()
            self.assertEqual(s.calls, [("acme/widget", 41, [HOLD])], ANCHOR)
            self.assertEqual(len(s.lines()), 1)
            s.kube.records["w1"] = "41\n42\n"   # a later number is labelled, the first is not touched again
            s.reap()
            self.assertEqual(s.calls[1:], [("acme/widget", 42, [HOLD])], ANCHOR)

    def test_s4_a_foreign_pr_is_untouched_and_a_missing_label_fails_once(self):
        def missing(token, repo, n, labels):
            raise RuntimeError("HTTP 422 label does not exist")
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene([pod()], {"w1": "41\n"}, tmp)
            s.reap()
            self.assertEqual({c[1] for c in s.calls}, {41}, "only the pod's recorded number is touched")
            self.assertNotIn(99, {c[1] for c in s.calls})
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene([pod()], {"w1": "41\n"}, tmp, github=missing)
            first = [r for r in s.reap() if r and "not labelled" in r[1]]
            second = [r for r in s.reap() if r and "not labelled" in r[1]]
            self.assertEqual((len(first), len(second)), (1, 0), "one failure line, said once")
            self.assertEqual(len(s.calls), 1, "no retry and no label creation")
            self.assertEqual([x["ok"] for x in s.lines()], [False])

    def test_bound_twenty_per_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene([pod()], {"w1": "".join("%d\n" % n for n in range(1, 26))}, tmp)
            out = s.reap()
            self.assertEqual(len(s.calls), 20)
            self.assertEqual(len([r for r in out if r and "per pass" in r[1]]), 1)
            s.reap()
            self.assertEqual(len(s.calls), 25)

    def test_spoofed_head_is_not_labelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene([pod()], {"w1": "41\n42\n43\n"}, tmp)
            s.heads = {41: ("someone-else/x", "open"), 42: ("coder/x", "open"), 43: ("coder/y", "closed")}
            s.reap()
            self.assertEqual([c[1] for c in s.calls], [42])

    def test_failed_done_mark_is_not_looped(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene([pod()], {"w1": "41\n42\n"}, tmp)
            def boom(*a, **k):
                raise RuntimeError("annotate down")
            s.kube.annotate = boom
            s.reap()
            self.assertEqual(len(s.calls), 1)

    def test_s5_no_option_no_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Scene([pod(labels="")], {"w1": "41\n"}, tmp)
            s.reap()
            self.assertEqual((s.calls, s.kube.execs), ([], []))

    def test_the_launch_option_reaches_the_pod(self):
        self.assertEqual(real.clean_pr_labels([HOLD, HOLD, "a"]), [HOLD, "a"])
        for bad in ("a,b", "", "x\ny"):
            with self.assertRaises(real.CloudError):
                real.clean_pr_labels([bad])


def mutant_copy(directory):
    with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
        src = f.read()
    old = "            out.extend(self.label_pod_prs(live, now))"
    assert src.count(old) == 1
    path = os.path.join(directory, "cloud_worker_prlabel_mutant.py")
    with open(path, "w") as f:
        f.write(src.replace(old, "            pass"))
    return path


class Mutant(unittest.TestCase):
    def run_test(self, path):
        return subprocess.run([sys.executable, "-m", "unittest", "test_pr_label_reaper.PrLabel.test_s3_a_recorded_pr_is_labelled_once"], cwd=HERE,
                              env=dict(os.environ, **{MODULE_ENV: path}), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=90)

    def test_mutant_without_the_step_fails_with_the_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = os.path.join(tmp, "control.py")
            with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
                src = f.read()
            with open(ctl, "w") as f:
                f.write(src)
            c = self.run_test(ctl)   # control first
            self.assertEqual(c.returncode, 0, c.stdout)
            p = self.run_test(mutant_copy(tmp))
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn(ANCHOR, p.stdout)


if __name__ == "__main__":
    unittest.main()
