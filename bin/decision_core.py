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
    BatchError for an empty part, a ref that names no issue, or a fence in the text; a part over its cap is cut."""
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


def row_of(ref, finding, rec):
    cut = lambda text, width: text if len(text) <= width else text[:width - 1] + "\u2026"
    finding, rec = cut(finding, BATCH_FINDING_MAX), cut(rec, BATCH_REC_MAX)
    short = short_ref_of(ref)
    return {"ref": ref, "short": short, "finding": finding, "recommendation": rec,
            "text": "%s: %s" % (short, short_refs(rec))}


def parse_decision_rows(body):
    """The rows in an issue body's fenced `decision-rows` block, in order; [] for a body with none. Lenient: a line
    that is not a row is skipped, and at most BATCH_ROWS_MAX rows are read (the daemon never refuses an issue)."""
    found = _ROWS_BLOCK.search(body) if isinstance(body, str) else None
    rows = []
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


# ---------------------------------------------------------------- folding: items in, the decision list out


FOLDED_LINES_KEPT = 3    # the newest folded report lines an issue decision keeps (`reported_lines`)
FOLDED_LINE_MAX = 160    # characters of each


def build_entries(items, ids, home, known_title, tier_flat=False):
    """The decisions (as list entries) the source `items` become, not yet ordered and without notices.

    `items` are what the sources found (dicts with kind, title, ask, repo, agent, since, seed, bare, tier, and
    optionally rows, raised_by, considered, session, request, repo_known); `ids` are their display ids, in the same order
    (assign_ids); `home` is the repo a bare issue belongs to (or None); `known_title(repo, number)` is a lookup of
    an issue's title the caller already holds, never a read (None when unknown). `tier_flat` is the Lab's
    `tier-flat` fault.

    One question, shown once: a report on an issue that is itself a decision (same repo and number) is folded into
    the issue's decision (its newest lines stay in the entry as `reported_lines`); a report whose repo is only a guess
    (`repo_known` false: no repo was named at all) folds into the one listed issue decision with its number; so is an untagged report whose text names exactly one such issue in its repo. A report
    about an issue the Chief of Staff escalated is the human's too."""
    yours = {(item["repo"] or home, item["ask"]) for item in items if item["kind"] == "issue" and item["tier"] == "human"}
    listed = {(item["repo"] or home, item["ask"]): item["title"] for item in items if item["kind"] == "issue"}
    wanted = {}
    for repo, number in listed:
        wanted.setdefault(repo, set()).add(number)
    ids = dict(zip((id(item) for item in items), ids))
    folded = {}
    for item in items:
        if item["kind"] != "report":
            continue
        repo, number = item["repo"] or home, item["ask"]
        if not number:
            named = named_issues(str(item["title"]), repo, wanted.get(repo, set()))
            number = named.pop() if len(named) == 1 else None
        if number and (repo, number) in listed:
            folded[id(item)] = (repo, number)
        elif number and item.get("repo_known") is False:
            # #126: the report named no repo the switchboard knows, so its repo is a guess (its sender's). The same
            # number on exactly one listed issue decision is that issue: one question, shown once
            same = [key for key in listed if key[1] == number]
            if len(same) == 1:
                folded[id(item)] = same[0]
    reporters, lines = {}, {}
    for item in sorted((i for i in items if id(i) in folded), key=lambda i: (i["since"], i["seed"])):
        reporters.setdefault(folded[id(item)], []).append(item["agent"])
        lines.setdefault(folded[id(item)], []).append("%s: %s" % (item["agent"], line_of(item["title"], FOLDED_LINE_MAX)))
    decisions = []
    for item in items:
        if id(item) in folded:
            continue
        ident, tier = ids[id(item)], item["tier"]
        if item["kind"] == "report" and item["ask"] and (item["repo"] or home, item["ask"]) in yours:
            tier = "human"  # the Chief of Staff escalated its issue: the report that raised it is yours too
        repo = item.get("repo")
        extra = {}
        about = None
        if item["kind"] == "report" and item["ask"]:
            key = (item["repo"] or home, item["ask"])
            about = (listed[key] or None) if key in listed else known_title(*key)
        headline = headline_of(item, about)
        if item.get("rows"):  # a batch: one decision, one row each
            extra["rows"] = item["rows"]
            headline = batch_headline(item["title"], len(item["rows"]))
            if item.get("raised_by"):
                extra["raised_by"] = item["raised_by"]
        if item["kind"] == "issue" and (item["repo"] or home, item["ask"]) in reporters:
            extra["reported_by"] = sorted(set(reporters[(item["repo"] or home, item["ask"])]))
            extra["reported_lines"] = lines[(item["repo"] or home, item["ask"])][-FOLDED_LINES_KEPT:]  # none is lost
        if item.get("considered"):
            extra["considered"] = item["considered"]
        if item.get("request"):
            extra.update(session=item["session"], request=item["request"])
        title = line_of(item["title"], DECISION_REPORT_TITLE_MAX if item["kind"] == "report" else DECISION_TITLE_MAX)
        decisions.append(Decision(ident, title, item["ask"], repo, item["agent"], item["kind"], iso(item["since"]),
                                  "human" if tier_flat else tier, headline, extra).entry())
    return decisions


def notice_entry(repo, number, tag, facts, stale, sender, created, note, hold_minutes):
    """The entry of a recorded PR notice (kind `notice`, tier `human`, id `pr:<tag>#N`): information, not a
    decision. `facts` is what `gh pr view` said (or {}), `stale` that it could not be read now, `created` the
    notice's epoch time, `hold_minutes` the merge hold (0 for none)."""
    headline = fit_lines(" ".join(("%s#%d" % (tag, number), facts.get("title") or "")).strip())
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
    return dict(notice, kind="captain", tier="human" if escalated else "cos", head=head, review=review,
                reviews=evidence, review_evidence=line_of(text, 200))


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


def outward_payload(entry, floor):
    """What is said about a decision outside, from the list entry: text with anything shaped like a credential taken
    out, and the floor tier the mapping assigns its tier. `rows` (a batch) go as options by id."""
    payload = {"title": line_of(entry.get("title"), DECISION_REPORT_TITLE_MAX),
               "headline": line_of(entry.get("headline"), 300), "kind": entry.get("kind"),
               "display_id": entry.get("id"), "tier": entry.get("tier"), "floor": floor, "ask": entry.get("ask"),
               "repo": entry.get("repo"), "agent": entry.get("agent"), "since": entry.get("since")}
    if entry.get("rows"):
        payload["batch"] = True
        payload["rows"] = [{"n": n, "ref": line_of(r.get("ref"), 100), "finding": line_of(r.get("finding"), 200),
                            "recommendation": line_of(r.get("recommendation"), 120)}
                           for n, r in enumerate(entry["rows"], 1)]
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
        """What the projection says now: {stale, message, age_seconds, written_at, generation, decisions, errors}."""

    @abc.abstractmethod
    def publish(self, decisions, errors, now):
        """Make `decisions` and `errors` the projection when they changed (otherwise only say it is alive).
        Returns True when it was written."""

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
