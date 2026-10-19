"""Offline tests for the stack pod shape (#567 slice 3): S2 (the stack pod), U1 (the default pod is unchanged), and the
fresh-node race. No cloud: kubectl is the fake beside this file; time is a fake clock.

The fixture fixtures/default_pod.json was built by the launcher's code on main (commit f5d6f98) BEFORE this change, with:
    python3 gen.py > cloud/launcher/tests/fixtures/default_pod.json
where gen.py calls cloud_worker.render_pod for the two cases named in the fixture (the inputs are in `_args` / CASES below).
"""
import argparse
import copy
import datetime as dt
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402
from test_cloud_worker import Base, T0  # noqa: E402

ENV = {"CW_CLOUD_IMAGE": "registry.example.internal/worker:1", "CW_TOKEN_ENDPOINT": "https://tokens.example.internal/mint"}


def _args(**kw):
    d = dict(repo="acme-org/widget-service", branch="main", idle="10m", hard="2h", max_hard="8h", shape="S", image=None,
             run_id=None, pull_secret=None)
    d.update(kw)
    return argparse.Namespace(**d)


CASES = {"default": (_args(), ()),
         "shape_m_run_secrets_pull": (_args(shape="M", run_id="abcdef0123456789", pull_secret="regcred"), ("deploy-key", "npm-token-file"))}


def build(a, names=()):
    return cw.render_pod(a, "coder7", cw.pod_name("coder7"), T0, T0 + dt.timedelta(hours=2), ENV, names)


class U1DefaultPodUnchanged(unittest.TestCase):
    def test_default_pod_is_byte_identical_to_the_fixture_from_main(self):
        with open(os.path.join(HERE, "fixtures", "default_pod.json")) as f:
            stored = f.read()
        built = {k: build(a, n) for k, (a, n) in CASES.items()}
        self.assertEqual(json.dumps(built, indent=2, sort_keys=True) + "\n", stored)

    def test_default_pod_has_none_of_the_stack_fields(self):
        pod = build(*CASES["default"])
        for field in ("runtimeClassName", "hostUsers", "nodeSelector"):
            self.assertNotIn(field, pod["spec"])
        self.assertNotIn("cw/stack", pod["metadata"]["annotations"])
        self.assertIs(pod["spec"]["containers"][0]["securityContext"]["allowPrivilegeEscalation"], False)


class S2StackPod(unittest.TestCase):
    def pod(self, **kw):
        return build(_args(stack=True, **kw))

    def test_the_shape(self):
        pod = self.pod()
        spec = pod["spec"]
        self.assertEqual(spec["runtimeClassName"], "sysbox-runc")
        self.assertIs(spec["hostUsers"], False)
        self.assertIs(spec["securityContext"]["runAsNonRoot"], True)
        self.assertEqual(spec["securityContext"]["runAsUser"], 1000)
        self.assertEqual(spec["nodeSelector"], {"sysbox-install": "yes"})
        self.assertNotIn("sysbox-runtime", json.dumps(spec["nodeSelector"]))   # never gate on the half-ready label
        self.assertIs(spec["automountServiceAccountToken"], False)
        self.assertEqual([v for v in spec["volumes"] if "hostPath" in v], [])
        self.assertNotIn("docker.sock", json.dumps(pod))
        for c in spec["containers"]:
            self.assertFalse(c["securityContext"].get("privileged", False))
        self.assertEqual(spec["containers"][0]["resources"]["limits"], {"cpu": "2", "memory": "4Gi"})   # the hard limits stay
        self.assertEqual(pod["metadata"]["annotations"]["cw/stack"], "true")

    def test_security_context_constant_pins(self):
        sc = cw.STACK_SECURITY_CONTEXT
        self.assertEqual(sc, {"allowPrivilegeEscalation": True, "capabilities": {"drop": ["ALL"]}})
        self.assertNotIn("privileged", sc)
        self.assertEqual(sc["capabilities"].get("add", []), [])
        self.assertEqual(self.pod()["spec"]["containers"][0]["securityContext"], sc)

    def test_the_constant_is_copied_not_shared(self):
        pod = self.pod()
        pod["spec"]["containers"][0]["securityContext"]["capabilities"]["drop"].append("X")
        self.assertEqual(cw.STACK_SECURITY_CONTEXT["capabilities"]["drop"], ["ALL"])

    def test_memory_limit_holds_the_stack(self):
        for shape in ("S", "M"):
            lim = self.pod(shape=shape)["spec"]["containers"][0]["resources"]["limits"]
            self.assertGreaterEqual(int(lim["memory"][:-2]), 4)
            self.assertGreaterEqual(int(lim["cpu"]), 2)


# one mutant per member of the forbidden table: how to put the field on a clean stack pod
def _m_hostpath(p): p["spec"]["volumes"].append({"name": "h", "hostPath": {"path": "/var/lib"}})
def _m_sock(p): p["spec"]["containers"][0]["volumeMounts"].append({"name": "s", "mountPath": "/var/run/docker.sock"})
def _m_priv(p): p["spec"]["containers"][0]["securityContext"]["privileged"] = True
def _m_net(p): p["spec"]["hostNetwork"] = True
def _m_pid(p): p["spec"]["hostPID"] = True
def _m_ipc(p): p["spec"]["hostIPC"] = True
def _m_users(p): p["spec"]["hostUsers"] = True
def _m_caps(p): p["spec"]["containers"][0]["securityContext"]["capabilities"]["add"] = ["SYS_ADMIN"]
def _m_token(p): p["spec"]["automountServiceAccountToken"] = True


MUTANTS = {"hostPath": _m_hostpath, "docker.sock": _m_sock, "privileged": _m_priv, "hostNetwork": _m_net, "hostPID": _m_pid,
           "hostIPC": _m_ipc, "hostUsers true": _m_users, "capabilities add SYS_ADMIN/ALL": _m_caps, "service-account token": _m_token}


class ForbiddenFieldsTable(unittest.TestCase):
    def test_every_member_of_the_table_has_a_mutant(self):   # completeness: a new member without a mutant fails here
        self.assertEqual(set(MUTANTS), set(cw.FORBIDDEN_POD_FIELDS))

    def test_clean_pods_have_none(self):
        self.assertEqual(cw.forbidden_pod_fields(build(_args(stack=True))), [])
        self.assertEqual(cw.forbidden_pod_fields(build(*CASES["default"])), [])

    def test_each_member_is_found_alone(self):   # kills: a predicate emptied, or one that also fires for another member
        for name, mutate in MUTANTS.items():
            for stack in (True, False):
                with self.subTest(member=name, stack=stack):
                    pod = build(_args(stack=stack))
                    mutate(pod)
                    self.assertEqual(cw.forbidden_pod_fields(pod), [name])

    def test_caps_all_and_a_token_volume_are_found(self):
        pod = build(_args(stack=True))
        pod["spec"]["containers"][0]["securityContext"]["capabilities"]["add"] = ["ALL"]
        self.assertEqual(cw.forbidden_pod_fields(pod), ["capabilities add SYS_ADMIN/ALL"])
        pod = build(_args(stack=True))
        pod["spec"]["volumes"].append({"name": "t", "projected": {"sources": [{"serviceAccountToken": {"path": "t"}}]}})
        self.assertEqual(cw.forbidden_pod_fields(pod), ["service-account token"])

    def test_render_refuses_a_stack_context_that_adds_a_member(self):   # kills: the check in render_pod removed
        for add in (["SYS_ADMIN"], ["ALL"]):
            with self.subTest(add=add):
                bad = copy.deepcopy(cw.STACK_SECURITY_CONTEXT)
                bad["capabilities"]["add"] = add
                old, cw.STACK_SECURITY_CONTEXT = cw.STACK_SECURITY_CONTEXT, bad
                try:
                    with self.assertRaises(cw.CloudError):
                        build(_args(stack=True))
                finally:
                    cw.STACK_SECURITY_CONTEXT = old
        old, cw.STACK_SECURITY_CONTEXT = cw.STACK_SECURITY_CONTEXT, {"privileged": True}
        try:
            with self.assertRaises(cw.CloudError):
                build(_args(stack=True))
        finally:
            cw.STACK_SECURITY_CONTEXT = old


class FreshNodeRace(Base):
    RUN_ID = "abcdef0123456789"

    def stack(self, *extra, code=0):
        c, out, err = self.run_cli("create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--run-id", self.RUN_ID, *extra)
        self.assertEqual(c, code, err)
        return out, err

    def audit_lines(self):
        with open(self.env["CW_CLOUD_AUDIT"]) as f:
            return [json.loads(l) for l in f]

    def attempts(self):
        return [l for l in self.audit_lines() if l["action"] == "create-attempt"]

    def calls(self, word):
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            return [c for c in map(json.loads, f) if word in c]

    def secrets_made(self):
        return [o["metadata"]["name"] for o in self.st().get("applied", []) if o["kind"] == "Secret"]

    def test_the_message_once_then_success(self):
        self.patch_state(race_creates=1)
        self.stack("--stack")
        a = self.attempts()
        self.assertEqual(len(a), 2)
        self.assertIn("attempt 1 of 3", a[0]["reason"]); self.assertFalse(a[0]["ok"])
        self.assertIn("attempt 2 of 3", a[1]["reason"]); self.assertTrue(a[1]["ok"])
        pods = [o for o in self.st()["applied"] if o["kind"] == "Pod"]
        self.assertEqual(len(pods), 2)
        self.assertEqual([p["metadata"]["annotations"]["cw/run-id"] for p in pods], [self.RUN_ID] * 2)
        self.assertEqual(pods[0]["spec"], pods[1]["spec"])   # the same pod, the same nonce
        self.assertEqual(self.secrets_made(), [cw.pod_name("coder7") + "-pw"])   # the password Secret was made once
        self.assertIsNotNone(self.pod())
        self.assertEqual(self.audit_lines()[-1]["action"], "create"); self.assertTrue(self.audit_lines()[-1]["ok"])

    def test_the_window_bounds_the_retries(self):   # kills: the deadline check in _start_stack_pod removed
        self.patch_state(race_creates=2)
        old, cw.STACK_RACE_WINDOW_SECONDS = cw.STACK_RACE_WINDOW_SECONDS, 0
        try:
            out, err = self.stack("--stack", code=cw.EXIT_CLOUD)
        finally:
            cw.STACK_RACE_WINDOW_SECONDS = old
        self.assertIn("fresh-node race", err)
        a = self.attempts()
        self.assertEqual(len(a), 1)
        self.assertIn("giving up", a[0]["reason"])
        self.assertEqual(len([o for o in self.st()["applied"] if o["kind"] == "Pod"]), 1)

    def test_the_message_three_times_fails_and_leaves_nothing(self):
        self.patch_state(race_creates=3)
        out, err = self.stack("--stack", code=cw.EXIT_CLOUD)
        self.assertIn("fresh-node race", err)
        self.assertEqual(len(self.attempts()), 3)
        self.assertEqual([("attempt %d of 3" % i) in l["reason"] for i, l in enumerate(self.attempts(), 1)], [True] * 3)
        self.assertEqual(len([o for o in self.st()["applied"] if o["kind"] == "Pod"]), 3)
        self.assertEqual(self.secrets_made(), [cw.pod_name("coder7") + "-pw"])   # made once, never duplicated
        self.assertEqual(self.st()["pods"], {})
        self.assertEqual(self.st()["secrets"], {})   # and cleaned once the create failed
        self.assertFalse(self.audit_lines()[-1]["ok"])

    def test_run_secret_is_made_once_across_attempts(self):
        r, w = os.pipe(); os.write(w, b"s3cret-value-1234"); os.close(w)
        self.patch_state(race_creates=1)
        try:
            self.stack("--stack", "--run-secret-fd", "tok=%d" % r)
        finally:
            os.close(r)
        creates = [c for c in self.calls("generic") if c[c.index("generic") - 1] == "secret"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(self.attempts()), 2)

    def test_a_different_failure_is_not_retried(self):
        self.patch_state(other_failure="failed to pull image: not found")
        out, err = self.stack("--stack", "--wait", "10", code=cw.EXIT_TIMEOUT)
        self.assertEqual(len([o for o in self.st()["applied"] if o["kind"] == "Pod"]), 1)
        self.assertEqual(len(self.attempts()), 1)
        self.assertFalse(self.attempts()[0]["ok"])
        self.assertEqual(self.st()["pods"], {})

    def test_a_foreign_pod_is_left_whole(self):
        self.patch_state(race_creates=1)
        real = cw.Launcher._mine
        cw.Launcher._mine = lambda self_, kind, name: False if kind == "pod" else real(self_, kind, name)
        try:
            self.stack("--stack", code=cw.EXIT_CLOUD)
        finally:
            cw.Launcher._mine = real
        self.assertEqual(len([o for o in self.st()["applied"] if o["kind"] == "Pod"]), 1)   # no second create
        self.assertEqual([c for c in self.calls("delete") if "pod" in c and "--wait=true" in c], [])   # and no delete of the pod
        self.assertIn(cw.pod_name("coder7"), self.st()["pods"])   # left whole

    def test_events_of_an_earlier_pod_do_not_count(self):
        # an old pod's event under the same name must not trigger a retry of a healthy new pod
        self.patch_state(events=[{"reason": "FailedCreatePodSandBox", "involvedObject": {"kind": "Pod", "name": cw.pod_name("coder7"), "uid": "uid-0"},
                                  "message": "RuntimeClass handler sysbox-runc does not support user namespaces: the handler sysbox-runc is not known"}],
                         never_ready=True)
        self.stack("--stack", "--wait", "10", code=cw.EXIT_TIMEOUT)
        self.assertEqual(len([o for o in self.st()["applied"] if o["kind"] == "Pod"]), 1)

    def test_the_default_create_never_watches(self):
        self.stack()
        self.assertEqual(self.calls("events"), [])
        self.assertEqual(self.attempts(), [])
        self.assertNotIn("runtimeClassName", self.pod()["spec"])

    def test_the_stack_create_ends_with_the_stack_pod(self):
        self.stack("--stack")
        self.assertEqual(self.pod()["spec"]["runtimeClassName"], "sysbox-runc")


if __name__ == "__main__":
    unittest.main()
