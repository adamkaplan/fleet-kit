"""The CPU guard (the P0 CPU order, item D). Pure of the switchboard: the caller hands it numbers, the clock and a state directory.

The load average (one `os.getloadavg()`, a system call: the one-minute figure is shown, the FIVE-minute figure drives the alarm, so a one-minute spike never wakes anyone) over the number of cores (`os.cpu_count()`) is the ratio. A pass reads nothing else and starts no
process. When the ratio stays above `high_ratio` for `minutes` the Chief of Staff is told ONCE for the episode; the episode ends when the ratio falls below
two thirds of the guard (the default guard of 3 re-arms below 2), and it is kept in `cpu-guard.json` so a restart does not begin another. The only process the
guard ever starts is ONE bounded `ps` (and, only on an alarm, ONE bounded `lsof` of the top ten processes, to name each one's lane: the agent or worktree it runs
in) when an episode begins, on a thread, to name the top consumers.

Memory (ask #420): a `mem` figure beside the CPU one, because a load that is paging is not CPU. Swap and free memory come from the kernel's counters
(`sysctl` by name through ctypes on macOS, `/proc/meminfo` on Linux) and the biggest holders by program from the kernel's process table (`libproc` on macOS,
`/proc` on Linux), so building the line starts NO process. When swap stays above `swap_percent` for `swap_minutes` the Chief of Staff is told once per episode."""

import ctypes
import json
import math
import os
import re
import sys
import tempfile
import time

DEFAULTS = {"high_ratio": 3.0, "minutes": 5}
MEM_DEFAULTS = {"swap_percent": 90, "swap_minutes": 5}
STATE_FILE = "cpu-guard.json"
MEM_STATE_FILE = "mem-guard.json"
SLOTS_FILE = "heavy-slots.json"
REARM_NUM, REARM_DEN = 2, 3        # re-arm below high_ratio * 2/3 (floats, compared directly)
HEAVY_SLOTS_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "heavy_slots.py")   # the optional module of the `heavy` command
PS_ARGV = ["ps", "-axo", "pid=,pcpu=,comm="]
PS_SECONDS = 5
LSOF_SECONDS = 5
LANE_PIDS = 10                      # the top processes whose lane is looked up (one bounded lsof)
WAKE_GRACE_SECONDS = PS_SECONDS + LSOF_SECONDS + 2   # the wake waits this long for the consumers; a later answer is ONE follow-up note
PS_BYTES = 2000000
CATEGORIES = 5
PROGRAMS_MAX = 2000                 # distinct program names kept by the process-table walk


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate(raw, source="config"):
    """Raises ValueError: `cpu_guard` is absent or null (the defaults), false (off), or {"high_ratio": > 0, "minutes": 1..120}."""
    if raw is None or raw is False:
        return
    if not isinstance(raw, dict):
        raise ValueError("%s: 'cpu_guard' must be false, null or an object, got %r" % (source, raw))
    allowed = list(DEFAULTS) + list(MEM_DEFAULTS)
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ValueError("%s: 'cpu_guard' has unknown key(s) %s (it takes %s)" % (source, ", ".join(unknown), ", ".join(allowed)))
    percent = raw.get("swap_percent", MEM_DEFAULTS["swap_percent"])
    if not _number(percent) or not 20 <= percent <= 100:
        raise ValueError("%s: 'cpu_guard.swap_percent' must be a number from 20 to 100 (the share of swap in use), got %r" % (source, percent))
    swap_minutes = raw.get("swap_minutes", MEM_DEFAULTS["swap_minutes"])
    if not _number(swap_minutes) or not 1 <= swap_minutes <= 120:
        raise ValueError("%s: 'cpu_guard.swap_minutes' must be a number from 1 to 120, got %r" % (source, swap_minutes))
    ratio = raw.get("high_ratio", DEFAULTS["high_ratio"])
    if not _number(ratio) or not 0 < ratio <= 1000:
        raise ValueError("%s: 'cpu_guard.high_ratio' must be a number above 0 (load over cores), got %r" % (source, ratio))
    minutes = raw.get("minutes", DEFAULTS["minutes"])
    if not _number(minutes) or not 1 <= minutes <= 120:
        raise ValueError("%s: 'cpu_guard.minutes' must be a number from 1 to 120, got %r" % (source, minutes))


def settings(raw):
    """The merged settings, or None when the guard is off (`cpu_guard: false`)."""
    if raw is False:
        return None
    merged = dict(DEFAULTS)
    merged.update(raw or {})
    merged["rearm_ratio"] = merged["high_ratio"] * REARM_NUM / float(REARM_DEN)
    return merged


def mem_settings(raw):
    """The memory alarm's settings as an Episode config (`high_ratio` is the swap percent), or None when the guard is off (`cpu_guard: false`)."""
    if raw is False:
        return None
    merged = dict(MEM_DEFAULTS)
    merged.update({k: v for k, v in (raw or {}).items() if k in MEM_DEFAULTS})
    return {"high_ratio": merged["swap_percent"], "minutes": merged["swap_minutes"], "rearm_ratio": max(merged["swap_percent"] - 10, 0)}


class Reading:
    """What the system said: `load1`, `load5` (finite and not negative, or None), `cores` (a positive whole number, or None) and `problem` (None, or why
    the load could not be read). The ratio is known only when both the load and the cores are."""

    def __init__(self, load1=None, load5=None, cores=None, problem=None):
        self.load1, self.load5, self.cores, self.problem = load1, load5, cores, problem

    @property
    def ratio_known(self):
        return self.problem is None and self.load5 is not None and self.cores is not None


def _good_load(value):
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def reading():
    """A Reading: one `os.getloadavg()` and one `os.cpu_count()`, system calls with no process. A load that cannot be read, or is not a finite number of at
    least 0, is a problem (said, never guessed); a core count that is missing, 0 or negative leaves `cores` None (the ratio is then UNKNOWN, never taken as one core)."""
    try:
        loads = os.getloadavg()   # looked up at the call, so a test can replace it
        load1, load5 = _good_load(loads[0]), _good_load(loads[1])
    except (OSError, AttributeError, ValueError, TypeError, IndexError) as err:
        return Reading(problem="%s: %s" % (type(err).__name__, err) if str(err) else type(err).__name__)
    if load1 is None or load5 is None:
        return Reading(problem="the load average is not a finite number of at least 0")
    try:
        cores = os.cpu_count()
        cores = int(cores) if cores is not None and int(cores) > 0 else None
    except (OSError, ValueError, TypeError):
        cores = None
    return Reading(load1, load5, cores)


class Memory:
    """What the kernel says about memory: `swap_used`, `swap_total`, `free` in bytes (or None when a figure cannot be read) and `problem` (None, or why nothing
    could be read). An unreadable figure is never shown as zero."""

    def __init__(self, swap_used=None, swap_total=None, free=None, problem=None):
        self.swap_used, self.swap_total, self.free, self.problem = swap_used, swap_total, free, problem

    @property
    def swap_percent(self):
        """The share of swap in use, 0 for a machine with no swap; None when it could not be read."""
        if self.problem is not None or self.swap_used is None or self.swap_total is None:
            return None
        return 100.0 * self.swap_used / self.swap_total if self.swap_total > 0 else 0.0


def _sysctl_bytes(libc, name, fmt):
    value = fmt()
    size = ctypes.c_size_t(ctypes.sizeof(value))
    return value if libc.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0) == 0 else None


def memory_reading():
    """A Memory from the kernel's counters, no process: `sysctl` by name through ctypes on macOS (`vm.swapusage`, `vm.page_free_count`,
    `vm.page_speculative_count`, `hw.pagesize`), `/proc/meminfo` on Linux. Free memory is free plus speculative pages on macOS, `MemAvailable` on Linux."""
    try:
        if sys.platform == "darwin":
            libc = ctypes.CDLL("libc.dylib", use_errno=True)

            class Swap(ctypes.Structure):
                _fields_ = [("total", ctypes.c_uint64), ("avail", ctypes.c_uint64), ("used", ctypes.c_uint64), ("pagesize", ctypes.c_uint32), ("encrypted", ctypes.c_int)]
            swap = _sysctl_bytes(libc, b"vm.swapusage", Swap)
            free_pages = _sysctl_bytes(libc, b"vm.page_free_count", ctypes.c_uint32)
            speculative = _sysctl_bytes(libc, b"vm.page_speculative_count", ctypes.c_uint32)
            page = _sysctl_bytes(libc, b"hw.pagesize", ctypes.c_uint32)
            if swap is None:
                return Memory(problem="vm.swapusage cannot be read")
            free = (free_pages.value + (speculative.value if speculative is not None else 0)) * page.value if free_pages is not None and page is not None else None
            return Memory(int(swap.used), int(swap.total), free)
        if sys.platform.startswith("linux"):
            figures = {}
            with open("/proc/meminfo") as handle:
                for line in handle.read(65536).splitlines():
                    key, _, rest = line.partition(":")
                    parts = rest.split()
                    if parts and parts[0].isdigit():
                        figures[key] = int(parts[0]) * 1024
            if "SwapTotal" not in figures or "SwapFree" not in figures:
                return Memory(problem="/proc/meminfo has no swap figures")
            return Memory(figures["SwapTotal"] - figures["SwapFree"], figures["SwapTotal"], figures.get("MemAvailable"))
        return Memory(problem="no memory counters on %s" % sys.platform)
    except (OSError, AttributeError, ValueError, TypeError) as err:
        return Memory(problem="%s: %s" % (type(err).__name__, err) if str(err) else type(err).__name__)


def process_table(limit_seconds=2.0, clock=time.monotonic):
    """{program: (count, resident bytes)} of this user's processes from the kernel's table, no process started: `libproc` through ctypes on macOS, `/proc` on
    Linux. None when it cannot be read. Stops after `limit_seconds`: a starved machine is never held up by it."""
    deadline = clock() + limit_seconds
    totals = {}
    try:
        if sys.platform == "darwin":
            import ctypes.util
            lib = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)

            class TaskInfo(ctypes.Structure):
                _fields_ = [("virtual", ctypes.c_uint64), ("resident", ctypes.c_uint64), ("user", ctypes.c_uint64), ("system", ctypes.c_uint64), ("policy", ctypes.c_uint64),
                            ("ps", ctypes.c_uint64), ("faults", ctypes.c_int32), ("pageins", ctypes.c_int32), ("cow", ctypes.c_int32), ("sent", ctypes.c_int32),
                            ("received", ctypes.c_int32), ("mach", ctypes.c_int32), ("unix", ctypes.c_int32), ("switches", ctypes.c_int32), ("threads", ctypes.c_int32),
                            ("running", ctypes.c_int32), ("priority", ctypes.c_int32)]
            count = lib.proc_listallpids(None, 0)
            if count <= 0:
                return None
            pids = (ctypes.c_int * (count + 64))()
            got = lib.proc_listallpids(pids, ctypes.sizeof(pids))
            name = ctypes.create_string_buffer(256)
            for i in range(max(got, 0)):
                if clock() > deadline:
                    break
                pid = pids[i]
                info = TaskInfo()
                if pid <= 0 or lib.proc_pidinfo(pid, 4, 0, ctypes.byref(info), ctypes.sizeof(info)) <= 0:
                    continue   # not this user's, or gone
                name.value = b""   # a failed call never leaves the previous process's name behind
                if lib.proc_name(pid, name, 256) <= 0:
                    continue
                program = _clean_name(name.value.decode("utf-8", "replace"))
                if program not in totals and len(totals) >= PROGRAMS_MAX:
                    continue
                held = totals.get(program, (0, 0))
                totals[program] = (held[0] + 1, held[1] + int(info.resident))
            return totals
        if sys.platform.startswith("linux"):
            page = os.sysconf("SC_PAGE_SIZE")
            for entry in os.listdir("/proc"):
                if clock() > deadline:
                    break
                if not entry.isdigit():
                    continue
                try:
                    with open("/proc/%s/statm" % entry) as handle:
                        resident = int(handle.read(200).split()[1]) * page
                    with open("/proc/%s/comm" % entry) as handle:
                        program = _clean_name(handle.read(200))
                except (OSError, ValueError, IndexError):
                    continue
                if program not in totals and len(totals) >= PROGRAMS_MAX:
                    continue
                held = totals.get(program, (0, 0))
                totals[program] = (held[0] + 1, held[1] + resident)
            return totals
    except (OSError, AttributeError, ValueError, TypeError):
        return None
    return None


def biggest_holders(table, limit=4):
    """[(program, count, megabytes)] largest first from a process_table result; [] for None or empty."""
    if not table:
        return []
    ranked = sorted(table.items(), key=lambda kv: (-kv[1][1], kv[0]))[:limit]
    return [(name, count, int(round(resident / 1e6))) for name, (count, resident) in ranked]


def _gb(value):
    return "%.1f" % (value / 1e9)


def mem_status_line(mem, holders=None):
    """`swap 9.1 of 10.2 GB (89%), free 0.9 GB; opencode 62 procs 18300 MB, ...`; `unreadable: <reason>` when the counters cannot be read (never a zero)."""
    if mem.problem is not None:
        return "unreadable: %s" % re.sub(r"[^A-Za-z0-9_.:, /-]", "?", mem.problem)[:80]
    if mem.swap_total == 0:
        line = "no swap"
    else:
        line = "swap %s of %s GB (%d%%)" % (_gb(mem.swap_used), _gb(mem.swap_total), round(mem.swap_percent))
    line += ", free %s GB" % _gb(mem.free) if mem.free is not None else ", free memory unreadable"
    if holders:
        line += "; " + ", ".join("%s %d procs %d MB" % h for h in holders)
    return line


def mem_wake_text(mem, holders, cfg, prog):
    """The once-per-episode memory wake: numbers, program names and counts only."""
    text = "memory high: swap %s of %s GB (%d%%; above %g%% for %g minutes), free %s GB." % (
        _gb(mem.swap_used), _gb(mem.swap_total), round(mem.swap_percent), cfg["high_ratio"], cfg["minutes"], _gb(mem.free) if mem.free is not None else "unreadable")
    if holders:
        text += " Biggest holders: " + ", ".join("%s %d procs %d MB" % h for h in holders) + "."
    return text + " A load that is paging is not CPU: `%s status` shows the cpu and mem lines." % prog


def _holder_text(row, now):
    """`name command 4m` for one holder, cleaned and short; None for a row that is not usable."""
    if not isinstance(row, dict):
        return None
    clean = lambda v, n: re.sub(r"[^A-Za-z0-9_.+ -]", "?", str(v))[:n].strip()
    name, command, since = clean(row.get("name", ""), 24), clean(row.get("command", ""), 24), row.get("since")
    age = ""
    if _number(since) and since > 0:
        minutes = max(0, int((now - since) // 60))
        age = " %dm" % minutes if minutes < 120 else " %dh" % (minutes // 60)
    text = (name + " " + command).strip() + age
    return text or None


def slots_text(config, state_dir, now=None):
    """`heavy slots 2/3 (coder-live make 4m, ...)` from the optional `heavy_slots` module of the `heavy` command (bin/heavy_slots.py, loaded by path):
    `settings(config.get("heavy"))` is the number of slots and `holders(state_dir, max)` the rows `{slot, name, pid, command, since}` of the slots whose lock is
    held NOW (the kernel's flock is the truth: no file to lie after a kill; no process is started). A missing module, or any trouble with it, adds nothing."""
    try:
        import importlib.util
        import itertools
        import time
        path = HEAVY_SLOTS_PATH
        if not os.path.isfile(path):
            return None
        spec = importlib.util.spec_from_file_location("heavy_slots", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        total = module.settings((config or {}).get("heavy"))
        if isinstance(total, dict):   # a settings dict carries the number as "slots" or "max"
            total = total.get("slots", total.get("max"))
        if isinstance(total, bool) or not isinstance(total, int) or not 1 <= total <= 1000:
            return None
        rows = list(itertools.islice(module.holders(state_dir, total), 1001))   # a runaway list is cut
        stamp = time.time() if now is None else now
        shown = [t for t in (_holder_text(r, stamp) for r in rows[:3]) if t]
        return "heavy slots %d/%d%s" % (len(rows), total, " (%s%s)" % (", ".join(shown), ", ..." if len(rows) > 3 else "") if shown else "")
    except BaseException:   # a module that exits or raises must not take `status` or the daemon's pass with it
        return None


def status_line(cfg, read, slots=None):
    """`load 86 on 14 cores (6.1x) [HIGH]`; `load 86, cores unknown` when the core count is not known (no ratio, no marker); `unreadable: <reason>` when the
    load is not. The DISPLAYED load is the one-minute average; the alarm uses the five-minute one."""
    if read.problem is not None:
        return "unreadable: %s" % re.sub(r"[^A-Za-z0-9_.:, -]", "?", read.problem)[:80]
    if read.cores is None:
        line = "load %d, cores unknown" % round(read.load1)
    else:
        ratio = read.load1 / read.cores
        line = "load %d on %d cores (%.1fx)" % (round(read.load1), read.cores, ratio)
        if cfg is not None and ratio > cfg["high_ratio"]:
            line += " [HIGH]"
    if slots:
        line += "; " + slots
    return line


class Episode:
    """The state machine. `step(ratio, now, cfg, can_begin)` returns "begin" once when the ratio has stayed above the guard for `minutes` and an episode may
    begin (a Chief of Staff exists), "end" when an open episode falls below the re-arm ratio, else None. `episode` is the epoch it began (None when closed)."""

    def __init__(self, episode=None):
        self.episode = episode
        self.high_since = None

    def step(self, ratio, now, cfg, can_begin=True):
        if self.episode is not None:
            if ratio < cfg["rearm_ratio"]:
                self.episode, self.high_since = None, None
                return "end"
            return None
        if ratio > cfg["high_ratio"]:
            if self.high_since is None:
                self.high_since = now
            if now - self.high_since >= cfg["minutes"] * 60 and can_begin:
                self.episode = now
                return "begin"
        else:
            self.high_since = None
        return None


def load_episode(state_dir, name=STATE_FILE):
    try:
        with open(os.path.join(str(state_dir), name)) as handle:
            raw = json.loads(handle.read(4096))
        value = raw.get("episode")
        return float(value) if _number(value) else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def save_episode(state_dir, episode, name=STATE_FILE):
    """Atomic, 0600, written only when the episode changes (a few times a year). Raises OSError."""
    path = os.path.join(str(state_dir), name)
    fd, temp = tempfile.mkstemp(prefix=name + ".", suffix=".tmp", dir=str(state_dir))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps({"episode": episode}, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _clean_name(raw):
    """The program's base name only: the path is dropped, and so is anything that looks like an argument (a command line is never copied)."""
    name = os.path.basename(str(raw).strip())
    name = re.split(r"\s+-", name)[0]
    return re.sub(r"[^A-Za-z0-9_.+-]", "?", name)[:40] or "?"


def process_rows(ps_text):
    """[(pid or None, pcpu, name)] from `ps -axo pid=,pcpu=,comm=` text (or the older two-column `pcpu=,comm=`, whose rows carry no pid). Bounded."""
    rows = []
    for line in (ps_text or "")[:PS_BYTES].splitlines():
        row = None
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            try:
                row = (int(parts[0]), float(parts[1]), parts[2])
            except ValueError:
                row = None
        if row is None:
            two = line.strip().split(None, 1)
            if len(two) == 2:
                try:
                    row = (None, float(two[0]), two[1])
                except ValueError:
                    row = None
        if row is not None and math.isfinite(row[1]) and row[1] >= 0:
            rows.append((row[0], row[1], _clean_name(row[2])))
    return rows


def categories(ps_text, limit=CATEGORIES):
    """[(name, total pcpu)] of the biggest consumers from `ps` text, by the program's base name: the top `limit`, largest first."""
    totals = {}
    for _, cpu, name in process_rows(ps_text):
        totals[name] = totals.get(name, 0.0) + cpu
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def top_pids(rows, limit=LANE_PIDS):
    """The pids of the `limit` hungriest processes that have one."""
    return [pid for pid, _, _ in sorted((r for r in rows if r[0] is not None), key=lambda r: -r[1])[:limit]]


def lsof_argv(pids):
    return ["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(str(int(p)) for p in pids)]


def parse_lsof(text):
    """{pid: working directory} from `lsof -Fpn` output (`p<pid>` then `n<path>` lines). Bounded."""
    found, pid = {}, None
    for line in (text or "")[:PS_BYTES].splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n/") and pid is not None:
            found.setdefault(pid, line[1:])
    return found


def lane_of(cwd, registry):
    """The lane a working directory belongs to: the agent that owns the registered worktree it is inside (the longest match), else the name of the
    worktree directory (`<repo>.wt/<name>`), else None. `registry` is {path: agent}."""
    try:
        real = os.path.realpath(cwd)
        best = None
        for path, agent in (registry or {}).items():
            root = os.path.realpath(path).rstrip("/")
            if (real == root or real.startswith(root + "/")) and isinstance(agent, str) and (best is None or len(root) > best[0]):
                best = (len(root), agent)
        if best:
            return re.sub(r"[^A-Za-z0-9_.+-]", "?", best[1])[:40]
        parts = real.split("/")
        for i, part in enumerate(parts[:-1]):
            if part.endswith(".wt") and parts[i + 1]:
                return re.sub(r"[^A-Za-z0-9_.+-]", "?", parts[i + 1])[:40]
    except (OSError, ValueError, TypeError):
        pass
    return None


def consumers(ps_text, lanes=None, limit=3):
    """[(program, pcpu, lane or None)] for the `limit` hungriest programs; the lane is the one of the program's hungriest process whose lane is known."""
    rows = process_rows(ps_text)
    totals, best = {}, {}
    for pid, cpu, name in rows:
        totals[name] = totals.get(name, 0.0) + cpu
        if pid is not None and (lanes or {}).get(pid) and cpu >= best.get(name, (-1, None))[0]:
            best[name] = (cpu, lanes[pid])
    top = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [(n, c, best.get(n, (0, None))[1]) for n, c in top]


def detail(cats):
    """`top: node 60% (coder-live), docker 25%`: the lane in brackets only when it is known. Names, percentages and lane names: nothing from a command line."""
    if not cats:
        return ""
    return "top: " + ", ".join(("%s %d%% (%s)" % (c[0], round(c[1]), c[2])) if len(c) > 2 and c[2] else "%s %d%%" % (c[0], round(c[1])) for c in cats)
