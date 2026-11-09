# ---------------------------------------------------------------- `merge-ready` (ask #2265)
#
# Before an admin merge a lane runs `merge-ready <owner/repo#N>`. It states, in one place, the facts four wrong admin merges got wrong: the base is the repository's
# default branch; no review at ANY commit asked for changes or blocked, unless the SAME reviewer later approved at a later commit (that lifts their own earlier block and is named; another reviewer's approval or a dismissed one does not) (a dismissed request for changes or block is named with who dismissed it and counts as refused; a dismissed approval is a note); the captain label the
# repository's `captain_labels` entry names is on the pull request; every required check is green at the current head. It reads (GET only) and prints READY (exit 0) or one
# line per failed fact (exit 1). `--override "<who ordered it>: <reason>"` records an owner-ordered fix-forward merge: it appends ONE line to the switchboard's own
# audit file (a local write, never a GitHub write), prints OVERRIDDEN, and exits 6. It does not merge anything and it blocks nothing.
import json
import re
import sys

MERGE_READY_OVERRIDDEN = 6   # distinct from 0 (READY), 1 (refused), 2 (usage), 3 to 5 (other commands' holds)
_MR_BLOCK_WORD = re.compile(r"^\W*(?:BLOCK\b|\[BLOCK\])", re.I)
_MR_BLOCK_FINDING = re.compile(r"^- \[BLOCK\]", re.M)
_MR_GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED", "COMPLETED"}


def _mr_who_reason(text):
    """('who', 'reason') of `<who>: <reason>`, or None when either is empty (an override that names nobody or no reason is refused)."""
    who, sep, reason = str(text or "").partition(":")
    who, reason = " ".join(who.split()), " ".join(reason.split())
    return (who, reason) if sep and who and reason else None


def _mr_dicts(items):
    return [i for i in items if isinstance(i, dict)]


def _mr_login(user):
    return str((user or {}).get("login") or "unknown") if isinstance(user, dict) else "unknown"


def _mr_get(engine, runner, repo, path, paginate=False):
    """One GET through the engine's gh account (counted, never a write): the objects the answer holds (a page of a list is flattened). Raises NoticeError."""
    try:
        done = engine.accounts.read(runner, path, repo, engine.config["call_timeout_seconds"], paginate=paginate)
    except (GhRateLimited, AccountError) as err:
        raise NoticeError("gh api %s: %s" % (path.split("?")[0], _one_line(str(err), 160))) from None
    if done.returncode != 0:
        raise NoticeError("gh api %s failed: %s" % (path.split("?")[0], engine.accounts.scrub(_one_line(done.stderr or "", 160)) or "exit %d" % done.returncode))
    try:
        got = decode_json_stream(done.stdout or "")
    except ValueError:
        raise NoticeError("gh api %s answered something that is not JSON" % path.split("?")[0]) from None
    return [x for item in got for x in (item if isinstance(item, list) else [item]) if isinstance(x, dict)]


def merge_ready_facts(engine, runner, repo, number):
    """The list of failed facts (one line each) for the pull request. Raises NoticeError when a read fails: nothing is guessed."""
    pr = read_pr_json(engine, repo, number)
    head, base = str(pr.get("headRefOid") or ""), str(pr.get("baseRefName") or "")
    failed, dismissed_notes = [], []
    default = (_mr_dicts(_mr_get(engine, runner, repo, "repos/%s" % repo)) or [{}])[0].get("default_branch")
    pinned = (engine.config.get("merge_base") or {}).get(repo)
    expected, expected_name = (pinned, "the configured merge base %s" % pinned) if pinned else (default, "the default branch %s" % default)
    notes_base = ["base %s: %s" % (base, "the configured merge_base" if pinned else "the default branch")]
    if not expected:
        failed.append("base: the default branch of %s could not be read" % repo)
    elif base != expected:
        failed.append("base: the pull request is into %s, not %s" % (base or "(none)", expected_name))
    reviews = _mr_dicts(_mr_get(engine, runner, repo, "repos/%s/pulls/%d/reviews" % (repo, number), True))
    events = _mr_dicts(_mr_get(engine, runner, repo, "repos/%s/issues/%d/events" % (repo, number), True))
    dismissers, was = {}, {}
    for event in events:
        if event.get("event") == "review_dismissed" and isinstance(event.get("dismissed_review"), dict):
            dismissers[event["dismissed_review"].get("review_id")] = _mr_login(event.get("actor"))
            was[event["dismissed_review"].get("review_id")] = str(event["dismissed_review"].get("state") or "").upper()
    def _when(i):
        return (str(reviews[i].get("submitted_at") or ""), i)
    lifted_notes = []
    for review in reviews:   # EVERY review at EVERY commit, never the latest per reviewer
        state, body = str(review.get("state") or "").upper(), str(review.get("body") or "")
        at, who = str(review.get("commit_id") or "")[:8] or "an unknown commit", _mr_login(review.get("user"))
        by = dismissers.get(review.get("id"), "someone not found in the events")
        if state == "DISMISSED" and was.get(review.get("id")) == "APPROVED" and not (_MR_BLOCK_WORD.match(body) or _MR_BLOCK_FINDING.search(body)):
            dismissed_notes.append("%s's approval at %s was dismissed by %s (not a refusal)" % (who, at, by))
        elif state == "DISMISSED":
            failed.append("review: %s's review at %s was dismissed by %s (a dismissed request for changes or block counts as refused)" % (who, at, by))
        elif state == "CHANGES_REQUESTED" or _MR_BLOCK_WORD.match(body) or _MR_BLOCK_FINDING.search(body):
            mine = reviews.index(review)   # the same reviewer's LATER approval at a later commit lifts it (never another reviewer's, never a dismissed one)
            later = [r for i, r in enumerate(reviews) if _when(i) > _when(mine) and _mr_login(r.get("user")) == who and str(r.get("state") or "").upper() == "APPROVED" and not (_MR_BLOCK_WORD.match(str(r.get("body") or "")) or _MR_BLOCK_FINDING.search(str(r.get("body") or "")))
                     and str(r.get("commit_id") or "") != str(review.get("commit_id") or "")]
            if later:
                lifted_notes.append("%s's block at %s lifted by their later approval at %s" % (who, at, str(later[-1].get("commit_id") or "")[:8] or "an unknown commit"))
                continue
            failed.append("review: %s requested changes or blocked at %s%s" % (who, at, "" if at == head[:8] else " (an earlier head than %s)" % (head[:8] or "unknown")))
    captain = None if uses_spire_land(engine.config, repo) else (engine.config.get("captain_labels") or {}).get(repo)   # #2320: a `spire land` repository has no captain rule
    names = {str(l.get("name")) for l in _mr_dicts(pr.get("labels") or [])}
    if captain and captain not in names:
        failed.append("captain: the required captain label %s is missing" % captain)
    notes = notes_base + ([] if captain else ["no captain rule"]) + dismissed_notes + lifted_notes
    required, configured = set(), False
    try:
        protection = _mr_dicts(_mr_get(engine, runner, repo, "repos/%s/branches/%s/protection/required_status_checks" % (repo, base)))
        if protection:   # branch protection, when present
            configured = True
            required |= set(protection[0].get("contexts") or []) | {c.get("context") for c in _mr_dicts(protection[0].get("checks") or [])}
    except NoticeError as err:
        if "404" not in str(err):
            failed.append("checks: the required checks could not be read (%s)" % _one_line(str(err), 120))
            return failed, notes
    try:   # the rulesets that apply to the base branch
        for rule in _mr_dicts(_mr_get(engine, runner, repo, "repos/%s/rules/branches/%s" % (repo, base))):
            if rule.get("type") == "required_status_checks":
                configured = True
                params = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
                required |= {c.get("context") for c in _mr_dicts(params.get("required_status_checks") or [])}
    except NoticeError as err:
        failed.append("checks: the required checks could not be read (%s)" % _one_line(str(err), 120))
        return failed, notes
    if not configured:
        notes.append("no required checks configured: %s has no branch protection and no ruleset rule" % base)
    else:
        seen = {}
        for check in _mr_dicts(pr.get("statusCheckRollup") or []):
            seen[str(check.get("name") or check.get("context"))] = str(check.get("conclusion") or check.get("state") or check.get("status") or "").upper()
        for name in sorted(n for n in required if n):
            if name not in seen:
                failed.append("checks: required check %s has no result at the head %s" % (name, head[:8]))
            elif seen[name] not in _MR_GREEN:
                failed.append("checks: required check %s is %s at the head %s" % (name, seen[name] or "not finished", head[:8]))
    return failed, notes


def cmd_merge_ready(args, runner, now):
    """`merge-ready <owner/repo#N>`: READY (0) or one line per failed fact (1); with `--override "<who>: <reason>"` the failed facts are put in the audit and it exits 6."""
    given = None
    if args.override is not None:
        given = _mr_who_reason(args.override)
        if given is None:
            sys.stderr.write("%s: merge-ready: --override needs \"<who ordered it>: <reason>\", both not empty\n" % PROG)
            return EXIT_USAGE
    try:
        repo, number = parse_pr_ref(args.pr)
    except NoticeError as err:
        sys.stderr.write("%s: %s\n" % (PROG, _one_line(str(err), 300)))
        return EXIT_USAGE
    engine, _, _ = build_engine(runner, now, "cli", writable=given is not None)
    try:
        failed, notes = merge_ready_facts(engine, runner, repo, number)
    except NoticeError as err:
        sys.stderr.write("%s: merge-ready: %s\n" % (PROG, _one_line(str(err), 300)))
        return EXIT_ERROR   # a read that failed is never READY
    ref = "%s#%d" % (repo, number)
    if not failed:
        print("READY %s%s" % (ref, "".join(" (%s)" % n for n in notes)))
        return EXIT_OK
    if given is not None:
        engine.audit.append("cli", "merge_ready.override", ref, {"failed": failed, "who": given[0], "reason": given[1]})
        print("OVERRIDDEN by %s: %s" % (given[0], "; ".join(failed)))
        return MERGE_READY_OVERRIDDEN
    for line in failed:
        print("NOT READY %s: %s" % (ref, line))
    return EXIT_ERROR


cmd_merge_ready.direct = True   # no error mapping, no daemon start; GET only
cmd_merge_ready.guard_records = True   # its name says `merge`: on a tree the installed-tree guard cannot read it refuses, so a READY never comes from a tree that is not known to be the installed one


def add_merge_ready_parsers(sub):
    ready = sub.add_parser("merge-ready", help="Before an admin merge: READY, or one line per failed fact (base, reviews at every commit, hold label, required checks); read-only",
                           description="Reads the pull request (GET only) and says READY (exit 0) only when its base is the default branch, no review at any commit requested changes or "
                                       "blocked (a dismissed request for changes or block counts as refused and names who dismissed it; a dismissed approval is a note), the repository's required hold label is present and every required "
                                       "check is green at the head. Otherwise one line per failed fact, exit 1. --override records an owner-ordered merge in the local audit (exit 6).")
    ready.add_argument("pr", help="The pull request: owner/repo#N or its URL")
    ready.add_argument("--override", metavar="WHO: REASON", help="Record an owner-ordered fix-forward merge by name: one line in the switchboard audit, OVERRIDDEN printed, exit 6")
