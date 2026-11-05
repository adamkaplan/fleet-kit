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
        return "fresh", "fresh: CI ran on the tip %s" % tip[:12]
    if behind is None:
        return "stale", "stale: the commits behind were not counted (%s, %s): close and reopen the pull request for a fresh run" % (run_base, tip)
    return "stale", "stale: %d commits behind (CI ran on %s, the tip is %s): close and reopen the pull request for a fresh run" % (behind, run_base[:8], tip[:8])


def apply(facts, run_base, tip, behind):
    """Judge a green `facts["ci"]` in place. `ci_line` holds the one line of `ci_state` that the readers print verbatim; a stale or unknown run is never green: `ci` says `stale: N behind` or `unknown`."""
    state, line = ci_state(facts.get("ci"), run_base, tip, behind)
    if state:
        facts["ci_line"] = line
    if state in ("stale", "unknown"):
        facts["ci"] = "stale: %d behind" % behind if state == "stale" and behind is not None else state


def fresh_fact(facts, setting, make_gh, reading, repo, number):
    """The helper of the entry, by callables (it imports nothing of the entry): `setting()` is the repo's plan_gates entry, `make_gh()` the merge gate's reader, `reading` the module pr_reading.
    Only an explicit `ci_current_tip: false` turns the rule off; an unreadable configuration or reader is not an opt-out, and a failed read is `unknown`."""
    try: off = (setting() or {}).get("ci_current_tip") is False
    except Exception: off = False
    if facts.get("ci") == "green" and not off:
        try: gh = make_gh()
        except Exception: gh = None
        try: got = reading.read_fresh(gh, repo, number, facts.get("head"), facts.get("base_ref"))
        except Exception: got = (None, None, None)   # a reader that raises is a read that failed
        apply(facts, *got)
    return facts
