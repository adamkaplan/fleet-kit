# ---------------------------------------------------------------- `preflight-pr --body PR`: the description against the plan's cause-first scenario
#
# Read only. It reads the pull request's description and the plan's reviewer scenario that the pull request claims and asks for the cause first (the
# existing readers: `read_pr_json` as `pr-explain --check` does, `fetch_plan` as `plan check` does), and refuses, naming the rule item that is not met,
# a description whose FIRST `## ` section is not a cause section that comes before the why block, or whose first section holds fewer than the pass
# rule's count (`at least N`, `count equals N`) of named places (`path:NNN`) or named items. It judges the shape, never whether the items are the right ones.

BODY_COUNT_RE = re.compile(r"\b(at least|count equals)\s+(\d{1,4})\b", re.I)   # the ONLY forms read: any other number of the rule (a date, an id) is not a count
BODY_SECTION_RE = re.compile(r"(?m)^##[ \t]+(.*)$")
BODY_PLACE_RE = re.compile(r"(?<![\w./-])[\w./-]*[A-Za-z_][\w-]*\.[A-Za-z0-9]{1,6}:\d+\b")   # path:NNN, a file name with an extension
BODY_ITEM_RE = re.compile(r"(?<![\w/])#\d+\b")
BODY_WHY_RE = re.compile(r"<!--\s*pr-explainer:begin\s*-->|^#{1,6}[ \t]+why\b", re.I | re.M)


class BodyNotChecked(Exception):
    """A read that failed or a rule that cannot be read: the command says `not checked: <reason>` and exits non-zero."""


def body_rule_count(rule):
    """(N, form) from a pass rule's own count form, `at least N` or `count equals N`; (None, None) when the rule has no such count."""
    found = BODY_COUNT_RE.search(rule or "")
    return (int(found.group(2)), found.group(1).lower()) if found else (None, None)


def body_cause_scenarios(scenarios, body):
    """The reviewer scenarios the pull request claims that ask for the cause first (the first section, or a cause stated first)."""
    claimed, _, error = plan_ledger.pr_scenarios(scenarios, body)
    if error:
        raise BodyNotChecked("the claim line is refused: %s" % _one_line(error, 160))
    found = []
    for s in claimed:
        text = " ".join((str(s.get("text", "")), str(s.get("pass", "")))).lower()
        if plan_ledger.evaluator_of(s) == "reviewer" and ("first section" in text or ("cause" in text and "first" in text)):
            found.append(s)
    return found


def body_first_section(body):
    """(heading, text, before, found): the first `## ` section of the description, the text before it, and whether there is one."""
    first = BODY_SECTION_RE.search(body)
    if not first:
        return "", "", body, False
    after = BODY_SECTION_RE.search(body, first.end())
    return first.group(1).strip(), body[first.end():after.start() if after else len(body)], body[:first.start()], True


def body_check_problems(body, scenario):
    """([refusal items], [not checked reasons]) of one description against one cause-first scenario. Pure: no read, no write."""
    refused, unchecked = [], []
    heading, text, before, found = body_first_section(body)
    why_at = BODY_WHY_RE.search(body)
    if not found or "cause" not in heading.lower() or BODY_WHY_RE.search(before) or (why_at and why_at.start() < body.find(heading)):
        refused.append("the cause section is not first")
    count, form = body_rule_count(str(scenario.get("pass", "")))
    if count is None:
        unchecked.append("the pass rule has no count")
        return refused, unchecked
    rule = str(scenario.get("pass", "")) + " " + str(scenario.get("text", ""))
    if re.search(r"\bline\b", rule, re.I):
        label, got = "places with a file and line", len(set(BODY_PLACE_RE.findall(text)))
    else:
        label, got = "named items", len(set(BODY_ITEM_RE.findall(text)) | set(BODY_PLACE_RE.findall(text)))
    if (got < count) if form == "at least" else (got != count):
        refused.append("%s: found %d of %s %d" % (label, got, form, count))
    return refused, unchecked


def body_check_read(pr, runner, now):
    """(body, scenarios) read with GETs only. Raises BodyNotChecked."""
    try:
        repo, number = parse_pr_ref(pr)
        engine, _, _ = build_engine(runner, now, "cli", writable=False)
        body = read_pr_json(engine, repo, number).get("body")
        if not isinstance(body, str):
            raise BodyNotChecked("the description of %s#%d is not text" % (repo, number))
        config = load_config()
        found = PLAN_LINE_RE.search(plan_ledger.visible_body(body))
        if not found:
            raise BodyNotChecked("the description names no PLAN: line")
        plan_repo, plan_number, comment_id = parse_plan_source(found.group(1).strip("<>.,"), config.get("intent_repo"))
        reader = IntentReader(config["gh"], plan_repo, runner, now, config["call_timeout_seconds"], accounts=GhAccounts(config, runner, now))
        text = fetch_plan(reader, plan_repo, plan_number, comment_id)[0]
        return body, plan_check.parse_scenarios(plan_check.scenario_block(text))
    except BodyNotChecked:
        raise
    except Exception as err:   # any failed read or unreadable plan is said, never a pass
        raise BodyNotChecked(_one_line(str(err) or err.__class__.__name__, 200)) from None


def cmd_body_check(pr, runner, now):
    """`preflight-pr --body PR`: exit 0 when the description meets the cause-first rule, 1 when refused or not checked. Writes nothing."""
    try:
        body, scenarios = body_check_read(pr, runner, now)
        cause = body_cause_scenarios(scenarios, body)
        if not cause:
            raise BodyNotChecked("the plan has no cause-first scenario")
    except BodyNotChecked as err:
        print("preflight-pr --body: not checked: %s" % err)
        return EXIT_ERROR
    refused, unchecked = [], []
    for scenario in cause:
        r, u = body_check_problems(body, scenario)
        refused += [x for x in r if x not in refused]
        unchecked += [x for x in u if x not in unchecked]
    for item in refused:
        print("preflight-pr --body: REFUSED: %s" % item)
    for reason in unchecked:
        print("preflight-pr --body: not checked: %s" % reason)
    if not refused and not unchecked:
        print("preflight-pr --body: PASS: the first section is the cause and meets the plan's count")
    return EXIT_ERROR if refused or unchecked else EXIT_OK
