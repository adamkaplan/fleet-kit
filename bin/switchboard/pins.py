# ---------------------------------------------------------------- `pins` (ask #1543): the existing tests that name what the diff changes
#
# Read-only: `git diff` and `git grep` over the checkout, no network, no write. It lists the EXISTING tests that name a function, class or constant the diff
# changes or removes, or a quoted message (12 characters or more) of a removed or changed line, so a builder runs those classes before it pushes. It never
# lists a test the diff itself adds. Exit 0 whatever it finds; non-zero only when the diff cannot be read. The command is registered by name, here.

# (standard-library modules are imported inside the functions that use them: a module-level import is a binding the clear's closure would read.)
PINS_MAX_LINES = 200
PINS_MIN_MESSAGE = 12   # a quoted string shorter than this is noise
PINS_MIN_NAME = 4
PINS_MAX_TERMS = 300   # of each kind: a bound on what one search is given
PINS_SEARCH_PATHS = ("tests", "bin/test-*", "cloud/launcher/tests", "cloud/worker/tests", "cloud/tests")
PINS_COMMON_WORDS = frozenset((
    "self", "args", "kwargs", "main", "name", "test", "tests", "data", "value", "values", "result", "results", "items", "item", "text", "line", "lines",
    "path", "file", "files", "list", "dict", "keys", "type", "kind", "none", "true", "false", "make", "read", "write", "load", "dump", "send", "print",
    "setup", "run", "get", "set", "init", "call", "help", "key", "now", "done", "open", "close", "check", "build", "parse", "state", "config", "error"))
PINS_NAME_RES = (re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"), re.compile(r"^\s*class\s+([A-Za-z_]\w*)"),
                 re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*(?::[^=]+)?="))
PINS_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
PINS_STRING_RE = re.compile(r"\"((?:[^\"\\\n]|\\.){%d,})\"|'((?:[^'\\\n]|\\.){%d,})'" % (PINS_MIN_MESSAGE, PINS_MIN_MESSAGE))
PINS_TEST_DEF_RE = re.compile(r"^(\s*)(?:async\s+)?def\s+(test\w*)")
PINS_ANY_DEF_RE = re.compile(r"^(\s*)(?:async\s+)?def\s+\w+")
PINS_CLASS_RE = re.compile(r"^(\s*)class\s+(\w+)")


class PinsError(Exception):
    """The diff could not be read."""


def _pins_one_line(text, limit=300):
    return " ".join(str(text).split())[:limit]


def _pins_name_ok(name):
    import keyword
    return len(name) >= PINS_MIN_NAME and not keyword.iskeyword(name) and name.lower() not in PINS_COMMON_WORDS


def pins_names_of(line):
    """The definition names a source line holds (def, class or constant assignment), the ones worth searching for."""
    return [m.group(1) for r in PINS_NAME_RES for m in [r.match(line)] if m and _pins_name_ok(m.group(1))]


def pins_messages_of(line):
    """The quoted strings of 12 or more characters on a line."""
    return [a or b for a, b in PINS_STRING_RE.findall(line) if (a or b).strip()]   # a string of spaces only would match every indented line


def pins_read_diff(base, runner):
    """{path: {"added": set of new-side line numbers, "names": set, "messages": set}} for `git diff -U0 base`; PinsError when git cannot read it."""
    import subprocess
    try:
        done = runner(["git", "diff", "-U0", "--no-color", "--no-ext-diff", base, "--"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as err:
        raise PinsError("could not read the diff: %s" % _pins_one_line(str(err), 200))
    if done.returncode != 0:
        raise PinsError("could not read the diff against %s: %s" % (base, _pins_one_line(done.stderr or "git diff failed", 200)))
    files, path, new_line, old_path = {}, None, 0, None
    left_old = left_new = 0   # the lines a hunk still owes: inside one, a line is content whatever it starts with (a removed `-- x` reads `--- x`)
    for raw in (done.stdout or "").splitlines():
        if left_old > 0 or left_new > 0:
            if path is not None:
                entry = files[path]
                if raw.startswith("+"):
                    entry["added"].add(new_line)   # an added line cannot be named by a test that already exists: only the removed side and the hunk's own function are searched
                    new_line += 1
                elif raw.startswith("-"):
                    entry["names"].update(pins_names_of(raw[1:]))
                    entry["messages"].update(pins_messages_of(raw[1:]))
            if raw.startswith("+"):
                left_new -= 1
            elif raw.startswith("-"):
                left_old -= 1
            elif not raw.startswith("\\"):
                left_old, left_new = left_old - 1, left_new - 1
            continue
        if raw.startswith("--- a/") or raw == "--- /dev/null":
            old_path = raw[6:].strip() if raw.startswith("--- a/") else None
            continue
        if raw.startswith("+++ b/") or raw == "+++ /dev/null":
            path = raw[6:].strip() if raw.startswith("+++ b/") else old_path   # a deleted file is read from its old side
            if path:
                files.setdefault(path, {"added": set(), "names": set(), "messages": set()})
            continue
        if raw.startswith("diff --git"):
            path, old_path = None, None
            continue
        hunk = PINS_HUNK_RE.match(raw)
        if hunk and path is not None:
            new_line = int(hunk.group(3))
            left_old = 1 if hunk.group(2) is None else int(hunk.group(2))
            left_new = 1 if hunk.group(4) is None else int(hunk.group(4))
            files[path]["names"].update(pins_names_of(hunk.group(5)))   # the function or class the hunk sits in
    return files


def pins_all_terms(files):
    """(names, messages): every term the diff changes or removes, sorted, none cut."""
    return (sorted(set().union(*[f["names"] for f in files.values()])), sorted(set().union(*[f["messages"] for f in files.values()])))


def pins_terms(files):
    """(names, messages): the terms one search is given, sorted, at most PINS_MAX_TERMS of each kind."""
    names, messages = pins_all_terms(files)
    return names[:PINS_MAX_TERMS], messages[:PINS_MAX_TERMS]


def pins_term_cut(files):
    """(names, messages) that `pins_terms` leaves out of the search: the counts."""
    names, messages = pins_all_terms(files)
    return max(0, len(names) - PINS_MAX_TERMS), max(0, len(messages) - PINS_MAX_TERMS)


def pins_search(names, messages, runner):
    """[(path, line number, line text, term)] for every line of the existing tests that holds a term (git grep over tracked files only)."""
    import re
    import subprocess
    terms = list(names) + list(messages)
    if not terms:
        return []
    try:   # the terms go in on standard input (`-f -`), never as arguments
        done = runner(["git", "grep", "-n", "-I", "-F", "--no-color", "-f", "-", "--"] + list(PINS_SEARCH_PATHS), input="\n".join(terms) + "\n",
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as err:
        raise PinsError("could not search the tests: %s" % _pins_one_line(str(err), 200))
    if done.returncode not in (0, 1):
        raise PinsError("could not search the tests: %s" % _pins_one_line(done.stderr or "git grep failed", 200))
    name_res = {n: re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(n)) for n in names}
    hits = []
    for raw in (done.stdout or "").splitlines():
        parts = raw.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        path, number, text = parts[0], int(parts[1]), parts[2]
        for term in [n for n in names if name_res[n].search(text)] + [m for m in messages if m in text]:
            hits.append((path, number, text, term))
    return hits


def pins_index(lines):
    """One pass over a file: its def lines and class lines, and for each def the answer `pins_enclosing` gives a line below it (a monotone stack of the defs
    whose indent is lower than every def after them, so no hit rescans the file)."""
    defs, answers, classes, stack = [], [], [], []   # defs: line numbers; answers: (test, test line, indent) per def; stack: (indent, test or None, line)
    for i, text in enumerate(lines):
        m = PINS_ANY_DEF_RE.match(text)
        if m:
            indent = len(m.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            t = PINS_TEST_DEF_RE.match(text)
            stack.append((indent, t.group(2) if t else None, i + 1))
            answer = (None, None, stack[0][0])
            for ind, name, line in reversed(stack):
                if name is not None:
                    answer = (name, line, ind)
                    break
            defs.append(i + 1)
            answers.append(answer)
            continue
        c = PINS_CLASS_RE.match(text)
        if c:
            classes.append((i + 1, len(c.group(1)), c.group(2)))
    return {"defs": defs, "answers": answers, "classes": classes, "size": len(lines), "class_cache": {}}


def pins_enclosing(lines, number, index=None):
    """(class name or "-", test name or None, the test's def line number) of the existing line `number` (1-based): the nearest test def above it, and the class around it."""
    import bisect
    index = index or pins_index(lines)
    k = bisect.bisect_right(index["defs"], min(number, index["size"])) - 1
    test, test_line, indent = index["answers"][k] if k >= 0 else (None, None, None)
    cls_indent = indent if indent is not None else 1 << 30
    top = test_line or number
    key = (top, cls_indent)
    if key not in index["class_cache"]:
        found = "-"
        for line, ind, name in reversed(index["classes"]):
            if line <= top and ind < cls_indent:
                found = name
                break
        index["class_cache"][key] = found
    return index["class_cache"][key], test, test_line


def pins_listing(files, hits, read_lines):
    """The tests to list: [(path, line, class, test, [terms])], one per test, sorted. A hit inside a test the diff adds (its def line is added) is dropped."""
    found, indexes = {}, {}
    for path, number, text, term in hits:
        lines = read_lines(path)
        if lines is None:
            continue
        if path not in indexes:
            indexes[path] = pins_index(lines)
        added = files.get(path, {}).get("added", set())
        cls, test, test_line = pins_enclosing(lines, number, indexes[path])
        if test is None:
            if indexes[path]["defs"]:
                continue   # a helper or a module line, not a test
            test, test_line = "-", number   # a file with no Python functions (a shell test): the line itself
        if test_line in added or number in added and test == "-":
            continue
        key = (path, test_line, cls, test)
        terms = found.setdefault(key, [number, []])
        if term not in terms[1]:
            terms[1].append(term)
        terms[0] = min(terms[0], number)
    return sorted((p, ln[0], c, t, ln[1]) for (p, tl, c, t), ln in found.items())


def pins_base_problem(base):
    """Why `base` is refused, or None: a value that starts with `-` would be read by git as an option (`--output=FILE` writes a file)."""
    if not isinstance(base, str) or not base or base.startswith("-"):
        return "pins: the base is a ref, not an option"
    return None


def cmd_pins(args, runner, now):
    """`pins [--base REF]`: the existing tests that name a function, class or constant the diff changes or removes, or a quoted message of a removed or changed line."""
    import sys
    from pathlib import Path
    base = args.base
    problem = pins_base_problem(base)
    if problem:
        sys.stderr.write("%s\n" % problem)
        return 1
    top = Path.cwd()
    for parent in [top] + list(top.parents):   # the diff and the tests are read from the toplevel, so a run from a subdirectory answers the same
        if (parent / ".git").exists():
            top = parent
            break
    root = lambda argv, **kw: runner(argv, cwd=str(top), **kw)
    try:
        files = pins_read_diff(base, root)
        names, messages = pins_terms(files)
        hits = pins_search(names, messages, root)
    except PinsError as err:
        sys.stderr.write("fleet-switchboard: pins: %s\n" % err)
        return 1
    cut_names, cut_messages = pins_term_cut(files)
    if cut_names or cut_messages:
        print("cut: %d names / %d messages not searched" % (cut_names, cut_messages))
    cache = {}

    def read_lines(path):
        if path not in cache:
            try:
                cache[path] = (top / path).read_text(errors="replace").splitlines()
            except OSError:
                cache[path] = None
        return cache[path]

    listing = pins_listing(files, hits, read_lines)
    if not listing:
        print("no existing test names a changed definition or message")
        return 0
    for path, number, cls, test, terms in listing[:PINS_MAX_LINES]:
        print("%s:%d  %s::%s  matched %s" % (path, number, cls, test, ", ".join(repr(t) if " " in t or len(t) >= PINS_MIN_MESSAGE and not t.isidentifier() else t for t in terms)))
    if len(listing) > PINS_MAX_LINES:
        print("cut: the listing stops at %d of %d tests; narrow the diff or read the rest with git grep" % (PINS_MAX_LINES, len(listing)))
    print("%d existing test%s name a changed definition or message (read only: run these classes before you push)" % (len(listing), "" if len(listing) == 1 else "s"))
    return 0


cmd_pins.direct = True   # reads only: no error mapping, no daemon start


def add_pins_parsers(sub):
    pins = sub.add_parser("pins", help="List the existing tests that name what your diff changes or removes (read-only)",
                          description="Reads `git diff` against the base and `git grep` over the existing tests; prints one line per test with the term it matched, "
                                      "never a test the diff adds, bounded to %d lines. It writes nothing and calls no network. Run the listed classes before you push." % PINS_MAX_LINES)
    pins.add_argument("--base", default="origin/integration", help="The ref to diff against (default origin/integration)")
