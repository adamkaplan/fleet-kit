"""The minter manifest (cloud/k8s/cw-minter.yaml): four objects, one Role rule that can only create a token for cw-operator, no secret value.

The repository has no yaml library: the file is read by a small parser of the subset it uses (block maps, block lists, inline lists of quoted
strings, scalars), as the other manifest test reads its file as text. Each guard has a mutant (a changed copy of the text) that fails the check.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
PATH = os.path.join(ROOT, "cloud", "k8s", "cw-minter.yaml")
with open(PATH) as _f:
    TEXT = _f.read()


def scalar(text):
    text = text.strip()
    if text.startswith("["):
        inner = text[1:-1].strip()
        return [scalar(x) for x in inner.split(",")] if inner else []
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return {"true": True, "false": False}.get(text, text)


def parse_block(lines, i, indent):
    """Parse the block at `indent` starting at line i: a map or a list. Returns (value, next index)."""
    if lines[i][1].startswith("- "):
        out = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            rest = lines[i][1][2:]
            lines[i] = (indent + 2, rest)   # the item's first key is a line of the item's map
            item, i = parse_block(lines, i, indent + 2)
            out.append(item)
        return out, i
    out = {}
    while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
        key, _, value = lines[i][1].partition(":")
        i += 1
        if value.strip():
            out[key.strip()] = scalar(value)
        elif i < len(lines) and (lines[i][0] > indent or (lines[i][0] == indent and lines[i][1].startswith("- "))):
            out[key.strip()], i = parse_block(lines, i, lines[i][0])
        else:
            out[key.strip()] = {}
    return out, i


def parse(text):
    """The objects of a multi-document file, as dicts."""
    docs = []
    for chunk in re.split(r"^---\s*$", text, flags=re.M):
        lines = []
        for raw in chunk.splitlines():
            body = raw.split(" #")[0].rstrip() if not raw.lstrip().startswith("#") else ""
            if body.strip():
                lines.append((len(body) - len(body.lstrip()), body.strip()))
        if lines:
            docs.append(parse_block(lines, 0, 0)[0])
    return docs


def problems(text):
    """Every way the manifest text breaks the minter's contract (an empty list is a pass)."""
    found = []
    docs = parse(text)
    if len(docs) != 4:
        found.append("not four objects: %d" % len(docs))
    kinds = sorted(d.get("kind", "") for d in docs)
    if kinds != ["Role", "RoleBinding", "Secret", "ServiceAccount"]:
        found.append("kinds are %s" % kinds)
    if "*" in text:
        found.append("a wildcard")
    for d in docs:
        meta = d.get("metadata", {})
        if meta.get("namespace") != "cw":
            found.append("%s is not in namespace cw" % d.get("kind"))
        if meta.get("labels", {}).get("app") != "cw-minter":
            found.append("%s has no app: cw-minter label" % d.get("kind"))
        if meta.get("name") not in ("cw-minter", "cw-minter-token"):
            found.append("%s has an unexpected name" % d.get("kind"))
    by = {d.get("kind"): d for d in docs}
    sa = by.get("ServiceAccount", {})
    if sa.get("automountServiceAccountToken") is not False:
        found.append("the service account mounts its token")
    role = by.get("Role", {})
    rules = role.get("rules", [])
    if len(rules) != 1:
        found.append("the Role has %d rules" % len(rules))
    elif rules[0] != {"apiGroups": [""], "resources": ["serviceaccounts/token"], "resourceNames": ["cw-operator"], "verbs": ["create"]}:
        found.append("the one rule is %s" % rules[0])
    binding = by.get("RoleBinding", {})
    if binding.get("roleRef") != {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "cw-minter"}:
        found.append("the roleRef is wrong")
    if binding.get("subjects") != [{"kind": "ServiceAccount", "name": "cw-minter", "namespace": "cw"}]:
        found.append("the subject is wrong")
    secret = by.get("Secret", {})
    if secret.get("type") != "kubernetes.io/service-account-token":
        found.append("the Secret type is wrong")
    if secret.get("metadata", {}).get("annotations", {}).get("kubernetes.io/service-account.name") != "cw-minter":
        found.append("the Secret's account annotation is wrong")
    if secret.get("metadata", {}).get("name") != "cw-minter-token":
        found.append("the Secret's name is wrong")
    if "data" in secret or "stringData" in secret:
        found.append("the Secret holds data")
    return found


class Minter(unittest.TestCase):
    def test_the_manifest_meets_the_contract(self):
        self.assertEqual(problems(TEXT), [])

    def test_the_parts_read_as_written(self):
        docs = parse(TEXT)
        self.assertEqual(len(docs), 4)
        role = [d for d in docs if d["kind"] == "Role"][0]
        self.assertEqual(len(role["rules"]), 1)
        rule = role["rules"][0]
        self.assertEqual(sorted(rule), ["apiGroups", "resourceNames", "resources", "verbs"])
        self.assertEqual(rule["verbs"], ["create"])
        self.assertEqual(rule["resourceNames"], ["cw-operator"])
        self.assertEqual(rule["resources"], ["serviceaccounts/token"])

    def test_every_namespace_line_is_cw(self):
        self.assertEqual(set(re.findall(r"^\s*namespace:\s*(\S+)", TEXT, flags=re.M)), {"cw"})

    def test_no_secret_value_is_in_the_file(self):
        self.assertNotRegex(TEXT, r"(?m)^\s*(data|stringData|token):")


class Mutants(unittest.TestCase):
    def mutant(self, old, new):
        self.assertIn(old, TEXT)
        text = TEXT.replace(old, new, 1)
        self.assertNotEqual(text, TEXT)   # the mutation applied
        return text

    def test_each_mutant_fails(self):
        cases = {
            "an added verb get": ('verbs: ["create"]', 'verbs: ["create", "get"]'),
            "a wildcard resource": ('resources: ["serviceaccounts/token"]', 'resources: ["*"]'),
            "resourceNames removed": ('    resourceNames: ["cw-operator"]\n', ""),
            "a data key added": ("type: kubernetes.io/service-account-token", "type: kubernetes.io/service-account-token\ndata:\n  token: eA=="),
            "another namespace": ("  name: cw-minter-token\n  namespace: cw", "  name: cw-minter-token\n  namespace: other"),
            "the token is mounted": ("automountServiceAccountToken: false", "automountServiceAccountToken: true"),
            "a second rule": ('verbs: ["create"]\n', 'verbs: ["create"]\n  - apiGroups: [""]\n    resources: ["pods"]\n    verbs: ["list"]\n'),
        }
        for name, (old, new) in cases.items():
            with self.subTest(mutant=name):
                self.assertNotEqual(problems(self.mutant(old, new)), [], name)


class Runner(unittest.TestCase):
    NAME = os.path.basename(__file__)

    def chain_problems(self, text):
        out = []
        if "tests/%s" % self.NAME not in text:
            out.append("not chained")
        if self.NAME + ":" in text.split("EXCEPTIONS=")[1].split("\n")[0]:
            out.append("listed in EXCEPTIONS")
        return out

    def test_this_file_is_chained_and_never_an_exception(self):
        with open(os.path.join(ROOT, "bin", "test-cloud-launcher")) as f:
            text = f.read()
        self.assertEqual(self.chain_problems(text), [])
        unchained = text.replace("tests/%s" % self.NAME, "tests/gone.py")
        self.assertNotEqual(unchained, text)
        self.assertIn("not chained", self.chain_problems(unchained))
        listed = text.replace('EXCEPTIONS="', 'EXCEPTIONS="%s:x ' % self.NAME, 1)
        self.assertNotEqual(listed, text)
        self.assertIn("listed in EXCEPTIONS", self.chain_problems(listed))


if __name__ == "__main__":
    unittest.main()
