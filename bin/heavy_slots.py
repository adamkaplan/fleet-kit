"""CPU slots for heavy commands (ask #34): `fleet-switchboard heavy [--slots N] [--wait-max S] -- <command...>`.

Full test suites and browser/e2e runs starve the machine when many agents start them at once. This runs such a command only when one of N slots is free
(default 3), in the background QoS class, so at most N of them share the machine.

  - A slot is a file `heavy-slot-<i>.lock` in the state directory, held with `flock` by the RUNNING COMMAND: the command inherits the locked descriptor, so
    a slot is busy exactly while the command (or anything it left running with that descriptor) lives, and the kernel frees it when the last holder dies.
    No pid file can lie about it. Killing the wrapper alone does not free the slot while its command still runs, which is what a slot is for.
  - Waiting prints one line every 30 s (`heavy: waiting for a slot (3 of 3 busy: ...)`); past `--wait-max` seconds (default 1800) it exits 75
    (EX_TEMPFAIL) and the command is not run.
  - The command runs as `taskpolicy -b <command>` (macOS background class), else `nice -n 10 <command>`; its arguments, environment, working directory and
    stdin/stdout/stderr are untouched, nothing goes through a shell, its exit status is the wrapper's (128 + n for a signal), and SIGINT/SIGTERM/SIGHUP
    sent to the wrapper are forwarded to it. The child is always reaped: no zombie.
  - `holders(state_dir)` says who holds a slot NOW: [{"slot", "name", "pid", "command", "since"}], read from the slot file of a slot whose lock is held.

Pure of the switchboard: the caller hands it the state directory, the clock, the settings and the output. The slots are a courtesy limit among cooperating agents,
not a boundary: FLEET_HEAVY=off, a CI run or a preset FLEET_HEAVY_SLOT skip them, and a holder's name is whatever its environment says."""

import errno
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

DEFAULT_SLOTS = 3
DEFAULT_WAIT_MAX = 1800
POLL_SECONDS = 0.5
WAIT_REPORT_SECONDS = 30
EXIT_TEMPFAIL = 75      # no slot within --wait-max
EXIT_USAGE = 64
EXIT_NOT_FOUND = 127
INFO_MAX = 4096         # bytes of a slot file read
SLOT_ENV = "FLEET_HEAVY_SLOT"   # set in the command's environment: a command already in a slot does not take a second one
OFF_ENV = "FLEET_HEAVY"         # "off": bypass (CI never uses slots)
SLOTS_MAX = 64
MEM_ENV = "FLEET_HEAVY_MEM_MB"           # memory ceiling of the command's whole process tree, in MB; 0 = off
TIME_ENV = "FLEET_HEAVY_MAX_SECONDS"     # run-time ceiling, in seconds; 0 = off
DEFAULT_MEM_MB = 16384
DEFAULT_MAX_SECONDS = 7200
WATCH_SECONDS = 5       # how often the watchdog looks
KILL_GRACE_SECONDS = 10  # between the polite stop and the forced one
SWEEP_ROUNDS = 5        # forced-stop rounds that look again for what is left
SWEEP_SECONDS = 0.3     # between them
EXIT_TIME = 124
EXIT_MEMORY = 137
EXPRESS = "express"                    # the name of the one extra slot a declared-short command may use
EXPRESS_SECONDS = 120                   # the wrapper's own time ceiling on a command in it
QUEUE_FILE = "heavy-queue.json"          # the arrival order of the waiters
QUEUE_LOCK = "heavy-queue.lock"
QUEUE_MAX = 1000                        # entries read; more reads as a corrupt file
QUEUE_BYTES = 262144
TEMPORARY_NAME = re.compile(r"heavy-queue\.json\.([0-9]{1,9})\.tmp", re.ASCII)   # a temporary file of the order, with the pid of the process that wrote it
HARD_MAX_SECONDS = 14400                # the longest any run may hold a slot (4 h); a request above it, or 0, reads as this: a bound always exists
ORPHAN_LOCK = "heavy-orphan.lock"       # held (not blocking) by whoever is stopping an orphan, so two checkers never stop and report the same one
STOP_TOLD = ("sent", "unknown name", "failed")   # what the audit says of the message to the lane
PID_MAX = 2 ** 22                       # no pid is larger (Linux pid_max at most 4194304)
FORWARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class HeavyError(Exception):
    """A refusal with a sentence for the caller."""


SELECTORS = ("-k", "--filter", "-run", "-c", "-e", "-m")   # options whose NEXT word is a value (a filter, a shell string): the value is never read for a kind
E2E_WORDS = ("playwright", "chromium", "chrome", "cypress", "puppeteer", "webdriver", "e2e", "--browser", "--headed")   # a WHOLE word of the command, or a part of the program's own name


def _split(command):
    """(program, rest): the program's basename and the words after it, once the wrappers that only start it are skipped (an interpreter given a script or `-m module`,
    `timeout N`, `env VAR=x`, `taskpolicy -b`, `nice -n N`, `npx`). The kind is read from these and never from a substring of an argument."""
    words = [str(word) for word in command]
    while words:
        base = os.path.basename(words[0])
        if re.fullmatch(r"python[0-9.]*", base) or base in ("node", "sh", "bash", "zsh", "npx"):
            if len(words) > 1 and words[1] == "-m":
                words = words[2:]   # the module is the program
                break
            if len(words) > 1 and not words[1].startswith("-"):
                words = words[1:]
                continue
            break
        elif base == "timeout":
            words = words[1:]
            while words and (words[0].startswith("-") or re.fullmatch(r"[0-9.]+[smhd]?", words[0])):
                words = words[1:]
        elif base == "env":
            words = words[1:]
            while words and (words[0].startswith("-") or "=" in words[0]):
                words = words[1:]
        elif base in ("taskpolicy", "nice"):
            words = words[1:]
            while words and (words[0].startswith("-") or re.fullmatch(r"[0-9]+", words[0])):
                words = words[1:]
        else:
            break
    return (os.path.basename(words[0]).lower(), words[1:]) if words else ("", [])


def _options(rest, any_option=False):
    """The words of `rest` that are not the value of an option: always the value of a selector (-k, -c ...), and with `any_option` the word after any option-looking word
    (`--name chrome`), except after the whole option words that select a kind themselves."""
    skip, found = False, []
    for word in rest:
        if skip:
            skip = False
            continue
        found.append(word)
        if word in SELECTORS or (any_option and word.startswith("-") and "=" not in word and word not in E2E_WORDS):
            skip = True
    return found


def _is_e2e(program, rest, short):
    return any(tool in program for tool in E2E_WORDS if not tool.startswith("-")) or any(word.lower() in E2E_WORDS for word in _options(rest, any_option=True))


def _is_focused(program, rest, short):
    if short or any(word in ("-k", "--filter", "-run") for word in rest):
        return True
    named = [program] + [os.path.basename(word) for word in _options(rest)]
    if any(re.fullmatch(r"(test_[^/]*\.py|[^/]*\.test\.[^/]*)", word) for word in named) or any(re.search(r"\.py::\S", word) for word in _options(rest)):
        return True
    chosen = [word for word in rest if not word.startswith("-")]
    return program == "unittest" and bool(chosen) and chosen[0] != "discover"   # a dotted name is a focused run; bare or `discover` is the whole suite


def _is_full(program, rest, short):
    return (program.startswith("test-") or program in ("pytest", "unittest") or [program] + rest[:1] in (["make", "test"], ["npm", "test"], ["go", "test"]))


# THE kind table: (kind, default ceiling in seconds, matcher(program, rest, short)); the first match wins, "other" matches everything. The defaults come from the
# measured run times in docs/switchboard.md ("heavy"); e2e has no sample (a judgement).
KINDS = (
    ("e2e", 3600, _is_e2e),
    ("focused", 1800, _is_focused),
    ("full", 5400, _is_full),
    ("other", 3600, lambda program, rest, short: True),
)
KIND_DEFAULTS = {kind: seconds for kind, seconds, _match in KINDS}


def classify(command, short=False):
    """The kind of run `command` is, read from the program and its whole option words: "e2e", "focused", "full" or "other". A path, a filter value or a shell
    string that merely contains a kind's word does not change it."""
    program, rest = _split(command)
    for kind, _seconds, match in KINDS:
        if match(program, rest, short):
            return kind
    return "other"


def _whole(value, low=1, high=10 ** 9):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def settings(raw):
    """The slot count from the config's `heavy` value (None, or {"slots": n, "ceilings": {...}}); HeavyError for anything else."""
    if raw is None:
        return DEFAULT_SLOTS
    if not isinstance(raw, dict) or set(raw) - {"slots", "ceilings"}:
        raise HeavyError("'heavy' must be an object with only \"slots\" and \"ceilings\" in it, got %r" % (raw,))
    slots = raw.get("slots", DEFAULT_SLOTS)
    if not isinstance(slots, int) or isinstance(slots, bool) or not 1 <= slots <= SLOTS_MAX:
        raise HeavyError("'heavy.slots' must be a whole number from 1 to %d, got %r" % (SLOTS_MAX, slots))
    given = raw.get("ceilings", {})
    if not isinstance(given, dict) or set(given) - set(KIND_DEFAULTS) - {"max"}:
        raise HeavyError("'heavy.ceilings' must be an object with only %s and \"max\" in it, got %r" % (", ".join('"%s"' % kind for kind in KIND_DEFAULTS), given))
    for key, value in given.items():
        if not _whole(value, 1, HARD_MAX_SECONDS):
            raise HeavyError("'heavy.ceilings.%s' must be a whole number of seconds from 1 to %d (the hard maximum), got %r" % (key, HARD_MAX_SECONDS, value))
    return slots


def ceiling_table(raw):
    """{kind: seconds, "max": seconds} from the config's `heavy` value, read leniently: a value that is not a whole number of 1 or more is the default (a ceiling is never off)."""
    given = (raw or {}).get("ceilings") if isinstance(raw, dict) else None
    given = given if isinstance(given, dict) else {}
    top = given.get("max") if _whole(given.get("max"), 1, HARD_MAX_SECONDS) else HARD_MAX_SECONDS   # the configuration can only lower the maximum
    table = {"max": top}
    for kind, default in KIND_DEFAULTS.items():
        value = given.get(kind) if _whole(given.get(kind), 1, HARD_MAX_SECONDS) else default
        table[kind] = min(value, top)
    return table


def resolve_ceiling(kind, environ, request=None, config=None):
    """The time ceiling in seconds of a run of `kind`: the kind's default (or the configured one), or the run's own request (`request`, i.e. --ceiling, else
    FLEET_HEAVY_MAX_SECONDS) up to the hard maximum. A request of 0 reads as the maximum (a bound always exists); a request that is not a whole number reads as the default."""
    table = ceiling_table(config)
    top, default = table["max"], table.get(kind, table["other"])
    raw = request if request is not None else environ.get(TIME_ENV)
    text = "" if raw is None else str(raw).strip()
    if re.fullmatch(r"[0-9]{1,30}", text):
        wanted = int(text)
        return top if wanted == 0 else min(wanted, top)
    return default


def should_take_slot(environ):
    """"off" when FLEET_HEAVY=off or a CI run (CI never uses slots), "inside" when this process already runs in a slot, else "taken" (it should take one)."""
    if str(environ.get(OFF_ENV, "")).lower() == "off" or str(environ.get("CI", "")).lower() in ("1", "true"):
        return "off"
    if environ.get(SLOT_ENV):
        return "inside"
    return "taken"


def _plain(text, limit=80):
    """Printable characters only, cut to `limit`: what is shown to a person never carries a terminal escape (a slot file or an environment variable is not trusted)."""
    return "".join(c if c.isprintable() and c not in "\x7f" else " " for c in str(text))[:limit]


def holder_name(environ):
    """Who a slot is held for: FLEET_HEAVY_NAME, else the OpenCode session id, else the user. No lookup: a wrapper must not make reads of its own."""
    for key in ("FLEET_HEAVY_NAME", "OPENCODE_SESSION_ID", "USER", "LOGNAME"):
        value = environ.get(key)
        if value:
            return _plain(value)
    return "unknown"


def slot_path(state_dir, index):
    return os.path.join(str(state_dir), "heavy-slot-%s.lock" % index)   # index: 0, 1, ... or "express"


def _open(state_dir, index):
    return os.open(slot_path(state_dir, index), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def _read_info(fd):
    try:
        raw = os.pread(fd, INFO_MAX, 0).decode("utf-8", "replace").strip("\0 \n")
        info = json.loads(raw) if raw else {}
    except (OSError, ValueError):
        info = {}
    return info if isinstance(info, dict) else {}


def holders(state_dir, slots=DEFAULT_SLOTS):
    """The slots held NOW: [{"slot", "name", "pid", "command", "since"}]. A slot is held when its lock cannot be taken; the probe takes it for an instant and
    lets go, so a holder that died is simply not listed. A held slot whose file has no readable record is listed as name "?"."""
    found = []
    for index in range(slots):
        try:
            fd = _open(state_dir, index)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                continue   # free
            except OSError as err:
                if err.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    continue
            info = _read_info(fd)
            found.append({"slot": index, "name": _plain(info.get("name", "?")), "pid": info.get("pid") if isinstance(info.get("pid"), int) else None,
                          "command": _plain(info.get("command", "?")), "since": info.get("since") if isinstance(info.get("since"), (int, float)) else None})
        finally:
            os.close(fd)
    return found


def _process_start(pid):
    """The start time of `pid` as text ("" when it cannot be read): with the pid it names one process, so a reused pid is never taken for the waiter that died."""
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return ""
    return " ".join(done.stdout.split()) if done.returncode == 0 else ""


_POPEN = subprocess.Popen   # the real one, kept at import: start times are read with it (a test that replaces the command's Popen replaces nothing here)


def _start_of(pid):
    """The start time of `pid` for a slot record, "" when it cannot be had (the process is then named by its pid alone and never trusted as a root). Never raises."""
    try:
        with _POPEN(["ps", "-o", "lstart=", "-p", str(pid)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as proc:
            out, _err = proc.communicate(timeout=5)
        return " ".join(out.split()) if proc.returncode == 0 else ""
    except Exception:
        return ""


def _own_start():
    return _start_of(os.getpid())


def _is_zombie(pid):
    """A process that has died and not been reaped: it still has a pid, but it is gone."""
    try:
        done = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return False
    return done.returncode == 0 and done.stdout.strip().startswith("Z")


def _alive(pid, start):
    """Is the process (pid, start time) still there? A pid that exists with another start time is another process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass   # exists, not ours to signal
    if _is_zombie(pid):
        return False
    return not start or _process_start(pid) in ("", start)


class QueueUnusable(Exception):
    """The order file cannot be read, written or locked: the caller falls back to taking whatever slot is free."""


class Line:
    """The arrival order of the callers waiting for a slot, kept in `heavy-queue.json` in the state directory (written atomically, read and changed under a lock).
    An entry is {"pid", "start", "name", "short"}, in arrival order; an entry whose process is gone is dropped at the next look, so a dead waiter never blocks the
    ones behind it. The waiter at the head of the line is the only one that tries an ordinary slot: a slot that frees goes to the one that has waited longest."""

    def __init__(self, state_dir, name, short=False, pid=None, alive=_alive, start_of=_process_start, lock_tries=50, lock_pause=0.02, slots=DEFAULT_SLOTS, held=None, note=None):
        self.dir, self.name, self.short = str(state_dir), name, bool(short)
        if not os.path.isdir(self.dir):
            raise OSError(errno.ENOENT, "no state directory")   # the caller takes whatever slot is free, as before
        self.pid = os.getpid() if pid is None else pid
        self.start = start_of(self.pid)
        self.alive, self.lock_tries, self.lock_pause = alive, lock_tries, lock_pause
        self.path, self.lock_path = os.path.join(self.dir, QUEUE_FILE), os.path.join(self.dir, QUEUE_LOCK)
        self.joined = False
        self.held = held if held is not None else (lambda: held_by(self.dir, slots))   # pids that hold a slot now: a ticket never outlives the slot its owner took
        self.note, self.sweep_noted = note or (lambda text: None), False
        self.verified = {}   # (pid, start) -> monotonic time its liveness was last checked with `ps`

    def _locked(self):
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        for _ in range(self.lock_tries):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError as err:
                if err.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    os.close(fd)
                    raise
                time.sleep(self.lock_pause)
        os.close(fd)
        raise OSError(errno.EAGAIN, "the order file stays locked")   # a deliberate fail-open: after about a second the caller takes whatever slot is free (one note)

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return []
        try:
            raw = os.read(fd, QUEUE_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > QUEUE_BYTES:
            raise QueueUnusable("the order file is too large")
        try:
            data = json.loads(raw.decode("utf-8")) if raw.strip() else {"entries": []}
            entries = data["entries"]
            if not isinstance(entries, list) or len(entries) > QUEUE_MAX:
                raise ValueError("entries")
            for entry in entries:
                if not (isinstance(entry, dict) and isinstance(entry.get("pid"), int) and not isinstance(entry.get("pid"), bool) and isinstance(entry.get("start"), str)):
                    raise ValueError("entry")
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            raise QueueUnusable("the order file is corrupt")
        return entries

    def _write(self, entries):
        temporary = "%s.%d.tmp" % (self.path, os.getpid())
        self._sweep_temporaries()
        data = json.dumps({"entries": entries}).encode("utf-8")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            try:
                view = memoryview(data)
                while view:   # a short write is finished, never renamed into place half done
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, self.path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _sweep_temporaries(self):
        """Remove the `heavy-queue.json.<pid>.tmp` files of processes that are gone (a kill between the write and the rename leaves one). It can never raise into the
        wrapper: a name that is not exactly that shape (ASCII digits, a possible pid) is left alone, only a regular file is removed (a directory or a symlink never),
        and any failure is one note and the command carries on."""
        try:
            for name in os.listdir(self.dir):
                found = TEMPORARY_NAME.fullmatch(name)
                if found is None:
                    continue
                pid = int(found.group(1))
                if not 2 <= pid <= PID_MAX or pid == os.getpid():
                    continue
                path = os.path.join(self.dir, name)
                try:
                    os.kill(pid, 0)
                    continue   # the process is there
                except ProcessLookupError:
                    pass
                except OSError:
                    continue
                if stat.S_ISREG(os.lstat(path).st_mode):
                    os.unlink(path)
        except Exception as err:   # the guard fails open for the command
            if not self.sweep_noted:
                self.sweep_noted = True
                self.note("heavy: queue sweep failed: %s\n" % type(err).__name__)

    def _mine(self, entry):
        return entry["pid"] == self.pid and entry["start"] == self.start

    def _update(self, join):
        """Under the lock: drop the entries of processes that are gone, add this caller at the end when `join` and not there yet, write back if changed; the entries."""
        try:
            lock = self._locked()
        except OSError as err:
            raise QueueUnusable(err.strerror or str(err))
        try:
            entries = self._read()
            holding = self.held()
            kept = [entry for entry in entries if self._mine(entry) or (entry["pid"] not in holding and self._still_there(entry))]
            if join and not any(self._mine(entry) for entry in kept):
                kept.append({"pid": self.pid, "start": self.start, "name": _plain(self.name, 40), "short": self.short})
            if kept != entries:
                self._write(kept)
            return kept
        except OSError as err:
            raise QueueUnusable(err.strerror or str(err))
        finally:
            os.close(lock)

    def _still_there(self, entry):
        """Is the entry's process alive? What `ps` said is kept for 5 s, so a line of waiters does not run it for every entry at every poll."""
        key, now = (entry["pid"], entry["start"]), time.monotonic()
        if self.alive is _alive and key in self.verified and now - self.verified[key] < 5.0:   # (an injected check is asked every time)
            try:
                os.kill(entry["pid"], 0)   # the cheap check every time: a pid that is gone is dropped at once
            except ProcessLookupError:
                self.verified.pop(key, None)
                return False
            except OSError:
                pass
            return True
        if self.alive(entry["pid"], entry["start"]):
            self.verified[key] = now
            return True
        self.verified.pop(key, None)
        return False

    def place(self):
        """This caller's place in the line (1 is the head), joining at the end the first time. Raises QueueUnusable."""
        entries = self._update(True)
        self.joined = True
        return next(index for index, entry in enumerate(entries, 1) if self._mine(entry))

    def leave(self):
        """Take this caller out of the line (it got a slot, gave up or was stopped). True when it is out (or never was in); False when the file could not be
        changed. Never raises."""
        if not self.joined:
            return True
        try:
            lock = self._locked()
        except OSError:
            return False
        try:
            entries = self._read()
            kept = [entry for entry in entries if not self._mine(entry)]
            if kept != entries:
                self._write(kept)
            self.joined = False
            return True
        except (OSError, QueueUnusable):
            return False
        finally:
            os.close(lock)


def _take_express(state_dir):
    """The fd of the express slot just locked, or None when it is busy."""
    fd = os.open(slot_path(state_dir, EXPRESS), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as err:
        os.close(fd)
        if err.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise
    return fd


def held_by(state_dir, slots=DEFAULT_SLOTS):
    """The pids that hold a slot NOW, the express slot included: the command's and the wrapper's (`pid` and `wrapper` of each held slot's record). Nothing is created."""
    pids = set()
    for index in list(range(slots)) + [EXPRESS]:
        try:
            fd = os.open(slot_path(state_dir, index), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                continue   # free
            except OSError as err:
                if err.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    continue
            info = _read_info(fd)
            pids |= {value for value in (info.get("pid"), info.get("wrapper")) if isinstance(value, int) and not isinstance(value, bool)}
        finally:
            os.close(fd)
    return pids


def _describe(held):
    return ", ".join("%s %s%s" % (h["name"], h["command"], " (pid %d)" % h["pid"] if h["pid"] else "") for h in held) or "unknown holders"


def _take(state_dir, slots):
    """(fd, index) of a slot just locked, or (None, None) when every slot is busy."""
    for index in range(slots):
        fd = _open(state_dir, index)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            os.close(fd)
            if err.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                continue
            raise
        return fd, index
    return None, None


def _record(fd, name, pid, command, now, extra=None):
    """Write who holds the slot (truncating what the last holder wrote). Best effort: a slot without a record is still a held slot. `extra`: kind, ceiling, lane and the
    wrapper's start time (an old record without them reads as kind "other")."""
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(dict(extra or {}, name=name, pid=pid, wrapper=os.getpid(), command=os.path.basename(command)[:80], since=int(now))).encode("utf-8"), 0)
    except OSError:
        pass


def command_prefix(which=shutil.which):
    """["taskpolicy", "-b"] where macOS's taskpolicy exists, else ["nice", "-n", "10"], else [] (no way to lower the priority: said by the caller)."""
    found = which("taskpolicy")
    if found:
        return [found, "-b"]
    found = which("nice")
    return [found, "-n", "10"] if found else []


def _resolve(command, environ):
    """The command's first word as an executable path, or None: looked up like a shell would (PATH, or a path with a slash)."""
    word = command[0]
    if os.sep in word:
        return word if os.access(word, os.X_OK) and os.path.isfile(word) else None
    return shutil.which(word, path=environ.get("PATH"))


def ceilings(environ):
    """(memory MB, seconds) the watchdog enforces, each 0 when off: FLEET_HEAVY_MEM_MB (default 16384) and FLEET_HEAVY_MAX_SECONDS (default 7200). Anything that is
    not a whole number of 0 or more reads as the default: a typo never turns the guard off."""
    def read(key, default):
        raw = str(environ.get(key, "")).strip()
        return int(raw) if re.fullmatch(r"[0-9]{1,12}", raw) else default
    return read(MEM_ENV, DEFAULT_MEM_MB), read(TIME_ENV, DEFAULT_MAX_SECONDS)


def read_process_table():
    """[(pid, ppid, pgid, rss_kb, start)] from `ps`; `start` is the process's start time as text, so a pid that was reused is told from the process it was. Raises
    OSError when the table cannot be read."""
    try:
        done = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,rss=,lstart="], capture_output=True, text=True, timeout=20)
    except (subprocess.SubprocessError, OSError) as err:
        raise OSError("ps failed: %s" % err)
    rows = []
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and all(re.fullmatch(r"[0-9]{1,12}", part) for part in parts[:4]):
            rows.append(tuple(int(part) for part in parts[:4]) + (" ".join(parts[4:]),))
    if done.returncode != 0 or not rows:
        raise OSError("ps gave no process table")
    return rows


def read_lock_holders(path):
    """The pids that have `path` open, from `lsof -t` (same user only); raises OSError when lsof is missing, fails or is too slow (no holder is then known)."""
    try:
        done = subprocess.run(["lsof", "-a", "-u", str(os.getuid()), "-t", "--", str(path)], capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as err:
        raise OSError("lsof failed: %s" % err)
    if done.returncode not in (0, 1):   # 1: nothing has it open
        raise OSError("lsof exited %d" % done.returncode)
    return {int(word) for word in done.stdout.split() if re.fullmatch(r"[0-9]{1,12}", word)}


def _start(row):
    return row[4] if len(row) > 4 else ""


def tree(rows, roots):
    """The pids of `roots` and everything below them by parent links."""
    kids = {}
    for row in rows:
        kids.setdefault(row[1], []).append(row[0])
    found, todo = set(), list(roots)
    while todo:
        pid = todo.pop()
        if pid not in found:
            found.add(pid)
            todo.extend(kids.get(pid, ()))
    return found


class Watchdog:
    """Stops the command the wrapper started when its process tree passes a memory ceiling or it runs past a time ceiling: SIGTERM, a grace, then SIGKILL, then a
    sweep that repeats until nothing of the run is alive (or says who is).

    Every process seen in the command's tree is remembered by (pid, start time) at each sample, so a double-forked orphan, a child that ignores the polite stop and a
    child that started its own session are still found and stopped, and a pid that was reused by another process never is. Only those remembered processes (and the
    command's own process group when it has one) are signalled; never the wrapper, its parent, its own group, pid 1 or any other process. A process table that cannot
    be read stops nothing beyond the command itself (one note); the time ceiling needs no table."""

    def __init__(self, pid, command, mem_mb, max_seconds, out, started, read_table=read_process_table, clock=time.time, pause=None, killpg=os.killpg,
                 kill=os.kill, every=WATCH_SECONDS, grace=KILL_GRACE_SECONDS, rounds=SWEEP_ROUNDS, sweep_every=SWEEP_SECONDS, sweep_pause=time.sleep, me=None, lock_path=None, lsof=read_lock_holders, hard_max=HARD_MAX_SECONDS, decided=None):
        self.hard_max, self.decided = hard_max, decided
        self.pid, self.command, self.mem_mb, self.max_seconds, self.out, self.started = pid, command, mem_mb, max_seconds, out, started
        self.read_table, self.clock, self.killpg, self.kill, self.every, self.grace = read_table, clock, killpg, kill, every, grace
        self.rounds, self.sweep_every, self.sweep_pause = rounds, sweep_every, sweep_pause
        self.lock_path, self.lsof, self.lock_noted, self.candidates = lock_path, lsof, False, set()
        self.me = me if me is not None else (os.getpid(), os.getppid(), os.getpgrp())   # never signalled: this wrapper, its caller, its group
        self.stop_event = threading.Event()
        self.pause = pause or (lambda seconds: self.stop_event.wait(seconds))
        self.peak_kb, self.reason, self.noted, self.survivors, self.ran = 0, None, False, [], 0
        self.pgids = {}      # pid -> process group, as last seen
        self.seen = set()    # (pid, start time) of every process seen in the command's tree
        self.group = None    # the command's own process group, when it is not the wrapper's
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def start(self):
        if self.mem_mb or self.max_seconds:
            self.thread.start()

    def finish(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=self.grace + self.rounds * (self.sweep_every + 1) + 10)

    def exit_status(self):
        return {"memory": EXIT_MEMORY, "time": EXIT_TIME}.get(self.reason)

    def remember(self, rows):
        """Add what is below the command (or below anything remembered that is still alive) to the remembered set; returns the live remembered (pid, start) keys."""
        at = {row[0]: row for row in rows}
        alive = {key for key in self.seen if key[0] in at and _start(at[key[0]]) == key[1]}
        if self.pid in at and (self.pid, _start(at[self.pid])) not in self.seen:
            alive.add((self.pid, _start(at[self.pid])))
        holding = set()
        for pid in self.lock_holders():   # the slot lock's descriptor is inherited by every descendant, a double fork that left the tree included
            if pid in at and pid not in self.me[:2]:
                holding.add((pid, _start(at[pid])))
        alive |= holding & self.candidates   # held at two looks IN A ROW: not a process that only opened the lock file for an instant (a waiting wrapper, holders())
        self.candidates = holding            # the next look compares with this one only; it never grows beyond one look's holders
        own = at.get(self.pid)
        if own is not None and own[2] not in (0, 1, self.me[2]):
            self.group = own[2]
            alive |= {(row[0], _start(row)) for row in rows if row[2] == self.group}
        for pid in tree(rows, [key[0] for key in alive]):
            alive.add((pid, _start(at[pid])))
        alive = {key for key in alive if key[0] not in self.me[:2] and key[0] > 1}
        self.seen |= alive
        self.pgids.update({row[0]: row[2] for row in rows})
        return alive

    def lock_holders(self):
        if self.lock_path is None:
            return set()
        try:
            return self.lsof(self.lock_path)
        except OSError as err:
            if not self.lock_noted:
                self.lock_noted = True
                self.out.write("heavy: cannot list who holds the slot lock (%s): a process that left the command's tree is not found\n" % err)
            return set()

    def sample(self):
        """One look; returns ("memory" | "time" | None, live keys)."""
        live = set()
        try:
            rows = self.read_table()
            live = self.remember(rows)
            at = {row[0]: row for row in rows}
            self.peak_kb = max(self.peak_kb, sum(at[pid][3] for pid, _s in live))
        except OSError as err:
            if not self.noted:
                self.noted = True
                self.out.write("heavy: cannot read the process table (%s): only the command itself can be stopped, and the memory ceiling is not enforced\n" % err)
        if self.mem_mb and self.peak_kb // 1024 > self.mem_mb:
            return "memory", live
        if self.max_seconds and self.clock() - self.started > self.max_seconds:
            self.ran = int(self.clock() - self.started)
            return "time", live
        return None, live

    def watch(self):
        while not self.stop_event.is_set() and not self.pause(self.every):
            reason, live = self.sample()
            if reason:
                self.reason = reason
                self.say(reason)
                if self.decided is not None and reason == "time":
                    try:
                        self.decided()   # the audit line is written before the first signal
                    except Exception as err:
                        self.out.write("heavy: the stop report failed (%s)\n" % _plain("%s: %s" % (type(err).__name__, err), 120))
                self.stop(live)
                return

    def stop(self, live):
        """SIGTERM, the grace, then the sweep: the whole run."""
        self.signal(signal.SIGTERM, live, fallback=True)   # no table: at least the command itself
        self.stop_event.wait(self.grace)   # the grace ends the moment the command is gone; its children may not have gone with it
        self.sweep()

    def sweep(self):
        """Kill what is left of the run, round after round, looking again each time (a child forked since is found; a process that is gone is not signalled)."""
        live = set()
        for _ in range(self.rounds):
            live = self.look()
            if live is None:   # no table: the command itself is all that can be judged
                self.signal(signal.SIGKILL, set(), fallback=True)
                return
            if not live:
                return
            self.signal(signal.SIGKILL, live)
            self.sweep_pause(self.sweep_every)
        live = self.look()
        if live:
            self.survivors = sorted(pid for pid, _s in live)
            self.out.write("heavy: survivors after the stop: %s (a process that left the tree and closed the slot lock cannot be found)\n"
                           % ", ".join(str(pid) for pid in self.survivors))
            self.out.flush()

    def look(self):
        try:
            return self.remember(self.read_table())
        except OSError:
            return None

    def say(self, reason):
        text = _plain(" ".join(self.command), 120)
        if reason == "memory":
            self.out.write("heavy: stopped `%s`: its process tree passed the memory ceiling of %d MB (peak %d MB); FLEET_HEAVY_MEM_MB changes it, 0 turns it off\n"
                           % (text, self.mem_mb, self.peak_kb // 1024))
        else:
            self.out.write("heavy: stopped `%s`: it ran past the time ceiling of %d s (peak %d MB); FLEET_HEAVY_MAX_SECONDS or --ceiling asks for more, up to %d s\n"
                           % (text, self.max_seconds, self.peak_kb // 1024, self.hard_max))
        self.out.flush()

    def signal(self, number, live, fallback=False):
        """Send `number` to each live remembered process, and to the command's own group when it has one. The wrapper, its parent and its group are refused here."""
        grouped = self.group is not None and self.group not in (0, 1, self.me[2]) and any(pid == self.pid for pid, _s in live)   # the group only while the command that led it is there: a gone leader's group id may be another's
        if grouped:
            try:
                self.killpg(self.group, number)
            except OSError:
                pass
        for pid in sorted({pid for pid, _s in live} or ({self.pid} if fallback else set())):
            if pid in self.me[:2] or pid <= 1 or (grouped and self.pgids.get(pid) == self.group):   # a group member has had the group's signal
                continue
            try:
                self.kill(pid, number)
            except OSError:
                pass


def record_stop(info, holder, audit, note=None):
    """The audit line `heavy.stopped`, written BEFORE the stop (holder as the subject; detail: kind, ran_seconds, ceiling_seconds, pid, reason, source: fixed words and
    numbers, never the environment or an argument). A sink that fails never prevents the stop: the failure is said on `note` instead."""
    detail = {"kind": info["kind"], "ran_seconds": info["ran_seconds"], "ceiling_seconds": info["ceiling_seconds"], "pid": info["pid"], "reason": "time", "source": info["source"]}
    try:
        audit("heavy.stopped", holder, detail)
    except Exception as err:
        if note is not None:
            note("heavy: could not write the audit line heavy.stopped (%s): the run is stopped all the same\n" % _plain("%s: %s" % (type(err).__name__, err), 120))


def tell_lane(info, holder, lane, send, audit, hard_max=HARD_MAX_SECONDS, note=None):
    """After the stop: tell the lane ONCE (`send(lane, text)` returns one of STOP_TOLD; an error reads as "failed") and write the audit line `heavy.lane_told` (pid and what
    the lane was told). Fixed words and numbers only. Neither can change the stop or the exit code. Returns what the lane was told."""
    text = ("heavy: your %s run (pid %d) was stopped after %d s; its ceiling was %d s (FLEET_HEAVY_MAX_SECONDS or heavy --ceiling asks for more, up to %d)"
            % (info["kind"], info["pid"], info["ran_seconds"], info["ceiling_seconds"], hard_max))
    try:
        told = send(lane, text)
    except Exception:
        told = "failed"
    told = told if told in STOP_TOLD else "failed"
    try:
        audit("heavy.lane_told", holder, {"pid": info["pid"], "source": info["source"], "lane_told": told})
    except Exception as err:
        if note is not None:
            note("heavy: could not write the audit line heavy.lane_told (%s)\n" % _plain("%s: %s" % (type(err).__name__, err), 120))
    return told


def _held_record(state_dir, index):
    """The record of slot `index` when its lock is held now, else None. Nothing is created."""
    try:
        fd = os.open(slot_path(state_dir, index), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None   # free
        except OSError as err:
            if err.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return None
        return _read_info(fd)
    finally:
        os.close(fd)


def check_orphans(state_dir, slots=DEFAULT_SLOTS, config=None, clock=time.time, on_stop=None, out=None, alive=_alive, watchdog=None, start_of=_start_of):
    """Stop the runs that keep a slot with no wrapper left (the wrapper was killed: the watchdog lived in it). A held slot whose recorded wrapper (pid and start time)
    is gone, and whose run is older than its ceiling counted from the record's own `since`, is stopped like a wrapper's run (SIGTERM, grace, sweep) and reported through
    `on_stop(phase, info, holder, lane)`: phase "decided" BEFORE the stop (the audit line), phase "told" after it (the message to the lane). The stop is rooted in the
    processes that HOLD THE SLOT'S LOCK (seen at two looks) and what they started; the record's own pid is a root only when its recorded start time matches the live
    process (a pid reused by another process, or an older record with no start time, is never a root). Never the wrapper, its parent, its group or pid 1. A record
    without a kind or ceiling (an older one) reads as kind "other". A run whose wrapper is alive is never touched. A run that is looked at again after a stop that left
    survivors is stopped again but reported once (a marker `heavy-told-<slot>.json`, written when the audit line is: a tick that does not stop marks nothing). Returns the infos of what it stopped."""
    out = sys.stderr if out is None else out
    table = ceiling_table(config)
    stopped = []
    for index in list(range(slots)) + [EXPRESS]:
        info = _held_record(state_dir, index)
        if not info:
            continue
        wrapper, since = info.get("wrapper"), info.get("since")
        if not _whole(wrapper, 1, PID_MAX) or isinstance(since, bool) or not isinstance(since, (int, float)):
            continue
        if wrapper == os.getpid() or alive(wrapper, info.get("wstart") if isinstance(info.get("wstart"), str) else ""):
            continue
        kind = info.get("kind") if info.get("kind") in KIND_DEFAULTS else "other"
        ceiling = min(info["ceiling"] if _whole(info.get("ceiling"), 1, 10 ** 9) else table[kind], table["max"])
        ran = clock() - since
        if ran <= ceiling:
            continue
        pid = info.get("pid") if _whole(info.get("pid"), 2, PID_MAX) else 0
        report = {"kind": kind, "ran_seconds": int(ran), "ceiling_seconds": ceiling, "pid": pid, "source": "orphan"}
        holder, lane = _plain(info.get("name", "?")), _plain(info.get("lane", info.get("name", "?")))
        first = not _reported(state_dir, index, info)
        hooked = []

        def decided():
            if first and on_stop is not None:
                hooked.append(1)
                _mark_reported(state_dir, index, info)   # the stop is happening: from now on the run counts as reported
                try:
                    on_stop("decided", report, holder, lane)
                except Exception as err:
                    out.write("heavy: the stop report failed (%s)\n" % _plain("%s: %s" % (type(err).__name__, err), 120))
        if not _stop_orphan(state_dir, index, info, out, clock, watchdog, start_of, decided):
            continue
        out.write("heavy: stopped an orphaned %s run (pid %d): its wrapper was gone and it ran past the time ceiling of %d s\n" % (kind, pid, ceiling))
        out.flush()
        stopped.append(report)
        if hooked and on_stop is not None:
            try:
                on_stop("told", report, holder, lane)
            except Exception as err:
                out.write("heavy: the stop report failed (%s)\n" % _plain("%s: %s" % (type(err).__name__, err), 120))
    return stopped


def _marker_path(state_dir, index):
    return os.path.join(str(state_dir), "heavy-told-%s.json" % index)


def _marker_key(info):
    return {"since": info.get("since"), "pid": info.get("pid")}


def _reported(state_dir, index, info):
    """True when this run (slot, `since`, pid) was already reported (by a stop that happened)."""
    try:
        with open(_marker_path(state_dir, index)) as handle:
            return json.loads(handle.read(512)) == _marker_key(info)
    except (OSError, ValueError):
        return False


def _mark_reported(state_dir, index, info):
    """Written at the moment the audit line is: only a stop that HAPPENS marks the run reported. Without the marker (it cannot be written) the run may be told again,
    the lesser harm."""
    try:
        fd = os.open(_marker_path(state_dir, index), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(_marker_key(info)))
    except OSError:
        pass


def _stop_orphan(state_dir, index, info, out, clock, watchdog, start_of, decided):
    """Stop what holds slot `index`; False when another checker is doing it, the slot changed hands meanwhile, or nothing holds the lock. `decided()` runs right before
    the first signal."""
    try:
        gate = os.open(os.path.join(str(state_dir), ORPHAN_LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        pid = info.get("pid") if _whole(info.get("pid"), 2, PID_MAX) else 0
        recorded = info.get("cstart")
        if not (pid and isinstance(recorded, str) and recorded and start_of(pid) == recorded):
            pid = 0   # no recorded start time, or another process now has the pid: only what holds the lock is a root
        dog = Watchdog(pid, ["orphan"], 0, 0, out, clock(), clock=clock, **dict({"lock_path": slot_path(state_dir, index)}, **(watchdog or {})))
        for _ in range(2):
            dog.look()   # the lock's holders count when seen at two looks in a row
        now = _held_record(state_dir, index)
        if not now or now.get("since") != info.get("since") or now.get("pid") != info.get("pid"):
            return False   # freed, or taken by another run since the check began: not this one
        live = dog.look()
        if not live:
            return False
        decided()
        dog.stop(live)
        return True
    finally:
        os.close(gate)


def run(command, state_dir, slots=DEFAULT_SLOTS, wait_max=DEFAULT_WAIT_MAX, name=None, environ=None, out=None, clock=time.time, sleep=time.sleep,
        which=shutil.which, watchdog=None, short=False, line=None, ceiling=None, ceiling_config=None, on_stop=None):
    """Wait for a slot, run `command` in it, return its exit status. 75 when no slot came within `wait_max` seconds, 127 when the command does not exist.
    A watchdog stops a runaway: 137 past the memory ceiling, 124 past the time ceiling of the run's KIND (see `classify`, `resolve_ceiling`: `ceiling` is the run's own
    request, else FLEET_HEAVY_MAX_SECONDS; `ceiling_config` the config's `heavy` value; `on_stop(phase, info, holder, lane)` is called with "decided" before a time stop and "told" after it (also for an orphan this wrapper finds while it waits)); `watchdog` is a dict of Watchdog keyword arguments and `line` one of Line's (tests).
    Callers wait in arrival order (`Line`); `short` declares the command short: it may use the express slot when the ordinary ones are busy and is stopped after
    EXPRESS_SECONDS by the watchdog."""
    environ = os.environ if environ is None else environ
    out = sys.stderr if out is None else out
    if _resolve(command, environ) is None:
        out.write("heavy: %s: command not found\n" % command[0])
        return EXIT_NOT_FOUND
    name = name or holder_name(environ)
    mode = should_take_slot(environ)   # "off": FLEET_HEAVY=off or CI, run it as it is; "inside": already in a slot (a nested heavy), no second one
    if mode == "inside":
        out.write("heavy: already inside a slot: running without a second one\n")
    unslotted = None if mode == "taken" and state_dir is not None else ("" if mode == "off" else "no slot directory" if mode == "taken" else "inside")
    started = clock()
    try:
        every = float(environ.get("FLEET_HEAVY_REPORT_SECONDS") or WAIT_REPORT_SECONDS)   # 30 s; a test shortens it
    except ValueError:
        every = WAIT_REPORT_SECONDS
    next_report = started + every
    got = {"signal": None}
    child = {"p": None}

    def on_signal(number, _frame):
        got["signal"] = number
        if child["p"] is not None:
            try:
                child["p"].send_signal(number)
            except OSError:
                pass

    old = {number: signal.signal(number, on_signal) for number in FORWARDED}
    fd = None
    queue = None
    queue_noted = False
    try:
        if unslotted is None:
            try:
                queue = Line(state_dir, name, short, **dict({"slots": slots, "note": out.write}, **(line or {})))
            except OSError:
                queue = None
        while unslotted is None:
            place = None
            if queue is not None:
                try:
                    place = queue.place()
                except QueueUnusable as err:   # the order cannot be kept: the present behaviour (whoever polls first), said once
                    if not queue_noted:
                        queue_noted = True
                        out.write("heavy: no waiting order (%s): taking whichever slot is free\n" % err)
                    queue.leave()   # an entry it already wrote must not stay behind it: a head that is not waiting would hold the others back
                    queue = None
            try:
                fd = None
                if place is None or place == 1:   # only the head of the line tries an ordinary slot
                    fd, index = _take(state_dir, slots)
                if fd is None and short:
                    fd = _take_express(state_dir)
                    index = EXPRESS
            except OSError as err:   # a missing, unwritable or locked-out directory never stops the command: one line, and it runs without a slot
                out.write("heavy: no slot (%s): running without one\n" % (err.strerror or err))
                unslotted = "unusable"
                break
            if fd is not None:
                break
            if got["signal"] is not None:
                return 128 + got["signal"]
            now = clock()
            if now - started >= wait_max:
                out.write("heavy: no slot after %d s (%d of %d busy: %s): the command was not run\n" % (int(now - started), slots, slots, _describe(holders(state_dir, slots))))
                out.flush()
                return EXIT_TEMPFAIL
            if now >= next_report:
                try:
                    check_orphans(state_dir, slots, ceiling_config, clock, on_stop, out)   # a run whose wrapper died keeps a slot for nobody: bounded here, where it starves someone
                except Exception:
                    pass
                held = holders(state_dir, slots)
                where = ", place %d in line (%d ahead)" % (place, place - 1) if place is not None else ""
                out.write("heavy: waiting for a slot (%d of %d busy: %s)%s\n" % (len(held), slots, _describe(held), where))
                out.flush()
                next_report = now + every
            sleep(POLL_SECONDS)
        if queue is not None and not (queue.leave() or queue.leave()):   # it has its slot (or none will come): out of the line at once; one more try before it says so
            out.write("heavy: could not leave the waiting order: the others pass this caller's ticket once its slot shows as held\n")
        env = dict(environ)
        kind = classify(command, short)
        mem_mb = ceilings(environ)[0]
        max_seconds = resolve_ceiling(kind, environ, ceiling, ceiling_config)
        if fd is not None and short and index == EXPRESS:   # the express slot's own time ceiling (an environment value may only make it shorter)
            max_seconds = min(max_seconds, EXPRESS_SECONDS)
        lane = holder_name(environ)
        extra = {"kind": kind, "ceiling": max_seconds, "lane": lane, "wstart": _own_start() if fd is not None else ""}
        if fd is not None:
            _record(fd, name, os.getpid(), command[0], clock(), extra)   # at once, so a killed holder's old record is never shown for this one
            os.set_inheritable(fd, True)   # the command keeps the lock: the slot is busy while it runs, whatever happens to this wrapper
            env[SLOT_ENV] = str(index)    # and a heavy inside it knows it is in a slot: it takes no second one (nested heavy never deadlocks)
        argv = (command_prefix(which) if unslotted not in ("", "inside") else []) + list(command)   # off or nested: the command as it is
        try:
            child["p"] = subprocess.Popen(argv, env=env, pass_fds=(fd,) if fd is not None else ())
        except OSError as err:
            out.write("heavy: cannot run %s: %s\n" % (command[0], err.strerror or err))
            return EXIT_NOT_FOUND
        if fd is not None:
            _record(fd, name, child["p"].pid, command[0], clock(), dict(extra, cstart=_start_of(child["p"].pid)))
        if got["signal"] is not None:   # it arrived between taking the slot and the start: the command gets it too
            try:
                child["p"].send_signal(got["signal"])
            except OSError:
                pass
        lock = slot_path(state_dir, index) if fd is not None else None
        def stop_info():
            return {"kind": kind, "ran_seconds": dog.ran, "ceiling_seconds": max_seconds, "pid": child["p"].pid, "source": "wrapper"}

        def decided():   # before the first signal: the audit line
            if on_stop is not None:
                on_stop("decided", stop_info(), name, lane)
        dog = Watchdog(child["p"].pid, command, mem_mb, max_seconds, out, clock(), clock=clock,
                       **dict({"lock_path": lock, "hard_max": ceiling_table(ceiling_config)["max"], "decided": decided}, **(watchdog or {})))
        dog.start()
        try:
            while True:
                try:
                    status = child["p"].wait()
                    break
                except InterruptedError:   # a signal during wait: Python retries; this is belt and braces
                    continue
        finally:
            dog.finish()
        if dog.reason == "time" and on_stop is not None:   # after the stop; it never changes the exit status
            try:
                on_stop("told", stop_info(), name, lane)
            except Exception as err:
                out.write("heavy: the stop report failed (%s)\n" % _plain("%s: %s" % (type(err).__name__, err), 120))
        if dog.exit_status():
            return dog.exit_status()
        return 128 - status if status < 0 else status   # a signal: 128 + n, as a shell reports it
    finally:
        if queue is not None:
            queue.leave()
        for number, handler in old.items():
            signal.signal(number, handler)
        if fd is not None:
            os.close(fd)   # the record stays: `holders` lists only slots whose lock is held, and the next holder overwrites it


def wrap_argv(argv, environ, python, switchboard, script, err=None, state_dir=None):
    """The command line that re-runs `script argv` through `heavy`, or None when it should run as it is (FLEET_HEAVY=off, a CI run, already in a slot, or the
    state directory cannot be written: then ONE line on `err` and the caller runs unslotted: a missing slot machinery never stops the suite). Used by bin/test-switchboard."""
    err = sys.stderr if err is None else err
    if should_take_slot(environ) != "taken":
        return None
    try:
        probe = str(state_dir) if state_dir is not None else os.path.join(environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state"), "fleet-switchboard")
        os.makedirs(probe, mode=0o700, exist_ok=True)
        if not os.access(probe, os.W_OK):
            raise OSError("not writable")
    except OSError as error:
        err.write("heavy: no slot machinery here (%s): running without a slot\n" % (error.strerror or error))
        return None
    return [python, switchboard, "heavy", "--name", os.path.basename(script), "--", python, script] + list(argv)
