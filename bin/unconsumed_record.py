"""#1844: what the daemon tried to steer and which reminder ids it delivered (unconsumed.json in the state dir).

{"steered": [inbox ids whose steer was tried], "reminders": {"reminder:<id>": [dedupe key, from, to]}}, each bounded to CAP, oldest dropped.
`parse` is record_json.parse_bounded, passed by the caller. A missing, corrupt, oversized, unreadable or wrong-typed file reads as empty
with a reason (the caller audits it once). `write` touches the disk only when the content changed.
"""
import json
import os
import stat
from pathlib import Path


FILE = "unconsumed.json"
MAX_BYTES = 1 << 20
CAP = 500


def _clean(data):
    steered = [i for i in data.get("steered", []) if isinstance(i, str)] if isinstance(data.get("steered"), list) else []
    reminders = {}
    raw = data.get("reminders")
    for key, ident in (raw.items() if isinstance(raw, dict) else []):
        if isinstance(key, str) and isinstance(ident, list) and len(ident) == 3 and isinstance(ident[0], str) and isinstance(ident[1], str):
            reminders[key] = ident
    return {"steered": steered[-CAP:], "reminders": dict(list(reminders.items())[-CAP:])}


def read(state_dir, parse):
    """(record, reason): reason is None when the file was read, else why the record reads as empty."""
    empty = {"steered": [], "reminders": {}}
    try:
        if not stat.S_ISREG(os.lstat(str(Path(state_dir) / FILE)).st_mode):   # a FIFO would block the pass, a symlink is not followed
            return empty, "unreadable"
        with open(str(Path(state_dir) / FILE), "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return empty, "missing"
    except OSError:
        return empty, "unreadable"
    if len(raw) > MAX_BYTES:
        return empty, "oversized"
    try:
        data = parse(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return empty, "corrupt"
    if not isinstance(data, dict):
        return empty, "wrong type"
    return _clean(data), None


def write(state_dir, record, parse):
    """Writes only when the content differs from the file (temporary file, rename, 0600). Raises OSError on failure."""
    new = _clean(record)
    old, reason = read(state_dir, parse)
    if reason is None and old == new:
        return False
    path = Path(state_dir) / FILE
    temp = path.with_name("%s.%d.tmp" % (FILE, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(new, sort_keys=True) + "\n")
        os.rename(str(temp), str(path))
    except OSError:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise
    return True
