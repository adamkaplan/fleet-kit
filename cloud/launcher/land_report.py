"""The daily land report (ask #2448): what the cloud workers' landings did in the last 24 hours, one message to the Chief of Staff and one comment line.

READ-ONLY. It reads the operator's run records (`<record dir>/<run id>/run.json`, written by run_record.py, with `end_hook.run_logs`) and the landed commits'
`Land-Checks` trailers (one `git log`), and writes only: one message through the switchboard's `send cos` and one comment line on ONE configured issue
(`gh issue comment`; no body is edited). The reaper's pass calls `daily_pass` (cloud_worker.py); it sends once a day after 13:00 UTC and keeps the last day in a
small private state file, per part (message, comment), so a part that failed is tried at the next pass and a part that went is never sent twice.

What the data holds (read on 12 November 2026, stated first):
  run_logs    {"runs": [{"name", "exit", "woke", "log_tail"}], "wake_lines": []}: NO time field. A per-step time of `tests:server` or `tests:pg` is therefore NOT
              recorded; the report reads one only from a line `step=<name> seconds=<n>` in a run's log tail, and a land run without it is counted as `no step data`
              (never as zero). The follow-up is that the land tool prints that line. The land wall time is the run record's own `end` minus `start`.
  attempt     a record whose newest run named `land` or `land-...` exists; its exit is 0 landed, 18 council, 13 tests, 3 environment, 124 or 137 time limit, else other.
              A record with no such run is not an attempt (`no land run`).
  trailer     `Land-Checks: files=<n> lines=<n> rounds=<n>` (key=value pairs; `rounds` may be in `Land-Council` instead); a missing key is counted as unread, not 0.
  lane        the worker name without its trailing `-<digits>`.
Everything read is untrusted: a corrupt record, a link, a file over RECORD_MAX bytes is skipped and counted (`N records not read`); the output goes through one scrub."""
import datetime as dt
import json
import math
import os
import re
import stat
import subprocess
import tempfile

RECORD_MAX = 1 << 20          # a run.json over this is not read (the real one is a few KB)
MAX_RECORDS = 5000            # directories looked at in one pass
SMALL_LINES_DEFAULT = 50      # the small bound, a setting (CW_LAND_REPORT_SMALL_LINES)
SEND_HOUR = 13                # UTC: the report goes on the first pass at or after this hour
WINDOW = dt.timedelta(hours=24)
CALL_SECONDS = 60
MAX_TRIES = 3                 # tries of one part per day
MESSAGE_MAX = 3500
EXIT_REASON = {0: "landed", 18: "council", 13: "tests", 3: "environment", 124: "time limit", 137: "time limit"}
REASONS = ("council", "tests", "environment", "time limit", "other")
STEPS = ("tests:server", "tests:pg")
STEP_LINE = re.compile(r"\bstep=(tests:server|tests:pg) seconds=(\d{1,7}(?:\.\d{1,3})?)\b")
LAND_NAME = re.compile(r"land(?:-[A-Za-z0-9._-]{0,60})?")
RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}-\d{8}T\d{6}Z-[0-9a-f]{6}")
ISSUE_REF = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99})#([1-9][0-9]{0,8})")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
ISO = "%Y-%m-%dT%H:%M:%SZ"
SECRET_SHAPES = re.compile(r"(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|sk-|xoxb-|xoxp-|xoxa-|AKIA|AIza|Bearer )[A-Za-z0-9._~+/=-]{8,}|eyJ[A-Za-z0-9_-]{8,}|-----BEGIN ")


def scrub(text):
    """The one scrub of every output: secret shapes become [redacted], anything but printable ASCII and newline is dropped."""
    text = SECRET_SHAPES.sub("[redacted]", str(text))
    return "".join(c for c in text if c == "\n" or " " <= c <= "~")


def name_text(value, limit=40, extra=""):
    return (re.sub(r"[^A-Za-z0-9._%s-]" % re.escape(extra), "", value) if isinstance(value, str) else "")[:limit] or "unknown"


def lane_of(worker):
    return re.sub(r"-\d+$", "", name_text(worker, 64))


def parse_time(text):
    try:
        return dt.datetime.strptime(text, ISO).replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def small_bound(env):
    """The small bound in changed lines: the setting, else 50; a value that is not a whole number from 1 to 100000 is the default."""
    raw = (env or {}).get("CW_LAND_REPORT_SMALL_LINES")
    return int(raw) if isinstance(raw, str) and re.fullmatch(r"[1-9][0-9]{0,5}", raw.strip()) else SMALL_LINES_DEFAULT


def median(values):
    v = sorted(values)
    if not v:
        return None
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def p90(values):
    """Nearest rank: the value at rank ceil(0.9 n) of the sorted list."""
    v = sorted(values)
    return v[math.ceil(0.9 * len(v)) - 1] if v else None


def num(x):
    return ("%.1f" % x).rstrip("0").rstrip(".") if isinstance(x, float) else str(x)


# ---- reading the run records
def read_record(path):
    """The run.json dict of one record directory, or None (a link, a non-regular or big file, bad JSON, a wrong shape). Never raises."""
    try:
        d = os.lstat(path)
        if not stat.S_ISDIR(d.st_mode):
            return None
        fd = os.open(os.path.join(path, "run.json"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > RECORD_MAX:
                return None
            data = os.read(fd, RECORD_MAX + 1)
        finally:
            os.close(fd)
        rec = json.loads(data.decode("utf-8"))
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def land_run(rec):
    """The newest land run of a record as {exit, tail}, or None when it has none."""
    hook = rec.get("end_hook")
    logs = hook.get("run_logs") if isinstance(hook, dict) else None
    runs = logs.get("runs") if isinstance(logs, dict) else None
    for r in runs if isinstance(runs, list) else []:   # newest first
        if isinstance(r, dict) and isinstance(r.get("name"), str) and LAND_NAME.fullmatch(r["name"]):
            code = r.get("exit")
            tail = r.get("log_tail")
            return {"exit": code if type(code) is int else None, "tail": tail if isinstance(tail, str) else ""}
    return None


def load_records(root, start, end):
    """(records of the window [start, end) as dicts, number not read). A record is one directory of `root` named as a run id."""
    out, unread = [], 0
    try:
        names = sorted(n for n in os.listdir(root) if RUN_ID.fullmatch(n))[-MAX_RECORDS:]
    except OSError:
        return [], 0
    for n in names:
        rec = read_record(os.path.join(root, n))
        if rec is None:
            unread += 1
            continue
        ended = parse_time(rec.get("end"))
        if ended is None:
            unread += 1
            continue
        if not start <= ended < end:
            continue
        began = parse_time(rec.get("start"))
        hook = rec.get("end_hook") if isinstance(rec.get("end_hook"), dict) else {}
        out.append({"worker": name_text(rec.get("worker"), 64), "repo": name_text(rec.get("repository"), 100, "/"), "lane": lane_of(rec.get("worker")),
                    "minutes": (ended - began).total_seconds() / 60 if began and ended >= began else None, "land": land_run(rec),
                    "sha": rec.get("head_sha") if isinstance(rec.get("head_sha"), str) and re.fullmatch(r"[0-9a-f]{7,40}", rec["head_sha"]) else None,
                    "reason": name_text(rec.get("exit_reason"), 30) if isinstance(rec.get("exit_reason"), str) else "unknown",
                    "pushed": hook.get("pushed") is True})
    return out, unread


def reason_of(code):
    return EXIT_REASON.get(code, "other")


# ---- the landed commits
def parse_trailer(text):
    out = {}
    for k, v in re.findall(r"\b(files|lines|rounds)=(\d{1,9})\b", text if isinstance(text, str) else ""):
        out.setdefault(k, int(v))
    return out


def git_landed(git_dir, branch, since, run=subprocess.run):
    """[{sha, files, lines, rounds}] of the commits on `branch` since `since` that carry a Land-Checks trailer; None when git failed (the report says so)."""
    if not git_dir or not BRANCH.fullmatch(branch or "") or branch.startswith("-"):
        return None
    fmt = "%x1e%H%x1f%(trailers:key=Land-Checks,valueonly,unfold)%x1f%(trailers:key=Land-Council,valueonly,unfold)"
    try:
        res = run(["git", "-C", git_dir, "log", branch, "--since=" + since.strftime(ISO), "--format=" + fmt, "--"], capture_output=True, text=True, timeout=CALL_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    out = []
    for item in res.stdout.split("\x1e")[1:]:
        parts = item.split("\x1f")
        if len(parts) < 3 or not re.fullmatch(r"[0-9a-f]{40}", parts[0].strip()) or not parts[1].strip():
            continue
        checks = parse_trailer(parts[1])
        rounds = checks.get("rounds", parse_trailer(parts[2]).get("rounds"))
        out.append({"sha": parts[0].strip(), "files": checks.get("files"), "lines": checks.get("lines"), "rounds": rounds})
    return out


# ---- the report
def build(records, unread, landed, bound, previous=(), day=""):
    """(multi-line text, one comment line). `records` are the window's, `previous` the 24 h before (for the trend); `landed` the commits or None."""
    lanes = {}
    for r in records:
        lanes.setdefault((r["repo"], r["lane"]), []).append(r)
    landed_shas = [c["sha"] for c in landed or []]
    is_landed = lambda r: bool(r["sha"]) and any(s.startswith(r["sha"]) for s in landed_shas)   # noqa: E731
    prev_by = {}
    for r in previous:
        if r["land"] and r["land"]["exit"] == 0 and r["minutes"] is not None:
            prev_by.setdefault((r["repo"], r["lane"]), []).append(r["minutes"])
    attempts_all = sum(1 for r in records if r["land"])
    landed_all = sum(1 for r in records if r["land"] and r["land"]["exit"] == 0)
    lines = ["Land report %s (window: last 24 h; small bound %d changed lines)" % (day, bound)]
    for (repo, lane), rs in sorted(lanes.items()):
        att = [r for r in rs if r["land"]]
        ok = [r for r in att if r["land"]["exit"] == 0]
        fails = {k: 0 for k in REASONS}
        for r in att:
            if r["land"]["exit"] != 0:
                fails[reason_of(r["land"]["exit"]) if reason_of(r["land"]["exit"]) in fails else "other"] += 1
        lines.append("%s / %s: attempted %d, landed %d, failed: council %d, tests %d, environment %d, time limit %d, other %d; no land run %d" % (
            repo, lane, len(att), len(ok), fails["council"], fails["tests"], fails["environment"], fails["time limit"], fails["other"], len(rs) - len(att)))
        times = [r["minutes"] for r in ok if r["minutes"] is not None]
        if len(times) < 3:
            lines.append("  land wall time: n/a (%d landings with a time, 3 needed)" % len(times))
        else:
            before = median(prev_by.get((repo, lane), []))
            trend = "no earlier day" if before is None else "%+g min vs yesterday" % round(median(times) - before, 1)
            lines.append("  land wall time: median %s min, p90 %s min (n=%d), trend %s" % (num(median(times)), num(p90(times)), len(times), trend))
        for step in STEPS:
            vals, none = [], 0
            for r in att:
                m = [float(s) for n, s in STEP_LINE.findall(r["land"]["tail"]) if n == step]
                if m:
                    vals.append(m[-1])
                else:
                    none += 1
            lines.append("  %s: %s; no step data %d" % (step, "median %s s (n=%d)" % (num(median(vals)), len(vals)) if vals else "n/a", none))
    if landed is None:
        lines.append("landed commits: not read (git failed or no repository directory set)")
    else:
        size = [c for c in landed if c["lines"] is not None]
        over = sum(1 for c in size if c["lines"] > bound)
        files = [c["files"] for c in landed if c["files"] is not None]
        lines.append("landed commits: %d; change size per landing: median %s files, median %s lines (n=%d, %d unread)" % (
            len(landed), num(median(files)) if files else "n/a", num(median([c["lines"] for c in size])) if size else "n/a", len(size), len(landed) - len(size)))
        lines.append("share over %d lines: %d of %d" % (bound, over, len(size)))
        rounds = [c["rounds"] for c in landed if c["rounds"] is not None]
        lines.append("council rounds per landing: %s (n=%d, %d unread)" % (
            "median %s, max %d, 3 or more: %d" % (num(median(rounds)), max(rounds), sum(1 for x in rounds if x >= 3)) if rounds else "n/a", len(rounds), len(landed) - len(rounds)))
    unlanded = [r for r in records if not is_landed(r)]
    lines.append("pods ended unlanded: %d of %d" % (len(unlanded), len(records)))
    for r in unlanded[:30]:
        lines.append("  %s (%s)" % (r["worker"], r["reason"]))
    if unread:
        lines.append("%d records not read" % unread)
    comment = "Land report %s: attempted %d, landed %d, pods ended unlanded %d of %d%s" % (
        day, attempts_all, landed_all, len(unlanded), len(records), "; %d records not read" % unread if unread else "")
    if not landed_all and not landed:
        text = "Land report %s: no landings in the last 24 h (%d attempts, %d pods ended%s)" % (day, attempts_all, len(records), ", %d records not read" % unread if unread else "")
        return scrub(text), scrub(text)
    return scrub("\n".join(lines))[:MESSAGE_MAX], scrub(comment)[:400]


# ---- the schedule
def due(now, state):
    """True when the hour is at or after SEND_HOUR UTC and today's report is not complete in the state."""
    day = now.strftime("%Y-%m-%d")
    done = state.get("day") == day and state.get("message") is True and state.get("comment") is True
    return now.hour >= SEND_HOUR and not done


def read_state(path):
    try:
        d = os.lstat(path)
        if not stat.S_ISREG(d.st_mode) or d.st_size > 4096:
            return {}
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def write_state(path, state):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".land-report-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def send_message(text, issue, run=subprocess.run):
    res = run(["fl" + "eet-switchboard", "send", "cos", "--issue", issue, text], capture_output=True, text=True, timeout=CALL_SECONDS)
    return res.returncode == 0


def post_comment(line, issue, run=subprocess.run):
    m = ISSUE_REF.fullmatch(issue or "")
    if not m:
        return False
    res = run(["gh", "issue", "comment", m.group(2), "--repo", m.group(1), "--body", line], capture_output=True, text=True, timeout=CALL_SECONDS)
    return res.returncode == 0


def daily_pass(env, now, root, state_path, git_dir=None, send=send_message, comment=post_comment, log=None, git=git_landed):
    """One pass of the reaper: when due, build and send. Returns "sent", "partial", "not due" or "failed". Never raises (a failure is logged and tried at the next pass)."""
    log = log or (lambda text: None)
    try:
        state = read_state(state_path)
        if not due(now, state):
            return "not due"
        day = now.strftime("%Y-%m-%d")
        if state.get("day") != day:
            state = {"day": day, "message": False, "comment": False}
        issue = (env or {}).get("CW_LAND_REPORT_ISSUE") or ""
        if not ISSUE_REF.fullmatch(issue):
            log("land report: CW_LAND_REPORT_ISSUE is not set to owner/repo#N; nothing is sent")
            return "failed"
        start = now - WINDOW
        records, unread = load_records(root, start, now)
        earlier, _ = load_records(root, start - WINDOW, start)
        landed = git(git_dir or (env or {}).get("CW_LAND_REPORT_GIT_DIR"), (env or {}).get("CW_LAND_REPORT_BRANCH") or "origin/main", start)
        text, line = build(records, unread, landed, small_bound(env), earlier, day)
        for part, call, body in (("message", send, text), ("comment", comment, line)):
            if state.get(part) is True or state.get(part + "_tries", 0) >= MAX_TRIES:
                continue
            state[part + "_tries"] = state.get(part + "_tries", 0) + 1
            write_state(state_path, state)   # BEFORE the call: a state that cannot be written sends nothing; tries are bounded per day and survive a restart
            try:
                state[part] = bool(call(body, issue))
            except Exception as err:
                state[part] = False
                log("land report: the %s was not sent (%s)" % (part, type(err).__name__))
            else:
                if not state[part]:
                    log("land report: the %s was not sent (refused)" % part)
            write_state(state_path, state)
        return "sent" if state["message"] and state["comment"] else "partial"
    except Exception as err:
        try:
            log("land report: the pass failed (%s)" % type(err).__name__)
        except Exception:
            pass
        return "failed"
