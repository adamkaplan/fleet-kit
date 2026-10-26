# ---- #130: the triage function (rule family T), grown from the sweep: the same hub reads, call budget, fresh re-check
# before a finding, shadow default and batched fact to the Chief of Staff. THE OWNER'S HOLD (binding): it WRITES NOTHING to
# GitHub, ever: no label, assignment, comment, close or edit. Its only writes are audit lines, triage.json (a display
# file) and the batched fact to the Chief of Staff. The fix rules of the plan stay unbuilt until he lifts the hold.

TRIAGE_DEFAULTS = {"enabled": False, "repos": None, "required_labels": {}, "sla": {}, "sla_overrides": {},
                   "our_logins": None, "daily_summary": True, "realarm_hours": 24, "max_findings": 30,
                   "calls_per_pass": 30, "pass_seconds": 10, "backoff": True, "digest_interval_minutes": 60, "ownerless": None, "alert_labels": {}, "integration_branch": None}
# Every `_days` value is in days and may be a fraction (2/24 is two hours); `t7_ours_hours` is in hours.
TRIAGE_SLA = {"t1_days": 1, "t2_days": 1 / 24, "t3_ask_days": 1, "t3_other_days": 3, "t5_days": 1, "t6_days": 2,
              "t7_ours_hours": 4, "t7_other_days": 2, "t8_days": 1, "t9_days": 3, "t11_days": 1}
# (No tracked repo is named here: a public source names no private org. The extra repos go in the config's `triage.repos`.)
TRIAGE_KINDS = {"T1": "owner missing", "T2": "required label missing", "T3": "no next step",
                "T5": "human-tier decision unanswered", "T6": "MERGE: captain PR waiting", "T7": "PR with no review",
                "T8": "blocked ask", "T9": "ask with no activity", "T10": "closed but still labelled awaiting",
                "T11": "orphan", "T12": "plan issue still open after its PR merged", "OO": "other-owner (flag only)"}
# #805: the inputs each rule judges, in ONE table: the candidates a pass drops and the records the prune keeps are both read from it, so they cannot drift.
# A rule that judges whether an item is OURS (every one but T5, T6 and T10) also needs `agents`: ownership comes from the running agents.
TRIAGE_RULE_INPUTS = {"T1": ("agents", "issues", "merged"), "T2": ("agents", "issues"), "T3": ("agents", "issues", "comments"), "T5": ("decisions", "issues"),
                      "T6": ("decisions", "issues"), "T7": ("agents", "issues", "comments", "decisions"), "T8": ("agents", "issues"),
                      "T9": ("agents", "issues", "comments"), "T10": ("closed",), "T11": ("agents", "issues", "merged"), "T12": ("agents", "issues", "merged", "comments")}
# "agents" and "decisions" are global; the others are per repository (`<name>:<repo>`). T7 also reads the decision list (a captain PR listed there is T6's);
# T1 and T11 give way to T12 when a merged PR links the item (so they read `merged`), and T9 gives way to T3 (so it reads `comments`).
# The records of the OTHER kinds are kept whenever ANY input of ANY rule is not whole: the other-owner flag stands for whichever rule would have fired, and the
# invalid-park flag is raised from the digest of all of them. Their set is derived from the table, so a new rule's input is included at once.
TRIAGE_UNION_KINDS = ("OO", "PARKED")


def triage_inputs(kind):
    """The inputs the records of `kind` judge, or None for a kind the table does not know (its records are then all kept)."""
    if kind in TRIAGE_UNION_KINDS:
        return tuple(sorted({name for needs in TRIAGE_RULE_INPUTS.values() for name in needs}))
    return TRIAGE_RULE_INPUTS.get(kind)


TRIAGE_T10_GRACE = 600         # seconds after a close in which a still-set awaiting label is not yet a finding
TRIAGE_TTL = 900               # seconds a unit read is kept
TRIAGE_CALLS_PER_PASS = TRIAGE_DEFAULTS["calls_per_pass"]   # default of `triage.calls_per_pass` (reads and fresh checks alike)
TRIAGE_CALLS_CAP = 100
TRIAGE_PASS_SECONDS = TRIAGE_DEFAULTS["pass_seconds"]       # default of `triage.pass_seconds`; cap TRIAGE_SECONDS_CAP
TRIAGE_SECONDS_CAP = 60
TRIAGE_LIST_PAGES = 2
TRIAGE_MERGED_PER_PAGE = 50    # #384: the recently closed pull requests read per repo per refresh (one page)
TRIAGE_COMMENT_DAYS = 14       # the comment window: next-step markers and review comments are looked for in it
TRIAGE_UNREADABLE_RETRY = 3600
TRIAGE_FILE = "triage.json"
TRIAGE_OWNER_RE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?(?:\*\*|__)?(?:owner|owned by)(?:\*\*|__)?[ \t]*:?(?:\*\*|__)?[ \t]*([A-Za-z0-9@_`\"'\[(][^\n]*)$", re.I | re.M)
TRIAGE_OWNER_PLACEHOLDER_RE = re.compile(r"^[\W_]*(?:tbd|tbc|none|n/?a|na|nobody|no one|unknown|unassigned|unowned|todo|\?+)[\W_]*$", re.I)
TRIAGE_NOT_APPROVING_RE = re.compile(r"\b(?:not\s+(?:yet\s+)?(?:approved|lgtm)|changes\s+requested|needs?\s+changes|request(?:s|ed)?\s+changes)\b", re.I)
PARKED_UNTIL_RE = re.compile(r"^[ \t]*Parked until:[ \t]*([0-9]{4}-[0-9]{2}-[0-9]{2})[ \t]*\r?$", re.M)  # ASCII digits only; a CRLF body is read
PARKED_MAX_DAYS = 366   # a park reaches at most this far ahead of the moment it is judged: `9999-12-31` does not park forever


def parked_until(body, labels):
    """The epoch second a `<user>:parked` item is parked until, or None. It counts ONLY with the label AND one line
    `Parked until: YYYY-MM-DD` in the body (outside quotes and code); a missing, malformed or impossible date is None, and the day
    is parked through its end (UTC). Strict by design: a deferral that cannot be read is not a deferral."""
    if not any(isinstance(l, str) and l.endswith(":parked") for l in labels or ()) or not isinstance(body, str):
        return None
    found = PARKED_UNTIL_RE.findall(SWEEP_QUOTED_RE.sub(" ", body))
    if len(found) != 1:
        return None
    try:
        day = datetime.strptime(found[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return day.timestamp() + 86400.0


def triage_parked_now(item, now):
    """True while a normalised item is parked: its `parked_until` (read by `parked_until` above, the one date reader) is in the future and within PARKED_MAX_DAYS. T3 and the
    ownerless line share this one definition; the owner-wait exemption is T3's own."""
    until = item.get("parked_until")
    return until is not None and now < until <= now + PARKED_MAX_DAYS * 86400.0
TRIAGE_NEXT_RE = re.compile(r"^[ \t]*(?:next(?: steps?)?[ \t]*:|[-*][ \t]+\[[ xX]\])", re.I | re.M)
TRIAGE_STANDING_RE = re.compile(r"^[ \t]*(?:[-*>][ \t]+)?(?:\*\*|__)?(?:standing line|next(?: steps?)?)(?:[ \t]*\([^)\n]*\))?(?:\*\*|__)?[ \t]*:(.*)$", re.I | re.M)
TRIAGE_STANDING_BATCH_RE = re.compile(r"\b(?:awaiting|waiting (?:for|on)|until)[ \t]+(?:the[ \t]+)?batch\b|\bin batch[ \t]+[0-9]+", re.I)
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
TRIAGE_STANDING_ISO_RE = re.compile(r"\b([0-9]{4})-([0-9]{2})-([0-9]{2})\b")
TRIAGE_STANDING_DMY_RE = re.compile(r"\b([0-9]{1,2})(?:st|nd|rd|th)?[ \t]+" + _MON + r"(?:,?[ \t]+([0-9]{4})\b)?", re.I)
TRIAGE_STANDING_MDY_RE = re.compile(r"\b" + _MON + r"[ \t]+([0-9]{1,2})(?:st|nd|rd|th)?\b(?:,?[ \t]+([0-9]{4})\b)?", re.I)


TRIAGE_STANDING_YEARLESS_DAYS = 60   # a year-less wait date further ahead than this is not read


def _standing_day(text, at):
    """The FIRST date in `text` that is not in the past (on or after the comment's own day `at`), as the epoch second of its UTC start; None when there is no date;
    "bad" when one is impossible, when every date is past, or when a year-less one is more than 60 days ahead. A year-less date before the comment's day is past (never
    next year's), except a December comment naming a date within 60 days after the new year."""
    found = []
    for m in TRIAGE_STANDING_ISO_RE.finditer(text):
        found.append((m.start(), int(m.group(1)), int(m.group(2)), int(m.group(3)), True))
    for m in TRIAGE_STANDING_DMY_RE.finditer(text):
        found.append((m.start(), m.group(3), _MONTHS.index(m.group(2).lower()[:3]) + 1, int(m.group(1)), None))
    for m in TRIAGE_STANDING_MDY_RE.finditer(text):
        found.append((m.start(), m.group(3), _MONTHS.index(m.group(1).lower()[:3]) + 1, int(m.group(2)), None))
    if not found:
        return None
    at_day = datetime.fromtimestamp(at, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    base = at_day.year
    for _pos, year, month, day, _full in sorted(found, key=lambda f: f[0]):
        try:
            if year is not None:
                when = datetime(int(year), month, day, tzinfo=timezone.utc)
                if when < at_day:
                    continue   # written with its year and already past: not a wait
                return when.timestamp()
            when = datetime(base, month, day, tzinfo=timezone.utc)
            if when < at_day:   # a year-less date before the comment's own day is PAST, never next year's, except a December comment naming a January date
                when = datetime(base + 1, month, day, tzinfo=timezone.utc)
                if at_day.month != 12 or when.timestamp() > at_day.timestamp() + TRIAGE_STANDING_YEARLESS_DAYS * 86400.0:
                    continue
            if when.timestamp() > at_day.timestamp() + TRIAGE_STANDING_YEARLESS_DAYS * 86400.0:
                return "bad"   # a year-less date more than 60 days ahead is not read as a wait
            return when.timestamp()
        except (ValueError, OverflowError):
            return "bad"
    return "bad"   # every date was past


def standing_line(text, at):
    """THE reader of a standing line (T3 and T12 share it; nothing else parses one). `text` has its quotes and code stripped; `at` is the epoch second the text
    was posted. The LAST line headed `Standing line (...):`, `Next step (...):` or `Next:` decides: ("date", the epoch second the named day ends, UTC), ("batch", None)
    for "awaiting batch" with no date, or ("none", None) for a line that names neither or an impossible date. None when the text has no such line.
    A date in the parentheses of the heading is when it was posted, not a date it waits for. Pure: no read, no write."""
    lines = TRIAGE_STANDING_RE.findall(text) if isinstance(text, str) else []
    if not lines:
        return None
    line = lines[-1]
    day = _standing_day(line, at)
    if day == "bad":
        return ("none", None)
    if day is not None:
        return ("date", day + 86400.0)
    if TRIAGE_STANDING_BATCH_RE.search(line):
        return ("batch", None)
    return ("none", None)


def triage_standing_silent(item, comments, now, merged_batch=False, trusted=frozenset()):
    """True while the latest standing line of an item (its body, then its comments, the latest wins) keeps T3 and T12 quiet: a date that ends after `now` and within
    PARKED_MAX_DAYS, or "awaiting batch" with no later comment, within PARKED_MAX_DAYS of the line, and not after the batch reached the default branch."""
    latest = item.get("standing")   # (posted, state) from the body
    newer = False
    for c in comments or ():
        if c.get("standing") is not None and (c.get("author") in trusted or (c.get("author") and c.get("author") == item.get("author"))):
            # only a fleet login or the issue's author can silence. `trusted` is `triage_our_logins` (config `our_logins`, else gh_users values + owner_logins, lowercased, exact
            # membership): the set triage already uses. A bot listed only in `github_bot_logins` does not silence until #1304 lands. An unreadable author ('' on both sides) never matches.
            latest, newer = (c["at"], c["standing"]), False
        else:
            newer = True
    if latest is None:
        return False
    posted, (kind, until) = latest
    if kind == "date":
        return now < until <= now + PARKED_MAX_DAYS * 86400.0
    if kind == "batch":
        return not newer and not merged_batch and now <= posted + PARKED_MAX_DAYS * 86400.0
    return False
TRIAGE_REVIEW_RE = re.compile(r"\b(?:review of [0-9a-f]{7}|approved?|changes requested|lgtm)\b", re.I)


def triage_says_reviewed(text):
    """A comment that reads as an approving review: it says review/approved/lgtm and does not say `not approved`, `changes
    requested` or `needs changes`. (A review that asks for changes is not a reason to call the PR reviewed.)"""
    return bool(isinstance(text, str) and TRIAGE_REVIEW_RE.search(text) and not TRIAGE_NOT_APPROVING_RE.search(text))


def triage_names_owner(body):
    """True when a line of the body (outside code and quotes) says `Owner: <name>` or `owned by <name>` with a real name: a
    placeholder (TBD, none, n/a, unknown, ?) is not an owner."""
    if not isinstance(body, str):
        return False
    return any(not TRIAGE_OWNER_PLACEHOLDER_RE.match(m.group(1)) for m in TRIAGE_OWNER_RE.finditer(SWEEP_QUOTED_RE.sub(" ", body)))


def triage_owner_name(body):
    """The name on the FIRST `Owner:` line of the body (outside code and quotes): its first word, or None. Only that one word is kept."""
    if not isinstance(body, str):
        return None
    for m in TRIAGE_OWNER_RE.finditer(SWEEP_QUOTED_RE.sub(" ", body)):
        word = re.match(r"[\W_]*([A-Za-z0-9][A-Za-z0-9._-]*)", m.group(1))
        return word.group(1) if word and not TRIAGE_OWNER_PLACEHOLDER_RE.match(m.group(1)) else None
    return None


def _alert_not_a_map(raw, repos):
    return None if isinstance(raw, dict) else "must be a map of OWNER/REPO to a list of label names, got %r" % (type(raw).__name__,)


def _alert_bad_repo(raw, repos):
    bad = [r for r in raw if not (isinstance(r, str) and REPO_RE.match(r))] if isinstance(raw, dict) else []
    return "the repository name must be OWNER/REPO, got %r" % (bad[0],) if bad else None


def _alert_labels_not_a_list(raw, repos):
    bad = [r for r, v in raw.items() if not isinstance(v, list)] if isinstance(raw, dict) else []
    return "%r must name a list of label names" % (bad[0],) if bad else None


def _alert_bad_label(raw, repos):
    for repo, labels in (raw.items() if isinstance(raw, dict) else []):
        for label in labels if isinstance(labels, list) else []:
            if not (isinstance(label, str) and label.strip() and label == label.strip() and "," not in label):
                return "%r holds a label that is not a non-empty string without surrounding spaces or a comma: %r" % (repo, label)
    return None


def _alert_duplicate_label(raw, repos):
    for repo, labels in (raw.items() if isinstance(raw, dict) else []):
        labels = [l for l in labels if isinstance(l, str)] if isinstance(labels, list) else []
        if len(set(labels)) != len(labels):
            return "%r names a label twice" % (repo,)
    return None


def _alert_repo_not_read(raw, repos):
    if repos is None or not isinstance(raw, dict):
        return None   # with no `triage.repos` the watched repositories are read
    missing = [r for r in raw if isinstance(r, str) and r not in repos]
    return "%r is not in triage.repos, so its issues are never read" % (missing[0],) if missing else None


# (code, check(alert_labels, triage_repos) -> a problem text or None), in the order they are applied: the one table the validator and its tests read.
ALERT_SHAPE_CHECKS = [("not-a-map", _alert_not_a_map), ("bad-repo", _alert_bad_repo), ("labels-not-a-list", _alert_labels_not_a_list),
                      ("bad-label", _alert_bad_label), ("duplicate-label", _alert_duplicate_label), ("repo-not-read", _alert_repo_not_read)]


def validate_triage_config(config, source):
    """`triage`: absent, null or {} runs in shadow (findings audited, nobody woken); `enabled` true adds only the wake of
    the Chief of Staff; `false` turns it fully off. Nothing here can make it write to GitHub: there is no such code."""
    if "triage" not in config:
        return
    raw = config["triage"]
    if raw is False:
        return
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("%s: 'triage' must be an object or false, got %r" % (source, type(raw).__name__))
    unknown = sorted(set(raw) - set(TRIAGE_DEFAULTS))
    if unknown:
        warn_unknown_config("%s: 'triage' has key(s) this tool does not read: %s (known: %s)"
                          % (source, ", ".join(unknown), ", ".join(TRIAGE_DEFAULTS)), 'triage', unknown, list(TRIAGE_DEFAULTS))
    section = dict(TRIAGE_DEFAULTS, **raw)
    where = "%s: 'triage" % source
    if section["integration_branch"] is not None and not (isinstance(section["integration_branch"], str) and section["integration_branch"]):   # #921
        raise ConfigError("%s.integration_branch' must be a branch name or null, got %r" % (where, section["integration_branch"]))
    for key in ("enabled", "daily_summary", "backoff"):
        if not isinstance(section[key], bool):
            raise ConfigError("%s.%s' must be true or false, got %r" % (where, key, section[key]))
    for key in ("repos", "our_logins"):
        value = section[key]
        if value is not None and not (isinstance(value, list) and all(isinstance(r, str) and r for r in value)):
            raise ConfigError("%s.%s' must be a list of strings or null, got %r" % (where, key, value))
    if section["repos"] is not None and not all(REPO_RE.match(r) for r in section["repos"]):
        raise ConfigError("%s.repos' must be OWNER/REPO names, got %r" % (where, section["repos"]))
    if not _is_number(section["realarm_hours"]) or section["realarm_hours"] <= 0:
        raise ConfigError("%s.realarm_hours' must be a positive number, got %r" % (where, section["realarm_hours"]))
    if not _is_number(section["digest_interval_minutes"]) or section["digest_interval_minutes"] < 0:
        raise ConfigError("%s.digest_interval_minutes' must be a number of minutes, 0 or more (0 sends a digest every pass that has one), got %r" % (where, section["digest_interval_minutes"]))
    own = section["ownerless"]   # #605: off unless the owner names the team repositories
    if own is not None:
        if not isinstance(own, dict) or set(own) - {"repos", "days", "limit", "owner_labels"}:
            raise ConfigError("%s.ownerless' must be null or {repos, days, limit, owner_labels}, got %r" % (where, own))
        repos = own.get("repos")
        if isinstance(repos, dict):   # a map of OWNER/REPO to {label_owners: {label: lane}}
            if not repos or not all(isinstance(r, str) and REPO_RE.match(r) for r in repos):
                raise ConfigError("%s.ownerless.repos' must be a non-empty list or map of OWNER/REPO names, got %r" % (where, list(repos)))
            for repo, entry in repos.items():
                if not isinstance(entry, dict) or set(entry) - {"label_owners"}:
                    raise ConfigError("%s.ownerless.repos.%s' must be {label_owners: {label: lane}}, got %r" % (where, repo, entry))
                lo = entry.get("label_owners", {})
                if not isinstance(lo, dict) or not all(isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip() for k, v in lo.items()):
                    raise ConfigError("%s.ownerless.repos.%s.label_owners' must be a map of non-empty label to non-empty lane name, got %r" % (where, repo, lo))
        elif not (isinstance(repos, list) and repos and all(isinstance(r, str) and REPO_RE.match(r) for r in repos)):
            raise ConfigError("%s.ownerless.repos' must be a non-empty list or map of OWNER/REPO names, got %r" % (where, repos))
        if "days" in own and (not _is_number(own["days"]) or own["days"] <= 0):
            raise ConfigError("%s.ownerless.days' must be a positive number of days, got %r" % (where, own["days"]))
        if "limit" in own and (not isinstance(own["limit"], int) or isinstance(own["limit"], bool) or not 100 <= own["limit"] <= 2000):
            raise ConfigError("%s.ownerless.limit' must be a whole number from 100 to 2000, got %r" % (where, own["limit"]))
        if "owner_labels" in own and not (isinstance(own["owner_labels"], list) and all(isinstance(x, str) and x for x in own["owner_labels"])):
            raise ConfigError("%s.ownerless.owner_labels' must be a list of label names or patterns (agent:*), got %r" % (where, own["owner_labels"]))
    for code, check in ALERT_SHAPE_CHECKS:   # #851: one table of the shapes `alert_labels` may not take; each is a named error
        bad = check(section["alert_labels"], section["repos"])
        if bad:
            raise ConfigError("%s.alert_labels' %s: %s" % (where, code, bad))
    cap = section["max_findings"]
    if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= 200:
        raise ConfigError("%s.max_findings' must be a whole number from 1 to 200, got %r" % (where, cap))
    calls = section["calls_per_pass"]
    if not isinstance(calls, int) or isinstance(calls, bool) or not 4 <= calls <= TRIAGE_CALLS_CAP:
        raise ConfigError("%s.calls_per_pass' must be a whole number from 4 to %d, got %r" % (where, TRIAGE_CALLS_CAP, calls))
    seconds = section["pass_seconds"]
    if not _is_number(seconds) or not 1 <= seconds <= TRIAGE_SECONDS_CAP:
        raise ConfigError("%s.pass_seconds' must be a number from 1 to %d, got %r" % (where, TRIAGE_SECONDS_CAP, seconds))
    for name in ("sla",):
        _check_sla(section[name], where + "." + name)
    overrides = section["sla_overrides"]
    if not isinstance(overrides, dict):
        raise ConfigError("%s.sla_overrides' must map OWNER/REPO to an sla object, got %r" % (where, overrides))
    for repo, sla in overrides.items():
        _check_sla(sla, "%s.sla_overrides.%s" % (where, repo))
    labels = section["required_labels"]
    if not isinstance(labels, dict) or not all(
            isinstance(repo, str) and isinstance(group, list) and all(
                isinstance(l, str) or (isinstance(l, list) and l and all(isinstance(x, str) for x in l)) for l in group)
            for repo, group in labels.items()):
        raise ConfigError("%s.required_labels' must map OWNER/REPO to a list of labels (or lists of alternatives), "
                          "got %r" % (where, labels))
    config["triage"] = section


def _check_sla(sla, where):
    if not isinstance(sla, dict) or any(k not in TRIAGE_SLA or not _is_number(v) or v <= 0 for k, v in sla.items()):
        raise ConfigError("%s' must be an object of positive numbers named among %s, got %r"
                          % (where, ", ".join(TRIAGE_SLA), sla))


def triage_sla(cfg, repo):
    merged = dict(TRIAGE_SLA)
    merged.update(cfg.get("sla") or {})
    merged.update((cfg.get("sla_overrides") or {}).get(repo) or {})
    return merged


def triage_repos(engine, cfg):
    if cfg.get("repos") is not None:
        return list(cfg["repos"])
    return sorted(set(decision_repos(engine)))


def triage_our_logins(engine, cfg):
    if cfg.get("our_logins") is not None:
        return {l.lower() for l in cfg["our_logins"]}
    config = engine.config
    return {str(v).lower() for v in (config.get("gh_users") or {}).values()} | {l.lower() for l in config.get("owner_logins") or []}


def triage_t10_graced(engine, cfg, record, now):
    """True when a close is too recent for T10: inside TRIAGE_T10_GRACE of `closed_at`, UNLESS the closer (`closed_by`, when the
    record names one) is one of ours: our own agents take the label off in the same sweep as the close, so a label still set is
    their miss and no grace is given (the owner's panel shows a closed-but-labelled issue meanwhile). A closer the record does
    not name, or one that is not ours, keeps the grace."""
    if not sweep_within(parse_iso(record.get("closed_at")), now, TRIAGE_T10_GRACE):
        return False
    closer = record.get("closed_by")
    login = (_login_of(closer) or "").lower() if isinstance(closer, dict) else ""
    tcfg = cfg if isinstance(cfg, dict) else {}
    return not (login and login in triage_our_logins(engine, tcfg))


class TriageState:
    """Memory only: rebuilt from GitHub and the decision list."""

    def __init__(self):
        self.wanted = 0          # #1140: units a pass wants read (repositories x units), for the skip line
        self.units = {}          # (repo, unit) -> {"at", "data", "truncated", "error"}
        self.unreadable = {}     # repo -> reason, audited once
        self.confirmed, self.attempted, self.alarmed = {}, {}, {}
        self.verified = {}       # (rule, ref) -> when a fresh read last gave an answer (true or false): the age of "unchecked"
        self.first_seen = {}     # (rule, ref) -> when a pass first saw it as a candidate (the age of one never checked)
        self.enriched = {}       # (rule, ref) -> evidence the fresh read added
        self.live = {}           # #576: (repo, number) -> {"token": the issue's updated time, "info": the label's age facts or None, "at"}: one timeline read each
        self.batch_waiting = {}  # #921: repo -> how many open issues wait for the batch (their change merged into the integration branch), this pass
        self.reasons = {}        # (rule, ref) -> the worded reason of a finding that carries one (`awaiting-live expired`)
        self.logged, self.woken_logged, self.facts, self.current = set(), set(), {}, {}
        self.started, self.day, self.day_counts = 0, None, {}
        self.summarised = None
        self.seen = None         # #349: the stale digest's memory (bin/triage_digest.py), loaded at the first pass
        self.meta = None         # #406: its cadence (warm-up done, the last delivery), kept in the same file


def triage_state(engine):
    state = getattr(engine, "triage", None)
    if state is None:
        state = engine.triage = TriageState()
    return state


def _login_of(user):
    return pr_login(user)


def triage_norm(issue):
    """One issue or PR record as the rules read it; None when it is not usable. Only reduced facts: no text is kept
    except the booleans computed from the body."""
    number = _number(issue)
    created, updated = parse_iso(issue.get("created_at")), parse_iso(issue.get("updated_at"))
    if number is None or created is None or updated is None:
        return None
    body = issue.get("body")
    summary = issue.get("sub_issues_summary") if isinstance(issue.get("sub_issues_summary"), dict) else {}
    total, done = summary.get("total"), summary.get("completed")
    return {"number": number, "pr": _is_pr(issue), "draft": bool(issue.get("draft")), "state": issue.get("state", "open"),
            "created": created, "updated": updated, "author": (_login_of(issue.get("user")) or "").lower(),
            "assignees": [(_login_of(a) or "").lower() for a in issue.get("assignees") or [] if isinstance(a, dict)],
            "labels": {(l.get("name") if isinstance(l, dict) else l) for l in issue.get("labels") or []},
            "section": sweep_has_section(body), "next_body": bool(isinstance(body, str) and TRIAGE_NEXT_RE.search(
                SWEEP_QUOTED_RE.sub(" ", body))), "parent": parent_of(issue),
            "standing": triage_body_standing(body, created),
            "parked_until": parked_until(body, {(l.get("name") if isinstance(l, dict) else l) for l in issue.get("labels") or []}),
            "owner_body": triage_names_owner(body), "owner_name": triage_owner_name(body),
            "open_sub": isinstance(total, int) and isinstance(done, int) and total > done,
            "pending_text": load_triage_plan_open().pending_text(body),   # #410: its own text says work is left
            "captain": is_captain({"labels": {(l.get("name") if isinstance(l, dict) else l) for l in issue.get("labels") or []},
                                   "body": body})}


def triage_body_standing(body, created):
    """(posted, state) from the standing line in an item's body, or None."""
    state = standing_line(SWEEP_QUOTED_RE.sub(" ", body), created) if isinstance(body, str) else None
    return (created, state) if state is not None else None


def triage_comment_facts(records):
    """[{"at", "next", "standing", "author", "review"}] oldest first from comment records; no text kept."""
    found = []
    for c in records:
        at = parse_iso(c.get("created_at")) if isinstance(c, dict) else None
        text = SWEEP_QUOTED_RE.sub(" ", c.get("body") or "") if at is not None and isinstance(c.get("body"), str) else ""
        if at is not None:
            found.append({"at": at, "next": bool(TRIAGE_NEXT_RE.search(text)), "standing": standing_line(text, at), "author": (_login_of(c.get("user")) or "").lower(), "review": triage_says_reviewed(text)})
    return sorted(found, key=lambda c: c["at"])


def triage_pending_proofs(engine, links, items, repo):
    """#410: the open issues of `links` whose linked merged PR still has a proof the ledger has not seen pass. The ledger is one local file, read only
    when a T12 candidate exists; an unreadable or tampered one makes every candidate unknown (suppressed this pass), never a finding."""
    candidates = {n: l for n, l in links.items() if n in items}
    if not candidates:
        return set()
    if engine.state_dir is None:
        return set()   # no state directory: no ledger exists
    try:
        entries, problem = plan_ledger.read_ledger(engine.state_dir)
    except Exception:
        return set(candidates)
    if problem:
        return set(candidates)
    return load_triage_plan_open().pending_issues(candidates, entries, repo)


def triage_context(engine, repo, items, discovery):
    """The charters, asks, work items and agent-owned numbers of one repo, from the agents list and the parent links the
    issue list carries (`parent_issue_url`): no read of their own."""
    owned, charters = set(), set()
    view = registry_charter_view(engine)   # #969: an orchestrator's charter is the registry's; its metadata `issue` is an ask, never a charter
    for agent in (discovery.agents.values() if discovery else []):
        fleet = agent.fleet or {}
        if _fleet_repo(fleet, engine.config) != repo and fleet.get("repo") != repo:
            continue
        mine = owned_numbers(agent.name, fleet, view)
        owned |= mine
        if fleet.get("role") == "orchestrator":
            charters |= mine
        elif isinstance(fleet.get("charter"), int) and not isinstance(fleet.get("charter"), bool):
            charters.add(fleet["charter"])
    charters |= {n for n, i in items.items() if any(str(l).endswith(":orchestrator") for l in i["labels"])}
    asks = {n for n, i in items.items() if i["parent"] in charters}
    below, unresolved = triage_charter_descendants(items, charters)
    return {"charters": charters, "asks": asks, "chain": charters | below, "owned": owned, "unresolved": unresolved}


TRIAGE_ANCESTOR_DEPTH = 8   # #396: how many parent links the owner walk follows (a deeper chain ends the walk)


def triage_charter_descendants(items, charters):
    """(numbers whose parent chain reaches a charter within TRIAGE_ANCESTOR_DEPTH links, numbers whose chain leaves the issues read first).
    Memory only: the parent links of the lists already held, so no read. A cycle or a too-deep chain ends the walk (neither). A parent that
    is not among the issues read (another repo, closed) makes the answer unknown, never a missing owner."""
    below, unresolved = set(), set()
    for start in items:
        seen, current = {start}, start
        for _ in range(TRIAGE_ANCESTOR_DEPTH):
            parent = items[current]["parent"]
            if parent is None:
                break                                  # the root of the chain: no charter above it
            if parent in charters:
                below.add(start)
                break
            if parent not in items:
                unresolved.add(start)
                break
            if parent in seen:
                break                                  # a cycle: the walk ends
            seen.add(parent)
            current = parent
    return below, unresolved


def triage_awaits_live(item):
    """True when an open issue carries a label ending in `:awaiting-live`: it waits on a live event, so T9 and T12 stay quiet (#574),
    until the label is older than `awaiting_live_days` with no comment since (#576)."""
    return bool(load_module("awaiting_live").label_names(item["labels"]))


def triage_rules(item, comments, ctx, sla, now, ours_logins, required, notice_prs, window_ok=True, captain_skip=False, merged=None, pending=None, live=None, live_days=None, waiting=None):
    """{rule: evidence} for one OPEN item (a pure function: the list copy and the fresh copy go through it alike), and
    whether the item is ours. `comments` is None when the comment unit is not read (T3 and T7 are then not judged).
    Evidence holds only labels' names, ages in days and owner logins."""
    number, found = item["number"], {}
    chain = number in ctx["chain"]
    ours = (item["author"] in ours_logins or any(a in ours_logins for a in item["assignees"]) or chain
            or number in ctx["owned"] or number in notice_prs)
    day = 86400.0
    age_created, age_updated = (now - item["created"]) / day, (now - item["updated"]) / day
    ask = number in ctx["asks"]
    if not item["pr"]:
        named = item.get("owner_body", False)   # an explicit `Owner: <name>` or `owned by <name>` line names the owner
        if not load_module("triage_ownerless").is_claimed(dict(item, owner_body=named), ctx) and age_created >= sla["t1_days"]:   # one definition of "has an owner" (#605)
            found["T1"] = {"created_days": round(age_created, 2)}
        group_missing = [g for g in required if not any(l in item["labels"] for l in (g if isinstance(g, list) else [g]))]
        if group_missing and age_created >= sla["t2_days"]:
            found["T2"] = {"missing": [g if isinstance(g, str) else "/".join(g) for g in group_missing],
                           "labels": sorted(item["labels"])[:8]}
        awaiting = any(str(l).endswith((":awaiting-user", ":awaiting-cos")) or "awaiting" in str(l) for l in item["labels"])
        parked = triage_parked_now(item, now) and not awaiting  # a park reaches at most PARKED_MAX_DAYS ahead: `9999-12-31` does not park forever. It never exempts an owner-wait; T5 and the captain PRs are other rules
        if comments is not None and not item["section"] and not awaiting and not item["next_body"] and not parked \
                and not item["open_sub"] and not any(c["next"] for c in comments) and not triage_standing_silent(item, comments, now, trusted=ours_logins) \
                and age_updated >= (sla["t3_ask_days"] if ask else sla["t3_other_days"]):
            found["T3"] = {"quiet_days": round(age_updated, 2), "ask": ask}
        if ask and any("blocked" in str(l).lower() for l in item["labels"]) and age_updated >= sla["t8_days"]:
            found["T8"] = {"quiet_days": round(age_updated, 2)}
        if ask and "T3" not in found and not triage_awaits_live(item) and age_updated >= sla["t9_days"]:
            found["T9"] = {"quiet_days": round(age_updated, 2)}
        if triage_awaits_live(item):   # #576: the exemption is capped: an old label with no comment since comes back, an unreadable age stays visible
            module = load_module("awaiting_live")
            reason = module.judge(live, now, live_days or module.CONFIG_DEFAULT)
            if reason:
                found["T9"] = {"quiet_days": round(age_updated, 2), "reason": reason}
                if live is not None:
                    found["T9"]["label_days"] = round((now - live["set"]) / day, 2)
        if not chain and not item["assignees"] and not named and "T1" not in found and number not in ctx["unresolved"] \
                and age_updated >= sla["t11_days"]:
            found["T11"] = {"quiet_days": round(age_updated, 2)}
        link = (merged or {}).get(number) if number not in ctx["charters"] and number not in ctx["asks"] else None   # a charter or an ask is a long-lived umbrella: a merged part does not finish it
        if number in (waiting or ()) and number not in ctx["charters"] and number not in ctx["asks"]:   # #921: its change merged into the integration branch and waits for the batch: not done yet, and it has an owner of record
            found.pop("T1", None)
            found.pop("T11", None)
        if link:   # #384: its PR merged and it is still open: that is the finding, not "no owner"; ours when the PR's author is ours
            found.pop("T1", None)
            found.pop("T11", None)
            if not (item["open_sub"] or item["pending_text"] or number in (pending or ()) or triage_awaits_live(item)
                    or (comments is not None and triage_standing_silent(item, comments, now, merged_batch=waiting is not None and number not in waiting, trusted=ours_logins))):   # #410: not truly done: an open sub-issue, pending work in its text or a proof not passed
                found["T12"] = load_triage_plan_open().evidence(link, max(0.0, now - (parse_iso(link["merged_at"]) or now)) / day)
                ours = ours or link["by"] in ours_logins
    elif not item["draft"] and comments is not None and ours and not captain_skip:
        # Only OUR PRs: our agents review by signed comment, which is in the data; other teams use formal GitHub reviews,
        # which the triage does not read, so a PR of theirs cannot be called unreviewed (it would be a false positive).
        limit = sla["t7_ours_hours"] * 3600
        if now - item["created"] >= limit and not any(c["review"] for c in comments):
            if now - item["created"] <= TRIAGE_COMMENT_DAYS * day:   # older: the review may be outside the window
                found["T7"] = {"waiting_hours": round((now - item["created"]) / 3600, 1)}
    return found, ours


def triage_read_unit(hub, repo, unit, now, budget):
    """One unit's list read, charged what it used. Units: issues (open issues and PRs), comments (the last
    TRIAGE_COMMENT_DAYS), closed:<label> (closed issues that still carry a label)."""
    if unit == "issues":
        path = "repos/%s/issues?state=open&per_page=100" % repo
        records, cut = hub.read_pages(path, cap=TRIAGE_LIST_PAGES)
    elif unit == "merged":   # #384: ONE page of the recently closed pull requests; those with a merge date name the issues they implemented
        records, cut = hub.read_pages("repos/%s/pulls?state=closed&sort=updated&direction=desc&per_page=%d" % (repo, TRIAGE_MERGED_PER_PAGE), cap=1)
    elif unit == "comments":
        since = iso(now - TRIAGE_COMMENT_DAYS * 86400)
        records, cut = hub.read_pages("repos/%s/issues/comments?since=%s&sort=created&direction=desc&per_page=100"
                                      % (repo, since), older=("created_at", since), cap=TRIAGE_LIST_PAGES)
    else:
        label = unit.split(":", 1)[1]
        records, cut = hub.read_pages("repos/%s/issues?state=closed&labels=%s&per_page=100" % (repo, quote(label, safe="")),
                                      cap=1)
    budget[0] -= sweep_pages_used(records, cut)
    return records, cut


def triage_refresh(engine, state, repos, units, now, budget, reserve=1, seconds=TRIAGE_PASS_SECONDS):
    """Read the stalest units (round-robin across every repo and unit, the least recently read first) while the pass's
    call budget (less the reserve for fresh checks) and time allow. A repo the account cannot read is `unreadable`, once."""
    hub = getattr(engine, "hub", None)
    if hub is None:
        return
    todo = []
    for repo in repos:
        if repo in state.unreadable and now - state.unreadable[repo][1] < TRIAGE_UNREADABLE_RETRY:
            continue
        for unit in units:
            held = state.units.get((repo, unit))
            if held is None or now - held["at"] >= TRIAGE_TTL:
                todo.append(((held or {"at": 0})["at"], repo, unit))
    alerts = set(((engine.config.get("triage") or {}).get("alert_labels") or {}) if isinstance(engine.config.get("triage"), dict) else ())
    # #1140: the not-yet-read `issues` unit of an alert repository goes first (one list per such repository), then the old order
    for _, repo, unit in sorted(todo, key=lambda t: (not (t[0] == 0 and t[2] == "issues" and t[1] in alerts),) + t):
        if budget[0] - reserve < 1 or engine.now() - state.started >= seconds:
            break
        try:
            records, cut = triage_read_unit(hub, repo, unit, now, budget)
            state.units[(repo, unit)] = {"at": now, "data": records, "truncated": cut, "error": None}
            state.unreadable.pop(repo, None)
        except GitHubUnavailable as err:
            budget[0] -= 1
            reason = _one_line(str(err), 160)
            if (repo, unit) in state.units:   # it was read before: a failed refresh keeps the old copy and tries later
                state.units[(repo, unit)].update(at=now, error=reason)
            else:
                if repo not in state.unreadable:
                    engine._note("triage.unreadable", repo, {"repo": repo, "unit": unit, "reason": reason})
                state.unreadable[repo] = (reason, now)


def triage_read_live(engine, state, repos, now, budget, reserve=1, seconds=TRIAGE_PASS_SECONDS):
    """#576: the age of each `:awaiting-live` label, from the issue's timeline: ONE bounded read (a single page) per labelled open issue, kept in
    `state.live` while the issue's updated time is the one it was read at (a re-added label or a new comment moves it), so an unchanged issue is not read
    again. Each read is charged to the pass's call budget (less the reserve for fresh checks), the least recently read first. A failed or cut read is kept as
    unknown and tried again after TRIAGE_TTL: never read as fresh. Reads nothing when no issue carries the label."""
    hub, module = getattr(engine, "hub", None), load_module("awaiting_live")
    if hub is None:
        return
    todo = []
    for repo in repos:
        unit = state.units.get((repo, "issues"))
        for record in (unit["data"] if unit else []):
            item = triage_norm(record)
            if item is None or item["pr"] or item["state"] != "open" or not module.label_names(item["labels"]):
                continue
            held = state.live.get((repo, item["number"]))
            if held is None or held["token"] != item["updated"] or (held["info"] is None and now - held["at"] >= TRIAGE_TTL):
                todo.append(((held or {"at": 0})["at"], repo, item))
    for _, repo, item in sorted(todo, key=lambda t: t[:2] + (t[2]["number"],)):
        if budget[0] - reserve < 1 or engine.now() - state.started >= seconds:
            break
        budget[0] -= 1
        info = None
        try:
            events, cut = hub.read_pages("repos/%s/issues/%d/timeline?per_page=100" % (repo, item["number"]), cap=1)
            if not cut:   # a page that is not the whole timeline cannot tell the latest label event or the last comment
                info = module.read(events, module.label_names(item["labels"]), parse_iso)
        except GitHubUnavailable:
            pass
        state.live[(repo, item["number"])] = {"token": item["updated"], "info": info, "at": now}


def triage_findings(engine, state, repos, decisions, cfg, now, errors):
    """(candidates [(rule, ref, evidence)], unchecked {repo: [units missing]}, truncated repos). Reads nothing."""
    discovery = getattr(engine, "discovery", None)
    ours_logins = triage_our_logins(engine, cfg)
    notice_prs = {}
    try:
        for n in read_pr_notices(engine.state_dir):
            notice_prs.setdefault(n["repo"], set()).add(n["number"])
    except (NoticeError, OSError, TypeError):
        pass
    out, unchecked, truncated = [], {}, []
    state.batch_waiting = {}   # #921: repo -> open plan issues whose change merged into the integration branch and waits for the batch (counted apart)
    agents_whole = whole(engine, "agents")[0]
    live_days = load_module("awaiting_live").days_of(engine.config)
    captain_listed = {sweep_ref_of(d, engine.config.get("intent_repo")) for d in decisions if d.get("kind") == "captain"}
    for repo in repos:
        issues_unit = state.units.get((repo, "issues"))
        if issues_unit is None:
            continue
        if issues_unit["truncated"]:
            truncated.append(repo)
        items = {}
        for record in issues_unit["data"]:
            item = triage_norm(record)
            if item is not None and item["state"] == "open":
                items[item["number"]] = item
        ctx = triage_context(engine, repo, items, discovery)
        comments_unit = state.units.get((repo, "comments"))
        by_number = {}
        if comments_unit is not None:
            for c in comments_unit["data"]:
                n = _url_number(c.get("issue_url"))
                if n is not None:
                    by_number.setdefault(n, []).append(c)
        merged_unit = state.units.get((repo, "merged"))   # #384: not read yet (or no unit): no T12, never a guess
        links = load_triage_plan_open().merged_links(merged_unit["data"], repo) if merged_unit is not None else {}
        integration = cfg.get("integration_branch")   # #921: the only place the integration branch is named: the configuration (none: judged as before)
        links, waiting = load_triage_plan_open().split_batch_waiting(merged_unit["data"] if merged_unit is not None else [], links, integration if isinstance(integration, str) else None)
        state.batch_waiting[repo] = sum(1 for n in waiting if n in items and not items[n]["pr"] and n not in ctx["charters"] and n not in ctx["asks"])
        pending = triage_pending_proofs(engine, links, items, repo)
        sla, required = triage_sla(cfg, repo), (cfg.get("required_labels") or {}).get(repo) or []
        if comments_unit is None:
            unchecked.setdefault(repo, []).append("comments")
        for number, item in sorted(items.items()):
            comments = triage_comment_facts(by_number.get(number, [])) if comments_unit is not None else None
            # a `MERGE: captain` PR already in the decision list is T6's; while the list is incomplete a captain PR waits
            captain_skip = item["captain"] and ((repo, number) in captain_listed or bool(errors))
            live = state.live.get((repo, number)) if triage_awaits_live(item) else None
            found, ours = triage_rules(item, comments, ctx, sla, now, ours_logins, required, notice_prs.get(repo, set()),
                                       captain_skip=captain_skip, merged=links, pending=pending,
                                       live=live["info"] if live else None, live_days=live_days, waiting=waiting)
            if not agents_whole:   # #805: "no owner" is judged against the running agents: a fleet that was not read whole is no evidence of an absence
                for rule in [r for r in found if "agents" in (triage_inputs(r) or ())]:
                    found.pop(rule)
                    whole_skipped(engine, "triage.owner_rules", "agents")
            if not found:
                continue
            ref = "%s#%d" % (repo, number)
            if ours:
                out.extend((rule, ref, evidence) for rule, evidence in sorted(found.items()))
            else:   # another person's or team's: only ever a flag, never counted as our violation
                out.append(("OO", ref, {"would_be": sorted(found)}))
        for unit in ("closed:" + l for l in sorted(triage_awaiting_labels(engine))):
            held = state.units.get((repo, unit))
            if held is None:
                unchecked.setdefault(repo, []).append(unit)
                continue
            for record in held["data"]:
                number = _number(record)
                names = {(l.get("name") if isinstance(l, dict) else l) for l in record.get("labels") or []}
                if (number is not None and record.get("state") == "closed" and unit.split(":", 1)[1] in names
                        and not triage_t10_graced(engine, cfg, record, now)):   # the label usually comes off minutes after the close
                    out.append(("T10", "%s#%d" % (repo, number), {"label": unit.split(":", 1)[1]}))
    if not errors:   # T5, T6 read only the decision list: judged only when it is whole
        for d in decisions:
            since = parse_iso(d.get("since"))
            if d.get("tier") != "human" or d.get("kind") not in ("issue", "report", "captain") or since is None:
                continue
            ref = sweep_ref_of(d, engine.config.get("intent_repo"))
            ident = "%s#%d" % ref if ref and ref[0] else (d.get("id") or "?")
            days = (now - since) / 86400
            if d.get("kind") == "captain" and days >= triage_sla(cfg, ref[0] if ref else "")["t6_days"]:
                evidence = {"waiting_days": round(days, 2), "waiting_hours": round(days * 24, 1)}
                if d.get("holds"):
                    evidence["holds"] = list(d["holds"])   # the hold-* labels: why it is held
                out.append(("T6", ident, evidence))
            elif d.get("kind") != "captain" and days >= triage_sla(cfg, ref[0] if ref else "")["t5_days"]:
                out.append(("T5", ident, {"waiting_days": round(days, 2), "waiting_hours": round(days * 24, 1)}))
    return out, unchecked, truncated


def triage_awaiting_labels(engine):
    return {awaiting_label(engine.config), awaiting_cos_label(engine.config)}


def triage_verify(engine, state, rule, ref, cfg, now, budget, evidence=None):
    """True when a FRESH read bears the finding out, False when it does not, None when it cannot be checked now. T5 and
    T6 are about the decision list derived this very pass (no read)."""
    if rule in ("T5", "T6"):
        return True
    hub = getattr(engine, "hub", None)
    repo, _, number = ref.rpartition("#")
    if hub is None or not number.isdigit():
        return None
    wants_comments = rule in ("T3", "T7") or (rule == "OO" and {"T3", "T7"} & set((evidence or {}).get("would_be", [])))
    number, need = int(number), (2 if wants_comments else 1)
    if rule == "T7":
        return triage_verify_t7(engine, state, repo, number, cfg, now, budget)
    if budget[0] < need:
        return None
    budget[0] -= 1
    try:
        data = hub._read_one(repo, "issues/%d" % number, now, fresh=True)
    except GitHubUnavailable:
        return None
    if not isinstance(data, dict) or "number" not in data and "state" not in data:
        return None
    if rule == "T10":
        names = {(l.get("name") if isinstance(l, dict) else l) for l in data.get("labels") or []}
        return (data.get("state") == "closed" and any(l in names for l in triage_awaiting_labels(engine))
                and not triage_t10_graced(engine, cfg, data, now))
    if data.get("state") != "open":
        return False
    if rule == "T12":   # #384: the merged PR was read in the list; the issue still being open is what a fresh read confirms
        return True
    item = triage_norm(data)
    if item is None:
        return None
    comments = None
    if need == 2:
        budget[0] -= 1
        since = iso(now - TRIAGE_COMMENT_DAYS * 86400)
        try:
            recent, cut = hub.read_pages("repos/%s/issues/%d/comments?since=%s&per_page=100" % (repo, number, since), cap=1)
        except GitHubUnavailable:
            return None
        if cut:
            return None
        comments = triage_comment_facts(recent)
    issues_unit = state.units.get((repo, "issues"))
    items = {}
    for record in (issues_unit["data"] if issues_unit else []):
        other = triage_norm(record)
        if other is not None and other["state"] == "open":
            items[other["number"]] = other
    items[number] = item
    ctx = triage_context(engine, repo, items, getattr(engine, "discovery", None))
    notice_prs = set()
    try:
        notice_prs = {n["number"] for n in read_pr_notices(engine.state_dir) if n["repo"] == repo}
    except (NoticeError, OSError, TypeError):
        pass
    live = state.live.get((repo, number)) if triage_awaits_live(item) else None   # #576: the age read this or an earlier pass; none read is "unknown", still a finding
    found, ours = triage_rules(item, comments, ctx, triage_sla(cfg, repo), now, triage_our_logins(engine, cfg),
                               (cfg.get("required_labels") or {}).get(repo) or [], notice_prs,
                               live=live["info"] if live else None, live_days=load_module("awaiting_live").days_of(engine.config))
    return (rule in found) if (ours and rule != "OO") else (rule == "OO" and bool(found))


def triage_verify_t7(engine, state, repo, number, cfg, now, budget):
    """T7 against the captain decision's own evidence (`captain_evidence`, built on `pr_reviews_at` and `pr_signed_at`): a
    formal review by a non-author at the exact head, or a signed head-naming comment from a non-author agent that is not a
    sender of the PR's notice, means REVIEWED: no finding. A review at an OLDER head only fires once the last push is older
    than the T7 SLA, with the evidence 'review at an older head'. True/False/None as `triage_verify`."""
    if budget[0] < 1:
        return None
    budget[0] -= 1
    try:
        facts = read_pr_facts(engine, repo, number, "commits")
    except NoticeError:
        return None
    if facts["state"] != "open":
        return False
    if facts.get("verdict_review"):
        return False         # the independent reviewer's verdict review of this exact head, from any account (T7 only)
    senders, noticed = set(), False
    try:
        for item in read_pr_notices(engine.state_dir):
            if item["repo"] == repo and item["number"] == number:
                senders |= set(notice_senders(item))
                noticed = True
    except (NoticeError, OSError, TypeError):
        noticed = False
    # A signed comment is review evidence only when the PR's notice is on file (its sender is then known and excluded):
    # with none, the author's own signed comment would pass for a review. Without a notice only a formal review counts.
    if noticed:
        evidence = captain_evidence(facts, senders)
    else:
        evidence = facts["reviews"] if facts.get("reviews") is None else [dict(r, kind="review") for r in facts["reviews"]]
        if evidence is not None and not evidence:
            # No notice to name the author's agent: only a comment that SAYS it is a review (not merely names the head) counts, and
            # the PR's own author login never does.
            reviewers = [who for who in (facts.get("signed_review") or []) if who != facts.get("author")]
            if reviewers:
                evidence = [{"by": who, "kind": "comment", "state": "signed review comment"} for who in reviewers[:CAPTAIN_EVIDENCE_MAX]]
    if evidence is None:
        return None          # reviews or comments could not be read: no finding either way
    if evidence:
        return False         # reviewed at the exact head, in the fleet's own flow
    note = {} if noticed else {"notice": "no notice on file"}
    if facts.get("reviews_older"):
        pushed = facts.get("head_at")
        hours = cfg and triage_sla(cfg, repo)["t7_ours_hours"]
        if pushed is None or now - pushed < hours * 3600:
            return False     # reviewed before the last push, and the push is not older than the SLA yet
        note.update(review="review at an older head", since_push_hours=round((now - pushed) / 3600, 1))
    if note:
        state.enriched[("T7", "%s#%d" % (repo, number))] = note
    return True


def triage_ownerless_options(cfg):
    """The `triage.ownerless` settings with their defaults, or None when it is off."""
    own = (cfg or {}).get("ownerless") if isinstance(cfg, dict) else None
    if not isinstance(own, dict) or not own.get("repos"):
        return None
    module = load_module("triage_ownerless")
    return {"repos": list(own["repos"]), "label_owners": {r: dict((e or {}).get("label_owners") or {}) for r, e in own["repos"].items()} if isinstance(own["repos"], dict) else {},
            "days": own.get("days", module.DEFAULT_DAYS), "limit": own.get("limit", module.DEFAULT_LIMIT),
            "owner_labels": list(own.get("owner_labels") or module.DEFAULT_OWNER_LABELS)}


def triage_read_ownerless(hub, repo, options, budget):
    """(records, cut, total) of one team repository: every open item, page by page to `limit` items, and the repository's own count of open items (one more read) for
    `read X of Y`. Charged to the pass's call budget."""
    records, cut = hub.read_pages("repos/%s/issues?state=open&sort=updated&direction=asc&per_page=100" % repo, cap=max(1, -(-options["limit"] // 100)))   # the LEAST recently updated first: a cut keeps the stale ones this line exists for (the triage's own shared read is unchanged)
    budget[0] -= sweep_pages_used(records, cut) + 1
    total = None
    try:
        total = hub.open_issues_count(repo)
    except GitHubUnavailable:
        total = None
    if total is None and not cut:
        total = len(records)
    return records, cut, total


def triage_refresh_ownerless(engine, state, cfg, now, budget, seconds):
    """Read the team repositories' whole lists (the stalest first) while the call budget and time allow. A failed read keeps the old copy."""
    options, hub = triage_ownerless_options(cfg), getattr(engine, "hub", None)
    if options is None or hub is None:
        return
    todo = sorted((state.units.get((repo, "ownerless"), {"at": 0})["at"], repo) for repo in options["repos"]
                  if repo not in state.unreadable and now - state.units.get((repo, "ownerless"), {"at": 0})["at"] >= TRIAGE_TTL)
    for _, repo in todo:
        if budget[0] < 2 or engine.now() - state.started >= seconds:
            return
        try:
            records, cut, total = triage_read_ownerless(hub, repo, options, budget)
            state.units[(repo, "ownerless")] = {"at": now, "data": records, "truncated": cut, "total": total, "error": None}
        except GitHubUnavailable as err:
            budget[0] -= 1
            if (repo, "ownerless") in state.units:   # it was read before: a failed refresh keeps the old copy and tries again after the usual wait
                state.units[(repo, "ownerless")].update(at=now, error=_one_line(str(err), 160))
            else:
                state.units[(repo, "ownerless")] = {"at": now - TRIAGE_TTL + 60, "data": [], "truncated": False, "total": None, "error": _one_line(str(err), 160), "empty": True}


OWNERLESS_FILE = "triage-ownerless.json"   # {repo: {"at": epoch}}: when each repository's daily line was last sent (private; survives a restart)


def triage_ownerless_pass(engine, state, cfg, now):
    """#605: once a day per team repository, ONE line to the Chief of Staff and to the orchestrators of that repository: how many open issues have no owner and no
    activity for N days, how old the oldest is, the five oldest as links, and how much of the list was read. Counts, ages and links only; nothing is written to any issue.
    Returns {repo: summary} for the report. Never raises."""
    options = triage_ownerless_options(cfg)
    if options is None or engine.state_dir is None:
        return {}
    if whole_skipped(engine, "triage.ownerless", "agents"):
        return {}   # #805: an issue is ownerless when no running agent owns it: a fleet that was not read whole is no evidence of that
    module, shown = load_module("triage_ownerless"), {}
    try:
        discovery, ours = getattr(engine, "discovery", None), triage_our_logins(engine, cfg)
        for repo in options["repos"]:
            unit = state.units.get((repo, "ownerless"))
            if unit is None or unit.get("empty") or unit.get("error") and not unit["data"]:
                continue
            items = {}
            for record in unit["data"]:
                item = triage_norm(record)
                if item is not None and item["state"] == "open":
                    items[item["number"]] = item
            lanes = {name.lower() for name, agent in (discovery.agents.items() if discovery else [])   # a known lane: an orchestrator the daemon holds for the repository
                     if (agent.fleet or {}).get("role") == "orchestrator" and (_fleet_repo(agent.fleet or {}, engine.config) == repo or (agent.fleet or {}).get("repo") == repo)}
            rows, parked = module.ownerless_split(items, triage_context(engine, repo, items, discovery), ours, options["owner_labels"], now, options["days"],
                                                  options["label_owners"].get(repo), lanes, triage_parked_now)   # #889: a future-parked issue is counted apart, never listed
            read, total = len(unit["data"]), unit.get("total")
            shown[repo] = {"count": len(rows), "parked": len(parked), "read": read, "total": total, "oldest_days": int(rows[0][1]) if rows else None}
            if not rows:
                continue
            sent = _read_small(engine.state_dir, OWNERLESS_FILE)
            if now - sent.get(repo, {}).get("at", 0) < 86400:
                continue
            sent[repo] = {"at": int(now)}
            _write_small(engine.state_dir, OWNERLESS_FILE, sent)   # before the send: at most one line a day, even when a send fails
            line = module.summary_line(repo, rows, read, total, options["days"], unit.get("truncated") is True, len(parked))
            names = ["chief-of-staff"] + sorted(name for name, agent in (discovery.agents.items() if discovery else [])
                                                if _is_orchestrator(agent) and _fleet_repo(agent.fleet or {}, engine.config) == repo)
            told = []
            for name in names:
                try:
                    outcome = engine.send("switchboard", name, line)
                    if not (isinstance(outcome, dict) and outcome.get("sent") is False):
                        told.append(name)
                except Exception as err:   # a failed send is audited, never retried into a second line
                    engine._note("triage.ownerless_send_failed", repo, {"to": name, "error": _one_line(str(err), 120)})
            engine._note("triage.ownerless", repo, {"count": len(rows), "parked": len(parked), "read": read, "total": total, "told": told})
    except Exception as err:
        engine._note("triage.ownerless_error", "", {"error": _one_line("%s: %s" % (type(err).__name__, err), 160)})
    return shown


def read_mark(engine, name, whole, reason, now):
    """Record that the input `name` was (or was not) read whole in this pass: no error, not cut by a budget, not provisional.
    The record lives in memory only, so a restart leaves every input not whole until its first whole read (#805)."""
    reads = engine.__dict__.setdefault("read_whole", {})
    reads[name] = {"whole": bool(whole), "reason": "" if whole else (reason or "not read whole"), "at": now}


def whole(engine, name):
    """(True, "") when `name` was read whole in the latest pass, else (False, why). Never read yet in this process is not whole."""
    held = getattr(engine, "read_whole", {}).get(name)
    if held is None:
        return False, "not read yet in this process"
    return held["whole"], held["reason"]


def whole_skipped(engine, consumer, name):
    """One debug audit line, at most once per pass and consumer, saying a destructive step was skipped because `name` was not whole."""
    ok, why = whole(engine, name)
    if ok:
        return False
    at = engine.read_whole.get(name, {}).get("at") if getattr(engine, "read_whole", None) else None
    said = engine.__dict__.setdefault("read_whole_said", {})
    if said.get((consumer, name)) != at:
        said[(consumer, name)] = at
        state = getattr(engine, "triage", None)
        if name.startswith("issues:") and state is not None and getattr(state, "wanted", 0):
            why = "%s (%d of %d units read)" % (why, sum(1 for u in state.units.values() if u["error"] is None), state.wanted)   # #1140: how far the cold start has got
        engine._note("whole.skipped", None, {"consumer": consumer, "input": name, "why": why})
    return True


def mark_decisions_read(engine, errors, now):
    """The decision list is whole only with no error and a notice read that finished whole: a noticed PR the reader has no facts for
    derives no held-PR row, so its absence is no evidence it left. Never inherited: an input no read marked, or whose read raised, is not whole."""
    notices, notices_why = whole(engine, "notices")
    why = "the decision list had errors" if errors else ("" if notices else notices_why)
    read_mark(engine, "decisions", not errors and notices, why, now)


def triage_mark_unit(engine, state, name, repo, units, now):
    """Mark `<name>:<repo>` from the held units of that kind: whole only when every one is held, without error and not cut."""
    bad = None
    for unit_name in units:
        unit = state.units.get((repo, unit_name))   # a unit never read, a failed read or a cut list is no evidence of an absence
        if unit is None:
            bad = "%s not read yet" % unit_name
        elif unit.get("error"):
            bad = "the last read of %s failed: %s" % (unit_name, unit["error"])
        elif unit.get("truncated"):
            bad = "the %s list was cut" % unit_name
        if bad:
            break
    read_mark(engine, "%s:%s" % (name, repo), bad is None, bad or "", now)


def triage_mark_reads(engine, state, repos, errors, now):
    """The inputs the triage rules judge, marked from the reads this pass already made (no new read)."""
    mark_decisions_read(engine, errors, now)
    closed = ["closed:" + l for l in sorted(triage_awaiting_labels(engine))]
    for repo in repos:
        triage_mark_unit(engine, state, "issues", repo, ["issues"], now)
        triage_mark_unit(engine, state, "comments", repo, ["comments"], now)
        triage_mark_unit(engine, state, "merged", repo, ["merged"], now)
        triage_mark_unit(engine, state, "closed", repo, closed, now)


def triage_owner_tier_t6(engine, decisions):
    """{("T6", ref)} for the merge-held PR decisions already at the owner's tier."""
    out = set()
    for d in decisions:
        if d.get("kind") == "captain" and d.get("tier") == "human":
            ref = sweep_ref_of(d, engine.config.get("intent_repo"))
            if ref and ref[0]:
                out.add(("T6", "%s#%d" % ref))
    return out


def triage_pass(engine, decisions, now, errors=()):
    """The triage's whole pass: refresh a few units, judge, re-check each candidate against a fresh read, audit, count,
    and (only when enabled) queue ONE batched fact for the Chief of Staff and the daily summary. Writes nothing to GitHub."""
    cfg = engine.config.get("triage", TRIAGE_DEFAULTS)
    if cfg is False or cfg is None:
        return
    repos = triage_repos(engine, cfg)
    if not repos:
        return  # nothing is tracked: nothing to read, nothing to say
    state = triage_state(engine)
    state.started = engine.now()
    budget = [cfg.get("calls_per_pass", TRIAGE_CALLS_PER_PASS)]
    seconds = cfg.get("pass_seconds", TRIAGE_PASS_SECONDS)
    units = ["issues", "comments", "merged"] + ["closed:" + l for l in sorted(triage_awaiting_labels(engine))]
    state.wanted = len(repos) * len(units)
    triage_refresh(engine, state, repos, units, now, budget, max(1, budget[0] // 3), seconds)  # a third kept for fresh checks
    triage_read_live(engine, state, repos, now, budget, max(1, cfg.get("calls_per_pass", TRIAGE_CALLS_PER_PASS) // 3), seconds)
    triage_refresh_ownerless(engine, state, cfg, now, budget, seconds)   # #605: the team repositories' whole lists, after the shared units
    try:
        plan_wake_pass(engine, state, repos, now, budget)   # #765: the plans posted or edited, from the units just held
    except Exception as err:
        engine._note("plan_wake.error", "", {"error": _one_line("%s: %s" % (type(err).__name__, err), 160)})
    triage_mark_reads(engine, state, repos, errors, now)
    try:
        alert_wake_pass(engine, state, cfg, now)   # #851: an alert issue wakes its repository's orchestrator once, from the lists just held
    except Exception as err:
        engine._note("alert_wake.error", "", {"error": _one_line("%s: %s" % (type(err).__name__, err), 160)})
    candidates, unchecked_units, truncated = triage_findings(engine, state, repos, decisions, cfg, now, errors)
    findings, unchecked, unchecked_other, oldest = [], 0, 0, 0.0
    why = {}   # #605: repo -> {reason: count}, and its other-owner share and oldest age, for the report

    def verify(rule, ref, evidence):
        """True/False/None for one finding; keeps the confirmed, verified and attempted records."""
        pair = (rule, ref)
        state.attempted[pair] = now
        verdict = triage_verify(engine, state, rule, ref, cfg, now, budget, evidence)
        if verdict is not None:
            state.verified[pair] = now
        if verdict:
            state.confirmed[pair] = now
        elif verdict is False:
            state.confirmed.pop(pair, None)
            if (pair, "d", int(now // 86400)) not in state.logged:
                state.logged.add((pair, "d", int(now // 86400)))
                engine._note("triage.dismissed", ref, {"rule": rule, "ref": ref, "why": "a fresh read does not bear it out"})
        return verdict

    # Our own findings first, the other-owner flags (which nobody acts on) after them: they must not use the budget our findings
    # need. Within each, the least recently attempted first, so every one is reached within a bounded number of passes.
    for rule, ref, evidence in sorted(candidates, key=lambda c: (c[0] == "OO", state.attempted.get((c[0], c[1]), 0), c[0], c[1])):
        pair = (rule, ref)
        state.first_seen.setdefault(pair, now)
        if now - state.confirmed.get(pair, 0) < TRIAGE_TTL:
            findings.append((rule, ref, evidence))
            continue
        verdict = None
        if engine.now() - state.started < seconds:
            verdict = verify(rule, ref, evidence)
        if verdict:
            findings.append((rule, ref, dict(evidence, **state.enriched.pop(pair, {}))))
        elif verdict is None:
            unchecked += 1
            unchecked_other += rule == "OO"
            oldest = max(oldest, now - state.verified.get(pair, state.first_seen[pair]))
            repo_of = ref.rpartition("#")[0]
            reason = "call budget" if (budget[0] < 1 or engine.now() - state.started >= seconds) else "failed read"
            entry = why.setdefault(repo_of, {"reasons": {}, "other_owner": 0, "oldest": 0.0})
            entry["reasons"][reason] = entry["reasons"].get(reason, 0) + 1
            entry["other_owner"] += rule == "OO"
            entry["oldest"] = max(entry["oldest"], now - state.verified.get(pair, state.first_seen[pair]))
    findings.sort()
    day = int(now // 86400)
    counts, other_owner = {}, 0
    for rule, ref, evidence in findings:
        if isinstance(evidence, dict) and evidence.get("reason"):
            state.reasons[(rule, ref)] = evidence["reason"]
        if rule == "OO":
            other_owner += 1   # a flag, never counted as one of our findings
        else:
            counts[rule] = counts.get(rule, 0) + 1
    for repo_of, units_missing in unchecked_units.items():   # a unit not read yet is a finding that could not be judged
        if units_missing:
            why.setdefault(repo_of, {"reasons": {}, "other_owner": 0, "oldest": 0.0})["reasons"]["not read yet"] = len(units_missing)
    for repo_of in repos:
        if (repo_of, "issues") not in state.units and repo_of not in state.unreadable:
            why.setdefault(repo_of, {"reasons": {}, "other_owner": 0, "oldest": 0.0})["reasons"]["not read yet"] = why.get(repo_of, {}).get("reasons", {}).get("not read yet", 0) + 1
    ownerless = triage_ownerless_pass(engine, state, cfg, now)
    state.current = {"enabled": cfg["enabled"], "counts": counts, "total": sum(counts.values()),
                     "other_owner": other_owner, "at": iso(now),
                     "unchecked": unchecked, "unchecked_other_owner": unchecked_other,
                     "waiting_for_batch": sum(state.batch_waiting.values()),
                     "oldest_unchecked_seconds": int(oldest), "unchecked_units": {r: sorted(u) for r, u in unchecked_units.items()},
                     "unreadable": sorted(state.unreadable), "truncated": sorted(truncated), "repos": len(repos),
                     "digest_interval_minutes": cfg.get("digest_interval_minutes", 60)}
    if why:
        state.current["unchecked_why"] = {r: {"reasons": v["reasons"], "other_owner": v["other_owner"], "oldest_seconds": int(v["oldest"])} for r, v in sorted(why.items())}
    if ownerless:
        state.current["ownerless"] = ownerless
    held = getattr(engine, "plan_wake", None)
    if held is not None and held.unapproved:   # #765: the plans that look unapproved, one status line (a hint: see docs/switchboard.md)
        state.current["plans_unapproved"] = held.unapproved[:load_triage_plan_open().MAX_LISTED]
        state.current["plans_unapproved_more"] = max(0, len(held.unapproved) - load_triage_plan_open().MAX_LISTED)
    triage_write(engine, state)
    flagged = [(r, ref) for r, ref, _ in findings if r == "OO" and now - state.alarmed.get((r, ref), 0) >= cfg["realarm_hours"] * 3600]
    owner_t6 = triage_owner_tier_t6(engine, decisions)
    due, invalid = triage_digest_due(engine, state, [(r, ref) for r, ref, _ in findings if r != "OO"],
                                     [(r, ref) for r, ref, _ in candidates if r != "OO"], cfg, now, owner_t6)
    triage_logged_seed(state, due, cfg, now, day)   # #849: before the commit below rewrites the memory
    if cfg["enabled"] and cfg.get("backoff", True):   # #406: warm up first, then at most one digest per interval; a held digest keeps its memory untouched
        before = state.meta["swept"]
        open_units = sum(len(u) for u in unchecked_units.values()) + sum(1 for r in repos if (r, "issues") not in state.units and r not in state.unreadable)   # a list not read yet is a finding not checked
        if not state.meta["swept"] and getattr(state, "warming_since", None) is None:
            state.warming_since = now
        allowed, digest = triage_digest.gate(state.meta, now, unchecked + open_units, cfg.get("digest_interval_minutes", 60) * 60,
                                             getattr(state, "warming_since", None))
        state.current["digest"] = digest
        triage_write(engine, state)
        if state.meta["swept"] != before:
            triage_digest.write_seen(engine.state_dir, state.seen, state.meta)
        if not allowed:
            due, invalid = [], []
    rolled = None
    if cfg["enabled"] and state.meta is not None:   # #766: the one daily line for T5 items listed before and still in the owner's tier
        rolled = triage_digest.rollup(state.seen, state.meta, [(r, ref) for r, ref, _ in findings if r == "T5" or (r, ref) in owner_t6], now)
        if rolled:
            state.facts["triage.rollup:%d" % int(now)] = (rolled, now)
            triage_digest.cost_record(engine.state_dir, now, rolled)
            engine._note("triage.rollup", None, {"text": rolled, "at": iso(now)})
            triage_digest.write_seen(engine.state_dir, state.seen, state.meta)
    if cfg["enabled"] and (due or invalid):   # never a wake solely for other-owner flags: they ride along when a wake happens anyway
        state.carried = set()
        text = triage_queue_fact(state, due, now, cfg, len(flagged), invalid)
        triage_digest.cost_record(engine.state_dir, now, text)
        state.meta["last"] = now
        triage_digest_commit(engine, state, due[:cfg["max_findings"]], invalid, now)
        if state.current.get("digest", {}).get("state") == "ready" and cfg.get("digest_interval_minutes", 60) > 0:
            state.current["digest"] = {"state": "held", "next_at": now + cfg["digest_interval_minutes"] * 60}
            triage_write(engine, state)
        for pair in flagged:
            state.alarmed[pair] = now
        for rule, _ in due:
            state.day_counts[rule] = state.day_counts.get(rule, 0) + 1
        woken = set(state.carried) | set(flagged)   # #849: what the digest carried, and the other-owner flags that rode along (counted in its text, remembered below)
    else:
        woken = set()
    for rule, ref, evidence in findings:   # #849: the audit line says what was done: woken only when this pass queued a wake for it
        once = (rule, ref, day)
        if once not in state.logged or ((rule, ref) in woken and once not in state.woken_logged):
            if len(state.logged) > 20000:
                state.logged.clear()
                state.woken_logged.clear()
            state.logged.add(once)
            if (rule, ref) in woken:
                state.woken_logged.add(once)
            engine._note("triage.finding", ref, {"rule": rule, "ref": ref, "what": TRIAGE_KINDS[rule],
                                                 "evidence": evidence, "woken": (rule, ref) in woken, "applied": False})
    triage_daily_summary(engine, state, cfg, now, day, repos)


def triage_logged_seed(state, due, cfg, now, day):
    """Once per process: what the persisted digest memory listed inside the re-alarm window (and is not due again now) was
    already audited before the restart, so it is not logged again (#849). Needs the memory loaded."""
    if getattr(state, "logged_seeded", False) or state.seen is None:
        return
    state.logged_seeded = True
    again = set(due)
    for key, rec in state.seen.items():
        rule, _, ref = key.partition(" ")
        if rule in TRIAGE_KINDS and (rule, ref) not in again and now - rec["at"] < cfg["realarm_hours"] * 3600:
            state.logged.add((rule, ref, day))


def triage_digest_items(state):
    """"repo#N" -> the reduced issue the rules read, from the lists already held (no read)."""
    index = {}
    for (repo, unit), held in state.units.items():
        if unit == "issues" and isinstance(held, dict) and isinstance(held.get("data"), list):
            for record in held["data"]:
                item = triage_norm(record) if isinstance(record, dict) else None
                if item is not None:
                    index["%s#%d" % (repo, item["number"])] = item
    return index


def triage_digest_memory(engine, state):
    """The digest's memory and its cadence, loaded once together (the gate and the commit both need `state.meta`)."""
    if state.seen is None or state.meta is None:
        state.seen = triage_digest.load_seen(engine.state_dir)
        state.meta = triage_digest.load_meta(engine.state_dir)


def triage_digest_keep(engine):
    """keep(rule, ref) for the prune: True when the record's candidates could not be computed in this pass (#805)."""
    def keep(rule, ref):
        needs = triage_inputs(rule)
        if needs is None:
            return True   # a rule the table does not know: nothing about it is forgotten
        repo = ref.rpartition("#")[0]
        return any(whole_skipped(engine, "triage.prune", name if name in ("agents", "decisions") else "%s:%s" % (name, repo))
                   for name in needs if name in ("agents", "decisions") or repo)
    return keep


def triage_digest_due(engine, state, ours, candidates, cfg, now, owner_t6=()):
    """([(rule, ref)] for this pass's one digest, [ref] with an invalid parked label); see bin/triage_digest.py."""
    triage_digest_memory(engine, state)
    triage_digest.prune(state.seen, candidates, triage_digest_keep(engine))
    mutes = triage_digest.load_mutes(engine.state_dir) if engine.state_dir else {}
    return triage_digest.select(state.seen, triage_digest_items(state), mutes, ours, now, cfg.get("backoff", True), state.alarmed,
                                cfg["realarm_hours"] * 3600, PARKED_MAX_DAYS, owner_t6)


def triage_digest_commit(engine, state, shown, invalid, now):
    triage_digest.commit(state.seen, triage_digest_items(state), shown, invalid, now)
    for ref in invalid:
        engine._note("triage.parked_invalid", ref, {"ref": ref, "why": "a parked label without one valid `Parked until: YYYY-MM-DD` line"})
    triage_digest.write_seen(engine.state_dir, state.seen, state.meta)


def triage_queue_fact(state, due, now, cfg, other_owner=0, invalid=()):
    """Queue THE one digest of the pass and return its text (the cost figure counts it). Only the shown items are marked."""
    shown = due[:cfg["max_findings"]]
    digest = hashlib.sha256("|".join("%s:%s" % pair for pair in shown).encode()).hexdigest()[:10]
    key = "triage:%s:%d" % (digest, int(now // (cfg["realarm_hours"] * 3600)))
    kinds = sorted({r for r, _ in due})
    if not due:   # only the once-only flag of an invalid park: still one message, and nothing else in it
        summary = "triage: no stale item to show; parked without a valid `Parked until: YYYY-MM-DD` line (treated as not parked): %s." % ", ".join(invalid)
        state.facts["triage:parked:%s" % hashlib.sha256(summary.encode()).hexdigest()[:10]] = (summary, now)
        return summary
    summary = "triage: %d finding%s: %s%s. %s. Resolve, route or escalate each; the triage changes nothing on GitHub." % (
        len(due), "" if len(due) == 1 else "s", ", ".join("%s %s%s" % (ref, rule, " (%s)" % state.reasons[(rule, ref)] if (rule, ref) in state.reasons else "") for rule, ref in shown),
        " (+%d more)" % (len(due) - len(shown)) if len(due) > len(shown) else "",
        "; ".join("%s = %s" % (k, TRIAGE_KINDS[k]) for k in kinds))
    if other_owner:
        summary += " Also %d other-owner flag(s): not ours, nothing to do." % other_owner
    if invalid:
        summary += " Parked without a valid `Parked until: YYYY-MM-DD` line (treated as not parked): %s." % ", ".join(invalid)
    for pair in shown:
        state.alarmed[pair] = now
    state.facts[key] = (summary, now)
    for old in [k for k, (_, at) in state.facts.items() if now - at > cfg["realarm_hours"] * 7200 and "summary" not in k]:
        del state.facts[old]
    state.carried = set(shown)   # #849: what this digest carried and remembered; an overflow beyond the cap stays due
    return summary


def triage_daily_summary(engine, state, cfg, now, day, repos):
    """Once per 24 h SINCE THE LAST ONE (not per calendar day): found, other-owner, escalated, unchecked and unreadable counts.
    Audited always; sent to the Chief of Staff only when enabled and `daily_summary`. Not before every repo was read once."""
    if not state.current or (state.summarised is not None and now - state.summarised < 86400):
        return
    if not all((r, "issues") in state.units or r in state.unreadable for r in repos):
        return   # not every repo has been read once yet (a restart's first passes): a summary of nothing says nothing
    state.summarised = now
    counts = state.current["counts"]
    text = "triage summary: found %d (%s); other-owner flags %d; escalated since the last summary %d; unchecked %d; unreadable repos %d%s" % (
        state.current["total"], ", ".join("%s %d" % kv for kv in sorted(counts.items())) or "none",
        state.current.get("other_owner", 0), sum(state.day_counts.values()), state.current["unchecked"],
        len(state.current["unreadable"]),
        " (" + ", ".join(state.current["unreadable"][:5]) + ")" if state.current["unreadable"] else "")
    state.day_counts = {}
    engine._note("triage.summary", None, {"text": text, "counts": counts, "enabled": cfg["enabled"], "at": iso(now)})
    if cfg["enabled"] and cfg["daily_summary"]:
        state.facts["triage.summary:%d" % int(now)] = (text, now)
        triage_digest.cost_record(engine.state_dir, now, text)


def triage_write(engine, state):
    path = Path(engine.state_dir) / TRIAGE_FILE
    body = json.dumps(state.current, sort_keys=True)
    try:
        state_write(path, body + "\n", _without(state.current, "at"), HEARTBEAT_SWEEP)
    except OSError:
        pass


def triage_summary(state_dir):
    try:
        raw = json.loads((Path(state_dir) / TRIAGE_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not (isinstance(raw, dict) and isinstance(raw.get("counts"), dict)):
        return None
    raw["cost"] = triage_digest.cost_read(str(state_dir))   # #349: None means no figure was recorded
    return raw
