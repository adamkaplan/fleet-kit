"""switchboard_health: freshness check of the switchboard state files (relay, floor-live).

    python3 tools/switchboard_health.py [STATE_DIR]   exit 0 healthy, else one reason per line and exit 1

The 120 s default is the stall watchdog's bound (a 2 minute loop); the sidebar in plugins/fleet-decisions-tui/health.mjs
shows a looser 600 s staleness for floor-live.json, a display threshold, not a stall alarm."""
import os
import sys
import time

NAMES = ("relay", "floor-live")


def check_freshness(now, mtimes, max_age_s=120):
    """(name, reason) for each of relay and floor-live whose mtime is missing or older than max_age_s."""
    out = []
    for name in NAMES:
        m = mtimes.get(name)
        if m is None:
            out.append((name, "missing"))
        elif now - m > max_age_s:
            out.append((name, "stale: %ds old" % int(now - m)))
    return out


def _mtime(state_dir, name):
    for suffix in ("", ".json"):
        try:
            return os.stat(os.path.join(state_dir, name + suffix)).st_mtime
        except FileNotFoundError:
            pass
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    state_dir = argv[0] if argv else "state"
    mtimes, unreadable = {}, []
    for n in NAMES:
        try:
            mtimes[n] = _mtime(state_dir, n)
        except OSError as err:
            unreadable.append((n, "unreadable: %s" % err.strerror))
    found = unreadable + check_freshness(time.time(), {k: v for k, v in mtimes.items()})
    found = [f for f in found if not (f[1] == "missing" and f[0] in dict(unreadable))]
    for name, reason in found:
        print("%s: %s" % (name, reason))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
