"""The read of a retired cloud worker (#1546): `read NAME` after `retire` answers from the worker's run record, and the boss kept at the removal.

A part of bin/cloud_worker.py (the one that loads it and calls it); it holds the code that file needs only after a worker is gone, so that file stays within its size row.
`bind(deps)` hands it the names of the cloud worker module it works with (the file helpers, the launcher library, the caller rule) and the
switchboard's own namespace. It reads files only: nothing is written on a read, no cluster is called and no path of a run record is printed. What it writes
is ONE small private file per worker at a removal (`keep_boss`): who the boss was, the end time and the worker, so that only the operator, the Chief of
Staff and that boss can read the worker's last text afterwards. The scrub of the text works by shape (the launcher's one scrub function) and flattens
non-ASCII characters to `?`.
"""
import datetime
import json
import os
import re
import stat
import time
from pathlib import Path

D = {}   # what the cloud worker module hands over, set by bind()

BOSS_DIR = "cloud-boss"       # beside the cloud directory, never inside it: <state>/cloud-boss/<worker>.json
BOSS_MAX_BYTES = 1024         # one kept boss file
BOSS_MAX_FILES = 500
BOSS_KEEP_SECONDS = 30 * 86400
TEMP_KEEP_SECONDS = 86400     # a leftover `<worker>.json.<pid>.tmp` of a write that died
READ_RETIRED_CHARS = 4000
TRANSCRIPT_READ_MAX = 6 * 1024 * 1024
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.ASCII)
_DIR_STAMP_RE = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z", re.ASCII)


def bind(deps):
    D.clear()
    D.update(deps)


def _name_ok(name):
    """A worker name, exactly: `fullmatch`, so the trailing newline that a `$` lets through is refused."""
    return isinstance(name, str) and bool(D["NAME_RE"].fullmatch(name))


def _stamp_seconds(text):
    """The seconds of a stamp `YYYY-MM-DDTHH:MM:SSZ` of ASCII digits only (strptime alone accepts Unicode digits), or None."""
    if not isinstance(text, str) or not _STAMP_RE.fullmatch(text):
        return None
    try:
        return datetime.datetime.strptime(text, _STAMP).replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def _drop(path):
    try:
        path.unlink()
    except OSError:
        pass


def keep_boss(state, record, clock=None):
    """Keep who the boss of a worker was when its record goes: ONE small file, 0600, written atomically, holding the worker, the boss and the end time and
    nothing else; a record with no boss deletes the file an earlier run of the name left. Expired files (30 days), the oldest beyond 500 and the leftovers of
    a dead write are removed HERE, at a write, and nowhere else. It may raise (a directory that cannot be made, a full disk): its one caller,
    `_keep_boss` of bin/cloud_worker.py, catches it, because a boss that cannot be kept is a worker only the operator and the Chief of Staff can read,
    and the removal goes on exactly as before."""
    name, boss = record.get("worker"), record.get("reports_to")
    if not _name_ok(name):
        return
    now = time.time() if clock is None else clock
    base = Path(state) / BOSS_DIR
    target = base / (name + ".json")
    if not isinstance(boss, str) or not boss:   # a run with no boss: the boss of an earlier run of this name must not outlive it
        if not base.is_symlink():
            try:
                target.unlink()
            except FileNotFoundError:
                pass
        return
    base.mkdir(parents=True, mode=0o700, exist_ok=True)
    if base.is_symlink():
        return
    D["_write_private"](target, json.dumps({"boss": boss[:100], "ended": time.strftime(_STAMP, time.gmtime(now)), "worker": name}, sort_keys=True) + "\n")
    kept = []
    for path in base.glob("*.json*"):
        if not path.name.endswith(".json"):   # `<worker>.json.<pid>.tmp`: a leftover, once a day old
            try:
                if path.lstat().st_mtime < now - TEMP_KEEP_SECONDS:
                    _drop(path)
            except OSError:
                pass
            continue
        found, _ = D["read_record_file"](path, BOSS_MAX_BYTES)
        when = _stamp_seconds(found.get("ended")) if found else None
        if when is None or when < now - BOSS_KEEP_SECONDS:
            _drop(path)
        else:
            kept.append((when, path.name, path))
    for _, _, path in sorted(kept)[:max(0, len(kept) - BOSS_MAX_FILES)]:
        _drop(path)


def kept_boss(state, name):
    """(boss, end time text) kept for `name`, or (None, None): none, unreadable, not ours or not private (the owner and mode check of the record's own reader: a
    mistake-guard, not a defence against another process of the same user), or older than 30 days all read as no boss on record. Deletes nothing."""
    if not _name_ok(name):
        return None, None
    try:
        found = D["parse_bounded"](D["_read_private"](Path(state) / BOSS_DIR / (name + ".json"), BOSS_MAX_BYTES))
    except (OSError, ValueError):
        return None, None
    if not isinstance(found, dict) or found.get("worker") != name or not isinstance(found.get("boss"), str) or not found["boss"]:
        return None, None
    when = _stamp_seconds(found.get("ended"))
    if when is None or when < time.time() - BOSS_KEEP_SECONDS:
        return None, None
    return found["boss"], found["ended"]


def _runs_root():
    return Path(os.environ.get("CW_RUN_RECORD_DIR") or D["_lib"]().run_record.default_root(os.environ))


def retired_run(name):
    """(directory, stamp, own) of the newest run record of `name`: the latest end, ties by directory name (the later wins); a record without a usable
    `end` is dated by the time in its directory name (own is False then). None when there is none or the name is not exactly a name. Read only."""
    if not _name_ok(name):
        return None
    pattern = re.compile(re.escape(name) + r"-(\d{8}T\d{6}Z)-[0-9a-f]{6}", re.ASCII)
    best = None
    try:
        entries = list(os.scandir(str(_runs_root())))
    except OSError:
        return None
    for entry in entries:
        found = pattern.fullmatch(entry.name)
        if not found or entry.is_symlink() or not entry.is_dir():
            continue
        info, _ = D["read_record_file"](Path(entry.path) / "run.json", 65536)
        stamp = info.get("end") if isinstance(info, dict) else None
        own = _stamp_seconds(stamp) is not None
        if not own:
            parts = _DIR_STAMP_RE.fullmatch(found.group(1))
            stamp = "%s-%s-%sT%s:%s:%sZ" % parts.groups()
            if _stamp_seconds(stamp) is None:
                stamp = ""
        key = (stamp, entry.name)
        if best is None or key > best[0]:
            best = (key, entry.path, own)
    return None if best is None else (best[1], best[0][0], best[2])


def _last_worker_text(directory):
    """The last text of the worker in transcript.jsonl, or (None, why). Never raises."""
    path = Path(directory) / "transcript.jsonl"
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None, "the run record holds no transcript"
    except OSError:
        return None, "the transcript of the run record cannot be read"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "the transcript of the run record cannot be read"
        raw = b""
        while len(raw) <= TRANSCRIPT_READ_MAX:
            chunk = os.read(fd, TRANSCRIPT_READ_MAX + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    except OSError:
        return None, "the transcript of the run record cannot be read"
    finally:
        os.close(fd)
    if len(raw) > TRANSCRIPT_READ_MAX:
        return None, "the transcript of the run record is too large to read"
    found = text = None
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            message = D["parse_bounded"](line)
        except (ValueError, D["TooDeep"]):
            continue
        if isinstance(message, dict) and message.get("type", message.get("role")) == "assistant":
            text = D["_message_text"](message).strip()
            if text:
                found = text
    if found is None:
        return None, "the transcript of the run record holds no text from the worker"
    return found, None


def read_retired(name, retired, state, runner, now):
    """The last text of a retired cloud worker, from its run record: only for the callers `retire` allows (the operator, the Chief of Staff, the boss kept
    at its removal, and only when the kept file is not older than the run it would speak for), scrubbed before and after the cap."""
    LaunchError = D["LaunchError"]
    kind = D["gates_caller_kind"](runner, now)
    directory, stamp, own = retired
    boss, boss_end = kept_boss(state, name)
    run_seconds, boss_seconds = _stamp_seconds(stamp), _stamp_seconds(boss_end)
    if boss is not None and (run_seconds is None or boss_seconds is None or boss_seconds < run_seconds):
        boss = None   # a boss kept for an earlier run is not the boss of this one
    if not D["_may_retire"](kind, {"reports_to": boss}):
        if boss is None and kind[0] == "agent":
            raise LaunchError("refused: %s was removed with no boss on record: only the operator or the Chief of Staff may read it" % name)
        raise LaunchError("refused: only the operator, the Chief of Staff or %s's own boss may read it" % name)
    text, why = _last_worker_text(directory)
    if text is None:
        raise LaunchError("%s: %s" % (name, why))
    scrub = D["_scrub_lib"]()
    cut = max(0, len(text) - READ_RETIRED_CHARS)
    text = scrub(text, (), 2000000)
    text = scrub(text[:READ_RETIRED_CHARS], (), READ_RETIRED_CHARS)
    print("[%s: the text of the record of a retired run%s]" % (name, (" that ended " if own else " dated ") + stamp if stamp else ""))
    print(text)
    if cut:
        print("[%d characters cut]" % cut)
    return D["EXIT_OK"]
