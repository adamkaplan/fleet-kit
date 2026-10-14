"""The temp-directory janitor (ask #321). Pure of the switchboard: the caller hands it the directories, the clock, the settings and an audit
function. It looks at the TOP-LEVEL entries of the temp directory only, and an entry is removed (as a unit) when ALL of these hold:

  - it is older than `days`: the newest modification time of anything inside it (a bounded walk; a tree too big to judge is kept);
  - the fleet's own records do not name it: its full path, or its basename when that is at least six characters, does not occur in any state
    file of the state directory (`*.json` and `*.jsonl`, never the compressed archives; a big file is read by its tail);
  - it does not match a keep-list glob of the settings, and it does not hold a `.fleet-keep` marker at its top;
  - it belongs to the user running this, and it is not the temp directory itself.

A symlink is removed as a link and never followed; a directory is removed without following any link inside it. Nothing here touches a path
outside the temp directory. A dry run (the default) removes nothing. What is NOT seen: a path written only in a GitHub issue or in an agent's
own memory: use the keep-list or a `.fleet-keep` marker for that."""

import fnmatch
import os
import shutil
import stat
import time
from pathlib import Path

DEFAULTS = {"enabled": True, "days": 7, "keep": [], "dir": None}
KEEP_MARKER = ".fleet-keep"
WALK_ENTRIES = 20000          # files one entry may hold and still be judged
WALK_SECONDS = 5.0            # the whole scan
REMOVE_MAX = 500              # removals per call
STATE_FILE_BYTES = 2000000    # of one state file read (its tail)
STATE_TOTAL_BYTES = 16000000
MIN_NAME = 6                  # a basename shorter than this is too common to count as naming something


def settings(raw):
    merged = dict(DEFAULTS)
    merged.update(raw or {})
    return merged


def validate(raw, source="config"):
    """Raises ValueError with a message when `temp_janitor` is not {"enabled": bool, "days": 1..365, "keep": [glob, ...], "dir": absolute path}."""
    if raw is None:
        return
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("%s: 'temp_janitor' must be an object with only enabled, days, keep and dir, got %r" % (source, raw))
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        raise ValueError("%s: temp_janitor.enabled must be true or false" % source)
    days = raw.get("days", DEFAULTS["days"])
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        raise ValueError("%s: temp_janitor.days must be a whole number from 1 to 365, got %r" % (source, days))
    keep = raw.get("keep", [])
    if not isinstance(keep, list) or any(not isinstance(k, str) or not k.strip() or "\x00" in k for k in keep) or len(keep) > 200:
        raise ValueError("%s: temp_janitor.keep must be a list of at most 200 non-empty glob strings" % source)
    where = raw.get("dir")
    if where is not None and (not isinstance(where, str) or not os.path.isabs(where)):
        raise ValueError("%s: temp_janitor.dir must be an absolute path" % source)


def default_dir():
    import tempfile
    return Path(tempfile.gettempdir()) / "opencode"


def safe_dir(path):
    """(ok, why): the directory the janitor may look in. Never a link, never one that is not the user's own, never a root-like path."""
    try:
        info = os.lstat(str(path))
    except OSError as err:
        return False, "cannot read %s (%s)" % (path, err.strerror or err)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False, "%s is not a plain directory (a link or a file is refused)" % path
    if info.st_uid != os.getuid():
        return False, "%s is owned by another user" % path
    real = os.path.realpath(str(path))
    if len(Path(real).parts) < 3 or real == os.path.realpath(os.path.expanduser("~")):
        return False, "%s is too close to the root or the home directory to clean" % path
    return True, ""


def named_text(state_dir):
    """The text of the fleet's own records: what a path must not occur in. Bounded; unreadable files are skipped."""
    parts, total = [], 0
    try:
        names = sorted(os.listdir(str(state_dir)))
    except OSError:
        return ""
    for name in names:
        if not (name.endswith(".json") or name.endswith(".jsonl")):
            continue
        path = os.path.join(str(state_dir), name)
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                continue
            with open(path, "rb") as handle:
                if info.st_size > STATE_FILE_BYTES:
                    handle.seek(info.st_size - STATE_FILE_BYTES)
                data = handle.read(STATE_FILE_BYTES).decode("utf-8", "replace")
        except OSError:
            continue
        total += len(data)
        parts.append(data)
        if total > STATE_TOTAL_BYTES:
            break
    return "\n".join(parts)


def _newest(path, deadline):
    """(newest mtime under path, complete): a bounded walk that never follows a link."""
    try:
        info = os.lstat(path)
    except OSError:
        return None, False
    newest, seen, pending = info.st_mtime, 0, []
    if stat.S_ISDIR(info.st_mode):
        pending.append(path)
    while pending:
        try:
            scan = os.scandir(pending.pop())
        except OSError:
            continue
        with scan:
            for entry in scan:
                seen += 1
                if seen > WALK_ENTRIES or time.monotonic() > deadline:
                    return newest, False
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                newest = max(newest, st.st_mtime)
                if stat.S_ISDIR(st.st_mode):
                    pending.append(entry.path)
    return newest, True


def _size(path):
    try:
        info = os.lstat(path)
    except OSError:
        return 0
    total = info.st_size
    if stat.S_ISDIR(info.st_mode):
        stack, seen = [path], 0
        while stack and seen <= WALK_ENTRIES:
            try:
                scan = os.scandir(stack.pop())
            except OSError:
                continue
            with scan:
                for entry in scan:
                    seen += 1
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += st.st_size
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
    return total


def scan(temp_dir, state_dir, now, cfg, uid=None, clock=time.monotonic):
    """[{"name", "path", "action": "remove" | "keep", "why", "size", "age_days"}] for each top-level entry, sorted by name. Reads only."""
    uid = os.getuid() if uid is None else uid
    cutoff = now - cfg["days"] * 86400
    text = named_text(state_dir)
    keep = list(cfg.get("keep") or [])
    deadline = clock() + WALK_SECONDS
    rows = []
    try:
        names = sorted(os.listdir(str(temp_dir)))
    except OSError:
        return rows
    for name in names:
        path = os.path.join(str(temp_dir), name)
        row = {"name": name, "path": path, "action": "keep", "why": "", "size": 0, "age_days": 0}
        rows.append(row)
        try:
            info = os.lstat(path)
        except OSError:
            row["why"] = "vanished"
            continue
        if info.st_uid != uid:
            row["why"] = "owned by another user"
            continue
        if any(fnmatch.fnmatchcase(name, g) or fnmatch.fnmatchcase(path, g) for g in keep):
            row["why"] = "matches a keep glob"
            continue
        if stat.S_ISDIR(info.st_mode) and os.path.lexists(os.path.join(path, KEEP_MARKER)):
            row["why"] = "holds a %s marker" % KEEP_MARKER
            continue
        newest, complete = _newest(path, deadline)
        if newest is None:
            row["why"] = "cannot be read"
            continue
        row["age_days"] = int((now - newest) / 86400)
        if not complete:
            row["why"] = "too many files (or too slow) to judge: kept"
            continue
        if newest > cutoff:
            row["why"] = "newer than %d day(s)" % cfg["days"]
            continue
        if path in text or (len(name) >= MIN_NAME and name in text):
            row["why"] = "named by the fleet's records"
            continue
        row["action"], row["size"] = "remove", _size(path)
    return rows


def remove_entry(path):
    """Remove one top-level entry: a link or a file by unlink, a directory without following any link inside it. Raises OSError."""
    info = os.lstat(path)
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        errors = []
        shutil.rmtree(path, onerror=lambda func, p, exc: errors.append((p, exc[1])))
        if errors:
            raise OSError("could not remove %s: %s" % (errors[0][0], errors[0][1]))
    else:
        os.unlink(path)


def run(temp_dir, state_dir, now, cfg, apply, audit, uid=None):
    """Returns (lines, removed, kept). `audit(name, detail)` is called once for each removal that happened."""
    lines, removed, kept = [], 0, 0
    ok, why = safe_dir(temp_dir)
    if not ok:
        return ["refused: %s" % why], 0, 0
    rows = scan(temp_dir, state_dir, now, cfg, uid)
    for row in rows:
        if row["action"] != "remove":
            kept += 1
            continue
        if removed >= REMOVE_MAX:
            lines.append("only %d entries handled per call: run it again for the rest" % REMOVE_MAX)
            break
        if not apply:
            lines.append("would remove %s (%d day(s) old, %d bytes)" % (row["name"], row["age_days"], row["size"]))
            removed += 1
            continue
        try:
            remove_entry(row["path"])
        except OSError as err:
            lines.append("could not remove %s: %s" % (row["name"], err))
            kept += 1
            continue
        removed += 1
        audit(row["name"], {"path": row["path"], "age_days": row["age_days"], "bytes": row["size"]})
        lines.append("removed %s (%d day(s) old, %d bytes)" % (row["name"], row["age_days"], row["size"]))
    for row in rows:
        if row["action"] == "keep" and row["why"] not in ("newer than %d day(s)" % cfg["days"],):
            lines.append("kept %s: %s" % (row["name"], row["why"]))
    return lines, removed, kept
