"""The clear's closure follows the parts (ask #1092, slice S13).

tools/derive-closure derives the closure of the delegated clear over the entry plus every part of bin/fleet-switchboard.parts and
writes, for each part that holds a closure definition, a `closure_files` entry and its `module_roots`. These tests check the
derivation in both directions, that a change to a closure definition inside a part is refused by the clear's own closure check, that
moving a definition without regenerating fails, and the floor of the assembled closure. Nothing here runs the daemon.
"""
import ast
import difflib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "derive-closure"
MATRIX = "docs/review-laws/impact-matrix.json"
PARTS = "bin/switchboard/"
FLOOR = 395   # definitions reachable from closure_roots over the entry and the parts at the batch-6 head: it only grows
ENTRY_ONLY = 246   # the entry's own closure from closure_roots at the batch-6 head, as before the parts were counted


def run(*args):
    done = subprocess.run([sys.executable, str(TOOL)] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    return done.returncode, done.stdout


def clear():
    spec = importlib.util.spec_from_file_location("delegated_clear_t", str(REPO / "bin" / "delegated_clear.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rule_of(root=REPO):
    return json.loads((Path(root) / MATRIX).read_text())["owner_hold"]["delegated_clear"]


def fixture():
    """A copy of what the tool reads: the clear, the entry, its manifest and parts, the matrix."""
    root = Path(tempfile.mkdtemp(prefix="closure-"))
    (root / "bin").mkdir()
    for name in ("delegated_clear.py", "fleet-switchboard", "fleet-switchboard.parts"):
        shutil.copy(str(REPO / "bin" / name), str(root / "bin" / name))
    shutil.copytree(str(REPO / "bin" / "switchboard"), str(root / "bin" / "switchboard"))
    (root / MATRIX).parent.mkdir(parents=True)
    shutil.copy(str(REPO / MATRIX), str(root / MATRIX))
    return root


def matrix_edit(root, change):
    path = Path(root) / MATRIX
    data = json.loads(path.read_text())
    change(data["owner_hold"]["delegated_clear"])
    path.write_text(json.dumps(data, indent=2) + "\n")


def patch_of(base, head):
    return "".join(difflib.unified_diff(base.splitlines(True), head.splitlines(True), "a", "b", n=0))


def function_lines(source, name):
    node = next(n for n in ast.parse(source).body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name)
    return node


class TestDerivedClosure(unittest.TestCase):
    def test_the_matrix_is_what_the_code_derives_and_the_closure_is_over_the_floor(self):
        code, out = run("--check", "--floor", str(FLOOR))
        self.assertEqual(code, 0, out)
        counts = [int(w) for w in out.split("from closure_roots alone: ")[1].split("\n")[0].replace(",", " ").split() if w.isdigit()]
        self.assertEqual(counts[0], ENTRY_ONLY, "the entry's own closure is as before")
        self.assertGreaterEqual(counts[1], FLOOR)

    def test_a_missing_name_is_named_with_its_part(self):
        root = fixture()
        self.addCleanup(shutil.rmtree, str(root))
        part = PARTS + "audit.py"
        matrix_edit(root, lambda r: r["module_roots"][part].remove("redact"))
        code, out = run("--check", "--root", str(root))
        self.assertEqual(code, 1)
        self.assertIn("%s: missing definition redact" % part, out)

    def test_a_missing_part_and_an_extra_name_and_an_extra_part_fail(self):
        root = fixture()
        self.addCleanup(shutil.rmtree, str(root))
        part = PARTS + "audit.py"
        def change(r):
            r["closure_files"].remove(part)
            del r["module_roots"][part]
            r["module_roots"][PARTS + "decision.py"].append("not_a_definition")
            r["closure_files"].append(PARTS + "judge.py")
            r["module_roots"][PARTS + "judge.py"] = ["x"]
        matrix_edit(root, change)
        code, out = run("--check", "--root", str(root))
        self.assertEqual(code, 1)
        for line in ("%s: missing from closure_files" % part, "%s: missing definition redact" % part,
                     PARTS + "decision.py: extra definition not_a_definition", PARTS + "judge.py: extra part"):
            self.assertIn(line, out)

    def test_the_entry_roots_files_and_row_are_unchanged_by_the_derivation(self):
        root = fixture()
        self.addCleanup(shutil.rmtree, str(root))
        before = rule_of(root)
        self.assertEqual(run("--write", "--root", str(root))[0], 0)
        after = rule_of(root)
        self.assertEqual(after, before)   # already derived: a write changes nothing
        self.assertEqual(after["closure_roots"], rule_of()["closure_roots"])
        self.assertEqual(after["daemon_file"], "bin/fleet-switchboard")
        self.assertEqual(run("--check", "--root", str(root))[0], 0)


class TestRefusalInAPart(unittest.TestCase):
    def refusals(self, part, name, roots, in_function=True):
        module = clear()
        base = (REPO / part).read_text()
        node = function_lines(base, name)
        lines = base.splitlines(True)
        target = next(i for i in range(node.body[-1].lineno - 1, node.end_lineno) if lines[i].strip())   # a line of the body
        lines[target] = lines[target].rstrip("\n") + "  # changed\n"
        head = "".join(lines)
        return module.closure_refusals(base, head, patch_of(base, head), roots, set(), True)

    def test_a_change_to_a_closure_definition_of_a_part_is_refused_and_names_it(self):
        rule = rule_of()
        for part, name in ((PARTS + "audit.py", "redact"), (PARTS + "decision.py", "decide"), (PARTS + "handoff.py", "worker_owned")):
            with self.subTest(part=part, name=name):
                self.assertIn(name, rule["module_roots"][part])
                reasons = self.refusals(part, name, rule["module_roots"][part])
                self.assertTrue(any(name in r for r in reasons), reasons)
                self.assertEqual(self.refusals(part, name, []), [], "without the part's entry (the matrix before) the same patch gives no reason")

    def test_a_change_to_a_function_of_the_part_outside_the_closure_gives_no_reason(self):
        rule = rule_of()
        part, outside = None, []
        for part in sorted(k for k in rule["module_roots"] if k.startswith(PARTS)):
            tree = ast.parse((REPO / part).read_text())
            outside = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name not in rule["module_roots"][part] and len(n.body) > 1]
            if outside:
                break
        self.assertTrue(outside, "a listed part has a function outside the closure")
        self.assertEqual(self.refusals(part, outside[0], rule["module_roots"][part]), [])

    def test_a_top_level_statement_edit_in_a_listed_part_is_refused(self):
        module = clear()
        part = PARTS + "audit.py"
        base = (REPO / part).read_text()
        head = base + "\nEXTRA = 1\n"
        self.assertTrue(module.closure_refusals(base, head, patch_of(base, head), rule_of()["module_roots"][part], set(), True))


class TestMovingADefinition(unittest.TestCase):
    def test_a_move_without_regenerating_fails_and_a_write_changes_only_the_part_entries(self):
        root = fixture()
        self.addCleanup(shutil.rmtree, str(root))
        source, target = root / PARTS / "decisions_a.py", root / PARTS / "decisions_b.py"
        text = source.read_text()
        node = function_lines(text, "parse_bare_pr_ref")
        lines = text.splitlines(True)
        moved = "".join(lines[node.lineno - 1 - len(node.decorator_list):node.end_lineno])
        source.write_text("".join(lines[:node.lineno - 1 - len(node.decorator_list)] + lines[node.end_lineno:]))
        target.write_text(target.read_text() + "\n\n" + moved)
        before = rule_of(root)
        code, out = run("--check", "--root", str(root))
        self.assertEqual(code, 1, out)
        self.assertIn("decisions_a.py", out)
        self.assertEqual(run("--write", "--root", str(root))[0], 0)
        self.assertEqual(run("--check", "--root", str(root))[0], 0)
        after = rule_of(root)
        self.assertIn(PARTS + "decisions_b.py", after["module_roots"])
        self.assertNotIn(PARTS + "decisions_a.py", after["module_roots"])
        strip = lambda r: {k: ([f for f in v if not f.startswith(PARTS)] if k == "closure_files" else {f: x for f, x in v.items() if not f.startswith(PARTS)} if k == "module_roots" else v) for k, v in r.items()}
        self.assertEqual(strip(after), strip(before))
        self.assertEqual(list(after), list(before), "the key order is kept")

    def test_the_floor_fails_on_a_derivation_that_lacks_a_definition(self):
        root = fixture()
        self.addCleanup(shutil.rmtree, str(root))
        module, rule = clear(), rule_of()
        trees = {n: ast.parse((REPO / "bin" / n).read_text()) for n in (l.strip() for l in (REPO / "bin" / "fleet-switchboard.parts").read_text().splitlines()) if n and not n.startswith("#")}
        whole = ast.Module(body=[b for t in trees.values() for b in t.body], type_ignores=[])
        reached = module.closure(whole, rule["closure_roots"], list(rule.get("dynamic_actions") or []))[0]
        name = sorted(reached & {n.name for n in trees["switchboard/audit.py"].body if isinstance(n, ast.FunctionDef)})[0]   # a definition the closure_roots reach, in a part
        part = root / PARTS / "audit.py"
        text = part.read_text()
        node = function_lines(text, name)
        lines = text.splitlines(True)
        part.write_text("".join(lines[:node.lineno - 1] + lines[node.end_lineno:]))
        self.assertEqual(run("--write", "--root", str(root))[0], 0)
        code, out = run("--check", "--root", str(root), "--floor", str(FLOOR))
        self.assertEqual(code, 1, out)
        self.assertIn("below the floor", out)


if __name__ == "__main__":
    unittest.main()
