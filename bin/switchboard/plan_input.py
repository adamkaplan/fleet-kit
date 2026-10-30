# ---------------------------------------------------------------- `plan check` (#135, gate G0a)

PLAN_URL_RE = re.compile(r"^https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([0-9]+)(?:#issuecomment-([0-9]+))?/?$")
PLAN_COMMENT_PAGES = 10


def parse_plan_source(text, default_repo=None):
    """(repo, number, comment id or None) from `owner/repo#N`, an issue URL (a `#issuecomment-ID` picks that comment),
    or a bare `N` / `#N` with `default_repo`. Raises PlanInputError."""
    text = (text or "").strip()
    found = PLAN_URL_RE.match(text)
    if found:
        return found.group(1), int(found.group(2)), int(found.group(3)) if found.group(3) else None
    found = re.match(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$", text) or re.match(r"^()#?([0-9]+)$", text)
    if not found or int(found.group(2)) < 1:
        raise PlanInputError("%r is not a plan: write owner/repo#N, an issue URL, or --file PATH" % text)
    repo = found.group(1) or default_repo
    if not repo:
        raise PlanInputError("%r names no repo and no intent_repo is configured: write owner/repo#N" % text)
    return repo, int(found.group(2)), None


def plan_comment_choice(body, comments):
    """(text, source, defects): the ONE rule every reader of an issue's plan applies: the plan select_plan_text picks, with the comment-level
    defects (a second plan-looking comment naming both ids, a bad receipt) RETURNED beside it, never kept in a global."""
    text, source = plan_check.select_plan_text(body, comments)
    return text, source, plan_check.plan_comment_defects(comments)


def second_plan_defects(defects):
    return [d for d in defects if d.get("code") == "second-plan-comment"]


def fetch_plan(reader, repo, number, comment_id=None):
    """(text, source label, done_when of the issue, comment-level defects) read from GitHub with GETs only. `reader` has `_api(path, repo)`.
    The defects are computed from every comment of the issue also when `comment_id` names one: naming a comment does not skip the report."""
    try:
        issue = reader._api("repos/%s/issues/%d" % (repo, number), repo)
        comments = []
        for page in range(1, PLAN_COMMENT_PAGES + 1):
            got = reader._api("repos/%s/issues/%d/comments?per_page=100&page=%d" % (repo, number, page), repo)
            if not isinstance(got, list):
                raise PlanInputError("the comments of %s#%d did not come back as a list" % (repo, number))
            comments.extend(got)
            if len(got) < 100:
                break
    except IntentError as err:
        raise PlanInputError("cannot read %s#%d: %s" % (repo, number, err)) from None
    body = issue.get("body") if isinstance(issue, dict) and isinstance(issue.get("body"), str) else ""
    defects = plan_check.plan_comment_defects(comments)
    if comment_id is not None:
        picked = [c for c in comments if isinstance(c, dict) and c.get("id") == comment_id]
        if not picked:
            raise PlanInputError("%s#%d has no comment %d" % (repo, number, comment_id))
        text, source = picked[0].get("body") or "", "comment %d%s" % (comment_id, plan_check.comment_author(picked[0]))
    else:
        text, source, defects = plan_comment_choice(body, comments)
    return text, "%s of %s#%d" % (source, repo, number), parse_intent(body)[1], defects


def _parent_ask_done_when(reader, repo, number):
    """The Done-when of the ASK that is the native sub-issue parent of issue `number` of `repo`, or None. Like `IntentReader.ask_of`, a parent counts as the ask only
    when it has a parent of its own (the charter): the issue under a charter is itself the ask, and a charter's Done-when is never borrowed. Anything that is not
    "the ask of this repository's work item" is REFUSED (PlanInputError), never read as another issue: a parent in another repository (the parent's repository comes
    from the API payload, the reader's `parent()` keeps only the number), a pull request, the issue itself, and a cycle."""
    where = "%s#%d" % (repo, number)

    def parent_payload(of):
        try:
            found = reader._api("repos/%s/issues/%d/parent" % (repo, of), repo)
        except IntentError as err:
            if err.reason == "not_found":
                return None
            raise
        if not isinstance(found, dict) or not isinstance(found.get("number"), int):
            raise PlanInputError("the parent of %s#%d did not come back as an issue" % (repo, of))
        if found.get("pull_request"):
            raise PlanInputError("the parent of %s#%d is a pull request, not an ask" % (repo, of))
        url = str(found.get("repository_url") or "").lower().rstrip("/")
        if not url.endswith("/repos/" + repo.lower()):
            raise PlanInputError("the parent of %s#%d (#%d) is in another repository (%s): a plan reads the Done-when of an ask of its own repository only"
                                 % (repo, of, found["number"], url.rsplit("/repos/", 1)[-1] or "unknown"))
        return found
    try:
        parent = parent_payload(number)
        if parent is None:
            return None
        if parent["number"] == number:
            raise PlanInputError("%s is its own parent" % where)
        grand = parent_payload(parent["number"])
        if grand is None:
            return None
        if grand["number"] in (number, parent["number"]):
            raise PlanInputError("the parent chain of %s loops back on itself" % where)
        return reader.issue(parent["number"], repo).done_when
    except IntentError as err:
        raise PlanInputError("cannot read the parent ask of %s: %s" % (where, err)) from None


def load_plan(args, runner, now):
    """(text, source label, result) of the plan `args` names (source, file, ask, done_when), checked. Raises
    PlanInputError, ValueError (an unreadable block) or the readers' errors."""
    try:
        plan_check.MAX_BYTES
    except Exception as err:  # the module is missing or broken: say so, never a traceback
        raise PlanInputError("the plan checker (bin/plan_check.py) cannot be loaded: %s" % _one_line(str(err), 150)) from None
    done_when = args.done_when
    plan_own = None
    comment_defects = []
    if args.file:
        if args.source:
            raise PlanInputError("give a plan source or --file, not both")
        try:
            with open(args.file, "r", encoding="utf-8") as handle:
                text = handle.read(plan_check.MAX_BYTES * 4 + 1)
        except (OSError, UnicodeDecodeError) as err:
            raise PlanInputError("cannot read %s: %s" % (args.file, getattr(err, "strerror", None) or err)) from None
        if len(text) > plan_check.MAX_BYTES * 4:
            raise PlanInputError("%s is larger than %d bytes" % (args.file, plan_check.MAX_BYTES * 4))
        source = "file %s" % args.file
        plan_own = parse_intent(text)[1]
        done_when = done_when or plan_own
        plan_own = plan_own if done_when != plan_own else None
    else:
        if not args.source:
            raise PlanInputError("give a plan: owner/repo#N, an issue URL, or --file PATH")
        config = load_config()
        repo, number, comment_id = parse_plan_source(args.source, config.get("intent_repo"))
        reader = IntentReader(config["gh"], repo, runner, now, config["call_timeout_seconds"],
                              accounts=GhAccounts(config, runner, now))
        text, source, issue_done, comment_defects = fetch_plan(reader, repo, number, comment_id)
        if args.ask and not done_when:
            ask_repo, ask_number, _ = parse_plan_source(args.ask, repo)
            done_when = reader.issue(ask_number, ask_repo).done_when  # the ask is the authority
            plan_own = issue_done
        elif done_when:
            plan_own = issue_done if issue_done and issue_done != done_when else None
        else:
            done_when = issue_done
            if not done_when:   # a work item states no Done-when of its own: its ask's is the authority (the sub-issue parent, never a "Work item of" line)
                done_when = _parent_ask_done_when(reader, repo, number)
    if args.ask and args.file and not args.done_when:
        raise PlanInputError("--ask needs a plan read from GitHub; for --file give --done-when TEXT")
    result = plan_check.check(text, done_when, plan_own)
    if comment_defects:
        result["defects"] = list(result["defects"]) + list(comment_defects)
        result["result"] = "fail"
    return text, source, result


def cmd_plan_check(args, runner, now):
    """`plan check`: read a plan, check it, print PASS or FAIL with one line per defect and the content hash. Exit 0
    pass, 1 fail, 2 unreadable. Reads only; never writes GitHub."""
    def finish(code, payload, lines, error=None):
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        elif error:
            sys.stderr.write("%s: plan check: %s\n" % (PROG, error))
        else:
            print("\n".join(lines))
        return code

    try:
        text, source, result = load_plan(args, runner, now)
    except (PlanInputError, ValueError, ConfigError, IntentError) as err:
        return finish(EXIT_USAGE, {"result": "error", "error": _one_line(str(err), 300)}, [], _one_line(str(err), 300))
    result["source"] = source
    return finish(EXIT_OK if result["result"] == "pass" else EXIT_ERROR, result, plan_check.render(result, source, explaining=getattr(args, "explain", False)))


def cmd_plan_template(args, runner, now):
    """`plan template <ask>` (or `--done-when TEXT`): print a prefilled plan for the ask: its Done-when clauses numbered and one scenario stub
    per clause. Reads only (a GET of the ask); never writes GitHub. Exit 0, or 2 when the ask or its Done-when cannot be read."""
    try:
        done_when, source = args.done_when, None
        if not done_when:
            if not args.ask:
                raise PlanInputError("give the ask (owner/repo#N) or --done-when TEXT")
            config = load_config()
            repo, number, _ = parse_plan_source(args.ask, config.get("intent_repo"))
            reader = IntentReader(config["gh"], repo, runner, now, config["call_timeout_seconds"], accounts=GhAccounts(config, runner, now))
            done_when, source = reader.issue(number, repo).done_when, "%s#%d" % (repo, number)
        clauses = plan_check.clauses_of(done_when)
        if not (done_when or "").strip() or not clauses:
            raise PlanInputError("the ask has no Done-when line: nothing to prefill (write the plan by hand, or add the Done-when first)")
    except (PlanInputError, ValueError, ConfigError, IntentError) as err:
        sys.stderr.write("%s: plan template: %s\n" % (PROG, _one_line(str(err), 300)))
        return EXIT_USAGE
    print("\n".join(plan_check.template(clauses, _plain(source, 120) if source else None)))
    return EXIT_OK
