"""Reassign a running worker to another orchestrator (ask #347, slice 4).

A worker's boss is its session's `metadata.fleet.reports_to`, set at launch. The v2 service has no operation that updates a session's
metadata, so a reassign does NOT rewrite the session: the switchboard keeps a small private record per worker (`boss-overrides.json`: a boss
override, mode 0600, written atomically under a lock, at most MAX_ENTRIES entries and MAX_BYTES bytes) and applies it where discovery reads a
session's fleet metadata (`overlay`), so `report`, the wake grouping and every boss lookup follow it. An override is bound to the worker's
SESSION: a retired worker that is launched again under the same name starts with its own boss, and an override older than MAX_AGE_DAYS, or
one of a retired worker, is dropped at the next write. A worker can neither set nor clear one: the command refuses every caller but the Chief
of Staff or the operator (a plain shell), and a refusal is audited.

It fails closed: no consent, consent from someone who is not the worker's CURRENT boss, a boss or a target that is not a live orchestrator,
or any problem reading or writing the record, means nothing is written. The consent is the old boss's name, recorded with the reassign in
the same command (and audited with who ran it); both orchestrators and the worker are told. `--undo` removes the override (the worker's own
metadata applies again); it needs no consent because it only restores the boss the worker was launched with.

The logic is here; `bin/fleet-switchboard` registers the command and hands this module a namespace `sb` of its own helpers (`NEEDS`).
"""

import fcntl
import json
import os
import re
import time
from pathlib import Path

FILE = "boss-overrides.json"
LOCK = "boss-overrides.lock"
MAX_ENTRIES = 100
MAX_BYTES = 64 * 1024
MAX_AGE_DAYS = 30
OPERATOR = "operator"
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")   # herdr's own pattern for an agent name

NEEDS = ("PROG", "EXIT_OK", "EXIT_ERROR", "EXIT_USAGE", "boss_name", "iso", "_one_line", "read_retired")

_cache = {}   # path -> ((mtime_ns, size), overrides): one small file read again only when it changes


def _clean(raw):
    """The valid entries of a parsed file: {worker: {"boss", "was", "original" (the boss it was launched with), "session", "by", "at"}}; anything else is dropped."""
    found = {}
    items = raw.get("overrides") if isinstance(raw, dict) else None
    for worker, entry in (items.items() if isinstance(items, dict) else []):
        if (isinstance(worker, str) and _NAME.match(worker) and isinstance(entry, dict)
                and isinstance(entry.get("boss"), str) and _NAME.match(entry["boss"])
                and isinstance(entry.get("session"), str) and entry["session"]
                and isinstance(entry.get("at"), (int, float)) and not isinstance(entry.get("at"), bool)):
            found[worker] = {"boss": entry["boss"], "was": str(entry.get("was") or "")[:64], "original": str(entry.get("original") or "")[:64], "session": entry["session"][:128],
                             "by": str(entry.get("by") or "")[:64], "at": float(entry["at"])}
    return found


def read_overrides(state_dir):
    """(overrides, problem): the valid entries, and a sentence when the file exists but cannot be used (then NO override applies)."""
    if state_dir is None:
        return {}, None
    path = Path(state_dir) / FILE
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}, None
    except OSError as err:
        return {}, "%s cannot be read: %s" % (FILE, err.strerror or err)
    key = (stat.st_mtime_ns, stat.st_size)
    hit = _cache.get(str(path))
    if hit and hit[0] == key:
        return hit[1], None
    if stat.st_size > MAX_BYTES:
        return {}, "%s is larger than %d bytes: no override applies" % (FILE, MAX_BYTES)
    try:
        found = _clean(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeError) as err:
        return {}, "%s cannot be read (%s): no override applies" % (FILE, str(err)[:80])
    _cache[str(path)] = (key, found)
    return found, None


def overlay(state_dir, fleet, session_id):
    """`fleet` (a session's metadata.fleet) with `reports_to` replaced by the worker's override, when one exists for THIS session; `fleet`
    itself otherwise. Never raises: an unreadable file leaves the launch-time boss."""
    try:
        entry = read_overrides(state_dir)[0].get((fleet or {}).get("name"))
    except Exception:
        return fleet
    if entry is None or entry["session"] != session_id:
        return fleet
    return dict(fleet, reports_to=entry["boss"])


def write_overrides(state_dir, overrides):
    """Replace the file atomically, mode 0600. OSError when it cannot be written, ValueError when it would be too large."""
    path = Path(state_dir) / FILE
    data = json.dumps({"overrides": overrides}, indent=1, sort_keys=True) + "\n"
    if len(data.encode("utf-8")) > MAX_BYTES or len(overrides) > MAX_ENTRIES:
        raise ValueError("too many overrides (the file holds at most %d entries): undo one first" % MAX_ENTRIES)
    temp = path.with_name("%s.%d.tmp" % (FILE, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(path))
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise


def prune(overrides, retired, now):
    """Drop the overrides of retired workers and those older than MAX_AGE_DAYS. Returns the number dropped."""
    gone = [w for w, e in overrides.items() if w in retired or now - e["at"] > MAX_AGE_DAYS * 86400]
    for worker in gone:
        overrides.pop(worker, None)
    return len(gone)


class locked:
    """The lock that makes read-modify-write of the file one step."""

    def __init__(self, state_dir):
        self.path = str(Path(state_dir) / LOCK)

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def check_caller(caller, role):
    """The refusal text, or None: only the Chief of Staff or the operator (a plain shell: `caller` is None) reassigns. A worker never does."""
    if caller is None or role == "chief-of-staff":
        return None
    if role == "unidentified":
        return "only the chief-of-staff or the operator reassigns a worker; this is %s" % caller
    return "only the chief-of-staff or the operator reassigns a worker; %s is %s" % (caller, "a %s" % role if role else "not given a role")


def live_orchestrator(discovery, name):
    seen = discovery.agents.get(name) if isinstance(name, str) else None
    return seen is not None and (seen.fleet or {}).get("role") == "orchestrator" and not seen.problem


def tell(sb, caller, recipient, text):
    """One message to `recipient`. None when delivered, else why not."""
    try:
        outcome = sb.engine.send(caller or OPERATOR, recipient, text, None, None)
    except Exception as err:   # an unreachable recipient never hides the rest
        return sb._one_line("%s: %s" % (type(err).__name__, err), 160)
    return None if outcome.get("sent") else "%s (%s)" % (outcome.get("action"), outcome.get("reason"))


def run_reassign(sb, worker, to, consent, undo, caller, role, out, err):
    """`reassign <worker> --to <orchestrator> --consent-from <current boss>` or `--undo`. Exit 0 done; 1 the caller is refused (audited) or a write
    failed or a notice failed (the override stands, the output says which); 2 refused for a reason that is not the caller (nothing written)."""
    engine = sb.engine
    subject = "agent:%s" % worker
    audit = lambda event, **detail: engine.audit.append(engine.actor, event, subject, dict(detail, caller=caller or OPERATOR, worker=worker))
    refusal = check_caller(caller, role)
    if refusal:
        audit("reassign.refused", why="caller", role=role, to=to, undo=undo)
        err("%s: reassign: %s; nothing was changed" % (sb.PROG, refusal))
        return sb.EXIT_ERROR

    def refuse(why):
        audit("reassign.refused", why=sb._one_line(why, 160), to=to, consent=consent, undo=undo)
        err("%s: reassign: %s; nothing was changed" % (sb.PROG, why))
        return sb.EXIT_USAGE

    if not isinstance(worker, str) or not _NAME.match(worker):
        return refuse("name the worker (a fleet agent name)")
    discovery = engine.discover(engine.now())
    seen = discovery.agents.get(worker)
    if seen is None:
        return refuse("%s is not a fleet agent the switchboard can see" % worker)
    if (seen.fleet or {}).get("role") in ("orchestrator", "chief-of-staff"):
        return refuse("%s is %s, not a worker: only a worker is reassigned" % (worker, (seen.fleet or {}).get("role")))
    state = engine.state_dir
    if state is None:
        return refuse("there is no state directory to keep the override in")
    current = sb.boss_name(discovery, seen)
    if undo:
        return undo_override(sb, discovery, seen, worker, current, caller, state, audit, refuse, out, err)
    if not isinstance(to, str) or not isinstance(consent, str) or not to or not consent:
        return refuse("a reassign needs --to <orchestrator> and --consent-from <the worker's current boss>")
    if consent != current:
        return refuse("consent must come from the worker's CURRENT boss, %s; %s was named" % (current or "nobody (the worker reports to nobody)", consent))
    if not live_orchestrator(discovery, current):
        return refuse("the current boss %s is not a live orchestrator: its consent cannot be taken" % current)
    if not live_orchestrator(discovery, to):
        return refuse("%s is not a live orchestrator: a worker is only handed to one" % to)
    if to == current:
        return refuse("%s already reports to %s" % (worker, to))
    now = engine.now()
    try:
        with locked(state):
            overrides, problem = read_overrides(state)
            if problem:
                return refuse("the override record cannot be used (%s)" % problem)
            retired = sb.read_retired(state)
            prune(overrides, retired, now)
            original = (overrides.get(worker) or {}).get("original") or (seen.fleet or {}).get("reports_to") or ""
            overrides[worker] = {"boss": to, "was": current, "original": original, "session": seen.session_id, "by": caller or OPERATOR, "at": float(now)}
            with engine._external("reassign", subject, {"caller": caller or OPERATOR, "worker": worker, "from": current, "to": to, "consent": consent}):
                write_overrides(state, overrides)
    except (OSError, ValueError) as problem:
        err("%s: reassign failed: the override could not be written (%s); nothing was changed" % (sb.PROG, sb._one_line(str(problem), 160)))
        return sb.EXIT_ERROR
    out("%s now reports to %s (was %s; consent from %s, ordered by %s)" % (worker, to, current, consent, caller or OPERATOR))
    return notify(sb, caller, worker, current, to, out, err,
                  "%s was reassigned from %s to %s by %s, with %s's consent. Finish the work under your new boss and report to %s." % (worker, current, to, caller or OPERATOR, current, to))


def undo_override(sb, discovery, seen, worker, current, caller, state, audit, refuse, out, err):
    engine = sb.engine
    try:
        with locked(state):
            overrides, problem = read_overrides(state)
            if problem:
                return refuse("the override record cannot be used (%s)" % problem)
            if worker not in overrides:
                return refuse("%s has no override to undo" % worker)
            held = overrides.pop(worker)
            with engine._external("reassign.undo", "agent:%s" % worker, {"caller": caller or OPERATOR, "worker": worker, "was": held["boss"], "restored": held["original"]}):
                write_overrides(state, overrides)
    except (OSError, ValueError) as problem:
        err("%s: reassign --undo failed: the record could not be written (%s); nothing was changed" % (sb.PROG, sb._one_line(str(problem), 160)))
        return sb.EXIT_ERROR
    restored = held["original"] or None
    out("%s's override is removed: it reports to its own boss again (%s)" % (worker, restored or "nobody"))
    return notify(sb, caller, worker, held["boss"], restored, out, err,
                  "%s's reassignment was undone by %s: it reports to %s again." % (worker, caller or OPERATOR, restored or "nobody"))


def notify(sb, caller, worker, old, new, out, err, text):
    """Tell the worker and both orchestrators. The override stands whatever happens here; a failed notice is said and exits 1."""
    failed = []
    for who in dict.fromkeys(n for n in (new, old, worker) if n):
        why = tell(sb, caller, who, text)
        if why:
            failed.append("%s (%s)" % (who, why))
        else:
            out("told %s" % who)
    if failed:
        err("%s: the change stands, but these were not told: %s" % (sb.PROG, "; ".join(failed)))
        return sb.EXIT_ERROR
    return sb.EXIT_OK
