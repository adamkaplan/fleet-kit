"""The clear's ONE closure, derived from the code (ask #940, on top of #1333).

`bin/delegated_clear.py` (`derive_closure`) derives the closure of the delegated clear from the code of the entry, every part of the manifest and the bin modules the clear loads or runs. Nothing of it is kept in the matrix. The
delegated clear's refusal and the owner hold both read it through `closure_read` (bin/impact_hold.py). These tests check: the pins (the closure only grows), that no list of its names exists anywhere, that every kind the derivation
follows is followed with no second edit, a hostile change in EACH member refused and held with the same members, a mutant for EACH member, a mutant that gives the hold its own reading, that an unresolvable call resolves to every
candidate, and that a run-time trace of the real clear is a subset of the closure (the helper `executed_outside`, used by bin/test-switchboard on the real clear). Nothing here runs the daemon.
"""
import ast
import base64
import difflib
import importlib.util
import json
import os
import re
import subprocess
import sys
import types
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "derive-closure"
MATRIX = "docs/review-laws/impact-matrix.json"
PARTS = "bin/switchboard/"
FLOOR = 397   # definitions reachable from the derived roots over the entry and the parts: it only grows (the floors are re-derived at each batch entry, only up; raised only in its own commit, with the owner's sign-off)
FLOOR_BINDINGS = 335   # the module-level bindings (and imports) those definitions read, over the entry and the parts: the same rule
ENTRY_ONLY = 248   # the entry's own closure from the derived roots, re-derived at each batch entry with the floors
RETIRED = ("closure_roots", "closure_files", "module_roots", "closure_exempt")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLEAR = load("delegated_clear_t", REPO / "bin" / "delegated_clear.py")
FLOORMOD = load("impact_floor_ct", REPO / "bin" / "impact_floor.py")
RULE = json.loads((REPO / MATRIX).read_text())["owner_hold"]["delegated_clear"]
_DERIVED = []


def derived():
    if not _DERIVED:
        _DERIVED.append(CLEAR.closure_for(RULE, root=str(REPO), matches=FLOORMOD.matches))
    return _DERIVED[0]


def run(*args):
    done = subprocess.run([sys.executable, str(TOOL)] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    return done.returncode, done.stdout


def patch_of(base, head):
    return "".join(difflib.unified_diff(base.splitlines(True), head.splitlines(True), "a", "b", n=0))


def span(node):
    return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]), node.end_lineno


def executed_outside(clear, closure, rule, root, executed, matches):
    """[what] a run-time trace executed that the derived closure does not hold. `executed` is a set of (file path, first line of a code object); each is mapped to the top-level definition of its file (and the method of a
    class) by the ast. A daemon file's definition must be in the closure (a method of a class: its name read by the closure, or a special method); a flat module the clear loads must be judged, and the definition must be reached
    from its roots; the clear's own code (`clear_code_paths`) is held whole and is not asked."""
    out, trees = [], {}
    for path, line in sorted(executed):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if not rel.startswith("bin/") or rel.endswith("/"):
            continue
        if rel not in trees:
            trees[rel] = ast.parse(Path(path).read_text())
        top = next((n for n in trees[rel].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and span(n)[0] <= line <= n.end_lineno), None)
        if top is None:
            continue
        method = next((c for c in top.body if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) and span(c)[0] <= line <= c.end_lineno), None) if isinstance(top, ast.ClassDef) else None
        if rel == rule["daemon_file"] or rel.startswith(PARTS):
            defs, members = closure.defs, closure.members
        elif matches(rel, rule["clear_code_paths"]):
            continue
        elif rel in closure.files:
            reach = clear.closure(trees[rel], closure.files[rel]["roots"])
            defs, members = reach[0], reach[3]
        else:
            out.append("%s: a module the closure does not judge ran (%s)" % (rel, top.name))
            continue
        if top.name not in defs:
            out.append("%s:%s" % (rel, top.name))
        elif method is not None and not (method.name in members.get(top.name, set()) or (method.name.startswith("__") and method.name.endswith("__"))):
            out.append("%s:%s.%s" % (rel, top.name, method.name))
    return sorted(set(out))


def synthetic(entry_extra=None, flat=None, rule=None):
    """(rule, sources, flat) of a tiny daemon: an entry that binds a clear module into its namespace, one part, and flat modules."""
    entry = ("import os\n\nHELPER_MODULES = {'load_tool': 'tool'}\n\ndef load_module(name):\n    return name\n\ndef _make(function, module):\n    return lambda: load_module(module)\n\n"
             "for _f, _m in HELPER_MODULES.items():\n    globals()[_f] = _make(_f, _m)\n\nload_module('hold').bind(globals())\n\n" + (entry_extra or "def main():\n    return 0\n"))
    sources = {"bin/fleet-switchboard": entry, "bin/switchboard/part.py": "LIMIT = 3\n\ndef helper(x):\n    return x + LIMIT\n\ndef unused(x):\n    return x\n"}
    modules = {"bin/hold.py": "def check():\n    return clear_entry() + helper(1)\n", "bin/impact_floor.py": "def matches(p, g):\n    return False\n", "bin/tool.py": "def run():\n    return 1\n\ndef other():\n    return 2\n"}
    modules.update(flat or {})
    base = {"daemon_file": "bin/fleet-switchboard", "dynamic_actions": [], "rating_roots": ["main"], "clear_code_paths": ["bin/hold.py", "bin/impact_floor.py"]}
    return dict(base, **(rule or {})), sources, modules


def derive_synthetic(**kw):
    rule, sources, flat = synthetic(**kw)
    sources = dict(sources)
    return CLEAR.derive_closure(rule, sources, flat, FLOORMOD.matches)


def synthetic_with(entry_extra, part=None):
    rule, sources, flat = synthetic(entry_extra)
    if part is not None:
        sources["bin/switchboard/part.py"] = part
    return CLEAR.derive_closure(rule, sources, flat, FLOORMOD.matches)


class TestTheOneClosure(unittest.TestCase):
    def test_the_derived_closure_is_over_the_pins_and_the_tool_checks_it(self):
        found = derived()
        self.assertEqual(found.plain, (ENTRY_ONLY, found.plain[1], found.plain[2], found.plain[3]))
        self.assertGreaterEqual(found.plain[1], FLOOR)
        self.assertGreaterEqual(found.plain[3], FLOOR_BINDINGS)
        code, out = run("--check", "--floor", str(FLOOR), "--floor-bindings", str(FLOOR_BINDINGS))
        self.assertEqual(code, 0, out)
        code, out = run("--check", "--floor", str(found.plain[1] + 1))
        self.assertEqual(code, 1)
        self.assertIn("below the floor of %d" % (found.plain[1] + 1), out)
        code, out = run("--check", "--floor-bindings", str(found.plain[3] + 1))
        self.assertEqual(code, 1)
        self.assertIn("below the floor of %d" % (found.plain[3] + 1), out)

    def test_s2_the_matrix_holds_no_list_of_closure_names_and_the_tool_fails_when_it_does(self):
        self.assertEqual(sorted(set(RULE) & set(RETIRED)), [], "no matrix key of derived names")
        import tempfile, shutil
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root))
        shutil.copytree(str(REPO / "bin"), str(root / "bin"), ignore=shutil.ignore_patterns("test-*", "__pycache__"))
        (root / MATRIX).parent.mkdir(parents=True)
        data = json.loads((REPO / MATRIX).read_text())
        data["owner_hold"]["delegated_clear"]["closure_roots"] = ["main"]
        (root / MATRIX).write_text(json.dumps(data))
        code, out = run("--check", "--root", str(root))
        self.assertEqual(code, 1, out)
        self.assertIn("the matrix holds closure_roots", out)

    def test_u3_no_list_of_derived_names_is_left_in_the_code_or_the_matrix(self):
        names = derived().defs | derived().binds
        found = []
        for path in ("bin/delegated_clear.py", "bin/impact_hold.py", "bin/impact_floor.py", "tools/derive-closure"):
            for node in ast.walk(ast.parse((REPO / path).read_text())):
                if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
                    strings = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                    if len(strings) >= 5 and len(strings) == len(node.elts) and len(set(strings) & names) >= 5:
                        found.append("%s:%d" % (path, node.lineno))
        self.assertEqual(found, [], "a list literal of closure names")
        allowed = {"rating_roots", "clear_entry_points", "dynamic_actions"}
        for key, value in RULE.items():
            if key not in allowed and isinstance(value, list) and len(value) >= 5 and all(isinstance(x, str) for x in value):
                self.assertLess(len(set(value) & names), 5, "%s is a list of closure names" % key)
        for path in ("docs/switchboard.md", "docs/architecture.md", MATRIX):
            text = (REPO / path).read_text()
            for word in RETIRED:
                self.assertNotIn(word, text, "%s names %s" % (path, word))

    def test_s2_every_reader_calls_the_one_function(self):
        """The readers are found by walking the code: who calls `closure_refusals` (the refusal's line mapper), who calls `closure_for` (the one derivation) and who calls `closure_read` (the one read)."""
        def callers(path, wanted):
            tree = ast.parse((REPO / path).read_text())
            found = set()
            for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for x in ast.walk(fn):
                    if isinstance(x, ast.Call) and getattr(x.func, "attr", getattr(x.func, "id", None)) in wanted:
                        found.add((fn.name, getattr(x.func, "attr", getattr(x.func, "id", None))))
            return found
        hold = callers("bin/impact_hold.py", {"closure_refusals", "closure_for", "closure_read", "closure_hold", "daemon_roots"})
        self.assertEqual({f for f, c in hold if c == "closure_refusals"}, {"delegated_clear_map"}, "ONE place maps a change to the closure")
        self.assertEqual({f for f, c in hold if c == "closure_read"}, {"delegated_clear_inputs", "closure_hold"}, "the refusal's inputs and the hold read the same function")
        self.assertIn(("delegated_clear_map", "closure_for"), hold)
        entry = "\n".join((REPO / "bin" / n.strip()).read_text() for n in (REPO / "bin" / "fleet-switchboard.parts").read_text().split("\n") if n.strip() and not n.startswith("#"))
        daemon = ast.parse(entry)
        calling = {fn.name for fn in ast.walk(daemon) if isinstance(fn, ast.FunctionDef) for x in ast.walk(fn) if isinstance(x, ast.Call) and getattr(x.func, "id", "") == "closure_hold"}
        self.assertEqual(calling, {"pr_floor_report"}, "the owner hold's reader is the daemon's report")
        for fn in ast.walk(daemon):
            if isinstance(fn, ast.FunctionDef):
                for x in ast.walk(fn):
                    if isinstance(x, ast.Call) and getattr(x.func, "attr", getattr(x.func, "id", "")) in ("closure_refusals", "closure_for", "derive_closure"):
                        self.fail("%s walks the closure itself" % fn.name)
        self.assertEqual(len(callers("tools/derive-closure", {"closure_for"})), 1)
        self.assertEqual(callers("bin/impact_floor.py", {"closure_refusals", "closure_for", "closure_read"}), set(), "the floor takes the closure's reasons as an input")

    def test_s16_the_closure_words_are_the_same_in_the_matrix_and_both_docs(self):
        words = RULE["meaning"][RULE["meaning"].index("The clear's closure is ONE set"):]
        words = words[:words.index(" Other changes under bin/")]
        for path in ("docs/switchboard.md", "docs/architecture.md"):
            self.assertIn(words, (REPO / path).read_text(), path)
        self.assertGreater(len(words), 500)


class TestWhatTheDerivationFollows(unittest.TestCase):
    def test_s4_each_added_kind_is_in_the_closure_with_no_edit_and_an_unread_method_is_not(self):
        self.assertNotIn("added_function", synthetic_with(None).defs)
        entry = ("def added_function():\n    return 1\n\nADDED_BINDING = 5\n\nclass Thing:\n    def read_me(self):\n        return 1\n    def never_read_a(self):\n        return 2\n    def never_read_b(self):\n        return 3\n"
                 "    def never_read_c(self):\n        return 4\n\ndef main():\n    return added_function() + ADDED_BINDING + Thing().read_me()\n")
        got = synthetic_with(entry)
        self.assertIn("added_function", got.defs)
        self.assertIn("ADDED_BINDING", got.binds)
        self.assertIn("Thing", got.defs)
        self.assertIn("read_me", got.members["Thing"])
        for name in ("never_read_a", "never_read_b", "never_read_c"):
            self.assertNotIn(name, got.members["Thing"], "a method nothing reads is not run")
        part = synthetic_with(None)
        self.assertIn("LIMIT", part.binds, "a binding a closure definition reads, in a part")
        self.assertNotIn("unused", part.files["bin/switchboard/part.py"]["roots"])

    def test_s5_a_flat_module_function_the_closure_reads_is_in_and_one_nothing_reads_is_not(self):
        entry = "def uses():\n    return load_tool().run()\n\ndef main():\n    return uses()\n"
        rule, sources, flat = synthetic(entry)
        got = CLEAR.derive_closure(rule, sources, flat, FLOORMOD.matches)
        self.assertIn("run", got.files["bin/tool.py"]["roots"])
        self.assertNotIn("other", got.files["bin/tool.py"]["roots"], "a function of that module nothing reads is not a root")
        base = flat["bin/tool.py"]
        for name, expect in (("run", 1), ("other", 0)):
            head = base.replace("return 1" if name == "run" else "return 2", "return 9")
            reasons = CLEAR.closure_refusals(base, head, patch_of(base, head), got.files["bin/tool.py"]["roots"], set(), True)
            self.assertEqual(len([r for r in reasons if name in r]), expect, (name, reasons))

    def test_s6_a_call_that_cannot_be_resolved_resolves_to_every_candidate_of_its_name(self):
        shapes = {
            "an attribute call on a receiver of unknown class": "def c1(self):\n    return 1\n\ndef main(obj):\n    return obj.c1()\n",
            "a function called through a parameter": "def callback():\n    return 1\n\ndef apply(fn):\n    return fn()\n\ndef main():\n    return apply(callback)\n",
            "a getattr with a computed name": "class Box:\n    def target(self):\n        return 1\n\ndef main(box, kind):\n    return getattr(box, 'tar' + kind)()\n",
        }
        for label, extra in shapes.items():
            with self.subTest(shape=label):
                rule, sources, flat = synthetic(extra)
                got = CLEAR.derive_closure(rule, sources, flat, FLOORMOD.matches)
                if "getattr" in extra:
                    self.assertIn("Box", got.defs, "the class a computed getattr could reach is NOT left out: a stated limit of the by-name walk is that the name is built at run time")
                    self.assertTrue(got.members.get("Box") is not None)
                elif "obj.c1" in extra:
                    self.assertIn("c1", got.defs, "every definition of the attribute's name")
                else:
                    self.assertIn("callback", got.defs)
                    self.assertIn("apply", got.defs)

    def test_s8_every_rating_root_and_the_dispatchers_string_edge_are_in_the_closure(self):
        found = derived()
        self.assertEqual([n for n in RULE["rating_roots"] + ["cmd_review", "add_review_parsers"] if n not in found.defs], [])
        plain = CLEAR.derive_closure(dict(RULE, dynamic_actions=[]), *self.sources(), FLOORMOD.matches)
        self.assertNotIn("cmd_review", plain.defs, "without the dispatcher's string edge cmd_review is not reached")

    def sources(self):
        s, f = CLEAR.read_sources(str(REPO), RULE["daemon_file"])
        return s, f

    def test_u2_a_closure_that_cannot_be_derived_fails_closed(self):
        s, f = self.sources()
        with self.assertRaises(CLEAR.ClosureError):
            CLEAR.derive_closure(RULE, dict(s, **{PARTS + "audit.py": "def broken(:\n"}), f, FLOORMOD.matches)
        with self.assertRaises(CLEAR.ClosureError):
            CLEAR.derive_closure(dict(RULE, daemon_file="bin/nope"), s, f, FLOORMOD.matches)
        redefined = dict(s, **{PARTS + "decision.py": s[PARTS + "decision.py"] + "\n\ndef redact(x):\n    return x\n"})
        with self.assertRaises(CLEAR.ClosureError):
            CLEAR.derive_closure(RULE, redefined, f, FLOORMOD.matches)
        no_bind = dict(s, **{"bin/fleet-switchboard": s["bin/fleet-switchboard"].replace('load_module("impact_hold").bind(globals())', "pass")})
        with self.assertRaises(CLEAR.ClosureError):
            CLEAR.derive_closure(RULE, no_bind, f, FLOORMOD.matches)

    def test_the_manifest_that_is_not_the_assembly_fails_closed(self):
        import tempfile, shutil
        for label, edit, words in (("an entry that is not a file", lambda t: t + "switchboard/nope.py\n", "listed in bin/fleet-switchboard.parts but not a file"),
                                   ("a path with ..", lambda t: t + "switchboard/../fleet-switchboard\n", "not the entry and not switchboard/<name>.py"),
                                   ("a duplicate", lambda t: t + "switchboard/audit.py\n", "listed twice"),
                                   ("a part not listed", lambda t: t.replace("switchboard/audit.py\n", ""), "does not list"),
                                   ("no entry", lambda t: t.replace("fleet-switchboard\n", "", 1), "the entry is not listed")):
            with self.subTest(case=label):
                root = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, str(root))
                shutil.copytree(str(REPO / "bin"), str(root / "bin"), ignore=shutil.ignore_patterns("test-*", "__pycache__"))
                path = root / "bin" / "fleet-switchboard.parts"
                path.write_text(edit(path.read_text()))
                with self.assertRaises(CLEAR.ClosureError) as caught:
                    CLEAR.read_sources(str(root), "bin/fleet-switchboard")
                self.assertIn(words, str(caught.exception))


class Members:
    """The members of the derived closure and one changed line in each: the daemon files (entry and parts) and the flat modules the clear judges."""

    @staticmethod
    def targets(rel, roots_filter=None):
        """{member name: line to change} for one file: every definition and binding of the derived closure that this file holds."""
        found = derived()
        text = (REPO / rel).read_text()
        tree = ast.parse(text)
        lines = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in found.defs and (rel == RULE["daemon_file"] or rel.startswith(PARTS)):
                    lines[node.name] = node.lineno if isinstance(node, ast.ClassDef) else node.body[-1].end_lineno
            else:
                names = CLEAR._bound_names(node)
                if names and isinstance(node, CLEAR.BIND_KINDS + (ast.Import, ast.ImportFrom)):
                    hit = sorted(names & found.binds)
                    if hit and (rel == RULE["daemon_file"] or rel.startswith(PARTS)):
                        lines.setdefault(hit[0], node.end_lineno)
        return text, lines


def changed(text, numbers):
    lines = text.splitlines(True)
    for n in sorted(set(numbers)):
        lines[n - 1] = lines[n - 1].rstrip("\n") + "  # changed\n"
    return "".join(lines)


class TestEveryMember(unittest.TestCase):
    """A changed line in EACH member of the closure (definitions and bindings, derived here from the real files) is refused by the real mapper and named; one patch for a file changes one line of every member of it."""

    def reasons_for(self, rel, text, head, roots, module_level, entry_points=frozenset()):
        return CLEAR.closure_refusals(text, head, patch_of(text, head), roots, entry_points, module_level, list(RULE["dynamic_actions"]), CLEAR.known_of(derived()) if rel == RULE["daemon_file"] else None)

    def test_s9_every_member_is_refused_and_named(self):
        found, total = derived(), 0
        for rel in sorted(found.files):
            if rel != RULE["daemon_file"] and not rel.startswith(PARTS):
                continue
            text, lines = Members.targets(rel)
            if not lines:
                continue
            head = changed(text, lines.values())
            info = found.files[rel]
            reasons = self.reasons_for(rel, text, head, info["roots"] if rel != RULE["daemon_file"] else CLEAR.daemon_roots(RULE, found), info["module_level"], set(RULE["clear_entry_points"]))
            joined = "\n".join(reasons)
            for name, line in sorted(lines.items()):
                total += 1
                with self.subTest(file=rel, member=name):
                    if info["module_level"]:
                        self.assertTrue(name in joined or re.search(r"head line %d\b" % line, joined) or "head line" in joined, name)
                        self.assertTrue(re.search(r"\b%s\b" % re.escape(name), joined) or re.search(r"head line %d\b" % line, joined), name)
                    else:
                        self.assertTrue(re.search(r"\b%s\b" % re.escape(name), joined), name)
        self.assertGreaterEqual(total, FLOOR + FLOOR_BINDINGS - 60, "the members of the daemon were walked")

    def test_s12_a_definition_outside_the_closure_is_neither_refused_nor_held(self):
        found = derived()
        outside = 0
        part = next(r for r in sorted(found.files) if r.startswith(PARTS) and any(isinstance(n, ast.FunctionDef) and n.name not in found.defs and len(n.body) > 1 for n in ast.parse((REPO / r).read_text()).body))
        for rel in ("bin/fleet-switchboard", part):
            tree = ast.parse((REPO / rel).read_text())
            names = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name not in found.defs and len(n.body) > 1]
            self.assertTrue(names, rel)
            node = names[0]
            text = (REPO / rel).read_text()
            head = changed(text, [node.body[-1].end_lineno])
            info = found.files.get(rel, {"roots": [], "module_level": True})
            roots = CLEAR.daemon_roots(RULE, found) if rel == RULE["daemon_file"] else info["roots"]
            got = CLEAR.closure_refusals(text, head, patch_of(text, head), roots, set(RULE["clear_entry_points"]) if rel == RULE["daemon_file"] else set(), info["module_level"], list(RULE["dynamic_actions"]), CLEAR.known_of(found) if rel == RULE["daemon_file"] else None)
            self.assertEqual([r for r in got if node.name in r], [], rel)
            outside += 1
        self.assertEqual(outside, 2)

    def test_s14_a_mutant_that_drops_one_member_is_seen_for_every_member(self):
        """One mutant for each member of the daemon file's closure (a definition or a binding): the closure the mapper reads lacks that member (it is removed from the definitions, bindings, read names and methods), and the member's own
        changed line is then NOT refused. The real closure refuses it, so every mutant is seen (a mutant the refusal does not notice would be a survivor)."""
        found = derived()
        rel = RULE["daemon_file"]
        text, lines = Members.targets(rel)
        text_lines = text.splitlines(True)
        base_tree = ast.parse(text)
        real_closure = CLEAR.closure
        cache = {}
        def cached_closure(tree, roots, dynamic=()):
            key = (id(tree), tuple(roots))
            if key not in cache:
                cache[key] = real_closure(tree, roots, dynamic)
            return cache[key]
        roots = CLEAR.daemon_roots(RULE, found)
        def refused(name, line, drop):
            new = text_lines[line - 1].rstrip("\n") + "  # changed\n"
            patch = "@@ -%d,1 +%d,1 @@\n-%s+%s" % (line, line, text_lines[line - 1], new)
            head = "".join(text_lines[:line - 1] + [new] + text_lines[line:])
            def mutant(tree, roots_, dynamic=()):
                d, g, r, m = cached_closure(tree, roots_, dynamic)
                return d - {drop}, g - {drop}, r - {drop}, {k: v for k, v in m.items() if k != drop}
            parse = lambda source, *a, **k: base_tree
            whole = CLEAR.known_of(found)
            known = (whole[0] - {drop}, whole[1] - {drop}, whole[2] - {drop}, {k: v for k, v in whole[3].items() if k != drop})
            with unittest.mock.patch.object(CLEAR, "closure", mutant), unittest.mock.patch.object(CLEAR.ast, "parse", parse):
                return [r for r in CLEAR.closure_refusals(text, head, patch, roots, set(), False, list(RULE["dynamic_actions"]), known) if re.search(r"\b%s\b" % re.escape(name), r)]
        survivors = []
        for name, line in sorted(lines.items()):
            real = refused(name, line, None)
            if not real:
                survivors.append("%s: not refused by the real closure" % name)
            elif refused(name, line, name) == real:
                survivors.append("%s: a closure without it gives the same answer" % name)
        self.assertEqual(survivors, [])
        self.assertGreater(len(lines), 300)


class TestTheReadersAgree(unittest.TestCase):
    """The refusal's read (`closure_read`) and the owner hold's (`closure_hold`) on a fake GitHub that serves one pull request changing one line of every member of the daemon file: same members, hold decision."""

    @classmethod
    def setUpClass(cls):
        cls.sb = None
        try:
            sys.path.insert(0, str(REPO / "tests" / "switchboard"))
            from support.loader import sb
            cls.sb = sb
        except Exception as err:   # the daemon cannot be loaded here: the tests below say so
            cls.why = err

    def setUp(self):
        if self.sb is None:
            self.fail("the daemon could not be loaded: %s" % self.why)

    def fake(self, edits):
        """(engine, runner, paths, head): a pull request that modifies the files of `edits` ({path: new text})."""
        files, blobs, heads = [], {}, {}
        for path, new in edits.items():
            old = (REPO / path).read_text()
            old_sha, new_sha = "o" + str(abs(hash(path)))[:8], "n" + str(abs(hash(path)))[:8]
            blobs[old_sha], blobs[new_sha] = old, new
            heads[path] = old_sha
            files.append({"filename": path, "status": "modified", "sha": new_sha, "patch": "\n".join(list(difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=0))[2:])})
        def runner(argv, **kwargs):
            api = argv[2]
            def done(body):
                return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
            if "/pulls/7/files" in api:
                return done(files)
            if api.endswith("/pulls/7"):
                return done({"head": {"sha": "h" * 40}, "base": {"sha": "b" * 40}})
            if "/compare/" in api:
                return done({"merge_base_commit": {"sha": "b" * 40}})
            if "/contents/" in api:
                path = api.split("/contents/")[1].split("?")[0]
                return done({"type": "file", "sha": heads[path]})
            if "/git/blobs/" in api:
                return done({"encoding": "base64", "content": base64.b64encode(blobs[api.rsplit("/", 1)[1]].encode()).decode()})
            return subprocess.CompletedProcess(argv, 1, "", "no such route")
        engine = types.SimpleNamespace(config={"gh": "gh", "call_timeout_seconds": 5}, state_dir=None, accounts=None, gh_runner=runner)
        return engine, runner, list(edits), "h" * 40

    def one_pr(self):
        found, edits = derived(), {}
        for rel in sorted(found.files):
            if rel == RULE["daemon_file"] or rel.startswith(PARTS):
                text, lines = Members.targets(rel)
                if lines:
                    edits[rel] = changed(text, lines.values())
        return edits

    def matrix(self):
        return self.sb.impact_floor.load_matrix()

    def test_s7_the_refusal_and_the_hold_name_the_same_members_and_the_hold_decision_is_hold(self):
        edits = self.one_pr()
        engine, runner, paths, head = self.fake(edits)
        matrix = self.matrix()
        listed, _ = self.sb.impact_floor.clear_code_paths(matrix)
        rule = dict(matrix["owner_hold"]["delegated_clear"], clear_code_paths=listed)
        _files, refusal, why = self.sb.closure_read(engine, runner, "acme-org/widget-api", 7, head, rule)
        self.assertIsNone(why)
        held, why = self.sb.closure_hold(engine, "acme-org/widget-api", 7, head, paths, matrix)
        self.assertIsNone(why)
        self.assertEqual(sorted(set(refusal)), sorted(edits), "every changed file of the closure is read")
        self.assertEqual(self.disagreement(refusal, held), [], "the hold names exactly what the refusal names")
        self.assertTrue(held)
        decision, reasons = self.sb.impact_floor.owner_decide(matrix, paths, [], None, (), None, None, (held, why))
        self.assertEqual(decision, "hold")
        self.assertTrue(any("the clear's closure" in r for r in reasons))
        for path, mapped in refusal.items():
            with self.subTest(file=path):
                self.assertTrue(mapped, "%s: a changed line in each member is refused" % path)

    @staticmethod
    def disagreement(refusal, held):
        want = sorted("%s: %s" % (f, r) for f, mapped in refusal.items() for r in (mapped or []))
        return [x for x in want if x not in held] + [x for x in held if x not in want]

    def test_s15_a_hold_that_reads_its_own_list_disagrees_and_is_seen(self):
        edits = self.one_pr()
        engine, runner, paths, head = self.fake(edits)
        matrix = self.matrix()
        listed, _ = self.sb.impact_floor.clear_code_paths(matrix)
        rule = dict(matrix["owner_hold"]["delegated_clear"], clear_code_paths=listed)
        _files, refusal, _why = self.sb.closure_read(engine, runner, "acme-org/widget-api", 7, head, rule)
        # the mutant: the hold reads only the entry file (a different list)
        entry_only = {k: v for k, v in refusal.items() if k == RULE["daemon_file"]}
        held = ["%s: %s" % (f, r) for f, mapped in entry_only.items() for r in mapped]
        self.assertGreaterEqual(len(self.disagreement(refusal, held)), 1)
        real, _ = self.sb.closure_hold(engine, "acme-org/widget-api", 7, head, paths, matrix)
        self.assertEqual(self.disagreement(refusal, real), [])

    def test_u2_a_closure_that_cannot_be_derived_holds_and_refuses(self):
        engine, runner, paths, head = self.fake(self.one_pr())
        matrix = self.matrix()
        with unittest.mock.patch.object(CLEAR, "closure_for", side_effect=CLEAR.ClosureError("a part does not parse")):
            held, why = self.sb.closure_hold(engine, "acme-org/widget-api", 7, head, paths, matrix)
            self.assertIsNone(held)
            self.assertIn("cannot be derived", why)
            listed, _ = self.sb.impact_floor.clear_code_paths(matrix)
            _f, _c, why = self.sb.closure_read(engine, runner, "acme-org/widget-api", 7, head, dict(matrix["owner_hold"]["delegated_clear"], clear_code_paths=listed))
            self.assertIn("cannot be derived", why)
        decision, _ = self.sb.impact_floor.owner_decide(matrix, ["docs/x.md"], [], None, (), None, None, (None, "the clear's closure cannot be derived: held"))
        self.assertEqual(decision, "hold")

    def test_s12_a_change_outside_the_closure_is_not_held(self):
        found = derived()
        text = (REPO / RULE["daemon_file"]).read_text()
        tree = ast.parse(text)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name not in found.defs and len(n.body) > 1)
        engine, runner, paths, head = self.fake({RULE["daemon_file"]: changed(text, [node.body[-1].end_lineno])})
        held, why = self.sb.closure_hold(engine, "acme-org/widget-api", 7, head, paths, self.matrix())
        self.assertEqual((held, why), ([], None))


class TestTheTraceIsInsideTheClosure(unittest.TestCase):
    def test_s11_a_call_built_at_run_time_is_outside_the_static_closure_and_the_check_sees_it(self):
        import tempfile, shutil
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root))
        (root / "bin" / "switchboard").mkdir(parents=True)
        entry = ("def main(kind):\n    return globals()['hid' + kind]()\n\ndef hidden():\n    return 1\n\ndef hid_den():\n    return 1\n")
        (root / "bin" / "fleet-switchboard").write_text(entry)
        rule = {"daemon_file": "bin/fleet-switchboard", "clear_code_paths": []}
        closure = CLEAR.Closure()
        closure.defs = {"main"}
        ns = {}
        code = compile(entry, str(root / "bin" / "fleet-switchboard"), "exec")
        exec(code, ns)
        seen = set()
        def prof(frame, event, arg):
            if event == "call":
                seen.add((frame.f_code.co_filename, frame.f_code.co_firstlineno))
        sys.setprofile(prof)
        try:
            ns["main"]("den")
        finally:
            sys.setprofile(None)
        self.assertEqual(executed_outside(CLEAR, closure, rule, str(root), seen, FLOORMOD.matches), ["bin/fleet-switchboard:hid_den"])
        closure.defs = {"main", "hid_den"}
        self.assertEqual(executed_outside(CLEAR, closure, rule, str(root), seen, FLOORMOD.matches), [], "with the definition in the closure nothing is outside")


if __name__ == "__main__":
    unittest.main()
