"""The installed tree is always the default branch, clean (ask #1380).

The tree the fleet's commands and the daemon run from (the checkout `fleet-switchboard install` was run from) must be on the default branch, at a
commit that is an ancestor of (or equal to) the remote default branch, with no uncommitted change to a tracked file. Otherwise no command, verdict or
merge gate may be computed by that code.

This module only READS the tree: `git status`, `git rev-parse` and `git merge-base`, at most three calls, no fetch, no network, no write (U2). It
applies only to the tree `install` recorded in <state dir>/installed-tree: a worktree, a CI checkout or a test tree has no record and is never
judged. A tree whose remote-tracking ref of the default branch is missing (a fresh clone with no fetch yet) is allowed when it is clean on the default
branch, with a one-line note.

The install also writes the hooks that record a move of HEAD, and copies the two standalone scripts (guard_record_head_move.py, guard_alert.py) into
<state dir>/guard/, outside the tree. Standard library only.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

MARKER = "installed-tree"             # in the state directory: two plain lines, the real path of the installed tree and its default branch (no JSON: nothing to parse)
SCRIPTS_DIR = "guard"                 # in the state directory: the two standalone scripts
SCRIPTS = ("guard_record_head_move.py", "guard_alert.py")
HOOKS = ("post-checkout", "post-merge")
HOOK_MARK = "# fleet-kit: records a move of the installed tree's HEAD (ask #1380)"
EXEMPT_COMMANDS = ("install", "upgrade", "doctor")   # the commands the operator moves the tree with: never refused by the guard
SHOWN_PATHS = 5
GIT_CALLS = 3                         # at most, per check (PC-1)
SHA = re.compile(r"^[0-9a-f]{40}$")


def read_marker(state_dir):
    """(root, default branch) the install recorded, or None (no record, or an unreadable one: the guard does not apply)."""
    try:
        lines = (Path(state_dir) / MARKER).read_text().splitlines()
    except (OSError, ValueError):
        return None
    return (lines[0], lines[1]) if len(lines) == 2 and lines[0] and lines[1] else None


def _git(root, *argv, run=subprocess.run):
    return run(["git", "--no-optional-locks", "-C", str(root)] + list(argv), capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)


def check(root, default_branch, run=subprocess.run):
    """The state of the tree at `root`: {"ok", "note", "branch" (None when detached), "commit", "dirty" [paths], "remote" (commit or None), "why"}."""
    result = {"ok": False, "note": None, "branch": None, "commit": None, "dirty": [], "remote": None, "why": None}
    try:
        status = _git(root, "status", "--porcelain=v1", "--branch", "--untracked-files=no", run=run)
    except (OSError, subprocess.SubprocessError) as err:   # git missing, or it timed out: no evidence of a bad tree
        status = None
        result["note"] = "unreadable: %s" % type(err).__name__
    lines = status.stdout.splitlines() if status is not None and status.returncode == 0 else []
    if not lines or not lines[0].startswith("## "):
        # A read that failed (a lock, a timeout, a repository mid-operation) is NOT evidence of a bad tree: the command runs, the daemon goes on, and
        # the next check reads again. Only a read that succeeded and found something wrong refuses.
        result["ok"], result["note"] = True, result["note"] or "unreadable: git status failed"
        return result
    head = lines[0][3:]
    if head.startswith("HEAD (no branch)"):
        branch = None
    elif head.startswith("No commits yet on "):
        branch = head[len("No commits yet on "):]
    else:
        branch = head.split("...", 1)[0].split(" ", 1)[0]
    result["branch"] = branch
    result["dirty"] = [line[3:].split(" -> ")[-1] for line in lines[1:] if len(line) > 3]
    remote_ref = "refs/remotes/origin/%s" % default_branch
    try:
        revs = _git(root, "rev-parse", "HEAD", remote_ref, run=run)
    except (OSError, subprocess.SubprocessError) as err:
        result["ok"], result["note"] = True, "unreadable: %s" % type(err).__name__
        return result
    out = revs.stdout.split()
    result["commit"] = out[0] if out and SHA.match(out[0]) else None
    result["remote"] = out[1] if revs.returncode == 0 and len(out) > 1 and SHA.match(out[1]) else None
    if result["dirty"]:
        result["why"] = "uncommitted change to a tracked file"
    elif branch is not None and branch != default_branch:
        result["why"] = "on branch %s, not the default branch %s" % (branch, default_branch)
    elif result["commit"] is None:
        result["why"] = "no commit"
    elif result["remote"] is None:
        if branch is None:
            result["why"] = "detached head, and origin/%s is unknown so its ancestry cannot be verified" % default_branch
        else:
            result["ok"], result["note"] = True, "ancestry unverified: origin/%s is not fetched yet" % default_branch
    elif _ancestor(root, result, run):
        result["ok"] = True
    else:
        result["why"] = "%s is not an ancestor of origin/%s" % ("a detached head" if branch is None else "the commit", default_branch)
    return result


def _ancestor(root, result, run):
    """True when HEAD is an ancestor of the remote default branch; also True (with a note) when git could not answer: a failed read is no evidence."""
    try:
        done = _git(root, "merge-base", "--is-ancestor", result["commit"], result["remote"], run=run)
    except (OSError, subprocess.SubprocessError) as err:
        result["note"] = "unreadable: %s" % type(err).__name__
        return True
    if done.returncode not in (0, 1):   # 1 is the answer "no"; anything else is git failing
        result["note"] = "unreadable: git merge-base failed"
        return True
    return done.returncode == 0


def describe(root, default_branch, state):
    """The one line that names the branch, the commit and the dirty paths of a bad tree."""
    shown = state["dirty"][:SHOWN_PATHS]
    more = len(state["dirty"]) - len(shown)
    dirty = (", ".join(shown) + (" and %d more" % more if more > 0 else "")) if shown else "none"
    return ("the installed tree %s is not the default branch, clean (%s): branch %s, commit %s, dirty paths: %s; put it back on %s, clean, "
            "after you save your work, then run `fleet-switchboard install`" % (
                root, state["why"], state["branch"] or "(detached)", (state["commit"] or "unknown")[:12], dirty, default_branch))


def problem(state_dir, root, run=subprocess.run):
    """None when the guard does not apply to `root` or the tree is good; else the one line that names what is wrong."""
    marker = read_marker(state_dir)
    if marker is None or os.path.realpath(marker[0]) != os.path.realpath(str(root)):
        return None
    state = check(marker[0], marker[1], run)
    return None if state["ok"] else describe(marker[0], marker[1], state)


def note(state_dir, root, run=subprocess.run):
    """The one-line caution for a tree the guard allowed on thin evidence (the remote ref is missing), or None."""
    marker = read_marker(state_dir)
    if marker is None or os.path.realpath(marker[0]) != os.path.realpath(str(root)):
        return None
    return check(marker[0], marker[1], run)["note"]


def entry_problem_line(action, state_dir, root, run=subprocess.run):
    """What a command prints, or None to run it. Exempt commands are never refused."""
    if action in EXEMPT_COMMANDS:
        return None
    line = problem(state_dir, root, run)
    return None if line is None else "fleet-switchboard: refused: " + line


# ------------------------------------------------------------------ install

def default_branch_of(root, run=subprocess.run):
    """The remote's default branch as the clone knows it (origin/HEAD), else `main`."""
    done = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", run=run)
    name = done.stdout.strip()
    return name.split("/", 1)[1] if done.returncode == 0 and name.startswith("origin/") and len(name) > 7 else "main"


def hooks_dir(root, run=subprocess.run):
    done = _git(root, "rev-parse", "--git-common-dir", run=run)
    if done.returncode != 0 or not done.stdout.strip():
        return None
    base = Path(done.stdout.strip())
    return (base if base.is_absolute() else Path(root) / base) / "hooks"


def hook_text(state_dir, python):
    script = Path(state_dir) / SCRIPTS_DIR / SCRIPTS[0]
    return '#!/bin/sh\n%s\n"%s" "%s" "%s" "$(basename "$0")" "$@" || true\nexit 0\n' % (HOOK_MARK, python, script, state_dir)   # never fails the git command that ran it


def install(root, state_dir, kit_bin, python, dry_run=False, run=subprocess.run):
    """Record the tree, copy the two scripts out of it and write the two hooks. Returns [(step, status, detail)]; writes only under the state
    directory and the tree's .git/hooks (not a tracked path: the tree stays clean). A hook that is not ours is left alone and said so."""
    out = []
    root, state_dir = Path(os.path.realpath(str(root))), Path(state_dir)
    branch = default_branch_of(root, run)
    marker = "%s\n%s\n" % (root, branch)
    target = state_dir / MARKER
    changed = not target.is_file() or target.read_text() != marker
    if changed and not dry_run:
        state_dir.mkdir(parents=True, exist_ok=True)
        _write(target, marker, 0o600)
    out.append(("installed tree", "done" if changed else "already done", "%s on %s" % (root, branch)))
    for name in SCRIPTS:
        text = (Path(kit_bin) / name).read_text()
        dest = state_dir / SCRIPTS_DIR / name
        changed = not dest.is_file() or dest.read_text() != text
        if changed and not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            _write(dest, text, 0o700)
        out.append(("guard %s" % name[6:-3].split("_")[0], "done" if changed else "already done", str(dest)))
    directory = hooks_dir(root, run)
    for hook in HOOKS:
        if directory is None:
            out.append(("tree hook %s" % hook[5:], "skipped", "%s: no git directory" % hook))
            continue
        path = directory / hook
        text = hook_text(state_dir, python)
        if path.exists() and HOOK_MARK not in path.read_text():
            out.append(("tree hook %s" % hook[5:], "skipped", "%s exists and is not ours: left alone, so a move of HEAD by that hook is not recorded" % path))
            continue
        changed = not path.is_file() or path.read_text() != text
        if changed and not dry_run:
            directory.mkdir(parents=True, exist_ok=True)
            _write(path, text, 0o755)
        out.append(("tree hook %s" % hook[5:], "done" if changed else "already done", str(path)))
    return out


def _write(path, text, mode):
    temp = path.with_name(path.name + ".tmp%d" % os.getpid())
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(str(temp), mode)
    os.replace(str(temp), str(path))
