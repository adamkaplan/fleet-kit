#!/usr/bin/env python3
"""decisions_pull: rewrite the local decisions file from a remote source (slice 4a of the plan on #1059, ask #1078).

The Decisions plugin reads $FLEET_SWITCHBOARD_STATE/decisions.json (plugins/fleet-decisions-tui/decisions.mjs, `loadSnapshot`). In the cloud the
daemon keeps the list elsewhere; this program fetches it and writes the same file, so the plugin is unchanged.

Freshness (#1158). `written_at` is NOT a liveness signal: when the list is unchanged LocalStore.publish only touches the file's mtime, so an
idle healthy daemon has an old `written_at`; and the generation restarts at 1 on a fresh volume. The liveness is the AGE of the source file,
measured on the source's own clock, and it arrives in ONE of two ways. The daemon and the plugin are unchanged.

  * A hub source (the hub's GET /decisions; no `--age-command`): the response is the decisions body plus one top-level integer `age_seconds`,
    so body and age come from ONE read (`fetch` runs exactly once) and cannot race. It is taken out of the body before writing (the written
    file never has it). It must be an integer (not a bool, float or string), 0 to a day; anything else, or none, is an unreachable source.
    The hub's age is the decisions file's MTIME on the hub: a backup, copy or remount that touches the mtime makes an old list read fresh.
    The hub answers 503 (read here as unreachable) for an absent or unreadable file, or an mtime more than 60 s in its future.
  * A plain-file source (no hub: a path, or a command that cats a file): the age comes from a second command, `--age-command` (one
    non-negative integer of seconds). When `--age-command` is given the source is a plain file: a top-level `age_seconds` in its body is
    only the file's own content, is dropped, never believed, and the command is run (one line on stderr says so).

  * A good body (an object with `decisions` and `errors` lists) with an age of at most 180 s is written as the daemon writes it (same
    keys, indent 1, sorted keys, a final newline, mode 0600), to a temporary file and renamed over the old one, with the mtime set to now
    minus the age (the plugin's age is the mtime).
  * The source unreachable, or its age (either way) missing, negative, not an integer, over a day, or over 180 s: the file is kept but its mtime is moved
    past the plugin's stale limit, so the panel says "daemon not updating".
  * Garbage (not JSON, not the shape, a generation outside 0 to 2**31 - 1): the file is not touched at all.
  * Order when the answer is bad: a missing age wins over garbage (garbage with no usable age is "marked-stale", not "untouched"); garbage
    with a good age leaves the file untouched.
  * `written_at` is only a plausibility check: more than 60 s in the future of this clock, the body is not trusted (file aged). Old is fine.
    Accepted formats: ISO 8601 with a `Z` or a numeric UTC offset (`+00:00`, `+02:00`), fractions of a second allowed; a naive stamp is
    read as UTC. A value that parses in none of these is ignored (it is no liveness signal).
  * A lower generation is accepted when `daemon_pid` differs from the held file's (a restarted daemon); with the same pid it is a stale replay
    and is refused (file aged). TRUST MODEL: the source is trusted for generation order. A body with no `daemon_pid` is accepted at a lower
    generation (no pid proves no replay), and a source that changes its `daemon_pid` defeats the ordering by design; this guards against an
    honest stale replay, not a hostile source. The check and the write run under one flock on the state directory,
    so two pulls cannot interleave and let a lower generation overwrite a higher one. Recovery: `--reset-generation` accepts any in-bound generation once; deleting decisions.json does the same.

The source is a command that prints the file (for example `kubectl exec switchboard-0 -- cat <state>/decisions.json`), given after `--`.
No key or token is read, stored or printed here; the command's own credentials are the caller's.

A hub source (a command that GETs /decisions with the caller's own credentials and prints the response):

    bin/decisions_pull.py --state DIR -- COMMAND-THAT-PRINTS-THE-HUB-RESPONSE

A plain-file source:

    bin/decisions_pull.py --state DIR --age-command "kubectl exec switchboard-0 -- stat -c %Y /state/decisions.json" -- kubectl exec switchboard-0 -- cat /state/decisions.json
(an age command prints the age in seconds, for example `echo $(( $(date +%s) - $(stat -c %Y FILE) ))` run on the source side.)
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

DECISIONS_FILE = "decisions.json"
STALE_SECONDS = 180                  # the plugin's STALE_MS / 1000
FETCH_TIMEOUT = 30
MAX_BYTES = 8 * 1024 * 1024          # a larger body is garbage, not a decisions list
MAX_DEPTH = 32                       # nesting deeper than this is garbage (the daemon's list is a few levels)
MAX_ITEMS = 10000                    # decisions or errors beyond this: garbage
MAX_AGE_SECONDS = 86400              # an age above a day is not believed: unreachable
FUTURE_TOLERANCE = 60                # written_at further ahead of this clock than this is not trusted
MAX_GENERATION = 2 ** 31 - 1         # a larger generation is garbage (it could block every later pull)
KEPT_KEYS = ("generation", "written_at", "daemon_pid", "decisions", "errors", "provisional")

WROTE, STALED, UNTOUCHED = "wrote", "marked-stale", "untouched"


class Unreachable(Exception):
    """The source could not be read (down, timed out, non-zero exit)."""


class Garbage(Exception):
    """The source answered, but not with the daemon's decisions list."""


def kill_group(proc):
    """Kill the child's whole process group (it leads its own session), so a grandchild holding the pipe cannot outlive the deadline."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError, TypeError):
        proc.kill()


def command_fetch(argv, timeout=FETCH_TIMEOUT, popen=subprocess.Popen):
    """The source text of a command's standard output, at most MAX_BYTES+1 bytes (the process is killed past that: the extra byte makes
    parse() call it garbage). A start failure, a timeout or a non-zero exit is Unreachable."""
    try:
        proc = popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    except (OSError, subprocess.SubprocessError) as err:
        raise Unreachable(type(err).__name__)
    expired = threading.Event()

    def expire():
        expired.set()
        kill_group(proc)
    timer = threading.Timer(timeout, expire)
    timer.start()
    try:
        data = proc.stdout.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            kill_group(proc)
            proc.wait()
            return data
        code = proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            kill_group(proc)
        proc.stdout.close()
    if expired.is_set():
        raise Unreachable("timed out")
    if code != 0:
        raise Unreachable("exit %s" % code)
    return data


def _no_constant(name):
    raise ValueError("not JSON: %s" % name)


def parse(raw):
    """The daemon's file as a dict; Garbage (with the reason, never the body) for anything else."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if not isinstance(raw, (bytes, bytearray)):
        raise Garbage("not bytes")
    if len(raw) > MAX_BYTES:
        raise Garbage("larger than %d bytes" % MAX_BYTES)
    try:
        body = json.loads(bytes(raw).decode("utf-8"), parse_constant=_no_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise Garbage("not JSON")
    if not (isinstance(body, dict) and isinstance(body.get("decisions"), list) and isinstance(body.get("errors"), list)):
        raise Garbage("not an object with decisions and errors lists")
    if len(body["decisions"]) > MAX_ITEMS or len(body["errors"]) > MAX_ITEMS:
        raise Garbage("more than %d items" % MAX_ITEMS)
    if not all(isinstance(d, dict) for d in body["decisions"]):
        raise Garbage("a decision is not an object")
    if not all(isinstance(e, (str, dict)) for e in body["errors"]):
        raise Garbage("an error is not a string or an object")
    if "generation" in body and not in_bound(body["generation"]):
        raise Garbage("generation outside 0 to %d" % MAX_GENERATION)
    try:
        return clean(body)
    except RecursionError:
        raise Garbage("nested too deeply")


def in_bound(generation):
    return isinstance(generation, int) and not isinstance(generation, bool) and 0 <= generation <= MAX_GENERATION


def parse_age(raw):
    """The supplied age in whole seconds; Unreachable when missing, not an integer, negative or over a day."""
    if raw is None:
        raise Unreachable("no age supplied")
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("ascii", "replace")
    text = str(raw).strip()
    if not re.fullmatch(r"[0-9]{1,12}", text):
        raise Unreachable("age is not a non-negative integer")
    age = int(text)
    if age > MAX_AGE_SECONDS:
        raise Unreachable("age over a day")
    return age


def response_age(value):
    """The age a hub response carries in `age_seconds`: a true integer (never a bool, float or string), 0 to a day; else Unreachable."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise Unreachable("age_seconds is not an integer")
    return parse_age(str(value))


def same_daemon(held, body):
    """True only when both bodies carry the same usable pid (an integer, not a bool): a missing or odd pid proves no replay."""
    pid = held.get("daemon_pid")
    return isinstance(pid, int) and not isinstance(pid, bool) and body.get("daemon_pid") == pid


def written_at_in_future(body, now):
    try:
        text = body["written_at"].strip()
        if text[-1:] in ("Z", "z"):
            text = text[:-1] + "+00:00"
        text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)   # any fraction length
        when = datetime.datetime.fromisoformat(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        stamp = when.timestamp()
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError, OSError):
        return False        # absent or odd: not a reason to refuse (it is no liveness signal)
    return stamp > now + FUTURE_TOLERANCE


def held_body(path):
    """The held file's body, or None when missing, unreadable, not a decisions file, or its generation is out of bound."""
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict) or not in_bound(body.get("generation")):
        return None
    return body


_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")


def clean(value, depth=0):
    """Every string in `value` (keys too) without control characters (escape sequences reach the terminal otherwise). Garbage when nested
    deeper than MAX_DEPTH or when two keys become one after stripping."""
    if isinstance(value, str):
        return _CONTROL.sub("", value)
    if isinstance(value, (list, dict)) and depth >= MAX_DEPTH:
        raise Garbage("nested deeper than %d" % MAX_DEPTH)
    if isinstance(value, list):
        return [clean(v, depth + 1) for v in value]
    if isinstance(value, dict):
        out = {clean(k): clean(v, depth + 1) for k, v in value.items()}
        if len(out) != len(value):
            raise Garbage("two keys are the same after cleaning")
        return out
    return value


def _iso(now):
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token():
    return secrets.token_hex(8)


def _open_new(temp):
    """Create `temp` exclusively (never through a symlink, mode 0600); on a name clash unlink what is there and retry once."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        return os.open(str(temp), flags, 0o600)
    except FileExistsError:
        os.unlink(str(temp))
        return os.open(str(temp), flags, 0o600)


def write_atomic(path, body, now, written=None):
    body = {k: body[k] for k in KEPT_KEYS if k in body}
    body.setdefault("written_at", _iso(now if written is None else written))
    temp = path.with_name("%s.%d.%s.pull.tmp" % (DECISIONS_FILE, os.getpid(), _token()))
    fd = _open_new(temp)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(body, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.utime(str(temp), (now, now))
        os.replace(str(temp), str(path))
        dir_fd = os.open(str(path.parent), os.O_RDONLY)   # the rename itself must survive a crash
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise


def mark_stale(path, now):
    """Age the file past the plugin's limit (never younger than it is); a missing file stays missing. Stat and utime go through one open
    descriptor, so a write_atomic that renames a new file in meanwhile is not aged: only the old inode is."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        older = min(os.fstat(fd).st_mtime, now - STALE_SECONDS - 1)
        os.utime(fd, (older, older))
    finally:
        os.close(fd)
    return True


def say(err, text):
    """One line on `err`: the reason, never the source's body."""
    print("decisions_pull: %s" % text, file=err)


def pull(state_dir, fetch, now=None, err=None, age=None, reset_generation=False):
    """One pull. `fetch()` returns the source's bytes or raises Unreachable; `age()` returns the source file's age in seconds (an integer, or
    its text) or raises Unreachable; no `age` is a missing age. Returns WROTE, STALED or UNTOUCHED; says why on `err` otherwise."""
    now = time.time() if now is None else now
    err = sys.stderr if err is None else err
    path = Path(state_dir) / DECISIONS_FILE

    def stale(reason):
        if mark_stale(path, now):
            say(err, "%s; marked %s stale" % (reason, DECISIONS_FILE))
            return STALED
        say(err, "%s; no %s to mark stale" % (reason, DECISIONS_FILE))
        return UNTOUCHED
    try:
        raw = fetch()
    except Unreachable as why:
        return stale("source unreachable (%s)" % why)
    garbage, body = None, None
    try:
        body = parse(raw)
    except Garbage as why:
        garbage = why
    try:
        if age is not None:                                  # a plain-file source: the age comes from the second command
            if body is not None and "age_seconds" in body:
                body.pop("age_seconds")
                say(err, "age_seconds in the body of a plain-file source is ignored; --age-command is used")
            seconds = parse_age(age())
        elif body is not None and "age_seconds" in body:     # a hub response: body and age from one read
            seconds = response_age(body.pop("age_seconds"))
        else:
            seconds = parse_age(None)
    except Unreachable as why:
        return stale("source unreachable (%s)" % why)
    if garbage is not None:
        why = garbage
        say(err, "source answered something that is not the daemon's decisions list (%s); %s untouched" % (why, DECISIONS_FILE))
        return UNTOUCHED
    if seconds > STALE_SECONDS:
        return stale("source file is %d s old (limit %d)" % (seconds, STALE_SECONDS))
    if written_at_in_future(body, now):
        return stale("written_at is in the future; body not trusted")
    lock = os.open(str(path.parent), os.O_RDONLY)             # the state directory itself is the lock: no extra file
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)                     # the check and the write are one step
        held = held_body(path)
        new = body.get("generation")
        if held is not None and new is not None and new < held["generation"] and not reset_generation and same_daemon(held, body):
            return stale("generation %d is lower than the held %d from the same daemon (a stale replay); to accept it once run with "
                         "--reset-generation, or delete %s" % (new, held["generation"], DECISIONS_FILE))
        write_atomic(path, body, now - seconds, now)
    finally:
        os.close(lock)                                       # closing releases the flock
    return WROTE


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", default=os.environ.get("FLEET_SWITCHBOARD_STATE"), help="the state directory the plugin reads")
    parser.add_argument("--age-command", help="plain-file source only: a command (one string) that prints the source file's age in whole seconds (a hub response carries its own age_seconds, which wins)")
    parser.add_argument("--reset-generation", action="store_true", help="accept any in-bound generation once (recovery after a restart)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="after --: the command that prints the remote decisions.json")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not args.state or not command:
        parser.error("--state (or FLEET_SWITCHBOARD_STATE) and a source command after -- are required")
    age = None
    if args.age_command:
        age_argv = shlex.split(args.age_command)
        age = lambda: command_fetch(age_argv)
    result = pull(args.state, lambda: command_fetch(command), age=age, reset_generation=args.reset_generation)
    print(result)
    return 0 if result == WROTE else 1


if __name__ == "__main__":
    sys.exit(main())
