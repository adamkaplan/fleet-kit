"""#1801: how long a message delivered by `queue` waited in the session's inbox (queued-wait.json in the state dir).

{message id: {"name", "session", "keys", "delivered", "consumed"?}}. The daemon notes a queue delivery; each pass `observe` reads the inbox of
every session with an unconsumed note: an item still listed is unconsumed, an item gone is CONSUMED, stamped with the time of the pass that saw
it gone (never earlier than the read, at most one pass late) and reported once. A note unconsumed for DROP_AFTER seconds is dropped (reported
once); a consumed note is kept CONSUMED_KEEP seconds for `pending`. Bounded to MAX_NOTES; 0600; renamed into place; written only on a change;
`parse` is record_json.parse_bounded, handed in by the caller. A file is kept across a restart.
"""
import json
import os
from pathlib import Path


FILE = "queued-wait.json"
MAX_NOTES = 500
MAX_BYTES = 1 << 20
DROP_AFTER = 7 * 86400
CONSUMED_KEEP = 86400
PENDING_WAIT_MINUTES = 5


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
    temp = path.with_name("%s.%d.tmp" % (FILE, os.getpid()))
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
    """A message was delivered by queue: remember when."""
    old = _read(state_dir, parse)
    new = dict(old)
    new[message_id] = {"name": name, "session": session, "keys": list(keys), "delivered": delivered}
    while len(new) > MAX_NOTES:
        del new[min(new, key=lambda k: new[k]["delivered"])]
    _save(state_dir, old, new)


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
    """One pass. `read_inbox(session)` -> a list, or None when unreadable (then nothing is concluded). Returns [(event, name, detail)] to audit."""
    old = _read(state_dir, parse)
    new = {k: dict(v) for k, v in old.items()}
    events = []
    inboxes = {}
    for message_id, note in sorted(new.items(), key=lambda kv: kv[1]["delivered"]):
        if "consumed" in note:
            if now - note["consumed"] > CONSUMED_KEEP:
                del new[message_id]
            continue
        session = note["session"]
        if session not in inboxes:
            inboxes[session] = read_inbox(session)
        inbox = inboxes[session]
        if isinstance(inbox, list) and not _listed(message_id, note, inbox):
            note["consumed"] = now
            events.append(("message.consumed", note.get("name"), {
                "message_id": message_id, "keys": note.get("keys") or [], "delivered": note["delivered"], "consumed": now,
                "waited_seconds": int(now - note["delivered"])}))
        elif now - note["delivered"] > DROP_AFTER:
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
