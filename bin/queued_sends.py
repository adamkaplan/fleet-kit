"""The queued sends: a message sent to a pane that is on hold is kept in the recipient's own queue until the hold ends.

One file per recipient in the state directory (`hold-queue-<name>.json`, mode 0600, written through a temp file and a rename, under one lock),
bounded in count and in bytes. The queue never drops an unseen message to make room: a full queue refuses the next one, with the reason.
Nothing here decides who may be queued or delivers anything: the daemon asks, and calls `done` once a message is delivered.

Standard library only. Layer 2 (a record of the world), read and written by `bin/fleet-switchboard`.
"""
import fcntl
import json
import os
import re
import stat
import time
from pathlib import Path

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("record_json", str(Path(__file__).resolve().parent / "record_json.py"))
_record_json = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_record_json)   # the shared bounded-depth parse: the queue file is daemon-written state another process can write

PREFIX = "hold-queue-"
LOCK = "hold-queue.lock"
NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")   # an agent's own name pattern: the only names that become a file name
MAX_MESSAGES = 50            # per recipient: the recorded case held 11 messages in 2.5 hours
MAX_BYTES = 120000           # the text of the whole queue of one recipient, in bytes
MAX_TEXT = 3000              # one message: below the delivery cap, so a queued message is never cut at delivery
MAX_FILE = 400000            # a queue file larger than this is not read (it is not ours)
KEEP_DELIVERED = 200         # keys kept as delivered, so a repeat is never delivered twice
OLD_SECONDS = 24 * 3600      # a message older than this is delivered marked with the time it was sent


def path(state_dir, name):
    """The queue file of `name`, or None for a name that is not an agent name."""
    return Path(state_dir) / (PREFIX + name + ".json") if isinstance(name, str) and NAME.fullmatch(name) else None


def _read(file):
    """{"seq", "items", "delivered"} from `file`; an empty queue when it is absent, not a regular file, too large or not ours."""
    empty = {"seq": 0, "items": [], "delivered": []}
    try:
        fd = os.open(str(file), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return empty
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            return empty
        with os.fdopen(fd, "r", closefd=False) as handle:
            data = _record_json.parse_bounded(handle.read())
    except (OSError, ValueError):
        return empty
    finally:
        os.close(fd)
    if not isinstance(data, dict):
        return empty
    items = [i for i in data.get("items") if isinstance(i, dict) and isinstance(i.get("key"), str) and isinstance(i.get("text"), str)
             and isinstance(i.get("at"), (int, float)) and isinstance(i.get("seq"), int)] if isinstance(data.get("items"), list) else []
    delivered = [k for k in data.get("delivered") if isinstance(k, str)] if isinstance(data.get("delivered"), list) else []
    seq = data.get("seq") if isinstance(data.get("seq"), int) else 0
    return {"seq": max([seq] + [i["seq"] for i in items]), "items": items, "delivered": delivered}


def _write(file, data):
    temp = file.with_name("%s.%d.tmp" % (file.name, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(str(temp), str(file))
    except OSError:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise


class _Lock:
    def __init__(self, state_dir):
        self.state_dir = state_dir

    def __enter__(self):
        self.fd = os.open(str(Path(self.state_dir) / LOCK), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)

    def __exit__(self, *exc):
        os.close(self.fd)


def enqueue(state_dir, recipient, key, sender, text, sent, issue=None, repo=None):
    """Keep one message. Returns (True, how many are now queued) or (False, the reason it was refused: nothing is stored)."""
    file = path(state_dir, recipient)
    if file is None:
        return False, "%r is not an agent name, so it has no queue" % (recipient,)
    if len(text) > MAX_TEXT:
        return False, "the message is %d characters and a held pane's queue takes at most %d" % (len(text), MAX_TEXT)
    try:
        with _Lock(state_dir):
            queue = _read(file)
            if key in queue["delivered"] or any(i["key"] == key for i in queue["items"]):
                return True, len(queue["items"])
            if len(queue["items"]) >= MAX_MESSAGES:
                return False, "the queue of %s is full (%d messages): none is dropped, so this one is refused" % (recipient, MAX_MESSAGES)
            if sum(len(i["text"].encode("utf-8")) for i in queue["items"]) + len(text.encode("utf-8")) > MAX_BYTES:
                return False, "the queue of %s is full (%d bytes): none is dropped, so this one is refused" % (recipient, MAX_BYTES)
            queue["seq"] += 1
            queue["items"].append({"key": key, "from": sender, "at": sent, "issue": issue, "repo": repo, "text": text, "seq": queue["seq"]})
            _write(file, queue)
            return True, len(queue["items"])
    except OSError as err:
        return False, "the queue of %s could not be written (%s)" % (recipient, err.strerror or type(err).__name__)


def waiting(state_dir, recipient):
    """The queued messages of `recipient`, in the order they were sent (ties by their sequence number)."""
    file = path(state_dir, recipient)
    if file is None:
        return []
    with _Lock(state_dir):
        return sorted(_read(file)["items"], key=lambda i: (i["at"], i["seq"]))


def recipients(state_dir):
    """The names that have a queue file now, sorted."""
    try:
        names = os.listdir(str(state_dir))
    except OSError:
        return []
    return sorted(n[len(PREFIX):-len(".json")] for n in names
                  if n.startswith(PREFIX) and n.endswith(".json") and NAME.fullmatch(n[len(PREFIX):-len(".json")]))


def done(state_dir, recipient, key):
    """`key` is delivered: it leaves the queue and is remembered as delivered, so it is never delivered again."""
    file = path(state_dir, recipient)
    if file is None:
        return
    with _Lock(state_dir):
        queue = _read(file)
        queue["items"] = [i for i in queue["items"] if i["key"] != key]
        if key not in queue["delivered"]:
            queue["delivered"] = (queue["delivered"] + [key])[-KEEP_DELIVERED:]
        _write(file, queue)


def marked(item, now):
    """The text to deliver: a message sent more than 24 hours earlier carries the time it was sent."""
    if now - item["at"] <= OLD_SECONDS:
        return item["text"]
    return "[queued while the pane was on hold; sent %s] %s" % (time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(item["at"])), item["text"])
