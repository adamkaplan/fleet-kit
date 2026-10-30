"""Manifests of the in-cluster reaper and its token service (ask #1582): the rights match the listed pairs, the workload is one
restarting replica with no key, and each guard has a mutant that is asserted applied. Nothing is applied to a cluster."""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_cw_minter_manifest import parse  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
REAPER = os.path.join(ROOT, "cloud", "k8s", "cw-cloud-reaper.yaml")
SERVICE = os.path.join(ROOT, "cloud", "k8s", "cw-token-service.yaml")
# The (verb, resource) pairs of the reaper's pass, written from the code: list and read pods, stop through the end hook (exec), delete,
# patch a per-run Secret, hold the lease. test_cloud_reaper_pass_pairs derives the same set from a recorded fake pass.
LISTED = {("get", "pods"), ("list", "pods"), ("patch", "pods"), ("delete", "pods"), ("create", "pods/exec"), ("get", "secrets"),
          ("patch", "secrets"), ("delete", "secrets"), ("get", "leases"), ("create", "leases"), ("update", "leases")}
FORBIDDEN = ["app key", "app-key", "app_key", "app id", "app_id", "vault", "repository token", "repo-token", "git_token",
             "personal login", "signing key", "az login"]


def read(path):
    with open(path) as f:
        return f.read()


def pairs(text):
    role = [d for d in parse(text) if d.get("kind") == "Role"][0]
    return {(v, r) for rule in role["rules"] for r in rule["resources"] for v in rule["verbs"]}


def workload_problems(text):
    docs = parse(text)
    found = []
    deps = [d for d in docs if d.get("kind") == "Deployment"]
    if len(deps) != 1:
        return ["workloads: %d" % len(deps)]
    spec = deps[0]["spec"]
    if str(spec.get("replicas")) != "1":
        found.append("replicas")
    pod = spec["template"]["spec"]
    if pod.get("restartPolicy") != "Always":
        found.append("restart rule")
    sa = pod.get("serviceAccountName")
    if sa != "cw-cloud-reaper" or sa not in [d["metadata"]["name"] for d in docs if d.get("kind") == "ServiceAccount"]:
        found.append("own service account")
    low = text.lower()
    found += ["forbidden name: " + n for n in FORBIDDEN if n in "\n".join(l for l in low.splitlines() if not l.lstrip().startswith("#"))]
    return found


class TestRightsMatch(unittest.TestCase):
    def test_rights_pairs_match_the_list(self):
        got = pairs(read(REAPER))
        self.assertEqual(got - LISTED, set())
        self.assertEqual(LISTED - got, set())

    def test_no_wildcard_and_lease_pairs_present(self):
        self.assertNotIn('"*"', read(REAPER))
        self.assertLessEqual({("get", "leases"), ("create", "leases"), ("update", "leases")}, pairs(read(REAPER)))

    def test_mutant_extra_right_is_caught(self):
        text = read(REAPER)
        mutant = text.replace('verbs: ["get", "list", "patch", "delete"]', 'verbs: ["get", "list", "patch", "delete", "create"]', 1)
        self.assertNotEqual(mutant, text)
        self.assertEqual(pairs(mutant) - LISTED, {("create", "pods")})

    def test_mutant_lease_removed_is_caught(self):
        text = read(REAPER)
        mutant = text.replace('resources: ["leases"]', 'resources: []', 1)
        self.assertNotEqual(mutant, text)
        self.assertTrue(LISTED - pairs(mutant))


class TestWorkload(unittest.TestCase):
    def test_workload_is_clean(self):
        self.assertEqual(workload_problems(read(REAPER)), [])

    def test_mutant_key_mount_is_caught(self):
        text = read(REAPER)
        mutant = text.replace("value: /var/run", "value: /etc/app-key/x /var/run", 1)
        self.assertNotEqual(mutant, text)
        self.assertEqual(len(workload_problems(mutant)), 1)

    def test_comments_say_applied_by_the_administrator(self):
        for p in (REAPER, SERVICE):
            self.assertIn("APPLIED BY THE ADMINISTRATOR, NEVER BY THIS CHANGE", read(p))


class TestTokenServiceManifest(unittest.TestCase):
    def test_service_mounts_the_key_and_policy_matches_the_reaper(self):
        self.assertNotIn("cw-app-key", read(REAPER))
        docs = parse(read(SERVICE))
        self.assertIn("cw-app-key", read(SERVICE))
        pol = [d for d in docs if d.get("kind") == "NetworkPolicy"][0]
        self.assertEqual(pol["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]["app"], "cw-cloud-reaper")
        self.assertEqual(len(pol["spec"]["ingress"][0]["from"]), 1)


# ---- S16: the key Secret out of the reaper's reach; S17: the workloads' hardening ----
KEY_NAMESPACE = "cw-tokens"
REAPER_NAMESPACE = "cw"
REAPER_ACCOUNT = "cw-cloud-reaper"
SENSITIVE = ("secrets", "pods/exec", "*")


def deployment(text):
    return [d for d in parse(text) if d.get("kind") == "Deployment"][0]


def namespace_problems(reaper_text, service_text):
    """Returns (problems, rights reaching the key namespace, key secrets outside the reaper's namespace)."""
    found, docs = [], parse(reaper_text) + parse(service_text)
    dep = deployment(service_text)
    key_ns = dep["metadata"].get("namespace")
    if key_ns != KEY_NAMESPACE:
        found.append("token service namespace %s" % key_ns)
    mounts = [v for v in dep["spec"]["template"]["spec"]["volumes"] if isinstance(v.get("secret"), dict)]
    outside = len([v for v in mounts if v["secret"].get("secretName") == "cw-app-key"]) if key_ns != REAPER_NAMESPACE else 0
    if len(mounts) != 1 or key_ns != KEY_NAMESPACE:
        found.append("key mount namespace")
    reaching = 0
    for b in docs:
        if b.get("kind") not in ("RoleBinding", "ClusterRoleBinding"):
            continue
        if not any(s.get("name") == REAPER_ACCOUNT for s in b.get("subjects", [])):
            continue
        ref = b["roleRef"]
        role = [d for d in docs if d.get("kind") == ref["kind"] and d["metadata"]["name"] == ref["name"]][0]
        where = b["metadata"].get("namespace")   # None for a ClusterRoleBinding: cluster-wide
        for rule in role["rules"]:
            sensitive = any(r in SENSITIVE for r in rule["resources"])
            if where in (key_ns, None) and (where == key_ns or sensitive):
                reaching += len(rule["resources"]) * len(rule["verbs"])
    if reaching:
        found.append("rights reaching the key namespace: %d" % reaching)
    return found, reaching, outside


def hardening_problems(text):
    dep = deployment(text)
    pod = dep["spec"]["template"]["spec"]
    found = []
    ctx = pod.get("securityContext")
    if not isinstance(ctx, dict) or ctx.get("runAsNonRoot") is not True or int(ctx.get("runAsUser", 0)) < 1 or int(ctx.get("runAsGroup", 0)) < 1 \
            or (ctx.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
        found.append("pod securityContext")
    for c in pod["containers"]:
        image = c.get("image", "")
        if ":latest" in image or "@sha256:" not in image:
            found.append("mutable image")
        cc = c.get("securityContext")
        if not isinstance(cc, dict) or cc.get("runAsNonRoot") is not True or int(cc.get("runAsUser", 0)) < 1 \
                or int(cc.get("runAsGroup", 0)) < 1 or cc.get("readOnlyRootFilesystem") is not True \
                or cc.get("allowPrivilegeEscalation") is not False or cc.get("capabilities", {}).get("drop") != ["ALL"] \
                or (cc.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
            found.append("container securityContext")
        res = c.get("resources") or {}
        for kind in ("requests", "limits"):
            if not all((res.get(kind) or {}).get(k) for k in ("cpu", "memory")):
                found.append("resources " + kind)
        if not any(m.get("mountPath") == "/tmp" for m in c.get("volumeMounts", [])):
            found.append("no writable /tmp")
    return found


def child(code):
    """Run `code` in a child process that imports this module; returns its exit status."""
    return subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); from test_cloud_reaper_manifests import *\n%s" % (HERE, code)],
                          capture_output=True).returncode


class TestKeyNamespace(unittest.TestCase):
    def test_key_namespace_is_out_of_the_reaper_reach(self):
        found, reaching, outside = namespace_problems(read(REAPER), read(SERVICE))
        self.assertEqual(found, [])
        self.assertEqual(KEY_NAMESPACE, "cw-tokens")
        self.assertEqual(read(SERVICE).split("---")[0].count("kind: Namespace"), 1)
        for d in parse(read(SERVICE)):
            if d.get("kind") != "Namespace":
                self.assertEqual(d["metadata"].get("namespace", KEY_NAMESPACE), KEY_NAMESPACE, d.get("kind"))
        self.assertNotIn("cw-app-key", [d["metadata"]["name"] for d in parse(read(SERVICE)) if d.get("kind") == "Secret"])
        self.assertIn("cw-tokens.svc", read(REAPER))
        print("S16: rights reaching the key namespace %d, key secrets outside the reaper namespace %d" % (reaching, outside))
        self.assertEqual((reaching, outside), (0, 1))

    def test_network_policy_admits_only_the_reaper_namespace_and_pods(self):
        pol = [d for d in parse(read(SERVICE)) if d.get("kind") == "NetworkPolicy"][0]
        frm = pol["spec"]["ingress"][0]["from"]
        self.assertEqual(len(frm), 1)
        self.assertEqual(frm[0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"], "cw")
        self.assertEqual(frm[0]["podSelector"]["matchLabels"]["app"], "cw-cloud-reaper")

    def test_mutant_service_back_in_the_reaper_namespace_fails(self):
        mutant = read(SERVICE).replace("namespace: cw-tokens", "namespace: cw")
        self.assertNotEqual(mutant, read(SERVICE))
        self.assertTrue(namespace_problems(read(REAPER), mutant)[0])
        code = "import test_cloud_reaper_manifests as m\nt = m.read(m.SERVICE).replace('namespace: cw-tokens', 'namespace: cw')\n" \
               "assert t != m.read(m.SERVICE)\nsys.exit(1 if m.namespace_problems(m.read(m.REAPER), t)[0] else 0)"
        self.assertEqual(child(code), 1)


class TestHardening(unittest.TestCase):
    def test_both_workloads_are_hardened_and_pinned(self):
        ok = 0
        for p in (REAPER, SERVICE):
            self.assertEqual(hardening_problems(read(p)), [], p)
            ok += 1
        mutable = sum(1 for p in (REAPER, SERVICE) if ":latest" in read(p) or "@sha256:" not in read(p))
        print("S17: mutable images %d, workloads with the security settings %d of 2" % (mutable, ok))
        self.assertEqual((mutable, ok), (0, 2))

    def test_the_token_service_has_both_probes_and_the_caller_setting(self):
        c = deployment(read(SERVICE))["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(c["livenessProbe"]["tcpSocket"]["port"], "8080")
        self.assertEqual(c["readinessProbe"]["tcpSocket"]["port"], "8080")
        self.assertIn("system:serviceaccount:cw:cw-cloud-reaper", read(SERVICE))
        self.assertIn("CW_TOKEN_SERVICE_CALLER", read(SERVICE))

    def test_mutant_without_security_context_fails(self):
        mutant = read(REAPER).replace("securityContext:", "noContext:")
        self.assertNotEqual(mutant, read(REAPER))
        self.assertTrue(hardening_problems(mutant))
        code = "import test_cloud_reaper_manifests as m\nt = m.read(m.REAPER).replace('securityContext:', 'noContext:')\n" \
               "assert t != m.read(m.REAPER)\nsys.exit(1 if m.hardening_problems(t) else 0)"
        self.assertEqual(child(code), 1)


if __name__ == "__main__":
    unittest.main()
