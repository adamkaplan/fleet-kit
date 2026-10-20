"""A plan or work-item issue still open after the pull request that implements it merged (#384, part of #130).

A PR names the issue it implements with `Closes #N` or `Part of #N` in its body, or with a `PLAN:` link to it. `Closes` normally closes the issue when
the PR merges; `Part of` and `PLAN:` do not, so the issue stays open with nobody having said it is finished. The triage reads one page of the recently
closed pull requests per repo, and this module says which open issues they name.

Pure: it reads plain records and returns plain values; it touches no file, GitHub or agent.

The same module holds the rules of a posted or edited plan telling its approver (#765, the section "A posted or edited plan" below): the daemon's triage pass
reads the units it already holds and asks these rules who approved what. Nothing the delegated clear runs loads it.
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


def _ref(record, side):
    """The branch name of one side (`base` or `head`) of a pull request record, or None when it is not readable as a non-empty string."""
    part = record.get(side) if isinstance(record, dict) else None
    ref = part.get("ref") if isinstance(part, dict) else None
    return ref if isinstance(ref, str) and ref else None


def split_batch_waiting(prs, links, integration_branch):
    """(judged, waiting): `links` (from `merged_links`) split by whether the change is still waiting for the batch.

    A link waits only when its pull request merged with a base that EQUALS the configured integration branch and no merged batch pull request is in the
    records already read: one whose head is the integration branch and whose base is the repository's default branch, merged at or after the member.
    Anything else is judged as before: no integration branch configured (not a non-empty string), a base that cannot be read, another branch, the
    default branch, or a doubt of any kind. Reads nothing new; the branch name comes only from the configuration."""
    if not (isinstance(integration_branch, str) and integration_branch):
        return links, {}
    records = {}
    batches = []
    for record in prs or []:
        if not isinstance(record, dict) or not record.get("merged_at"):
            continue
        number = _number(record.get("number"))
        if number is not None:
            records[number] = record
        base = record.get("base")
        default = ((base.get("repo") or {}).get("default_branch") if isinstance(base, dict) and isinstance(base.get("repo"), dict) else None)
        if _ref(record, "head") == integration_branch and isinstance(default, str) and default and _ref(record, "base") == default:
            batches.append(str(record["merged_at"]))
    judged, waiting = {}, {}
    for issue, link in links.items():
        member = records.get(link["pr"])
        if member is not None and _ref(member, "base") == integration_branch and not any(at >= str(link["merged_at"]) for at in batches):
            waiting[issue] = link
        else:
            judged[issue] = link
    return judged, waiting


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


# ---- #765: a posted or edited plan tells its approver once per hash. Approved means ANY of: (1) a comment on the plan's issue by a different SIGNER than the plan
# comment's (the `from=` marker, else the login) that says "approved" with the first 8 hex characters of the plan hash; (2) a pull request opened at or after the
# plan's last edit whose body's first line is `PLAN: <the plan comment's url>`; (3) a consensus pass in the ledger for that exact hash. (1) and (2) are a
# notification HINT, never an approval gate (a marker is plain text): G0c stays the real approval.

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


# ---- #790: a fourth approval form, an approval SENT as a message. The audit records a delivered send as a `v2.synthetic` entry: `detail.keys` holds
# `send:<sender>:<id>` and `detail.description` is `switchboard: <sender> message on <issue tag>: <first 100 characters of the text>` (200 in all), so
# `Approved at sha256:` + 64 hex (83 characters) fits. A message batched with others (`N items for ...`) is not recognised: the note then fires, as before.

DAEMON_SENDERS = ("switchboard", "fleet-switchboard", "fleet-daemon")   # the daemon's own lines quote other people; they are never an approver
MAX_SENDS = 50000       # audit entries scanned per pass
MAX_DESCRIPTION = 400   # the daemon writes at most 200 characters
_SEND_DESC_RE = re.compile(r"^switchboard: ([A-Za-z0-9_.-]{1,64}) message on [^\s:]{1,120}: (.*)$", re.S)
_QUOTED_LINE_RE = re.compile(r"(?:^|[\r\n])\s*>")


def _send_sender(detail):
    """The sender of a single audited send: the one `send:<sender>:<id>` key, agreeing with the description's own sender; else None."""
    keys = detail.get("keys")
    if not isinstance(keys, list) or not keys:
        return None
    senders = set()
    for key in keys:
        parts = key.split(":") if isinstance(key, str) else []
        if len(parts) != 3 or parts[0] != "send" or not parts[1]:
            return None
        senders.add(parts[1].lower())
    return senders.pop() if len(senders) == 1 else None


def approved_by_send(plan, plan_hash, entries, signed_by):
    """True when an audited send by someone other than the plan comment's signer starts `Approved at sha256:<hash>`, or says `approved` (not negated,
    not a quoted line) and holds the plan's FULL 64-hex hash at a hex boundary. A prefix or another hash never counts. A hint, as the other forms are."""
    mine = signer(plan, signed_by)
    full = plan_hash.split(":", 1)[-1].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", full):
        return False
    token = re.compile(r"(?<![0-9a-f])%s(?![0-9a-f])" % full)
    starts = re.compile(r"^Approved at sha256:%s(?![0-9a-f])" % full, re.I)
    for entry in (entries or [])[-MAX_SENDS:]:   # bounded: the caller's audit tail is 8 MiB, and only the newest entries are read; a failure to match is only the old note
        detail = entry.get("detail") if isinstance(entry, dict) else None
        if (not isinstance(detail, dict) or entry.get("event") != "v2.synthetic" or not isinstance(detail.get("description"), str)
                or len(detail["description"]) > MAX_DESCRIPTION):
            continue
        sender = _send_sender(detail)
        found = _SEND_DESC_RE.match(detail["description"])
        if sender is None or found is None or found.group(1).lower() != sender or sender in DAEMON_SENDERS or "agent:" + sender == mine:
            continue
        text = found.group(2)
        if starts.match(text):
            return True
        if token.search(text.lower()) and _APPROVED_RE.search(text) and not _NEGATED_RE.search(text) and not _QUOTED_LINE_RE.search(text):
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


def line(number, url, plan_hash, passed, stale=False):
    """The one line the approver reads: the issue, the url, the hash and the check result. Nothing from the plan's own text."""
    result = "PASS" if passed else "FAIL (issue body not re-read: may be stale; run plan check)" if stale else "FAIL"
    return "plan posted on #%d: %s %s, plan check %s" % (number, url, plan_hash, result)


def unapproved_line(refs, extra=0):
    """The one sweep line, or None when there is none."""
    if not refs:
        return None
    return "plans posted over 30 min ago, no approval comment, no PR citing it, no consensus seen (%s): %s%s" % (
        WINDOWS, ", ".join(refs), " (+%d more)" % extra if extra else "")
