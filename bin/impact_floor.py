"""The impact hold's deterministic floor and decision table (#179, slice L3). Pure: nothing here reads GitHub, a file the caller did not
name, or the clock. The floor is PATH GLOBS only (a PR that adds, changes or deletes a file inside its repo's floor globs holds, whatever any
reviewer says); the independent rating can only ADD a hold, never lower the floor; the size of a change is never a hold by itself. The
matrix is docs/review-laws/impact-matrix.json, the single source of truth. L3 is advisory in code terms: it decides and records; it writes
no label (that is slice L4)."""

import fnmatch
import json
import re
from pathlib import Path

# The floor globs per repo, a proposal the owner confirms: the gh shim (signing and gates), the policy hook, the service units, the
# permissions generator and the ledger format. fleet-kit has no migrations directory and no declared public API files, so none are named.
# Excluded: definitions and docs are already captain PRs, and including them would hold nearly every PR.
DEFAULT_FLOORS = {
    "adamkaplan/fleet-kit": {
        "globs": ["shims/**", "plugins/fleet-hooks/**", "templates/**", "tools/cos-permissions-proof/**", "bin/plan_ledger.py",
                  "bin/impact_floor.py", "docs/review-laws/impact-matrix.json"],
        "exclude": ["agents/**", "skills/**", "docs/**", "AGENTS.md"],
        # the logic and the table that decide holds are never excluded with the docs: changing them must hold
        "force": ["bin/impact_floor.py", "docs/review-laws/impact-matrix.json"],
    },
}
ACTION_HOLD, ACTION_SECOND, ACTION_NONE = "H", "2", "."
MATRIX_PATH = Path(__file__).resolve().parent.parent / "docs" / "review-laws" / "impact-matrix.json"


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
    return re.compile("^" + "".join(out) + "$")


def matches(path, patterns):
    """Case-insensitive: the fleet runs on case-insensitive checkouts where `SHIMS/x` and `shims/x` are one file (a false hold costs little)."""
    lowered = path.lower()
    return any(_glob_regex(p).match(lowered) for p in patterns)


def floor_paths(paths, globs, exclude=(), force=()):
    """The paths of a change that trigger the floor: inside a floor glob and not excluded (a `force` path is never excluded). A deletion
    counts like any change (the caller passes deleted paths too; so does the OLD path of a rename); one outside the globs never does."""
    return sorted({p for p in paths if isinstance(p, str) and matches(p, globs) and (not matches(p, exclude) or matches(p, force))})


def floor_for(repo, config=None):
    """(globs, exclude, force) for `repo` from the config (`impact_floor.repos.<owner/repo>`), else the built-in proposal, else none."""
    wanted = (repo or "").lower()
    for name, entry in (((config or {}).get("impact_floor") or {}).get("repos") or {}).items():
        if name.lower() == wanted and isinstance(entry, dict):
            return list(entry.get("globs") or []), list(entry.get("exclude") or []), list(entry.get("force") or [])
    for name, entry in DEFAULT_FLOORS.items():
        if name.lower() == wanted:
            return list(entry["globs"]), list(entry["exclude"]), list(entry.get("force") or [])
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
