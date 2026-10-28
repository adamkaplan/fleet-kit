"""The caller's own scratch directory under the state directory (ask #1456): `<state>/scratch/<agent>`, private (0700), kept by every fleet command.

The system temp area is swept by the system; the state directory is the fleet's own. A helper script or a rule text kept in a temp directory vanished between
turns and had to be recreated by hand. This module holds the rules of the scratch directory and nothing else: it makes the directory, refuses an unsafe one, and
measures it against a 100 MB bound that only WARNS. It never deletes, empties, moves or refuses anything because of a size. Pure of the switchboard."""

import os
import re
import stat
import time

BOUND_BYTES = 100 * 1024 * 1024
BOUND_TEXT = "100 MB"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")   # herdr's own pattern for an agent name (the entry's NAME_RE)
DAY_FILE = "scratch-bound-days.txt"   # <state>/: one line "<agent> <UTC day>" for each directory over the bound: the day its audit line was last recorded


class Refused(Exception):
    """An unsafe or unusable scratch directory: one line, nothing was made or followed."""


def check_name(name):
    if not isinstance(name, str) or not name:
        raise Refused("no scratch directory: the caller has no agent name")
    if "/" in name or os.sep in name or "\\" in name or "\0" in name:
        raise Refused("no scratch directory for %r: an agent name holds a path separator" % name)
    if name.startswith("."):
        raise Refused("no scratch directory for %r: an agent name starts with a dot" % name)
    if not NAME_RE.fullmatch(name):
        raise Refused("no scratch directory for %r: not a valid agent name" % name)


def _private_dir(path):
    """Make `path` (one level) private if it is absent; refuse a symlink, a non-directory or one owned by another user. Never follows a link."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as err:
        raise Refused("the scratch directory %s cannot be made: %s" % (path, err.strerror or err)) from None
    try:
        info = os.lstat(path)
    except OSError as err:
        raise Refused("the scratch directory %s cannot be read: %s" % (path, err.strerror or err)) from None
    if stat.S_ISLNK(info.st_mode):
        raise Refused("the scratch directory %s is a symlink: refused, not followed" % path)
    if not stat.S_ISDIR(info.st_mode):
        raise Refused("%s is not a directory: refused" % path)
    if info.st_uid != os.getuid():
        raise Refused("the scratch directory %s is owned by another user: refused" % path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)   # no link swapped in after the check
    except OSError as err:
        raise Refused("the scratch directory %s cannot be opened without following a link: %s" % (path, err.strerror or err)) from None
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def own_dir(state_dir, name):
    """The path of agent `name`'s scratch directory, made (0700) when absent. Raises Refused. The name is the caller's own, never an argument."""
    check_name(name)
    if state_dir is None:
        raise Refused("no state directory: no scratch directory can be made")
    base = os.path.join(str(state_dir), "scratch")
    _private_dir(base)
    path = os.path.join(base, name)
    _private_dir(path)
    return path


def size_of(path):
    """Bytes of the files under `path` (apparent size, no link followed). Reads only."""
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(entry.path)
                    else:
                        total += info.st_size
        except OSError:
            continue
    return total


def warning(path):
    """One line when `path` holds more than the bound, else None. It only says; nothing is removed or refused."""
    size = size_of(path)
    if size <= BOUND_BYTES:
        return None
    return "scratch: %s holds %d MB, over the %s bound (nothing is removed: clear it by hand)" % (path, size // (1024 * 1024), BOUND_TEXT)


def over_bound(state_dir):
    """[(agent, bytes)] for every scratch directory over the bound. Reads only."""
    found = []
    base = os.path.join(str(state_dir), "scratch")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return found
    for name in names:
        path = os.path.join(base, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        size = size_of(path)
        if size > BOUND_BYTES:
            found.append((name, size))
    return found


def due_today(state_dir, now):
    """[(agent, bytes)] over the bound that have NOT been reported on this UTC day; the day is recorded for each, so a second pass the same day returns none.
    Writes only <state>/scratch-bound-days.txt, never anything under scratch/."""
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    path = os.path.join(str(state_dir), DAY_FILE)
    seen = {}
    try:
        with open(path) as handle:
            for line in handle.read(100000).splitlines():
                agent, _, stamp = line.partition(" ")
                if agent and stamp:
                    seen[agent] = stamp
    except OSError:
        pass
    over = over_bound(state_dir)
    fresh = [(name, size) for name, size in over if seen.get(name) != day]
    kept = {name: seen[name] for name, _ in over if name in seen}   # an agent back under the bound is forgotten: a later excess is reported again
    kept.update({name: day for name, _ in fresh})
    if kept != seen:
        temp = "%s.%d.tmp" % (path, os.getpid())
        try:
            with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as handle:
                handle.write("".join("%s %s\n" % (name, stamp) for name, stamp in sorted(kept.items())))
            os.replace(temp, path)
        except OSError:
            return []   # not recorded: say nothing now rather than say it every pass
    return fresh


LOOK_SECONDS = 600   # the daemon looks at most this often


def daily_look(engine):
    """The daemon's look (called from a pass, only in the real daemon): one `scratch.over_bound` audit line a day for each scratch directory that stays over the
    bound. It only says: nothing is removed or refused. Never raises."""
    try:
        now = engine.now()
        last = getattr(engine, "scratch_last", None)
        if last is not None and now - last < LOOK_SECONDS:
            return
        engine.scratch_last = now
        for agent, size in due_today(engine.state_dir, now):
            if engine.audit is not None:
                engine.audit.append(engine.actor, "scratch.over_bound", agent, {"bytes": size, "bound": BOUND_BYTES})
    except Exception as err:   # a look never stops the daemon
        try:
            engine._note("scratch.bound_check_error", None, {"error": ("%s: %s" % (type(err).__name__, err))[:160]})
        except Exception:
            pass
