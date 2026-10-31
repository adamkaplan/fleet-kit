"""A launch for issue N is never started on a brief that names another issue or a stale pull request head (#1350).

`launch --issue N` used to read the brief and compare nothing: the text went to the new agent as its first prompt. Now, before anything starts,
only three things in the brief are read: an `ISSUE:` line, a PR line (a line with `PR #M`, `PR: .../pull/M`, or a `/pull/M` link) and an
`at head <sha>` clause. An issue number other than N, or a head that is not a prefix of the named PR's current head, is a refusal that names both.
A brief with none of these is accepted and no pull request is read. A head that cannot be verified (the PR read failed) is a refusal too.

Pure: `check` takes the brief, the issue number, the default repo and a `read_head(repo, number)` callable; it returns a refusal text or None.
"""

import re

ISSUE_LINE = re.compile(r"^\s*ISSUE:\s*(.*)$", re.M)
ISSUE_REF = re.compile(r"(?:github\.com/([\w.-]+/[\w.-]+)/issues/|#|\b)(\d+)\b")
HEAD = re.compile(r"\bat head\s+`?([0-9a-fA-F]{4,40})\b", re.I)
PR_NUM = re.compile(r"(?:/pull/|\bPRs?\s*:?\s*#?)(\d+)\b", re.I)
PR_REPO = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/\d+")


class HeadUnreadable(Exception):
    """The pull request's head could not be read (an error, not a missing pull request)."""


def issue_mismatch(brief, issue):
    """(named, N) of the first ISSUE: line that names another issue number, or None."""
    for line in ISSUE_LINE.findall(brief):
        found = ISSUE_REF.search(line)
        if found and int(found.group(2)) != int(issue):
            return found.group(2), issue
    return None


def heads(brief):
    """[(pr number or None, sha, repo or None)] for every `at head` clause, with the PR named on the same line."""
    out = []
    for line in brief.splitlines():
        head = HEAD.search(line)
        if head:
            pr, repo = PR_NUM.search(line), PR_REPO.search(line)
            out.append((int(pr.group(1)) if pr else None, head.group(1).lower(), repo.group(1) if repo else None))
    return out


def check(brief, issue, repo, read_head):
    """A refusal text, or None when the brief matches (or says nothing checkable)."""
    wrong = issue_mismatch(brief, issue)
    if wrong:
        return "the brief names issue #%s but this launch is for --issue %s" % wrong
    for number, sha, named_repo in heads(brief):
        if number is None:
            continue   # a head with no pull request named has nothing to compare with
        where = named_repo or repo
        try:
            if not where:
                raise HeadUnreadable("no repository to read PR #%d in" % number)
            current = read_head(where, number)
        except HeadUnreadable as err:
            return "the brief's head %s of PR #%d is unverified (%s): retry, or pass --allow-brief-mismatch REASON" % (sha, number, err)
        if not isinstance(current, str) or not current.lower().startswith(sha):
            return "the brief says PR #%d is at head %s but its current head is %s" % (number, sha, current)
    return None
