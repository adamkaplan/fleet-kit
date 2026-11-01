"""A cloud worker that the launcher ended alerts its boss and the Chief of Staff (ask #1480).

A failed pod is alerted only when the reaper stops it at its idle or hard limit (cause pod_failed): the reaper reads a pod's activity through the pod, a
failed pod cannot answer, so the reaper does not stop it sooner (a follow-up ask is filed as #1526).

The launcher writes one JSON line per action to its audit file. This pass reads the NEW stop lines (from a stored cursor), and for a stop line that carries the field `cause` (idle,
hard, pod_gone or pod_failed; decided from that field alone), it sends ONE message to the
Chief of Staff always and ONE to the boss recorded in the worker's cloud record when that name is a valid agent name, unless the worker already
reported done. The message holds five facts, plus, for pod_failed, the pod's termination reason and exit code (added by a later change): the worker, the reason, the time, pushed (yes, no or unknown) and the run record's name (or
unknown); never a branch, an address, a log line or a transcript.

The state is one small private file (0600): {"offset": bytes already read, "sent": [[run id, cause], ...] (the last 500)}. A missing or corrupt
file starts at the END of the audit file, so old history is never replayed. The state is saved BEFORE the messages go: a crash loses a message,
it never sends one twice. Pure of the daemon: the send function, the paths, the last reports and the shared bounded parse (record_json.parse_bounded) are handed in.
"""
import datetime
import json
import os
import re
import stat

CHIEF = "chief-of-staff"
SENT_KEEP = 500          # the (run id, reason) keys kept
READ_BYTES = 1 << 20     # the most audit bytes one pass reads
FILE_BYTES = 65536       # the most a record file may be
# The launcher writes the field `cause` on every stop it makes of its own accord; the decision reads that field and nothing else (the reason text is
# never read for it: a person's words, even `idle`, send nothing). The audit file and the run records are UNTRUSTED text.
CAUSES = {"idle": "idle limit", "hard": "hard limit", "pod_gone": "pod gone", "pod_failed": "pod failed"}
AGENT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")   # the switchboard's own agent-name pattern (NAME_RE in bin/fleet-switchboard)
WORKER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RECORD_STAMP = r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}"   # the launcher's run record name: <worker>-<UTC stamp>-<6 hex> (cloud/launcher/run_record.py)
SAFE = re.compile(r"[^A-Za-z0-9._/:+@=-]")


def plain(value, limit=80):
    """Text made safe to show: printable ASCII of a small set only, cut short."""
    return SAFE.sub("", str(value))[:limit]


ISO_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def when_text(value):
    """The launcher's own ISO UTC time, or the word unknown (a shape check, never a copy of other text)."""
    return value if isinstance(value, str) and ISO_UTC.fullmatch(value) else "unknown"


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


def message_text(worker, label, when, pushed, record_name, kept=False, failure=None):
    text = ("[cloud worker ended] %s was ended by the launcher: reason %s; at %s; work pushed: %s; run record: %s."
            % (worker, label, when, pushed, record_name or "unknown"))
    if kept:
        text += " The end hook could not push: the pod was kept and the work may be unpushed."
    if failure is not None:
        text += " Pod termination: %s." % failure
    return text


# ---- ask #1743: a rotated audit is read through its archives, every line once (the rotation record is written by cloud/launcher/cloud_worker.py) ----
ROTATION_WORDS = "an audit archive needed to read older stop lines was missing or unreadable"
_ARCHIVE_NAME = re.compile(r"audit-[0-9T-]+\.jsonl\.gz")
_ROTATION_RECORD_MAX = 4096
_ARCHIVE_CUT_MAX = 8 << 20
_CUTS_KEPT = 16


def _open_regular(path):
    """A file descriptor of a regular file opened without following a link, or None. Never raises."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        if os.fstat(fd).st_mode & 0o170000 == 0o100000:
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _rotation_record(path, parse, size):
    """(rotations completed, [(cut, archive name)] oldest first, the last perhaps a begun one with no name yet) from the record beside the audit file;
    (0, []) when there is no record; None for one that is damaged, of the wrong shape or larger than 4096 bytes (the reader then behaves as before)."""
    record = path + ".rotations"
    if not os.path.lexists(record):
        return 0, []
    fd = _open_regular(record)
    if fd is None:
        return None
    try:
        raw = os.read(fd, _ROTATION_RECORD_MAX + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        data = parse(raw.decode("utf-8")) if len(raw) <= _ROTATION_RECORD_MAX else None
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    done, cuts, pending = data.get("done"), data.get("cuts"), data.get("pending")
    if isinstance(done, bool) or not isinstance(done, int) or done < 0 or not isinstance(cuts, list) or len(cuts) > _CUTS_KEPT or len(cuts) > done:
        return None
    entries = []
    for item in cuts:
        cut, name = (item.get("cut"), item.get("archive")) if isinstance(item, dict) else (None, None)
        if isinstance(cut, bool) or not isinstance(cut, int) or cut <= 0 or not isinstance(name, str):
            return None
        entries.append((cut, name))
    if pending is not None:
        if not isinstance(pending, dict) or any(isinstance(pending.get(k), bool) or not isinstance(pending.get(k), int) or pending.get(k) < 0 for k in ("cut", "old_size")):
            return None
        if size < pending["old_size"]:   # the replacement has happened: one more rotation
            return done + 1, entries + [(pending["cut"], None)]
    return done, entries


def _archive_stream(directory, name, cut):
    """The decompressed bytes of one archive as a file object (gzip), or None when the name, the file or the cut is not acceptable."""
    import gzip
    if not isinstance(name, str) or not _ARCHIVE_NAME.fullmatch(name) or not isinstance(cut, int) or not 0 < cut <= _ARCHIVE_CUT_MAX:
        return None
    fd = _open_regular(os.path.join(directory, name))
    if fd is None:
        return None
    raw = os.fdopen(fd, "rb")
    stream = gzip.GzipFile(fileobj=raw, mode="rb")
    stream.myfileobj = raw   # closing the stream closes the file
    return stream


def _archives_whole(directory, segs):
    """True when every archive of the chain decompresses to exactly its cut in bytes (read through gzip for at most its cut plus one)."""
    for name, cut in segs:
        stream = _archive_stream(directory, name, cut)
        if stream is None:
            return False
        try:
            with stream:
                total = 0
                while total <= cut:
                    block = stream.read(min(65536, cut + 1 - total))
                    if not block:
                        break
                    total += len(block)
        except (OSError, EOFError, ValueError):
            return False
        if total != cut:
            return False
    return True


def _archive_chunk(directory, chain, limit):
    """(complete lines as bytes, the chain after them or None when it is done) for one pass, or None when an archive cannot be read."""
    name, cut = chain["segs"][chain["i"]]
    pos = chain["pos"]
    stream = _archive_stream(directory, name, cut)
    if stream is None:
        return None
    try:
        with stream:
            skip = pos
            while skip > 0:
                block = stream.read(min(65536, skip))
                if not block:
                    return None
                skip -= len(block)
            raw = stream.read(min(limit, cut - pos))
    except (OSError, EOFError, ValueError):
        return None
    end = raw.rfind(b"\n") + 1 or len(raw)   # an archive holds complete lines; one longer than the bound is skipped whole
    pos += end
    if pos < cut:
        return raw[:end], dict(chain, pos=pos)
    following = chain["i"] + 1
    return raw[:end], (dict(chain, i=following, pos=0) if following < len(chain["segs"]) else None)


def _rotation_step(path, parse, size, offset, applied, chain, limit):
    """One pass of the rotation rule: (offset, rotations applied, chain, archive bytes or None, failed). None for the bytes means the caller reads the live file
    from `offset` as it always did; a chain still open or just finished gives its complete lines (maybe none) and the caller reads the live file only afterwards."""
    directory = os.path.dirname(path) or "."
    if chain is None:
        rec = _rotation_record(path, parse, size)
        if rec is None:
            return offset, applied, None, None, False
        count, entries = rec
        if applied is None:
            return offset, count, None, None, False
        grown = count - applied
        if grown <= 0 or grown > len(entries):
            return offset, applied, None, None, False
        live, segs, start = offset, [], 0
        for index, (cut, name) in enumerate(entries[-grown:]):
            if live >= cut:
                live -= cut
            else:
                segs = [[n, c] for c, n in entries[-grown:][index:]]
                start, live = live, 0
                break
        if not segs:
            return live, count, None, None, False
        if not _archives_whole(directory, segs):
            return 0, count, None, None, True
        chain = {"segs": segs, "i": 0, "pos": start}
        offset, applied = 0, count
    got = _archive_chunk(directory, chain, limit)
    if got is None:
        return 0, applied, None, None, True
    data, chain = got
    return offset, applied, chain, data, False


def _valid_chain(value):
    """A chain kept in a reader's own state: None or {segs: [[name, cut]], i, pos}, all of the right type; anything else is no chain."""
    if value is None:
        return True
    if not isinstance(value, dict) or not isinstance(value.get("segs"), list) or not 0 < len(value["segs"]) <= _CUTS_KEPT:
        return False
    for seg in value["segs"]:
        if not (isinstance(seg, list) and len(seg) == 2 and isinstance(seg[0], str) and _ARCHIVE_NAME.fullmatch(seg[0])
                and isinstance(seg[1], int) and not isinstance(seg[1], bool) and 0 < seg[1] <= _ARCHIVE_CUT_MAX):
            return False
    i, pos = value.get("i"), value.get("pos")
    return all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (i, pos)) and i < len(value["segs"]) and pos < value["segs"][i][1]


_IN_ARCHIVE_PASS = []   # set while scan_audit reads a chunk of an archive through itself


def _write_count(count_file, value):
    """The count file by a temporary file and a rename (a reader that polls it never sees it empty)."""
    temp = "%s.%d.tmp" % (count_file, os.getpid())
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as handle:
        handle.write(json.dumps(value) + "\n")
    os.replace(temp, count_file)


def _follow_rotation(audit_file, state_file, cloud_dir, last_reports, send, parse, audit):
    """The audit's rotation rule (ask #1743): the unread archive lines of this pass go through scan_audit itself from a private temporary file, before the live
    file. None when the live file is to be read as always (its offset is translated first). The reader's count of applied rotations and its place in the chain
    live in a file of their own beside the state file."""
    import tempfile
    path = str(audit_file)
    try:
        size = os.stat(path).st_size
    except OSError:
        return None
    state = read_small(state_file, parse)
    offset, keys = (state.get("offset"), state.get("sent")) if state else (None, None)
    count_file = str(state_file) + ".rotations"
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or not isinstance(keys, list):
        first = _rotation_record(path, parse, size)   # first sight: the current count is taken and nothing moves
        if first is not None:
            _write_count(count_file, {"applied": first[0], "chain": None})
        return None
    mine = read_small(count_file, parse) or {}
    applied, chain = mine.get("applied"), mine.get("chain")
    if isinstance(applied, bool) or not isinstance(applied, int) or applied < 0 or not _valid_chain(chain):
        applied, chain = None, None
    new_offset, new_applied, new_chain, data, failed = _rotation_step(path, parse, size, offset, applied, chain, READ_BYTES)
    if (new_offset, new_applied, new_chain) != (offset, applied, chain):
        _write_count(count_file, {"applied": new_applied, "chain": new_chain})
        if new_offset != offset:
            save_state(state_file, new_offset, [(str(k[0]), str(k[1])) for k in keys if isinstance(k, list) and len(k) == 2])
    if failed:
        try:
            send(CHIEF, "[cloud worker ended] %s." % ROTATION_WORDS)
        except Exception:
            pass
    if data is None:
        return None
    if not data:
        return []
    scratch = tempfile.mkdtemp(prefix=".audit-archive-", dir=os.path.dirname(str(state_file)) or ".")
    try:
        part, scratch_state = os.path.join(scratch, "part.jsonl"), os.path.join(scratch, "state")
        with os.fdopen(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            handle.write(data)
        save_state(scratch_state, 0, [(str(k[0]), str(k[1])) for k in keys if isinstance(k, list) and len(k) == 2])
        _IN_ARCHIVE_PASS.append(1)
        try:
            result = scan_audit(part, scratch_state, cloud_dir, last_reports, send, parse, audit)
        finally:
            _IN_ARCHIVE_PASS.pop()
        merged = read_small(scratch_state, parse)
        if merged and isinstance(merged.get("sent"), list):
            save_state(state_file, new_offset, [(str(k[0]), str(k[1])) for k in merged["sent"] if isinstance(k, list) and len(k) == 2])
        return result
    finally:
        for name in os.listdir(scratch):
            try:
                os.unlink(os.path.join(scratch, name))
            except OSError:
                pass
        os.rmdir(scratch)


def scan_audit(audit_file, state_file, cloud_dir, last_reports, send, parse, audit=None):
    """One pass. `send(recipient, text)` is the daemon's own send path. Returns [(recipient, text)] handed to it."""
    if not _IN_ARCHIVE_PASS:
        followed = _follow_rotation(audit_file, state_file, cloud_dir, last_reports, send, parse, audit)
        if followed is not None:
            return followed
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
        if not isinstance(line, dict) or line.get("action") != "stop" or not isinstance(line.get("ok"), bool):
            continue
        cause, worker = line.get("cause"), line.get("worker")
        label = CAUSES.get(cause) if isinstance(cause, str) else None
        reason = line.get("reason") if isinstance(line.get("reason"), str) else ""
        if label is None or not isinstance(worker, str) or not WORKER_NAME.fullmatch(worker):
            continue
        run_id, record_name = parse_reason(reason)
        hour = re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}", str(line.get("ts")))
        if not run_id and not hour:
            continue
        key = (run_id or "%s@%s" % (worker, hour.group(0)), cause)   # no run id: worker, cause and the hour
        if key in keys:
            continue
        if (last_reports.get(worker) or {}).get("state") == "done":
            continue
        record = read_small(os.path.join(str(cloud_dir), worker + ".json"), parse)
        boss = record.get("reports_to") if record and record.get("worker") == worker else None
        name = record_name_of(worker, record_name)
        pushed = pushed_text(raw_dir(reason), parse) if name else "unknown"
        text = message_text(worker, label, when_text(line.get("ts")), pushed, name, line["ok"] is False)
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


# ---- ask #1484: a cloud worker that ended its turn and waits tells its recorded boss (the same sender, state file, keys and bound as above) ----
TURN_HOLD = 60          # seconds a turn end is held: a new turn within it cancels the message (a tool-driven continuation)
TURN_CAUSE = "turn_end"  # the second half of the stored key (worker@last update, turn_end)


def turn_message_text(worker, when):
    """Five facts: the worker, the time, the fixed words, how to read the answer and the idle limit. Never the worker's own text."""
    return ("[cloud worker waiting] %s ended its turn and waits for an instruction; at %s; read its answer with: fleet-switchboard read %s;"
            " the idle limit will stop it if nothing is sent." % (worker, when, worker))


def scan_turns(workers, activity, state_file, audit_file, send, parse, now, memory, hold=TURN_HOLD):
    """One pass over the live cloud workers [(worker, boss)]. `activity(worker)` gives (busy, last update in epoch seconds) or None (unreadable: skipped).
    `memory` is the daemon's own dict, kept between passes. One message per turn end, held `hold` seconds; sent through `send(recipient, text)`
    to the recorded boss when it is a valid agent name, else to the Chief of Staff alone. Returns [(recipient, text)] handed to it."""
    try:
        size = os.stat(str(audit_file)).st_size
    except OSError:
        size = 0
    offset, keys, _fresh = load_state(state_file, size, parse)
    due = []
    for worker, boss in workers:
        if not isinstance(worker, str) or not WORKER_NAME.fullmatch(worker):
            continue
        try:
            act = activity(worker)
        except Exception:
            act = None
        if not act:
            continue
        busy, updated = act
        mem = memory.setdefault(worker, {"busy": False, "seen": None, "pending": None})
        if busy:
            mem["busy"], mem["pending"] = True, None   # a new turn cancels a pending message
            continue
        if isinstance(updated, bool) or not isinstance(updated, (int, float)):
            continue
        ended = mem["busy"] or (mem["seen"] is not None and updated > mem["seen"])
        mem["busy"], mem["seen"] = False, updated
        if ended and mem["pending"] is None:
            mem["pending"] = (updated, now)
        pending = mem["pending"]
        if pending and now - pending[1] >= hold:
            mem["pending"] = None
            key = ("%s@%d" % (worker, pending[0]), TURN_CAUSE)
            if key in tuple(keys):
                continue
            keys.append(key)
            when = datetime.datetime.fromtimestamp(pending[0], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            to = boss if isinstance(boss, str) and AGENT_NAME.fullmatch(boss) else CHIEF
            due.append((to, turn_message_text(worker, when)))
    if due:
        save_state(state_file, offset, keys)   # saved BEFORE the messages go, as above
        for to, text in due:
            try:
                send(to, text)
            except Exception:
                pass
    return due
