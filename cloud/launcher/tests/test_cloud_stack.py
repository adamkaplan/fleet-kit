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
def _m_ctx(p): p["spec"]["containers"][0]["securityContext"]["capabilities"]["add"] = ["NET_RAW"]
def _m_shape(p): p["spec"]["runtimeClassName"] = "runc"
def _m_rest(p): p["spec"]["nodeName"] = "node-1"
def _m_token(p): p["spec"]["automountServiceAccountToken"] = True


REST = "stack pod has a field or object outside the stack allow-list"
MUTANTS = {"hostPath": _m_hostpath, "docker.sock": _m_sock, "privileged": _m_priv, "hostNetwork": _m_net, "hostPID": _m_pid,
           "hostIPC": _m_ipc, "hostUsers true": _m_users, "capabilities add SYS_ADMIN/ALL": _m_caps, "service-account token": _m_token,
           "stack security context differs from STACK_SECURITY_CONTEXT": _m_ctx, "stack pod shape differs": _m_shape, REST: _m_rest}
STACK_ONLY = {"stack security context differs from STACK_SECURITY_CONTEXT", "stack pod shape differs", REST}
ALLOW = sorted(STACK_ONLY)


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
                    found = cw.forbidden_pod_fields(pod)
                    if name in STACK_ONLY and not stack:
                        continue   # the allow-list rows apply to a stack pod only (a runtimeClassName makes it one, see below)
                    # on a stack pod the allow-list may also fire for a mutation of the container context
                    self.assertEqual([f for f in found if f not in STACK_ONLY], [] if name in STACK_ONLY else [name])
                    if name in STACK_ONLY:
                        self.assertIn(name, found)

    def test_caps_all_and_a_token_volume_are_found(self):
        pod = build(_args(stack=True))
        pod["spec"]["containers"][0]["securityContext"]["capabilities"]["add"] = ["ALL"]
        self.assertIn("capabilities add SYS_ADMIN/ALL", cw.forbidden_pod_fields(pod))
        pod = build(_args(stack=True))
        pod["spec"]["volumes"].append({"name": "t", "projected": {"sources": [{"serviceAccountToken": {"path": "t"}}]}})
        self.assertIn("service-account token", cw.forbidden_pod_fields(pod))   # (the extra volume also trips the allow-list row)

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

    # -- the pod that vanishes while pending (S11 and the table of watch outcomes) --
    def pod_creates(self):
        return [o for o in self.st()["applied"] if o["kind"] == "Pod"]

    def reasons(self):
        return [l["reason"] for l in self.attempts()]

    def test_s11_the_message_then_the_pod_vanishes_then_success(self):   # kills: recreate only on the event message
        self.patch_state(plan=["race_vanish", "ok"])
        self.stack("--stack")
        pods = self.pod_creates()
        self.assertEqual(len(pods), 2)
        self.assertEqual([p["metadata"]["annotations"]["cw/run-id"] for p in pods], [self.RUN_ID] * 2)
        self.assertEqual(self.secrets_made(), [cw.pod_name("coder7") + "-pw"])   # one secret create
        self.assertEqual(len(self.attempts()), 2)
        self.assertIsNotNone(self.pod())

    def test_vanish_with_no_message_then_success(self):   # kills: the vanish branch removed
        self.patch_state(plan=["vanish", "ok"])
        self.stack("--stack")
        self.assertEqual(len(self.pod_creates()), 2)
        self.assertIn("pod vanished", self.reasons()[0]); self.assertFalse(self.attempts()[0]["ok"])
        self.assertTrue(self.attempts()[1]["ok"])
        self.assertEqual(self.secrets_made(), [cw.pod_name("coder7") + "-pw"])

    def test_message_then_vanish_then_success_across_pods(self):
        self.patch_state(plan=["race", "vanish", "ok"])
        self.stack("--stack")
        self.assertEqual(len(self.pod_creates()), 3)
        self.assertIn("handler", self.reasons()[0]); self.assertIn("pod vanished", self.reasons()[1])
        self.assertEqual(self.secrets_made(), [cw.pod_name("coder7") + "-pw"])

    def test_vanish_twice_then_success(self):
        self.patch_state(plan=["vanish", "vanish", "ok"])
        self.stack("--stack")
        self.assertEqual(len(self.pod_creates()), 3)
        self.assertEqual(len(self.attempts()), 3)
        self.assertEqual([("attempt %d of 3" % i) in r for i, r in enumerate(self.reasons(), 1)], [True] * 3)
        self.assertEqual(self.secrets_made(), [cw.pod_name("coder7") + "-pw"])   # kills: secrets recreated per attempt

    def test_vanish_three_times_fails_names_it_and_leaves_nothing(self):   # kills: the attempt cap ignored
        self.patch_state(plan=["vanish"] * 5)
        out, err = self.stack("--stack", code=cw.EXIT_CLOUD)
        self.assertIn("vanished", err)
        self.assertEqual(len(self.pod_creates()), 3)
        self.assertEqual(len(self.attempts()), 3)
        self.assertIn("giving up", self.reasons()[2])
        self.assertEqual(self.secrets_made(), [cw.pod_name("coder7") + "-pw"])   # made once
        self.assertEqual(self.st()["pods"], {}); self.assertEqual(self.st()["secrets"], {})
        self.assertFalse(self.audit_lines()[-1]["ok"])

    def test_vanish_respects_the_window(self):   # kills: the deadline ignored on the vanish branch
        self.patch_state(plan=["vanish"] * 3)
        old, cw.STACK_RACE_WINDOW_SECONDS = cw.STACK_RACE_WINDOW_SECONDS, 0
        try:
            out, err = self.stack("--stack", code=cw.EXIT_CLOUD)
        finally:
            cw.STACK_RACE_WINDOW_SECONDS = old
        self.assertIn("vanished", err)
        self.assertEqual(len(self.pod_creates()), 1)

    def test_a_pod_that_never_existed_is_not_retried(self):
        self.patch_state(hide_pod=True)
        out, err = self.stack("--stack", "--wait", "10", code=cw.EXIT_TIMEOUT)
        self.assertEqual(len(self.pod_creates()), 1)
        self.assertEqual(len(self.attempts()), 1)
        self.assertNotIn("vanished", self.reasons()[0])

    def test_the_run_secret_is_made_once_when_the_pod_vanishes(self):
        r, w = os.pipe(); os.write(w, b"x" * 12); os.close(w)
        self.patch_state(plan=["vanish", "ok"])
        try:
            self.stack("--stack", "--run-secret-fd", "tok=%d" % r)
        finally:
            os.close(r)
        self.assertEqual(len([c for c in self.calls("generic") if c[c.index("generic") - 1] == "secret"]), 1)

    def test_a_failed_phase_is_not_retried(self):
        self.patch_state(other_failure="failed to pull image: not found")
        self.stack("--stack", "--wait", "10", code=cw.EXIT_TIMEOUT)
        self.assertEqual(len(self.pod_creates()), 1)
        self.assertNotIn("vanished", self.reasons()[0])

    def test_every_outcome_of_the_watch_loop_has_a_test(self):   # completeness: the table of outcomes the loop can end in
        outcomes = {"ready", "race", "vanish", "never_seen", "other_failure", "cap", "window"}
        tested = {"ready": "test_the_message_once_then_success", "race": "test_the_message_three_times_fails_and_leaves_nothing",
                  "vanish": "test_vanish_twice_then_success", "never_seen": "test_a_pod_that_never_existed_is_not_retried",
                  "other_failure": "test_a_different_failure_is_not_retried", "cap": "test_vanish_three_times_fails_names_it_and_leaves_nothing",
                  "window": "test_vanish_respects_the_window"}
        self.assertEqual(set(tested), outcomes)
        for name in tested.values():
            self.assertTrue(hasattr(self, name), name)

    def test_the_default_create_never_watches_a_vanish(self):
        self.stack()
        self.assertEqual(self.calls("events"), [])


def _with_caps(stack, caps, where="containers"):
    pod = build(_args(stack=stack))
    k = copy.deepcopy(pod["spec"]["containers"][0])
    k["securityContext"].setdefault("capabilities", {})["add"] = list(caps)
    if where == "containers":
        pod["spec"]["containers"][0] = k
    else:
        pod["spec"][where] = [k]
    return pod


class CapabilityNames(unittest.TestCase):
    NAMED = "capabilities add SYS_ADMIN/ALL"
    ALLOW_ROW = "stack security context differs from STACK_SECURITY_CONTEXT"

    def test_spellings_of_the_worst_are_refused_by_name(self):   # kills: the normalisation dropped
        for caps in (["SYS_ADMIN"], ["sys_admin"], ["CAP_SYS_ADMIN"], ["cap_sys_admin"], ["ALL"], ["all"], ["CAP_ALL"], ["Sys_Admin"]):
            for stack in (True, False):
                for where in ("containers", "initContainers", "ephemeralContainers"):
                    with self.subTest(caps=caps, stack=stack, where=where):
                        self.assertIn(self.NAMED, cw.forbidden_pod_fields(_with_caps(stack, caps, where)))

    def test_any_other_capability_is_refused_on_a_stack_pod(self):   # kills: the allow-list dropped
        for cap in ("NET_RAW", "SYS_PTRACE", "SYS_MODULE", "NET_ADMIN", "BPF", "DAC_READ_SEARCH", "SYS_RAWIO", "SETUID", "net_raw", "CAP_NET_RAW"):
            for where in ("containers", "initContainers", "ephemeralContainers"):
                with self.subTest(cap=cap, where=where):
                    self.assertIn(self.ALLOW_ROW, cw.forbidden_pod_fields(_with_caps(True, [cap], where)))

    def test_render_refuses_them_end_to_end(self):
        for add in (["CAP_SYS_ADMIN"], ["all"]):   # NET_RAW in the constant itself is caught by test_security_context_constant_pins
            bad = copy.deepcopy(cw.STACK_SECURITY_CONTEXT)
            bad["capabilities"]["add"] = add
            old, cw.STACK_SECURITY_CONTEXT = cw.STACK_SECURITY_CONTEXT, bad
            try:
                with self.subTest(add=add), self.assertRaises(cw.CloudError):
                    build(_args(stack=True))
            finally:
                cw.STACK_SECURITY_CONTEXT = old

    def test_an_init_or_ephemeral_container_without_the_context_is_refused(self):
        for where in ("initContainers", "ephemeralContainers"):
            pod = build(_args(stack=True))
            pod["spec"][where] = [{"name": "x", "image": "i"}]
            with self.subTest(where=where):
                self.assertIn(self.ALLOW_ROW, cw.forbidden_pod_fields(pod))


class StackShapeFamily(unittest.TestCase):
    """Every field the table compares by exact value on a stack pod; each variant spelling is refused (equality, not a list)."""
    ROW = "stack pod shape differs"
    VARIANTS = {
        "runtimeClassName": [("spec", "runtimeClassName", v) for v in ("Sysbox-runc", "SYSBOX-RUNC", "sysbox-runc ", "runc", "")],
        "nodeSelector": [("spec", "nodeSelector", v) for v in ({"sysbox-install": "Yes"}, {"sysbox-install": "true"}, {"Sysbox-install": "yes"}, {"sysbox-runtime": "running"}, {})],
        "hostUsers": [("spec", "hostUsers", v) for v in ("false", None, 0, True)],
        "annotation": [("annotations", "cw/stack", v) for v in ("True", "TRUE", "1", "yes", "")],
        "securityContext": [("spec", "securityContext", v) for v in (
            {"runAsNonRoot": True, "runAsUser": 0, "seccompProfile": {"type": "RuntimeDefault"}},
            {"runAsNonRoot": True, "runAsUser": 1000, "seccompProfile": {"type": "Unconfined"}},
            {"runAsNonRoot": True, "runAsUser": 1000, "seccompProfile": {"type": "runtimedefault"}},
            {"runAsNonRoot": False, "runAsUser": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
            {"runAsNonRoot": True, "runAsUser": "1000", "seccompProfile": {"type": "RuntimeDefault"}},
            {"runAsNonRoot": True, "runAsUser": 1000})],
    }

    def test_every_variant_is_refused(self):   # kills: one comparison of the row dropped (each member has its own cases)
        for member, cases in self.VARIANTS.items():
            for where, key, v in cases:
                with self.subTest(member=member, value=v):
                    pod = build(_args(stack=True))
                    (pod["metadata"]["annotations"] if where == "annotations" else pod["spec"])[key] = v
                    self.assertIn(self.ROW, cw.forbidden_pod_fields(pod))

    def test_the_docker_socket_in_any_case_is_refused(self):
        for path in ("/var/run/Docker.sock", "/run/DOCKER.SOCK"):
            pod = build(_args(stack=True))
            pod["spec"]["containers"][0]["volumeMounts"].append({"name": "s", "mountPath": path})
            with self.subTest(path=path):
                self.assertIn("docker.sock", cw.forbidden_pod_fields(pod))

    def test_a_runtime_class_alone_makes_it_a_stack_pod(self):   # kills: stack detected by the annotation only
        pod = build(_args(stack=False))
        pod["spec"]["runtimeClassName"] = "sysbox-runc"
        self.assertIn(self.ROW, cw.forbidden_pod_fields(pod))


def _freeze(pod):
    return json.dumps(pod, indent=2, sort_keys=True)


STACK_CASES = {"stack": (_args(stack=True), ()),
               "stack_shape_m_run_secrets_pull": (_args(stack=True, shape="M", run_id="abcdef0123456789", pull_secret="regcred"), ("deploy-key", "npm-token-file"))}


class StackPodFrozen(unittest.TestCase):
    """The pod `create --stack` builds is pinned like the default pod: ANY added container, field, volume or env fails here.
    A deliberate change to the stack pod updates tests/fixtures/stack_pod.json (regenerate with the command in the README)
    in the same commit as the constant or builder edit. An image by TAG is accepted by design: the operator picks the image."""
    PATH = os.path.join(HERE, "fixtures", "stack_pod.json")

    def built(self):
        return {k: build(a, n) for k, (a, n) in STACK_CASES.items()}

    def test_the_stack_pod_equals_the_frozen_fixture(self):
        if os.environ.get("CW_WRITE_STACK_FIXTURE"):
            with open(self.PATH, "w") as f:
                f.write(json.dumps(self.built(), indent=2, sort_keys=True) + "\n")
        with open(self.PATH) as f:
            stored = f.read()
        self.assertEqual(json.dumps(self.built(), indent=2, sort_keys=True) + "\n", stored)

    def test_the_fixture_is_clean_and_is_a_stack_pod(self):   # the frozen file itself is never a forbidden pod
        with open(self.PATH) as f:
            for k, pod in json.load(f).items():
                with self.subTest(case=k):
                    self.assertEqual(cw.forbidden_pod_fields(pod), [])
                    self.assertEqual(pod["spec"]["runtimeClassName"], "sysbox-runc")


def _sidecar(p): p["spec"]["containers"].append(copy.deepcopy(p["spec"]["containers"][0]) | {"name": "side"})
def _renamed(p): p["spec"]["containers"][0]["name"] = "other"
def _init(p): p["spec"]["initContainers"] = [{"name": "i", "image": "x", "securityContext": copy.deepcopy(cw.STACK_SECURITY_CONTEXT)}]
def _ephemeral(p): p["spec"]["ephemeralContainers"] = [{"name": "e", "image": "x", "securityContext": copy.deepcopy(cw.STACK_SECURITY_CONTEXT)}]
def _psc(key, val): return lambda p: p["spec"]["securityContext"].__setitem__(key, val)
def _spec(key, val): return lambda p: p["spec"].__setitem__(key, val)
def _ctr(key, val): return lambda p: p["spec"]["containers"][0].__setitem__(key, val)
def _env(e): return lambda p: p["spec"]["containers"][0]["env"].append(e)
def _vol(v): return lambda p: p["spec"]["volumes"].append(v)
def _meta(key, val): return lambda p: p["metadata"].__setitem__(key, val)
def _unlimit(p): del p["spec"]["containers"][0]["resources"]["limits"]["memory"]
def _tokens_disk(p): [v for v in p["spec"]["volumes"] if v["name"] == "tokens"][0]["emptyDir"] = {}
def _mount(p): p["spec"]["containers"][0]["volumeMounts"].append({"name": "scratch", "mountPath": "/etc"})
def _pw_gone(p): p["spec"]["volumes"] = [v for v in p["spec"]["volumes"] if v["name"] != "pw"]
def _pw_host(p): [v for v in p["spec"]["volumes"] if v["name"] == "pw"][0]["configMap"] = {"name": "c"}
def _pw_swapped(p): p["spec"]["volumes"] = [{"name": "pw", "configMap": {"name": "c"}} if v["name"] == "pw" else v for v in p["spec"]["volumes"]]
def _no_probe(p): del p["spec"]["containers"][0]["readinessProbe"]
def _no_sa(p): p["spec"]["serviceAccountName"] = ""
def _restart(p): p["spec"]["restartPolicy"] = "Always"
def _empty_image(p): p["spec"]["containers"][0]["image"] = ""


# one case per field the reviewers named (and its sibling): each must put the pod in the allow-list row or the pod-shape row
REST_CASES = {
    "second container": _sidecar, "container renamed": _renamed, "init container": _init, "ephemeral container": _ephemeral,
    "fsGroup 0": _psc("fsGroup", 0), "runAsGroup 0": _psc("runAsGroup", 0), "supplementalGroups [0]": _psc("supplementalGroups", [0]),
    "sysctls": _psc("sysctls", [{"name": "kernel.shm_rmid_forced", "value": "1"}]), "seLinuxOptions": _psc("seLinuxOptions", {"type": "spc_t"}),
    "nodeName": _spec("nodeName", "n1"), "serviceAccountName removed": _no_sa, "hostAliases": _spec("hostAliases", [{"ip": "1.2.3.4", "hostnames": ["x"]}]),
    "tolerations": _spec("tolerations", [{"operator": "Exists"}]), "shareProcessNamespace": _spec("shareProcessNamespace", True),
    "dnsPolicy": _spec("dnsPolicy", "None"), "dnsConfig": _spec("dnsConfig", {"nameservers": ["1.1.1.1"]}),
    "priorityClassName": _spec("priorityClassName", "system-node-critical"), "hostname": _spec("hostname", "h"),
    "restartPolicy": _restart, "extra emptyDir": _vol({"name": "x", "emptyDir": {}}), "extra secret volume": _vol({"name": "x", "secret": {"secretName": "s"}}),
    "extra configMap volume": _vol({"name": "x", "configMap": {"name": "c"}}), "pw volume gone": _pw_gone, "pw volume with a configMap": _pw_host, "pw volume swapped for a configMap": _pw_swapped,
    "tokens volume on disk": _tokens_disk, "secretKeyRef env": _env({"name": "K", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}),
    "envFrom a secret": _ctr("envFrom", [{"secretRef": {"name": "s"}}]), "hostPort": _ctr("ports", [{"containerPort": 1, "hostPort": 1}]),
    "command changed": _ctr("command", ["sh"]), "args": _ctr("args", ["-c"]), "limits removed": _unlimit, "resources removed": _ctr("resources", {}),
    "readiness probe removed": _no_probe, "mount path": _mount, "empty image": _empty_image, "workingDir": _ctr("workingDir", "/"),
    "lifecycle": _ctr("lifecycle", {"postStart": {}}), "extra metadata key": _meta("ownerReferences", []),
}


class StackRestAllowList(unittest.TestCase):
    def test_every_named_field_is_refused(self):   # kills: any part of _stack_rest_differs / the whole-context equality dropped
        for name, mutate in REST_CASES.items():
            with self.subTest(field=name):
                pod = build(_args(stack=True))
                mutate(pod)
                self.assertTrue({cw_row for cw_row in cw.forbidden_pod_fields(pod)} & {REST, "stack pod shape differs", "stack security context differs from STACK_SECURITY_CONTEXT"}, name)

    def test_the_same_pod_is_fine_without_the_stack(self):   # the rows are for stack pods only (the default pod is pinned by U1)
        pod = build(_args(stack=False))
        _spec("tolerations", [{"operator": "Exists"}])(pod)
        self.assertEqual(cw.forbidden_pod_fields(pod), [])

    def test_render_refuses_a_stack_builder_that_sets_an_unlisted_key(self):   # kills: the check in render_pod removed (the real path)
        orig = cw.STACK_SPEC_KEYS
        try:
            cw.STACK_SPEC_KEYS = orig - {"nodeSelector"}   # the builder sets a key the allow-list no longer allows
            with self.assertRaises(cw.CloudError):
                build(_args(stack=True))
        finally:
            cw.STACK_SPEC_KEYS = orig


class StackCreateObjects(FreshNodeRace):
    """The objects a create makes BESIDES the pod (listed from `go`: the password Secret `<pod>-pw`, the run Secret `<pod>-run`
    with its labels, and the run record, which `stop` writes under the operator's own record dir and is not stack-specific)
    are the same for a stack create and a default create: pinned by equality, and to the frozen fixture."""
    PATH = os.path.join(HERE, "fixtures", "stack_create_objects.json")

    def objects(self, *extra):
        import os as _os
        fds = []
        for v in ("model-value-0123456789", "git-value-0123456789"):
            r, w = _os.pipe(); _os.write(w, v.encode()); _os.close(w); fds.append(r)
        try:
            c, _, err = self.run_cli("create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--run-id", self.RUN_ID,
                                     "--pull-secret", "regcred", "--run-secret-fd", "model-key=%d" % fds[0], "--run-secret-fd", "git-key=%d" % fds[1], *extra)
        finally:
            for f in fds:
                try: _os.close(f)
                except OSError: pass
        self.assertEqual(c, 0, err)
        st = self.st()
        applied = [o for o in st["applied"] if o["kind"] != "Pod"]
        for o in applied:
            if "stringData" in o:
                o["stringData"] = {k: "<password>" for k in o["stringData"]}
        return {"applied": applied, "secrets": sorted(st.get("secrets", {})), "secret_labels": st.get("secret_labels", {}),
                "secret_key_names": {k: sorted(v) for k, v in st.get("secret_keys", {}).items()}}

    def test_a_stack_create_makes_the_same_other_objects_as_a_default_create(self):
        stack = self.objects("--stack")
        self.write_state({"pods": {}})
        self.assertEqual(stack, self.objects())

    def test_the_other_objects_equal_the_frozen_fixture(self):
        got = json.dumps(self.objects("--stack"), indent=2, sort_keys=True) + "\n"
        if os.environ.get("CW_WRITE_STACK_FIXTURE"):
            with open(self.PATH, "w") as f:
                f.write(got)
        with open(self.PATH) as f:
            self.assertEqual(got, f.read())


if __name__ == "__main__":
    unittest.main()
