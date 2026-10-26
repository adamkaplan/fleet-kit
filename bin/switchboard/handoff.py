# ---- handoff: a brief, not a fork

class HandoffRefused(Exception):
    """A handoff that was refused before anything was written."""


BRIEF_LIMIT = 20000  # characters; a GitHub comment holds 65536
BRIEF_LABELS = ("goal", "done so far", "next steps", "watch out for")
_BRIEF_INLINE = re.compile(r"^\s{0,3}(?:#{1,6}\s+)?(?:[-*]\s+)?\**(%s)\**\s*:\**\s*(.*?)\s*$"
                           % "|".join(BRIEF_LABELS), re.IGNORECASE)
_BRIEF_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(%s)\s*#*\s*$" % "|".join(BRIEF_LABELS), re.IGNORECASE)
_BRIEF_TITLE = re.compile(r"^\s{0,3}#{1,6}\s+handoff\b", re.IGNORECASE)


def parse_brief(text):
    """{label: text} from a Chief of Staff's brief: sections named `Done so far:`, `Next steps:`,
    `Watch out for:` (and a `Goal:`, which the switchboard replaces). Anything else is refused,
    so no written word is silently dropped. Raises HandoffRefused."""
    if len(text) > BRIEF_LIMIT:
        raise HandoffRefused("the brief is %d characters; the limit is %d: say less, link more" % (len(text), BRIEF_LIMIT))
    sections, label, stray = {}, None, []
    for line in text.splitlines():
        found = _BRIEF_INLINE.match(line) or _BRIEF_HEADING.match(line)
        if found:
            label = found.group(1).lower()
            sections[label] = [found.group(2)] if found.lastindex == 2 and found.group(2) else []
        elif label is not None:
            sections[label].append(line)
        elif line.strip() and not _BRIEF_TITLE.match(line):
            stray.append(line.strip())
    if stray:
        raise HandoffRefused("the brief has text outside its sections (%s): put it under Done so far, Next steps "
                             "or Watch out for" % _one_line(stray[0], 80))
    parsed = {name: "\n".join(lines).strip() for name, lines in sections.items()}
    missing = [name.capitalize() for name in ("done so far", "next steps") if not parsed.get(name)]
    if missing:
        raise HandoffRefused("the brief has no %s: a subagent cannot continue from a brief without them"
                             % " and no ".join(missing))
    return parsed


def goal_line(issue):
    """Goal, from the ask's own Intent and Done-when: never retyped by the Chief of Staff."""
    line = issue.intent or NO_INTENT
    return "%s (Done when: %s)" % (line, issue.done_when) if issue.done_when else line


def handoff_markdown(issue, parsed):
    """The doc's handoff markdown for ask `issue` (an IssueIntent) and a parsed brief."""
    lines = ["## Handoff: ask %d" % issue.number, "Goal: %s" % goal_line(issue),
             "Done so far: %s" % parsed["done so far"], "Next steps: %s" % parsed["next steps"],
             "Watch out for: %s" % (parsed.get("watch out for") or "nothing noted")]
    return "\n".join(lines)


def post_ask_comment(engine, issue, body, author=None, repo=None):
    """Comment on the ask through the configured `gh`. The GitHub writes the switchboard makes: the handoff
    brief, so the record survives (docs: Hand off), and an agent's own comment (`comment`). Signed with
    the author's name when there is one, so it is never delivered back as an event. Audited before and after."""
    reader = engine.intents
    if author:
        body = body.rstrip("\n") + "\n\n" + fleet_mark(author)
    repo = repo or reader.repo
    path = "repos/%s/issues/%d/comments" % (repo, issue)
    subject = "issue:%d" % issue if repo == reader.repo else "issue:%s#%d" % (repo, issue)
    with engine._external("github.comment", subject, {"repo": repo, "issue": int(issue), "chars": len(body)}) as result:
        env = reader.env_for(repo)
        try:
            done = reader.runner([reader.gh, "api", path, "--method", "POST", "--input", "-"],
                                 input=json.dumps({"body": body}), capture_output=True, text=True,
                                 timeout=reader.timeout, **({"env": env} if env is not None else {}))
        except subprocess.TimeoutExpired:
            raise IntentError("unavailable", "gh did not answer within %ss" % reader.timeout) from None
        except OSError as err:
            raise IntentError("unavailable", "cannot run %s: %s" % (reader.gh, err.strerror or err)) from None
        if done.returncode != 0:
            detail = _one_line(done.stderr) or _one_line(done.stdout) or "exit %d" % done.returncode
            raise IntentError("gh", reader.accounts.scrub(detail) if reader.accounts else detail)
        try:
            posted = json.loads(done.stdout)
        except ValueError:
            posted = {}
        url = posted.get("html_url") if isinstance(posted, dict) else None
        result["url"] = url if isinstance(url, str) else None
    return result["url"]


def handoff_refusals(engine, issue, name, directory, workspace, agent, repo=None):
    """Why nothing may be written, found before anything is: the ask, the agent's name and place.
    Returns the ask (an IssueIntent)."""
    if engine.intents is None:
        raise HandoffRefused("'intent_repo' is not configured: a handoff fills Goal from the ask and posts "
                             "the brief on it")
    ask = engine.intents.issue(issue, repo)
    if ask.charter:
        raise HandoffRefused("#%d is a charter, not an ask: hand off an ask under it" % issue)
    if ask.state != "open":
        raise HandoffRefused("ask #%d is %s: there is nothing to hand off" % (issue, ask.state or "not open"))
    problems = launch_problems(name, agent, directory, workspace, issue)
    if problems:
        raise HandoffRefused("; ".join(problems))
    taken = engine.discover(engine.now()).agents.get(name)
    if taken is not None:
        raise HandoffRefused("a fleet agent named %s already runs in pane %s: send it what is new with "
                             "fleet-switchboard send %s --issue %d" % (name, taken.pane, name, issue))
    return ask


def subagent_prompt(caller, issue, markdown):
    return ("%s\n\nYou are a background subagent for %s. Report with: fleet-switchboard send %s --issue %d \"...\" "
            "when you reach the ask's Done when, or when you are stuck." % (markdown, caller, caller, issue))


def handoff_ask(engine, caller, issue, brief, name=None, directory=None, workspace=None, agent=SUBAGENT_AGENT,
                patience=LAUNCH_PATIENCE, sleep=time.sleep, repo=None):
    """The Chief of Staff's handoff: check, post the brief on the ask, launch the subagent. Returns a dict.

    Everything that can be refused is refused before the comment is posted.
    A launch that fails after the comment says so: the record stands.
    """
    explicit = issue_repo(issue)
    repo = repo or explicit
    issue = int(issue)
    # The subagent's own metadata names a repo only when the caller did, or when several repos are in play:
    # with one repo its metadata is what it always was.
    record = explicit or (repo if len(engine.repos_in_play()) > 1 else None)
    name = name or "cos-%d" % issue
    parsed = parse_brief(brief)
    ask = handoff_refusals(engine, issue, name, directory, workspace, agent, repo)
    markdown = handoff_markdown(ask, parsed)
    url = post_ask_comment(engine, issue, markdown, author=caller, repo=repo)
    try:
        done = launch_agent(engine, name, agent, directory, workspace, COS_SUBAGENT, caller, issue,
                            subagent_prompt(caller, issue, markdown), patience, sleep, repo=record)
    except LaunchError as err:
        raise LaunchError("the brief was posted on ask #%d, but the subagent did not start: %s" % (issue, err)) from err
    return dict(done, issue=issue, reports_to=caller, role=COS_SUBAGENT, comment=url,
                replaced_goal="goal" in parsed)


def cmd_handoff(args, runner, now, socket_call=None):
    brief = read_brief(args.brief_file, sys.stdin)
    engine, _, _ = build_engine(runner, now, "cli", socket_call=socket_call)
    workspace = caller_workspace(engine, args.workspace)
    caller = identify_caller(engine.herdr, engine.v2, os.environ)
    try:
        repo, number = resolve_for_sender(engine, engine.config, args.issue, caller)
        done = handoff_ask(engine, caller, RepoIssue(number, issue_repo(args.issue)), brief, args.name,
                           args.dir or os.getcwd(), workspace, repo=repo)
    except (HandoffRefused, IssueRefError) as err:
        sys.stderr.write("%s: handoff refused: %s\n" % (PROG, err))
        return EXIT_USAGE
    if done.pop("replaced_goal"):
        sys.stderr.write("%s: the Goal line in your brief was replaced by the ask's own Intent and Done when\n" % PROG)
    print(json.dumps(done, sort_keys=True))
    return EXIT_OK


cmd_handoff.ensure_on_use = True   # the daemon is alive when it runs


def cmd_comment(args, runner, now):
    """Post a comment as the calling agent, signed so the switchboard does not deliver it back."""
    engine, _, config = build_engine(runner, now, "cli")
    if engine.intents is None:
        raise ConfigError("'intent_repo' is not configured in %s: set it to OWNER/REPO" % config_path())
    body = read_brief(args.body_file, sys.stdin)
    if body is None:
        raise IntentError("usage", "--body-file is required (a file, or - for stdin)")
    caller = identify_caller(engine.herdr, engine.v2, os.environ)
    try:
        repo, number = resolve_for_sender(engine, config, args.issue, caller)
    except IssueRefError as err:
        sys.stderr.write("%s: %s\n" % (PROG, err))
        return EXIT_USAGE
    url = post_ask_comment(engine, number, body, author=caller, repo=repo)
    print(json.dumps({"issue": number, "author": caller, "comment": url}, sort_keys=True))
    return EXIT_OK


cmd_comment.ensure_on_use = True   # the daemon is alive when it runs


def cmd_whoami(args, runner, now):
    """The calling agent's fleet name: what the `gh` shim signs a comment with. Exit 1, silent on stdout,
    when the caller is not a fleet agent."""
    engine, _, config = build_engine(runner, now, "cli", writable=False)
    if getattr(args, "json", False):
        fleet = caller_fleet(engine.herdr, engine.v2, os.environ)
        repo = fleet.get("repo") if isinstance(fleet.get("repo"), str) else None
        own = repo or _fleet_repo(fleet, config)
        # `own` is false when the repo is only the config's `intent_repo` default: an agent with no repo of its own (the Chief of Staff) has no account of
        # its own to keep, so the gh shim may use the account of the repo a command names
        who = {"name": fleet["name"], "repo": own or config.get("intent_repo") or None, "own": bool(own), "role": fleet.get("role") if isinstance(fleet.get("role"), str) else None}   # the RECORDED role: the gh shim gives only the Chief of Staff the named repository's App token
        if "read_repo" in fleet:   # #618: only a worker launched with --read-repo has the key, so every other answer is as it was; one that cannot be read says so (the shim refuses)
            who["read_repo"] = fleet["read_repo"] if isinstance(fleet["read_repo"], str) and GhAccounts.normal_repo(fleet["read_repo"]) == fleet["read_repo"] \
                and re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", fleet["read_repo"]) else False
        print(json.dumps(who, sort_keys=True))
        return EXIT_OK
    if getattr(args, "repo", False):
        fleet = caller_fleet(engine.herdr, engine.v2, os.environ)
        repo = fleet.get("repo") if isinstance(fleet.get("repo"), str) else None
        print(repo or _fleet_repo(fleet, config) or config.get("intent_repo") or "")
        return EXIT_OK
    print(identify_caller(engine.herdr, engine.v2, os.environ))
    return EXIT_OK


def add_foreground_parsers(sub):
    whoami = sub.add_parser("whoami", help="Print your fleet name (for scripts: the gh shim signs with it)")
    whoami.add_argument("--json", action="store_true", help="Print {\"name\", \"repo\"} on one line: the gh shim's single "
                        "lookup (name for the signature, repo for the account)")
    whoami.add_argument("--repo", action="store_true", help="Print the repo of your resolved identity "
                                       "(for the gh shim, which picks the account from it); empty when it has none")
    comment = sub.add_parser("comment", help="Comment on an issue as yourself, signed so it does not come back to "
                                             "you as an event")
    comment.add_argument("--issue", type=issue_arg, required=True, help="The issue to comment on")
    comment.add_argument("--body-file", dest="body_file", required=True, help="The comment: a file, or - for stdin")
    handoff = sub.add_parser("handoff", help="Hand your current work to a background subagent: post the brief on "
                                             "the ask and launch it, without typing")
    handoff.add_argument("--issue", type=issue_arg, required=True, help="The ask being handed off")
    handoff.add_argument("--brief-file", dest="brief_file", required=True,
                         help="The brief (Done so far, Next steps, Watch out for): a file, or - for stdin")
    handoff.add_argument("--name", help="The subagent's fleet name (default: cos-<issue>)")
    handoff.add_argument("--dir", help="The subagent's working directory (default: here)")
    handoff.add_argument("--workspace", help="The herdr workspace for its tab (default: the workspace of this session, else $HERDR_WORKSPACE_ID)")


# ======================================================================
# Worker events, reminders and toasts (PR 4)
#
# Two fact sources, registered in fact_sources(): WorkerFacts derives "a worker
# finished" and "a worker is blocked" from herdr and v2 on every pass, and
# ReminderFacts turns the reminders file into facts once they are due. Neither
# stores anything about delivery. The only new state is reminders.json.
# Hooks into the engine: Switchboard.__init__ binds each source that has a
# `bind`, discover() keeps the newest Discovery, deliver() calls
# note_unreachable, render's toast candidates include source_toasts(), and
# run_pass() ends with finish_pass.
# ======================================================================

WORKER_IDLE = "worker.idle"
WORKER_BLOCKED = "worker.blocked"
WORKER_STOPPED = "worker.stopped"  # PR 13: a worker stopped without reporting and the boss must know
NUDGE = "nudge"                    # PR 13: the one reminder to a worker that stopped without reporting
REPORT = "report"
REPORT_ATTENTION = ("done", "failed", "blocked", "question")  # wake the boss when it is not engaged
REPORT_PROGRESS = ("working", "paused", "withdrawn")          # riders: carried, never the reason for a message
REPORT_STATES = REPORT_ATTENTION + REPORT_PROGRESS
WITHDRAW_ALL = decision_core.WITHDRAW_ALL  # leads a `withdrawn` report made with --all: it clears every open question of the worker
STALLED_LIST_MAX_AGE = 1200   # #811: seconds; an open-PR list read longer ago than this is not judged (the pass re-reads it every few minutes)
STALLED_RUNNING_CHECKS = ("in_progress", "queued", "pending", "waiting", "requested", "expected")   # #811: a check word that is not finished


def _stalled_age(seconds):
    hours = seconds / 3600.0
    return "%dm" % (seconds // 60) if hours < 1 else "%.1fh" % hours


class _Unread(Exception):
    """A guard's input could not be read: the predicate answers "unread" and the caller says nothing."""


def _guard_last_report(w):
    if isinstance(w.last, dict) and w.last.get("state") in ("blocked", "question"):
        return ["its last report is %s" % w.last["state"]]
    return []


def _label_names(labels):
    return {(l.get("name") if isinstance(l, dict) else l) for l in (labels or [])}


def _guard_awaiting_issue(w):
    return ["an awaiting label on the issue" if r["number"] == w.issue else "an awaiting label on #%d" % r["number"]
            for r in w.issues if _label_names(r.get("labels")) & w.awaiting]


def _guard_awaiting_pr(w):
    return ["an awaiting label on pull request #%d" % r["number"] for r in w.mine
            if _label_names(r.get("labels")) & w.awaiting]


def _guard_ci(w):
    found = []
    for r in w.mine:
        number, head = r["number"], (r.get("head") or {}).get("sha") if isinstance(r.get("head"), dict) else None
        held = (getattr(w.state, "pr_state", None) or {}).get("%s#%d" % (w.key, number)) or {}
        if held.get("head") == head and held.get("examined_head") == head and any(
                isinstance(c, dict) and str(c.get("conclusion") or "").lower() in STALLED_RUNNING_CHECKS for c in (held.get("checks") or [])):
            found.append("CI is running at the head of #%d" % number)
    return found


def _guard_review_requested(w):
    return ["a review is requested on #%d" % r["number"] for r in w.mine if not r.get("draft") and (r.get("requested_reviewers") or r.get("requested_teams"))]


def _guard_review_window(w):
    found = []
    for r in w.mine:
        if r.get("draft") or r.get("requested_reviewers") or r.get("requested_teams"):
            continue   # a draft awaits no review; a requested one is the guard above
        created = parse_iso(r.get("created_at"))
        if created is None:
            raise _Unread()   # the age of the pull request cannot be told: fails toward saying nothing
        if w.now - created < w.window:
            found.append("#%d is younger than the review window" % r["number"])
    return found


def _guard_reminder(w):
    try:
        reminders = read_reminders(w.engine.state_dir)
    except ReminderError:
        raise _Unread() from None   # an unreadable file, or a reminder with no readable time (read_reminders refuses it): fails toward saying nothing
    for reminder in reminders:
        if (reminder.get("to") == w.observation.name and _is_issue_number(reminder.get("issue")) and reminder["issue"] in w.numbers and reminder["at"] > w.now
                and "while_running" not in (reminder.get("condition") or {})):   # the checkpoint `launch` sets is the boss's, not the worker waiting
            return ["it set a reminder for later"]
    return []


def _guard_decision(w):
    body = read_decisions_file(w.engine.state_dir / DECISIONS_FILE)
    if body is None:
        raise _Unread()
    refs = {(w.key, n) for n in w.numbers} | {(w.key, r["number"]) for r in w.mine}
    for d in body["decisions"]:
        if not isinstance(d, dict):
            continue
        ref = sweep_ref_of(d, w.repo)
        if d.get("tier", "human") == "human" and ref and (plan_ledger.normal_repo(ref[0]), ref[1]) in refs:
            return ["a decision waits on the owner"]
    return []


def _owned_issue(ctx):
    return [r for r in ctx.records if isinstance(r, dict) and r.get("number") == ctx.issue and "pull_request" not in r][:1]


def _owned_work_items(ctx):
    """The issues below the assignment through the parent links of the issue list: a visited set and a depth bound make a cycle end the walk."""
    plain = [r for r in ctx.records if isinstance(r, dict) and isinstance(r.get("number"), int) and "pull_request" not in r]
    found, seen, frontier = [], {ctx.issue}, {ctx.issue}
    for _ in range(TRIAGE_ANCESTOR_DEPTH):   # the one bound on parent links (defined with the triage walk below; read at call time)
        nxt = set()
        for r in plain:
            parent, url = parent_of(r), r.get("parent_issue_url")
            if r["number"] in seen or parent not in frontier or (isinstance(url, str) and not re.search(r"/repos/%s/issues/" % re.escape(ctx.key), url, re.I)):
                continue   # another repository's parent is not this assignment's
            found.append(r)
            seen.add(r["number"])
            nxt.add(r["number"])
        frontier = nxt
        if not frontier:
            break
    return found


def _owned_pulls(ctx):
    return [r for r in ctx.pulls if isinstance(r, dict) and isinstance(r.get("number"), int) and any(
        isinstance(ref, list) and ref[1] in ctx.numbers and plan_ledger.normal_repo(ref[0] or ctx.repo) == ctx.key for ref in pr_body_refs(r.get("body")))]


# #874: THE table of what a worker owns: kind -> (where the daemon already holds it, the builder). `worker_owned` LOOPS OVER it in this order (a
# later kind may read the numbers of the earlier ones), so a kind cannot exist in code and not here; the tests are parametrised over it.
OWNED_KINDS = {
    "issue": ("the assignment's issue, from the triage issue list", _owned_issue),
    "work item": ("its sub-issues at any depth (bounded, cycle-safe), from the same list's parent links", _owned_work_items),
    "pull request": ("every open pull request whose strict references (pr_body_refs) name the assignment or a work item, any base branch, from the open-pull-request list", _owned_pulls),
}


def worker_owned(records, pulls, issue, repo):
    """#874: THE one function that says what a worker owns, from lists the daemon already holds (no read): `issues` (the assignment first, then its
    work items), `pulls` (every open pull request naming any of them through a strict reference, whatever its base branch) and `numbers`. None when
    the assignment is not in the list (not judged). The caller has already checked that the lists were read whole."""
    ctx = types.SimpleNamespace(records=records, pulls=pulls, issue=issue, repo=repo, key=plan_ledger.normal_repo(repo), numbers=set())
    found = {}
    for kind, (_, build) in OWNED_KINDS.items():
        found[kind] = build(ctx)
        if kind != "pull request":
            ctx.numbers |= {r["number"] for r in found[kind]}
        if kind == "issue" and not found[kind]:
            return None
    return types.SimpleNamespace(issues=found["issue"] + found["work item"], numbers=ctx.numbers, pulls=found["pull request"])


def _readable_time(r):
    return parse_iso(r.get("updated_at")) is not None


def _readable_head(r):
    head = r.get("head")
    return isinstance(head, dict) and isinstance(head.get("sha"), str) and bool(head["sha"])


# #874: THE table of what must be readable on what a worker owns before a rule may judge it: member -> (which owned items, the check). `owned_readable`
# LOOPS OVER it, so a member cannot be checked in code and not here; the tests are parametrised over it, each member in each position of the list.
OWNED_READABLE = {
    "assignment time": (lambda owned: owned.issues[:1], _readable_time),
    "work item time": (lambda owned: owned.issues[1:], _readable_time),
    "pull request head": (lambda owned: owned.pulls, _readable_head),
    "pull request time": (lambda owned: owned.pulls, _readable_time),
}


def owned_readable(owned):
    """True when every member of OWNED_READABLE reads on every owned item it covers (default-deny: one bad item anywhere is not readable)."""
    return all(check(r) for select, check in OWNED_READABLE.values() for r in select(owned))


# #874: every consumer of `worker_owned` and what it takes from the set. The two rules and every row of WAITING_GUARDS; a completeness test fails when
# one is added to the code without a row here.
WORKER_OWNED_CONSUMERS = {
    "stalled-working": "the newest movement of every owned issue and pull request, and the head of each owned pull request",
    "idle-at-a-seam": "the waiting verdict only",
    "last-report": "nothing from the set (the report record only)",
    "awaiting-issue": "an awaiting label on any owned issue",
    "awaiting-pr": "an awaiting label on any owned pull request",
    "ci": "CI running at the head of any owned pull request",
    "review-requested": "a review requested on any owned pull request",
    "review-window": "any owned pull request younger than the review window",
    "reminder": "a reminder for the worker naming any owned issue",
    "decision": "a human decision on any owned issue or pull request",
}


# #816: THE table of waiting guards: name -> (needs the daemon's lists, check). The predicate LOOPS OVER it, so a guard cannot exist in code and
# not here, and the tests are parametrised over it (a new row fails the completeness test until its case is added).
WAITING_GUARDS = {
    "last-report": (False, _guard_last_report),
    "awaiting-issue": (True, _guard_awaiting_issue),
    "awaiting-pr": (True, _guard_awaiting_pr),
    "ci": (True, _guard_ci),
    "review-requested": (True, _guard_review_requested),
    "review-window": (True, _guard_review_window),
    "reminder": (True, _guard_reminder),
    "decision": (True, _guard_decision),
}


def legitimately_waiting(engine, observation, issue, repo, last):
    """#816: THE shared predicate of the idle-at-a-seam rule and the stalled-working rule. Reads only what the daemon already holds (the last
    report record, the issue list, the open-PR list and its cached check state, the reminders, the decision list), never GitHub.
    Returns a namespace: `state` is "unread" (an input was not read whole or not readable: the caller says nothing), "waiting" (`codes` names the
    guards from WAITING_GUARDS that hold, `reason` says why: the caller says nothing and the stretch is NOT consumed because nothing is stored)
    or "clear"; `issue_at`, `pr`, `number`, `head`, `pr_at` describe what it found for the caller's movement judgement. A review is pending when
    one is requested on a non-draft pull request, or such a pull request is younger than `review_window_minutes` (the list holds no reviews, so
    "no review yet" is the window). Without the daemon's whole lists (a repository outside `plan_gates` never gets its open-PR list read) the
    answer is "unread": both rules are silent there, by the read-whole rule."""
    out = types.SimpleNamespace(state="unread", reason="", codes=[], issue_at=None, pr=None, number=None, head=None, pr_at=None,
                                issues=[], pulls=[], heads={})
    now, key = engine.now(), plan_ledger.normal_repo(repo)
    w = types.SimpleNamespace(engine=engine, observation=observation, issue=issue, repo=repo, key=key, last=last, now=now, mine=[],
                              awaiting=set(), issues=[], numbers={issue}, state=getattr(engine, "ci_record_state", None), window=engine.config.get("review_window_minutes", 60) * 60)
    found = []
    def run(early):
        for name, (needs, check) in WAITING_GUARDS.items():
            if needs != (not early):
                continue
            for text in check(w):
                found.append((name, text))
    try:
        run(True)
        if found:
            out.state, out.codes, out.reason = "waiting", [n for n, _ in found], "; ".join(t for _, t in found)
            return out
        if any(not whole(engine, name)[0] for name in ("issues:" + repo, "decisions", "pulls:" + key)):
            return out
        listed_at = engine.read_whole["pulls:" + key].get("at")
        if not _is_number(listed_at) or now - listed_at > STALLED_LIST_MAX_AGE:
            return out   # an open-PR list that is not being re-read is no evidence
        units = getattr(getattr(engine, "triage", None), "units", None) or {}
        records = (units.get((repo, "issues")) or {}).get("data") or []
        pulls = ((getattr(w.state, "lists", None) or {}).get(key) or (0, []))[1]
        owned = worker_owned(records, pulls, issue, repo)
        if owned is None:
            return out
        w.issues, w.numbers, w.mine = owned.issues, owned.numbers, owned.pulls
        if not owned_readable(owned):
            return out   # an owned item whose time or head cannot be read is an input not read whole: not judged
        out.issue_at = max(parse_iso(r.get("updated_at")) for r in owned.issues)
        out.issues, out.pulls = owned.issues, owned.pulls
        stamps = [(parse_iso(r.get("updated_at")), r["number"], r["head"]["sha"]) for r in owned.pulls]
        if stamps:
            out.pr_at, out.number, out.head = max(stamps)   # the newest-moved one when several are owned
            out.pr = next(r for r in owned.pulls if r["number"] == out.number)
            out.heads = {n: h for _, n, h in stamps}
        w.awaiting = triage_awaiting_labels(engine)
        run(False)
    except _Unread:
        out.state = "unread"
        return out
    out.state, out.codes, out.reason = ("waiting", [n for n, _ in found], "; ".join(t for _, t in found)) if found else ("clear", [], "")
    return out


SEAM_IDLE_SECONDS = 1200   # #773: an idle worker whose last report was `working` is noticed after this long
REPORT_MAX = 300  # characters: a report is a sentence; detail goes on GitHub
SUMMARY_CAP = 600  # characters of a worker's last reply in its fact
LAB_WORKER_SINCE_ENV = "FLEET_SWITCHBOARD_LAB_WORKER_SINCE"
REMINDERS = "reminders.json"
REMINDER_LOCK = "reminders.lock"
REMINDER_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def worker_horizon(config, now):
    """The oldest `idle_at` that still counts: `worker_lookback_hours` back, so a
    restart does not resurface ancient completions. In the Lab, the environment
    may name a later start (LAB_WORKER_SINCE_ENV, epoch seconds), so the warm-up
    turns that precede a play are not news; it is ignored outside the Lab."""
    horizon = now - config["worker_lookback_hours"] * 3600
    raw = os.environ.get(LAB_WORKER_SINCE_ENV) if os.environ.get(LAB_ENV) == "1" else None
    if raw:
        try:
            horizon = max(horizon, float(raw))
        except ValueError:
            raise FactError("%s must be epoch seconds, got %r" % (LAB_WORKER_SINCE_ENV, raw)) from None
    return horizon


def reply_text(message):
    """The text of one v2 assistant message: `text`, or its text parts."""
    if not isinstance(message, dict):
        return ""
    text = message.get("text")
    if not isinstance(text, str):
        parts = message.get("content") if isinstance(message.get("content"), list) else []
        text = " ".join(part.get("text", "") for part in parts
                        if isinstance(part, dict) and isinstance(part.get("text"), str))
    return text


def _issue_of(fleet):
    issue = fleet.get("issue") if isinstance(fleet, dict) else None
    return issue if isinstance(issue, int) and not isinstance(issue, bool) and issue >= 1 else None


def _boss_of(observation):
    """Who a worker reports to, or None when it reports to nobody (or to itself)."""
    boss = observation.fleet.get("reports_to")
    return boss if isinstance(boss, str) and boss and boss != observation.name else None


class WorkerFacts:
    """Facts about workers (fleet agents that have a `reports_to`), derived on every call.

    `worker.idle:<session>:<idle_at ms>`: its turn ended (herdr says idle, v2's
    `time.idle` is set and within the look-back). For its `reports_to`.
    `worker.blocked:<session>:<request id>`: herdr says blocked, and v2 lists a
    pending permission request or form. For its `reports_to`; the human also
    gets a toast (see `toasts`).
    Nothing is stored: the same state gives the same facts (invariant 1).
    """

    __name__ = "worker_facts"

    def __init__(self):
        self.engine = None
        self._blocked = []       # toasts of the last call: (agent, reason, title, body)
        self.requests = []       # PR 14: the pending requests of every blocked agent in the last call (decisions)
        self._summaries = {}     # (session, idle ms) -> text; a cache, rebuilt after a restart
        self._stops = {}         # (session, idle ms) -> the facts of that stop (PR 13); a cache, rebuilt likewise
        self._heads = {}         # #811, #874: worker -> {owned pull request number: (the head last seen, when a change of it was seen)}; memory only
        self._no_forms = False   # v2 does not know the form operation (UNVERIFIED name)

    def bind(self, engine):
        self.engine = engine

    def toasts(self):
        return list(self._blocked)

    def __call__(self):
        self._blocked, self.requests = [], []
        engine = self.engine
        discovery = getattr(engine, "discovery", None)
        if engine is None or discovery is None or fault("no-worker-source"):
            if engine is not None:
                read_mark(engine, "requests", False, "no worker source was read", engine.now())   # #805: an earlier whole mark is not this pass's
            return []
        read_mark(engine, "requests", False, "the worker read did not finish", engine.now())   # #805: never inherited: whole only at the end of a read that completed
        horizon = worker_horizon(engine.config, engine.now())
        facts, skipped = [], []
        for observation in sorted(discovery.agents.values(), key=lambda o: o.name):
            boss = boss_name(discovery, observation)  # #102: the stop triage looks the boss up by its registered name
            if observation.conflict or observation.problem:
                skipped.append(observation.name)
                continue
            try:
                if boss is not None:
                    facts.extend(self.finished(observation, boss, horizon))
                    facts.extend(self.stalled_facts(observation, boss))
                facts.extend(self.blocked(observation, boss))
            except V2Error as err:  # one worker's trouble never hides the others
                skipped.append(observation.name)
                engine._note("source.error", "worker_facts:" + observation.name, {"error": _one_line(str(err))})
        # #805: the pending requests are whole only when every worker was read: an unread worker's requests are absent, not gone
        problems = list(getattr(discovery, "problems", None) or [])
        why = "the fleet could not be read whole" if problems else ("%d worker(s) not read: %s" % (len(skipped), ", ".join(skipped[:3])) if skipped else "")
        read_mark(engine, "requests", not problems and not skipped, why, engine.now())
        return facts

    def finished(self, observation, boss, horizon):
        """The facts of one worker's finished turn. PR 13: a bare idle is a rider; whether the boss must hear
        about the stop is `stop_facts`'s question."""
        if observation.status != "idle" or observation.idle_at is None or observation.idle_at < horizon:
            return []
        idle_ms = int(round(observation.idle_at * 1000))
        key = "%s:%s:%d" % (WORKER_IDLE, observation.session_id, idle_ms)
        head = "%s finished its turn (%s)" % (observation.name, observation.outcome or "no outcome recorded")
        reply = self.last_reply(observation.session_id, idle_ms)
        idle = Fact(key, boss, WORKER_IDLE, head + (": " + reply if reply else ""), observation.idle_at,
                    issue=_issue_of(observation.fleet), sender=observation.name, rider=not fault("idle-wakes"))
        idle.clean = observation.outcome in QUIET_OUTCOMES  # only a clean finish may be folded into a quiet line
        found = self.stop_facts(observation, boss, idle_ms, idle, reply)
        if len(found) == 1 and found[0].kind == WORKER_IDLE:   # nothing else is said about this stop: it may be an idle at a seam (#773)
            found = found + self.seam_facts(observation, boss, idle_ms)
        return found

    def seam_applies(self, observation, issue):
        """#793: the idle-at-a-seam rule is for a worker whose launch-record role is `coder` or `tester` and whose issue is not a charter.
        A charter is known from memory only (no new read): the issue is the `charter` of this worker's own record or of any discovered
        agent's. (A closed charter is already closed.) A role that cannot be read is not a coder or tester."""
        fleet = observation.fleet if isinstance(observation.fleet, dict) else {}
        if fleet.get("role") not in ("coder", "tester") or not isinstance(fleet.get("role"), str):
            return False
        engine = self.engine
        repo = _fleet_repo(fleet, engine.config) or engine.config.get("intent_repo")
        discovery = getattr(engine, "discovery", None)
        for agent in ([observation] + (list(discovery.agents.values()) if discovery is not None else [])):
            other = agent.fleet if isinstance(agent.fleet, dict) else {}
            if other.get("charter") == issue and not isinstance(other.get("charter"), bool):
                if agent is observation or _fleet_repo(other, engine.config) in (repo, None) or other.get("repo") == repo:
                    return False
        return True

    def seam_facts(self, observation, boss, idle_ms):
        """#773: idle SEAM_IDLE_SECONDS or more, an OPEN assignment, and a last report of `working`: ONE nudge for the worker and ONE wake
        for its boss, each keyed by this idle stretch (`idle_at`), so neither is sent twice. Reads nothing new: the observation, the
        last-reports.json record `report` wrote, the retired list, and the closed-issue events the hub already holds (no `gh` call)."""
        engine, state_dir = self.engine, self.engine.state_dir
        issue = _issue_of(observation.fleet)
        if state_dir is None or issue is None or engine.now() - observation.idle_at < SEAM_IDLE_SECONDS:
            return []
        if not self.seam_applies(observation, issue):
            return []   # #793: only a coder or tester on a work item or an ask; anything not readable fails toward not nudging
        kept = registry_retired(state_dir).get(observation.name)
        if kept is not None and kept.get("session") in (None, observation.session_id):
            return []   # retired on purpose
        last = _read_small(state_dir, LAST_REPORT_FILE).get(observation.name)
        if not isinstance(last, dict) or last.get("state") != "working":
            return []   # a closing report (or none) is the existing triage's business
        repo = _fleet_repo(observation.fleet, engine.config) or engine.config.get("intent_repo")
        hub = getattr(engine, "hub", None)
        # Fails OPEN on purpose: the hub keeps closed events in memory only, so after a restart a closed issue can get one nudge and one wake.
        closed = hub.closed_issue_snapshot() if hub is not None and hasattr(hub, "closed_issue_snapshot") else {}
        if repo and "%s#%d" % (plan_ledger.normal_repo(repo), issue) in closed:
            return []   # its assignment is closed
        if not repo or legitimately_waiting(engine, observation, issue, repo, last).state != "clear":
            return []   # #816: waiting legitimately (or an input not read whole): nothing is stored, so a later pass says it if it is still idle
        stretch = "%s:%s:%d" % ("seam", observation.session_id, int(round(observation.idle_at * 1000)))
        line = last.get("text") if isinstance(last.get("text"), str) else ""
        nudge = Fact("%s:%s" % (NUDGE, stretch), observation.name, NUDGE,
                     "you went idle after reporting working on #%d: report done, blocked or question, or continue" % issue,
                     observation.idle_at, issue=issue, batch=False, sender="switchboard", repo=repo)
        wake = Fact("%s:%s" % (WORKER_IDLE, stretch), boss, WORKER_IDLE,
                    "idle without a closing report: #%d (worker %s, last report: %s)" % (issue, observation.name, _one_line(redact_command_text(line), 160) or "none recorded"),
                    observation.idle_at, issue=issue, batch=False, sender=observation.name, repo=repo)
        return [nudge, wake]

    def stalled_facts(self, observation, boss):
        """#811: a coder or tester whose last reports were all `working` for `stalled_working_hours` while nothing it owns moved (the pull
        request's head and updated time, the issue's updated time) wakes its boss ONCE per stretch; the worker is never nagged. Reads only
        what the daemon holds (the report record, the lists, the check summaries, the reminders, the decision list), never GitHub, and
        judges only when each of them was read whole. Anything not readable fails toward no wake."""
        engine, state_dir = self.engine, self.engine.state_dir
        issue = _issue_of(observation.fleet)
        now, window = engine.now(), engine.config.get("stalled_working_hours", 2) * 3600
        if state_dir is None or issue is None or not self.seam_applies(observation, issue):
            return []
        if observation.status == "idle" and observation.idle_at is not None:
            return []   # an idle worker is the idle-at-a-seam rule's (one wake for it, not a second under another key)
        last = _read_small(state_dir, LAST_REPORT_FILE).get(observation.name)
        if not isinstance(last, dict) or last.get("state") != "working" or not _is_number(last.get("work_since")) or not isinstance(last.get("work_n"), int):
            return []   # a blocked, question or closing report (or none) is not this case
        kept = registry_retired(state_dir).get(observation.name)
        if kept is not None and kept.get("session") in (None, observation.session_id):
            return []
        repo = _fleet_repo(observation.fleet, engine.config) or engine.config.get("intent_repo")
        if not repo:
            return []
        key = plan_ledger.normal_repo(repo)
        hub = getattr(engine, "hub", None)
        closed = hub.closed_issue_snapshot() if hub is not None and hasattr(hub, "closed_issue_snapshot") else {}
        if "%s#%d" % (key, issue) in closed:
            return []
        ctx = legitimately_waiting(engine, observation, issue, repo, last)
        moved, head, number = ctx.issue_at, ctx.head, ctx.number
        if ctx.state == "unread":
            return []
        if ctx.pr is not None:
            moved = max(moved, ctx.pr_at)
            known = self._heads.get(observation.name)   # a head this process saw change on ANY owned pull request is movement now, whatever GitHub's clock says
            kept = {}
            for pr_number, pr_head in ctx.heads.items():   # recorded BEFORE the waiting verdict is used: a head that moved during a guard is still movement
                seen = (known or {}).get(pr_number)
                kept[pr_number] = seen if seen is not None and seen[0] == pr_head else (pr_head, now if known is not None and seen is not None else 0)
            self._heads[observation.name] = kept
            moved = max([moved] + [t for _, t in kept.values()])
        if ctx.state == "waiting":
            return []
        since = max(moved, last["work_since"])
        if now - since < window:
            return []
        ref = "%s#%d" % (repo, number or issue)
        line = last.get("text") if isinstance(last.get("text"), str) else ""
        stretch = "stalled:%s:%d:%s:%d" % (observation.session_id, issue, head or "-", int(moved))
        text = "no movement for %s on %s (worker %s, %d working reports; last: %s)" % (
            _stalled_age(now - since), ref, observation.name, last["work_n"], _one_line(redact_command_text(line), 160) or "none recorded")
        return [Fact("%s:%s" % (WORKER_IDLE, stretch), boss, WORKER_IDLE, text, now, issue=issue, batch=False, sender=observation.name, repo=repo)]

    def stop_facts(self, observation, boss, idle_ms, idle, reply):
        """The facts for one stop, worked out once (a stop is asked about once; the cache is rebuilt after a
        restart). The idle rider, plus a nudge for the worker, or instead a `worker.stopped` for the boss."""
        memo = (observation.session_id, idle_ms)
        engine = self.engine
        if memo in self._stops:
            return list(self._stops[memo])
        if engine.audit is None:
            return [idle]  # a read-only look never asks the decision model
        verdict = triage_stop(engine, self, observation, boss, idle_ms, reply)
        if verdict is None:
            found = [idle]   # reported, not worth asking, or not readable: only the rider
        elif verdict["outcome"] == "stopped":
            found = [Fact("%s:%s:%d" % (WORKER_STOPPED, observation.session_id, idle_ms), boss, WORKER_STOPPED,
                          "%s stopped without reporting (%s)%s" % (
                              observation.name, observation.outcome or "no outcome recorded",
                              ": " + reply if reply else ""), observation.idle_at,
                          issue=_issue_of(observation.fleet), sender=observation.name)]
            found[0].stop = {"reply": reply, "clean": observation.outcome in QUIET_OUTCOMES,
                             "role": (observation.fleet or {}).get("role"), "fleet": observation.fleet}
        elif verdict["outcome"] == "nudge":
            found = [idle, verdict["nudge"]]
        else:
            found = [idle]
        if len(self._stops) > 500:
            self._stops.clear()
        self._stops[memo] = list(found)
        return found

    def last_reply(self, session_id, idle_ms):
        """The worker's last assistant reply, cut to SUMMARY_CAP; read once per turn."""
        cached = self._summaries.get((session_id, idle_ms))
        if cached is not None:
            return cached
        if self.engine.light:
            return ""
        page = self.engine.v2.message_list(session_id, limit=1, type="assistant")
        text = _one_line(reply_text(page[0]) if isinstance(page, list) and page else "", SUMMARY_CAP)
        if len(self._summaries) > 500:
            self._summaries.clear()
        self._summaries[(session_id, idle_ms)] = text
        return text

    def blocked(self, observation, boss):
        """The facts for a blocked worker's pending requests. PR 14: every blocked agent's requests are read and
        kept in `requests` (a boss with no boss of its own waits on you too); only a worker has a fact."""
        if observation.status != "blocked":
            return []
        facts = []
        for kind, items in (("permission", self.engine.v2.permission_list(observation.session_id)),
                            ("question", self.pending_forms(observation))):
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
                    self.requests.append((observation, kind, item))
                    if boss is not None:
                        facts.append(self.blocked_fact(observation, boss, kind, item))
        return facts

    def pending_forms(self, observation):
        if self._no_forms:
            return []
        try:
            return self.engine.v2.form_list(observation.session_id)
        except V2Error as err:
            if err.reason == "http" and err.status == 404:
                self._no_forms = True  # this v2 has no such operation; stop asking
                return []
            raise

    def blocked_fact(self, observation, boss, kind, item):
        key = "%s:%s:%s" % (WORKER_BLOCKED, observation.session_id, item["id"])
        what = " ".join(str(part) for part in [item.get("action") or item.get("title") or kind]
                        + [r for r in item.get("resources") or [] if isinstance(r, str)])
        asked = _created(item)  # when v2 says it asked; without it the fact is urgent, not batched
        summary = "%s is blocked on a %s request and cannot go on until you answer it: %s" % (
            observation.name, kind, _one_line(what, 200))
        if isinstance(item.get("message"), str) and item["message"].strip():
            summary += " (reason given: %s)" % _one_line(item["message"], 200)
        issue = _issue_of(observation.fleet)
        repo = _fleet_repo(observation.fleet, self.engine.config) if issue else None  # PR 15: the issue's repo
        # A hook's message (PR 9: the policy judge's reason) is on the request as `message`: say it first.
        note = _one_line(item["message"], 120) if isinstance(item.get("message"), str) else ""
        shown = "%s (%s)" % (note, _one_line(what, 60)) if note else what
        self._blocked.append((observation.name, key, "Fleet: %s is blocked" % observation.name,
                              _one_line("%s is waiting for you: %s%s" % (
                                  observation.name, shown,
                                  " (issue %s)" % issue_tag(repo, issue, self.engine.repos_in_play()) if issue else ""), 200)))
        return Fact(key, boss, WORKER_BLOCKED, summary, asked if asked is not None else self.engine.now(),
                    issue=issue, batch=asked is not None, sender=observation.name, repo=repo)
