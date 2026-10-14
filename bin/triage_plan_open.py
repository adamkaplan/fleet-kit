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
        url = record.get("html_url")
        url = url if isinstance(url, str) and url.startswith("https://github.com/") and len(url) < 200 else "https://github.com/%s/pull/%d" % (repo, number)
        by = ((record.get("user") or {}).get("login") or "") if isinstance(record.get("user"), dict) else ""
        for issue in references(record.get("body"), repo):
            old = links.get(issue)
            if old is None or str(record["merged_at"]) > str(old["merged_at"]):
                links[issue] = {"pr": number, "url": url, "by": by.lower() if isinstance(by, str) else "", "merged_at": str(record["merged_at"])[:30]}
    return links


def evidence(link, merged_age_days):
    """The finding's evidence: the PR that merged, its link, and how long ago."""
    return {"pr": link["pr"], "pr_url": link["url"], "merged_days": round(merged_age_days, 2)}
