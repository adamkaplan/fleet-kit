"""The one way the switchboard makes a directory (#708).

Every site that makes a directory used to do it its own way: look first and create after, or `mkdir(exist_ok=True)`.
Each decided differently what a dangling symlink, a race with another process, the owner and the mode mean. This module is the one place that
decides, so a new site calls `ensure_dir` and cannot bring the old failures back (a test fails on any other `mkdir(..., exist_ok=...)`).

Loaded by path from beside the caller (`load_module("private_dir")` in the daemon, a small loader in each sibling module), never imported by name,
so a module never imports the daemon. Standard library only.
"""
import os
from pathlib import Path


class PrivateDirError(OSError):
    """A directory that cannot be made or is not acceptable. One line, naming the path. `kind` says which refusal:
    `dangling` (a symlink to nothing), `not-directory`, or `loose` (not owned by the user, or the group or world can read it)."""

    def __init__(self, message, kind):
        super().__init__(message)
        self.kind = kind


def _dangling(path):
    """The first part of `path` that exists as an entry, if it is a symlink to nothing; else None. Nothing above an existing entry matters."""
    for part in (path,) + tuple(path.parents):
        if os.path.lexists(str(part)):
            return part if os.path.islink(str(part)) and not os.path.exists(str(part)) else None
    return None


def chmod_private(path, mode=0o700):
    """Set `mode` on the directory `path` without following a link: the mode goes on a descriptor opened with O_NOFOLLOW, so a link a same-user
    process swapped in since the caller's mkdir or check is refused (OSError, ELOOP) and its target is never changed. Every site that sets a directory's mode calls this."""
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def ensure_dir(path, *, private=True, parents=True, check=True):
    """Make the directory `path` (and its parents, when `parents`) and return it as a Path.

    Another process making it at the same moment is not an error: it exists, and is checked like any existing one. A symlink to nothing, at
    the path or at a parent that would have to be made, is refused with `PrivateDirError` (kind `dangling`) and nothing is created at its target;
    a symlink that resolves to a directory is accepted. With `private` the directory the call makes gets mode 0700 whatever the umask, and
    (unless `check` is False, for a caller that checks and repairs by its own rule) an existing one must be a directory owned by the user with no
    group or world bits, or `PrivateDirError` (kind `not-directory` or `loose`) says so. Without `private` the mode is the user's (install and
    config directories) and nothing about owner or mode is checked. Only the directory the call itself makes is forced to 0700; parents it has to make follow the umask. A link swapped in between the mkdir and the mode being set is refused (the mode is set on a descriptor opened without following a link), so the link's target is never changed. Any other OSError (no space, a file in the way of a parent) is raised as it is.
    """
    path = Path(path)
    gone = _dangling(path)
    if gone is not None:
        raise PrivateDirError("%s is a symbolic link to nothing: not following it (remove it or point it at a directory)" % gone, "dangling")
    created = False
    if parents and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)   # the one bare-call exception to the scan in test-switchboard: another process may make a parent first
    try:
        os.mkdir(str(path), 0o700 if private else 0o777)
        created = True
    except FileExistsError:
        pass   # another process made it (or it was there): checked below like any existing one
    if created and private:
        chmod_private(path)   # mkdir's mode is filtered by the umask; only the process that made it sets it
    gone = _dangling(path)
    if gone is not None:
        raise PrivateDirError("%s is a symbolic link to nothing: not following it (remove it or point it at a directory)" % gone, "dangling")
    if private and check:
        info = os.stat(str(path))
        if not os.path.isdir(str(path)):
            raise PrivateDirError("state path is not a directory: %s" % path, "not-directory")
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PrivateDirError("state directory must be private (mode 0700, owned by you): %s" % path, "loose")
    elif not os.path.isdir(str(path)):
        raise PrivateDirError("not a directory: %s" % path, "not-directory")
    return path
