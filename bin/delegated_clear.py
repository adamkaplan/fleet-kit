"""The Chief of Staff's clear under the owner's standing order (#661).

The owner's own `impact clear` lifts an impact hold at the pull request's head. The owner can also order, as a numbered standing order on a charter, that the Chief of Staff may clear
ordinary held pull requests and tell him after. This module is the CHECK of that delegated clear (the command and its audit are in `bin/fleet-switchboard`); it decides nothing else.
It is OFF unless the configuration (`delegated_clear`) names the order numbers that delegate a clear and the charter that holds them. It refuses: any caller but the Chief of Staff, an
order that is not listed or does not exist, a pull request whose owner-hold decision names a path of a class the matrix lists as never delegated (credentials, identity), one whose
independent review's class is a top tier class (credentials, identity or authority), one that changes ANY file of the clear code or of what it depends on (the daemon file included),
and anything it cannot read.

Pure: plain values in, a list of reasons out (empty means allowed). The classes and the clear code are DATA in the matrix file (`owner_hold.delegated_clear`): this module holds no path or class.
"""
import ast
import base64
import hashlib
import re

CONFIG_KEY = "delegated_clear"   # the module declares its own configuration key (bin/fleet-switchboard config_declarations)
CONFIG_DEFAULT = None            # None: no default is added; absent = off
MAX_ORDERS = 20


def order_hash(text):
    """The hash the configuration binds an order to: sha256 of the order's text (the bullet as `orders list --json` shows it), so an edit, an insertion or a removal above it revokes the delegation."""
    return hashlib.sha256(str(text).strip().encode("utf-8")).hexdigest()


def _entry_ok(entry):
    return (isinstance(entry, dict) and set(entry) == {"number", "sha256"} and isinstance(entry["number"], int) and not isinstance(entry["number"], bool) and entry["number"] >= 1
            and isinstance(entry["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is not None)


def validate(raw, source):
    """`delegated_clear`: absent, null or false = off; else {"orders": [{"number": N, "sha256": "<hash of the order's text>"}, ...], "charter": the charter issue number that holds them}. An empty
    list is off. Raises ValueError naming the key."""
    if raw is None or raw is False:
        return
    if not isinstance(raw, dict) or set(raw) - {"orders", "charter"}:
        raise ValueError("%s: 'delegated_clear' must be false or an object with only orders and charter, got %r" % (source, raw))
    orders, charter = raw.get("orders"), raw.get("charter")
    if not (isinstance(orders, list) and len(orders) <= MAX_ORDERS and all(_entry_ok(e) for e in orders)) or len({e["number"] for e in orders}) != len(orders):
        raise ValueError("%s: delegated_clear.orders must be a list of {\"number\": N, \"sha256\": <64 hex of the order's text>} (an empty list is off; a number may appear once)" % source)
    if not (isinstance(charter, int) and not isinstance(charter, bool) and charter >= 1):
        raise ValueError("%s: delegated_clear.charter must be the charter's issue number" % source)


def settings(config):
    """({number: sha256}, charter) when the delegated clear is configured and well formed, else None: not enabled (absent, empty, malformed, false: all off)."""
    raw = (config or {}).get(CONFIG_KEY) if isinstance(config, dict) else None
    try:
        validate(raw, "config")
    except ValueError:
        return None
    return ({e["number"]: e["sha256"] for e in raw["orders"]}, raw["charter"]) if isinstance(raw, dict) and raw["orders"] else None


def caller_is_chief(kind):
    """True for the Chief of Staff's own session: `kind` is `gates_caller_kind`'s answer (an agent whose RECORDED role is chief-of-staff). Never read from a flag or text."""
    return isinstance(kind, tuple) and len(kind) > 2 and kind[0] == "agent" and kind[2] == "chief-of-staff"


def order_text(orders, number):
    """The text of the 1-based order `number` of a charter's `## Standing orders`, or None when there is no such order."""
    if isinstance(number, int) and not isinstance(number, bool) and isinstance(orders, list) and 1 <= number <= len(orders) and isinstance(orders[number - 1], str):
        return orders[number - 1]
    return None


def refusals(rule, report, files, matches=None, closure=None):
    """[reasons] a delegated clear is refused for this pull request; [] when none. `rule` is the matrix's `owner_hold.delegated_clear` block; `report` is the owner-hold report the
    daemon uses (`pr_floor_report`: its decision, its top tier path hits by class, the independent review's class, whether the file list is complete); `files` is the pull request's
    changed files ([{"filename", "previous_filename", "status"}]) or None when they could not be read. `matches(path, globs)` is the owner-hold floor's OWN path matcher (case-,
    normalisation- and spelling-insensitive: `./bin/x`, `bin//x`, `BIN/X`, a `..`, a rename's source all read as the file they name): the caller passes it, this module holds no second one.
    `closure` is {file name: [reasons] or None}: what `closure_refusals` said for each changed file of `closure_files` (None: it could not be mapped)."""
    if not isinstance(rule, dict) or not isinstance(report, dict) or matches is None:
        return ["the delegated-clear rule or the owner-hold decision cannot be read"]
    for key in ("never_path_classes", "top_tier_review_classes", "clear_code_paths", "new_file_globs", "closure_files"):
        if not isinstance(rule.get(key), list) or not all(isinstance(x, str) for x in rule[key]):
            return ["the delegated-clear rule is malformed (%s)" % key]
    if report.get("owner_decision") not in ("hold", "second-review", "none"):
        return ["the owner-hold decision cannot be read"]
    if report.get("complete") is not True or report.get("unknown") or report.get("renamed_no_source"):
        return ["the pull request's changes cannot be read in full (%s)" % (report.get("unknown") or "the file list is cut")]
    reasons = []
    hits = report.get("top_tier_hits") or {}
    for cls in sorted(hits):
        if cls.lower() in {c.lower() for c in rule["never_path_classes"]}:
            reasons.append("it changes a %s path (%s): that stays the owner's own clear" % (cls, ", ".join(hits[cls][:3])))
    if report.get("review_action") is None:
        reasons.append("no independent review is recorded at this head: unknown is refused")
    review = report.get("review_class")
    if review is None and report.get("review_action") == "hold":
        reasons.append("the independent review holds it with no class named: unknown is refused")
    if isinstance(review, str) and review.lower() in {c.lower() for c in rule["top_tier_review_classes"]}:
        reasons.append("the independent review's class is %s, a top tier class: that stays the owner's own clear" % review)
    if files is None:
        reasons.append("the changed files cannot be read, so a change to the clear code cannot be ruled out")
        return reasons
    named = set(report.get("changed_paths") or [])
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            reasons.append("a changed file cannot be read: refused")
            continue
        both = [x for x in (item.get("filename"), item.get("previous_filename")) if x is not None]
        if any(not isinstance(x, str) or "\x00" in x for x in both):
            reasons.append("a changed path cannot be read (%r): refused" % str(both)[:60])
            continue
        for path in both:
            if matches(path, rule["clear_code_paths"]):
                reasons.append("it changes a file that is the clear or hold machinery or that the daemon loads (%s): that stays the owner's own clear" % path[:80])
        if any(matches(path, rule["new_file_globs"]) for path in both) and (item.get("status") in ("added", "removed", "renamed", "copied") or item.get("previous_filename")):
            reasons.append("it adds, deletes or renames a file under a directory the daemon loads at start (%s): that stays the owner's own clear" % item["filename"][:80])
        for path in both:
            if matches(path, rule["closure_files"]):
                mapped = (closure or {}).get(item["filename"])
                if mapped is None:
                    reasons.append("the change to %s cannot be mapped to its definitions: refused" % path[:60])
                else:
                    reasons.extend(mapped)
    for path in sorted(p for p in named if isinstance(p, str)):
        if matches(path, rule["clear_code_paths"]):
            reasons.append("it changes a file that is the clear or hold machinery or that the daemon loads (%s): that stays the owner's own clear" % path[:80])
    return sorted(set(reasons))


# ---- the daemon file: which top-level definitions a changed line is in (ast, never hunk headers), and whether any of them is in the clear closure

DEF_KINDS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
BIND_KINDS = (ast.Assign, ast.AnnAssign, ast.AugAssign)


_RUNS_CODE = (ast.Call, ast.Lambda, ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def import_time(node, number):
    """True when line `number` of the definition `node` RUNS CODE WHEN THE MODULE IS IMPORTED: a decorator, a default or an annotation that calls something (a plain name or constant only
    binds, so a new function with a plain signature is not import-time code), or, in a class, anything that is not inside a method's body. A function's own body does not run at import.
    A body that starts on the line its signature ends on is judged as the signature (the stricter way)."""
    if isinstance(node, ast.ClassDef):
        for child in node.body:
            if isinstance(child, DEF_KINDS) and _span(child)[0] <= number <= child.end_lineno:
                return import_time(child, number)
        return True
    args = node.args
    header = list(node.decorator_list) + list(args.defaults) + [d for d in args.kw_defaults if d is not None] + ([node.returns] if node.returns is not None else [])
    header += [a.annotation for a in args.posonlyargs + args.args + args.kwonlyargs + [x for x in (args.vararg, args.kwarg) if x] if a.annotation is not None]
    header_end = max([node.lineno] + [x.end_lineno for x in ast.walk(args) if hasattr(x, "end_lineno")] + [h.end_lineno for h in header])
    if node.decorator_list and number < node.lineno:
        return True   # a decorator is applied (called) at import
    return number <= header_end and any(isinstance(x, _RUNS_CODE) for h in header for x in ast.walk(h))


def _span(node):
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return start, node.end_lineno


def _bound_names(node):
    names = set()
    if isinstance(node, BIND_KINDS):
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
            names |= {x.id for x in ast.walk(target) if isinstance(x, ast.Name)}
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        names |= {(a.asname or a.name).split(".")[0] for a in node.names}
    elif isinstance(node, DEF_KINDS):
        names.add(node.name)
    return names


def _index(tree):
    """({name: def node}, {global name: its binding nodes}) of the module level."""
    defs, binds = {}, {}
    for node in tree.body:
        if isinstance(node, DEF_KINDS):
            defs[node.name] = node
        else:
            for name in _bound_names(node):
                binds.setdefault(name, []).append(node)
    return defs, binds


def _is_dunder(name):
    return name.startswith("__") and name.endswith("__")


def closure(tree, roots):
    """(def names, global names, referenced names, {class: methods}) reachable from `roots` by NAME: every module-level definition a closure definition refers to (a call, a value, a default, a
    decorator, a base class) and every module-level binding it reads (its right-hand side, to a fixed point). A CLASS the closure refers to brings its own statements (bases, decorators, class
    body) and only those methods whose name the closure reads (as a name or an attribute) or that are special methods (`__init__`, `__getattr__`, `__call__`...): a method nothing reads is not run.
    Over-approximate by name on purpose (any attribute of that name); dynamic dispatch by string is not followed (a stated limit)."""
    defs, binds = _index(tree)
    seen, globals_, refs, members, todo = set(), set(), set(), {}, [r for r in roots]

    def walk(nodes):
        while nodes:
            for x in ast.walk(nodes.pop()):
                if isinstance(x, ast.Name):
                    refs.add(x.id)
                    if x.id in defs:
                        todo.append(x.id)
                    if x.id in binds and x.id not in globals_:   # a name that is a def AND is rebound (`f = wrap(f)`): both are followed
                        globals_.add(x.id)
                        nodes.extend(binds[x.id])
                elif isinstance(x, ast.Attribute):
                    refs.add(x.attr)

    while True:
        while todo:
            name = todo.pop()
            if name in seen or name not in defs:
                continue
            seen.add(name)
            node = defs[name]
            if isinstance(node, ast.ClassDef):
                members[name] = set()
                walk([c for c in node.body if not isinstance(c, DEF_KINDS)] + list(node.bases) + list(node.keywords) + list(node.decorator_list))
            else:
                walk([node])
        grew = False
        for name in sorted(members):   # a method the closure reads by name (or a special method) is run
            for method in defs[name].body:
                if isinstance(method, DEF_KINDS) and method.name not in members[name] and (method.name in refs or _is_dunder(method.name)):
                    members[name].add(method.name)
                    walk([method])
                    grew = True
        if not grew and not todo:
            return seen, globals_, refs, members


def changed_lines(patch):
    """(old line numbers, new line numbers, lines that could not be read) of a unified diff: the REMOVED lines are numbered in the base file and the ADDED ones in the head file; a patch that is
    not text or has a hunk header it cannot read is `None`."""
    if not isinstance(patch, str):
        return None
    old, new, number_old, number_new, in_hunk = {}, {}, 0, 0, False
    for line in patch.split("\n"):
        match = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if line.startswith("@@"):
            if match is None:
                return None
            number_old, number_new, in_hunk = int(match.group(1)), int(match.group(2)), True
        elif not in_hunk or line.startswith("\\"):
            continue
        elif line.startswith("+"):
            new[number_new] = line[1:]
            number_new += 1
        elif line.startswith("-"):
            old[number_old] = line[1:]
            number_old += 1
        else:
            number_old += 1
            number_new += 1
    return old, new


def closure_refusals(base_src, head_src, patch, roots, entry_points, module_level=False):
    """[reasons] a change to the daemon file touches the clear closure. `base_src` and `head_src` are the file before and after, `patch` the diff of it, `roots` the clear command's entry points and
    `entry_points` the names a NEW caller of is refused. The closure is computed on BOTH files and united (a pull request that ADDS a call into it counts). The changed lines are mapped to the
    top-level definition they are in by the ast of the file they belong to (removed lines against the base, added lines against the head). Refused: a closure definition changed; a module-level
    binding or import a closure definition reads changed; any other top-level statement changed; a changed definition that refers to an entry point; a line that cannot be mapped; a syntax error."""
    lines = changed_lines(patch)
    if lines is None:
        return ["the patch of the daemon file cannot be read: refused"]
    try:
        base, head = ast.parse(base_src), ast.parse(head_src)
    except (SyntaxError, ValueError):
        return ["the daemon file does not parse before or after the change: refused"]
    base_closure, head_closure = closure(base, roots), closure(head, roots)
    in_defs, in_globals, in_refs = (base_closure[0] | head_closure[0]), (base_closure[1] | head_closure[1]), (base_closure[2] | head_closure[2])
    in_members = {k: base_closure[3].get(k, set()) | head_closure[3].get(k, set()) for k in set(base_closure[3]) | set(head_closure[3])}
    reasons = []
    for side, tree, numbers in (("base", base, lines[0]), ("head", head, lines[1])):
        spans = [(_span(n), n) for n in tree.body]
        for number, text in sorted(numbers.items()):
            owners = [n for (a, b), n in spans if a <= number <= b]
            if not owners:
                if text.strip() and not text.strip().startswith("#"):
                    reasons.append("a changed line (%s line %d) is in no top-level definition and is not a comment: it cannot be mapped" % (side, number))
                continue
            for owner in owners:
                names = _bound_names(owner)
                if isinstance(owner, DEF_KINDS):
                    method = next((c for c in owner.body if isinstance(c, DEF_KINDS) and _span(c)[0] <= number <= c.end_lineno), None) if isinstance(owner, ast.ClassDef) else None
                    unit = method or owner   # a changed line inside one method is judged by that method, not by the whole class
                    used = entry_points & {x.id for x in ast.walk(unit) if isinstance(x, ast.Name)}
                    if owner.name in in_defs and (method is None or method.name in in_members.get(owner.name, set()) or owner.name in roots):
                        reasons.append("it changes %s, part of the clear (%s file)" % (owner.name if method is None else "%s.%s" % (owner.name, method.name), side))
                    elif module_level and import_time(owner, number):
                        reasons.append("it changes import-time code of a module the clear loads (%s line %d, in %s): refused" % (side, number, owner.name))
                    elif used:
                        reasons.append("it changes %s, which refers to a clear entry point (%s)" % (owner.name, ", ".join(sorted(used)[:3])))
                elif isinstance(owner, BIND_KINDS + (ast.Import, ast.ImportFrom)):
                    if module_level:
                        reasons.append("it changes import-time code of a module the clear loads (%s line %d): refused" % (side, number))
                    elif names & (in_globals | in_defs | in_refs):
                        reasons.append("it changes the module-level binding of %s, which the clear reads" % ", ".join(sorted(names & (in_globals | in_defs | in_refs))[:3]))
                    elif len(owners) > 1:
                        reasons.append("a changed line (%s line %d) holds more than one top-level statement: refused" % (side, number))
                elif isinstance(owner, ast.Expr) and isinstance(getattr(owner, "value", None), ast.Constant):
                    continue
                else:
                    reasons.append("it changes a module-level statement (%s line %d) that is not a definition or a binding: refused" % (side, number))
    return sorted(set(reasons))


def decode_blob(answer):
    """The text of a `git/blobs` answer, or None when it is not a complete base64 text."""
    if not isinstance(answer, dict) or answer.get("encoding") != "base64" or not isinstance(answer.get("content"), str):
        return None
    try:
        return base64.b64decode("".join(answer["content"].split()), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return None
