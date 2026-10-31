"""The impact hold's deterministic floor and decision table (#179, slice L3). Pure: nothing here reads GitHub, a file the caller did not
name, or the clock. The floor is PATH GLOBS only (a PR that adds, changes or deletes a file inside its repo's floor globs holds, whatever any
reviewer says); the independent rating can only ADD a hold, never lower the floor; the size of a change is never a hold by itself. The
matrix is docs/review-laws/impact-matrix.json, the single source of truth. L3 is advisory in code terms: it decides and records; it writes
no label (that is slice L4)."""

import fnmatch
import hashlib
import json
import re
import unicodedata
from pathlib import Path

ACTION_HOLD, ACTION_SECOND, ACTION_NONE = "H", "2", "."
MATRIX_PATH = Path(__file__).resolve().parent.parent / "docs" / "review-laws" / "impact-matrix.json"


class MatrixError(ValueError):
    """The impact matrix file cannot be read or has no usable `owner_hold.floor`. Never swallowed: the floor lived in code and could not fail to load, so a
    file that cannot be read stops the decision, and the callers (the daemon pass and the `impact hold` command) say so loudly (`impact.error`, one line and
    a non-zero exit) and make no judgement; they never read it as "no floor"."""


_MATRIX_CACHE = {}


def _matrix_cached():
    """The matrix file, re-read only when it changes (the daemon asks per record per pass)."""
    try:
        stamp = MATRIX_PATH.stat().st_mtime_ns
        if _MATRIX_CACHE.get("stamp") != stamp:
            _MATRIX_CACHE.update({"stamp": stamp, "matrix": load_matrix()})
        return _MATRIX_CACHE["matrix"]
    except (OSError, ValueError) as err:
        _MATRIX_CACHE.clear()
        raise MatrixError("the impact matrix file cannot be read (%s: %s)" % (type(err).__name__, err)) from None


def rule_fingerprint():
    """A short fingerprint of the matrix file's bytes, the version of the rule: the daemon judges a pull request again when it changes (#451 slice 3). Raises
    MatrixError when the file cannot be read (a rule that cannot be read has no version)."""
    try:
        return hashlib.sha256(MATRIX_PATH.read_bytes()).hexdigest()[:12]
    except OSError as err:
        raise MatrixError("the impact matrix file cannot be read (%s: %s)" % (type(err).__name__, err)) from None


def default_floors(matrix=None):
    """The built-in floor per repo, read from the matrix file's `owner_hold.floor` (a proposal the owner confirms): the code names no path. Raises
    MatrixError when the file cannot be read or holds no `owner_hold.floor` object."""
    floors = ((matrix if matrix is not None else _matrix_cached()).get("owner_hold") or {}).get("floor")
    if not isinstance(floors, dict):
        raise MatrixError("the impact matrix file has no owner_hold.floor object")
    return {name: entry for name, entry in floors.items() if isinstance(entry, dict)}


_FOLDS = {}


def folds(text):
    """The case- and normalisation-folded spellings of `text`: the NFC and the NFD form of its casefold AND of its lower-case (so this is a superset of what
    `str.lower()` alone gave). A case-insensitive, normalisation-insensitive file system (APFS) reads `SHIMS/gh`, `\u017fhims/gh` (a long s) and a decomposed
    accent as one file, and `str.lower()` misses most of them. ALL forms are tried (when in doubt, hold: a fold that makes an unrelated path hit only adds a hold)."""
    text = str(text)
    got = _FOLDS.get(text)
    if got is None:
        got = frozenset(unicodedata.normalize(form, getattr(unicodedata.normalize(form, text), method)()) for form in ("NFC", "NFD") for method in ("casefold", "lower"))
        if len(_FOLDS) < 20000:
            _FOLDS[text] = got
    return got


def _glob_regex(pattern):
    """The compiled regexes of a glob, one per folded form of it."""
    return [_compiled(form) for form in folds(pattern)]


_CACHE = {}


def _compiled(pattern):
    if pattern not in _CACHE:
        _CACHE[pattern] = _build(pattern)
    return _CACHE[pattern]


def _build(pattern):
    """`**` crosses directories, `*` and `?` do not; a trailing `/**` also matches the directory's own files at any depth."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out), re.DOTALL)   # DOTALL and fullmatch (below): a newline, a tab or a NUL in a path never escapes a glob, and a trailing newline never makes an exact file match


def canonical(path):
    """(the path as the file system would read it, lower-cased, escapes): backslashes become `/`, empty and `.` segments are dropped, `..` removes the
    segment before it. `escapes` is True when a `..` has nothing to remove, or the path is absolute, starts with `~` or a drive letter (the path leaves the repository; Windows-only spellings such as a trailing dot are out of scope: the fleet runs on macOS and Linux). A variant of a path (`Shims/GH`, `shims//gh`,
    `shims/./gh`, `shims/x/../gh`, `a/../shims/gh`, `shims\\gh`) names the same file as the plain one, so it must be judged as that file."""
    text = str(path).replace("\\", "/")
    parts, escapes = [], text.startswith(("/", "~")) or bool(re.match(r"^[A-Za-z]:", text))   # an absolute path, `~` or a drive letter is not a path inside the repository
    for segment in text.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            else:
                escapes = True
            continue
        parts.append(segment)
    return unicodedata.normalize("NFD", "/".join(parts)).casefold(), escapes


def escapes(path):
    """True when the path cannot be read as a path inside the repository: it is HELD (when in doubt, hold)."""
    return canonical(path)[1]


def matches(path, patterns):
    """Judged as the file the path names (see `canonical`), case- and normalisation-insensitively (see `folds`): a variant can only ADD a hit, so the raw
    spelling and the canonical one are both tried, each in both folded forms (a false hold costs little)."""
    texts = folds(path) | folds(canonical(path)[0])
    return any(regex.fullmatch(text) for p in patterns for regex in _glob_regex(p) for text in texts)


def matches_named(path, patterns):
    """Judged ONLY as the file the path names (see `canonical`): for an EXCLUDE list, which can only take a hit away, so a spelling must never be able to
    move a path into it (`shims/docs/../gh` is `shims/gh`, not under `shims/docs/**`)."""
    texts = folds(canonical(path)[0])
    return any(regex.fullmatch(text) for p in patterns for regex in _glob_regex(p) for text in texts)


def floor_paths(paths, globs, exclude=(), force=()):
    """The paths of a change that trigger the floor: inside a floor glob and not excluded (a `force` path is never excluded). A deletion
    counts like any change (the caller passes deleted paths too; so does the OLD path of a rename); one outside the globs never does."""
    return sorted({p for p in paths if isinstance(p, str) and (escapes(p) or (matches(p, globs) and (not matches_named(p, exclude) or matches(p, force))))})


def floor_for(repo, config=None):
    """(globs, exclude, force) for `repo` from the config (`impact_floor.repos.<owner/repo>`), else the built-in proposal, else none."""
    wanted = (repo or "").lower()
    for name, entry in (((config or {}).get("impact_floor") or {}).get("repos") or {}).items():
        if name.lower() == wanted and isinstance(entry, dict):
            return list(entry.get("globs") or []), list(entry.get("exclude") or []), list(entry.get("force") or [])
    for name, entry in default_floors().items():
        if name.lower() == wanted:
            return list(entry.get("globs") or []), list(entry.get("exclude") or []), list(entry.get("force") or [])
    return [], [], []


CONFIG_KEY = "impact_floor"   # the module declares its own configuration key (bin/fleet-switchboard config_declarations)
CONFIG_DEFAULT = None         # None: no default is added to the configuration (absent = off)


def validate(raw, source="config"):
    """The configuration check the shared validator calls: `validate_config`, with the source named in front of its message."""
    try:
        validate_config(raw)
    except ValueError as err:
        raise ValueError("%s: %s" % (source, err)) from None


def validate_config(raw):
    """Raise ValueError naming the key when `impact_floor` is not {enabled?, calls_per_pass?, repos: {owner/repo: {globs, exclude?, force?}}}
    with lists of non-empty strings: a string where a list belongs must never silently hold nothing."""
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ValueError("'impact_floor' must be an object")
    unknown = sorted(set(raw) - {"enabled", "calls_per_pass", "repos"})
    if unknown:
        raise ValueError("'impact_floor' has key(s) this tool does not read: %s" % ", ".join(unknown))
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        raise ValueError("'impact_floor.enabled' must be true or false")
    calls = raw.get("calls_per_pass", 1)
    if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
        raise ValueError("'impact_floor.calls_per_pass' must be a whole number, 0 or more")
    repos = raw.get("repos", {})
    if not isinstance(repos, dict):
        raise ValueError("'impact_floor.repos' must be an object keyed by owner/repo")
    for name, entry in repos.items():
        if not isinstance(entry, dict):
            raise ValueError("'impact_floor.repos.%s' must be an object" % name)
        extra = sorted(set(entry) - {"globs", "exclude", "force", "hold", "hold_exclude"})
        if extra:
            raise ValueError("'impact_floor.repos.%s' has key(s) this tool does not read: %s" % (name, ", ".join(extra)))
        for key in ("globs", "exclude", "force"):
            value = entry.get(key, [])
            if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
                raise ValueError("'impact_floor.repos.%s.%s' must be a list of non-empty path globs, got %r" % (name, key, value))
        if "hold" in entry or "hold_exclude" in entry:
            problem = hold_problem(entry)
            if problem:
                raise ValueError("'impact_floor.repos.%s.%s" % (name, problem))
            _refuse_machinery_excludes(name, entry.get("hold_exclude") or [])
        elif not entry.get("globs"):
            raise ValueError("'impact_floor.repos.%s.globs' must name at least one glob" % name)


OPERATOR_HOLD_CLASS = "execution"   # the one class an entry may list that the matrix's top tier does not name (the operator's own, not kit-wide data)


def hold_classes():
    """The classes a repos entry may list: the matrix's top tier classes (read from the file, none written here) and the operator's own. An unreadable matrix
    leaves only the operator's class, so an entry naming a top tier class is read as invalid and the decision falls back to holding the whole change."""
    try:
        rule = owner_hold_rule(_matrix_cached())
    except MatrixError:
        rule = None
    top = sorted(rule[0]) if rule else []
    return tuple(top) + (OPERATOR_HOLD_CLASS,)


def __getattr__(name):   # HOLD_CLASSES stays readable as a module attribute, but is derived at use
    if name == "HOLD_CLASSES":
        return hold_classes()
    raise AttributeError(name)


def hold_problem(entry):
    """None when the `hold` / `hold_exclude` keys of a repos entry are well formed, else the rest of the message (starting at the key, naming it). One reader for the
    configuration check and for the decision (which holds the whole change on a problem)."""
    hold, HOLD_CLASSES = entry.get("hold", {}), hold_classes()
    if not isinstance(hold, dict):
        return "hold' must be an object with the classes %s" % ", ".join(HOLD_CLASSES)
    unknown = sorted(set(hold) - set(HOLD_CLASSES))
    if unknown:
        return "hold' has class(es) this tool does not read: %s (the classes are %s)" % (", ".join(map(str, unknown)), ", ".join(HOLD_CLASSES))
    for cls in HOLD_CLASSES + ("hold_exclude",):
        value = hold.get(cls, []) if cls != "hold_exclude" else entry.get("hold_exclude", [])
        if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
            key = "hold_exclude" if cls == "hold_exclude" else "hold.%s" % cls
            return "%s' must be a list of non-empty path globs, got %r" % (key, value)
    return None


def _literal_dir(pattern):
    """The directory part of a canonical glob before its first wildcard segment (a file name with no wildcard counts by its directory)."""
    parts, out = canonical(pattern)[0].split("/"), []
    for i, seg in enumerate(parts):
        if any(c in seg for c in "*?["):
            return "/".join(out)
        out.append(seg)
    return "/".join(out[:-1])


def _sample(pattern):
    return canonical(re.sub(r"\*\*|\*|\?", "x", pattern))[0]


def machinery_paths(matrix):
    """Every machinery pattern the matrix names: clear_code_paths, never_carried_paths and the closure files (read from the matrix; none is written here)."""
    block = (matrix.get("owner_hold") or {}) if isinstance(matrix, dict) else {}
    listed = list(clear_code_paths(matrix)[0] or []) + list(carry_never_paths(matrix))
    closure = (block.get("delegated_clear") or {}).get("closure_files") if isinstance(block.get("delegated_clear"), dict) else None
    return sorted({x for x in listed + (closure if isinstance(closure, list) else []) if isinstance(x, str)})


def exclude_hits_machinery(glob, machinery):
    """The machinery patterns a `hold_exclude` glob reaches: they overlap (by pattern, after `canonical`, either way round) and the glob does not sit strictly above
    the machinery's directory under a named directory (`docs/**` is allowed beside `docs/review-laws/impact-matrix.json`; `bin/**`, `**` and `bin/impact_*.py` are not).
    The hold itself never depends on this check: the machinery is held after, and apart from, any entry."""
    prefix, found = _literal_dir(glob), []
    for m in machinery:
        if not (matches(_sample(m), [glob]) or matches(_sample(glob), [m]) or matches(m, [glob])):
            continue
        mdir = _literal_dir(m)
        if prefix and prefix != mdir and (mdir + "/").startswith(prefix + "/"):
            continue
        found.append(m)
    return found


def _refuse_machinery_excludes(name, excludes):
    if not excludes:
        return
    try:
        machinery = machinery_paths(_matrix_cached())
    except MatrixError as err:
        raise ValueError("'impact_floor.repos.%s.hold_exclude' cannot be checked against the machinery paths: %s" % (name, err)) from None
    for glob in excludes:
        hit = exclude_hits_machinery(glob, machinery)
        if hit:
            raise ValueError("'impact_floor.repos.%s.hold_exclude' names %r, which reaches the clear and hold machinery (%s): the machinery is never excluded" % (name, glob, ", ".join(hit[:3])))


def entry_hold(config, repo, matrix=None):
    """The per-repository owner hold of `repo`: None when its `impact_floor.repos` entry has no `hold` / `hold_exclude` key (the kit-wide rule, as before);
    ("invalid", why) when it cannot be read (the caller holds the whole change and says so, never no hold); else ("ok", {class: globs}, exclude, replaces)
    where `replaces` is False for the kit repository (named in the matrix floor): there the entry only ADDS holds and the matrix top tier paths still apply."""
    wanted = (repo or "").lower()
    section = (config or {}).get("impact_floor") if isinstance(config, dict) else None
    repos = section.get("repos") if isinstance(section, dict) else None
    if section is not None and not isinstance(section, dict) or repos is not None and not isinstance(repos, dict):
        return None
    for name, entry in (repos or {}).items():
        if isinstance(name, str) and name.lower() == wanted:
            if not isinstance(entry, dict) or not ("hold" in entry or "hold_exclude" in entry):
                return None
            try:
                problem = hold_problem(entry)
            except Exception as err:   # anything odd about the entry: fail towards holding the whole change
                problem = "%s' unreadable: %s" % ("hold", type(err).__name__)
            if problem:
                return "invalid", problem
            try:
                kit = {n.lower() for n in default_floors(matrix)}
            except MatrixError:
                kit = {wanted}
            hold, HOLD_CLASSES = entry.get("hold", {}), hold_classes()
            return "ok", {cls: list(hold.get(cls) or []) for cls in HOLD_CLASSES}, list(entry.get("hold_exclude") or []), wanted not in kit
    return None


def entry_hits(paths, hold, exclude):
    """[(class, glob, path)] of the changed paths that match a listed glob of the entry and no exclude (an empty class lists nothing)."""
    out = []
    for cls in hold_classes():
        for glob in hold.get(cls) or []:
            for p in sorted({p for p in paths if isinstance(p, str)}):
                if matches(p, [glob]) and not matches_named(p, exclude):
                    out.append((cls, glob, p))
    return out


def load_matrix(path=None):
    return json.loads(Path(path or MATRIX_PATH).read_text())


def cell_action(matrix, severity, probability):
    """(level, action) of one cell, or None for an unrated impact (probability Eliminated, which cannot occur and is dropped)."""
    if probability in (matrix.get("not_rated") or ()):
        return None
    cell = ((matrix["cells"].get(severity) or {}).get(probability))
    if cell is None:
        raise ValueError("no such cell: %s / %s" % (severity, probability))
    return cell["level"], cell["action"]


def rate(matrix, impacts):
    """The rated impacts of a list of (severity, probability): an Eliminated one yields no rating. Returns [(severity, probability, level, action)]."""
    rated = []
    for severity, probability in impacts:
        got = cell_action(matrix, severity, probability)
        if got is not None:
            rated.append((severity, probability, got[0], got[1]))
    return rated


def decide(floor_hit, rated, unknown=None):
    """The combined decision: "hold" when the floor holds (whatever the rating says: it cannot be lowered) or any rated impact is H;
    "second-review" when none holds and a rated impact asks for 2; else "none". Returns (decision, reasons)."""
    reasons = []
    if unknown:   # the diff cannot be read in full: fail towards hold
        reasons.append("floor unknown: %s" % unknown)
    if floor_hit:
        reasons.append("floor: %d path(s) inside the floor globs" % len(floor_hit))
    reasons.extend("rating: %s / %s is %s (hold)" % (s, p, level) for s, p, level, action in rated if action == ACTION_HOLD)
    if reasons:
        return "hold", reasons
    if any(action == ACTION_SECOND for _, _, _, action in rated):
        return "second-review", ["rating: a medium-band impact asks for a second independent review"]
    return "none", []


# ---- who may clear a hold: the owner's own clear is for the top tier only (ask #451). Everything below reads the matrix file's `owner_hold` block.

def owner_hold_rule(matrix):
    """The `owner_hold` block of the matrix, normalised: (top classes, other classes, {class: globs}) with class names lower-cased, or None when the block is
    missing or MALFORMED (not an object, a list that is not a list of strings, no top tier path at all). None is read by `owner_decide` as: every change holds."""
    block = (matrix or {}).get("owner_hold") if isinstance(matrix, dict) else None
    if not isinstance(block, dict):
        return None
    top, other, paths = block.get("top_tier_classes"), block.get("other_classes"), block.get("top_tier_paths")
    if not (isinstance(top, list) and isinstance(other, list) and isinstance(paths, dict) and top and paths):
        return None
    if not all(isinstance(c, str) and c.strip() for c in top + other):
        return None
    if not all(isinstance(g, list) and all(isinstance(x, str) and x.strip() for x in g) for g in paths.values()) or not any(paths.values()):
        return None
    top = [c.strip().lower() for c in top]
    other = [c.strip().lower() for c in other if c.strip().lower() not in top]
    return top, other, {c: list(g) for c, g in paths.items()}


def clear_code_paths(matrix):
    """The matrix's `owner_hold.delegated_clear.clear_code_paths` list (the files that ARE the clear and hold machinery: never delegated, and always held for the owner), the ONE
    reader the floor and the delegated clear both use. Returns (list or None when the block is absent, problem or None); a block that is present but whose list is not a list of
    non-empty strings is a problem (the floor then holds)."""
    hold = matrix.get("owner_hold") if isinstance(matrix, dict) else None
    block = hold.get("delegated_clear") if isinstance(hold, dict) else None
    if block is None:
        return None, None
    if not isinstance(block, dict):
        return None, "owner_hold.delegated_clear is malformed"
    got = block.get("clear_code_paths")
    if not (isinstance(got, list) and got and all(isinstance(x, str) and x.strip() for x in got)):
        return None, "owner_hold.delegated_clear.clear_code_paths is missing or malformed"
    return list(got), None


def top_tier_hits(paths, matrix):
    """{class: [paths]} of the changed paths that match a top tier glob of the matrix file (a path may be named under more than one class)."""
    rule = owner_hold_rule(matrix)
    if rule is None:
        return {}
    hits = {}
    for cls, globs in rule[2].items():
        got = sorted({p for p in paths if isinstance(p, str) and matches(p, globs)})
        if got:
            hits[cls] = got
    return hits


def narrow_rule(matrix):
    """(globs, keys, max_files) of the matrix file's `owner_hold.narrow_by_diff` block, or None when it is absent or malformed (then the whole
    top tier path holds, as it did before). The block names the paths whose hold depends on their CONTENT (an agent definition: only its permission
    block counts), the top level front matter keys that make that block, and how many such files one pull request may change before it holds."""
    hold = matrix.get("owner_hold") if isinstance(matrix, dict) else None
    block = hold.get("narrow_by_diff") if isinstance(hold, dict) else None
    if not isinstance(block, dict):
        return None
    globs, keys, limit = block.get("paths"), block.get("keys"), block.get("max_files")
    if not (isinstance(globs, list) and globs and all(isinstance(g, str) and g.strip() for g in globs)):
        return None
    if not (isinstance(keys, list) and keys and all(isinstance(k, str) and re.fullmatch(r"[A-Za-z_][\w-]*", k) for k in keys)):
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        return None
    return list(globs), list(keys), limit


def narrow_paths(matrix, paths):
    """([paths], limit): the changed paths the matrix file's `narrow_by_diff` block judges by content (a path another top tier glob also names stays a plain
    top tier path), sorted, and the most such files one pull request may change before it holds unread. ([], None) when there is no usable block."""
    rule, top = narrow_rule(matrix), owner_hold_rule(matrix)
    if rule is None or top is None:
        return [], None
    globs, _keys, limit = rule
    elsewhere = [g for named in top[2].values() for g in named if g not in globs]
    return sorted({p for p in paths if isinstance(p, str) and matches(p, globs) and not matches(p, elsewhere)}), limit


NARROW_TEXT_MAX = 200000   # a definition larger than this is not parsed: it holds


_YAML_INDIRECTION = re.compile(r"(?:^|[\s:\[{,-])[&*!]\S")              # an anchor, an alias or a tag (outside quotes): a value can then come from somewhere else
_YAML_QUOTED = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'")
_YAML_BLOCK_SCALAR = re.compile(r"(?:^|[\s:-])[|>][+\-0-9]*\s*$")     # a block scalar indicator: `#` and blank lines inside it are content
_LINE_BREAKS = ("\x85", "\u2028", "\u2029")                            # line ends a YAML 1.1 reader honours and `split` does not


_DOUBLE_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')
_SINGLE_QUOTED = re.compile(r"'(?:[^']|'')*'")
_KEY_LINE = re.compile(r"([A-Za-z_][\w./*-]*)[ \t]*:(?:[ \t]+(.*))?$")


def _after_comment(rest):
    """True when `rest` (what follows a value on its line) is empty or a comment."""
    return not rest.strip() or (rest[:1] in " \t" and rest.strip().startswith("#"))


def _flow_closed(value):
    """True when the flow collection that `value` starts with (`[` or `{`) closes on this line and only a comment follows. A quote opens only where a value can start."""
    depth, quote, previous, index = 0, None, "", 0
    while index < len(value):
        char = value[index]
        if quote:
            if quote == '"' and char == "\\":
                index += 1
            elif char == quote:
                if quote == "'" and value[index + 1:index + 2] == "'":
                    index += 1
                else:
                    quote = None
        elif char in "\"'" and previous in ":,[{":
            quote = char
        elif char == "#" and index and value[index - 1] in " \t":
            return False   # a comment before the collection closed: what follows it (a closer too) is comment text, so the collection is still open
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return _after_comment(value[index + 1:])
            if depth < 0:
                return False
        if not quote and not char.isspace():
            previous = char
        index += 1
    return False


def _value_ok(value):
    """True for a value this reader understands: empty, one quoted scalar closed on the line, a flow collection closed on the line, or a plain scalar (no
    indicator at its start, no `: ` inside). Everything else (a block scalar, an anchor, a tag, a directive, a complex key, a scalar left open) is refused."""
    value = value.strip()
    if not value:
        return True
    if value[0] == '"':
        found = _DOUBLE_QUOTED.match(value)
        return bool(found) and _after_comment(value[found.end():])
    if value[0] == "'":
        found = _SINGLE_QUOTED.match(value)
        return bool(found) and _after_comment(value[found.end():])
    if value[0] in "[{":
        return _flow_closed(value)
    if value[0] in "|>&*!%@`?:#,]}" or (value[0] == "-" and value[1:2] in ("", " ", "\t")):
        return False
    plain = re.split(r"[ \t]#", value, 1)[0]
    return ": " not in plain and not plain.rstrip().endswith(":")


def _line_ok(line):
    """True when one front matter line has a shape this reader understands: a comment, a `key: value`, a `- value` or a `- key: value`, each with a value
    that `_value_ok` accepts. A positive list on purpose: whatever is not on it cannot be judged line by line, and the caller holds."""
    body = line.strip()
    if body.startswith("#"):
        return True
    if body == "-" or body.startswith("- ") or body.startswith("-\t"):
        body = body[1:].strip()
        if not body:
            return True
        if body == "-" or body.startswith("- "):
            return False   # a nested sequence on one line
    found = _KEY_LINE.match(body)
    return _value_ok(found.group(2) or "") if found else _value_ok(body)


def permission_block(text, keys):
    """{key: [normalised lines]} of the top level front matter keys `keys` in `text`, or None when it cannot be judged: no front matter, not text, too
    large, or anything outside the plain subset of YAML that this reader understands (a quoted or merge key or any other column 0 line that is no plain
    `key:`; an anchor, alias or tag; a repeated tracked key; a block scalar under a tracked key; a front matter closed anywhere but at column 0; any line whose shape is not on the positive list of `_line_ok` (a quoted scalar or flow collection that does not close on its own line, a complex key, a directive, a plain scalar spread over lines, ...); a Unicode
    line break). The caller holds on None. A key's block is its line and every following line up to the next top level key; blank lines and comments at
    column 0 are dropped, line ends and trailing space are normalised. Two versions have the same permission block when these dicts are equal."""
    if not isinstance(text, str) or len(text) > NARROW_TEXT_MAX or any(mark in text for mark in _LINE_BREAKS):
        return None
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None
    end = next((n for n in range(1, len(lines)) if lines[n].rstrip() == "---"), None)
    if end is None:
        return None
    found, current = {}, None
    for line in lines[1:end]:
        if not line.strip():
            continue
        if not line.lstrip().startswith("#") and (_YAML_INDIRECTION.search(_YAML_QUOTED.sub('""', line)) or not _line_ok(line)):
            return None
        indented = line[0] in " \t"
        if not indented and line.startswith("#"):
            continue   # a comment at the start of a line (outside any block)
        if indented:
            if current is not None:
                found[current].append(line.rstrip())   # content of a tracked block, a `#` line inside it too
            continue
        top = re.match(r"([A-Za-z_][\w-]*)\s*:", line)
        if top:
            current = top.group(1) if top.group(1) in keys else None
            if current is not None:
                if current in found:
                    return None   # a repeated tracked key: which one a reader takes is not ours to guess
                found[current] = [line.rstrip()]
        elif line.startswith("- ") or line.rstrip() == "-":
            if current is not None:
                found[current].append(line.rstrip())   # a list item written at the key's own indentation
        else:
            return None   # a line that is no plain key (a quoted key, a merge key, a stray line)
    if any(_YAML_BLOCK_SCALAR.search(line) for block in found.values() for line in block):
        return None
    return found


def block_state(old, new, keys):
    """"unchanged" or "changed" for two versions of a definition, "unreadable" when either has no parsable front matter."""
    before, after = permission_block(old, keys), permission_block(new, keys)
    if before is None or after is None:
        return "unreadable"
    return "unchanged" if before == after else "changed"


def class_is_top_tier(matrix, cls):
    """True for a top tier class, a missing class and an UNKNOWN class (fail closed); False only for a class the matrix file names as not top tier
    (compared case-insensitively)."""
    rule = owner_hold_rule(matrix)
    if rule is None or not isinstance(cls, str) or not cls.strip():
        return True
    return cls.strip().lower() not in rule[1]


def registrar_reasons(registrar):
    """[reasons] the registrar lint (#964) holds a pull request for the owner. `registrar` is None when the daemon file did not change (nothing was read), else {"findings": [(registrar, verb, name)], "closure": [the closure
    names] or absent, "why": None or the reason the diff could not be read}. An unreadable diff holds (fail closed); a finding holds and names the registrar and, when the closure derivation names it, the closure name,
    otherwise says that the shape is not allowed. The reason says the same words as the rater's line."""
    if registrar is None:
        return []
    if not isinstance(registrar, dict) or registrar.get("why") or not isinstance(registrar.get("findings"), list):
        return ["registrar diff could not be read (%s): held" % str((registrar or {}).get("why") if isinstance(registrar, dict) else "unreadable")[:120]]
    closure = set(registrar.get("closure") or ())
    return ["registrar %s %s closure name %s: %s" % (name, verb, hit, REGISTRAR_RULE_TEXT) if hit in closure
            else "registrar %s %s %s: this shape is not allowed in a registrar (%s)" % (name, verb, hit, REGISTRAR_RULE_TEXT) for name, verb, hit in registrar["findings"]]


REGISTRAR_RULE_TEXT = "a registrar that imports anything, writes outside its own names, or calls a function it neither defines nor receives, except the named registration helpers and the pure builtins of one table (every import, call or rebinding of a name of the clear's closure among them) is held for the owner"


def owner_decide(matrix, paths, impacts, unknown=None, floor_hit=(), narrow=None, record=None, registrar=None, entry=None):
    """The decision under the owner-hold rule. `paths`: every changed path (a deletion and a rename's old path too); `impacts`: [(severity, probability, class)]
    from the independent rating (class may be None); `floor_hit`: the paths that hit the old floor (floor_paths). Returns (decision, reasons):

      - "hold" (for the owner): the diff cannot be read in full; or a changed path is a top tier path of the matrix file; or a rated impact is at a hold
        level and its class is top tier, missing or unknown;
      - "second-review": no hold, but a floor path outside the top tier, a hold-level rating of a class the matrix file names as not top tier, or a
        medium-band rating asks for a second independent review (the usual checks then decide the merge);
      - "none" otherwise.

    `narrow` ({path: "unchanged"|"changed"|"added"|"removed"|"unreadable"}, from the caller's content reads) judges the paths the matrix file's
    `narrow_by_diff` block names: only a changed permission block holds; an added, removed or unreadable one holds; a path with no entry is unread and
    holds; more such paths than the block allows hold without being read. `record` ((action, class) of the independent impact reviewer's record for
    this head, or None): a hold action of a top tier, missing or unknown class holds, of a class the matrix file names as not top tier asks for a second review.
    `entry` (from `entry_hold`, default None: nothing changes): a repository's own hold. ("ok", ...) replaces the top tier PATH hold of that repository (not for the
    kit repository, where it only adds); ("invalid", why) holds the whole change and says so. The machinery holds below run whatever the entry says."""
    reasons = []
    if owner_hold_rule(matrix) is None:   # FAIL CLOSED: no usable rule means every change holds for the owner
        reasons.append("owner_hold rule missing or malformed in the impact matrix file: every change holds")
    if unknown:
        reasons.append("floor unknown: %s" % unknown)
    lost = sorted({p for p in paths if isinstance(p, str) and escapes(p)})
    if lost:   # a path that leaves the repository cannot be judged as a file of it: hold
        reasons.append("path outside the repository or not normalisable: %s" % ", ".join(repr(p)[:60] for p in lost[:3]))
    listed, problem = clear_code_paths(matrix)   # the clear and hold machinery, by name: never delegated, so always held for the owner
    if problem:
        reasons.append("%s: every change holds" % problem)
    if listed:
        machinery = sorted({p for p in paths if isinstance(p, str) and matches(p, listed)})
        if machinery:
            reasons.append("clear_code_paths (the clear and hold machinery, never delegated): %s" % ", ".join(machinery[:5]) + (" ..." if len(machinery) > 5 else ""))
    replaced = isinstance(entry, tuple) and entry[0] == "ok" and entry[3]
    if isinstance(entry, tuple) and entry[0] == "invalid":
        reasons.append("impact_floor.repos.<repo>.hold unreadable or invalid (%s): kit-wide default applied" % entry[1])
    if isinstance(entry, tuple) and entry[0] == "ok":
        for cls, glob, path in entry_hits(paths, entry[1], entry[2]):
            reasons.append("owner hold (%s): %s matches %s" % (cls, path, glob))
    hits = {} if replaced else top_tier_hits(paths, matrix)
    prose = []   # narrow paths whose permission block did not change: no hold from the path
    narrowed, limit = ([], None) if replaced else narrow_paths(matrix, paths)
    if narrowed:
        if len(narrowed) > limit:
            reasons.append("more than %d definitions with a narrow rule changed (%d): not read, held" % (limit, len(narrowed)))
        else:
            for path in narrowed:
                for cls in list(hits):
                    hits[cls] = [x for x in hits[cls] if x != path]
                state = (narrow or {}).get(path)
                if state == "unchanged":
                    prose.append(path)
                else:
                    why = {"changed": "permission block changed", "added": "added", "removed": "removed", "unreadable": "its permission block could not be read"}.get(state, "its permission block was not read")
                    reasons.append("top tier path (permissions): %s (%s)" % (path, why))
            hits = {cls: got for cls, got in hits.items() if got}
    for cls, got in sorted(hits.items()):
        reasons.append("top tier path (%s): %s" % (cls, ", ".join(got[:5]) + (" ..." if len(got) > 5 else "")))
    for reason in registrar_reasons(registrar):
        reasons.append(reason)
    if record is not None and record[0] == "hold" and class_is_top_tier(matrix, record[1]):
        reasons.append("independent impact review says hold, class %s" % (record[1] or "missing"))
    rated = []
    for severity, probability, cls in impacts:
        for _s, _p, level, action in rate(matrix, [(severity, probability)]):
            rated.append((severity, probability, level, action, cls))
    for severity, probability, level, action, cls in rated:
        if action == ACTION_HOLD and class_is_top_tier(matrix, cls):
            reasons.append("rating: %s / %s is %s (hold), class %s" % (severity, probability, level, cls or "missing"))
    if reasons:
        return "hold", reasons
    softer = []
    if prose:
        softer.append("definition change without a permission block change (%s): a second independent review" % ", ".join(prose[:3]))
    if record is not None and record[0] == "hold":   # only a class the matrix file names as not top tier reaches here
        softer.append("independent impact review says hold, class %s: a second independent review" % record[1])
    if floor_hit:
        softer.append("floor: %d path(s) outside the top tier ask for a second independent review" % len(floor_hit))
    softer.extend("rating: %s / %s (%s), class %s, asks for a second independent review" % (s, p, level, cls) for s, p, level, action, cls in rated
                  if action in (ACTION_HOLD, ACTION_SECOND))
    if softer:
        return "second-review", softer
    return "none", []


def validate_hold_config(raw):
    """Raise ValueError naming the key when `impact_hold` is not {enabled?, dry_run?, calls_per_pass?, carry_clear?, repos: {owner/repo: {enabled}}}.
    The hold is OFF unless `enabled` is true (the one line a person flips)."""
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ValueError("'impact_hold' must be an object")
    unknown = sorted(set(raw) - {"enabled", "dry_run", "calls_per_pass", "repos", "carry_clear"})
    if unknown:
        raise ValueError("'impact_hold' has key(s) this tool does not read: %s" % ", ".join(unknown))
    for key in ("enabled", "dry_run", "carry_clear"):
        if key in raw and not isinstance(raw[key], bool):
            raise ValueError("'impact_hold.%s' must be true or false" % key)
    calls = raw.get("calls_per_pass", 1)
    if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
        raise ValueError("'impact_hold.calls_per_pass' must be a whole number, 0 or more")
    repos = raw.get("repos", {})
    if not isinstance(repos, dict):
        raise ValueError("'impact_hold.repos' must be an object keyed by owner/repo")
    for name, entry in repos.items():
        if not isinstance(entry, dict) or set(entry) - {"enabled"} or not isinstance(entry.get("enabled", True), bool):
            raise ValueError("'impact_hold.repos.%s' must be {\"enabled\": true|false}" % name)


def carry_never_paths(matrix=None):
    """(#815) The paths whose touch refuses the conflict-resolving carry of an owner's clear: the matrix file's `owner_hold.never_carried_paths` (the clear's own code and the matrix itself), as a
    tuple. ONE table, in the data: this code names no path. () when the block is missing or malformed: the carry is then refused (an empty table protects nothing, so the carry refuses it)."""
    try:
        matrix = _matrix_cached() if matrix is None else matrix
    except MatrixError:
        return ()
    block = matrix.get("owner_hold") if isinstance(matrix, dict) else None
    listed = block.get("never_carried_paths") if isinstance(block, dict) else None
    if not isinstance(listed, list) or not listed or not all(isinstance(x, str) and x.strip() for x in listed):
        return ()
    return tuple(listed)


def carry_enabled(config, repo):
    """True only when the owner turned `impact_hold.carry_clear` on (the default is OFF) and the hold itself is enforced for `repo`: an owner's clear then carries over a pure merge
    of main (see bin/clear_carry.py). Anything else, a string or 1 included, is off."""
    section = (config or {}).get("impact_hold") or {}
    return section.get("carry_clear") is True and hold_mode(config, repo) == "enforce"


def hold_mode(config, repo):
    """"off" (the default: nothing is written), "dry-run" (decisions are audited, nothing is written) or "enforce", for `repo`.
    A per-repo `enabled: false` stops the writes for that repo."""
    section = (config or {}).get("impact_hold") or {}
    if section.get("enabled") is not True:
        return "off"
    for name, entry in (section.get("repos") or {}).items():
        if name.lower() == (repo or "").lower() and entry.get("enabled") is False:
            return "off"
    return "dry-run" if section.get("dry_run") else "enforce"
