"""path_suites: which test suites (and classes) exercise a changed path (ask #1045). Read-only on the repository: it reads tracked files and, for a merged run,
works in a temporary detached worktree that is removed afterwards; it never edits the checkout, never touches the branch, never stops a process.

The map is DERIVED by search: a suite exercises a path when its source names the path, the file's name, the module (an import, a quoted name, a `/name`), or a
source file that itself names the path (one hop, `HOPS`). A hand-kept row (`tools/path-suites.json`: glob, suites, REASON) is added to the derivation, never
instead of it; `check_manual` fails a row that names a suite that does not exist or misses one the derivation finds.

Cause (from the recorded case, neutral names): a change to the `gh` command's shim went green on the classes the review request named and in the lane, and was red
twice in CI on a second suite that also runs the shim. Two suites ran that path; the review request named one. The same shape: a pane-exit change and a registrar
lint. Nothing listed the suites that run a path, so the classes a diff happens to name were taken for all of them."""
import fnmatch
import json
import os
import re
import subprocess
import tempfile

MANUAL = "tools/path-suites.json"
HEADER = "suites that exercise your change"
HOPS = 1
HUB_MAX = 3   # a source named by more suites than this is a hub (a command line, a shared helper): it is not followed, or one change would name every suite
READ_MAX = 4 * 1024 * 1024
SOURCE_SUFFIXES = (".py", ".mjs", ".js", ".sh")
SKIP_DIRS = ("node_modules/", ".git/", "scenarios/", "__pycache__/")
SUITE_PATTERNS = ("bin/test-*", "bin/test_*.py", "cloud/tests/test-*", "*.test.mjs", "*/test_*.py", "test_*.py")


# The guards every NEW file under bin/ must pass, whatever suite touches the file's own functions (ask #1045, clause 2). One table: (name, file, class, test selector or None,
# what it guards). A class guard runs `-k <class>`; a guard with a selector runs `-k <selector>` (the test inside the class); a script has no class and runs itself.
NEW_BIN_GUARDS = (
    ("TestAuthorityModulesGuard", "bin/test-switchboard", "TestAuthorityModulesGuard", None, "a module that decides who may clear, hold or merge is listed or exempted by name"),
    ("TestAgentRegistryInventory", "bin/test-switchboard", "TestAgentRegistryInventory", None, "every module that reads or writes an agent record is in the registry inventory"),
    ("TestImpactHold module-name guard", "bin/test-switchboard", "TestImpactHold", "test_the_modules_the_clear_loads_or_runs_are_all_judged_and_the_list_is_derived_from_what_runs", "every module the clear runs is judged by name"),
    ("bin/check-public-names", "bin/check-public-names", None, None, "no private name in a tracked file"),
)


class PathSuitesError(Exception):
    pass


def _git(root, *args, runner=subprocess.run, check=False):
    done = runner(["git"] + list(args), cwd=root, capture_output=True, text=True)
    if check and done.returncode != 0:
        raise PathSuitesError("git %s: %s" % (args[0], (done.stderr or "").strip()[:200]))
    return done


def tracked(root, runner=subprocess.run):
    done = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", runner=runner)
    if done.returncode != 0:
        raise PathSuitesError("not a git work tree: %s" % root)
    return [p for p in done.stdout.split("\0") if p and os.path.isfile(os.path.join(root, p)) and not p.startswith(SKIP_DIRS) and "/__pycache__/" not in p and not p.endswith(".pyc")]


def is_suite(path):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in SUITE_PATTERNS) and not path.endswith((".json", ".md"))


def _read(root, path):
    try:
        with open(os.path.join(root, path), encoding="utf-8", errors="replace") as handle:
            return handle.read(READ_MAX)
    except OSError:
        return ""


def terms(path):
    """(strong, weak) regexes that say a source names `path`. Strong forms name the file itself (its path, its file name, an import of the module, the directory and name
    side by side); weak forms are a bare quoted command name or `/name`, which another program may share: they find suites but never carry a hop."""
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    strong, weak = [re.escape(path)], []
    folder = os.path.basename(os.path.dirname(path))
    if folder:
        strong.append(r"""["']""" + re.escape(folder) + r"""["']\s*[,/]\s*["']""" + re.escape(base) + r"""["']""")
    if ext:
        strong.append(r"(?<![\w.-])" + re.escape(base) + r"(?![\w-])")
    if ext in (".py", ".mjs", ".js") and len(stem) >= 3:
        s = re.escape(stem)
        strong += [r"\b(?:import|from)\s+" + s + r"\b", r"load_module\(\s*[\"']" + s]
        weak += [r"""["']""" + s + r"""["']""", r"\b" + s + r"\.\w"]   # a quoted name; for a class body, the module's attribute used (`calc.add`)
    elif not ext:
        s = re.escape(base)
        weak += [r"""["']""" + s + r"""["']""", r"/" + s + r"(?![\w-])"]
    return strong, weak


def _hit(patterns, text):
    return any(re.search(p, text) for p in patterns)


def class_blocks(text):
    """[(class name, body)] of a Python source: a block runs from its `class` line to the next top-level class."""
    marks = [(m.start(), m.group(1)) for m in re.finditer(r"(?m)^class (\w+)", text)]
    return [(name, text[start:marks[i + 1][0] if i + 1 < len(marks) else len(text)]) for i, (start, name) in enumerate(marks)]


def derive(root, path, files=None, texts=None, runner=subprocess.run):
    """{suite path: [classes]} for one changed path, by search of the suites' sources. An empty class list means the suite names the path outside any class: the whole suite."""
    files = tracked(root, runner) if files is None else files
    texts = {} if texts is None else texts

    def text(p):
        if p not in texts:
            texts[p] = _read(root, p)
        return texts[p]
    suites = [p for p in files if is_suite(p)]
    sources = [p for p in files if p not in suites and (p.endswith(SOURCE_SUFFIXES) or not os.path.splitext(p)[1])]
    reached, frontier = [path], [path]
    wanted = list(terms(path)[0] + terms(path)[1])
    for _hop in range(HOPS):
        nxt = []
        for f in frontier:
            strong = terms(f)[0]
            for s in sources:
                if s not in reached and s != f and _hit(strong, text(s)):
                    own = terms(s)[0]
                    if sum(1 for u in suites if _hit(own, text(u))) > HUB_MAX:
                        continue
                    reached.append(s)
                    nxt.append(s)
                    wanted += terms(s)[0]
        frontier = nxt
    flat = wanted
    found = {}
    for s in suites:
        if s == path:
            continue
        body = text(s)
        if not _hit(flat, body):
            continue
        classes = [name for name, block in class_blocks(body) if name.startswith("Test") and _hit(flat, block)] if body.startswith("#!") or s.endswith(".py") else []
        found[s] = classes
    return found


def load_manual(root):
    try:
        with open(os.path.join(root, MANUAL), encoding="utf-8") as handle:
            rows = json.load(handle).get("rows", [])
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("glob"), str)]


def _glob(path, pattern):
    return fnmatch.fnmatchcase(path, pattern)


def suites_for(root, path, files=None, texts=None, runner=subprocess.run):
    """{suite: [classes]} for `path`: the derivation plus every hand-kept row whose glob matches."""
    files = tracked(root, runner) if files is None else files
    found = derive(root, path, files, texts, runner)
    for row in load_manual(root):
        if _glob(path, row["glob"]):
            for s in row.get("suites", []):
                found.setdefault(s, [])
    return found


def check_manual(root, runner=subprocess.run):
    """Problems of the hand-kept rows: a missing reason, a suite that does not exist, a derived suite the row misses. [] is a pass."""
    files = tracked(root, runner)
    texts, problems = {}, []
    for row in load_manual(root):
        glob = row["glob"]
        if not str(row.get("reason", "")).strip():
            problems.append("%s: no stated reason" % glob)
        listed = row.get("suites", [])
        for s in listed:
            if not os.path.isfile(os.path.join(root, s)):
                problems.append("%s: names a suite that does not exist: %s" % (glob, s))
        for p in files:
            if _glob(p, glob):
                for s in derive(root, p, files, texts, runner):
                    if s not in listed:
                        problems.append("%s: misses %s, which the derivation finds for %s" % (glob, s, p))
    return sorted(set(problems))


def is_python(root, suite):
    return suite.endswith(".py") or _read(root, suite)[:80].startswith("#!") and "python" in _read(root, suite).split("\n", 1)[0]


def command_for(root, suite, classes):
    """The exact command that runs `suite`, narrowed to `classes` when it can be (a Python suite: one `-k` per class)."""
    if suite.endswith((".mjs", ".js")):
        return ["node", "--test", suite]
    cmd = [suite] if not suite.endswith(".py") else ["python3", suite]
    if classes and is_python(root, suite):
        for c in classes:
            cmd += ["-k", c]
    return cmd


def guard_command(guard):
    name, path, cls, test, _why = guard
    if cls is None:
        return [path]
    return [path, "-k", test or cls]


def guard_missing(root, guard):
    """"" when the guard can run in `root`; else the plain sentence of why it cannot (the file, the class or the test is not there)."""
    _name, path, cls, test, _why = guard
    if not os.path.isfile(os.path.join(root, path)):
        return "%s is not in this tree" % path
    if cls is not None:
        body = _read(root, path)
        if not re.search(r"(?m)^class %s\b" % re.escape(cls), body):
            return "class %s is not defined in %s" % (cls, path)
        if test and not re.search(r"def %s\b" % re.escape(test), body):
            return "test %s is not defined in %s" % (test, path)
    return ""


def added_paths(root, base, runner=subprocess.run):
    """The files under bin/ the head ADDS against the merge-base with `base` (committed, uncommitted or untracked)."""
    mb = _git(root, "merge-base", "HEAD", base, runner=runner)
    if mb.returncode != 0 or not mb.stdout.strip():
        raise PathSuitesError("no common ancestor of HEAD and %s" % base)
    diff = _git(root, "diff", "--name-only", "--no-renames", "--diff-filter=A", mb.stdout.strip(), runner=runner, check=True)
    other = _git(root, "ls-files", "--others", "--exclude-standard", runner=runner)
    return sorted(p for p in set(diff.stdout.split("\n")) | set(other.stdout.split("\n")) if p.startswith("bin/") and "__pycache__" not in p and not p.endswith(".pyc"))


def guard_rows(added):
    """The rows (suite, classes, command, why) of the four guards, when `added` holds a new file under bin/; [] otherwise. Same shape as the rows of `plan`."""
    new = [p for p in added or [] if p.startswith("bin/")]
    if not new:
        return []
    return [(g[1], [g[2]] if g[2] else [], guard_command(g), new) for g in NEW_BIN_GUARDS]


def plan(root, paths, files=None, runner=subprocess.run, added=None):
    """([(suite, classes, command, [paths that bring it])], [paths no suite exercises]). `added` (the new files of the diff): a new file under bin/ always brings the four guards of NEW_BIN_GUARDS."""
    files = tracked(root, runner) if files is None else files
    texts, by, none = {}, {}, []
    for p in paths:
        if is_suite(p) and os.path.isfile(os.path.join(root, p)):
            found = {p: []}   # a changed suite runs itself
        else:
            found = suites_for(root, p, files, texts, runner) if os.path.isfile(os.path.join(root, p)) else {}
        if not found:
            none.append(p)
        for s, classes in found.items():
            entry = by.setdefault(s, [set(), []])
            entry[1].append(p)
            entry[0] = entry[0] | set(classes) if (entry[0] or len(entry[1]) == 1) and classes else set()
    rows = []
    for s in sorted(by):
        classes = sorted(by[s][0])
        rows.append((s, classes, command_for(root, s, classes), by[s][1]))
    rows += guard_rows(added)
    return rows, none


def render(rows, none, paths=None):
    lines = [HEADER + ":"]
    for s, classes, cmd, why in rows:
        lines.append("  %s%s\n    run: %s\n    because: %s" % (s, " (%s)" % ", ".join(classes) if classes else " (the whole suite)", " ".join(cmd), ", ".join(why)))
        if why and cmd in [guard_command(g) for g in NEW_BIN_GUARDS] and all(w.startswith("bin/") for w in why):
            lines[-1] += " (new file under bin/: every new bin file must pass this guard)"
    for p in none:
        lines.append("  %s: no suite exercises this path (nothing to run; say so in the pull request)" % p)
    if not rows and not none:
        lines.append("  (no changed paths)")
    return "\n".join(lines)


def is_quick(cmd):
    """True for ONE test file or ONE class: a focused command with at most one selector. A whole suite never is."""
    return (cmd[:2] == ["node", "--test"] and len(cmd) == 3) or (cmd.count("-k") == 1)


# ---- the local merge in a temporary worktree
def default_base(root, runner=subprocess.run):
    ref = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", runner=runner)
    candidates = ([ref.stdout.strip()] if ref.returncode == 0 and ref.stdout.strip() else []) + ["origin/main", "origin/master", "main", "master"]
    for c in candidates:
        if _git(root, "rev-parse", "--verify", "--quiet", c + "^{commit}", runner=runner).returncode == 0:
            return c
    return None


def changed_paths(root, base, runner=subprocess.run):
    """The paths the head (working tree included) changed against the merge-base with `base`, and the merge-base."""
    mb = _git(root, "merge-base", "HEAD", base, runner=runner)
    if mb.returncode != 0 or not mb.stdout.strip():
        raise PathSuitesError("no common ancestor of HEAD and %s" % base)
    diff = _git(root, "diff", "--name-only", "--no-renames", mb.stdout.strip(), runner=runner, check=True)
    other = _git(root, "ls-files", "--others", "--exclude-standard", runner=runner)
    return sorted(p for p in (set(diff.stdout.split("\n")) | set(other.stdout.split("\n"))) if p and "__pycache__" not in p and not p.endswith(".pyc"))


class Merged:
    """Context manager: a detached temporary worktree holding HEAD (and the working tree's uncommitted changes as one commit there) with `base` merged in. `.path` is the
    worktree, `.problem` the sentence when the merge could not be made (a conflict). Removed on exit, with its administrative entry; no branch is ever created."""
    IDENT = ["-c", "user.name=fleet-merged-run", "-c", "user.email=merged-run@example.invalid", "-c", "commit.gpgsign=false"]

    def __init__(self, root, base, runner=subprocess.run):
        self.root, self.base, self.runner, self.path, self.problem, self.holder = root, base, runner, None, None, None

    def __enter__(self):
        self.holder = tempfile.mkdtemp(prefix="merged-run-")
        self.path = os.path.join(self.holder, "tree")
        _git(self.root, "worktree", "add", "--detach", self.path, "HEAD", runner=self.runner, check=True)
        dirty = self.runner(["git", "diff", "--binary", "HEAD"], cwd=self.root, capture_output=True)
        if dirty.stdout:
            applied = self.runner(["git", "apply", "--index", "-"], cwd=self.path, input=dirty.stdout, capture_output=True)
            if applied.returncode != 0:
                self.problem = "the uncommitted changes could not be applied in the temporary worktree"
                return self
            self.runner(["git"] + self.IDENT + ["commit", "-q", "-m", "working tree"], cwd=self.path, capture_output=True, text=True)
        done = self.runner(["git"] + self.IDENT + ["merge", "--no-edit", "--no-ff", self.base], cwd=self.path, capture_output=True, text=True)
        if done.returncode != 0:
            self.problem = "conflict after the merge of %s into the head: %s" % (self.base, " ".join((done.stdout or "").split())[:160])
            self.runner(["git", "merge", "--abort"], cwd=self.path, capture_output=True, text=True)
        return self

    def __exit__(self, *exc):
        if self.path:
            self.runner(["git", "worktree", "remove", "--force", self.path], cwd=self.root, capture_output=True, text=True)
            self.runner(["git", "worktree", "prune"], cwd=self.root, capture_output=True, text=True)
        if self.holder:
            import shutil
            shutil.rmtree(self.holder, ignore_errors=True)
        return False


def gate_finding(changed, suites_by_path, body):
    """The gates finding (information, never a block): for a pull request that changes a path and none of its listed suites is named in `body`.
    `suites_by_path` is {path: {suite: classes}}. Returns the line or None."""
    missing = {}
    for p in changed:
        listed = suites_by_path.get(p) or {}
        if listed and not any(os.path.basename(s) in body or s in body for s in listed):
            for s in listed:
                missing[s] = True
    if not missing:
        return None
    return "finding: the pull request changes paths and names none of the suites that exercise them: %s (information, not a block)" % ", ".join(sorted(missing))


if __name__ == "__main__":   # `python3 bin/path_suites.py --check`: exit 1 when a hand-kept row is stale; `path/...`: print the suites of each path
    import sys
    here = os.getcwd()
    if sys.argv[1:] == ["--check"]:
        problems = check_manual(here)
        print("\n".join(problems) if problems else "path-suites: the hand-kept rows agree with the derivation")
        sys.exit(1 if problems else 0)
    rows, none = plan(here, sys.argv[1:])
    print(render(rows, none))
