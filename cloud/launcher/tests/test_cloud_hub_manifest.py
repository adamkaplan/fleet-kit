"""cloud/k8s/cloud-hub.yaml (ask #1161): offline parse, kind list, label on every object, empty Secret, shared names."""
import copy, json, os, re, sys, unittest

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
        with open(PATH) as fh:
            self.text = fh.read()
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
        self.assertEqual(set(env), {"FLEET_CLOUD_CHANNEL_LISTEN", "FLEET_CLOUD_HUB_AGENTS", "FLEET_CLOUD_HUB_SECRETS", "FLEET_CLOUD_HUB_DECISIONS", "FLEET_CLOUD_AGENT_NAMESPACE"})
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


def _read(*parts):
    with open(os.path.join(HERE, "..", "..", "..", *parts)) as fh:
        return fh.read()


def section_4c(text):
    s = text.index("## 4c.")
    return text[s:text.index("\n## ", s + 5)]


PLAIN_APPLY = "kubectl -n cw apply -f cloud/k8s/cloud-hub.yaml"


def docs_problems(sec):
    out = []
    if "kubectl apply -f cloud/k8s/cloud-hub.yaml" in sec or PLAIN_APPLY in sec:
        out.append("plain apply")
    for need in ("kubectl create -f cloud/k8s/cloud-hub.yaml", "set image", "AlreadyExists",
                 "deregisters every live agent", "delete deployment,service -l", "@sha256:",
                 ".svc.cluster.local", "created (server dry run)", "get deployment,service,secret"):
        if need not in sec:
            out.append("missing " + need)
    return out


class Mut(unittest.TestCase):
    def setUp(self):
        with open(PATH) as fh:
            self.docs = parse(fh.read())

    def dep(self, docs):
        return [d for d in docs if d["kind"] == "Deployment"][0]

    def bad(self, docs):
        d = self.dep(docs)
        sp = d["spec"]["template"]["spec"]
        c = sp["containers"][0]
        out = []
        if sp.get("hostNetwork") or sp.get("hostPID") or sp.get("hostIPC"):
            out.append("host")
        if c["securityContext"].get("privileged"):
            out.append("privileged")
        if c["securityContext"].get("capabilities") != {"drop": ["ALL"]}:
            out.append("caps")
        if sp["securityContext"].get("runAsUser") in (0, None):
            out.append("root")
        if c["securityContext"].get("runAsUser") == 0:
            out.append("root")
        if c["securityContext"].get("allowPrivilegeEscalation") is not False:
            out.append("escalation")
        if any("hostPath" in v for v in sp["volumes"]):
            out.append("hostpath")
        if sp.get("automountServiceAccountToken") is not False:
            out.append("token")
        for s in docs:
            if s["kind"] == "Service" and s["metadata"]["name"] == "cloud-hub" and s["spec"].get("type") != "ClusterIP":
                out.append("svctype")
        return out

    def test_clean(self):
        self.assertEqual(self.bad(self.docs), [])

    def mutant(self, fn, want):
        docs = copy.deepcopy(self.docs)
        fn(docs)
        self.assertNotEqual(docs, self.docs)
        self.assertIn(want, self.bad(docs))

    def test_mutants(self):
        def sp(d): return self.dep(d)["spec"]["template"]["spec"]
        def cs(d): return sp(d)["containers"][0]["securityContext"]
        self.mutant(lambda d: sp(d).update(hostNetwork=True), "host")
        self.mutant(lambda d: cs(d).update(privileged=True), "privileged")
        self.mutant(lambda d: cs(d)["capabilities"].update(add=["NET_ADMIN"]), "caps")
        self.mutant(lambda d: sp(d)["securityContext"].update(runAsUser=0), "root")
        self.mutant(lambda d: cs(d).update(allowPrivilegeEscalation=True), "escalation")
        self.mutant(lambda d: sp(d)["volumes"].append({"name": "h", "hostPath": {"path": "/"}}), "hostpath")
        self.mutant(lambda d: sp(d).update(automountServiceAccountToken=True), "token")
        for ty in ("LoadBalancer", "NodePort"):
            def f(d, ty=ty):
                [x for x in d if x["kind"] == "Service" and x["metadata"]["name"] == "cloud-hub"][0]["spec"]["type"] = ty
            self.mutant(f, "svctype")

    def test_path_and_nested_pw_mount(self):
        c = self.dep(self.docs)["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(c["command"][1], "/usr/local/bin/cloud_hub.py")
        self.assertIn("/usr/local/bin/", _read("cloud", "worker", "Dockerfile"))
        self.assertIn("cloud_hub.py", _read("cloud", "worker", "Dockerfile"))

    def test_header_names_network_policy_ask(self):
        self.assertIn("ask #1256 (cloud/k8s/cloud-hub-network.yaml)", _read("cloud", "k8s", "cloud-hub.yaml"))


class Docs(unittest.TestCase):
    def setUp(self):
        self.sec = section_4c(_read("docs", "cloud-operator-quickstart.md"))

    def test_docs_ok(self):
        self.assertEqual(docs_problems(self.sec), [])
        self.assertIn("ask #1256 (cloud/k8s/cloud-hub-network.yaml)", self.sec)

    def test_channel_doc(self):
        t = _read("docs", "cloud-channel.md")
        self.assertIn("deregisters every live agent", t)
        self.assertIn("kubectl create -f cloud/k8s/cloud-hub.yaml", t)
        self.assertIn("cw.svc.cluster.local", t)

    def test_mutant_plain_apply_fails(self):
        mut = self.sec.replace("kubectl create -f cloud/k8s/cloud-hub.yaml", PLAIN_APPLY)
        self.assertNotEqual(mut, self.sec)
        self.assertIn("plain apply", docs_problems(mut))

    def test_mutant_no_dereg_sentence_fails(self):
        mut = self.sec.replace("deregisters every live agent", "x")
        self.assertIn("missing deregisters every live agent", docs_problems(mut))

    def test_mutant_no_digest_rule_fails(self):
        mut = self.sec.replace("@sha256:", "")
        self.assertTrue(docs_problems(mut))

    def test_mutant_svc_short_form_fails(self):
        mut = self.sec.replace(".svc.cluster.local", ".svc")
        self.assertTrue(docs_problems(mut))


def namespace_setting_problems(docs, channel_doc, quickstart):
    """The places that must name FLEET_CLOUD_AGENT_NAMESPACE with the manifest's own namespace as its value."""
    dep = [d for d in docs if d["kind"] == "Deployment"][0]
    env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    ns = dep["metadata"]["namespace"]
    bad = []
    if env.get("FLEET_CLOUD_AGENT_NAMESPACE") != ns:
        bad.append("manifest")
    for label, text in (("channel doc", channel_doc), ("quickstart", quickstart)):
        if not re.search(r"FLEET_CLOUD_AGENT_NAMESPACE`[^.]{0,40}`%s`" % re.escape(ns), text):
            bad.append(label)
    return bad


class NamespaceSetting(unittest.TestCase):
    def setUp(self):
        with open(PATH) as fh:
            self.docs = parse(fh.read())
        root = os.path.join(HERE, "..", "..", "..", "docs")
        with open(os.path.join(root, "cloud-channel.md")) as fh:
            self.channel = fh.read()
        with open(os.path.join(root, "cloud-operator-quickstart.md")) as fh:
            self.quick = fh.read()

    def test_namespace_setting_documented(self):
        self.assertEqual(namespace_setting_problems(self.docs, self.channel, self.quick), [])

    def test_namespace_setting_matches_manifest_namespace(self):
        for d in self.docs:
            if d["kind"] == "Deployment":
                env = d["spec"]["template"]["spec"]["containers"][0]["env"]
                self.assertEqual({e["name"]: e["value"] for e in env}["FLEET_CLOUD_AGENT_NAMESPACE"], d["metadata"]["namespace"])
        wrong = copy.deepcopy(self.docs)
        removed = copy.deepcopy(self.docs)
        for docs, change in ((wrong, "set"), (removed, "drop")):
            for d in docs:
                if d["kind"] == "Deployment":
                    c = d["spec"]["template"]["spec"]["containers"][0]
                    if change == "set":
                        for e in c["env"]:
                            if e["name"] == "FLEET_CLOUD_AGENT_NAMESPACE":
                                e["value"] = "other-" + "agents"
                    else:
                        c["env"] = [e for e in c["env"] if e["name"] != "FLEET_CLOUD_AGENT_NAMESPACE"]
            self.assertEqual(namespace_setting_problems(docs, self.channel, self.quick), ["manifest"], change)


if __name__ == "__main__":
    unittest.main()
