"""registry: the one agent registry (slice 2 of #903). Interface frozen in docs/agent-registry.md (#906).

One file, `registry.json`, in the daemon's state directory (mode 0600): `{"version": 1, "agents": {name: Agent}, "retired": {name: Retired}}`.
Readers take no lock and rely on the atomic write (temp file, fsync, rename). Every write call takes an exclusive flock on `registry.lock`, reads,
changes and writes in one step, and returns `{"changed": bool, "problem": str|None}`. LOCK ORDER: this lock first, then the boss-record lock.

`read` returns `{"whole", "version", "agents", "retired", "problem"}`: `whole` is true only when the file was read, parsed and its version is known
(a missing file is whole and empty). Anything else reads nothing (`agents` and `retired` empty, `whole` false, one `problem` line); a caller that
ACTS on a read acts only when `whole` is true. `pane-health.json` and `retired.json` are folded in on the first read or write of a state directory
that has no registry yet; `retired.json` is then rewritten from the registry as DERIVED output on every change.

Python 3.9+, standard library only; loaded by path from beside bin/fleet-switchboard.
"""

import fcntl
import json
import os
import time
from pathlib import Path

VERSION = 1
FILE = "registry.json"
LOCK = "registry.lock"
AUDIT = "registry-audit.jsonl"
RETIRED_DERIVED = "retired.json"
PANE_HEALTH = "pane-health.json"
MAX_BYTES = 4000000
AUDIT_MAX_BYTES = 512000
RETIRED_KEEP_SECONDS = 4 * 7 * 86400   # a retirement long past the status window is dropped (the bound the old retired.json had)
STATES = ("working", "blocked", "idle", "detached", "dead")
ORCHESTRATOR = "orchestrator"   # the role of an agent that owns a charter (a name, not a comparison: this module decides nothing about who may act)
AGENT_FIELDS = ("session", "pane", "role", "charter", "repo", "workspace", "cwd")   # the seven fields the old readers use
RECORD_FIELDS = ("pane", "role", "charter", "repo", "workspace", "cwd")             # what a retirement keeps of an agent row
LOCK_SECONDS = 5


def _version_line(found):
    return "%s is version %s; this reader knows %d: nothing read" % (FILE, found, VERSION)


def _empty(whole=True, problem=None, version=VERSION):
    return {"whole": whole, "version": version, "agents": {}, "retired": {}, "problem": problem}


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _parse(raw):
    """The Read for the bytes of the file."""
    if len(raw) > MAX_BYTES:
        return _empty(False, "%s is too large: nothing read" % FILE)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return _empty(False, "%s is not valid JSON: nothing read" % FILE)
    if not isinstance(doc, dict) or not _is_int(doc.get("version")):
        return _empty(False, "%s has no version: nothing read" % FILE)
    if doc["version"] > VERSION:
        return _empty(False, _version_line(doc["version"]), doc["version"])
    agents, retired = doc.get("agents"), doc.get("retired", {})
    if not isinstance(agents, dict) or not isinstance(retired, dict) \
            or not all(isinstance(v, dict) for v in agents.values()) or not all(isinstance(v, dict) for v in retired.values()):
        return _empty(False, "%s is not a map of agents: nothing read" % FILE)
    return {"whole": True, "version": doc["version"], "agents": agents, "retired": retired, "problem": None}


def _load(state_dir):
    """The file as it is: a Read, with `exists` apart. No migration."""
    try:
        with open(str(Path(state_dir) / FILE), "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as err:
        return _empty(False, "%s cannot be read (%s): nothing read" % (FILE, getattr(err, "strerror", None) or type(err).__name__))
    return _parse(raw)


def _small(state_dir, name):
    try:
        with open(str(Path(state_dir) / name), "rb") as handle:
            raw = json.loads(handle.read(1000001).decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _old_retired(state_dir):
    """(retired map, problem) from the old `retired.json`: a MISSING file is empty; one that is unreadable, not JSON, not a map of objects or too large is a
    problem (the fold must never read corruption as 'nobody is retired': that would let a retired agent come back)."""
    try:
        with open(str(Path(state_dir) / RETIRED_DERIVED), "rb") as handle:
            raw = handle.read(1000001)
    except FileNotFoundError:
        return {}, None
    except OSError as err:
        return {}, "%s cannot be read (%s)" % (RETIRED_DERIVED, getattr(err, "strerror", None) or type(err).__name__)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return {}, "%s is not valid JSON" % RETIRED_DERIVED
    if not isinstance(data, dict) or len(raw) > 1000000 or not all(isinstance(v, dict) for v in data.values()):
        return {}, "%s is not a map of retired agents" % RETIRED_DERIVED
    return data, None


def pane_record(row):
    """What a retirement keeps of an agent row: the pane fields, and for a non-local agent its `kind` and `run_id` (so `unretire` restores a cloud worker as one)."""
    out = {k: row[k] for k in RECORD_FIELDS if isinstance(row, dict) and row.get(k) is not None}
    if isinstance(row, dict) and row.get("kind") not in (None, "local"):
        out["kind"] = row["kind"]
        if row.get("run_id") is not None:
            out["run_id"] = row["run_id"]
    return out


def _agent(fields, state="idle", since=None):
    """A complete Agent: the seven fields, `state`, `kind`, `run_id`, `since`; a new optional field given is kept."""
    out = {k: fields.get(k) for k in AGENT_FIELDS}
    out.update({k: v for k, v in fields.items() if k not in out and k not in ("seen",)})
    out["state"] = out.get("state") if out.get("state") in STATES else state
    out["kind"] = out.get("kind") or "local"
    out["run_id"] = out.get("run_id")
    out["since"] = out.get("since") if _is_int(out.get("since")) else (since if since is not None else int(time.time()))
    return out


def fold_old(state_dir, now=None):
    """The registry document made from today's records: `pane-health.json` agents (with their states), `retired.json` and the cloud records
    of `state/cloud/*.json`. Nothing is lost: the seven fields, every retirement incl. `record` and `headless`. Pure: reads, writes nothing."""
    now = int(time.time() if now is None else now)
    health = _small(state_dir, PANE_HEALTH)
    retired, problem = _old_retired(state_dir)
    if problem:
        return _empty(False, "%s: nothing read" % problem)
    states = health.get("states") if isinstance(health.get("states"), dict) else {}
    agents = {}
    for name, row in (health.get("agents") if isinstance(health.get("agents"), dict) else {}).items():
        if not isinstance(row, dict):
            continue
        entry = retired.get(name)
        if isinstance(entry, dict) and entry.get("session") is not None and entry.get("session") == row.get("session"):
            continue   # retired for this very session: it lives under `retired` only
        live = (states.get(name) or {}).get("state") if isinstance(states.get(name), dict) else None
        mapped = {"detached": "detached", "dead": "dead"}.get(live, "idle")
        agents[name] = _agent(row, mapped, row.get("seen") if _is_int(row.get("seen")) else now)
    cloud = Path(state_dir) / "cloud"
    if cloud.is_dir():
        for path in sorted(cloud.glob("*.json")):
            try:
                rec = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and path.name[:-5] not in agents and not covers(retired.get(path.name[:-5]), rec.get("session")):
                agents[path.name[:-5]] = _agent({"session": rec.get("session"), "repo": rec.get("repo"), "kind": "cloud",
                                                 "run_id": rec.get("run_id") or rec.get("runtime_id")}, "idle", now)
    return {"whole": True, "version": VERSION, "agents": agents, "retired": retired, "problem": None}


def _text(doc):
    return json.dumps({"version": doc["version"] if _is_int(doc.get("version")) else VERSION, "agents": doc["agents"], "retired": doc["retired"]},
                      indent=1, sort_keys=True) + "\n"


def _write_atomic(path, text):
    temp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(str(temp), str(path))
    except OSError:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise


def _derive(state_dir, doc):
    """`retired.json` as derived output of the registry (the old format, so old readers see the same). Best effort: the registry is the record."""
    try:
        _write_atomic(Path(state_dir) / RETIRED_DERIVED, json.dumps(doc["retired"], indent=1, sort_keys=True) + "\n")
    except OSError:
        pass


def _stamp(now):
    return int(time.time() if now is None else now)


def _audit(state_dir, events, now=None):
    if not events:
        return
    path = Path(state_dir) / AUDIT
    try:
        try:
            if path.stat().st_size > AUDIT_MAX_BYTES:
                lines = path.read_text().splitlines()
                path.write_text("\n".join(lines[len(lines) // 2:]) + "\n")
        except OSError:
            pass
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as handle:
            for event in events:
                handle.write(json.dumps(dict({"at": _stamp(now)}, **event), sort_keys=True) + "\n")
    except OSError:
        pass


class _Lock:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / LOCK
        self.fd = None

    def __enter__(self):
        self.fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT, 0o600)
        deadline = time.monotonic() + LOCK_SECONDS
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    os.close(self.fd)
                    raise TimeoutError("%s is busy" % LOCK)
                time.sleep(0.02)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def _migrate(state_dir):
    """Create the registry from the old records when there is none (under the lock). The Read, or None when it could not be written."""
    try:
        with _Lock(state_dir):
            found = _load(state_dir)
            if found is not None:
                return found
            doc = fold_old(state_dir)
            if not doc["whole"]:
                return doc
            _write_atomic(Path(state_dir) / FILE, _text(doc))
            _derive(state_dir, doc)
            return doc
    except (OSError, TimeoutError):
        return None


def read(state_dir):
    """The Read. No lock. A directory with no registry but old records is folded first; a fold that cannot be written is still returned (in memory)."""
    if state_dir is None:
        return _empty()
    found = _load(state_dir)
    if found is not None:
        return found
    old = [n for n in (PANE_HEALTH, RETIRED_DERIVED) if (Path(state_dir) / n).exists()] or (["cloud"] if (Path(state_dir) / "cloud").is_dir() else [])
    if not old:
        return _empty()
    return _migrate(state_dir) or fold_old(state_dir)


def get(state_dir, name):
    """The Agent, or None. `None` with a problem is not 'absent': use `read` to tell (a caller that acts reads `whole`)."""
    return read(state_dir)["agents"].get(name)


def get_retired(state_dir, name):
    """The Retired record (session, at, by, reason, `record`...), or None. A record with no `session` covers nothing (#870 rule): it is still returned
    (the caller decides), but `known_agent` and `covers` treat it as no agent."""
    entry = read(state_dir)["retired"].get(name)
    return entry if isinstance(entry, dict) else None


def covers(entry, session):
    """True when the retirement `entry` is for THIS session (#870: no session on the record covers nothing)."""
    return isinstance(entry, dict) and entry.get("session") is not None and entry.get("session") == session


def _mutate(state_dir, change, now=None):
    """One locked read-change-write. `change(doc)` returns (changed, events, problem)."""
    try:
        with _Lock(state_dir):
            doc = _load(state_dir)
            if doc is None:
                doc = fold_old(state_dir)
            if not doc["whole"]:
                return {"changed": False, "problem": doc["problem"]}
            changed, events, problem = change(doc)
            if changed:
                _write_atomic(Path(state_dir) / FILE, _text(doc))
                _derive(state_dir, doc)
            _audit(state_dir, events, now)   # a change that wrote nothing may still carry an event (a REFUSED removal is audited with its reason)
            return {"changed": changed, "problem": problem}
    except TimeoutError as err:
        return {"changed": False, "problem": str(err)}
    except OSError as err:
        return {"changed": False, "problem": "%s could not be written (%s)" % (FILE, getattr(err, "strerror", None) or type(err).__name__)}


def _prune(retired, now):
    for old in [n for n, v in retired.items() if now - (v.get("at") or 0) > RETIRED_KEEP_SECONDS]:
        retired.pop(old, None)


def _retire(state_dir, name, by, reason, auto, event, session=None, record=None, extra=None, now=None):
    def change(doc):
        now_ = _stamp(now)
        row = doc["agents"].pop(name, None)
        if row is None and session is None and name not in doc["retired"]:
            pass   # nothing known of it: retired with no session, which covers nothing
        _prune(doc["retired"], now_)
        entry = {"at": now_, "by": by, "reason": reason, "session": session or (row or {}).get("session"), "auto": bool(auto)}
        rec = record if record is not None else (pane_record(row) if row else None)
        if rec:
            entry["record"] = rec
        entry.update(extra or {})
        doc["retired"][name] = entry
        return True, [{"event": event, "name": name, "by": by, "reason": reason, "auto": bool(auto), "session": entry["session"]}], None
    return _mutate(state_dir, change, now)


def retire(state_dir, name, by, reason, auto=False, session=None, record=None, extra=None, now=None):
    """The agent keeps its record under `retired` and leaves `agents`. `session`, `record` and `extra` (optional) carry what the caller knows when the
    agent has no row (a lost headless worker, a retirement of an agent seen only by discovery)."""
    return _retire(state_dir, name, by, reason, auto, "retire", session, record, extra, now)


def close(state_dir, name, by, reason, session=None, record=None, extra=None, now=None):
    """The pane is closed on purpose: as `retire`, with the reason recorded."""
    return _retire(state_dir, name, by, reason, False, "close", session, record, extra, now)


def unretire(state_dir, name):
    """The reverse of retire. The agent row comes back from `retired[name].record` with its session (a headless worker, which has no pane record, comes back
    from its own launch record, not from here). `changed: false` with no problem when it was not retired."""
    def change(doc):
        entry = doc["retired"].pop(name, None)
        if entry is None:
            return False, [], None
        if entry.get("session") and not entry.get("headless") and name not in doc["agents"]:
            doc["agents"][name] = _agent(dict(entry["record"] if isinstance(entry.get("record"), dict) else {}, session=entry["session"]), "idle")
        return True, [{"event": "unretire", "name": name}], None
    return _mutate(state_dir, change)


def upsert(state_dir, name, agent, now=None):
    """Add or update an agent. NO CHURN: nothing changed writes nothing (`changed: false`). An agent retired for this very session is refused."""
    def change(doc):
        old = doc["agents"].get(name)
        if covers(doc["retired"].get(name), (agent or {}).get("session")):
            return False, [], "%s is retired for this session" % name
        new = _agent(dict(agent or {}), (old or {}).get("state") or "idle", (old or {}).get("since") or _stamp(now))
        if old == new:
            return False, [], None
        doc["agents"][name] = new
        return True, [{"event": "update" if old else "add", "name": name}] if not old else [], None   # an audit line on add only: a change of state is no event
    return _mutate(state_dir, change, now)


def is_charter(value):
    """True for an issue number a charter can be (a positive int, never a bool)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def orchestrator_charters(agents):
    """{name: charter number} of the orchestrators of a registry's `agents` that record a charter (rows with none are left out). The ONE reading of "whose charter is this" (#969)."""
    return {name: row["charter"] for name, row in (agents or {}).items()
            if isinstance(row, dict) and row.get("role") == ORCHESTRATOR and is_charter(row.get("charter"))}


def duplicate_charters(agents, repos=None):
    """{(repo, charter): [names]} of a charter number recorded by two or more orchestrators of the same repo (sorted names). `repos` maps an agent name to its repo when the row has none."""
    found = {}
    for name, number in orchestrator_charters(agents).items():
        row = agents[name]
        repo = row.get("repo") or (repos or {}).get(name)
        found.setdefault((repo, number), []).append(name)
    return {key: sorted(names) for key, names in found.items() if len(names) > 1}


def set_charter(state_dir, name, charter, by, now=None):
    """Write an orchestrator's registry charter (an issue number, or None to clear it): the ONLY command-side writer of the field. One audit event with the old and the new value and who ran it.
    Refused (nothing written, a problem line) for an agent not in the registry, one retired, or one that is not an orchestrator, and for a value that is not an issue number or None."""
    def change(doc):
        row = doc["agents"].get(name)
        if not isinstance(row, dict) or name in doc["retired"]:
            return False, [], "%s is not a registered agent" % name
        if row.get("role") != ORCHESTRATOR:
            return False, [], "%s is not an orchestrator" % name
        if charter is not None and not is_charter(charter):
            return False, [], "a charter is an issue number or none"
        old = row.get("charter")
        if old == charter:
            return False, [], None
        row["charter"] = charter
        return True, [{"event": "charter", "name": name, "old": old, "new": charter, "by": by}], None
    return _mutate(state_dir, change, now)


def reconcile_remove(state_dir, name, evidence, whole):
    """Remove a DEAD agent. Refuses unless `whole` is True AND the registry's own check of the evidence shows it dead: the agent's recorded state is
    `dead`, and the live read lacked its pane (`evidence["pane_listed"]` false) and its process (`evidence["process_found"]` false)."""
    def change(doc):
        row = doc["agents"].get(name)
        if row is None:
            return False, [], None
        if row.get("state") != "dead" or not isinstance(evidence, dict) or evidence.get("pane_listed") is not False \
                or evidence.get("process_found") is not False:
            why = "%s is not shown dead by the evidence: nothing removed" % name
            return False, [{"event": "reconcile_refused", "name": name, "evidence": evidence if isinstance(evidence, dict) else None, "why": why}], why
        doc["agents"].pop(name)
        return True, [{"event": "reconcile_remove", "name": name, "evidence": evidence, "session": row.get("session")}], None
    if whole is not True:
        why = "the read was not whole: nothing removed"
        _audit(state_dir, [{"event": "reconcile_refused", "name": name, "evidence": evidence if isinstance(evidence, dict) else None, "why": why}])
        return {"changed": False, "problem": why}
    return _mutate(state_dir, change)
