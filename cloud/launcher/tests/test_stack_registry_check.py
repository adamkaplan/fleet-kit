"""Ask #1022 slice A: stack-registry-check with a recording fake az (S5, S6, S15 role half, S7 docs)."""
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402

ISSUER = "https://issuer.example.internal/acme/"
PRINCIPAL = "aaaaaaaa-0000-0000-0000-000000000001"
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ["AZ_CALLS"], "a") as f:
    f.write(json.dumps(a) + "\n")
st = json.load(open(os.environ["AZ_STATE"]))
if st.get("logged_out"):
    sys.stderr.write("Please run az login\n"); sys.exit(1)
j = " ".join(a)
if a[:2] == ["identity", "show"]:
    if st.get("no_identity"):
        sys.stderr.write("ResourceNotFound\n"); sys.exit(3)
    print(json.dumps({"principalId": st["principal"], "clientId": "c-1"}))
elif a[:3] == ["identity", "federated-credential", "list"]:
    print(json.dumps(st["fics"]))
elif a[:2] == ["acr", "show"]:
    print(json.dumps({"id": "/subscriptions/s/resourceGroups/r/providers/Microsoft.ContainerRegistry/registries/acme-registry"}))
elif a[:3] == ["role", "assignment", "list"]:
    print(json.dumps([{"roleDefinitionName": r} for r in st["roles"]]))
else:
    sys.exit(9)
'''


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        os.makedirs(os.path.join(d, "bin"))
        az = os.path.join(d, "bin", "az")
        with open(az, "w") as f:
            f.write(FAKE)
        os.chmod(az, 0o755)
        self.calls, self.state = os.path.join(d, "calls"), os.path.join(d, "state.json")
        self.env = {"PATH": os.path.join(d, "bin") + ":" + os.environ["PATH"], "AZ_CALLS": self.calls, "AZ_STATE": self.state,
                    "CW_STACK_REGISTRY": "acme-registry.azurecr.io", "CW_CLOUD_NAMESPACE": "cw"}
        os.environ.update({"AZ_CALLS": self.calls, "AZ_STATE": self.state})
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("AZ_CALLS", "AZ_STATE")])
        self.set()

    def set(self, **kw):
        st = {"principal": PRINCIPAL, "roles": ["AcrPull"], "fics": [{"issuer": ISSUER, "subject": "system:serviceaccount:cw:cw-stack-puller",
                                                                      "audiences": ["api://AzureADTokenExchange"]}]}
        st.update(kw)
        with open(self.state, "w") as f:
            json.dump(st, f)

    def run_check(self):
        a = type("A", (), {"resource_group": "rg-acme", "issuer": ISSUER, "identity": cw.PULLER_IDENTITY})()
        out = io.StringIO()
        return cw.stack_registry_check(a, self.env, out), out.getvalue()

    def test_ok_prints_the_principal_id(self):   # S5
        code, out = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn(PRINCIPAL, out)

    def test_missing_federated_credential(self):   # S5
        self.set(fics=[])
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("federated credential", out)
        self.assertIn("system:serviceaccount:cw:cw-stack-puller", out)

    def test_wrong_subject_is_missing(self):
        self.set(fics=[{"issuer": ISSUER, "subject": "system:serviceaccount:cw:cw-worker", "audiences": ["api://AzureADTokenExchange"]}])
        self.assertEqual(self.run_check()[0], 1)

    def test_missing_role(self):   # S5
        self.set(roles=[])
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("AcrPull", out)
        self.assertIn(PRINCIPAL, out)

    def test_write_capable_role_is_refused(self):   # S15
        self.set(roles=["AcrPull", "AcrPush"])
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("AcrPush", out)

    def test_missing_identity(self):
        self.set(no_identity=True)
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("owner step 1", out)

    def test_logged_out_is_cannot_tell(self):   # S5
        self.set(logged_out=True)
        code, out = self.run_check()
        self.assertEqual(code, 2)
        self.assertIn("CANNOT TELL", out)

    def test_every_az_call_is_a_read(self):   # S6
        self.set(roles=[])
        self.run_check()
        with open(self.calls) as f:
            calls = [json.loads(x) for x in f]
        self.assertGreater(len(calls), 3)
        for argv in calls:
            self.assertTrue(set(argv[:4]) & {"show", "list", "get"}, argv)
            self.assertFalse({"create", "update", "delete", "assign"} & set(argv), argv)


class Docs(unittest.TestCase):
    def test_the_documentation_has_the_five_items(self):   # S7
        text = ""
        for p in ("cloud/README.md", "docs/agent-credentials.md"):
            with open(os.path.join(ROOT, p)) as f:
                t = f.read()
            self.assertIn("cw-stack-puller", t)
            text += t
        for item in ("principal id", "system:serviceaccount:<ns>:cw-stack-puller", "api://AzureADTokenExchange", "docker` group", "stack-registry-check"):
            self.assertIn(item, text)
        sec = text[text.index("**Owner steps, in order**"):]
        positions = [sec.index(w) for w in ("1. Create", "2. Create", "3. The registry", "4. Apply")]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
