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

KINDS = ("issue", "permission", "question", "report", "notice")
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
    worker has one) after the report. `send_lines` are the worker's synthetic lines, as
    [(created, message id, line number, line)]."""
    for created, _, _, line in send_lines:
        match = SEND_LINE.match(line)
        if match and match.group(1) == report["boss"] and created > report["at"][0] \
                and (match.group(3) is None or (int(match.group(3)) == report["issue"]
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


def build_entries(items, ids, home, known_title, tier_flat=False):
    """The decisions (as list entries) the source `items` become, not yet ordered and without notices.

    `items` are what the sources found (dicts with kind, title, ask, repo, agent, since, seed, bare, tier, and
    optionally rows, raised_by, considered, session, request); `ids` are their display ids, in the same order
    (assign_ids); `home` is the repo a bare issue belongs to (or None); `known_title(repo, number)` is a lookup of
    an issue's title the caller already holds, never a read (None when unknown). `tier_flat` is the Lab's
    `tier-flat` fault.

    One question, shown once: a report on an issue that is itself a decision (same repo and number) is folded into
    the issue's decision; so is an untagged report whose text names exactly one such issue in its repo. A report
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
    reporters = {}
    for item in items:
        if id(item) in folded:
            reporters.setdefault(folded[id(item)], []).append(item["agent"])
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


# ---------------------------------------------------------------- ports (no implementation yet; used from step 3)


class DecisionSource(abc.ABC):
    """Reads the open decisions of one kind from some store (GitHub issues, a harness's pending requests, a boss's
    reports). The Switchboard's three readers become implementations of this port; an external store may add one."""

    @abc.abstractmethod
    def read_open(self, now):
        """(items, problems): the open decisions this source holds at `now` (the caller's clock), as the dicts
        build_entries takes, and what could not be read (one line each). A source that fails raises; the
        caller records it as an error entry and still lists the other sources."""


class AnswerDelivery(abc.ABC):
    """Delivers an answer to the asker (the agent that is waiting). The module decides that an answer exists and
    what it means; the Switchboard, which knows the harness, is the one that delivers it."""

    @abc.abstractmethod
    def deliver(self, decision, answer):
        """Deliver `answer` (a dict: choice or text, who answered with what role, when) for `decision` (an entry
        of the list) to the agent it waits on, once. Returns a result dict; raises when it could not be delivered,
        in which case the decision stays open."""


class DecisionStore(abc.ABC):
    """The hook point for an adapter that holds decisions outside the Switchboard and takes answers from there.
    Optional capabilities are named in `capabilities`; a missing one is reported, not guessed."""

    capabilities = frozenset()

    @abc.abstractmethod
    def health(self):
        """None when the store can be used now, else one line saying why not."""

    @abc.abstractmethod
    def push(self, decision, idempotency_key):
        """Record or update `decision` in the store; the same key twice is the same write. Returns the store's
        own id for it (and any link)."""

    @abc.abstractmethod
    def pull_changes(self, cursor):
        """(events, next_cursor): what changed in the store since `cursor` (answers, withdrawals), at least once;
        the caller de-duplicates by event id."""
