"""The switchboard's layout, checked against docs/architecture.md (ask #1092, slice S8).

The document is the single source: its **Units** table gives every unit (a file) a layer, a job and the units it may also use; its **Known violations**
table lists every break of the rules that today's code still has, each with a ceiling. This test derives the real dependencies of the code with `ast`
and `symtable` and fails when:

  - a file of `bin/*.py`, `bin/switchboard/*.py` or the entry is not a row of the table, or a row has no file;
  - a unit depends on a unit of a higher layer (not named in its "Also may use") and the break is not a row of the violations table;
  - a violation is not exactly its row's number (more: a new break; fewer: lower the row to the count the message names), or a row no longer
    matches a violation: the list only shrinks, and it is exact;
  - two units of one layer depend on each other (a cycle);
  - a name is defined by two units and a third reads it (an ambiguous owner: which one is meant is not decided by the code);
  - a source file is over 1,500 lines or a test file over 2,500 (unless it is a `size` row with a ceiling).

Dynamic imports it cannot follow (a non-constant `import_module`, `load_module` or `__import__`, a path-based load) are counted per unit and listed
in the document's "Dynamic imports not followed" table, so the blind spot is visible and cannot grow unseen.
Nothing here runs the daemon: the files are parsed, never executed. Standard library only. Run it with `bin/test-switchboard` or
`python3 -m unittest tests/switchboard/test_architecture.py`.
"""
import ast
import builtins
import collections
import glob
import os
import re
import shutil
import symtable
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENTRY = "fleet-switchboard"
SOURCE_LIMIT = 1500
TEST_LIMIT = 2500
LAYERS = range(0, 7)
KINDS = ("namespace", "entry-import", "ambiguous", "import", "size", "cycle")


# ---------------------------------------------------------------- the document

def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _section(text, heading):
    match = re.search(r"^## %s\n(.*?)(?=^## |\Z)" % re.escape(heading), text, re.S | re.M)
    return match.group(1) if match else ""


def parse_document(text):
    """The Units table as {path: (layer, job, also-may-use set)} and the Known violations table as {(from, to, kind): (ceiling, why)}."""
    units, violations = {}, {}
    for line in _section(text, "Units").splitlines():
        if line.startswith("| `"):
            unit, layer, job, also = (_cells(line) + [""] * 4)[:4]
            units[unit.strip("`")] = (int(layer), job, {u.strip().strip("`") for u in also.split(",") if u.strip()})
    for line in _section(text, "Known violations").splitlines():
        if line.startswith("| `"):
            source, target, kind, ceiling, why = (_cells(line) + [""] * 5)[:5]
            violations[(source.strip("`"), target.strip("`"), kind)] = (int(ceiling.replace(",", "")), why)
    return units, violations


def parse_dynamic(text):
    """The "Dynamic imports not followed" table as {unit: count}."""
    found = {}
    for line in _section(text, "Dynamic imports not followed").splitlines():
        if line.startswith("| `"):
            unit, count, _what = (_cells(line) + [""] * 3)[:3]
            found[unit.strip("`")] = int(count)
    return found


# ------------------------------------------------------------------ the code

def unit_files(root):
    """Every file that is a unit, as its path relative to bin/: the entry, the parts and the flat modules."""
    bin_dir = Path(root) / "bin"
    found = [ENTRY] if (bin_dir / ENTRY).exists() else []
    found += sorted("switchboard/" + os.path.basename(p) for p in glob.glob(str(bin_dir / "switchboard" / "*.py")) if not p.endswith("__init__.py"))
    found += sorted(os.path.basename(p) for p in glob.glob(str(bin_dir / "*.py")))
    return found


def test_files(root):
    """Every test file of the switchboard, relative to the repository root."""
    root = Path(root)
    found = sorted(os.path.relpath(p, root) for p in glob.glob(str(root / "bin" / "test-*")))
    found += sorted(os.path.relpath(p, root) for p in glob.glob(str(root / "tests" / "switchboard" / "**" / "*.py"), recursive=True))
    return found


def line_count(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return sum(1 for _ in handle)


def _tables(path):
    source = Path(path).read_text(encoding="utf-8")
    return symtable.symtable(source, str(path), "exec"), ast.parse(source)


def _walk(table):
    yield table
    for child in table.get_children():
        yield from _walk(child)


def top_level_names(path):
    """(names the file defines at its top level, names it imports)."""
    top, _tree = _tables(path)
    defined, imported = set(), set()
    for symbol in top.get_symbols():
        if symbol.is_imported():
            imported.add(symbol.get_name())
        elif symbol.is_assigned() or symbol.is_namespace():
            defined.add(symbol.get_name())
    return defined, imported


def used_names(path):
    """The names the file reads that it does not bind itself: import-time reads at the top level and global reads inside functions and classes."""
    top, _tree = _tables(path)
    used = set()
    for table in _walk(top):
        for symbol in table.get_symbols():
            if not symbol.is_referenced():
                continue
            name = symbol.get_name()
            if table is top:
                if not (symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()):
                    used.add(name)
            elif symbol.is_global():
                used.add(name)
    return used


def helper_modules(entry_path):
    """The entry's HELPER_MODULES table (the `load_<module>` names), read as data."""
    for node in ast.parse(Path(entry_path).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "HELPER_MODULES":
            return ast.literal_eval(node.value)
    return {}


def loaded_modules(path, helpers):
    """The flat modules a file of the daemon loads: `load_module("x")` and the `load_x()` helpers."""
    found = set()
    for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "load_module" and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                found.add(node.args[0].value)
            elif node.func.id in helpers:
                found.add(helpers[node.func.id])
    return found


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def imported_modules(path, flat):
    """The flat modules a file imports: `import x`, `from x import y`, the relative `from . import x` and `from .x import y`, and the constant-string
    `__import__("x")` and `importlib.import_module("x")`."""
    found = set()
    for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
            elif node.level == 1 and node.module:
                found.add(node.module.split(".")[0])
            elif node.level == 1:
                found |= {a.name for a in node.names}
        elif isinstance(node, ast.Call) and _call_name(node) in ("__import__", "import_module"):
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                found.add(node.args[0].value.split(".")[0])
    return found & flat


PATH_LOADS = ("spec_from_file_location", "SourceFileLoader", "run_path")


def unfollowed_sites(path):
    """[(line, what)] of every dynamic import the derivation does not follow: an `__import__`, `import_module` or `load_module` whose argument is not a
    constant string, and every load by path."""
    sites = []
    for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            constant = bool(node.args) and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
            if name in ("__import__", "import_module", "load_module") and not constant:
                sites.append((node.lineno, "%s(non-constant)" % name))
            elif name in PATH_LOADS:
                sites.append((node.lineno, "%s(path)" % name))
    return sorted(sites)


def unfollowed(root, units):
    """{unit: count} of the dynamic imports of each unit that are not followed."""
    found = {}
    for unit in unit_files(root):
        if unit in units:
            count = len(unfollowed_sites(Path(root) / "bin" / unit))
            if count:
                found[unit] = count
    return found


def derive(root, units):
    """The real dependencies: ({(from, to): {names or 'load'}} for every edge, {(from, to, kind): count} for every edge that breaks the layers)."""
    bin_dir = Path(root) / "bin"
    files = [u for u in unit_files(root) if u in units]
    namespace = [u for u in files if u == ENTRY or u.startswith("switchboard/")]
    flat = {u[:-3] for u in files if "/" not in u and u.endswith(".py")}
    defined, imported = {}, {}
    for unit in namespace:
        defined[unit], imported[unit] = top_level_names(bin_dir / unit)
    owners = collections.defaultdict(set)
    for unit in namespace:
        for name in defined[unit]:
            owners[name].add(unit)
    entry_imports = imported.get(ENTRY, set())
    builtin_names = set(dir(builtins))
    edges = collections.defaultdict(set)
    kinds = {}
    for unit in namespace:
        for name in used_names(bin_dir / unit) - builtin_names:
            if name in defined[unit] or name in imported[unit]:
                continue
            owner = owners.get(name)
            if owner and len(owner) == 1:
                target = next(iter(owner))
                edges[(unit, target)].add(name)
                kinds[(unit, target)] = "namespace"
            elif owner and len(owner) > 1:
                edges[(unit, "+".join(sorted(owner)), "ambiguous")].add(name)
            elif not owner and name in entry_imports and unit != ENTRY:
                edges[(unit, ENTRY, "entry-import")].add(name)
    helpers = helper_modules(bin_dir / ENTRY) if ENTRY in files else {}
    for unit in namespace:
        for module in (loaded_modules(bin_dir / unit, helpers) | imported_modules(bin_dir / unit, flat)) & flat:
            edges[(unit, module + ".py")].add("load")
            kinds[(unit, module + ".py")] = "import"
    for unit in sorted(u for u in files if u not in namespace):
        for module in imported_modules(bin_dir / unit, flat) - {unit[:-3]}:
            edges[(unit, module + ".py")].add("import")
            kinds[(unit, module + ".py")] = "import"
    return edges, kinds


def violations_of(root, units):
    """{(from, to, kind): count} for every dependency that breaks the layering rule, every cycle and every file over its size limit."""
    edges, kinds = derive(root, units)
    found = {}
    graph = collections.defaultdict(set)
    for key, names in edges.items():
        source, target = key[0], key[1]
        if len(key) == 3 and key[2] == "ambiguous":   # a name two units define, read by a third: which one is meant is not in the code
            if source in units and all(owner in units for owner in target.split("+")) and source not in target.split("+"):
                found[(source, target, "ambiguous")] = len(names)
            continue
        if source not in units or target not in units or source == target:
            continue
        if len(key) == 3:   # the entry-import count rides beside the namespace one
            if units[target][0] > units[source][0] and target not in units[source][2]:
                found[(source, target, "entry-import")] = len(names)
            continue
        kind = kinds[key]
        if units[target][0] > units[source][0] and target not in units[source][2]:
            found[(source, target, kind)] = found.get((source, target, kind), 0) + len(names)
        elif units[target][0] == units[source][0]:
            graph[source].add(target)
    for component in _cycles(graph):
        found[(" ".join(component), "-", "cycle")] = len(component)
    for unit in unit_files(root):
        if unit in units and unit != ENTRY:
            lines = line_count(Path(root) / "bin" / unit)
            if lines > SOURCE_LIMIT:
                found[(unit, "-", "size")] = lines
    if ENTRY in units:
        lines = line_count(Path(root) / "bin" / ENTRY)
        if lines > SOURCE_LIMIT:
            found[(ENTRY, "-", "size")] = lines
    for path in test_files(root):
        lines = line_count(Path(root) / path)
        if lines > TEST_LIMIT:
            found[(path, "-", "size")] = lines
    return found


def _cycles(graph):
    """The groups of two or more units that depend on each other (Tarjan's strongly connected components), each sorted."""
    index, low, on_stack, stack, groups, counter = {}, {}, set(), [], [], [0]

    def visit(node):
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            group = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                group.append(member)
                if member == node:
                    break
            if len(group) > 1:
                groups.append(sorted(group))

    for node in sorted(graph):
        if node not in index:
            visit(node)
    return groups


def check(root, text):
    """Every way the code and the document disagree, one sentence each. Empty when they agree."""
    units, allowed = parse_document(text)
    problems = []
    files = set(unit_files(root))
    for unit in sorted(files - set(units)):
        problems.append("%s is a unit file with no row in the Units table" % unit)
    for unit in sorted(set(units) - files):
        problems.append("%s is a row of the Units table but there is no such file" % unit)
    for unit, (layer, job, also) in sorted(units.items()):
        if layer not in LAYERS:
            problems.append("%s has layer %s: a layer is 0 to 6" % (unit, layer))
        if not job:
            problems.append("%s has no job in the Units table" % unit)
        for other in sorted(also - set(units)):
            problems.append("%s may also use %s, which is not a unit" % (unit, other))
    actual = violations_of(root, {u: v for u, v in units.items() if u in files and v[0] in LAYERS})
    for key, count in sorted(actual.items()):
        if key not in allowed:
            problems.append("new violation %s -> %s (%s, %d): fix it, or add a row to Known violations with its reason" % (key[0], key[1], key[2], count))
        elif count > allowed[key][0]:
            problems.append("violation %s -> %s (%s) is %d, over its row's %d: a new break" % (key[0], key[1], key[2], count, allowed[key][0]))
        elif count < allowed[key][0]:
            problems.append("violation %s -> %s (%s) is down to %d from %d: write %d in its row" % (key[0], key[1], key[2], count, allowed[key][0], count))
    for key, (ceiling, why) in sorted(allowed.items()):
        if key[2] not in KINDS:
            problems.append("violation row %s -> %s has kind %r: one of %s" % (key[0], key[1], key[2], ", ".join(KINDS)))
        elif key not in actual:
            problems.append("violation row %s -> %s (%s) matches no violation any more: delete the row" % key)
        if not why.strip():
            problems.append("violation row %s -> %s (%s) has no reason" % key)
    listed = parse_dynamic(text)
    seen = unfollowed(root, units)
    for unit, count in sorted(seen.items()):
        if unit not in listed:
            problems.append("%s has %d dynamic import(s) the test does not follow and the table 'Dynamic imports not followed' does not list" % (unit, count))
        elif listed[unit] != count:
            problems.append("%s has %d dynamic import(s) not followed, the table says %d: write %d" % (unit, count, listed[unit], count))
    for unit in sorted(set(listed) - set(seen)):
        problems.append("the table 'Dynamic imports not followed' lists %s, which has none any more: delete the row" % unit)
    return problems


# ----------------------------------------------------------------- the tests

class TestArchitectureDocument(unittest.TestCase):
    """The real tree against the real document."""

    @classmethod
    def setUpClass(cls):
        cls.text = (REPO / "docs" / "architecture.md").read_text(encoding="utf-8")

    def test_the_code_obeys_the_document(self):
        self.assertEqual(check(REPO, self.text), [], "docs/architecture.md and the code disagree")

    def test_every_unit_has_a_layer_a_job_and_the_layers_are_named(self):
        units, _allowed = parse_document(self.text)
        self.assertGreater(len(units), 50)
        named = set(int(m) for m in re.findall(r"^\| (\d) \| \w+ \|", _section(self.text, "Layers"), re.M))
        self.assertEqual(named, set(LAYERS), "the Layers table names layers 0 to 6")
        self.assertEqual({layer for layer, _job, _also in units.values()} - named, set())

    def test_the_manifest_and_the_table_name_the_same_parts(self):
        units, _allowed = parse_document(self.text)
        listed = [n.strip() for n in (REPO / "bin" / "fleet-switchboard.parts").read_text().splitlines() if n.strip() and not n.lstrip().startswith("#")]
        self.assertEqual(sorted(n for n in listed if n != ENTRY), sorted(u for u in units if u.startswith("switchboard/")))

    def test_the_entry_is_the_only_unit_of_layer_six_and_every_part_is_below_it(self):
        units, _allowed = parse_document(self.text)
        self.assertEqual([u for u, v in units.items() if v[0] == 6], [ENTRY])
        self.assertTrue(all(v[0] < units[ENTRY][0] for u, v in units.items() if u.startswith("switchboard/")))


class TestArchitectureDocumentFailurePaths(unittest.TestCase):
    """A tree built for the purpose: each way the document and the code can disagree is reported, and a tree that agrees reports nothing."""

    ENTRY_SOURCE = "import json\nHELPER_MODULES = {'load_tool': 'tool'}\n\ndef shared():\n    return json.dumps(1)\n\nOTHER = 1\n"

    def build(self, parts=None, flat=None, doc_units=None, doc_violations="", entry=None, doc_dynamic=""):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "bin" / "switchboard").mkdir(parents=True)
        (root / "bin" / ENTRY).write_text(entry if entry is not None else self.ENTRY_SOURCE)
        for name, source in (parts or {}).items():
            (root / "bin" / "switchboard" / name).write_text(source)
        for name, source in (flat or {}).items():
            (root / "bin" / name).write_text(source)
        rows = {ENTRY: 6}
        rows.update({"switchboard/" + n: 2 for n in (parts or {})})
        rows.update({n: 1 for n in (flat or {})})
        rows.update(doc_units or {})
        units = "".join("| `%s` | %d | a job | %s |\n" % (u, (v[0] if isinstance(v, tuple) else v), (v[1] if isinstance(v, tuple) else "")) for u, v in rows.items())
        text = "## Units\n\n| Unit | Layer | Job | Also may use |\n|---|---|---|---|\n%s\n## Known violations\n\n| From | To | Kind | Count | Why |\n|---|---|---|---|---|\n%s\n## Dynamic imports not followed\n\n| Unit | Sites | What |\n|---|---|---|\n%s" % (units, doc_violations, doc_dynamic)
        return root, text

    def problems(self, **kwargs):
        root, text = self.build(**kwargs)
        return check(root, text)

    def test_a_tree_that_agrees_with_its_document_reports_nothing(self):
        self.assertEqual(self.problems(parts={"low.py": "def go():\n    return 1\n"}), [])

    def test_a_file_with_no_row_and_a_row_with_no_file_are_both_reported(self):
        root, text = self.build(parts={"a.py": "X = 1\n"})
        (root / "bin" / "switchboard" / "extra.py").write_text("Y = 1\n")
        text = text.replace("## Known violations", "| `switchboard/gone.py` | 2 | a job | |\n\n## Known violations", 1)
        problems = check(root, text)
        self.assertIn("switchboard/extra.py is a unit file with no row in the Units table", problems)
        self.assertIn("switchboard/gone.py is a row of the Units table but there is no such file", problems)

    def test_a_part_that_uses_a_name_the_entry_defines_is_an_upward_dependency_until_a_row_allows_it(self):
        parts = {"low.py": "def go():\n    return shared() + OTHER\n"}
        problems = self.problems(parts=parts)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("new violation switchboard/low.py -> fleet-switchboard (namespace, 2)", problems[0])
        self.assertEqual(self.problems(parts=parts, doc_violations="| `switchboard/low.py` | `fleet-switchboard` | namespace | 2 | one namespace |\n"), [])

    def test_a_count_that_is_not_exactly_its_row_and_a_row_that_matches_nothing_are_reported(self):
        parts = {"low.py": "def go():\n    return shared() + OTHER\n"}
        over = self.problems(parts=parts, doc_violations="| `switchboard/low.py` | `fleet-switchboard` | namespace | 1 | one namespace |\n")
        self.assertEqual(over, ["violation switchboard/low.py -> fleet-switchboard (namespace) is 2, over its row's 1: a new break"])
        under = self.problems(parts=parts, doc_violations="| `switchboard/low.py` | `fleet-switchboard` | namespace | 3 | one namespace |\n")
        self.assertEqual(under, ["violation switchboard/low.py -> fleet-switchboard (namespace) is down to 2 from 3: write 2 in its row"])
        stale = self.problems(parts={"low.py": "X = 1\n"}, doc_violations="| `switchboard/low.py` | `fleet-switchboard` | namespace | 2 | one namespace |\n")
        self.assertEqual(stale, ["violation row switchboard/low.py -> fleet-switchboard (namespace) matches no violation any more: delete the row"])

    def test_a_name_the_part_imports_itself_or_binds_locally_is_not_a_dependency(self):
        parts = {"low.py": "import json\n\ndef go(OTHER):\n    shared = 1\n    return json.dumps(OTHER) + str(shared)\n"}
        self.assertEqual(self.problems(parts=parts), [])

    def test_a_part_that_relies_on_an_import_of_the_entry_is_counted_as_an_entry_import(self):
        parts = {"low.py": "def go():\n    return json.dumps(1)\n"}
        problems = self.problems(parts=parts)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("(entry-import, 1)", problems[0])

    def test_a_layer_may_be_exempted_in_the_also_may_use_column(self):
        parts = {"low.py": "def go():\n    return shared()\n"}
        self.assertEqual(self.problems(parts=parts, doc_units={"switchboard/low.py": (2, ENTRY)}), [])

    def test_two_units_of_one_layer_that_depend_on_each_other_are_a_cycle(self):
        parts = {"a.py": "def a():\n    return b()\n", "b.py": "def b():\n    return a()\n"}
        problems = self.problems(parts=parts)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("switchboard/a.py switchboard/b.py -> - (cycle, 2)", problems[0])

    def test_a_flat_module_that_imports_a_module_of_a_higher_layer_and_the_daemon_that_loads_one_are_reported(self):
        flat = {"low.py": "import high\n", "high.py": "X = 1\n", "tool.py": "Y = 1\n"}
        problems = self.problems(flat=flat, doc_units={"low.py": 1, "high.py": 3, "tool.py": 7})
        self.assertTrue(any("new violation low.py -> high.py (import, 1)" in p for p in problems), problems)
        loaded = self.problems(entry="HELPER_MODULES = {'load_tool': 'tool'}\n\ndef f():\n    return load_tool()\n", flat={"tool.py": "Y = 1\n"}, doc_units={ENTRY: 0, "tool.py": 3})
        self.assertTrue(any("new violation fleet-switchboard -> tool.py (import, 1)" in p for p in loaded), loaded)

    def test_a_file_over_its_size_limit_is_reported_until_a_size_row_allows_it(self):
        big = "X = 1\n" * (SOURCE_LIMIT + 1)
        problems = self.problems(parts={"big.py": big})
        self.assertEqual(problems, ["new violation switchboard/big.py -> - (size, %d): fix it, or add a row to Known violations with its reason" % (SOURCE_LIMIT + 1)])
        row = "| `switchboard/big.py` | `-` | size | %d | being split |\n" % (SOURCE_LIMIT + 1)
        self.assertEqual(self.problems(parts={"big.py": big}, doc_violations=row), [])
        self.assertEqual(self.problems(parts={"big.py": "X = 1\n"}, doc_violations=row),
                         ["violation row switchboard/big.py -> - (size) matches no violation any more: delete the row"])

    def test_a_test_file_over_its_limit_is_reported(self):
        root, text = self.build()
        (root / "bin" / "test-big").write_text("x = 1\n" * (TEST_LIMIT + 1))
        problems = check(root, text)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("bin/test-big -> - (size, %d)" % (TEST_LIMIT + 1), problems[0])

    def test_a_row_without_a_reason_with_a_bad_kind_or_a_bad_layer_is_reported(self):
        bad = self.problems(parts={"low.py": "X = 1\n"}, doc_units={"switchboard/low.py": 9}, doc_violations="| `switchboard/low.py` | `fleet-switchboard` | wrong | 1 |  |\n")
        self.assertEqual(bad, ["switchboard/low.py has layer 9: a layer is 0 to 6",
                               "violation row switchboard/low.py -> fleet-switchboard has kind 'wrong': one of %s" % ", ".join(KINDS),
                               "violation row switchboard/low.py -> fleet-switchboard (wrong) has no reason"])

    def test_an_also_may_use_that_names_no_unit_is_reported(self):
        problems = self.problems(parts={"low.py": "X = 1\n"}, doc_units={"switchboard/low.py": (2, "nope.py")})
        self.assertEqual(problems, ["switchboard/low.py may also use nope.py, which is not a unit"])

    def test_an_oversized_entry_is_reported_until_a_size_row_allows_it(self):
        entry = "X = 1\n" * (SOURCE_LIMIT + 1)
        self.assertEqual(self.problems(entry=entry), ["new violation fleet-switchboard -> - (size, %d): fix it, or add a row to Known violations with its reason" % (SOURCE_LIMIT + 1)])
        row = "| `fleet-switchboard` | `-` | size | %d | being split |\n" % (SOURCE_LIMIT + 1)
        self.assertEqual(self.problems(entry=entry, doc_violations=row), [])

    def test_a_name_two_units_define_and_a_third_reads_is_an_ambiguous_owner(self):
        parts = {"a.py": "def f():\n    return 1\n", "b.py": "def f():\n    return 2\n", "c.py": "def g():\n    return f()\n"}
        problems = self.problems(parts=parts)
        self.assertEqual(problems, ["new violation switchboard/c.py -> switchboard/a.py+switchboard/b.py (ambiguous, 1): fix it, or add a row to Known violations with its reason"])
        row = "| `switchboard/c.py` | `switchboard/a.py+switchboard/b.py` | ambiguous | 1 | two definitions |\n"
        self.assertEqual(self.problems(parts=parts, doc_violations=row), [])
        own = {"a.py": "def f():\n    return f\n", "b.py": "def f():\n    return 2\n"}
        self.assertEqual(self.problems(parts=own), [], "a unit that defines the name itself is not reading someone else's")

    def test_every_form_of_import_of_a_flat_module_is_followed(self):
        forms = {
            "plain": "import high\n",
            "from": "from high import X\n",
            "relative-module": "from .high import X\n",
            "relative-name": "from . import high\n",
            "dunder": "def f():\n    return __import__('high')\n",
            "import_module": "import importlib\n\ndef f():\n    return importlib.import_module('high')\n",
            "bare import_module": "from importlib import import_module\n\ndef f():\n    return import_module('high')\n",
        }
        for name, source in forms.items():
            with self.subTest(form=name):
                problems = self.problems(flat={"low.py": source, "high.py": "X = 1\n"}, doc_units={"low.py": 1, "high.py": 3})
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("new violation low.py -> high.py (import, 1)", problems[0])

    def test_a_part_that_loads_a_flat_module_directly_or_imports_it_is_followed(self):
        for source in ("def f():\n    return load_module('high')\n", "import high\n"):
            with self.subTest(source=source):
                problems = self.problems(parts={"low.py": source}, flat={"high.py": "X = 1\n"}, doc_units={"high.py": 3})
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("new violation switchboard/low.py -> high.py (import, 1)", problems[0])

    def test_every_dynamic_import_the_test_cannot_follow_is_counted_and_listed(self):
        sources = {
            "non-constant import_module": "import importlib\n\ndef f(n):\n    return importlib.import_module(n)\n",
            "non-constant __import__": "def f(n):\n    return __import__(n)\n",
            "non-constant load_module": "def f(n):\n    return load_module(n)\n",
            "path load": "import importlib.util\n\ndef f(p):\n    return importlib.util.spec_from_file_location('x', p)\n",
            "source loader": "import importlib.machinery\n\ndef f(p):\n    return importlib.machinery.SourceFileLoader('x', p)\n",
            "run_path": "import runpy\n\ndef f(p):\n    return runpy.run_path(p)\n",
        }
        for name, source in sources.items():
            with self.subTest(site=name):
                problems = self.problems(parts={"low.py": source})
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("switchboard/low.py has 1 dynamic import(s) the test does not follow", problems[0])
                row = "| `switchboard/low.py` | 1 | a site |\n"
                self.assertEqual(self.problems(parts={"low.py": source}, doc_dynamic=row), [])
                self.assertEqual(self.problems(parts={"low.py": source}, doc_dynamic="| `switchboard/low.py` | 2 | a site |\n"),
                                 ["switchboard/low.py has 1 dynamic import(s) not followed, the table says 2: write 1"])
        self.assertEqual(self.problems(parts={"low.py": "X = 1\n"}, doc_dynamic="| `switchboard/low.py` | 1 | a site |\n"),
                         ["the table 'Dynamic imports not followed' lists switchboard/low.py, which has none any more: delete the row"])
        constant = {"low.py": "import importlib\n\ndef f():\n    return importlib.import_module('json') or __import__('os') or load_module('x')\n"}
        self.assertEqual(self.problems(parts=constant), [], "constant-string loads are followed, so they are not listed as blind spots")


if __name__ == "__main__":
    unittest.main()
