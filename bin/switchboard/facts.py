# ------------------------------------------------------------------ the facts

class FactError(ValueError):
    """A fact that cannot be used: a missing field, or a value of the wrong type."""


DISK_GUARD_DEFAULTS = {"min_free_gb": 50, "hard_free_gb": 10}
DISK_REARM_PERCENT = 110                # an episode ends when the free space is back at the threshold plus 10% (integer arithmetic: 50 * 1.1 is 55.00000000000001)
DISK_GUARD_FILE = "disk-guard.json"     # {"low": bool, "episode": <epoch the episode began>, "hard": bool}: so a restart does not start a second episode


def validate_disk_guard(raw, source):
    """`disk_guard`: absent or null = the defaults; false = off; else {"min_free_gb", "hard_free_gb"} (numbers, hard at most min)."""
    if raw is None or raw is False:
        return
    if not isinstance(raw, dict):
        raise ConfigError("%s: 'disk_guard' must be false, null or an object, got %r" % (source, raw))
    unknown = sorted(set(raw) - set(DISK_GUARD_DEFAULTS))
    if unknown:
        raise ConfigError("%s: 'disk_guard' has unknown key(s) %s (it takes %s)" % (source, ", ".join(unknown), ", ".join(DISK_GUARD_DEFAULTS)))
    for key, value in raw.items():
        if not _is_number(value) or value < 0:
            raise ConfigError("%s: 'disk_guard.%s' must be a number of gigabytes of at least 0, got %r" % (source, key, value))
    merged = dict(DISK_GUARD_DEFAULTS, **raw)
    if merged["hard_free_gb"] > merged["min_free_gb"]:
        raise ConfigError("%s: 'disk_guard.hard_free_gb' (%r) must not be more than 'min_free_gb' (%r)" % (source, merged["hard_free_gb"], merged["min_free_gb"]))


def disk_guard_settings(config):
    """{"min_free_gb", "hard_free_gb"}, or None when the guard is off."""
    raw = config.get("disk_guard")
    if raw is False:
        return None
    return dict(DISK_GUARD_DEFAULTS, **(raw or {}))


def disk_free(path):
    """(free bytes, total bytes) of the volume holding `path`: ONE stat, no walk; None when it cannot be read."""
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return None
    return usage.free, usage.total


def disk_status_line(config, state_dir):
    """`238 GB free (24%)`, with LOW and the thresholds when below the guard; None when the volume cannot be read."""
    found = disk_free(state_dir)
    if found is None:
        return None
    free, total = found
    gb = free / 1e9
    line = "%d GB free (%d%%)" % (gb, round(100.0 * free / total)) if total else "%d GB free" % gb
    guard = disk_guard_settings(config)
    if guard is not None and gb < guard["min_free_gb"]:
        line += ": %s (below %g GB; hard line %g GB)" % ("VERY LOW" if gb < guard["hard_free_gb"] else "LOW", guard["min_free_gb"], guard["hard_free_gb"])
    return line


class DiskFacts:
    """The disk guard: ONE `disk_usage` of the state directory's volume per pass. Below `disk_guard.min_free_gb` the Chief of Staff is told ONCE for
    the episode (`disk.low:<episode>`, the fact stays pending until delivered and the key is stable, so it is not told again), `disk.low` is
    audited, and the episode ends (`disk.recovered`) when the free space is back above the threshold plus 10%. The episode is kept in `disk-guard.json`
    so a restart does not begin a second one. Reads nothing else: no `du`, no walk."""

    __name__ = "disk_facts"

    def __init__(self):
        self.engine = None
        self.episode = None   # (episode epoch, hard flag) while low, from the file or this process

    def bind(self, engine):
        self.engine = engine

    def _load(self, state_dir):
        try:
            with open(str(Path(state_dir) / DISK_GUARD_FILE)) as handle:
                raw = json.loads(handle.read(4096))   # a few bytes are all it ever holds
            if raw.get("low") and _is_number(raw.get("episode")):
                return float(raw["episode"]), bool(raw.get("hard"))
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        return None

    def _save(self, engine, low, episode, hard):
        try:
            path = Path(engine.state_dir) / DISK_GUARD_FILE   # written only on a change of episode: a handful of times a year
            temp = path.with_name("%s.%d.tmp" % (DISK_GUARD_FILE, os.getpid()))
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps({"low": bool(low), "episode": episode, "hard": bool(hard)}, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp), str(path))
        except OSError as err:
            try:
                temp.unlink()
            except (OSError, UnboundLocalError):
                pass
            engine._note("disk.guard_file", None, {"error": _plain("%s: %s" % (type(err).__name__, err), 160)})   # said once per change: after a restart a second episode could begin

    def __call__(self):
        engine = self.engine
        guard = disk_guard_settings(engine.config) if engine is not None else None
        if guard is None or engine.state_dir is None:
            return []
        found = disk_free(engine.state_dir)
        if found is None:   # FAIL OPEN, on purpose: a guard that cannot read the volume says so once (audit) and wakes nobody: a false alarm helps no one
            engine._note("disk.unreadable", None, {"path": str(engine.state_dir)})
            return []
        free_gb, now = found[0] / 1e9, engine.now()
        if self.episode is None:
            self.episode = self._load(engine.state_dir)
        if self.episode is not None and free_gb >= guard["min_free_gb"] * DISK_REARM_PERCENT / 100:
            if engine.audit is not None:   # appended, not `_note`d: a second episode that recovers at the same figure is still news
                engine.audit.append(engine.actor, "disk.recovered", None, {"free_gb": int(free_gb), "min_free_gb": guard["min_free_gb"], "episode": int(self.episode[0])})
            self.episode = None
            self._save(engine, False, None, False)
        discovery = getattr(engine, "discovery", None)
        chiefs = sorted(n for n, o in discovery.agents.items() if (o.fleet or {}).get("role") == "chief-of-staff") if discovery is not None and not discovery.problems else []
        if self.episode is None and free_gb < guard["min_free_gb"] and chiefs:   # an episode begins when there is someone to tell: with no Chief of Staff running, the next pass tries again
            self.episode = (now, free_gb < guard["hard_free_gb"])
            if engine.audit is not None:
                engine.audit.append(engine.actor, "disk.low", None, {"free_gb": int(free_gb), "min_free_gb": guard["min_free_gb"],
                                                                      "hard_free_gb": guard["hard_free_gb"], "hard": self.episode[1]})
            self._save(engine, True, now, self.episode[1])
        if self.episode is None:
            return []
        episode, hard = self.episode
        if free_gb < guard["hard_free_gb"] and not hard:   # it got worse inside the episode: said in the status line and the audit, not a second wake
            self.episode = (episode, True)
            if engine.audit is not None:
                engine.audit.append(engine.actor, "disk.hard", None, {"free_gb": int(free_gb), "hard_free_gb": guard["hard_free_gb"], "episode": int(episode)})
            self._save(engine, True, episode, True)
        if not chiefs:
            return []
        text = "disk low: %d GB free on the volume holding the state directory (the guard is %g GB, hard line %g GB). `%s disk prune --dry-run` shows what would be " \
               "pruned (docker build cache and images older than a week, never volumes; it measures the state directory's volume, so check where docker keeps its data)" % (free_gb, guard["min_free_gb"], guard["hard_free_gb"], PROG)
        return [Fact("disk.low:%d" % int(episode), name, "disk", text, episode, sender="switchboard") for name in chiefs]


class SpotlightFacts:
    """The Spotlight guard's fact source (bin/spotlight_guard.py, ask #438). Once an hour a BACKGROUND thread asks `mdfind` (5 s per folder) whether the index holds
    anything under each guarded folder; a pass only collects what the thread has finished, so it starts no process and a slow tool never delays it. A folder
    indexed on two checks in a row begins an episode: ONE wake to the Chief of Staff (`spotlight.lost:<folder>:<episode>`, audited `spotlight.lost`); a zero count
    ends it (`spotlight.recovered`). Writes only `spotlight-guard.json`, and only when an answer changed. Changes no setting."""

    __name__ = "spotlight_facts"

    def __init__(self):
        self.engine = None
        self.tracker = None
        self.thread = None
        self.done = None             # results the thread has finished, not yet stepped
        self.lock = threading.Lock()  # the hand-off of `done`: a result stored while the pass takes the last one is never lost
        self.started = None          # the engine's clock when the last check began
        self.saved = None            # the stable content the file holds
        self.counts = {}             # the last count seen per folder (the wake text names it)

    def bind(self, engine):
        self.engine = engine

    def _start(self, engine, sg, cfg):
        runner = getattr(engine, "spotlight_runner", None)
        folders = list(cfg["folders"])
        self.started = engine.now()

        def work():
            results = sg.check_all(folders, runner)
            with self.lock:
                if self.thread is threading.current_thread():
                    self.done = results
        self.thread = threading.Thread(target=work, name="spotlight-check", daemon=True)
        self.thread.start()

    def _chiefs(self, engine):
        discovery = getattr(engine, "discovery", None)
        return sorted(n for n, o in discovery.agents.items() if (o.fleet or {}).get("role") == "chief-of-staff") if discovery is not None and not discovery.problems else []

    def __call__(self):
        engine = self.engine
        if engine is None or engine.state_dir is None:
            return []
        sg = load_module("spotlight_guard")
        cfg = sg.settings(engine.config.get("spotlight_guard"))
        if cfg is None:
            return []
        if self.tracker is None:
            saved = sg.load(engine.state_dir)
            self.tracker = sg.Tracker((saved or {}).get("folders"))
            self.saved = sg.stable(saved) if saved else None
            self.counts = {f: sg.clean_count(e.get("count")) for f, e in ((saved or {}).get("folders") or {}).items() if isinstance(e, dict)}
        with self.lock:
            results, self.done = self.done, None
        if results:
            self._apply(engine, sg, results)
        facts = self._open(engine, sg, cfg["folders"])   # EVERY pass while an episode is open: the delivery layer batches (a fact must still be pending when the batch is ripe) and dedups by the stable key
        alive = self.thread is not None and self.thread.is_alive()
        if not alive and (self.started is None or engine.now() - self.started >= sg.CHECK_SECONDS):
            self._start(engine, sg, cfg)
        return facts

    def _open(self, engine, sg, folders):
        """The wake of each open episode, one fact per Chief of Staff, the same key and `created` on every pass."""
        chiefs = self._chiefs(engine)
        facts = []
        for folder, episode in sorted(self.tracker.episode.items()):
            if episode is None or not chiefs or folder not in folders:   # a folder no longer guarded wakes nobody
                continue
            text = sg.wake_text(folder, self.counts.get(folder), PROG)
            key = "spotlight.lost:%s:%d" % (hashlib.sha256(folder.encode()).hexdigest()[:10], int(episode))
            facts.extend(Fact(key, name, "spotlight", " ".join(text.split()), episode, sender="switchboard") for name in chiefs)
        return facts

    def _apply(self, engine, sg, results):
        now = engine.now()
        for folder, (state, count) in sorted(results.items()):
            event = self.tracker.step(folder, state, now)
            if count is not None:
                self.counts[folder] = count
            if event == "begin" and engine.audit is not None:
                engine.audit.append(engine.actor, "spotlight.lost", None, {"folder": folder, "count": count})
            elif event == "end" and engine.audit is not None:
                engine.audit.append(engine.actor, "spotlight.recovered", None, {"folder": folder})
        content = sg.body(results, self.tracker, now)
        if self.saved != sg.stable(content):
            try:
                sg.save(engine.state_dir, content)
                self.saved = sg.stable(content)
            except OSError as err:
                engine._note("spotlight.guard_file", None, {"error": _plain("%s: %s" % (type(err).__name__, err), 160)})


class CpuFacts:
    """The CPU and memory guard's fact source (bin/cpu_guard.py): ONE `os.getloadavg()` and the kernel's memory counters per pass, no process. When the load over
    the cores stays above the guard for the configured minutes the Chief of Staff is told ONCE for the episode (`cpu.high:<episode>`: the key is stable, so it is
    delivered once), `cpu.high` is audited, and the wake NAMES THE TOP CONSUMERS by program and by lane (the agent or worktree the process runs in), from ONE
    bounded `ps` and ONE bounded `lsof` run on a thread when the episode begins (never on another pass). The wake is held until they answer or `WAKE_GRACE_SECONDS`
    pass; an answer that comes after the wake went out is sent as ONE follow-up note (`cpu.top:<episode>`), and a `ps` that never answers is said once in the
    wake. The episode ends (`cpu.recovered`) below two thirds of the guard and is kept in `cpu-guard.json`, so a restart does not begin a second one. The same
    pattern guards memory: swap above `swap_percent` for `swap_minutes` wakes once per episode (`mem.high:<episode>`, `mem-guard.json`)."""

    __name__ = "cpu_facts"

    def __init__(self):
        self.engine = None
        self.tracker = None
        self.mem_tracker = None
        self.detail = ""
        self.thread = None
        self.begin_at = None          # the engine's clock when this process began the episode (None: it was loaded from the file)
        self.waked_without = False    # the wake went out while the consumers were still being read
        self.saved_episode = None     # the episode the file holds (None: closed)
        self.saved_mem = None

    def bind(self, engine):
        self.engine = engine

    def _registry(self, engine):
        try:
            return {path: entry.get("agent") for path, entry in read_worktrees(engine.state_dir).items() if isinstance(entry, dict)}
        except Exception:
            return {}

    def _start_ps(self, engine, cg):
        """ONE bounded `ps`, then ONE bounded `lsof` of the top ten processes for their lanes, on a thread: a starved machine must never hang the daemon's pass.
        The result lands in `self.detail` when it is done, and only while this is still the current thread (a slow one of an earlier episode writes nothing)."""
        runner = getattr(engine, "cpu_ps_runner", None) or subprocess.run
        registry = self._registry(engine)

        def work():
            text = ""
            try:
                done = runner(cg.PS_ARGV, capture_output=True, text=True, timeout=cg.PS_SECONDS, stdin=subprocess.DEVNULL)
                if done.returncode == 0:
                    lanes = {}
                    pids = cg.top_pids(cg.process_rows(done.stdout))
                    if pids:
                        try:
                            looked = runner(cg.lsof_argv(pids), capture_output=True, text=True, timeout=cg.LSOF_SECONDS, stdin=subprocess.DEVNULL)
                            if looked.returncode in (0, 1):
                                lanes = {pid: cg.lane_of(cwd, registry) for pid, cwd in cg.parse_lsof(looked.stdout).items()}
                        except Exception:
                            lanes = {}
                    text = cg.detail(cg.consumers(done.stdout, lanes))
            except Exception:
                text = ""
            if self.thread is threading.current_thread():
                self.detail = text
        self.thread = threading.Thread(target=work, name="cpu-ps", daemon=True)
        self.thread.start()

    def _chiefs(self, engine):
        discovery = getattr(engine, "discovery", None)
        return sorted(n for n, o in discovery.agents.items() if (o.fleet or {}).get("role") == "chief-of-staff") if discovery is not None and not discovery.problems else []

    def __call__(self):
        engine = self.engine
        if engine is None or engine.state_dir is None:
            return []
        cg = load_cpu_guard()
        raw = engine.config.get("cpu_guard")
        cfg = cg.settings(raw)
        if cfg is None:
            return []
        chiefs = self._chiefs(engine)
        facts = self._cpu(engine, cg, cfg, chiefs)
        facts.extend(self._mem(engine, cg, cg.mem_settings(raw), chiefs))
        return facts

    def _cpu(self, engine, cg, cfg, chiefs):
        read = cg.reading()
        if not read.ratio_known:   # FAIL OPEN: a load or a core count that cannot be read tells nobody (the status line says so), and never counts as one core
            return []
        if self.tracker is None:
            self.tracker = cg.Episode(cg.load_episode(engine.state_dir))
            self.saved_episode = self.tracker.episode
        load1, load5, cores = read.load1, read.load5, read.cores
        ratio = load5 / cores   # the five-minute average decides: a one-minute spike never wakes the Chief of Staff
        now = engine.now()
        event = self.tracker.step(ratio, now, cfg, can_begin=bool(chiefs))
        if event == "begin":
            self.detail, self.begin_at, self.waked_without = "", now, False
            self._start_ps(engine, cg)
            if engine.audit is not None:
                engine.audit.append(engine.actor, "cpu.high", None, {"load1": round(load1, 1), "load5": round(load5, 1), "cores": cores, "ratio5": round(ratio, 2),
                                                                      "high_ratio": cfg["high_ratio"], "minutes": cfg["minutes"]})
            self._save(engine, cg, self.tracker.episode)
        elif event == "end":
            if engine.audit is not None:
                engine.audit.append(engine.actor, "cpu.recovered", None, {"load1": round(load1, 1), "load5": round(load5, 1), "cores": cores, "ratio5": round(ratio, 2)})
            self.detail, self.thread, self.begin_at = "", None, None
            self._save(engine, cg, None)
        episode = self.tracker.episode
        if self.saved_episode != episode:   # a save that failed is tried again on the next pass
            self._save(engine, cg, episode)
        if episode is None or not chiefs:
            return []
        alive = self.thread is not None and self.thread.is_alive()
        if alive and self.begin_at is not None and now - self.begin_at < cg.WAKE_GRACE_SECONDS:
            return []   # the consumers are being read: the wake waits a few seconds for them
        if self.detail:
            consumers = self.detail + "."
        elif alive:
            consumers = "Consumers not read within %d s (ps is slow; one follow-up note carries them if it answers)." % cg.WAKE_GRACE_SECONDS
        elif self.begin_at is not None:
            consumers = "Consumers could not be read."
        else:
            consumers = ""
        if alive and not self.detail:
            self.waked_without = True
        text = "cpu high: load %d on %d cores (%.1fx over five minutes; the guard is %gx for %g minutes). %s `%s status` shows the line; heavy work should wait for a slot." % (
            round(load5), cores, ratio, cfg["high_ratio"], cfg["minutes"], consumers, PROG)
        facts = [Fact("cpu.high:%d" % int(episode), name, "cpu", " ".join(text.split()), episode, sender="switchboard") for name in chiefs]
        if self.waked_without and self.detail:   # the answer came after the wake: ONE follow-up note (the same key on every pass, so it is delivered once)
            note = "cpu consumers (the answer came after the cpu high note): %s." % self.detail
            facts.extend(Fact("cpu.top:%d" % int(episode), name, "cpu-top", note, episode, sender="switchboard") for name in chiefs)
        return facts

    def _mem(self, engine, cg, mem_cfg, chiefs):
        """Swap above the guard for the minutes: ONE wake per episode with the biggest holders (the kernel's counters, no process)."""
        mem = cg.memory_reading()
        percent = mem.swap_percent
        if percent is None:   # FAIL OPEN: unreadable memory tells nobody (the status line says so)
            return []
        if self.mem_tracker is None:
            self.mem_tracker = cg.Episode(cg.load_episode(engine.state_dir, cg.MEM_STATE_FILE))
            self.saved_mem = self.mem_tracker.episode
        event = self.mem_tracker.step(percent, engine.now(), mem_cfg, can_begin=bool(chiefs))
        holders = []
        if event == "begin":
            holders = cg.biggest_holders(cg.process_table())
            self.mem_holders = holders
            if engine.audit is not None:
                engine.audit.append(engine.actor, "mem.high", None, {"swap_percent": round(percent, 1), "swap_used_mb": int(mem.swap_used / 1e6), "swap_total_mb": int(mem.swap_total / 1e6),
                                                                      "high_percent": mem_cfg["high_ratio"], "minutes": mem_cfg["minutes"]})
            self._save_mem(engine, cg, self.mem_tracker.episode)
        elif event == "end":
            self.mem_holders = []
            if engine.audit is not None:
                engine.audit.append(engine.actor, "mem.recovered", None, {"swap_percent": round(percent, 1)})
            self._save_mem(engine, cg, None)
        episode = self.mem_tracker.episode
        if self.saved_mem != episode:
            self._save_mem(engine, cg, episode)
        if episode is None or not chiefs:
            return []
        text = cg.mem_wake_text(mem, getattr(self, "mem_holders", []), mem_cfg, PROG)
        return [Fact("mem.high:%d" % int(episode), name, "mem", " ".join(text.split()), episode, sender="switchboard") for name in chiefs]

    def _save(self, engine, cg, episode):
        try:
            cg.save_episode(engine.state_dir, episode)
            self.saved_episode = episode
        except OSError as err:
            engine._note("cpu.guard_file", None, {"error": _plain("%s: %s" % (type(err).__name__, err), 160)})

    def _save_mem(self, engine, cg, episode):
        try:
            cg.save_episode(engine.state_dir, episode, cg.MEM_STATE_FILE)
            self.saved_mem = episode
        except OSError as err:
            engine._note("mem.guard_file", None, {"error": _plain("%s: %s" % (type(err).__name__, err), 160)})
