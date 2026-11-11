"""Ask #2341 (S6 and the image's files): cloud/k8s/copilot-bridge.yaml, the Dockerfile, the entrypoint and the lockfile. Offline."""
import json
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
BRIDGE = os.path.join(ROOT, "cloud", "copilot-bridge")
LAUNCHER = os.path.join(ROOT, "cloud", "launcher", "cloud_worker.py")


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def docs(text):
    body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    return [json.loads(p) for p in body.split("---") if p.strip()]


def checks(text, entry):
    """The list of problems of the manifest text and the entrypoint text (empty = good)."""
    d = docs(text)
    out = []
    kinds = sorted(x["kind"] for x in d)
    if kinds != ["Deployment", "NetworkPolicy", "Service"]:
        out.append("kinds %s" % kinds)
    dep = next(x for x in d if x["kind"] == "Deployment")
    if dep["spec"]["replicas"] != 1:
        out.append("replicas")
    if "HOST=127.0.0.1 node node_modules/copilot-api/dist/main.js start" not in entry:
        out.append("no loopback bind")
    return out


class Manifest(unittest.TestCase):
    def setUp(self):
        self.text = read("cloud", "k8s", "copilot-bridge.yaml")
        self.entry = read("cloud", "copilot-bridge", "entrypoint.sh")
        self.docs = docs(self.text)
        self.dep = next(x for x in self.docs if x["kind"] == "Deployment")
        self.pod = self.dep["spec"]["template"]["spec"]
        self.c = self.pod["containers"][0]

    def test_control_is_clean_and_the_mutants_are_caught(self):
        self.assertEqual(checks(self.text, self.entry), [])
        self.assertEqual(checks(self.text, self.entry.replace("HOST=127.0.0.1 ", "")), ["no loopback bind"])
        self.assertEqual(checks(self.text.replace('"replicas": 1', '"replicas": 2'), self.entry), ["replicas"])

    def test_one_deployment_one_replica_one_container(self):
        self.assertEqual([x["kind"] for x in self.docs], ["Deployment", "Service", "NetworkPolicy"])
        self.assertEqual(self.dep["spec"]["replicas"], 1)
        self.assertEqual(len(self.pod["containers"]), 1)
        self.assertTrue(self.c["image"].startswith("registry.invalid/"))

    def test_the_bridge_binds_the_loopback_only_and_never_shows_the_token(self):
        self.assertEqual(self.entry.count("HOST=127.0.0.1 node node_modules/copilot-api/dist/main.js start --port 4141"), 1)
        self.assertRegex(self.entry, r'--rate-limit "\$RATE" --wait')
        code = "\n".join(l for l in self.entry.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("show-token", code)   # the comments may name the option; the commands never pass it
        self.assertNotIn("show-token", "\n".join(l for l in self.text.splitlines() if not l.startswith("#")))
        self.assertEqual(self.c["ports"], [{"name": "front", "containerPort": 8080}])

    def test_init_copies_the_secret_file_with_mode_0600_into_memory(self):
        self.assertIn('install -m 0600 "$SRC" "$DIR/github_token"', self.entry)
        self.assertIn("SRC=/run/copilot/github_token", self.entry)
        self.assertIn('/.local/share/copilot-api"', self.entry)
        vols = {v["name"]: v for v in self.pod["volumes"]}
        self.assertEqual(vols["home"]["emptyDir"]["medium"], "Memory")
        self.assertEqual(vols["github-token"]["secret"]["secretName"], "copilot-github-token")
        mounts = {m["name"]: m for m in self.c["volumeMounts"]}
        self.assertEqual((mounts["github-token"]["mountPath"], mounts["github-token"]["readOnly"]), ("/run/copilot", True))
        self.assertEqual(mounts["home"]["mountPath"], "/run/home")
        self.assertIn({"name": "HOME", "value": "/run/home"}, self.c["env"])
        self.assertIn("BRIDGE_DAILY_BUDGET", [e["name"] for e in self.c["env"]])

    def test_non_root_and_no_privilege(self):
        self.assertTrue(self.pod["securityContext"]["runAsNonRoot"])
        self.assertFalse(self.c["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(self.c["securityContext"]["capabilities"], {"drop": ["ALL"]})

    def test_the_service_exposes_the_front_port_only(self):
        svc = next(x for x in self.docs if x["kind"] == "Service")
        self.assertEqual(svc["spec"]["type"], "ClusterIP")
        self.assertEqual([(p["port"], p["targetPort"]) for p in svc["spec"]["ports"]], [(8080, 8080)])

    def test_the_policy_admits_only_the_launchers_worker_label(self):
        label = re.search(r'^LABEL = "([^"]+)"', read("cloud", "launcher", "cloud_worker.py"), re.M).group(1)
        self.assertIn('"labels": {"app": LABEL,', read("cloud", "launcher", "cloud_worker.py"))   # render_pod puts app=LABEL on every worker pod
        np = next(x for x in self.docs if x["kind"] == "NetworkPolicy")
        ing = np["spec"]["ingress"]
        self.assertEqual(len(ing), 1)
        self.assertEqual(ing[0]["from"], [{"podSelector": {"matchLabels": {"app": label}}}])
        self.assertEqual(ing[0]["ports"], [{"protocol": "TCP", "port": 8080}])
        self.assertEqual(np["spec"]["policyTypes"], ["Ingress"])

    def test_no_secret_value_anywhere(self):
        self.assertNotIn("Secret", [x["kind"] for x in self.docs])
        for name in ("k8s/copilot-bridge.yaml", "copilot-bridge/Dockerfile", "copilot-bridge/entrypoint.sh", "copilot-bridge/bridge-front.mjs",
                     "copilot-bridge/package.json"):
            t = read("cloud", *name.split("/"))
            self.assertIsNone(re.search(r"gh[pousr]_[A-Za-z0-9]{8,}|github_pat_|ghu_|Bearer [A-Za-z0-9._-]{8,}|-----BEGIN", t), name)


class Image(unittest.TestCase):
    def test_dockerfile(self):
        t = read("cloud", "copilot-bridge", "Dockerfile")
        w = read("cloud", "worker", "Dockerfile")
        self.assertIn("FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55\n", t)
        for arg in ("NODE_VERSION", "NODE_SHA256_X64", "NODE_SHA256_ARM64"):   # the same pins as the worker image
            line = re.search(r"^ARG %s=.*$" % arg, w, re.M).group(0)
            self.assertIn(line + "\n", t)
        self.assertIn('echo "${h}  /tmp/${f}" | sha256sum -c -', t)
        self.assertIn("npm ci --ignore-scripts --omit=dev", t)
        self.assertEqual(re.findall(r"^EXPOSE .*$", t, re.M), ["EXPOSE 8080"])
        self.assertRegex(t, r"(?m)^USER 10001$")
        self.assertNotRegex(t, r"(?mi)^(ENV|ARG)\s+\S*(TOKEN|SECRET|KEY|PASSWORD)")

    def test_the_lockfile_pins_the_bridge_and_every_dependency_by_integrity(self):
        pkg = json.loads(read("cloud", "copilot-bridge", "package.json"))
        self.assertEqual(pkg["dependencies"], {"copilot-api": "0.7.0"})
        lock = json.loads(read("cloud", "copilot-bridge", "package-lock.json"))
        pk = {k: v for k, v in lock["packages"].items() if k}
        self.assertEqual(pk["node_modules/copilot-api"]["version"], "0.7.0")
        self.assertTrue(pk["node_modules/copilot-api"]["integrity"].startswith("sha512-"))
        self.assertGreater(len(pk), 10)
        for name, v in pk.items():
            self.assertTrue(v.get("integrity", "").startswith("sha512-"), name)
            self.assertTrue(v.get("resolved", "").startswith("https://registry.npmjs.org/"), name)
            self.assertFalse(v.get("hasInstallScript"), name + " has an install script")


if __name__ == "__main__":
    unittest.main()
