# ---- #117: the "waiting on the person" sweep. Deterministic (no model), over what the hub can read in a few bounded,
# cached list reads per repo. S1 a decision section and the awaiting label disagree; S2 a PR held for the person that is
# not a `MERGE: captain` PR; S3 something said to wait on the person that is not in the human tier; S4 a decision whose
# issue or PR is closed (withdrawn); S5 a cos-tier decision that has aged. It never edits GitHub and never changes a tier.

SWEEP_DEFAULTS = {"enabled": False, "aged_cos_hours": 2, "comment_window_days": 7, "max_findings": 20,
                  "realarm_hours": 6, "person_names": ["the owner"], "calls_per_pass": 12}
SWEEP_S6A_GRACE = 900         # a PR younger than this is not yet "marker without label": the label is applied after it opens
SWEEP_S1A_GRACE = 600         # a decision section whose issue changed this recently may be mid-escalation (one label off, the next not yet on)
SWEEP_TTL = 900               # seconds a repo's lists are kept before they are read again
SWEEP_CALLS_PER_PASS = SWEEP_DEFAULTS["calls_per_pass"]   # default of `sweep.calls_per_pass`: the most `gh` calls one pass makes
SWEEP_CALLS_CAP = 100
SWEEP_CONFIRM_RESERVE = 2     # of them, kept for the single-issue reads (is this decision's issue closed?)
SWEEP_PASS_SECONDS = 8        # no new read is started once the sweep has spent this long in a pass
SWEEP_LIST_PAGES = 2          # pages one list read follows (200 records); more is shown as capped
SWEEP_PR_COMMENTS = 5         # the last comments of an item that are searched for the phrases
SWEEP_FILE = "sweep.json"
SWEEP_READY_FILE = "sweep-ready.json"   # #986: per held pull request, the head and the moment it first became ready for the owner's decision (S5 counts the wait from it)
SWEEP_READY_READS = 10   # held pull requests whose gates one pass may read; the rest are counted as not checked and read first on the next pass
SWEEP_READY_READ_COST = 7   # least calls of the shared pass budget one gate reading is charged: a ready reading makes at least 7 GETs (PR view 1, plan issue + 1 comments page 2, floor view 1 + narrowed agent reads, clear_who 3), a non-ready one at least 4; the calls a reading REALLY makes are counted (engine.reading_calls) and the excess over this is charged too
SWEEP_ALARMED_FILE = "sweep-alarmed.json"   # #805: when each finding was last raised, so the re-alarm window survives a restart
SWEEP_KINDS = {"S6a": "first-line marker without the merge-captain label (our PR)", "S6b": "merge label and first-line marker disagree",
               "S1a": "decision section without an awaiting label", "S1b": "awaiting label without a decision section",
               "S2": "held for the person but not marked as a captain PR (no merge-captain label)", "S3": "said to wait on the person, not in their tier",
               "S5": "cos-tier decision aged (and not already in the owner's tier); a held pull request only once it is ready for the owner's decision and has waited since then",
               "S7": "decisions that wait for each other (a cycle): none of them can be done first",
               "S8": "a decision waits for an issue that is closed or missing",
               "S9": "a merge or hold label left on a closed, unmerged PR",
               "S10": "a charter recorded by two orchestrators in the registry (the lane of its pull requests is unknown)"}
SWEEP_WAIT_VERBS = ("held for", "hold for", "waiting on", "waits on", "awaiting", "blocked on", "pending", "needs")


def validate_sweep_config(config, source):
    """`sweep`: absent, null or {} runs in shadow (findings are audited, nobody is woken, nothing is withdrawn) until
    `enabled` is true; an absent key adds nothing to the config. `false` turns the sweep fully off (no reads, no writes)."""
    if "sweep" not in config:
        return
    raw = config["sweep"]
    if raw is False:
        return
    if raw is None:
        config["sweep"] = dict(SWEEP_DEFAULTS, person_names=list(SWEEP_DEFAULTS["person_names"]))
        return
    if not isinstance(raw, dict):
        raise ConfigError("%s: 'sweep' must be an object, got %r" % (source, type(raw).__name__))
    unknown = sorted(set(raw) - set(SWEEP_DEFAULTS))
    if unknown:
        warn_unknown_config("%s: 'sweep' has key(s) this tool does not read: %s (known: %s)"
                          % (source, ", ".join(unknown), ", ".join(SWEEP_DEFAULTS)), 'sweep', unknown, list(SWEEP_DEFAULTS))
    section = dict(SWEEP_DEFAULTS, **raw)
    where = "%s: 'sweep" % source
    if not isinstance(section["enabled"], bool):
        raise ConfigError("%s.enabled' must be true or false, got %r" % (where, section["enabled"]))
    for key in ("aged_cos_hours", "comment_window_days", "realarm_hours"):
        if not _is_number(section[key]) or section[key] <= 0:
            raise ConfigError("%s.%s' must be a positive number, got %r" % (where, key, section[key]))
    cap = section["max_findings"]
    if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= 200:
        raise ConfigError("%s.max_findings' must be a whole number from 1 to 200, got %r" % (where, cap))
    calls = section["calls_per_pass"]
    if not isinstance(calls, int) or isinstance(calls, bool) or not 4 <= calls <= SWEEP_CALLS_CAP:
        raise ConfigError("%s.calls_per_pass' must be a whole number from 4 to %d, got %r" % (where, SWEEP_CALLS_CAP, calls))
    names = section["person_names"]
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and re.fullmatch(r"[\w@ .-]{1,40}", n)
                                                           for n in names):
        raise ConfigError("%s.person_names' must be a non-empty list of names, got %r" % (where, names))
    section["person_names"] = list(names)
    config["sweep"] = section


def sweep_phrase_re(names):
    """The phrases that say an item waits on the person: held for / hold for / waiting on / waits on / awaiting /
    blocked on / pending / needs <name>, `<name> to review|approve|decide|merge|answer`, and `for <name>'s review`.
    Whole phrases, case-insensitive; a curly apostrophe is the straight one. `names` may hold a handle (`@login`)."""
    who = "|".join(re.escape(n) for n in names)
    verbs = "|".join(re.escape(v) for v in SWEEP_WAIT_VERBS)
    return re.compile(r"(?<![\w])(?:(?:%s)\s+(?:%s)|(?:%s)\s+to\s+(?:review|approve|decide|merge|answer)"
                      r"|for\s+(?:%s)'s\s+review)(?![\w])" % (verbs, who, who, who), re.I)


SWEEP_STRONG_VERBS = ("waiting on", "waits on", "held for")
SWEEP_STRONG_NEEDS = ("decision", "yes", "answer", "approval", "review", "ok")
SWEEP_HEDGE_RE = re.compile(r"\b(?:if|would|may|proposal|proposals|option|options|could|says|saying|said"
                            r"|when|once|before|after|later|deferred|parked|not started)\b", re.I)   # the later words name a FUTURE gate
SWEEP_FUTURE_AFTER_RE = re.compile(r"\b(?:deferred|parked|not started|when (?:picked up|a quiet window))\b", re.I)


def sweep_strong_re(names):
    """The strong forms only (S3): waiting on / waits on / held for <name>, needs <name>'s decision|yes|answer|approval|
    review|OK, for <name>'s review. Whole phrases, any case."""
    who = "|".join(re.escape(n) for n in names)
    verbs = "|".join(re.escape(v) for v in SWEEP_STRONG_VERBS)
    needs = "|".join(SWEEP_STRONG_NEEDS)
    return re.compile(r"(?<![\w])(?:(?:%s)\s+(?:%s)|needs\s+(?:%s)'s\s+(?:%s)|for\s+(?:%s)'s\s+review)(?![\w])"
                      % (verbs, who, who, needs, who), re.I)


SWEEP_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+|\n")
SWEEP_REF_RE = re.compile(r"(?<![\w/&#])(?:([\w.-]+/[\w.-]+)|([\w.-]+))?#(\d+)\b")


def sweep_strong_statement(strong, text, default_repo, repos, matched=None):
    """(fires, refs): whether `text` states, in a strong form that no hedge or negation cancels, that something waits on the
    person, and the refs `[(repo, number)]` its sentence(s) NAME (`#N`, `repo#N`, `owner/repo#N`; a bare `#N` is in
    `default_repo`, a short `repo#N` among `repos`). A sentence ends at `.`, `!` or `?` followed by a space, or a line end,
    so `v3.3, #9101` stays one sentence. No refs means the statement names nothing by itself."""
    if not isinstance(text, str):
        return False, []
    text = SWEEP_QUOTED_RE.sub(" ", text.replace("\u2019", "'"))
    fires, refs = False, []
    for match in strong.finditer(text):
        before = [m.end() for m in SWEEP_BOUNDARY_RE.finditer(text, 0, match.start())]
        start = before[-1] if before else 0
        after = SWEEP_BOUNDARY_RE.search(text, match.end())
        sentence_end = after.start() if after else len(text)
        if SWEEP_HEDGE_RE.search(text[start:match.start()]) or SWEEP_NEGATION_RE.search(
                text[max(0, match.start() - 30):match.start()]) or SWEEP_FUTURE_AFTER_RE.search(text[match.end():sentence_end]):
            continue
        fires = True
        if matched is not None:
            matched[:] = [_one_line(match.group(0), 80)]   # the phrase that fired, for the audit
        for full, short, number in SWEEP_REF_RE.findall(text[start:sentence_end]):
            repo = full or ((decision_core.repo_of_tag(short, repos) or short) if short else default_repo)  # unresolved: kept as named
            if repo and (repo, int(number)) not in refs:
                refs.append((repo, int(number)))
    return fires, refs


def sweep_says_waiting_strong(strong, text):
    """A strong form, outside code and quotes, not negated, in a sentence with no conditional or descriptive marker before it
    (if, would, may, could, proposal, option(s), and reported speech: says, saying, said): a statement that it waits, not a description of a plan."""
    if not isinstance(text, str):
        return False
    text = SWEEP_QUOTED_RE.sub(" ", text.replace("\u2019", "'"))
    for match in strong.finditer(text):
        start = max(text.rfind(c, 0, match.start()) for c in ".!?\n") + 1
        clause = text[start:match.start()]  # only what comes BEFORE the phrase hedges it: "waiting on the owner, if you agree" fires
        end = [i for i in (text.find(c, match.end()) for c in ".!?\n") if i >= 0]
        rest = text[match.end():min(end) if end else len(text)]   # a plain "deferred" / "parked" after it also makes it not-now
        if (SWEEP_HEDGE_RE.search(clause) or SWEEP_NEGATION_RE.search(text[max(0, match.start() - 30):match.start()])
                or SWEEP_FUTURE_AFTER_RE.search(rest)):
            continue
        return True
    return False


SWEEP_NEGATION_RE = re.compile(r"\b(?:not|no longer|never|isn't|aren't|wasn't|don't|doesn't|didn't|nobody|without|was|were|had been|previously|formerly)\s+"
                               r"(?:\w+\s+){0,2}$", re.I)
SWEEP_QUOTED_RE = re.compile(r"(?s:```.*?```)|`[^`\n]*`|^[ \t]*>.*$", re.M)


def sweep_says_waiting(regex, text):
    """True when the text, outside code blocks, inline code and quoted lines, says an item waits on the person and does
    not negate it ("not waiting on the owner", "no longer needs the owner"). Quoted text is somebody else's sentence."""
    if not isinstance(text, str):
        return False
    text = SWEEP_QUOTED_RE.sub(" ", text.replace("\u2019", "'"))
    for match in regex.finditer(text):
        if not SWEEP_NEGATION_RE.search(text[max(0, match.start() - 30):match.start()]):
            return True
    return False


DECISION_SECTION_RE = re.compile(r"^##[ \t]+decision required(?![A-Za-z0-9_])", re.I | re.M)


def sweep_has_heading(body):
    """True when a line of the body STARTS with a level-2 `## Decision required` heading (any case, CRLF-safe) followed by
    the end of the line or any suffix that does not continue the word: `## Decision required (re-verified 2026-10-09)`,
    `## Decision required: pick`, `## Decision required - x`. Not a heading that merely contains the words
    (`## Resolved (was: Decision required)`), not a deeper heading (`###`), not an inline mention, not code or a quote."""
    if not isinstance(body, str):
        return False
    body = body.replace("\r\n", "\n").replace("\r", "\n")  # text typed in the web editor keeps CRLF
    return bool(DECISION_SECTION_RE.search(SWEEP_QUOTED_RE.sub(" ", body)))


def sweep_has_section(body):
    """The heading, or a decision-rows block (a batch issue's decision). S1b (a label with no section) takes either; S1a
    (a section with no label) takes the heading only: a resolved batch keeps its rows block."""
    return sweep_has_heading(body) or (isinstance(body, str) and bool(parse_decision_rows(body)))


class SweepState:
    """Memory only. Everything here is rebuilt from GitHub and the decision list; losing it costs one re-read and, at
    worst, one repeated (and key-deduplicated) wake."""

    def __init__(self):
        self.repos = {}          # repo -> {"at", "items": {number: {...}}, "truncated", "error"}
        self.checked = {}        # (repo, number) -> (read time, closed)
        self.alarmed = {}        # (kind, ref) -> time cos was last told
        self.withdrawn = set()   # refs already audited as withdrawn
        self.logged = set()      # (kind, ref, day): findings already audited today
        self.woken_logged = set()  # (kind, ref, day): of those, the ones audited as woken (a later wake the same day is audited once more)
        self.facts = {}          # key -> (summary, created): the pending cos facts, until they age out
        self.deferred_since = {}  # repo or "*" -> when S3 was first deferred for it
        self.confirmed = {}      # (kind, ref) -> when a fresh read last bore the finding out
        self.attempted = {}      # (kind, ref) -> when it was last tried (the oldest is tried first)
        self.sources = {}        # (kind, ref) -> what raised it, for the audit line
        self.current = {}        # the last run's counts
        self.strong = sweep_strong_re(SWEEP_DEFAULTS["person_names"])
        self.started = 0         # when this pass's sweep began (the time budget)
        self.ready = {}          # "repo#n" -> {"head", "at"}: the first moment the pull request was ready at that head (#986)
        self.ready_loaded = False
        self.ready_read = {}     # "repo#n" -> the order of its last gate reading (the least recently read goes first)
        self.read_seq = 0
        self.s5 = {}             # decision id -> (head, ready time, who clears) for each held pull request that has waited past the threshold
        self.not_checked_since = {}   # "repo#n" -> when it was first not checked (unreadable or skipped), until it is read
        self.s5_oldest = 0       # seconds the longest not-checked held pull request has gone unread
        self.s5_skipped = {"draft": 0, "parked": 0}
        self.s5_unchecked = 0    # held pull requests whose gates could not be read this pass: never raised (#986)
        self.details = {}        # (kind, ref) -> the finding's own sentence


def sweep_alarmed_load(engine, state, now, cfg):
    """Once per process: the raise times of the last run, within the re-alarm window (older ones are forgotten)."""
    if getattr(state, "alarmed_loaded", False) or engine.state_dir is None:
        return
    state.alarmed_loaded = True
    for key, record in list(_read_small(engine.state_dir, SWEEP_ALARMED_FILE).items())[:5000]:   # only this code writes it: a bound anyway
        kind, _, ref = key.partition(" ")
        at = record.get("at")
        if kind in SWEEP_KINDS and isinstance(at, (int, float)) and not isinstance(at, bool) and now - at < cfg["realarm_hours"] * 3600:
            state.alarmed.setdefault((kind, ref), at)
            state.logged.add((kind, ref, int(now // 86400)))   # raised inside the window: a restart does not log it again


def sweep_alarmed_save(engine, state, now, cfg):
    if engine.state_dir is None:
        return
    keep = {"%s %s" % pair: {"at": at} for pair, at in sorted(state.alarmed.items()) if now - at < cfg["realarm_hours"] * 3600}
    try:
        _write_small(engine.state_dir, SWEEP_ALARMED_FILE, keep)
    except OSError:
        pass   # a disposable file: a failed write costs one repeated finding after a restart


def sweep_state(engine):
    state = getattr(engine, "sweep", None)
    if state is None:
        state = engine.sweep = SweepState()
    return state


def sweep_pages_used(records, truncated):
    return SWEEP_LIST_PAGES if truncated else max(1, -(-len(records) // GITHUB_PAGE_SIZE))


def sweep_read_repo(hub, repo, now, window_days, regex, budget, strong):
    """One repo's open issues and PRs (one bounded list read: bodies and labels come with it) and the recent
    comments (one bounded list read, newest first). Only reduced facts are kept: no text survives. `budget` is a
    one-item list: the calls this pass may still make; each read is charged what it used."""
    since = iso(now - window_days * 86400)
    items, truncated = {}, False
    records, cut = hub.read_pages("repos/%s/issues?state=open&per_page=100" % repo, cap=SWEEP_LIST_PAGES)
    budget[0] -= sweep_pages_used(records, cut)
    truncated = truncated or cut
    for record in records:
        number = _number(record)
        if number is None:
            continue
        names = {(l.get("name") if isinstance(l, dict) else l) for l in record.get("labels") or []}
        body = record.get("body")
        items[number] = {"pr": _is_pr(record), "labels": names,
                         "section": sweep_has_section(body), "heading": sweep_has_heading(body),
                         "captain": is_captain({"labels": names, "body": body}),
                         "captain_findings": pr_marker_state(names, body)["findings"], "holds": pr_hold_reasons(names), "marker": pr_marker(body),
                         "author": (pr_login(record.get("user")) or "").lower(),
                         "created": parse_iso(record.get("created_at")), "updated": parse_iso(record.get("updated_at")),
                         "assignees": [(pr_login(a) or "").lower() for a in record.get("assignees") or [] if isinstance(a, dict)],
                         "draft": _is_pr(record) and record.get("draft") is True,   # S5: a draft is a paused change
                         "parked_until": parked_until(body, names),   # S5: the one reader of the parked date
                         "closes": [r for r in pr_body_refs(body) if r[0] in (None, repo)] if _is_pr(record) else [],
                         "body_waits": sweep_says_waiting(regex, body), "comments": [], "comment_ids": [],
                         "latest_strong": False, "latest_refs": [], "latest_source": None}
    closed_holds = {}   # S9: the most recent closed (or merged) PRs that still carry a merge or hold label
    recent, cut = hub.read_pages("repos/%s/issues?state=closed&sort=updated&direction=desc&per_page=50" % repo, cap=1)
    budget[0] -= sweep_pages_used(recent, cut)
    for record in recent:
        number = _number(record)
        names = {(l.get("name") if isinstance(l, dict) else l) for l in record.get("labels") or []}
        if number is not None and _is_pr(record) and not _issue_pr_merged(record) and _label_kind(names, CAPTAIN_MERGE_LABELS[:1] + CAPTAIN_HOLD_LABELS):   # #559: a merged PR is the daemon's to clean, never a finding
            closed_holds[number] = sorted(_label_kind(names, CAPTAIN_MERGE_LABELS[:1] + CAPTAIN_HOLD_LABELS))
    comments, cut = hub.read_pages("repos/%s/issues/comments?since=%s&sort=created&direction=desc&per_page=100"
                                   % (repo, since), older=("created_at", since), cap=SWEEP_LIST_PAGES)
    budget[0] -= sweep_pages_used(comments, cut)
    truncated = truncated or cut
    for comment in reversed(comments):  # oldest first, so the last N are the newest
        number = _url_number(comment.get("issue_url"))
        if number in items and isinstance(comment.get("created_at"), str) and comment["created_at"] >= since:
            items[number]["comments"].append(sweep_says_waiting(regex, comment.get("body")))
            items[number]["comment_ids"].append(comment.get("id"))
            matched = []
            fires, refs = sweep_strong_statement(strong, comment.get("body"), repo, [repo], matched)  # the last one wins
            items[number]["latest_strong"], items[number]["latest_refs"] = fires, refs
            items[number]["latest_source"] = {"source": "comment:%s" % comment.get("id"), "match": matched[0] if matched else ""} if fires else None
    return {"at": now, "items": items, "truncated": truncated, "error": None, "closed_holds": closed_holds}


def sweep_refresh(engine, state, repos, now, cfg, regex, budget):
    """Read stale repos, the least recently read first, while the pass's call budget (less the reserve for the
    single-issue reads) allows a whole repo (its two lists, up to SWEEP_LIST_PAGES pages each). Oldest first means
    every repo is reached in turn: none starves. A failed read keeps the old picture and is retried after SWEEP_TTL."""
    hub = getattr(engine, "hub", None)
    if hub is None:
        return
    stale = sorted((r for r in repos if now - state.repos.get(r, {"at": 0})["at"] >= SWEEP_TTL),
                   key=lambda r: (state.repos.get(r, {"at": 0})["at"], r))
    for repo in stale:
        if budget[0] - SWEEP_CONFIRM_RESERVE < 2 or engine.now() - state.started >= SWEEP_PASS_SECONDS:
            break
        try:
            state.repos[repo] = sweep_read_repo(hub, repo, now, cfg["comment_window_days"], regex, budget,
                                                state.strong)
        except GitHubUnavailable as err:
            budget[0] -= 1
            old = state.repos.get(repo) or {"items": {}, "truncated": False}
            state.repos[repo] = dict(old, at=now, error=_one_line(str(err), 160))


def sweep_ref_of(decision, home):
    number = decision.get("number") if isinstance(decision.get("number"), int) else decision.get("ask")
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    return (decision.get("repo") or home, number)


_DECISION_ID_REF = re.compile(r"^pr:([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)#(\d+)$")


def sweep_ref_keys(decision, home, repos):
    """The set of (repo lower-case, number) a decision is about: its repo and ask or number, and the ref its id spells (`pr:owner/repo#N`
    or `pr:repo#N`, the short name resolved among `repos` when exactly one matches). Repos are compared without case."""
    keys = set()
    ref = sweep_ref_of(decision, home)
    if ref and ref[0]:
        keys.add((ref[0].lower(), ref[1]))
    found = _DECISION_ID_REF.match(str(decision.get("id") or ""))
    if found:
        tag, number = found.group(1).lower(), int(found.group(2))
        if "/" in tag:
            keys.add((tag, number))
        else:
            named = [r.lower() for r in repos if r.lower().split("/")[-1] == tag]
            if len(named) == 1:
                keys.add((named[0], number))
    return keys


def sweep_withdraw(engine, state, decisions, home, now, cfg, budget):
    """(kept decisions, refs found stale): a decision whose issue or PR is closed or merged is stale. Closed state is read
    one issue at a time, cached SWEEP_TTL, within the pass's call budget (the least recently read first)."""
    hub = getattr(engine, "hub", None)
    candidates = {}
    for d in decisions:
        ref = sweep_ref_of(d, home)
        if d.get("kind") in ("issue", "captain") and ref and ref[0]:  # a report's question may outlive its closed ask
            candidates.setdefault(ref, []).append(d)
    if hub is not None:
        due = sorted((r for r in candidates if now - state.checked.get(r, (0, False))[0] >= SWEEP_TTL),
                     key=lambda r: (state.checked.get(r, (0, False))[0], r))
        for ref in due[:max(0, budget[0])]:
            if engine.now() - state.started >= SWEEP_PASS_SECONDS:
                break
            budget[0] -= 1
            try:
                data = hub._read_one(ref[0], "issues/%d" % ref[1], now, fresh=True)
            except GitHubUnavailable:
                state.checked[ref] = (now, state.checked.get(ref, (0, False))[1])
                continue
            state.checked[ref] = (now, isinstance(data, dict) and data.get("state") == "closed")
    stale = sorted(r for r in candidates if state.checked.get(r, (0, False))[1])
    drop = {id(d) for r in stale for d in candidates[r]}
    return [d for d in decisions if id(d) not in drop], stale


def sweep_ours(engine, repo, number, author, assignees, notices=None):
    """Whether a PR is OURS (a fleet agent's), by the rule the triage uses to tell ours from other-owner items: its author or an
    assignee is one of `our_logins` (the triage's key, else the `gh_users` accounts and `owner_logins`), or an agent of ours recorded a
    notice for it. Another team's PR is not ours: the sweep only flags what our own fleet should have labelled."""
    tcfg = engine.config.get("triage")
    mine = triage_our_logins(engine, tcfg if isinstance(tcfg, dict) else {})
    if notices is None:
        try:
            notices = {(n["repo"], n["number"]) for n in read_pr_notices(engine.state_dir)}
        except (NoticeError, OSError, TypeError):
            notices = set()
    return (author in mine or any(a in mine for a in assignees or []) or (repo, number) in notices)


def sweep_within(when, now, seconds):
    """True when `when` (epoch seconds, or None when unknown) is less than `seconds` before `now`: the grace is only ever given on
    a time that is known."""
    return isinstance(when, (int, float)) and 0 <= now - when < seconds


def sweep_held_pr(decision, home):
    """(repo, number) when a cos-tier decision is a held pull request (a captain row about a pull request), else None (#986)."""
    ref = sweep_ref_of(decision, home)
    if decision.get("kind") == "captain" and decision.get("tier") == "cos" and ref and ref[0]:
        return ref
    return None


def sweep_gate_reading(engine, repo, number, now):
    """(open gates, head, who clears it, None) read the way `gates` reads a pull request, or (None, None, None, why) when it cannot be read. GETs only."""
    original = getattr(engine, "gh_runner", None)
    calls = [0]
    try:
        real = original or subprocess.run

        def runner(*a, **k):   # every call a reading makes is counted, and charged against the shared budget
            calls[0] += 1
            return real(*a, **k)
        engine.gh_runner = runner
        ns = types.SimpleNamespace(pr="%s#%d" % (repo, number), plan=None, ask=None, head=None)
        ctx = _ledger_context(ns, runner, lambda: now, False, parts=(engine, engine.state_dir, engine.config), floor=True)
        if ctx.unreadable or not (ctx.facts or {}).get("head"):
            return None, None, None, _one_line(str(ctx.unreadable or "no head"), 120)
        if ctx.plan_problem:   # #986 round 5: a plan that could not be read is not "no plan" (G0a would say missing): not checked
            return None, None, None, _one_line("the plan could not be read: %s" % ctx.plan_problem, 120)
        if ctx.problem:   # a corrupt ledger makes G1 untrustworthy: not checked
            return None, None, None, _one_line("the ledger could not be read: %s" % ctx.problem, 120)
        if ((ctx.facts or {}).get("carries") or {}).get("unreadable"):   # the dependency named by CARRIES: could not be read: not checked
            return None, None, None, "the carried dependency could not be read"
        if (ctx.facts or {}).get("body_unreadable"):   # a body that is not text: G5 and the held-body check would say missing
            return None, None, None, "the body could not be read"
        report = plan_ledger.evaluate_gates(ctx.plan, ctx.entries, ctx.problem, ctx.facts if ctx.facts.get("head") or ctx.facts.get("body") else None, ctx.pr, now=now)
        floor = (ctx.facts or {}).get("floor")
        if floor is not None and not isinstance(floor, dict):   # a floor of the wrong type is not a reading
            return None, None, None, "the impact floor has an unexpected shape"
        if isinstance(floor, dict) and floor.get("decision") is None:   # #986 round 4: an impact floor that could not be read is not a whole reading (G6 would say "waiting"): not checked
            return None, None, None, _one_line("the impact floor could not be read: %s" % (floor.get("unreadable") or "no decision"), 120)
        if isinstance(floor, dict) and floor.get("unreadable"):   # the re-hold read failed too (impact-state file corrupt: rehold None): not checked
            return None, None, None, _one_line("the impact state could not be read: %s" % floor["unreadable"], 120)
        opened = [r["gate"] for r in report["gates"] if r["status"] not in plan_ledger.SATISFIED]   # the FULL board, whichever gates are enforced: S5 means every gate but the owner's clear is met
        if (ctx.facts or {}).get("draft") is True:
            opened.insert(0, "draft")   # a draft is not ready, whatever CI says
        who = None
        if opened == ["G6"]:
            try:
                line = load_module("delegated_clear").clear_line(clear_who(engine, runner, repo, number, report))
            except Exception:
                line = None
            who = line or "who can clear it: not known"
        return opened, ctx.facts["head"], who, None
    except Exception as err:
        return None, None, None, _one_line("%s: %s" % (type(err).__name__, err), 120)
    finally:
        engine.gh_runner = original
        engine.reading_calls = calls[0]


def sweep_ready_pass(engine, state, decisions, home, now, cfg, decisions_whole=True, budget=None):
    """#986: for each held pull request in the cos tier (and not already the owner's), read its gates and keep when it first became ready at its
    current head. A new head or an open gate drops the moment. Fills `state.s5` with the ones that have waited past the threshold; a gate read that
    fails is counted in `state.s5_unchecked` and raises nothing. Reads GitHub only (GET).
    Fairness: the held pull requests are read least recently read first (`state.ready_read`), so one the cap, the shared call budget or the
    pass time left unread is first in line on the next pass: none stays hidden. The moment kept is when the sweep first OBSERVED the pull
    request ready, not when it became ready: after a restart without the state file the wait restarts (a pull request not read on a pass keeps its
    moment, the wait does not restart), so a finding can be late, never early. The pass draws at most half of the call budget left
    (at least one reading when the budget allows two), the rest is for verifying the findings. The read order is in memory only. A clock that goes backwards makes now - start negative: nothing is raised until it catches up."""
    state.s5, state.s5_unchecked, state.s5_oldest = {}, 0, 0
    state.s5_skipped = {"draft": 0, "parked": 0}   # the ONE counter of a sweep: reset here (this pass runs first), added to by sweep_findings
    if engine.state_dir is not None and not state.ready_loaded:
        state.ready_loaded = True
        for key, row in list(_read_small(engine.state_dir, SWEEP_READY_FILE).items())[:2000]:
            if not isinstance(key, str) or not isinstance(row, dict):   # a corrupt file: skip the row, never fail the pass
                continue
            if isinstance(row.get("head"), str) and isinstance(row.get("at"), (int, float)) and not isinstance(row.get("at"), bool):
                state.ready.setdefault(key, {"head": row["head"], "at": row["at"]})
    repos_known = list(state.repos) + ([home] if home else [])
    human_keys = set()
    for d in decisions:
        if d.get("tier", "human") == "human" and d.get("kind") != "notice":
            human_keys |= sweep_ref_keys(d, home, repos_known)
    seen, reads, before = set(), 0, json.dumps(state.ready, sort_keys=True)
    todo = []
    for d in decisions:
        held = sweep_held_pr(d, home)
        if not held or d.get("kind") == "notice" or sweep_ref_keys(d, home, repos_known) & human_keys:
            continue
        seen.add("%s#%d" % held)
        todo.append((state.ready_read.get("%s#%d" % held, 0), "%s#%d" % held, held, d))
    todo.sort(key=lambda t: (t[0], t[1]))   # the least recently read first
    for _, key, held, d in todo:
        skip = sweep_s5_skip(state, held, d, now)   # a draft, or a pull request of a parked ask: not read, not unchecked, the clock restarts when it is ready and neither
        if skip:
            state.s5_skipped[skip] += 1
            state.ready.pop(key, None)
            state.ready_read.pop(key, None)
            state.not_checked_since.pop(key, None)
            continue
        if reads >= SWEEP_READY_READS or (budget is not None and budget[0] < SWEEP_READY_READ_COST) \
                or engine.now() - state.started >= SWEEP_PASS_SECONDS:
            state.not_checked_since.setdefault(key, now)
            state.s5_unchecked += 1
            continue
        if budget is not None:
            budget[0] -= SWEEP_READY_READ_COST
        state.read_seq += 1
        state.ready_read[key] = state.read_seq   # an order, not a time: it cannot tie or go backwards
        reads += 1
        engine.reading_calls = 0
        opened, head, who, why = sweep_gate_reading(engine, held[0], held[1], now)
        if budget is not None:
            budget[0] = max(0, budget[0] - max(0, (getattr(engine, "reading_calls", 0) or 0) - SWEEP_READY_READ_COST))   # the calls really made beyond the least charge
        if opened is None:
            state.ready.pop(key, None)   # an unread pass drops the moment: the wait restarts, late and never early
            state.not_checked_since.setdefault(key, now)
            state.s5_unchecked += 1
            continue
        state.not_checked_since.pop(key, None)
        if opened != ["G6"]:
            state.ready.pop(key, None)   # a gate is open: not ready; the wait restarts when it is ready again
            continue
        row = state.ready.get(key)
        if not row or row["head"] != head:   # a new head restarts the wait
            row = state.ready[key] = {"head": head, "at": now}
        if now - row["at"] >= cfg["aged_cos_hours"] * 3600:
            state.s5[d.get("id") or key] = (head, row["at"], who)
    for key in list(state.not_checked_since):
        if key not in seen:
            state.not_checked_since.pop(key, None)
    state.s5_oldest = max([now - t for t in state.not_checked_since.values()] or [0])
    if decisions_whole:   # a partial list cannot say a pull request is no longer held: keep its moment
        for key in [k for k in state.ready if k not in seen]:
            state.ready.pop(key, None)
    for key in [k for k in state.ready_read if k not in seen] if decisions_whole else []:
        state.ready_read.pop(key, None)
    if engine.state_dir is not None and json.dumps(state.ready, sort_keys=True) != before:
        try:
            _write_small(engine.state_dir, SWEEP_READY_FILE, dict(sorted(state.ready.items())[:2000]))
        except OSError:
            pass   # a disposable file: a failed write costs a restarted wait after a restart


def sweep_findings(engine, state, decisions, home, now, cfg, regex, defer=(), decisions_whole=True):
    """The S1, S2, S3 and S5 findings: [(kind, "repo#n")], sorted. Reads nothing: it works on what the refresh kept.
    `defer` holds the repos (or "*": all) whose human-tier decisions are not all known this pass (their source failed or
    is still unread): S3, the only kind that a missing decision can raise falsely, is not judged for them. S1 reads only
    labels and bodies, S5 only the cos-tier decisions that ARE listed: neither waits."""
    found = set()
    state.details = {}
    state.sources = {}   # (kind, ref) -> what raised it: {"source": "comment:<id>" | "report:<id>" | "body", "match": the phrase}
    awaiting = {awaiting_label(engine.config), awaiting_cos_label(engine.config)}
    human = {sweep_ref_of(d, home) for d in decisions if d.get("tier", "human") == "human" and d.get("kind") != "notice"}

    def ref_text(repo, number):
        return "%s#%d" % (repo, number)
    s2, unattributed = set(), 0
    try:
        notices = {(n["repo"], n["number"]) for n in read_pr_notices(engine.state_dir)}
    except (NoticeError, OSError, TypeError):
        notices = set()
    for repo, picture in state.repos.items():
        for number, item in picture["items"].items():
            if not item["pr"]:
                labelled = bool(item["labels"] & awaiting)
                if item["heading"] and not labelled:
                    if not sweep_within(item.get("updated"), now, SWEEP_S1A_GRACE):
                        found.add(("S1a", ref_text(repo, number)))
                elif labelled and not item["section"]:
                    found.add(("S1b", ref_text(repo, number)))
            elif not item["captain"] and (item["body_waits"] or any(item["comments"][-SWEEP_PR_COMMENTS:])):
                found.add(("S2", ref_text(repo, number)))
                s2.add((repo, number))
                waiting = [i for i, w in enumerate(item["comments"][-SWEEP_PR_COMMENTS:]) if w]
                ids = item.get("comment_ids", [])[-SWEEP_PR_COMMENTS:]
                state.sources[("S2", ref_text(repo, number))] = (
                    {"source": "body", "match": "the PR body says it waits"} if item["body_waits"] else
                    {"source": "comment:%s" % (ids[waiting[-1]] if waiting and waiting[-1] < len(ids) else "?"), "match": "a recent comment says it waits"})
            if item["pr"]:   # #133: the label is the truth; the first-line marker is read only in the transition window
                if "marker-without-label" in item["captain_findings"] and not sweep_within(
                        item.get("created"), now, SWEEP_S6A_GRACE) and sweep_ours(
                        engine, repo, number, item.get("author", ""), item.get("assignees"), notices):
                    found.add(("S6a", ref_text(repo, number)))   # only OUR PR: another team's author may carry the line
                if "disagree" in item["captain_findings"] and not impact_rehold_open(engine.state_dir, repo, number) and not sweep_clear_agrees(engine, repo, number, item["labels"], item.get("marker")):   # (#815) a re-hold at this head is not a disagreement
                    found.add(("S6b", ref_text(repo, number)))
    for repo, picture in state.repos.items():
        if repo in defer or "*" in defer:
            continue
        for number, item in picture["items"].items():
            if not item["latest_strong"]:
                continue
            # a comment on an item is about that item, unless its sentence names other refs and not this one
            refs = item.get("latest_refs", [])
            named = [r for r in refs if r[0] in state.repos]
            targets = [(repo, number)] if not refs or (repo, number) in named else named
            if refs and not named:
                unattributed += 1   # it names only refs we cannot resolve to a watched item: not assumed to be about this one
            for target in targets:
                if target not in s2 and target not in human and target[1] in (state.repos[target[0]]["items"]):
                    found.add(("S3", ref_text(*target)))
                    state.sources[("S3", ref_text(*target))] = item.get("latest_source") or {"source": "comment:?", "match": ""}
    repos_known = list(state.repos) + ([home] if home else [])
    human_keys = set()
    for d in decisions:   # everything the owner's tier already holds, however its ref is spelled
        if d.get("tier", "human") == "human" and d.get("kind") != "notice":
            human_keys |= sweep_ref_keys(d, home, repos_known)
    skipped = state.__dict__.setdefault("s5_skipped", {"draft": 0, "parked": 0})   # shared with sweep_ready_pass, reset once per sweep there
    for d in decisions:
        ref = sweep_ref_of(d, home)
        # a report is about the refs its sentence names, never assumed to be about its own ask
        matched = []
        fires, refs = (sweep_strong_statement(state.strong, d.get("title"), ref[0], list(state.repos), matched)
                       if d.get("kind") == "report" and ref and ref[0] else (False, []))
        for target in (refs if fires else ()):
            if (target[0] in defer or "*" in defer or target in human or target in s2 or target[0] not in state.repos
                    or target[1] not in state.repos[target[0]]["items"]):
                continue
            found.add(("S3", ref_text(*target)))
            state.sources[("S3", ref_text(*target))] = {"source": "report:%s" % d.get("id"), "match": matched[0] if matched else ""}
        since = parse_iso(d.get("since"))
        if sweep_held_pr(d, home) and d.get("kind") != "notice":   # #986: a held pull request is raised from its gates (sweep_ready_pass), once per head
            ready = getattr(state, "s5", {}).get(d.get("id"))
            if decisions_whole and ready and not (sweep_ref_keys(d, home, repos_known) & human_keys):
                pair = ("S5", "%s@%s" % (d.get("id"), ready[0][:12]))
                found.add(pair)
                state.details[pair] = _one_line("head %s, ready at %s, held since %s; %s" % (ready[0][:12], iso(ready[1]), _one_line(d.get("since") or "?", 80), _one_line(ready[2], 200)), 400)
            continue
        if (decisions_whole and d.get("tier") == "cos" and d.get("kind") != "notice" and since is not None
                and now - since >= cfg["aged_cos_hours"] * 3600
                and not (sweep_ref_keys(d, home, repos_known) & human_keys)):   # already the owner's: not stale for cos
            skip = sweep_s5_skip(state, ref, d, now)
            if skip:
                skipped[skip] += 1
                continue
            found.add(("S5", d.get("id") or ref_text(*(ref or ("?", 0)))))
    for repo, picture in state.repos.items():   # S9: a merge or hold label left on a closed, unmerged PR (read-only: nothing is removed; a merged one is the daemon's to clean, #559)
        for number in sorted(picture.get("closed_holds") or {}):
            found.add(("S9", ref_text(repo, number)))
    for group in decision_core.find_cycles(decisions):   # S7: one finding per cycle, named by its first member
        found.add(("S7", group[0]))
    listed = {decision_core.decision_ref(d) for d in decisions} - {None}
    for d in decisions:   # S8: waits for an issue that is in a watched repo, is not an open decision and is not an open item there
        for target in d.get("waits_for") or []:
            repo, _, number = target.rpartition("#")
            picture = state.repos.get(repo)
            if (target not in listed and picture is not None and not picture.get("error") and not picture.get("truncated")
                    and number.isdigit() and int(number) not in picture["items"]):
                found.add(("S8", target))
    try:   # S10 (#969): a charter number two orchestrators of one repo record in the REGISTRY (read from the registry rows, never from GitHub or session metadata): one finding on the charter's issue, naming both
        registry = load_module("registry")
        reg = registry.read(engine.state_dir) if engine.state_dir is not None else None
        for (crepo, charter), names in sorted((registry.duplicate_charters(reg["agents"]) if reg and reg["whole"] else {}).items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
            if crepo:
                found.add(("S10", ref_text(crepo, charter)))
                state.sources[("S10", ref_text(crepo, charter))] = {"source": "registry", "match": "recorded by %s" % ", ".join(names)}
    except Exception:
        pass   # an unreadable registry raises nothing: the lookup itself says "unknown"
    state.unattributed = unattributed
    return sorted(found)


def sweep_s5_skip(state, ref, decision, now):
    """"draft" or "parked" when the decision's pull request is a draft, or closes or belongs to an ask parked now (the one reader
    `parked_until`, judged by `triage_parked_now`); else None. Reads only the picture the sweep holds: an item not in it leaves S5 raised."""
    picture = (state.repos.get(ref[0]) or {}).get("items", {}) if ref and ref[0] else {}
    item = picture.get(ref[1]) if ref else None
    if item is None or not item.get("pr"):
        return None
    if item.get("draft"):
        return "draft"
    asks = [item] + [picture.get(n) for _, n in item.get("closes", [])]
    if isinstance(decision.get("ask"), int) and not isinstance(decision.get("ask"), bool):
        asks.append(picture.get(decision["ask"]))
    return "parked" if any(a and triage_parked_now(a, now) for a in asks) else None


def _issue_pr_merged(record):
    """True when an issue record (the issues API) of a pull request says it was merged: its `pull_request.merged_at` is set (#559)."""
    pull = record.get("pull_request") if isinstance(record, dict) else None
    return isinstance(pull, dict) and bool(pull.get("merged_at"))


def sweep_verify(engine, state, kind, ref, now, cfg, regex, budget):
    """Re-check one finding against a fresh read of its issue or PR (and, when comments raised it, a fresh read of its
    last comments), so a cached copy cannot raise it. True: still holds. False: no longer (or never did). None: could not
    be checked now (no budget, a failed or truncated read): it is neither raised nor audited this pass."""
    if kind in ("S5", "S7", "S10"):
        return True  # about the decision list, or the registry, derived this very pass
    hub = getattr(engine, "hub", None)
    repo, _, number = ref.rpartition("#")
    if hub is None or not number.isdigit():
        return None
    number = int(number)
    picture = (state.repos.get(repo) or {"items": {}})["items"].get(number) or {}
    comments_raised = ((kind == "S2" and any(picture.get("comments", [])[-SWEEP_PR_COMMENTS:]))
                       or (kind == "S3" and picture.get("latest_strong", False)))
    if budget[0] < (2 if comments_raised else 1):
        return None
    budget[0] -= 1
    if kind == "S9":   # still a PR, no longer open, not merged, and still carrying a merge or hold label
        try:
            data = hub._read_one(repo, "issues/%d" % number, now, fresh=True)
        except GitHubUnavailable:
            return None
        if not isinstance(data, dict) or "state" not in data:
            return None
        names = {(l.get("name") if isinstance(l, dict) else l) for l in data.get("labels") or []}
        return _is_pr(data) and data.get("state") != "open" and not _issue_pr_merged(data) and bool(_label_kind(names, CAPTAIN_MERGE_LABELS[:1] + CAPTAIN_HOLD_LABELS))
    if kind == "S8":   # a decision waits for this issue: it still holds when the issue is closed, or is not there at all
        try:
            data = hub._read_one(repo, "issues/%d" % number, now, fresh=True)
        except GitHubUnavailable as err:
            return True if NOT_FOUND_RE.search(str(err)) else None
        return None if not isinstance(data, dict) or "state" not in data else data.get("state") == "closed"
    try:
        data = hub._read_one(repo, "issues/%d" % number, now, fresh=True)
    except GitHubUnavailable:
        return None
    if not isinstance(data, dict) or "number" not in data and "state" not in data:
        return None
    if data.get("state") != "open":
        return False
    names = {(l.get("name") if isinstance(l, dict) else l) for l in data.get("labels") or []}
    body, is_pr = data.get("body"), _is_pr(data)
    labelled = bool(names & {awaiting_label(engine.config), awaiting_cos_label(engine.config)})
    if kind in ("S6a", "S6b"):
        want = "marker-without-label" if kind == "S6a" else "disagree"
        if kind == "S6a" and not sweep_ours(engine, repo, number, (pr_login(data.get("user")) or "").lower(),
                                            [(pr_login(a) or "").lower() for a in data.get("assignees") or [] if isinstance(a, dict)]):
            return False
        if kind == "S6a" and sweep_within(parse_iso(data.get("created_at")), now, SWEEP_S6A_GRACE):
            return False   # opened a moment ago: the label is applied after it opens
        if kind == "S6b" and impact_rehold_open(engine.state_dir, repo, number):
            return False   # (#815) held again at this head: the line and the label are being reconciled
        return is_pr and want in pr_marker_state(names, body)["findings"] and not (kind == "S6b" and sweep_clear_agrees(engine, repo, number, names, pr_marker(body)))
    if kind in ("S1a", "S1b"):
        if is_pr:
            return False
        if kind == "S1a" and sweep_within(parse_iso(data.get("updated_at")), now, SWEEP_S1A_GRACE):
            return False   # changed a moment ago: an escalation may be half done (one label off, the next not yet on)
        return (sweep_has_heading(body) and not labelled) if kind == "S1a" else (labelled and not sweep_has_section(body))
    waits_in_comments = False
    if comments_raised:
        budget[0] -= 1
        since = iso(now - cfg["comment_window_days"] * 86400)
        try:
            recent, cut = hub.read_pages("repos/%s/issues/%d/comments?since=%s&per_page=100" % (repo, number, since),
                                         cap=1)
        except GitHubUnavailable:
            return None
        if cut:
            return None  # the last comments are not on the first page: cannot say
        if kind == "S3":   # only the LATEST comment on the item counts
            waits_in_comments = bool(recent) and sweep_says_waiting_strong(state.strong, recent[-1].get("body"))
        else:
            waits_in_comments = any(sweep_says_waiting(regex, c.get("body")) for c in recent[-SWEEP_PR_COMMENTS:])
    if kind == "S2":
        return is_pr and not is_captain({"labels": names, "body": body}) and (sweep_says_waiting(regex, body) or waits_in_comments)
    if kind == "S3":
        if awaiting_label(engine.config) in names:
            return False  # it carries the person's label: a human-tier decision, whatever the list copy said
        return waits_in_comments or not comments_raised
    return None


SWEEP_DEFER_SECONDS = 1800   # S3 waits at most this long for a repo's decisions to be read; then it runs and says so


def sweep_deferrals(state, errors, repos, now):
    """(repos whose S3 is deferred now, [{"for", "why", "since", "overdue"}]). S3 is judged against the human-tier
    decisions, so a repo whose awaiting issues are unread or unreadable (its own entry), or a failed reports or request
    source (every repo), defers it. A deferral that outlasts SWEEP_DEFER_SECONDS stops deferring (the fresh label check
    still guards each finding) and is reported as overdue: nothing is hidden for good, and `status` and doctor show it."""
    reasons = {}
    for entry in errors or ():
        source, text = entry.get("source"), str(entry.get("error") or "")
        if source == "github":
            named = [r for r in repos if r in text]
            for key in named or ["*"]:
                reasons.setdefault(key, text)
        elif source in ("v2", "reports", "registry"):
            reasons.setdefault("*", "%s: %s" % (source, text))
    state.deferred_since = {k: v for k, v in getattr(state, "deferred_since", {}).items() if k in reasons}
    deferred, defer = [], set()
    for key, why in sorted(reasons.items()):
        since = state.deferred_since.setdefault(key, now)
        overdue = now - since >= SWEEP_DEFER_SECONDS
        deferred.append({"for": key, "why": _one_line(why, 160), "since": iso(since), "overdue": overdue})
        if not overdue:
            defer.add(key)
    return defer, deferred


def sweep_decisions(engine, decisions, now, errors=()):
    """The list after the sweep (stale decisions removed when enabled), with the sweep's side effects: audit lines, the
    pending cos fact and sweep.json. Called once per pass with the freshly derived list."""
    cfg = engine.config.get("sweep", SWEEP_DEFAULTS)  # no key at all: shadow
    if cfg is False or cfg is None:
        return decisions  # `sweep: false`: fully off, reads nothing, writes nothing
    state = sweep_state(engine)
    state.started = engine.now()
    sweep_alarmed_load(engine, state, now, cfg)
    repos = decision_repos(engine)
    watched = [d for d in decisions]
    home = engine.config.get("intent_repo") or (repos[0] if len(repos) == 1 else None)
    regex = sweep_phrase_re(cfg["person_names"])
    state.strong = sweep_strong_re(cfg["person_names"])
    budget = [cfg.get("calls_per_pass", SWEEP_CALLS_PER_PASS)]
    sweep_refresh(engine, state, repos, now, cfg, regex, budget)
    share = [(budget[0] + 1) // 2]  # the single reads are split: half for S4, the rest (and S4's unused) to verifying
    start = share[0]
    kept, stale = sweep_withdraw(engine, state, watched, home, now, cfg, share)
    budget[0] -= start - share[0]
    for ref in stale:
        text = "%s#%d" % ref
        if text not in state.withdrawn:
            if len(state.withdrawn) > 5000:
                state.withdrawn.clear()
            state.withdrawn.add(text)
            engine._note("sweep.withdrawn" if cfg["enabled"] else "sweep.finding", text,
                         {"kind": "S4", "ref": text, "applied": cfg["enabled"], "why": "its issue or PR is closed"})
    notices_whole, why = whole(engine, "notices")   # #805: the owner-tier rows of held PRs come from the notice read: S3 and S5 judge by them, and a read that is not whole is no evidence of an absence
    if not notices_whole:
        errors = list(errors or ()) + [{"source": "registry", "error": "the notice read is not whole: %s" % why}]
    defer, deferred = sweep_deferrals(state, errors, repos, now)
    whole_list = notices_whole or "*" not in defer
    ready_share = [max(budget[0] // 2, SWEEP_READY_READ_COST if budget[0] >= SWEEP_READY_READ_COST else 0)]   # #986: at most half of what is left, so the findings' verification keeps the rest
    ready_start = ready_share[0]
    sweep_ready_pass(engine, state, kept, home, now, cfg, whole_list, ready_share)
    budget[0] -= ready_start - ready_share[0]
    candidates = sweep_findings(engine, state, kept, home, now, cfg, regex, defer, whole_list)
    findings, unchecked = [], 0
    attempts = sorted(candidates, key=lambda c: (state.attempted.get(c, 0), c))  # the longest untried first
    for pair in attempts:
        if now - state.confirmed.get(pair, 0) < SWEEP_TTL:
            findings.append(pair)
            continue
        if engine.now() - state.started >= SWEEP_PASS_SECONDS:
            unchecked += 1
            continue
        state.attempted[pair] = now
        verdict = sweep_verify(engine, state, pair[0], pair[1], now, cfg, regex, budget)
        if verdict:
            state.confirmed[pair] = now
            findings.append(pair)
        elif verdict is None:
            unchecked += 1
        else:
            state.confirmed.pop(pair, None)
            if (pair, int(now // 86400)) not in state.logged:
                state.logged.add((pair, int(now // 86400)))
                engine._note("sweep.dismissed", pair[1], {"kind": pair[0], "ref": pair[1],
                                                          "why": "a fresh read does not bear it out"})
    findings.sort()
    day = int(now // 86400)
    due = [(k, r) for k, r in findings
           if cfg["enabled"] and now - state.alarmed.get((k, r), 0) >= cfg["realarm_hours"] * 3600]   # what is queued this pass
    woken = sweep_queue_fact(state, due, now, cfg) if due else set()   # #849: "woken" = the pass's wake carried the finding: the pairs the queue function set the re-alarm memory for
    if due:
        sweep_alarmed_save(engine, state, now, cfg)
    for kind, ref in findings:
        once = (kind, ref, day)
        if once not in state.logged or ((kind, ref) in woken and once not in state.woken_logged):
            if len(state.logged) > 5000:
                state.logged.clear()
                state.woken_logged.clear()
            state.logged.add(once)
            if (kind, ref) in woken:
                state.woken_logged.add(once)
            detail = {"kind": kind, "ref": ref, "what": SWEEP_KINDS[kind], "woken": (kind, ref) in woken}
            if (kind, ref) in state.details:
                detail["text"] = _one_line(state.details[(kind, ref)], 400)
            if (kind, ref) in state.sources:
                detail["source"] = state.sources[(kind, ref)]["source"]   # #805: what raised it, so the reason is readable from the audit
                detail["match"] = _one_line(state.sources[(kind, ref)]["match"], 80)
            engine._note("sweep.finding", ref, detail)
    counts = {}
    for kind, _ in findings:
        counts[kind] = counts.get(kind, 0) + 1
    if stale:
        counts["S4"] = len(stale)
    state.current = {"enabled": cfg["enabled"], "counts": counts, "total": len(findings),
                     "truncated": sorted(r for r, p in state.repos.items() if p.get("truncated")),
                     "unreadable": sorted(r for r, p in state.repos.items() if p.get("error")), "at": iso(now),
                     "deferred": deferred, "unchecked": unchecked, "not_checked": state.s5_unchecked, "not_checked_oldest_seconds": int(max(state.s5_oldest, 0)),
                     "unattributed": getattr(state, "unattributed", 0), "s5_skipped": dict(getattr(state, "s5_skipped", {}))}

    sweep_write(engine, state)
    return kept if cfg["enabled"] else decisions


def sweep_queue_fact(state, due, now, cfg):
    """ONE fact for everything newly due: `sweep: N findings: ref kind, ...`, capped, refs and kinds only. The key
    names the set and the slot, so a restart that rebuilds the same set in the same slot is one delivery."""
    shown = due[:cfg["max_findings"]]
    digest = hashlib.sha256("|".join("%s:%s" % pair for pair in shown).encode()).hexdigest()[:10]
    key = "sweep:%s:%d" % (digest, int(now // (cfg["realarm_hours"] * 3600)))
    kinds = sorted({k for k, _ in due})
    summary = "sweep: %d finding%s: %s%s. %s. Resolve or escalate each; the sweep changes nothing." % (
        len(due), "" if len(due) == 1 else "s", ", ".join("%s %s%s" % (ref, kind, " (%s)" % state.details[(kind, ref)] if (kind, ref) in state.details else "") for kind, ref in shown),
        " (+%d more)" % (len(due) - len(shown)) if len(due) > len(shown) else "",
        "; ".join("%s = %s" % (k, SWEEP_KINDS[k]) for k in kinds))
    for pair in due:
        state.alarmed[pair] = now
    state.facts[key] = (summary, now)
    for old in [k for k, (_, at) in state.facts.items() if now - at > cfg["realarm_hours"] * 7200]:
        del state.facts[old]
    return set(due)   # every due finding is carried (named, or counted in "+N more") and remembered


def sweep_write(engine, state):
    """sweep.json: the last run's counts, for `status` (a display; nothing decides from it). Written on a change."""
    path = Path(engine.state_dir) / SWEEP_FILE
    try:
        state_write(path, json.dumps(state.current, sort_keys=True) + "\n", _without(state.current, "at"), HEARTBEAT_SWEEP)   # its `at` is when it last ran: a heartbeat keeps it honest
    except OSError:
        pass


def sweep_summary(state_dir):
    """What `status` shows: the daemon's last sweep, or None."""
    try:
        raw = json.loads((Path(state_dir) / SWEEP_FILE).read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and isinstance(raw.get("counts"), dict) else None


class SweepFacts:
    """The sweep's pending fact for the Chief of Staff (`sweep:<set>:<slot>`), one per Chief of Staff that runs. It
    stays pending until delivered (the engine finds its key in the transcript), then ages out of the sweep's memory."""

    __name__ = "sweep_facts"

    def __init__(self):
        self.engine = None

    def bind(self, engine):
        self.engine = engine

    def __call__(self):
        engine = self.engine
        state = getattr(engine, "sweep", None) if engine else None
        discovery = getattr(engine, "discovery", None) if engine else None
        if not state or not state.facts or discovery is None or discovery.problems:
            return []
        chiefs = sorted(n for n, o in discovery.agents.items() if (o.fleet or {}).get("role") == "chief-of-staff")
        return [Fact(key, name, "sweep", summary, created, sender="switchboard")
                for key, (summary, created) in sorted(state.facts.items()) for name in chiefs]
