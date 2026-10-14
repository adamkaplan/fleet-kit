"""The CPU guard (the P0 CPU order, item D). Pure of the switchboard: the caller hands it numbers, the clock and a state directory.

The load average (one `os.getloadavg()`, a system call: the one-minute figure is shown, the FIVE-minute figure drives the alarm, so a one-minute spike never wakes anyone) over the number of cores (`os.cpu_count()`) is the ratio. A pass reads nothing else and starts no
process. When the ratio stays above `high_ratio` for `minutes` the Chief of Staff is told ONCE for the episode; the episode ends when the ratio falls below
two thirds of the guard (the default guard of 3 re-arms below 2), and it is kept in `cpu-guard.json` so a restart does not begin another. The only process the
guard ever starts is ONE bounded `ps` when an episode begins, to name the top categories of what is running."""

import json
import math
import os
import re
import tempfile

DEFAULTS = {"high_ratio": 3.0, "minutes": 5}
STATE_FILE = "cpu-guard.json"
SLOTS_FILE = "heavy-slots.json"
REARM_NUM, REARM_DEN = 2, 3        # re-arm below high_ratio * 2/3 (floats, compared directly)
HEAVY_SLOTS_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "heavy_slots.py")   # the optional module of the `heavy` command
PS_ARGV = ["ps", "-axo", "pcpu=,comm="]
PS_SECONDS = 5
PS_BYTES = 2000000
CATEGORIES = 5


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate(raw, source="config"):
    """Raises ValueError: `cpu_guard` is absent or null (the defaults), false (off), or {"high_ratio": > 0, "minutes": 1..120}."""
    if raw is None or raw is False:
        return
    if not isinstance(raw, dict):
        raise ValueError("%s: 'cpu_guard' must be false, null or an object, got %r" % (source, raw))
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ValueError("%s: 'cpu_guard' has unknown key(s) %s (it takes %s)" % (source, ", ".join(unknown), ", ".join(DEFAULTS)))
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


def load_episode(state_dir):
    try:
        with open(os.path.join(str(state_dir), STATE_FILE)) as handle:
            raw = json.loads(handle.read(4096))
        value = raw.get("episode")
        return float(value) if _number(value) else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def save_episode(state_dir, episode):
    """Atomic, 0600, written only when the episode changes (a few times a year). Raises OSError."""
    path = os.path.join(str(state_dir), STATE_FILE)
    fd, temp = tempfile.mkstemp(prefix=STATE_FILE + ".", suffix=".tmp", dir=str(state_dir))
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


def categories(ps_text, limit=CATEGORIES):
    """[(name, total pcpu)] of the biggest consumers from `ps -axo pcpu=,comm=` text, by the program's base name: the top `limit`, largest first."""
    totals = {}
    for line in (ps_text or "")[:PS_BYTES].splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            cpu = float(parts[0])
        except ValueError:
            continue
        name = re.sub(r"[^A-Za-z0-9_.+-]", "?", os.path.basename(parts[1].strip()))[:40] or "?"
        totals[name] = totals.get(name, 0.0) + cpu
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def detail(cats):
    return "top: " + ", ".join("%s %d%%" % (n, round(c)) for n, c in cats) if cats else ""
