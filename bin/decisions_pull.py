#!/usr/bin/env python3
"""decisions_pull: rewrite the local decisions file from a remote source (slice 4a of the plan on #1059, ask #1078).

The Decisions plugin reads $FLEET_SWITCHBOARD_STATE/decisions.json (plugins/fleet-decisions-tui/decisions.mjs, `loadSnapshot`). In the cloud the
daemon keeps the list elsewhere; this program fetches it and writes the same file, so the plugin is unchanged.

  * A good body (an object with `decisions` and `errors` lists) is written as the daemon writes it (same keys, indent 1, sorted keys, a
    final newline, mode 0600), to a temporary file and renamed over the old one, with the mtime set to now (the plugin's age is the mtime).
  * The source unreachable: the file is kept but its mtime is moved past the plugin's stale limit, so the panel says "daemon not updating".
  * Garbage (not JSON, not the shape): the file is not touched at all.

The source is a command that prints the file (for example `kubectl exec switchboard-0 -- cat <state>/decisions.json`), given after `--`.
No key or token is read, stored or printed here; the command's own credentials are the caller's.

    bin/decisions_pull.py --state DIR -- kubectl exec switchboard-0 -- cat /state/decisions.json
"""
import argparse
import datetime
import json
import os
import re
import secrets
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
    try:
        return clean(body)
    except RecursionError:
        raise Garbage("nested too deeply")


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


def write_atomic(path, body, now):
    body = {k: body[k] for k in KEPT_KEYS if k in body}
    body.setdefault("written_at", _iso(now))
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


def pull(state_dir, fetch, now=None, err=None):
    """One pull. `fetch()` returns the source's bytes or raises Unreachable. Returns WROTE, STALED or UNTOUCHED; says why on `err` otherwise."""
    now = time.time() if now is None else now
    err = sys.stderr if err is None else err
    path = Path(state_dir) / DECISIONS_FILE
    try:
        raw = fetch()
    except Unreachable as why:
        if mark_stale(path, now):
            say(err, "source unreachable (%s); marked %s stale" % (why, DECISIONS_FILE))
            return STALED
        say(err, "source unreachable (%s); no %s to mark stale" % (why, DECISIONS_FILE))
        return UNTOUCHED
    try:
        body = parse(raw)
    except Garbage as why:
        say(err, "source answered something that is not the daemon's decisions list (%s); %s untouched" % (why, DECISIONS_FILE))
        return UNTOUCHED
    write_atomic(path, body, now)
    return WROTE


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", default=os.environ.get("FLEET_SWITCHBOARD_STATE"), help="the state directory the plugin reads")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="after --: the command that prints the remote decisions.json")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not args.state or not command:
        parser.error("--state (or FLEET_SWITCHBOARD_STATE) and a source command after -- are required")
    result = pull(args.state, lambda: command_fetch(command))
    print(result)
    return 0 if result == WROTE else 1


if __name__ == "__main__":
    sys.exit(main())
