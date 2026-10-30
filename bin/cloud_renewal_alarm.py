"""The alarm on a failed git-token renewal of a cloud pod (#1579, part three).

The launcher's reaper writes every renewal to its own audit file (`renew-git-token`, ok true or false, the reason already scrubbed). This module is the
daemon's per-pass tick over the tail of that file: for a pod whose renewal failed it sends ONE message to the pod's boss and ONE to the cloud lane, naming
the pod and a scrubbed reason. The file is read bounded (the last READ_BYTES), bad lines are skipped, and nothing is imported from the launcher.

The rules: a line is handled once (a cursor, in `cloud-renewal-alarm.json` of the state directory, written atomically); the same pod failing again within
SERIES_SECONDS of its alarm sends nothing; a later success of the pod ends its series (the next failure alarms at once); pods that fail in one tick with the
same reason are ONE message to each cloud-lane recipient that names the count and at most NAMES_MAX names, and each pod's boss still gets its own. The
reason is cut to REASON_CHARS, flattened to printable ASCII and every token-shaped run is replaced by `[redacted]`; no other field of an audit line is copied.
No cluster access and no credential: the file is read, the daemon's own send is used (handed in by the entry)."""
import json
import os
import re
import time

CONFIG_KEY = "cloud_alarm_to"   # the module declares its own configuration key (bin/fleet-switchboard config_declarations)
CONFIG_DEFAULT = []             # empty: the chief of staff alone is the cloud lane
CONFIG_MAX = 20
ACTION = "renew-git-token"
KIND = "cloud-renewal-alarm"
EVENT = "cloud.renewal.alarm"
STATE_FILE = "cloud-renewal-alarm.json"
READ_BYTES = 262144
SERIES_SECONDS = 30 * 60
MAX_AGE = 3600            # a failure older than this is history: its token lapsed long ago
REASON_CHARS = 160
NAMES_MAX = 10
POD_POOL = 200            # pods the state file keeps
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TOKEN_RE = re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]*|[A-Za-z0-9+/_=-]{36,}")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def validate(raw, source):
    """Raises ValueError naming the key: a list of at most CONFIG_MAX agent names."""
    if not isinstance(raw, list) or len(raw) > CONFIG_MAX or not all(isinstance(n, str) and NAME_RE.fullmatch(n) for n in raw):
        raise ValueError("%s: '%s' must be a list of at most %d agent names, got %r" % (source, CONFIG_KEY, CONFIG_MAX, raw if not isinstance(raw, list) else "a list that does not fit"))


def audit_path(env=None):
    """The launcher's rule: CW_CLOUD_AUDIT, else <XDG_STATE_HOME or ~/.local/state>/cw-cloud/audit.jsonl."""
    env = os.environ if env is None else env
    return env.get("CW_CLOUD_AUDIT") or os.path.join(env.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "cw-cloud", "audit.jsonl")


def scrub(text):
    """One line of printable ASCII, token-shaped runs replaced, cut to REASON_CHARS."""
    flat = "".join(c if 32 <= ord(c) < 127 else ("?" if not c.isspace() else " ") for c in str(text if text is not None else ""))
    flat = " ".join(flat.split())
    flat = TOKEN_RE.sub("[redacted]", flat)[:REASON_CHARS]
    return TOKEN_RE.sub("[redacted]", flat).rstrip()


def _seconds(text):
    import calendar
    try:
        return float(calendar.timegm(time.strptime(text, _STAMP))) if isinstance(text, str) and text.isascii() else None
    except ValueError:
        return None


def read_tail(path):
    """The renewal lines of the file's last READ_BYTES: [(seconds, pod, worker, ok, scrubbed reason)] in file order. Unreadable file: []."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - READ_BYTES))
            lines = handle.read().decode(errors="replace").splitlines()
    except OSError:
        return []
    found = []
    for raw in lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("action") != ACTION or not isinstance(rec.get("ok"), bool):
            continue
        at, pod = _seconds(rec.get("ts")), rec.get("runtime_id")
        if at is None or not isinstance(pod, str) or not pod.startswith("pod/") or not NAME_RE.fullmatch(pod[4:]):
            continue
        worker = rec.get("worker")
        found.append((at, pod[4:], worker if isinstance(worker, str) and NAME_RE.fullmatch(worker) else None, rec["ok"], scrub(rec.get("reason"))))
    return found


def _read_state(path):
    try:
        with open(path) as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = None
    if not isinstance(state, dict) or not isinstance(state.get("pods"), dict):
        return {"cursor": None, "edge": [], "pods": {}}
    cursor = state.get("cursor")
    return {"cursor": cursor if isinstance(cursor, (int, float)) and not isinstance(cursor, bool) else None,
            "edge": [e for e in state.get("edge", []) if isinstance(e, str)] if isinstance(state.get("edge"), list) else [],
            "pods": {k: v for k, v in state["pods"].items() if isinstance(v, (int, float)) and not isinstance(v, bool)}}


def _write_state(path, state):
    temp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(state, sort_keys=True))
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _lane(config, discovery, default):
    named = (config or {}).get(CONFIG_KEY) or CONFIG_DEFAULT
    if named:
        return [n for n in named if n in discovery.agents]
    return [default] if default in discovery.agents else []


def tick(engine, discovery, send, hold_reason, boss_of, default_lane, env=None):
    """One pass. `send` is the daemon's add_repeat_reminder, `hold_reason(observation)` its hold rule, `boss_of(worker)` the resolved boss name of a
    cloud worker's record (or None), `default_lane` the chief of staff's name (the lane when the key is empty). Returns [(recipient, text)] of what was sent."""
    if discovery is None or engine.state_dir is None:
        return []
    now = engine.now()
    state_path = os.path.join(str(engine.state_dir), STATE_FILE)
    state = _read_state(state_path)
    lines = read_tail(audit_path(env))
    if not lines:
        return []
    cursor, edge = state["cursor"], set(state["edge"])
    fresh = []
    for line in lines:
        sig = "%s|%s|%s" % (line[1], line[3], line[4])
        if cursor is not None and (line[0] < cursor or (line[0] == cursor and sig in edge)):
            continue
        fresh.append(line)
    if not fresh:
        return []
    newest = max(l[0] for l in fresh)
    state["edge"] = sorted(({"%s|%s|%s" % (l[1], l[3], l[4]) for l in fresh if l[0] == newest}) | (edge if cursor == newest else set()))
    state["cursor"] = newest
    failed = {}   # pod -> (seconds, worker, reason) of its newest failure in this tick, after the last success
    for at, pod, worker, ok, reason in sorted(fresh, key=lambda l: l[0]):
        if ok:
            state["pods"].pop(pod, None)
            failed.pop(pod, None)
        elif now - at <= MAX_AGE:
            failed[pod] = (at, worker, reason)
    alarms = {}
    for pod, (at, worker, reason) in sorted(failed.items()):
        last = state["pods"].get(pod)
        if last is not None and 0 <= at - last < SERIES_SECONDS:
            continue
        state["pods"][pod] = at
        alarms[pod] = (worker, reason)
    if len(state["pods"]) > POD_POOL:
        state["pods"] = dict(sorted(state["pods"].items(), key=lambda kv: kv[1])[-POD_POOL:])
    sent = []
    lane = _lane(engine.config, discovery, default_lane)
    by_reason = {}
    for pod, (worker, reason) in alarms.items():
        by_reason.setdefault(reason, []).append(pod)
    told = {}

    def tell(to, text, subject, reason):
        item = send(engine.state_dir, to, now, now, text, KIND, subject, reason, first=True)
        if item is not None:
            sent.append((to, text))
            told.setdefault(subject, []).append(to)
    for pod, (worker, reason) in sorted(alarms.items()):
        boss = boss_of(worker) if worker else None
        seen = discovery.agents.get(boss) if boss else None
        if seen is not None and not hold_reason(seen):
            tell(boss, "Cloud renewal alarm: the git token of pod %s could not be renewed: %s" % (pod, reason or "no reason given"), pod, reason)
        else:
            boss = None
        if len(by_reason[reason]) == 1:
            for to in lane:
                if to != boss:
                    tell(to, "Cloud renewal alarm: the git token of pod %s could not be renewed: %s" % (pod, reason or "no reason given"), pod, reason)
    for reason, pods in sorted(by_reason.items()):
        if len(pods) > 1:
            names = sorted(pods)
            text = "Cloud renewal alarm: the git token of %d pods could not be renewed, the same reason: %s. Pods: %s%s" % (
                len(names), reason or "no reason given", ", ".join(names[:NAMES_MAX]), ", and %d more" % (len(names) - NAMES_MAX) if len(names) > NAMES_MAX else "")
            for to in lane:
                tell(to, text, "group:" + names[0], reason)
    _write_state(state_path, state)
    if engine.audit is not None:
        for pod, (worker, reason) in sorted(alarms.items()):
            engine.audit.append(engine.actor, EVENT, pod, {"told": sorted(set(told.get(pod, []))), "reason": reason, "group": len(by_reason[reason])})
    return sent
