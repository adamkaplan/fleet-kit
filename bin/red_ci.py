"""#1141: which check conclusions are red, which are green, and which say nothing; and the once-per-key memory of what was told.

One table decides every conclusion GitHub names. A conclusion not in the table says nothing (never read as red by guessing).
Pure: no GitHub read, no write, no clock.
"""

# conclusion (lower case) -> "red" | "green" | "none". Every conclusion a check run or workflow run can have has a row.
COLOUR = {
    "failure": "red",
    "success": "green",
    "cancelled": "none",
    "skipped": "none",
    "neutral": "none",
    "timed_out": "none",
    "action_required": "none",
    "stale": "none",
    "startup_failure": "none",
}


def colour(run):
    """`red`, `green` or `none` of one run record: only a COMPLETED run with a listed conclusion counts."""
    if not isinstance(run, dict) or run.get("status") not in (None, "completed", "COMPLETED"):
        return "none"
    word = run.get("conclusion")
    return COLOUR.get(word.lower(), "none") if isinstance(word, str) else "none"


def is_required(required, name):
    """`required` is the configured list of required check names of the repository ("*" means every check); nothing configured: none is."""
    return bool(required) and (("*" in required) or name in required)


def pr_key(repo, number, head, check):
    return "pr:%s#%d:%s:%s" % (repo, number, head, check)


def branch_key(kind, repo, run_id):
    return "branch-%s:%s:%s" % (kind, repo, run_id)


def pr_line(repo, number, check, url, head):
    return "required check %s failed on %s#%d at head %s: %s" % (check, repo, number, head, url)


def branch_line(kind, repo, branch, name, url, head, job=None):
    if kind == "green":
        return "default branch %s of %s is green again: %s at head %s %s" % (branch, repo, name, head, url)
    return "default branch %s of %s is red: %s failed at head %s (failing job: %s) %s" % (branch, repo, name, head, job or "unknown", url)
