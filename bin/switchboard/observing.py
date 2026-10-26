# --------------------------------------------------------- observing the fleet

# herdr's agent_status -> the switchboard's. `done` is idle that you have not
# looked at yet; anything not listed is unknown, and unknown holds.
STATUS = {"working": "working", "idle": "idle", "done": "idle", "blocked": "blocked"}

READ_LIMIT = 200    # transcript messages per read, newest first
SINCE_SLACK = 60    # seconds read back beyond the oldest fact, for clock skew
MESSAGE_CAP = 4000  # characters in one delivered message
HELD_TOAST_AFTER = 15 * 60  # seconds an item is held before you get a toast


def agent_session(entry):
    """The v2 session id a herdr agent entry names, or None.

    herdr 0.9 reports `agent_session` as an object, `{"kind": "id", "value":
    "ses_..."}` (S6); a plain string is accepted too.
    """
    value = entry.get("agent_session") if isinstance(entry, dict) else None
    if isinstance(value, dict):
        value = value.get("value") if value.get("kind", "id") == "id" else None
    return value if isinstance(value, str) and value else None


PROGRAM_VERSION_SECONDS = 3
_VERSIONS = {}   # (realpath, mtime) -> the first line of `<program> --version`, or None; so a pass spawns nothing twice


def program_version(path):
    """The first line `<path> --version` prints, or None (it could not run, took over PROGRAM_VERSION_SECONDS, or said
    nothing). Remembered per file and modification time: the daemon asks on every pass."""
    try:
        key = (path, os.stat(path).st_mtime)
    except OSError:
        return None
    if key in _VERSIONS:
        return _VERSIONS[key]
    version = None
    try:
        done = subprocess.run([path, "--version"], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=PROGRAM_VERSION_SECONDS)
        lines = (done.stdout or "").strip().splitlines() if done.returncode == 0 else []
        version = lines[0].strip() if lines else None
    except (OSError, subprocess.SubprocessError, ValueError):
        version = None
    if len(_VERSIONS) > 64:
        _VERSIONS.clear()
    _VERSIONS[key] = version
    return version


def resolve_program(name):
    """The real path of a program the pane's command line names by a BARE name (`opencode`): the daemon's PATH lookup, then
    realpath. None when it is not a bare name or is not found (a relative path means nothing without the pane's directory)."""
    if not isinstance(name, str) or not name or "/" in name:
        return None
    found = shutil.which(name)
    return os.path.realpath(found) if found else None


def runs_program(cmdline, executable):
    """True when a process command line is `executable`, alone or with arguments.

    The program and the configured path are compared by realpath, both sides: a pane runs the resolved
    path of a binary the config names by its stable link (a package upgrade moves the target, not the link).
    A pane started with the bare command (`opencode -s ses_...`, which the shell resolved on ITS path) is the same program when
    the bare name resolves, on the daemon's PATH, to the same real file; when it resolves to a different file the two are still the
    same program if both answer `--version` with the same line (a copy). Anything that cannot be decided is NOT the program: a
    shell, an unknown name, a different binary or version is held.
    """
    if not isinstance(cmdline, str) or not executable:
        return False
    if cmdline == executable or cmdline.startswith(executable + " "):
        return True
    first = cmdline.split(" ", 1)[0]
    wanted = os.path.realpath(executable) if os.path.isabs(executable) else None
    if wanted is not None and not os.path.isabs(first):
        found = resolve_program(first)
        if found is not None:
            if found == wanted:
                return True
            if os.path.basename(found) not in (os.path.basename(executable), os.path.basename(wanted)):
                return False   # a program of another name (a shell, node) is never asked for its version
            seen, configured = program_version(found), program_version(wanted)
            return bool(seen) and seen == configured
        return False
    if not os.path.isabs(cmdline) or not os.path.isabs(executable):
        return False
    ends = [at for at, char in enumerate(cmdline) if char == " "] + [len(cmdline)]
    return any(os.path.realpath(cmdline[:end]) == wanted for end in ends[:16])


def _seconds(value):
    """v2 time (epoch milliseconds, or seconds) -> epoch seconds; None if absent."""
    if not _is_number(value):
        return None
    return value / 1000.0 if value > 1e11 else float(value)


def _fleet_meta(record):
    """`metadata.fleet` of a v2 record: a message has `metadata` at the top;
    an inbox item (session.inbox.list, 2.0.22) has it under `payload`."""
    if not isinstance(record, dict):
        return None
    meta = record.get("metadata")
    if meta is None and isinstance(record.get("payload"), dict):
        meta = record["payload"].get("metadata")
    fleet = meta.get("fleet") if isinstance(meta, dict) else None
    return fleet if isinstance(fleet, dict) else None


def _fleet_keys(record):
    keys = (_fleet_meta(record) or {}).get("keys")
    return [key for key in keys if isinstance(key, str)] if isinstance(keys, list) else []


def _is_orders_note(record):
    """PR 17: a record whose every key is a standing-orders key (`orders:...`)."""
    keys = _fleet_keys(record)
    return bool(keys) and all(key.startswith("orders:") for key in keys)


def _created(record):
    times = record.get("time") if isinstance(record, dict) else None
    return _seconds(times.get("created")) if isinstance(times, dict) else None


def iso(epoch):
    return decision_core.iso(epoch)


class Observation:
    """One fleet agent as observed now: herdr's status, v2's session.

    `last_human_prompt` is the time of your newest prompt within the
    engagement window, or None when there is none in it. `problem` is set
    when something needed for a decision could not be read; it holds.
    """

    def __init__(self, name, session_id, pane, status, fleet, idle_at=None, outcome=None,
                 last_human_prompt=None, problem=None):
        self.name, self.session_id, self.pane, self.status = name, session_id, pane, status
        self.fleet, self.idle_at, self.outcome = fleet, idle_at, outcome
        self.last_human_prompt, self.problem = last_human_prompt, problem
        self.created = None  # PR 17: when v2 says the session began (epoch seconds)
        self.wrong_program = False  # the pane runs something other than the configured v2
        self.headless = False  # #433: a worker with no terminal: what it does is read from the OpenCode service, never from herdr
        self.conflict = []  # other sessions that claim the same name

    def engaged(self, now, window):
        """You prompted this agent within `window` seconds. Off under `no-engagement-gate`."""
        if fault("no-engagement-gate"):
            return False
        return self.last_human_prompt is not None and now - self.last_human_prompt < window

    def to_json(self):
        out = {"name": self.name, "session": self.session_id, "pane": self.pane, "status": self.status,
               "fleet": self.fleet, "idle_at": iso(self.idle_at), "outcome": self.outcome,
               "last_human_prompt": iso(self.last_human_prompt), "problem": self.problem,
               "wrong_program": self.wrong_program, "conflict": self.conflict}
        if self.headless:   # present only for a headless worker: a terminal-only fleet's output is what it always was
            out["headless"] = True
        return out


class Discovery:
    """Every fleet agent found in one look, by name, and what got in the way."""

    def __init__(self):
        self.agents = {}
        self.problems = []
        self.entries = []  # herdr's agent list as read, so render can compare tokens
        self.headless_records = None   # the headless records THIS look scanned (None: it did not look): the fact sources judge only against these

    def add(self, observation):
        known = self.agents.get(observation.name)
        if known is None:
            self.agents[observation.name] = observation
        elif known.session_id != observation.session_id and observation.session_id not in known.conflict:
            known.conflict.append(observation.session_id)

    def missing(self, name):
        """Why `name` has no observation: not running, or something unreadable."""
        if self.problems:
            return "%s not found; %s" % (name, "; ".join(self.problems))
        return "%s not found: no herdr pane shows a v2 session named %s" % (name, name)
