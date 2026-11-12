"""The stall watch (ask #2399): the reaper's pass reads each running pod ONCE and tells the pod's boss and the Chief of Staff when it sits stalled.

Cause: a pod wrote that every shell command was refused and then sat idle for 75 minutes, because nothing read a pod's progress file, run logs or the
agent's last words while the pod was alive. This module only READS (one `kubectl exec` of a small program per pod per pass) and writes ONE audit line per alert (the daemon sends the messages); it never
stops, restarts, nudges or changes anything in a pod.

Three conditions (thresholds below), each alerted at most once per pod per 60 minutes once DELIVERED (its audit line written). The mark is a pod annotation (cw/stall-alert-<condition>) set BEFORE
the send, as the minute-40 note does; when the send reached nobody the mark is moved back so the next pass 10 or more minutes later tries again:
  progress-stale    the progress file and every run log unchanged for 15 minutes and no run active (an exit file absent and its log changed in the last 3 minutes)
  refusal           the agent's last message or the progress file's last two lines say Permission denied, refused, not allowed or Blocked (any case), or one error
                    line occurs 3 times in the progress file's last 10 lines
  failed-run-idle   a run's exit file holds a non-zero number and neither the progress file nor the agent session changed for 10 minutes after the exit file's time

A pod whose server reports the session idle (ask #2443; see `classify`) is not stalled for progress-stale and failed-run-idle: its boss got the idle notice (refusal still fires). The text lines `done` and `STATUS: done` decide nothing.
A pod younger than 10 minutes or a pod whose exec fails is skipped; the pass line names it (pod and reason, no pod content). The message holds the pod
name, the condition, one evidence line (secret-looking text and the pod's token replaced by [redacted], at most 300 characters; untrusted data, never a
command) and the time. Pod text is never run.
"""
import datetime as dt
import json
import math
import os
import re
import sys
import time

AGENT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
CHIEF = "cos"                      # the Chief of Staff's switchboard name when the config names none (`remind cos`)
CONFIG_KEY = "cos_name"            # the switchboard config's key (config.json) that names the Chief of Staff's agent, when it has one
STALL_ASK = "adamkaplan/fl" "eet-kit#2399"   # the stall watch's own ask: the switchboard's `send` needs --issue (every message names its ask)
IDLE_NOTICE_MIN_SECONDS = 120      # ask #2443: a pod's idle notices are at least this far apart (the key comes from the pod's own server: a looping pod cannot flood its boss)
RETRY_SECONDS = 10 * 60            # an alert that reached nobody is tried again at the next pass at least this long after
MIN_POD_AGE_SECONDS = 600          # a younger pod is not read
PASS_BUDGET_SECONDS = 90           # the pass reads no more pods once its reads have taken this long (the rest wait for the next pass)
EXEC_SECONDS = 3                   # the one exec of a pod is given at most this long
STALE_SECONDS = 15 * 60            # (a)
RUN_ACTIVE_SECONDS = 3 * 60        # (a) a run whose log changed within this is active
FAILED_IDLE_SECONDS = 10 * 60      # (c)
REPEAT_COUNT = 3                   # (b) the same error line this many times ...
REPEAT_WINDOW = 10                 # ... in this many last lines
REALERT_SECONDS = 60 * 60          # once per condition per pod per this long
EVIDENCE_MAX = 300
CONDITIONS = ("progress-stale", "refusal", "failed-run-idle")
IDLE_ACTION = "idle-notice"        # ask #2443: the audit action of one idle notice (the daemon sends it to the boss, bin/cloud_end_alert.py scan_idle)
ALERT_ACTION = "stall-alert"       # the audit action of one alert; the switchboard daemon reads it and sends the messages (bin/cloud_end_alert.py scan_stall)
REFUSAL_WORDS = re.compile(r"Permission denied: shell|permission\.rejected|^\s*(?:refused:|Blocked:|Blocked at)")
ERROR_LINE = re.compile(r"error|fail|exception|denied|refused|cannot|unable|not found", re.I)

# Run INSIDE the pod by `kubectl exec python3 -c <this>`. Reads only: the progress file (mtime, last 10 lines), /work/runs (per file name, mtime and the last
# line of a log, the number of an exit file) and the agent session's last non-user message (at most 400 characters) from the pod's own server. Prints ONE JSON line.
PROBE_CODE = r'''
import base64, json, os, re, sys, time, urllib.parse, urllib.request
def tail(p, n):
    try:
        with open(p, "rb") as f:
            f.seek(0, 2); f.seek(max(0, f.tell() - 8192))
            return [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()][-n:]
    except OSError:
        return None
out = {"now": time.time(), "results": "", "results_exists": False, "progress": None, "runs": [], "exits": [], "msg": "", "msg_time": None, "turn": {"turn": "unknown", "outcome": None, "finish": None},
       "idle": None, "active_read": False, "session_updated": None, "turn_start": None, "ahead": None, "updated": None, "activity": None, "running": None}
try:
    p = "/work/progress.txt"
    out["progress"] = {"mtime": os.path.getmtime(p), "tail": [l[:400] for l in (tail(p, 10) or [])]}
except OSError:
    pass
out["results"] = ((tail("/work/results.txt", 1) or [""])[0])[:200]   # text for humans: decides nothing
out["results_exists"] = os.path.isfile("/work/results.txt") and not os.path.islink("/work/results.txt")   # the deliverable as structure
try:
    out["results_mtime"] = os.path.getmtime("/work/results.txt")
except OSError:
    pass
rd = os.environ.get("CW_RUNS_DIR") or "/work/runs"
try:
    names = sorted(os.listdir(rd))[:200]
except OSError:
    names = []
for n in names:
    f = os.path.join(rd, n)
    try:
        if n.endswith(".log"):
            t = tail(f, 1) or [""]
            out["runs"].append({"name": n[:-4][:80], "mtime": os.path.getmtime(f), "last": t[0][:400]})
        elif n.endswith(".exit"):
            out["exits"].append({"name": n[:-5][:80], "code": open(f).read(20).strip(), "mtime": os.path.getmtime(f)})
    except (OSError, ValueError):
        pass
try:
    port = os.environ.get("CW_ACTIVITY_PORT", "4096")
    pw = open(os.environ.get("CW_ACTIVITY_PASSWORD_FILE", "/run/cw/server-password")).read().strip()
    auth = "Basic " + base64.b64encode(("opencode:" + pw).encode()).decode()
    def get(path):
        r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%s%s" % (port, path), headers={"Authorization": auth}), timeout=1)
        return json.loads(r.read().decode())
    ss = get("/api/session").get("data")
    ss = list(ss.values()) if isinstance(ss, dict) else ss
    ss = [s for s in ss if isinstance(s, dict) and s.get("id")]
    ss.sort(key=lambda s: (s.get("time") or {}).get("updated") or 0)
    if ss:
        u = (ss[-1].get("time") or {}).get("updated")
        if isinstance(u, (int, float)) and not isinstance(u, bool):
            out["msg_time"] = u / 1000.0 if u > 1e11 else u
            if out["msg_time"] > out["now"] + 60:
                out["msg_time"] = None
            out["updated"] = out["msg_time"]
        try:
            act = get("/api/session/active").get("data")
            out["running"] = any(not isinstance(v, dict) or v.get("type") in (None, "running") for v in act.values()) if isinstance(act, dict) else bool(act)
        except Exception:
            pass
        def sec(x):
            return (x / 1000.0 if x > 1e11 else x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None
        best, bt, q = "", -1, ""
        for _page in range(5):
            body = get("/api/session/%s/message%s" % (ss[-1]["id"], q)) if time.time() - out["now"] < 1.2 else {}
            items = body.get("data") if isinstance(body, dict) else body
            for m in items or []:
                info = m.get("info") or m
                if m.get("type") == "user" or info.get("role") == "user":
                    continue
                text = m.get("text") or info.get("text") or ""
                if not text and isinstance(m.get("content"), list):   # the v2 assistant message: its text blocks are in `content`
                    text = " ".join(str(x.get("text") or "") for x in m["content"] if isinstance(x, dict) and x.get("type") == "text")
                if not text and isinstance(m.get("parts"), list):
                    text = " ".join(str(x.get("text") or "") for x in m["parts"] if isinstance(x, dict))
                t = (info.get("time") or {}).get("created")
                t = t if isinstance(t, (int, float)) else 0
                for tm in [info.get("time") or {}] + [((x.get("state") or {}).get("time") or x.get("time") or {}) for x in (m.get("parts") if isinstance(m.get("parts"), list) else []) if isinstance(x, dict)]:
                    for k in ("created", "start", "end", "completed"):
                        v = sec(tm.get(k)) if isinstance(tm, dict) else None
                        if v is not None and v <= out["now"] + 60:
                            out["activity"] = max(out["activity"] or 0, v)
                if text and t >= bt:
                    best, bt = text, t
            cur = body.get("cursor") if isinstance(body, dict) else None
            nxt = cur.get("next") if isinstance(cur, dict) else None
            if not nxt:
                break
            q = "?cursor=" + urllib.parse.quote(str(nxt))
        out["msg"] = best[-400:]
        # ask #2443: the structural turn state, as cloud/worker/activity.py turn_state reads it (the same three answers; anything odd stays unknown)
        def newest(kind):
            it = get("/api/session/%s/message?limit=1&order=desc&type=%s" % (ss[-1]["id"], kind)).get("data")
            if not it:
                return None
            if not isinstance(it, list) or not isinstance(it[0], dict) or it[0].get("type") != kind or not isinstance((it[0].get("time") or {}).get("created"), (int, float)):
                raise ValueError("odd")
            return it[0]
        act = get("/api/session/active").get("data")
        if isinstance(act, (dict, list)) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", str(ss[-1]["id"])):
            if (act if isinstance(act, list) else [v for v in act.values() if not isinstance(v, dict) or v.get("type") in (None, "running")]):
                out["turn"] = {"turn": "running", "outcome": None, "finish": None}
            else:
                out["active_read"] = True   # the server says: not running (an unreadable turn below is then "idle state unknown", not silence)
                out["session_updated"] = ss[-1].get("time", {}).get("updated") if isinstance(ss[-1].get("time"), dict) else None
                idle, asst = newest("idle"), newest("assistant")
                fin = asst.get("finish") if asst else None
                if idle and idle.get("outcome") in ("succeeded", "failed", "interrupted") and (fin is None or fin in ("stop", "length", "tool-calls", "content-filter", "error", "unknown")) and not (asst and asst["time"]["created"] > idle["time"]["created"]):
                    out["turn"] = {"turn": "idle", "outcome": idle["outcome"], "finish": fin}
                    if isinstance(idle.get("id"), str) and re.fullmatch(r"msg_[A-Za-z0-9_]{1,80}", idle["id"]):
                        out["idle"] = {"id": idle["id"], "created": idle["time"]["created"]}
                    usr = newest("user")   # the turn began with the newest user message: a report made after it belongs to this turn
                    out["turn_start"] = usr["time"]["created"] if usr else None
except Exception:
    pass
try:   # commits ahead of the default branch, a local read (no network): evidence of a deliverable, never a gate
    import subprocess
    for d in (os.environ.get("CW_WORKDIR"), "/home/worker", "/work/repo"):
        if d and os.path.isdir(os.path.join(d, ".git")):
            r = subprocess.run(["git", "-C", d, "rev-list", "--count", "origin/HEAD..HEAD"], capture_output=True, text=True, timeout=1)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                out["ahead"] = int(r.stdout.strip())
                break
except Exception:
    pass
print(json.dumps(out))
'''

_SECRET = [
    re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}"),
    re.compile(r"\bxox[a-z]-[A-Za-z0-9-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+){0,2}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b[\w.-]*(?:password|passwd|secret|token|api[_-]?key|authorization|credential)[\w.-]*\s*[:=]\s*\S+"),
    re.compile(r"(?i)://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"[A-Za-z0-9+/_=-]{32,}"),
]


def redact(text, known=()):
    """Secret-looking text, every known secret (and any 8+ character piece of one) and every long token-shaped string become [redacted]; control characters go."""
    text = "".join(c if c.isprintable() else " " for c in str(text))
    for s in known:
        if isinstance(s, str) and len(s) >= 8:
            text = text.replace(s, "[redacted]")
    for rx in _SECRET:
        text = rx.sub("[redacted]", text)
    return text


def evidence(text, known=()):
    return " ".join(redact(text, known).split())[:EVIDENCE_MAX]


def _ann(pod, key, default=None):
    return ((pod.get("metadata") or {}).get("annotations") or {}).get("cw/" + key, default)


def _num(v):
    """A finite number or None (a pod's answer is untrusted: NaN and infinity read as no number)."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


TURN_VALUES = {"turn": ("running", "idle", "unknown"), "outcome": ("succeeded", "failed", "interrupted", None), "finish": ("stop", "length", "tool-calls", "content-filter", "error", "unknown", None)}


def classify(data):
    """The pod's state from what its server reported (ask #2443): "running" (the session is active), "idle" (not active, the last idle message and assistant finish are
    readable) or "unknown" (anything unreadable or off the schema's values: never idle by guess). Text lines decide nothing. An idle pod is told to its boss at once by
    the idle notice (`idle_notice`); the stall conditions below are only the backstop for a session that never goes idle (running, or unreadable)."""
    turn = data.get("turn")
    if not isinstance(turn, dict) or any(turn.get(k) not in TURN_VALUES[k] for k in TURN_VALUES):
        return "unknown"
    return turn["turn"] if turn["turn"] in ("running", "idle") else "unknown"


RUNNING_QUIET_SECONDS = 30 * 60    # a running turn with no new message or tool call for this long no longer silences a condition (a turn hung inside one tool call)


def session_state(data, now, threshold):
    """(working, reason): working is True when the session shows activity that silences a condition of `threshold` seconds (its last message or tool call, else its
    last update, is younger than the threshold; or a turn is running and that time is younger than RUNNING_QUIET_SECONDS), False when it is quiet, None with the
    reason when the server did not answer or the fields are missing (the condition then decides from the files)."""
    def ok(v):   # a time that is a finite number not in the future (a pod could write any value)
        v = _num(v)
        return v if v is not None and v == v and abs(v) != float("inf") and v <= now + 60 else None
    last = ok(data.get("activity"))
    if last is None:
        last = ok(data.get("updated"))
    running = data.get("running") if isinstance(data.get("running"), bool) else None
    if last is None:
        return None, ("the session's activity was not readable" if running is None and data.get("updated") is None else "the session's last activity time was missing")
    age = now - last
    if age < threshold:
        return True, ""
    if running and age < RUNNING_QUIET_SECONDS:
        return True, ""
    return False, ""


def _results_done(data):
    """Ask #2472: the run record's one rule (last non-empty line, and newer than the pod's start when both times are known)."""
    from run_record import results_verdict
    return results_verdict(str(data.get("results") or ""), _num(data.get("results_mtime")), _num(data.get("started")), nocase=True) == "done"


def decide(data):
    """{condition: evidence text} for the three conditions, from the probe's answer. Pure: times are the pod's own clock (`now` in the answer)."""
    now = _num(data.get("now"))
    if now is None:
        return {}
    found = {}
    prog = data.get("progress") if isinstance(data.get("progress"), dict) else None
    runs = [r for r in (data.get("runs") or []) if isinstance(r, dict) and _num(r.get("mtime")) is not None]
    exits = [e for e in (data.get("exits") or []) if isinstance(e, dict) and _num(e.get("mtime")) is not None]
    exited = {e.get("name") for e in exits}
    ptime = _num(prog.get("mtime")) if prog else None
    tail = [str(x) for x in (prog.get("tail") or [])] if prog else []
    stime = _num(data.get("msg_time"))
    if stime is not None and not (stime <= now + 60 and abs(stime) != float("inf")):   # a future or non-finite session time is read as missing (a pod could write any value)
        stime = None
    # (a)
    times = [t for t in [ptime] + [_num(r["mtime"]) for r in runs] if t is not None]
    active = any(r.get("name") not in exited and now - _num(r["mtime"]) < RUN_ACTIVE_SECONDS for r in runs)
    # Ask #2443: a pod whose server reports the session idle is not "stalled": its boss got the idle notice. (a) and (c) are the backstop for a session that never goes idle
    # (running, or an unreadable answer); no text line decides anything. (b) refusal still fires.
    finished = classify(data) == "idle"
    if not finished and times and not active and now - max(times) >= STALE_SECONDS:
        working, why = session_state(data, now, STALE_SECONDS)
        if not working:
            found["progress-stale"] = "no change for %d minutes%s; last progress line: %s" % ((now - max(times)) // 60, "" if working is False else " (decided from the files: %s)" % why, tail[-1] if tail else "(none)")
    # (b)
    msg = str(data.get("msg") or "")
    hit = next((l for l in msg.splitlines() + tail[-2:] if REFUSAL_WORDS.search(l)), None)
    if hit is None:
        last = [l.strip() for l in tail[-REPEAT_WINDOW:] if ERROR_LINE.search(l)]
        for l in last:
            if last.count(l) >= REPEAT_COUNT:
                hit = l
                break
    if hit is not None:
        found["refusal"] = hit
    # (c)
    bad = []
    for e in exits:
        try:
            if int(str(e.get("code")).strip()) != 0:
                bad.append(e)
        except ValueError:
            continue
    if bad and not finished:
        e = max(bad, key=lambda x: _num(x["mtime"]))
        moved = max([t for t in (_num(e["mtime"]), ptime, stime) if t is not None])
        working, why = session_state(data, now, FAILED_IDLE_SECONDS) if now - moved >= FAILED_IDLE_SECONDS else (True, "")
        if not working:
            log = next((r.get("last") for r in runs if r.get("name") == e.get("name")), "") or ""
            found["failed-run-idle"] = "run %s exited %s; no change for %d minutes%s; last log line: %s" % (str(e.get("name"))[:60], str(e.get("code"))[:6], (now - moved) // 60, "" if working is False else " (decided from the files: %s)" % why, log)
    return found


def idle_notice(data):
    """Ask #2443: the idle notice of a pod's probe answer, or None. A session the server reports idle (not active; readable last idle message) gives state "idle", keyed by the
    idle message's id (one notice per idle); a session the server reports not running whose turn cannot be read gives state "unknown" ("idle state unknown"), keyed by the
    session's update time; a running session, or an answer that does not even say whether it runs, gives None (never a guess). Evidence only: results file, commits ahead, jobs."""
    cls, idle = classify(data), data.get("idle")
    if cls == "idle" and isinstance(idle, dict) and isinstance(idle.get("id"), str) and re.fullmatch(r"msg_[A-Za-z0-9_]{1,80}", idle["id"]):
        state, key = "idle", idle["id"]
    elif cls != "running" and data.get("active_read") is True and _num(data.get("session_updated")) is not None:
        state, key = "unknown", "u%d" % int(_num(data["session_updated"]))
    else:
        return None
    now = _num(data.get("now"))
    runs = [r for r in (data.get("runs") or []) if isinstance(r, dict) and _num(r.get("mtime")) is not None]
    exited = {e.get("name") for e in (data.get("exits") or []) if isinstance(e, dict)}
    jobs = [str(r.get("name"))[:40] for r in runs if r.get("name") not in exited and now is not None and now - _num(r["mtime"]) < RUN_ACTIVE_SECONDS][:5]
    start, ahead = _num(data.get("turn_start")), data.get("ahead")
    turn = data["turn"] if state == "idle" else {"outcome": None, "finish": None}
    return {"key": key, "state": state, "outcome": turn.get("outcome"), "finish": turn.get("finish"), "jobs": jobs,
            "turn_start": int(start / 1000 if start > 1e11 else start) if start is not None else None,
            "results_exists": data.get("results_exists") if isinstance(data.get("results_exists"), bool) else None,
            "ahead": ahead if isinstance(ahead, int) and not isinstance(ahead, bool) and 0 <= ahead < 10**6 else None, "text": str(data.get("msg") or "")}


def cos_name(launcher):
    """The Chief of Staff's switchboard name: the switchboard config's `cos_name` (config.json beside cloud.env) when it holds a valid name, else CHIEF."""
    env = getattr(launcher, "env", None) or {}
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")
    try:
        with open(os.path.join(base, "fl" + "eet-switchboard", "config.json")) as f:
            name = json.load(f).get(CONFIG_KEY)
    except (OSError, ValueError, AttributeError):
        return CHIEF
    return name if isinstance(name, str) and AGENT_NAME.fullmatch(name) else CHIEF


def ask_of(launcher, worker):
    """The ask the pod's own launch named (`--issue`, kept in the switchboard's worker record: issue and repo), as owner/repo#N (N alone when the record has no repo);
    STALL_ASK when the record names none or is unreadable."""
    rd = (getattr(launcher, "env", None) or {}).get("CW_CLOUD_RECORD_DIR")
    try:
        with open(os.path.join(rd, worker + ".json")) as f:
            rec = json.load(f)
        issue, repo = rec.get("issue"), rec.get("repo")
    except (OSError, ValueError, AttributeError, TypeError):
        return STALL_ASK
    if isinstance(issue, bool) or not isinstance(issue, (int, str)) or not re.fullmatch(r"[1-9][0-9]{0,9}", str(issue)):
        return STALL_ASK
    if isinstance(repo, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", repo):
        return "%s#%s" % (repo, issue)
    return str(issue)


def boss_of(launcher, worker):
    """The worker's reports_to from the switchboard's record (read only, CW_CLOUD_RECORD_DIR); None when unreadable or not a name."""
    rd = (getattr(launcher, "env", None) or {}).get("CW_CLOUD_RECORD_DIR")
    if not rd:
        return None
    try:
        with open(os.path.join(rd, worker + ".json")) as f:
            boss = json.load(f).get("reports_to")
    except (OSError, ValueError, AttributeError):
        return None
    return boss if isinstance(boss, str) and AGENT_NAME.fullmatch(boss) else None


def _idle_notice_send(launcher, pod, worker, name, data, now):
    """Ask #2443: ONE `idle-notice` audit line per idle of a pod (the key is the idle message's id, kept in the pod annotation cw/idle-notice BEFORE the send; a line that could not be
    written puts the old mark back so the next pass tries again). The daemon adds the pod's last report and sends the message to the boss. Returns True when a line was written."""
    try:
        note = idle_notice(data)
    except Exception:   # the pod's answer is untrusted: whatever it holds, the stall conditions after this call still run
        return False
    if note is None or _ann(pod, "idle-notice") == note["key"]:
        return False
    last = launcher_parse(_ann(pod, "idle-notice-at"))
    if last is not None and 0 <= (now - last).total_seconds() < IDLE_NOTICE_MIN_SECONDS:
        return False   # too soon after this pod's last notice: the next pass tells the newest idle
    kube, uid = launcher.kube, (pod.get("metadata") or {}).get("uid")
    scrub = (lambda t: kube.scrub(t, 3000)) if callable(getattr(kube, "scrub", None)) else (lambda t: t)
    boss = boss_of(launcher, worker)
    kube.annotate(name, uid, idle_notice=note["key"], idle_notice_at=launcher_iso(now))   # BEFORE the send
    try:
        wrote = launcher.audit(worker, "pod/" + name, IDLE_ACTION, 0.0, True, "",
                               extra={"idle": note["key"], "state": note["state"], "outcome": note["outcome"], "finish": note["finish"], "turn_start": note["turn_start"],
                                      "evidence": evidence(scrub(note["text"])), "jobs": note["jobs"], "results_exists": note["results_exists"], "ahead": note["ahead"],
                                      "recipients": [boss] if boss else [], "ask": ask_of(launcher, worker)})
        ok = wrote is not False
    except Exception as e:
        print("cloud-worker: idle notice for %s not recorded: %s" % (name, type(e).__name__), file=sys.stderr)
        ok = False
    if not ok:
        kube.annotate(name, uid, idle_notice=_ann(pod, "idle-notice") or "none", idle_notice_at=_ann(pod, "idle-notice-at") or "none")
    return ok


def watch_pass(launcher, pods, now, name_ok):
    """One pass over the pods the reaper holds: ONE exec per pod older than 10 minutes, the alerts decided from its answer. Sets launcher.stall_counts to
    {"read": n, "alerted": m} for the pass line. Never raises, never stops or changes a pod. Returns the (pod, condition) pairs alerted, for the tests."""
    counts = launcher.__dict__["stall_counts"] = {"read": 0, "alerted": 0, "skipped": []}
    alerted, began = [], time.monotonic()
    kube = launcher.kube
    for pod in pods:
        name = "?"
        try:
            worker, name = _ann(pod, "worker"), pod["metadata"]["name"]
            if (pod.get("status") or {}).get("phase") != "Running" or not name_ok(worker or ""):
                continue
            started = launcher_parse(_ann(pod, "started"))
            if started is None:
                counts["skipped"].append("%s:%s" % (name, "no-readable-start-time"))
                continue
            young = (now - started).total_seconds() < MIN_POD_AGE_SECONDS
            if young:   # the stall conditions wait for 10 minutes of age; the idle notice does not (a quick task is idle early)
                counts["skipped"].append("%s:%s" % (name, "younger-than-10-minutes"))
            if time.monotonic() - began > PASS_BUDGET_SECONDS:
                counts["skipped"].append("%s:%s" % (name, "read-budget-used"))
                continue
            try:
                data = json.loads(kube.exec(name, "python3", "-I", "-c", PROBE_CODE, timeout=EXEC_SECONDS).strip().splitlines()[-1])
                if not isinstance(data, dict):
                    raise ValueError("not an object")
            except Exception:
                if not young:
                    counts["skipped"].append("%s:%s" % (name, "the-read-failed"))
                continue
            if not young:
                counts["read"] += 1
            if _idle_notice_send(launcher, pod, worker, name, data, now):
                counts["notices"] = counts.get("notices", 0) + 1
            if young:
                continue
            data["started"] = started.timestamp()   # ask #2472: results.txt older than this is an earlier run's
            for condition, text in decide(data).items():
                mark = _ann(pod, "stall-alert-" + condition)
                last = launcher_parse(mark)
                if mark and (last is None or 0 <= (now - last).total_seconds() < REALERT_SECONDS):
                    continue   # alerted within the hour (an unreadable mark counts as alerted)
                ev = evidence(kube.scrub(text, 3000) if callable(getattr(kube, "scrub", None)) else text)
                kube.annotate(name, (pod.get("metadata") or {}).get("uid"), **{"stall_alert_" + condition.replace("-", "_"): launcher_iso(now)})   # BEFORE the send
                boss = boss_of(launcher, worker)
                if boss is None:
                    print("cloud-worker: stall alert for %s goes to the Chief of Staff only: no boss in the record" % name, file=sys.stderr)
                delivered = 0
                try:   # ONE audit line; the daemon sends the messages (the reaper has no agent identity and cannot run `send`)
                    wrote = launcher.audit(worker, "pod/" + name, ALERT_ACTION, 0.0, True, "",
                                           extra={"condition": condition, "recipients": [boss] if boss else [], "ask": ask_of(launcher, worker), "evidence": ev})
                    delivered = 0 if wrote is False else 1   # the real writer answers False when the line could not be written
                except Exception as e:
                    print("cloud-worker: stall alert for %s not recorded: %s" % (name, type(e).__name__), file=sys.stderr)
                if not delivered:   # not recorded: the mark moves back so the next pass at least RETRY_SECONDS later tries again
                    kube.annotate(name, (pod.get("metadata") or {}).get("uid"), **{"stall_alert_" + condition.replace("-", "_"): launcher_iso(now - dt.timedelta(seconds=REALERT_SECONDS - RETRY_SECONDS))})
                if delivered:
                    counts["alerted"] += 1
                    alerted.append((name, condition))
        except Exception as e:
            print("cloud-worker: stall watch failed for %s: %s" % (name, type(e).__name__), file=sys.stderr)
    return alerted


def launcher_parse(text):
    import datetime as dt
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def launcher_iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def pass_text(launcher):
    """The two counts of the pass line: ' stall read=N alerted=M' (empty when the step did not run)."""
    c = getattr(launcher, "__dict__", {}).get("stall_counts")
    if not c:
        return ""
    skipped = " skipped=" + ",".join(re.sub(r"[^A-Za-z0-9._:-]", "", x)[:90] for x in c["skipped"][:10]) + ("+%d" % (len(c["skipped"]) - 10) if len(c["skipped"]) > 10 else "") if c["skipped"] else ""
    return " stall read=%d alerted=%d%s" % (c["read"], c["alerted"], skipped)
