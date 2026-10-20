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
import difflib
import hashlib
import json
import re
from pathlib import Path

CONFIG_KEY = "delegated_clear"   # the module declares its own configuration key (bin/fleet-switchboard config_declarations)
CONFIG_DEFAULT = None            # None: no default is added; absent = off
MAX_ORDERS = 20


MATRIX_FILE = Path(__file__).resolve().parent.parent / "docs" / "review-laws" / "impact-matrix.json"
OWNER_FILE = "owner-file"   # an order's `source` when its text is a line of the owner's own standing-orders file (never written by the fleet), not an order of a charter


def score_block():
    """The matrix file's `owner_hold.delegated_clear.score` block (the ladder, the delegable levels, the floor's rule ids and lowest level, the roles whose rating counts), or None when it cannot be read. #835."""
    try:
        block = json.loads(MATRIX_FILE.read_text())["owner_hold"]["delegated_clear"]["score"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    ok = (isinstance(block, dict) and all(isinstance(block.get(k), list) and block[k] and all(isinstance(x, str) for x in block[k]) for k in ("ladder", "delegable_levels", "rating_roles"))
          and isinstance(block.get("floor"), dict) and isinstance(block["floor"].get("lowest_level"), str) and isinstance(block["floor"].get("rules"), dict)
          and all(isinstance(k, str) and isinstance(v, str) for k, v in block["floor"]["rules"].items()))
    return block if ok else None


def order_hash(text):
    """The hash the configuration binds an order to: sha256 of the order's text (the bullet as `orders list --json` shows it), so an edit, an insertion or a removal above it revokes the delegation."""
    return hashlib.sha256(str(text).strip().encode("utf-8")).hexdigest()


def line_hash(line):
    """The hash a file-sourced order is bound to: sha256 of its EXACT line, trimmed of its line end only (no case folding, no other trimming)."""
    return hashlib.sha256(str(line).rstrip("\r\n").encode("utf-8")).hexdigest()


def file_order_line(text, sha):
    """The ONE line of the owner's standing-orders text whose line_hash is `sha`, or None: zero lines or more than one match, or a blank line, is None (the order is absent). No fuzzy, substring or case-folded match."""
    if not isinstance(text, str):
        return None
    found = [line.rstrip("\r\n") for line in text.split("\n") if line.strip() and line_hash(line) == sha]
    return found[0] if len(found) == 1 else None


def _entry_ok(entry):
    return (isinstance(entry, dict) and set(entry) <= {"number", "sha256", "max_level", "source"} and {"number", "sha256"} <= set(entry)
            and isinstance(entry["number"], int) and not isinstance(entry["number"], bool) and entry["number"] >= 1
            and isinstance(entry["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is not None
            and entry.get("source", OWNER_FILE) == OWNER_FILE)


def _level_problem(entry):
    """What is wrong with an entry's `max_level` (None when it is absent: clear nothing by score, or valid): a level the matrix does not list as delegable (High never is) is refused by name."""
    if "max_level" not in entry:
        return None
    block = score_block()
    if block is None:
        return "the matrix file's score block cannot be read"
    if not isinstance(entry["max_level"], str) or entry["max_level"] not in block["delegable_levels"]:
        return "must be one of %s (High is always the owner's), got %r" % (", ".join(block["delegable_levels"]), entry["max_level"])
    return None


def validate(raw, source):
    """`delegated_clear`: absent, null or false = off; else {"orders": [{"number": N, "sha256": "<hash of the order's text>", "max_level": "<highest level she may clear>"?, "source": "owner-file"?}, ...], "charter": the
    charter issue number that holds the charter-sourced orders (not needed when every order is `owner-file`)}. An empty list is off. `max_level` absent = the old rules (nothing is cleared by score). Raises ValueError naming the key."""
    if raw is None or raw is False:
        return
    if not isinstance(raw, dict) or set(raw) - {"orders", "charter"}:
        raise ValueError("%s: 'delegated_clear' must be false or an object with only orders and charter, got %r" % (source, raw))
    orders, charter = raw.get("orders"), raw.get("charter")
    if not (isinstance(orders, list) and len(orders) <= MAX_ORDERS and all(_entry_ok(e) for e in orders)) or len({e["number"] for e in orders}) != len(orders):
        raise ValueError("%s: delegated_clear.orders must be a list of {\"number\": N, \"sha256\": <64 hex of the order's text>, \"max_level\"?, \"source\"?: \"owner-file\"} (an empty list is off; a number may appear once)" % source)
    for entry in orders:
        problem = _level_problem(entry)
        if problem:
            raise ValueError("%s: delegated_clear.orders max_level of order %d: %s" % (source, entry["number"], problem))
    if any("source" not in e for e in orders) and not (isinstance(charter, int) and not isinstance(charter, bool) and charter >= 1):
        raise ValueError("%s: delegated_clear.charter must be the charter's issue number" % source)
    if charter is not None and not (isinstance(charter, int) and not isinstance(charter, bool) and charter >= 1):
        raise ValueError("%s: delegated_clear.charter must be the charter's issue number" % source)


def settings(config):
    """({number: sha256}, charter) when the delegated clear is configured and well formed, else None: not enabled (absent, empty, malformed, false: all off)."""
    raw = (config or {}).get(CONFIG_KEY) if isinstance(config, dict) else None
    try:
        validate(raw, "config")
    except ValueError:
        return None
    return ({e["number"]: e["sha256"] for e in raw["orders"]}, raw.get("charter")) if isinstance(raw, dict) and raw["orders"] else None


def entry_of(config, number):
    """The configured order entry `number` ({"number", "sha256", "max_level"?, "source"?}) when the delegated clear is enabled and well formed, else None."""
    if settings(config) is None:
        return None
    return next((e for e in config[CONFIG_KEY]["orders"] if e["number"] == number), None)


def caller_is_chief(kind):
    """True for the Chief of Staff's own session: `kind` is `gates_caller_kind`'s answer (an agent whose RECORDED role is chief-of-staff). Never read from a flag or text."""
    return isinstance(kind, tuple) and len(kind) > 2 and kind[0] == "agent" and kind[2] == "chief-of-staff"


def order_text(orders, number):
    """The text of the 1-based order `number` of a charter's `## Standing orders`, or None when there is no such order."""
    if isinstance(number, int) and not isinstance(number, bool) and isinstance(orders, list) and 1 <= number <= len(orders) and isinstance(orders[number - 1], str):
        return orders[number - 1]
    return None


def judge(block, cells, entries, report, threshold):
    """{"level": the score or None, "reasons": [why refused]} (no reasons: the score is at or below `threshold`, so the clear may go on). `block` is the matrix's score block, `cells` its `cells` (severity ->
    probability -> {"level"}), `entries` the ledger's entries (the caller filtered nothing: this checks kind, reviewer, pr, EXACT head and the recorded role again), `report` the owner-hold report (`pr`, `head`,
    `top_tier_hits` {class: [paths]}). The score is the HIGHEST level of the counted records (a later, lower one never lowers it), taken from the matrix cell of the recorded severity and probability, never from
    a word of the reviewer's. A floor class hit makes `floor.lowest_level` the lowest score allowed, and the rating's reason must name that class's rule id. Anything it cannot read is refused, never Low. #835."""
    if not isinstance(block, dict) or not isinstance(cells, dict) or not isinstance(report, dict):
        return {"level": None, "reasons": ["the score rule or the rating is unknown: refused"]}
    ladder = block["ladder"]
    if threshold not in block["delegable_levels"] or threshold not in ladder:
        return {"level": None, "reasons": ["the threshold %r is unknown: refused" % (threshold,)]}
    counted, bad = [], 0
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("kind") != "review_record" or entry.get("reviewer") != "impact" or entry.get("pr") != report.get("pr") or entry.get("head") != report.get("head") or not report.get("head"):
            continue
        if entry.get("by_role") not in block["rating_roles"]:
            continue   # the pull request's author, the Chief of Staff, any other role (or a record made before roles were kept) is not the independent rating
        level = ((cells.get(entry.get("impact_severity")) or {}).get(entry.get("impact_probability")) or {}).get("level") if isinstance(entry.get("impact_severity"), str) and isinstance(entry.get("impact_probability"), str) else None
        if level not in ladder:
            bad += 1
        else:
            counted.append((ladder.index(level), level, entry))
    if bad:
        return {"level": None, "reasons": ["a rating at this head has no severity and probability the matrix knows: the score is unknown, refused"]}
    if not counted:
        return {"level": None, "reasons": ["there is no independent impact rating at this head: the score is unknown, refused"]}
    top = max(i for i, _, _ in counted)
    level = ladder[top]
    reasons = []
    hits = report.get("top_tier_hits") or {}
    for rule_id, cls in sorted(block["floor"]["rules"].items()):
        if not hits.get(cls):
            continue
        if top < ladder.index(block["floor"]["lowest_level"]):
            reasons.append("it changes a %s path and the rating %s is below the floor %s: refused" % (cls, level, block["floor"]["lowest_level"]))
        elif not any(i == top and re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(rule_id), str(e.get("impact_reason") or "")) for i, _, e in counted):
            reasons.append("it changes a %s path and the rating's reason does not name the rule %s: the rule is not named, the rating does not count" % (cls, rule_id))
    if level not in block["delegable_levels"]:
        reasons.append("the score is %s: %s is the owner's" % (level, level))
    elif ladder.index(level) > ladder.index(threshold):
        reasons.append("the score %s is above the threshold %s" % (level, threshold))
    return {"level": level, "reasons": reasons}


def daemon_roots(rule):
    """The daemon file's roots of the clear closure: `closure_roots` (what the clear command runs) and `rating_roots` (the code that WRITES and PARSES the independent rating the score is read from, #835): a change to a definition reachable from any of them is the owner's own clear."""
    return sorted(set(rule["closure_roots"]) | set(rule.get("rating_roots") or []))


def refusals(rule, report, files, matches=None, closure=None, scored=None):
    """[reasons] a delegated clear is refused for this pull request; [] when none. `rule` is the matrix's `owner_hold.delegated_clear` block; `report` is the owner-hold report the
    daemon uses (`pr_floor_report`: its decision, its top tier path hits by class, the independent review's class, whether the file list is complete); `files` is the pull request's
    changed files ([{"filename", "previous_filename", "status"}]) or None when they could not be read. `matches(path, globs)` is the owner-hold floor's OWN path matcher (case-,
    normalisation- and spelling-insensitive: `./bin/x`, `bin//x`, `BIN/X`, a `..`, a rename's source all read as the file they name): the caller passes it, this module holds no second one.
    `closure` is {file name: [reasons] or None}: what `closure_refusals` said for each changed file of `closure_files` (None: it could not be mapped). `scored` is `judge`'s answer for an order with a
    max_level: then the score is the only rule about the pull request's classes (the path-class and review-class refusals below are not made), and the machinery refusals stay."""
    if not isinstance(rule, dict) or not isinstance(report, dict) or matches is None:
        return ["the delegated-clear rule or the owner-hold decision cannot be read"]
    for key in ("never_path_classes", "top_tier_review_classes", "clear_code_paths", "new_file_globs", "closure_files"):
        if not isinstance(rule.get(key), list) or not all(isinstance(x, str) for x in rule[key]):
            return ["the delegated-clear rule is malformed (%s)" % key]
    if report.get("owner_decision") not in ("hold", "second-review", "none"):
        return ["the owner-hold decision cannot be read"]
    if report.get("complete") is not True or report.get("unknown") or report.get("renamed_no_source"):
        return ["the pull request's changes cannot be read in full (%s)" % (report.get("unknown") or "the file list is cut")]
    reasons = list(scored["reasons"]) if isinstance(scored, dict) else []
    held = report.get("registrar_reasons")
    if not isinstance(held, list) or any(not isinstance(x, str) for x in held):
        reasons.append("the registrar lint's answer cannot be read: refused")
    else:
        reasons.extend("%s: that stays the owner's own clear" % x[:160] for x in held)   # #964: whatever the rating says
    hits = report.get("top_tier_hits") or {}
    for cls in sorted(hits) if scored is None else ():
        if cls.lower() in {c.lower() for c in rule["never_path_classes"]}:
            reasons.append("it changes a %s path (%s): that stays the owner's own clear" % (cls, ", ".join(hits[cls][:3])))
    if report.get("review_action") is None and scored is None:
        reasons.append("no independent review is recorded at this head: unknown is refused")
    review = report.get("review_class")
    if review is None and report.get("review_action") == "hold" and scored is None:
        reasons.append("the independent review holds it with no class named: unknown is refused")
    if isinstance(review, str) and review.lower() in {c.lower() for c in rule["top_tier_review_classes"]} and scored is None:
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


def closure(tree, roots, dynamic_actions=()):
    """(def names, global names, referenced names, {class: methods}) reachable from `roots` by NAME: every module-level definition a closure definition refers to (a call, a value, a default, a
    decorator, a base class) and every module-level binding it reads (its right-hand side, to a fixed point). A CLASS the closure refers to brings its own statements (bases, decorators, class
    body) and only those methods whose name the closure reads (as a name or an attribute) or that are special methods (`__init__`, `__getattr__`, `__call__`...): a method nothing reads is not run.
    Over-approximate by name on purpose (any attribute of that name). Dynamic dispatch by string is followed ONLY for the daemon's command table, for the actions in `dynamic_actions` (the rating's
    path, #835): the literal `"cmd_"` (the dispatcher builds `"cmd_" + action`) is an edge to `cmd_<action>` and the name `REGISTRAR_NAME_RE` (the parser collects every `add_<x>_parsers`) is an edge to
    `add_<action>_parsers`. Any OTHER dynamic edge (getattr, globals(), importlib, eval...) is a stated limit: a test lists the ones on the path from `main`."""
    defs, binds = _index(tree)
    roots = list(defs) if "*" in roots else list(roots)   # `*`: every function of the module (a module the clear runs and no recording says which)
    seen, globals_, refs, members, todo = set(), set(), set(), {}, list(roots)

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
                elif isinstance(x, ast.Constant) and x.value == "cmd_":
                    todo.extend("cmd_" + a.replace("-", "_") for a in dynamic_actions)
                if isinstance(x, ast.Name) and x.id == "REGISTRAR_NAME_RE":
                    todo.extend("add_%s_parsers" % a.replace("-", "_") for a in dynamic_actions)

    walk([n for r in roots if r in binds and r not in globals_ and not globals_.add(r) for n in binds[r]])   # a root that is a binding is read too
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


def closure_refusals(base_src, head_src, patch, roots, entry_points, module_level=False, dynamic_actions=()):
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
    base_closure, head_closure = closure(base, roots, dynamic_actions), closure(head, roots, dynamic_actions)
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


# ---- a command registrar that imports, calls or rebinds a name of the clear's closure (#964)
# In one process a hostile registrar can always undo a guard: this lint, the floor that reads it and the independent rating are the control. Both lists are DERIVED here: the closure names from `closure`
# (the one the clear uses, over the matrix's roots) and the registrars from the daemon file's own `REGISTRAR_NAME_RE`; this code holds neither. What it does hold are the names Python itself gives for reaching a
# namespace by string, which are not closure names (`NAMESPACE_REACHERS`, `NAMESPACE_ATTRIBUTES`).

NAMESPACE_REACHERS = ("globals", "vars", "locals", "setattr", "delattr", "getattr", "eval", "exec", "compile", "__import__", "import_module")
NAMESPACE_ATTRIBUTES = ("__dict__", "f_globals", "f_locals", "__globals__", "__builtins__", "modules")   # an attribute that IS a namespace
SETTERS = ("setattr", "delattr", "__setattr__", "__delattr__")
READ_ONLY_METHODS = ("get", "keys", "items", "values", "copy", "__getitem__", "__contains__", "__len__", "__iter__")
REGISTRAR_SOURCE_MAX = 4000000


def registrar_pattern(tree):
    """The compiled `REGISTRAR_NAME_RE` of a daemon file (read from its text, never held here), or None when it is not a literal `re.compile("...")`."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "REGISTRAR_NAME_RE" for t in node.targets):
            call = node.value
            if isinstance(call, ast.Call) and call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                try:
                    return re.compile(call.args[0].value)
                except re.error:
                    return None
    return None


def _root(node):
    """The name an expression is built from: `a.b[c](d).e` is `a`; None when it starts from something else."""
    while True:
        if isinstance(node, ast.Attribute) or isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Starred):
            node = node.value
        else:
            return node.id if isinstance(node, ast.Name) else None


def _spans(node, added):
    return any(number in added for number in range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1))


def _own_scope(unit):
    """([names bound in the unit's OWN scope], [names declared global]): its parameters, its stores and the names of the definitions in it, not what a nested function, lambda, class or comprehension binds (a name
    bound only in an inner scope does not hide the module's name in this one)."""
    own, glob, todo = set(), set(), [unit]
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    while todo:
        node = todo.pop()
        if isinstance(node, ast.arg):
            own.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            own.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            glob.update(node.names)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, scopes) and child is not unit:
                if not isinstance(child, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                    own.add(child.name)
                if isinstance(child, ast.ClassDef):
                    todo.extend(child.decorator_list)
                continue
            todo.append(child)
    arguments = getattr(unit, "args", None)
    if isinstance(arguments, ast.arguments):   # the unit's own parameters
        own.update(a.arg for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs + [x for x in (arguments.vararg, arguments.kwarg) if x is not None])
    return own, glob


def _registrar_unit(unit, names, added, helpers=None, opaque=(), literals=()):
    """[(verb, closure name)] a registrar (a function, a class, or the lambda a registrar name is assigned) does to the closure, counting only the nodes that touch a line in `added` (`None`: every line). `names` are the
    closure names. `helpers`, when a set is given, collects the names the unit refers to that are not closure names and not its own (a helper it may call: the caller reads those too). A call of a name in `opaque` (a
    module-level binding that is not a def, a class or a lambda, so it cannot be read) is a finding."""
    own, glob = _own_scope(unit)
    own -= glob   # a name declared `global` is the module's, whatever is stored into it
    live = set(names) - set(literals)   # the closure names a registrar could RUN: a name the module binds only to a literal (a tuple of strings) is data, so iterating it or passing it on runs nothing
    if helpers is not None:
        helpers.update(x.id for x in ast.walk(unit) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id not in own and x.id not in names)
    def namespace(expr):   # globals(), vars(), x.__dict__, frame.f_globals, sys.modules...
        return (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id in ("globals", "vars", "locals")) or (isinstance(expr, ast.Attribute) and expr.attr in NAMESPACE_ATTRIBUTES) \
            or (isinstance(expr, ast.Subscript) and namespace(expr.value))
    tainted = {}   # alias -> the closure name (or reacher) it came from
    changed = True
    while changed:   # an alias of a closure object or a namespace (`m = plan_ledger`, `g = globals()`, a parameter whose default is one): a name that holds what they hold
        changed = False
        pairs = []
        for x in ast.walk(unit):
            if isinstance(x, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and getattr(x, "value", None) is not None:
                pairs.extend((t, x.value) for t in (x.targets if isinstance(x, ast.Assign) else [x.target]))
            elif isinstance(x, (ast.For, ast.AsyncFor, ast.comprehension)):
                pairs.append((x.target, x.iter))
            elif isinstance(x, (ast.With, ast.AsyncWith)):
                pairs.extend((i.optional_vars, i.context_expr) for i in x.items if i.optional_vars is not None)
            elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                arguments = x.args
                positional = arguments.posonlyargs + arguments.args
                pairs.extend((ast.Name(id=a.arg, ctx=ast.Store()), d) for a, d in zip(positional[len(positional) - len(arguments.defaults):], arguments.defaults))
                pairs.extend((ast.Name(id=a.arg, ctx=ast.Store()), d) for a, d in zip(arguments.kwonlyargs, arguments.kw_defaults) if d is not None)
        for target, value in pairs:
            base = _root(value)
            via = base if (base in tainted or (base in live and base not in own) or base in NAMESPACE_REACHERS) else None
            if via is None and base not in own:   # a value built from a closure name in ANY way (a container, a conditional, an `or`, an argument of a call outside the registrar): the target holds it
                via = next((y.id for y in ast.walk(value) if isinstance(y, ast.Name) and isinstance(y.ctx, ast.Load) and (y.id in tainted or (y.id in live and y.id not in own))), None)
            if via is None and any(namespace(y) for y in ast.walk(value)):
                via = "a namespace"
            if via is not None:
                for y in ast.walk(target):
                    if isinstance(y, ast.Name) and y.id not in tainted:
                        tainted[y.id] = tainted.get(via, via)
                        changed = True
    def reaches(expr):
        base = _root(expr)
        return (base is not None and (base in tainted or (base in live and base not in own) or base in NAMESPACE_REACHERS)) or any(namespace(y) for y in ast.walk(expr))
    found = set()
    def note(node, verb, name):
        if added is None or _spans(node, added):
            found.add((verb, name))
    unreadable = "a name that cannot be read"
    for x in ast.walk(unit):
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):   # a decorator, a base class or a class keyword runs when the definition does
            for expr in list(x.decorator_list) + (list(x.bases) + [k.value for k in x.keywords] if isinstance(x, ast.ClassDef) else []):
                for y in ast.walk(expr):
                    if isinstance(y, ast.Name) and (y.id in tainted or (y.id in live and y.id not in own)):
                        note(x, "calls", tainted.get(y.id, y.id))
        if isinstance(x, (ast.Import, ast.ImportFrom)):
            bound = [a.name.split(".")[0] for a in x.names] + [a.asname for a in x.names if a.asname] + ([x.module.split(".")[0]] if isinstance(x, ast.ImportFrom) and x.module else [])
            hits = sorted(n for n in bound if n in names)
            for n in hits or ([] if all(isinstance(a.name, str) for a in x.names) else [unreadable]):
                note(x, "imports", n)
        elif isinstance(x, (ast.Global, ast.Nonlocal)):
            for n in x.names:
                if n in names:
                    note(x, "rebinds", n)
        elif isinstance(x, ast.Call):
            callee = _root(x.func)
            fname = x.func.id if isinstance(x.func, ast.Name) else (x.func.attr if isinstance(x.func, ast.Attribute) else None)
            if callee is not None and ((callee in live and callee not in own) or callee in tainted):
                note(x, "calls", tainted.get(callee, callee))
            elif callee is not None and callee not in own:   # a closure name handed to something that is not the registrar's own object (`partial(f)`, `Thread(target=f)`, `map(f, xs)`): it may run it
                for a in list(x.args) + list(x.keywords):
                    handed = _root(a.value if isinstance(a, ast.keyword) else a)
                    if handed in tainted or (handed in live and handed not in own):
                        note(x, "calls", tainted.get(handed, handed))
            if isinstance(x.func, ast.Name) and x.func.id in opaque and x.func.id not in own and x.func.id not in names:
                note(x, "calls", "a helper that cannot be read (%s)" % x.func.id)
            if fname in ("__import__", "import_module"):
                note(x, "imports", unreadable)
            elif fname in ("eval", "exec", "compile"):
                note(x, "calls", fname)
            elif fname in SETTERS or fname in ("getattr", "update", "pop", "setdefault"):
                strings = [a.value for a in x.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                target = x.func.value if isinstance(x.func, ast.Attribute) and fname not in ("getattr", "setattr", "delattr") else (x.args[0] if x.args else None)
                named = sorted(n for n in strings if n in names)
                if fname in ("getattr", "update", "pop", "setdefault"):
                    if target is not None and reaches(target) and fname == "getattr" and named:
                        note(x, "calls", named[0])
                    elif fname != "getattr" and target is not None and reaches(target):
                        note(x, "rebinds", named[0] if named else unreadable)
                elif named:
                    note(x, "rebinds", named[0])
                elif target is None or reaches(target) or not (len(x.args) > 1 and isinstance(x.args[1], ast.Constant)):
                    note(x, "rebinds", unreadable)
            elif isinstance(x.func, ast.Attribute) and reaches(x.func.value) and x.func.attr not in READ_ONLY_METHODS and (namespace(x.func.value) or _root(x.func.value) in tainted or _root(x.func.value) in NAMESPACE_REACHERS):
                note(x, "rebinds", unreadable)   # `globals().__setitem__(k, v)`, `.clear()`, `.popitem()` on a namespace
            if any(namespace(a.value if isinstance(a, ast.keyword) else a) or _root(a.value if isinstance(a, ast.keyword) else a) in tainted and tainted.get(_root(a.value if isinstance(a, ast.keyword) else a)) in ("a namespace",) + NAMESPACE_REACHERS
                   for a in list(x.args) + list(x.keywords)):
                note(x, "rebinds", unreadable)   # a namespace handed to a call (`operator.setitem(globals(), k, v)`, `f(g)`): what it does to it cannot be read
        elif isinstance(x, (ast.Attribute, ast.Subscript)) and isinstance(x.ctx, (ast.Store, ast.Del)):
            if isinstance(x, ast.Attribute) and x.attr in names:
                note(x, "rebinds", x.attr)
            elif reaches(x.value):
                key = x.slice if isinstance(x, ast.Subscript) else None
                base = _root(x.value)
                base = tainted.get(base, base)
                label = key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value in names else (base if base in names else unreadable)
                note(x, "rebinds", label)
        elif namespace(x) and isinstance(x, ast.Attribute):
            note(x, "rebinds", unreadable)   # reaching a namespace at all: a closure name cannot be ruled out
    return sorted(found)


def _module_statements(body):
    """Every statement the module runs by itself: the body, and the bodies of an `if`, `try`, `with`, `for` or `while` in it; never the inside of a def or a class."""
    for node in body:
        yield node
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)):
            for field in ("body", "orelse", "finalbody"):
                yield from _module_statements(getattr(node, field, []))
            for handler in getattr(node, "handlers", []):
                yield from _module_statements(handler.body)


def _binds_registrar(node, patterns):
    """[(name, unit)] the registrars a module-level statement defines or binds: a def, an async def, a class, an assignment (plain, annotated, augmented, tuple or multi target) whose value is a lambda or anything else
    (then the unit is None: it cannot be read), an import under a registrar name. Whatever `command_registrars` could pick up by name out of the module's globals."""
    def named(name):
        return isinstance(name, str) and any(p.fullmatch(name) for p in patterns)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [(node.name, node)] if named(node.name) else []
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        out = []
        for target in targets:
            for y in ast.walk(target):
                if isinstance(y, ast.Name) and named(y.id):
                    value = getattr(node, "value", None)
                    out.append((y.id, value if isinstance(value, ast.Lambda) and isinstance(target, ast.Name) else None))
        return out
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name.split(".")[0], None) for a in node.names if named(a.asname or a.name.split(".")[0])]
    return []


def registrar_findings(base_src, head_src, rule):
    """([(registrar, verb, closure name)], None) for a pull request's own base and head text of the daemon file, or (None, why) when they cannot be read (the caller holds, fail closed). A finding is an
    import, a call or a rebinding of a closure name by a registrar on a line the pull request added; an untouched line or a removed one gives none. A registrar is whatever binds a name the daemon file's own
    `REGISTRAR_NAME_RE` matches, wherever the module binds it (def, async def, class, assignment of any form, import, an `if` or `try` around it, a write into `globals()`); a registrar that cannot be read is held. A
    function a registrar refers to that is not a closure name (a helper) is read as part of the registrar (every line of it when a changed line of the registrar names it). The closure is the union of the base and
    the head (a pull request that ADDS a closure name counts); the registrar pattern is the union of both files' own `REGISTRAR_NAME_RE`."""
    if not isinstance(base_src, str) or not isinstance(head_src, str) or len(base_src) > REGISTRAR_SOURCE_MAX or len(head_src) > REGISTRAR_SOURCE_MAX:
        return None, "the daemon file before or after the change cannot be read"
    try:
        base, head = ast.parse(base_src), ast.parse(head_src)
        roots, actions = daemon_roots(rule), rule.get("dynamic_actions") or ()
    except (SyntaxError, ValueError, KeyError, TypeError, AttributeError, RecursionError, MemoryError):
        return None, "the daemon file does not parse before or after the change, or the rule cannot be read"
    patterns = [p for p in (registrar_pattern(base), registrar_pattern(head)) if p is not None]
    if not patterns or registrar_pattern(head) is None:
        return None, "the registrar name pattern of the daemon file cannot be read"
    names = set()
    for tree in (base, head):
        reach = closure(tree, roots, actions)
        names |= reach[0] | reach[1]
    if not names:
        return None, "the clear's closure is empty, so it cannot be told what a registrar touches"
    patch = "\n".join(difflib.unified_diff(base_src.split("\n"), head_src.split("\n"), n=0, lineterm=""))
    lines = changed_lines(patch)
    if lines is None:
        return None, "the diff of the daemon file cannot be read"
    added = set(lines[1])
    found, defs, opaque = [], {}, set()   # the helpers: what the module binds, by any form, that is not the closure; `opaque` are the bindings that cannot be read
    for node in _module_statements(head.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name not in names:
                defs[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                for y in ast.walk(target):
                    if isinstance(y, ast.Name) and y.id not in names:
                        if isinstance(getattr(node, "value", None), ast.Lambda) and isinstance(target, ast.Name):
                            defs[y.id] = node.value
                            node.value.lineno, node.value.end_lineno = node.lineno, node.end_lineno
                        else:
                            opaque.add(y.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            opaque.update((a.asname or a.name.split(".")[0]) for a in node.names if (a.asname or a.name.split(".")[0]) not in names)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try)):   # names these bind at module level (`for f in ...`, `with x as f`, `except E as f`)
            targets = [node.target] if isinstance(node, (ast.For, ast.AsyncFor)) else [i.optional_vars for i in getattr(node, "items", []) if i.optional_vars is not None]
            opaque.update(y.id for t in targets for y in ast.walk(t) if isinstance(y, ast.Name) and y.id not in names)
            opaque.update(h.name for h in getattr(node, "handlers", []) if h.name and h.name not in names)
    bound_to, literals = {}, set()   # a closure name every module-level binding of which is a literal is data
    for tree in (base, head):
        for node in _module_statements(tree.body):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and getattr(node, "value", None) is not None:
                try:
                    ast.literal_eval(node.value)
                    literal = True
                except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                    literal = False
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    for y in ast.walk(target):
                        if isinstance(y, ast.Name):
                            bound_to[y.id] = bound_to.get(y.id, True) and literal
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
                for bound in ([node.name] if hasattr(node, "name") else [(a.asname or a.name.split(".")[0]) for a in node.names]):
                    bound_to[bound] = False
    literals = {n for n, ok in bound_to.items() if ok and n in names}
    def read(label, unit, lines_counted):
        helpers = set()
        for verb, hit in _registrar_unit(unit, names, lines_counted, helpers, opaque, literals):
            found.append((label, verb, hit))
        return helpers
    for node in _module_statements(head.body):
        for name, unit in _binds_registrar(node, patterns):
            if unit is None:
                if _spans(node, added):
                    found.append((name, "rebinds", "a registrar bound in a form that cannot be read"))
                continue
            if isinstance(unit, ast.Lambda):
                unit.lineno, unit.end_lineno = node.lineno, node.end_lineno
            done, todo = {}, [(name, unit, added)]
            while todo:   # the registrar, then the helpers it refers to (all lines of a helper that a changed line of its caller names)
                label, current, counted = todo.pop()
                refs = read(name if label == name else "%s (through %s)" % (name, label), current, counted)
                for ref in sorted(refs & set(defs)):
                    full = counted is None or any(isinstance(n, ast.Name) and n.id == ref and _spans(n, added) for n in ast.walk(current))
                    if ref not in done or (full and done[ref] is not None):
                        done[ref] = None if full else added
                        todo.append((ref, defs[ref], done[ref]))
        # a write into the namespace at module level that names a registrar (or a key that cannot be read)
    for node in ast.walk(head):
        if isinstance(node, (ast.Subscript, ast.Call)) and _spans(node, added) and not isinstance(node, ast.Constant):
            target = node.value if isinstance(node, ast.Subscript) else None
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)) and isinstance(target, ast.Call) and isinstance(target.func, ast.Name) and target.func.id in ("globals", "vars"):
                key = node.slice
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)) or any(p.fullmatch(key.value) for p in patterns):
                    found.append(("a module-level write into globals()", "rebinds", "a registrar bound in a form that cannot be read"))
    return sorted(set(found)), None


def decode_blob(answer):
    """The text of a `git/blobs` answer, or None when it is not a complete base64 text."""
    if not isinstance(answer, dict) or answer.get("encoding") != "base64" or not isinstance(answer.get("content"), str):
        return None
    try:
        return base64.b64decode("".join(answer["content"].split()), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return None
