"""agent_calls: GitHub calls made from agents' shells, counted per account and per hour (#367).

The `gh` shim (shims/gh) appends one line per call to a spool in the state directory: a time, an account name and an endpoint class.
Nothing else is recorded: no token, no argument value, no body, no repo name. The daemon folds the spool, and its own call count,
into hourly buckets once a pass (`fold`); `status_lines` shows this hour per account and the last six hours, before and after.

Bounded everywhere: the spool stops growing at SPOOL_LIMIT bytes (the shim's rule), at most FOLD_LINES lines are read per fold, and the
hourly file keeps HOURS_KEPT hours. A corrupt file starts a fresh count. A figure with no record behind it says `not recorded`.
Python 3.9+, standard library only; loaded by path from beside bin/fleet-switchboard.
"""

import json
import os
import re
import tempfile
import time

SPOOL = "agent-calls.jsonl"
HOURS_FILE = "agent-calls-hours.json"
SPOOL_LIMIT = 262144
FOLD_LINES = 20000
HOURS_KEPT = 48
ACCOUNT_LIMIT = 60
_SAFE = re.compile(r"[^A-Za-z0-9._@-]")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _account(value):
    return _SAFE.sub("?", str(value))[:ACCOUNT_LIMIT] or "default"


def _hour(ts):
    return int(ts // 3600) * 3600


def _read_json(path):
    try:
        with open(path) as handle:
            raw = json.loads(handle.read(2000001))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_json(path, body):
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".agent-calls-")
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(body, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _spool_lines(path):
    """[(ts, account)] from a spool file; a line that is not what the shim writes is skipped."""
    found = []
    try:
        with open(path) as handle:
            for number, line in enumerate(handle):
                if number >= FOLD_LINES:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and _number(row.get("ts")) and isinstance(row.get("account"), str):
                    found.append((float(row["ts"]), _account(row["account"])))
    except OSError:
        pass
    return found


def load_hours(state_dir):
    raw = _read_json(os.path.join(state_dir, HOURS_FILE)) if state_dir else None
    hours = raw.get("hours") if raw else None
    since = raw.get("since") if raw else None
    clean = {}
    if isinstance(hours, dict):
        for key, row in hours.items():
            if key.isdigit() and isinstance(row, dict):
                clean[key] = {side: {_account(a): int(n) for a, n in (row.get(side) or {}).items() if _number(n) and n >= 0}
                              for side in ("agent", "daemon") if isinstance(row.get(side), dict)}
    return clean, float(since) if _number(since) else None


def fold(state_dir, now, daemon_delta=None):
    """Fold the shim's spool and the daemon's new calls ({account: n}) into the hourly buckets. Returns True when something was written."""
    if not state_dir:
        return False
    hours_path = os.path.join(state_dir, HOURS_FILE)
    if not os.path.exists(hours_path):
        _write_json(hours_path, {"v": 1, "since": now, "hours": {}})   # the daemon switches counting on: the shim writes nothing until this file exists
    spool, folding = os.path.join(state_dir, SPOOL), os.path.join(state_dir, SPOOL + ".fold")
    try:
        if os.path.exists(spool):
            if not os.path.exists(folding):
                os.replace(spool, folding)   # the shim's next append makes a fresh spool: no line is lost between the read and the delete
    except OSError:
        pass
    lines = _spool_lines(folding) if os.path.exists(folding) else []
    delta = {_account(a): int(n) for a, n in (daemon_delta or {}).items() if _number(n) and n > 0}
    if not lines and not delta:
        return False
    hours, since = load_hours(state_dir)
    for ts, account in lines:
        row = hours.setdefault(str(_hour(min(ts, now))), {}).setdefault("agent", {})
        row[account] = row.get(account, 0) + 1
    for account, n in delta.items():
        row = hours.setdefault(str(_hour(now)), {}).setdefault("daemon", {})
        row[account] = row.get(account, 0) + n
    first = min(([since] if since is not None else []) + [ts for ts, _ in lines] + [now])
    for old in sorted(hours, key=int)[:-HOURS_KEPT]:
        del hours[old]
    written = _write_json(os.path.join(state_dir, HOURS_FILE), {"v": 1, "since": first, "hours": hours})
    if written:
        try:
            os.unlink(folding)
        except OSError:
            pass
    return written


def status_lines(state_dir, now):
    """The `status` lines: agent-shell calls this hour per account, then the last 6 hours (agent shell and daemon). Without any record: one line."""
    hours, since = load_hours(state_dir)
    here = {}
    for ts, account in _spool_lines(os.path.join(state_dir, SPOOL)) + _spool_lines(os.path.join(state_dir, SPOOL + ".fold")):   # not yet folded
        if _hour(ts) == _hour(now):
            here[account] = here.get(account, 0) + 1
    if since is None and not here:
        return ["      github     agent-shell calls: not recorded"]
    if since is None:
        since = now
    current = str(_hour(now))
    for account, n in (hours.get(current, {}).get("agent") or {}).items():
        here[account] = here.get(account, 0) + n
    lines = ["      github     agent shell calls this hour: " + (", ".join("%s %d" % kv for kv in sorted(here.items())) or "none")]
    for back in range(5, -1, -1):
        start = _hour(now) - back * 3600
        label = time.strftime("%H:00", time.gmtime(start))
        if start < _hour(since):
            lines.append("      github     hour %sZ: not recorded" % label)
            continue
        row = hours.get(str(start), {})
        agent, daemon = sum((row.get("agent") or {}).values()), sum((row.get("daemon") or {}).values())
        if start == _hour(now):
            agent += sum(n for a, n in here.items()) - sum((row.get("agent") or {}).values())   # this hour's spool is counted too
        lines.append("      github     hour %sZ: agent shell %d, daemon %d" % (label, agent, daemon))
    return lines
