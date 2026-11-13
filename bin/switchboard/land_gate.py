# ---------------------------------------------------------------- `land-gate` (ask #2517)
#
# Cause: two landings of one repository collide at one place, the push of its default branch, and nothing owned that place; the order was set by messages a pod may not obey.
# One gate token per repository on the land list (`land_repos`), kept in the state file `land-gate.json` under one lock. `take` joins the queue (first come, first served by the time of
# the request) and says HELD or QUEUED; the holder keeps the token by `touch`; a holder that has not touched for `land_gate.expire_minutes` (default 10), or whose pod is gone,
# loses it at once and the next in the queue gets it; `release` hands it on; `move` (the Chief of Staff only) puts a queued name at a place. A waiter that stopped asking
# (no `take` for the same time) or whose pod is gone leaves the queue, so a dead waiter never blocks it. The pure rules are the `lg_*` functions (a clock and a liveness reader
# are handed in); the enforcement (the land wrapper and the pod's pre-push hook) reads this state in a later slice.
import copy
import json
import re
import sys

LAND_GATE_FILE = "land-gate.json"
LAND_GATE_KEY = "land_gate"
LAND_GATE_EXPIRE_DEFAULT = 10   # minutes without a touch
LAND_GATE_EXPIRE_MAX = 240
LAND_GATE_QUEUE_MAX = 50
LAND_GATE_REPOS_MAX = 100
_LG_NAME = re.compile(r"[A-Za-z0-9._-]{1,60}")


def validate_land_gate(config, source):
    """`land_gate`: null or absent is the default; else an object whose only key is `expire_minutes`, a whole number from 1 to 240."""
    raw = config.get(LAND_GATE_KEY)
    if raw is None:
        config.pop(LAND_GATE_KEY, None)
        return
    minutes = raw.get("expire_minutes", LAND_GATE_EXPIRE_DEFAULT) if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or set(raw) - {"expire_minutes"} or not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= LAND_GATE_EXPIRE_MAX:
        raise ConfigError("%s: 'land_gate' must be an object with only 'expire_minutes', a whole number from 1 to %d, got %r" % (source, LAND_GATE_EXPIRE_MAX, raw))


def lg_expire_seconds(config):
    raw = (config or {}).get(LAND_GATE_KEY) if isinstance(config, dict) else None
    minutes = raw.get("expire_minutes") if isinstance(raw, dict) else None
    return 60 * (minutes if isinstance(minutes, int) and not isinstance(minutes, bool) and 1 <= minutes <= LAND_GATE_EXPIRE_MAX else LAND_GATE_EXPIRE_DEFAULT)


def lg_clean(text):
    """A name fit for one line: letters, digits, dot, dash, underscore; anything else is a question mark; at most 60 characters."""
    return re.sub(r"[^A-Za-z0-9._-]", "?", str(text))[:60] or "?"


def lg_entry(data, repo):
    """The record of one repository in the state `data` (made when absent): {"holder": {...} | None, "queue": [{"name", "at", "seen"}]}. A malformed record reads as empty."""
    repos = data.setdefault("repos", {})
    entry = repos.get(repo)
    if not isinstance(entry, dict):
        entry = repos[repo] = {}
    holder = entry.get("holder")
    ok = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)   # noqa: E731
    entry["holder"] = holder if isinstance(holder, dict) and isinstance(holder.get("name"), str) and ok(holder.get("since")) and ok(holder.get("touched")) else None
    queue = entry.get("queue")
    entry["queue"] = [q for q in (queue if isinstance(queue, list) else []) if isinstance(q, dict) and isinstance(q.get("name"), str) and ok(q.get("at")) and ok(q.get("seen"))][:LAND_GATE_QUEUE_MAX]
    return entry


def lg_settle(entry, now, expire, alive):
    """Drop a dead holder and dead waiters, then give the token to the head of the queue when nobody holds it. `alive(name)` is False for a pod that is gone. Returns [(event, name, why)]."""
    events = []
    holder = entry["holder"]
    if holder is not None:
        if now - holder["touched"] > expire:
            events.append(("expired", holder["name"], "no touch for %d minutes" % (expire // 60)))
            entry["holder"] = None
        elif not alive(holder["name"]):
            events.append(("expired", holder["name"], "its pod is gone"))
            entry["holder"] = None
    kept = []
    for waiter in entry["queue"]:
        if not alive(waiter["name"]):
            events.append(("dropped", waiter["name"], "its pod is gone"))
        elif now - waiter["seen"] > expire:
            events.append(("dropped", waiter["name"], "no request for %d minutes" % (expire // 60)))
        else:
            kept.append(waiter)
    entry["queue"] = kept
    if entry["holder"] is None and kept:
        head = kept.pop(0)
        entry["holder"] = {"name": head["name"], "since": now, "touched": now}
        events.append(("granted", head["name"], "first in the queue"))
    return events


def lg_take(entry, name, now):
    """The caller asks for the token: (state, place): "held" (place 0) or "queued" (place 1 = next). Joins the queue once, by the time of its first request; asking again only shows it is alive."""
    holder = entry["holder"]
    if holder is not None and holder["name"] == name:
        holder["touched"] = now
        return "held", 0
    for waiter in entry["queue"]:
        if waiter["name"] == name:
            waiter["seen"] = now
            break
    else:
        if len(entry["queue"]) >= LAND_GATE_QUEUE_MAX:
            return "full", 0
        entry["queue"].append({"name": name, "at": now, "seen": now})
    return "queued", [q["name"] for q in entry["queue"]].index(name) + 1


def lg_state_line(repo, entry, now):
    """One line: who holds the token and how long the queue is."""
    holder = entry["holder"]
    return "%s: %s; queue %d" % (repo, "held by %s for %ds, last touch %ds ago" % (lg_clean(holder["name"]), now - holder["since"], now - holder["touched"]) if holder else "free", len(entry["queue"]))


def lg_check(data, repo, name, now, expire, alive):
    """The question of the enforcement: (True, line) when `name` holds the live token of `repo`, else (False, the refusal: why, who holds it, how long the queue is). Reads a copy: nothing is changed."""
    entry = lg_entry(copy.deepcopy(data), repo)
    lg_settle(entry, int(now), expire, alive)
    holder = entry["holder"]
    if holder is not None and holder["name"] == name:
        return True, "%s holds the land token of %s" % (lg_clean(name), repo)
    return False, "refused: %s does not hold the land token of %s; %s; queue length %d. Run: fleet-switchboard land-gate take %s" % (
        lg_clean(name), repo, ("it is held by %s (last touch %ds ago)" % (lg_clean(holder["name"]), int(now) - holder["touched"])) if holder else "nobody holds it",
        len(entry["queue"]), repo)


def lg_alive_reader(state_dir):
    """alive(name): False only when the registry was read whole and neither a live row nor an endpoint row names the agent (its pod is gone). Any doubt reads as alive."""
    try:
        reg = load_module("registry").read(state_dir)
    except Exception:
        return lambda name: True
    if not reg.get("whole"):
        return lambda name: True
    agents = reg.get("agents") or {}
    return lambda name: name in agents


def _lg_caller(runner, now):
    """(name, is_chief_of_staff, why_not): the caller's fleet name (a person at a shell and an uncertain identity have none, and the reason is said)."""
    kind = gates_caller_kind(runner, now)
    if kind[0] != "agent":
        return None, False, str(kind[1])
    return lg_clean(kind[1]), len(kind) > 2 and kind[2] == "chief-of-staff", None


def cmd_land_gate(args, runner, now):
    """`land-gate take|touch|release|status|move`: the one land token of a repository on the land list. Exit 0 when the caller holds it (or the command did its work), 1 when queued or refused, 2 on a usage error."""
    config = load_config() if config_path().exists() else load_config(None)
    sub = args.gate_action
    expire = lg_expire_seconds(config)
    repo = getattr(args, "repo", None)
    if repo is not None:
        repo = repo.lower() if isinstance(repo, str) else repo
        if not isinstance(repo, str) or not uses_spire_land(config, repo):
            sys.stderr.write("%s: land-gate: %s is not on the land list (`land_repos`): it has no gate\n" % (PROG, lg_clean(repo)))
            return EXIT_USAGE
    name, chief, why = _lg_caller(runner, now) if sub != "status" else (None, False, None)
    if sub != "status" and name is None:
        sys.stderr.write("%s: land-gate %s: the caller is not a fleet agent (%s)\n" % (PROG, sub, _one_line(why, 160)))
        return EXIT_USAGE
    state = ensure_state_dir()
    audit = AuditLog(state / "audit.jsonl")
    alive = lg_alive_reader(state)
    stamp = int(now())
    out, code = [], EXIT_OK
    with agent_lock(state, "land-gate", seconds=5):
        data = {"repos": _read_small(state, LAND_GATE_FILE).get("repos", {})}
        before = json.dumps(data, sort_keys=True)
        repos = [repo] if repo is not None else sorted(r for r in data["repos"] if isinstance(r, str))
        for each in repos:
            entry = lg_entry(data, each)
            for event, who, reason in lg_settle(entry, stamp, expire, alive):
                audit.append("land-gate", "land_gate." + event, each, {"name": lg_clean(who), "why": reason})
            if sub == "take":
                got, place = lg_take(entry, name, stamp)
                for event, who, reason in lg_settle(entry, stamp, expire, alive):
                    audit.append("land-gate", "land_gate." + event, each, {"name": lg_clean(who), "why": reason})
                if entry["holder"] is not None and entry["holder"]["name"] == name:
                    got, place = "held", 0
                if got == "full":
                    out.append("refused: the queue of %s is full (%d)" % (each, LAND_GATE_QUEUE_MAX))
                    code = EXIT_ERROR
                elif got == "held":
                    out.append("HELD %s by %s: touch it at least every %d minutes (fleet-switchboard land-gate touch %s) and release it when done" % (each, name, expire // 60, each))
                    audit.append("land-gate", "land_gate.take", each, {"name": name, "result": "held"})
                else:
                    out.append("QUEUED %s at place %d behind %s: ask again with take (at least every %d minutes)" % (each, place, lg_clean(entry["holder"]["name"]) if entry["holder"] else "nobody", expire // 60))
                    code = EXIT_ERROR
            elif sub == "touch":
                if entry["holder"] is not None and entry["holder"]["name"] == name:
                    entry["holder"]["touched"] = stamp
                    out.append("TOUCHED %s" % each)
                else:
                    out.append("refused: %s does not hold the land token of %s (%s)" % (name, each, lg_state_line(each, entry, stamp)))
                    code = EXIT_ERROR
            elif sub == "release":
                target = lg_clean(args.name) if getattr(args, "name", None) else name
                if target != name and not chief:
                    out.append("refused: only the Chief of Staff releases another name's place")
                    code = EXIT_ERROR
                elif entry["holder"] is not None and entry["holder"]["name"] == target:
                    entry["holder"] = None
                    audit.append("land-gate", "land_gate.release", each, {"name": target, "by": name})
                    lg_settle(entry, stamp, expire, alive)
                    out.append("RELEASED %s; %s" % (each, lg_state_line(each, entry, stamp)))
                elif any(q["name"] == target for q in entry["queue"]):
                    entry["queue"] = [q for q in entry["queue"] if q["name"] != target]
                    audit.append("land-gate", "land_gate.release", each, {"name": target, "by": name, "queued": True})
                    out.append("LEFT the queue of %s" % each)
                else:
                    out.append("refused: %s holds nothing and is not queued for %s" % (target, each))
                    code = EXIT_ERROR
            elif sub == "move":
                who = lg_clean(args.name)
                if not chief:
                    audit.append("land-gate", "land_gate.move_refused", each, {"name": who, "by": name})
                    out.append("refused: only the Chief of Staff moves a place in the queue")
                    code = EXIT_ERROR
                elif not any(q["name"] == who for q in entry["queue"]):
                    out.append("refused: %s is not in the queue of %s" % (who, each))
                    code = EXIT_ERROR
                else:
                    moved = [q for q in entry["queue"] if q["name"] == who]
                    rest = [q for q in entry["queue"] if q["name"] != who]
                    place = min(max(args.place, 1), len(rest) + 1)
                    entry["queue"] = rest[:place - 1] + moved + rest[place - 1:]
                    audit.append("land-gate", "land_gate.move", each, {"name": who, "place": place, "by": name})
                    out.append("MOVED %s to place %d of %s" % (who, place, each))
            else:   # status
                out.append(lg_state_line(each, entry, stamp))
                if entry["holder"]:
                    out.append("  holder %s since %ds, last touch %ds ago (expires after %d minutes without one)" % (
                        lg_clean(entry["holder"]["name"]), stamp - entry["holder"]["since"], stamp - entry["holder"]["touched"], expire // 60))
                for place, waiter in enumerate(entry["queue"], 1):
                    out.append("  %d. %s asked %ds ago, last asked %ds ago" % (place, lg_clean(waiter["name"]), stamp - waiter["at"], stamp - waiter["seen"]))
        data["repos"] = {r: e for r, e in data["repos"].items() if isinstance(e, dict) and (e.get("holder") or e.get("queue"))}
        if json.dumps(data, sort_keys=True) != before:
            _write_small(state, LAND_GATE_FILE, data)
    for line in out or ["no land token is held or queued"]:
        print(line)
    return code


cmd_land_gate.direct = True   # state file only: no daemon start, no error mapping


def add_land_gate_parsers(sub):
    gate = sub.add_parser("land-gate", help="The one land token of a repository on the land list: take, touch, release, status, move (Chief of Staff only)",
                          description="One gate token per repository in `land_repos`, first come first served. `take <repo>` says HELD or QUEUED (ask again at least every land_gate.expire_minutes, default 10); "
                                      "the holder runs `touch <repo>` while it lands and `release <repo>` when done; a holder that stops touching or whose pod is gone loses the token at once. "
                                      "`status [repo]` shows holder, queue and times. `move <repo> <name> <place>` is the Chief of Staff's.")
    actions = gate.add_subparsers(dest="gate_action", required=True)
    for word in ("take", "touch"):
        actions.add_parser(word, help="%s the land token of the repository" % word.capitalize()).add_argument("repo", help="owner/repo on the land list")
    rel = actions.add_parser("release", help="Give the token up (or leave the queue); the Chief of Staff may name another")
    rel.add_argument("repo", help="owner/repo on the land list")
    rel.add_argument("name", nargs="?", help="Another name's place (the Chief of Staff only)")
    status = actions.add_parser("status", help="The holder, the queue and the times, one line each")
    status.add_argument("repo", nargs="?", help="owner/repo (default: every repository with a holder or a queue)")
    move = actions.add_parser("move", help="Put a queued name at a place (1 = next); the Chief of Staff only")
    move.add_argument("repo", help="owner/repo on the land list")
    move.add_argument("name", help="The queued name")
    move.add_argument("place", type=int, help="The place, 1 = next")
