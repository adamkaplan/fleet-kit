"""Rebinding a moved name for a test: the one place that follows a name to every support module that holds it.

A moved fake or helper reads `sb` (or another moved global) from its own module, not from the test module, so a test that rebinds the name in the test
module (`mock.patch.dict(globals(), {"sb": module})`) must rebind it in the support modules too, or the moved code keeps reading the old object."""
import contextlib
import sys
from unittest import mock


@contextlib.contextmanager
def rebound(name, value):
    """For the length of the block, `name` is `value` in every support module that has a global of that name."""
    modules = [module for key, module in list(sys.modules.items()) if key.startswith("support.") and name in vars(module)]
    if not modules:
        raise LookupError("no support module has a global named %r: a typo, or the module is not imported yet" % name)
    with contextlib.ExitStack() as stack:
        for module in modules:
            stack.enter_context(mock.patch.object(module, name, value))
        yield


def spliced_head(cache, src, real_ast):
    """The tree of `src`, the daemon file with a few lines changed: when it has as many lines as the real file, only the module-level
    definitions that hold a changed line are parsed again (at their own line numbers) and put in place of theirs in the real tree; the rest of the
    file is the same text. Another shape is parsed whole. A head that differs only by trailing ` # x` comments IS the real tree (and its closure)."""
    base, head = cache["source"].split("\n"), src.split("\n")
    tree = None
    if len(base) == len(head):
        changed = [k + 1 for k, (a, b) in enumerate(zip(base, head)) if a != b]
        if all(head[k - 1].endswith("  # x") and head[k - 1][:-5] == base[k - 1] for k in changed):
            cache.setdefault("same_closure_ids", set()).add(id(cache["tree"]))
            tree = cache["tree"]
        else:
            body = cache["tree"].body
            span = lambda n: (min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])]), n.end_lineno)
            hit = [i for i, n in enumerate(body) if any(span(n)[0] <= k <= span(n)[1] for k in changed)]
            if hit and len(hit) == hit[-1] - hit[0] + 1 and all(any(span(body[i])[0] <= k <= span(body[i])[1] for i in hit) for k in changed):
                first, last = span(body[hit[0]])[0], span(body[hit[-1]])[1]
                try:
                    parsed = real_ast.parse("\n" * (first - 1) + "\n".join(head[first - 1:last])).body
                    tree = real_ast.Module(body=body[:hit[0]] + parsed + body[hit[-1] + 1:], type_ignores=[])
                except SyntaxError:
                    tree = None
    if tree is None:
        return real_ast.parse(src)
    cache["heads"][src] = tree
    return tree
