"""#1536: the reaper's pass removes a per-run Secret whose pod is absent, after a stated age, with one audit line.

Offline: a fake clock and a fake cluster of this file (it records every list and delete); the real Launcher.reap step and the real Kube list request
(through a recording runner). The mutants are scratch copies of the real source, each mutation asserted applied, run in a child process.
"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, LAUNCHER)
import cloud_worker as real  # noqa: E402

T0 = dt.datetime(2026, 10, 30, 12, 0, 0, tzinfo=dt.timezone.utc)
MODULE_ENV = "CW_TEST_ORPHAN_MODULE"   # a scratch copy of the code, for the mutant run
MARKER = "PLANTED-MARKER-9f3a7c"


def load_module(path):
    spec = importlib.util.spec_from_file_location("cloud_worker_scratch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def target_module():
    path = os.environ.get(MODULE_ENV)
    return load_module(path) if path else real


def secret(name, pod=None, app=None, age=7200):
    return {"name": name, "pod": pod, "app": app, "age": age, "data": {"k": MARKER}}


def run_secret(pod, age=7200):
    return secret(pod + "-run", pod=pod, age=age)


def pw_secret(pod, age=7200):
    return secret(pod + "-pw", app="cw-worker", age=age)


def pod(name):
    return {"metadata": {"name": name, "annotations": {}}, "status": {"phase": "Running"}}


class FakeKube:
    """A fake cluster: lists the pods and the Secrets (names, labels, ages; the data stays inside), records every request."""

    def __init__(self, pods, secrets, clock, refuse_secrets=False, refuse_pods=False, mod=real):
        self.pods, self.secrets, self.clock, self.mod = pods, secrets, clock, mod
        self.refuse_secrets, self.refuse_pods = refuse_secrets, refuse_pods
        self.lists, self.deleted = [], []

    def list_pods(self):
        if self.refuse_pods:
            raise self.mod.CloudError("kubectl get: forbidden")
        return list(self.pods)

    def list_secrets(self, selector):
        self.lists.append(selector)
        if self.refuse_secrets:
            raise self.mod.CloudError("kubectl get: forbidden")
        key = "pod" if selector == "cw/pod" else "app"
        return [(s["name"], s["pod"] or "", s["app"] or "", self.mod.iso(self.clock() - dt.timedelta(seconds=s["age"])))
                for s in self.secrets if s[key]]

    def delete_run_secret(self, name):
        self.deleted.append(name + "-run")

    def delete_secret(self, name):
        self.deleted.append(name + "-pw")

    def scrub(self, text, limit=300):
        return str(text)[:limit]

    def add_secrets(self, *a):
        pass

    def close(self):
        pass


def pass_of(mod, pods, secrets, **kw):
    """One real reap pass; returns (fake cluster, audit lines, stdout, stderr, raised)."""
    tmp = tempfile.mkdtemp()
    now = [T0]
    clock = lambda: now[0]  # noqa: E731
    kube = FakeKube(pods, secrets, clock, mod=mod, **kw)
    env = {"CW_CLOUD_AUDIT": os.path.join(tmp, "audit.jsonl"), "XDG_STATE_HOME": tmp}

    class L(mod.Launcher):
        def hard_deadline(self, p):
            return T0 + dt.timedelta(hours=99)

        def activity(self, name):
            return (True, T0)

        def cost(self, p):
            return 0.0

    out, err, raised = io.StringIO(), io.StringIO(), None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            L(kube, env, clock, lambda s: None).reap()
        except mod.CloudError as e:
            raised = e
    try:
        with open(env["CW_CLOUD_AUDIT"]) as f:
            lines = [json.loads(x) for x in f if x.strip()]
    except OSError:
        lines = []
    return kube, [l for l in lines if l.get("action") == mod.ORPHAN_SECRET_ACTION], out.getvalue(), err.getvalue(), raised


class OrphanSecrets(unittest.TestCase):
    def setUp(self):
        self.mod = target_module()

    def test_orphan_removed_by_exact_name(self):
        secrets = [run_secret("aa-0001"), run_secret("bb-0002"), pw_secret("cc-0003"), run_secret("dd-0004"), run_secret("ee-0005", age=60)]
        kube, audit, out, err, raised = pass_of(self.mod, [pod("dd-0004")], secrets)
        self.assertIsNone(raised)
        self.assertEqual(sorted(kube.deleted), ["aa-0001-run", "bb-0002-run", "cc-0003-pw"])
        self.assertEqual(len(audit), 3)
        for line in audit:
            self.assertIn("age 7200 seconds", line["reason"])
            self.assertIn(self.mod.ORPHAN_SECRET_REASON, line["reason"])
            self.assertEqual(line["runtime_id"], "reaper")
        self.assertEqual(sorted(l["reason"].split()[0] for l in audit), ["aa-0001-run", "bb-0002-run", "cc-0003-pw"])
        print("S2: deletions %d, audit lines %d, other deletions %d" % (len(kube.deleted), len(audit), len(set(kube.deleted) - {"aa-0001-run", "bb-0002-run", "cc-0003-pw"})))

    def test_age_boundary(self):
        counts = []
        for age in (3599, 3601):
            kube, audit, *_ = pass_of(self.mod, [], [run_secret("aa-0001", age=age)])
            counts.append(len(kube.deleted))
        self.assertEqual(counts, [0, 1])
        self.assertEqual(self.mod.ORPHAN_SECRET_AFTER_SECONDS, 3600)
        print("S3: at 3599 deletions %d, at 3601 deletions %d, constant %d" % (counts[0], counts[1], self.mod.ORPHAN_SECRET_AFTER_SECONDS))

    def test_secret_of_a_listed_pod_kept(self):
        kube, audit, *_ = pass_of(self.mod, [pod("aa-0001"), pod("bb-0002")], [run_secret("aa-0001"), pw_secret("bb-0002")])
        self.assertEqual((kube.deleted, audit), ([], []))
        print("S4: deletions %d, audit lines %d" % (len(kube.deleted), len(audit)))

    def test_kept_without_the_launchers_label_or_with_a_persistent_pod(self):
        secrets = [run_secret("orch-0001"), secret("cloud-hub-registry"), secret("cloud-hub-pw", app="cloud-hub"), secret("stray-0009-pw")]
        kube, audit, *_ = pass_of(self.mod, [pod("orch-0001-0")], secrets)
        self.assertEqual((kube.deleted, audit), ([], []))
        print("S5: deletions %d of %d" % (len(kube.deleted), len(secrets)))

    def test_unreadable_list_removes_nothing(self):
        secrets = [run_secret("aa-0001"), pw_secret("bb-0002")]
        k1, a1, o1, e1, r1 = pass_of(self.mod, [], secrets, refuse_secrets=True)
        k2, a2, o2, e2, r2 = pass_of(self.mod, [], secrets, refuse_pods=True)
        self.assertIsNone(r1)
        self.assertIsNotNone(r2)   # the pass's own pod list failing already fails the pass: kept
        self.assertEqual(k1.deleted + k2.deleted, [])
        self.assertEqual(k2.lists, [])   # no Secret list is made when the pod list is unreadable
        lines1 = [l for l in e1.splitlines() if "orphan secrets" in l and "Secret list" in l]
        self.assertEqual(len(lines1), 1)
        reported = len(lines1) + (1 if r2 else 0)
        print("S6: passes 2, deletions %d, reported lines %d (no workload list on this branch)" % (len(k1.deleted) + len(k2.deleted), reported))

    def test_list_request_names_fields_and_no_value_is_seen(self):
        calls = []

        class Rec(self.mod.Kube):
            def run(self, args, **kw):
                calls.append(list(args))
                return "aa-0001-run\taa-0001\t\t2026-10-30T09:00:00Z\n"

        k = Rec({"CW_KUBE_CONTEXT": "x"})
        rows = k.list_secrets("cw/pod")
        k.list_secrets("app=cw-worker")
        self.assertEqual(rows, [("aa-0001-run", "aa-0001", "", "2026-10-30T09:00:00Z")])
        self.assertEqual([c[:4] for c in calls], [["get", "secrets", "-l", "cw/pod"], ["get", "secrets", "-l", "app=cw-worker"]])
        whole = [c for c in calls if c[c.index("-o") + 1] in ("json", "yaml") or "-o" not in c]
        self.assertEqual(whole, [])
        self.assertTrue(all(c[5].startswith("jsonpath=") and ".data" not in c[5] for c in calls))
        kube, audit, out, err, raised = pass_of(self.mod, [], [run_secret("aa-0001"), pw_secret("bb-0002")])
        seen = json.dumps([calls, kube.lists, kube.deleted, audit, out, err])
        self.assertEqual(seen.count(MARKER), 0)
        print("S7: marker occurrences %d, whole-object list requests %d" % (seen.count(MARKER), len(whole)))


    def test_template_has_escapes_not_control_characters(self):
        calls = []

        class Rec(self.mod.Kube):
            def run(self, args, **kw):
                calls.append(list(args))
                return ""

        Rec({"CW_KUBE_CONTEXT": "x"}).list_secrets("cw/pod")
        arg = calls[0][calls[0].index("-o") + 1]
        self.assertEqual((arg.count("\n"), arg.count("\t")), (0, 0))
        self.assertIn('{"\\t"}', arg)
        self.assertIn('{"\\n"}', arg)
        print("S7: template control characters 0, escapes 2 kinds")


class Mutants(unittest.TestCase):
    EDITS = {
        "age": ("            if age <= ORPHAN_SECRET_AFTER_SECONDS:\n", "            if False:\n", "OrphanSecrets.test_age_boundary"),
        "template": ("        tmpl = (r'{range", "        tmpl = ('{range", "OrphanSecrets.test_template_has_escapes_not_control_characters"),
        "pod": ('            if pod in names or pod + "-0" in names:\n', "            if False:\n", "OrphanSecrets.test_secret_of_a_listed_pod_kept"),
    }

    def test_mutants_fail_their_scenario(self):
        with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
            src = f.read()
        failed = applied = 0
        for key, (old, new, test) in self.EDITS.items():
            self.assertEqual(src.count(old), 1, key)
            applied += 1
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "cloud_worker_mutant.py")
                with open(path, "w") as f:
                    f.write(src.replace(old, new))
                p = subprocess.run([sys.executable, os.path.join(HERE, "test_orphan_secrets.py"), test],
                                   env=dict(os.environ, **{MODULE_ENV: path}), capture_output=True, text=True)
            self.assertEqual(p.returncode, 1, key + p.stderr[-300:])
            failed += 1
        print("%d of 3 mutants fail, %d of 3 mutations applied" % (failed, applied))


class Documentation(unittest.TestCase):
    PHRASES = {
        "README.md": ["per-run Secret whose pod is absent", "more than 3600 seconds old", "one audit line", "removed: a Secret whose pod is listed",
                      "persistent orchestrator's Secret", "must keep the pod",
                      "pass's own clock", "launched again a moment later", "NAME-0"],
        "../../docs/cloud-worker-interface.md": ["per-run Secret whose pod is absent", "more than 3600 seconds old", "one audit line", "removed: a Secret whose pod is listed",
                                                 "persistent orchestrator's Secret", "must keep the pod",
                                                 "pass's own clock", "launched again a moment later", "NAME-0"],
    }

    def test_four_sentences_found(self):
        found = 0
        for rel, phrases in self.PHRASES.items():
            with open(os.path.join(LAUNCHER, rel)) as f:
                text = " ".join(f.read().split())
            self.assertEqual([p for p in phrases if p not in text], [], rel)
        found = 4
        print("S9: %d of 4 sentences found" % found)

    def test_no_change_under_k8s(self):
        p = subprocess.run(["git", "diff", "origin/cw/reaper-row", "--stat", "--", "cloud/k8s"], cwd=ROOT, capture_output=True, text=True)
        if p.returncode != 0:
            self.skipTest("no git history here")
        self.assertEqual(p.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
