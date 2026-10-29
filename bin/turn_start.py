"""#1540: when each working session's current turn began, as the daemon first saw it (turn-start.json in the state dir).

{session id: epoch the pass first saw it `working`}. A session whose status is not `working` is dropped, so working, idle, working
restarts the age; a missing, unreadable or unlisted entry is an unknown age (None), which the caller counts as young. `parse` is record_json.parse_bounded, passed by the caller. Used only
with `steer_after_minutes` on; with it off nothing here runs. Bounded: one entry per working session.
"""
import json
import os
from pathlib import Path


FILE = "turn-start.json"
MAX_BYTES = 1 << 20


def _read(state_dir, parse):
    try:
        with open(str(Path(state_dir) / FILE), "rb") as handle:
            data = parse(handle.read(MAX_BYTES).decode("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)}


def age(state_dir, session_id, now, parse):
    """`parse` is record_json.parse_bounded, handed in by the daemon (the one bounded parse of a record it did not just write). Seconds since the recorded start of this session's turn, or None when unknown."""
    start = _read(state_dir, parse).get(session_id)
    return None if start is None or start > now else now - start


def note(state_dir, sessions, now, parse):
    """`sessions`: (session id, status) pairs seen this pass. Writes the file only when its content changes."""
    old = _read(state_dir, parse)
    new = {}
    for session_id, status in sessions:
        if status == "working" and isinstance(session_id, str) and session_id:
            new[session_id] = old.get(session_id, now)
    if new == old:
        return
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
