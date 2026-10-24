"""#1068: the cloud launcher starts an orchestrator: one role table, one shape row, a persistent StatefulSet, a token role table.

Offline: a fake cluster (an in-memory object that behaves like the cluster and its StatefulSet controller), the real mint against a loopback
stand-in for GitHub, the real render and reap code. Mutants are built from the real source with one text replaced; each must fail the check
that shows its clause. No token is a literal: every value is assembled at run time."""
import argparse
import base64
import copy
import datetime as dt
import hashlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import app_token  # noqa: E402
import cloud_worker as cw  # noqa: E402
import test_app_token as tam  # noqa: E402

T0 = dt.datetime(2026, 10, 14, 12, 0, 0, tzinfo=dt.timezone.utc)
REPO = "acme-org/widget-service"
WORKER = "orch1"
RUN = "0123456789abcdef"
ORCH_SET = {"issues": "write", "pull_requests": "read", "checks": "read", "metadata": "read"}
FORBIDDEN_IN_POD = ("APP_KEY", "APP_ID", "KEY_CMD", "TOKEN_ENDPOINT", "azure.workload.identity")


def tok(label):
    return "t" + hashlib.sha256(label.encode()).hexdigest()[:36]


def load_module(name, old=None, new=None):
    """The real source of cloud_worker.py, or with one text replaced (a mutant)."""
    path = os.path.join(LAUNCHER, name)
    src = open(path).read()
    if old is not None:
        assert src.count(old) == 1, old
        src = src.replace(old, new)
    mod = type(sys)("mutant")
    mod.__file__ = path
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class FakeKube:
    """A fake cluster: pods, StatefulSets with a controller (reconcile recreates a missing NAME-0), Secrets."""
    def __init__(self):
        self.pods, self.sets, self.secrets, self.run_secrets, self.uid = {}, {}, {}, {}, 0
        self.created = []
        self.hold = False   # True: the controller has not yet recreated a missing pod (node loss, eviction)

    def delete_command(self, kind, name):
        return "kubectl delete %s %s" % (kind, name)

    def scrub(self, text, limit=300):
        return str(text)[:limit]

    def add_secrets(self, *v):
        pass

    def reconcile(self):
        if self.hold:
            return
        for name, ss in list(self.sets.items()):
            if name + "-0" not in self.pods:
                self.uid += 1
                t = ss["spec"]["template"]
                self.pods[name + "-0"] = {"metadata": dict(copy.deepcopy(t["metadata"]), name=name + "-0", uid="uid-%d" % self.uid),
                                          "spec": copy.deepcopy(t["spec"]),
                                          "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}

    def get_pod(self, name):
        self.reconcile()
        return copy.deepcopy(self.pods.get(name))

    def get_workload(self, name):
        return copy.deepcopy(self.sets.get(name))

    def list_pods(self):
        self.reconcile()
        return copy.deepcopy(list(self.pods.values()))

    def apply(self, obj):
        self.created.append(copy.deepcopy(obj))
        name = obj["metadata"]["name"]
        if obj["kind"] == "StatefulSet":
            self.sets[name] = copy.deepcopy(obj)
        elif obj["kind"] == "Secret":
            self.secrets[name] = copy.deepcopy(obj)
        else:
            self.uid += 1
            obj = copy.deepcopy(obj)
            obj["metadata"]["uid"] = "uid-%d" % self.uid
            obj["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
            self.pods[name] = obj

    def get_secret(self, full):
        return copy.deepcopy(self.secrets.get(full) or self.run_secrets.get(full))

    def create_run_secret(self, name, pod, values, run_id=None):
        self.run_secrets[name + "-run"] = {
            "metadata": {"name": name + "-run", "uid": "su", "resourceVersion": "1", "creationTimestamp": cw.iso(T0),
                         "labels": dict({"cw/pod": pod}, **({"cw/run-id": run_id} if run_id else {})), "annotations": {}},
            "data": {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}}

    def get_run_secret(self, name):
        return copy.deepcopy(self.run_secrets.get(name + "-run"))

    def delete_pod(self, name, uid=None):
        self.pods.pop(name, None)

    def delete_secret(self, name):
        self.secrets.pop(name + "-pw", None)

    def delete_run_secret(self, name):
        self.run_secrets.pop(name + "-run", None)

    def delete_workload(self, name):
        self.sets.pop(name, None)

    def delete(self, name, uid=None):
        for fn in (self.delete_pod, self.delete_secret, self.delete_run_secret):
            fn(name)

    def annotate(self, name, uid, **kv):
        pass

    def run(self, args, stdin=None, **k):
        assert args[:2] == ["patch", "secret"], args
        patch = json.loads(stdin)
        sec = self.run_secrets[args[2]]
        sec["data"].update(patch.get("data", {}))
        sec["metadata"]["annotations"].update(patch["metadata"].get("annotations", {}))
        sec["metadata"]["resourceVersion"] = str(int(sec["metadata"]["resourceVersion"]) + 1)

    def exec(self, name, *cmd, keep_stdout=False, timeout=120):
        if cmd[0] == "cw-worker-activity":
            return json.dumps({"busy": False, "last_activity": cw.iso(T0)})   # idle since the start
        return json.dumps({"pushed": False, "wip_branch": None})

    def pod_events(self, name):
        return []

    def everything_of_run(self):
        return len(self.pods) + len(self.sets) + len(self.secrets) + len(self.run_secrets)


class Fx(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.records = os.path.join(d, "records")
        os.makedirs(self.records)
        self.minted = os.path.join(d, "minted")
        mint = os.path.join(d, "mint.sh")   # the fake mint command: it records the role it was asked for and prints a synthetic token
        with open(mint, "w") as f:
            f.write("#!/bin/sh\necho \"${CW_GIT_TOKEN_ROLE:-worker}\" >> '%s'\nprintf %%s '%s'\n" % (self.minted, tok("renewed")))
        os.chmod(mint, 0o700)
        self.env = {"CW_ALLOWED_REPOS": REPO, "CW_CLOUD_IMAGE": "registry.example/img@sha256:" + "a" * 64, "CW_CLOUD_AUDIT": os.path.join(d, "audit.jsonl"),
                    "CW_CLOUD_RECORD_DIR": self.records, "CW_GIT_TOKEN_CMD": mint, "CW_TOKEN_ENDPOINT": "https://tokens.example.internal/mint",
                    "CW_APP_KEY_CMD": "planted", "CW_APP_ID_CMD": "planted"}
        self.kube, self.clock = FakeKube(), Clock()
        self.L = cw.Launcher(self.kube, self.env, self.clock)

    def write_record(self, role, worker=WORKER):
        with open(os.path.join(self.records, worker + ".json"), "w") as f:
            json.dump({"repo": REPO, "role": role, "hard_deadline": cw.iso(T0 + dt.timedelta(hours=2))}, f)

    def args(self, **kw):
        fd = os.open(os.path.join(self.tmp.name, "gt"), os.O_RDWR | os.O_CREAT, 0o600)
        os.write(fd, tok("first").encode())
        os.lseek(fd, 0, 0)
        self.addCleanup(lambda: os.close(fd) if fd else None)
        base = dict(worker=WORKER, repo=REPO, branch="main", idle="10m", hard="2h", max_hard="8h", shape="S", image=None, stack=False, wait=5,
                    pull_secret=None, run_secret_fd=["git-token=%d" % fd], run_id=RUN, password_fd=None, role="orchestrator")
        base.update(kw)
        return argparse.Namespace(**base)

    def launch(self, **kw):
        out = self.L.create(self.args(**kw))
        self.write_record(kw.get("role", "orchestrator"))
        return out

    def stop(self, **kw):
        a = argparse.Namespace(worker=WORKER, reason="done", run_id=RUN, no_record=True, record_dir=None)
        return self.L.stop(a)

    def workload(self):
        return self.kube.sets[cw.pod_name(WORKER)]


def pod_of(spec):
    return spec["spec"]["template"]["spec"]


def has_deadline(wl):
    text = json.dumps(wl)
    return "activeDeadlineSeconds" in text or "cw/hard-deadline" in text


def holds_key(wl, secrets=()):
    text = json.dumps([wl] + list(secrets))
    return any(w in text for w in FORBIDDEN_IN_POD)


class Shape(Fx):   # DW-2 (S2, S3)
    def test_s2_the_pod_has_the_orchestrator_row_as_requests_and_limits_and_no_runtime_class(self):
        self.launch()
        wl = self.workload()
        res = pod_of(wl)["containers"][0]["resources"]
        row = cw.SHAPES["ORCH"]
        self.assertEqual(res, {"requests": {"cpu": "250m", "memory": "768Mi"}, "limits": {"cpu": "250m", "memory": "768Mi"}})
        self.assertEqual((row["cpu"], row["memory"]), ("250m", "768Mi"))
        self.assertNotIn("runtimeClassName", json.dumps(wl))
        self.assertEqual(wl["spec"]["template"]["metadata"]["annotations"]["cw/shape"], "ORCH")

    def test_s2_editing_the_row_changes_the_pod_and_the_docs_block(self):
        row = copy.deepcopy(cw.SHAPES["ORCH"])
        try:
            cw.SHAPES["ORCH"].update(cpu="500m", memory="1536Mi", usd_per_hour=0.05)
            self.launch()
            self.assertEqual(pod_of(self.workload())["containers"][0]["resources"]["limits"], {"cpu": "500m", "memory": "1536Mi"})
            doc = cw.render_roles_doc()
            self.assertIn("500m CPU, 1536Mi", doc)
            self.assertIn("USD %.2f per day" % (24 * 0.05), doc)
        finally:
            cw.SHAPES["ORCH"].update(row)

    def test_s3_an_operator_cannot_pick_the_orchestrator_shape_for_a_worker_nor_a_stack_for_an_orchestrator(self):
        for kw in (dict(role="worker", shape="ORCH"), dict(role="orchestrator", stack=True)):
            with self.subTest(kw=kw):
                with self.assertRaises(cw.CloudError) as c:
                    self.L.create(self.args(**kw))
                self.assertEqual(c.exception.code, cw.EXIT_USAGE)
                self.assertEqual(self.kube.created, [])
                self.assertEqual(self.kube.everything_of_run(), 0)

    def test_the_shape_choices_are_the_table_minus_role_only_rows(self):
        self.assertNotIn("ORCH", cw.SHAPE_CHOICES)
        self.assertEqual(set(cw.SHAPE_CHOICES) | {"ORCH"}, set(cw.SHAPES))


class Mode(Fx):   # DW-3 (S4, S5, S6)
    def test_s4_a_one_replica_statefulset_restart_always_and_no_deadline(self):
        self.launch()
        wl = self.workload()
        self.assertEqual((wl["kind"], wl["spec"]["replicas"], pod_of(wl)["restartPolicy"]), ("StatefulSet", 1, "Always"))
        self.assertFalse(has_deadline(wl))
        self.assertNotIn("CW_HARD_DEADLINE", json.dumps(wl))
        self.assertEqual(cw.ROLES["orchestrator"]["persistent"], True)
        self.assertEqual(self.kube.pods[cw.pod_name(WORKER) + "-0"]["metadata"]["annotations"]["cw/role"], "orchestrator")

    def test_s4_the_worker_pod_is_still_a_pod_with_a_deadline(self):
        self.launch(role="worker")
        pod = self.kube.pods[cw.pod_name(WORKER)]
        self.assertEqual((pod["kind"], pod["spec"]["restartPolicy"]), ("Pod", "Never"))
        self.assertIn("activeDeadlineSeconds", pod["spec"])
        self.assertEqual(pod["spec"]["containers"][0]["resources"], {"limits": {"cpu": "2", "memory": "4Gi"}})

    def test_s4_the_allow_list_and_the_spend_cap_still_apply(self):
        with self.assertRaises(cw.CloudError) as c:
            self.L.create(self.args(repo="acme-org/other"))
        self.assertEqual((c.exception.code, self.kube.created), (cw.EXIT_USAGE, []))
        self.L.env["CW_CLOUD_SPEND_CAP_USD"] = "0.01"   # one day of the row costs 0.48
        with self.assertRaises(cw.CloudError) as c:
            self.L.create(self.args())
        self.assertEqual((c.exception.code, self.kube.created), (cw.EXIT_CAP, []))

    def test_s5_the_reaper_leaves_a_persistent_pod_alone_at_100_hours_and_stop_removes_everything(self):
        self.launch()
        self.clock.t = T0 + dt.timedelta(hours=100)
        out = self.L.reap()
        self.assertEqual([x for x in out if x[2] == 0 and x[1] in ("hard limit", "idle")], [])
        self.assertIn(cw.pod_name(WORKER) + "-0", self.kube.pods)
        self.assertIn(cw.pod_name(WORKER), self.kube.sets)
        self.stop()
        self.assertEqual((self.kube.pods, self.kube.sets, self.kube.secrets, self.kube.run_secrets), ({}, {}, {}, {}))
        self.kube.reconcile()   # the controller runs again: nothing comes back
        self.assertEqual(self.kube.pods, {})

    def test_s5_a_worker_is_still_ended_by_its_deadline(self):
        self.launch(role="worker")
        self.clock.t = T0 + dt.timedelta(hours=3)
        self.assertEqual([x[1] for x in self.L.reap() if x[2] == 0][:1], ["hard limit"])

    def test_s5_extend_is_refused_for_a_persistent_pod(self):
        self.launch()
        with self.assertRaises(cw.CloudError):
            self.L.extend(argparse.Namespace(worker=WORKER, hard="+1h", idle=None, reason=""))

    def test_s6_a_token_31_minutes_old_with_no_deadline_is_renewed_with_the_orchestrator_set(self):
        self.launch()
        self.clock.t = T0 + dt.timedelta(minutes=31)
        out = self.L.reap()
        self.assertEqual([x for x in out if x[1].startswith("git token")], [(WORKER, "git token renewed", 0)])
        sec = self.kube.get_run_secret(cw.pod_name(WORKER))
        self.assertEqual(sec["metadata"]["annotations"][cw.GIT_TOKEN_MINTED], cw.iso(self.clock.t))
        lines = [json.loads(l) for l in open(self.env["CW_CLOUD_AUDIT"]) if '"renew-git-token"' in l]
        self.assertEqual([(l["ok"], l["action"]) for l in lines], [(True, "renew-git-token")])
        self.assertEqual(open(self.minted).read().split(), ["orchestrator"])

    def test_s8_a_flipped_annotation_does_not_change_the_recorded_role(self):
        self.launch(role="worker", idle="3h")   # the record says worker
        pod = self.kube.pods[cw.pod_name(WORKER)]
        pod["metadata"]["annotations"]["cw/role"] = "orchestrator"
        pod["status"] = {"phase": "Running"}
        self.clock.t = T0 + dt.timedelta(minutes=31)
        self.L.reap()
        self.assertEqual(open(self.minted).read().split(), ["worker"])

    def test_a_record_that_does_not_say_orchestrator_never_makes_a_pod_persistent(self):
        self.launch(role="worker")
        self.clock.t = T0 + dt.timedelta(hours=3)
        self.assertFalse(self.L._persistent(self.kube.list_pods()[0]))


class Credentials(Fx):   # DW-4 (S8, U3)
    def test_s8_the_token_is_the_per_run_secret_and_the_pod_holds_no_key_endpoint_or_identity(self):
        self.launch()
        sec = self.kube.get_run_secret(cw.pod_name(WORKER))
        self.assertEqual(list(sec["data"]), ["git-token"])
        self.assertFalse(holds_key(self.workload(), list(self.kube.secrets.values()) + [sec]))
        self.assertIn("git-token", json.dumps(pod_of(self.workload())["volumes"]))

    def test_an_orchestrator_with_no_run_secret_is_refused(self):
        with self.assertRaises(cw.CloudError):
            self.L.create(self.args(run_secret_fd=[]))
        self.assertEqual(self.kube.everything_of_run(), 0)


class Mint(unittest.TestCase):   # DW-4, DW-5 (S7, S9): the real mint against the loopback stand-in (the older suite's fixtures, not its tests)
    setUpClass = classmethod(tam.Mint.setUpClass.__func__)
    tearDownClass = classmethod(tam.Mint.tearDownClass.__func__)
    setUp = tam.Mint.setUp

    def mint_role(self, role=None):
        e = dict(os.environ, CW_APP_ID_CMD="echo 12345", CW_APP_INSTALLATION_CMD="echo 678",
                 CW_APP_KEY_CMD="cat '%s'" % self.keyfile, CW_GITHUB_API_BASE=self.base, CW_OPENSSL=self.wrap)
        argv = [sys.executable, tam.MINT, tam.REAL] + ([role] if role else [])
        return tam.subprocess.run(argv, env=e, capture_output=True, text=True, timeout=60)

    def test_s7_the_worker_request_is_the_old_one_and_the_orchestrator_asks_for_exactly_its_four(self):
        tam.Stub.answer["permissions"] = dict(tam.PERMS)
        p = self.mint_role()
        self.assertEqual((p.returncode, p.stdout), (0, tam.TOKEN), p.stderr)
        self.assertEqual(tam.Stub.seen[-1][2], {"repositories": ["widget-service"], "permissions": tam.PERMS})
        tam.Stub.answer["permissions"] = dict(ORCH_SET)
        p = self.mint_role("orchestrator")
        self.assertEqual((p.returncode, p.stdout), (0, tam.TOKEN), p.stderr)
        self.assertEqual(tam.Stub.seen[-1][2]["permissions"], ORCH_SET)
        self.assertNotIn("contents", tam.Stub.seen[-1][2]["permissions"])
        self.assertEqual(app_token.ROLE_PERMISSIONS, {"worker": tam.PERMS, "orchestrator": ORCH_SET})

    def test_s7_an_orchestrator_answer_with_contents_or_any_extra_key_is_refused(self):
        for extra in ({"contents": "write"}, {"contents": "read"}, {"administration": "write"}):
            tam.Stub.answer["permissions"] = dict(ORCH_SET, **extra)
            p = self.mint_role("orchestrator")
            self.assertEqual((p.returncode, p.stdout), (1, ""), extra)
            self.assertIn("extra permission", p.stderr)

    def test_s7_the_worker_answer_is_not_good_for_the_orchestrator_and_the_other_way(self):
        tam.Stub.answer["permissions"] = dict(tam.PERMS)
        self.assertEqual(self.mint_role("orchestrator").returncode, 1)
        tam.Stub.answer["permissions"] = dict(ORCH_SET)
        self.assertEqual(self.mint_role().returncode, 1)

    def test_s9_each_missing_member_of_each_role_is_refused_naming_it_and_the_owner_step(self):
        for role, table in (("worker", tam.PERMS), ("orchestrator", ORCH_SET)):
            for k in table:
                with self.subTest(role=role, missing=k):
                    tam.Stub.answer["permissions"] = {x: y for x, y in table.items() if x != k}
                    p = self.mint_role(role)
                    self.assertEqual((p.returncode, p.stdout), (1, ""))
                    self.assertIn("lacks the permission: " + k, p.stderr)
                    self.assertIn(app_token.owner_step(role), p.stderr)

    def test_an_unknown_role_is_refused_before_any_call(self):
        n = len(tam.Stub.seen)
        p = self.mint_role("admin")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(len(tam.Stub.seen), n)


class Mutants(Fx):   # DW-6 (S10): the same checks, run on a mutated copy of the real source, must fail
    def contents_free(self, mod):
        return "contents" not in mod.ROLE_PERMISSIONS["orchestrator"]

    def test_mutant_contents_write_in_the_orchestrator_set_fails_the_set_check(self):
        self.assertTrue(self.contents_free(app_token))
        m = load_module("app_token.py", '"orchestrator": {"issues": "write",', '"orchestrator": {"contents": "write", "issues": "write",')
        self.assertFalse(self.contents_free(m))

    def render(self, mod):
        a = argparse.Namespace(worker=WORKER, repo=REPO, branch="main", idle="10m", hard="2h", max_hard="8h", shape="S", image="img", stack=False,
                               pull_secret=None, run_id=RUN, role="orchestrator")
        env = {"CW_CLOUD_SERVICE_ACCOUNT": "cw-worker", "CW_TOKEN_ENDPOINT": "https://tokens.example.internal/mint"}
        return mod.render_workload(mod.render_pod(a, WORKER, "cw-" + WORKER, T0, None, env, ["git-token"]))

    def test_mutant_a_deadline_left_on_a_persistent_pod_fails_the_no_deadline_check(self):
        self.assertFalse(has_deadline(self.render(cw)))
        m = load_module("cloud_worker.py", 'del pod["spec"]["activeDeadlineSeconds"]', "pass")
        self.assertTrue(has_deadline(self.render(m)))

    def test_mutant_a_hard_deadline_annotation_left_fails_too(self):
        m = load_module("cloud_worker.py", 'for k in ("cw/hard-deadline", "cw/idle-seconds", "cw/max-hard"):', 'for k in ("cw/idle-seconds", "cw/max-hard"):')
        self.assertTrue(has_deadline(self.render(m)))

    def test_mutant_a_pod_holding_the_key_fails_the_key_scan(self):
        self.assertFalse(holds_key(self.render(cw)))
        m = load_module("cloud_worker.py", "endpoint = None   # never a token endpoint: the pod cannot mint", 'e["CW_APP_KEY_CMD"] = "cat k"')
        self.assertTrue(holds_key(self.render(m)))
        m = load_module("cloud_worker.py", "        del pod[\"metadata\"][\"labels\"][\"azure.workload.identity/use\"]\n", "")
        self.assertTrue(holds_key(self.render(m)))

    def test_mutant_the_reaper_applying_the_worker_rule_fails_s5(self):
        m = load_module("cloud_worker.py", "if self._persistent(pod):", "if False:")
        self.launch()
        L = m.Launcher(self.kube, self.env, self.clock)
        self.clock.t = T0 + dt.timedelta(hours=100)
        # the mutant tries to read the missing deadline: the pod is reported unreadable, never ended; a persistent pod with a deadline would be
        self.assertIn("unreadable deadline", [x[1] for x in L.reap()])

    def test_mutant_a_stop_that_deletes_only_the_pod_leaves_the_workload_and_s5_fails(self):
        m = load_module("cloud_worker.py", "        self.kube.delete_workload(name)\n        try:\n            self.kube.delete_pod(pod_name_, uid)",
                        "        try:\n            self.kube.delete_pod(pod_name_, uid)")
        self.launch()
        m.Launcher(self.kube, self.env, self.clock).stop(argparse.Namespace(worker=WORKER, reason="x", run_id=RUN, no_record=True, record_dir=None))
        self.kube.reconcile()
        self.assertNotEqual(self.kube.pods, {})   # the controller recreated it: the real stop does not do this (S5)

    def test_delete_force_removes_the_workload_then_everything_and_reconcile_brings_nothing_back(self):
        self.launch()
        out = self.L.delete(argparse.Namespace(worker=WORKER, force=True, reason="x"))
        self.assertTrue(out["deleted"])
        self.kube.reconcile()
        self.assertEqual((self.kube.pods, self.kube.sets, self.kube.secrets, self.kube.run_secrets), ({}, {}, {}, {}))

    def test_mutant_a_delete_that_ignores_the_workload_leaves_it_running(self):
        m = load_module("cloud_worker.py", "                if ann(pod, \"workload\"):   # a persistent role: the workload FIRST", "                if False:   # a persistent role: the workload FIRST")
        self.launch()
        m.Launcher(self.kube, self.env, self.clock).delete(argparse.Namespace(worker=WORKER, force=True, reason="x"))
        self.kube.reconcile()
        self.assertNotEqual((self.kube.pods, self.kube.sets), ({}, {}))   # the real delete leaves nothing


class PodGone(Fx):   # M1: the pod NAME-0 is absent but its StatefulSet (and Secrets) exist
    def gone(self):
        self.launch()
        self.kube.pods.clear()
        self.kube.hold = True
        self.assertEqual((len(self.kube.sets), len(self.kube.secrets) + len(self.kube.run_secrets) > 0), (1, True))

    def left(self, kube=None):
        k = kube or self.kube
        return (sorted(k.pods), sorted(k.sets), sorted(k.secrets), sorted(k.run_secrets))

    def test_stop_with_no_pod_removes_the_statefulset_and_the_secrets(self):
        self.gone()
        self.stop()
        self.assertEqual(self.left(), ([], [], [], []))

    def test_delete_force_with_no_pod_removes_the_statefulset_and_the_secrets(self):
        self.gone()
        out = self.L.delete(argparse.Namespace(worker=WORKER, force=True, reason="x"))
        self.assertTrue(out["deleted"])
        self.assertEqual(self.left(), ([], [], [], []))

    def test_stop_with_another_runs_nonce_leaves_the_statefulset_whole(self):
        self.gone()
        self.L.stop(argparse.Namespace(worker=WORKER, reason="x", run_id="f" * 16, no_record=True, record_dir=None))
        self.assertEqual(list(self.kube.sets), [cw.pod_name(WORKER)])

    def test_mutant_stop_ignoring_the_workload_when_the_pod_is_absent_fails(self):
        m = load_module("cloud_worker.py", "self._remove_gone(worker, name, nonce or None)", "self._remove(name, nonce)")
        self.gone()
        m.Launcher(self.kube, self.env, self.clock).stop(argparse.Namespace(worker=WORKER, reason="x", run_id=RUN, no_record=True, record_dir=None))
        self.assertNotEqual(self.left(), ([], [], [], []))

    def test_mutant_delete_ignoring_the_workload_when_the_pod_is_absent_fails(self):
        m = load_module("cloud_worker.py", "self._remove_gone(worker, name, None)", "None")
        self.gone()
        m.Launcher(self.kube, self.env, self.clock).delete(argparse.Namespace(worker=WORKER, force=True, reason="x"))
        self.assertNotEqual(self.left(), ([], [], [], []))

    def test_a_failed_workload_delete_says_so_by_name_and_is_never_deleted_true(self):
        self.gone()
        def boom(name):
            raise cw.CloudError("api down")
        self.kube.delete_workload = boom
        with self.assertRaises(cw.CloudError) as c:
            self.L.delete(argparse.Namespace(worker=WORKER, force=True, reason="x"))
        self.assertIn("statefulset/" + cw.pod_name(WORKER), str(c.exception))
        self.assertEqual(list(self.kube.sets), [cw.pod_name(WORKER)])

    def test_a_failed_create_cleans_up_the_workload_and_the_secrets(self):
        real = self.kube.apply
        def apply(obj):
            real(obj)
            if obj["kind"] == "StatefulSet":
                raise cw.CloudError("apply failed")
        self.kube.apply = apply
        with self.assertRaises(cw.CloudError):
            self.L.create(self.args())
        self.assertEqual(self.left(), ([], [], [], []))

    def test_a_failed_create_whose_workload_delete_fails_names_the_statefulset(self):
        real = self.kube.apply
        def apply(obj):
            real(obj)
            if obj["kind"] == "StatefulSet":
                raise cw.CloudError("apply failed")
        self.kube.apply = apply
        def boom(name):
            raise cw.CloudError("api down")
        self.kube.delete_workload = boom
        with self.assertRaises(cw.CloudError) as c:
            self.L.create(self.args())
        self.assertIn("statefulset/" + cw.pod_name(WORKER), str(c.exception))


class GoneMatrix(Fx):   # the reproducer matrix: callers x states, each cell's leftovers and answer
    WHOLE = ([cw.pod_name(WORKER) + "-0"], [cw.pod_name(WORKER)], [cw.pod_name(WORKER) + "-pw"], [cw.pod_name(WORKER) + "-run"])
    EMPTY = ([], [], [], [])

    def left(self):
        k = self.kube
        return (sorted(k.pods), sorted(k.sets), sorted(k.secrets), sorted(k.run_secrets))

    def gone(self):
        self.state("absent")

    def state(self, which):
        self.launch()
        if which == "absent":
            self.kube.pods.clear()
            self.kube.hold = True
        elif which == "none":
            for d in (self.kube.pods, self.kube.sets, self.kube.secrets, self.kube.run_secrets):
                d.clear()
            self.kube.hold = True

    def call(self, caller):
        """Returns (kind, answer-or-error): 'ok' with the answer, or 'refused'/'error' with the exit code."""
        try:
            if caller.startswith("stop"):
                nonce = {"stop-match": RUN, "stop-other": "f" * 16, "stop-none": None}[caller]
                return "ok", self.L.stop(argparse.Namespace(worker=WORKER, reason="x", run_id=nonce, no_record=True, record_dir=None))
            return "ok", self.L.delete(argparse.Namespace(worker=WORKER, force=caller == "delete-force", reason="x"))
        except cw.CloudError as e:
            return "error", e.code

    CELLS = {
        ("stop-match", "present"): ("ok", "EMPTY"), ("stop-other", "present"): ("error", "WHOLE"), ("stop-none", "present"): ("ok", "EMPTY"),
        ("delete-force", "present"): ("ok", "EMPTY"), ("delete", "present"): ("error", "WHOLE"),
        ("stop-match", "absent"): ("ok", "EMPTY"), ("stop-other", "absent"): ("ok", "WHOLE_NOPOD"), ("stop-none", "absent"): ("ok", "EMPTY"),
        ("delete-force", "absent"): ("ok", "EMPTY"), ("delete", "absent"): ("ok", "EMPTY"),
        ("stop-match", "none"): ("ok", "EMPTY"), ("stop-other", "none"): ("ok", "EMPTY"), ("stop-none", "none"): ("ok", "EMPTY"),
        ("delete-force", "none"): ("ok", "EMPTY"), ("delete", "none"): ("ok", "EMPTY"),
    }

    def test_matrix(self):
        bad = []
        for (caller, st), (kind, left) in sorted(self.CELLS.items()):
            self.setUp()
            self.state(st)
            dels = []
            for meth in ("delete_pod", "delete_secret", "delete_run_secret", "delete_workload", "delete"):
                real = getattr(self.kube, meth)
                setattr(self.kube, meth, lambda *a, _r=real, _m=meth, **k: (dels.append(_m), _r(*a, **k))[1])
            got_kind, ans = self.call(caller)
            if st == "none" and caller == "stop-none" and dels:   # pod AND workload AND Secrets absent: no delete call at all
                bad.append("%s x %s: delete calls %s" % (caller, st, dels))
            want = self.EMPTY[1:] if left == "EMPTY" else self.WHOLE[1:]
            got = self.left()[1:]
            if got_kind != kind or got != want:
                bad.append("%s x %s: got %s %s, want %s %s" % (caller, st, got_kind, got, kind, want))
            if kind == "ok" and caller.startswith("delete") and ans.get("deleted") is not True:
                bad.append("%s x %s: answer %s" % (caller, st, ans))
        self.assertEqual(bad, [])

    def test_stop_by_name_only_with_no_pod_removes_the_statefulset_then_the_secrets(self):
        self.gone()
        order = []
        real = self.kube.delete_workload
        self.kube.delete_workload = lambda n: (order.append("workload"), real(n))
        rs = self.kube.delete
        self.kube.delete = lambda n, uid=None: (order.append("secrets"), rs(n, uid))
        out = self.L.stop(argparse.Namespace(worker=WORKER, reason="x", run_id=None, no_record=True, record_dir=None))
        self.assertEqual((out["pushed"], self.left(), order), (False, ([], [], [], []), ["workload", "secrets"]))

    def test_stop_by_name_only_with_no_pod_and_no_statefulset_leaves_planted_secrets_and_deletes_nothing(self):
        self.launch()
        self.kube.pods.clear(); self.kube.sets.clear(); self.kube.hold = True   # a cut-off create: only the Secrets remain
        before = self.left()
        dels = []
        for meth in ("delete_pod", "delete_secret", "delete_run_secret", "delete_workload", "delete"):
            real = getattr(self.kube, meth)
            setattr(self.kube, meth, lambda *a, _r=real, _m=meth, **k: (dels.append(_m), _r(*a, **k))[1])
        out = self.L.stop(argparse.Namespace(worker=WORKER, reason="x", run_id=None, no_record=True, record_dir=None))
        self.assertEqual((out["pushed"], dels, self.left()), (False, [], before))
        self.assertTrue(before[2] and before[3])

    def test_mutant_no_nonce_skip_fails(self):
        m = load_module("cloud_worker.py", "self._remove_gone(worker, name, nonce or None)", "(self._remove_gone(worker, name, nonce) if nonce else None)")
        self.gone()
        m.Launcher(self.kube, self.env, self.clock).stop(argparse.Namespace(worker=WORKER, reason="x", run_id=None, no_record=True, record_dir=None))
        self.assertNotEqual(self.left(), ([], [], [], []))

    def test_a_lookup_error_on_delete_is_never_answered_deleted(self):
        self.gone()
        def boom(name):
            raise cw.CloudError("internal error")
        self.kube.get_workload = boom
        with self.assertRaises(cw.CloudError) as c:
            self.L.delete(argparse.Namespace(worker=WORKER, force=True, reason="x"))
        self.assertIn("statefulset/" + cw.pod_name(WORKER), str(c.exception))
        self.assertNotEqual(c.exception.code, 0)
        self.assertEqual(list(self.kube.sets), [cw.pod_name(WORKER)])

    def test_mutant_swallowed_lookup_error_fails(self):
        m = load_module("cloud_worker.py", "            raise CloudError(\"could not look up statefulset/%s; REMAINS unknown: %s\" % (name, e))",
                        "            return None")
        self.gone()
        def boom(name):
            raise m.CloudError("internal error")
        self.kube.get_workload = boom
        out = m.Launcher(self.kube, self.env, self.clock).delete(argparse.Namespace(worker=WORKER, force=True, reason="x"))
        self.assertTrue(out["deleted"])   # the mutant answers deleted: the real code raises (the test above), so that test fails on it
        self.assertEqual(list(self.kube.sets), [cw.pod_name(WORKER)])


class OperatorRole(unittest.TestCase):
    """The cw-operator Role must grant every verb the launcher uses on a StatefulSet, and stay namespaced."""
    RBAC = os.path.join(ROOT, "cloud", "k8s", "operator-rbac.yaml")

    @staticmethod
    def launcher_verbs(src):
        verbs = set(re.findall(r'self\.run\(\["(\w+)", "statefulset"', src))
        verbs.add("create")   # apply(): `kubectl create -f -` of the StatefulSet
        if re.search(r'"delete", "statefulset"[^\n]*--wait=true', src):
            verbs |= {"list", "watch"}   # kubectl's delete wait lists and watches the object
        return {"delete" if v == "delete" else v for v in verbs}

    @staticmethod
    def granted(text):
        out = set()
        for rule in re.split(r"\n  - apiGroups:", text):
            if re.match(r'\s*\["apps"\]', rule) and 'resources: ["statefulsets"]' in rule:
                out |= set(re.findall(r'"(\w+)"', re.search(r"verbs: \[(.*?)\]", rule).group(1)))
        return out

    def test_every_launcher_verb_on_a_statefulset_is_granted(self):
        need = self.launcher_verbs(open(os.path.join(LAUNCHER, "cloud_worker.py")).read())
        self.assertTrue({"get", "create", "delete"} <= need)
        self.assertEqual(need - self.granted(open(self.RBAC).read()), set())

    def test_the_role_stays_namespaced(self):
        text = open(self.RBAC).read()
        self.assertNotIn("ClusterRole", text)
        self.assertIn("kind: Role\n", text)

    def test_mutant_a_dropped_verb_is_caught(self):
        text = open(self.RBAC).read().replace('verbs: ["get", "list", "watch", "create", "delete"]', 'verbs: ["get", "list", "watch", "create"]')
        need = self.launcher_verbs(open(os.path.join(LAUNCHER, "cloud_worker.py")).read())
        self.assertNotEqual(need - self.granted(text), set())


class Docs(unittest.TestCase):   # DW-7 (S11)
    FILES = ("cloud/README.md", "cloud/launcher/README.md", "docs/cloud-operator-quickstart.md")

    def block(self, rel):
        text = open(os.path.join(ROOT, rel)).read()
        m = re.search(re.escape(cw.ROLES_BEGIN) + r"\n(.*?)\n" + re.escape(cw.ROLES_END), text, re.S)
        self.assertTrue(m, rel)
        return m.group(1)

    def test_every_doc_carries_exactly_the_generated_block(self):
        for rel in self.FILES:
            self.assertEqual(self.block(rel), cw.render_roles_doc(), rel)

    def test_the_block_states_the_role_the_mode_both_token_sets_and_a_per_day_figure_from_the_row(self):
        doc = self.block(self.FILES[0])
        for must in ("`orchestrator`", "restart Always", "no deadline", app_token.render_permissions(app_token.ROLE_PERMISSIONS["worker"]),
                     app_token.render_permissions(ORCH_SET), "USD %.2f per day" % (24 * cw.SHAPES["ORCH"]["usd_per_hour"])):
            self.assertIn(must, doc)


if __name__ == "__main__":
    unittest.main()
