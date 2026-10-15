"""The impact hold's deterministic floor and decision table (#179, slice L3). Pure: nothing here reads GitHub, a file the caller did not
name, or the clock. The floor is PATH GLOBS only (a PR that adds, changes or deletes a file inside its repo's floor globs holds, whatever any
reviewer says); the independent rating can only ADD a hold, never lower the floor; the size of a change is never a hold by itself. The
matrix is docs/review-laws/impact-matrix.json, the single source of truth. L3 is advisory in code terms: it decides and records; it writes
no label (that is slice L4)."""

import fnmatch
import json
import re
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


def default_floors(matrix=None):
    """The built-in floor per repo, read from the matrix file's `owner_hold.floor` (a proposal the owner confirms): the code names no path. Raises
    MatrixError when the file cannot be read or holds no `owner_hold.floor` object."""
    floors = ((matrix if matrix is not None else _matrix_cached()).get("owner_hold") or {}).get("floor")
    if not isinstance(floors, dict):
        raise MatrixError("the impact matrix file has no owner_hold.floor object")
    return {name: entry for name, entry in floors.items() if isinstance(entry, dict)}


def _glob_regex(pattern):
    return _compiled(pattern.lower())


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
    segment before it. `escapes` is True when a `..` has nothing to remove (the path leaves the repository). A variant of a path (`Shims/GH`, `shims//gh`,
    `shims/./gh`, `shims/x/../gh`, `a/../shims/gh`, `shims\\gh`) names the same file as the plain one, so it must be judged as that file."""
    parts, escapes = [], False
    for segment in str(path).replace("\\", "/").split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            else:
                escapes = True
            continue
        parts.append(segment)
    return "/".join(parts).lower(), escapes


def escapes(path):
    """True when the path cannot be read as a path inside the repository: it is HELD (when in doubt, hold)."""
    return canonical(path)[1]


def matches(path, patterns):
    """Case-insensitive, and judged as the file the path names (see `canonical`): the fleet runs on case-insensitive checkouts where `SHIMS/x` and `shims/x`
    are one file. A variant can only ADD a hit: the raw lower-cased spelling and the canonical one are both tried (a false hold costs little)."""
    lowered = path.lower()
    named = canonical(path)[0]
    return any(_glob_regex(p).fullmatch(lowered) or _glob_regex(p).fullmatch(named) for p in patterns)


def floor_paths(paths, globs, exclude=(), force=()):
    """The paths of a change that trigger the floor: inside a floor glob and not excluded (a `force` path is never excluded). A deletion
    counts like any change (the caller passes deleted paths too; so does the OLD path of a rename); one outside the globs never does."""
    return sorted({p for p in paths if isinstance(p, str) and (escapes(p) or (matches(p, globs) and (not matches(p, exclude) or matches(p, force))))})


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
        extra = sorted(set(entry) - {"globs", "exclude", "force"})
        if extra:
            raise ValueError("'impact_floor.repos.%s' has key(s) this tool does not read: %s" % (name, ", ".join(extra)))
        for key in ("globs", "exclude", "force"):
            value = entry.get(key, [])
            if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
                raise ValueError("'impact_floor.repos.%s.%s' must be a list of non-empty path globs, got %r" % (name, key, value))
        if not entry.get("globs"):
            raise ValueError("'impact_floor.repos.%s.globs' must name at least one glob" % name)


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


def class_is_top_tier(matrix, cls):
    """True for a top tier class, a missing class and an UNKNOWN class (fail closed); False only for a class the matrix file names as not top tier
    (compared case-insensitively)."""
    rule = owner_hold_rule(matrix)
    if rule is None or not isinstance(cls, str) or not cls.strip():
        return True
    return cls.strip().lower() not in rule[1]


def owner_decide(matrix, paths, impacts, unknown=None, floor_hit=()):
    """The decision under the owner-hold rule. `paths`: every changed path (a deletion and a rename's old path too); `impacts`: [(severity, probability, class)]
    from the independent rating (class may be None); `floor_hit`: the paths that hit the old floor (floor_paths). Returns (decision, reasons):

      - "hold" (for the owner): the diff cannot be read in full; or a changed path is a top tier path of the matrix file; or a rated impact is at a hold
        level and its class is top tier, missing or unknown;
      - "second-review": no hold, but a floor path outside the top tier, a hold-level rating of a class the matrix file names as not top tier, or a
        medium-band rating asks for a second independent review (the usual checks then decide the merge);
      - "none" otherwise."""
    reasons = []
    if owner_hold_rule(matrix) is None:   # FAIL CLOSED: no usable rule means every change holds for the owner
        reasons.append("owner_hold rule missing or malformed in the impact matrix file: every change holds")
    if unknown:
        reasons.append("floor unknown: %s" % unknown)
    lost = sorted({p for p in paths if isinstance(p, str) and escapes(p)})
    if lost:   # a path that leaves the repository cannot be judged as a file of it: hold
        reasons.append("path outside the repository or not normalisable: %s" % ", ".join(repr(p)[:60] for p in lost[:3]))
    for cls, got in sorted(top_tier_hits(paths, matrix).items()):
        reasons.append("top tier path (%s): %s" % (cls, ", ".join(got[:5]) + (" ..." if len(got) > 5 else "")))
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
    if floor_hit:
        softer.append("floor: %d path(s) outside the top tier ask for a second independent review" % len(floor_hit))
    softer.extend("rating: %s / %s (%s), class %s, asks for a second independent review" % (s, p, level, cls) for s, p, level, action, cls in rated
                  if action in (ACTION_HOLD, ACTION_SECOND))
    if softer:
        return "second-review", softer
    return "none", []


def validate_hold_config(raw):
    """Raise ValueError naming the key when `impact_hold` is not {enabled?, dry_run?, calls_per_pass?, repos: {owner/repo: {enabled}}}.
    The hold is OFF unless `enabled` is true (the one line a person flips)."""
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ValueError("'impact_hold' must be an object")
    unknown = sorted(set(raw) - {"enabled", "dry_run", "calls_per_pass", "repos"})
    if unknown:
        raise ValueError("'impact_hold' has key(s) this tool does not read: %s" % ", ".join(unknown))
    for key in ("enabled", "dry_run"):
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
