"""A green run counts only when it ran on the current tip of the base branch (ask #1755).

`ci_state(ci, run_base, tip, behind)` is ONE pure decision: it reads and writes no file and calls no GitHub. It returns (state, line):
`fresh`, `stale` or `unknown`, or (None, "") for a `ci` that is not green (failing, pending, none: not judged here). `unknown` and `stale` are never green.
"""


def ci_state(ci, run_base, tip, behind):
    if ci != "green":
        return None, ""
    if not isinstance(run_base, str) or not run_base:
        return "unknown", "unknown: the base of the last run is not recorded"
    if not isinstance(tip, str) or not tip:
        return "unknown", "unknown: the tip could not be read"
    if run_base == tip:
        return "fresh", "fresh: CI ran on the tip %s" % tip[:8]
    if behind is None:
        return "stale", "stale: the commits behind were not counted (%s, %s)" % (run_base, tip)
    return "stale", "stale: %d commits behind (CI ran on %s, the tip is %s)" % (behind, run_base[:8], tip[:8])
