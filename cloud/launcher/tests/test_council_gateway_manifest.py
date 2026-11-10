#!/usr/bin/env python3
"""Ask #2322, S6: cloud/k8s/council-gateway.yaml holds a Deployment, a Service, a NetworkPolicy that admits only worker pods, and a Secret
REFERENCE (no Secret object, no value); a mutant with an open ingress fails."""
import copy, json, os, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "..", "..", "k8s", "council-gateway.yaml")
WORKER_LABEL = {"app": "cw-worker"}


def parse(text):
    body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    return [json.loads(p) for p in ("\n" + body).split("\n---\n") if p.strip()]


def check(docs):
    """Raises AssertionError when the manifests are not as asked."""
    kinds = sorted(d["kind"] for d in docs)
    assert "Deployment" in kinds and "Service" in kinds and "NetworkPolicy" in kinds, "kinds: %s" % kinds
    assert "Secret" not in kinds, "a Secret object (value) is not allowed"
    dep = next(d for d in docs if d["kind"] == "Deployment")
    assert dep["spec"]["replicas"] == 1
    vols = dep["spec"]["template"]["spec"]["volumes"]
    assert [v["secret"]["secretName"] for v in vols if "secret" in v] == ["council-gateway-key"], "the Secret reference"
    ingress = [d for d in docs if d["kind"] == "NetworkPolicy" and "Ingress" in d["spec"]["policyTypes"]]
    assert len(ingress) == 1, "one ingress policy"
    rules = ingress[0]["spec"]["ingress"]
    assert len(rules) == 1, "one ingress rule"
    assert rules[0]["from"] == [{"podSelector": {"matchLabels": WORKER_LABEL}}], "ingress only from the worker label"
    assert ingress[0]["spec"]["podSelector"]["matchLabels"] == dep["spec"]["selector"]["matchLabels"]
    assert not any("namespaceSelector" in f or "ipBlock" in f for f in rules[0]["from"])


class Manifest(unittest.TestCase):
    def setUp(self):
        with open(PATH, encoding="utf-8") as f:
            self.text = f.read()
        self.docs = parse(self.text)

    def test_s6_four_kinds_one_ingress_rule_no_secret_value(self):
        check(self.docs)
        self.assertEqual(sorted({d["kind"] for d in self.docs}), ["ConfigMap", "Deployment", "NetworkPolicy", "Service"])

    def test_s6_nothing_that_looks_like_a_key_is_in_the_file(self):
        for word in ("stringData", "BEGIN ", "sk-", "Bearer ", "\"data\": {\"key"):
            self.assertNotIn(word, self.text)
        cm = next(d for d in self.docs if d["kind"] == "ConfigMap")
        settings = json.loads(cm["data"]["settings.json"])
        self.assertEqual(settings["models"], ["anthropic/claude-haiku-5.5", "anthropic/claude-sonnet-5.5"])
        self.assertEqual((settings["max_request_chars"], settings["max_answer_tokens"], settings["daily_budget"]), (400000, 24000, 500))

    def test_s6_the_launcher_label_is_the_policy_label(self):
        import sys
        sys.path.insert(0, os.path.dirname(HERE))
        import cloud_worker as cw
        self.assertEqual(WORKER_LABEL, {"app": cw.LABEL})

    def test_s6_mutants_with_an_open_ingress_fail(self):
        for mutate in (lambda s: s.__setitem__("ingress", [{}]),
                       lambda s: s["ingress"][0].pop("from"),
                       lambda s: s["ingress"][0]["from"].append({"namespaceSelector": {}}),
                       lambda s: s["ingress"][0]["from"][0]["podSelector"]["matchLabels"].clear()):
            docs = copy.deepcopy(self.docs)
            mutate(next(d for d in docs if d["metadata"]["name"] == "council-gateway-ingress")["spec"])
            with self.assertRaises((AssertionError, KeyError)):
                check(docs)

    def test_s6_a_secret_object_fails(self):
        docs = self.docs + [{"kind": "Secret", "metadata": {"name": "x"}}]
        with self.assertRaises(AssertionError):
            check(docs)


if __name__ == "__main__":
    unittest.main()
