#!/usr/bin/env python3
"""Offline test of cloud/k8s/cloud-hub-network.yaml: the six network policies, with widening mutants."""
import os, re, sys, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
PATH = os.path.join(ROOT, "cloud", "k8s", "cloud-hub-network.yaml")
NAMES = ["allow-egress", "cloud-agents-ingress-from-hub", "cloud-agents-to-hub",
         "cloud-hub-ingress", "cloud-hub-to-agents", "default-deny"]
HUB = NAMES[1:5]
PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


def scalar(s):
    s = s.strip()
    if s == "{}":
        return {}
    return int(s) if re.fullmatch(r"\d+", s) else s


def parse_block(lines, i, indent):
    """Parse a block (mapping or list) whose lines sit at `indent`; returns (value, next index)."""
    if lines[i][1].startswith("- "):
        out = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            rest = lines[i][1][2:]
            if ":" in rest:
                lines[i] = (indent + 2, rest)
                v, i = parse_block(lines, i, indent + 2)
                out.append(v)
            else:
                out.append(scalar(rest)); i += 1
        return out, i
    out = {}
    while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
        k, _, v = lines[i][1].partition(":")
        i += 1
        if v.strip():
            out[k.strip()] = scalar(v)
        elif i < len(lines) and (lines[i][0] > indent or (lines[i][0] == indent and lines[i][1].startswith("- "))):
            out[k.strip()], i = parse_block(lines, i, lines[i][0])
        else:
            out[k.strip()] = None
    return out, i


def parse(text):
    docs = []
    for part in re.split(r"^---\s*$", text, flags=re.M):
        lines = [(len(l) - len(l.lstrip()), l.strip()) for l in part.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        if lines:
            docs.append(parse_block(lines, 0, 0)[0] if lines[0][0] == 0 else None)
    return docs


def rules(doc):
    spec = doc.get("spec") or {}
    for d in ("ingress", "egress"):
        for r in spec.get(d) or []:
            yield d, r


def problems(text):
    """Every check on the text; returns a list of failure strings."""
    bad = []
    docs = parse(text)
    if len(docs) != 6:
        bad.append("documents: %d" % len(docs))
    bad += ["kind %s" % d.get("kind") for d in docs if d.get("kind") != "NetworkPolicy"]
    bad += ["namespace of %s" % d["metadata"]["name"] for d in docs
            if d["metadata"].get("namespace") != "cw"]
    names = sorted(d["metadata"]["name"] for d in docs)
    if names != NAMES:
        bad.append("names %s" % names)
    by = {d["metadata"]["name"]: d for d in docs}
    dd = by.get("default-deny")
    if dd is None:
        bad.append("default-deny missing")
    else:
        s = dd["spec"]
        if s.get("podSelector") != {} or sorted(s.get("policyTypes") or []) != ["Egress", "Ingress"] \
                or list(rules(dd)):
            bad.append("default-deny shape")
    hi = by.get("cloud-hub-ingress")
    if hi is not None:
        rs = [r for _, r in rules(hi)]
        ok = (hi["spec"].get("podSelector") == {"matchLabels": {"app": "cloud-hub"}} and len(rs) == 1
              and rs[0].get("from") == [{"podSelector": {"matchLabels": {"app": "cw-worker"}}}]
              and rs[0].get("ports") == [{"port": 8443, "protocol": "TCP"}])
        if not ok:
            bad.append("hub ingress shape")
    for d in docs:
        n = d.get("metadata", {}).get("name")
        for _, r in rules(d):
            for peer in (r.get("from") or []) + (r.get("to") or []):
                if peer.get("podSelector") == {} or peer.get("namespaceSelector") == {}:
                    bad.append("empty selector in %s" % n)
                ip = peer.get("ipBlock")
                if ip and ip.get("cidr") == "0.0.0.0/0" and \
                        not all(p in (ip.get("except") or []) for p in PRIVATE):
                    bad.append("wide ipBlock in %s" % n)
            for p in r.get("ports") or []:
                if "endPort" in p:
                    bad.append("port range in %s" % n)
    for n in HUB:
        lab = (by.get(n, {}).get("metadata") or {}).get("labels") or {}
        if lab.get("app.kubernetes.io/part-of") != "cloud-hub":
            bad.append("label on %s" % n)
    if "sha256:" in text or re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text):
        bad.append("digest or uuid")
    return bad


def read(p):
    with open(os.path.join(ROOT, p)) as f:
        return f.read()


class T(unittest.TestCase):
    text = read("cloud/k8s/cloud-hub-network.yaml")

    def test_clean(self):
        self.assertEqual(problems(self.text), [])

    def test_labels_as_applied(self):
        by = {d["metadata"]["name"]: d for d in parse(self.text)}
        for n in HUB:
            self.assertEqual(by[n]["metadata"]["labels"],
                             {"app.kubernetes.io/part-of": "cloud-hub", "cw/run": "first-ask"})
        for n in ("allow-egress", "default-deny"):
            self.assertNotIn("labels", by[n]["metadata"])

    def test_apply_and_rollback_noted(self):
        for t in (self.text, read("docs/cloud-operator-quickstart.md")):
            low = " ".join(t.lower().split()).replace("# ", "")
            for w in ("apply the allow policies before the default deny", "--dry-run=server",
                      "delete networkpolicy -l app.kubernetes.io/part-of=cloud-hub",
                      "delete networkpolicy allow-egress default-deny"):
                self.assertIn(w, low)

    def test_docs_state_cause_and_list(self):
        d = read("docs/cloud-operator-quickstart.md")
        self.assertIn("six applied by hand", d)
        for n in NAMES:
            self.assertIn("`%s`" % n, d)

    def test_boundary_pointer(self):
        self.assertIn("cloud/k8s/cloud-hub-network.yaml", read("docs/cloud-channel.md"))

    def test_mutants(self):
        t = self.text
        pod = "        - podSelector:\n            matchLabels:\n              app: cloud-hub\n"
        muts = {
            "empty pod source": t.replace(pod, "        - podSelector: {}\n", 1),
            "empty ns selector": t.replace("        - namespaceSelector:\n            matchLabels:\n"
                "              kubernetes.io/metadata.name: kube-system\n",
                "        - namespaceSelector: {}\n", 1),
            "bare 0.0.0.0/0": re.sub(r"            except:\n(              - .*\n)+", "", t, count=1),
            "no default deny": t[:t.index("---\napiVersion", t.index("name: default-deny") - 120)],
            "added port": t[:t.index("name: cloud-hub-ingress")] + t[t.index("name: cloud-hub-ingress"):].replace(
                "        - port: 8443\n          protocol: TCP\n",
                "        - port: 8443\n          protocol: TCP\n        - port: 9000\n          protocol: TCP\n", 1),
            "added kind": t + "---\napiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: x\n  namespace: cw\n",
            "deny loses Egress": t.replace("    - Ingress\n    - Egress\n", "    - Ingress\n"),
            "port range": t.replace("        - port: 4096\n", "        - port: 4096\n          endPort: 4100\n", 1),
        }
        for name, m in muts.items():
            self.assertNotEqual(m, t, name + " not applied")
            self.assertTrue(problems(m), name + " not caught")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=0)
