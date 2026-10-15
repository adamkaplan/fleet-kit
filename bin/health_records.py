"""Health recorders (ask #357, slice 2). Pure of the switchboard: the caller hands over what it has ALREADY read (assistant messages, the
pr-state picture, the tool-call counter file the fleet-hooks plugin leaves) and a clock. Nothing here makes a network, GitHub or OpenCode
request, and nothing holds text: only agent/session names and numbers.

It publishes ONE derived file, `health-records.json` (written by the daemon through `state_write`: 0600, renamed into place, capped at FILE_CAP bytes), which the health reader merges:

    {"version": 1, "written_at": <s>,
     "tool_calls":  {"<session>": {"per_minute": [15 ints, oldest first], "approx": true}},           # absent key = not recorded
     "usage":       {"<session>": {"hours": {"<hour epoch>": {"tokens": n, "cost": x, "messages": n}},
                                   "context": {"tokens": n, "limit": n|absent, "percent": x|absent, "at": s}, "partial": true}},
     "pushes":      {"<owner/repo>": {"<hour epoch>": n}},
     "pushes_watched": ["<owner/repo>", ...],                       # repositories whose events the daemon receives: no push in the hour reads 0
     "sources":     {"tool_calls": "ok"|"stale"|"missing",            # missing = the fleet-hooks counter is not installed in the agents' profile
                     "usage": "on"|"off: v2_light"}}                  # v2_light reads no session history, so nothing feeds `usage`

A figure with no source is ABSENT (the reader says "not recorded"): never 0. A source that is missing or older than TOOL_STALE seconds
(the plugin is not loaded, the daemon's reads fail) gives no `tool_calls` at all. Spend and tokens are `partial`: the daemon's feed reads
the newest assistant message of a session, so a message it never read is not counted. The count is a floor, and is labelled so.
"""

import json
import os
import threading

VERSION = 1
FILE = "health-records.json"
TOOL_FILE = "tool-rate.json"        # written by plugins/fleet-hooks: {"written_at": s, "sessions": {session: {minute epoch: count}}}
TOOL_STALE = 180                    # seconds: older than this and the plugin is taken as not running
TOOL_WINDOW = 15                    # minutes of the sparkline
HOUR_KEEP = 48                      # hours of hourly buckets
MAX_AGENTS = 500
MAX_SEEN = 5000                     # message ids remembered for the dedupe
FILE_CAP = 256 * 1024
NAME_MAX = 80
READ_CAP = 1024 * 1024


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and value not in (float("inf"), float("-inf"))


def _name(value):
    """A name that is safe to keep: one line, bounded, printable."""
    text = "".join(c for c in str(value) if c.isprintable())[:NAME_MAX]
    return text or None


def _hour(now):
    return int(now // 3600) * 3600


def message_tokens(message):
    """Tokens of the message's prompt and answer (input + output + reasoning + cache read and write), or None."""
    tokens = message.get("tokens") if isinstance(message, dict) else None
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    parts = [tokens.get("input"), tokens.get("output"), tokens.get("reasoning"), cache.get("read"), cache.get("write")]
    if not any(_num(p) for p in parts):
        return None
    return sum(p for p in parts if _num(p))


def context_limit(message):
    """The model's context window where the message's own model info names it, else None (not recorded). Never guessed."""
    model = message.get("model") if isinstance(message, dict) else None
    if not isinstance(model, dict):
        return None
    for key in ("contextLimit", "context", "contextWindow"):
        if _num(model.get(key)) and model[key] > 0:
            return model[key]
    limit = model.get("limit")
    if isinstance(limit, dict) and _num(limit.get("context")) and limit["context"] > 0:
        return limit["context"]
    return None


def tool_series(body, now):
    """{session: [TOOL_WINDOW ints]} from the plugin's counter file body, or {} when it is missing, malformed or stale (not recorded)."""
    if not isinstance(body, dict) or not _num(body.get("written_at")) or now - body["written_at"] > TOOL_STALE:
        return {}
    sessions = body.get("sessions")
    if not isinstance(sessions, dict):
        return {}
    end = int(now // 60)
    out = {}
    for session, minutes in list(sessions.items())[:MAX_AGENTS]:
        name = _name(session)
        if name is None or not isinstance(minutes, dict):
            continue
        series = [0] * TOOL_WINDOW
        for key, count in minutes.items():
            try:
                age = end - int(key)
            except (TypeError, ValueError):
                continue
            if 0 <= age < TOOL_WINDOW and _num(count) and count >= 0:
                series[TOOL_WINDOW - 1 - age] = int(count)
        out[name] = series
    return out


def tool_state(body, now):
    """Why the tool-call series is what it is: "ok" (a fresh counter file), "stale" (the plugin left one, but it is old: not running) or
    "missing" (no readable file: the plugin that counts is not installed in the agents' profile)."""
    if not isinstance(body, dict):
        return "missing"
    return "ok" if _num(body.get("written_at")) and now - body["written_at"] <= TOOL_STALE else "stale"


def read_tool_file(state_dir):
    """The plugin's counter file as a body, or None. Bounded read; any fault is 'not recorded'."""
    try:
        with open(os.path.join(str(state_dir), TOOL_FILE), "rb") as handle:
            raw = handle.read(READ_CAP + 1)
        if len(raw) > READ_CAP:
            return None
        return json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None


class Recorder:
    """Memory of what the daemon has read: usage per session per hour, the newest context, and head changes of pull requests."""

    def __init__(self, clock):
        self.clock = clock
        self.seen = {}          # message id -> True (insertion order; oldest dropped)
        self.hours = {}         # session -> {hour: [tokens, cost, messages, messages with a cost]}
        self.context = {}       # session -> {"tokens", "limit", "at"}
        self.heads = {}         # "owner/repo#N" -> last head sha
        self.pushes = {}        # "owner/repo" -> {hour: n}
        self.dirty = False
        self.lock = threading.Lock()   # the feed's thread ingests messages while the pass thread builds the body

    def ingest_message(self, session, message):
        """One assistant message the daemon already read. True when it was new."""
        with self.lock:
            return self._ingest_message(session, message)

    def _ingest_message(self, session, message):
        name = _name(session)
        if name is None or not isinstance(message, dict) or message.get("type") not in (None, "assistant"):
            return False
        mid = message.get("id")
        if not isinstance(mid, str) or mid in self.seen:
            return False
        tokens = message_tokens(message)
        cost = message.get("cost") if _num(message.get("cost")) and message["cost"] >= 0 else None
        if tokens is None and cost is None:
            return False
        self.seen[mid] = True
        while len(self.seen) > MAX_SEEN:
            self.seen.pop(next(iter(self.seen)))
        now = self.clock()
        stamp = message.get("time", {}).get("created") if isinstance(message.get("time"), dict) else None
        at = (stamp / 1000.0 if stamp > 1e11 else stamp) if _num(stamp) else now   # v2 times are milliseconds
        if at > now + 3600 or at < now - HOUR_KEEP * 3600:
            at = now
        bucket = self.hours.setdefault(name, {}).setdefault(_hour(at), [0, 0.0, 0, 0])
        bucket[0] += tokens or 0
        bucket[1] += cost or 0.0
        bucket[2] += 1
        bucket[3] += 1 if cost is not None else 0
        old = self.context.get(name)
        if tokens is not None and (old is None or at >= old["at"]):
            self.context[name] = {"tokens": tokens, "limit": context_limit(message), "at": at}
        self.dirty = True
        return True

    def ingest_prs(self, prs):
        """The pr-state picture (`{"owner/repo#N": {"head": sha, ...}}`): a CHANGED head, never a first sighting, is one push."""
        with self.lock:
            self._ingest_prs(prs)

    def _ingest_prs(self, prs):
        if not isinstance(prs, dict):
            return
        now = self.clock()
        for ref, entry in list(prs.items())[:5000]:
            head = entry.get("head") if isinstance(entry, dict) else None
            if not isinstance(ref, str) or "#" not in ref or not isinstance(head, str) or not head:
                continue
            before = self.heads.get(ref)
            self.heads[ref] = head
            if before is not None and before != head:
                repo = _name(ref.rsplit("#", 1)[0])
                if repo:
                    hour = self.pushes.setdefault(repo, {})
                    hour[_hour(now)] = hour.get(_hour(now), 0) + 1
                    self.dirty = True
        if len(self.heads) > 5000:
            for ref in list(self.heads)[: len(self.heads) - 5000]:
                del self.heads[ref]

    def _last(self, session):
        hours = self.hours.get(session)
        return max(hours) if hours else (self.context.get(session) or {}).get("at", 0)

    def body(self, tool_body=None, light=False, watched=()):
        """The derived file's content. Bounded by hours kept, agents kept and FILE_CAP. `light`: v2_light is on, so no session history is read and
        `usage` cannot be fed. `watched`: the repositories whose events the daemon receives (a running forwarder): one with no push reads 0."""
        with self.lock:
            return self._body(tool_body, light, watched)

    def _body(self, tool_body=None, light=False, watched=()):
        now = self.clock()
        oldest = _hour(now) - (HOUR_KEEP - 1) * 3600
        for table in (self.hours, self.pushes):
            for key in list(table):
                table[key] = {h: v for h, v in table[key].items() if h >= oldest}
                if not table[key]:
                    del table[key]
        for table in (self.hours, self.context):
            for key in sorted(table, key=self._last)[: max(0, len(table) - MAX_AGENTS)]:
                del table[key]
        usage = {}
        for session, hours in self.hours.items():
            entry = {"hours": {str(h): dict({"tokens": v[0], "messages": v[2]}, **({"cost": round(v[1], 6)} if v[3] else {})) for h, v in sorted(hours.items())}, "partial": True}
            ctx = self.context.get(session)
            if ctx:
                entry["context"] = {"tokens": ctx["tokens"], "at": int(ctx["at"])}
                if ctx["limit"]:
                    entry["context"]["limit"] = ctx["limit"]
                    entry["context"]["percent"] = round(100.0 * ctx["tokens"] / ctx["limit"], 1)
            usage[session] = entry
        series = tool_series(tool_body, now)
        out = {"version": VERSION, "written_at": int(now),
               "tool_calls": {s: {"per_minute": v, "approx": True} for s, v in series.items()},
               "usage": usage,
               "sources": {"tool_calls": tool_state(tool_body, now), "usage": "off: v2_light" if light else "on"},
               "pushes_watched": sorted({n for n in (_name(r) for r in list(watched)[:MAX_AGENTS]) if n}),
               "pushes": {r: {str(h): n for h, n in sorted(v.items())} for r, v in self.pushes.items()}}
        while len(json.dumps(out, sort_keys=True)) > FILE_CAP and any(u["hours"] for u in usage.values()):   # the cap: oldest hours go first
            for u in usage.values():
                if u["hours"]:
                    u["hours"].pop(min(u["hours"], key=int))
        return out
