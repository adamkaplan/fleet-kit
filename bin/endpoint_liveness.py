"""endpoint_liveness (#1181): the liveness of a row that has no pane to judge. ON DEMAND only (the doctor's charters check, tests): the daemon pass never calls it. It needs a CLOUD RECORD (state/cloud/NAME.json); with none the answer is `unknown`. It then runs `fleet-switchboard read NAME --cloud-only --limit 1`, which reads the session THROUGH THAT RECORD (the row's endpoint value is not read here) and never falls to a local agent of the same name. The answer is `alive` with the time it answered, or `unknown`: NEVER `dead`. A failed, slow, refused or partial answer is
`unknown` and changes nothing. The request is the reused `fleet-switchboard read NAME` (tunnel + the pod's own Basic-auth password, kept by cloud_worker); no credential is read here or stored in the row.

    check(name, row, ...) -> {"verdict": "alive", "at": epoch} | {"verdict": "unknown", "why": text}
    check_all(rows, ...)  -> {name: answer}: one request per row, all at once (on demand; never per pass), never waiting longer than LIVENESS_SECONDS in all.
"""
import os
import signal
import subprocess
import sys
import threading
import time

LIVENESS_SECONDS = 5   # the one named time limit: a request, and the whole pass's wait for all of them
SWITCHBOARD = os.path.join(os.path.dirname(os.path.realpath(__file__)), "fleet-switchboard")   # FLEET_SWITCHBOARD_BIN names another (a test stub)


def read_newest(name, limit):
    """The reused call: `fleet-switchboard read NAME --limit 1` (cloud_worker.cmd_read: the pod's tunnel, the pod's own password file, message_list(limit=1,
    order=desc)). True when it exits 0 within `limit` seconds; anything else (no password, no tunnel, no kubectl, slow) is False. Its process group is killed
    at the limit. The credential is handled only inside that command: this module reads and stores none."""
    child = subprocess.Popen([sys.executable, os.environ.get("FLEET_SWITCHBOARD_BIN") or SWITCHBOARD, "read", name, "--cloud-only", "--limit", "1"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return child.wait(timeout=limit) == 0
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=1)
        except Exception:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except Exception:
                pass
        return False


def has_cloud_record(name):
    """True when state/cloud/NAME.json exists (where cloud_worker.route looks a cloud worker up). A local agent of that name is not one."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return bool(name) and "/" not in str(name) and os.path.exists(os.path.join(base, "fleet-switchboard", "cloud", str(name) + ".json"))


def unknown(why):
    return {"verdict": "unknown", "why": why}


def check(name, row, limit=LIVENESS_SECONDS, read=None, clock=time.time, record=None):
    """One request, only when a cloud record exists for NAME (`record(name)`, default has_cloud_record); else unknown. By the reused read (`read(name, limit)` -> True when the session answered). Never raises, never says dead."""
    try:
        if not isinstance(row, dict) or not row.get("endpoint"):
            return unknown("no endpoint")
        if not (record or has_cloud_record)(name):
            return unknown("no cloud record")
        if (read or read_newest)(name, limit):
            return {"verdict": "alive", "at": int(clock())}
        return unknown("the read did not answer")
    except Exception as err:   # failed, slow, partial, absent record: unknown
        return unknown(type(err).__name__)


def check_all(rows, limit=LIVENESS_SECONDS, read=None, clock=time.time, record=None):
    """{name: answer} for {name: row}: one request per row in its own thread, the pass waits at most `limit` seconds in all; a row not answered by then is unknown."""
    out = {name: unknown("no answer within %ss" % limit) for name in rows}
    lock = threading.Lock()

    def run(name, row):
        answer = check(name, row, limit, read, clock, record)
        with lock:
            out[name] = answer
    threads = [threading.Thread(target=run, args=(n, r), daemon=True) for n, r in sorted(rows.items())]
    for t in threads:
        t.start()
    end = time.monotonic() + limit
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    with lock:
        return dict(out)
