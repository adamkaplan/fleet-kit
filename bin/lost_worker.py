"""A lost worker is told to its lane and the Chief of Staff, once, with its cause (ask #1481, plan sha256:9c19b12f...).

One detector in the daemon's pass. It READS records the switchboard already keeps and sends messages; it changes no worker, pod, label or record
(its own small state file `lost-workers.json` apart), and a message that cannot be sent is audited once and never stops the pass.

Division with #1480: the cloud launcher's sender tells for every cloud worker the launcher ends (a stop line). This detector sends for what no stop
line announces. The kinds (`KINDS`):

- `pod_no_stop`: a cloud pod with a `cloud.created` line, its hard deadline passed, and NO stop line in the switchboard audit and none in the launcher
  audit (`cw-cloud/audit.jsonl`, action `stop`, ok), and no final report line (launcher action `report`). A stop of any reason, `done` included, is
  never alerted here (a clean end, or the launcher's own sender).
- `local_worker`: a local worker seen alive in an earlier pass that is gone from discovery and is not retired in the registry.
- `refused_launch`: a `cloud.create_failed` line of the switchboard audit.
- `reaper_missed`: a failed line of the reaper's own login refresh in the launcher audit (runtime id `reaper`, `ok` false): the reaper could not pass.

Each loss is alerted ONCE (`alerted` in `lost-workers.json`, kept across restarts), to the launching lane (the record's `reports_to`) and to the Chief of
Staff (one message when they are the same agent). The text names the worker, the ask, the kind, the last event time and the evidence line, and says
whether the ask was lost before (`repeat`, with the number of earlier lost workers of the same ask).

The audits are read from a stored byte position (at most READ_BYTES a pass; a file that shrank is read from its start), never in full each pass.
Pure text handling: audit lines are untrusted text, cut to one short line.
"""

import json
import os
import re

STATE_FILE = "lost-workers.json"
KINDS = ("pod_no_stop", "local_worker", "refused_launch", "reaper_missed")
READ_BYTES = 1 << 20          # most one audit file gives a pass
FIRST_BYTES = 4 << 20         # a first pass looks at most this far back
ALERTED_MAX = 2000
SEEN_MAX = 500
EVIDENCE_MAX = 200
REAPER_ID = "reaper"

NEEDS = ("parse_iso", "_one_line")


def launcher_audit_path(env=None):
    env = os.environ if env is None else env
    return env.get("CW_CLOUD_AUDIT") or os.path.join(env.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "cw-cloud", "audit.jsonl")


def _clean(text, limit=EVIDENCE_MAX):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", str(text)).strip()[:limit]


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save(path, state):
    tmp = "%s.tmp" % path
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True)
    os.replace(tmp, path)


def _read_new(path, state, key):
    """The new complete lines of an audit file since the stored position (bounded). Never raises; unreadable gives no lines."""
    offsets = state.setdefault("offsets", {})
    try:
        size = os.stat(path).st_size
        start = offsets.get(key)
        if not isinstance(start, int) or start > size or start < 0:
            start = max(0, size - FIRST_BYTES) if start is None else 0
        with open(path, "rb") as handle:
            handle.seek(start)
            raw = handle.read(READ_BYTES)
    except OSError:
        return []
    end = raw.rfind(b"\n")
    if end < 0:
        if len(raw) < READ_BYTES:
            return []
        end = len(raw) - 1   # one line longer than the bound: skipped whole
    offsets[key] = start + end + 1
    lines = []
    for chunk in raw[:end + 1].split(b"\n"):
        try:
            item = json.loads(chunk.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(item, dict):
            lines.append((item, _clean(chunk.decode("utf-8", "replace"))))
    return lines


def _fold(state, sb, lines, source):
    """Fold audit lines into the per-worker facts: created, stopped, reported, refused launches, reaper failures."""
    created, stop, report = state.setdefault("created", {}), state.setdefault("stop", {}), state.setdefault("report", {})
    for item, raw in lines:
        ts = sb.parse_iso(item.get("ts")) if isinstance(item.get("ts"), str) else None
        if ts is None:
            continue
        if source == "switchboard":
            name, event, detail = item.get("subject"), item.get("event"), item.get("detail") if isinstance(item.get("detail"), dict) else {}
            if not isinstance(name, str) or not name:
                continue
            if event == "cloud.created":
                created[name] = {"at": ts, "deadline": sb.parse_iso(detail.get("hard_deadline")) if isinstance(detail.get("hard_deadline"), str) else None,
                                 "line": raw}
            elif event == "cloud.stopped":
                stop[name] = max(ts, stop.get(name, 0))
            elif event == "cloud.create_failed":
                state.setdefault("refused", {})["%s@%s" % (name, item["ts"])] = {"name": name, "at": ts, "line": raw}
        else:
            name, action, ok = item.get("worker"), item.get("action"), item.get("ok")
            if item.get("runtime_id") == REAPER_ID and ok is False:
                state.setdefault("reaper", {})["%s@%s" % (action, item["ts"])] = {"at": ts, "line": raw}
            elif isinstance(name, str) and name and ok is True:
                if action == "stop":
                    stop[name] = max(ts, stop.get(name, 0))
                elif action == "report":
                    report[name] = max(ts, report.get(name, 0))
    for table in ("created", "stop", "report", "refused", "reaper"):
        data = state.get(table, {})
        while len(data) > SEEN_MAX:
            data.pop(min(data, key=lambda k: data[k]["at"] if isinstance(data[k], dict) else data[k]))


def _ask_of(name, record):
    for field in ("branch", "ask"):
        if isinstance(record.get(field), str) and record[field]:
            return _clean(record[field], 80)
    return re.sub(r"[-_.]?\d+$", "", name) or name


def _losses(state, sb, engine, discovery, now, records):
    out = []
    for name, c in sorted(state.get("created", {}).items()):
        deadline = c.get("deadline")
        if deadline is None or now < deadline:
            continue
        if state.get("stop", {}).get(name, 0) >= c["at"] or state.get("report", {}).get(name, 0) >= c["at"]:
            continue
        record = records.get(name, {})
        out.append({"key": "pod_no_stop:%s:%s" % (name, c["at"]), "kind": "pod_no_stop", "name": name, "ask": _ask_of(name, record),
                    "at": c["at"], "lane": record.get("reports_to"), "line": c["line"]})
    for key, r in sorted(state.get("refused", {}).items()):
        record = records.get(r["name"], {})
        out.append({"key": "refused_launch:" + key, "kind": "refused_launch", "name": r["name"], "ask": _ask_of(r["name"], record),
                    "at": r["at"], "lane": record.get("reports_to"), "line": r["line"]})
    for key, r in sorted(state.get("reaper", {}).items()):
        out.append({"key": "reaper_missed:" + key, "kind": "reaper_missed", "name": REAPER_ID, "ask": "-", "at": r["at"], "lane": None, "line": r["line"]})
    seen = state.setdefault("seen", {})
    present = set(discovery.agents) if discovery is not None else None
    if present is not None:
        for name, obs in discovery.agents.items():
            fleet = obs.fleet if isinstance(obs.fleet, dict) else {}
            boss = fleet.get("reports_to")
            if isinstance(boss, str) and boss and boss != name and obs.status != "unknown":
                state.get("gone", {}).pop(name, None)
                seen[name] = {"at": now, "lane": boss, "ask": _clean(fleet.get("issue") or fleet.get("repo") or name, 80)}
        for name, s in sorted(seen.items()):
            if name in present or name in state.get("gone", {}):
                continue
            if engine_retired(engine, sb, name):
                state.setdefault("gone", {})[name] = now
                continue
            out.append({"key": "local_worker:%s:%s" % (name, s["at"]), "kind": "local_worker", "name": name, "ask": s["ask"], "at": s["at"],
                        "lane": s["lane"], "line": "seen alive at %s, absent from discovery and not retired" % _iso(s["at"])})
            state.setdefault("gone", {})[name] = now
        while len(seen) > SEEN_MAX:
            seen.pop(min(seen, key=lambda k: seen[k]["at"]))
    return out


def engine_retired(engine, sb, name):
    check = getattr(sb, "retired", None)
    try:
        return bool(check(engine, name)) if check else False
    except Exception:
        return False


def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def text_of(loss, earlier):
    repeat = ("repeat: %d earlier lost worker(s) of this ask" % earlier) if earlier else "first loss of this ask"
    return "[lost worker] %s (ask %s) kind=%s, last event %s. Evidence: %s. %s." % (
        loss["name"], loss["ask"], loss["kind"], _iso(loss["at"]), _clean(loss["line"]), repeat)


def run_pass(sb, engine, now, discovery=None, launcher_audit=None):
    """One pass. Never raises into the caller. Returns the alerts raised this pass (a list of dicts)."""
    if engine.audit is None:
        return []
    state_dir = os.path.dirname(str(engine.audit.path))
    path = os.path.join(state_dir, STATE_FILE)
    state = _load(path)
    before = json.dumps(state, sort_keys=True)
    _fold(state, sb, _read_new(str(engine.audit.path), state, "switchboard"), "switchboard")
    _fold(state, sb, _read_new(launcher_audit or launcher_audit_path(), state, "launcher"), "launcher")
    records = {}
    for r in (getattr(engine, "cloud_watch", None) or {}).get("records", []):
        if isinstance(r, dict) and isinstance(r.get("worker"), str):
            records[r["worker"]] = r
    alerted = state.setdefault("alerted", {})
    raised = []
    for loss in _losses(state, sb, engine, discovery, now, records):
        if loss["key"] in alerted:
            continue
        earlier = sum(1 for a in alerted.values() if a.get("ask") == loss["ask"] and loss["ask"] != "-")
        alerted[loss["key"]] = {"ask": loss["ask"], "at": now}   # marked BEFORE the send: a send that fails is not tried again (once per loss)
        text = text_of(loss, earlier)
        for recipient in dict.fromkeys([r for r in (loss.get("lane"), "chief-of-staff") if isinstance(r, str) and r]):
            try:
                outcome = engine.send("switchboard", recipient, text)
                if not outcome.get("sent"):
                    raise RuntimeError("%s (%s)" % (outcome.get("action"), outcome.get("reason")))
            except Exception as err:
                engine.audit.append(engine.actor, "lost_worker.alert_not_sent", loss["name"],
                                    {"to": recipient, "kind": loss["kind"], "why": sb._one_line(str(err), 160)})
        raised.append(dict(loss, text=text, earlier=earlier))
    while len(alerted) > ALERTED_MAX:
        alerted.pop(min(alerted, key=lambda k: alerted[k]["at"]))
    if json.dumps(state, sort_keys=True) != before:
        try:
            _save(path, state)
        except OSError as err:
            engine.audit.append(engine.actor, "lost_worker.state_not_saved", None, {"why": sb._one_line(str(err), 160)})
    return raised
