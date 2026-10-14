"""A plan or work-item issue still open after the pull request that implements it merged (#384, part of #130).

A PR names the issue it implements with `Closes #N` or `Part of #N` in its body, or with a `PLAN:` link to it. `Closes` normally closes the issue when
the PR merges; `Part of` and `PLAN:` do not, so the issue stays open with nobody having said it is finished. The triage reads one page of the recently
closed pull requests per repo, and this module says which open issues they name.

Pure: it reads plain records and returns plain values; it touches no file, GitHub or agent.
"""
import re

REF_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|part\s+of)[ \t]*:?[ \t]+#([0-9]{1,9})\b", re.I)
PLAN_RE = re.compile(r"\bPLAN:[ \t]*<?(?:https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/|([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#)([0-9]{1,9})\b", re.I)
KIND = "T12"
LABEL = "plan issue still open after its PR merged"
MAX_BODY = 20000   # characters of a PR body read; the references are near its top


def _number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def references(body, repo):
    """The issue numbers of `repo` a PR body names (`Closes`, `Fixes`, `Resolves`, `Part of`, or a `PLAN:` link or `owner/repo#N` for this repo)."""
    if not isinstance(body, str):
        return set()
    body = body[:MAX_BODY]
    found = {int(n) for n in REF_RE.findall(body)}
    for url_repo, short_repo, number in PLAN_RE.findall(body):
        if (url_repo or short_repo).lower() == (repo or "").lower():
            found.add(int(number))
    return found


def merged_links(prs, repo):
    """{issue number: {"pr", "url", "by", "merged_at"}} from the records of recently closed pull requests of `repo`: only a MERGED one counts (a PR
    closed without merging implemented nothing); the latest merge wins when several name the same issue."""
    links = {}
    for record in prs or []:
        if not isinstance(record, dict) or not record.get("merged_at"):
            continue
        number = _number(record.get("number"))
        if number is None:
            continue
        url = "https://github.com/%s/pull/%d" % (repo, number)   # rebuilt from what we know, never taken from the record (control characters, another host)
        by = ((record.get("user") or {}).get("login") or "") if isinstance(record.get("user"), dict) else ""
        for issue in references(record.get("body"), repo):
            old = links.get(issue)
            if old is None or str(record["merged_at"]) > str(old["merged_at"]):
                links[issue] = {"pr": number, "url": url, "by": by.lower() if isinstance(by, str) else "", "merged_at": str(record["merged_at"])[:30]}
    return links


def evidence(link, merged_age_days):
    """The finding's evidence: the PR that merged, its link, and how long ago."""
    return {"pr": link["pr"], "pr_url": link["url"], "merged_days": round(merged_age_days, 2)}


# -- #410: T12 fires only for an issue that is truly done. Pending work is read from records already held, never from a new GitHub read.
PENDING_TASK_RE = re.compile(r"^[ \t]*(?:[-*][ \t]+)\[ \][ \t]+\S", re.M)
PENDING_TEXT_RE = re.compile(r"\b(?:pending[ \t]+(?:live[ \t]+|tester[ \t]+)?proof|(?:awaiting|waiting[ \t]+for)[ \t]+(?:the[ \t]+)?(?:live[ \t]+|tester[ \t]+)?proof)\b", re.I)
QUOTED_RE = re.compile(r"```.*?```|^[ \t]*>.*$", re.S | re.M)


def pending_text(body):
    """True when the issue's own text (outside code and quotes) says work is left: an unchecked task-list item or a `pending proof` line."""
    if not isinstance(body, str):
        return False
    text = QUOTED_RE.sub(" ", body[:MAX_BODY])
    return bool(PENDING_TASK_RE.search(text) or PENDING_TEXT_RE.search(text))


def ledger_pending(entries, repo, pr):
    """True when the ledger holds, for pull request `pr` of `repo`, a scenario with a recorded proof whose latest verdict is missing or not a pass
    (a proof not yet judged, or judged failed). Entries are the plain records of the plan ledger."""
    ref = "%s#%d" % (repo, pr)
    proofs, verdicts = set(), {}
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("pr") != ref:
            continue
        if entry.get("kind") == "proof":
            proofs.add(entry.get("scenario"))
        elif entry.get("kind") == "verdict":
            verdicts[entry.get("scenario")] = entry.get("result")   # entries are oldest first: the last one wins
    return any(verdicts.get(sid) != "pass" for sid in proofs if sid)


def pending_issues(links, entries, repo):
    """The issue numbers of `links` whose linked merged pull request still has a proof not passed in the ledger."""
    return {issue for issue, link in links.items() if ledger_pending(entries, repo, link["pr"])}
