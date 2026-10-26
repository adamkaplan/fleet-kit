# ---------------------------------------------------------------- the docker reaper (ask #321)
#
# A test stack or a compose project that an agent starts carries two labels: `fleet.owner=<agent>` and `fleet.expires=<epoch seconds>`. The reaper removes
# ONLY objects that carry BOTH and whose expiry has passed: containers (`docker rm -f <id>`, never `-v`: an anonymous volume is not ours), then networks
# (`docker network rm <id>`), then volumes (`docker volume rm <name>`, never one a running container mounts). An unlabelled object, a live one and a
# label that is not an epoch are never touched; docker itself and the daemon's own files are not reachable from here. No `system prune`, no sudo.
# A dry run is the default for the command (`--apply` removes); the daemon applies once a day while `docker_reaper` is not false.
DOCKER_LABEL_OWNER = "fleet.owner"
DOCKER_LABEL_EXPIRES = "fleet.expires"
DOCKER_REAP_TIMEOUT = 60
DOCKER_REAP_FILE = "docker-reap.json"
DOCKER_REAP_INTERVAL = 86400
DOCKER_REAP_RETRY = 3600
_DOCKER_LIST_IDS = {   # the ids only (never a label in a line: a label value may hold a newline), then `inspect` says the truth
    "container": ("ps", "-a", "-q", "--no-trunc", "--filter", "label=" + DOCKER_LABEL_EXPIRES),
    "network": ("network", "ls", "-q", "--no-trunc", "--filter", "label=" + DOCKER_LABEL_EXPIRES),
    "volume": ("volume", "ls", "-q", "--filter", "label=" + DOCKER_LABEL_EXPIRES),
}
_DOCKER_RUNNING_IDS = ("ps", "-q", "--no-trunc", "--filter", "status=running")
_DOCKER_REMOVE = {"container": ("rm", "-f", "--"), "network": ("network", "rm", "--"), "volume": ("volume", "rm", "--")}
_DOCKER_ID = {"container": re.compile(r"[0-9a-f]{64}"), "network": re.compile(r"[0-9a-f]{64}"), "volume": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")}
DOCKER_EPOCH_MIN = 10 ** 9      # an expiry before the year 2001 is not a plan, it is a way to get an object removed at once
DOCKER_INSPECT_CHUNK = 100


def _docker_run(docker, runner, argv):
    done = runner([docker] + list(argv), capture_output=True, text=True, timeout=DOCKER_REAP_TIMEOUT, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise OSError("docker %s: exit %s: %s" % (" ".join(argv[:2]), done.returncode, _plain(done.stderr or done.stdout, 160)))
    return done.stdout or ""


def _docker_inspect(docker, runner, kind, ids):
    """The inspect records of `ids` (a list of dicts): the labels as docker says they are NOW."""
    found = []
    for start in range(0, len(ids), DOCKER_INSPECT_CHUNK):
        try:
            records = json.loads(_docker_run(docker, runner, ("inspect", "--type", kind) + ("--",) + tuple(ids[start:start + DOCKER_INSPECT_CHUNK])) or "[]")
        except ValueError:
            raise OSError("docker inspect: not JSON")
        found.extend(r for r in records if isinstance(r, dict))
    return found


def _docker_labelled(kind, record):
    """(id, owner, expires) when the record carries BOTH labels with an owner and an epoch expiry, else None. The id is checked against the strict shape for its kind."""
    labels = ((record.get("Config") or {}).get("Labels") if kind == "container" else record.get("Labels")) or {}
    ident = record.get("Id") if kind != "volume" else record.get("Name")
    owner, expires = labels.get(DOCKER_LABEL_OWNER), labels.get(DOCKER_LABEL_EXPIRES)
    if not (isinstance(ident, str) and _DOCKER_ID[kind].fullmatch(ident) and isinstance(owner, str) and owner.strip()
            and isinstance(expires, str) and re.fullmatch(r"\d{1,12}", expires) and int(expires) >= DOCKER_EPOCH_MIN):
        return None
    return ident, owner.strip(), int(expires)


def docker_reap(docker, runner, now, apply, audit=None, actor="daemon"):
    """Remove (apply) or list (dry run) the EXPIRED labelled docker objects. Returns {"removed": [...], "kept": [...], "errors": [...]}; each entry is
    (kind, id, owner, expires[, reason]). What is listed is only ids; each object's labels are read from `docker inspect` (not from a text line a label
    could forge), and an id must have docker's own shape. A listing that fails ends the run before anything is removed. The listing and the removal are
    two steps: a volume a container starts to use in between is refused by docker itself, which is the backstop."""
    result = {"removed": [], "kept": [], "errors": []}
    expired = {}
    try:
        for kind, argv in _DOCKER_LIST_IDS.items():
            ids = [line.strip() for line in _docker_run(docker, runner, argv).splitlines() if _DOCKER_ID[kind].fullmatch(line.strip())]
            expired[kind] = []
            for record in _docker_inspect(docker, runner, kind, ids) if ids else []:
                labelled = _docker_labelled(kind, record)
                if labelled is None:
                    continue
                if labelled[2] <= now:
                    expired[kind].append(labelled)
                else:
                    result["kept"].append((kind,) + labelled + ("not expired",))
    except (OSError, subprocess.SubprocessError) as err:
        result["errors"].append(_plain(str(err), 200))
        return result

    def remove(kind, ident, owner, expires):
        if apply:
            try:
                _docker_run(docker, runner, _DOCKER_REMOVE[kind] + (ident,))
            except (OSError, subprocess.SubprocessError) as err:
                result["errors"].append(_plain("%s %s: %s" % (kind, ident, err), 200))
                return
            result["removed"].append((kind, ident, owner, expires))
            try:
                if audit is not None:
                    audit.append(actor, "docker.reaped", ident, {"kind": kind, "owner": owner, "expires": expires})
            except Exception as err:   # the object is already gone: say the audit failed and go on with the rest
                result["errors"].append(_plain("audit of %s %s failed: %s" % (kind, ident, err), 200))
            return
        result["removed"].append((kind, ident, owner, expires))

    for labelled in expired["container"]:
        remove("container", *labelled)
    for labelled in expired["network"]:
        remove("network", *labelled)
    if expired["volume"]:
        try:
            running = [line.strip() for line in _docker_run(docker, runner, _DOCKER_RUNNING_IDS).splitlines() if _DOCKER_ID["container"].fullmatch(line.strip())]
            in_use = {m.get("Name") for r in (_docker_inspect(docker, runner, "container", running) if running else []) for m in (r.get("Mounts") or [])
                      if isinstance(m, dict) and m.get("Type") == "volume"}   # after the containers went: what a still-running one holds
        except (OSError, subprocess.SubprocessError) as err:
            result["errors"].append(_plain(str(err), 200))
            return result
        for ident, owner, expires in expired["volume"]:
            if ident in in_use:
                result["kept"].append(("volume", ident, owner, expires, "a running container mounts it"))
            else:
                remove("volume", ident, owner, expires)
    return result


def _docker_problem(name):
    """(path, None) for a docker binary the disk tools may run, else (None, one line saying why not). The tools take `--docker` from a person, so the
    value is NOT trusted to be a docker: its basename must be exactly `docker` and so must the basename of a symlink's target (or Docker Desktop's
    `com.docker.cli`); it must be a regular executable file owned by this user or root; neither it nor any directory above it may be writable by group
    or other (a sticky directory such as /tmp excepted); a bare name is resolved from PATH. The path RETURNED is the vetted real path, so the file run is
    the file checked. Anything else is refused."""
    if not isinstance(name, str) or not name or "\x00" in name or name.startswith("-"):
        return None, "is not a usable executable name"
    path = name if os.sep in name else shutil.which(name)
    if path is None or not os.path.exists(path):
        return None, "not installed or not on PATH"
    if not os.path.isabs(path):
        return None, "must be an absolute path (or a bare name on PATH)"
    real = os.path.realpath(path)
    if os.path.basename(path) != "docker" or os.path.basename(real) not in ("docker", "com.docker.cli"):
        return None, "is not a docker binary: its name (and the name a link points to) must be exactly `docker`"
    try:
        info = os.stat(real)
        if not stat.S_ISREG(info.st_mode) or not os.access(real, os.X_OK):
            return None, "is not an executable file"
        if info.st_uid not in (0, os.getuid()):
            return None, "belongs to another user: not trusted"
        if info.st_mode & 0o022:
            return None, "is writable by group or other: not trusted"
        here, first = os.path.dirname(real), True
        while True:
            at = os.stat(here)
            mode = at.st_mode
            if at.st_uid not in (0, os.getuid()):
                return None, "sits in a directory (%s) that others can change (another user owns it): not trusted; pass the absolute path of a docker kept in a directory only you or root can write" % _plain(here, 80)
            if not mode & 0o1000:   # a sticky directory (/tmp) lets nobody replace another's file
                # the file's own directory must be closed to group and other; an ancestor only to OTHER, or to a group when it is not root's (a root-owned
                # group-writable /Applications or /usr/local, where a standard Docker install lives, is the operating system's own)
                if (first and mode & 0o022) or mode & 0o002 or (mode & 0o020 and at.st_uid not in (0,)):
                    return None, "sits in a directory (%s) that others can change: not trusted; pass the absolute path of a docker kept in a directory only you or root can write" % _plain(here, 80)
            first = False
            parent = os.path.dirname(here)
            if parent == here:
                break
            here = parent
    except OSError:
        return None, "cannot be read"
    return real, None


def _docker_executable(name):
    return _docker_problem(name)[0]


def _docker_message(name):
    """The one refusal line for a `--docker` value that is not a usable docker."""
    why = _docker_problem(name)[1] or "not usable"
    if why.startswith("not installed"):
        return "docker is not installed or not on PATH (%s)" % _plain(name, 160)
    return "refused: %s %s" % (_plain(name, 120), why)


class DockerReapFacts:
    """The daily reap: once `DOCKER_REAP_INTERVAL` has passed since the last run (kept in `docker-reap.json`, so a restart does not run it again), the
    daemon starts the reaper on a background thread (docker may be slow: a pass never waits for it). Only a daemon that set `docker_reap_runner` runs
    it (a test engine does not), and never while `docker_reaper` is false. Returns no facts."""

    __name__ = "docker_reap_facts"

    def __init__(self):
        self.engine, self.thread = None, None

    def bind(self, engine):
        self.engine = engine

    def _last(self, state_dir):
        try:
            return float(json.loads((Path(state_dir) / DOCKER_REAP_FILE).read_text()[:1024])["at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def __call__(self):
        engine = self.engine
        runner = getattr(engine, "docker_reap_runner", None) if engine is not None else None
        if runner is None or engine.state_dir is None or not engine.config.get("docker_reaper", True):
            return []
        if self.thread is not None and self.thread.is_alive():
            return []
        now = engine.now()
        last = self._last(engine.state_dir)
        if last is not None and now - last < DOCKER_REAP_INTERVAL:
            return []
        self.thread = threading.Thread(target=self._run, args=(engine, runner, now), name="docker-reap", daemon=True)
        self.thread.start()
        return []

    def _run(self, engine, runner, now):
        stamp = now   # a failed run (no docker, a docker that would not answer) is tried again in an hour, not tomorrow
        try:
            docker = _docker_executable("docker")
            if docker is None:
                engine._note("docker.reap_unavailable", None, {})
                stamp = now - DOCKER_REAP_INTERVAL + DOCKER_REAP_RETRY
            else:
                result = docker_reap(docker, runner, now, True, engine.audit, engine.actor)
                engine._note("docker.reap", None, {"removed": len(result["removed"]), "errors": result["errors"][:3]})
                if result["errors"]:
                    stamp = now - DOCKER_REAP_INTERVAL + DOCKER_REAP_RETRY
            path = Path(engine.state_dir) / DOCKER_REAP_FILE
            temp = path.with_name("%s.%d.tmp" % (DOCKER_REAP_FILE, os.getpid()))
            with open(str(temp), "w") as handle:
                handle.write(json.dumps({"at": stamp}))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp), str(path))
        except Exception as err:   # a reaper never stops the daemon
            engine._note("docker.reap_error", None, {"error": _plain("%s: %s" % (type(err).__name__, err), 160)})


TEMP_JANITOR_FILE = "temp-janitor-daemon.json"
TEMP_JANITOR_INTERVAL = 6 * 86400   # the weekly gate: never twice within 6 days
TEMP_JANITOR_NAMES = 200            # names carried by one audit line (the count says the rest)
TEMP_JANITOR_GATE_FILE = "temp-janitor-gate.json"   # {"rule", "preview", "sent", "count"}: the rule the last preview was made under (hashes and a count, never a name)
TEMP_JANITOR_ACK_FILE = "temp-janitor-ack.json"     # {"rule", "preview", "by", "at"}: the recorded yes, written by `disk janitor-ack <preview>` only
TEMP_JANITOR_PREVIEW_NAMES = 25     # names listed in the preview message (the count says the rest)


def read_janitor_record(state_dir, name):
    """(ok, dict): a missing file is (True, {}); one that cannot be read in full or is not an object of text and numbers is (False, {}): the doubt applies nothing."""
    try:
        with open(str(Path(state_dir) / name), "rb") as handle:
            raw = json.loads(handle.read(4097).decode("utf-8"))
    except FileNotFoundError:
        return True, {}
    except (OSError, ValueError, UnicodeError):
        return False, {}
    if not isinstance(raw, dict) or not all(isinstance(v, (str, int, float, bool)) for v in raw.values()):
        return False, {}
    return True, raw


def janitor_apply_allowed(state_dir, rule):
    """(allowed, state): True only when the last preview was SENT under THIS rule and a yes is recorded for that very preview. state is one of
    `acked`, `no_preview` (none sent under this rule), `awaiting_ack` or `unreadable`. Every doubt is False."""
    gate_ok, gate = read_janitor_record(state_dir, TEMP_JANITOR_GATE_FILE)
    ack_ok, ack = read_janitor_record(state_dir, TEMP_JANITOR_ACK_FILE)
    if not gate_ok or not ack_ok:
        return False, "unreadable"
    if gate.get("rule") != rule or gate.get("sent") is not True or not gate.get("preview"):
        return False, "no_preview"
    if ack.get("rule") == rule and ack.get("preview") == gate["preview"] and gate.get("nonce") and ack.get("nonce") == gate["nonce"]:   # the nonce is new for every preview: a yes given to an earlier one (the rule may have come back) never counts
        return True, "acked"
    return False, "awaiting_ack"


class TempJanitorFacts:
    """The weekly temp janitor, run by the daemon itself (ask #762), the way `DockerReapFacts` runs the Docker reaper. The call is told apart from an agent's
    by the PATH it takes, never by anything a caller passes: this class runs only in the daemon's own process, only when that process set
    `engine.temp_janitor_runner` (a test engine and every command-line process has none), and it calls `disk_janitor.run` directly, never `cmd_disk_janitor`
    (whose refusal of an agent stays). It reads `temp_janitor.daemon_apply` (default false: a dry run, audited as `janitor.would_remove`); the command line never reads it.
    The selection rule is the janitor's own, unchanged. Once `TEMP_JANITOR_INTERVAL` has passed since the last run (kept in `temp-janitor-daemon.json`, written BEFORE
    the run, so a crash or a restart does not repeat it); an unreadable stamp means do not run. A background thread; returns no facts."""

    __name__ = "temp_janitor_facts"

    def __init__(self):
        self.engine, self.thread, self.noted = None, None, False
        self.outbox = None   # (preview fingerprint, text) made by the thread, handed to the Chief of Staff by the next main-thread pass

    def bind(self, engine):
        self.engine = engine

    def _hand_over(self, engine):
        """The preview the thread made: ONE message to each Chief of Staff, keyed by the preview (so a repeat is never a second message); the gate is marked
        `sent` only now, once there is someone to tell. With no Chief of Staff running it waits for the next pass."""
        box = self.outbox
        discovery = getattr(engine, "discovery", None)
        chiefs = sorted(n for n, o in discovery.agents.items() if (o.fleet or {}).get("role") == "chief-of-staff") if discovery is not None and not discovery.problems else []
        if box is None or not chiefs:
            return []
        preview, rule, text = box
        ok, gate = read_janitor_record(engine.state_dir, TEMP_JANITOR_GATE_FILE)
        if not ok or gate.get("preview") != preview:
            self.outbox = None   # the gate moved on or cannot be read: nothing is marked, and the next run makes a new preview
            return []
        try:
            _write_small(engine.state_dir, TEMP_JANITOR_GATE_FILE, dict(gate, sent=True))
        except OSError:
            return []   # not marked sent: the next pass tries again (the same key is never delivered twice)
        self.outbox = None
        if engine.audit is not None:
            engine.audit.append(engine.actor, "janitor.preview", "temp", {"rule": rule, "preview": preview, "count": gate.get("count"), "to": chiefs})
        return [Fact("janitor.preview:%s" % preview, name, "janitor", text, engine.now(), sender="switchboard") for name in chiefs]

    def _last(self, state_dir):
        """(ok, time or None): a missing file is (True, None); a file that cannot be read in full is (False, None): the doubt keeps everything."""
        try:
            with open(str(Path(state_dir) / TEMP_JANITOR_FILE)) as handle:
                text = handle.read(1024)
        except FileNotFoundError:
            return True, None
        except OSError:
            return False, None
        try:
            return True, float(json.loads(text)["at"])
        except (ValueError, KeyError, TypeError):
            return False, None

    def __call__(self):
        engine = self.engine
        runner = getattr(engine, "temp_janitor_runner", None) if engine is not None else None
        if runner is None or engine.state_dir is None:
            return []
        cfg = engine.config.get("temp_janitor") or {}
        if cfg.get("enabled", True) is False:
            return []
        if self.thread is not None and self.thread.is_alive():
            return []
        handed = self._hand_over(engine) if self.outbox is not None else []
        if handed:
            return handed
        ok, last = self._last(engine.state_dir)
        if not ok:
            if not self.noted:
                self.noted = True
                engine._note("janitor.state_unreadable", None, {"file": TEMP_JANITOR_FILE, "kept": "nothing was run this pass"})
            return []
        now = engine.now()
        if last is not None and not (now - last >= TEMP_JANITOR_INTERVAL):
            return []
        try:   # the stamp first: a run that dies half way is not repeated until the gate opens again
            path = Path(engine.state_dir) / TEMP_JANITOR_FILE
            temp = path.with_name("%s.%d.tmp" % (TEMP_JANITOR_FILE, os.getpid()))
            with open(str(temp), "w") as handle:
                handle.write(json.dumps({"at": now}))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp), str(path))
        except OSError:
            try:
                os.unlink(str(temp))
            except (OSError, UnboundLocalError):
                pass
            return []
        self.thread = threading.Thread(target=self._run, args=(engine, runner, now), name="temp-janitor", daemon=True)
        self.thread.start()
        return []

    def _run(self, engine, runner, now):
        try:
            janitor = load_disk_janitor()
            cfg = janitor.settings(engine.config.get("temp_janitor"))
            rule = janitor.rule_fingerprint(cfg)
            allowed, gate_state = janitor_apply_allowed(engine.state_dir, rule)   # a changed rule, a missing yes or a record in doubt: nothing is applied
            apply = cfg["daemon_apply"] is True and allowed
            temp = Path(cfg["dir"]) if cfg.get("dir") else janitor.default_dir()
            outcome = []
            started = time.monotonic()

            def fresh_busy(path):   # the re-check right before a removal reads the session index and the processes again, never the run's early copy
                engine._wt_session_index = None
                return janitor_busy_function(engine, runner, janitor)(path)
            with worktree_sweep(engine):   # this thread's own sweep state (it is per thread): the main thread's auto-retire reclaim never shares it
                lines, removed, kept = janitor.run(
                    temp, engine.state_dir, now, cfg, apply,
                    lambda name, detail: engine.audit.append(engine.actor, "temp.removed", _plain(name, 120), detail),
                    busy=janitor_busy_function(engine, runner, janitor), weekly=True, outcome=outcome, recheck_busy=fresh_busy)
            used = round(time.monotonic() - started, 1)
            notes = [_plain(l, 200) for l in lines if not l.startswith(("removed ", "would remove "))][:20]
            detail = {"count": len(outcome), "bytes": sum(o["bytes"] for o in outcome), "names": [_plain(o["name"], 120) for o in outcome[:TEMP_JANITOR_NAMES]],
                      "budget_seconds": cfg["weekly_scan_seconds"], "used_seconds": used, "kept": kept, "notes": notes}
            if not apply:
                detail["gate"] = gate_state if cfg["daemon_apply"] is True else "dry_run"
            engine.audit.append(engine.actor, "janitor.summary" if apply else "janitor.would_remove", "temp", detail)
            if not apply and gate_state == "no_preview":
                self._make_preview(engine, janitor, rule, outcome, cfg)
        except Exception as err:   # a janitor never stops the daemon
            engine._note("janitor.error", None, {"error": _plain("%s: %s" % (type(err).__name__, err), 160)})

    def _make_preview(self, engine, janitor, rule, outcome, cfg):
        """The selection rule changed (or was never previewed): record the preview's fingerprint in the gate, and queue ONE message to the Chief of Staff. The
        names go in that message only (and in the private `janitor.would_remove` audit line above); the gate file and the `janitor.preview` audit line hold hashes and a count."""
        names = [o["name"] for o in outcome]
        preview = janitor.preview_fingerprint(rule, names)
        try:   # a new preview ends every earlier yes
            os.unlink(str(Path(engine.state_dir) / TEMP_JANITOR_ACK_FILE))
        except FileNotFoundError:
            pass
        _write_small(engine.state_dir, TEMP_JANITOR_GATE_FILE, {"rule": rule, "preview": preview, "sent": False, "count": len(names), "nonce": uuid.uuid4().hex})
        shown = ", ".join(_plain(n, 60) for n in names[:TEMP_JANITOR_PREVIEW_NAMES])
        text = ("temp janitor: its selection rule changed, so nothing is applied until you say yes to this preview. It WOULD remove %d entr%s (%d bytes): %s%s. "
                "To say yes to this preview: `%s disk janitor-ack %s`. The yes covers the RULE this preview was made under: later weekly runs under the same rule remove "
                "whatever qualifies then, not only the names listed; a change of the rule makes a new preview and needs a new yes. The daemon removes anything only while "
                "`temp_janitor.daemon_apply` is true. If this message is lost no apply happens until the rule changes again." % (
                    len(names), "y" if len(names) == 1 else "ies", sum(o["bytes"] for o in outcome), shown or "nothing",
                    " and %d more" % (len(names) - TEMP_JANITOR_PREVIEW_NAMES) if len(names) > TEMP_JANITOR_PREVIEW_NAMES else "", PROG, preview))
        self.outbox = (preview, rule, text)


HEAVY_ORPHAN_INTERVAL = 60   # seconds between the daemon's looks for a run whose wrapper is gone


class HeavyOrphanFacts:
    """The daemon's look for orphaned heavy runs (ask #930): a run that keeps a slot after its wrapper (and so its watchdog) died is stopped past its ceiling, counted from
    the slot's own start time, and audited like a wrapper's stop (`bin/heavy_slots.py check_orphans`). Once per `HEAVY_ORPHAN_INTERVAL`, on a thread (lsof and the grace
    may take seconds: a pass never waits); returns no facts. `bin/heavy_slots.py` is loaded once by the daemon (a restart installs a change for this look); every `heavy` run loads it afresh."""

    __name__ = "heavy_orphan_facts"

    def __init__(self):
        self.engine, self.thread, self.last = None, None, None

    def bind(self, engine):
        self.engine = engine

    def __call__(self):
        engine = self.engine
        if engine is None or engine.state_dir is None or getattr(engine, "temp_janitor_runner", None) is None:   # only the real daemon: a test engine and a command line have none
            return []
        if self.thread is not None and self.thread.is_alive():
            return []
        now = engine.now()
        if self.last is not None and now - self.last < HEAVY_ORPHAN_INTERVAL:
            return []
        self.last = now
        self.thread = threading.Thread(target=self._run, args=(engine,), name="heavy-orphans", daemon=True)
        self.thread.start()
        return []

    def _run(self, engine):
        try:
            import io
            module = load_heavy_slots()
            config = engine.config.get("heavy")
            reporter = heavy_stop_reporter(module, config, lambda: engine, engine.actor)
            module.check_orphans(engine.state_dir, module.settings(config), config, time.time, reporter, io.StringIO())
        except Exception as err:   # a look never stops the daemon
            engine._note("heavy.orphan_check_error", None, {"error": _plain("%s: %s" % (type(err).__name__, err), 160)})


class Fact:
    """Something one fleet agent should be told.

    `key` names the source fact, such as `send:<from>:<id>` or `lab.fact:<id>`.
    It is what makes delivery idempotent: every message lists the keys it
    covers, and a key found in the recipient's transcript or inbox is never
    delivered again. `created` is epoch seconds. `batch` false skips the batch
    window, as `send` does.
    """

    FIELDS = ("key", "recipient", "kind", "summary", "created", "issue", "batch", "sender", "rider", "repo",
              "orders")

    def __init__(self, key, recipient, kind, summary, created, issue=None, batch=True, sender=None, rider=False,
                 repo=None, orders=False):
        for name, value in (("key", key), ("recipient", recipient), ("kind", kind)):
            if not isinstance(value, str) or not value.strip():
                raise FactError("fact %s must be a non-empty string, got %r" % (name, value))
        if not isinstance(summary, str):
            raise FactError("fact summary must be a string, got %r" % (summary,))
        if not _is_number(created):
            raise FactError("fact created must be epoch seconds, got %r" % (created,))
        if issue is not None and not (isinstance(issue, int) and not isinstance(issue, bool) and issue >= 1):
            raise FactError("fact issue must be an issue number, got %r" % (issue,))
        if not isinstance(batch, bool):
            raise FactError("fact batch must be true or false, got %r" % (batch,))
        if sender is not None and not isinstance(sender, str):
            raise FactError("fact from must be a string, got %r" % (sender,))
        if not isinstance(rider, bool):
            raise FactError("fact rider must be true or false, got %r" % (rider,))
        if not isinstance(orders, bool):
            raise FactError("fact orders must be true or false, got %r" % (orders,))
        self.key, self.recipient, self.kind, self.summary = key, recipient, kind, summary
        self.created, self.issue, self.batch, self.sender = float(created), issue, batch, sender
        self.rider = rider  # PR 13: carried in the next message, never the reason for one
        if repo is not None and not (isinstance(repo, str) and REPO_RE.match(repo)):
            raise FactError("fact repo must be OWNER/REPO, got %r" % (repo,))
        self.repo = repo  # PR 15: the repo `issue` is in; None means the recipient's own
        self.orders = orders  # PR 17: the charter's standing orders: a note on its own, at once, never a wake
        self.supersedes = ()  # #583: the creation times of this sender's earlier messages on this issue that the recipient has not seen yet (set by `send`; never stored)

    @classmethod
    def from_json(cls, obj):
        if not isinstance(obj, dict):
            raise FactError("a fact must be a JSON object")
        unknown = sorted(set(obj) - {"key", "recipient", "kind", "summary", "created", "issue", "batch", "from", "rider",
                                  "repo", "orders"})
        if unknown:
            raise FactError("fact has unknown field(s): %s" % ", ".join(unknown))
        missing = [name for name in ("key", "recipient", "kind", "summary", "created") if name not in obj]
        if missing:
            raise FactError("fact is missing: %s" % ", ".join(missing))
        return cls(obj["key"], obj["recipient"], obj["kind"], obj["summary"], obj["created"],
                   obj.get("issue"), obj.get("batch", True), obj.get("from"), obj.get("rider", False),
                   obj.get("repo"), obj.get("orders", False))

    def to_json(self):
        body = {"key": self.key, "recipient": self.recipient, "kind": self.kind, "summary": self.summary,
                "created": self.created, "issue": self.issue, "batch": self.batch, "from": self.sender,
                "rider": self.rider, "orders": self.orders}
        if self.repo is not None:  # one repo: the shape is what it always was
            body["repo"] = self.repo
        return body

    def __repr__(self):
        return "Fact(%r -> %r)" % (self.key, self.recipient)


LAB_FACTS = "lab-facts.jsonl"


def lab_fact_source(state_dir):
    """The Lab's test seam: facts a scenario runner appends to $STATE/lab-facts.jsonl.

    One JSON object per line, in Fact.to_json's shape. Read only when
    FLEET_SWITCHBOARD_LAB=1; anywhere else it yields nothing, so a stray file
    can never cause a delivery. A bad line fails the whole source, loudly: in
    the Lab a typo must not quietly drop a fact.
    """
    path = Path(state_dir) / LAB_FACTS

    def lab_facts():
        if os.environ.get(LAB_ENV) != "1":
            return []
        try:
            text = path.read_text()
        except FileNotFoundError:
            return []
        facts = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                facts.append(Fact.from_json(json.loads(line)))
            except ValueError as err:
                raise FactError("%s:%d: %s" % (path, number, err)) from None
        return facts

    return lab_facts


def fact_sources(state_dir):
    """Every fact source, in order: callables that each return a list of Facts.

    The one registration point. Worker facts (PR 4), GitHub facts (PR 5) and
    reminders join this list. `send` is not a source: it delivers directly, by
    the same rule, and stores nothing.
    """
    return [lab_fact_source(state_dir), WorkerFacts(), ReminderFacts(state_dir), ReportRiderFacts(state_dir),
            ForegroundFacts(), OrdersFacts(), SweepFacts(), TriageFacts(), PlanWakeFacts(), AlertWakeFacts(), RedCiFacts(), JobFacts(), PaneFacts(), PaneExitFacts(), HeadlessRestartFacts(), DiskFacts(), CpuFacts(), SpotlightFacts(), DockerReapFacts(), TempJanitorFacts(), HeavyOrphanFacts()]
