# ---------------------------------------------------------------- `preflight-pr --body PR`: the description against the plan's cause-first scenario
#
# Read only. It reads the pull request's description and the plan's reviewer scenario that the pull request claims and asks for the cause first (the
# existing readers: `read_pr_json` as `pr-explain --check` does, `fetch_plan` as `plan check` does), and refuses, naming the rule item that is not met,
# a description whose FIRST `## ` section is not a cause section that comes before the why block, or whose first section holds fewer than the pass
# rule's count (`at least N`, `count equals N`) of named places (`path:NNN`) or named items. It judges the shape, never whether the items are the right ones.

BODY_COUNT = r"\b(at least|count equals)\s+(\d{1,4})\b"   # the ONLY forms read: any other number of the rule (a date, an id) is not a count
BODY_SECTION = r"(?m)^##[ \t]+(.*)$"
BODY_PLACE = r"(?<![\w./-])[\w./-]*[A-Za-z_][\w-]*\.[A-Za-z0-9]{1,6}:\d+\b"   # path:NNN, a file name with an extension
BODY_ITEM = r"(?<![\w/])#\d+\b"
BODY_WHY = r"<!--\s*pr-explainer:begin\s*-->|^#{1,6}[ \t]+why\b"


def _body_re(text, *flags):
    """A pattern compiled on use: the part imports `re` here, so it reads no name of the entry."""
    import re
    return re.compile(text, sum(getattr(re, f) for f in flags))


def _body_line(text, limit=200):
    """One line, cut to `limit` characters (the part's own, not the entry's `_one_line`)."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class BodyNotChecked(Exception):
    """A read that failed or a rule that cannot be read: the command says `not checked: <reason>` and exits non-zero."""


def body_rule_count(rule):
    """(N, form) from a pass rule's own count form, `at least N` or `count equals N`; (None, None) when the rule has no such count."""
    found = _body_re(BODY_COUNT, "I").search(rule or "")
    return (int(found.group(2)), found.group(1).lower()) if found else (None, None)


def body_cause_scenarios(scenarios, body, plan_ledger):
    """The reviewer scenarios the pull request claims that ask for the cause first (the first section, or a cause stated first)."""
    claimed, _, error = plan_ledger.pr_scenarios(scenarios, body)
    if error:
        raise BodyNotChecked("the claim line is refused: %s" % _body_line(error, 160))
    found = []
    for s in claimed:
        text = " ".join((str(s.get("text", "")), str(s.get("pass", "")))).lower()
        if plan_ledger.evaluator_of(s) == "reviewer" and ("first section" in text or ("cause" in text and "first" in text)):
            found.append(s)
    return found


def body_unfenced(text):
    """The text with every line inside a fenced code block blanked (same length, so offsets hold). A fence is a line of three or more backticks or
    tildes, indented up to 3 spaces; it is closed by a line of the same character at least as long, or open to the end of the text."""
    out, fence = [], None
    for line in text.splitlines(keepends=True):
        opened = _body_re(r"^ {0,3}(`{3,}|~{3,})").match(line)
        if fence is None and opened:
            fence = opened.group(1)
            masked = True
        elif fence is not None:
            closing = _body_re(r"^ {0,3}(`+|~+)[ \t]*$").match(line.rstrip("\r\n"))
            if closing and closing.group(1)[0] == fence[0] and len(closing.group(1)) >= len(fence):
                fence = None
            masked = True
        else:
            masked = False
        out.append(_body_re(r"[^\r\n]").sub(" ", line) if masked else line)
    return "".join(out)


def body_first_section(body):
    """(heading, text, before, found): the first `## ` section of the description, the text before it, and whether there is one.
    Headings are searched in the unfenced copy; the text of the section is the ORIGINAL (fenced lines included)."""
    masked = body_unfenced(body)
    first = _body_re(BODY_SECTION).search(masked)
    if not first:
        return "", "", masked, False
    after = _body_re(BODY_SECTION).search(masked, first.end())
    return first.group(1).strip(), body[first.end():after.start() if after else len(body)], masked[:first.start()], True


def body_check_problems(body, scenario):
    """([refusal items], [not checked reasons]) of one description against one cause-first scenario. Pure: no read, no write."""
    refused, unchecked = [], []
    heading, text, before, found = body_first_section(body)
    why_at = _body_re(BODY_WHY, "I", "M").search(body_unfenced(body))
    if not found or "cause" not in heading.lower() or _body_re(BODY_WHY, "I", "M").search(before) or (why_at and why_at.start() < body.find(heading)):
        refused.append("the cause section is not first")
    count, form = body_rule_count(str(scenario.get("pass", "")))
    if count is None:
        unchecked.append("the pass rule has no count")
        return refused, unchecked
    rule = str(scenario.get("pass", "")) + " " + str(scenario.get("text", ""))
    if _body_re(r"\bline\b", "I").search(rule):
        label, got = "places with a file and line", len(set(_body_re(BODY_PLACE).findall(text)))
    else:
        label, got = "named items", len(set(_body_re(BODY_ITEM).findall(text)) | set(_body_re(BODY_PLACE).findall(text)))
    if (got < count) if form == "at least" else (got != count):
        refused.append("%s: found %d of %s %d" % (label, got, form, count))
    return refused, unchecked


def body_check_read(pr, runner, now, d):
    """(body, scenarios) read with GETs only. Raises BodyNotChecked."""
    try:
        repo, number = d["parse_pr_ref"](pr)
        engine, _, _ = d["build_engine"](runner, now, "cli", writable=False)
        body = d["read_pr_json"](engine, repo, number).get("body")
        if not isinstance(body, str):
            raise BodyNotChecked("the description of %s#%d is not text" % (repo, number))
        config = d["load_config"]()
        found = d["PLAN_LINE_RE"].search(d["plan_ledger"].visible_body(body))
        if not found:
            raise BodyNotChecked("the description names no PLAN: line")
        plan_repo, plan_number, comment_id = d["parse_plan_source"](found.group(1).strip("<>.,"), config.get("intent_repo"))
        reader = d["IntentReader"](config["gh"], plan_repo, runner, now, config["call_timeout_seconds"], accounts=d["GhAccounts"](config, runner, now))
        text = d["fetch_plan"](reader, plan_repo, plan_number, comment_id)[0]
        return body, d["plan_check"].parse_scenarios(d["plan_check"].scenario_block(text))
    except BodyNotChecked:
        raise
    except Exception as err:   # any failed read or unreadable plan is said, never a pass
        raise BodyNotChecked(_body_line(str(err) or err.__class__.__name__, 200)) from None


def cmd_body_check(pr, runner, now, d):
    """`preflight-pr --body PR`: exit 0 when the description meets the cause-first rule, 1 when refused or not checked. Writes nothing."""
    try:
        body, scenarios = body_check_read(pr, runner, now, d)
        cause = body_cause_scenarios(scenarios, body, d["plan_ledger"])
        if not cause:
            raise BodyNotChecked("the plan has no cause-first scenario")
    except BodyNotChecked as err:
        print("preflight-pr --body: not checked: %s" % err)
        return 1
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
    return 1 if refused or unchecked else 0
