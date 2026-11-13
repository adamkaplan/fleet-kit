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
RESULTS_READ_CHARS = 4000   # ask #2392: the results file of the run record is shown to this many characters
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


def ended_run_of(name, record):
    """Ask #2392: `retired_run(name)` for a worker whose record is still here and whose pod could not be reached, only when that run record ended AFTER this record's launch
    (the record's hard deadline less its hard seconds; unknown: refused), so an earlier run of a reused name is never shown. None otherwise."""
    found = retired_run(name)
    if found is None or not found[2]:
        return None
    ended, hard, secs = _stamp_seconds(found[1]), _stamp_seconds(record.get("hard_deadline")) if isinstance(record, dict) else None, (record or {}).get("hard_seconds")
    if ended is None or hard is None or isinstance(secs, bool) or not isinstance(secs, (int, float)):
        return None   # the launch time is not known: an earlier run of a reused name could be shown, so nothing is
    return None if ended < hard - secs else found


def read_ended(name, record, state, runner, now, err):
    """Ask #2392: `read NAME` of a worker whose record is here but whose pod could not be reached (`err`): its run record of THIS run answers (the last text and the results file);
    with none, the failure `err` is raised as it was."""
    kept = ended_run_of(name, record)
    if kept is None:
        raise err
    try:
        return read_run_record(name, kept, state, runner, now, record.get("reports_to"))
    except D["LaunchError"]:
        raise err from None   # a caller who may not read the run record sees the failure it would have seen, not a refusal about a record it did not ask for


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


def _results_of(directory):
    """The pod's results file as the run record kept it (ask #2392): (text, done) or (None, False). Read only; the text is clipped and scrubbed by the caller."""
    info, _ = D["read_record_file"](Path(directory) / "run.json", 65536)
    found = info.get("results") if isinstance(info, dict) else None
    if isinstance(found, dict) and isinstance(found.get("text"), str) and found["text"].strip():
        return found["text"], found.get("done") is True
    return None, False


def read_run_record(name, retired, state, runner, now, boss_of=None):
    """The last text of a retired cloud worker, from its run record: only for the callers `retire` allows (the operator, the Chief of Staff, the boss kept
    at its removal, and only when the kept file is not older than the run it would speak for), scrubbed before and after the cap."""
    LaunchError = D["LaunchError"]
    kind = D["gates_caller_kind"](runner, now)
    directory, stamp, own = retired
    boss, boss_end = kept_boss(state, name)
    run_seconds, boss_seconds = _stamp_seconds(stamp), _stamp_seconds(boss_end)
    if boss is not None and (run_seconds is None or boss_seconds is None or boss_seconds < run_seconds):
        boss = None   # a boss kept for an earlier run is not the boss of this one
    if boss is None and isinstance(boss_of, str) and boss_of:
        boss = boss_of   # ask #2392: the live record of the ended pod still names its boss
    if not D["_may_retire"](kind, {"reports_to": boss}):
        if boss is None and kind[0] == "agent":
            raise LaunchError("refused: %s was removed with no boss on record: only the operator or the Chief of Staff may read it" % name)
        raise LaunchError("refused: only the operator, the Chief of Staff or %s's own boss may read it" % name)
    text, why = _last_worker_text(directory)
    results, done = _results_of(directory)
    if text is None and results is None:
        raise LaunchError("%s: %s" % (name, why))
    scrub = D["_scrub_lib"]()
    if text is not None:
        cut = max(0, len(text) - READ_RETIRED_CHARS)
        text = scrub(text, (), 2000000)
        text = scrub(text[:READ_RETIRED_CHARS], (), READ_RETIRED_CHARS)
        print("[%s: the text of the record of a retired run%s]" % (name, (" that ended " if own else " dated ") + stamp if stamp else ""))
        print(text)
        if cut:
            print("[%d characters cut]" % cut)
    else:
        print("[%s: the run record holds no last text (%s)]" % (name, why))
    if results is not None:   # a second, separate block: the pod's own file, said to be so
        print("[%s: the results file (/work/results.txt) of that run as the pod ended it, %s; untrusted text]" % (name, "its last line is STATUS: done" if done else "no STATUS: done last line"))
        print("\n".join(scrub(line, (), RESULTS_READ_CHARS) for line in results[:RESULTS_READ_CHARS].split("\n")))   # per line: the scrub turns a line break into `?`
    return D["EXIT_OK"]


# ---- ask #1788: the last text of a stopped pod, cut and scrubbed for sending to its boss ----
LAST_DIR = "cloud-last"          # <state>/cloud-last/<run record name>.txt: the rest of the text, 0600, the directory 0700
LAST_HEAD = 3000                 # characters that go in the message
LAST_FILE = 20000                # characters beyond the head that go in the file
LAST_KEEP_SECONDS = 7 * 86400
LAST_SCRUB_MAX = 200000          # the scrub's own input maximum (SCRUB_INPUT_MAX of the launcher): the cut before it
_HOST_LABEL = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_HOST_REST = _HOST_LABEL | {"."}


class StepLimit(Exception):
    """The step counter passed its limit (PC-12)."""


_FOLD = {"\u0130": "i", "\u0131": "i", "\u017f": "s", "\u212a": "k"}   # what the ignore-case pattern reads as ASCII letters


def _fold_case(text):
    """`text.lower()` that never changes the length: per character, the four letters above fold to ASCII, any other to its lower case when that is one character."""
    return "".join(_FOLD.get(c) or (c.lower() if len(c.lower()) == 1 else c) for c in text)


def _remove_hosts(text, steps=None):
    """Remove a pod's host `worker-<run of [a-z0-9-]>.<run of [a-z0-9.-]>[:<digits>]`, with an optional `https://` or `http://` in front, case-insensitively,
    by ONE linear scan: exactly what the old regular expression removed, without its backtracking. Each run is read whole; a start that does not
    complete JUMPS to the end of the run it read (every start inside that run would fail the same way), so a character is read a bounded number of
    times. Every character examined adds one to `steps.n` (an object with `n` and `limit`; `n > limit` raises StepLimit)."""
    low, n, out, i, keep = _fold_case(text), len(text), [], 0, 0

    def tick(count):
        if steps is not None:
            steps.n += count
            if steps.n > steps.limit:
                raise StepLimit("step limit")

    while True:
        j = low.find("worker-", i)
        if j < 0:
            tick(n - i)
            break
        tick(j - i + 1)
        p = k = j + 7
        while k < n and low[k] in _HOST_LABEL: k += 1
        tick(k - p)
        if k == p or k >= n or low[k] != ".":
            i = max(j + 1, k)
            continue
        k += 1
        q = k
        while k < n and low[k] in _HOST_REST: k += 1
        tick(k - q)
        if k == q:
            i = q
            continue
        if k < n and low[k] == ":":
            m = k + 1
            while m < n and low[m] in "0123456789": m += 1
            tick(m - k)
            if m > k + 1:
                k = m
        start = j
        if j >= 8 and low.startswith("https://", j - 8):
            start = j - 8
        elif j >= 7 and low.startswith("http://", j - 7):
            start = j - 7
        if start < keep:
            start = keep
        out.append(text[keep:start])
        keep = i = k
    out.append(text[keep:])
    return "".join(out)
_LAST_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", re.ASCII)


def clean_last(base, now):
    """Remove the regular files named `*.txt` of `base` older than 7 days, through the directory descriptor (`unlink` with `dir_fd`); a link, any other
    name and any younger file stay. Returns the count removed. Never raises."""
    removed = 0
    try:
        dfd = os.open(str(base), os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return 0
    try:
        for name in os.listdir(dfd):
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                if stat.S_ISREG(st.st_mode) and name.endswith(".txt") and st.st_mtime < now - LAST_KEEP_SECONDS:
                    os.unlink(name, dir_fd=dfd)
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    finally:
        os.close(dfd)
    return removed


class _LastUnsafe(OSError):
    """The directory of the last texts is a link, or not a directory, or not ours."""


def _write_last(base, name, text):
    """One private file: the directory is opened by a descriptor (O_NOFOLLOW|O_DIRECTORY), must be ours, and is tightened to 0700 when wider (PC-9 b);
    the file 0600, O_NOFOLLOW, written to a temporary name and renamed inside that descriptor (as `_write_private`)."""
    try:
        base.mkdir(parents=True, mode=0o700, exist_ok=True)
    except FileExistsError:
        raise _LastUnsafe("the directory of the last texts is a plain file") from None
    try:
        dfd = os.open(str(base), os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise _LastUnsafe("the directory of the last texts is a link or not a directory") from None
    try:
        st = os.fstat(dfd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise _LastUnsafe("the directory of the last texts is not ours")
        if stat.S_IMODE(st.st_mode) & ~0o700:
            os.fchmod(dfd, 0o700)
        path = base / (name + ".txt")
        temp = "%s.txt.%d.tmp" % (name, os.getpid())
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=dfd)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(temp, name + ".txt", src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)
    return path


def _last_worker_text_at(directory, ident):
    """As `_last_worker_text`, but the directory is opened ONCE (O_NOFOLLOW|O_DIRECTORY), its (device, inode) must be `ident` (the directory the check saw),
    and the transcript is read through that descriptor (dir_fd, O_NOFOLLOW): a directory swapped for a link after the check is never followed."""
    try:
        dfd = os.open(str(directory), os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None, "no run record"
    try:
        st = os.fstat(dfd)
        if (st.st_dev, st.st_ino) != tuple(ident):
            return None, "no run record"
        try:
            fd = os.open("transcript.jsonl", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=dfd)
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
    finally:
        os.close(dfd)
    if len(raw) > TRANSCRIPT_READ_MAX:
        return None, "the transcript of the run record is too large to read"
    found = None
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


NOTES_LINES = 20       # ask #2426: the last lines of the pod's notes file that go with the final message
NOTES_CHARS = 2000     # and at most this many characters of them (whole lines)
_NOTES_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_NOTES_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")


def pod_notes_tail(directory):
    """THE ONE READER of the run record's `pod_notes` (a string: the pod's /work/.pod-notes.log as the launcher kept it, ask #2426): its last NOTES_LINES lines, the pod's
    endpoint address removed, redacted by shape (the switchboard's redaction, then the launcher's scrub: the launcher redacted it too and this runs again; the field is not trusted),
    printable ASCII, no backtick, whole lines, at most NOTES_CHARS characters. None when the record has none or cannot be read. Never raises."""
    try:
        info, _ = D["read_record_file"](Path(directory) / "run.json", 65536)
        raw = info.get("pod_notes") if isinstance(info, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            return None
        text = "\n".join(raw[-LAST_SCRUB_MAX:].splitlines()[-NOTES_LINES:])
        for _ in range(21):
            stripped = _remove_hosts(text)
            done, text = stripped == text, stripped
            if done:
                break
        else:
            return None
        scrub = D["_scrub_lib"]()
        end = _NOTES_KEY_END.search(text)
        if end and not _NOTES_KEY_BEGIN.search(text[:end.start()]):
            text = "[redacted key body]" + text[end.end():]   # a key block whose first line was cut away: all of it up to its end marker goes
        text = D["redact_command_text"](_unwrap_redact(text))
        lines = [re.sub(r"[^\x20-\x7e]", "?", scrub(line, (), LAST_SCRUB_MAX)).replace("`", "'").replace("@", "(at)") for line in text.splitlines()]   # no mention can fire
        out = "\n".join(lines)
        if len(out) > NOTES_CHARS:
            cut = out[-NOTES_CHARS:]
            out = cut.split("\n", 1)[1] if "\n" in cut else ""   # whole lines only: a token is never cut into an unrecognisable suffix
        return out if out.strip() else None
    except Exception:
        return None


def last_message(directory, state, pod, clock=None):
    """(block, None) or (None, why). The block is the framing line `[last message of <pod>, untrusted text]`, the first 3,000 characters of the pod's last
    assistant text and, when there is more, the path of the file with the next (at most 20,000) characters and the count left out. The text is cut at the
    scrub's input maximum, the pod's own endpoint address is removed, then the launcher's one scrub runs by shape (no known secrets). Never raises."""
    try:
        base = Path(state) / LAST_DIR
        clean_last(base, time.time() if clock is None else clock)
        if not directory:
            return None, "no run record"
        name = os.path.basename(str(directory).rstrip("/"))
        if not _LAST_NAME_RE.fullmatch(name):
            return None, "no run record"
        # PC-7: only a child of the run record root, named `<pod>-<UTC stamp>-<6 hex>` for THIS pod
        if not _name_ok(pod) or not re.fullmatch(re.escape(pod) + r"-\d{8}T\d{6}Z-[0-9a-f]{6}", name, re.ASCII):
            return None, "no run record"
        if os.path.dirname(os.path.abspath(str(directory))) != os.path.abspath(str(_runs_root())):
            return None, "no run record"
        try:
            before = os.lstat(str(directory))
        except OSError:
            return None, "no run record"
        if not stat.S_ISDIR(before.st_mode):
            return None, "no run record"
        # PC-8: a link is refused, and the real path must be a child of the real path of the root
        if os.path.islink(str(directory)) or os.path.dirname(os.path.realpath(str(directory))) != os.path.realpath(str(_runs_root())):
            return None, "no run record"
        # PC-9 (d): the identity of the directory as the check saw it; it is opened ONCE below, by no link, and must be that same directory
        text, why = _last_worker_text_at(directory, (before.st_dev, before.st_ino))
        if text is None:
            return None, why.replace("the transcript of the run record holds no text from the worker", "no text from the worker") \
                if why else "no text from the worker"
        text = text[:LAST_SCRUB_MAX]
        for _ in range(21):   # PC-10 (a): repeat until the text stops changing (20 passes at most), so a rebuilt address is removed too
            stripped = _remove_hosts(text)
            changed = stripped != text
            text = stripped
            if not changed:
                break
        else:
            return None, "endpoint loop"
        shown = LAST_HEAD + LAST_FILE   # only the part that can be shown is worked on
        text = _unwrap_redact(text[:shown]) + text[shown:]   # R-SEC-2: a secret folded over lines, while the line breaks are still there
        text = D["_scrub_lib"]()(text, (), LAST_SCRUB_MAX)
        head, rest = text[:LAST_HEAD], text[LAST_HEAD:]
        lines = ["[last message of %s, untrusted text]" % pod, head]
        if rest:
            kept = rest[:LAST_FILE]
            path = _write_last(base, name, kept)
            lines.append("[%d more characters in %s; %d characters left out]" % (len(kept), path, len(rest) - len(kept)))
        notes = pod_notes_tail(directory)   # ask #2426: the pod's notes (pushes to the default branch, merges, ref and issue writes), when it has any
        if notes:
            lines += ["[last lines of /work/.pod-notes.log, untrusted text]", notes]
        return "\n".join(lines), None
    except _LastUnsafe:
        return None, "cloud last unsafe"
    except Exception as err:
        return None, "%s" % type(err).__name__


# ---- ask #2393: the pod's end, posted on its ask: the last text and the last lines of its land log ----
END_LOG_CHARS = 4000     # the most characters of one run log that are posted (whole lines)
END_LOG_ENTRIES = 8      # the most run logs posted
END_POST_CHARS = 40000   # the whole comment
RUN_JSON_MAX = 1 << 20
KEY_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
SECRETISH = re.compile(r"sk-|gh[opsu]_|github_pat|AKIA|xox|eyJ|[A-Za-z0-9]{30}")   # as cloud_end_alert's: a name of this shape is not shown
KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")


def _run_record_dir(directory, pod):
    """The run record directory when it is a real child of the run record root named for THIS pod (the checks of last_message), else None."""
    try:
        name = os.path.basename(str(directory).rstrip("/"))
        if not (_LAST_NAME_RE.fullmatch(name) and _name_ok(pod) and re.fullmatch(re.escape(pod) + r"-\d{8}T\d{6}Z-[0-9a-f]{6}", name, re.ASCII)):
            return None
        root = os.path.abspath(str(_runs_root()))
        if os.path.dirname(os.path.abspath(str(directory))) != root or os.path.islink(str(directory)) or not stat.S_ISDIR(os.lstat(str(directory)).st_mode):
            return None
        return str(directory) if os.path.dirname(os.path.realpath(str(directory))) == os.path.realpath(root) else None
    except OSError:
        return None


def _redact_block(block):
    """The pod's last-text block with a key block (whole or clipped) and the switchboard's command-text redaction applied, line by line otherwise unchanged."""
    text = str(block)
    end = KEY_END.search(text)
    if end and not KEY_BEGIN.search(text[:end.start()]):
        text = "[redacted key body]" + text[end.end():]
    return D["redact_command_text"](text)


def _clean_tail(tail):
    """A run log tail as it may be posted: the pod's endpoint address removed, a key block (also one whose first line was clipped) redacted whole, the
    switchboard's redaction and the launcher's scrub by shape, printable ASCII, no backtick, whole lines only, at most END_LOG_CHARS. None when nothing is left.
    The field is never trusted: the pod-side hook redacted it too, and this runs again."""
    scrub = D["_scrub_lib"]()
    text = tail[-LAST_SCRUB_MAX:] if len(tail) <= LAST_SCRUB_MAX else tail[-LAST_SCRUB_MAX:].split("\n", 1)[-1]   # our own cut drops its partial first line
    for _ in range(21):   # the pod's own endpoint address is removed as in last_message (a rebuilt one too)
        stripped = _remove_hosts(text)
        done_, text = stripped == text, stripped
        if done_:
            break
    else:
        return None
    end = KEY_END.search(text)
    if end and not KEY_BEGIN.search(text[:end.start()]):
        text = "[redacted key body]" + text[end.end():]   # a key block whose first line was clipped away: all of it up to its end marker goes
    text = D["redact_command_text"](_unwrap_redact(text))   # a secret folded over two lines first; the whole text next (a key block spans lines), then the cut
    lines = [re.sub(r"[^\x20-\x7e]", "?", scrub(line, (), LAST_SCRUB_MAX)).replace("`", "'") for line in text.splitlines()]   # per line: the scrub flattens a newline
    out = "\n".join(lines)
    return _cut_tail(out)


def _cut_tail(out):
    if len(out) > END_LOG_CHARS:
        out = out[-END_LOG_CHARS:].split("\n", 1)[-1]   # whole lines only: a token is never cut into an unrecognisable suffix
    return out if out.strip() else None


def run_log_entries(directory, pod):
    """THE ONE READER of the run record's run files (ask #2432, as merged): `run.json["end_hook"]["run_logs"]["runs"]`
    is a list of {name: str, exit: int or null, woke: bool, log_tail: str}. Returns [(name, exit text, cleaned tail)] for the entries that have a log_tail, at
    most END_LOG_ENTRIES; [] when the field is absent (today's pods), malformed or the record cannot be read. Never raises."""
    try:
        where = _run_record_dir(directory, pod)
        if where is None:
            return []
        before = os.lstat(where)   # the directory is opened ONCE, by no link, and must be the one the check saw (as last_message does)
        dfd = os.open(where, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        try:
            if not os.path.samestat(before, os.fstat(dfd)):
                return []
            fd = os.open("run.json", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=dfd)
        finally:
            os.close(dfd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return []
            raw = os.read(fd, RUN_JSON_MAX + 1)
        finally:
            os.close(fd)
        info = D["parse_bounded"](raw[:RUN_JSON_MAX].decode("utf-8", "replace")) if len(raw) <= RUN_JSON_MAX else None
        hook = info.get("end_hook") if isinstance(info, dict) else None
        logs = hook.get("run_logs") if isinstance(hook, dict) else None   # ask #2432: the launcher keeps the end hook's run files here, {"runs": [...], "wake_lines": [...]}
        logs = logs.get("runs") if isinstance(logs, dict) else None
        out = []
        for entry in (logs if isinstance(logs, list) else [])[:END_LOG_ENTRIES * 4]:   # entries examined are bounded too
            try:
                tail = entry.get("log_tail") if isinstance(entry, dict) else None
                if not isinstance(tail, str) or not tail.strip():
                    continue
                cleaned = _clean_tail(tail[-4 * END_LOG_CHARS:])   # the contract says 4000; a longer field is clipped before any work
                code = entry.get("exit")
                code = "exit %d" % code if isinstance(code, int) and not isinstance(code, bool) and abs(code) < 10 ** 6 else "still running or no exit file"
                label = entry["name"] if isinstance(entry.get("name"), str) else ""
                label = D["redact_command_text"](label)   # the name is untrusted too: redacted, then reduced to one safe token
                label = os.path.basename(label.replace("\\", "/"))
                label = label if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", label) and not SECRETISH.search(label) else "unnamed"
            except Exception:
                continue   # one bad entry never drops the others
            if cleaned:
                out.append((label, code, cleaned))
            if len(out) >= END_LOG_ENTRIES:
                break
        return out
    except Exception:
        return []


def end_comment(pod, reason, block, logs):
    """The comment for the ask: the pod, the reason, its last text (block: already cut and scrubbed, or None) and each run log (name, exit, tail) of `logs`.
    No mention can fire (no @) and the whole is cut at END_POST_CHARS."""
    lines = ["cloud worker %s retired (%s)" % (pod, re.sub(r"[^A-Za-z0-9 ._:,;()-]", "?", str(reason)))]
    if block:   # the pod's text is untrusted: no local path line, flattened, in its own fence (no backtick can end it)
        kept = [l for l in str(block).split("\n") if " more characters in " not in l]
        lines += ["[last text of the pod, untrusted]", "```", re.sub(r"[^\x20-\x7e\n]", "?", "\n".join(kept)).replace("`", "'"), "```"]
    else:
        lines.append("last text not read")
    for name, code, tail in logs:
        lines += ["[run log %s (%s), untrusted text]" % (name, code), "```", tail, "```"]
    return re.sub(r"@", "(at)", "\n".join(lines))[:END_POST_CHARS]


NAMES_MAX_BYTES = 200000   # the private-names list is read once, to this many bytes
NAMES_MAX_TERMS = 500
_NAMES_CHECKER = []


def _name_terms():
    """([compiled regexes], None) of the private-names list (the one bin/check-public-names reads: its parser, its location, `FLEET_PRIVATE_NAMES` the
    list itself, else `FLEET_PRIVATE_NAMES_FILE` or ~/.config/fleet-switchboard/private-names.txt), or ([], why) when it is missing, unreadable or unusable.
    Read once per call, bounded, never printed (the reason names no entry)."""
    try:
        if not _NAMES_CHECKER:
            import importlib.machinery
            import importlib.util
            path = str(Path(__file__).resolve().with_name("check-public-names"))
            loader = importlib.machinery.SourceFileLoader("cr_check_public_names", path)
            mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("cr_check_public_names", loader))
            loader.exec_module(mod)
            _NAMES_CHECKER.append(mod)
        mod = _NAMES_CHECKER[0]
        text = os.environ.get("FLEET_PRIVATE_NAMES", "")
        if not text.strip():
            with open(os.environ.get("FLEET_PRIVATE_NAMES_FILE") or mod.DEFAULT_LIST, "rb") as handle:
                text = handle.read(NAMES_MAX_BYTES + 1).decode("utf-8", "replace")
        problems = []
        if len(text) > NAMES_MAX_BYTES:
            text = text[:NAMES_MAX_BYTES].rsplit("\n", 1)[0]   # the entry the cut fell in is dropped, not half read
            problems.append("list cut")
        terms, bad = [], 0
        for line in text.splitlines():   # one entry at a time: a bad one is skipped, the others still apply
            try:
                found, _ignores = mod.parse_list(line)
            except ValueError:
                bad += 1
                continue
            terms += [regex for _i, regex in found]
        if bad:
            problems.append("%d entries unusable" % bad)
        if len(terms) > NAMES_MAX_TERMS:
            terms = terms[:NAMES_MAX_TERMS]
            problems.append("list too long")
        if not terms and not problems:
            problems.append("no list" if not text.strip() else "no entries")
        return terms, ("; ".join(problems) or None)
    except Exception as err:
        return [], type(err).__name__[:40]


def _hide_names(text, terms):
    """`text` with every match of a private-name regex (case rules as the checker's, per line) replaced by [private name]."""
    out = []
    for line in text.splitlines():   # as the checker splits
        for regex in terms:
            line = regex.sub(lambda m: "[private name]" if m.group(0) else m.group(0), line)
        out.append(line)
    return "\n".join(out)


_WRAP_WORD = 512      # the longest word joined across a fold; a longer one is no token of ours
_WRAP_LINES = 8       # the most lines one folded secret is followed over
_HINT = re.compile(r"(?i)sk-|gh[pousr]_|github_pat_|glpat-|xox|AKIA|eyJ|@|(?:token|secret|passw|api[_-]?key|credential|auth)[^=:]{0,40}[=:]|[A-Za-z0-9+/_-]{24}")
_WRAP_BUDGET = 20000  # the most redaction attempts of one call: a text built to be slow stops being followed, it is never slow   # what any secret shape needs: a join without one is not tried
_WRAP_LEAD = " \t>|\"'`,;+*("   # what may stand before the continuation: an indent, a quote mark, a table bar, a string quote
_INVISIBLE = re.compile("[\u200b-\u200f\u2060-\u2064\ufeff]")
_WRAP_PART = re.compile(r"[A-Za-z0-9_+/=.&@:-]+")


def _looks_like_rest(word):
    """A continuation of a token already redacted whole: random-looking (a digit and a letter, four characters or more), not prose."""
    return _WRAP_PART.fullmatch(word) is not None and (len(word) >= 12 or (len(word) >= 4 and bool(re.search(r"\d", word)) and bool(re.search(r"[A-Za-z]", word))))


def _unwrap_redact(text):
    """`text` with a secret that was folded over lines redacted: at each line break the last word of the line (a trailing backslash or hyphen of the fold
    dropped, or kept) is joined to the first word of the next line (leading blanks ignored), and of up to three more lines while each is one word, then
    redacted by shape (the switchboard's redaction and the launcher's scrub); only when the joined word changes are the parts replaced, the rest of each line
    kept. Where the first part is already a whole token, the next word is taken only if it looks like the rest of one. Words are capped, so a long line costs
    nothing: work is linear in the (already cut) text."""
    scrub, redact = D["_scrub_lib"](), D["redact_command_text"]

    budget = [_WRAP_BUDGET]

    def shaped(word):
        budget[0] -= 1
        return redact(scrub(word, (), LAST_SCRUB_MAX)) if budget[0] >= 0 else word   # past the budget nothing more is tried
    lines = _INVISIBLE.sub("", text).splitlines()   # invisible characters cannot split a token; every kind of line break the later steps split on
    for i in range(len(lines) - 1):
        a = lines[i].rstrip(" \t+,;|&")   # a string concatenation or a table bar after the first part
        tail = a[-(_WRAP_WORD + 1):]
        if not a or not tail.split() or len(tail.split()[-1]) > _WRAP_WORD:
            continue
        wa = tail.split()[-1]
        head = a[:len(a) - len(wa)]
        for cut in dict.fromkeys((wa[:-1] if wa[-1] in "\\-" else "", wa.rstrip("\\-\"',;+"), wa)):
            if not cut:
                continue
            joined, parts, found, blanks, j = cut, [], None, 0, i + 1
            while j < len(lines) and len([1 for p_ in parts if p_[2]]) < _WRAP_LINES and blanks <= 64:
                b = lines[j].lstrip(_WRAP_LEAD)   # an indent, a quote mark or a table bar
                words = b[:_WRAP_WORD + 1].split()
                if not b.strip():
                    parts.append((j, lines[j], ""))   # a blank line in the fold
                    blanks += 1
                    j += 1
                    continue
                if not words or len(words[0]) > _WRAP_WORD:
                    break
                wb = words[0]
                parts.append((j, b, wb))
                joined += wb
                if _HINT.search(joined) and shaped(joined) != joined and (shaped(cut) == cut or _looks_like_rest(wb) or cut != wa):
                    found = shaped(joined)
                    break
                if b.rstrip(" |\"',;+") != wb:
                    break   # a middle line is one word
                j += 1
            if found is None:
                continue
            last = parts[-1][0]
            while last + 1 < len(lines) and len(parts) < _WRAP_LINES and lines[last].strip(_WRAP_LEAD) == parts[-1][2]:   # the rest of a token, one word to a line
                nxt = lines[last + 1].strip(_WRAP_LEAD)
                word = nxt[:_WRAP_WORD + 1].split()[0] if nxt else ""
                if not word or len(word) > _WRAP_WORD or not _looks_like_rest(word) or shaped(joined + word) != found:
                    break
                joined += word
                parts.append((last + 1, nxt, word))
                last += 1
            lines[i] = head + found
            for j, b, wb in parts:
                lines[j] = b[len(wb):] if wb else b
            break
    return "\n".join(lines)


def post_end(engine, state, record, name, reason, body, post, note, audit=None):
    """Ask #2393: ONCE per end, the pod's final message and the last lines of its land log are posted on its ask issue through `post` (handoff.post_ask_comment),
    scrubbed and cut (end_comment); a pod without a log posts the message alone, a pod without an ask (`issue` and `repo` of its cloud record) posts nothing.
    Never raises: a failed post is handed to `note(why)` and the retire goes on."""
    issue, repo = record.get("issue"), record.get("repo")
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1 or not isinstance(repo, str) or not repo:
        return
    try:
        where = body.get("record") if isinstance(body, dict) else None
        block, _why = last_message(where, state, name) if isinstance(where, str) and where else (None, "no run record")
        block = _redact_block(block) if block else block   # the last text gets the run logs' redaction too before it is posted
        tail = run_log_entries(where, name) if isinstance(where, str) and where else []
        comment = end_comment(name, D["_scrub_lib"]()(reason, (), 300)[:120], block, tail)
        terms, unchecked = _name_terms()   # the post may land on a public repository: the private names are replaced, as bin/check-public-names would refuse them
        if unchecked:
            try:
                audit and audit("private names not checked (%s)" % unchecked)
            except Exception:
                pass
        comment = _unwrap_redact(_hide_names(comment, terms) if terms else comment)[:END_POST_CHARS]   # then a secret folded over two lines; the cut again (a short name grows)
        post(engine, issue, comment, author=name, repo=repo)
    except Exception as err:
        try:
            note(type(err).__name__[:60])
        except Exception:
            pass   # a failed audit line never breaks the retire
