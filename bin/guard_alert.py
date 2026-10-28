#!/usr/bin/env python3
"""The alert of a bad installed tree, by a path that needs no guarded code (ask #1380).

`fleet-switchboard install` copies this file into the state directory, outside the installed tree. It imports nothing of the kit and
nothing from the tree it reports on: standard library only. The daemon runs it as a program when the tree is not the default branch,
clean, at an ancestor of the remote default branch.

    guard_alert.py --state DIR --text TEXT [--herdr PATH] [--cos NAME] [--notifier PATH] [--now EPOCH] [--every SECONDS]
    guard_alert.py --state DIR --text '' --clear

The first alert of a bad tree goes at once; a repeat is sent only after --every seconds (a day). One call does all of these, each on its own (one failing never stops the others), and prints one JSON line of what happened:

  - the audit line `installed_tree.alert` in DIR/audit.jsonl;
  - a toast addressed to the Chief of Staff, by `herdr notification show` (herdr's own program, by its absolute path; nothing is typed into a pane);
  - a desktop notification for the person, on EVERY alert (osascript on macOS, notify-send elsewhere, or --notifier), so it shows even when herdr is down;
  - a line appended to DIR/installed-tree-alerts.txt: the last resort.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

TITLE = "fleet-kit: the installed tree is not the default branch"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run(argv):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def audit(state, text):
    fd = os.open(os.path.join(state, "audit.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps({"ts": now(), "actor": "fleet-switchboard", "event": "installed_tree.alert", "subject": None,
                                 "detail": {"text": text}}, sort_keys=True) + "\n")


def herdr_toast(herdr, cos, text):
    if not herdr or not os.path.isabs(herdr):
        return False
    return run([herdr, "notification", "show", "%s (for %s)" % (TITLE, cos or "cos"), "--body", text[:240]])


def desktop(notifier, text):
    if notifier:
        return run([notifier, TITLE, text])
    if sys.platform == "darwin":
        script = 'display notification %s with title %s' % (json.dumps(text[:240]), json.dumps(TITLE))
        return run(["/usr/bin/osascript", "-e", script])
    return run(["notify-send", TITLE, text[:240]])


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--herdr")
    parser.add_argument("--cos", default="cos")
    parser.add_argument("--notifier")
    parser.add_argument("--now", type=float, help="the clock, in epoch seconds (tests)")
    parser.add_argument("--every", type=float, default=86400, help="seconds between alerts for a tree that stays bad (default: one day)")
    parser.add_argument("--clear", action="store_true", help="the tree is good again: forget the last alert, so the next bad tree alerts at once")
    args = parser.parse_args(argv)
    marker = os.path.join(args.state, "installed-tree-alert")   # the clock of the last alert, one number in plain text
    if args.clear:
        try:
            os.unlink(marker)
        except OSError:
            pass
        print(json.dumps({"cleared": True}))
        return 0
    clock = args.now if args.now is not None else datetime.now(timezone.utc).timestamp()
    try:
        last = float(open(marker).read().strip())
    except (OSError, ValueError):
        last = None
    if last is not None and 0 <= clock - last < args.every:
        print(json.dumps({"sent": False, "why": "alerted less than a day ago"}))
        return 0
    try:
        with open(marker, "w") as handle:
            handle.write(repr(clock))
    except OSError:
        pass
    done = {}
    for name, step in (("audit", lambda: audit(args.state, args.text) or True),
                       ("herdr", lambda: herdr_toast(args.herdr, args.cos, args.text)),
                       ("desktop", lambda: desktop(args.notifier, args.text))):
        try:
            done[name] = bool(step())
        except Exception:
            done[name] = False
    try:
        with open(os.path.join(args.state, "installed-tree-alerts.txt"), "a") as handle:
            handle.write("%s %s\n" % (now(), args.text))
        done["file"] = True
    except OSError:
        done["file"] = False
    done["sent"] = True
    print(json.dumps(done, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
