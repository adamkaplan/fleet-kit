# ---- the Cockpit's fleet feed (#1423): ONE read-only, versioned JSON document of the fleet's own state, for a reader outside the daemon.
#
# `fleet-switchboard cockpit --json` prints it; the daemon writes the same document to $STATE/cockpit.json (mode 0600, atomic) at the end of a pass, when the
# content changed and at least every COCKPIT_HEARTBEAT seconds. READ ONLY: it reads state files the daemon already keeps (decisions.json, registry.json,
# last-reports.json), makes NO network call and needs NO credential, calls no verb that answers, resolves or defers a decision, and writes one file: cockpit.json.
# What it carries is an ALLOWLIST (docs/cockpit-feed.md lists it, a test compares both ways): plain text, never a link, every string capped, at most 50
# items per card, newest first. Nothing else leaves it: no transcript, diff, token, file path, load number or repository text beyond a title.

COCKPIT_FILE = "cockpit.json"
COCKPIT_SCHEMA_VERSION = 1
COCKPIT_HEARTBEAT = 60        # seconds: the file is rewritten at least this often (the reader shows it stale after 180)
COCKPIT_MAX_ITEMS = 50        # per card
# card -> (the keys of one item, in the order they are written). The ONE allowlist: a key outside it is never written.
COCKPIT_FIELDS = {
    "decisions": ("id", "etag", "since", "title", "repo", "agent", "kind"),
    "held_prs": ("id", "etag", "repo", "number", "title", "holds", "reason", "since"),
    "lanes": ("id", "etag", "state", "since", "last_seen"),
    "blocked": ("id", "etag", "blocked_since", "blocked_on", "reason"),
    "heads_up": ("id", "etag", "repo", "number", "note", "sender", "since"),
}
COCKPIT_CAPS = {"title": 120, "reason": 200, "note": 300, "blocked_reason": 120, "repo": 100, "agent": 80, "sender": 80, "kind": 20, "hold": 40}
COCKPIT_HOLDS_MAX = 5
COCKPIT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
COCKPIT_LANE_STATES = {"working": "ok", "blocked": "blocked", "idle": "idle", "detached": "down", "dead": "down"}
_COCKPIT_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_COCKPIT_DIFF_LINE = re.compile(r"^(diff --git |index [0-9a-f]+\.\.|@@ |\+\+\+ |--- |[+-][^+\-\s])")
_COCKPIT_DIFF_INLINE = re.compile(r"diff --git\s+\S+\s+\S+|@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_COCKPIT_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|[A-Fa-f0-9]{32,}|[A-Za-z0-9+/_-]{40,}={0,2})\b")
_COCKPIT_PATH = re.compile(r"(?:(?<![\w.-])(?:~|\.{1,2})?/(?:[\w.@+-]+/)*[\w.@+-]+|\b[A-Za-z]:\\[^\s]+|\b[\w.-]+(?:/[\w.-]+){2,}(?:\.\w+)?)")


def cockpit_text(value, limit):
    """`value` as one plain line of at most `limit` characters, for a reader outside the fleet: control characters gone, diff lines dropped, a token-like string
    and a file path replaced by a mark. Applied to every string of the feed, whatever the source said."""
    if not isinstance(value, str):
        return ""
    lines = value.splitlines()
    kept = [line for line in lines if not _COCKPIT_DIFF_LINE.match(line)] if len(lines) > 1 else lines   # a one-line title that starts with a sign is a title, not a diff
    text = _COCKPIT_CONTROL.sub(" ", " ".join(kept))
    text = _COCKPIT_DIFF_INLINE.sub("[removed]", text)
    text = _COCKPIT_TOKEN.sub("[removed]", text)
    text = _COCKPIT_PATH.sub("[path]", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def _cockpit_hash(seed):
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _cockpit_name(value):
    """A name that fits an id: the pattern's characters only."""
    return re.sub(r"[^A-Za-z0-9._:-]", "_", str(value or ""))[:100] or "unknown"


def cockpit_etag(item):
    """The first 16 hex of the sha256 of the item's canonical JSON (sorted keys, no whitespace) without its etag."""
    body = {k: v for k, v in item.items() if k != "etag"}
    return _cockpit_hash(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def _cockpit_item(card, values):
    """The item with exactly the allowlisted keys of its card (a missing value is null), and its etag."""
    item = {key: values.get(key) for key in COCKPIT_FIELDS[card] if key != "etag"}
    item["etag"] = cockpit_etag(item)
    return {key: item[key] for key in COCKPIT_FIELDS[card]}


def _cockpit_since(value):
    """A time as the feed shows it: an ISO text (as decisions.json holds it) or None. An epoch is turned into one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == value:
        return iso(value)
    return cockpit_text(value, 40) or None if isinstance(value, str) else None


def _cockpit_newest_first(items):
    return sorted(items, key=lambda item: item.get("since") or item.get("blocked_since") or item.get("last_seen") or "", reverse=True)[:COCKPIT_MAX_ITEMS]


def cockpit_cards(decisions, agents, last_reports):
    """{card: [item]} from what the daemon already holds: `decisions` (the list decisions.json carries), `agents` (the registry's rows) and `last_reports`
    (last-reports.json). Pure: no file, no clock."""
    cards = {name: [] for name in COCKPIT_FIELDS}
    human = [d for d in decisions if isinstance(d, dict) and d.get("tier") == "human"]
    by_agent = {}
    for entry in human:
        repo, ask, agent, kind = entry.get("repo") or "", entry.get("ask"), entry.get("agent") or "", entry.get("kind")
        number = entry.get("number") if isinstance(entry.get("number"), int) and not isinstance(entry.get("number"), bool) else None
        since = _cockpit_since(entry.get("since"))
        if kind == "captain":
            seed = "%s#%s" % (repo, number if number is not None else entry.get("id"))
            holds = [cockpit_text(h, COCKPIT_CAPS["hold"]) for h in (entry.get("holds") or []) if isinstance(h, str)][:COCKPIT_HOLDS_MAX]
            reason = entry.get("rationale") or entry.get("note") or entry.get("title")
            cards["held_prs"].append(_cockpit_item("held_prs", {
                "id": "pr:" + _cockpit_hash(seed), "repo": cockpit_text(repo, COCKPIT_CAPS["repo"]), "number": number,
                "title": cockpit_text(entry.get("title"), COCKPIT_CAPS["title"]), "holds": holds,
                "reason": cockpit_text(reason, COCKPIT_CAPS["reason"]), "since": since}))
        elif kind == "notice":
            seed = "%s#%s|notice" % (repo, number if number is not None else entry.get("id"))
            cards["heads_up"].append(_cockpit_item("heads_up", {
                "id": "hu:" + _cockpit_hash(seed), "repo": cockpit_text(repo, COCKPIT_CAPS["repo"]), "number": number,
                "note": cockpit_text(entry.get("title") or entry.get("headline"), COCKPIT_CAPS["note"]),
                "sender": cockpit_text(agent, COCKPIT_CAPS["sender"]) or None, "since": since}))
        else:
            seed = "%s#%s" % (repo, ask) if ask else "%s|%s|%s" % (agent, kind, entry.get("id"))
            item = _cockpit_item("decisions", {
                "id": "dec:" + _cockpit_hash(seed), "since": since, "title": cockpit_text(entry.get("title") or entry.get("headline"), COCKPIT_CAPS["title"]),
                "repo": cockpit_text(repo, COCKPIT_CAPS["repo"]) or None, "agent": cockpit_text(agent, COCKPIT_CAPS["agent"]) or None,
                "kind": cockpit_text(kind, COCKPIT_CAPS["kind"]) or None})
            cards["decisions"].append(item)
            if agent:
                by_agent.setdefault(agent, []).append(item)
    orchestrators = {}
    for name, row in sorted(agents.items()):
        if isinstance(row, dict) and row.get("role") == "orchestrator":
            orchestrators.setdefault(row.get("charter"), name)
    for name, row in sorted(agents.items()):
        if not isinstance(row, dict):
            continue
        state = COCKPIT_LANE_STATES.get(row.get("state"))
        seen = (last_reports.get(name) or {}).get("at") if isinstance(last_reports.get(name), dict) else None
        if row.get("role") == "orchestrator" and state is not None:
            cards["lanes"].append(_cockpit_item("lanes", {"id": "lane:" + _cockpit_name(name), "state": state, "since": _cockpit_since(row.get("since")), "last_seen": _cockpit_since(seen)}))
        if row.get("state") == "blocked":
            raised = sorted(by_agent.get(name, []), key=lambda item: item.get("since") or "", reverse=True)
            boss = orchestrators.get(row.get("charter"))
            on = raised[0]["id"] if raised else "lane:" + _cockpit_name(boss or name)
            last = last_reports.get(name) if isinstance(last_reports.get(name), dict) else {}
            reason = last.get("text") if last.get("state") == "blocked" else None
            cards["blocked"].append(_cockpit_item("blocked", {
                "id": "blk:" + _cockpit_hash("%s|blocked" % name), "blocked_since": _cockpit_since(row.get("since")), "blocked_on": on,
                "reason": cockpit_text(reason or "blocked", COCKPIT_CAPS["blocked_reason"])}))
    return {name: _cockpit_newest_first(items) for name, items in cards.items()}


def cockpit_read(state_dir):
    """(decisions, agents, last_reports) as the state files hold them; a file that is missing or damaged reads as empty. Reads only: the registry's own
    `_load` never migrates, and nothing here writes."""
    state = Path(state_dir)
    body = read_decisions_file(state / DECISIONS_FILE)
    decisions = body["decisions"] if body else []
    try:
        loaded = load_module("registry")._load(state)
    except Exception:
        loaded = None
    agents = loaded["agents"] if isinstance(loaded, dict) and loaded.get("whole") else {}
    return decisions, agents, _read_small(state, LAST_REPORT_FILE)


def cockpit_document(cards, generation, now):
    return {"schema_version": COCKPIT_SCHEMA_VERSION, "generation": generation, "produced_at": iso(now), "cards": cards}


def cockpit_stable(cards, generation):
    """The text compared to decide a rewrite: the content without `produced_at`."""
    return json.dumps({"schema_version": COCKPIT_SCHEMA_VERSION, "generation": generation, "cards": cards}, sort_keys=True)


def cockpit_current(path):
    """(generation, cards) of the file as it is, or (0, None) when it is missing or not a feed of this version."""
    try:
        body = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return 0, None
    if not isinstance(body, dict) or body.get("schema_version") != COCKPIT_SCHEMA_VERSION or not isinstance(body.get("generation"), int) or not isinstance(body.get("cards"), dict):
        return 0, None
    return body["generation"], body["cards"]


def cockpit_next(state_dir, now):
    """(document, cards, generation): the feed for now. The generation is the file's own while the cards are unchanged, and one more when they changed."""
    cards = cockpit_cards(*cockpit_read(state_dir))
    generation, current = cockpit_current(Path(state_dir) / COCKPIT_FILE)
    if current != cards:
        generation += 1
    return cockpit_document(cards, generation, now), cards, generation


def publish_cockpit(engine):
    """The end of a pass: write cockpit.json when the content changed and at least every COCKPIT_HEARTBEAT seconds (through `state_write`: a private temp file
    renamed into place, mode 0600). Returns True when written. Writes this one file and nothing else."""
    state = engine.state_dir
    if state is None:
        return False
    now = engine.now()
    document, cards, generation = cockpit_next(state, now)
    return state_write(Path(state) / COCKPIT_FILE, json.dumps(document, indent=1, sort_keys=True) + "\n", stable=cockpit_stable(cards, generation), heartbeat=COCKPIT_HEARTBEAT)


def add_cockpit_parsers(sub):
    cockpit = sub.add_parser("cockpit", help="Print the Cockpit's fleet feed: decisions, held PRs, lanes, blocked lanes and heads-ups as one versioned JSON document (read-only)",
                             description="Prints the document the daemon writes to $STATE/cockpit.json: a read-only, plain-text projection of the state files the daemon already keeps "
                                         "(no network, no credential, nothing is changed). See docs/cockpit-feed.md.")
    cockpit.add_argument("--json", action="store_true", help="Print the document as JSON (the only format)")


def cmd_cockpit(args, now):
    """`cockpit --json`: the feed for now, from the state files; it writes nothing."""
    if not getattr(args, "json", False):
        sys.stderr.write("%s: cockpit prints JSON: use --json\n" % PROG)
        return EXIT_USAGE
    document, _, _ = cockpit_next(state_dir_path(), now())
    print(json.dumps(document, indent=1, sort_keys=True))
    return EXIT_OK


cmd_cockpit.direct = True   # no error mapping, no daemon start: it reads files and prints
