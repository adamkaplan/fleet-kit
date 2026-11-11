"""#1886: the once-a-day memory of the alert that an open-item list is longer than OPEN_LIST_PAGES (open-list-alert.json in the state dir).

{repository: the day (YYYY-MM-DD) it was last told}. An absent, unreadable or oversized file is empty and SILENT (a normal start raises nothing and audits nothing).
Bounded: at most MAX_REPOS entries, one per watched repository; written only when its content changes.
"""
import json
import os
from pathlib import Path


FILE = "open-list-alert.json"
MAX_BYTES = 1 << 16
MAX_REPOS = 200


def _read(state_dir, parse):
    try:
        with open(str(Path(state_dir) / FILE), "rb") as handle:
            data = parse(handle.read(MAX_BYTES).decode("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def due(state_dir, repo, day, parse):
    """True when `repo` was not yet told on `day`."""
    return _read(state_dir, parse).get(repo) != day


def mark(state_dir, repo, day, parse):
    """Remembers that `repo` was told on `day`; writes only on a change (0600, renamed into place; `parse` is record_json.parse_bounded, handed in by the daemon). False when it could not be written."""
    old = _read(state_dir, parse)
    if old.get(repo) == day:
        return True
    new = dict(old, **{repo: day})
    while len(new) > MAX_REPOS:
        new.pop(sorted(new, key=lambda k: (new[k], k))[0])
    path = Path(state_dir) / FILE
    tmp = path.with_name(FILE + ".tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(new, sort_keys=True) + "\n")
        os.replace(str(tmp), str(path))
    except OSError:
        return False
    return True
