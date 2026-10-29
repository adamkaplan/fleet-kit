"""The file cache of the ask header (#1520): an issue's Intent, Done-when, state, updated_at, and its parent's number.

Shared by the commands of one state directory. ONE FILE PER ISSUE under `<state>/header-cache/`, named by hashes of the
key (account + repo + issue number), written whole to a temp file and `os.replace`d (no locks: the last writer wins and a
reader sees either whole file), mode 0600 in a 0700 directory, at most MAX_ENTRIES files (the oldest are evicted) of at
most MAX_BYTES each. A missing, corrupt, oversized, unreadable or unwritable cache is ignored: every function here
answers "no entry" or does nothing, and never raises. Only the named fields are stored: no token, no header, no body.

Ages: an OPEN issue is served for at most OPEN_AGE seconds, a CLOSED one for at most CLOSED_AGE (issues are reopened).
This module only holds files; which commands may use it (the CACHE-ALLOWED set) and the allowed-repo and account checks
that run before it are the reader's (bin/fleet-switchboard, IntentReader). Loaded by path, lazily.
"""
import hashlib
import json
import os
import time

OPEN_AGE = 600            # seconds: an open issue
CLOSED_AGE = 24 * 3600    # seconds: a closed issue
MAX_ENTRIES = 2000
MAX_BYTES = 8192
DIR_NAME = "header-cache"
ISSUE_FIELDS = ("number", "state", "intent", "done_when", "updated_at", "charter", "pull_request", "workspace")
VERSION = 1


def enabled(config):
    """The setting `header_cache`, read at call time: anything but off/false/0 leaves the cache on."""
    value = config.get("header_cache") if isinstance(config, dict) else None
    if isinstance(value, str):
        return value.strip().lower() not in ("off", "false", "0", "no")
    return value is not False


def _h(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:20]


def _dir(state):
    return os.path.join(str(state), DIR_NAME)


def _name(account, repo, number):
    return "%s.%s.json" % (_h("%s#%s" % (str(repo).lower(), number)), _h(str(account)))


def age_for(state):
    return CLOSED_AGE if state == "closed" else OPEN_AGE


def _read(path):
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) and data.get("v") == VERSION else None
    except (OSError, ValueError, UnicodeError):
        return None


def load(state, account, repo, number, part, now, max_age=None):
    """The stored `part` ("issue" or "parent") of one key, a dict, or None when there is none, it is too old, or it is not for this key."""
    try:
        data = _read(os.path.join(_dir(state), _name(account, repo, number)))
        if data is None or data.get("account") != str(account) or str(data.get("repo", "")).lower() != str(repo).lower() or data.get("number") != number:
            return None
        found = data.get(part)
        if not isinstance(found, dict) or not isinstance(found.get("at"), (int, float)):
            return None
        issue = data.get("issue") if isinstance(data.get("issue"), dict) else {}
        age = age_for(issue.get("state")) if max_age is None else max_age
        if not 0 <= now - found["at"] < age:   # a stamp in the future is not trusted either
            return None
        return found
    except Exception:
        return None


def store(state, account, repo, number, part, value, now):
    """Keep `part` ("issue": the ISSUE_FIELDS of `value`; "parent": {"parent": number or None}) of one key. Best effort; True when written."""
    try:
        path_dir = _dir(state)
        os.makedirs(path_dir, mode=0o700, exist_ok=True)
        os.chmod(path_dir, 0o700)
        path = os.path.join(path_dir, _name(account, repo, number))
        data = _read(path)
        if data is None or data.get("account") != str(account) or data.get("number") != number:
            data = {}
        data.update({"v": VERSION, "account": str(account), "repo": str(repo), "number": number})
        if part == "issue":
            data["issue"] = dict({k: value.get(k) for k in ISSUE_FIELDS}, at=now)
        elif part == "parent":
            data["parent"] = {"parent": value.get("parent") if isinstance(value.get("parent"), int) else None, "at": now}
        else:
            return False
        text = json.dumps(data, sort_keys=True)
        if len(text) > MAX_BYTES:
            return False
        tmp = "%s.%d.%s.tmp" % (path, os.getpid(), _h(repr(time.time())))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _evict(path_dir)
        return True
    except Exception:
        return False


def _evict(path_dir):
    """Keep at most MAX_ENTRIES files: the oldest (by modification time) go first; stray temp files older than a minute go too."""
    try:
        names = os.listdir(path_dir)
        if len(names) <= MAX_ENTRIES:
            return
        stats = []
        for name in names:
            try:
                stats.append((os.stat(os.path.join(path_dir, name)).st_mtime, name))
            except OSError:
                pass
        stats.sort()
        for _, name in stats[:max(0, len(stats) - MAX_ENTRIES)]:
            try:
                os.unlink(os.path.join(path_dir, name))
            except OSError:
                pass
    except Exception:
        pass


def drop(state, repo, number):
    """Delete the entries of this issue of every account (the daemon's event, `--fresh`, a write to the issue). Never raises; the count dropped."""
    dropped = 0
    try:
        prefix = _h("%s#%s" % (str(repo).lower(), number)) + "."
        for name in os.listdir(_dir(state)):
            if name.startswith(prefix):
                try:
                    os.unlink(os.path.join(_dir(state), name))
                    dropped += 1
                except OSError:
                    pass
    except Exception:
        pass
    return dropped
