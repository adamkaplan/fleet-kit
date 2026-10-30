# ---- a batch: related findings raised as ONE decision (one issue, one Markdown section per row in its body)

BATCH_FENCE = decision_core.BATCH_FENCE
BATCH_REF_MAX, BATCH_FINDING_MAX, BATCH_REC_MAX = (decision_core.BATCH_REF_MAX, decision_core.BATCH_FINDING_MAX,
                                                   decision_core.BATCH_REC_MAX)
BATCH_TITLE_MAX = decision_core.BATCH_TITLE_MAX
BatchError = decision_core.BatchError
_ROWS_BLOCK = decision_core._ROWS_BLOCK
_ROW_REF = decision_core._ROW_REF


def short_ref_of(ref):
    return decision_core.short_ref_of(ref)


def parse_batch_row(line):
    return decision_core.parse_batch_row(line)


def row_repo(ref):
    return decision_core.row_repo(ref)


def row_of(ref, finding, rec):
    return decision_core.row_of(ref, finding, rec)


def parse_decision_rows(body):
    return decision_core.parse_decision_rows(body)


def batch_headline(title, count):
    return decision_core.batch_headline(title, count)


def batch_body(name, rows):
    """The issue body of a batch: a short introduction, then one Markdown section for each row in FULL (see decision_core.batch_row_sections), then the fleet's mark."""
    return ("Related findings raised as ONE decision (%d rows) by %s. Each section below is one row: the issue, the finding and the recommendation, "
            "in full. One answer (`as recommended`, or per-row changes) answers every row. Nothing here changes the row "
            "issues: the raising orchestrator acts on the answer.\n\n%s\n\n%s"
            % (len(rows), name, decision_core.batch_row_sections(rows), fleet_mark(name)))


def decisions_from_github(engine, now):
    """(items, problems): the open issues with your label, from the hub's picture. The listing makes no `gh` call; each issue's plan kind
    (#1094) is one cached read of its comments, at most PLAN_KIND_READS_PER_REFRESH new reads per call (the rest read "unknown" until the next refresh)."""
    if not decision_repos(engine):
        return [], []
    hub = engine.hub
    if hub is None:
        return [], ["GitHub is read by the daemon: this process holds no hub, so the issues that wait on you "
                    "are not listed"]
    records, problems = hub.awaiting_listing()
    agents = engine.discovery.agents if engine.discovery is not None else {}
    items = []
    budget = [PLAN_KIND_READS_PER_REFRESH]
    kinds = GitHubHub.plan_kinds(hub, records, budget)
    for record in records:
        numbers = [record["number"]] + ([record["parent"]] if record.get("parent") else [])
        items.append({"kind": "issue", "title": record["title"] or "issue #%d" % record["number"],
                      "ask": record["number"], "agent": pick_owner(agents, numbers, record["repo"], view=registry_charter_view(engine)),
                      "since": record["since"], "seed": "issue:%s#%d" % (record["repo"], record["number"]),
                      "bare": True, "repo": record["repo"], "tier": record.get("tier") or "human",
                      "rows": record.get("rows") or [], "raised_by": record.get("raised_by"),
                      "parent": record.get("parent"), "closed": bool(record.get("closed")),
                      "waits_for": record.get("waits_for") or [],
                      "plan_kind": kinds[(record["repo"], record["number"])]})
    return items, problems


def decisions_from_requests(engine, now):
    """(items, problems): the pending permission requests and questions of every fleet agent, as WorkerFacts read them.

    PR 18: a request is the Chief of Staff's (`cos`) until it has been pending `decisions_cos_grace_seconds`, or
    until the Chief of Staff escalated it (`decisions escalate`, escalated-requests.json); then it is yours. An
    agent blocked on a prompt is never hidden from you for good."""
    source = next((s for s in engine.sources if isinstance(s, WorkerFacts)), None)
    if source is None:
        return [], []
    items, seen = [], {}
    grace = engine.config["decisions_cos_grace_seconds"]
    escalated = read_escalated(engine.state_dir) if engine.state_dir is not None else {}
    for observation, kind, request in source.requests:
        key = (observation.session_id, request["id"])
        what = " ".join(str(part) for part in [request.get("action") or request.get("title") or kind]
                        + [r for r in request.get("resources") or [] if isinstance(r, str)])
        asked = _created(request)
        seen[key] = asked if asked is not None else engine.decision_seen.get(key, now)
        ask = _issue_of(observation.fleet)
        tier = decision_core.request_tier(now, seen[key], grace, "%s:%s" % key in escalated)
        items.append({"kind": kind, "title": "%s: %s" % (kind, what), "ask": ask,
                      "repo": _fleet_repo(observation.fleet, engine.config),
                      "agent": observation.name, "since": seen[key],
                      "seed": "request:%s:%s" % key, "bare": False, "tier": tier,
                      "session": observation.session_id, "request": request["id"]})
    engine.decision_seen = seen  # memory only: a request with no time of its own keeps the first time it was seen
    engine.request_keys = {"%s:%s" % key for key in seen}
    return items, []


def synthetic_lines(engine, session_id, since, waiting=False):
    """The lines of a session's synthetic messages since `since`: [(created, message id, line number, line)],
    newest message first. The one read of what a message said, shared by both report checks.

    `waiting` (the worker's side: the sends that answer a report) adds the notes still in v2's inbox, first. A note
    delivered to a session that is busy or engaged with a person waits there and is in no transcript until its
    next turn: it counts as delivered, as everywhere else (`_fleet_keys`), or a report answered by such a send would
    stay open. The inbox is read every time (one small read; it is not in the cache, because it changes without the
    transcript's newest id changing). The boss's side (the reports) stays the transcript's: a report is listed once
    the boss has it."""
    if getattr(engine, "light", False):
        return []
    found = transcript_lines(engine, session_id, since)
    if not waiting:
        return found
    inbox = engine.v2.inbox_list(session_id)  # an inbox that cannot be read raises: the caller says so, it is not guessed
    have = {line[1] for line in found}
    waiting = []
    for item in inbox if isinstance(inbox, list) else []:
        payload = item.get("payload") if isinstance(item, dict) and isinstance(item.get("payload"), dict) else {}
        created = _created(item) if isinstance(item, dict) else None
        if not isinstance(item, dict) or item.get("type") != "synthetic" or item.get("id") in have \
                or created is None or created < since or not isinstance(payload.get("text"), str):
            continue
        waiting.extend((created, item.get("id"), number, line) for number, line in numbered_lines(payload["text"], item))
    return sorted(waiting, key=lambda line: -line[0]) + found


def numbered_lines(text, message=None):
    """[(number, line)] of a message; the number grows with time, so a message composed newest first (#1275) is numbered from its last line.
    Which messages are newest first is read from the STRUCTURE (the daemon's own fleet metadata: from the switchboard, with keys, and its header line), never from the text alone: an agent's
    reply that ends its first line with the same words is numbered as written."""
    lines = text.splitlines()
    meta = _fleet_meta(message) if isinstance(message, dict) else None
    if lines and isinstance(meta, dict) and meta.get("from") == "switchboard" and _fleet_keys(message) and lines[0].startswith("[switchboard] ") and lines[0].endswith(NEWEST_FIRST_MARK):
        return [(len(lines) - 1 - n, line) for n, line in enumerate(lines)]
    return list(enumerate(lines))


def transcript_lines(engine, session_id, since):
    """The transcript's synthetic lines since `since`, newest message first (cached by the newest synthetic id)."""
    if getattr(engine, "light", False):
        return []
    newest = engine.v2.message_list(session_id, limit=1, type="synthetic")  # one small read per pass
    newest_id = newest[0].get("id") if isinstance(newest, list) and newest and isinstance(newest[0], dict) else None
    cached = engine.synthetic_cache.get(session_id)
    if cached is not None and cached[0] == newest_id and cached[1] <= since:
        return [line for line in cached[2] if line[0] >= since]  # nothing new since the last full read
    found, complete = [], False
    for page, more in engine.v2.message_pages(session_id, SYNTHETIC_PAGE, "synthetic", MAX_READ_PAGES):
        for message in page:
            created = _created(message) if isinstance(message, dict) else None
            if created is None:
                continue
            if created < since:
                complete = True
                break
            for number, line in numbered_lines(reply_text(message), message):
                found.append((created, message.get("id"), number, line))
        if complete or not more:
            break
    if len(engine.synthetic_cache) > 200:
        engine.synthetic_cache.clear()
    engine.synthetic_cache[session_id] = (newest_id, since, found)  # memory only, rebuilt after a restart
    return found


COS_ALIAS = "chief-of-staff"   # what a worker's reports_to says when it was launched with the role name


def resolve_boss(discovery, name):
    """The registered agent a `reports_to` names. The role name `chief-of-staff` is an alias for the one agent whose
    role is chief-of-staff, whatever it is registered as (`cos`); any other name, or an alias with no single
    registered chief of staff, is taken literally."""
    if name in discovery.agents or name != COS_ALIAS:
        return name
    found = [n for n, o in discovery.agents.items() if (o.fleet or {}).get("role") == COS_ALIAS]
    return found[0] if len(found) == 1 else name


def boss_name(discovery, observation):
    """The registered name of the agent `observation` reports to (`resolve_boss` over its `reports_to`), or None. Every lookup of a
    boss as an AGENT goes through here (#102); `_boss_of` alone is the literal text, for display."""
    boss = _boss_of(observation)
    boss = resolve_boss(discovery, boss) if boss is not None and discovery is not None else boss
    return boss if boss != observation.name else None  # a chief of staff launched with reports_to chief-of-staff is not its own boss
