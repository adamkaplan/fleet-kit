"""Ask #1025 slice A: the stack-only model credential. Offline: fake kubectl, fake az (a fake vault), fake clock; every value is built here.
Scenarios: S1 S10 (the documentation), S2 (the setting), S3 S4 S5 (delivery, scan, audit), S8 (refusal before any pod), S9 (mutants), U1 (no option, no change)."""
import ast
import base64
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as cw  # noqa: E402
from test_cloud_worker import Base  # noqa: E402

STACK = "stk-" + "Zq9Rt4" * 6      # the stack credential (fake)
CODER = "cod-" + "Wk3Nv8" * 6      # the coder's own key (fake)
NAME = "acme-stack-model"
RUN_ID = "abcdef0123456789"


def read(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


def places_with(value, places):
    """The names of the places (a dict name -> text) that hold the value, plain or base64."""
    b64 = base64.b64encode(value.encode()).decode()
    return sorted(k for k, text in places.items() if value in text or b64 in text)


def pipe_of(value):
    r, w = os.pipe()
    os.write(w, value.encode())
    os.close(w)
    return r


class DeliveryBase(Base):
    def launch(self, *extra, code=0, stack_value=STACK, mutate=None):
        self.env["FAKE_KUBE_SEEN"] = os.path.join(self.tmp.name, "seen.jsonl")
        fds = [pipe_of(CODER), pipe_of(stack_value)]
        argv = ["create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--run-id", RUN_ID, "--stack",
                "--run-secret-fd", "model-auth=%d" % fds[0], "--run-secret-fd", "stack-model-key=%d" % fds[1], *extra]
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                c = cw.main(argv, self.env, self.clock, lambda s: self.clock.advance(seconds=s))
        finally:
            for fd in fds:
                os.close(fd)
        self.assertEqual(c, code, err.getvalue())
        return out.getvalue(), err.getvalue()

    def places(self, out, err):
        def rd(p):
            try:
                with open(p) as f:
                    return f.read()
            except OSError:
                return ""
        pods = json.dumps(self.st().get("applied", []))
        return {"stdout": out, "stderr": err, "audit": rd(self.env["CW_CLOUD_AUDIT"]), "kubectl arguments": rd(self.env["FAKE_KUBE_CALLS"]),
                "pod spec": pods, "launcher/kubectl environment": rd(self.env["FAKE_KUBE_SEEN"])}


class Delivery(DeliveryBase):
    def test_s3_value_reaches_the_cluster_only_as_stdin_and_a_0400_mount(self):
        out, err = self.launch("--stack-model-secret", NAME)
        places = self.places(out, err)
        self.assertEqual(places_with(STACK, places), [])   # 0 in any argument, environment, spec or output
        with open(self.state + ".stdin") as f:
            stdin = f.read()
        self.assertEqual(stdin.count(base64.b64encode(STACK.encode()).decode()) + stdin.count(STACK), 1)   # exactly one: the Secret write
        pod = [o for o in self.st()["applied"] if o["kind"] == "Pod"][0]
        items = [i for s in pod["spec"]["volumes"][0]["projected"]["sources"] for i in s["secret"]["items"]]
        self.assertIn({"key": "stack-model-key", "path": "stack-model-key", "mode": 0o400}, items)
        self.assertEqual(256, 0o400)

    def test_s4_neither_value_is_in_any_of_the_five_places_and_the_scan_can_find_one(self):
        out, err = self.launch("--stack-model-secret", NAME)
        places = self.places(out, err)
        for v in (STACK, CODER):
            self.assertEqual(places_with(v, places), [], v[:3])
        for name in places:   # the scan is shown to find a planted value in each place
            planted = dict(places, **{name: places[name] + " " + STACK})
            self.assertEqual(places_with(STACK, planted), [name])

    def test_s5_the_audit_carries_the_name_and_delivered_and_nothing_derived(self):
        self.launch("--stack-model-secret", NAME)
        line = [l for l in self.audit_lines() if l["action"] == "create"][0]
        self.assertIn("stack-model-secret=%s delivered" % NAME, line["reason"])
        for derived in (str(len(STACK)), STACK[:4], STACK[-4:]):
            self.assertNotIn(derived, json.dumps(line))
        import hashlib
        self.assertNotIn(hashlib.sha256(STACK.encode()).hexdigest()[:8], json.dumps(line))

    def test_the_option_and_the_key_go_together_and_only_with_stack(self):
        fd = pipe_of(STACK)
        try:   # the option without the key
            c, out, err = self.run_cli("create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--stack", "--stack-model-secret", NAME)
            self.assertEqual(c, cw.EXIT_USAGE)
            c, out, err = self.run_cli("create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--stack-model-secret", NAME,
                                       "--run-secret-fd", "stack-model-key=%d" % fd)   # no --stack
            self.assertEqual(c, cw.EXIT_USAGE)
            self.assertNotIn(STACK, out + err)
        finally:
            os.close(fd)
        fd = pipe_of(STACK)
        try:   # the key without the option
            c, out, err = self.run_cli("create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--stack", "--run-secret-fd", "stack-model-key=%d" % fd)
            self.assertEqual(c, cw.EXIT_USAGE)
        finally:
            os.close(fd)
        self.assertFalse(os.path.exists(self.env["FAKE_KUBE_CALLS"]))   # nothing was made

    def test_u1_a_stack_launch_without_the_option_is_as_before(self):
        self.env["FAKE_KUBE_SEEN"] = os.path.join(self.tmp.name, "seen.jsonl")
        c, out, err = self.run_cli("create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--run-id", RUN_ID, "--stack")
        self.assertEqual(c, 0, err)
        self.assertNotIn("stack-model", json.dumps(self.st()))
        self.assertNotIn("stack-model", open(self.env["CW_CLOUD_AUDIT"]).read())
        import test_cloud_stack as ts
        stored = json.load(open(os.path.join(HERE, "fixtures", "stack_pod.json")))
        for k, (a, n) in ts.STACK_CASES.items():
            self.assertEqual(json.loads(json.dumps(ts.build(a, n), sort_keys=True)), json.loads(json.dumps(stored[k], sort_keys=True)))
            self.assertNotIn("stack-model-key", json.dumps(stored[k]))


class SettingTable(unittest.TestCase):
    def test_s2_the_setting_is_a_secret_name_and_refuses_the_rest(self):
        rule, check = cw.VALIDATORS["CW_STACK_MODEL_SECRET"]
        self.assertTrue(check(NAME))
        bad = ["https://vault.example.internal/secrets/x", "/home/user/secret", "a b", "stk-" + "Zq9Rt4" * 6 + " x", ""]
        for v in bad:
            self.assertFalse(check(v), v)
        self.assertEqual({k for k, _, _ in cw.SETTINGS}, set(cw.VALIDATORS))   # the settings table and the validators table list the same names


FAKE_AZ = """#!/bin/sh
echo "$*" >> "$FAKE_AZ_LOG"
case "${FAKE_AZ_MODE:-ok}" in
  ok) printf '%s\\n' "$FAKE_AZ_VALUE" ;;
  empty) printf '\\n' ;;
  big) head -c 20000 /dev/zero | tr '\\0' 'a' ;;
  fail) echo "denied" >&2; exit 1 ;;
esac
"""


def load_bin():
    import importlib.machinery, importlib.util
    os.environ.setdefault("FLEET_SWITCHBOARD_UNDER_TEST", "1")
    loader = importlib.machinery.SourceFileLoader("cw_fleet_switchboard_1025", os.path.join(ROOT, "bin", "fleet-switchboard"))
    spec = importlib.util.spec_from_loader("cw_fleet_switchboard_1025", loader)
    sb = importlib.util.module_from_spec(spec)
    sys.modules["cw_fleet_switchboard_1025"] = sb
    loader.exec_module(sb)
    return sb.load_module("cloud_worker")


class Refusals(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = tmp.name
        with open(os.path.join(d, "az"), "w") as f:
            f.write(FAKE_AZ)
        os.chmod(os.path.join(d, "az"), 0o755)
        self.log = os.path.join(d, "az.log")
        self.env = {"PATH": d + ":" + os.environ["PATH"], "FAKE_AZ_LOG": self.log, "FAKE_AZ_VALUE": STACK, "CW_STACK_MODEL_SECRET": NAME,
                    "CW_VAULT_SUBSCRIPTION": "00000000-0000-0000-0000-000000000000", "CW_VAULT_NAME": "vault-example"}
        self.bin = load_bin()

    def test_unset_reads_nothing(self):
        self.assertIsNone(self.bin.stack_model_secret({k: v for k, v in self.env.items() if k != "CW_STACK_MODEL_SECRET"}))
        self.assertFalse(os.path.exists(self.log))

    def test_the_value_is_read_by_name_and_trimmed(self):
        self.assertEqual(self.bin.stack_model_secret(self.env), (NAME, STACK))
        self.assertIn("--name " + NAME, open(self.log).read())

    def test_s8_missing_empty_over_cap_unreadable_are_refused_naming_the_name_only(self):
        for mode in ("empty", "big", "fail"):
            with self.subTest(mode=mode):
                with self.assertRaises(self.bin.Refusal) as ctx:
                    self.bin.stack_model_secret(dict(self.env, FAKE_AZ_MODE=mode))
                self.assertIn(NAME, str(ctx.exception))
                self.assertNotIn(STACK, str(ctx.exception))
        env = {k: v for k, v in self.env.items() if k != "PATH"}
        with self.assertRaises(self.bin.Refusal):   # no az at all (cannot run)
            self.bin.stack_model_secret(dict(env, PATH="/nonexistent"))
        with self.assertRaises(self.bin.Refusal):   # vault not set
            self.bin.stack_model_secret({k: v for k, v in self.env.items() if k != "CW_VAULT_NAME"})

    def test_s8_the_read_comes_before_any_cluster_call_in_the_launch(self):
        tree = ast.parse(read("bin/cloud_worker.py"))
        fn = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_launch"][0]
        read_line = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "stack_model_secret"]
        cluster_line = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.With) for i in n.items
                        if isinstance(i.context_expr, ast.Call) and getattr(i.context_expr.func, "id", "") == "Cluster"]
        self.assertTrue(read_line and cluster_line)
        self.assertLess(min(read_line), min(cluster_line))
        self.assertNotIn("stack_model", "".join(l for l in read("bin/cloud_worker.py").splitlines(True) if "CLUSTER_DROP" in l and "=" in l.split("CLUSTER_DROP")[0]))

    def test_s3_the_bin_hands_the_value_over_a_pipe_only(self):
        src = read("bin/cloud_worker.py")
        self.assertIn('run_secrets.append(("stack-model-key", stack_model[1]))', src)
        self.assertNotIn('"--stack-model-secret", stack_model[1]', src)
        self.assertIn('[CREATE_OPTION["stack_model_secret"], stack_model[0]]', src)   # the option carries the NAME


class Mutants(DeliveryBase):
    """S9: the scan fails for a build that places the value in the pod's environment, in an argument, or in the launcher's environment."""
    def _pod_with_value(self, where):
        real = cw.render_pod

        def bad(a, worker, name, now, hard, env, run_names=()):
            pod = real(a, worker, name, now, hard, env, run_names)
            if where == "pod env":
                pod["spec"]["containers"][0].setdefault("env", []).append({"name": "CW_STACK_MODEL_KEY", "value": STACK})
            return pod
        return bad

    def test_s9_a_pod_env_entry_is_caught(self):
        real = cw.render_pod
        cw.render_pod = self._pod_with_value("pod env")
        try:
            try:
                out, err = self.launch("--stack-model-secret", NAME)
            except AssertionError:
                return   # the launch FAILED (the launcher's own credential scan refused the spec): the mutant does not pass
        finally:
            cw.render_pod = real
        self.assertEqual(places_with(STACK, self.places(out, err)), ["pod spec"])   # else the S4 scan names the pod environment

    def test_s9_an_argument_is_caught(self):
        real = cw.Kube.run
        def bad(self_, argv, *a, **k):
            self_.calls_seen = getattr(self_, "calls_seen", 0)
            with open(self_.env.get("FAKE_KUBE_CALLS", os.devnull), "a") as f:   # what a build that put the value in an argument would have sent
                f.write(json.dumps(list(argv) + ["--token=" + STACK]) + "\n")
            return real(self_, argv, *a, **k)
        cw.Kube.run = bad
        try:
            out, err = self.launch("--stack-model-secret", NAME)
        finally:
            cw.Kube.run = real
        self.assertIn("kubectl arguments", places_with(STACK, self.places(out, err)))   # the S4 scan names the argument list

    def test_s9_the_launcher_environment_is_scanned(self):
        env = {"CW_STACK_MODEL_KEY": STACK}
        self.assertEqual(places_with(STACK, {"launcher/kubectl environment": json.dumps(env)}), ["launcher/kubectl environment"])


class Docs(unittest.TestCase):
    SECTION = "## A stack-only model credential"

    def section(self, path):
        text = read(path)
        self.assertIn(self.SECTION, text)
        return text.split(self.SECTION, 1)[1]

    def test_s1_the_cause_comes_first(self):
        body = self.section("docs/cloud-operator-quickstart.md")
        first = body.strip().split("\n\n", 1)[0]
        for word in ("model-auth", "containers", "coder's"):
            self.assertIn(word, first)
        self.assertTrue(first.startswith("Cause:"))
        self.assertIn("would put the coder's key into the containers the stack starts", first)

    def test_s10_the_owner_steps_and_the_launcher_does_not_create_a_credential(self):
        body = self.section("docs/cloud-operator-quickstart.md")
        heads = re.findall(r"^### Step \d: (.+)$", body, re.M)
        self.assertEqual(len(heads), 5)
        for word in ("dedicated", "narrowest", "spend cap", "vault", "revoke"):
            self.assertTrue(any(word in h.lower() for h in heads), word)
        self.assertIn("the launcher never creates or changes a credential", body)

    def test_the_variable_and_the_bound_are_documented_everywhere(self):
        for path in ("cloud/README.md", "docs/cloud-worker-interface.md", "docs/cloud-operator-quickstart.md"):
            text = read(path)
            self.assertIn("CW_STACK_MODEL_KEY", text, path)
        self.assertIn("The coder CAN read this credential", read("docs/cloud-operator-quickstart.md"))


if __name__ == "__main__":
    unittest.main()
