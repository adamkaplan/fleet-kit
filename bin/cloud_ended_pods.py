"""The ended pods of the cloud status (ask #2343): how many pods of phase Failed or Succeeded still stand, the age of the oldest (since its hard
deadline) and one line naming each one past its grace (the launcher's KEPT_GRACE_SECONDS, 24 hours), which the reaper's rule keeps at none.

A part of bin/cloud_worker.py, loaded by bin/cloud_inspect.py (so that file takes no new line): its `bind` wraps the `_summary_line` and `status_lines` of that file, so the
status prints these lines under the cloud summary line. The snapshot of the launcher names each pod's phase, hard deadline and kept mark; a pod whose
deadline cannot be read is counted but never called past its grace.
"""
import datetime

D: object   # set by the loader before this file runs

GRACE_SECONDS = 24 * 3600   # the equal of KEPT_GRACE_SECONDS in cloud/launcher/cloud_worker.py (a test holds them equal)
ENDED = ("Failed", "Succeeded")


def _since(text, now):
    """Seconds since the hard deadline text (an ISO time), or None when it cannot be read."""
    try:
        when = datetime.datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None
    return max(0, int(now - when.timestamp()))


def ended_lines(snapshot, now):
    """[] when no pod has ended; else the count and the oldest age, then one line per pod past its grace (name, phase, age, kept or not)."""
    rows = []
    for pod in ((snapshot or {}).get("pods") or []):
        if isinstance(pod, dict) and pod.get("phase") in ENDED:
            rows.append((D["_safe"](pod.get("name"), "?"), pod["phase"], _since(pod.get("hard_deadline"), now), pod.get("kept") is True))
    if not rows:
        return []
    ages = [r[2] for r in rows if r[2] is not None]
    lines = ["cloud: ended pods still standing %d, oldest %s" % (len(rows), D["_age"](max(ages)) if ages else "unknown")]
    for name, phase, age, kept in sorted(rows, key=lambda r: -(r[2] or 0)):
        if age is not None and age > GRACE_SECONDS:
            lines.append("cloud: ended pod %s (%s, %s) is %s past its hard deadline, past its grace of %s" % (name, phase, "kept" if kept else "not kept", D["_age"](age), D["_age"](GRACE_SECONDS)))
    return lines


def bind(namespace):
    """Wrap two functions of bin/cloud_worker.py: its `_summary_line` (unchanged, but it hands the snapshot's ended-pod lines on) and its `status_lines`
    (which then puts them, as lines of their own, right under the summary line). Nothing is added when no pod has ended."""
    inner_summary, inner_status, held = namespace["_summary_line"], namespace["status_lines"], []

    def _summary_line(snapshot, now):
        held[:] = ended_lines(snapshot, now)
        return inner_summary(snapshot, now)

    def status_lines(*args, **kwargs):
        held[:] = []
        lines, rows = inner_status(*args, **kwargs)
        extra, held[:] = list(held), []
        return lines[:1] + ["      " + line for line in extra] + lines[1:], rows
    _summary_line.__wrapped__, status_lines.__wrapped__ = inner_summary, inner_status
    namespace["_summary_line"], namespace["status_lines"] = _summary_line, status_lines
