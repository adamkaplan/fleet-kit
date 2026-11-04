"""Ask #1022 slice A: the stack pod that pulls from the stack registry (scenarios S2, S3, S12, S13, S15, S16, U1, U2, U3, S11).
No cloud: the fake kubectl beside this file; sentinel secrets are assembled here at run time."""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402
from test_cloud_stack import ENV, _args, build, CASES, STACK_CASES  # noqa: E402
from test_cloud_worker import Base, T0  # noqa: E402
import datetime as dt  # noqa: E402

KEYS = {"CW_STACK_REGISTRY": "acme-registry.azurecr.io", "CW_STACK_PULL_CLIENT_ID": "11111111-2222-3333-4444-555555555555",
        "CW_STACK_PULL_TENANT_ID": "66666666-7777-8888-9999-000000000000"}
ROW = "service-account token"
FIXTURE = os.path.join(HERE, "fixtures", "stack_pod_registry.json")


def build_reg(a=None, names=(), keys=KEYS):
    return cw.render_pod(a or _args(stack=True), "coder7", cw.pod_name("coder7"), T0, T0 + dt.timedelta(hours=2), {**ENV, **keys}, names)


class S2Shape(unittest.TestCase):
    def test_equals_the_fixture_and_is_clean(self):   # S2
        got = json.dumps({"stack_registry": build_reg()}, indent=2, sort_keys=True) + "\n"
        if os.environ.get("CW_WRITE_STACK_FIXTURE"):
            with open(FIXTURE, "w") as f:
                f.write(got)
        with open(FIXTURE) as f:
            self.assertEqual(got, f.read())
        self.assertEqual(cw.forbidden_pod_fields(build_reg()), [])

    def test_the_pinned_values(self):   # S2, S15
        pod = build_reg()
        self.assertEqual(pod["spec"]["serviceAccountName"], "cw-stack-puller")
        self.assertIs(pod["spec"]["automountServiceAccountToken"], False)
        self.assertNotIn("azure.workload.identity/use", pod["metadata"]["labels"])
        src = cw._token_sources(pod)
        self.assertEqual(len(src), 1)
        self.assertEqual(src[0]["serviceAccountToken"], {"audience": "api://AzureADTokenExchange", "expirationSeconds": 600, "path": "token"})
        vol = [v for v in pod["spec"]["volumes"] if v["name"] == "registry-token"][0]
        self.assertEqual(vol["projected"]["defaultMode"], 0o400)
        mounts = [m for m in pod["spec"]["containers"][0]["volumeMounts"] if m["name"] == "registry-token"]
        self.assertEqual(mounts, [{"name": "registry-token", "mountPath": "/opt/cw-registry/token", "readOnly": True}])
        self.assertEqual(sum(1 for m in pod["spec"]["containers"][0]["volumeMounts"] if m["mountPath"] in ("/run/cw-tmp", "/run/cw") and m["name"] == "registry-token"), 0)

    def test_the_other_volumes_and_env_equal_a_stack_pod_without_the_keys(self):   # U3
        plain, reg = build(_args(stack=True)), build_reg()
        for key in ("pw", "tokens", "scratch"):
            self.assertEqual([v for v in plain["spec"]["volumes"] if v["name"] == key], [v for v in reg["spec"]["volumes"] if v["name"] == key])
        self.assertEqual(plain["spec"]["containers"][0]["env"], reg["spec"]["containers"][0]["env"])
        self.assertEqual(build(_args(stack=True), ("k1",)) ["spec"]["volumes"][0], build_reg(names=("k1",))["spec"]["volumes"][0])


def _mut(f):
    def go(p):
        f(p)
        return p
    return go


def _vol(p): return [v for v in p["spec"]["volumes"] if v["name"] == "registry-token"][0]
def _src(p): return _vol(p)["projected"]["sources"][0]["serviceAccountToken"]
def _mnt(p): return [m for m in p["spec"]["containers"][0]["volumeMounts"] if m["name"] == "registry-token"][0]


MUTANTS = {
    "second mount path": lambda p: p["spec"]["containers"][0]["volumeMounts"].append({"name": "registry-token", "mountPath": "/run/cw-tmp/t", "readOnly": True}),
    "automount true": lambda p: p["spec"].__setitem__("automountServiceAccountToken", True),
    "label present": lambda p: p["metadata"]["labels"].__setitem__("azure.workload.identity/use", "true"),
    "other audience": lambda p: _src(p).__setitem__("audience", "api://AzureADTokenExchange2"),
    "longer life": lambda p: _src(p).__setitem__("expirationSeconds", 3600),
    "third source": lambda p: _vol(p)["projected"]["sources"].append({"configMap": {"name": "c"}}),
    "other service account": lambda p: p["spec"].__setitem__("serviceAccountName", "cw-worker"),
    "mounted writable": lambda p: _mnt(p).__setitem__("readOnly", False),
    "second token source": lambda p: _vol(p)["projected"]["sources"].append({"serviceAccountToken": {"audience": "x", "path": "t2"}}),
}


class S3Guard(unittest.TestCase):
    def test_each_mutant_is_refused_by_the_row(self):   # S3
        for name, m in MUTANTS.items():
            with self.subTest(mutant=name):
                pod = build_reg()
                m(pod)
                self.assertIn(ROW, cw.forbidden_pod_fields(pod))

    def test_the_token_in_a_default_pod_is_refused(self):
        pod = build(_args())
        pod["spec"]["volumes"].append(copy.deepcopy(cw.PULLER_VOLUME))
        pod["spec"]["containers"][0]["volumeMounts"].append({"name": "registry-token", "mountPath": cw.PULLER_MOUNT_PATH, "readOnly": True})
        self.assertIn(ROW, cw.forbidden_pod_fields(pod))


def _mutated(old, new):
    """cloud_worker with ONE source line changed: shows a test would catch that mutation (the mutant accepts what the real code refuses)."""
    import types
    with open(cw.__file__) as f:
        src = f.read()
    assert src.count(old) == 1, old
    mod = types.ModuleType("cw_mutant")
    mod.__file__ = cw.__file__
    exec(compile(src.replace(old, new), cw.__file__, "exec"), mod.__dict__)
    return mod


def _with_puller(pod):
    """Give any pod the puller pieces exactly as render_pod does for a stack pod."""
    pod["spec"]["serviceAccountName"] = cw.PULLER_SERVICE_ACCOUNT
    pod["metadata"].get("labels", {}).pop(cw.PULLER_LABEL, None)
    pod["spec"]["volumes"].append(copy.deepcopy(cw.PULLER_VOLUME))
    pod["spec"]["containers"][0]["volumeMounts"].append({"name": "registry-token", "mountPath": cw.PULLER_MOUNT_PATH, "readOnly": True})
    return pod


class TokenRowStackCondition(unittest.TestCase):
    STACK_COND = '_is_stack(pod) and spec.get("serviceAccountName") == PULLER_SERVICE_ACCOUNT'

    def test_a_stack_pod_with_the_puller_pieces_passes(self):   # (c)
        self.assertEqual(cw.forbidden_pod_fields(build_reg()), [])
        self.assertEqual(cw.forbidden_pod_fields(_with_puller(build(_args(stack=True)))), [])

    def test_non_stack_pod_with_the_puller_volume_mount_and_account_is_refused(self):   # (a)
        pod = _with_puller(build(_args()))
        self.assertFalse(cw._is_stack(pod))
        self.assertIn(ROW, cw.forbidden_pod_fields(pod))

    def test_non_stack_pod_with_a_plain_token_is_refused(self):   # (b)
        pod = build(_args())
        pod["spec"]["automountServiceAccountToken"] = True
        self.assertIn(ROW, cw.forbidden_pod_fields(pod))

    def test_mutant_without_the_stack_condition_accepts_a_non_stack_pod(self):   # (a) mutant
        m = _mutated(self.STACK_COND, 'spec.get("serviceAccountName") == PULLER_SERVICE_ACCOUNT')
        pod = _with_puller(build(_args()))
        self.assertNotIn(ROW, m.forbidden_pod_fields(pod))   # the mutant accepts it ...
        self.assertIn(ROW, cw.forbidden_pod_fields(pod))     # ... the real guard refuses it

    def test_a_duplicated_puller_volume_is_refused(self):
        pod = build_reg()
        pod["spec"]["volumes"].append(copy.deepcopy(cw.PULLER_VOLUME))
        self.assertTrue(cw.forbidden_pod_fields(pod))
        self.assertIn(ROW, cw.forbidden_pod_fields(pod))

    def test_the_count_clause_is_redundant_with_the_single_source_list(self):
        # vols.count(PULLER_VOLUME) == 1 cannot be pinned alone: two identical volumes already fail the one-element list clause before it.
        m = _mutated("        and vols.count(PULLER_VOLUME) == 1\n", "")
        pod = build_reg()
        pod["spec"]["volumes"].append(copy.deepcopy(cw.PULLER_VOLUME))
        self.assertIn(ROW, m.forbidden_pod_fields(pod))

    def test_a_puller_volume_not_last_is_refused_and_the_ordering_mutant_accepts_it(self):
        pod = build_reg()
        pod["spec"]["volumes"].insert(0, pod["spec"]["volumes"].pop())
        self.assertTrue(cw.forbidden_pod_fields(pod))
        m = _mutated("        or (_has_puller_volume(pod) and vols[-1] != PULLER_VOLUME)\n", "")
        self.assertEqual(m.forbidden_pod_fields(pod), [])


class U1U2Unchanged(unittest.TestCase):
    def test_default_pod_ignores_the_keys_and_has_no_token(self):   # U1
        pod = build_reg(_args(), keys=KEYS)
        with open(os.path.join(HERE, "fixtures", "default_pod.json")) as f:
            stored = json.load(f)
        self.assertEqual(pod, stored["default"])
        self.assertIs(pod["spec"]["automountServiceAccountToken"], False)
        self.assertEqual(cw._token_sources(pod), [])

    def test_stack_without_keys_equals_the_frozen_fixture(self):   # U2
        with open(os.path.join(HERE, "fixtures", "stack_pod.json")) as f:
            stored = json.load(f)
        for k, (a, n) in STACK_CASES.items():
            self.assertEqual(build(a, n), stored[k])
            self.assertEqual(cw._token_sources(build(a, n)), [])

    def test_partial_keys_are_refused_naming_keys_not_values(self):
        for drop in KEYS:
            keys = {k: v for k, v in KEYS.items() if k != drop}
            with self.assertRaises(cw.CloudError) as c:
                build_reg(keys=keys)
            self.assertIn(drop, str(c.exception))
            self.assertEqual(c.exception.code, 2)
            self.assertNotIn("azurecr", str(c.exception))


class S13Values(unittest.TestCase):
    BAD = ["https://acme-registry.azurecr.io/", "acme-registry.azurecr.io/x", "acme-registry.azurecr.io:5000", "10.1.2.3", "azurecr.io", "acme-registry",
           "acme.example.com", "-x.azurecr.io", "Acme.azurecr.io", "user@acme.azurecr.io", "ghp_" + "A1b2C3d4E5f6G7h8" + ".azurecr.io", "127.0.0.1.azurecr.io.evil"]

    def test_good_and_bad(self):
        ok = cw.VALIDATORS["CW_STACK_REGISTRY"][1]
        self.assertTrue(ok("acme-registry.azurecr.io"))
        for v in self.BAD:
            with self.subTest(v=v):
                self.assertFalse(ok(v))
        self.assertGreaterEqual(len(self.BAD), 10)

    def test_bad_value_in_a_launch_is_refused_without_echo(self):   # S16
        for v in self.BAD[:3] + [self.BAD[10]]:
            with self.assertRaises(cw.CloudError) as c:
                build_reg(keys={**KEYS, "CW_STACK_REGISTRY": v})
            self.assertEqual(c.exception.code, 2)
            self.assertNotIn(v, str(c.exception))

    def test_guid_keys_and_all_or_none_in_the_file(self):
        ok = cw.VALIDATORS["CW_STACK_PULL_CLIENT_ID"][1]
        self.assertTrue(ok(KEYS["CW_STACK_PULL_CLIENT_ID"]))
        self.assertFalse(ok("not-a-guid"))
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cloud.env")
            base = {"CW_ALLOWED_REPOS": "acme-org/widget-service", "CW_CLOUD_IMAGE": "registry.example.internal/acme/worker@sha256:" + "ab12" * 16,
                    "CW_CLOUD_SERVER": "https://cluster.example.internal:6443", "CW_VAULT_SUBSCRIPTION": "00000000-0000-0000-0000-000000000000",
                    "CW_VAULT_NAME": "vault-example", "CW_CLOUD_NAMESPACE": "cw"}
            with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                f.write("".join("%s=%s\n" % kv for kv in {**base, "CW_STACK_REGISTRY": KEYS["CW_STACK_REGISTRY"]}.items()))
            with self.assertRaises(cw.CloudError) as c:
                cw.apply_settings({}, path)
            self.assertIn("CW_STACK_PULL_CLIENT_ID", str(c.exception))
            self.assertNotIn("azurecr", str(c.exception))
            with open(path, "w") as f:
                f.write("".join("%s=%s\n" % kv for kv in {**base, **KEYS}.items()))
            self.assertEqual(cw.apply_settings({}, path), path)

    def test_the_registry_host_is_not_read_from_the_environment_of_a_default_launch(self):   # S16
        self.assertEqual(build_reg(_args(), keys=KEYS)["spec"]["serviceAccountName"], "cw-worker")


class S11NoSecretInOutput(Base):
    SENTINEL = "sentinel-" + "exchange-" + "0123456789abcdef"

    def test_stack_create_and_stop_leak_no_sentinel(self):
        self.env.update(KEYS)
        self.env["CW_RUN_RECORD_DIR"] = os.path.join(self.tmp.name, "runs")
        self.env["CW_FAKE_SENTINEL"] = self.SENTINEL   # never read by the launcher; present in the environment like a stray value
        code, out, err = self.run_cli("create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--stack")
        self.assertEqual(code, 0, err)
        pod = self.pod()
        self.assertEqual(pod["spec"]["serviceAccountName"], "cw-stack-puller")
        code2, out2, err2 = self.run_cli("stop", "coder7")
        def slurp(path):
            with open(path) as f:
                return f.read()
        blob = out + err + out2 + err2 + json.dumps(self.st()["pods"]) + slurp(self.env["CW_CLOUD_AUDIT"])
        for root, _, files in os.walk(self.env["CW_RUN_RECORD_DIR"]):
            for f in files:
                blob += slurp(os.path.join(root, f))
        self.assertNotIn(self.SENTINEL, blob)


class S12Mutants(unittest.TestCase):
    """The pod builder mutated to mount the token for the whole pod: each mutant fails the guard test above (run as a subprocess-free check)."""
    def test_the_three_whole_pod_mutants_are_caught(self):
        def default_with_volume():
            pod = build(_args())
            pod["spec"]["volumes"].append(copy.deepcopy(cw.PULLER_VOLUME))
            pod["spec"]["containers"][0]["volumeMounts"].append({"name": "registry-token", "mountPath": cw.PULLER_MOUNT_PATH, "readOnly": True})
            return pod

        def second_path():
            pod = build_reg()
            pod["spec"]["containers"][0]["volumeMounts"].append({"name": "registry-token", "mountPath": "/work/t", "readOnly": True})
            return pod

        def label_back():
            pod = build_reg()
            pod["metadata"]["labels"]["azure.workload.identity/use"] = "true"
            return pod
        for make in (default_with_volume, second_path, label_back):
            with self.subTest(m=make.__name__):
                self.assertIn(ROW, cw.forbidden_pod_fields(make()))


if __name__ == "__main__":
    unittest.main()
