# ---- #494: a time budget for each heavy reader that had none, so a restart under load cannot hold a pass for minutes. The reader checks the
# budget BETWEEN calls (no thread, no process), stops at a safe point and finishes over the following passes; 0 means no limit.
SOURCE_BUDGET_DEFAULTS = {"load_alert": 20, "charters": 15, "catch_up": 90}   # seconds a pass; catch_up's default is the bound it always had


def validate_source_budgets(config, source):
    """`source_budgets`: absent is the defaults; an object of {load_alert, charters, catch_up} seconds (a number, 0 = no limit)."""
    raw = config.get("source_budgets")
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ConfigError("%s: 'source_budgets' must be an object, got %r" % (source, type(raw).__name__))
    unknown = sorted(set(raw) - set(SOURCE_BUDGET_DEFAULTS))
    if unknown:
        raise ConfigError("%s: 'source_budgets' has no key %s (it has %s)" % (source, ", ".join(repr(k) for k in unknown), ", ".join(SOURCE_BUDGET_DEFAULTS)))
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 3600:
            raise ConfigError("%s: 'source_budgets.%s' must be a number of seconds from 0 (no limit) to 3600, got %r" % (source, key, value))


def source_budget_seconds(config, name):
    """The seconds a pass may spend in the reader `name` (0: no limit); the default when the key is absent."""
    raw = (config or {}).get("source_budgets")
    value = raw.get(name) if isinstance(raw, dict) else None
    return SOURCE_BUDGET_DEFAULTS[name] if value is None else value


def set_source_state(engine, name, pending):
    """What `name` still has to read (a list of short texts, [] when it is done), kept in memory for the status line of #494 slice 3:
    {name: {"state": "pending"|"done", "pending": [...]}}. Never raises."""
    engine.__dict__.setdefault("source_state", {})[name] = {"state": "pending" if pending else "done", "pending": list(pending)[:20]}


def source_states(engine):
    """{source: {"state", "pending"}} of the readers with a time budget, as last set (empty before their first pass)."""
    return dict(getattr(engine, "source_state", None) or {})


class SourceBudget:
    """The time one reader has in this pass, checked between its calls: `spent()` is True once `seconds` have passed since it was made
    (never when `seconds` is 0). It stops nothing by itself: the reader asks and stops at a safe point."""

    def __init__(self, seconds, now):
        self.seconds, self.now, self.started = seconds, now, now()

    def spent(self):
        return bool(self.seconds) and self.now() - self.started >= self.seconds


def validate_load_alert_config(config, source):
    """`load_alert`: absent or null is off (the load figures are still kept and shown)."""
    if "load_alert" not in config:
        return
    try:
        config["load_alert"] = load_fleet_health().validate(config["load_alert"], "%s: 'load_alert'" % source)
    except ValueError as err:
        raise ConfigError(str(err)) from None


def coder_finished(obs, retired, last_reports):
    """True for a coder that is on the books but done: on the retired list (the same name and session, as the pane check reads it), or whose
    last report was a final `done` or `failed` (a later `working` report replaces it, so a coder given new work counts again)."""
    name = obs.name
    entry = retired.get(name)
    if isinstance(entry, dict) and entry.get("session") in (None, obs.session_id):
        return True
    return (last_reports.get(name) or {}).get("state") in ("done", "failed")


CLOUD_LOAD_MAX_ENTRIES = 200      # #1097: the most cloud records one pass reads for the load line
CLOUD_LOAD_MAX_SCAN = 2000        # and the most directory entries it examines (of any name)
CLOUD_LOAD_SECONDS = 0.5          # and the most time (monotonic) it spends
CLOUD_LOAD_MAX_BYTES = 65536      # and the most bytes of one record
CLOUD_LOAD_STALE_SECONDS = 3600   # a record that carries last_report_at older than this is not live


CLOUD_NO_BOSS_LANE = "(no boss)"   # #1097: the named fallback lane of a cloud worker whose record has no reports_to (counted, but no load row shows it)
_CLOUD_TRUNCATED = {}              # state dir -> True while the listing is cut (one audit line per episode)


CLOUD_BOSS_MAX = 64


def _cloud_boss(value):
    """#1097: the lane key a record's reports_to gives: a string with its control characters removed and at most CLOUD_BOSS_MAX long; else "" (no boss)."""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable())[:CLOUD_BOSS_MAX].strip()


def _cloud_when(text):
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def cloud_record_live(record, worker, retired, last_reports, now):
    """The ONE liveness test of a cloud worker's record (#1097, #1066): not retired, no final report, not past its hard deadline (a record WITHOUT a valid
    one is no evidence of life), and a last_report_at, if present, neither future-dated nor older than CLOUD_LOAD_STALE_SECONDS. An ended record stays on
    disk until cloud-clear, so a file's existence proves nothing."""
    if worker in retired or (last_reports.get(worker) or {}).get("state") in ("done", "failed") or record.get("retired"):
        return False
    ends = _cloud_when(record.get("hard_deadline"))
    if ends is None or ends < now:
        return False
    if "last_report_at" in record:
        seen = _cloud_when(record.get("last_report_at"))
        if seen is None or seen > now or now - seen > CLOUD_LOAD_STALE_SECONDS:
            return False
    return True


def cloud_sender(state_dir, name):
    """#1478: the pane-less sender of a cloud worker's report: an Observation built from its endpoint record (the same `reports_to` the lane count
    reads), or None when `name` has no such record. Only a record of that exact worker counts; nothing in it but name, reports_to, repo is used."""
    if state_dir is None or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        return None
    record, _kind = load_module("cloud_worker").read_record_file(Path(state_dir) / "cloud" / (name + ".json"), CLOUD_LOAD_MAX_BYTES)
    if record is None or record.get("worker") != name:
        return None
    boss = _cloud_boss(record.get("reports_to"))
    fleet = {"name": name, "role": "cloud-worker", "reports_to": boss or None, "issue": None}
    if isinstance(record.get("repo"), str):
        fleet["repo"] = record["repo"]
    return Observation(name, None, None, "unknown", fleet)


def live_cloud_by_lane(state_dir, discovery, retired, last_reports, audit=None, now=None, clock=None):
    """#1097: {lane name: live cloud workers} from the files under the cloud record directory (one bounded scan, no GitHub read, no process). A worker
    that is retired, made its final report, or is past its hard deadline is not counted; a record WITHOUT a valid hard_deadline (no evidence of liveness)
    is not counted either, nor one whose last_report_at (if present) is older than CLOUD_LOAD_STALE_SECONDS. A lane is the boss the local coders' rule gives
    (`resolve_boss`). The files are read by cloud_worker.read_record_file (never blocks, no symlink, regular files only); a hostile file is skipped.
    An unreadable directory gives {} and ONE audit line. The scan stops at CLOUD_LOAD_MAX_SCAN entries examined, CLOUD_LOAD_MAX_ENTRIES records read or
    CLOUD_LOAD_SECONDS (injected `clock`, monotonic): ONE audit line (load.cloud_truncated, "bound": files|records|time) per episode. `now` is injected."""
    base = Path(state_dir) / "cloud"
    counts = {}
    clock = clock or time.monotonic
    deadline = clock() + CLOUD_LOAD_SECONDS
    now = time.time() if now is None else now
    reader = load_module("cloud_worker").read_record_file
    examined = read = 0
    bound = None
    try:
        if not base.is_dir():
            return counts
        with os.scandir(str(base)) as it:
            for entry in it:
                if examined >= CLOUD_LOAD_MAX_SCAN:
                    bound = "files"
                    break
                if clock() > deadline:
                    bound = "time"
                    break
                examined += 1
                fname = entry.name
                if not fname.endswith(".json"):
                    continue
                if read >= CLOUD_LOAD_MAX_ENTRIES:
                    bound = "records"
                    break
                read += 1
                worker = fname[:-5]
                record, _kind = reader(base / fname, CLOUD_LOAD_MAX_BYTES)
                if record is None or record.get("worker") != worker:
                    continue
                if not cloud_record_live(record, worker, retired, last_reports, now):
                    continue
                boss = _cloud_boss(record.get("reports_to"))
                lane = resolve_boss(discovery, boss) if boss else CLOUD_NO_BOSS_LANE
                counts[lane] = counts.get(lane, 0) + 1
    except OSError as err:
        if audit is not None:
            audit.append("daemon", "load.cloud_unreadable", None, {"why": _one_line(str(err), 120)})
        return {}
    key = str(state_dir)
    if bound:
        if not _CLOUD_TRUNCATED.get(key) and audit is not None:
            audit.append("daemon", "load.cloud_truncated", None, {"bound": bound, "limit": {"files": CLOUD_LOAD_MAX_SCAN, "records": CLOUD_LOAD_MAX_ENTRIES, "time": CLOUD_LOAD_SECONDS}[bound],
                                                                  "entries": examined, "name": {"files": "CLOUD_LOAD_MAX_SCAN", "records": "CLOUD_LOAD_MAX_ENTRIES", "time": "CLOUD_LOAD_SECONDS"}[bound]})
        _CLOUD_TRUNCATED[key] = True
    else:
        _CLOUD_TRUNCATED[key] = False
    return counts


HELD_LINKS_WAIT_SECONDS = 900   # #796: after a start the alert waits at most this long for every human-tier held PR's links, so an unreadable one cannot silence it for ever


def load_tick(engine, discovery):
    """The end of a pass: the load figures per orchestrator, and the alert to the Chief of Staff when a threshold is crossed. Reads no v2 and
    makes at most a bounded number of GitHub reads an hour; a source it cannot read means no wake. Returns what was sent."""
    if discovery is None or engine.state_dir is None:
        return []
    if getattr(engine, "awaiting_first_full_pass", False):   # #796: the held-PR links come from the first full decisions pass; judging before it reads waiting-on-owner as 0
        load_fleet_health().mark_waiting(engine.state_dir)   # status says so; the figures, the episodes and the alert are left exactly as they were
        return []
    health, now = load_fleet_health(), engine.now()
    waited = engine.__dict__.setdefault("links_wait_since", now) if hasattr(engine, "__dict__") else now
    links_gate = getattr(engine, "awaiting_held_links", False) and now - waited < HELD_LINKS_WAIT_SECONDS   # #796: a start's held-PR links must be whole before the first alert; it lapses after a bound
    cfg = engine.config.get("load_alert") or dict(health.DEFAULTS)
    reminders = read_reminders(engine.state_dir)
    listed = registry_read(engine.state_dir)   # the load line is a view of the registry: a retired agent has no figures; a read that is not whole alerts nobody (#805)
    if not listed["whole"]:
        return []
    retired, last_reports = listed["retired"], _read_small(engine.state_dir, LAST_REPORT_FILE)
    orchestrators = {}
    cloud_live = live_cloud_by_lane(engine.state_dir, discovery, retired, last_reports, engine.audit)
    cloud_unplaced = sum(n for lane, n in cloud_live.items() if lane not in discovery.agents or (discovery.agents[lane].fleet or {}).get("role") != "orchestrator")   # live cloud workers no load row shows
    for name, seen in sorted(discovery.agents.items()):
        fleet = seen.fleet or {}
        if fleet.get("role") != "orchestrator":
            continue
        entry = retired.get(name)
        if isinstance(entry, dict) and entry.get("session") in (None, getattr(seen, "session_id", None)):
            continue   # on the retired list (the same name and session rule as coder_finished): no load figures and no health row
        coders = [o for o in discovery.agents.values() if boss_name(discovery, o) == name and (o.fleet or {}).get("role") == "coder"
                  and not coder_finished(o, retired, last_reports)]   # a retired coder, or one that made its final report, is on the books but not a coder of this workspace
        orchestrators[name] = {"charter": charter_or_first_seen(engine, name, fleet),   # a fallback: the charter is found by the yaml that names the agent; the registry's, never the launch metadata (#969)
                               "repo": _fleet_repo(fleet, engine.config) or (engine.intents.repo if engine.intents is not None else None),
                               "busy": sum(1 for c in coders if c.status == "working"), "idle": sum(1 for c in coders if c.status == "idle"),
                               "workers": [o.name for o in coders if getattr(o, "name", None)], "cloud": cloud_live.get(name, 0),
                               "messages": sum(1 for r in reminders if r.get("to") == name and isinstance(r.get("at"), (int, float)) and r["at"] <= now)}
    cos = next((n for n, o in sorted(discovery.agents.items()) if (o.fleet or {}).get("role") == "chief-of-staff"), None)
    pr_refs = {key.lower(): (held.get("facts") or {}).get("refs") or [] for key, held in list(getattr(engine, "notice_facts", {}).items())
               if isinstance(held, dict) and not (held.get("facts") or {}).get("state") in ("merged", "closed")}   # the strict references of each noticed PR's current body, from the facts already read: no read of its own

    def read_asks(charter, repo):
        if engine.intents is None:
            raise RuntimeError("no ask repo is configured")
        return [{"repo": issue.repo or repo, "number": issue.number, "labels": list(getattr(issue, "labels", ()) or ()),
                 "parked_until": getattr(issue, "parked_until", None)} for issue in engine.intents.open_asks(charter, repo)]

    def read_charters(repo):
        """The open issues of `repo` that carry the orchestrator label (whatever the owner's prefix) as [(number, body)]: two bounded reads."""
        if engine.intents is None:
            raise RuntimeError("no ask repo is configured")
        labels = engine.intents._api("repos/%s/labels?per_page=100" % repo, repo)
        names = sorted({l["name"] for l in labels if isinstance(l, dict) and str(l.get("name")).endswith(":orchestrator")}) if isinstance(labels, list) else []
        out = []
        for name in names[:2]:   # every owner's prefix, at most two (each is one more read)
            found = engine.intents._api("repos/%s/issues?state=open&per_page=100&labels=%s" % (repo, urllib.parse.quote(name)), repo)
            out.extend((i["number"], i.get("body") or "") for i in found   # the issues list also returns pull requests: a PR body never names a charter
                       if isinstance(i, dict) and isinstance(i.get("number"), int) and "pull_request" not in i) if isinstance(found, list) else None
        return out

    def read_parent(repo, number):
        """The parent issue number of `repo#number` in the same repository (None when it has none): one read."""
        if engine.intents is None:
            raise RuntimeError("no ask repo is configured")
        data = engine.intents._api("repos/%s/issues/%d" % (repo, number), repo)
        url = data.get("parent_issue_url") if isinstance(data, dict) else None
        found = re.search(r"/repos/([^/]+/[^/]+)/issues/(\d+)$", url) if isinstance(url, str) else None
        return int(found.group(2)) if found and found.group(1).lower() == str(repo).lower() else None

    def wake(text):
        alert = getattr(context, "alert", None) or {}   # #1275: keyed by the DATA (the lane and its severity and reasons), never by the prose
        if add_repeat_reminder(engine.state_dir, cos, now, now, text, "load-alert", str(alert.get("agent") or "unknown"), str(alert.get("state") or "unknown"),
                               wake_class=WAKE_LOAD) is None:
            return
        if engine.audit is not None:
            engine.audit.append(engine.actor, "load.alert", cos, {"text": _one_line(text, 400)})

    meter = {}
    accounts = getattr(engine.intents, "accounts", None)
    if accounts is not None:   # the gh accounts' own books: which account a repo uses, whether it is paused (#361), and the reads GitHub really billed (a 304 is free)
        def snapshot():
            with accounts.lock:   # GhAccounts.read appends and trims the deque from other threads under this lock
                return list(accounts.calls)

        def begin_read():
            return max([call[0] for call in snapshot()] or [0])   # the newest read so far: what happens after it is this read's

        def read_cost(since, repo):
            account = accounts.describe(repo)   # only this account's reads are charged to it, and a 304 is free
            return sum(1 for call in snapshot() if call[0] > since and call[1] == account and not call[3])
        meter = {"account_of": accounts.describe, "paused": lambda repo: accounts.paused.get(accounts.describe(repo), 0) > now,
                 "begin_read": begin_read, "read_cost": read_cost}
    budget = SourceBudget(source_budget_seconds(engine.config, "load_alert"), engine.now)   # #494: the reads stop between calls when this pass's time is spent
    pending = []
    context = health.Context(now=now, state_dir=engine.state_dir, cfg=cfg, orchestrators=orchestrators, read_asks=read_asks, read_charters=read_charters, read_parent=read_parent, cos=cos, wake=wake,
                                      time_up=budget.spent, pending=pending, reminders=reminders, pr_refs=pr_refs, links_gate=links_gate, notices_unread=getattr(engine, "notice_unread", 0), **meter)
    context.cloud_unplaced = cloud_unplaced
    sent = health.tick(context)
    if getattr(context, "links_complete", False):
        engine.awaiting_held_links = False   # #796: one pass with every human-tier held PR's links whole: the alert may judge from now on
    set_source_state(engine, "load_alert", pending)
    return sent


def cmd_load_replay(args, runner, now):
    """`load-replay`: when the load alert WOULD have fired over a recorded day of the hourly series (read-only; sends nothing)."""
    health = load_fleet_health()
    cfg = dict(health.DEFAULTS)
    try:
        configured = load_config().get("load_alert")
        cfg = dict(cfg, **(configured or {}))
    except ConfigError:
        pass
    for key, value in (("stalled_hours", args.stalled_hours), ("stalled_asks", args.stalled_asks), ("backlog_hours", args.backlog_hours)):
        if value is not None:
            cfg[key] = value
    try:
        cfg = health.validate(cfg, "load-replay")
    except ValueError as err:
        sys.stderr.write("%s: load-replay: %s\n" % (PROG, err))
        return EXIT_USAGE
    if args.day is not None and not re.fullmatch(r"\d{4}-\d\d-\d\d", args.day):
        sys.stderr.write("%s: load-replay: --day must be a UTC date, YYYY-MM-DD, got %r\n" % (PROG, args.day[:20]))
        return EXIT_USAGE
    try:
        if args.day is not None:
            datetime.strptime(args.day, "%Y-%m-%d")
    except ValueError:
        sys.stderr.write("%s: load-replay: --day %r is not a calendar date\n" % (PROG, args.day))
        return EXIT_USAGE
    days, firings = health.replay(state_dir_path(), cfg, args.day)
    chosen = args.day or datetime.fromtimestamp(now(), timezone.utc).strftime("%Y-%m-%d")
    source, notes = "the recorded hourly series", None
    if args.from_audit or (firings is None and args.day):
        firings, notes = health.replay_audit(state_dir_path(), cfg, chosen)
        source = "the audit log and its archives (best effort)"
    if firings is None:
        print("no recorded day%s and no audit log to rebuild it from; recorded days: %s" % (" " + args.day if args.day else "", ", ".join(days) or "none"))
        return EXIT_USAGE if args.day else EXIT_OK
    day = chosen if source != "the recorded hourly series" else (args.day or days[-1])
    if args.json:
        print(json.dumps({"day": day, "source": source, "not_recorded": notes, "days": days, "firings": firings}, indent=2, sort_keys=True))
        return EXIT_OK
    print("load alert replay over %s from %s (thresholds: %d stalled ask(s) of %g h, backlog %g h); recorded days: %s" % (
        day, source, cfg["stalled_asks"], cfg["stalled_hours"], cfg["backlog_hours"], ", ".join(days) or "none"))
    if notes:
        print("not recorded for it: %s" % notes)
    print("would have fired %d time(s)%s" % (len(firings), ":" if firings else ""))
    for firing in firings:
        print("  %s  %s  %s" % (firing["hour"], firing["orchestrator"], firing["text"]))
    return EXIT_OK


cmd_load_replay.direct = True   # reads a file: no daemon start


def add_load_replay_parsers(sub):
    replay = sub.add_parser("load-replay", help="When the load alert WOULD have fired over a recorded day of the hourly load series (read-only)")
    replay.add_argument("--day", help="A UTC date, YYYY-MM-DD (default: the newest recorded day; the last few days are kept)")
    replay.add_argument("--stalled-asks", type=int, default=None, help="Try another threshold: asks stalled")
    replay.add_argument("--stalled-hours", type=float, default=None, help="Try another threshold: hours without movement")
    replay.add_argument("--backlog-hours", type=float, default=None, help="Try another threshold: hours of moved asks in the backlog test")
    replay.add_argument("--from-audit", action="store_true", help="Rebuild the day from the audit log and its archives even when the series holds it (a past day is rebuilt this way when the series does not)")
    replay.add_argument("--json", action="store_true")
