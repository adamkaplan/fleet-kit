# ---------------------------------------------------------------- `preflight-pr --body PR`: the description against the plan's cause-first scenario
#
# Read only. It reads the pull request's description and the plan's reviewer scenario that the pull request claims and asks for the cause first (the
# existing readers: `read_pr_json` as `pr-explain --check` does, `fetch_plan` as `plan check` does), and refuses, naming the rule item that is not met,
# a description whose FIRST `## ` section (read from the page's own rendering, `body_html`) is not a cause section that comes before the why block, or whose first section holds fewer than the pass
# rule's count (`at least N`, `count equals N`) of named places (`path:NNN`) or named items. It judges the shape, never whether the items are the right ones.

BODY_COUNT = r"\b(at least|count equals)\s+(\d{1,4})\b"   # the ONLY forms read: any other number of the rule (a date, an id) is not a count
BODY_PLACE = r"(?<![\w./-])[\w./-]*[A-Za-z_][\w-]*\.[A-Za-z0-9]{1,6}:\d+\b"   # path:NNN, a file name with an extension
BODY_ITEM = r"(?<![\w/])#\d+\b"


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


def body_rendered(html):
    """(heading, text, before, found, why): the description as the PAGE renders it (`body_html`), read with the standard library's `html.parser`.
    `heading` is the text of the first h2, `text` the text from that h2 to the next h2 (code blocks, lists and link text included; an attribute such
    as data-snippet-clipboard-copy-content is not text), `before` the text before the first h2, `found` whether there is an h2, and `why` whether the
    why block stands before it (the explainer's title `Why (before and after)` in `before`, or a heading element h1..h6 there whose text begins with `Why`).
    Nothing is matched against the raw text: a construct GitHub does not render as a heading is never one."""
    from html.parser import HTMLParser

    class Page(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts, self.open, self.heads = [], None, []   # parts: [(h2 index or None, text)]; heads: (level, text, h2 seen before)
            self.h2s = 0

        def handle_starttag(self, tag, attrs):
            if len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
                self.open = [int(tag[1]), "", self.h2s]
                if tag == "h2":
                    self.h2s += 1

        def handle_endtag(self, tag):
            if self.open and tag == "h%d" % self.open[0]:
                self.heads.append(tuple(self.open))
                self.open = None

        def handle_data(self, data):
            self.parts.append((self.h2s, data))
            if self.open:
                self.open[1] += data

    page = Page()
    page.feed(html or "")
    page.close()
    h2 = [h for h in page.heads if h[0] == 2]
    if not h2:
        return "", "", " ".join(t for _, t in page.parts), False, False
    before = "".join(t for n, t in page.parts if n == 0)
    text = "".join(t for n, t in page.parts if n == 1)
    why = "why (before and after)" in " ".join(before.lower().split()) or any(h[2] == 0 and h[1].strip().lower().startswith("why") for h in page.heads)
    return h2[0][1].strip(), text, before, True, why


def body_check_problems(html, scenario):
    """([refusal items], [not checked reasons]) of one rendered description against one cause-first scenario. Pure: no read, no write."""
    refused, unchecked = [], []
    heading, text, before, found, why = body_rendered(html)
    if not found or "cause" not in heading.lower() or why:
        refused.append("the cause section is not first")
    count, form = body_rule_count(str(scenario.get("pass", "")))
    if count is None:
        unchecked.append("the pass rule has no count")
        return refused, unchecked
    if _body_re(r"\bline\b", "I").search(str(scenario.get("pass", ""))):
        label, got = "places with a file and line", len(set(_body_re(BODY_PLACE).findall(text)))
    else:
        label, got = "named items", len(set(_body_re(BODY_ITEM).findall(text)) | set(_body_re(BODY_PLACE).findall(text)))
    if (got < count) if form == "at least" else (got != count):
        refused.append("%s: found %d of %s %d" % (label, got, form, count))
    return refused, unchecked


def body_check_read(pr, runner, now, d):
    """(body, html, scenarios) read with GETs only: the pull request (raw body), the same pull request with `Accept: application/vnd.github.html+json`
    (`body_html`, through the account reader's `read` with that header) and the plan. Raises BodyNotChecked."""
    try:
        repo, number = d["parse_pr_ref"](pr)
        engine, _, _ = d["build_engine"](runner, now, "cli", writable=False)
        body = d["read_pr_json"](engine, repo, number).get("body")
        if not isinstance(body, str):
            raise BodyNotChecked("the description of %s#%d is not text" % (repo, number))
        config = d["load_config"]()
        import json
        import subprocess
        accounts = d["GhAccounts"](config, runner, now)
        done = accounts.read(runner, "repos/%s/pulls/%d" % (repo, number), repo, config["call_timeout_seconds"],
                             extra_flags=["-H", "Accept: application/vnd.github.html+json"], stdin=subprocess.DEVNULL)
        html = json.loads(done.stdout).get("body_html") if done.returncode == 0 else None
        if not isinstance(html, str) or (not html.strip() and body.strip()):
            raise BodyNotChecked("the rendered description (body_html) of %s#%d cannot be read" % (repo, number))
        found = d["PLAN_LINE_RE"].search(d["plan_ledger"].visible_body(body))
        if not found:
            raise BodyNotChecked("the description names no PLAN: line")
        plan_repo, plan_number, comment_id = d["parse_plan_source"](found.group(1).strip("<>.,"), config.get("intent_repo"))
        reader = d["IntentReader"](config["gh"], plan_repo, runner, now, config["call_timeout_seconds"], accounts=accounts)
        text = d["fetch_plan"](reader, plan_repo, plan_number, comment_id)[0]
        return body, html, d["plan_check"].parse_scenarios(d["plan_check"].scenario_block(text))
    except BodyNotChecked:
        raise
    except Exception as err:   # any failed read or unreadable plan is said, never a pass
        raise BodyNotChecked(_body_line(str(err) or err.__class__.__name__, 200)) from None


def cmd_body_check(pr, runner, now, d):
    """`preflight-pr --body PR`: exit 0 when the description meets the cause-first rule, 1 when refused or not checked. Writes nothing."""
    try:
        body, html, scenarios = body_check_read(pr, runner, now, d)
        cause = body_cause_scenarios(scenarios, body, d["plan_ledger"])
        if not cause:
            raise BodyNotChecked("the plan has no cause-first scenario")
    except BodyNotChecked as err:
        print("preflight-pr --body: not checked: %s" % err)
        return 1
    refused, unchecked = [], []
    for scenario in cause:
        r, u = body_check_problems(html, scenario)
        refused += [x for x in r if x not in refused]
        unchecked += [x for x in u if x not in unchecked]
    for item in refused:
        print("preflight-pr --body: REFUSED: %s" % item)
    for reason in unchecked:
        print("preflight-pr --body: not checked: %s" % reason)
    if not refused and not unchecked:
        print("preflight-pr --body: PASS: the first section is the cause and meets the plan's count")
    return 1 if refused or unchecked else 0
