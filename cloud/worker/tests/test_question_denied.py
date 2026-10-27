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


def base_file(rel, mode="text"):
    """The base commit's copy of a file through git (origin/integration, else origin/main); (ref, content) or (None, None)."""
    for ref in ("origin/integration", "origin/main"):
        r = subprocess.run(["git", "show", ref + ":" + rel], cwd=ROOT, capture_output=True)
        if r.returncode == 0:
            return ref, r.stdout
    return None, None


def permission_block(data):
    """The front matter's rules (ask #1286 protects them); comment lines and the prose below are not part of the pin."""
    parts = data.split(b"---\n", 2)
    assert len(parts) == 3, "no front matter"
    lines = [l for l in parts[1].splitlines() if not l.lstrip().startswith(b"#")]
    return lines[lines.index(b"permission:"):]


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
        self.assertEqual(strip_question(load()), base)

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

    PERMISSION_PIN = [b"permission:", b"  edit: deny", b"  read: allow", b"  question: allow", b"  todowrite: allow", b"  bash: allow", b"  external_directory: allow"]

    def test_s3_permission_block_pinned(self):
        """Guard 1 (ask #1286): the whole permission block of both copies is pinned exactly; only comment lines are exempt."""
        for rel in ("agents/opencode/orchestrator.md", ORCH_REL):
            with open(os.path.join(ROOT, rel), "rb") as f:
                self.assertEqual(permission_block(f.read()), self.PERMISSION_PIN, rel)

    def test_s3_permission_block_mutants_refused(self):
        with open(os.path.join(ROOT, ORCH_REL), "rb") as f:
            base = f.read()
        for old, new in ((b"  question: allow\n", b"  question: deny\n"), (b"  edit: deny\n", b"  edit: allow\n"), (b"  bash: allow\n", b"  bash: ask\n")):
            m = base.replace(old, new)
            self.assertNotEqual(m, base)
            self.assertNotEqual(permission_block(m), self.PERMISSION_PIN, old)

    def test_s3_image_definition_equals_recorded_copy(self):
        """Guard 2: both copies equal the recorded copy byte for byte; a change to the definition updates the recorded copy in the same pull request."""
        with open(os.path.join(ROOT, "cloud/worker/tests/golden/orchestrator.md"), "rb") as f:
            gold = f.read()
        for rel in ("agents/opencode/orchestrator.md", ORCH_REL):
            with open(os.path.join(ROOT, rel), "rb") as f:
                self.assertEqual(f.read(), gold, rel)

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
