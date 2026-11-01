"""`issue-line`: change ONE line of an issue body and nothing else (ask #1751).

A lane that wrote a whole body from an empty read left an ask with no Intent and no Done-when. This command changes only the next-step line or the
`Parked until:` line, from a fresh read, and refuses (one plain line, exit 1, nothing written) when the body it read is empty, unread, or lacks the
`## Intent` heading line or the `Done when:` line.

Pure of the daemon: the functions here take text and return text or a one-line refusal; they read and write no file and call no GitHub. `run` is
handed everything it touches in a namespace `sb` (the engine's reader and audit, the writer of an issue body, the state directory, the file calls
of the copy as `sb.fs`, the shared bounded parse, the clock). `bin/fleet-switchboard` registers the subcommand and builds that namespace.

The last good copy (PC-5) is `<state>/issue-line/<hash of repo#N>.json` holding {"intent": true, "done_when": true, "at": <epoch>} and no text.
"""

import hashlib
import re
from datetime import datetime, timedelta, timezone

INTENT_RE = re.compile(r"^##[ \t]+Intent[ \t]*\r?$", re.M)
DONE_RE = re.compile(r"^[ \t]*Done when:", re.M)
NEXT_RE = re.compile(r"^(?:Next step(?:[ \t]*\([^)\n]*\))?|Next)[ \t]*:")
PARKED_RE = re.compile(r"^[ \t]*Parked until:")
MARKER_RE = re.compile(r"^<!--.*-->[ \t]*\r?$")
HEADING_RE = re.compile(r"^#{1,6}[ \t]")
TEXT_MAX = 600
PARKED_MAX_DAYS = 366
DIR_NAME = "issue-line"
MAX_COPIES = 2000
COPY_MAX_BYTES = 512
COPY_KEYS = ("intent", "done_when", "at")


class Refused(Exception):
    """A refusal: one plain line."""


def check_arguments(kind, value, now):
    """None when the argument is fit to write, else the refusal line. `kind` is "next" or "parked"; `now` is the epoch second."""
    if kind == "next":
        if not isinstance(value, str) or not value.strip():
            return "the next-step text is empty"
        if "\n" in value or "\r" in value or "\x00" in value:
            return "the next-step text has a line break"
        if len(value) > TEXT_MAX:
            return "the next-step text is %d characters, over %d" % (len(value), TEXT_MAX)
        return None
    if kind == "parked":
        try:
            day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc) if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value or "") else None
        except ValueError:
            day = None
        if day is None:
            return "the date is not a real day (write YYYY-MM-DD)"
        today = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if day <= today:
            return "the date is not in the future"
        if day > today + timedelta(days=PARKED_MAX_DAYS):
            return "the date is more than %d days ahead" % PARKED_MAX_DAYS
        return None
    return "no line is named"


def parts(body):
    """(has the Intent heading line, has the Done-when line) of a body."""
    text = body if isinstance(body, str) else ""
    return bool(INTENT_RE.search(text)), bool(DONE_RE.search(text))


def body_problem(body):
    """None when the body read is an ask body to edit, else why it is not."""
    if not isinstance(body, str) or not body.strip():
        return "the body read is empty"
    has_intent, has_done = parts(body)
    if not has_intent:
        return "the body read has no `## Intent` heading line"
    if not has_done:
        return "the body read has no `Done when:` line"
    return None


def _intent_span(lines):
    """The index range [start, end) of the Intent section (its heading to the next heading), or (0, 0)."""
    for n, line in enumerate(lines):
        if INTENT_RE.match(line):
            for m in range(n + 1, len(lines)):
                if HEADING_RE.match(lines[m]):
                    return n, m
            return n, len(lines)
    return 0, 0


def _ending(line):
    return "\r\n" if line.endswith("\r\n") else "\n"


def edit(body, kind, value, now):
    """`body` with its one line changed: the LAST `Next step (...):` or `Next:` line (kind "next") or the `Parked until:` line (kind "parked") replaced,
    or the line appended after the last paragraph and before a trailing marker line. Every other byte is unchanged."""
    if kind == "next":
        new, pattern = "Next: %s" % value.strip(), NEXT_RE
    else:
        new, pattern = "Parked until: %s" % value, PARKED_RE
    lines = body.splitlines(keepends=True)
    start, end = _intent_span(lines)
    found = [n for n, line in enumerate(lines) if pattern.match(line) and not start <= n < end]
    if found:
        n = found[-1]
        ending = _ending(lines[n]) if lines[n].endswith("\n") else ""
        lines[n] = new + ending
        return "".join(lines)
    last = max((n for n, line in enumerate(lines) if line.strip()), default=-1)
    cut = last + 1
    if last >= 0 and MARKER_RE.match(lines[last].rstrip("\r\n")):
        before = max((n for n, line in enumerate(lines[:last]) if line.strip()), default=-1)
        cut = before + 1
    ending = _ending(lines[0]) if lines and lines[0].endswith("\r\n") else "\n"
    if cut and not lines[cut - 1].endswith("\n"):
        lines[cut - 1] += ending
    lines[cut:cut] = [ending, new + ending]
    return "".join(lines)


# ---- the last good copy: every file call goes through `fs` (the os module, handed in)

def copy_path(sb, repo, number):
    key = hashlib.sha256(("%s#%d" % (repo, number)).encode("utf-8")).hexdigest()[:24]
    return sb.fs.path.join(str(sb.state), DIR_NAME, key + ".json")


def read_copy(sb, repo, number):
    """None for a MISSING copy (a first use); the record {"intent", "done_when"} for a good one; raises Refused for one that cannot be read as the record."""
    path = copy_path(sb, repo, number)
    try:
        fd = sb.fs.open(path, sb.fs.O_RDONLY)
    except FileNotFoundError:
        return None
    except OSError as err:
        raise Refused("the last good copy cannot be read (%s)" % (err.strerror or "error")) from None
    try:
        raw = sb.fs.read(fd, COPY_MAX_BYTES + 1)
    except OSError as err:
        raise Refused("the last good copy cannot be read (%s)" % (err.strerror or "error")) from None
    finally:
        sb.fs.close(fd)
    if len(raw) > COPY_MAX_BYTES:
        raise Refused("the last good copy is oversized")
    try:
        record = sb.parse_bounded(raw.decode("utf-8"))
    except ValueError:
        raise Refused("the last good copy is corrupt") from None
    if (not isinstance(record, dict) or set(record) != set(COPY_KEYS) or record["intent"] is not True or record["done_when"] is not True
            or isinstance(record["at"], bool) or not isinstance(record["at"], (int, float))):
        raise Refused("the last good copy is corrupt")
    return record


def write_copy(sb, repo, number):
    """Write the copy (temp file, then replace); evict the oldest beyond MAX_COPIES. True when it was written."""
    fs, directory = sb.fs, sb.fs.path.join(str(sb.state), DIR_NAME)
    path = copy_path(sb, repo, number)
    tmp = "%s.%d.tmp" % (path, fs.getpid())
    try:
        fs.makedirs(directory, mode=0o700, exist_ok=True)
        fs.chmod(directory, 0o700)
        fd = fs.open(tmp, fs.O_WRONLY | fs.O_CREAT | fs.O_EXCL, 0o600)
        try:
            fs.write(fd, ('{"intent": true, "done_when": true, "at": %d}' % int(sb.now())).encode("ascii"))
        finally:
            fs.close(fd)
        fs.replace(tmp, path)
        names = [n for n in fs.listdir(directory) if n.endswith(".json")]
        if len(names) > MAX_COPIES:
            aged = sorted(names, key=lambda n: (fs.stat(fs.path.join(directory, n)).st_mtime, n))
            for name in aged[:len(names) - MAX_COPIES]:
                fs.unlink(fs.path.join(directory, name))
        return True
    except OSError:
        try:
            fs.unlink(tmp)
        except OSError:
            pass
        return False


# ---- the command

def _stop(sb, repo, number, reason, text):
    sb.engine.audit.append(sb.engine.actor, "issue_line.refused", "issue:%s#%s" % (repo, number), {"repo": repo, "number": number, "reason": reason})
    sb.err("%s: issue-line: %s: nothing written\n" % (sb.PROG, text))
    return sb.EXIT_ERROR


def _read(sb, repo, number):
    data = sb.reader._api("repos/%s/issues/%d" % (repo, number), repo)
    return data.get("body") if isinstance(data, dict) and isinstance(data.get("body"), str) else ""


def run(sb, repo, number, next_step, parked):
    """Change the one line of `repo#number`: `next_step` or `parked`, exactly one. Exit code: 0 written; 1 refused, failed or restored (one plain line on stderr)."""
    sb.engine = sb.build_engine()
    sb.reader = sb.engine.intents or sb.make_reader()
    sb.state = sb.engine.state_dir
    sb.now = sb.engine.now
    kind, value = ("next", next_step) if parked is None else ("parked", parked)
    if (next_step is None) == (parked is None):
        bad = "give exactly one of --next-step and --parked"
    else:
        bad = check_arguments(kind, value, sb.now()) if repo else "name the issue as owner/repo#N"
    if bad:
        return _stop(sb, repo, number, "arguments", bad)
    reader = sb.reader
    try:
        body = _read(sb, repo, number)
    except sb.IntentError as err:
        return _stop(sb, repo, number, "read", "the issue could not be read (%s)" % sb.one_line(str(err), 120))
    try:
        copy = read_copy(sb, repo, number)
    except Refused as err:
        return _stop(sb, repo, number, "copy", str(err))
    problem = body_problem(body)
    if problem:
        said = " (the last good read held both parts)" if copy is not None else ""
        return _stop(sb, repo, number, "body", problem + said)
    saved = write_copy(sb, repo, number)
    new_body = edit(body, kind, value, sb.now())
    if new_body == body:
        sb.out("%s#%d: the line is already so; nothing written\n" % (repo, number))
        return sb.EXIT_OK
    try:
        if _read(sb, repo, number) != body:
            return _stop(sb, repo, number, "moved", "#%d changed while this edit was prepared; run it again" % number)
    except sb.IntentError as err:
        return _stop(sb, repo, number, "read", "the issue could not be read again (%s)" % sb.one_line(str(err), 120))
    detail = {"repo": repo, "number": number, "line": kind, "copy": saved}
    try:
        with sb.engine._external("issue_line.written", "issue:%s#%d" % (repo, number), detail):
            sb.write(reader, repo, number, new_body)
    except sb.IntentError as err:
        return _stop(sb, repo, number, "write", "the write failed (%s)" % sb.one_line(str(err), 120))
    for wait in sb.waits:
        sb.sleep(wait)
        try:
            back = _read(sb, repo, number)
        except sb.IntentError:
            continue
        if parts(back) != (True, True):
            try:
                sb.write(reader, repo, number, body)
            except sb.IntentError:
                pass
            sb.engine.audit.append(sb.engine.actor, "issue_line.restored", "issue:%s#%d" % (repo, number), {"repo": repo, "number": number, "reason": "readback"})
            sb.err("%s: issue-line: #%d read back without its Intent or Done-when: the body read before the edit was written back once; check the issue\n" % (sb.PROG, number))
            return sb.EXIT_ERROR
        break
    if not saved:
        sb.err("%s: issue-line: written, but the last good copy could not be saved\n" % sb.PROG)
    sb.out("%s#%d: %s line written\n" % (repo, number, "next-step" if kind == "next" else "parked"))
    return sb.EXIT_OK
