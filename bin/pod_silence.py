"""A running cloud pod that pushed nothing and reported nothing for 20 minutes tells its boss ONE short summary (ask #2115, slice 3).

Pure of the daemon: the live workers, the clock, the state path, the send function and the summary function are handed in. The summary comes from
`summary(worker)`, the one line of the switchboard's `inspect NAME --short` (a stub in tests); an empty or failing summary still sends the line 'state unreadable: <reason>' and writes ONE audit line (`audit(worker, detail)`), once per quiet period.
A worker is silent when `now - max(first seen, last report, last push)` reaches QUIET seconds. One message per worker and quiet period: the worker is
written to the state file BEFORE the message goes, and is told again only after a later report or push. A worker that reported `done`, or is no longer
live (finished or retired), sends nothing and leaves the state. The state is one private file (0600): {"workers": {name: {"seen": epoch, "told": epoch|null}}}.
"""
import json
import os

QUIET = 20 * 60
STATE_FILE = "pod-silence.json"
READ_BYTES = 1 << 20
TEXT_MAX = 400


def _load(path, parse):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            data = parse(handle.read(READ_BYTES))
    except (OSError, ValueError):
        return {}
    work = data.get("workers") if isinstance(data, dict) else None
    return {k: v for k, v in work.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(work, dict) else {}


def _save(path, workers):
    line = json.dumps({"workers": workers}) + "\n"
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            if handle.read(len(line) + 1) == line:
                return   # nothing changed: an idle pass does not rewrite the file (TestWriteOnlyOnChange)
    except (OSError, ValueError):
        pass
    temp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(line)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def message_text(worker, minutes, line):
    one = " ".join(str(line).split())[:TEXT_MAX]
    return "[cloud worker quiet] %s has pushed nothing and sent no report for %d minutes. Its state: %s" % (worker, minutes, one)


def scan(workers, now, state_path, send, summary, parse, quiet=QUIET, prune=True, audit=None):
    """`workers`: [{"worker", "boss", "last_report" epoch|None, "last_push" epoch|None, "done" bool}] of the LIVE pods. Returns [(boss, text)] handed to send.
    `prune` False (a capped read: the pass did not see every pod) keeps the state of the pods not handed in, so an unread pod keeps its told mark."""
    state = _load(state_path, parse)
    live = {w.get("worker") for w in workers if isinstance(w, dict)}
    if prune:
        state = {k: v for k, v in state.items() if k in live}   # a pod that finished or was retired leaves the state
    due = []
    for w in workers:
        name, boss = w.get("worker"), w.get("boss")
        if not isinstance(name, str) or not isinstance(boss, str) or not boss or w.get("done"):
            continue
        mem = state.setdefault(name, {"seen": now, "told": None})
        active = max(_num(mem.get("seen")), _num(w.get("last_report")), _num(w.get("last_push")))
        told = mem.get("told")
        if told is not None and active > _num(told):
            mem["told"] = told = None   # a report or a push after the message: a new quiet period
        if told is not None or now - active < quiet:
            continue
        reason = None
        try:
            line = summary(name)
        except Exception as err:
            line, reason = None, " ".join(str(err).split())[:120] or type(err).__name__
        if not isinstance(line, str) or not line.strip():
            line = "state unreadable: %s" % (reason or "no summary came back")   # never skip silently: the boss hears it, once, and one audit line says so
            if audit is not None:
                try:
                    audit(name, {"reason": line[:200]})
                except Exception:
                    pass
        mem["told"] = now
        due.append((boss, message_text(name, int((now - active) // 60), line)))
    _save(state_path, state)   # saved BEFORE the messages go: a crash loses a message, it never sends one twice
    for boss, text in due:
        try:
            send(boss, text)
        except Exception:
            pass
    return due
