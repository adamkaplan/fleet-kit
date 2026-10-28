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
    bad = {}
    if vals.get("top") is None:
        bad["top"] = None
    if "agent:worker" not in vals:
        bad["agent:worker"] = None
    bad.update( {k: v for k, v in vals.items() if v != "deny"})
    return bad


def strip_question(cfg):
    c = copy.deepcopy(cfg)
    c["permission"].pop("question", None)
    for a in c.get("agent", {}).values():
        a.get("permission", {}).pop("question", None)
    return c


RECORD_BASH_PATTERNS = ("*CW_PR_RECORD*", "*pr-record*")   # ask 1200: the closed list of what its change adds
RECORD_EDIT_ENTRY = {"*": "allow", "*pr-record*": "deny"}   # the editing tool's entry for the record paths


def strip_record_entries(cfg):
    """A deep copy without ONLY the entries ask 1200 adds (the two bash patterns and the exact edit entry), in both blocks."""
    c = copy.deepcopy(cfg)
    blocks = [c["permission"]] + [a.get("permission", {}) for a in c.get("agent", {}).values()]
    for b in blocks:
        for pat in RECORD_BASH_PATTERNS:
            b.get("bash", {}).pop(pat, None)
        if b.get("edit") == RECORD_EDIT_ENTRY:
            del b["edit"]
    return c


def base_file(rel, mode="text"):
    """The base commit's copy of a file through git (origin/integration, else origin/main); (ref, content) or (None, None)."""
    for ref in ("origin/integration", "origin/main"):
        r = subprocess.run(["git", "show", ref + ":" + rel], cwd=ROOT, capture_output=True)
        if r.returncode == 0:
            return ref, r.stdout
    return None, None


def loud_skip(test, what):
    msg = "SKIPPED: no origin/integration and no origin/main ref (fallback tried: origin/main); " + what + " not compared"
    print(msg, file=sys.stderr)
    test.skipTest(msg)


def base_other_keys():
    ref, data = base_file(JSON_REL)
    return None if ref is None else strip_question(json.loads(data))


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
            loud_skip(self, "the base permission file")
        self.assertEqual(strip_record_entries(strip_question(load())), base)

    def test_record_entries_strip_only_the_closed_list(self):
        cfg = load()
        stripped = strip_record_entries(cfg)
        self.assertNotEqual(stripped, cfg, "mutation not applied")
        for b in (stripped["permission"], stripped["agent"]["worker"]["permission"]):
            self.assertNotIn("edit", b)
            self.assertFalse(set(RECORD_BASH_PATTERNS) & set(b["bash"]))
        for mut in ("bash", "edit"):
            m = copy.deepcopy(cfg)
            if mut == "bash":
                m["permission"]["bash"]["gh extra thing*"] = "allow"
            else:
                m["agent"]["worker"]["permission"]["edit"]["*other*"] = "deny"
            self.assertNotEqual(m, cfg, "mutation not applied")
            self.assertNotEqual(strip_record_entries(m), strip_record_entries(cfg))

    def test_s4_empty_agent_block_is_not_dropped(self):
        cfg = load()
        m = copy.deepcopy(cfg); m["agent"]["extra"] = {"permission": {}}
        self.assertNotEqual(m, cfg, "mutation not applied")
        self.assertNotEqual(strip_question(m), strip_question(cfg))
        m = copy.deepcopy(cfg); m["agent"]["extra"] = {"permission": {"question": "deny"}}
        self.assertNotEqual(strip_question(m), strip_question(cfg))

    def test_s4_mutant_on_real_base_copy(self):
        ref, data = base_file(JSON_REL)
        if ref is None:
            loud_skip(self, "the base permission file")
        base = json.loads(data)
        m = copy.deepcopy(base)
        rule = next(k for k, v in m["permission"]["bash"].items() if v == "deny")
        m["permission"]["bash"][rule] = "allow"
        self.assertNotEqual(m, base, "mutation not applied")
        self.assertNotEqual(strip_question(m), strip_question(load()), "bash rule edit not refused")

    def test_s4_check_blocks_flags_missing_blocks(self):
        cfg = load()
        m = copy.deepcopy(cfg); del m["permission"]["question"]
        self.assertNotEqual(m, cfg, "mutation not applied")
        self.assertIn("top", check_blocks(m))
        m = copy.deepcopy(cfg); del m["agent"]["worker"]
        self.assertNotEqual(m, cfg, "mutation not applied")
        self.assertIn("agent:worker", check_blocks(m))

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
        base = strip_question(cfg)
        m = copy.deepcopy(cfg)
        m["permission"]["bash"]["*"] = "ask"
        self.assertNotEqual(m, cfg, "other key: mutation not applied")
        self.assertNotEqual(strip_question(m), base, "other key change not refused")
        refused += 1
        self.assertEqual(refused, 5)

    def test_s3_image_definition_unchanged(self):
        with open(os.path.join(ROOT, "agents/opencode/orchestrator.md"), "rb") as a, open(os.path.join(ROOT, ORCH_REL), "rb") as b:
            img = b.read()
            self.assertEqual(a.read(), img)
        ref, data = base_file(ORCH_REL)
        if ref is None:
            loud_skip(self, "the base orchestrator definition")
        self.assertEqual(img, data)

    def test_no_orchestrator_block_in_config(self):
        self.assertEqual(sorted(load()["agent"]), ["worker"])

    def test_s1_s7_docs(self):
        with open(DOC) as f:
            d = f.read()
        self.assertIn("a question prompt on a headless pod is never answered", d.lower())
        low = d.lower()
        for phrase in ("the question tool is refused in a pod", "a question goes out through the report path", "kind `question`",
                       "written with `deny` at pod start", "last match wins", "is unchanged, a byte-identical copy"):
            self.assertIn(phrase, low, "missing doc phrase: " + phrase)


if __name__ == "__main__":
    unittest.main()
