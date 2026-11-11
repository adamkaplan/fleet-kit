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


def read_run_record(name, retired, state, runner, now):
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
        text = D["_scrub_lib"]()(text, (), LAST_SCRUB_MAX)
        head, rest = text[:LAST_HEAD], text[LAST_HEAD:]
        lines = ["[last message of %s, untrusted text]" % pod, head]
        if rest:
            kept = rest[:LAST_FILE]
            path = _write_last(base, name, kept)
            lines.append("[%d more characters in %s; %d characters left out]" % (len(kept), path, len(rest) - len(kept)))
        return "\n".join(lines), None
    except _LastUnsafe:
        return None, "cloud last unsafe"
    except Exception as err:
        return None, "%s" % type(err).__name__


# ---- ask #2393: the pod's end, posted on its ask: the last text and the last lines of its land log ----
END_LOG_LINES = 20       # the last lines of /work/land.log that are posted
END_LOG_CHARS = 2000     # and at most this many characters of them
END_POST_CHARS = 6000    # the whole comment
RUN_JSON_MAX = 1 << 20
KEY_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
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


def land_log_tail(directory, pod):
    """The pod's land log tail from its run record (`land_log_tail`, a clipped string the launcher put there; ASSUMED field name, ask #2393): the last END_LOG_LINES
    lines, at most END_LOG_CHARS characters, redacted by shape (the switchboard's redaction, then the launcher's scrub), flattened to printable ASCII with no backtick.
    None when the pod has no log, the field is absent or the record cannot be read. Never raises."""
    try:
        where = _run_record_dir(directory, pod)
        if where is None:
            return None
        fd = os.open(os.path.join(where, "run.json"), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            raw = os.read(fd, RUN_JSON_MAX + 1)
        finally:
            os.close(fd)
        info = D["parse_bounded"](raw[:RUN_JSON_MAX].decode("utf-8", "replace")) if len(raw) <= RUN_JSON_MAX else None
        tail = info.get("land_log_tail") if isinstance(info, dict) else None
        if not isinstance(tail, str) or not tail.strip():
            return None
        scrub = D["_scrub_lib"]()
        text = tail[-LAST_SCRUB_MAX:]
        for _ in range(21):   # the pod's own endpoint address is removed as in last_message (a rebuilt one too)
            stripped = _remove_hosts(text)
            changed, text = stripped != text, stripped
            if not changed:
                break
        else:
            return None
        end = KEY_END.search(text)
        if end and not KEY_BEGIN.search(text[:end.start()]):
            text = "[redacted key body]" + text[end.end():]   # a key block whose first line was clipped away: all of it up to its end marker goes
        text = D["redact_command_text"](text)   # the whole text first (a key block spans lines), then the cut
        lines = text.splitlines()[-END_LOG_LINES:]
        lines = [re.sub(r"[^\x20-\x7e]", "?", scrub(line, (), LAST_SCRUB_MAX)).replace("`", "'") for line in lines]   # per line: the scrub flattens a newline
        out = "\n".join(lines)
        if len(out) > END_LOG_CHARS:
            out = out[-END_LOG_CHARS:].split("\n", 1)[-1]   # whole lines only: a token is never cut into an unrecognisable suffix
        return out or None
    except Exception:
        return None


def end_comment(pod, reason, block, tail):
    """The comment for the ask: the pod, the reason, its last text (block: already cut and scrubbed, or None) and the land log tail (or nothing when the pod has none).
    No mention can fire (no @) and the whole is cut at END_POST_CHARS."""
    lines = ["cloud worker %s retired (%s)" % (pod, re.sub(r"[^A-Za-z0-9 ._:,;()-]", "?", str(reason)))]
    if block:   # the pod's text is untrusted: no local path line, flattened, in its own fence (no backtick can end it)
        kept = [l for l in str(block).split("\n") if " more characters in " not in l]
        lines += ["[last text of the pod, untrusted]", "```", re.sub(r"[^\x20-\x7e\n]", "?", "\n".join(kept)).replace("`", "'"), "```"]
    else:
        lines.append("last text not read")
    if tail:
        lines += ["[last lines of /work/land.log, untrusted text]", "```", tail, "```"]
    return re.sub(r"@", "(at)", "\n".join(lines))[:END_POST_CHARS]
