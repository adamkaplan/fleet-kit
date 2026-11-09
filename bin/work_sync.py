"""work_sync: one external work item per worker assignment, as information for an outside dashboard.

OFF by default (`external_decisions.work_items`) and a no-op unless the external adapter is on and has a work sink.
Read-only in the first version: nothing flows back, and a failing sink never blocks the pass, raises, or hides anything.

An ASSIGNMENT is a fleet agent that reports to a boss and holds an ask (an issue in a repo) in its launch metadata.
Its identity is (agent name, repo, issue): one work item per assignment, its id a name-based UUID of that key, so a
restart (or a lost state file) creates no duplicate: the put is idempotent. The same name working the same issue
again after it finished is the SAME assignment (no launch epoch is added): a finished item stays finished. An agent
reassigned to another issue is a new assignment, and its old one is cancelled when the agent no longer holds it.

Fields (generic words; the outside mapping names them): title, kind (the agent's role), state (queued, running,
awaiting, blocked), progress (one line from the latest report) and serves. `serves` carries the EXTERNAL ITEM ID of
the decision linked to that ask when the link table has one, else the issue ref as text `<repo>#<N>`. An assignment
ends as done, failed or cancelled, with the last line as its summary.

The rules are pure functions of plain data (`assignments`, `state_of`, `serves_of`). The WorkSync class holds the
state file (work-items.json) and the writes, through a small port, WorkSink, and reaches the Switchboard only
through its `host`. Python 3.9+, standard library only; loaded by path from beside bin/fleet-switchboard.
"""

import abc
import json
import os
import time
import uuid

import decision_core as core

STATE_FILE = "work-items.json"
STATUS_FILE = "work-status.json"
MIN_INTERVAL = 60                 # seconds between two writes of one item, whatever the sink reports
FINISHED_KEEP_SECONDS = 7 * 86400
STATES = ("queued", "running", "awaiting", "blocked")
ENDS = ("done", "failed", "cancelled")
_SPACE = uuid.UUID("2b7d9a64-5e1f-4c3a-8d20-7a9c1e4f6b35")


class WorkSink(abc.ABC):
    """Where work items are written: put, patch, finish. An implementation never needs to know the fleet. Writes are
    idempotent by item id. `interval` (seconds) is the least time the other side wants between two writes of one item.
    `fields` (optional) names the fields it takes; others are not passed."""

    interval = MIN_INTERVAL

    @abc.abstractmethod
    def put(self, work_id, title, **fields):
        """Create the item (the same id again changes nothing)."""

    @abc.abstractmethod
    def patch(self, work_id, **fields):
        """Change fields of the item. May answer {"sent": False, "deferred": True, "due_in": s} (not written yet)."""

    @abc.abstractmethod
    def finish(self, work_id, end, summary, **fields):
        """End the item: `end` is done, failed or cancelled. Final."""


class Host:
    """What the pump needs from the Switchboard; the script gives the real one, a test a fake."""

    def agents(self):
        """[{name, role, reports_to, repo, issue, status, intent}] of the fleet agents now."""
        return []

    def reports(self):
        """[{sender, issue, repo, state, text, at}] reports the bosses hold, any order."""
        return []

    def title_of(self, repo, issue):
        """The ask's title from what the daemon already holds (never a new read), or None."""
        return None

    def links(self):
        """The decision link table's links ({key: {external_id, repo, ask, ...}}) or {}."""
        return {}

    def audit(self, event, subject, detail):
        """One audit entry (no secrets)."""


# ---------------------------------------------------------------- pure rules


def assignment_key(agent, repo, issue):
    return "%s|%s|%d" % (agent, repo, issue)


def work_id_of(key):
    return str(uuid.uuid5(_SPACE, "work:%s" % key))


def serves_of(links, repo, issue):
    """What `serves` carries: the outside item id of the decision linked to the ask, else `<repo>#<N>`."""
    for link in (links or {}).values():
        if link.get("repo") == repo and link.get("ask") == issue and link.get("external_id") and not link.get("withdrawn"):
            return str(link["external_id"])
    return "%s#%d" % (repo, issue)


def state_of(status, mine):
    """(state, end, reason) of one assignment from the agent's live `status` and its reports on the ask (`mine`, each
    {state, text, at}). The latest report decides while it is final (done, failed, withdrawn: the end); otherwise the
    live status first (working is running, blocked is blocked), then the latest report (a question awaits, blocked
    blocks, paused awaits, working then idle awaits), and an agent with no report is queued."""
    latest = max(mine, key=lambda r: r["at"]) if mine else None
    reason = core.line_of(latest["text"], 200) if latest else ""
    if latest is not None and latest["state"] in ("done", "failed", "withdrawn"):
        return latest["state"] if latest["state"] != "withdrawn" else "cancelled", \
            (latest["state"] if latest["state"] != "withdrawn" else "cancelled"), reason
    if status == "working":
        return "running", None, reason
    if status == "blocked":
        return "blocked", None, reason
    if latest is None:
        return "queued", None, reason
    if latest["state"] == "blocked":
        return "blocked", None, reason
    return "awaiting", None, reason


def assignments(agents, reports, title_of, links):
    """{key: assignment} for the agents that hold an ask. A worker with no issue (or no boss, or no repo) is not one."""
    out = {}
    for agent in agents:
        issue, repo = agent.get("issue"), agent.get("repo")
        if not agent.get("reports_to") or not isinstance(issue, int) or isinstance(issue, bool) or issue < 1 \
                or not isinstance(repo, str) or not repo:
            continue
        mine = [r for r in reports if r.get("sender") == agent["name"] and r.get("issue") in (None, issue)
                and r.get("repo") in (None, repo)]
        state, end, reason = state_of(agent.get("status"), mine)
        key = assignment_key(agent["name"], repo, issue)
        intent = " ".join(str(agent.get("intent") or "").split())
        title = core.line_of(intent.split(". ")[0] if intent else (title_of(repo, issue) or ""), 160) \
            or "%s: %s#%d" % (agent["name"], repo, issue)
        out[key] = {"key": key, "work_id": work_id_of(key), "agent": agent["name"], "repo": repo, "issue": issue,
                    "kind": agent.get("role") or "worker", "state": state, "end": end, "reason": reason,
                    "title": title, "serves": serves_of(links, repo, issue)}
    return out


# ---------------------------------------------------------------- the pump


def _read_json(path):
    try:
        with open(path) as handle:
            body = json.load(handle)
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _write_json(path, body):
    temp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(body, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


class WorkSync:
    def __init__(self, settings, state_dir, sink, host, now=time.time):
        """`settings`: work_grace_seconds, work_outbox_alarm_depth, retry_base_seconds, retry_max_seconds."""
        self.settings, self.sink, self.host, self.now = settings, sink, host, now
        self.path = os.path.join(str(state_dir), STATE_FILE)
        self.status_path = os.path.join(str(state_dir), STATUS_FILE)
        body = _read_json(self.path)
        self.items = body["items"] if body and isinstance(body.get("items"), dict) else {}
        self.last_write = body.get("last_write") if body else None

    @property
    def interval(self):
        return max(MIN_INTERVAL, float(getattr(self.sink, "interval", MIN_INTERVAL) or MIN_INTERVAL))

    def cycle(self):
        """One pass. Never raises. Returns {open, outbox, errors, last_write, alarm}."""
        now = self.now()
        try:
            current = assignments(self.host.agents(), self.host.reports(), self.host.title_of, self.host.links())
            self._observe(current, now)
            self._write(now)
        except Exception as err:   # the pump's own fault: counted, never raised into the pass
            self.host.audit("work.error", "pump", {"error": core.one_line("%s: %s" % (type(err).__name__, err), 200)})
        try:
            self._save(now)
        except OSError:
            pass
        status = self._status()
        try:
            _write_json(self.status_path, dict(status, written_at=now))
        except OSError:
            pass
        return status

    def _observe(self, current, now):
        grace = self.settings["work_grace_seconds"]
        for key, a in current.items():
            item = self.items.get(key)
            if item is None:
                item = self.items[key] = {"work_id": a["work_id"], "agent": a["agent"], "put": False, "finished": None,
                                          "sent": {}, "last_write": None, "next_at": 0, "tries": 0, "error": None,
                                          "missing_since": None, "end": None}
            if item["finished"]:
                continue
            item["missing_since"] = None
            item["want"] = {"title": a["title"], "kind": a["kind"], "state": a["state"], "progress": a["reason"],
                            "serves": a["serves"]}
            item["end"] = [a["end"], a["reason"]] if a["end"] else None
        for key, item in self.items.items():
            if key in current or item["finished"]:
                continue
            if item["missing_since"] is None:
                item["missing_since"] = now
            elif now - item["missing_since"] >= grace and not item["end"]:
                item["end"] = ["cancelled", "the agent is gone"]

    def _fields(self, want):
        allowed = getattr(self.sink, "fields", None)
        return {k: v for k, v in want.items() if allowed is None or k in allowed}

    def _write(self, now):
        for key in sorted(self.items):
            item = self.items[key]
            if item["finished"] or item["next_at"] > now or "want" not in item:
                continue
            want = self._fields(item["want"])
            try:
                if not item["put"]:
                    fields = {k: v for k, v in want.items() if k != "title"}
                    self.sink.put(item["work_id"], want.get("title") or key, **fields)
                    item.update(put=True, sent=dict(want), last_write=now)
                    self.host.audit("work.put", key, {"work_id": item["work_id"], "state": item["want"]["state"]})
                    self.last_write = now
                if item["end"]:
                    end, summary = item["end"]
                    self.sink.finish(item["work_id"], end, summary or end)
                    item.update(finished=end, finished_at=now, last_write=now, error=None, tries=0)
                    self.host.audit("work.finish", key, {"work_id": item["work_id"], "end": end})
                    self.last_write = now
                    continue
                changed = {k: v for k, v in want.items() if item["sent"].get(k) != v}
                if changed and (item["last_write"] is None or now - item["last_write"] >= self.interval):
                    result = self.sink.patch(item["work_id"], **changed)
                    if isinstance(result, dict) and result.get("sent") is False and result.get("deferred"):
                        item["next_at"] = now + max(float(result.get("due_in") or 0), 1.0)
                        continue
                    item["sent"].update(changed)
                    item["last_write"] = now
                    self.last_write = now
                    self.host.audit("work.patch", key, {"work_id": item["work_id"], "fields": sorted(changed)})
                item.update(error=None, tries=0)
            except Exception as err:   # retried later; never blocks the others and never raises
                item["tries"] += 1
                item["error"] = core.one_line("%s: %s" % (type(err).__name__, err), 160)
                item["next_at"] = now + min(self.settings["retry_max_seconds"],
                                             self.settings["retry_base_seconds"] * 2 ** (item["tries"] - 1))
                self.host.audit("work.failed", key, {"tries": item["tries"], "error": item["error"]})

    def _pending(self, item):
        if item["finished"] or "want" not in item:
            return False
        if not item["put"] or item["end"]:
            return True
        want = self._fields(item["want"])
        return any(item["sent"].get(k) != v for k, v in want.items())

    def _status(self):
        open_items = [i for i in self.items.values() if not i["finished"]]
        outbox = sum(1 for i in open_items if self._pending(i))
        errors = sum(1 for i in open_items if i.get("error"))
        return {"open": len(open_items), "outbox": outbox, "errors": errors, "last_write": self.last_write,
                "alarm": outbox > self.settings["work_outbox_alarm_depth"],
                "error": next((i["error"] for i in open_items if i.get("error")), None)}

    def _save(self, now):
        self.items = {k: i for k, i in self.items.items()
                      if not (i["finished"] and now - i.get("finished_at", now) > FINISHED_KEEP_SECONDS)}
        _write_json(self.path, {"version": 1, "items": self.items, "last_write": self.last_write})
