"""The shape-M count of a lane and the hard-deadline test of a worker record (ask #2259).

A part of bin/cloud_worker.py (the one that loads it and re-exports every name defined here); it holds the code that file needs so that it stays within
its size row. The text of `_ended` is the text that stood in bin/cloud_worker.py; `lane_m_workers` gains one condition: a record whose pod the launcher
already stopped (an ok `stop` line for its runtime id after its create line in the launcher's audit) is not counted. A name which stayed behind there
is read as `D["name"]`, and `D` reads that file's own globals at each call.
"""
import datetime
import json
import os
import time

D: object   # the names that stayed in bin/cloud_worker.py, set by its loader before this file runs

AUDIT_WINDOW_BYTES = 262144   # the same tail window the launcher reads (cloud/launcher/cloud_worker.py last_audit_line)


def _epoch(text):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _started(record):
    """The record's own start (epoch): hard_deadline minus hard_seconds, else idle_deadline minus idle_seconds; None when neither can be computed."""
    for deadline, seconds in (("hard_deadline", "hard_seconds"), ("idle_deadline", "idle_seconds")):
        end, length = _epoch(record.get(deadline)), record.get(seconds)
        if end is not None and isinstance(length, (int, float)) and not isinstance(length, bool):
            return end - length
    return None


def _stopped_ids():
    """The runtime ids (each with the epoch of its newest ok `stop` line) whose stop follows their newest ok `create` line, read from the tail of the launcher's audit file.
    A missing or unreadable audit gives an empty map: every worker then counts."""
    try:
        with open(D["_lib"]().audit_path(os.environ), "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - AUDIT_WINDOW_BYTES))
            lines = handle.read().decode(errors="replace").splitlines()
    except Exception:
        return {}
    state = {}
    for raw in lines:
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        rid = line.get("runtime_id") if isinstance(line, dict) else None
        if not isinstance(rid, str) or not rid or line.get("ok") is not True:
            continue
        if line.get("action") == "create":
            state[rid] = None
        elif line.get("action") in ("stop", "delete") and rid in state:
            when = _epoch(line.get("ts"))
            state[rid] = when if when is not None else state[rid]
    return {rid: when for rid, when in state.items() if when is not None}


def lane_m_workers(state, lane):
    """The sorted names of the lane's live shape-M workers, from the record directory alone (no token, no cluster call)."""
    found, stopped = [], None
    for record in D["list_records"](state):
        if not isinstance(record, dict) or "problem" in record or record.get("shape") != "M" or _ended(record):
            continue
        if (record.get("reports_to") or "(no boss)") == lane:
            stopped = _stopped_ids() if stopped is None else stopped
            began, when = _started(record), stopped.get(record.get("runtime_id"))
            if when is None or began is None or when <= began:   # a stop counts only when later than the record's own start
                found.append(str(record.get("worker")))
    return sorted(found)


def _ended(record, clock=None):
    """True when the worker's hard deadline has passed: its pod is gone by design, so there is nothing to probe (and no token to mint for it)."""
    text = record.get("hard_deadline")
    try:
        when = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return False
    return when.timestamp() < (time.time() if clock is None else clock)
