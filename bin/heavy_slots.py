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
FORWARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class HeavyError(Exception):
    """A refusal with a sentence for the caller."""


def settings(raw):
    """The slot count from the config's `heavy` value (None, or {"slots": n}); HeavyError for anything else."""
    if raw is None:
        return DEFAULT_SLOTS
    if not isinstance(raw, dict) or set(raw) - {"slots"}:
        raise HeavyError("'heavy' must be an object with only \"slots\" in it, got %r" % (raw,))
    slots = raw.get("slots", DEFAULT_SLOTS)
    if not isinstance(slots, int) or isinstance(slots, bool) or not 1 <= slots <= SLOTS_MAX:
        raise HeavyError("'heavy.slots' must be a whole number from 1 to %d, got %r" % (SLOTS_MAX, slots))
    return slots


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
    return os.path.join(str(state_dir), "heavy-slot-%d.lock" % index)


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


def _record(fd, name, pid, command, now):
    """Write who holds the slot (truncating what the last holder wrote). Best effort: a slot without a record is still a held slot."""
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps({"name": name, "pid": pid, "command": os.path.basename(command)[:80], "since": int(now)}).encode("utf-8"), 0)
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
        if len(parts) >= 5 and all(part.isdigit() for part in parts[:4]):
            rows.append(tuple(int(part) for part in parts[:4]) + (" ".join(parts[4:]),))
    if done.returncode != 0 or not rows:
        raise OSError("ps gave no process table")
    return rows


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
                 kill=os.kill, every=WATCH_SECONDS, grace=KILL_GRACE_SECONDS, rounds=SWEEP_ROUNDS, sweep_every=SWEEP_SECONDS, sweep_pause=time.sleep, me=None):
        self.pid, self.command, self.mem_mb, self.max_seconds, self.out, self.started = pid, command, mem_mb, max_seconds, out, started
        self.read_table, self.clock, self.killpg, self.kill, self.every, self.grace = read_table, clock, killpg, kill, every, grace
        self.rounds, self.sweep_every, self.sweep_pause = rounds, sweep_every, sweep_pause
        self.me = me if me is not None else (os.getpid(), os.getppid(), os.getpgrp())   # never signalled: this wrapper, its caller, its group
        self.stop_event = threading.Event()
        self.pause = pause or (lambda seconds: self.stop_event.wait(seconds))
        self.peak_kb, self.reason, self.noted, self.survivors = 0, None, False, []
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
            return "time", live
        return None, live

    def watch(self):
        while not self.stop_event.is_set() and not self.pause(self.every):
            reason, live = self.sample()
            if reason:
                self.reason = reason
                self.say(reason)
                self.signal(signal.SIGTERM, live, fallback=True)   # no table: at least the command itself
                self.stop_event.wait(self.grace)   # the grace ends the moment the command is gone; its children may not have gone with it
                self.sweep()
                return

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
            self.out.write("heavy: survivors after the stop: %s\n" % ", ".join(str(pid) for pid in self.survivors))
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
            self.out.write("heavy: stopped `%s`: it ran past the time ceiling of %d s (peak %d MB); FLEET_HEAVY_MAX_SECONDS changes it, 0 turns it off\n"
                           % (text, self.max_seconds, self.peak_kb // 1024))
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


def run(command, state_dir, slots=DEFAULT_SLOTS, wait_max=DEFAULT_WAIT_MAX, name=None, environ=None, out=None, clock=time.time, sleep=time.sleep,
        which=shutil.which, watchdog=None):
    """Wait for a slot, run `command` in it, return its exit status. 75 when no slot came within `wait_max` seconds, 127 when the command does not exist.
    A watchdog stops a runaway: 137 past the memory ceiling, 124 past the time ceiling (see `ceilings`); `watchdog` is a dict of Watchdog keyword arguments (tests)."""
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
    try:
        while unslotted is None:
            try:
                fd, index = _take(state_dir, slots)
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
                held = holders(state_dir, slots)
                out.write("heavy: waiting for a slot (%d of %d busy: %s)\n" % (len(held), slots, _describe(held)))
                out.flush()
                next_report = now + every
            sleep(POLL_SECONDS)
        env = dict(environ)
        if fd is not None:
            _record(fd, name, os.getpid(), command[0], clock())   # at once, so a killed holder's old record is never shown for this one
            os.set_inheritable(fd, True)   # the command keeps the lock: the slot is busy while it runs, whatever happens to this wrapper
            env[SLOT_ENV] = str(index)    # and a heavy inside it knows it is in a slot: it takes no second one (nested heavy never deadlocks)
        argv = (command_prefix(which) if unslotted not in ("", "inside") else []) + list(command)   # off or nested: the command as it is
        try:
            child["p"] = subprocess.Popen(argv, env=env, pass_fds=(fd,) if fd is not None else ())
        except OSError as err:
            out.write("heavy: cannot run %s: %s\n" % (command[0], err.strerror or err))
            return EXIT_NOT_FOUND
        if fd is not None:
            _record(fd, name, child["p"].pid, command[0], clock())
        if got["signal"] is not None:   # it arrived between taking the slot and the start: the command gets it too
            try:
                child["p"].send_signal(got["signal"])
            except OSError:
                pass
        mem_mb, max_seconds = ceilings(environ)
        dog = Watchdog(child["p"].pid, command, mem_mb, max_seconds, out, clock(), clock=clock, **(watchdog or {}))
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
        if dog.exit_status():
            return dog.exit_status()
        return 128 - status if status < 0 else status   # a signal: 128 + n, as a shell reports it
    finally:
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
