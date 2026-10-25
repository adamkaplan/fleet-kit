"""#1141: which check conclusions are red, which are green, and which say nothing; and the once-per-key memory of what was told.

One table decides every conclusion GitHub names. A conclusion not in the table says nothing (never read as red by guessing).
Pure: no GitHub read, no write, no clock.
"""
import re

# conclusion (lower case) -> "red" | "green" | "none". Every conclusion a check run or workflow run can have has a row.
COLOUR = {
    "failure": "red",
    "success": "green",
    "cancelled": "none",
    "skipped": "none",
    "neutral": "none",
    "timed_out": "red",          # a required check that timed out blocks the merge
    "startup_failure": "red",    # a workflow that failed to start blocks the merge
    "action_required": "none",
    "stale": "none",
}
# Every field of a run record the code trusts, and what it is used for (docs/switchboard.md carries the same table; a test keeps the two equal).
FIELDS = ("id", "name", "status", "conclusion", "head_sha", "head_branch", "event", "html_url", "created_at", "pull_requests", "run_attempt", "workflow_id")
TEXT_CAP = 120


def colour(run):
    """`red`, `green` or `none` of one run record: only a COMPLETED run with a listed conclusion counts."""
    if not isinstance(run, dict) or run.get("status") not in ("completed", "COMPLETED"):   # a missing status is unknown: silent
        return "none"
    word = run.get("conclusion")
    return COLOUR.get(word.lower(), "none") if isinstance(word, str) else "none"


def is_required(required, name):
    """`required` is the configured list of required check names of the repository ("*" means every check); nothing configured: none is."""
    return bool(required) and (("*" in required) or name in required)


def clean(text, cap=TEXT_CAP):
    """Text of GitHub origin made safe for a message: no control or ANSI characters, no newline, no markdown or angle brackets, capped."""
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", str(text))
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", text)
    text = re.sub(r"[`*_\[\]<>|#]", "", text)
    text = " ".join(text.split())
    return text[:cap]


def pr_key(repo, number, head, check, run_id=0, attempt=1):
    """A new head, a new run or a re-run (new attempt) is a new event."""
    return "pr:%s#%d:%s:%s:%s:%s" % (repo, number, clean(head, 64).replace(":", "_"), clean(check, 60).replace(":", "_"), run_id, attempt)


def branch_key(kind, repo, run_id):
    return "branch-%s:%s:%s" % (kind, repo, run_id)


def pr_line(repo, number, check, url, head):
    return "required check (workflow) %s failed on %s#%d at head %s: %s" % (clean(check), repo, number, clean(head, 64), clean(url, 200))


def unowned_line(repo, number, check, url, head):
    return "required check (workflow) %s failed on %s#%d at head %s and no worker owns that pull request: %s" % (clean(check), repo, number, clean(head, 64), clean(url, 200))


def branch_line(kind, repo, branch, name, url, head):
    if kind == "green":
        return "default branch %s of %s is green again: workflow %s at head %s %s" % (clean(branch, 60), repo, clean(name), clean(head, 64), clean(url, 200))
    return "default branch %s of %s is red: workflow %s failed at head %s (the alarm names the workflow only; jobs are not read) %s" % (clean(branch, 60), repo, clean(name), clean(head, 64), clean(url, 200))
