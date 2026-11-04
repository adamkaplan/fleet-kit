"""A lost worker is told to its lane and the Chief of Staff, once, with its cause (ask #1481, plan sha256:9c19b12f...).

One detector in the daemon's pass. It READS records the switchboard already keeps and sends messages; it changes no worker, pod, label or record
(its own small state file `lost-workers.json` apart), and a message that cannot be sent is audited once and never stops the pass.

Division with #1480: the cloud launcher's sender tells for every cloud worker the launcher ends (a stop line). This detector sends for what no stop
line announces. The kinds (`KINDS`):

- `pod_no_stop`: a cloud pod with a `cloud.created` line, its hard deadline passed, and NO stop line in the switchboard audit and none in the launcher
  audit (`cw-cloud/audit.jsonl`, action `stop`, ok), and no final report line (launcher action `report`). A stop of any reason, `done` included, is
  never alerted here (a clean end, or the launcher's own sender).
- `local_worker`: a local worker seen alive (`alive`: name -> lane and ask, built by the daemon through `boss_name`) in an earlier pass that is gone and is not retired in the registry.
- `refused_launch`: a `cloud.create_failed` line of the switchboard audit.
- `reaper_missed`: a failed line of the reaper's own login refresh in the launcher audit (runtime id `reaper`, `ok` false): the reaper could not pass.

Each loss is alerted ONCE (`alerted` in `lost-workers.json`, kept across restarts), to the launching lane (the record's `reports_to`) and to the Chief of
Staff (one message when they are the same agent). The text names the worker, the ask, the kind, the last event time and the evidence line, and says
whether the ask was lost before (`repeat`, with the number of earlier lost workers of the same ask).

The audits are read from a stored byte position (at most READ_BYTES a pass; a file that shrank is read from its start), never in full each pass.
Pure text handling: audit lines are untrusted text, cut to one short line.
"""

import json
import os
import re

STATE_FILE = "lost-workers.json"
KINDS = ("pod_no_stop", "local_worker", "refused_launch", "reaper_missed")
READ_BYTES = 1 << 20          # most one audit file gives a pass
FIRST_BYTES = 4 << 20         # a first pass looks at most this far back
ALERTED_MAX = 2000
PASS_CAP = 10                 # individual alerts a pass sends; the rest are rolled up and alerted on the following passes
RECENT_SECONDS = 3600         # with no usable memory, only workers created in the last hour are alerted
NAME_MAX = 80
SEEN_MAX = 500
EVIDENCE_MAX = 200
REAPER_ID = "reaper"

_PROC_ALERTED = {}            # per memory path: keys this process already alerted or skipped: survives a memory file that cannot be saved or set aside
_PROC_AUDITED = set()          # memory paths whose memory-only audit line this process already wrote (once per path)

NEEDS = ("parse_iso", "_one_line", "parse_bounded")


def launcher_audit_path(env=None):
    env = os.environ if env is None else env
    return env.get("CW_CLOUD_AUDIT") or os.path.join(env.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "cw-cloud", "audit.jsonl")


def _clean(text, limit=EVIDENCE_MAX):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", str(text)).strip()[:limit]


def _name(text):
    """A worker name for a message: control characters (NUL included) removed, at most NAME_MAX characters."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]+", "", str(text))[:NAME_MAX]


def _load(sb, path):
    """(state, status): status is `none` (no file: the first pass ever), `corrupt` (a file that cannot be read as a memory) or `ok`."""
    if not os.path.lexists(path):
        return {}, "none"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = sb.parse_bounded(handle.read(READ_BYTES))
        if isinstance(data, dict):
            data.pop("_gap_ready", None)   # a working flag of one pass, never memory
            return data, "ok"
    except Exception:
        pass
    return {}, "corrupt"


def _save(path, state):
    tmp = "%s.tmp" % path
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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


def _is_new_file(path, new_id):
    """True when the live file's device and inode equal the new_id of a pending rotation."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return isinstance(new_id, list) and new_id == [st.st_dev, st.st_ino]


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
        body = parse(raw.decode("utf-8")) if len(raw) <= _ROTATION_RECORD_MAX else None
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    done, cuts, pend = body.get("done"), body.get("cuts"), body.get("pending")
    if isinstance(done, bool) or not isinstance(done, int) or done < 0 or not isinstance(cuts, list) or len(cuts) > _CUTS_KEPT or len(cuts) > done:
        return None
    entries = []
    for item in cuts:
        cut, name = (item.get("cut"), item.get("archive")) if isinstance(item, dict) else (None, None)
        if isinstance(cut, bool) or not isinstance(cut, int) or cut <= 0 or not isinstance(name, str):
            return None
        entries.append((cut, name))
    if pend is not None:
        if not isinstance(pend, dict) or any(isinstance(pend.get(k), bool) or not isinstance(pend.get(k), int) or pend.get(k) < 0 for k in ("cut", "old_size")):
            return None
        if _is_new_file(path, pend.get("new_id")):   # the replacement has happened (the live file is the file the rotation put in place): one more rotation
            return done + 1, entries + [(pend["cut"], pend.get("archive"))]
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


def _follow_rotation(sb, path, state, key):
    """The audit's rotation rule for this key (ask #1743): the lines of the archives this reader had not read, a pass at a time, before the live file;
    None when the live file is to be read as always (the offset is translated first)."""
    offsets = state.get("offsets") if isinstance(state.get("offsets"), dict) else {}
    offset = offsets.get(key)
    try:
        size = os.stat(path).st_size
    except OSError:
        return None
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        first = _rotation_record(path, sb.parse_bounded, size)   # first sight: the current count is taken and nothing moves
        if first is not None:
            state.setdefault("rotations", {})[key] = {"applied": first[0], "chain": None}
        return None
    mine = state.get("rotations") if isinstance(state.get("rotations"), dict) else {}
    kept = mine.get(key) if isinstance(mine.get(key), dict) else {}
    applied, chain = kept.get("applied"), kept.get("chain")
    if isinstance(applied, bool) or not isinstance(applied, int) or applied < 0 or not _valid_chain(chain):
        applied, chain = None, None
    new_offset, new_applied, new_chain, data, failed = _rotation_step(path, sb.parse_bounded, size, offset, applied, chain, READ_BYTES)
    if (new_offset, new_applied, new_chain) != (offset, applied, chain):
        state.setdefault("offsets", {})[key] = new_offset
        state.setdefault("rotations", {})[key] = {"applied": new_applied, "chain": new_chain}
    if failed:
        import sys
        print("lost-worker: %s" % ROTATION_WORDS, file=sys.stderr)
    if data is None:
        return None
    lines = []
    for chunk in data.split(b"\n"):
        try:
            item = sb.parse_bounded(chunk.decode("utf-8", "replace"))
        except Exception:
            continue
        if isinstance(item, dict):
            lines.append((item, _clean(chunk.decode("utf-8", "replace"))))
    return lines


def _read_new(sb, path, state, key):
    """The new complete lines of an audit file since the stored position (bounded). Never raises; unreadable gives no lines."""
    followed = _follow_rotation(sb, path, state, key)
    if followed is not None:
        return followed
    offsets = state.setdefault("offsets", {})
    try:
        size = os.stat(path).st_size
        start = offsets.get(key)
        if not isinstance(start, int) or start > size or start < 0:
            start = max(0, size - FIRST_BYTES) if start is None else 0
        with open(path, "rb") as handle:
            handle.seek(start)
            raw = handle.read(READ_BYTES)
    except OSError:
        return []
    end = raw.rfind(b"\n")
    if end < 0:
        if len(raw) < READ_BYTES:
            return []
        end = len(raw) - 1   # one line longer than the bound: skipped whole
    offsets[key] = start + end + 1
    lines = []
    for chunk in raw[:end + 1].split(b"\n"):
        try:
            item = sb.parse_bounded(chunk.decode("utf-8", "replace"))
        except Exception:
            continue
        if isinstance(item, dict):
            lines.append((item, _clean(chunk.decode("utf-8", "replace"))))
    return lines


def _fold(state, sb, lines, source):
    """Fold audit lines into the per-worker facts: created, stopped, reported, refused launches, reaper failures."""
    created, stop, report = state.setdefault("created", {}), state.setdefault("stop", {}), state.setdefault("report", {})
    for item, raw in lines:
        ts = sb.parse_iso(item.get("ts")) if isinstance(item.get("ts"), str) else None
        if ts is None:
            continue
        if source == "switchboard":
            name, event, detail = item.get("subject"), item.get("event"), item.get("detail") if isinstance(item.get("detail"), dict) else {}
            if not isinstance(name, str) or not name:
                continue
            if event == "cloud.created":
                created[name] = {"at": ts, "deadline": sb.parse_iso(detail.get("hard_deadline")) if isinstance(detail.get("hard_deadline"), str) else None,
                                 "line": raw}
            elif event == "cloud.stopped":
                stop[name] = max(ts, stop.get(name, 0))
            elif event == "cloud.create_failed":
                state.setdefault("refused", {})["%s@%s" % (name, item["ts"])] = {"name": name, "at": ts, "line": raw}
        else:
            name, action, ok = item.get("worker"), item.get("action"), item.get("ok")
            if item.get("runtime_id") == REAPER_ID and ok in (True, False):
                state.setdefault("reaper_new", []).append([ts, ok, _clean(item.get("reason") or action or "", 120)])   # settled by `_reaper` in this pass
            elif isinstance(name, str) and name and ok is True:
                if action == "stop":
                    stop[name] = max(ts, stop.get(name, 0))
                elif action == "report":
                    report[name] = max(ts, report.get(name, 0))
    for table in ("created", "stop", "report", "refused"):
        data = state.get(table, {})
        while len(data) > SEEN_MAX:
            data.pop(min(data, key=lambda k: data[k]["at"] if isinstance(data[k], dict) else data[k]))


EPOCH_MAX = 4102444800        # a time in a memory record is a finite number of seconds in 0..EPOCH_MAX
COUNT_MAX = 1000000


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and 0 <= value <= EPOCH_MAX


def _valid_record(rec, outage):
    """The cleaned `reaper_outage` / `reaper_gap` record, or None when it is malformed (the one place the record's fields are checked)."""
    if not isinstance(rec, dict) or not _num(rec.get("first")) or not _num(rec.get("latest")):
        return None
    count = rec.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= COUNT_MAX or not isinstance(rec.get("reason"), str):
        return None
    out = {"first": rec["first"], "latest": rec["latest"], "count": count, "reason": _clean(rec["reason"], 120)}
    if outage:
        if not isinstance(rec.get("sent"), bool):
            return None
        out["sent"] = rec["sent"]
        out["closed_by"] = rec.get("closed_by") if _num(rec.get("closed_by")) else None
    return out


def _table(valid):
    """A cleaner for a table: every entry whose key is a string and whose value `valid` accepts is kept, the rest dropped; a non-table is dropped whole."""
    def clean(value):
        if not isinstance(value, dict):
            return None
        return {k: v for k, v in value.items() if isinstance(k, str) and valid(v)}
    return clean


def _is_text(value, limit=100000):
    return isinstance(value, str) and len(value) <= limit


def _c_offsets(value):
    if not isinstance(value, dict):
        return None
    return {k: v for k, v in value.items() if k in ("switchboard", "launcher") and isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1 << 62}


def _c_new(value):
    if not isinstance(value, list):
        return None
    return [e for e in value if isinstance(e, list) and len(e) == 3 and _num(e[0]) and isinstance(e[1], bool) and _is_text(e[2])] or None


def _c_outage(value):
    return _valid_record(value, True)


def _c_gap(value):
    return _valid_record(value, False)


def _c_num(value):
    return value if _num(value) else None


def _c_flag(value):
    return value if isinstance(value, bool) else None


def _c_never(value):
    return None   # a legacy key (the old per-failure `reaper` table): nothing reads it, so it is dropped


# EVERY key of the memory the code reads or writes has its cleaner here, the one place a loaded memory is checked (a key not listed is dropped).
# The test derives the keys the module reads from the code and fails for one that is not listed.
_CLEANERS = {
    "offsets": _c_offsets,
    "rotations": _table(lambda v: isinstance(v, dict) and isinstance(v.get("applied"), int) and not isinstance(v.get("applied"), bool) and v["applied"] >= 0 and _valid_chain(v.get("chain"))),
    "alerted": _table(lambda v: isinstance(v, dict) and _num(v.get("at")) and _is_text(v.get("ask"))),
    "created": _table(lambda v: isinstance(v, dict) and _num(v.get("at")) and (v.get("deadline") is None or _num(v.get("deadline"))) and _is_text(v.get("line"))),
    "stop": _table(_num),
    "report": _table(_num),
    "refused": _table(lambda v: isinstance(v, dict) and _is_text(v.get("name")) and _num(v.get("at")) and _is_text(v.get("line"))),
    "seen": _table(lambda v: isinstance(v, dict) and _num(v.get("at")) and _is_text(v.get("ask")) and (v.get("lane") is None or _is_text(v.get("lane")))),
    "gone": _table(_num),
    "reaper": _c_never,
    "reaper_ok": _c_num,
    "reaper_outage": _c_outage,
    "reaper_gap": _c_gap,
    "reaper_new": _c_new,
    "catchup": _c_flag,
}


def _sanitize(state):
    """Malformed memory fails closed: every key is cleaned by its cleaner (a wrong type is dropped, a table is cleaned entry by entry) and the pass goes
    on (it never raises, so the detector is never wedged by one file). An unknown key is dropped."""
    for key in list(state):
        if key == "_gap_ready":
            continue
        cleaner = _CLEANERS.get(key)
        value = cleaner(state[key]) if cleaner else None
        if value is None:
            state.pop(key)
        else:
            state[key] = value


def _reaper(state, now):
    """Settle the reaper lines read this pass (closed rule: a failed line younger than RECENT_SECONDS joins the outage; an older one joins the gap
    summary and is never an alert of its own; an ok line closes the outage). Limits: a late line older than the window is summed, not alerted. A malformed
    record is dropped (fails closed: no alert from it)."""
    _sanitize(state)
    events = sorted((e for e in state.pop("reaper_new", []) if isinstance(e, list) and len(e) == 3 and _num(e[0])), key=lambda e: e[0])
    for ts, ok, reason in events:
        outage = state.get("reaper_outage")
        if ok:
            state["reaper_ok"] = max(ts, state.get("reaper_ok", 0))
            if isinstance(outage, dict) and outage.get("closed_by") is None and ts >= outage.get("latest", 0):
                outage["closed_by"] = ts
        elif ts < now - RECENT_SECONDS:
            gap = state.get("reaper_gap")
            if not isinstance(gap, dict):
                gap = state["reaper_gap"] = {"count": 0, "first": ts, "latest": ts, "reason": reason}
            gap["count"] += 1
            gap["first"] = min(gap["first"], ts)
            if ts >= gap["latest"]:
                gap["latest"], gap["reason"] = ts, reason
        elif isinstance(outage, dict) and (outage.get("closed_by") is None or ts <= outage["closed_by"]):
            outage["count"] += 1
            outage["first"] = min(outage["first"], ts)
            if ts >= outage["latest"]:
                outage["latest"], outage["reason"] = ts, reason
        else:
            state["reaper_outage"] = {"first": ts, "latest": ts, "count": 1, "reason": reason, "sent": False, "closed_by": None}


def _at_end(state, path, key):
    if not os.path.lexists(path):
        return True   # a missing file has nothing to read
    try:
        return state.get("offsets", {}).get(key, 0) >= os.stat(path).st_size
    except OSError:
        return False   # a file that exists and cannot be read is not at its end


def _ask_of(name, record):
    for field in ("branch", "ask"):
        if isinstance(record.get(field), str) and record[field]:
            return _clean(record[field], 80)
    return re.sub(r"[-_.]?\d+$", "", name) or name


def _losses(state, sb, engine, alive, now, records):
    _sanitize(state)
    out = []
    for name, c in sorted(state.get("created", {}).items()):
        deadline = c.get("deadline")
        if deadline is None or now < deadline:
            continue
        if state.get("stop", {}).get(name, 0) >= c["at"] or state.get("report", {}).get(name, 0) >= c["at"]:
            continue
        record = records.get(name, {})
        out.append({"key": "pod_no_stop:%s:%s" % (name, c["at"]), "kind": "pod_no_stop", "name": name, "ask": _ask_of(name, record),
                    "at": c["at"], "lane": record.get("reports_to"), "line": c["line"]})
    for key, r in sorted(state.get("refused", {}).items()):
        record = records.get(r["name"], {})
        out.append({"key": "refused_launch:" + key, "kind": "refused_launch", "name": r["name"], "ask": _ask_of(r["name"], record),
                    "at": r["at"], "lane": record.get("reports_to"), "line": r["line"]})
    outage = state.get("reaper_outage")
    if isinstance(outage, dict) and not outage.get("sent"):
        out.append({"key": "reaper_missed:outage@%s" % outage["first"], "kind": "reaper_missed", "name": REAPER_ID, "ask": "-", "at": outage["latest"],
                    "lane": None, "outage": True,
                    "line": "%d failed reaper line(s), first %s, latest %s, latest reason %s" % (outage["count"], _iso(outage["first"]), _iso(outage["latest"]), outage["reason"])})
    gap = state.get("reaper_gap")
    if isinstance(gap, dict) and gap.get("count") and state.get("_gap_ready"):
        out.append({"key": "reaper_gap:%s:%s:%d" % (gap["first"], gap["latest"], gap["count"]), "kind": "reaper_missed", "name": REAPER_ID, "ask": "-",
                    "at": gap["latest"], "lane": None, "gap": True,
                    "text": "[lost worker] reaper: %d failed reaper line(s) while the detector was down or before this pass, first %s, latest %s, latest reason %s" % (
                        gap["count"], _iso(gap["first"]), _iso(gap["latest"]), gap["reason"])})
    seen = state.setdefault("seen", {})
    if alive is not None:
        for name, info in alive.items():
            state.get("gone", {}).pop(name, None)
            seen[name] = {"at": now, "lane": info.get("lane"), "ask": _clean(info.get("ask") or name, 80)}
        for name, s in sorted(seen.items()):
            if name in alive or name in state.get("gone", {}):
                continue
            if engine_retired(engine, sb, name):
                state.setdefault("gone", {})[name] = now
                continue
            out.append({"key": "local_worker:%s:%s" % (name, s["at"]), "kind": "local_worker", "name": name, "ask": s["ask"], "at": s["at"],
                        "lane": s["lane"], "line": "seen alive at %s, absent from discovery and not retired" % _iso(s["at"])})
        while len(seen) > SEEN_MAX:
            seen.pop(min(seen, key=lambda k: seen[k]["at"]))
    return out


def engine_retired(engine, sb, name):
    check = getattr(sb, "retired", None)
    try:
        return bool(check(engine, name)) if check else False
    except Exception:
        return False


def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def text_of(loss, earlier):
    repeat = ("repeat: %d earlier lost worker(s) of this ask" % earlier) if earlier else "first loss of this ask"
    kind = loss["kind"] + (" (possibly lost: absent from discovery, not known to be dead)" if loss["kind"] == "local_worker" else "")
    return "[lost worker] %s (ask %s) kind=%s, last event %s. Evidence: %s. %s." % (
        _name(loss["name"]), loss["ask"], kind, _iso(loss["at"]), _clean(loss["line"]), repeat)


def run_pass(sb, engine, now, alive=None, launcher_audit=None):
    """One pass. Never raises into the caller. Returns the alerts raised this pass (a list of dicts)."""
    if getattr(engine.audit, "path", None) is None:
        return []   # no audit file to read (a stub, or none): nothing to do, nothing created
    state_dir = os.path.dirname(str(engine.audit.path))
    path = os.path.join(state_dir, STATE_FILE)
    state, status = _load(sb, path)
    if status == "corrupt":
        # An unreadable memory: this pass alerts nothing and says so once. The file is set aside, so the NEXT pass finds no memory, reads afresh
        # and writes a new one (offsets at the end of what it read, alerted empty): a corrupt file never silences alerts for ever.
        try:
            if os.path.isdir(path):
                raise OSError("a directory is not set aside")   # S11b: a directory at the memory path is memory-only mode
            os.replace(path, path + ".corrupt")
        except OSError:
            try:
                os.unlink(path)
            except OSError:
                pass
        if os.path.lexists(path):
            # S11b: corrupt AND neither written nor set aside: memory-only mode at once, one audit line per process, alerts as a first pass now.
            if path not in _PROC_AUDITED:
                _PROC_AUDITED.add(path)
                engine.audit.append(engine.actor, "lost_worker.memory_corrupt", None, {"file": STATE_FILE, "action": "cannot be set aside; memory-only mode, alerting as a first pass"})
            status = "memory_only"
        else:
            engine.audit.append(engine.actor, "lost_worker.memory_corrupt", None, {"file": STATE_FILE, "action": "nothing alerted this pass; fresh memory next pass"})
            return []
    before = json.dumps(state, sort_keys=True)   # before the cleaning: a cleaned memory is written back
    raw_keys = set(state)
    _sanitize(state)   # before the first fold: a record of the wrong type never reaches the code that adds to it
    if status == "ok" and ("offsets" not in state or ("alerted" in raw_keys and "alerted" not in state)):
        state, status = {}, "none"   # no usable positions or no usable list of what was alerted: no usable memory (recent losses only, the older marked)
    first = status in ("none", "memory_only")
    catchup = first or state.get("catchup") is True   # a catch-up: no usable memory, and every later pass until both audits are read to their end
    _fold(state, sb, _read_new(sb, str(engine.audit.path), state, "switchboard"), "switchboard")
    _fold(state, sb, _read_new(sb, launcher_audit or launcher_audit_path(), state, "launcher"), "launcher")
    _reaper(state, now)
    at_end = _at_end(state, str(engine.audit.path), "switchboard") and _at_end(state, launcher_audit or launcher_audit_path(), "launcher")
    state["catchup"] = (first or state.get("catchup") is True) and not at_end   # set only on a pass with no usable memory; cleared only by a pass that reads both files to their end
    state["_gap_ready"] = at_end
    records = {}
    for r in (getattr(engine, "cloud_watch", None) or {}).get("records", []):
        if isinstance(r, dict) and isinstance(r.get("worker"), str):
            records[r["worker"]] = r
    alerted = state.setdefault("alerted", {})
    alerted.update({k: v for k, v in _PROC_ALERTED.get(path, {}).items() if k not in alerted})   # what this process already alerted is never skipped or repeated
    losses = [x for x in _losses(state, sb, engine, alive, now, records) if x["key"] not in alerted]
    alerted = state.setdefault("alerted", {})   # #2017: _losses cleaned the memory and replaced the table: the marks of this pass go in the live one
    losses.sort(key=lambda x: (x["at"], x["key"]))
    state.pop("_gap_ready", None)
    if catchup:
        # A catch-up: only losses of the last hour are alerted; the older ones are marked, never sent (a valid memory outside one alerts every loss since).
        old = [x for x in losses if x["at"] < now - RECENT_SECONDS and x["kind"] != "reaper_missed"]
        for x in old:
            alerted[x["key"]] = {"ask": x["ask"], "at": now, "skipped": True}
        losses = [x for x in losses if x["at"] >= now - RECENT_SECONDS or x["kind"] == "reaper_missed"]
    batch, rest = losses[:PASS_CAP], losses[PASS_CAP:]
    raised = []
    for loss in batch:
        earlier = sum(1 for a in alerted.values() if a.get("ask") == loss["ask"] and loss["ask"] != "-")
        alerted[loss["key"]] = {"ask": loss["ask"], "at": now}   # marked BEFORE the send: a send that fails is not tried again (once per loss)
        text = loss.get("text") or text_of(loss, earlier)
        if loss.get("gap"):
            state.pop("reaper_gap", None)   # summed once per gap: cleared as it is sent
        if loss.get("outage") and isinstance(state.get("reaper_outage"), dict):
            state["reaper_outage"]["sent"] = True
        _deliver(engine, sb, loss, [loss.get("lane"), "chief-of-staff"], text)
        raised.append(dict(loss, text=text, earlier=earlier))
    if rest:
        text = "[lost worker] %d more lost worker(s) found; they are alerted on the following passes, %d at a time." % (len(rest), PASS_CAP)
        _deliver(engine, sb, {"name": "roll-up", "kind": "roll_up"}, ["chief-of-staff"], text)
    while len(alerted) > ALERTED_MAX:
        alerted.pop(min(alerted, key=lambda k: alerted[k]["at"]))
    _PROC_ALERTED[path] = dict(alerted)   # the trimmed set (it already holds this process's earlier keys): capped like the persisted memory
    if status == "memory_only":
        return raised   # the memory file is not written in memory-only mode
    if first or json.dumps(state, sort_keys=True) != before:
        try:
            _save(path, state)
        except OSError as err:
            engine.audit.append(engine.actor, "lost_worker.state_not_saved", None, {"why": sb._one_line(str(err), 160)})
    return raised


def _deliver(engine, sb, loss, recipients, text):
    for recipient in dict.fromkeys([r for r in recipients if isinstance(r, str) and r]):
        try:
            outcome = engine.send("switchboard", recipient, text)
            if not outcome.get("sent"):
                raise RuntimeError("%s (%s)" % (outcome.get("action"), outcome.get("reason")))
        except Exception as err:
            engine.audit.append(engine.actor, "lost_worker.alert_not_sent", _name(loss["name"]),
                                {"to": recipient, "kind": loss["kind"], "why": sb._one_line(str(err), 160)})
