"""review_checkout: materialise a pull request's EXACT head tree, its diff and the base copy of each touched file in one private directory,
so a reviewer that cannot run git or gh and cannot read outside its working directory can read the change (#721).

Layout of `<cwd>/.review/<head sha, 12 characters>/`: `head/` (the head tree), `base/` (the merge base's copy of each touched file), `pr.diff`
(a unified diff, built here from the two trees) and a marker file `.fleet-review-checkout` (the proof that this tool made the directory).

Hostile input is the main risk: a symlink or a submodule in the tree is never followed or created (named and skipped); a path with `..`, an
empty or `.` part, a backslash, an absolute path or a NUL is refused; every file is opened with O_EXCL and O_NOFOLLOW below a directory made
one component at a time, and the resolved path is checked to be inside the directory. A bound on file size, file count and total size refuses
with the limit and the number. A refusal removes what this call made. Nothing here reads GitHub itself: the caller passes `api(path) -> JSON`.
Python 3.9+, standard library only; loaded by path from beside bin/fleet-switchboard.
"""

import base64
import difflib
import json
import os
import re
import shutil
import stat

MARKER = ".fleet-review-checkout"
MARKER_TOOL = "fleet-switchboard review checkout"
PARENT = ".review"
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 5000
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_DIFF_BYTES = 5 * 1024 * 1024
SHA_RE = re.compile(r"[0-9a-f]{40}")


class Unsafe(Exception):
    """A path or an input that must not be written (exit 1)."""


class OverBound(Exception):
    """A bound was passed: the text names the limit and the number (exit 2)."""


class Problem(Exception):
    """GitHub answered something unusable, or the destination is taken (exit 1)."""


def bad_path(path):
    """Why `path` may not be written below the directory, or None."""
    if not isinstance(path, str) or not path:
        return "an empty path"
    if "\x00" in path:
        return "a path with a NUL"
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return "an absolute path"
    if "\\" in path:
        return "a path with a backslash"
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "a path with an empty, '.' or '..' part"
    return None


def _shown(path):
    return repr(path.replace("\x00", "\\x00"))[:120]


def read_tree(api, repo, sha, limits):
    """{path: (blob sha, size)} of the files of the tree at commit `sha`, plus the list of skipped (symlinks, submodules) and the list of refused
    (path, why). The tree comes in one read: a truncated answer is over the count bound."""
    got = api("repos/%s/git/trees/%s?recursive=1" % (repo, sha))
    if not isinstance(got, dict) or not isinstance(got.get("tree"), list):
        raise Problem("GitHub answered no tree for %s" % sha[:12])
    if got.get("truncated"):
        raise OverBound("the tree of %s is too large for GitHub to list in one answer (the file count limit is %d)" % (sha[:12], limits["files"]))
    files, skipped, refused = {}, [], []
    for entry in got["tree"]:
        if not isinstance(entry, dict) or entry.get("type") == "tree":
            continue
        path, mode = entry.get("path"), str(entry.get("mode"))
        why = bad_path(path)
        if why:
            refused.append((path if isinstance(path, str) else "?", why))
        elif entry.get("type") != "blob" or mode in ("120000", "160000"):
            skipped.append((path, "a symlink" if mode == "120000" else "a submodule or other non-file"))
        elif not isinstance(entry.get("sha"), str) or not SHA_RE.fullmatch(entry["sha"]) or not isinstance(entry.get("size"), int):
            refused.append((path, "an entry with no blob sha or size"))
        else:
            files[path] = (entry["sha"], entry["size"], mode == "100755")
    return files, skipped, refused


def check_bounds(files, limits, what):
    if len(files) > limits["files"]:
        raise OverBound("the %s tree has %d files, over the limit of %d files" % (what, len(files), limits["files"]))
    for path, (_, size, _x) in sorted(files.items()):
        if size > limits["file"]:
            raise OverBound("%s in the %s tree is %d bytes, over the limit of %d bytes for one file" % (_shown(path), what, size, limits["file"]))
    total = sum(v[1] for v in files.values())
    if total > limits["total"]:
        raise OverBound("the %s tree is %d bytes in all, over the limit of %d bytes in total" % (what, total, limits["total"]))


def fetch_blob(api, repo, sha, limits):
    got = api("repos/%s/git/blobs/%s" % (repo, sha))
    if not isinstance(got, dict) or got.get("encoding") != "base64" or not isinstance(got.get("content"), str):
        raise Problem("GitHub answered an unusable blob %s" % sha[:12])
    try:
        data = base64.b64decode(got["content"])
    except ValueError:
        raise Problem("GitHub answered an undecodable blob %s" % sha[:12]) from None
    if len(data) > limits["file"]:
        raise OverBound("a blob is %d bytes, over the limit of %d bytes for one file" % (len(data), limits["file"]))
    return data


def _make_dirs(root, rel_parts, made):
    """Make each directory of `rel_parts` below `root` one component at a time; a component that exists must be a real directory (never a symlink)."""
    here = root
    for part in rel_parts:
        here = os.path.join(here, part)
        try:
            info = os.lstat(here)
        except FileNotFoundError:
            try:
                os.mkdir(here, 0o700)
            except OSError as err:
                raise Unsafe("%s cannot be created: %s" % (_shown(here), err.strerror or err)) from None
            made.append(here)
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise Unsafe("%s is in the way: not a directory" % _shown(here))
    return here


def _inside(root, path):
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    return real == real_root or real.startswith(real_root + os.sep)


def write_file(root, rel, data):
    """Write `data` at `rel` below `root`; never overwrite, never follow a link, always check the resolved path is inside."""
    why = bad_path(rel)
    if why:
        raise Unsafe("%s: %s" % (_shown(rel), why))
    parts = rel.split("/")
    made = []
    parent = _make_dirs(root, parts[:-1], made)
    target = os.path.join(parent, parts[-1])
    if not _inside(root, parent) or not _inside(root, os.path.dirname(os.path.realpath(target))):
        raise Unsafe("%s resolves outside the directory" % _shown(rel))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        raise Unsafe("%s exists already (two paths collide)" % _shown(rel)) from None
    except OSError as err:
        raise Unsafe("%s cannot be created: %s" % (_shown(rel), err.strerror or err)) from None
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _diff_text(path, old, new):
    def text(data):
        return None if data is None else data.decode("utf-8", "replace").splitlines(keepends=True)
    if (old is not None and b"\x00" in old) or (new is not None and b"\x00" in new):
        return "Binary files a/%s and b/%s differ\n" % (path, path)
    lines = list(difflib.unified_diff(text(old) or [], text(new) or [], "a/" + path if old is not None else "/dev/null",
                                      "b/" + path if new is not None else "/dev/null"))
    out = "".join(l if l.endswith("\n") else l + "\n\\ No newline at end of file\n" for l in lines)
    return out


def make_readonly(root):
    """Bottom-up: files 0444 (the executable bit is not kept: a reviewer reads), directories 0555, the root last."""
    for current, dirs, names in os.walk(root, topdown=False, followlinks=False):
        for name in names:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                os.chmod(path, 0o444)
        for name in dirs:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                os.chmod(path, 0o555)
    os.chmod(root, 0o555)


def checkout(api, repo, number, head, base, cwd, limits=None, notify=lambda text: None):
    """Materialise PR `repo#number` at commit `head` (read ONCE by the caller; every fetch uses it) under `cwd`, diffed against merge base of
    `base`. Returns the directory path. On any failure everything this call made is removed and the exception (Unsafe, OverBound, Problem) is raised."""
    limits = dict({"file": MAX_FILE_BYTES, "files": MAX_FILES, "total": MAX_TOTAL_BYTES}, **(limits or {}))
    if not isinstance(head, str) or not SHA_RE.fullmatch(head) or not isinstance(base, str) or not SHA_RE.fullmatch(base):
        raise Problem("the head and the base must each be a full 40-character commit sha")
    cwd_real = os.path.realpath(cwd)
    parent, root = os.path.join(cwd_real, PARENT), os.path.join(cwd_real, PARENT, head[:12])
    created_parent = False
    try:
        if os.path.lexists(parent) and (os.path.islink(parent) or not os.path.isdir(parent)):
            raise Unsafe("%s exists and is not a plain directory" % PARENT)
        if not os.path.lexists(parent):
            os.mkdir(parent, 0o700)
            created_parent = True
        if os.path.lexists(root):
            raise Problem("%s exists already: remove it first (`review checkout --remove`)" % os.path.join(PARENT, head[:12]))
        os.mkdir(root, 0o700)
    except BaseException:
        if created_parent:
            shutil.rmtree(parent, ignore_errors=True)
        raise
    try:
        merge_base, fallback = base, False
        try:
            cmp = api("repos/%s/compare/%s...%s?per_page=1" % (repo, base, head))
            found = ((cmp or {}).get("merge_base_commit") or {}).get("sha") if isinstance(cmp, dict) else None
            if isinstance(found, str) and SHA_RE.fullmatch(found):
                merge_base = found
            if merge_base == base and found != base:
                fallback = True
        except Problem as err:   # the base copy then comes from the base commit itself, said aloud and recorded in the marker
            fallback = True
            notify("the merge base could not be read (%s): the base copies come from the base commit %s" % (err, base[:12]))
        head_files, skipped, refused = read_tree(api, repo, head, limits)
        base_files, base_skipped, base_refused = read_tree(api, repo, merge_base, limits)
        refused = refused + [(p + " (base)", w) for p, w in base_refused]
        if refused:
            raise Unsafe("refused, nothing written: " + "; ".join("%s: %s" % (_shown(p), w) for p, w in refused[:20])
                         + ("; skipped (never followed): " + ", ".join(_shown(p) for p, _ in skipped[:20]) if skipped else ""))
        touched = sorted(p for p in set(head_files) | set(base_files)
                         if (head_files.get(p) or (0,))[0] != (base_files.get(p) or (0,))[0])
        check_bounds(head_files, limits, "head")
        check_bounds({p: base_files[p] for p in touched if p in base_files}, limits, "base")
        total = sum(v[1] for v in head_files.values()) + sum(base_files[p][1] for p in touched if p in base_files)
        if total > limits["total"]:
            raise OverBound("the head and base copies are %d bytes in all, over the limit of %d bytes in total" % (total, limits["total"]))
        cache, fetched = {}, [0]

        def blob(entry):
            if entry[0] not in cache:
                cache[entry[0]] = fetch_blob(api, repo, entry[0], limits)
                fetched[0] += len(cache[entry[0]])
                if fetched[0] > limits["total"]:
                    raise OverBound("the blobs fetched are %d bytes in all, over the limit of %d bytes in total" % (fetched[0], limits["total"]))
            return cache[entry[0]]

        os.mkdir(os.path.join(root, "head"), 0o700)
        os.mkdir(os.path.join(root, "base"), 0o700)
        for path in sorted(head_files):
            write_file(os.path.join(root, "head"), path, blob(head_files[path]))
        diff, written = [], 0
        for path in touched:
            old = blob(base_files[path]) if path in base_files else None
            new = blob(head_files[path]) if path in head_files else None
            if old is not None:
                write_file(os.path.join(root, "base"), path, old)
            piece = _diff_text(path, old, new)
            written += len(piece.encode())
            if written > MAX_DIFF_BYTES:
                raise OverBound("the diff is over the limit of %d bytes" % MAX_DIFF_BYTES)
            diff.append(piece)
        write_file(root, "pr.diff", "".join(diff).encode())
        write_file(root, MARKER, (json.dumps({"tool": MARKER_TOOL, "pr": "%s#%d" % (repo, number), "head": head, "base": base,
                                              "merge_base": merge_base, "merge_base_fallback": fallback, "touched": len(touched)}, indent=1, sort_keys=True) + "\n").encode())
        for path, why in skipped:
            notify("skipped %s: %s (never followed or created)" % (_shown(path), why))
        make_readonly(root)
        return root
    except BaseException:
        remove_tree(root, force=True)
        if created_parent:
            try:
                os.rmdir(parent)
            except OSError:
                pass
        raise


def remove_tree(path, force=False):
    """Restore the permissions this tool set (directories to 0700, top down), then remove files and directories. Never follows a symlink."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    try:
        entries = list(os.scandir(path))
    except OSError:
        entries = []
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                remove_tree(entry.path, force)
            else:
                os.unlink(entry.path)
        except OSError:
            if not force:
                raise
    try:
        os.rmdir(path)
    except OSError:
        if not force:
            raise


def remove(directory, cwd):
    """Remove `directory` if and only if this tool made it: a real directory (not a link) directly under a `.review` directory inside `cwd`, holding
    a marker this tool wrote. Raises Unsafe with the reason otherwise and leaves it untouched."""
    cwd_real = os.path.realpath(cwd)
    if not isinstance(directory, str) or "\x00" in directory or not directory:
        raise Unsafe("not a directory path")
    absolute = directory if os.path.isabs(directory) else os.path.join(cwd_real, directory)
    absolute = os.path.normpath(absolute)
    if os.path.islink(absolute) or not os.path.isdir(absolute):
        raise Unsafe("%s is not a directory this command created (it is missing, or a link)" % _shown(directory))
    if os.path.dirname(absolute) != os.path.join(cwd_real, PARENT) or os.path.realpath(absolute) != absolute:
        raise Unsafe("%s is not a directory this command created (it is not directly under %s of the working directory)" % (_shown(directory), PARENT))
    marker = os.path.join(absolute, MARKER)
    try:
        info = os.lstat(marker)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a file")
        with open(marker, encoding="utf-8") as handle:
            data = json.loads(handle.read(65536))
        if not isinstance(data, dict) or data.get("tool") != MARKER_TOOL or not isinstance(data.get("head"), str) or data["head"][:12] != os.path.basename(absolute):
            raise ValueError("not our marker")
    except (OSError, ValueError, AttributeError):
        raise Unsafe("%s has no marker of this command: it was not created by it, so it is left alone" % _shown(directory)) from None
    remove_tree(absolute)
    try:
        os.rmdir(os.path.dirname(absolute))   # the .review directory, when nothing else is in it
    except OSError:
        pass
    return absolute
