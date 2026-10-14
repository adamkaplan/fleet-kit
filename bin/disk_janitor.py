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
    import tempfile
    base = os.path.realpath(tempfile.gettempdir())
    if real != base and not real.startswith(base.rstrip("/") + "/"):
        return False, "%s is not under the system temp directory (%s): the janitor only cleans there" % (path, base)
    if real == base:
        return False, "%s is the system temp directory itself: name a folder inside it" % path
    return True, ""


def named_text(state_dir):
    """(text, complete): the text of the fleet's own records, which a path must not occur in. A big file is read by its TAIL (by design: records older than
    that are not seen, and the docs say so). `complete` is False when the directory cannot be listed, a record cannot be read or the total limit was
    reached: the caller then judges NOTHING (an entry the records name must never look unnamed because a record was unreadable)."""
    parts, total, complete = [], 0, True
    try:
        names = sorted(os.listdir(str(state_dir)))
    except OSError:
        return "", False
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
            complete = False
            continue
        total += len(data)
        parts.append(data)
        if total > STATE_TOTAL_BYTES:
            return "\n".join(parts), False
    return "\n".join(parts), complete


def _newest(path, deadline, clock=time.monotonic):
    """(newest mtime under path, complete): a bounded walk that never follows a link and never crosses onto another device. Incomplete when a
    directory cannot be read, the walk is too big or too slow, or another device is mounted inside."""
    try:
        info = os.lstat(path)
    except OSError:
        return None, False
    newest, seen, pending, complete = info.st_mtime, 0, [], True
    if stat.S_ISDIR(info.st_mode):
        pending.append(path)
    while pending:
        try:
            scan_ = os.scandir(pending.pop())
        except OSError:
            complete = False
            continue
        with scan_:
            for entry in scan_:
                seen += 1
                if seen > WALK_ENTRIES or clock() > deadline:
                    return newest, False
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    complete = False
                    continue
                newest = max(newest, st.st_mtime)
                if stat.S_ISDIR(st.st_mode):
                    if st.st_dev != info.st_dev:
                        complete = False
                        continue
                    pending.append(entry.path)
    return newest, complete


def _size(path, deadline, clock=time.monotonic):
    try:
        info = os.lstat(path)
    except OSError:
        return 0
    total = info.st_size
    if stat.S_ISDIR(info.st_mode):
        stack, seen = [path], 0
        while stack and seen <= WALK_ENTRIES and clock() <= deadline:
            try:
                scan_ = os.scandir(stack.pop())
            except OSError:
                continue
            with scan_:
                for entry in scan_:
                    seen += 1
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += st.st_size
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
    return total


def _forms(value):
    """The raw text and its JSON-escaped form: a record stores a path with its backslashes and non-ASCII characters escaped."""
    import json
    return {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}


def judge(path, text, now, cfg, uid=None, clock=time.monotonic):
    """One top-level entry: {"name", "path", "action": "remove" | "keep", "why", "size", "age_days", "ident"}. `ident` is (device, inode, newest mtime):
    run compares it again right before removing. Reads only."""
    uid = os.getuid() if uid is None else uid
    name = os.path.basename(path)
    row = {"name": name, "path": path, "action": "keep", "why": "", "size": 0, "age_days": 0, "ident": None}
    try:
        info = os.lstat(path)
    except OSError:
        row["why"] = "vanished"
        return row
    if info.st_uid != uid:
        row["why"] = "owned by another user"
        return row
    try:
        if info.st_dev != os.lstat(os.path.dirname(path)).st_dev:
            row["why"] = "is on another device than the temp directory (a mount point)"
            return row
    except OSError:
        row["why"] = "its directory cannot be read"
        return row
    keep = list(cfg.get("keep") or [])
    if any(fnmatch.fnmatchcase(name, g) or fnmatch.fnmatchcase(path, g) for g in keep):
        row["why"] = "matches a keep glob"
        return row
    if stat.S_ISDIR(info.st_mode) and os.path.lexists(os.path.join(path, KEEP_MARKER)):
        row["why"] = "holds a %s marker" % KEEP_MARKER
        return row
    deadline = clock() + WALK_SECONDS
    newest, complete = _newest(path, deadline, clock)
    if newest is None:
        row["why"] = "cannot be read"
        return row
    row["age_days"] = int((now - newest) / 86400)
    if not complete:
        row["why"] = "too many files, too slow, unreadable or another device inside: kept"
        return row
    if newest > now - cfg["days"] * 86400:
        row["why"] = "newer than %d day(s)" % cfg["days"]
        return row
    if any(form in text for form in _forms(path)) or (len(name) >= MIN_NAME and any(form in text for form in _forms(name))):
        row["why"] = "named by the fleet's records"
        return row
    row["action"], row["size"] = "remove", _size(path, deadline, clock)
    row["ident"] = (info.st_dev, info.st_ino, newest)
    return row


def scan(temp_dir, state_dir, now, cfg, uid=None, clock=time.monotonic):
    """[judge rows] for each top-level entry, sorted by name; every entry is kept when the fleet's records could not be read in full. Reads only."""
    text, complete = named_text(state_dir)
    try:
        names = sorted(os.listdir(str(temp_dir)))
    except OSError:
        return []
    rows = [judge(os.path.join(str(temp_dir), n), text, now, cfg, uid, clock) for n in names]
    if not complete:
        for row in rows:
            if row["action"] == "remove":
                row["action"], row["why"] = "keep", "the fleet's records could not be read in full: nothing is judged"
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


def run(temp_dir, state_dir, now, cfg, apply, audit, uid=None, clock=time.monotonic):
    """Returns (lines, removed, kept). `audit(name, detail)` is called once for each removal that happened; if it raises, removing STOPS and the line says so.
    Right before each removal the entry is judged again from the records as they are then, and removed only if it is still eligible and is the same
    entry (device, inode and newest modification time)."""
    lines, removed, kept = [], 0, 0
    ok, why = safe_dir(temp_dir)
    if not ok:
        return ["refused: %s" % why], 0, 0
    rows = scan(temp_dir, state_dir, now, cfg, uid, clock)
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
        again_text, again_complete = named_text(state_dir)
        again = judge(row["path"], again_text, now, cfg, uid, clock)
        if not again_complete or again["action"] != "remove" or again["ident"] != row["ident"] or not safe_dir(temp_dir)[0]:
            lines.append("kept %s: it changed since the scan" % row["name"])
            kept += 1
            continue
        try:
            remove_entry(row["path"])
        except OSError as err:
            lines.append("could not remove %s: %s" % (row["name"], err))
            kept += 1
            continue
        removed += 1
        lines.append("removed %s (%d day(s) old, %d bytes)" % (row["name"], row["age_days"], row["size"]))
        try:
            audit(row["name"], {"path": row["path"], "age_days": row["age_days"], "bytes": row["size"]})
        except Exception as err:   # a record that cannot be written stops the removing: nothing goes unrecorded after this one
            lines.append("STOPPED: %s was removed but its audit line could not be written (%s)" % (row["name"], err))
            return lines, removed, kept
    for row in rows:
        if row["action"] == "keep" and not row["why"].startswith("newer than"):
            lines.append("kept %s: %s" % (row["name"], row["why"]))
    return lines, removed, kept
