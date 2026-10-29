"""The clear's closure follows the parts (ask #1092, slice S13).

tools/derive-closure derives the closure of the delegated clear over the entry plus every part of bin/fleet-switchboard.parts and
writes, for each part that holds a closure definition, a `closure_files` entry and its `module_roots`. These tests check the
derivation in both directions, that a change to a closure definition inside a part is refused by the clear's own closure check, that
moving a definition without regenerating fails, and the floor of the assembled closure. Nothing here runs the daemon.
"""
import ast
import difflib
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "derive-closure"
MATRIX = "docs/review-laws/impact-matrix.json"
PARTS = "bin/switchboard/"
FLOOR = 403   # definitions reachable from closure_roots over the entry and the parts: it only grows (the floors are re-derived at each batch entry, only up; raised only in its own commit, with the owner's sign-off)
FLOOR_BINDINGS = 340   # the module-level bindings (and imports) those definitions read, over the entry and the parts: the same rule
ENTRY_ONLY = 253   # the entry's own closure from closure_roots, re-derived at each batch entry with the floors


def run(*args):
    done = subprocess.run([sys.executable, str(TOOL)] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    return done.returncode, done.stdout


def tool_module():
    loader = importlib.machinery.SourceFileLoader("derive_closure_t", str(TOOL))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("derive_closure_t", loader))
    loader.exec_module(module)
    return module


def counts(out):
    """(entry definitions, assembled definitions, entry bindings, assembled bindings) from closure_roots alone, as the tool prints them."""
    found = re.search(r"definitions (\d+) over the entry, (\d+) over the entry and the parts; bindings (\d+) over the entry, (\d+) over", out)
    return tuple(int(x) for x in found.groups())


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
        code, out = run("--check", "--floor", str(FLOOR), "--floor-bindings", str(FLOOR_BINDINGS))
        self.assertEqual(code, 0, out)
        found = counts(out)
        self.assertEqual(found[0], ENTRY_ONLY, "the entry's own closure is as before")
        self.assertGreaterEqual(found[1], FLOOR)
        self.assertGreaterEqual(found[3], FLOOR_BINDINGS)
        code, out = run("--check", "--floor-bindings", str(found[3] + 1))
        self.assertEqual(code, 1)
        self.assertIn("below the floor of %d" % (found[3] + 1), out)

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


    def test_a_part_that_holds_only_a_binding_the_closure_reads_is_listed(self):
        part = PARTS + "import_v1.py"
        rule = rule_of()
        self.assertIn(part, rule["closure_files"])
        self.assertEqual(rule["module_roots"][part], [], "no closure definition, so no root: the part is listed and any top-level statement of it is refused")
        root = fixture()
        self.addCleanup(shutil.rmtree, str(root))
        def change(r):
            r["closure_files"].remove(part)
            del r["module_roots"][part]
        matrix_edit(root, change)
        code, out = run("--check", "--root", str(root))
        self.assertEqual(code, 1)
        self.assertIn("%s: missing from closure_files" % part, out)

    def manifest_case(self, edit):
        root = fixture()
        self.addCleanup(shutil.rmtree, str(root))
        path = root / "bin" / "fleet-switchboard.parts"
        path.write_text(edit(path.read_text()))
        before = (root / MATRIX).read_bytes()
        return root, before

    def test_a_manifest_that_is_not_the_assembly_fails_closed_naming_the_entry_and_writes_nothing(self):
        cases = {
            "an entry that is not a file": (lambda t: t + "switchboard/nope.py\n", "switchboard/nope.py: listed in bin/fleet-switchboard.parts but not a file"),
            "a path with ..": (lambda t: t + "switchboard/../fleet-switchboard\n", "not the entry and not switchboard/<name>.py"),
            "an absolute path": (lambda t: t + "/etc/hosts\n", "/etc/hosts: not the entry"),
            "a directory": (lambda t: t + "switchboard/\n", "switchboard/: not the entry"),
            "a duplicate": (lambda t: t + "switchboard/audit.py\n", "switchboard/audit.py: listed twice"),
            "a part file the manifest does not list": (lambda t: t.replace("switchboard/audit.py\n", ""), "switchboard/audit.py: a part file that bin/fleet-switchboard.parts does not list"),
            "no entry": (lambda t: t.replace("fleet-switchboard\n", "", 1), "fleet-switchboard: the entry is not listed"),
        }
        for label, (edit, words) in cases.items():
            with self.subTest(case=label):
                root, before = self.manifest_case(edit)
                for mode in ("--check", "--write"):
                    code, out = run(mode, "--root", str(root))
                    self.assertEqual(code, 1, out)
                    self.assertIn(words, out)
                    self.assertEqual((root / MATRIX).read_bytes(), before, "nothing is written from a derivation that failed")

    def test_a_closure_definition_defined_in_two_files_is_reported(self):
        root = fixture()
        self.addCleanup(shutil.rmtree, str(root))
        text = (root / PARTS / "audit.py").read_text()
        node = function_lines(text, "redact")
        copy = "\n".join(text.splitlines()[node.lineno - 1:node.end_lineno])
        other = root / PARTS / "decision.py"
        other.write_text(other.read_text() + "\n\n" + copy + "\n")
        before = (root / MATRIX).read_bytes()
        for mode in ("--check", "--write"):
            code, out = run(mode, "--root", str(root))
            self.assertEqual(code, 1, out)
            self.assertIn("redact: a closure definition defined in more than one file", out)
            self.assertEqual((root / MATRIX).read_bytes(), before)

    def test_a_write_is_atomic_and_a_failed_derivation_or_layout_leaves_the_matrix_as_it_was(self):
        module = tool_module()
        root = fixture()
        self.addCleanup(shutil.rmtree, str(root))
        part = PARTS + "audit.py"
        matrix_edit(root, lambda r: r["module_roots"][part].remove("redact"))
        path = root / MATRIX
        before = path.read_bytes()
        with unittest.mock.patch.object(module.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                module.main(["--write", "--root", str(root)])
        self.assertEqual(path.read_bytes(), before, "a failed rename leaves the matrix whole")
        self.assertEqual([f for f in os.listdir(str(path.parent)) if f.startswith(".derive-closure-")], [], "and no temp file")
        self.assertEqual(module.main(["--write", "--root", str(root)]), 0)
        self.assertEqual(run("--check", "--root", str(root))[0], 0)
        self.assertEqual([f for f in os.listdir(str(path.parent)) if f.startswith(".derive-closure-")], [])
        (root / PARTS / "audit.py").write_text("def broken(:\n")   # a part that does not parse: the tool cannot run (exit 2), the matrix is not touched
        before = path.read_bytes()
        self.assertEqual(run("--write", "--root", str(root))[0], 2)
        self.assertEqual(path.read_bytes(), before)
        shutil.copy(str(REPO / PARTS / "audit.py"), str(root / PARTS / "audit.py"))
        path.write_text(path.read_text().replace('"module_roots": {', '"module_roots":{'))   # a layout the splice does not know
        before = path.read_bytes()
        self.assertEqual(run("--write", "--root", str(root))[0], 2)
        self.assertEqual(path.read_bytes(), before)


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

    def test_a_change_to_an_import_a_closure_definition_reads_in_a_part_is_refused(self):
        module = clear()
        part = PARTS + "import_v1.py"
        base = (REPO / part).read_text()
        self.assertIn("import base64", base)
        head = base.replace("import base64", "import base64 as b64", 1)
        reasons = module.closure_refusals(base, head, patch_of(base, head), rule_of()["module_roots"][part], set(), True)
        self.assertTrue(reasons, "the binding is refused because the part is listed")
        self.assertEqual(module.closure_refusals(base, head, patch_of(base, head), [], set(), False), [], "and was not seen before (a part outside closure_files is not looked at)")

    def test_a_top_level_statement_edit_in_a_listed_part_is_refused(self):
        module = clear()
        part = PARTS + "audit.py"
        base = (REPO / part).read_text()
        head = base + "\nEXTRA = 1\n"
        self.assertTrue(module.closure_refusals(base, head, patch_of(base, head), rule_of()["module_roots"][part], set(), True))


class TestTheDerivationIsHeld(unittest.TestCase):
    """What decides the closure's coverage (the tool, this test, the manifest) is a clear_code_path: a patch to it is held for the owner by the floor, never delegated."""
    PATHS = ("tools/derive-closure", "tests/switchboard/test_closure.py", "bin/fleet-switchboard.parts")

    def setUp(self):
        spec = importlib.util.spec_from_file_location("impact_floor_t", str(REPO / "bin" / "impact_floor.py"))
        self.floor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.floor)
        self.matrix = self.floor.load_matrix()

    def decide(self, path, matrix):
        globs, exclude, force = self.floor.floor_for("adamkaplan/fleet-kit")
        decision, reasons = self.floor.owner_decide(matrix, [path], [], None, self.floor.floor_paths([path], globs, exclude, force))
        return decision, [r for r in reasons if r.startswith("clear_code_paths")]

    def test_a_patch_to_the_tool_its_test_or_the_manifest_is_held_and_dropping_the_entry_releases_it(self):
        for path in self.PATHS:
            with self.subTest(path=path):
                self.assertIn(path, self.matrix["owner_hold"]["delegated_clear"]["clear_code_paths"])
                self.assertIn(path, self.floor.carry_never_paths(self.matrix), "and a conflict-resolving carry never lifts it (every exact clear_code_paths entry is in never_carried_paths)")
                decision, why = self.decide(path, self.matrix)
                self.assertEqual(decision, "hold")
                self.assertEqual(len(why), 1)
                self.assertIn(path, why[0])
                mutant = json.loads(json.dumps(self.matrix))
                mutant["owner_hold"]["delegated_clear"]["clear_code_paths"].remove(path)
                self.assertEqual(self.decide(path, mutant)[1], [], "the mutant (the entry dropped) no longer names the list")

    def test_a_part_of_the_daemon_is_not_held_by_the_list_and_a_new_part_is_already_a_new_file_of_the_clear(self):
        self.assertEqual(self.decide("bin/switchboard/audit.py", self.matrix)[1], [], "a part is judged by the closure rule, not held wholesale")
        rule = self.matrix["owner_hold"]["delegated_clear"]
        self.assertTrue(self.floor.matches("bin/switchboard/a_new_part.py", rule["new_file_globs"]), "adding a part is refused for the delegated clear by new_file_globs, so listing the manifest adds no burden there")


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
        self.assertEqual(after["module_roots"][PARTS + "decisions_a.py"], [], "decisions_a.py still holds a binding the closure reads (_BARE_PR): listed, with no definition left")
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
