"""#1801: how long a message delivered by `queue` waited in the session's inbox (queued-wait.json in the state dir).

{message id: {"name", "session", "keys", "delivered", "consumed"?}}. The daemon notes a queue delivery; each pass `observe` reads the inbox of
every session with an unconsumed note: an item still listed is unconsumed, an item gone is CONSUMED, stamped with the time of the pass that saw
it gone (never earlier than the read, at most one pass late) and reported once. A note unconsumed for DROP_AFTER seconds is dropped (reported
once); a consumed note is kept CONSUMED_KEEP seconds for `pending`. Bounded to MAX_NOTES; 0600; renamed into place; written only on a change;
`parse` is record_json.parse_bounded, handed in by the caller. A file is kept across a restart.
"""
import fcntl
import itertools
import json
import os
import threading
import time
from pathlib import Path


FILE = "queued-wait.json"
MAX_NOTES = 500
MAX_BYTES = 1 << 20
DROP_AFTER = 7 * 86400
CONSUMED_KEEP = 86400
PENDING_WAIT_MINUTES = 5
MAX_READS_PER_PASS = 20   # inbox reads per pass; the oldest unconsumed note first, the rest the next pass
LOCK = "queued-wait.lock"
LOCK_WAIT = 2.0   # seconds a pass waits for the lock; then it skips (never raises)
EVENTS = "queued-wait-events.json"   # events (evictions) noted by note_delivery, audited by the next observe
_counter = itertools.count()


class _Lock:
    """Exclusive flock on LOCK in the state dir, bounded wait. `.held` is False when it could not be taken (the caller skips)."""

    def __init__(self, state_dir):
        self.dir, self.fd, self.held = state_dir, None, False

    def __enter__(self):
        try:
            self.fd = os.open(str(Path(self.dir) / LOCK), os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError:
            return self
        end = time.time() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.held = True
                return self
            except OSError:
                if time.time() >= end:
                    return self
                time.sleep(0.01)

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                if self.held:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
        return False


def _read(state_dir, parse):
    try:
        with open(str(Path(state_dir) / FILE), "rb") as handle:
            data = parse(handle.read(MAX_BYTES).decode("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    good = {}
    for key, note in data.items():
        if isinstance(key, str) and isinstance(note, dict) and isinstance(note.get("session"), str) \
                and isinstance(note.get("delivered"), (int, float)) and not isinstance(note.get("delivered"), bool):
            good[key] = note
    return good


def _write(state_dir, notes):
    path = Path(state_dir) / FILE
    return _write_to(path, notes)


def _write_to(path, notes):
    temp = path.with_name("%s.%d.%d.%d.tmp" % (path.name, os.getpid(), threading.get_ident(), next(_counter)))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(notes, sort_keys=True) + "\n")
        os.rename(str(temp), str(path))
    except OSError:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise


def _save(state_dir, old, new):
    if new != old:
        _write(state_dir, new)


def note_delivery(state_dir, message_id, name, session, keys, delivered, parse):
    """A message was delivered by queue: remember when. Returns [(event, name, detail)] for the caller to audit (an eviction, a skipped note)."""
    keys = list(keys or [])
    if not isinstance(message_id, str) or not message_id:
        first = next((k for k in keys if isinstance(k, str) and k), None)
        if first is None:
            return [("queued_wait.skipped", name, {"reason": "no message id and no key", "session": session})]
        message_id = first
    with _Lock(state_dir) as lock:
        if not lock.held:
            return [("queued_wait.skipped", name, {"reason": "lock busy", "message_id": message_id})]
        old = _read(state_dir, parse)
        new = dict(old)
        new[message_id] = {"name": name, "session": session, "keys": keys, "delivered": delivered}
        events = []
        while len(new) > MAX_NOTES:
            gone = min(new, key=lambda k: new[k]["delivered"])
            note = new.pop(gone)
            events.append(("queued_wait.evicted", note.get("name"), {
                "message_id": gone, "keys": note.get("keys") or [], "delivered": note["delivered"],
                "consumed": "consumed" in note, "reason": "more than %d notes" % MAX_NOTES}))
        _save(state_dir, old, new)
        if events:
            try:
                _write_to(Path(state_dir) / EVENTS, (_events(state_dir) + [list(e) for e in events])[-MAX_NOTES:])
            except OSError:
                pass
        return events


def _events(state_dir):
    try:
        data = json.loads((Path(state_dir) / EVENTS).read_text())
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, list) and len(e) == 3] if isinstance(data, list) else []


def _listed(message_id, note, inbox):
    keys = set(k for k in note.get("keys") or [] if isinstance(k, str))
    for item in inbox:
        if not isinstance(item, dict):
            continue
        if item.get("id") == message_id:
            return True
        meta = item.get("metadata") or (item.get("payload") or {}).get("metadata") or {}
        fleet = meta.get("fleet") if isinstance(meta, dict) else None
        have = fleet.get("keys") if isinstance(fleet, dict) else None
        if keys and isinstance(have, list) and keys <= set(k for k in have if isinstance(k, str)):
            return True
    return False


def observe(state_dir, now, parse, read_inbox):
    """One pass. `read_inbox(session)` -> a list, or None when unreadable (then nothing is concluded). Returns [(event, name, detail)] to audit.
    An item seen gone is CONSUMED only when a SECOND pass also sees it gone (stamped with the FIRST pass's time); a pass that lists it resets."""
    with _Lock(state_dir) as lock:
        if not lock.held:
            return []
        old = _read(state_dir, parse)
        new = {k: dict(v) for k, v in old.items()}
        events = [tuple(e) for e in _events(state_dir)]
        if events:
            try:
                os.unlink(str(Path(state_dir) / EVENTS))
            except OSError:
                pass
        inboxes = {}
        reads = 0
        for message_id, note in sorted(new.items(), key=lambda kv: kv[1]["delivered"]):
            if "consumed" in note:
                if now - note["consumed"] > CONSUMED_KEEP:
                    del new[message_id]
                continue
            session = note["session"]
            if session not in inboxes:
                if reads >= MAX_READS_PER_PASS:
                    continue   # the rest next pass
                reads += 1
                inboxes[session] = read_inbox(session)
            inbox = inboxes[session]
            if isinstance(inbox, list) and not _listed(message_id, note, inbox):
                first = note.get("gone_since")
                if not isinstance(first, (int, float)) or isinstance(first, bool):
                    note["gone_since"] = now   # first sighting: not yet concluded
                    continue
                del note["gone_since"]
                note["consumed"] = first
                events.append(("message.consumed", note.get("name"), {
                    "message_id": message_id, "keys": note.get("keys") or [], "delivered": note["delivered"], "consumed": first,
                    "waited_seconds": int(first - note["delivered"])}))
                continue
            if isinstance(inbox, list):
                note.pop("gone_since", None)   # read again: the sighting is reset
            if now - note["delivered"] > DROP_AFTER:
                del new[message_id]
                events.append(("message.consumed.dropped", note.get("name"), {
                    "message_id": message_id, "keys": note.get("keys") or [], "delivered": note["delivered"],
                    "reason": "neither listed nor seen consumed within %d days" % (DROP_AFTER // 86400)}))
        _save(state_dir, old, new)
        return events


def lines(state_dir, now, parse, name):
    """The `pending` lines of one agent: a wait over PENDING_WAIT_MINUTES, for an unconsumed note or one consumed within 24 h."""
    out = []
    for message_id, note in sorted(_read(state_dir, parse).items(), key=lambda kv: kv[1]["delivered"]):
        if note.get("name") != name:
            continue
        if "consumed" in note:
            if not isinstance(note["consumed"], (int, float)) or now - note["consumed"] > CONSUMED_KEEP:
                continue
            minutes, word = int((note["consumed"] - note["delivered"]) // 60), "waited %d min"
        else:
            minutes, word = int((now - note["delivered"]) // 60), "waited %d min, not yet read"
        if minutes > PENDING_WAIT_MINUTES:
            out.append("  - %s: %s" % (message_id, word % minutes))
    return out
