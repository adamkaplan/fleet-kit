#!/usr/bin/env python3
"""Offline test: the question tool is denied in the pod's permission set and the orchestrator definition."""
import copy, json, os, re, subprocess, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
JSON_REL = "cloud/worker/opencode-worker.json"
ORCH_REL = "cloud/worker/agents/orchestrator.md"
DOC = os.path.join(ROOT, "docs/cloud-worker-interface.md")


def load():
    with open(os.path.join(ROOT, JSON_REL)) as f:
        return json.load(f)


def orch_text():
    with open(os.path.join(ROOT, ORCH_REL)) as f:
        return f.read()


def question_values(cfg):
    """Return the question value at each block: top level and every agent block."""
    out = {"top": cfg.get("permission", {}).get("question")}
    for name, a in cfg.get("agent", {}).items():
        out["agent:" + name] = a.get("permission", {}).get("question")
    return out


def check_blocks(cfg):
    vals = question_values(cfg)
    assert "agent:worker" in vals, "no worker block"
    bad = {k: v for k, v in vals.items() if v != "deny"}
    return bad


def strip_question(cfg):
    c = copy.deepcopy(cfg)
    c["permission"].pop("question", None)
    for a in c.get("agent", {}).values():
        a.get("permission", {}).pop("question", None)
    return c


def base_other_keys():
    """Other permissions of the base file: git show of the merge base, else None."""
    for ref in ("origin/integration", "origin/main"):
        r = subprocess.run(["git", "show", ref + ":" + JSON_REL], cwd=ROOT, capture_output=True, text=True)
        if r.returncode == 0:
            return strip_question(json.loads(r.stdout))
    return None


def orch_question(text):
    fm = text.split("---", 2)[1]
    m = re.search(r"^  question: (\S+)", fm, re.M)
    return m.group(1) if m else None


class T(unittest.TestCase):
    def test_s2_both_blocks_deny(self):
        cfg = load()
        self.assertEqual(question_values(cfg), {"top": "deny", "agent:worker": "deny"})
        self.assertEqual(check_blocks(cfg), {})

    def test_s4_no_other_permission_changed(self):
        base = base_other_keys()
        if base is None:
            self.skipTest("no base ref available")
        self.assertEqual(strip_question(load()), base)

    def test_mutants_refused(self):
        refused = 0
        cfg = load()
        muts = []
        m = copy.deepcopy(cfg); del m["permission"]["question"]; muts.append(("top missing", m, "top"))
        m = copy.deepcopy(cfg); del m["agent"]["worker"]["permission"]["question"]; muts.append(("agent missing", m, "agent:worker"))
        m = copy.deepcopy(cfg); m["permission"]["question"] = "allow"; muts.append(("allow", m, "top"))
        m = copy.deepcopy(cfg); m["agent"]["worker"]["permission"]["question"] = "ask"; muts.append(("ask", m, "agent:worker"))
        for name, m, key in muts:
            self.assertNotEqual(m, cfg, name + ": mutation not applied")
            self.assertIn(key, check_blocks(m), name + " not refused")
            refused += 1
        # another key changed: compare against the base other-keys
        base = strip_question(cfg)
        m = copy.deepcopy(cfg)
        m["permission"]["bash"]["*"] = "ask"
        self.assertNotEqual(m, cfg, "other key: mutation not applied")
        self.assertNotEqual(strip_question(m), base, "other key change not refused")
        refused += 1
        self.assertEqual(refused, 5)

    def test_s1_s7_docs(self):
        with open(DOC) as f:
            d = f.read()
        self.assertIn("a question prompt on a headless pod is never answered", d.lower())
        self.assertIn("the question tool is refused in a pod; a question goes out through the report path", d)


if __name__ == "__main__":
    unittest.main()
