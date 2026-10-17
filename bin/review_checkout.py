"""review_checkout: materialise a pull request's EXACT head tree, its diff and the base copy of each touched file in one private directory,
so a reviewer that cannot run git or gh and cannot read outside its working directory can read the change (#721).

Layout of `<cwd>/.review/<head sha, 12 characters>/`: `head/` (the head tree), `base/` (the merge base's copy of each touched file), `pr.diff`
(a unified diff, built here from the two trees) and a marker file `.fleet-review-checkout` (the proof that this tool made the directory).

Hostile input is the main risk: a symlink or a submodule in the tree is never followed or created (named and skipped); a path with `..`, an
empty or `.` part, a backslash, an absolute path or a NUL is refused; every file is opened with O_EXCL and O_NOFOLLOW below a directory made
one component at a time, and the resolved path is checked to be inside the directory. A bound on file size, file count and total size refuses
with the limit and the number. A refusal removes what this call made. Nothing here reads GitHub itself: the caller passes `api(path) -> JSON` and `archive(sha) -> bytes` (the tarball of that exact commit).
The head tree comes from ONE archive call, read member by member by `read_archive` below (the archive is hostile input: it is never handed to an extracting
tool; every member name goes through bad_path, links and special files are named and skipped, every bound is checked from the member header BEFORE its bytes are
read). The marker is written FIRST, so a run that is killed or fails part-way leaves a directory `--remove` accepts.
Python 3.9+, standard library only; loaded by path from beside bin/fleet-switchboard.
"""

import base64
import difflib
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import time
import unicodedata
import zlib

MARKER = ".fleet-review-checkout"
MARKER_TOOL = "fleet-switchboard review checkout"
PARENT = ".review"
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 5000
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_DIFF_BYTES = 5 * 1024 * 1024
MAX_DIFF_LINES = 4000    # lines of one side: past it the line diff is skipped (difflib is quadratic: 4000 x 4000 fully different lines takes well under a second)
DIFF_SECONDS = 30        # the time all line diffs together may take; past it each remaining file says the diff was skipped
DEADLINE_SECONDS = 900   # the whole checkout
SHA_RE = re.compile(r"[0-9a-f]{40}")


class Unsafe(Exception):
    """A path or an input that must not be written (exit 1)."""


class OverBound(Exception):
    """A bound was passed: the text names the limit and the number (exit 2)."""


class Problem(Exception):
    """GitHub answered something unusable, or the destination is taken (exit 1)."""


def _dot_git(part):
    """Whether a path component could be the `.git` directory on some filesystem: any case, trailing dots or spaces (NTFS), zero-width and other
    invisible characters (HFS+), or the NTFS short name `git~1`."""
    folded = "".join(c for c in part if unicodedata.category(c) not in ("Cf", "Mn")).lower().rstrip(". ")
    return folded in (".git", "git~1")


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
    if any(_dot_git(p) for p in parts):
        return "a path with a .git component (a hostile .git/config would run when git is run there)"
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


def git_blob_sha(data):
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


class _Capped(io.RawIOBase):
    """A reader that stops, with OverBound, once more than `cap` decompressed bytes have been read: a gzip bomb, or headers and skipped members, cannot run on."""

    def __init__(self, inner, cap):
        self.inner, self.cap, self.seen = inner, cap, 0

    def readable(self):
        return True

    def readinto(self, buffer):
        got = self.inner.readinto(buffer)
        self.seen += got or 0
        if self.seen > self.cap:
            raise OverBound("the archive is more than %d bytes unpacked, over the limit of %d bytes in total" % (self.cap, self.cap))
        return got


def read_archive(data, root, limits, started, expected):
    """Unpack the tarball `data` (bytes) into `root` with write_file, the only writer. Members are read in a stream, never extracted by tarfile.
    The archive's single top directory prefix is stripped; a member outside it is refused. `expected` is {path: (blob sha, size, exec)} of the git TREE, the authority: a file the tree does not list is refused, and a file whose content does not hash to the tree's blob sha (`export-subst`) is not written (the caller fetches it by blob). Returns ({path: (git blob sha, size)} of what was written, skipped, refused, altered).
    Raises OverBound before a member's bytes are read when its header passes a bound."""
    files, skipped, refused, seen, altered = {}, [], [], set(), []
    members, total, top = 0, 0, None
    try:
        # unpacked here, under a byte cap that counts every byte (headers and skipped members too): total bound + room for headers and padding
        capped = io.BufferedReader(_Capped(gzip.GzipFile(fileobj=io.BytesIO(data)), limits["total"] + 1024 * (4 * limits["files"] + 16)))
        archive = tarfile.open(fileobj=capped, mode="r|")
    except (tarfile.TarError, OSError, EOFError, zlib.error) as err:
        raise Problem("GitHub answered an unreadable archive: %s" % err) from None
    try:
        for member in archive:
            if time.monotonic() - started > DEADLINE_SECONDS:
                raise OverBound("the checkout took more than %d seconds (the time limit)" % DEADLINE_SECONDS)
            members += 1
            if members > 4 * limits["files"] + 16:
                raise OverBound("the archive has more than %d members, over the limit of %d files" % (4 * limits["files"] + 16, limits["files"]))
            name = member.name
            why = bad_path(name.rstrip("/")) if name.rstrip("/") else "an empty path"
            if why:
                refused.append((name, why))
                continue
            parts = name.rstrip("/").split("/")
            if top is None:
                top = parts[0]
            if parts[0] != top:
                refused.append((name, "a member outside the archive's single top directory"))
                continue
            rel = "/".join(parts[1:])
            if member.isdir():
                continue
            if not rel:
                refused.append((name, "a file in place of the archive's top directory"))
                continue
            if member.issym() or member.islnk():
                skipped.append((rel, "a symlink" if member.issym() else "a hardlink"))
                continue
            if not member.isreg() or member.issparse():
                skipped.append((rel, "a special or sparse file"))
                continue
            if rel in seen:
                raise Unsafe("%s appears twice in the archive (duplicate member)" % _shown(rel))
            seen.add(rel)
            if len(seen) > limits["files"]:
                raise OverBound("the head tree has more than %d files, over the limit of %d files" % (limits["files"], limits["files"]))
            if member.size > limits["file"]:
                raise OverBound("%s in the head tree is %d bytes, over the limit of %d bytes for one file" % (_shown(rel), member.size, limits["file"]))
            total += member.size
            if total > limits["total"]:
                raise OverBound("the head tree is over %d bytes in all, over the limit of %d bytes in total" % (limits["total"], limits["total"]))
            if rel not in expected:
                refused.append((rel, "a file the git tree of the head does not list"))
                continue
            handle = archive.extractfile(member)
            body = handle.read(member.size + 1) if handle is not None else b""
            if len(body) != member.size:
                raise Problem("GitHub answered a truncated archive member %s" % _shown(rel))
            if git_blob_sha(body) != expected[rel][0]:
                altered.append(rel)   # export-subst or a forged archive: the blob of the tree is fetched instead
                continue
            write_file(os.path.join(root, "head"), rel, body)
            files[rel] = (expected[rel][0], len(body))
    except (tarfile.TarError, OSError, EOFError, zlib.error) as err:   # a bad gzip or tar stream, or a write that failed
        raise Problem("GitHub answered an unreadable archive: %s" % err) from None
    finally:
        archive.close()
    return files, skipped, refused, altered


def _marker_bytes(fields):
    return (json.dumps(fields, indent=1, sort_keys=True) + "\n").encode()


def _rewrite_marker(root, data):
    """Replace the content of the marker this call already made (a regular file; never created here, never followed)."""
    fd = os.open(os.path.join(root, MARKER), os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _diff_text(path, old, new, late=False):
    def text(data):
        return None if data is None else data.decode("utf-8", "replace").splitlines(keepends=True)
    if (old is not None and b"\x00" in old) or (new is not None and b"\x00" in new):
        return "Binary files a/%s and b/%s differ\n" % (path, path)
    counts = [len(d.splitlines()) for d in (old, new) if d is not None]
    if late or (counts and max(counts) > MAX_DIFF_LINES):
        return "diff skipped: too large to compare here (%d lines) for %s\n" % (max(counts) if counts else 0, path)
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


def checkout(api, repo, number, head, base, cwd, limits=None, notify=lambda text: None, archive=None):
    """Materialise PR `repo#number` at commit `head` (read ONCE by the caller; every fetch uses it) under `cwd`, diffed against merge base of
    `base`. Returns the directory path. On any failure everything this call made is removed and the exception (Unsafe, OverBound, Problem) is raised."""
    limits = dict({"file": MAX_FILE_BYTES, "files": MAX_FILES, "total": MAX_TOTAL_BYTES}, **(limits or {}))
    if not isinstance(head, str) or not SHA_RE.fullmatch(head) or not isinstance(base, str) or not SHA_RE.fullmatch(base):
        raise Problem("the head and the base must each be a full 40-character commit sha")
    cwd_real = os.path.realpath(cwd)
    parent, root = os.path.join(cwd_real, PARENT), os.path.join(cwd_real, PARENT, head[:12])
    created_parent = made_root = False
    try:
        if os.path.lexists(parent) and (os.path.islink(parent) or not os.path.isdir(parent)):
            raise Unsafe("%s exists and is not a plain directory" % PARENT)
        if not os.path.lexists(parent):
            os.mkdir(parent, 0o700)
            created_parent = True
        if os.path.lexists(root):
            raise Problem("%s exists already: remove it first (`review checkout --remove`)" % os.path.join(PARENT, head[:12]))
        os.mkdir(root, 0o700)
        made_root = True
        write_file(root, MARKER, _marker_bytes({"tool": MARKER_TOOL, "pr": "%s#%d" % (repo, number), "head": head, "base": base}))   # FIRST: a killed run leaves a directory `--remove` accepts
    except BaseException:
        if made_root:
            remove_tree(root, force=True)
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
        base_files, base_skipped, base_refused = read_tree(api, repo, merge_base, limits)
        started = time.monotonic()
        os.mkdir(os.path.join(root, "head"), 0o700)
        os.mkdir(os.path.join(root, "base"), 0o700)
        if archive is None:
            raise Problem("no archive reader was given")
        tree_files, tree_skipped, tree_refused = read_tree(api, repo, head, limits)   # the AUTHORITY on which files the head has (a tarball honours export-ignore and export-subst)
        check_bounds(tree_files, limits, "head")
        archived, skipped, refused, altered = read_archive(archive(head), root, limits, started, tree_files)   # ONE call: the exact head sha
        skipped = tree_skipped + [s_ for s_ in skipped if s_[0] not in {p for p, _ in tree_skipped}]
        refused = tree_refused + refused + [(p + " (base)", w) for p, w in base_refused]
        if refused:
            raise Unsafe("refused, nothing kept: " + "; ".join("%s: %s" % (_shown(p), w) for p, w in refused[:20])
                         + ("; skipped (never followed): " + ", ".join(_shown(p) for p, _ in skipped[:20]) if skipped else ""))
        head_files, from_blob = dict(archived), sorted(set(tree_files) - set(archived))   # the tree files the archive left out or altered: fetched by blob, never silently fewer
        fetched = [0]
        for path in from_blob:
            if time.monotonic() - started > DEADLINE_SECONDS:
                raise OverBound("the checkout took more than %d seconds (the time limit)" % DEADLINE_SECONDS)
            data = fetch_blob(api, repo, tree_files[path][0], limits)
            fetched[0] += len(data)
            if fetched[0] > limits["total"]:
                raise OverBound("the blobs fetched are %d bytes in all, over the limit of %d bytes in total" % (fetched[0], limits["total"]))
            if git_blob_sha(data) != tree_files[path][0]:
                raise Problem("the blob GitHub answered for %s is not the tree's blob" % _shown(path))
            write_file(os.path.join(root, "head"), path, data)
            head_files[path] = (tree_files[path][0], len(data))
        if from_blob:
            notify("%d of %d files were not in the archive as the tree has them (export-ignore or export-subst) and came from the blob API: %s"
                   % (len(from_blob), len(tree_files), ", ".join(_shown(p) for p in from_blob[:20])))
        touched = sorted(p for p in set(head_files) | set(base_files)
                         if (head_files.get(p) or (0,))[0] != (base_files.get(p) or (0,))[0])
        check_bounds({p: base_files[p] for p in touched if p in base_files}, limits, "base")
        total = sum(v[1] for v in head_files.values()) + sum(base_files[p][1] for p in touched if p in base_files)
        if total > limits["total"]:
            raise OverBound("the head and base copies are %d bytes in all, over the limit of %d bytes in total" % (total, limits["total"]))
        fetched = [0]

        def base_blob(entry):
            if time.monotonic() - started > DEADLINE_SECONDS:
                raise OverBound("the checkout took more than %d seconds (the time limit)" % DEADLINE_SECONDS)
            data = fetch_blob(api, repo, entry[0], limits)
            fetched[0] += len(data)
            if fetched[0] > limits["total"]:
                raise OverBound("the blobs fetched are %d bytes in all, over the limit of %d bytes in total" % (fetched[0], limits["total"]))
            return data

        def head_text(path):
            fd = os.open(os.path.join(root, "head", *path.split("/")), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                return handle.read(limits["file"] + 1)
        diff, written, diff_started = [], 0, time.monotonic()
        for path in touched:
            if time.monotonic() - started > DEADLINE_SECONDS:
                raise OverBound("the checkout took more than %d seconds (the time limit)" % DEADLINE_SECONDS)
            old = base_blob(base_files[path]) if path in base_files else None
            new = head_text(path) if path in head_files else None
            if old is not None:
                write_file(os.path.join(root, "base"), path, old)
            piece = _diff_text(path, old, new, late=time.monotonic() - diff_started > DIFF_SECONDS)
            written += len(piece.encode())
            if written > MAX_DIFF_BYTES:
                raise OverBound("the diff is over the limit of %d bytes" % MAX_DIFF_BYTES)
            diff.append(piece)
        write_file(root, "pr.diff", "".join(diff).encode())
        _rewrite_marker(root, _marker_bytes({"tool": MARKER_TOOL, "pr": "%s#%d" % (repo, number), "head": head, "base": base,
                                             "merge_base": merge_base, "merge_base_fallback": fallback, "touched": len(touched),
                                             "from_blob_api": from_blob, "altered_in_archive": sorted(altered)}))
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
