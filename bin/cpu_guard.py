"""The CPU guard (the P0 CPU order, item D). Pure of the switchboard: the caller hands it numbers, the clock and a state directory.

The load average (one `os.getloadavg()`, a system call: the one-minute figure is shown, the FIVE-minute figure drives the alarm, so a one-minute spike never wakes anyone) over the number of cores (`os.cpu_count()`) is the ratio. A pass reads nothing else and starts no
process. When the ratio stays above `high_ratio` for `minutes` the Chief of Staff is told ONCE for the episode; the episode ends when the ratio falls below
two thirds of the guard (the default guard of 3 re-arms below 2), and it is kept in `cpu-guard.json` so a restart does not begin another. The only process the
guard ever starts is ONE bounded `ps` when an episode begins, to name the top categories of what is running."""

import json
import os
import re
import tempfile

DEFAULTS = {"high_ratio": 3.0, "minutes": 5}
STATE_FILE = "cpu-guard.json"
SLOTS_FILE = "heavy-slots.json"
REARM_NUM, REARM_DEN = 2, 3        # re-arm below high_ratio * 2/3 (integer arithmetic on the ratio would be exact enough; floats are compared directly)
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


def reading():
    """(load over one minute, load over five minutes, cores), or None when the system will not say. A system call each: no process."""
    try:
        loads = os.getloadavg()
        load1, load5 = float(loads[0]), float(loads[1])   # looked up at the call, so a test can replace it
        cores = int(os.cpu_count() or 1)
    except (OSError, AttributeError, ValueError, TypeError, IndexError):
        return None
    return load1, load5, max(cores, 1)


def slots_text(config, state_dir):
    """`heavy slots 2/3` from the optional `heavy_slots` module (bin/heavy_slots.py: `slots(config)` the number of slots, `holders(state_dir)` who holds
    them), or None: no such module yet, or any trouble with it, adds nothing to the line."""
    try:
        import importlib.util
        path = HEAVY_SLOTS_PATH
        if not os.path.isfile(path):
            return None
        spec = importlib.util.spec_from_file_location("heavy_slots", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        total, held = module.slots(config), len(list(module.holders(state_dir)))
        if isinstance(total, bool) or not isinstance(total, int) or not 1 <= total <= 1000:
            return None
        return "heavy slots %d/%d" % (held, total)
    except Exception:
        return None


def status_line(cfg, read, slots=None):
    """`load 86 on 14 cores (6.1x) [HIGH]`; None when the load could not be read."""
    if read is None:
        return None
    load, _, cores = read
    ratio = load / cores
    line = "load %d on %d cores (%.1fx)" % (round(load), cores, ratio)   # the DISPLAYED load is the one-minute average; the alarm uses the five-minute one
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
