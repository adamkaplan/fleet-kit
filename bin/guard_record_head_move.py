#!/usr/bin/env python3
"""Record a move of the installed tree's HEAD, with the process chain that made it (ask #1380).

Run by the tree's git hooks (post-checkout, post-merge), which `fleet-switchboard install` writes. It is a copy kept in the state
directory, outside the tree, and it imports nothing of the kit: standard library only. It never fails the git command that called it.

    guard_record_head_move.py <state dir> <hook name> [hook arguments...]

It appends one line to <state dir>/audit.jsonl: event `installed_tree.head_moved`, the old and the new commit, the hook, the current
directory and the ancestors of this process (pid, parent pid, command line), nearest first.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

MAX_ANCESTORS = 16


def git(*argv):
    try:
        done = subprocess.run(["git"] + list(argv), capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def process_row(pid):
    """(parent pid, command line) of `pid`, or None."""
    try:
        done = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    line = done.stdout.strip()
    if done.returncode != 0 or not line:
        return None
    parent, _, command = line.partition(" ")
    try:
        return int(parent), command.strip()[:400]
    except ValueError:
        return None


def ancestors(start=None):
    """The chain of the processes above `start` (default: this one), nearest first: [{"pid", "ppid", "command"}]."""
    chain, pid = [], os.getppid() if start is None else start
    while pid and pid != 1 and len(chain) < MAX_ANCESTORS:
        row = process_row(pid)
        if row is None:
            break
        chain.append({"pid": pid, "ppid": row[0], "command": row[1]})
        pid = row[0]
    return chain


def entry(hook, args):
    new = git("rev-parse", "HEAD")
    old = args[0] if hook == "post-checkout" and args else git("rev-parse", "ORIG_HEAD")
    return {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "actor": "fleet-switchboard", "event": "installed_tree.head_moved", "subject": git("rev-parse", "--show-toplevel"),
            "detail": {"hook": hook, "old": old, "new": new, "branch": git("rev-parse", "--abbrev-ref", "HEAD"), "cwd": os.getcwd(),
                       "ancestors": ancestors()}}


def main(argv):
    if len(argv) < 2:
        return 0
    try:
        line = json.dumps(entry(argv[1], argv[2:]), sort_keys=True) + "\n"
        fd = os.open(os.path.join(argv[0], "audit.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(line)
    except Exception as err:   # a record that cannot be written never stops the git command
        sys.stderr.write("fleet-kit: could not record the move of the installed tree: %s\n" % err)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
