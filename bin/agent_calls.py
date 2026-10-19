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
LANE_LIMIT = 64
LANES_PER_HOUR = 40       # distinct lanes kept in one hour; more fold into "other"
ENDPOINTS_PER_CELL = 30   # distinct endpoint classes kept per lane and account in one hour; more fold into "other"
DAEMON_LANE = "daemon"
UNKNOWN_LANE = "unknown"  # a spool line from before lanes were recorded, or whose lane cannot be read
USAGE_FILE = "github-usage.json"
_SAFE = re.compile(r"[^A-Za-z0-9._@-]")
_LANE_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_ENDPOINT_SAFE = re.compile(r"[^A-Za-z0-9 #/._-]")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _account(value):
    return _SAFE.sub("?", str(value))[:ACCOUNT_LIMIT] or "default"


def _lane(value):
    return (_LANE_SAFE.sub("?", value)[:LANE_LIMIT] or UNKNOWN_LANE) if isinstance(value, str) else UNKNOWN_LANE


def _endpoint(value):
    return (_ENDPOINT_SAFE.sub("?", value)[:60] or "other") if isinstance(value, str) else "other"


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
    """[(ts, account, lane, endpoint)] from a spool file; a line that is not what the shim writes is skipped. A line with no lane (an older shim) is lane `unknown`."""
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
                    found.append((float(row["ts"]), _account(row["account"]), _lane(row.get("lane")), _endpoint(row.get("endpoint"))))
    except OSError:
        pass
    return found


def _clean_lanes(raw):
    """{lane: {account: {endpoint: n}}} from a record, whatever its shape; what is not a count is dropped."""
    clean = {}
    if not isinstance(raw, dict):
        return clean
    for lane, accounts in raw.items():
        if not isinstance(accounts, dict):
            continue
        for account, endpoints in accounts.items():
            if not isinstance(endpoints, dict):
                continue
            for endpoint, n in endpoints.items():
                if _number(n) and n > 0:
                    cell = clean.setdefault(_lane(lane), {}).setdefault(_account(account), {})
                    cell[_endpoint(endpoint)] = cell.get(_endpoint(endpoint), 0) + int(n)
    return clean


def _add_lane(lanes, lane, account, endpoint, n=1):
    """Bounded: past LANES_PER_HOUR lanes or ENDPOINTS_PER_CELL classes a count goes to `other`."""
    if lane not in lanes and len(lanes) >= LANES_PER_HOUR:
        lane = "other"
    cell = lanes.setdefault(lane, {}).setdefault(account, {})
    if endpoint not in cell and len(cell) >= ENDPOINTS_PER_CELL:
        endpoint = "other"
    cell[endpoint] = cell.get(endpoint, 0) + n


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
                lanes = _clean_lanes(row.get("lanes"))
                if lanes:
                    clean[key]["lanes"] = lanes
    return clean, float(since) if _number(since) else None


def fold(state_dir, now, daemon_delta=None):
    """Fold the shim's spool and the daemon's new calls into the hourly buckets. A daemon call count is {account: n} or {account: {endpoint: n}}
    (the second is recorded under the lane `daemon` by endpoint class). Returns True when something was written."""
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
    delta, detail = {}, {}
    for a, n in (daemon_delta or {}).items():
        if isinstance(n, dict):
            parts = {_endpoint(e): int(c) for e, c in n.items() if _number(c) and c > 0}
            if parts:
                detail[_account(a)] = parts
                delta[_account(a)] = sum(parts.values())
        elif _number(n) and n > 0:
            delta[_account(a)] = int(n)
    if not lines and not delta:
        return False
    hours, since = load_hours(state_dir)
    for ts, account, lane, endpoint in lines:
        bucket = hours.setdefault(str(_hour(min(ts, now))), {})
        row = bucket.setdefault("agent", {})
        row[account] = row.get(account, 0) + 1
        _add_lane(bucket.setdefault("lanes", {}), lane, account, endpoint)
    for account, n in delta.items():
        bucket = hours.setdefault(str(_hour(now)), {})
        row = bucket.setdefault("daemon", {})
        row[account] = row.get(account, 0) + n
        for endpoint, c in (detail.get(account) or {"other": n}).items():
            _add_lane(bucket.setdefault("lanes", {}), DAEMON_LANE, account, endpoint, c)
    first = min(([since] if since is not None else []) + [line[0] for line in lines] + [now])
    for old in sorted(hours, key=int)[:-HOURS_KEPT]:
        del hours[old]
    written = _write_json(os.path.join(state_dir, HOURS_FILE), {"v": 1, "since": first, "hours": hours})
    if written:
        try:
            os.unlink(folding)
        except OSError:
            pass
    return written


def lane_lines(*sources):
    """The last hour's calls per lane (the daemon is lane `daemon`) and, under it, per account with the top endpoint classes. Several {lane: {account: {endpoint: n}}} are added."""
    total = {}
    for source in sources:
        for lane, accounts in source.items():
            for account, endpoints in accounts.items():
                for endpoint, n in endpoints.items():
                    _add_lane(total, lane, account, endpoint, n)
    if not total:
        return []
    per_lane = sorted(((lane, sum(sum(e.values()) for e in accounts.values())) for lane, accounts in total.items()), key=lambda kv: (-kv[1], kv[0]))
    lines = ["      github     this hour per lane: " + ", ".join("%s %d" % kv for kv in per_lane)]
    for lane, n in per_lane[:8]:
        parts = []
        for account, endpoints in sorted(total[lane].items(), key=lambda kv: -sum(kv[1].values())):
            top = ", ".join("%s %d" % kv for kv in sorted(endpoints.items(), key=lambda kv: (-kv[1], kv[0]))[:3])
            parts.append("%s %d (%s)" % (account, sum(endpoints.values()), top))
        lines.append("      github       lane %s: %s" % (lane, "; ".join(parts)))
    return lines


def status_lines(state_dir, now):
    """The `status` lines: agent-shell calls this hour per account, then the last 6 hours (agent shell and daemon). Without any record: one line."""
    hours, since = load_hours(state_dir)
    here = {}
    unfolded = {}   # lanes of the spool not yet folded
    for ts, account, lane, endpoint in _spool_lines(os.path.join(state_dir, SPOOL)) + _spool_lines(os.path.join(state_dir, SPOOL + ".fold")):   # not yet folded
        if _hour(ts) == _hour(now):
            here[account] = here.get(account, 0) + 1
            _add_lane(unfolded, lane, account, endpoint)
    if since is None and not here:
        return ["      github     agent-shell calls: not recorded"]
    if since is None:
        since = now
    current = str(_hour(now))
    for account, n in (hours.get(current, {}).get("agent") or {}).items():
        here[account] = here.get(account, 0) + n
    lines = ["      github     agent shell calls this hour: " + (", ".join("%s %d" % kv for kv in sorted(here.items())) or "none")]
    lines += lane_lines(hours.get(current, {}).get("lanes") or {}, unfolded)
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


# ---- the account's real usage (clause 2): `gh api rate_limit` does not count against the budget; the daemon reads it once a minute per account.

USAGE_EVERY = 60
USAGE_ACCOUNTS = 8


def parse_usage(text):
    """(used, remaining, limit, reset) from `gh api rate_limit` output (the `core` bucket), or None when it is not that shape."""
    try:
        core = json.loads(text)["resources"]["core"]
        figures = [core[k] for k in ("used", "remaining", "limit", "reset")]
    except (ValueError, KeyError, TypeError):
        return None
    if not all(_number(v) and v >= 0 for v in figures):
        return None
    return tuple(int(v) for v in figures)


def record_usage(state_dir, now, readings):
    """Keep {account: (used, remaining, limit, reset)} where `status` reads it. Never raises; returns True when written."""
    if not state_dir or not readings:
        return False
    body = {"v": 1, "at": now, "accounts": {_account(a): {"used": u, "remaining": r, "limit": l, "reset": t}
                                           for a, (u, r, l, t) in list(readings.items())[:USAGE_ACCOUNTS]}}
    return _write_json(os.path.join(state_dir, USAGE_FILE), body)


def recorded_in_window(hours, account, window_start, now):
    """Calls recorded (agent shells plus the daemon) for `account` since `window_start`: an hour bucket counts in proportion to how much of it lies in the window."""
    total = 0.0
    for key, row in hours.items():
        start = int(key)
        end = min(start + 3600, now)
        if end <= start or end <= window_start:
            continue
        share = (end - max(start, window_start)) / float(end - start)
        total += share * ((row.get("agent") or {}).get(account, 0) + (row.get("daemon") or {}).get(account, 0))
    return int(round(total))


def usage_lines(state_dir, now):
    """The `status` lines for real usage: per account used, remaining and reset, the calls recorded in the same window and the difference
    (spent outside the shim and the daemon). A reading older than 5 minutes says so; none says `not recorded`."""
    body = _read_json(os.path.join(state_dir, USAGE_FILE)) if state_dir else None
    accounts = body.get("accounts") if body else None
    if not isinstance(accounts, dict) or not accounts or not _number(body.get("at")):
        return ["      github     real usage: not recorded"]
    hours, _ = load_hours(state_dir)
    unfolded = {}
    for ts, account, _lane_name, _endpoint_name in _spool_lines(os.path.join(state_dir, SPOOL)) + _spool_lines(os.path.join(state_dir, SPOOL + ".fold")):
        unfolded.setdefault(_hour(ts), {}).setdefault("agent", {})
        unfolded[_hour(ts)]["agent"][account] = unfolded[_hour(ts)]["agent"].get(account, 0) + 1
    merged = {k: {"agent": dict((v.get("agent") or {})), "daemon": v.get("daemon") or {}} for k, v in hours.items()}
    for start, row in unfolded.items():
        cell = merged.setdefault(str(start), {"agent": {}, "daemon": {}})["agent"]
        for account, n in row["agent"].items():
            cell[account] = cell.get(account, 0) + n
    age = now - body["at"]
    lines = []
    for account, row in sorted(accounts.items()):
        if not all(_number(row.get(k)) for k in ("used", "remaining", "reset")):
            continue
        used, reset = int(row["used"]), float(row["reset"])
        recorded = recorded_in_window(merged, _account(account), reset - 3600, now)
        lines.append("      github     real usage %s: used %d, remaining %d, resets %sZ; recorded %d, outside the shim and the daemon about %d%s"
                     % (_account(account), used, int(row["remaining"]), time.strftime("%H:%M", time.gmtime(reset)), recorded, max(0, used - recorded),
                        "" if age <= 300 else " (read %d min ago)" % (age // 60)))
    return lines or ["      github     real usage: not recorded"]
