"""The watchdog and the orphan check of the heavy wrapper (ask #2034): a part of heavy_slots.py, loaded by it at its end.

The part never imports heavy_slots (four loaders do not register it in sys.modules): heavy_slots.py hands it the names it reads, as copies taken at load, before
this file runs, and re-exports the names defined here."""

import errno
import fcntl
import json
import os
import sys
import threading
import time


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
    detail.update({key: info[key] for key in ("slot", "waited_seconds") if key in info})   # a run that held a slot says which and how long it waited for it (the ceiling never counts that wait)
    try:
        audit("heavy.stopped", holder, detail)
    except Exception as err:
        if note is not None:
            note("heavy: could not write the audit line heavy.stopped (%s): the run is stopped all the same\n" % _plain("%s: %s" % (type(err).__name__, err), 120))


def record_lost(info, audit, note=None):
    """The audit line `heavy.lost`: a wrapper that died while it waited in the line (its name as the subject; detail: waited_seconds, last_seen, last_seen_age_seconds,
    reason, source). A sink that fails is said on `note` and never raises."""
    detail = {"reason": "wrapper gone while queued", "waited_seconds": info["waited_seconds"], "last_seen": info["last_seen"], "last_seen_age_seconds": info["last_seen_age_seconds"], "source": "queue"}
    try:
        audit("heavy.lost", info["name"], detail)
    except Exception as err:
        if note is not None:
            note("heavy: could not write the audit line heavy.lost (%s)\n" % _plain("%s: %s" % (type(err).__name__, err), 120))


def tell_lane(info, holder, lane, send, audit, hard_max=HARD_MAX_SECONDS, note=None):
    """After the stop: tell the lane ONCE (`send(lane, text)` returns one of STOP_TOLD; an error reads as "failed") and write the audit line `heavy.lane_told` (pid and what
    the lane was told). Fixed words and numbers only. Neither can change the stop or the exit code. Returns what the lane was told."""
    waited = " (waited %d s for the slot first; the ceiling counts only the run)" % info["waited_seconds"] if "waited_seconds" in info else ""
    text = ("heavy: your %s run (pid %d) was stopped after %d s%s; its ceiling was %d s (FLEET_HEAVY_MAX_SECONDS or heavy --ceiling asks for more, up to %d)"
            % (info["kind"], info["pid"], info["ran_seconds"], waited, info["ceiling_seconds"], hard_max))
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
        report = {"kind": kind, "ran_seconds": int(ran), "ceiling_seconds": ceiling, "pid": pid, "source": "orphan", "slot": index}
        if _whole(info.get("waited"), 0, 10 ** 9):
            report["waited_seconds"] = info["waited"]
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
