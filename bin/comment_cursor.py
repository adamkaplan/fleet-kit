"""#1889: the cursor file of the incremental comment reads (comment-cursors.json in the state dir).

{"<consumer>|<repository>": the created_at (ISO, UTC) of the newest comment held}. Bounded: at most MAX_KEYS keys and MAX_BYTES bytes; written only when
the content changes (mode 0600, renamed into place). Read through `parse` (record_json.parse_bounded, handed in by the daemon).
"""
import json
import os
from pathlib import Path

FILE = "comment-cursors.json"
MAX_BYTES = 1 << 16
MAX_KEYS = 200


def load(state_dir, parse):
    """(cursors, status): status is "ok", "absent" or "unreadable" (a file too large, not JSON, too deep or not an object)."""
    try:
        with open(str(Path(state_dir) / FILE), "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {}, "absent"
    except OSError:
        return {}, "unreadable"
    if len(raw) > MAX_BYTES:
        return {}, "unreadable"
    try:
        data = parse(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}, "unreadable"
    if not isinstance(data, dict):
        return {}, "unreadable"
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}, "ok"


def save(state_dir, cursors, parse):
    """Writes `cursors` only when the file's content would change. False when it could not be written."""
    new = dict(sorted(cursors.items(), key=lambda kv: (kv[1], kv[0]))[-MAX_KEYS:])
    old, _ = load(state_dir, parse)
    if old == new:
        return True
    text = json.dumps(new, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_BYTES:
        return False
    path = Path(state_dir) / FILE
    tmp = path.with_name(FILE + ".tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(str(tmp), str(path))
    except OSError:
        return False
    return True
