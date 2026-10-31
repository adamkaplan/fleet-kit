# ------------------------------------------------------------------ the caches

class BoundedFileCache:
    """A small JSON file of {key: {at, value}}: bounded, expiring, private, replaceable.

    It is a cache and nothing else: deleting it loses only speed (invariant 8).
    Written atomically (a temp file, mode 0600, then rename); a missing,
    oversized or corrupt file reads as empty; a write that fails is not an error.
    A ttl of 0 turns it off.
    """

    def __init__(self, path, now, ttl, limit):
        self.path, self.now, self.ttl, self.limit = Path(path), now, ttl, limit

    def _fresh(self):
        if not self.ttl:
            return {}
        try:
            if self.path.stat().st_size > POLICY_CACHE_BYTES:
                return {}
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return {}
        return {key: entry for key, entry in entries.items()
                if isinstance(entry, dict) and _is_number(entry.get("at")) and self.now() - entry["at"] < self.ttl
                and self.now() >= entry["at"]}

    def get(self, key):
        entry = self._fresh().get(key)
        return entry["value"] if entry else None

    def put(self, key, value):
        """Store one entry, dropping the oldest beyond the limit. True when it was written."""
        if not self.ttl:
            return False
        entries = self._fresh()
        held = entries.get(key)
        if held is not None and held.get("value") == value and self.now() - held["at"] < self.ttl / 2:
            return True   # the same answer, still young: rewriting the whole file to move its time would only touch the disk
        entries[key] = {"at": self.now(), "value": value}
        for old in sorted(entries, key=lambda k: entries[k]["at"])[:max(0, len(entries) - self.limit)]:
            del entries[old]
        temp = self.path.with_name("%s.%d.tmp" % (self.path.name, os.getpid()))
        try:
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps({"entries": entries}, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp), str(self.path))
            return True
        except OSError:
            try:
                os.unlink(str(temp))
            except OSError:
                pass
            return False


def _valid_context(value):
    keys = ("issue", "intent", "done_when", "authority")
    ok = isinstance(value, dict) and all(k in value for k in keys) \
        and all(value[k] is None or isinstance(value[k], str) for k in keys[1:]) \
        and (value["issue"] is None or _is_issue_number(value["issue"]))
    if not ok:
        return None
    context = {k: value[k] for k in keys}
    # PR 17: the charter and its orders ride along (an older cache entry has neither: they read as none)
    orders = value.get("orders")
    context["charter"] = value.get("charter") if _is_issue_number(value.get("charter")) else None
    context["charter_repo"] = value["charter_repo"] if isinstance(value.get("charter_repo"), str) \
        and REPO_RE.match(value["charter_repo"]) else None
    context["orders"] = [t for t in orders if isinstance(t, str)] if isinstance(orders, list) else []
    return context


POLICY_AUDIT_TAIL = 8 << 20         # bytes of the audit log's end that a report reads


def _audit_tail(state_dir):
    """The parsed entries in the last POLICY_AUDIT_TAIL bytes of audit.jsonl (a torn first line is dropped)."""
    path = Path(state_dir) / "audit.jsonl"
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - POLICY_AUDIT_TAIL))
            data = handle.read()
            torn = size > POLICY_AUDIT_TAIL
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    out = []
    for number, line in enumerate(lines):
        if number == 0 and torn:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out
