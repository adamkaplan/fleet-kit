"""The temp-directory janitor (ask #321). Pure of the switchboard: the caller hands it the directories, the clock, the settings and an audit
function. It looks at the TOP-LEVEL entries of the temp directory only, and an entry is removed (as a unit) when ALL of these hold:

  - it is older than `days`: the latest modification, change or birth time of the entry and of anything inside it (a bounded walk; a tree too big to judge
    is kept; the change time is what a restore with tar or cp -a cannot fake);
  - the fleet's own records do not name it: its full path, or its basename when that is at least six characters, does not occur in any state
    file of the state directory (`*.json` and `*.jsonl`, never the compressed archives; a big file is read by its tail);
  - it does not match a keep-list glob of the settings, and it does not hold a `.fleet-keep` marker at its top;
  - nothing is using it: no process has it as its working directory, no pane of the fleet works in it, no live agent session runs in it (the caller's
    `busy` function says; if it cannot say, the entry is KEPT);
  - it belongs to the user running this, and it is not the temp directory itself.

A symlink is removed as a link and never followed; a directory is removed without following any link inside it. Nothing here touches a path
outside the temp directory. A dry run (the default) removes nothing. What is NOT seen: a path written only in a GitHub issue or in an agent's
own memory: use the keep-list or a `.fleet-keep` marker for that."""

import bisect
import fnmatch
import json
import os
import shutil
import stat
import time
from pathlib import Path

SCAN_SECONDS = 60.0          # the whole scan (config `scan_seconds`): past it the rest are NOT JUDGED, and the next run continues after the last judged name
DEFAULTS = {"enabled": True, "days": 7, "keep": [], "dir": None, "scan_seconds": SCAN_SECONDS, "weekly_scan_seconds": 600, "daemon_apply": False}
KEEP_MARKER = ".fleet-keep"
WALK_ENTRIES = 20000          # files one entry may hold and still be judged
WALK_SECONDS = 5.0            # one entry's walk
REMOVE_MAX = 500              # removals per call
STATE_FILE_BYTES = 2000000    # of one state file read (its tail)
STATE_TOTAL_BYTES = 16000000
CURSOR_FILE = "temp-janitor-cursor.json"   # {"after": <the last name judged>}: a NAME, never a position
BUDGET_MAX = 86400
MIN_NAME = 6                  # a basename shorter than this is too common to count as naming something


class Unknown(Exception):
    """Raised by the caller's `busy` function when it cannot say whether something uses an entry: the entry is then KEPT (fail closed)."""


def settings(raw):
    merged = dict(DEFAULTS)
    merged.update(raw or {})
    return merged


CONFIG_KEY = "temp_janitor"   # the module declares its own configuration key (bin/fleet-switchboard config_declarations)
CONFIG_DEFAULT = None         # None: no default is added to the configuration (absent = the defaults in settings())


def validate(raw, source="config"):
    """Raises ValueError with a message when `temp_janitor` is not {"enabled": bool, "days": 1..365, "keep": [glob, ...], "dir": absolute path}."""
    if raw is None:
        return
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("%s: 'temp_janitor' must be an object with only enabled, days, keep, dir, scan_seconds, weekly_scan_seconds and daemon_apply, got %r" % (source, raw))
    for key in ("scan_seconds", "weekly_scan_seconds"):
        value = raw.get(key, DEFAULTS[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= BUDGET_MAX:
            raise ValueError("%s: temp_janitor.%s must be a number of seconds from 1 to %d, got %r" % (source, key, BUDGET_MAX, value))
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        raise ValueError("%s: temp_janitor.enabled must be true or false" % source)
    if "daemon_apply" in raw and not isinstance(raw["daemon_apply"], bool):
        raise ValueError("%s: temp_janitor.daemon_apply must be true or false" % source)
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
    if not any((n.endswith(".json") or n.endswith(".jsonl")) and n != CURSOR_FILE for n in names):
        return "", False   # a state directory with no record at all is the wrong directory, or an empty one: nothing can be said to be unnamed
    for name in names:
        if not (name.endswith(".json") or name.endswith(".jsonl")) or name == CURSOR_FILE:
            continue   # the janitor's own cursor is not a record of the fleet: a name in it must not keep that entry
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


def _stamp(info):
    """The latest of modification time, change time and (where there is one) birth time: a tree restored with tar, cp -a, rsync -a or docker cp keeps its
    OLD modification times, but its change time is when it was created, and a user cannot set that."""
    return max(info.st_mtime, info.st_ctime, getattr(info, "st_birthtime", 0) or 0)


def _newest(path, deadline, clock=time.monotonic):
    """(newest mtime under path, complete): a bounded walk that never follows a link and never crosses onto another device. Incomplete when a
    directory cannot be read, the walk is too big or too slow, or another device is mounted inside."""
    try:
        info = os.lstat(path)
    except OSError:
        return None, False
    newest, seen, pending, complete = _stamp(info), 0, [], True
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
                newest = max(newest, _stamp(st))
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


def judge(path, text, now, cfg, uid=None, clock=time.monotonic, busy=None):
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
    path_forms = _forms(path) | _forms(os.path.realpath(path))
    if any(form in text for form in path_forms) or (len(name) >= MIN_NAME and any(form in text for form in _forms(name))):
        row["why"] = "named by the fleet's records"
        return row
    if busy is not None:
        try:
            using = busy(path)
        except Unknown as err:
            row["why"] = "cannot say whether anything is using it (%s): kept" % err
            return row
        if using:
            row["why"] = using
            return row
    row["action"], row["size"] = "remove", _size(path, deadline, clock)
    row["ident"] = (info.st_dev, info.st_ino, newest)
    return row


def read_cursor(state_dir):
    """The name the last budget stop ended after, or None. A missing, empty, unreadable or corrupt file is None (start at the first name), never an error."""
    try:
        with open(os.path.join(str(state_dir), CURSOR_FILE)) as handle:
            after = json.loads(handle.read(4096)).get("after")
        return after if isinstance(after, str) and after and "\x00" not in after else None
    except (OSError, ValueError, AttributeError):
        return None


def write_cursor(state_dir, name):
    """Record `name` (or clear the cursor with None): mode 0600, atomic, written only when it changes. Returns True when it wrote. Raises OSError."""
    path = os.path.join(str(state_dir), CURSOR_FILE)
    if read_cursor(state_dir) == name and (name is not None or not os.path.exists(path)):
        return False
    if name is None:
        os.unlink(path)
        return True
    temp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps({"after": name}))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return True


def scan(temp_dir, state_dir, now, cfg, uid=None, clock=time.monotonic, busy=None, budget=None):
    """[judge rows] for the top-level entries from the cursor on, in name order; every entry is kept when the fleet's records could not be read in full. Reads only.
    The scan starts at the first name AFTER the cursor (the last name an earlier budget stop judged; the first name when there is none or it is past the end).
    Past the time budget (`budget`, else the config's `scan_seconds`, else SCAN_SECONDS) the entries not reached are rows with `judged` False: never removed,
    reported as not judged, and the next run starts after the last judged one."""
    text, complete = named_text(state_dir)
    try:
        names = sorted(os.listdir(str(temp_dir)))
    except OSError:
        return []
    after = read_cursor(state_dir)
    start = bisect.bisect_right(names, after) if after else 0
    if start >= len(names):
        start = 0
    seconds = budget if budget is not None else (cfg or {}).get("scan_seconds") or SCAN_SECONDS
    limit, rows = clock() + seconds, []
    for n in names[start:]:
        if clock() > limit:
            rows.append({"name": n, "path": os.path.join(str(temp_dir), n), "action": "keep", "why": "not judged: the scan's time budget was spent", "size": 0,
                         "age_days": 0, "ident": None, "judged": False})
            continue
        rows.append(judge(os.path.join(str(temp_dir), n), text, now, cfg, uid, clock, busy))
    if not complete:
        for row in rows:
            row["incomplete"] = True
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


def run(temp_dir, state_dir, now, cfg, apply, audit, uid=None, clock=time.monotonic, busy=None, weekly=False, outcome=None):
    """Returns (lines, removed, kept). `outcome`, when a list, receives one dict (name, age_days, bytes) for each entry that is removed (or, on a dry run, would be): the daemon's audit summary reads it; it changes nothing else. `audit(name, detail)` is called once for each removal that happened; if it raises, removing STOPS and the line says so.
    Right before each removal the entry is judged again from the records as they are then, and removed only if it is still eligible and is the same
    entry (device, inode and newest modification time)."""
    lines, removed, kept = [], 0, 0
    ok, why = safe_dir(temp_dir)
    if not ok:
        return ["refused: %s" % why], 0, 0
    budget = (cfg or {}).get("weekly_scan_seconds" if weekly else "scan_seconds") or None
    rows = scan(temp_dir, state_dir, now, cfg, uid, clock, busy, budget)
    for row in rows:
        if row["action"] != "remove":
            if row.get("judged", True):
                kept += 1
            continue
        if removed >= REMOVE_MAX:
            lines.append("only %d entries handled per call: run it again for the rest" % REMOVE_MAX)
            break
        if not apply:
            lines.append("would remove %s (%d day(s) old, %d bytes)" % (row["name"], row["age_days"], row["size"]))
            if outcome is not None:
                outcome.append({"name": row["name"], "age_days": row["age_days"], "bytes": row["size"]})
            removed += 1
            continue
        again_text, again_complete = named_text(state_dir)
        again = judge(row["path"], again_text, now, cfg, uid, clock, busy)
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
        if outcome is not None:
            outcome.append({"name": row["name"], "age_days": row["age_days"], "bytes": row["size"]})
        lines.append("removed %s (%d day(s) old, %d bytes)" % (row["name"], row["age_days"], row["size"]))
        try:
            audit(row["name"], {"path": row["path"], "age_days": row["age_days"], "bytes": row["size"]})
        except Exception as err:   # a record that cannot be written stops the removing: nothing goes unrecorded after this one
            lines.append("STOPPED: %s was removed but its audit line could not be written (%s)" % (row["name"], err))
            return lines, removed, kept
    for row in rows:
        if row["action"] == "keep" and row.get("judged", True) and not row["why"].startswith("newer than"):
            lines.append("kept %s: %s" % (row["name"], row["why"]))
    lines.extend(_coverage(state_dir, rows, apply))
    return lines, removed, kept


def _coverage(state_dir, rows, apply):
    """The report lines about coverage, and (on an apply run) the cursor moved: how many entries were judged and how many were not, and `cycle complete` when
    the run reached the end of the list. A dry run reads the cursor and never moves it (a preview must not change what the next real run covers)."""
    if any(r.get("incomplete") for r in rows):   # nothing was really judged: no coverage was made, so the cursor stays where it was
        return ["nothing was judged: the fleet's records could not be read in full (the cursor did not move)"]
    judged = [r for r in rows if r.get("judged", True)]
    unreached = [r for r in rows if not r.get("judged", True)]
    lines = ["%d judged, %d not judged" % (len(judged), len(unreached))]
    after = judged[-1]["name"] if unreached and judged else None
    if unreached:
        lines.append("the time budget was spent: the next run continues after %s" % (_safe_name(after) if after else "the same place"))
    elif rows:
        lines.append("cycle complete")
    if apply:
        try:
            if unreached and not judged:
                pass   # no progress this run: the cursor stays where it was
            else:
                write_cursor(state_dir, after)
        except OSError as err:
            lines.append("the cursor could not be written (%s): check that the state directory is writable; the next run starts where this one did" % err.strerror)
    return lines


def _safe_name(name):
    return "".join(ch if ch.isprintable() else "?" for ch in str(name))[:120]
