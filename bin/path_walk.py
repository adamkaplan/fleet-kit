"""One walk up from a trusted path, shared by the cloud launcher's vault check and the installer's reaper service (one copy, stdlib only).

Lives in the kit's bin/ because the installer runs from there and so already trusts it; the launcher directory is what the walk judges, so the
walk must not be loaded from it. Never follows a link: every entry is judged with lstat."""
import os
import stat


def entry_problem(found, what):
    """One lstat result judged: not a link, owned by the user, not group- or world-writable. Returns a reason or None."""
    if stat.S_ISLNK(found.st_mode):
        return "has %s that is a symlink" % what
    if found.st_uid != os.getuid():
        return "has %s that is not owned by the user" % what
    if found.st_mode & 0o022:
        return "has %s that is group- or world-writable" % what
    return None


def parents_problem(path):
    """THE walk up from a trusted path (an absolute one): every parent from / down is judged with lstat, never followed: no symlink, owned by root or
    the user, and not group- or world-writable unless it carries the sticky bit. A reason (never the path) or None. Every trusted path of the vault
    check (the checkout and the lock's directory) goes through this one function."""
    parts = [x for x in path.split("/") if x]
    here = "/"
    for part in [""] + parts[:-1]:
        here = os.path.join(here, part) if part else "/"
        try:
            found = os.lstat(here)
        except OSError:
            return "has a parent that cannot be read"
        if stat.S_ISLNK(found.st_mode):
            # A link the OS itself provides (root-owned, e.g. macOS /var -> private/var) is followed, and its target is judged by this same walk.
            # A link owned by anyone else (the user or an attacker) is refused, as before.
            if found.st_uid != 0:
                return "has a parent that is a symlink"
            target = os.path.realpath(here)
            if target == here or target == "/":
                return "has a parent that is a symlink"
            inner = parents_problem(target + "/x")
            if inner:
                return inner
            try:
                found = os.stat(target)
            except OSError:
                return "has a parent that cannot be read"
        if not stat.S_ISDIR(found.st_mode):
            return "has a parent that is not a directory"
        if found.st_uid not in (0, os.getuid()):
            return "has a parent that is owned by another user"
        if found.st_mode & 0o022 and not found.st_mode & stat.S_ISVTX:
            return "has a parent that is group- or world-writable"
    return None
