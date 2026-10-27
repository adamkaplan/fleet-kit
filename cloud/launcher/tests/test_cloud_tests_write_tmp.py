#!/usr/bin/env python3
"""Ask #1289 (DW-4): a cloud test never opens a path for writing under the repository root; it writes under the directory the
temporary-directory variable names. One scan, by syntax tree, of cloud/*/tests/*.py.

What it sees: a call of open() with a write, append, exclusive or update mode, or of write_text / write_bytes, whose path is built
from the repository root: the names ROOT, HERE, REPO_ROOT or KIT, __file__, or a name assigned from one of those (followed through
plain assignments of the same file). LIMIT (stated, not hidden): a path assembled through a helper, an alias, a star import or a
subprocess is not seen; the mutants below show the direct forms only."""
import ast
import glob
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SEEDS = {"ROOT", "HERE", "REPO_ROOT", "KIT", "__file__"}
WRITE_CHARS = set("wax+")


def _root(node):
    """The name a path expression starts from: the first argument of a join or other os.path call, the left of a `/`, the first
    piece of an f-string, the receiver of an attribute; None when it starts from something else (a call of a helper, a constant)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _root(node.value)
    if isinstance(node, ast.BinOp):
        return _root(node.left)
    if isinstance(node, ast.JoinedStr):
        return next((_root(v.value) for v in node.values if isinstance(v, ast.FormattedValue)), None)
    if isinstance(node, ast.Call):
        f = node.func
        if node.args and (isinstance(f, ast.Name) and f.id in ("Path", "str") or isinstance(f, ast.Attribute) and (
                (isinstance(f.value, ast.Attribute) and f.value.attr == "path") or f.attr in ("Path", "resolve", "joinpath"))):
            return _root(node.args[0])
        if isinstance(f, ast.Attribute) and f.attr in ("resolve", "joinpath", "parent"):
            return _root(f.value)
    return None


def _own(scope):
    """The nodes of a scope without descending into a nested function (a class body is part of the module's scope)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(n))


def _taint(scope, taint):
    taint = set(taint)
    changed = True
    while changed:
        changed = False
        for n in _own(scope):
            if isinstance(n, ast.Assign) and _root(n.value) in taint:
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id not in taint:
                        taint.add(t.id)
                        changed = True
    return taint


def _mode(call):
    mode = call.args[1] if len(call.args) > 1 else None
    for k in call.keywords:
        if k.arg == "mode":
            mode = k.value
    return mode.value if isinstance(mode, ast.Constant) and isinstance(mode.value, str) else ""


def _calls(scope, taint, hits):
    for n in _own(scope):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        if name == "open" and n.args and WRITE_CHARS & set(_mode(n)):
            path = n.args[0]
        elif name in ("write_text", "write_bytes") and isinstance(f, ast.Attribute):
            path = f.value
        else:
            continue
        if _root(path) in taint:
            hits.add(n.lineno)


def writes_under_root(source):
    """Sorted line numbers of the calls in `source` that write to a path built from the repository root. Taint is followed through
    plain assignments, per function (a name such as `path` is a temporary path in one test and a root path in another)."""
    tree = ast.parse(source)
    module = _taint(tree, SEEDS)
    hits = set()
    _calls(tree, module, hits)
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _calls(fn, _taint(fn, module), hits)
    return sorted(hits)


MUTANT_OPEN = 'import os\nROOT = os.getcwd()\nf = open(os.path.join(ROOT, "x.txt"), "w")\n'
MUTANT_WRITE_TEXT = 'import pathlib\nROOT = pathlib.Path(__file__).parent\n(ROOT / "x.txt").write_text("x")\n'
MUTANT_ALIASED = 'import os\nHERE = os.path.dirname(__file__)\ntarget = os.path.join(HERE, "x")\nopen(target, "a")\n'
CLEAN = 'import os, tempfile\nd = tempfile.mkdtemp()\nopen(os.path.join(d, "x"), "w")\nopen(__file__)\n'


class Scan(unittest.TestCase):
    def test_no_cloud_test_writes_under_the_repository_root(self):
        files = sorted(glob.glob(os.path.join(KIT, "cloud", "*", "tests", "*.py")))
        self.assertTrue(files)
        found = []
        for p in files:
            with open(p) as fh:
                found += ["%s:%d" % (os.path.relpath(p, KIT), ln) for ln in writes_under_root(fh.read())]
        self.assertEqual(found, [])

    def test_each_planted_write_is_refused_and_clean_code_passes(self):
        for name, src in (("open", MUTANT_OPEN), ("write_text", MUTANT_WRITE_TEXT), ("through a name", MUTANT_ALIASED)):
            self.assertEqual(len(writes_under_root(src)), 1, name)
        self.assertEqual(writes_under_root(CLEAN), [])


if __name__ == "__main__":
    unittest.main()
