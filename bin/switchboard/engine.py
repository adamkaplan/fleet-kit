# ---------------------------------------------------------------- the engine

class LockError(Exception):
    """Another `run` holds the single-instance lock."""


@contextmanager
def single_instance(state_dir):
    """Hold $STATE/run.lock for the life of the block, or raise LockError at once."""
    path = Path(state_dir) / "run.lock"
    fd = adopt_inherited_lock(path)
    if fd is None:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    _HELD_LOCK["fd"] = fd
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = os.read(fd, 32).decode("ascii", "replace").strip() or "unknown"
            raise LockError("another %s run holds %s (pid %s)" % (PROG, path, holder)) from None
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)  # an adopted descriptor is not at the start
        os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
        yield path
    finally:
        _HELD_LOCK["fd"] = None
        os.close(fd)  # closing the descriptor releases the lock


LOCK_FD_ENV = "FLEET_SWITCHBOARD_LOCK_FD"  # set across a re-exec: the descriptor that still holds run.lock
_HELD_LOCK = {"fd": None}


def adopt_inherited_lock(path):
    """The descriptor a daemon passed across `exec` to itself, when it is the lock file of `path`; else None.
    The flock belongs to the open file description, so it was never released: there was no gap."""
    raw = os.environ.pop(LOCK_FD_ENV, None)
    try:
        fd = int(raw) if raw else None
        if fd is None:
            return None
        held, here = os.fstat(fd), os.stat(str(path))
        if (held.st_dev, held.st_ino) != (here.st_dev, here.st_ino):
            os.close(fd)
            return None
        os.set_inheritable(fd, False)
        return fd
    except (OSError, ValueError):
        return None


V2_LIGHT_OFF = ("the person's last prompt as a background read (the read before a write is still made)", "compactions (orders after one)", "inbox unread badge and lapsed-note conversion",
                "stopped/finished summary and stop classification", "landed-checks that page messages (a sent-key ledger answers instead)",
                "report-answered checks", "brief scan")
SENT_KEYS_FILE = "sent-keys.json"
SENT_PER_SESSION, SENT_SESSIONS = 400, 200   # the ledger's bounds
SENT_FILE_MAX = 4 * 1024 * 1024   # a ledger of that many keys is under 2 MB; a bigger file is not one


class SentLedger:
    """v2_light: the keys the daemon itself delivered, per session, kept in `sent-keys.json` (0600, renamed into place, bounded) so a restart
    keeps them. Two writers (the daemon and a CLI `send`) share it: every change is a locked read-modify-write (flock), every read
    sees the file as it is now.

    At most once: a send is `reserve`d BEFORE the write and `confirm`ed after it. A crash between the two leaves the key
    UNKNOWN (`unknown` in the file): it counts as delivered, is never sent again by itself, and `status` says how many there are. A
    send that FAILED (a V2Error) is `release`d: nothing was admitted, or v2 refuses a repeat (the message id is derived from the keys)."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / SENT_KEYS_FILE if state_dir is not None else None
        self.lock = self.path.with_name(SENT_KEYS_FILE + ".lock") if self.path is not None else None

    def load(self):
        """(sent, unknown). A MISSING file is an empty ledger; a file that is there and cannot be read or parsed is not: the ledger
        fails CLOSED (a V2Error, so the callers hold the send and say why), never forgetting what was delivered."""
        if self.path is None:
            return {}, {}
        try:
            with open(str(self.path)) as handle:
                text = handle.read(SENT_FILE_MAX + 1)   # never more than the cap: the file can grow between a stat and a read
            if len(text) > SENT_FILE_MAX:
                raise V2Error("ledger_corrupt", None, "sent-keys.json is larger than any ledger the daemon writes: nothing is sent until it is moved aside")
        except FileNotFoundError:
            return {}, {}
        except OSError as err:
            raise V2Error("ledger_unreadable", None, "sent-keys.json cannot be read (%s): nothing is sent until it can be" % err)
        try:
            body = json.loads(text)
            tables = []
            for name in ("sessions", "unknown"):
                table = {}
                for sid, entries in (body.get(name) or {}).items():
                    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in entries.values()):
                        raise ValueError("a key without a time")
                    table[sid] = {str(k): v for k, v in entries.items()}
                tables.append(table)
            sessions, unknown = tables
        except (ValueError, AttributeError, TypeError):
            raise V2Error("ledger_corrupt", None, "sent-keys.json is not a ledger: nothing is sent until it is moved aside (it is never read as empty)")
        return sessions, unknown

    @property
    def keys(self):
        return self.load()[0]

    def update(self, change):
        """Apply `change(sessions, unknown)` under the lock to what is on disk now, then write it back atomically. A ledger that cannot be
        locked, read or written raises (V2Error): the caller does not send. With no state directory there is nowhere to keep it: that fails closed too."""
        if self.path is None:
            raise V2Error("ledger_unwritable", None, "v2_light needs a state directory for sent-keys.json: nothing is sent without one")
        try:
            fd = os.open(str(self.lock), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a") as guard:
                fcntl.flock(guard, fcntl.LOCK_EX)
                sessions, unknown = self.load()
                change(sessions, unknown)
                # Only `sessions` is trimmed: an unknown-fate reservation is what says "do not send again", so it stays (bounded by SENT_FILE_MAX)
                for mine in sessions.values():
                    if len(mine) > SENT_PER_SESSION:
                        for key, _ in sorted(mine.items(), key=lambda kv: kv[1])[:len(mine) - SENT_PER_SESSION]:
                            del mine[key]
                for sid in [sid for sid, mine in sessions.items() if not mine]:
                    del sessions[sid]
                if len(sessions) > SENT_SESSIONS:
                    keep = sorted(sessions.items(), key=lambda kv: max(kv[1].values() or [0]), reverse=True)[:SENT_SESSIONS]
                    sessions.clear()
                    sessions.update(keep)
                self.save(sessions, unknown)
        except OSError as err:
            raise V2Error("ledger_unwritable", None, "sent-keys.json cannot be written (%s): nothing is sent until it can be" % err)

    def reserve(self, session_id, keys, created):
        """Reserve `keys` unless one is already in the ledger (sent or of unknown fate): True when reserved, False when another sender
        (the daemon, a CLI `send`) already has it. One locked step, so two senders never both write the same key."""
        taken = []

        def change(sessions, unknown):
            have = set(sessions.get(session_id, {})) | set(unknown.get(session_id, {}))
            if have & set(keys):
                taken.append(True)
                return
            unknown.setdefault(session_id, {}).update({key: created for key in keys})
        self.update(change)
        return not taken

    def confirm(self, session_id, keys, created):
        def change(sessions, unknown):
            sessions.setdefault(session_id, {}).update({key: created for key in keys})
            for key in keys:
                unknown.get(session_id, {}).pop(key, None)
        self.update(change)

    def release(self, session_id, keys):
        def change(sessions, unknown):
            for key in keys:
                unknown.get(session_id, {}).pop(key, None)
        self.update(change)

    def known(self, session_id, since):
        sessions, unknown = self.load()
        found = KeyTimes()
        for table in (sessions, unknown):   # an unknown-fate send counts as delivered: at most once
            for key, created in table.get(session_id, {}).items():
                if created >= since:
                    found.add_all([key], created)
        return found

    def unknown_count(self):
        return sum(len(mine) for mine in self.load()[1].values())

    def save(self, sessions, unknown):
        temp = None
        try:
            temp = self.path.with_name("%s.%d.tmp" % (self.path.name, os.getpid()))
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps({"sessions": sessions, "unknown": unknown}, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(str(temp), str(self.path))
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)   # the rename itself must survive a power loss
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception as err:
            if temp is not None:
                try:
                    temp.unlink()
                except OSError:
                    pass
            raise OSError("sent-keys.json could not be written: %s" % err)


class KeyTimes(set):
    """The keys `delivered_keys` found, a plain set, with `at[key]`: when the message that carries it was
    created (the oldest, when several do; None when v2 gave no time). PR 13 reads when a report arrived."""

    def __init__(self):
        super().__init__()
        self.at = {}

    def add_all(self, keys, created):
        for key in keys:
            self.add(key)
            known = self.at.get(key)
            if key not in self.at or (created is not None and (known is None or created < known)):
                self.at[key] = created


STATUS_REREAD_GAP = 5   # seconds: an agent whose state flaps is read again at most this often
COMPACTION_TTL_MAX = 30   # seconds a compaction list is remembered (an orders fact for a compacted agent waits at most this long)
