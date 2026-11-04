# ---- #1625: the review bots at a pull request's head. A bot is an ACCOUNT TYPE (`_is_bot`): no list, no name. Read-only: the lines are information.

BOT_FINDING_RE = re.compile(r"^- \[(FIX|BLOCK)\]", re.MULTILINE)
BOT_FIRST_RE = re.compile(r"^[^\n:]{1,60}: (\d+) findings? \((\d+) BLOCK, (\d+) FIX, (\d+) NOTE\)\s*$")


def bot_counts(body):
    """(fix, block, differ) of a bot review body, or None when it holds neither a findings first line nor a finding line (never zero).
    `differ` is None, or the (fix, block) the FIRST LINE says when the finding lines say something else."""
    text = body if isinstance(body, str) else ""
    seen = BOT_FINDING_RE.findall(text)
    first = BOT_FIRST_RE.match(text.split("\n", 1)[0].strip())
    if not seen and not first:
        return None
    lines = (seen.count("FIX"), seen.count("BLOCK"))
    if first and (int(first.group(3)), int(first.group(2))) != lines:
        return lines[0], lines[1], (int(first.group(3)), int(first.group(2)))
    return lines[0], lines[1], None


def _bot_name(review):
    return re.sub(r"[^\w.\[\]@-]", "?", (_login(review) or "bot"))[:60]


def bot_latest(reviews):
    """{login: the latest review} of the bot accounts (by submitted time, then id); a human review is never listed."""
    best = {}
    for review in reviews or []:
        if isinstance(review, dict) and _is_bot(review) and _login(review):
            key = (str(review.get("submitted_at") or ""), _id(review) or 0)
            if _login(review) not in best or key >= best[_login(review)][0]:
                best[_login(review)] = (key, review)
    return [best[login][1] for login in sorted(best)]


def bot_review_lines(reviews, head):
    """One `bot review:` line per review bot: its login, its latest state, the FIX and BLOCK counts and the head it was made at. A review at an
    OLDER head is shown as older and does not count; a body with no readable findings says `findings not read`; counts that differ say both."""
    out = []
    for review in bot_latest(reviews):
        commit = str(review.get("commit_id") or "unknown")
        state = str(review.get("state") or "unknown").upper()
        who = "bot review: %s %s at %s" % (_bot_name(review), state, commit[:8])
        if head and commit.lower() != head.lower():
            out.append("%s: older than the head %s, does not count" % (who, head[:8]))
            continue
        got = bot_counts(review.get("body"))
        if got is None:
            out.append("%s: findings not read" % who)
        elif got[2] is not None:
            out.append("%s: %d FIX, %d BLOCK in the finding lines but the first line says %d FIX, %d BLOCK: the numbers differ" % ((who,) + got[:2] + got[2]))
        else:
            out.append("%s: %d FIX, %d BLOCK" % (who, got[0], got[1]))
    return out


def bot_wake_item(repo, review, pr, now, known, lookups, number):
    """The ONE actionable item of a bot review with FIX or BLOCK findings made at the pull request's CURRENT head, keyed by the bot and the head (never by the review id), else None. The summary is fixed words: no review text."""
    head = str(((pr.get("head") if isinstance(pr, dict) else None) or {}).get("sha") or "")
    got = bot_counts(review.get("body")) if _is_bot(review) and _login(review) else None
    if not head or str(review.get("commit_id") or "").lower() != head.lower() or got is None or got[0] + got[1] == 0:
        return None
    fix, block = (got[2] if got[2] is not None and sum(got[2]) > got[0] + got[1] else got[:2])
    return GitHubItem("github.botreview:%s:%s" % (_login(review), head), "github.review",
                      "review bot %s left %d FIX and %d BLOCK findings on PR #%d at %s" % (_bot_name(review), fix, block, number, head[:8]),
                      repo, known, lookups, _login(review), now, [number], True)
