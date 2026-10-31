"""#1668: the registry decides death from evidence in ONE function, `registry.shows_dead(row, evidence)`. Neutral names (acme)."""
import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[2] / "bin"
KEYS = ("endpoint_verdict", "pane_listed", "process_found")


def load(name, source=None):
    path = BIN / (name + ".py")
    spec = importlib.util.spec_from_file_location("dv_" + name, path)
    module = importlib.util.module_from_spec(spec)
    if source is None:
        spec.loader.exec_module(module)
    else:
        module.__file__ = str(path)
        exec(compile(source, str(path), "exec"), module.__dict__)
    return module


REG = load("registry")
VIEW = load("agent_reconcile")


def scan(registry_src, view_src):
    """(functions that test the evidence keys for death, callers of shows_dead among reconcile_on_read / reconcile_remove, has_no_live_data defs)."""
    deciders, callers, old = [], [], 0
    for label, src in (("registry", registry_src), ("view", view_src)):
        for fn in [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)]:
            if fn.name == "has_no_live_data":
                old += 1
            tests = any(isinstance(c, ast.Compare) and any(isinstance(k, ast.Constant) and k.value in KEYS for k in ast.walk(c)) for c in ast.walk(fn))
            if tests:
                deciders.append(label + ":" + fn.name)
            calls = any(isinstance(c, ast.Call) and getattr(c.func, "attr", getattr(c.func, "id", None)) == "shows_dead" for c in ast.walk(fn))
            if calls and fn.name in ("reconcile_on_read", "reconcile_remove"):
                callers.append(fn.name)
    return deciders, sorted(callers), old


def problems(registry_src, view_src):
    deciders, callers, old = scan(registry_src, view_src)
    out = []
    if deciders != ["registry:shows_dead"]:
        out.append("deciders %r" % deciders)
    if callers != ["reconcile_on_read", "reconcile_remove"]:
        out.append("callers %r" % callers)
    if old:
        out.append("has_no_live_data defined")
    return out


def old_registry_rule(row, evidence):
    """The registry's old inline test of reconcile_remove."""
    if row.get("pane") is None and row.get("endpoint") is not None:
        return isinstance(evidence, dict) and evidence.get("endpoint_verdict") == "dead"
    return row.get("state") == "dead" and isinstance(evidence, dict) and evidence.get("pane_listed") is False and evidence.get("process_found") is False


def old_view_rule(row, evidence):
    """The view's old decision (whether it asked the registry to remove): no_pane_to_judge -> verdict dead; else dead state and both absent."""
    if REG.no_pane_to_judge(row):
        return evidence.get("endpoint_verdict") == "dead"
    return row.get("state") == "dead" and evidence.get("pane_listed") is False and evidence.get("process_found") is False


def old_removes(row, evidence):
    return old_view_rule(row, evidence) and old_registry_rule(row, evidence)


GONE = {"pane_listed": False, "process_found": False}
DEAD_V = {"endpoint_verdict": "dead"}
ROWS = [
    ("local pane row dead", {"state": "dead", "pane": "p_1"}, GONE),
    ("local pane row alive", {"state": "working", "pane": "p_1"}, GONE),
    ("local pane row dead, process present", {"state": "dead", "pane": "p_1"}, {"pane_listed": False, "process_found": True}),
    ("local endpoint row, no pane, dead verdict", {"state": "dead", "endpoint": "http://acme.example"}, DEAD_V),
    ("local endpoint row, no pane, unknown verdict", {"state": "dead", "endpoint": "http://acme.example"}, {"endpoint_verdict": "unknown"}),
    ("cloud row, no pane, dead verdict", {"kind": "cloud", "endpoint": "http://acme.example"}, DEAD_V),
    ("cloud row holding a pane (differing kind 1)", {"kind": "cloud", "state": "dead", "pane": "p_2"}, GONE),
    ("orchestrator, no endpoint, no pane (differing kind 2)", {"kind": "orchestrator", "state": "dead"}, GONE),
    ("endpoint with empty-string pane (differing kind 3)", {"state": "dead", "pane": "", "endpoint": "http://acme.example"}, GONE),
]
DIFFERING = ("differing kind",)


class TestSharesDeadVerdict(unittest.TestCase):
    def test_s2_truth_table(self):
        ep = {"endpoint": "http://acme.example"}
        table = [
            (True, {"state": "dead", "pane": "p_1"}, GONE),
            (False, {"state": "dead", "pane": "p_1"}, {"pane_listed": True, "process_found": False}),
            (False, {"state": "dead", "pane": "p_1"}, {"pane_listed": False, "process_found": True}),
            (False, {"state": "dead", "pane": "p_1"}, {"pane_listed": None, "process_found": False}),
            (False, {"state": "dead", "pane": "p_1"}, {"pane_listed": False, "process_found": None}),
            (False, {"state": "dead", "pane": "p_1"}, {"pane_listed": False}),
            (False, {"state": "working", "pane": "p_1"}, GONE),
            (True, dict(ep), DEAD_V),
            (False, dict(ep), {"endpoint_verdict": "alive"}),
            (False, dict(ep), {"endpoint_verdict": "unknown"}),
            (False, dict(ep), {}),
            (False, {"state": "dead", "pane": "p_1"}, "gone"),
            (False, dict(ep), None),
        ]
        got = [REG.shows_dead(row, ev) for _, row, ev in table]
        self.assertEqual(got, [want for want, _, _ in table])
        self.assertEqual(sum(got), 2)

    def test_s3_callers_and_the_view_against_a_fake_registry(self):
        self.assertEqual(problems((BIN / "registry.py").read_text(), (BIN / "agent_reconcile.py").read_text()), [])
        self.assertFalse(hasattr(VIEW, "has_no_live_data"))

        class Fake:
            def __init__(self):
                self.calls = []

            def no_pane_to_judge(self, row):
                return REG.no_pane_to_judge(row)

            def reconcile_remove(self, state_dir, name, evidence, whole):
                self.calls.append((name, evidence, whole))
                return {"changed": True, "problem": None}
        fake = Fake()
        read = {"whole": True, "agents": {"acme": {"state": "dead", "pane": "p_1"}}}
        self.assertEqual(VIEW.reconcile_on_read("/nowhere", read, lambda n, a: {"pane": False, "process": False}, fake), ["acme"])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][1]["pane_listed"], False)

    def test_s4_old_against_new_table(self):
        print("\n%-56s %-5s %-5s" % ("row", "old", "new"))
        added = 0
        kept = 0
        for label, row, ev in ROWS:
            old, new = bool(old_removes(row, ev)), REG.shows_dead(row, ev)
            print("%-56s %-5s %-5s" % (label, "rm" if old else "keep", "rm" if new else "keep"))
            if new and not old:
                added += 1
        self.assertEqual(added, 0)
        for label, row, ev in ROWS:
            if "differing kind" in label:
                self.assertTrue(old_registry_rule(row, ev), label)    # the registry's old inline test removed it
                self.assertFalse(REG.shows_dead(row, ev), label)      # the new function keeps it
                kept += 1
        self.assertEqual(kept, 3)

    def test_s4_reconcile_remove_keeps_the_differing_rows(self):
        for label, row, ev in ROWS:
            with tempfile.TemporaryDirectory() as d:
                REG.upsert(d, "acme", dict(row))
                if "acme" not in REG.read(d)["agents"]:
                    continue   # the registry's own writer refused this shape: nothing to remove
                changed = bool(REG.reconcile_remove(d, "acme", ev, True)["changed"])
                self.assertEqual(changed, bool(REG.shows_dead(row, ev)), label)

    def test_s5_s6_scan_and_three_mutants(self):
        reg_src = (BIN / "registry.py").read_text()
        view_src = (BIN / "agent_reconcile.py").read_text()
        deciders, callers, old = scan(reg_src, view_src)
        self.assertEqual((deciders, callers, old), (["registry:shows_dead"], ["reconcile_on_read", "reconcile_remove"], 0))
        mutants = {
            "second private rule in the view": (reg_src, view_src.replace(
                "        evidence = evidence_of(agent, live)\n",
                "        evidence = evidence_of(agent, live)\n        if evidence.get(\"pane_listed\") is not False:\n            continue\n", 1)),
            "the view not calling it": (reg_src, view_src.replace("_load_registry().shows_dead(", "(lambda a, e: True)(")),
            "the registry not calling it": (reg_src.replace("not shows_dead(row, evidence)", "not (evidence or {}).get(\"x\")"), view_src),
        }
        for label, (r, v) in mutants.items():
            with self.subTest(mutant=label):
                self.assertNotEqual((r, v), (reg_src, view_src), "mutant not applied")
                self.assertTrue(problems(r, v), "mutant not caught")

    def test_s7_docs_hold_one_section_with_the_four_statements(self):
        text = (BIN.parent / "docs" / "agent-registry.md").read_text()
        sections = [s for s in text.split("\n## ")[1:] if "shows_dead" in s]
        self.assertEqual(len(sections), 1)
        s = sections[0]
        for needle in ("one function", "no_pane_to_judge", "pane_state", "missing-pass", "U6", "not met as written"):
            self.assertIn(needle, s)


if __name__ == "__main__":
    unittest.main()
