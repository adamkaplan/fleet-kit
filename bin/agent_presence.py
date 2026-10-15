"""Presence for a worker that has no terminal (a headless local worker now, a cloud worker later): ONE rule, ask #433.

A headless worker is a session in an OpenCode service. herdr knows nothing about it, so what it is doing comes from the service alone. The rule below
was measured in a spike (a throwaway profile on the live version, never on the live service):

* the session id is in `session.active` (an entry `{"type": "running"}`): the worker is working;
* it is in `session.active` AND `session.permission.list` or `session.form.list` is not empty: it is BLOCKED. `session.active` still says `running`
  while a worker waits for an answer, so blocked is derived, never read from `active` alone;
* it is not in `session.active`: it is idle (a prompt queued a moment before the service starts its turn is seen as idle until the next read);
* the service does not answer: UNKNOWN. Never dead, never working: silence is not a state.

This module reads nothing itself except through the client it is given, writes only its own records, and holds no password (the shared-password
service client of the switchboard is the one that reaches the service). It is loaded by path (`load_agent_presence` in bin/fleet-switchboard).
Python 3.9 standard library only.
"""
import json
import os
import re
import stat
import time
from pathlib import Path

WORKING, BLOCKED, IDLE, UNKNOWN = "working", "blocked", "idle", "unknown"
STATES = (WORKING, BLOCKED, IDLE, UNKNOWN)
RECORD_DIR = "headless"          # state-dir/headless/<name>.json, one private record per headless worker
RECORD_MAX = 8192
MAX_RECORDS = 200                # files read per scan; more is said, not dropped
LOCAL = "local"                  # the record kind of a worker on this machine. The record is LOCAL-ONLY: the shared part with the cloud lane is the state rule; a shared record would need docs/cloud-worker-interface.md section 3 amended first
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
LOOPBACK_URL_RE = re.compile(r"^http://(127\.0\.0\.1|\[::1\]):[0-9]{1,5}$")


def state_of(active_entry, permissions, forms, last_message, reachable):
    """`working`, `blocked`, `idle` or `unknown` for one session, exactly per the rule above.

    `active_entry` is the session's entry in `session.active` (or None when it is not there), `permissions` and `forms` the lists the service gave
    (None or empty for none), `last_message` the session's newest message (kept for a later refinement: it does not change the state today) and
    `reachable` False when the service did not answer."""
    if not reachable:
        return UNKNOWN
    if active_entry:
        return BLOCKED if (permissions or forms) else WORKING
    return IDLE


def read_state(v2, session_id, active=None, last_message=None):
    """(state, why) for `session_id`, read through `v2` (a client with `session_active`, `permission_list`, `form_list`). `active` is the dict
    `session.active` returned this pass (one call serves every session); None reads it here. A V2 error other than 404 is `unknown`; a 404 is
    `("missing", ...)`: the session is gone, which is not a state of a worker."""
    try:
        if active is None:
            active = v2.session_active() or {}
        entry = active.get(session_id) if isinstance(active, dict) else None
        permissions = forms = None
        if entry:   # the lists matter only while it runs: an idle session cannot be waiting for an answer
            permissions, forms = v2.permission_list(session_id), v2.form_list(session_id)
        return state_of(entry, permissions, forms, last_message, True), None
    except Exception as err:   # the client raises its own error type; this module does not import it
        if getattr(err, "reason", None) == "http" and getattr(err, "status", None) == 404:
            return "missing", "the session no longer exists"
        return UNKNOWN, "the service did not answer (%s)" % str(err)[:80]


# ---------------------------------------------------------------- the record of a headless worker

def record_dir(state):
    path = Path(state) / RECORD_DIR
    if not path.is_dir():
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    found = os.lstat(str(path))
    if stat.S_ISLNK(found.st_mode) or found.st_uid != os.getuid():
        raise ValueError("the headless directory is not the user's own")
    os.chmod(str(path), 0o700)
    return path


def valid_name(name):
    return isinstance(name, str) and bool(NAME_RE.match(name))


def record_path(state, name):
    if not valid_name(name):
        raise ValueError("not a worker name: %r" % (name,))
    return record_dir(state) / (name + ".json")


def save_record(state, name, session, url, now=None):
    """Write the record of a headless worker (atomic, 0600): `worker`, `url`, `session`, `kind` local, `created`. The url is where the service was when
    it was launched: informational only, NEVER used to send a request (discovery goes through the switchboard's own service client)."""
    if not isinstance(session, str) or not session.startswith("ses_") or len(session) > 128:
        raise ValueError("not a session id")
    if url is not None and (not isinstance(url, str) or not LOOPBACK_URL_RE.match(url)):
        raise ValueError("a local worker's url is plain http to a loopback address (or none)")
    path = record_path(state, name)
    body = {"worker": name, "url": url, "session": session, "kind": LOCAL, "created": int(now if now is not None else time.time())}
    temp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(body, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(str(temp), str(path))
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise
    return body


def read_record(state, name):
    """(record, None) for a usable local record, (None, why) for a file that is there and cannot be used, (None, None) when there is none. Reads only:
    it never creates the directory."""
    if not valid_name(name):
        return None, "not a worker name"
    base, why = _checked_dir(state)
    if base is None:
        return None, why
    path = base / (name + ".json")
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None, None
    except OSError as err:
        return None, "cannot be opened (%s)" % (err.strerror or err)
    try:
        with os.fdopen(fd, "rb") as handle:
            found = os.fstat(handle.fileno())
            if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid() or found.st_mode & 0o077:
                return None, "is not private to this user (mode 0600, ours, a regular file)"
            raw = handle.read(RECORD_MAX + 1)
        if len(raw) > RECORD_MAX:
            return None, "is too large"
        record = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None, "is not readable JSON"
    if not isinstance(record, dict) or record.get("kind") != LOCAL or record.get("worker") != name:
        return None, "is not a local record of this worker"
    if not isinstance(record.get("session"), str) or not record["session"].startswith("ses_"):
        return None, "names no session"
    return record, None


def load_record(state, name):
    """The record of `name`, or None when there is none or it is not usable (never raises for a bad file)."""
    return read_record(state, name)[0]


def _checked_dir(state):
    """(path, None) of the headless directory when it is a real directory of this user, (None, why) when it is a symlink or not ours, (None, None) when
    there is none. Reads only."""
    base = Path(state) / RECORD_DIR
    try:
        found = os.lstat(str(base))
    except FileNotFoundError:
        return None, None
    except OSError as err:
        return None, "the headless record directory cannot be read (%s)" % (err.strerror or err)
    if stat.S_ISLNK(found.st_mode) or not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid():
        return None, "the headless record directory is not the user's own real directory: no record in it is read"
    return base, None


def scan_records(state):
    """([usable records sorted by worker], [one line per problem]). A missing directory is silent. A symlinked or foreign directory, a file whose name is
    not a worker name, a record that is unusable and more than MAX_RECORDS files are each said once: nothing is dropped without a word."""
    base, why = _checked_dir(state)
    if base is None:
        return [], ([why] if why else [])
    try:
        files = sorted(p.name for p in base.iterdir() if p.suffix == ".json")
    except OSError as err:
        return [], ["the headless record directory cannot be read (%s)" % (err.strerror or err)]
    records, problems = [], []
    named = [f for f in files if valid_name(f[:-5])]
    for f in files:
        if not valid_name(f[:-5]):
            problems.append("the file %r in the headless record directory is not named for a worker: it is not read" % f[:80])
    if len(named) > MAX_RECORDS:
        problems.append("more than %d headless records (%d): the rest are not listed (cap %d)" % (MAX_RECORDS, len(named), MAX_RECORDS))
    for f in named[:MAX_RECORDS]:
        name = f[:-5]
        record, why = read_record(state, name)
        if record is not None:
            records.append(record)
        elif why:
            problems.append("the headless record of %s %s: it is not listed" % (name, why))
    return records, problems


def list_records(state):
    """Every usable local record, sorted by worker name ([] when there is none)."""
    return scan_records(state)[0]


def remove_record(state, name):
    """Delete the record (a retired worker keeps none). True when one was there. Never creates the directory."""
    if not valid_name(name):
        return False
    base, _ = _checked_dir(state)
    if base is None:
        return False
    try:
        (base / (name + ".json")).unlink()
        return True
    except OSError:
        return False
