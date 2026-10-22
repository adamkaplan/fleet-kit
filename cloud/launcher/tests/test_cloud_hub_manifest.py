"""cloud/k8s/cloud-hub.yaml (ask #1161): offline parse, kind list, label on every object, empty Secret, shared names."""
import json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import hub_registry as hr  # noqa: E402

PATH = os.path.join(HERE, "..", "..", "k8s", "cloud-hub.yaml")
KINDS = {"Deployment", "Service", "Secret"}


def parse(text):
    body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    return [json.loads(p) for p in body.split("---") if p.strip()]


def kinds_bad(docs):
    return [d.get("kind") for d in docs if d.get("kind") not in KINDS]


class T(unittest.TestCase):
    def setUp(self):
        self.text = open(PATH).read()
        self.docs = parse(self.text)

    def test_kinds(self):
        self.assertEqual(sorted(d["kind"] for d in self.docs), ["Deployment", "Secret", "Secret", "Service", "Service"])
        self.assertEqual(sorted(d["metadata"]["name"] for d in self.docs if d["kind"] == "Service"), ["cloud-hub", "cw-agents"])
        self.assertEqual(sorted(d["metadata"]["name"] for d in self.docs if d["kind"] == "Secret"), ["cloud-hub-pw", "cloud-hub-registry"])
        self.assertEqual(kinds_bad(self.docs), [])

    def test_mutant_clusterrole_fails(self):
        self.assertEqual(kinds_bad(self.docs + [{"kind": "ClusterRole"}]), ["ClusterRole"])

    def test_label_and_namespace(self):
        k, v = hr.HUB_LABEL
        for d in self.docs:
            self.assertEqual(d["metadata"]["labels"][k], v)
            self.assertEqual(d["metadata"]["namespace"], "cw")

    def test_secret_only_empty_objects(self):
        s = [d for d in self.docs if d["kind"] == "Secret" and d["metadata"]["name"] == hr.HUB_REGISTRY_SECRET][0]
        pw = [d for d in self.docs if d["kind"] == "Secret" and d["metadata"]["name"] == "cloud-hub-pw"][0]
        self.assertFalse(pw.get("stringData")); self.assertFalse(pw.get("data"))
        self.assertEqual(s["metadata"]["name"], hr.HUB_REGISTRY_SECRET)
        self.assertEqual(set(s["stringData"]), {hr.HUB_SECRETS_KEY, hr.HUB_AGENTS_KEY})
        for v in s["stringData"].values():
            self.assertEqual(json.loads(v), {})
        self.assertNotIn("data", s)

    def test_deployment_shape(self):
        d = [x for x in self.docs if x["kind"] == "Deployment"][0]
        sp = d["spec"]["template"]["spec"]
        c = sp["containers"][0]
        self.assertEqual(d["spec"]["replicas"], 1)
        self.assertEqual(sp["imagePullSecrets"], [{"name": "cw-pull"}])
        self.assertIs(sp["automountServiceAccountToken"], False)
        self.assertTrue(sp["securityContext"]["runAsNonRoot"])
        self.assertEqual(c["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertIn("memory", c["resources"]["requests"])
        self.assertIn("memory", c["resources"]["limits"])
        self.assertEqual(c["image"], "IMAGE_DIGEST")
        self.assertEqual(c["command"], ["python3", "/usr/local/bin/cloud_hub.py"])
        self.assertNotRegex(self.text, r"sha256:[0-9a-f]{64}")
        env = {e["name"]: e["value"] for e in c["env"]}
        self.assertEqual(set(env), {"FLEET_CLOUD_CHANNEL_LISTEN", "FLEET_CLOUD_HUB_AGENTS", "FLEET_CLOUD_HUB_SECRETS", "FLEET_CLOUD_HUB_DECISIONS"})
        self.assertEqual(env["FLEET_CLOUD_HUB_DECISIONS"], "/var/hub/decisions.json")
        self.assertIs(c["securityContext"]["readOnlyRootFilesystem"], True)
        self.assertIs(c["securityContext"]["allowPrivilegeEscalation"], False)
        self.assertIsInstance(sp["securityContext"]["runAsUser"], int)
        self.assertEqual(sp["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        self.assertIn("cpu", c["resources"]["requests"])
        self.assertIn("cpu", c["resources"]["limits"])
        self.assertEqual(sp["securityContext"]["fsGroup"], sp["securityContext"]["runAsUser"])
        for v in sp["volumes"]:
            if "secret" in v:
                m = v["secret"]["defaultMode"]
                self.assertEqual(m & 0o007, 0)
                self.assertTrue(m & 0o040)
        self.assertEqual(c["readinessProbe"]["tcpSocket"]["port"], 8443)
        mounts = {m["mountPath"]: m for m in c["volumeMounts"]}
        self.assertTrue(mounts["/etc/hub/pw"]["readOnly"])
        vols = {v["name"]: v for v in sp["volumes"]}
        self.assertEqual(vols[mounts["/etc/hub/pw"]["name"]]["secret"]["secretName"], "cloud-hub-pw")
        self.assertIn("emptyDir", vols[mounts["/var/hub"]["name"]])
        self.assertEqual(env["FLEET_CLOUD_CHANNEL_LISTEN"], "0.0.0.0:8443")
        self.assertEqual(env["FLEET_CLOUD_HUB_SECRETS"], "/etc/hub/" + hr.HUB_SECRETS_KEY)
        self.assertEqual(env["FLEET_CLOUD_HUB_AGENTS"], "/etc/hub/" + hr.HUB_AGENTS_KEY)
        self.assertTrue([m for m in c["volumeMounts"] if m["mountPath"] == "/etc/hub"][0]["readOnly"])
        self.assertEqual(sp["volumes"][0]["secret"]["secretName"], hr.HUB_REGISTRY_SECRET)

    def test_service(self):
        s = [d for d in self.docs if d["kind"] == "Service" and d["metadata"]["name"] == "cloud-hub"][0]
        self.assertEqual(s["spec"]["type"], "ClusterIP")
        self.assertEqual(s["spec"]["ports"][0]["port"], 8443)
        self.assertEqual(s["spec"]["ports"][0]["name"], "http")

    def test_agents_headless_service(self):
        s = [d for d in self.docs if d["kind"] == "Service" and d["metadata"]["name"] == "cw-agents"][0]
        self.assertEqual(s["spec"]["clusterIP"], "None")
        self.assertEqual(s["spec"]["selector"], {"app": "cw-worker"})
        self.assertIs(s["spec"]["publishNotReadyAddresses"], True)
        self.assertEqual(len(s["spec"]["ports"]), 1)
        p = s["spec"]["ports"][0]
        self.assertEqual((p["port"], p["name"], p["protocol"]), (4096, "agent", "TCP"))


if __name__ == "__main__":
    unittest.main()
