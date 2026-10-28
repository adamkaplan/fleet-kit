"""A cloud worker that the launcher ended alerts its boss and the Chief of Staff at once (ask #1480).

The launcher writes one JSON line per action to its audit file. This pass reads the NEW stop lines (from a stored cursor), and for a stop the launcher
made for an idle worker, a worker past its hard limit, a pod that was gone or a failed pod, it sends ONE message to the boss recorded in the worker's
cloud record and ONE to the Chief of Staff, unless the worker already reported done. The message names the worker, the reason, the time, whether the
work was pushed and the run record's name; never an address, a log line or a transcript.

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
LABELS = (("idle", "idle"), ("hard limit", "hard limit"), ("pod gone", "pod gone"), ("pod failed", "failed pod"), ("pod was failed", "failed pod"),
          ("failed pod", "failed pod"))
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
    return (0 if offset > size else offset), keys, False   # a smaller file is a new file: read it from its start


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
    head = reason.split(";")[0].strip().lower() if isinstance(reason, str) else ""
    for start, label in LABELS:
        if head == start or head.startswith(start + " "):
            return label
    return None


def parse_reason(reason):
    """(run id, run record directory name) from the launcher's reason text; "" for what is not there."""
    run = re.search(r"run-id=([0-9A-Za-z]+)", reason)
    rec = re.search(r"run record (\S+)", reason)
    return (run.group(1) if run else ""), (os.path.basename(rec.group(1).rstrip("/")) if rec else "")


def pushed_text(record_dir, parse):
    """`yes (branch X)`, `no`, or `unknown`, from the run record's end_hook."""
    run = read_small(os.path.join(record_dir, "run.json"), parse) if record_dir else None
    hook = run.get("end_hook") if run else None
    if not isinstance(hook, dict):
        return "unknown"
    if hook.get("pushed") is True:
        branch = plain(hook.get("wip_branch") or "", 100)
        return "yes (branch %s)" % branch if branch else "yes"
    return "no"


def message_text(worker, label, when, pushed, record_name):
    return ("[cloud worker ended] %s was ended by the launcher: reason %s; at %s; work pushed: %s; run record: %s."
            % (worker, label, when, pushed, record_name or "none"))


def scan_audit(audit_file, state_file, cloud_dir, last_reports, send, parse):
    """One pass. `send(recipient, text)` is the daemon's own send path. Returns [(recipient, text)] handed to it."""
    try:
        size = os.stat(str(audit_file)).st_size
    except OSError:
        return []
    offset, keys, fresh = load_state(state_file, size, parse)
    if fresh:
        save_state(state_file, size, [])
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
        if label is None or not isinstance(worker, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", worker):
            continue
        run_id, record_name = parse_reason(line["reason"])
        key = (run_id or "%s@%s" % (worker, line.get("ts")), label)
        if key in keys:
            continue
        if (last_reports.get(worker) or {}).get("state") == "done":
            continue
        record = read_small(os.path.join(str(cloud_dir), worker + ".json"), parse)
        boss = record.get("reports_to") if record and record.get("worker") == worker else None
        boss = "".join(c for c in boss if c.isprintable())[:64].strip() if isinstance(boss, str) else ""
        if not boss:
            continue
        match = re.search(r"run record (\S+)", line["reason"])
        text = message_text(worker, label, plain(line.get("ts"), 30), pushed_text(match.group(1) if match else "", parse), plain(record_name, 80))
        keys.append(key)
        out.extend((to, text) for to in ([boss] if boss == CHIEF else [boss, CHIEF]))
    save_state(state_file, offset + cut, keys)
    for to, text in out:
        try:
            send(to, text)
        except Exception:
            pass   # one recipient that cannot be reached never stops the other
    return out
