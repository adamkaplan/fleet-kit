# ---------------------------------------------------------------- `pr-explain` (#179, slice L6)
#
# The explainer is NOT a reviewer: reviewers give no advice and no summaries. It writes, for the PR description, (a) a short before and after
# pseudo-code WHY block on every PR. It never writes a summary flowchart (ask #687: a Mermaid diagram belongs only where it shows data flow, system
# design or component design, never at the top of a body). This command is the simplest thing that works without write authority: it reads the PR once (a GET) and PRINTS the block (or the
# whole new body with the block put in once); the author pastes it with their own `gh pr edit --body-file`. `--check` judges a body.

EXPLAINER_BEGIN = "<!-- pr-explainer:begin -->"
EXPLAINER_END = "<!-- pr-explainer:end -->"
EXPLAINER_HEADER_RE = re.compile(r"^(?:PLAN|MERGE|canon):", re.I)   # the lines a body opens with; the block goes right after them
# The generated summary flowchart this tool used to write (four boxes: Changes, Touches, Irreversible, Risk). It is never emitted; a body that still holds it fails
# `--check`. Detection is by the four box labels inside ONE Mermaid fence, read line by line (bounded, linear, no lowered copy, ASCII-only matching).
SUMMARY_FLOWCHART_LABELS = ("Changes", "Touches", "Irreversible", "Risk")
_SUMMARY_LABEL = re.compile(r'[\["(]\s*(%s)\s*:' % "|".join(SUMMARY_FLOWCHART_LABELS), re.A | re.I)   # built from the tuple: one list of labels
_SUMMARY_LINE_MAX = 400   # a box label is short: the rest of a longer line is not looked at
_SUMMARY_BODY_MAX = 1 << 20   # a body is cut at 1 MB for this check (GitHub's own limit is 65536 characters)
_SUMMARY_FENCE_RE = re.compile(r"^(?:[ \t>]|[-*+][ \t]|\d{1,9}[.)][ \t])*(`{3,}|~{3,})(.*)$")   # a fence at any indent, inside quotes, or opened after any run of quote marks and list markers: GitHub renders those too (each prefix piece starts with a different character, so the run has one way to match: linear)
SUMMARY_FLOWCHART_DEFECT = ("the body holds the generated summary flowchart (a Mermaid diagram whose boxes are Changes:, Touches:, Irreversible:, Risk:): remove it; "
                            "a diagram is only for data flow, system design or component design")


def summary_flowchart_found(body):
    """True when a Mermaid fence ANYWHERE in the body holds the four labelled boxes of the generated summary flowchart (Changes:, Touches:, Irreversible:, Risk:). One
    pass over the lines under a step budget; each line is cut to _SUMMARY_LINE_MAX; fences are read as the held-body scan reads them (three or more backticks or
    tildes, closed by the same character at least as long, an unclosed fence running to the end) but at ANY indent and behind blockquote marks, since GitHub renders a fence
    nested in a list item or a quote; it is a lint for the generator's own output (a renamed label is not caught, and it errs toward finding it: a closing line written `- ```` counts), not a boundary; no lowered copy is indexed
    (ASCII-only folding). A diagram that shows data flow (without all four labels in ONE fence) is not it."""
    lines = str(body or "")[:_SUMMARY_BODY_MAX].splitlines()   # any line break, CR-only included
    budget = _held_steps(len(lines))
    fence, mermaid, seen = None, False, set()
    for line in lines:
        _held_spend(budget)
        text = line[:_SUMMARY_LINE_MAX]
        found = _SUMMARY_FENCE_RE.match(text)
        opens = found and not (found.group(1)[0] == "`" and "`" in found.group(2))
        words = found.group(2).split() if opens else []
        if opens and words and _ascii_lower(words[0]) == "mermaid":   # a Mermaid opener always (re)starts the scan, even inside a decoy fence: a decoy cannot hide the real one
            fence, mermaid, seen = (found.group(1)[0], len(found.group(1))), True, set()
            continue
        if fence is None:
            if opens:
                fence, mermaid, seen = (found.group(1)[0], len(found.group(1))), False, set()
            continue
        if found and found.group(1)[0] == fence[0] and len(found.group(1)) >= fence[1] and not found.group(2).strip():
            if mermaid and len(seen) == len(SUMMARY_FLOWCHART_LABELS):
                return True
            fence = None
        elif mermaid:
            for label in _SUMMARY_LABEL.finditer(text):
                seen.add(label.group(1).capitalize())
    return fence is not None and mermaid and len(seen) == len(SUMMARY_FLOWCHART_LABELS)   # a fence left open at the end of the body counts too


def summary_flowchart_defects(body):
    """The defect list for the summary flowchart: [] when absent, one message when found, and a message (never a raise) when the scan could not finish."""
    try:
        return [SUMMARY_FLOWCHART_DEFECT] if summary_flowchart_found(body) else []
    except ValueError as err:
        return ["the body could not be scanned for the summary flowchart (%s)" % _one_line(str(err), 100)]


def explainer_text(text):
    """A before or after line made safe for the block: one line, and never a marker comment, a comment close or a code fence (any of them would
    end the block early, add a second marker or break the fenced why part)."""
    plain = " ".join(str(text or "").split())
    for bad in (EXPLAINER_BEGIN, EXPLAINER_END, "<!--", "-->", "```"):
        plain = plain.replace(bad, " ")
    return " ".join(plain.split())


def explainer_block(why_before, why_after):
    """The ONE block of a PR description: the why part (every PR), between two marker comments. Never a diagram."""
    why_before, why_after = explainer_text(why_before), explainer_text(why_after)
    parts = [EXPLAINER_BEGIN, ""]
    parts += ["**Why (before and after)**", "", "```text", "before: %s" % why_before, "after:  %s" % why_after, "```", "", EXPLAINER_END]
    return "\n".join(parts)


def explainer_insert(body, block):
    """`body` with `block` in it ONCE: any block already there (between the markers) is removed first, and the new one goes right after the lines the
    body opens with (`PLAN:`, `MERGE:`, `canon:`), so a gate that reads the first line still sees it."""
    body = body or ""
    while EXPLAINER_BEGIN in body and EXPLAINER_END in body.split(EXPLAINER_BEGIN, 1)[1]:
        head, rest = body.split(EXPLAINER_BEGIN, 1)
        body = head.rstrip("\n") + "\n" + rest.split(EXPLAINER_END, 1)[1].lstrip("\n")
    lines = body.split("\n")
    at = 0
    while at < len(lines) and (EXPLAINER_HEADER_RE.match(lines[at]) or (not lines[at].strip() and at and EXPLAINER_HEADER_RE.match(lines[at - 1]))):
        at += 1
    if HELD_WHY in body:   # a held PR's top part stays first: the block goes below it, where the technical section begins
        at = next((i for i, l in enumerate(lines) if l.strip() in HELD_PARTS), at)
        at += held_top_end(lines[at:])
    head, tail = lines[:at], lines[at:]
    return "\n".join(head + ([""] if head and head[-1].strip() else []) + block.split("\n") + ([""] if tail and tail[0].strip() else []) + tail)


def explainer_defects(body):
    """What is wrong with a PR description's explainer: exactly one block, a why part with a real before and a real after (no placeholder left), and no
    generated summary flowchart anywhere in the body. A body with no diagram passes."""
    body = body or ""
    count = body.count(EXPLAINER_BEGIN)
    if count != 1 or body.count(EXPLAINER_END) != 1 or body.index(EXPLAINER_BEGIN) > body.index(EXPLAINER_END):
        return ["the description has %d explainer blocks: it carries exactly one (between %s and %s)" % (count, EXPLAINER_BEGIN, EXPLAINER_END)] + (
            summary_flowchart_defects(body))
    inner = body.split(EXPLAINER_BEGIN, 1)[1].split(EXPLAINER_END, 1)[0]
    problems = []
    why = re.search(r"```text\n(.*?)```", inner, re.S)
    before = re.search(r"^before: (.+)$", why.group(1), re.M) if why else None
    after = re.search(r"^after: +(.+)$", why.group(1), re.M) if why else None
    if not why or not before or not after:
        problems.append("the why part needs a ```text block with a `before:` line and an `after:` line")
    elif any(re.search(r"<[^>]*>", m.group(1)) for m in (before, after)):
        problems.append("the why part still has a placeholder in <angle brackets>")
    problems += summary_flowchart_defects(body)
    return problems


HELD_WHY = "## Why this needs you"
HELD_TABLE = "## What changes for you"
HELD_EXAMPLE = "## Example"
HELD_QUESTIONS = "## Questions for you"
HELD_PARTS = (HELD_WHY, HELD_TABLE, HELD_EXAMPLE, HELD_QUESTIONS)
def _held_none(line):
    """True for the line `Questions: none`, then an optional one-line parenthetical (and an optional final dot). Plain string tests: no backtracking."""
    line = line.strip()
    if not line.startswith("Questions: none"):
        return False
    tail = line[len("Questions: none"):].strip()
    return tail in ("", ".") or (tail.startswith("(") and (tail.endswith(")") or tail.endswith(").")))


def pr_is_held(labels, body, now=None):
    """A PR is held for the owner: it carries a hold label, or (the same rule as everywhere) a merge-captain label or the legacy first line."""
    return bool(pr_hold_reasons(labels)) or is_captain({"labels": labels, "body": body}, now)


_HELD_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HELD_BODY_MAX = 70000   # a longer body is not read (GitHub's own cap is 65536)
_HELD_PLACEHOLDER_RE = re.compile(r"<[^<>]*>")   # linear: the class stops at the next `<`


_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def _ascii_lower(text):
    """`text` with only A-Z lowered: the length never changes (str.lower() can make a string LONGER, e.g. U+0130), so an index found in the copy is
    an index of the original. ONE helper for every case-insensitive scan of a held body."""
    return text.translate(_ASCII_LOWER)


def _held_steps(size):
    """A fresh step budget for one scan loop: each iteration spends one, and running out raises (the body is then unreadable), so no input can hang the daemon."""
    return [4 * size + 64]


def _held_spend(budget):
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("the scan did not finish within its step budget")


_HTML_SPACE = " \t\n\f\r"   # HTML whitespace: space, tab, LF, FF, CR (not NBSP, not a vertical tab)
_HTML_SPACE_RE = re.compile(r"[ \t\n\f\r]+")


def _held_skip_space(text, i):
    """The first index at or after `i` that is not HTML whitespace: the ONE helper for every attribute step."""
    while i < len(text) and text[i] in _HTML_SPACE:
        i += 1
    return i


def _held_class_tokens(tag):
    """The class tokens (ASCII-lowered) of an HTML tag's text: a linear walk over every `class` (HTML whitespace around the name and the `=`, quoted either
    way or bare); tokens are separated by HTML whitespace only."""
    low = _ascii_lower(tag)
    budget = _held_steps(len(low))
    tokens, at = [], low.find("class")
    while at >= 0:
        _held_spend(budget)
        i = _held_skip_space(low, at + 5)
        if i < len(low) and low[i] == "=":
            i = _held_skip_space(low, i + 1)
            if i < len(low) and low[i] in "\"'":
                end = low.find(low[i], i + 1)
                end = len(low) if end < 0 else end
                tokens += [t for t in _HTML_SPACE_RE.split(low[i + 1:end]) if t]
                i = end
            else:
                end = i
                while end < len(low) and low[end] not in _HTML_SPACE and low[end] != ">":
                    end += 1
                tokens += [t for t in _HTML_SPACE_RE.split(low[i:end]) if t]
                i = end
        at = low.find("class", max(i, at + 5))
    return tokens


def _held_tag_end(line, at):
    """Index of the `>` that closes the tag opened at `at`, skipping quoted attribute values (a `>` inside quotes does not close it); -1 when it never closes."""
    i, size = at, len(line)
    while i < size:
        c = line[i]
        if c in "\"'":
            i = line.find(c, i + 1)
            if i < 0:
                return -1
        elif c == ">":
            return i
        i += 1
    return -1


def _held_pre_mermaid(line):
    """True when `line` opens a `<pre>` whose class tokens include `mermaid`, or a `<pre` that is never closed on the line (it cannot be judged, so it
    counts). Each `<pre` is walked to its closing `>` with `find` and the search resumes after it: linear, no window, no backtracking."""
    low = _ascii_lower(line)
    budget = _held_steps(len(low))
    at = low.find("<pre")
    while at >= 0:
        _held_spend(budget)
        after = low[at + 4:at + 5]
        if not after.isalpha():
            end = _held_tag_end(line, at)
            if end < 0:
                return True
            if "mermaid" in _held_class_tokens(line[at:end]):
                return True
            at = low.find("<pre", end)
        else:
            at = low.find("<pre", at + 4)
    return False


def held_scan(lines):
    """(clean, mermaid): `clean` is `lines` with every fenced code block (fences included: three or more backticks or tildes, up to three spaces of
    indent, closed by the same character at least as long, an unclosed fence running to the end) and every HTML comment blanked, so a heading
    counts only outside them (the explainer block's own opening marker is kept); `mermaid` lists the indexes where a Mermaid block opens (a fence
    whose info string starts with `mermaid`, any case, or a `<pre class=mermaid>`)."""
    clean, mermaid, fence, comment = [], [], None, False
    for i, line in enumerate(lines):
        if fence is not None:
            found = _HELD_FENCE_RE.match(line)
            if found and found.group(1)[0] == fence[0] and len(found.group(1)) >= fence[1] and not found.group(2).strip():
                fence = None
            clean.append("")
            continue
        if not comment:
            found = _HELD_FENCE_RE.match(line)
            if found and not (found.group(1)[0] == "`" and "`" in found.group(2)):
                fence = (found.group(1)[0], len(found.group(1)))
                words = found.group(2).split()
                if words and _ascii_lower(words[0]) == "mermaid":
                    mermaid.append(i)
                clean.append("")
                continue
            if line.strip() == EXPLAINER_BEGIN:
                clean.append(line)
                continue
        if _held_pre_mermaid(line):
            mermaid.append(i)
        parts, pos, size = [], 0, len(line)   # walk with an index: no slicing, no growing concatenation
        budget = _held_steps(size)
        while pos < size:
            _held_spend(budget)
            if comment:
                end = line.find("-->", pos)
                if end < 0:
                    pos = size
                else:
                    comment, pos = False, end + 3
            else:
                start = line.find("<!--", pos)
                if start < 0:
                    parts.append(line[pos:])
                    pos = size
                else:
                    parts.append(line[pos:start])
                    comment, pos = True, start + 4
        out = "".join(parts)
        clean.append(out)
    return clean, mermaid


def held_top_end(rest):
    """Index in `rest` where the top part ends (PC-5): the first heading (outside code fences and comments) after the first top part that is not one of
    the four top parts, or the explainer block's opening marker; the end of the body when there is none. `rest` is the body after the opening lines."""
    clean = held_scan(rest)[0]
    first = next((i for i, l in enumerate(clean) if l.strip() in HELD_PARTS), None)
    if first is None:
        return len(rest)
    return next((i for i in range(first + 1, len(rest)) if (clean[i].startswith("#") and clean[i].strip() not in HELD_PARTS) or clean[i].strip() == EXPLAINER_BEGIN), len(rest))


def held_defects(body):
    """The ONE check of a held PR's top (see `_held_defects`); anything it cannot read is a defect, never a pass."""
    try:
        if len(body or "") > HELD_BODY_MAX:
            return ["the held PR's top is unreadable: the body is longer than %d characters" % HELD_BODY_MAX]
        return _held_defects(body)
    except Exception as err:   # a body it cannot parse fails: unreadable, never ok
        return ["the held PR's top is unreadable (%s)" % _one_line("%s: %s" % (type(err).__name__, err), 100)]


def _held_defects(body):
    """What is wrong with the top of a PR body held for the owner (used by `pr-explain --check` and gate G6b). After the opening lines
    (PLAN:, MERGE:, canon:), in this order: `## Why this needs you` (1 to 3 sentences, no placeholder), `## What changes for you` (a Before | After
    table of 2 to 4 rows of real cells), `## Example` (real text), `## Questions for you` (a numbered list, each item with `Recommendation:`) or the
    line `Questions: none (reason)`. An image may sit anywhere in the top; a Mermaid block in the top is a defect; nothing below the top part is
    judged, except that the generated summary flowchart (four boxes: Changes:, Touches:, Irreversible:, Risk:) is a defect ANYWHERE in the body. Headings count only outside code fences and comments. Reads the body only."""
    lines = (body or "").split("\n")
    clean_all, mermaid_all = held_scan(lines)
    at = 0
    while at < len(lines) and (EXPLAINER_HEADER_RE.match(lines[at]) or not clean_all[at].strip()):
        at += 1
    rest = lines[at:]
    top_end = held_top_end(rest)
    top = clean_all[at:at + top_end]
    pos = {}
    for i, l in enumerate(top):
        pos.setdefault(l.strip(), i)

    def section(name):
        start = pos[name] + 1
        stop = next((i for i in range(start, len(top)) if top[i].startswith("## ")), len(top))
        return top[start:stop]

    def words(text):
        return len(re.findall(r"[^\W_]", text))   # underscores are not word characters here

    problems = []
    if HELD_WHY not in pos:
        problems.append("the held PR needs a `%s` section after the opening lines" % HELD_WHY)
    else:
        text = " ".join(l.strip() for l in section(HELD_WHY) if l.strip() and not l.strip().startswith("!["))
        sentences = [x for x in re.split(r"(?<=[.!?])\s+", text) if x.strip()]
        if words(text) < 3 or _HELD_PLACEHOLDER_RE.search(text):
            problems.append("`%s` is empty or still has a <placeholder>" % HELD_WHY)
        elif len(sentences) > 3:
            problems.append("`%s` has %d sentences: it is one to three" % (HELD_WHY, len(sentences)))
    if HELD_TABLE not in pos:
        problems.append("the held PR needs a `%s` table (Before | After, 2 to 4 rows)" % HELD_TABLE)
    else:
        rows = [[c.strip() for c in l.strip().strip("|").split("|")] for l in section(HELD_TABLE) if l.strip().startswith("|")]
        head = [_ascii_lower(c) for c in rows[0]] if rows else []
        data = [r for r in rows[2:]] if len(rows) >= 2 and all(re.fullmatch(r":?-{3,}:?", c) for c in rows[1]) else None
        if head != ["before", "after"] or data is None:
            problems.append("`%s`: the table needs a Before and an After column (a header row, a separator row, then the rows)" % HELD_TABLE)
        elif not 2 <= len(data) <= 4:
            problems.append("`%s`: the table has %d rows, it is 2 to 4" % (HELD_TABLE, len(data)))
        elif any(len(r) != 2 or any(words(c) < 3 or _HELD_PLACEHOLDER_RE.search(c) for c in r) for r in data):
            problems.append("`%s`: every table cell is a plain sentence (at least 3 word characters, no <placeholder>)" % HELD_TABLE)
    if HELD_EXAMPLE not in pos:
        problems.append("the held PR needs an `%s` section: one concrete case in a few lines" % HELD_EXAMPLE)
    else:
        text = " ".join(l.strip() for l in section(HELD_EXAMPLE) if l.strip() and not l.strip().startswith("!["))
        if len(text) < 20 or _HELD_PLACEHOLDER_RE.search(text):
            problems.append("`%s` is empty, too short (20 characters) or still has a <placeholder>" % HELD_EXAMPLE)
    if any(at <= m < at + top_end for m in mermaid_all):
        problems.append("the top part shows a Mermaid diagram: it goes below, in the technical section")
    if summary_flowchart_found(body):   # ANYWHERE in the body, the top part or below it or inside the explainer markers (ask #687)
        problems.append(SUMMARY_FLOWCHART_DEFECT)
    nones = [i for i, l in enumerate(top) if _held_none(l)]
    if HELD_QUESTIONS in pos:
        section_lines = [l for l in section(HELD_QUESTIONS) if l.strip()]
        items, cur = [], None
        for l in section_lines:
            if re.match(r"^\s*\d+\.\s", l):
                cur = [l]
                items.append(cur)
            elif cur is not None:
                cur.append(l)
        if items:
            for n, item in enumerate(items, 1):
                if "Recommendation:" not in " ".join(item):
                    problems.append("question %d has no `Recommendation:`" % n)
        elif not (len(section_lines) == 1 and _held_none(section_lines[0])):
            problems.append("`%s` is a numbered list (each with `Recommendation:`) or exactly one line `Questions: none (reason)`" % HELD_QUESTIONS)
    elif len(nones) != 1:
        problems.append("the held PR needs `%s` (a numbered list, each question with `Recommendation:`) or the line `Questions: none (reason)`" % HELD_QUESTIONS)
    order = [pos[n] for n in HELD_PARTS if n in pos]
    if HELD_QUESTIONS not in pos and len(nones) == 1:
        order.append(nones[0])
    if order != sorted(order):
        problems.append("the order is wrong: Why this needs you, What changes for you, Example, then Questions for you")
    return problems


def cmd_pr_explain(args, runner, now):
    """`pr-explain <owner/repo#N>`: read the PR once and print the explainer block (the why part), or with
    `--body-file F` the whole new body with the block in once, or with `--check` judge the description it has. It writes nothing anywhere."""
    try:
        repo, number = parse_pr_ref(args.pr)
    except NoticeError as err:
        sys.stderr.write("%s: %s\n" % (PROG, _one_line(str(err), 300)))
        return EXIT_USAGE
    engine, _, _ = build_engine(runner, now, "cli", writable=False)
    try:
        pr = read_pr_json(engine, repo, number)
    except NoticeError as err:
        sys.stderr.write("%s: %s\n" % (PROG, _one_line(str(err), 300)))
        return EXIT_ERROR
    body = pr.get("body") if isinstance(pr.get("body"), str) else ""
    if args.check:
        problems = explainer_defects(body)
        if pr_is_held(pr.get("labels") or [], body):
            problems += [p for p in held_defects(body) if p not in problems]   # the summary flowchart is named once
        for problem in problems:
            print("DEFECT: %s" % problem)
        print("explainer: %s" % ("FAIL" if problems else "PASS"))
        return EXIT_ERROR if problems else EXIT_OK
    block = explainer_block(args.before or "<what happened before, one or two lines>", args.after or "<what happens after, one or two lines>")
    if args.body_file:
        try:
            current = body if args.body_file == "-" else Path(args.body_file).read_text()
        except OSError as err:
            sys.stderr.write("%s: pr-explain: cannot read %s: %s\n" % (PROG, args.body_file, getattr(err, "strerror", None) or err))
            return EXIT_USAGE
        print(explainer_insert(current, block))
    else:
        print(block)
    return EXIT_OK


cmd_pr_explain.direct = True   # no error mapping, no daemon start


def add_pr_explain_parsers(sub):
    explain = sub.add_parser("pr-explain", help="Print the PR explainer block: a before/after why part (read-only; never a diagram)",
                             description="Reads the PR once and PRINTS the block for its description; it writes nothing. Paste it, or use --body-file to get the whole "
                                         "new body with the block in once, then `gh pr edit --body-file`. --check judges the description the PR has.")
    explain.add_argument("pr", help="The pull request: owner/repo#N or its URL")
    explain.add_argument("--before", help="The behaviour before, one or two lines")
    explain.add_argument("--after", help="The behaviour after, one or two lines")
    explain.add_argument("--body-file", help="A file with a PR body (or - for the PR's own): print it with the block in once")
    explain.add_argument("--check", action="store_true", help="Judge the PR description: one block, a real why part, no generated summary flowchart")
