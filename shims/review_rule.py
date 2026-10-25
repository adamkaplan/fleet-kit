"""The ONE rule for which reviews block a pull request, shared by the gh shim (`gh pr merge`) and `fleet-switchboard gates` (#1123 M1).
Reviews come in the `gh pr view --json reviews` shape: {"author": {"login"}, "state", "commit": {"oid"}}. A caller holding the REST shape maps it first."""


def blocking_reviews(reviews, head="", decision=""):
    """[(login, review)] sorted by login: The reviewers whose latest decisive review (APPROVED, CHANGES_REQUESTED or DISMISSED; a plain comment is not decisive) is CHANGES_REQUESTED AND
    still blocks this head: it is at the PR's current head (its commit oid equals `head`), or its commit or the head is not known, or GitHub's
    `decision` (reviewDecision) is CHANGES_REQUESTED. A changes-requested review at an OLDER head is superseded by that reviewer's newer review of any
    kind (a comment included; this rule only) unless GitHub's decision still says CHANGES_REQUESTED; one at the CURRENT head is cleared only by that
    reviewer's later approval or dismissal, never by a comment, because several agents can share one login. Where GitHub gives no decision (no required
    reviews) an old-head review does not block: this is a guardrail, GitHub's branch protection is the boundary. `tagged_blockers` is separate."""
    latest = {}
    for review in reviews if isinstance(reviews, list) else []:
        if not isinstance(review, dict):
            continue
        author = ((review.get("author") or {}).get("login")) if isinstance(review.get("author"), dict) else None
        state = str(review.get("state") or "").upper()
        if author and state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            commit = review.get("commit") if isinstance(review.get("commit"), dict) else {}
            latest[author] = (state, commit.get("oid") if isinstance(commit.get("oid"), str) else "", review)
    blocked = str(decision or "").upper() == "CHANGES_REQUESTED"
    return sorted(((who, review) for who, (state, oid, review) in latest.items()
                   if state == "CHANGES_REQUESTED" and (blocked or not oid or not head or oid.lower() == head.lower())), key=lambda pair: pair[0])


def review_blockers(reviews, head="", decision=""):
    """The logins of `blocking_reviews`."""
    return [who for who, _ in blocking_reviews(reviews, head, decision)]
