#!/usr/bin/env python3
"""Offline test of cloud/k8s/cloud-hub-network.yaml: the six network policies, with widening mutants."""
import os, re, sys, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
PATH = os.path.join(ROOT, "cloud", "k8s", "cloud-hub-network.yaml")
NAMES = ["allow-egress", "cloud-agents-ingress-from-hub", "cloud-agents-to-hub",
         "cloud-hub-ingress", "cloud-hub-to-agents", "default-deny"]
HUB = NAMES[1:5]
PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


class Refused(Exception):
    """The parser refuses a line it does not fully understand (fail closed)."""


KEY = re.compile(r"[A-Za-z0-9_./-]+")
BAD_CHARS = "\"'&*!|>#"


def scalar(s, n):
    s = s.strip()
    if s in ("{}", "[]"):
        return {} if s == "{}" else []
    if not s or any(c in s for c in "[]{}") or s.startswith("- "):
        raise Refused("line %d: unsupported value %r" % (n, s))
    return int(s) if re.fullmatch(r"\d+", s) else s


def split_kv(text, n):
    m = re.fullmatch(r"([^:]*):(?: (.*))?", text)
    if not m or not KEY.fullmatch(m.group(1)):
        raise Refused("line %d: not a plain 'key: value' line: %r" % (n, text))
    return m.group(1), (m.group(2) or "")


def parse_block(lines, i, indent):
    """Parse a block (mapping or list) whose lines sit at `indent`; returns (value, next index)."""
    if lines[i][2].startswith("- "):
        out = []
        while i < len(lines) and lines[i][0] == indent and lines[i][2].startswith("- "):
            n, rest = lines[i][1], lines[i][2][2:]
            if rest.startswith("- ") or rest == "-":
                raise Refused("line %d: a list inside a list" % n)
            if re.match(r"[^\s:]+:(\s|$)", rest) or ":" in rest:
                lines[i] = (indent + 2, n, rest)
                v, i = parse_block(lines, i, indent + 2)
                out.append(v)
            else:
                out.append(scalar(rest, n)); i += 1
        return out, i
    out = {}
    while i < len(lines) and lines[i][0] == indent and not lines[i][2].startswith("- "):
        n = lines[i][1]
        k, v = split_kv(lines[i][2], n)
        if k in out:
            raise Refused("line %d: duplicate key %r" % (n, k))
        i += 1
        if v.strip():
            out[k] = scalar(v, n)
        elif i < len(lines) and (lines[i][0] > indent or (lines[i][0] == indent and lines[i][2].startswith("- "))):
            out[k], i = parse_block(lines, i, lines[i][0])
        else:
            out[k] = None
    return out, i


def prepare(raw, n):
    if "\t" in raw:
        raise Refused("line %d: tab" % n)
    body = raw.strip()
    if any(c in body for c in BAD_CHARS):
        raise Refused("line %d: quote, anchor, alias, tag, block scalar or inline comment" % n)
    indent = len(raw) - len(raw.lstrip(" "))
    if indent % 2:
        raise Refused("line %d: indentation %d is not a multiple of 2" % (n, indent))
    return indent, n, body


def parse(text):
    docs, cur = [], []

    def close():
        if not cur:
            return
        if cur[0][0] != 0:
            raise Refused("line %d: document does not start at column 0" % cur[0][1])
        for a, b in zip(cur, cur[1:]):
            if b[0] - (a[0] + 2 if a[2].startswith("- ") else a[0]) > 2:
                raise Refused("line %d: indentation jumps by more than 2" % b[1])
        value, end = parse_block(cur, 0, 0)
        if end != len(cur):
            raise Refused("line %d: line not understood" % cur[end][1])
        docs.append(value)

    for n, raw in enumerate(text.splitlines(), 1):
        if re.fullmatch(r"---\s*", raw):
            close(); cur = []
        elif raw.strip() and not raw.strip().startswith("#"):
            cur.append(prepare(raw, n))
    close()
    return docs


PART = {"app.kubernetes.io/part-of": "cloud-hub", "cw/run": "first-ask"}
EXCEPT = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
          "169.254.0.0/16", "168.63.129.16/32", "127.0.0.0/8"]
PRIVATE_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


def pol(name, labels, spec):
    md = {"name": name, "namespace": "cw"}
    if labels:
        md["labels"] = dict(labels)
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": md, "spec": spec}


def sel(app):
    return {"matchLabels": {"app": app}}


def tcp(*ports):
    return [{"port": p, "protocol": "TCP"} for p in ports]


# The six policies exactly as applied, as plain structures (key order is not significant).
EXPECTED = {
    "allow-egress": pol("allow-egress", None, {
        "podSelector": {}, "policyTypes": ["Egress"],
        "egress": [
            {"ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
             "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                     "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}]},
            {"ports": tcp(443, 80),
             "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(EXCEPT)}}]},
        ]}),
    "cloud-agents-ingress-from-hub": pol("cloud-agents-ingress-from-hub", PART, {
        "podSelector": sel("cw-worker"), "policyTypes": ["Ingress"],
        "ingress": [{"from": [{"podSelector": sel("cloud-hub")}], "ports": tcp(4096)}]}),
    "cloud-agents-to-hub": pol("cloud-agents-to-hub", PART, {
        "podSelector": sel("cw-worker"), "policyTypes": ["Egress"],
        "egress": [{"to": [{"podSelector": sel("cloud-hub")}], "ports": tcp(8443)}]}),
    "cloud-hub-ingress": pol("cloud-hub-ingress", PART, {
        "podSelector": sel("cloud-hub"), "policyTypes": ["Ingress"],
        "ingress": [{"from": [{"podSelector": sel("cw-worker")}], "ports": tcp(8443)}]}),
    "cloud-hub-to-agents": pol("cloud-hub-to-agents", PART, {
        "podSelector": sel("cloud-hub"), "policyTypes": ["Egress"],
        "egress": [{"to": [{"podSelector": sel("cw-worker")}], "ports": tcp(4096)}]}),
    "default-deny": pol("default-deny", None, {
        "podSelector": {}, "policyTypes": ["Ingress", "Egress"]}),
}


def empty_selector(node):
    """True when an empty podSelector or namespaceSelector sits anywhere inside a peer."""
    if isinstance(node, dict):
        return any((k in ("podSelector", "namespaceSelector") and v == {}) or empty_selector(v)
                   for k, v in node.items())
    if isinstance(node, list):
        return any(empty_selector(v) for v in node)
    return False


def ipblock_problems(ip, n):
    import ipaddress
    bad = []
    try:
        net = ipaddress.ip_network(ip.get("cidr"))
    except (ValueError, TypeError):
        return ["unreadable cidr in %s" % n]
    inside = any(net.version == 4 and net.subnet_of(ipaddress.ip_network(r)) for r in PRIVATE_NETS + EXCEPT[3:4])
    if net.prefixlen < 8 and str(net) != "0.0.0.0/0":
        bad.append("cidr broader than /8 in %s" % n)
    if not inside and not all(e in (ip.get("except") or []) for e in EXCEPT):
        bad.append("ipBlock without the full except list in %s" % n)
    return bad


def rules(doc):
    spec = doc.get("spec") or {}
    for d in ("ingress", "egress"):
        for r in spec.get(d) or []:
            yield d, r


def problems(text):
    """Every check on the text; returns a list of failure strings."""
    bad = []
    try:
        docs = parse(text)
    except Refused as e:
        return ["refused: %s" % e]
    try:
        return _check(docs, text)
    except (AttributeError, TypeError, KeyError):
        return ["unexpected shape"]


def _check(docs, text):
    bad = []
    if len(docs) != 6:
        bad.append("documents: %d" % len(docs))
    bad += ["kind %s" % d.get("kind") for d in docs if d.get("kind") != "NetworkPolicy"]
    bad += ["namespace of %s" % d["metadata"]["name"] for d in docs
            if d["metadata"].get("namespace") != "cw"]
    names = sorted(d["metadata"]["name"] for d in docs)
    if names != NAMES:
        bad.append("names %s" % names)
    by = {d["metadata"]["name"]: d for d in docs}
    for n in NAMES:
        if by.get(n) != EXPECTED[n]:
            bad.append("policy %s differs from the applied one" % n)
    try:
        for d in docs:
            n = d["metadata"]["name"]
            for _, r in rules(d):
                for peer in (r.get("from") or []) + (r.get("to") or []):
                    if empty_selector(peer):
                        bad.append("empty selector in %s" % n)
                    if "ipBlock" in peer:
                        bad += ipblock_problems(peer["ipBlock"], n)
                for p in r.get("ports") or []:
                    if "endPort" in p:
                        bad.append("port range in %s" % n)
    except (AttributeError, TypeError, KeyError):
        bad.append("unexpected shape")
    for n in HUB:
        lab = (by.get(n, {}).get("metadata") or {}).get("labels") or {}
        if lab.get("app.kubernetes.io/part-of") != "cloud-hub":
            bad.append("label on %s" % n)
    if "sha256:" in text or re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text):
        bad.append("digest or uuid")
    return bad


def lenient_parse(text):
    """MUTANT: the earlier lenient parser (no refusals). Used only to prove the refusal tests bite."""
    def sc(x):
        x = x.strip()
        return {} if x == "{}" else int(x) if re.fullmatch(r"\d+", x) else x

    def blk(lines, i, ind):
        if lines[i][1].startswith("- "):
            out = []
            while i < len(lines) and lines[i][0] == ind and lines[i][1].startswith("- "):
                rest = lines[i][1][2:]
                if ":" in rest:
                    lines[i] = (ind + 2, rest)
                    v, i = blk(lines, i, ind + 2)
                    out.append(v)
                else:
                    out.append(sc(rest)); i += 1
            return out, i
        out = {}
        while i < len(lines) and lines[i][0] == ind and not lines[i][1].startswith("- "):
            k, _, v = lines[i][1].partition(":")
            i += 1
            if v.strip():
                out[k.strip()] = sc(v)
            elif i < len(lines) and (lines[i][0] > ind or (lines[i][0] == ind and lines[i][1].startswith("- "))):
                out[k.strip()], i = blk(lines, i, lines[i][0])
            else:
                out[k.strip()] = None
        return out, i
    docs = []
    for part in re.split(r"^---\s*$", text, flags=re.M):
        ls = [(len(l) - len(l.lstrip()), l.strip()) for l in part.splitlines()
              if l.strip() and not l.strip().startswith("#")]
        if ls:
            docs.append(blk(ls, 0, 0)[0])
    return docs


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

    def spellings(self):
        t = self.text
        dd = t.index("name: default-deny")
        return {
            "flow rule in allow-egress": t.replace("  egress:\n", "  egress:\n    - {to: [{ipBlock: {cidr: 0.0.0.0/0}}]}\n", 1),
            "quoted key": t.replace("  egress:\n", '  "egress":\n', 1),
            "single-quoted value": t.replace("kind: NetworkPolicy", "kind: 'NetworkPolicy'", 1),
            "flow in default-deny": t[:dd] + t[dd:].replace("    - Ingress\n    - Egress\n", "  x: [Ingress, Egress]\n"),
            "flow in to": t.replace("      to:\n        - podSelector:\n            matchLabels:\n"
                                    "              app: cloud-hub\n", "      to: [{podSelector: {}}]\n", 1),
            "anchor": t.replace("  podSelector: {}\n", "  podSelector: &a {}\n", 1),
            "alias": t.replace("  podSelector: {}\n", "  podSelector: *a\n", 1),
            "tag": t.replace("kind: NetworkPolicy", "kind: !!str NetworkPolicy", 1),
            "block scalar": t.replace("kind: NetworkPolicy", "kind: |\n    NetworkPolicy", 1),
            "tab": t.replace("  podSelector: {}\n", "\tpodSelector: {}\n", 1),
            "no space after colon": t.replace("kind: NetworkPolicy", "kind:NetworkPolicy", 1),
            "list in list": t.replace("    - Egress\n", "    - - Egress\n", 1),
            "duplicate key": t.replace("  namespace: cw\n", "  namespace: cw\n  namespace: other\n", 1),
            "odd indentation": t.replace("  podSelector: {}\n", "   podSelector: {}\n", 1),
            "indentation jump": t.replace("    - Egress\n", "        - Egress\n", 1),
            "bracket in value": t.replace("kind: NetworkPolicy", "kind: Network[Policy]", 1),
            "inline comment": t.replace("kind: NetworkPolicy", "kind: NetworkPolicy # x", 1),
        }

    def test_parser_fails_closed(self):
        for name, m in self.spellings().items():
            self.assertNotEqual(m, self.text, name + " not applied")
            with self.assertRaises(Refused, msg=name) as c:
                parse(m)
            self.assertRegex(str(c.exception), r"line \d+", name)
            self.assertTrue(problems(m)[0].startswith("refused: line"), name)

    def test_lenient_parser_mutant_is_caught(self):
        # the earlier lenient parser reads these silently: the refusal tests above would fail with it
        sp = self.spellings()
        for name in ("flow rule in allow-egress", "quoted key", "anchor", "single-quoted value"):
            lenient_parse(sp[name])  # must not raise: it accepts what the strict one refuses
            with self.assertRaises(Refused):
                parse(sp[name])
        flow = lenient_parse(sp["flow rule in allow-egress"])[0]["spec"]["egress"][0]
        self.assertNotIn("to", flow)  # the rule is read as junk, not as a peer list

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
        for r in EXCEPT:
            muts["except %s dropped" % r] = t.replace("              - %s\n" % r, "", 1)
        muts["cidr 0.0.0.0/1 without except"] = re.sub(
            r"            cidr: 0.0.0.0/0\n            except:\n(              - .*\n)+", "            cidr: 0.0.0.0/1\n", t, count=1)
        muts["port 22 added to allow-egress"] = t.replace("        - port: 80\n          protocol: TCP\n",
            "        - port: 80\n          protocol: TCP\n        - port: 22\n          protocol: TCP\n", 1)
        i = t.index("name: cloud-agents-ingress-from-hub")
        muts["ports block dropped from agents ingress"] = t[:i] + t[i:].replace(
            "      ports:\n        - port: 4096\n          protocol: TCP\n", "", 1)
        muts["other source app"] = t[:i] + t[i:].replace("app: cloud-hub", "app: other", 1)
        muts["namespaceSelector added to hub ingress"] = t.replace(
            "        - podSelector:\n            matchLabels:\n              app: cw-worker\n      ports:\n        - port: 8443",
            "        - namespaceSelector:\n            matchLabels:\n              a: b\n          podSelector:\n            matchLabels:\n              app: cw-worker\n      ports:\n        - port: 8443", 1)
        muts["changed selector"] = t.replace("k8s-app: kube-dns", "k8s-app: other", 1)
        muts["changed worker port"] = t.replace("port: 8443\n          protocol: TCP\n      to:", "port: 8444\n          protocol: TCP\n      to:", 1)
        for name, m in muts.items():
            self.assertNotEqual(m, t, name + " not applied")
            self.assertTrue(problems(m), name + " not caught")
        for name in ("except 169.254.0.0/16 dropped", "except 127.0.0.0/8 dropped",
                     "except 100.64.0.0/10 dropped", "cidr 0.0.0.0/1 without except"):
            self.assertTrue(any("except" in x for x in problems(muts[name])), name + ": general check silent")

    def test_empty_selector_nested_in_ipblock_peer(self):
        t = self.text.replace("        - ipBlock:\n            cidr: 0.0.0.0/0\n",
                              "        - podSelector: {}\n          ipBlock:\n            cidr: 0.0.0.0/0\n", 1)
        self.assertNotEqual(t, self.text)
        self.assertIn("empty selector in allow-egress", problems(t))

    def test_cidr_rules(self):
        self.assertEqual(ipblock_problems({"cidr": "10.1.0.0/16"}, "x"), [])
        self.assertEqual(ipblock_problems({"cidr": "0.0.0.0/0", "except": EXCEPT}, "x"), [])
        self.assertTrue(ipblock_problems({"cidr": "8.0.0.0/7", "except": EXCEPT}, "x"))
        self.assertTrue(ipblock_problems({"cidr": "203.0.113.0/24"}, "x"))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=0)
