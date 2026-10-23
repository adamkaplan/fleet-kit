"""#1228: a cloud orchestrator (and a worker that asks) keeps its session and unpushed work on a retained disk that survives the pod.

Offline: the orchestrator suite's in-memory cluster, extended with volume claims (a claim outlives its pod; a pod's own disk does not).
Mutants are built from the real source with one text replaced; each must fail the check that shows its clause. No token is a literal."""
import argparse
import contextlib
import copy
import datetime as dt
import io
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as cw  # noqa: E402
import test_cloud_orchestrator as to  # noqa: E402

T0, REPO, WORKER, RUN = to.T0, to.REPO, to.WORKER, to.RUN
NAME = cw.pod_name(WORKER)
CLAIM = "data-%s-0" % NAME
RBAC = os.path.join(ROOT, "cloud", "k8s", "operator-rbac.yaml")
README = os.path.join(LAUNCHER, "README.md")


class DiskKube(to.FakeKube):
    """The orchestrator suite's cluster plus claims. The controller creates NAME-0's claim from the template and wires it into the pod;
    deleting a pod keeps the claim; a file written under a mount lands on the claim's disk, or on the pod's own disk (lost with the pod)."""
    ns = "cw"

    def __init__(self):
        super().__init__()
        self.claims, self.disks, self.pod_disks, self.deleted_claims = {}, {}, {}, []

    def reconcile(self):
        super().reconcile()
        for name, ss in self.sets.items():
            pod = self.pods.get(name + "-0")
            for t in ss["spec"].get("volumeClaimTemplates", []):
                cn = "%s-%s-0" % (t["metadata"]["name"], name)
                if pod is not None and not any(v["name"] == t["metadata"]["name"] for v in pod["spec"].get("volumes", [])):
                    pod["spec"].setdefault("volumes", []).append({"name": t["metadata"]["name"], "persistentVolumeClaim": {"claimName": cn}})
                if cn not in self.claims:
                    c = copy.deepcopy(t)
                    c["metadata"].update(name=cn, creationTimestamp=cw.iso(T0))
                    self.claims[cn] = c

    def apply(self, obj):
        if obj["kind"] == "PersistentVolumeClaim":
            obj = copy.deepcopy(obj)
            obj["metadata"]["creationTimestamp"] = cw.iso(T0)
            self.claims[obj["metadata"]["name"]] = obj
            self.created.append(copy.deepcopy(obj))
            return
        super().apply(obj)

    def get_claim(self, name):
        return copy.deepcopy(self.claims.get(name))

    def list_claims(self):
        return copy.deepcopy(list(self.claims.values()))

    def delete_claim(self, name):
        self.deleted_claims.append(name)
        self.claims.pop(name, None)
        self.disks.pop(name, None)

    def _where(self, pod, path):
        best = None
        for m in pod["spec"]["containers"][0].get("volumeMounts", []):
            if path == m["mountPath"] or path.startswith(m["mountPath"].rstrip("/") + "/"):
                if best is None or len(m["mountPath"]) > len(best["mountPath"]):
                    best = m
        vol = next((v for v in pod["spec"].get("volumes", []) if best and v["name"] == best["name"]), None)
        if vol and "persistentVolumeClaim" in vol:
            return self.disks.setdefault(vol["persistentVolumeClaim"]["claimName"], {}), bool(best.get("readOnly"))
        return self.pod_disks.setdefault(pod["metadata"]["uid"], {}), False

    def write(self, pod, path, text):
        store, ro = self._where(pod, path)
        if ro:
            raise OSError("read-only file system: " + path)
        store[path] = text

    def read(self, pod, path):
        return self._where(pod, path)[0].get(path)


class Rig:
    """A launcher on a DiskKube, built from a given copy of the launcher's module (the real one, or a mutant)."""
    def __init__(self, mod=cw, **env):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.mod = mod
        os.makedirs(os.path.join(d, "records"))
        mint = os.path.join(d, "mint.sh")
        with open(mint, "w") as f:
            f.write("#!/bin/sh\nprintf %%s '%s'\n" % to.tok("renewed"))
        os.chmod(mint, 0o700)
        self.env = {"CW_ALLOWED_REPOS": REPO, "CW_CLOUD_IMAGE": "registry.example/img@sha256:" + "a" * 64, "CW_CLOUD_AUDIT": os.path.join(d, "audit.jsonl"),
                    "CW_CLOUD_RECORD_DIR": os.path.join(d, "records"), "CW_GIT_TOKEN_CMD": mint, "CW_TOKEN_ENDPOINT": "https://tokens.example.internal/mint"}
        self.env.update(env)
        self.kube, self.clock = DiskKube(), to.Clock()
        self.L = mod.Launcher(self.kube, self.env, self.clock)

    def args(self, **kw):
        fd = os.open(os.path.join(self.tmp.name, "gt"), os.O_RDWR | os.O_CREAT, 0o600)
        os.write(fd, to.tok("first").encode())
        os.lseek(fd, 0, 0)
        base = dict(worker=WORKER, repo=REPO, branch="main", idle="10m", hard="2h", max_hard="8h", shape="S", image=None, stack=False, wait=5,
                    pull_secret=None, run_secret_fd=["git-token=%d" % fd], run_id=RUN, password_fd=None, role="orchestrator", disk="auto")
        base.update(kw)
        return argparse.Namespace(**base)

    def launch(self, **kw):
        a = self.args(**kw)
        with contextlib.redirect_stderr(io.StringIO()):
            out = self.L.create(a)
        with open(os.path.join(self.tmp.name, "records", WORKER + ".json"), "w") as f:
            json.dump({"repo": REPO, "role": a.role, "hard_deadline": cw.iso(T0 + dt.timedelta(hours=2))}, f)
        return out

    def stop(self, **kw):
        with contextlib.redirect_stderr(io.StringIO()):
            return self.L.stop(argparse.Namespace(worker=WORKER, reason="done", run_id=RUN, no_record=True, record_dir=None, **kw))

    def workload(self):
        return self.kube.sets[NAME]

    def pod(self):
        return self.kube.get_pod(NAME + "-0")


def mutant(old, new):
    return to.load_module("cloud_worker.py", old, new)


# the checks a mutant must fail: each returns normally on the real source and raises AssertionError on a build with the fault
def check_stop_keeps_claim(mod):
    r = Rig(mod)
    r.launch()
    assert CLAIM in r.kube.claims
    r.stop()
    assert r.kube.sets == {} and r.kube.pods == {}, "pod and workload must be gone"
    assert CLAIM in r.kube.claims, "the claim must remain after stop"


def check_mount_writable(mod):
    r = Rig(mod)
    r.launch()
    r.kube.write(r.pod(), "/data/session/db", "s")   # raises OSError on a read-only mount
    assert r.kube.read(r.pod(), "/data/session/db") == "s"


def check_other_namespace_refused(mod):
    r = Rig(mod)
    try:
        r.launch(disk_namespace="other")
    except mod.CloudError as e:
        assert e.code == mod.EXIT_USAGE
        assert not r.kube.claims
        return
    raise AssertionError("a claim in another namespace was accepted")


def check_resumes(mod, **kw):
    r = Rig(mod)
    r.launch(**kw)
    pod = r.pod()
    r.kube.write(pod, "/data/session/opencode.db", "session")
    r.kube.write(pod, "/data/work/widget-service/.git/refs/unpushed", "commit")
    r.kube.pods.pop(NAME + "-0")   # the pod is killed; the controller recreates it
    new = r.pod()
    assert new["metadata"]["uid"] != pod["metadata"]["uid"]
    present = [r.kube.read(new, "/data/session/opencode.db") == "session", r.kube.read(new, "/data/work/widget-service/.git/refs/unpushed") == "commit"]
    assert all(present), "%d of 2 items present after the kill" % sum(present)


class Shape(unittest.TestCase):
    def test_claim_named_and_mounted(self):
        r = Rig()
        r.launch()
        ts = r.workload()["spec"]["volumeClaimTemplates"]
        self.assertEqual(len(ts), 1)
        self.assertEqual(ts[0]["metadata"]["name"], cw.DISK_VOLUME)
        self.assertIn(NAME, CLAIM)
        self.assertEqual(ts[0]["spec"]["resources"]["requests"]["storage"], "8Gi")
        self.assertEqual(ts[0]["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertNotIn("storageClassName", ts[0]["spec"])   # empty: the cluster's default class
        self.assertEqual(ts[0]["metadata"]["namespace"], "cw")
        mounts = to.pod_of(r.workload())["containers"][0]["volumeMounts"]
        self.assertEqual([m for m in mounts if m["name"] == "data"], [{"name": "data", "mountPath": "/data"}])
        self.assertEqual(r.kube.claims[CLAIM]["metadata"]["name"], CLAIM)
        print("1 of 1 volume claim templates named after the workload and mounted at the agent's data path")

    def test_storage_class_and_size_are_launch_settings(self):
        r = Rig()
        r.launch(disk="16", disk_class="fast")
        spec = r.workload()["spec"]["volumeClaimTemplates"][0]["spec"]
        self.assertEqual((spec["resources"]["requests"]["storage"], spec["storageClassName"]), ("16Gi", "fast"))
        for bad in ("33", "0", "x"):
            with self.assertRaises(cw.CloudError) as c:
                Rig().launch(disk=bad)
            self.assertEqual(c.exception.code, cw.EXIT_USAGE)

    def test_other_paths_unchanged(self):
        r = Rig()
        r.launch()
        pod = to.pod_of(r.workload())
        vols = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(vols["tokens"], {"name": "tokens", "emptyDir": {"medium": "Memory"}})
        self.assertEqual(vols["scratch"], {"name": "scratch", "emptyDir": {}})
        self.assertIn("projected", vols["pw"])
        self.assertNotIn("data", vols)   # the claim template supplies it
        mounts = {m["name"]: m for m in pod["containers"][0]["volumeMounts"]}
        self.assertTrue(mounts["pw"]["readOnly"])
        self.assertEqual((mounts["tokens"]["mountPath"], mounts["scratch"]["mountPath"]), ("/run/cw-tmp", "/work"))
        self.assertEqual(cw.forbidden_pod_fields(copy.deepcopy({"spec": pod})), [])
        print("3 of 3 other volumes unchanged (tokens memory-backed, per-run secret read-only, scratch as before)")

    def test_no_token_on_disk(self):
        r = Rig()
        r.launch()
        mounts = to.pod_of(r.workload())["containers"][0]["volumeMounts"]
        disk = next(m["mountPath"] for m in mounts if m["name"] == "data")
        under = [m["mountPath"] for m in mounts if m["name"] != "data" and (m["mountPath"] + "/").startswith(disk + "/")]
        self.assertEqual(under, [])
        self.assertTrue(all(not (disk + "/").startswith(m["mountPath"] + "/") for m in mounts if m["name"] != "data"))

    def test_plain_worker_unchanged(self):
        r = Rig()
        base = cw.render_pod(r.args(role="worker", disk=None), WORKER, NAME, T0, T0 + dt.timedelta(hours=2), r.env, ["git-token"])
        for same in ("auto", "off"):
            self.assertEqual(cw.render_pod(r.args(role="worker", disk=same), WORKER, NAME, T0, T0 + dt.timedelta(hours=2), r.env, ["git-token"],
                                           cw.parse_disk(r.args(role="worker", disk=same), "worker")), base)
        c = base["spec"]["containers"][0]
        self.assertEqual([m["name"] for m in c["volumeMounts"]], ["pw", "tokens", "scratch"])
        self.assertEqual([v["name"] for v in base["spec"]["volumes"]], ["pw", "tokens", "scratch"])
        self.assertNotIn("WORKER_DISK_DIR", json.dumps(base))
        a = r.args(role="worker", disk="4")
        withd = cw.render_pod(a, WORKER, NAME, T0, T0 + dt.timedelta(hours=2), r.env, ["git-token"], cw.parse_disk(a, "worker"))
        self.assertEqual([v["name"] for v in withd["spec"]["volumes"]], ["pw", "tokens", "scratch", "data"])
        self.assertEqual(withd["spec"]["volumes"][-1], {"name": "data", "persistentVolumeClaim": {"claimName": "data-" + NAME}})
        print("identical to the baseline")

    def test_plain_worker_launch_without_the_option_makes_no_claim(self):
        r = Rig()
        r.launch(role="worker", disk="auto")
        self.assertEqual(r.kube.claims, {})
        r2 = Rig()
        r2.launch(role="worker", disk="4")
        self.assertEqual(list(r2.kube.claims), ["data-" + NAME])
        r3 = Rig()
        r3.launch(disk="off")   # an orchestrator with the option off: no claim, as before
        self.assertEqual((r3.kube.claims, "volumeClaimTemplates" in r3.workload()["spec"]), ({}, False))


class Resume(unittest.TestCase):
    def test_kill_pod_resumes_same_disk(self):
        check_resumes(cw)
        print("2 of 2 items present after the kill (the session file and the unpushed commit)")

    def test_mutant_fresh_emptydir_loses_the_session(self):
        with self.assertRaises(AssertionError):
            check_resumes(cw, disk="off")

    def test_a_launch_of_the_same_name_attaches_the_same_claim_and_a_new_name_a_new_one(self):
        r = Rig()
        r.launch()
        r.kube.write(r.pod(), "/data/session/db", "s")
        r.stop()
        r.launch()
        self.assertEqual(r.kube.read(r.pod(), "/data/session/db"), "s")
        self.assertEqual(len(r.kube.claims), 1)
        with self.assertRaises(cw.CloudError) as c:   # a live lane's name is never given a second pod
            r.launch()
        self.assertEqual(c.exception.code, cw.EXIT_EXISTS)


class StopAndDelete(unittest.TestCase):
    def test_stop_keeps_claim(self):
        check_stop_keeps_claim(cw)
        print("1 of 1 claims remaining after stop")

    def test_delete_disk_flag_and_command(self):
        r = Rig()
        r.launch()
        r.stop(delete_disk=True)
        self.assertEqual(r.kube.claims, {})
        r.launch()
        r.stop()
        self.assertIn(CLAIM, r.kube.claims)
        out = r.L.delete_disk(argparse.Namespace(worker=WORKER, reason=""))
        self.assertEqual((out["deleted_claims"], r.kube.claims), ([CLAIM], {}))
        r.launch()
        with self.assertRaises(cw.CloudError) as c:   # a claim bound to a live pod is refused
            r.L.delete_disk(argparse.Namespace(worker=WORKER, reason=""))
        self.assertEqual(c.exception.code, cw.EXIT_REFUSED)
        self.assertIn(CLAIM, r.kube.claims)

    def test_the_cli_knows_the_option_and_the_command(self):
        p = cw.build_parser()
        self.assertEqual(p.parse_args(["create", "w", "--repo", REPO, "--branch", "b"]).disk, "auto")
        self.assertEqual(p.parse_args(["create", "w", "--repo", REPO, "--branch", "b", "--disk"]).disk, "")
        self.assertEqual(p.parse_args(["create", "w", "--repo", REPO, "--branch", "b", "--disk", "12"]).disk, "12")
        self.assertTrue(p.parse_args(["stop", "w", "--delete-disk"]).delete_disk)
        self.assertEqual(p.parse_args(["delete-disk", "w"]).cmd, "delete-disk")


class Reaper(unittest.TestCase):
    def test_reaper_reports_never_deletes(self):
        r = Rig()
        for n, days in (("data-old-0", 9), ("data-young-0", 3)):
            r.kube.claims[n] = {"metadata": {"name": n, "labels": {"app": cw.LABEL, "cw/worker": n[5:-2]},
                                             "creationTimestamp": cw.iso(T0 - dt.timedelta(days=days))},
                                "spec": {"resources": {"requests": {"storage": "8Gi"}}}}
        err = io.StringIO()
        for _ in range(3):
            with contextlib.redirect_stderr(err):
                r.L.reap()
        text = err.getvalue()
        self.assertIn("retained disk data-old-0 8Gi 9d", text)
        self.assertNotIn("data-young-0", text)
        self.assertEqual(sorted(r.kube.claims), ["data-old-0", "data-young-0"])
        self.assertEqual(r.kube.deleted_claims, [])   # 0 occurrences of a claim delete
        print("retained disk data-old-0 8Gi 9d")

    def test_a_claim_held_by_a_live_pod_is_not_reported(self):
        r = Rig()
        r.launch()
        r.kube.claims[CLAIM]["metadata"]["creationTimestamp"] = cw.iso(T0 - dt.timedelta(days=30))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            r.L.reap()
        self.assertNotIn("retained disk", err.getvalue())

    def test_retention_constant(self):
        self.assertEqual(cw.RETAINED_DISK_DAYS, 7)
        self.assertEqual(len(re.findall(r"\bRETAINED_DISK_DAYS\b", open(os.path.join(LAUNCHER, "cloud_worker.py")).read())) >= 3, True)
        self.assertIn("RETAINED_DISK_DAYS = 7", open(README).read())
        print("RETAINED_DISK_DAYS = 7")


class Cost(unittest.TestCase):
    def two_disks(self, r):
        for n in ("data-a-0", "data-b-0"):
            r.kube.claims[n] = {"metadata": {"name": n, "labels": {"app": cw.LABEL}}, "spec": {"resources": {"requests": {"storage": "8Gi"}}}}

    def test_cost_line_states_disks(self):
        r = Rig()
        self.two_disks(r)
        self.assertEqual(r.L.cost_line(), "disks: 16 GiB retained at 0.075 USD per GiB-month = 1.20 USD per month")
        print(r.L.cost_line())

    def test_cap_counts_disks(self):
        r = Rig(CW_CLOUD_SPEND_CAP_USD="1.6")   # the orchestrator's day (0.48) and its disk (0.60) fit
        r.launch()
        self.assertTrue(r.kube.claims)
        r2 = Rig(CW_CLOUD_SPEND_CAP_USD="1.6")
        self.two_disks(r2)   # 1.20 of retained disks: the compute alone would still fit
        with self.assertRaises(cw.CloudError) as c:
            r2.launch()
        self.assertEqual(c.exception.code, cw.EXIT_CAP)
        self.assertEqual(r2.kube.sets, {})
        print("exit status is %d" % c.exception.code)


class Permissions(unittest.TestCase):
    def test_rbac_claim_verbs_namespaced(self):
        text = open(RBAC).read()
        rules = [x for x in re.split(r"\n  - apiGroups:", text) if "persistentvolumeclaims" in x.split("\n---")[0] and "resources:" in x]
        self.assertEqual(len(rules), 1)
        self.assertEqual(re.findall(r'"(\w+)"', re.search(r"verbs: \[(.*?)\]", rules[0]).group(1)), ["get", "list", "create", "delete"])
        self.assertNotIn("ClusterRole", re.sub(r"#[^\n]*", "", text))
        self.assertNotIn('"*"', text)
        self.assertEqual(len(re.findall(r"^kind: Role$", text, re.M)), 1)   # one Role: the operator's; the worker account has none
        role = text.split("kind: Role\n")[1].split("\n---")[0]
        self.assertIn("namespace: cw", role)
        print("1 of 1 claim rules with exactly the verbs get, list, create, delete")

    def test_the_launcher_uses_no_claim_verb_the_role_lacks(self):
        src = open(os.path.join(LAUNCHER, "cloud_worker.py")).read()
        used = set(re.findall(r'self\.run\(\["(\w+)", "persistentvolumeclaims?"', src))
        self.assertEqual(used, {"get", "delete"})
        self.assertTrue(used <= {"get", "list", "create", "delete"})


class Namespace(unittest.TestCase):
    def test_claim_other_namespace_refused(self):
        check_other_namespace_refused(cw)
        with self.assertRaises(cw.CloudError) as c:
            cw.check_claim_namespace({"metadata": {"namespace": "other"}}, "cw")
        self.assertEqual(c.exception.code, 2)
        print("exit status is 2")


class Mutants(unittest.TestCase):
    def test_mutant_delete_at_stop(self):
        with self.assertRaises(AssertionError):
            check_stop_keeps_claim(mutant('if getattr(a, "delete_disk", False):', "if True:"))

    def test_mutant_readonly_mount(self):
        with self.assertRaises(OSError):
            check_mount_writable(mutant('{"name": DISK_VOLUME, "mountPath": DISK_MOUNT})', '{"name": DISK_VOLUME, "mountPath": DISK_MOUNT, "readOnly": True})'))

    def test_mutant_other_namespace(self):
        with self.assertRaises(AssertionError):
            check_other_namespace_refused(mutant("if ns is not None and ns != namespace:", "if False:"))

    def test_the_real_source_passes_the_same_checks(self):
        check_stop_keeps_claim(cw)
        check_mount_writable(cw)
        check_other_namespace_refused(cw)
        print("3 of 3 mutants fail the tests")


class Docs(unittest.TestCase):
    def text(self):
        return open(README).read()

    def test_docs_state_cause(self):
        t = self.text()
        self.assertIn("a pod's own disk is deleted with the pod", t)
        self.assertIn("no disk of its own", t)
        self.assertLess(t.index("a pod's own disk is deleted with the pod"), t.index("--disk"))   # the cause comes first
        print("a pod's own disk is deleted with the pod")

    def test_docs_name_disk_items(self):
        t = self.text()
        items = ["`--disk", "delete-disk", "RETAINED_DISK_DAYS = 7", "0.075 USD per GiB per month", "kubectl apply --dry-run=server"]
        self.assertEqual([i for i in items if i not in t], [])
        print("%d of %d named items present" % (len(items), len(items)))


if __name__ == "__main__":
    unittest.main()
