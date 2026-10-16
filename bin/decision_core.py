"""decision_core: the pure core of the Fleet Switchboard's Decision subsystem.

A Decision is anything that waits on someone above the agent that raised it. The Switchboard derives the list on
every pass (docs/switchboard.md, "Decisions"); this module holds the rules that turn what the sources found into
that list, and nothing else.

What it owns (all pure: plain data in, plain data out):

  - the vocabulary: kinds, tiers, statuses, the report states that matter to a decision, the entry shape (Decision);
  - display ids (assign_ids) and the id of an issue as a message names it (issue_tag);
  - the tier rules: of an issue's labels, of a pending request (including the grace rule), of a report;
  - answered-ness: when a report no longer waits on anyone (reports and sends are DATA, passed in);
  - folding and dedupe: one question shown once (build_entries), and the entry each decision becomes;
  - headline cutting, batch row parsing, the view filters, the over-1h count.

What it must never do: read or write a file, call `gh` or v2, read the clock (the caller passes `now`), read the
environment, or touch the audit log. The fault gate of the Lab is the caller's: a fault arrives as an argument.

What stays in bin/fleet-switchboard: discovery, the readers of GitHub, the harness and transcripts, delivery of an
answer to an agent, the audit, the state files, the daemon, the CLI. It reaches this module through the ports at the
end of this file (DecisionSource, AnswerDelivery, DecisionStore), which are used from step 3 on; none has an
implementation yet.

Python 3.9+, standard library only. The module is a sibling of the script and is loaded by path from beside it
(see `load_decision_core` in bin/fleet-switchboard), so it needs no packaging and no sys.path.
"""

import abc
from datetime import datetime, timezone
import hashlib
import re
import uuid

# ---------------------------------------------------------------- small text and time helpers


def one_line(text, limit=300):
    """`text` on one line, at most `limit` characters (an ellipsis when it was cut)."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def line_of(text, limit):
    """`text` as one line of at most `limit` characters, with anything shaped like a bearer
    credential or a provider key taken out."""
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [key]", str(text if text is not None else ""))
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{6,}", "[key]", text)
    return one_line(text, limit)


def iso(epoch):
    """Epoch seconds as an ISO-8601 UTC time with `Z`, or None."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(text):
    """An ISO-8601 time with `Z` or an offset -> epoch seconds, or None."""
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


# ---------------------------------------------------------------- the vocabulary

KINDS = ("issue", "permission", "question", "report", "notice", "captain")
DECISION_TIERS = ("cos", "human", "orchestrator")   # the order the CLI's --tier choices show
TIER_LADDER = ("orchestrator", "cos", "human")      # a decision goes up one tier at a time, in this order
STATUSES = ("open", "answered", "withdrawn", "superseded", "expired")   # today every listed decision is "open"

DECISION_TITLE_MAX = 200
# a report line may be REPORT_MAX characters and a `blocked` one gets a "blocked: " lead, so a report decision
# keeps its whole line (only reports: the other sources keep DECISION_TITLE_MAX)
DECISION_REPORT_TITLE_MAX = 300 + len("blocked: ")
DECISION_LONG_WAIT = 3600   # seconds: a decision waiting longer is "over 1h" (the panel's LONG_WAIT_SECONDS; a test keeps them equal)

WITHDRAW_ALL = "[all] "  # leads a `withdrawn` report made with --all: it clears every open question of the worker
# the report states that decide a decision (a report is open while it is `question` or `blocked`)
OPEN_REPORT_STATES = ("question", "blocked")
# what a LATER report of the same worker on the same ask closes: with an issue, a newer question or an end; with no
# issue, only an end (working, paused and blocked never answer)
ANSWERING_STATES = ("question", "done", "failed", "withdrawn")
ANSWERING_STATES_UNTAGGED = ("done", "failed", "withdrawn")

# `(issue #2)` with one repo in play, `(issue api#2)` or `(issue owner/api#2)` with several (issue_tag)
REPORT_LINE = re.compile(r"^- report from (\S+?)(?: \(issue (?:([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?))?#(\d+)\))?: ([a-z]+): (.*)$")
SEND_LINE = re.compile(r"^- send from (\S+?)(?: \(issue (?:([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?))?#(\d+)\))?: ")


class Decision:
    """One decision as the list shows it: the entry's fields by name.

    `entry()` is today's list shape, a plain dict, in the order readers have always seen. `extra` holds the fields
    only some decisions have (rows, raised_by, reported_by, considered, session, request), in the order to emit."""

    def __init__(self, ident, title, ask, repo, agent, kind, since, tier, headline, extra=None, status="open"):
        self.id, self.title, self.ask, self.repo, self.agent = ident, title, ask, repo, agent
        self.kind, self.since, self.tier, self.headline = kind, since, tier, headline
        self.extra = list((extra or {}).items())
        self.status = status

    def entry(self):
        found = {"id": self.id, "title": self.title, "ask": self.ask, "repo": self.repo, "agent": self.agent,
                 "kind": self.kind, "since": self.since, "tier": self.tier, "headline": self.headline}
        found.update(self.extra)
        return found


# ---------------------------------------------------------------- display ids


def issue_tag(repo, number, repos=None):
    """How a message names an issue: `#2` with one repo in play, `api#2` with several (the repo name,
    or owner/name when two of them share a name)."""
    if not repo or not repos or len(repos) < 2:
        return "#%d" % number
    short = repo.split("/")[1]
    clash = [r for r in repos if r.split("/")[1] == short]
    return "%s#%d" % (repo if len(clash) > 1 else short, number)


def decision_number(seed):
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12], 16)


def id_candidates(item, number, repos=None, bare_issue_number=False):
    """The ids a decision may take, best first. `bare_issue_number` is the Lab's `bare-issue-number` fault."""
    ask, agent = item.get("ask"), item.get("agent")
    tag = issue_tag(None if bare_issue_number else item.get("repo"), ask, repos) if ask else None
    if ask and item.get("bare"):
        yield tag                              # an issue's own label is the issue's tag: `#2`, or `api#2` with several repos
    if ask:
        for step in range(26 * 26):            # the tag and two letters taken from the decision's own hash
            pair = (number + step) % 676
            yield "%s%s%s" % (tag, chr(97 + pair // 26), chr(97 + pair % 26))
    else:
        for step in range(900):                # `<agent>-` and three digits likewise
            yield "%s-%03d" % (agent or "fleet", 100 + (number + step) % 900)
    yield "%s-%x" % (tag if ask else agent or "fleet", number)


def assign_ids(items, repos=None, bare_issue_number=False):
    """The id of each decision in `items` (dicts with seed, ask, agent, bare, since, repo), in the same order.
    `repos` are the watched repos: with more than one, an issue's tag names its repo, as in a message.

    Derived from the decision's own identity (`seed`) and nothing else: no counter, no file. An issue's own
    label is `#<issue>`; any other decision about an issue is `#<issue>` and two letters; one about no issue is
    `<agent>-` and three digits. The letters and digits come from a hash of the seed, so an id does not change
    when another decision resolves (a rank among the open ones would). Two open decisions never share an id: on
    a collision, the one that has waited longer keeps the id and the later one takes the next free one."""
    order = sorted(range(len(items)), key=lambda i: (items[i]["since"], items[i]["seed"]))
    taken, ids = set(), [None] * len(items)
    for index in order:
        item = items[index]
        for candidate in id_candidates(item, decision_number(item["seed"]), repos, bare_issue_number):
            if candidate not in taken:
                break
        taken.add(candidate)
        ids[index] = candidate
    return ids


# ---------------------------------------------------------------- tier rules


def issue_tier(label_names, human_label, cos_label):
    """The tier of an open issue by its labels: `human` with the human's label (an issue with both was escalated),
    `cos` with the Chief of Staff's only, None with neither (it is not a decision)."""
    return "human" if human_label in label_names else "cos" if cos_label in label_names else None


def request_tier(now, waiting_since, grace_seconds, escalated=False):
    """The tier of a pending permission request or question: the Chief of Staff's (`cos`) until it has waited
    `grace_seconds` or was escalated, then the human's. The grace rule as a pure function of its inputs."""
    return "human" if escalated or now - waiting_since >= grace_seconds else "cos"


def report_tier(boss_has_boss):
    """The tier of a report: an orchestrator's when the boss it was sent to has a boss of its own, else the Chief
    of Staff's (a boss that has no boss is the Chief of Staff)."""
    return "orchestrator" if boss_has_boss else "cos"


# ---------------------------------------------------------------- answered-ness


def repo_of_tag(tag, repos):
    """The repo a message's issue tag names (`web` or `acme/web`) among `repos`, or None when it names none or
    several (the tag is the repo name, or owner/name when two of the repos share a name: issue_tag)."""
    if not tag:
        return None
    found = [repo for repo in repos or [] if repo == tag or (("/" not in tag) and repo.split("/")[1] == tag)]
    return found[0] if len(found) == 1 else None


def answered_by_later_report(report, reports):
    """True when the same worker later reported on the same ask and repo a newer `question`, a `done`, `failed` or
    `withdrawn` (`withdrawn --all` on any ask). An untagged report is answered only by a later untagged `done`,
    `failed` or `withdrawn`. `reports` are dicts with at, sender, issue, repo, state, text."""
    for other in reports:
        if other is report or other["sender"] != report["sender"] or other["at"] <= report["at"]:
            continue
        if other["state"] == "withdrawn" and other["text"].startswith(WITHDRAW_ALL):
            return True  # `withdrawn --all`: every open question of the worker, on any ask
        if other["issue"] != report["issue"] or other["repo"] != report["repo"]:
            continue
        # working, paused and blocked never answer; an untagged report answers only by finishing or withdrawing
        if other["state"] in (ANSWERING_STATES_UNTAGGED if report["issue"] is None else ANSWERING_STATES):
            return True
    return False


def answered_by_send(report, send_lines, repos):
    """True when the boss sent the worker something on the ask (a `send` with no issue counts for any ask: the
    worker has one; such a send exists only where `send` does not require --issue, i.e. no `intent_repo` or
    `intent_required: false`, and with both set the rule is simply never reached) after the report. A report that names no issue (#85) is answered by ANY later send from its boss,
    whatever issue the send carries: it names no ask for the send to be about. `send_lines` are the worker's
    synthetic lines, as [(created, message id, line number, line)]."""
    for created, _, _, line in send_lines:
        match = SEND_LINE.match(line)
        if match and match.group(1) == report["boss"] and created > report["at"][0] \
                and (match.group(3) is None or report["issue"] is None
                     or (int(match.group(3)) == report["issue"]
                                                and repo_of_tag(match.group(2), repos) in (None, report["repo"]))):
            return True
    return False


def report_answered(report, reports, send_lines, repos):
    """True when the report is answered, given everything as data: the worker's reports, its synthetic lines (the
    sends it received) and the watched repos. The caller that must read the sends lazily (a report answered by a later
    report needs no read) calls the two halves, answered_by_later_report and answered_by_send."""
    return answered_by_later_report(report, reports) or answered_by_send(report, send_lines, repos)


# ---------------------------------------------------------------- headlines

HEADLINE_WIDTH = 34   # the Decisions panel's ROW_WIDTH (plugins/fleet-decisions-tui/decisions.mjs); a test keeps them equal
HEADLINE_LINES = 2
_URL = re.compile(r"https?://[^\s<>\"'`]+")
_GITHUB_REF = re.compile(r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(?:issues|pull)/(\d+)(?:[/?#].*)?$")
_ISSUE_URL = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)(?![\w])")
_BARE_REF = re.compile(r"(?<![\w/&.#-])#(\d+)(?!\w)")
_SHORT_REF = re.compile(r"(?<![\w/.-])((?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+)#(\d+)(?!\w)")


def short_refs(text):
    """`text` with every URL as a short ref: a GitHub issue or pull request URL is `repo#N`, any other URL its host."""
    def one(match):
        url = match.group(0)
        tail = ""
        while url and url[-1] in ".,;:!?)]}":
            url, tail = url[:-1], url[-1] + tail
        found = _GITHUB_REF.match(url)
        if found:
            return "%s#%s%s" % (found.group(2), found.group(3), tail)
        host = re.match(r"^https?://([^/?#\s]+)", url)
        return (host.group(1) if host else url) + tail
    return _URL.sub(one, text)


def wrap_count(words, width):
    """How many lines `words` take at `width`: the panel's word wrap (a word wider than a line is broken hard)."""
    lines, line = 0, 0
    for word in words:
        while len(word) > width:
            if line:
                lines, line = lines + 1, 0
            lines, word = lines + 1, word[width:]
        if not line:
            line = len(word)
        elif line + 1 + len(word) <= width:
            line += 1 + len(word)
        else:
            lines, line = lines + 1, len(word)
    return lines + (1 if line else 0)


def fit_lines(text, width=HEADLINE_WIDTH, lines=HEADLINE_LINES):
    """`text` cut at a word boundary to what fits `lines` lines of `width`; an ellipsis only when something was cut."""
    words = text.split()
    if wrap_count(words, width) <= lines:
        return " ".join(words)
    kept = []
    for word in words:
        if wrap_count(kept + [word + "\u2026"], width) > lines:
            break
        kept.append(word)
    if not kept:  # one word wider than the whole headline: break it hard
        return words[0][:width * lines - 1] + "\u2026"
    return " ".join(kept) + "\u2026"


def headline_of(item, issue_title):
    """One decision's headline: an issue its title; a report the title of the issue it is about (when known) and
    its gist, else the report line. URLs are short refs; at most HEADLINE_LINES panel lines."""
    text = short_refs(" ".join(str(item["title"]).split()))
    if item["kind"] != "report" or not issue_title:
        return fit_lines(text)
    about = short_refs(" ".join(issue_title.split()))
    if wrap_count((about + ": " + text).split(), HEADLINE_WIDTH) <= HEADLINE_LINES:
        return about + ": " + text
    if wrap_count(about.split(), HEADLINE_WIDTH) > 1:  # one line for the issue, the rest for the report
        about = fit_lines(about, HEADLINE_WIDTH, 1)
    first = about
    room = HEADLINE_WIDTH * HEADLINE_LINES - len(first) - 2
    return fit_lines(first + ": " + text, HEADLINE_WIDTH, HEADLINE_LINES) if room > 8 else fit_lines(text)


def suffixed_headline(text, suffix):
    """`text` with `suffix` after it, the suffix always whole: the text is cut to leave room when both do not fit."""
    if wrap_count((text + suffix).split(), HEADLINE_WIDTH) <= HEADLINE_LINES:
        return text + suffix
    return fit_lines(text, HEADLINE_WIDTH, 1) + suffix


def batch_headline(title, count):
    """`<title> (n rows)`: the title is cut to leave the count whole."""
    suffix = " (%d row%s)" % (count, "" if count == 1 else "s")
    text = short_refs(" ".join(str(title).split()))
    if wrap_count((text + suffix).split(), HEADLINE_WIDTH) <= HEADLINE_LINES:
        return text + suffix
    return fit_lines(text, HEADLINE_WIDTH, 1) + suffix


def named_issues(text, repo, wanted):
    """The issue numbers `text` names, as a short ref (`name#N`, or `owner/name#N`) or an issue URL, among `wanted`
    (the open issue decisions of `repo`)."""
    found = set()
    for match in _ISSUE_URL.finditer(text):
        if match.group(1) == repo and int(match.group(2)) in wanted:
            found.add(int(match.group(2)))
    bare = _URL.sub(" ", text)
    for match in _BARE_REF.finditer(bare):   # a bare `#N` is an issue of the report's own repo
        if repo and int(match.group(1)) in wanted:
            found.add(int(match.group(1)))
    for match in _SHORT_REF.finditer(bare):
        name = match.group(1)
        if repo and (name == repo or name == repo.split("/")[-1]) and int(match.group(2)) in wanted:
            found.add(int(match.group(2)))
    return found


# ---------------------------------------------------------------- a batch: related findings raised as ONE decision

BATCH_FENCE = "decision-rows"
BATCH_ROWS_MAX = 40            # rows one batch may carry (the panel draws one line or two per row)
BATCH_REF_MAX, BATCH_FINDING_MAX, BATCH_REC_MAX = 100, 200, 120
BATCH_TITLE_MAX = 120
BATCH_RAISE_ROWS_MAX = 20      # rows one batch may carry when it is RAISED (a batch already open may hold up to BATCH_ROWS_MAX: it is read, never refused)
BATCH_FINDING_RAISE_MAX = 4000   # the finding of one row as raised, kept in FULL in the issue body
BATCH_REC_RAISE_MAX = 2500       # the recommendation of one row as raised: shorter, because the recorded answer quotes it for every row (20 x (2500 + a 240-char choice + a 100-char ref) fits one comment)
BATCH_BODY_MAX_CHARS = 65000     # the whole issue body (GitHub's limit is 65536 characters)
BATCH_COMMENT_MAX_CHARS = 65000  # the recorded answer comment (the same platform limit): checked before anything is sent or written
OUTWARD_ROWS_BUDGET = 3000       # the characters of row text one outward message carries whole; a row that does not fit is sent cut AND marked, with a pointer to the issue
OUTWARD_CUT_MIN = 100            # a cut row still carries at least this much of each part
_BATCH_ROW_HEADING = re.compile(r"^###[ \t]+(?:[0-9]+\.[ \t]+)?([^\s|]+)[ \t]*$")
_ROWS_BLOCK = re.compile(r"^```%s[ \t]*\r?\n(.*?)^```[ \t]*\r?$" % BATCH_FENCE, re.M | re.S)
_ROW_REF = re.compile(r"^(?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+#[0-9]+$")


class BatchError(Exception):
    """A batch that cannot be raised, and why. Nothing was created."""


def short_ref_of(ref):
    """`repo#N` for a row's ref given as `repo#N`, `owner/repo#N` or an issue or pull request URL."""
    ref = short_refs(ref.strip())
    return ref.split("/")[-1] if "/" in ref else ref


def parse_batch_row(line):
    """One row line `<ref> | <finding> | <recommendation>` as {ref, short, finding, recommendation, text}. Raises
    BatchError for an empty part, a ref that names no issue, or a fence in the text. The text is kept whole (the command bounds its length)."""
    parts = [" ".join(part.split()) for part in line.split("|", 2)]
    if len(parts) != 3 or not all(parts):
        raise BatchError("a row is `<repo#N> | <finding> | <recommendation>`, each part not empty: %r" % one_line(line, 80))
    ref, finding, rec = parts
    if "```" in line:
        raise BatchError("a row may not contain a code fence: %r" % one_line(line, 80))
    if len(ref) > BATCH_REF_MAX or not (_ROW_REF.match(ref) or _GITHUB_REF.match(ref)):
        raise BatchError("%r is not an issue: write repo#N, owner/repo#N or the issue's URL" % one_line(ref, 60))
    return row_of(ref, finding, rec)


def row_repo(ref):
    """The `owner/repo` a row's ref names (`owner/repo#N` or an issue URL), or None for a short `name#N`."""
    found = _GITHUB_REF.match(ref)
    if found:
        return "%s/%s" % (found.group(1), found.group(2))
    return ref.split("#")[0] if "/" in ref.split("#")[0] else None


def display_cut(text, width):
    """The ONLY cut a row's text takes: for drawing. Nothing stored, answered, escalated or synced ever goes through it."""
    return text if len(text) <= width else text[:width - 1] + "\u2026"


def row_of(ref, finding, rec):
    """A row, whole: `finding` and `recommendation` are exactly the stored text (every reader that acts on a row uses these). `text` is the one DISPLAY field (what the
    decision list and the panel draw), the recommendation cut for drawing."""
    short = short_ref_of(ref)
    return {"ref": ref, "short": short, "finding": finding, "recommendation": rec,
            "text": "%s: %s" % (short, display_cut(short_refs(rec), BATCH_REC_MAX))}


_MD_ACTIVE = re.compile(r"([\\<>@\[\]`*_])")
_MD_ESCAPED = re.compile(r"\\([\\<>@\[\]`*_])")


def md_escape(text):
    """Row text made inert as Markdown: each active character is backslash-escaped (no mention, link, image, HTML or comment, and no look-alike of the signer mark), and
    md_unescape gives the exact text back."""
    return _MD_ACTIVE.sub(r"\\\1", text)


def md_unescape(text):
    return _MD_ESCAPED.sub(r"\1", text)


def batch_row_sections(rows):
    """The Markdown of a batch's rows: one section for each, `### N. <issue>` then `**Finding:** ...` and `**Recommendation:** ...` as their own paragraphs, the FULL text, no code
    block. A row's text is one line (whitespace is collapsed when it is parsed), so it can never start a line: a heading, a fence marker or a row-looking text inside it is
    mid-line text and cannot split the section."""
    return "\n\n".join("### %d. %s\n\n**Finding:** %s\n\n**Recommendation:** %s" % (number, row["ref"], md_escape(row["finding"]), md_escape(row["recommendation"]))
                       for number, row in enumerate(rows, 1))


def parse_markdown_rows(body):
    """The rows of a batch body written as Markdown sections (see batch_row_sections), in order; [] when it has none. Lenient like the fenced reader: a section without a finding and
    a recommendation, or whose heading names no issue, is skipped; at most BATCH_ROWS_MAX are read. The rows are the same records the fenced format gives (cut for display)."""
    rows, current, mode = [], None, None

    def close():
        if current and current["finding"] and current["rec"]:
            try:
                rows.append(row_of(current["ref"], md_unescape(" ".join(current["finding"])), md_unescape(" ".join(current["rec"]))))
            except BatchError:
                pass
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        heading = _BATCH_ROW_HEADING.match(line)
        if heading and (_ROW_REF.match(heading.group(1)) or _GITHUB_REF.match(heading.group(1))):
            close()
            if len(rows) >= BATCH_ROWS_MAX:
                return rows
            current, mode = {"ref": heading.group(1), "finding": [], "rec": []}, None
            continue
        if line.startswith("### "):
            close()
            current, mode = None, None
            continue
        if current is None or line.lstrip().startswith("<!--"):
            continue
        text = line.strip()
        if text.startswith("**Finding:**"):
            mode, text = "finding", text[len("**Finding:**"):].strip()
        elif text.startswith("**Recommendation:**"):
            mode, text = "rec", text[len("**Recommendation:**"):].strip()
        if mode and text:
            current[mode].append(" ".join(text.split()))
    close()
    return rows


def parse_decision_rows(body):
    """The rows of an issue body: its Markdown row sections, or, for a batch already open in the older format, its fenced `decision-rows` block; [] for a body with none.
    Lenient: a line or section that is not a row is skipped, and at most BATCH_ROWS_MAX rows are read (the daemon never refuses an issue)."""
    if not isinstance(body, str):
        return []
    rows = parse_markdown_rows(body)
    if rows:
        return rows
    found = _ROWS_BLOCK.search(body)
    for line in (found.group(1).splitlines() if found else []):
        if not line.strip():
            continue
        try:
            rows.append(parse_batch_row(line))
        except BatchError:
            continue
        if len(rows) >= BATCH_ROWS_MAX:
            break
    return rows


# ---------------------------------------------------------------- order: what a decision waits for

WAITS_FOR_MAX = 10   # refs one decision may name
_SECTION_HEADING = re.compile(r"^##[ \t]+decision required(?![A-Za-z0-9_])", re.I)
_ANY_H2 = re.compile(r"^##(?!#)[ \t]")
_WAITS_FOR_LINE = re.compile(r"^[ \t]*waits[ \t]+for[ \t]*:[ \t]*(.*?)[ \t]*$", re.I)
_WAIT_REF = re.compile(r"^(?:([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+))?#([0-9]+)$")


def parse_waits_for(body, repo=None):
    """The issues a decision says it waits for: the `Waits for: owner/repo#N, #M` line INSIDE its `## Decision required` section (up to the
    next `##` heading), as lower-case `owner/repo#N` refs in the order written, a bare `#M` meaning `repo` (the issue's own). Only that
    section is read: the same line anywhere else in the body is prose. Lenient: an entry that is not a ref is skipped, a repeat is dropped,
    and at most WAITS_FOR_MAX are kept (the daemon never refuses an issue). [] for a body with no such line."""
    if not isinstance(body, str):
        return []
    inside, refs = False, []
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _SECTION_HEADING.match(line):
            inside = True
            continue
        if inside and _ANY_H2.match(line):
            inside = False
        found = _WAITS_FOR_LINE.match(line) if inside else None
        if not found:
            continue
        for part in found.group(1).split(","):
            match = _WAIT_REF.match(part.strip())
            if not match:
                continue
            owner_repo = (match.group(1) or repo or "").lower()
            if not owner_repo:
                continue
            ref = "%s#%d" % (owner_repo, int(match.group(2)))
            if ref not in refs:
                refs.append(ref)
    return refs[:WAITS_FOR_MAX]


def decision_ref(entry):
    """`owner/repo#N` (lower case) of an issue decision, or None for anything else (a report, a request, a notice)."""
    if entry.get("kind") != "issue" or not entry.get("repo") or not isinstance(entry.get("ask"), int):
        return None
    return "%s#%d" % (str(entry["repo"]).lower(), entry["ask"])


def waits_graph(decisions):
    """({ref: entry}, {ref: [refs it waits for that are themselves open decisions]}): the edges inside the list. A ref that is not an open
    decision is not an edge (it is something else, or closed, or missing: the sweep says which)."""
    listed = {decision_ref(d): d for d in decisions if decision_ref(d)}
    edges = {ref: [w for w in (d.get("waits_for") or []) if w in listed] for ref, d in listed.items()}
    return listed, edges


def find_cycles(decisions):
    """The groups of decisions that wait for each other: [[ref, ...]] each sorted, the groups sorted. A decision that waits for itself is a group
    of one. Decisions that only wait for a cycle are not in it."""
    listed, edges = waits_graph(decisions)
    reach = {}
    for ref in listed:
        seen, stack = set(), list(edges[ref])
        while stack:
            at = stack.pop()
            if at in seen:
                continue
            seen.add(at)
            stack.extend(edges.get(at, ()))
        reach[ref] = seen
    members = {ref for ref in listed if ref in reach[ref]}
    groups, taken = [], set()
    for ref in sorted(members):
        if ref in taken:
            continue
        group = sorted(m for m in members if m in reach[ref] and ref in reach[m] or m == ref)
        taken.update(group)
        groups.append(group)
    return groups


ORDER_DEPTH_MAX = 4   # how deep a chain of waits is drawn; deeper ones sit at this depth


def annotate_order(decisions):
    """Say, on each issue decision, where it sits in the order of what to do. In place, and ONLY when some decision names what it waits for;
    a list in which none does is returned untouched (no field is added, the order stays oldest first).

    Added, all optional: `order` (its place, 0..), `depth` (0 for what can be done now, 1 under the decision it waits for, ...), `blocked_by`
    (the display id of that decision: the first of its `waits_for` that is an open decision), `mark`: `next` on the first decision that can
    be done now, `cycle` on decisions that wait for each other (they are drawn at the top and are never `next`). `waits_for` itself is
    the field the entry already carries. The list's own order is not changed: a reader that wants the arrangement sorts by `order`."""
    if not any(d.get("waits_for") for d in decisions):
        return decisions
    listed, edges = waits_graph(decisions)
    cyclic = {ref for group in find_cycles(decisions) for ref in group}
    parent = {ref: next((w for w in edges[ref] if w != ref), None) if ref not in cyclic else None for ref in listed}
    children = {}
    for ref, up in parent.items():
        if up is not None:
            children.setdefault(up, []).append(ref)
    place, first = [], True
    sequence = [d for d in decisions if decision_ref(d)]
    position = {decision_ref(d): i for i, d in enumerate(sequence)}

    def walk(ref, depth):
        place.append((ref, depth))
        for child in sorted(children.get(ref, ()), key=position.get):
            walk(child, min(depth + 1, ORDER_DEPTH_MAX))
    for d in sequence:
        ref = decision_ref(d)
        if parent[ref] is None:
            walk(ref, 0)
    for n, (ref, depth) in enumerate(place):
        entry = listed[ref]
        entry["order"], entry["depth"] = n, depth
        up = parent[ref]
        if up is not None:
            entry["blocked_by"] = listed[up]["id"]
        if ref in cyclic:
            entry["mark"] = "cycle"
        elif depth == 0 and first:
            entry["mark"], first = "next", False
    rest = [d for d in decisions if not decision_ref(d)]
    for n, d in enumerate(rest, len(place)):   # a report, a request: after the issues, as they were
        d["order"], d["depth"] = n, 0
    return decisions


def arranged(decisions):
    """The decisions in the order to show them: by `order` when annotate_order gave one to every entry, else as they are (oldest first)."""
    if decisions and all(isinstance(d.get("order"), int) for d in decisions):
        return sorted(decisions, key=lambda d: d["order"])
    return list(decisions)


# ---------------------------------------------------------------- folding: items in, the decision list out


FOLDED_LINES_KEPT = 3    # the newest folded report lines an issue decision keeps (`reported_lines`)
FOLDED_LINE_MAX = 160    # characters of each


def build_entries(items, ids, home, known_title, tier_flat=False, known_parent=None):
    """The decisions (as list entries) the source `items` become, not yet ordered and without notices.

    `items` are what the sources found (dicts with kind, title, ask, repo, agent, since, seed, bare, tier, and
    optionally rows, raised_by, considered, session, request, repo_known, parent); `ids` are their display ids, in the same order
    (assign_ids); `home` is the repo a bare issue belongs to (or None); `known_title(repo, number)` is a lookup of
    an issue's title the caller already holds, never a read (None when unknown); `known_parent(repo, number)` likewise gives an
    issue's parent (its ask, for a work item) or None. `tier_flat` is the Lab's `tier-flat` fault.

    One question, shown once: a report on an issue that is itself a decision (same repo and number) is folded into
    the issue's decision (its newest lines stay in the entry as `reported_lines`). Repos are compared without case. A report
    whose repo is only a guess (`repo_known` false) folds into the one listed issue decision with its number. A report with no
    issue number folds into the one open issue decision its text names (`repo#N`, an issue URL, or a bare `#N` in its own repo).
    Into a decision of the PERSON's tier (an escalated issue) a report also folds when it is about the same piece of work seen from
    the other end of the ask chain (a work item whose ask is that decision, or the ask of a work item that is) and when its issue
    number is not itself a decision but its text names exactly one such decision. A report on an issue that is only cos-tier,
    or names nothing listed, stays its own entry. A report about an issue the Chief of Staff escalated is the human's too."""
    def key(repo, number):
        return ((repo or home or "").lower(), number)
    yours = {key(item["repo"], item["ask"]) for item in items if item["kind"] == "issue" and item["tier"] == "human"}
    listed = {key(item["repo"], item["ask"]): item["title"] for item in items if item["kind"] == "issue"}
    parents = {key(item["repo"], item["ask"]): item.get("parent") for item in items if item["kind"] == "issue"}
    wanted, wanted_yours = {}, {}
    for repo, number in listed:
        wanted.setdefault(repo, set()).add(number)
        if (repo, number) in yours:
            wanted_yours.setdefault(repo, set()).add(number)
    ids = dict(zip((id(item) for item in items), ids))
    folded = {}
    for item in items:
        if item["kind"] != "report":
            continue
        repo, number = (item["repo"] or home or "").lower(), item["ask"]
        title = str(item["title"]).lower()
        if not number:
            named = named_issues(title, repo, wanted.get(repo, set()))
            number = named.pop() if len(named) == 1 else None
        if number and (repo, number) in listed:
            folded[id(item)] = (repo, number)
            continue
        if number and item.get("repo_known") is False:
            # #126: the report named no repo the switchboard knows, so its repo is a guess (its sender's). The same
            # number on exactly one listed issue decision is that issue: one question, shown once
            same = [k for k in listed if k[1] == number]
            if len(same) == 1:
                folded[id(item)] = same[0]
                continue
        mine = wanted_yours.get(repo, set())
        if number and mine:
            # the ask chain: a work item whose ask is the person's decision, or the ask of a work item that is
            up = known_parent(item["repo"] or home, number) if known_parent else None
            below = [n for n in mine if parents.get((repo, n)) == number]
            if up in mine:
                folded[id(item)] = (repo, up)
                continue
            if len(below) == 1:
                folded[id(item)] = (repo, below[0])
                continue
            # the report's own issue is no decision (a status ask, say) but its text names exactly one that is the person's
            named = named_issues(title, repo, mine)
            if len(named) == 1:
                folded[id(item)] = (repo, named.pop())
    reporters, lines = {}, {}
    for item in sorted((i for i in items if id(i) in folded), key=lambda i: (i["since"], i["seed"])):
        reporters.setdefault(folded[id(item)], []).append(item["agent"])
        lines.setdefault(folded[id(item)], []).append("%s: %s" % (item["agent"], line_of(item["title"], FOLDED_LINE_MAX)))
    decisions = []
    for item in items:
        if id(item) in folded or (item["kind"] == "issue" and item.get("closed")):   # a closed issue is never listed (#570)
            continue
        ident, tier = ids[id(item)], item["tier"]
        if item["kind"] == "report" and item["ask"] and key(item["repo"], item["ask"]) in yours:
            tier = "human"  # the Chief of Staff escalated its issue: the report that raised it is yours too
        repo = item.get("repo")
        extra = {}
        about = None
        if item["kind"] == "report" and item["ask"]:
            at = key(item["repo"], item["ask"])
            about = (listed[at] or None) if at in listed else known_title(item["repo"] or home, item["ask"])
        headline = headline_of(item, about)
        if item.get("rows"):  # a batch: one decision, one row each
            extra["rows"] = item["rows"]
            headline = batch_headline(item["title"], len(item["rows"]))
            if item.get("raised_by"):
                extra["raised_by"] = item["raised_by"]
        if item["kind"] == "issue" and item.get("waits_for"):
            extra["waits_for"] = list(item["waits_for"])   # optional: only an issue that says what it waits for carries it
        if item["kind"] == "issue" and key(item["repo"], item["ask"]) in reporters:
            extra["reported_by"] = sorted(set(reporters[key(item["repo"], item["ask"])]))
            extra["reported_lines"] = lines[key(item["repo"], item["ask"])][-FOLDED_LINES_KEPT:]  # none is lost
        if item.get("considered"):
            extra["considered"] = item["considered"]
        if item.get("request"):
            extra.update(session=item["session"], request=item["request"])
        title = line_of(item["title"], DECISION_REPORT_TITLE_MAX if item["kind"] == "report" else DECISION_TITLE_MAX)
        decisions.append(Decision(ident, title, item["ask"], repo, item["agent"], item["kind"], iso(item["since"]),
                                  "human" if tier_flat else tier, headline, extra).entry())
    return decisions


def notice_entry(repo, number, tag, facts, stale, sender, created, note, hold_minutes, default_repo=None):
    """The entry of a recorded PR notice (kind `notice`, tier `human`, id `pr:<tag>#N`): information, not a
    decision. `facts` is what `gh pr view` said (or {}), `stale` that it could not be read now, `created` the
    notice's epoch time, `hold_minutes` the merge hold (0 for none)."""
    # with a default repo configured the headline leads with `#N` (the repo's short name only when it is NOT the default), so what differs is first
    lead = "%s#%d" % (tag, number) if not default_repo else ("#%d" % number if repo == default_repo else "%s#%d" % (repo.split("/")[-1], number))
    headline = fit_lines(" ".join((lead, facts.get("title") or "")).strip())
    return {"id": "pr:%s#%d" % (tag, number), "title": line_of(headline, DECISION_TITLE_MAX),
            "headline": headline, "ask": None, "repo": repo, "agent": sender, "kind": "notice",
            "tier": "human", "since": iso(created), "number": number,
            "url": "https://github.com/%s/pull/%d" % (repo, number), "state": facts.get("state"),
            "additions": facts.get("additions"), "deletions": facts.get("deletions"),
            "files": facts.get("files"), "ci": facts.get("ci"), "review": facts.get("review"),
            "stale": stale, "note": note or None,
            "merge_after": iso(created + hold_minutes * 60) if hold_minutes > 0 else None}


def captain_entry(notice, facts, escalated, evidence):
    """A `MERGE: captain` PR as a decision (kind `captain`, same id as its notice, tier `cos`; `human` once the Chief
    of Staff escalated it for this head). `evidence` is who reviewed it at the exact head ([{"by", "kind", "state"}]:
    a formal review or a signed comment), [] when none was seen (the notice outlasted its wait), or None when that
    cannot be read: it then stands on the notice alone and says so."""
    head = facts.get("head")
    if evidence is None:
        text, review = "review not detectable: on notice alone", "not detectable"
    elif not evidence:  # a readable absence, listed because the notice has waited too long
        text, review = "no review seen at %s: on notice for over the wait limit" % head[:8], "none seen"
    else:
        text = "reviewed at %s by %s" % (head[:8], ", ".join(
            "%s (%s)" % (e["by"], e["state"] if e["kind"] == "review" else "comment") for e in evidence))
        review = "; ".join(e["state"] for e in evidence)
    entry = dict(notice, kind="captain", tier="human" if escalated else "cos", head=head, review=review,
                 reviews=evidence, review_evidence=line_of(text, 200))
    if facts.get("holds"):   # why it is held, from its `hold-*` labels (#133): shown, never read from prose
        entry["holds"] = list(facts["holds"])
    return entry


_PR_URL = re.compile(r"https?://github\.com/([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+)/pull/([0-9]+)", re.I)
_PR_SHORT = re.compile(r"(?<![\w/.:-])([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+)#([0-9]+)")


def named_prs(text):
    """The pull requests a text names: {(owner/repo lower, number)} from a GitHub pull URL or an `owner/repo#N` ref. A bare `#N` names nothing here
    (without its repo it could be any PR or issue)."""
    text = str(text or "")
    found = {(m.group(1).lower(), int(m.group(2))) for m in _PR_URL.finditer(text)}
    return found | {(m.group(1).lower(), int(m.group(2))) for m in _PR_SHORT.finditer(_PR_URL.sub(" ", text))}


def fold_reports_into_prs(decisions):
    """A report that names a `pr:` decision (kind `captain`) by its URL or `owner/repo#N` is that PR's question: it is folded into the PR's row
    (which gains `reported_by` and `reported_lines`, as an issue decision does) and has no row of its own, in whichever tier either is in. In
    place; returns `decisions`. A report that names no such PR, or names more than one, stays. Only the first
    DECISION_REPORT_TITLE_MAX characters of a report are searched (what the entry keeps)."""
    prs = {(str(d["repo"]).lower(), d["number"]): d for d in decisions
           if d.get("kind") == "captain" and d.get("repo") and isinstance(d.get("number"), int)}
    if not prs:
        return decisions
    folded, lines = set(), {}
    for d in decisions:
        if d.get("kind") != "report":
            continue
        hit = [prs[ref] for ref in named_prs(d.get("title")) if ref in prs]
        if len(hit) != 1:
            continue
        pr = hit[0]
        folded.add(id(d))
        pr["reported_by"] = sorted(set(pr.get("reported_by") or []) | {d.get("agent") or "-"})
        lines.setdefault(id(pr), list(pr.get("reported_lines") or [])).append("%s: %s" % (d.get("agent") or "-", line_of(d.get("title"), FOLDED_LINE_MAX)))
        pr["reported_lines"] = lines[id(pr)][-FOLDED_LINES_KEPT:]
    if folded:
        decisions[:] = [d for d in decisions if id(d) not in folded]
    return decisions


def order_decisions(decisions):
    """The list's order, in place: oldest first, ties by id."""
    decisions.sort(key=lambda d: (d["since"], d["id"]))
    return decisions


# ---------------------------------------------------------------- views and counts


def decisions_view(role, fleet_repo, repo=None, tier=None):
    """Which decisions a caller sees. {"role", "repo", "tiers", "text"}: `repo` None is every repo, `tiers`
    None is every tier. `fleet_repo` is the repo the caller's fleet metadata names (an orchestrator's own).

    With no flag: the Chief of Staff sees every repo and tier; an orchestrator its own repo, every tier; anyone
    else (a plain shell, a TUI that is not a fleet agent) the `human` tier, every repo. A flag overrides its own
    part and leaves the other at the caller's default."""
    if role == "chief-of-staff":
        shown, tiers = None, None
    elif role == "orchestrator":
        shown, tiers = fleet_repo, None
    else:
        shown, tiers = None, ["human"]
    if repo is not None:
        shown = repo
    if tier is not None:
        tiers = None if tier == "all" else [tier]
    who = role if role in ("chief-of-staff", "orchestrator") else "you"
    text = "%s, %s (for %s)" % ("repo %s" % shown if shown else "all repos",
                                "tier %s" % ", ".join(tiers) if tiers else "all tiers", who)
    return {"role": role, "repo": shown, "tiers": tiers, "text": text}


def select_decisions(decisions, view):
    """The decisions in `view`. One with no tier (a file an older daemon wrote) is the human's."""
    return [d for d in decisions if d.get("kind") != "notice"
            and (view["repo"] is None or d.get("repo") == view["repo"])
            and (view["tiers"] is None or d.get("tier", "human") in view["tiers"])]


def count_over_an_hour(decisions, now):
    """How many of `decisions` have waited longer than DECISION_LONG_WAIT at `now` (one with no readable time is
    not counted). The one place that decides it: the status line uses it."""
    waited = (parse_iso(d.get("since")) for d in decisions)
    return sum(1 for since in waited if since is not None and now - since > DECISION_LONG_WAIT)


# ---------------------------------------------------------------- external decision systems (pure rules)
#
# The rules for sending decisions to an external system and taking answers back. The state (links, outbox) and the
# calls are bin/decision_sync.py's; nothing here does I/O.

EXTERNAL_KINDS = ("issue", "report", "question")       # kinds that may leave the machine; a permission never does
ROLE_TIER = {"project_agent": "orchestrator", "owner_agent": "cos", "human": "human"}   # the authority of an answerer
_ROLE_WORDS = {"project agent": "project_agent", "project_agent": "project_agent", "owner's agent": "owner_agent",
               "owners agent": "owner_agent", "owner agent": "owner_agent", "owner_agent": "owner_agent",
               "human": "human"}
_KEY_SPACE = uuid.UUID("6f1c2a52-3b0e-4d4f-9a53-1d2f6c9e7a10")


def normalize_role(text):
    """`project_agent`, `owner_agent` or `human` for the role an adapter reports an answerer as, else None."""
    return _ROLE_WORDS.get(" ".join(str(text or "").lower().replace("-", " ").split()).replace("_", " ")) \
        or _ROLE_WORDS.get(str(text or "").strip().lower())


def answer_authority(role, tier):
    """(ok, why): whether an answer by `role` may settle a decision now at `tier`. An answer counts as its role's:
    the project agent's as an orchestrator's, the owner's agent's as the Chief of Staff's, the human's as the
    human's; one from a role below the decision's tier is not applied. An unknown role never is."""
    answers_as = ROLE_TIER.get(role)
    if answers_as is None:
        return False, "the answering role %r is not one this fleet knows" % (role,)
    if tier not in TIER_LADDER:
        return False, "the decision's tier %r is not one this fleet knows" % (tier,)
    if TIER_LADDER.index(answers_as) < TIER_LADDER.index(tier):
        return False, "an answer by the %s counts as the %s's; this decision waits on the %s" % (
            role.replace("_", " "), answers_as, tier)
    return True, "ok"


def decision_key(seed):
    """The stable, opaque key of a decision: from its source identity (the id seed), nothing else, so the same
    decision has the same key on every pass and after a restart. The display id is not an identity."""
    return str(uuid.uuid5(_KEY_SPACE, "decision:%s" % seed))


def idempotency_key(key, op, revision):
    """The key of one outbound operation: decision key, operation and revision, so a retry is the same write."""
    return str(uuid.uuid5(_KEY_SPACE, "%s|%s|%d" % (key, op, revision)))


def goes_outward(entry, repos, kinds=("issue", "report")):
    """Whether a listed decision is put to the external system: one of `kinds` (never a permission request, never a
    notice) in an enabled repo."""
    return (entry.get("kind") in kinds and entry.get("kind") in EXTERNAL_KINDS
            and entry.get("repo") in (repos or ()))


def outward_rows(entry):
    """The rows of a batch as said outside: WHOLE while they fit OUTWARD_ROWS_BUDGET characters of text, then a row that does not fit is sent cut and marked
    (`cut: true`, `see: "row N: see <repo>#<issue>"`), never silently."""
    where = "%s#%s" % (entry.get("repo"), entry.get("ask"))
    left, out = OUTWARD_ROWS_BUDGET, []
    whole = BATCH_BODY_MAX_CHARS
    for n, r in enumerate(entry["rows"], 1):
        finding, rec = line_of(r.get("finding"), whole), line_of(r.get("recommendation"), whole)
        row = {"n": n, "ref": line_of(r.get("ref"), 100), "finding": finding, "recommendation": rec}
        if len(finding) + len(rec) <= left:
            left -= len(finding) + len(rec)
        else:
            share = max(OUTWARD_CUT_MIN, left // 2)
            cut = lambda text: text if len(text) <= share else text[:share - 1] + "\u2026"
            row.update({"finding": cut(finding), "recommendation": cut(rec), "cut": True, "see": "row %d: see %s" % (n, where)})
            left = max(0, left - 2 * share)
        out.append(row)
    return out


def outward_payload(entry, floor):
    """What is said about a decision outside, from the list entry: text with anything shaped like a credential taken
    out, and the floor tier the mapping assigns its tier. `rows` (a batch) go as options by id."""
    payload = {"title": line_of(entry.get("title"), DECISION_REPORT_TITLE_MAX),
               "headline": line_of(entry.get("headline"), 300), "kind": entry.get("kind"),
               "display_id": entry.get("id"), "tier": entry.get("tier"), "floor": floor, "ask": entry.get("ask"),
               "repo": entry.get("repo"), "agent": entry.get("agent"), "since": entry.get("since")}
    if entry.get("rows"):
        payload["batch"] = True
        payload["rows"] = outward_rows(entry)
        payload["options"] = [{"id": "as_recommended", "label": "As recommended"}]
    return payload


def payload_digest(payload):
    """A short digest of what a decision says outside, without what changes without a new message (tier, floor,
    time): a different digest is a revision."""
    stable = {k: v for k, v in payload.items() if k not in ("tier", "floor", "since")}
    return hashlib.sha256(repr(sorted(stable.items(), key=lambda kv: kv[0])).encode("utf-8")).hexdigest()[:16]


SENT_BACK_TIMEOUT = 72 * 3600   # seconds an item sent back for clarification waits for the agent's revision, then it is withdrawn


def plan_operations(desired, links, errored_kinds=(), now=None, errored_bosses=()):
    """The outbound operations that bring `links` (what the outside system holds, projected through what is queued)
    to `desired` ({key: {"payload", "digest", "tier", "floor", "kind", "thread"}}). Pure.

    Returns a list of {"op": raise | revise | status | withdraw, "key", "revision", "rebind_from"?, "floor"?}. A key
    new to the links is a `raise`, unless a report of the same thread (repo, ask, agent) left the list as it
    arrived: then it is a `revise` of that item, rebound to the new key (a re-asked question is one item). A key
    that left the list is withdrawn, except while its source could not be read (`errored_kinds`, or for a report link `errored_bosses`: an empty read is
    not an answer). A link closed from outside is left alone. A link that was SENT BACK (`sent_back_at`: the outside
    system asked a question and the agent was told) is not withdrawn when its decision leaves the list, which the note
    itself causes for a report: the item waits for the agent's revision (the re-asked-question rule above revises it)
    and is withdrawn, with a reason, only when SENT_BACK_TIMEOUT has passed (`now` is needed to tell)."""
    ops, taken = [], set()
    for key, want in desired.items():
        link = links.get(key)
        if link is None:
            old = None
            if want["kind"] == "report" and want["thread"] is not None:
                found = [k for k, l in links.items() if k not in desired and k not in taken and l.get("kind") == "report"
                         and l.get("thread") == want["thread"] and not l.get("withdrawn") and not l.get("closed")]
                old = found[0] if len(found) == 1 else None
            if old is not None:
                taken.add(old)
                ops.append({"op": "revise", "key": key, "revision": links[old]["revision"] + 1, "rebind_from": old})
            else:
                ops.append({"op": "raise", "key": key, "revision": 1})
            continue
        if link.get("closed"):
            continue
        if link.get("withdrawn"):
            ops.append({"op": "raise", "key": key, "revision": link["revision"] + 1})
            continue
        if link.get("digest") != want["digest"]:
            ops.append({"op": "revise", "key": key, "revision": link["revision"] + 1})
        if link.get("floor") != want["floor"] or link.get("tier") != want["tier"]:
            ops.append({"op": "status", "key": key, "revision": link["revision"],
                        "seq": link.get("status_seq", 0) + 1})
    for key, link in links.items():
        if key in desired or key in taken or link.get("withdrawn") or link.get("closed"):
            continue
        if link.get("kind") in errored_kinds:
            continue
        if errored_bosses and link.get("kind") == "report" and (link.get("boss") is None or link.get("boss") in errored_bosses):
            continue  # #80: only that boss's reports wait; a link whose boss is unknown waits too (the cautious way)
        if link.get("sent_back_at") is not None:
            if now is None or now - link["sent_back_at"] < SENT_BACK_TIMEOUT:
                continue
            ops.append({"op": "withdraw", "key": key, "revision": link["revision"] + 1,
                        "reason": "no revision after clarification"})
            continue
        ops.append({"op": "withdraw", "key": key, "revision": link["revision"] + 1})
    return ops


# ---------------------------------------------------------------- ports
#
# Abstract: no implementation here. bin/fleet-switchboard holds the implementations that are today's behaviour
# (GitHub issues, harness requests, reports and PR notices as sources; a send, a harness reply and an issue
# comment with labels as deliveries; decisions.json and escalated-requests.json as the local store) and a
# registry a later adapter extends.


class DecisionSource(abc.ABC):
    """Reads the open decisions of one kind from some store (GitHub issues, a harness's pending requests, a boss's
    reports, recorded PR notices). `name` is how an error entry names the source."""

    name = ""
    # True: read_open returns finished list entries (a notice) appended after folding; False: it returns the items
    # build_entries folds and numbers.
    entries = False

    @abc.abstractmethod
    def read_open(self, now):
        """(items, problems): the open decisions this source holds at `now` (the caller's clock), as the dicts
        build_entries takes (or, for an `entries` source, finished entries), and what could not be read (one line
        each). A source that fails raises; the caller records it as an error entry and still lists the others."""


class AnswerDelivery(abc.ABC):
    """Delivers an answer to the asker (the agent that is waiting, or the issue that holds the question). The core
    decides that an answer exists and what it means; the Switchboard, which knows the harness, delivers it."""

    @abc.abstractmethod
    def deliver(self, decision, answer):
        """Deliver `answer` (a dict; its keys are the delivery's own) for `decision` (an entry of the list), once.
        Returns the delivery's result; raises when it could not be delivered, in which case nothing is claimed."""


class DecisionStore(abc.ABC):
    """Where the Switchboard keeps what it must remember about decisions: the projection a reader sees, and the
    requests the Chief of Staff escalated. Disposable and rebuildable: never the source of truth."""

    @abc.abstractmethod
    def read(self, now):
        """What the projection says now: {stale, message, age_seconds, written_at, generation, decisions, errors, provisional}."""

    @abc.abstractmethod
    def publish(self, decisions, errors, now, provisional=False):
        """Make `decisions` and `errors` the projection when they changed (otherwise only say it is alive).
        Returns True when it was written. `provisional` (the early list of a pass that is still reading) is kept with the projection until the end of the pass
        publishes the final one; a change of that flag alone is a write, and `read` says it (`provisional`)."""

    @abc.abstractmethod
    def escalated(self):
        """{"<session>:<request id>": time} of the requests escalated to the human."""

    @abc.abstractmethod
    def mark_escalated(self, key, when):
        """Remember that the request `key` was escalated."""

    @abc.abstractmethod
    def prune_escalated(self, pending):
        """Forget every escalation of a request that is not in `pending`."""


class ExternalDecisionAdapter(abc.ABC):
    """The port of a system outside the Switchboard that holds decisions and takes answers. The sync engine
    (bin/decision_sync.py) is its only caller; an implementation never needs to know the fleet.

    Every write carries an idempotency key and the same key twice is the same write. Reads are by cursor and may
    repeat an event: the caller de-duplicates by `event_id`. Errors are raised (any Exception) and the engine retries
    them with backoff, UNLESS the exception carries the attribute `permanent = True`: the other side refused that
    operation for good (an invalid body, an unknown item, "already decided"). Such an operation is dropped, audited
    and shown in the status, never retried, and never makes the engine withdraw or answer anything; a later change of
    the decision enqueues a fresh one. A rate limit or an unavailable service is not permanent."""

    capabilities = frozenset()

    @abc.abstractmethod
    def health(self):
        """None when the adapter can be used now, else one line saying why not. Cheap; called once per cycle."""

    @abc.abstractmethod
    def push(self, operation, idempotency_key):
        """Apply one outbound operation. `operation` is a dict: {"op": "raise" | "revise" | "status" | "withdraw",
        "key": the decision key, "external_id": the outside id (None for a raise), "revision": int, "decision":
        {title, headline, kind, display_id, tier, floor, ask, repo, agent, since, [batch, rows, options]},
        "floor": the floor tier name the mapping gives the tier, "note": optional text}. Returns {"external_id":
        str, "url": optional}; for anything but a raise the id is the one it was given."""

    @abc.abstractmethod
    def lookup(self, key):
        """The item already held for decision `key` (a raise whose answer was lost), as {"external_id", "revision"},
        or None. Used to rebuild the link table."""

    @abc.abstractmethod
    def pull_changes(self, cursor):
        """(events, next_cursor): what changed outside since `cursor` (None the first time), at least once. An event
        is a dict {"event_id", "external_id", "type": "answered" | "clarify" | "reversed" | "withdrawn", "answered_by":
        "project agent" | "owner's agent" | "human", "option_id", "text", "rows": optional {row number: choice},
        "at"}. An answer picks by option id; the text is the words that came with it."""
