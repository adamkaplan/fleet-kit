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


def _merge_base(ref):
    r = subprocess.run(["git", "merge-base", "HEAD", ref], cwd=ROOT, capture_output=True)
    out = r.stdout.decode().strip() if r.returncode == 0 else ""
    return out or ref


def base_file(rel, mode="text"):
    """The base commit's copy of a file through git (origin/integration, else origin/main); (ref, content) or (None, None)."""
    for ref in ("origin/integration", "origin/main"):
        r = subprocess.run(["git", "show", _merge_base(ref) + ":" + rel], cwd=ROOT, capture_output=True)
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
    # the base may already hold the record entries (on the integration tip the merge base is HEAD itself): strip them from both sides
    return None if ref is None else strip_record_entries(strip_question(json.loads(data)))


EXPECTED_STRIPPED = {"permission": {"bash": {"*": "allow"}}, "agent": {"worker": {"permission": {"bash": {"*": "allow"}}}}}


def check_pin(cfg):
    """Ask #2402: the whole content of the file apart from the question entries."""
    assert strip_question(cfg) == EXPECTED_STRIPPED, "the permission file is not the catch-all allow alone"


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
        # ask #2402: the file is compared with its whole expected content, not with the base commit's copy (the base held the rows this change removes)
        check_pin(load())

    def test_s4_unrelated_change_still_fails_against_base_with_record_entries(self):
        # the base already carries the record entries (CI on the tip): an unrelated change must still differ
        base = strip_record_entries(strip_question(load()))
        cur = load()
        self.assertEqual(without_ref_rules(strip_record_entries(strip_question(cur))), without_ref_rules(base))
        for mut in ("bash", "edit"):
            m = copy.deepcopy(cur)
            if mut == "bash":
                m["permission"]["bash"]["gh unrelated*"] = "allow"
            else:
                m["permission"].setdefault("edit", {})["*unrelated*"] = "deny"
            self.assertNotEqual(m, cur, "mutation not applied")
            self.assertNotEqual(without_ref_rules(strip_record_entries(strip_question(m))), without_ref_rules(base), mut + " change not refused")

    def test_s4_empty_agent_block_is_not_dropped(self):
        cfg = load()
        m = copy.deepcopy(cfg); m["agent"]["extra"] = {"permission": {}}
        self.assertNotEqual(m, cfg, "mutation not applied")
        self.assertNotEqual(strip_question(m), strip_question(cfg))
        m = copy.deepcopy(cfg); m["agent"]["extra"] = {"permission": {"question": "deny"}}
        self.assertNotEqual(strip_question(m), strip_question(cfg))

    def test_s4_mutant_with_a_bash_row_is_refused_by_the_whole_content_pin(self):
        # ask #2402: the old test mutated the base commit's copy (which held the deny rows); now the mutant is a copy of the current file
        check_pin(load())   # the control passes through the same pin the real test uses
        for mutate in (lambda m: m["permission"]["bash"].update({"gh pr merge*": "deny"}),
                       lambda m: m["agent"]["worker"]["permission"]["bash"].update({"*": "deny"}),
                       lambda m: m["permission"].update({"edit": {"*x*": "deny"}})):
            m = copy.deepcopy(load())
            mutate(m)
            self.assertNotEqual(m, load(), "mutation not applied")
            with self.assertRaises(AssertionError):
                check_pin(m)

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
