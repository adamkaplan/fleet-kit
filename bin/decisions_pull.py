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
import subprocess
import sys
import time
from pathlib import Path

DECISIONS_FILE = "decisions.json"
STALE_SECONDS = 180                  # the plugin's STALE_MS / 1000
FETCH_TIMEOUT = 30
MAX_BYTES = 8 * 1024 * 1024          # a larger body is garbage, not a decisions list
KEPT_KEYS = ("generation", "written_at", "daemon_pid", "decisions", "errors", "provisional")

WROTE, STALED, UNTOUCHED = "wrote", "marked-stale", "untouched"


class Unreachable(Exception):
    """The source could not be read (down, timed out, non-zero exit)."""


def command_fetch(argv, timeout=FETCH_TIMEOUT, run=subprocess.run):
    """The source text of a command's standard output. Anything but a clean exit is Unreachable."""
    try:
        done = run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as err:
        raise Unreachable(type(err).__name__)
    if done.returncode != 0:
        raise Unreachable("exit %s" % done.returncode)
    return done.stdout


def parse(raw):
    """The daemon's file as a dict, or None for garbage."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_BYTES:
        return None
    try:
        body = json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not (isinstance(body, dict) and isinstance(body.get("decisions"), list) and isinstance(body.get("errors"), list)):
        return None
    return body


def _iso(now):
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_atomic(path, body, now):
    body = {k: body[k] for k in KEPT_KEYS if k in body}
    body.setdefault("written_at", _iso(now))
    temp = path.with_name("%s.%d.pull.tmp" % (DECISIONS_FILE, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(body, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.utime(str(temp), (now, now))
        os.replace(str(temp), str(path))
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise


def mark_stale(path, now):
    """Age the file past the plugin's limit (never younger than it is); a missing file stays missing."""
    try:
        info = os.stat(str(path))
    except OSError:
        return False
    older = min(info.st_mtime, now - STALE_SECONDS - 1)
    os.utime(str(path), (older, older))
    return True


def pull(state_dir, fetch, now=None):
    """One pull. `fetch()` returns the source's bytes or raises Unreachable. Returns WROTE, STALED or UNTOUCHED."""
    now = time.time() if now is None else now
    path = Path(state_dir) / DECISIONS_FILE
    try:
        raw = fetch()
    except Unreachable:
        return STALED if mark_stale(path, now) else UNTOUCHED
    body = parse(raw)
    if body is None:
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
