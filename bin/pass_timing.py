"""Pass timing (#279): each pass and each stage of it is timed; a pass or stage over SLOW_SECONDS is one `pass.slow` audit line, and the last KEEP passes
are kept in `pass-timing.json` so `status` can show the longest. Recording never raises and costs one small atomic write. Standard library only."""
import contextlib
import json
import os
import time

FILE = "pass-timing.json"
SLOW_SECONDS = 30
KEEP = 20
READ_CAP = 256 * 1024


class Timer:
    """Seconds per named stage of one pass (a stage entered twice adds up) and the total since construction."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.start, self.stages = clock, clock(), {}

    @contextlib.contextmanager
    def stage(self, name):
        began = self.clock()
        try:
            yield
        finally:
            self.stages[name] = self.stages.get(name, 0.0) + (self.clock() - began)

    def result(self):
        total = self.clock() - self.start
        stages = {k: round(v, 3) for k, v in self.stages.items()}
        slowest = max(stages, key=stages.get) if stages else "pass"
        if total > SLOW_SECONDS and stages.get(slowest, 0) <= SLOW_SECONDS:
            slowest = "pass"   # only the total is over
        return {"total": round(total, 3), "slowest": slowest, "stages": stages}


def load(state_dir):
    """The kept passes, oldest first; missing, corrupt or oversize reads as none."""
    try:
        with open(os.path.join(str(state_dir), FILE), "rb") as handle:
            data = json.loads(handle.read(READ_CAP + 1)[:READ_CAP])
        passes = data.get("passes")
        return [p for p in passes if isinstance(p, dict) and isinstance(p.get("total"), (int, float))][-KEEP:]
    except Exception:
        return []


def finish(timer, state_dir, audit, actor, at):
    """Record the pass and audit it when slow. Never raises. `audit` is an object with append(actor, event, subject, detail) or None."""
    try:
        got = timer.result()
        slow = got["total"] > SLOW_SECONDS or any(v > SLOW_SECONDS for v in got["stages"].values())
        if slow and audit is not None:
            seconds = got["stages"].get(got["slowest"], got["total"])
            audit.append(actor, "pass.slow", None, {"stage": got["slowest"], "seconds": seconds, "total": got["total"], "stages": got["stages"]})
        if state_dir is not None:
            passes = load(state_dir) + [dict(got, at=at)]
            path = os.path.join(str(state_dir), FILE)
            tmp = "%s.%d.tmp" % (path, os.getpid())
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps({"passes": passes[-KEEP:]}, sort_keys=True))
            os.replace(tmp, path)
    except Exception:
        pass


def status_line(state_dir):
    """One `passes` line, or None when there is no record."""
    passes = load(state_dir)
    if not passes:
        return None
    longest = max(passes, key=lambda p: p["total"])
    stages = longest.get("stages") if isinstance(longest.get("stages"), dict) else {}
    top = sorted(stages.items(), key=lambda kv: -kv[1])[:3]
    over = sum(1 for p in passes if p["total"] > SLOW_SECONDS or any(v > SLOW_SECONDS for v in (p.get("stages") or {}).values()))
    return "passes: longest of last %d %.1fs (%s), last %.1fs, %d over %ds" % (
        len(passes), longest["total"], ", ".join("%s %.1fs" % kv for kv in top) or "no stages", passes[-1]["total"], over, SLOW_SECONDS)
