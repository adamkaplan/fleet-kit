# ------------------------------------------------ the config is reloaded when the file changes

CONFIG_STABLE_SECONDS = 1.0  # a changed file must stay as it is this long before it is read (an editor writes twice)


def load_runnable_config(path=None):
    """The config as the daemon needs it: valid, and with the OpenCode v2 binary named. The one validator the start
    path and a reload both use."""
    config = load_config(path)
    if config["opencode"] is None:
        raise ConfigError("'opencode' is not configured in %s: set it to the absolute path of the "
                          "OpenCode v2 binary" % (path or config_path()))
    return config


def config_signature(path):
    """(mtime, size) of the file, None when there is none (every default applies), or an `unreadable` marker."""
    try:
        info = os.stat(str(path))
    except FileNotFoundError:
        return None
    except OSError as err:
        return ("unreadable", err.errno)
    return (info.st_mtime_ns, info.st_size)


class ConfigWatch:
    """Compares the config file's mtime and size with what the daemon last loaded, once a pass.

    A changed file is read only after it has stayed the same for CONFIG_STABLE_SECONDS, so an editor's two writes
    are one reload and a half-written file is only ever read as the invalid file it is. `check()` answers `reload`
    when the file is valid and says something different (`changed` names the keys, never their values, and `config`
    is the new one). An invalid file never stops anything: it is audited once (`config.invalid`, with the reason)
    and recorded in daemon.json, and the daemon goes on with the config it has.
    """

    def __init__(self, path, config, now, audit=None, actor="daemon", record=None, signature=None):
        self.path, self.config, self.now, self.audit, self.actor = path, config, now, audit, actor
        self.record = record or (lambda **fields: None)
        self.evaluated = signature if signature is not None else config_signature(path)
        self.loaded_at, self.pending_since = now(), None
        self.seen = None
        self.invalid, self.changed, self.new_config = None, [], None

    @property
    def pending(self):
        return self.seen is not None

    def _audit(self, event, detail):
        if self.audit is not None:
            self.audit.append(self.actor, event, None, detail)

    def check(self):
        if fault("config-read-once"):
            return None
        signature, moment = config_signature(self.path), self.now()
        if signature == self.evaluated:
            self.seen = None
            return None
        if signature != self.seen:
            self.seen, self.pending_since = signature, moment
            return None
        if moment - self.pending_since < CONFIG_STABLE_SECONDS:
            return None
        try:
            new = load_runnable_config(self.path)
        except ConfigError as err:
            self.evaluated, self.seen = signature, None
            reason = _one_line(str(err), 300)
            since = (self.invalid or {}).get("since") or iso(moment)
            self.invalid = {"since": since, "reason": reason, "loaded_at": iso(self.loaded_at)}
            self._audit("config.invalid", {"reason": reason, "still_running_on_the_config_loaded_at": iso(self.loaded_at)})
            self.record(config_invalid=self.invalid)
            return None
        if config_signature(self.path) != signature:
            return None  # it changed while it was read: look again once it is still
        self.evaluated, self.seen = signature, None
        recovered, self.invalid = self.invalid, None
        if recovered:
            self.record(config_invalid=None)
        self.changed = sorted(name for name in set(new) | set(self.config) if new.get(name) != self.config.get(name))
        if not self.changed:
            if recovered:
                self._audit("config.recovered", {"was_invalid_since": recovered["since"]})
            return None
        self.new_config = new
        return "reload"


def reexec_in_place(argv):
    """Replace this process with `argv`, keeping the pid and the run.lock descriptor (so the lock is never released
    and `ensure` never sees a gap). Returns only when it could not."""
    fd, env = _HELD_LOCK["fd"], dict(os.environ)
    if fd is not None:
        os.set_inheritable(fd, True)
        env[LOCK_FD_ENV] = str(fd)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(argv[0], argv, env)
    except OSError:
        if fd is not None:
            os.set_inheritable(fd, False)
        raise


def reload_in_place(engine, hub, judge_server, watch, argv, execute=reexec_in_place):
    """Apply a valid config change by starting the daemon over: everything built from the config (engine, watch set,
    forwarders, judge server, accounts, clients) is rebuilt together, so nothing is half applied. The forwarders and
    the judge socket are stopped first, the audit line says which, and the new process holds the same lock."""
    forwarders = sorted(repo for repo, w in hub.watches.items() if w.proc is not None) if hub is not None else []
    if hub is not None:
        hub.stop()
    if judge_server is not None:
        judge_server.stop()
    engine.audit.append(engine.actor, "config.reloaded", None, {
        "changed": watch.changed, "forwarders_stopped": forwarders,
        "judge_socket": "closed here, bound again by the new process"})
    try:
        execute(argv)
    except OSError as err:  # it could not start over: carry on, with the old config and what was stopped started again
        engine.audit.append(engine.actor, "config.reload_failed", None, {"error": _one_line(str(err), 200)})
        if hub is not None:
            hub.start()
        if judge_server is not None:
            judge_server.start()


def format_config_lines(daemon):
    """The daemon's own account of its config, for `status`: when it loaded, or that the file is invalid now."""
    lines = []
    invalid = (daemon or {}).get("config_invalid")
    if invalid:
        lines.append("ERROR config     INVALID since %s: %s; still running on the config loaded at %s"
                     % (invalid.get("since"), invalid.get("reason"), invalid.get("loaded_at")))
    elif (daemon or {}).get("config_loaded_at"):
        lines.append("      config     loaded %s (the daemon's own record)" % daemon["config_loaded_at"])
    return lines
