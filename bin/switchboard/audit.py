# ----------------------------------------------------------------- the audit

SENSITIVE = ("token", "key", "password", "secret", "authorization")
REDACTED = "[redacted]"


def _sensitive(name):
    lowered = str(name).lower()
    # `keys` is the list of fact names a message covers (docs/switchboard.md
    # §Data), such as `worker.idle:<session>:<time>`. It is the one thing the
    # audit must show about a delivery, and it never holds a credential.
    if lowered == "keys":
        return False
    return any(word in lowered for word in SENSITIVE)


def redact(value):
    """A copy of `value` with every credential-like key's value replaced."""
    if isinstance(value, dict):
        return {k: (REDACTED if _sensitive(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


AUDIT_ROTATE_BYTES = 20 << 20    # the active audit file is rotated when an append finds it larger than this
AUDIT_KEEP_SECONDS = 86400       # ... keeping the last day in the active file (and never more than three quarters of AUDIT_ROTATE_BYTES)
AUDIT_ARCHIVE_DAYS = 14          # archives (audit-<UTC date>.jsonl.gz) older than this are deleted
AUDIT_SYNC_SECONDS = 1.0         # fsync of a non-intent append at most this often (an `intent` is always synced: it must be on disk before the write it announces)
_AUDIT_TS = re.compile(rb'"ts": "([^"]+)"')


class AuditLog:
    """Append-only JSON lines. Never read by a decision; written before and after.

    Appenders open the file per write (O_APPEND) under a shared lock; a rotation archives the old part to
    `audit-<UTC date>.jsonl.gz` (0600, written to a temp file and renamed), then takes the lock exclusively for the swap only, carries what was
    appended meanwhile into the new active file and replaces the old one: no appended line is lost. An `intent` entry is fsynced before the write it announces; the
    others at most once a second (a process crash loses nothing either way: the data is in the OS by `write`)."""

    _synced = {}   # path -> monotonic time of the last fsync, shared by every AuditLog of the process
    _failed = {}   # path -> (error text, monotonic time) of the last rotation failure said

    def __init__(self, path):
        self.path = Path(path)

    def append(self, actor, event, subject=None, detail=None, **extra):
        entry = {"ts": utc_now(), "actor": actor, "event": event,
                 "subject": subject, "detail": redact(detail)}
        entry.update(redact(extra))
        line = json.dumps(entry, sort_keys=True, default=str) + "\n"
        size = 0
        with self._append_lock(fcntl.LOCK_SH):   # a rotation holds this exclusively for its swap only: no line is written to a file that is being replaced
            fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as handle:
                handle.write(line)
                handle.flush()
                size = os.fstat(handle.fileno()).st_size
                key, clock = str(self.path), time.monotonic()
                if extra.get("phase") == "intent" or clock - AuditLog._synced.get(key, -1e9) >= AUDIT_SYNC_SECONDS:
                    os.fsync(handle.fileno())
                    AuditLog._synced[key] = clock
        if size > AUDIT_ROTATE_BYTES:
            try:
                self.rotate()
            except Exception as err:   # a rotation that fails is tried again at a later append and never fails this one; said once per change of error
                self._say_rotation_failed(err)
        return entry

    @contextmanager
    def _append_lock(self, mode):
        """A flock on `audit.jsonl.append.lock` (shared for an append, exclusive for the swap). If the lock file cannot be had the write goes ahead without it."""
        try:
            fd = os.open(str(self.path.with_name(self.path.name + ".append.lock")), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            yield
            return
        with os.fdopen(fd, "w") as guard:
            try:
                fcntl.flock(guard, mode)
            except OSError:
                pass
            yield

    def _say_rotation_failed(self, err):
        text = "%s: %s" % (type(err).__name__, _one_line(str(err), 120))
        key, clock = str(self.path), time.monotonic()
        said = AuditLog._failed.get(key)
        if said is not None and said[0] == text and clock - said[1] < 3600:
            return
        AuditLog._failed[key] = (text, clock)
        sys.stderr.write("%s: the audit log could not be rotated (%s); it keeps growing until it can\n" % (PROG, text))
        try:
            self.append(PROG, "audit.rotate_failed", None, {"error": text})
        except Exception:
            pass

    def rotate(self, now=None):
        """Archive the old part of the active file and keep the recent part. One rotator at a time (a lock file); another process that finds it
        held skips. Returns the archive path, or None when there was nothing to do."""
        now = time.time() if now is None else now
        lock_path = self.path.with_name(self.path.name + ".rotate.lock")
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return None
            archive = self._rotate_locked(now)
            pruned = self._prune_archives(now)
            if archive is not None or pruned:
                self.append(PROG, "audit.rotated", None, {"archive": archive.name if archive else None, "pruned": pruned})
            return archive

    def _rotate_locked(self, now):
        cutoff = datetime.fromtimestamp(now - AUDIT_KEEP_SECONDS, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            seen = self.path.stat().st_size
        except OSError:
            return None
        if seen <= AUDIT_ROTATE_BYTES:
            return None   # another process rotated first
        floor = max(0, seen - AUDIT_ROTATE_BYTES * 3 // 4)   # never keep more than three quarters of the threshold (room to grow before the next rotation), whatever the clock says
        stamp = datetime.fromtimestamp(now, timezone.utc)
        archive = self.path.with_name("audit-%s.jsonl.gz" % stamp.strftime("%Y-%m-%d"))
        if archive.exists():
            archive = self.path.with_name("audit-%s.jsonl.gz" % stamp.strftime("%Y-%m-%dT%H%M%S"))
        keep_temp = self.path.with_name("%s.%d.keep.tmp" % (self.path.name, os.getpid()))
        placed = False
        gz_temp = archive.with_name("%s.%d.tmp" % (archive.name, os.getpid()))
        try:
            offset, archived = 0, 0
            with open(str(self.path), "rb") as source:
                with os.fdopen(os.open(str(gz_temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "wb") as raw, \
                        gzip.GzipFile(fileobj=raw, mode="wb", filename="") as packed, \
                        os.fdopen(os.open(str(keep_temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "wb") as kept:
                    keeping = False
                    for line in source:
                        if offset >= seen:
                            break
                        if not keeping:
                            found = _AUDIT_TS.search(line)
                            stamp_text = found.group(1).decode("ascii", "replace")[:19] if found else ""
                            keeping = offset >= floor and (stamp_text >= cutoff or not found)
                        offset += len(line)
                        if keeping:
                            kept.write(line)
                        else:
                            packed.write(line)
                            archived += 1
                    raw.flush()
                    os.fsync(raw.fileno())
                    kept.flush()
                    os.fsync(kept.fileno())
            if not archived:
                os.unlink(str(gz_temp))
                os.unlink(str(keep_temp))
                return None
            os.rename(str(gz_temp), str(archive))
            placed = True
            self._swap(keep_temp, offset)
            return archive
        except BaseException:
            for temp in (gz_temp, keep_temp, archive if placed else None):   # a swap that failed leaves the log whole: its archive would be a duplicate of it
                try:
                    if temp is not None:
                        os.unlink(str(temp))
                except OSError:
                    pass
            raise

    def _swap(self, keep_temp, offset):
        """Carry what was appended after `offset` into the kept part, then put it in place of the active file."""
        with self._append_lock(fcntl.LOCK_EX):   # no appender is mid-write while the tail is copied and the file replaced
            with open(str(self.path), "rb") as source, open(str(keep_temp), "ab") as kept:
                source.seek(offset)
                while True:
                    chunk = source.read(1 << 20)
                    if not chunk:
                        break
                    kept.write(chunk)
                kept.flush()
                os.fsync(kept.fileno())
            os.replace(str(keep_temp), str(self.path))

    def _prune_archives(self, now):
        """Delete the archives older than AUDIT_ARCHIVE_DAYS; the names of the ones deleted."""
        horizon = now - AUDIT_ARCHIVE_DAYS * 86400
        gone = []
        for path in sorted(self.path.parent.glob("audit-*.jsonl.gz")):
            try:
                if path.stat().st_mtime < horizon:
                    path.unlink()
                    gone.append(path.name)
            except OSError:
                pass
        return gone

    @contextmanager
    def external(self, actor, event, subject=None, detail=None):
        """Audit one write to another system: `intent` before, `result` after.

        Yields a dict the caller may fill with what came back; it becomes the
        result entry's detail. An exception is recorded, then re-raised. A
        SimulatedCrash is re-raised with nothing recorded, as a real crash.
        """
        call = uuid.uuid4().hex[:12]
        self.append(actor, event, subject, detail, phase="intent", call=call)
        outcome = {}
        try:
            yield outcome
        except SimulatedCrash:
            raise
        except BaseException as err:
            self.append(actor, event, subject, outcome or None, phase="result", call=call,
                        ok=False, error="%s: %s" % (type(err).__name__, err))
            raise
        self.append(actor, event, subject, outcome or None, phase="result", call=call, ok=True)
