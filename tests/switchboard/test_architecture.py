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
    """The "Dynamic imports not followed" table as {(unit, site): count}."""
    found = {}
    for line in _section(text, "Dynamic imports not followed").splitlines():
        if line.startswith("| `"):
            unit, site, count, _what = (_cells(line) + [""] * 4)[:4]
            found[(unit.strip("`"), site.strip("`"))] = int(count)
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


# The rule is about the modules that can load code by name, not about the names of their functions (that list never ends): any import of one of
# them, any use of a name bound to one, and any use of `sys.modules`, is a site, together with the builtins `__import__`, `exec`, `eval` and `compile`
# and the daemon's own `load_module`. The one exception is a direct call with a constant module name, which is followed. It over-counts by design
# and fails closed: a use the test did not foresee is a site, not silence. What it does NOT cover is a way to load code that involves none of these
# modules or builtins (a module object handed in by a caller, `globals()[name]`): that is outside the stated rule.
LOADER_MODULES = ("importlib", "pkgutil", "runpy", "pydoc", "imp", "zipimport", "builtins", "__builtins__")
BUILTIN_NAMES = ("__import__", "exec", "eval", "compile", "load_module")
NAMED_BY_STRING = BUILTIN_NAMES + ("import_module", "__builtins__", "builtins")   # a builtin named by a string key: a use of the builtin
FOLLOWED = ("__import__", "import_module", "load_module")   # followed only as a direct call with a constant string; every other use is a site


def _direct_constant(call):
    """The constant module name of a call such as `__import__("x")` or `import_module("x")`, or None when the argument is not a constant string, is
    relative (a leading dot) or comes with a package."""
    arg = call.args[0] if call.args else None
    if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)) or arg.value.startswith(".") or not arg.value:
        return None
    if len(call.args) > 1 or any(k.arg is None or k.arg in ("package", "globals", "locals", "fromlist", "level") for k in call.keywords):
        return None
    return arg.value


def _walk_scopes(tree):
    """Every node with the dotted def and class path it sits in (`<module>` at the top level)."""
    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            inner = stack + [child.name] if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else stack
            yield child, ".".join(inner) or "<module>"
            yield from visit(child, inner)
    yield from visit(tree, [])


def _bindings(tree):
    """{name: (module, original name or None)} for every name an `import` or `from ... import` binds to one of the loader modules or to `sys.modules`,
    tracked by name over the whole file (an alias over-counts, which is safe)."""
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                root = item.name.split(".")[0]
                if root in LOADER_MODULES:
                    bound[item.asname or root] = (root, None)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".")[0]
            for item in node.names:
                if root in LOADER_MODULES:
                    bound[item.asname or item.name] = (root, item.name)
                elif node.module == "sys" and item.name == "modules":
                    bound[item.asname or item.name] = ("sys.modules", None)
    return bound


SAFE_SYS = ("argv", "executable", "exit", "path", "platform", "stderr", "stdin", "stdout", "version_info")   # the plain attributes of `sys` that load nothing


def _followed_primitive(func, bound):
    """`import_module`, `__import__` or `load_module` when the called expression spells it directly, else None."""
    if isinstance(func, ast.Name):
        if func.id in ("__import__", "load_module"):
            return func.id
        module, original = bound.get(func.id, (None, None))
        if module in ("importlib", "builtins") and original in ("import_module", "__import__"):
            return original
    elif isinstance(func, ast.Attribute) and func.attr in FOLLOWED:
        if func.attr == "load_module":
            return "load_module"
        base = func.value
        if isinstance(base, ast.Name) and ((bound.get(base.id) or (base.id, None))[0] in ("importlib", "builtins", "__builtins__")):
            return func.attr
    return None


def dynamic_uses(path):
    """[(line, scope, kind, how, constant)] for every use of a module that can load code by name, of `sys.modules` and of the loading builtins. `how` is
    `followed` (a direct call with a constant module name: `constant` holds it) or `site`. A kind is `import(module)` for an import statement, the module
    name for a use of the module object or of a name bound to it (however it is reached: an attribute, `getattr`, a subscript, an argument), `sys.modules`,
    or the builtin name, with `(non-constant)`, `(relative)` or `(reference)` where it is not a plain call."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    bound = _bindings(tree)
    sys_names = {"sys"} | {item.asname for node in ast.walk(tree) if isinstance(node, ast.Import) for item in node.names if item.name == "sys" and item.asname} \
        | {item.asname or item.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module != "sys" for item in node.names if item.name == "sys"}
    parent = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    uses, consumed, call_of = [], set(), {}
    for node, scope in _walk_scopes(tree):
        if isinstance(node, ast.Call):
            call_of[id(node.func)] = node
            primitive = _followed_primitive(node.func, bound)
            if primitive is not None:
                constant = _direct_constant(node)
                if constant is not None:
                    uses.append((node.lineno, scope, primitive, "followed", constant))
                    consumed.update(id(n) for n in ast.walk(node.func))
                    if constant.split(".")[0] in LOADER_MODULES + ("sys",):   # `__import__("sys")` hands back the module object itself
                        uses.append((node.lineno, scope, "import(%s)" % constant.split(".")[0], "site", None))
    for node, scope in _walk_scopes(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name.split(".")[0] in LOADER_MODULES:
                    uses.append((node.lineno, scope, "import(%s)" % item.name.split(".")[0], "site", None))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level == 0 and root in LOADER_MODULES:
                uses.append((node.lineno, scope, "import(%s)" % root, "site", None))
            elif node.level == 0 and node.module == "sys":
                for item in node.names:
                    if item.name not in SAFE_SYS:   # modules, __dict__, *, and anything else that is not a plain attribute
                        uses.append((node.lineno, scope, "import(sys.%s)" % item.name, "site", None))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and id(node) not in consumed and node.id in sys_names and node.id not in bound:
            above = parent.get(id(node))
            if isinstance(above, ast.Attribute) and above.value is node:
                if above.attr not in SAFE_SYS:
                    uses.append((node.lineno, scope, "sys.modules" if above.attr == "modules" else "sys.%s" % above.attr, "site", None))
            else:   # `m = sys`, `vars(sys)`, `getattr(sys, name)`: the module object itself
                uses.append((node.lineno, scope, "sys(object)", "site", None))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and id(node) not in consumed:
            if node.id in bound:
                uses.append((node.lineno, scope, bound[node.id][0], "site", None))
            elif node.id in ("builtins", "__builtins__"):   # never imported by name: `__builtins__` is always there
                uses.append((node.lineno, scope, node.id, "site", None))
            elif node.id in BUILTIN_NAMES:
                call = call_of.get(id(node))
                if call is None:
                    kind = node.id + "(reference)"
                elif node.id in ("exec", "eval", "compile"):
                    kind = node.id
                else:
                    first = call.args[0] if call.args else None
                    relative = isinstance(first, ast.Constant) and isinstance(first.value, str) and (first.value.startswith(".") or len(call.args) > 1 or bool(call.keywords))
                    kind = node.id + ("(relative)" if relative else "(non-constant)")
                uses.append((node.lineno, scope, kind, "site", None))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr == "sys":   # `os.sys.modules`: the module object off another module
            uses.append((node.lineno, scope, "sys(object)", "site", None))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr in NAMED_BY_STRING and id(node) not in consumed \
                and not (node.attr == "compile" and isinstance(node.value, ast.Name) and node.value.id in ("re", "regex")) \
                and not (isinstance(node.value, ast.Name) and (node.value.id in bound or node.value.id in ("builtins", "__builtins__"))):   # the module use is the site
            call = call_of.get(id(node))   # `obj.load_module(n)`, `g["__builtins__"].__import__(n)`, `.eval(t)`: a loading builtin reached on any object
            if call is None:
                kind = node.attr + "(reference)"
            elif node.attr in ("exec", "eval", "compile"):
                kind = node.attr
            else:
                first = call.args[0] if call.args else None
                relative = isinstance(first, ast.Constant) and isinstance(first.value, str) and (first.value.startswith(".") or len(call.args) > 1 or bool(call.keywords))
                kind = node.attr + ("(relative)" if relative else "(non-constant)")
            uses.append((node.lineno, scope, kind, "site", None))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in NAMED_BY_STRING:
            uses.append((node.lineno, scope, "%s(string)" % node.value, "site", None))   # a loading builtin or `__builtins__` named by a string, wherever it is used
    return sorted(uses, key=lambda u: (u[0], u[2], u[3]))


def loaded_modules(path, helpers):
    """The flat modules a file of the daemon loads: a direct `load_module("x")` and the `load_x()` helpers."""
    found = {constant for _line, _scope, kind, how, constant in dynamic_uses(path) if how == "followed" and kind == "load_module"}
    for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in helpers:
            found.add(helpers[node.func.id])
    return found


def imported_modules(path, flat):
    """The flat modules a file imports: `import x`, `from x import y`, the relative `from . import x` and `from .x import y`, and a direct
    `__import__("x")` or `import_module("x")` with a constant name."""
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
    for _line, _scope, kind, how, constant in dynamic_uses(path):
        if how == "followed" and kind in ("__import__", "import_module"):
            found.add(constant.split(".")[0])
    return found & flat


def unfollowed_sites(path):
    """[(line, "scope:kind")] of every use the derivation does not follow: all of dynamic_uses that is not a direct constant call, and a relative
    import of level two or more."""
    sites = [(line, "%s:%s" % (scope, kind)) for line, scope, kind, how, _constant in dynamic_uses(path) if how == "site"]
    for node, scope in _walk_scopes(ast.parse(Path(path).read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level >= 2:
            sites.append((node.lineno, "%s:from-import(relative level %d)" % (scope, node.level)))
    return sorted(sites)


def unfollowed(root, units):
    """{(unit, "scope:kind"): count}: each unfollowed site by its identity (the enclosing def and the kind), not by line and not only by unit, so that
    swapping one site for another is visible."""
    found = collections.Counter()
    for unit in unit_files(root):
        if unit in units:
            for _line, what in unfollowed_sites(Path(root) / "bin" / unit):
                found[(unit, what)] += 1
    return dict(found)


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
    for key, count in sorted(seen.items()):
        if key not in listed:
            problems.append("%s has %d dynamic import(s) at %s that the test does not follow and the table 'Dynamic imports not followed' does not list" % (key[0], count, key[1]))
        elif listed[key] != count:
            problems.append("%s has %d dynamic import(s) at %s not followed, the table says %d: write %d" % (key[0], count, key[1], listed[key], count))
    for key in sorted(set(listed) - set(seen)):
        problems.append("the table 'Dynamic imports not followed' lists %s at %s, which has none any more: delete the row" % key)
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

    def test_the_document_names_every_module_and_builtin_the_dynamic_import_rule_uses(self):
        section = _section(self.text, "Dynamic imports not followed")
        for name in LOADER_MODULES + BUILTIN_NAMES + ("sys.modules", "import_module"):
            with self.subTest(name=name):
                self.assertIn(name, section, "the rule uses %s: the document must name it" % name)

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
        text = "## Units\n\n| Unit | Layer | Job | Also may use |\n|---|---|---|---|\n%s\n## Known violations\n\n| From | To | Kind | Count | Why |\n|---|---|---|---|---|\n%s\n## Dynamic imports not followed\n\n| Unit | Site | Count | What |\n|---|---|---|---|\n%s" % (units, doc_violations, doc_dynamic)
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
                problems = [p for p in self.problems(flat={"low.py": source, "high.py": "X = 1\n"}, doc_units={"low.py": 1, "high.py": 3}) if p.startswith("new violation")]
                self.assertEqual(len(problems), 1, problems)   # the import of the module itself is a site too, which is not a violation of the layers
                self.assertIn("new violation low.py -> high.py (import, 1)", problems[0])

    def test_a_part_that_loads_a_flat_module_directly_or_imports_it_is_followed(self):
        for source in ("def f():\n    return load_module('high')\n", "import high\n"):
            with self.subTest(source=source):
                problems = self.problems(parts={"low.py": source}, flat={"high.py": "X = 1\n"}, doc_units={"high.py": 3})
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("new violation switchboard/low.py -> high.py (import, 1)", problems[0])

    IM = "<module>:import(importlib)"

    # Every form below is a way to load code by name that the derivation does not follow: each is a list of sites (the function it sits in and the
    # kind of use), one per use, and the test proves each form is listed, and that a wrong count is named. A new form is one new line.
    DYNAMIC_FORMS = {
        # the spellings
        "non-constant import_module": ("import importlib\n\ndef f(n):\n    return importlib.import_module(n)\n", [IM, "f:importlib"]),
        "non-constant __import__": ("def f(n):\n    return __import__(n)\n", ["f:__import__(non-constant)"]),
        "non-constant load_module": ("def f(n):\n    return load_module(n)\n", ["f:load_module(non-constant)"]),
        "path load": ("import importlib.util\n\ndef f(p):\n    return importlib.util.spec_from_file_location('x', p)\n", [IM, "f:importlib"]),
        "source loader": ("import importlib.machinery\n\ndef f(p):\n    return importlib.machinery.SourceFileLoader('x', p)\n", [IM, "f:importlib"]),
        "run_path": ("import runpy\n\ndef f(p):\n    return runpy.run_path(p)\n", ["<module>:import(runpy)", "f:runpy"]),
        "run_module": ("import runpy\n\ndef f(n):\n    return runpy.run_module(n)\n", ["<module>:import(runpy)", "f:runpy"]),
        "module_from_spec": ("import importlib.util\n\ndef f(s):\n    return importlib.util.module_from_spec(s)\n", [IM, "f:importlib"]),
        "find_spec": ("import importlib.util\n\ndef f():\n    return importlib.util.find_spec('high')\n", [IM, "f:importlib"]),
        "reload": ("import importlib\n\ndef f(m):\n    importlib.reload(m)\n", [IM, "f:importlib"]),
        "exec of text": ("def f(text):\n    exec(text)\n", ["f:exec"]),
        "eval of text": ("def f(text):\n    return eval(text)\n", ["f:eval"]),
        "compile of text": ("def f(text):\n    return compile(text, 'x', 'exec')\n", ["f:compile", "f:exec(string)"]),   # the mode string is a use of the name too: over-counting by design
        "builtins.exec": ("import builtins\n\ndef f(text):\n    builtins.exec(text)\n", ["<module>:import(builtins)", "f:builtins"]),
        "constant exec": ("def f():\n    exec('import high')\n", ["f:exec"]),
        # round 1: what the primitive is BOUND to
        "alias by assignment": ("imp = __import__\n\ndef f(n):\n    return imp(n)\n", ["<module>:__import__(reference)"]),
        "alias with a constant call": ("imp = __import__\n\ndef f():\n    return imp('high')\n", ["<module>:__import__(reference)"]),
        "from-import alias": ("from importlib import import_module as im\n\ndef f(n):\n    return im(n)\n", [IM, "f:importlib"]),
        "getattr call": ("import importlib\n\ndef f(n):\n    return getattr(importlib, 'import_module')(n)\n", [IM, "f:importlib", "f:import_module(string)"]),
        "getattr stored": ("import importlib\nload = getattr(importlib, 'import_module')\n", [IM, "<module>:importlib", "<module>:import_module(string)"]),
        "alias chain": ("a = __import__\nb = a\nc = b\nd = c\ne = d\n\ndef f(n):\n    return e(n)\n", ["<module>:__import__(reference)"]),
        "relative level two": ("from ..high import X\n", ["<module>:from-import(relative level 2)"]),
        # round 2: not an assignment statement at all
        "parameter default": ("def f(n, imp=__import__):\n    return imp(n)\n", ["f:__import__(reference)"]),
        "parameter default with a constant call": ("def f(imp=__import__):\n    return imp('high')\n", ["f:__import__(reference)"]),
        "class attribute": ("class C:\n    imp = __import__\n\n    def run(self, n):\n        return self.imp(n)\n", ["C:__import__(reference)"]),
        "conditional expression": ("imp = __import__ if True else None\n", ["<module>:__import__(reference)"]),
        "annotated assignment": ("imp: object = __import__\n", ["<module>:__import__(reference)"]),
        "walrus": ("def f(n):\n    return (imp := __import__)(n)\n", ["f:__import__(reference)"]),
        "tuple assignment": ("a, b = __import__, 1\n", ["<module>:__import__(reference)"]),
        "functools.partial": ("import functools\n\ndef f():\n    return functools.partial(__import__, 'high')\n", ["f:__import__(reference)"]),
        "map": ("def f(names):\n    return list(map(__import__, names))\n", ["f:__import__(reference)"]),
        "dict value": ("TABLE = {'i': __import__}\n\ndef f(n):\n    return TABLE['i'](n)\n", ["<module>:__import__(reference)"]),
        "globals lookup by the builtin's name": ("def f(n):\n    return globals()['__import__'](n)\n", ["f:__import__(string)"]),
        "getattr with a variable name": ("import importlib\n\ndef f(k, n):\n    return getattr(importlib, k)(n)\n", [IM, "f:importlib"]),
        "relative through the package argument": ("import importlib\n\ndef f():\n    return importlib.import_module('..high', 'pkg')\n", [IM, "f:importlib"]),
        "constant with a package argument": ("import importlib\n\ndef f():\n    return importlib.import_module('high', 'pkg')\n", [IM, "f:importlib"]),
        # round 3: the module is the unit of the rule, however it is reached
        "getattr on an alias of the module": ("import importlib as il\n\ndef f(k, n):\n    return getattr(il, k)(n)\n", [IM, "f:importlib"]),
        "module bound to another name": ("import importlib\nm = importlib\n\ndef f(k, n):\n    return getattr(m, k)(n)\n", [IM, "<module>:importlib"]),
        "sys.modules": ("import sys\n\ndef f(k, n):\n    return getattr(sys.modules['importlib'], k)(n)\n", ["f:sys.modules"]),
        "sys.modules imported by name": ("from sys import modules\n\ndef f(k):\n    return modules[k]\n", ["<module>:import(sys.modules)", "f:sys.modules"]),
        "sys.modules through an alias of sys": ("import sys as s\n\ndef f(k):\n    return s.modules[k]\n", ["f:sys.modules"]),
        "getattr(sys, 'modules')": ("import sys\n\ndef f(k):\n    return getattr(sys, 'modules')[k]\n", ["f:sys(object)"]),
        "getattr(sys, <variable>)": ("import sys\n\ndef f(k):\n    return getattr(sys, k)\n", ["f:sys(object)"]),
        "vars(sys)['modules']": ("import sys\n\ndef f(k):\n    return vars(sys)['modules'][k]\n", ["f:sys(object)"]),
        "vars(sys).get('modules')": ("import sys\n\ndef f():\n    return vars(sys).get('modules')\n", ["f:sys(object)"]),
        "sys.__dict__['modules']": ("import sys\n\ndef f(k):\n    return sys.__dict__['modules'][k]\n", ["f:sys.__dict__"]),
        "sys.__dict__.get('modules')": ("import sys\n\ndef f():\n    return sys.__dict__.get('modules')\n", ["f:sys.__dict__"]),
        "sys bound to another name": ("import sys\nm = sys\n\ndef f(k):\n    return m.modules[k]\n", ["<module>:sys(object)"]),
        "sys.modules off __import__('sys')": ("def f():\n    return __import__('sys').modules\n", ["f:import(sys)"]),
        "sys.modules off import_module('sys')": ("import importlib\n\ndef f():\n    return importlib.import_module('sys').modules\n", [IM, "f:import(sys)"]),
        "sys off another module": ("import os\n\ndef f(k):\n    return os.sys.modules[k]\n", ["f:sys(object)"]),
        "sys imported from another module": ("from os import sys as s\n\ndef f(k):\n    return s.modules[k]\n", ["f:sys.modules"]),
        "builtins by the string key, then __import__": ("def f():\n    return globals()['__builtins__'].__import__('x')\n", ["f:__builtins__(string)", "f:__import__(non-constant)"]),
        "builtins off __globals__, then eval": ("def f(g, t):\n    return g.__globals__['__builtins__'].eval(t)\n", ["f:__builtins__(string)", "f:eval"]),
        "getattr for the builtins": ("def f(g):\n    return getattr(g, '__builtins__')\n", ["f:__builtins__(string)"]),
        "a loading builtin as an attribute of any object": ("def f(o):\n    return o.__import__\n", ["f:__import__(reference)"]),
        "exec as an attribute of any object": ("def f(o, t):\n    o.exec(t)\n", ["f:exec"]),
        "the builtin by its string key through .get": ("def f(n):\n    return globals().get('__import__')(n)\n", ["f:__import__(string)"]),
        "the builtins module by its string key through .get": ("def f():\n    return globals().get('__builtins__')\n", ["f:__builtins__(string)"]),
        "the builtin by its string key through .pop": ("def f(d):\n    return d.pop('exec')\n", ["f:exec(string)"]),
        "the builtin by its string key through setdefault": ("def f(d):\n    return d.setdefault('eval', None)\n", ["f:eval(string)"]),
        "the builtin by its string key through itemgetter": ("import operator\n\ndef f(n):\n    return operator.itemgetter('__import__')(globals())(n)\n", ["f:__import__(string)"]),
        "a loading builtin as a dict key": ("TABLE = {'compile': 1}\n", ["<module>:compile(string)"]),
        "a constant name with **kwargs": ("import importlib\n\ndef f(kw):\n    return importlib.import_module('high', **kw)\n", [IM, "f:importlib"]),
        "star import of sys": ("from sys import *\n", ["<module>:import(sys.*)"]),
        "star import of a loader module": ("from importlib import *\n", [IM]),
        "load_module on an object": ("def f(loader, n):\n    return loader.load_module(n)\n", ["f:load_module(non-constant)"]),
        "load_module on a nested object": ("def f(s, n):\n    return s.loader.load_module(n)\n", ["f:load_module(non-constant)"]),
        "load_module on an object, relative": ("def f(loader):\n    return loader.load_module('.x', 'p')\n", ["f:load_module(relative)"]),
        "load_module stored": ("def f(loader):\n    return loader.load_module\n", ["f:load_module(reference)"]),
        "vars of the module": ("import importlib\n\ndef f(k, n):\n    return vars(importlib)[k](n)\n", [IM, "f:importlib"]),
        "__dict__ of the module": ("import importlib\n\ndef f(k, n):\n    return importlib.__dict__[k](n)\n", [IM, "f:importlib"]),
        "__dict__ of builtins": ("import builtins\n\ndef f(n):\n    return builtins.__dict__['__import__'](n)\n", ["<module>:import(builtins)", "f:builtins", "f:__import__(string)"]),
        "pkgutil.resolve_name": ("import pkgutil\n\ndef f(n):\n    return pkgutil.resolve_name(n)\n", ["<module>:import(pkgutil)", "f:pkgutil"]),
        "pydoc.locate": ("import pydoc\n\ndef f(n):\n    return pydoc.locate(n)\n", ["<module>:import(pydoc)", "f:pydoc"]),
        "operator.attrgetter on the module": ("import importlib, operator\n\ndef f():\n    return operator.attrgetter('import_module')(importlib)('high')\n", [IM, "f:importlib", "f:import_module(string)"]),
        "zipimport": ("import zipimport\n\ndef f(p):\n    return zipimport.zipimporter(p)\n", ["<module>:import(zipimport)", "f:zipimport"]),
        "builtins bound to another name": ("import builtins\nb = builtins\n\ndef f(t):\n    b.exec(t)\n", ["<module>:import(builtins)", "<module>:builtins", "f:exec"]),
        "spec_from_loader": ("from importlib.util import spec_from_loader\n\ndef f(l):\n    return spec_from_loader('x', l)\n", [IM, "f:importlib"]),
        "loader classes": ("from importlib.machinery import SourcelessFileLoader, ExtensionFileLoader\n\ndef f(p):\n    return SourcelessFileLoader('x', p), ExtensionFileLoader('y', p)\n",
                           [IM, "f:importlib", "f:importlib"]),
        "imp.load_compiled": ("import imp\n\ndef f(p):\n    return imp.load_compiled('x', p)\n", ["<module>:import(imp)", "f:imp"]),
    }

    def messages(self, sites):
        counts = collections.Counter(sites)
        return ["switchboard/low.py has %d dynamic import(s) at %s that the test does not follow and the table 'Dynamic imports not followed' does not list" % (c, site)
                for site, c in sorted(counts.items())]

    def test_every_dynamic_import_the_test_cannot_follow_is_a_listed_site_by_function_and_kind(self):
        for required in ("alias by assignment", "parameter default", "walrus", "functools.partial", "getattr with a variable name", "sys bound to another name",
                         "sys off another module", "builtins by the string key, then __import__", "pkgutil.resolve_name", "operator.attrgetter on the module",
                         "load_module on an object", "relative through the package argument"):
            self.assertIn(required, self.DYNAMIC_FORMS)
        for name, (source, sites) in self.DYNAMIC_FORMS.items():
            with self.subTest(form=name):
                self.assertEqual(self.problems(parts={"low.py": source}), self.messages(sites))
                rows = "".join("| `switchboard/low.py` | `%s` | %d | a site |\n" % (site, c) for site, c in sorted(collections.Counter(sites).items()))
                self.assertEqual(self.problems(parts={"low.py": source}, doc_dynamic=rows), [])
                first = sorted(collections.Counter(sites).items())[0]
                wrong = rows.replace("`%s` | %d |" % first, "`%s` | %d |" % (first[0], first[1] + 1), 1)
                self.assertEqual(self.problems(parts={"low.py": source}, doc_dynamic=wrong),
                                 ["switchboard/low.py has %d dynamic import(s) at %s not followed, the table says %d: write %d" % (first[1], first[0], first[1] + 1, first[1])])

    def test_what_is_outside_the_stated_rule_is_silent_and_says_so(self):
        outside = {
            "a module object handed in by the caller": "def f(m, n):\n    return getattr(m, n)\n",
            "exec_module on a spec that came from elsewhere": "def f(s, m):\n    s.loader.exec_module(m)\n",
            "globals() with a variable key": "def f(k, n):\n    return globals()[k](n)\n",
            "a local variable named like a module that is not imported": "def f(imp):\n    return imp.x\n",
            "re.compile": "import re\nP = re.compile('x')\n",
            "reload on another object": "def f(x):\n    return x.reload()\n",
            "regex.compile as an attribute": "import regex\nP = regex.compile('x')\n",
        }
        for attribute in SAFE_SYS:
            outside["sys.%s" % attribute] = "import sys\n\ndef f():\n    return sys.%s\n" % attribute
        for name, source in outside.items():
            with self.subTest(form=name):
                self.assertEqual(self.problems(parts={"low.py": source}), [])

    def test_a_listed_site_that_is_gone_and_a_swapped_site_are_reported(self):
        gone = self.problems(parts={"low.py": "X = 1\n"}, doc_dynamic="| `switchboard/low.py` | `f:exec` | 1 | a site |\n")
        self.assertEqual(gone, ["the table 'Dynamic imports not followed' lists switchboard/low.py at f:exec, which has none any more: delete the row"])
        swapped = self.problems(parts={"low.py": "def g(text):\n    exec(text)\n"}, doc_dynamic="| `switchboard/low.py` | `f:exec` | 1 | a site |\n")
        self.assertEqual(len(swapped), 2, swapped)
        self.assertTrue(any("at g:exec" in p for p in swapped) and any("at f:exec" in p for p in swapped), swapped)

    def test_a_direct_call_with_a_constant_module_name_is_followed_and_nothing_else_is(self):
        followed = {
            "importlib.import_module": "import importlib\n\ndef f():\n    return importlib.import_module('high')\n",
            "__import__": "def f():\n    return __import__('high')\n",
            "bare import_module": "from importlib import import_module\n\ndef f():\n    return import_module('high')\n",
        }
        for name, source in followed.items():
            with self.subTest(form=name):
                problems = self.problems(parts={"low.py": source}, flat={"high.py": "X = 1\n"}, doc_units={"high.py": 3})
                self.assertTrue(any("new violation switchboard/low.py -> high.py (import, 1)" in p for p in problems), problems)
                self.assertFalse(any("f:" in p for p in problems), "the followed call itself is not a site: %s" % problems)
        not_followed = {
            "getattr called at once": "import importlib\n\ndef f():\n    return getattr(importlib, 'import_module')('high')\n",
            "alias": "imp = __import__\n\ndef f():\n    return imp('high')\n",
        }
        for name, source in not_followed.items():
            with self.subTest(form=name):
                problems = self.problems(parts={"low.py": source}, flat={"high.py": "X = 1\n"}, doc_units={"high.py": 3})
                self.assertFalse(any("(import, 1)" in p and "-> high.py" in p for p in problems), "never an edge: %s" % problems)
                self.assertTrue(any("dynamic import(s)" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
