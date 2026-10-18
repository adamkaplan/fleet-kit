"""plan_wake: tell a plan's approver when a plan is posted or edited, and list a plan nobody seems to have approved (#765). Pure rules.

The daemon's triage pass already holds, for each watched repository, the open issues and pull requests, the comments of the last 14 days and the 50
most recently closed pull requests. This module reads only those plain records (and the local ledger's consensus hashes): it never reads GitHub,
touches no file and sends nothing. The caller owns the state and the delivery.

Approved means ANY of: (1) a comment on the plan's issue by a different SIGNER than the plan comment's (the `from=` marker, else the login) that says
"approved" and carries the first 8 hex characters of the plan hash; (2) a pull request, opened at or after the plan's last edit, whose body's first line
is `PLAN: <the plan comment's url>`; (3) a consensus pass in the ledger for that exact hash. (1) and (2) are a notification HINT that stops the sweep
line and the note; they are not an approval gate (a marker is plain text), and G0c stays the real approval.
Python 3.9+, standard library only; loaded by path from beside bin/fleet-switchboard.
"""

import re

ANNOUNCE_SECONDS = 24 * 3600     # a plan last posted or edited longer ago than this is neither announced nor listed (an old plan is not news)
UNAPPROVED_SECONDS = 30 * 60     # a plan with no approval seen this long after it was posted is listed on the sweep line
MAX_LISTED = 10

_APPROVED_RE = re.compile(r"\bapproved\b", re.I)
_NEGATED_RE = re.compile(r"(?:\bnot|\bnever|n't)\s+approved\b|\bunapproved\b", re.I)
_PLAN_CITE_RE = re.compile(r"^\s*PLAN:\s*<?https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([0-9]{1,9})#issuecomment-([0-9]{1,15})", re.I)
WINDOWS = "windows: 14 days of comments, 2 pages of issues, 50 merged PRs"


def signer(comment, signed_by):
    """Who wrote a comment: the agent its hidden marker names (`signed_by` is the daemon's own parser), else its login. Every fleet agent writes through
    the same login, so the marker is what tells them apart."""
    body = comment.get("body") if isinstance(comment, dict) else None
    mark = signed_by(body) if isinstance(body, str) else None
    if mark:
        return "agent:" + mark.lower()
    user = comment.get("user") if isinstance(comment, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    return "login:" + login.lower() if isinstance(login, str) and login else None


def latest_plans(comments, heading_re, number_of):
    """{issue number: the latest comment whose first non-empty line is a plan heading} (the latest by creation time, then id: the plan check's rule)."""
    found = {}
    for comment in comments or []:
        body = comment.get("body") if isinstance(comment, dict) else None
        number = number_of(comment.get("issue_url")) if isinstance(comment, dict) else None
        if not isinstance(body, str) or number is None or not isinstance(comment.get("id"), int):
            continue
        first = next((line for line in body.splitlines() if line.strip()), "")
        if not heading_re.match(first):
            continue
        key = (str(comment.get("created_at") or ""), comment["id"])
        if number not in found or key > found[number][0]:
            found[number] = (key, comment)
    return {number: comment for number, (_, comment) in found.items()}


def hex8(plan_hash):
    return plan_hash.split(":", 1)[-1].lower()[:8]


def approved_by_comment(plan, plan_hash, issue_comments, signed_by):
    """True when another signer's comment says `approved` (not negated) with the first 8 hex characters of `plan_hash`."""
    mine = signer(plan, signed_by)
    token = re.compile(r"(?<![0-9a-f])%s" % re.escape(hex8(plan_hash)), re.I)
    for comment in issue_comments or []:
        if not isinstance(comment, dict) or comment.get("id") == plan.get("id") or not isinstance(comment.get("body"), str):
            continue
        who = signer(comment, signed_by)
        if who is None or who == mine:
            continue
        text = comment["body"][:20000]
        if token.search(text) and _APPROVED_RE.search(text) and not _NEGATED_RE.search(text):
            return True
    return False


def cited_by_pr(repo, number, plan, pulls, parse_time):
    """True when a pull request opened at or after the plan's last edit has `PLAN: <this plan comment's url>` as the first line of its body."""
    edited = parse_time(plan.get("updated_at") or plan.get("created_at"))
    for pr in pulls or []:
        body = pr.get("body") if isinstance(pr, dict) else None
        if not isinstance(body, str):
            continue
        first = next((ln for ln in body[:5000].splitlines() if ln.strip()), "")
        found = _PLAN_CITE_RE.match(first)
        if not found or found.group(1).lower() != repo.lower() or int(found.group(2)) != number or int(found.group(3)) != plan.get("id"):
            continue
        opened = parse_time(pr.get("created_at"))
        if edited is None or opened is None or opened >= edited:
            return True
    return False


def plan_url(repo, number, comment_id):
    """The url of the plan comment, rebuilt from the repository, the number and the id: no text of the comment is ever put into it."""
    return "https://github.com/%s/issues/%d#issuecomment-%d" % (repo, number, comment_id)


def line(number, url, plan_hash, passed):
    """The one line the approver reads: the issue, the url, the hash and the check result. Nothing from the plan's own text."""
    return "plan posted on #%d: %s %s, plan check %s" % (number, url, plan_hash, "PASS" if passed else "FAIL")


def unapproved_line(refs, extra=0):
    """The one sweep line, or None when there is none."""
    if not refs:
        return None
    return "plans posted over 30 min ago, no approval comment, no PR citing it, no consensus seen (%s): %s%s" % (
        WINDOWS, ", ".join(refs), " (+%d more)" % extra if extra else "")
