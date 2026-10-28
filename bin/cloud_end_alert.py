"""A cloud worker that the launcher ended alerts its boss and the Chief of Staff at once (ask #1480).

The launcher writes one JSON line per action to its audit file. This pass reads the NEW stop lines (from a stored cursor), and for a stop the launcher
made for an idle worker or a worker past its hard limit (the reason's first part equals `idle` or `hard limit`, exactly), it sends ONE message to the
Chief of Staff always and ONE to the boss recorded in the worker's cloud record when that name is a valid agent name, unless the worker already
reported done. The message holds exactly five facts: the worker, the reason, the time, pushed (yes, no or unknown) and the run record's name (or
unknown); never a branch, an address, a log line or a transcript.

The state is one small private file (0600): {"offset": bytes already read, "sent": [[run id, reason], ...] (the last 500)}. A missing or corrupt
file starts at the END of the audit file, so old history is never replayed. The state is saved BEFORE the messages go: a crash loses a message,
it never sends one twice. Pure of the daemon: the send function, the paths, the last reports and the shared bounded parse (record_json.parse_bounded) are handed in.
"""
import json
import os
import re
import stat

CHIEF = "chief-of-staff"
SENT_KEEP = 500          # the (run id, reason) keys kept
READ_BYTES = 1 << 20     # the most audit bytes one pass reads
FILE_BYTES = 65536       # the most a record file may be
# The launcher's stop reasons (cloud/launcher/cloud_worker.py, reap and stop): `idle` and `hard limit`, followed by "; run-id=NONCE" and "; run record DIR".
# "pod gone" and the failed-pod wording are kept for a stop that names them; anything else (done, finished, ended, pushed ..., a person's words) sends nothing.
# The audit file and the run records are UNTRUSTED text: the reason's first part (up to the first semicolon) must EQUAL one of these, exactly.
LABELS = ("idle", "hard limit")
AGENT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")   # the switchboard's own agent-name pattern (NAME_RE in bin/fleet-switchboard)
WORKER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RECORD_STAMP = r"\d{8}T\d{6}Z-[0-9a-f]{6}"   # the launcher's run record name: <worker>-<UTC stamp>-<6 hex> (cloud/launcher/run_record.py)
SAFE = re.compile(r"[^A-Za-z0-9._/:+@=-]")


def plain(value, limit=80):
    """Text made safe to show: printable ASCII of a small set only, cut short."""
    return SAFE.sub("", str(value))[:limit]


def read_small(path, parse):
    """A small regular file's JSON dict, or None (a link, a FIFO, a big or unreadable file all give None). Never blocks, never raises."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, FILE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        data = parse(raw[:FILE_BYTES].decode("utf-8")) if len(raw) <= FILE_BYTES else None
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_state(path, size, parse):
    """(offset, sent keys). No file, a corrupt file or a wrong shape: the end of the audit file and no keys."""
    data = read_small(path, parse)
    offset, sent = data.get("offset") if data else None, data.get("sent") if data else None
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or not isinstance(sent, list):
        return size, [], True
    keys = [(str(k[0]), str(k[1])) for k in sent if isinstance(k, list) and len(k) == 2]
    if offset > size:
        return size, keys, True   # a truncated or rotated file: start at its end, alert no old line
    return offset, keys, False


def save_state(path, offset, keys):
    temp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps({"offset": offset, "sent": [list(k) for k in keys[-SENT_KEEP:]]}) + "\n")
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def label_of(reason):
    """The plain label of a stop reason the launcher gave, or None for any other reason."""
    head = reason.split(";")[0] if isinstance(reason, str) else ""
    return head if head in LABELS else None


def raw_dir(reason):
    """The run record path the reason names ("" when none)."""
    rec = re.search(r"run record (\S+)", reason)
    return rec.group(1) if rec else ""


def parse_reason(reason):
    """(run id, run record directory name) from the launcher's reason text; "" for what is not there."""
    run = re.search(r"run-id=([0-9A-Za-z]+)", reason)
    rec = re.search(r"run record (\S+)", reason)
    return (run.group(1) if run else ""), (os.path.basename(rec.group(1).rstrip("/")) if rec else "")


def record_name_of(worker, dirname):
    """The run record's name when it matches the launcher's pattern for this worker, else None."""
    return dirname if re.fullmatch(re.escape(worker) + "-" + RECORD_STAMP, dirname or "") else None


def pushed_text(record_dir, parse):
    """`yes`, `no` or `unknown` from the run record's end_hook; never the branch."""
    run = read_small(os.path.join(record_dir, "run.json"), parse) if record_dir else None
    hook = run.get("end_hook") if run else None
    if not isinstance(hook, dict) or not isinstance(hook.get("pushed"), bool):
        return "unknown"
    return "yes" if hook["pushed"] else "no"


def message_text(worker, label, when, pushed, record_name):
    return ("[cloud worker ended] %s was ended by the launcher: reason %s; at %s; work pushed: %s; run record: %s."
            % (worker, label, when, pushed, record_name or "unknown"))


def scan_audit(audit_file, state_file, cloud_dir, last_reports, send, parse, audit=None):
    """One pass. `send(recipient, text)` is the daemon's own send path. Returns [(recipient, text)] handed to it."""
    try:
        size = os.stat(str(audit_file)).st_size
    except OSError:
        return []
    offset, keys, fresh = load_state(state_file, size, parse)
    if fresh:
        save_state(state_file, size, keys)
        return []
    if offset >= size:
        if offset != size:
            save_state(state_file, offset, keys)
        return []
    try:
        with open(str(audit_file), "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(READ_BYTES)
    except OSError:
        return []
    cut = chunk.rfind(b"\n") + 1
    if cut == 0:
        cut = len(chunk) if len(chunk) >= READ_BYTES else 0   # a line too long to be ours is skipped; a half-written one waits
    out = []
    for raw in chunk[:cut].split(b"\n"):
        try:
            line = parse(raw.decode("utf-8")) if raw.strip() else None
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(line, dict) or line.get("action") != "stop" or line.get("ok") is not True:
            continue
        label = label_of(line.get("reason"))
        worker = line.get("worker")
        if label is None or not isinstance(worker, str) or not WORKER_NAME.fullmatch(worker):
            continue
        run_id, record_name = parse_reason(line["reason"])
        key = (run_id or "%s@%s" % (worker, line.get("ts")), label)
        if key in keys:
            continue
        if (last_reports.get(worker) or {}).get("state") == "done":
            continue
        record = read_small(os.path.join(str(cloud_dir), worker + ".json"), parse)
        boss = record.get("reports_to") if record and record.get("worker") == worker else None
        name = record_name_of(worker, record_name)
        pushed = pushed_text(raw_dir(line["reason"]), parse) if name else "unknown"
        text = message_text(worker, label, plain(line.get("ts"), 30), pushed, name)
        keys.append(key)
        to_list = [CHIEF] + ([boss] if isinstance(boss, str) and AGENT_NAME.fullmatch(boss) and boss != CHIEF else [])
        out.extend((to, text, worker, label) for to in reversed(to_list))
    save_state(state_file, offset + cut, keys)
    failed = set()
    for to, text, worker, label in out:
        try:
            result = send(to, text)
            ok = not (result is False or (isinstance(result, dict) and result.get("sent") is False))
        except Exception:
            ok = False
        if not ok and (worker, label) not in failed:
            failed.add((worker, label))
            try:
                if audit is not None:
                    audit(worker, {"reason": label})   # the worker and the reason only, never the message text
            except Exception:
                pass
    return [(to, text) for to, text, _w, _l in out]
