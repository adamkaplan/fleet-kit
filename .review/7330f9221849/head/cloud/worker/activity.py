#!/usr/bin/env python3
"""cw-worker-activity: prints one JSON line {"busy": bool, "last_activity": "<ISO UTC>"} from the worker's OWN headless server.

busy = GET /api/session/active names a running session: the real v2 server answers {"data": {"<session id>": {"type":
"running"}}} (an object keyed by session id; busy = at least one entry whose type is "running" or missing); a list is accepted too
(busy = not empty). last_activity = the newest `time.updated` among the sessions of
GET /api/session (epoch milliseconds, as the switchboard's V2 client reads it; seconds are accepted too); with no session
yet, or no readable time, the pod start time (env CW_STARTED, ISO) or else this process's start. Inbound request traffic is
never read. An unreadable answer (HTTP error, no connection, bad JSON, unexpected shape, no password file) prints nothing on stdout, one short
reason line on stderr (never the password) and exits 1 (the reaper treats that as "not a reason to stop").
Env (tests only): CW_ACTIVITY_PORT (4096), CW_ACTIVITY_PASSWORD_FILE (/run/cw/server-password).
"""
import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request


class Unreadable(Exception):
    pass


def get(port, auth, path, kind):
    req = urllib.request.Request("http://127.0.0.1:%s%s" % (port, path), headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read().decode())
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, kind):
        raise Unreadable("unexpected shape")
    return data


def is_busy(active):
    if isinstance(active, list):
        return bool(active)
    return any(not isinstance(v, dict) or v.get("type") in (None, "running") for v in active.values())


def to_dt(ms):
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    return dt.datetime.fromtimestamp(ms / 1000 if ms > 1e11 else ms, dt.timezone.utc)


def fail(reason):
    print("activity: " + reason, file=sys.stderr)
    return 1


def main():
    port = os.environ.get("CW_ACTIVITY_PORT", "4096")
    try:
        with open(os.environ.get("CW_ACTIVITY_PASSWORD_FILE", "/run/cw/server-password")) as f:
            password = f.read().strip()
        auth = "Basic " + base64.b64encode(("opencode:" + password).encode()).decode()
        active = get(port, auth, "/api/session/active", (dict, list))
        sessions = get(port, auth, "/api/session", list)
    except urllib.error.HTTPError as e:
        return fail("HTTP %d" % e.code)
    except Unreadable as e:
        return fail(str(e))
    except FileNotFoundError:
        return fail("password file unreadable")
    except OSError:
        return fail("server unreachable")
    except ValueError:
        return fail("bad JSON")
    times = [to_dt((s.get("time") or {}).get("updated")) for s in sessions if isinstance(s, dict) and isinstance(s.get("time"), dict)]
    times = [t for t in times if t]
    if times:
        last = max(times)
    else:
        try:
            last = dt.datetime.fromisoformat(os.environ["CW_STARTED"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            last = dt.datetime.now(dt.timezone.utc)
    print(json.dumps({"busy": is_busy(active), "last_activity": last.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
