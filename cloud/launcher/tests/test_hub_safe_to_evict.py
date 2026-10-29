"""The hub's pod template carries the autoscaler's do-not-evict mark (ask #1524): manifest, mutants, docs phrases."""
import copy, json, os, re, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "..", "..", "k8s", "cloud-hub.yaml")
DOCS = os.path.join(HERE, "..", "..", "..", "docs", "cloud-operator-quickstart.md")
KEY = "cluster-autoscaler.kubernetes.io/safe-to-evict"


def parse(text):
    body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    return [json.loads(p) for p in body.split("---") if p.strip()]


def mark_problems(text):
    """Empty list when the Deployment template carries the mark as the string false and no other document has the key."""
    docs = parse(text)
    bad = []
    deps = [d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == "cloud-hub"]
    if len(deps) != 1:
        return ["no single cloud-hub Deployment"]
    ann = deps[0]["spec"]["template"]["metadata"].get("annotations", {})
    if ann != {KEY: "false"}:
        bad.append("template annotations are %r" % (ann,))
    for d in docs:
        if d is deps[0]:
            continue
        if KEY in json.dumps(d):
            bad.append("another document carries the key: " + str(d.get("kind")))
    if KEY in deps[0]["metadata"].get("annotations", {}):
        bad.append("the Deployment's own metadata carries the key")
    return bad


def read(path):
    with open(path) as fh:
        return fh.read()


def mark_lines_removed(text):
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        if lines[i].strip() == '"annotations": {' and KEY in lines[i + 1] and lines[i + 2].strip() == "},":
            i += 3
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def bullet(text):
    lines = text.splitlines()
    for n, l in enumerate(lines):
        if KEY in l and l.startswith("- "):
            out = [l]
            for m in lines[n + 1:]:
                if not m.startswith("  - "):
                    break
                out.append(m.strip()[2:])
            return " ".join(out)
    return ""


def sentences(par):
    return [s for s in re.split(r"(?<=\.)\s+", par) if s]


def doc_problems(par):
    bad = []
    low = par.lower()
    if re.search(r"kubectl[^`]*\bapply\b[^`]*cloud-hub\.yaml", par) and "never the whole file" not in low:
        bad.append("tells the administrator to apply the whole file")
    if re.search(r"`kubectl apply[^`]*cloud-hub\.yaml`", par):
        bad.append("gives an apply of the whole file")
    for phrase in ("not a fix of the outage", "inferred and not shown", "#1525"):
        if phrase not in par:
            bad.append("missing: " + phrase)
    if re.search(r"\bis a fix\b|\bthe fix\b|\bfixes the outage", low):
        bad.append("calls it a fix")
    return bad


def not_stopped_missing(par):
    return [w for w in ("node upgrade", "node failure", "spot eviction", "reboot or drain") if w not in par.lower()]


class T(unittest.TestCase):
    def setUp(self):
        self.text = read(PATH)

    def test_template_carries_the_mark(self):
        self.assertEqual(mark_problems(self.text), [])

    def test_key_once_in_the_file_text(self):
        self.assertEqual(self.text.count(KEY), 1)
        self.assertEqual(self.text.replace('"cw"', '"NAMESPACE_NAME"').count(KEY), 1)

    def test_difference_is_that_one_object(self):
        stripped = mark_lines_removed(self.text)
        self.assertEqual(len(self.text.split("\n")) - len(stripped.split("\n")), 3)
        self.assertNotIn(KEY, stripped)
        docs = parse(self.text)
        dep = [d for d in docs if d["kind"] == "Deployment"][0]
        rest = copy.deepcopy(docs)
        r = [d for d in rest if d["kind"] == "Deployment"][0]
        del r["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(rest, parse(stripped))
        self.assertEqual(dep["spec"]["template"]["metadata"]["labels"], r["spec"]["template"]["metadata"]["labels"])

    def test_mutant_dropped_fails(self):
        mutated = mark_lines_removed(self.text)
        self.assertNotEqual(mutated, self.text)
        self.assertNotEqual(mark_problems(mutated), [])

    def test_mutant_true_fails(self):
        mutated = self.text.replace('"%s": "false"' % KEY, '"%s": "true"' % KEY)
        self.assertNotEqual(mutated, self.text)
        self.assertNotEqual(mark_problems(mutated), [])

    def test_mutant_on_deployment_metadata_fails(self):
        docs = parse(self.text)
        d = [x for x in docs if x["kind"] == "Deployment"][0]
        d["metadata"]["annotations"] = {KEY: "false"}
        self.assertNotEqual(mark_problems("---\n".join(json.dumps(x) for x in docs)), [])


class Docs(unittest.TestCase):
    def setUp(self):
        self.par = bullet(read(DOCS))

    def test_paragraph_found(self):
        self.assertTrue(self.par)

    def test_the_seven_sentences(self):
        ss = sentences(self.par)
        wanted = [
            "administrator's step", "launcher's own account cannot change a Deployment",
            "patch deployment cloud-hub --type merge", "--dry-run=server",
            "rolls the hub pod once", "one more restart for every lane",
            "registry Secrets and digests are untouched", "interim pod annotation",
            "a restarted pod loses it", "protects only against the autoscaler's scale-down",
            "no disruption budget", "blocks every drain and upgrade of its node indefinitely",
            "second replica with a budget is a separate ask",
            "dedupe keys (memory only, bounded)", "claimed but not yet delivered",
            "registry survives in the Secrets",
        ]
        for w in wanted:
            self.assertTrue(any(w in s for s in ss), w)

    def test_three_phrases(self):
        self.assertEqual(doc_problems(self.par), [])

    def test_no_whole_file_apply_instruction(self):
        self.assertEqual(len(re.findall(r"kubectl[^`]*\bapply\b[^`]*cloud-hub\.yaml", self.par)), 0)

    def test_mutant_whole_file_apply_fails(self):
        m = self.par + " Run `kubectl apply -f cloud/k8s/cloud-hub.yaml`."
        self.assertNotEqual(doc_problems(m), [])

    def test_mutant_fix_fails(self):
        m = self.par.replace("it is not a fix of the outage", "it is a fix")
        self.assertNotEqual(m, self.par)
        self.assertNotEqual(doc_problems(m), [])

    def test_names_what_the_mark_does_not_stop(self):
        self.assertEqual(not_stopped_missing(self.par), [])

    def test_mutant_spot_eviction_removed_fails(self):
        m = self.par.replace("spot eviction", "")
        self.assertNotEqual(m, self.par)
        self.assertNotEqual(not_stopped_missing(m), [])

    def test_later_whole_template_apply_sentence_once(self):
        self.assertEqual(self.par.count("a later apply of the whole template from the file would carry the mark"), 1)


if __name__ == "__main__":
    unittest.main()
