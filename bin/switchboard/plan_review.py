# ---------------------------------------------------------------- `plan review` (#109 part 2, slice R2)
#
# docs/plan-review-engine-design.md. R2: the ledger records, and `start`, `verdict`, `respond`, `status`, `abandon`, with real lane launching
# (the existing `launch_agent`; tests use the fake herdr). Inert by default: `plan_review.enabled` is false. It writes the ledger and audit
# and launches lane sessions; it writes nothing to GitHub. NOT here (later slices): the daemon's read of a lane's final message (R3), the
# closing round, the consensus write, escalation and `decide` (R4), `report` (R5). So no consensus record is ever written by this slice.

class ReviewError(Exception):
    """A `plan review` command that cannot go on (exit 2): said in one line, nothing was recorded."""


REVIEW_ACTIONS = ("start", "verdict", "respond", "status", "abandon", "decide", "tick", "close", "report")
DECISIONS_HEADING_RE = re.compile(r"(?m)^##[ \t]+Decisions[ \t]*$")
REVIEW_TABLE_MAX = 65536     # bytes read from a response table or a verdict block
REVIEW_OBJECTIONS_MAX = 20   # objections in one verdict


def _read_capped(path, limit):
    """The text of a file or stdin (`-`), refused when it is over `limit` bytes: one entry cannot bloat the ledger."""
    text = sys.stdin.read(limit + 1) if path in (None, "-") else Path(path).read_text(encoding="utf-8")
    if len(text.encode("utf-8")) > limit:
        raise ReviewError("the input is over %d bytes" % limit)
    return text
PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_.@~+-]*(?:/[A-Za-z0-9_.@~+-]+)*")


def review_lane_send_problem(config, recipient, sender):
    """A plan review lane takes no message from an agent: its brief is its only input (the plan is untrusted text, and so is any message).
    Only the switchboard itself may send to a lane of an open review. Applies only when the engine is on; fails open (None) on any trouble."""
    try:
        if sender == PROG or not (config.get("plan_review") or {}).get("enabled"):
            return None
        entries, problem = plan_ledger.read_ledger(ensure_state_dir())
        if problem or not any(e.get("kind") == "lane_session" and e.get("session") == recipient for e in entries):
            return None
        review = next(e["review"] for e in entries if e.get("kind") == "lane_session" and e.get("session") == recipient)
        rnd = _round_of(entries, review)
        if plan_review.lineage_state([e for e in entries if e.get("lineage") == rnd["lineage"]], rnd["plan_hash"])["open_review"]:
            return "%s is a reviewer lane of the open plan review %s: it takes no message from an agent" % (recipient, review)
    except Exception:
        return None
    return None


def review_config(config):
    cfg = config.get("plan_review")
    if not cfg or not cfg.get("enabled"):
        raise ReviewError("the plan review engine is off: set `plan_review.enabled` to true in the switchboard config")
    return cfg


def review_entries(state):
    entries, problem = plan_ledger.read_ledger(state)
    if problem:
        raise ReviewError("the ledger is not trustworthy, nothing was recorded: %s" % problem)
    return entries


def review_caller(runner, now, who):
    """(kind, name): `start` and `abandon` are for a person at a shell or a registered orchestrator or the chief of staff; `respond` is
    the planner (the round's recorded starter)."""
    got = gates_caller_kind(runner, now)
    if got[0] == "person":
        return "person", "owner"
    if got[0] == "agent" and got[2] in ("orchestrator", "chief-of-staff"):
        return "agent", got[1]
    raise ReviewError("%s may not %s: only a registered orchestrator, the chief of staff, or a person at a shell" % (got[1], who))


def _review_record(engine, state, entry, now):
    stored = plan_ledger.append_entry(state, entry, now())
    if engine.audit is not None:
        engine.audit.append(engine.actor, "plan.review.%s" % entry["kind"], entry.get("lineage") or "-",
                            {"review": entry.get("review"), "round": entry.get("round"), "lane": entry.get("lane"),
                             "seq": stored["seq"], "chain": stored["hash"]})
    return stored


def _plan_ask_problem(config, runner, now, plan_repo, plan_number, ask_repo, ask_number):
    """None when the plan issue IS the ask or sits under it (its parent chain, up to 6 links, reaches the ask), read from GitHub with the
    repo's account like `plan check`; else a sentence. The caller's `--ask` is only a claim: this is what makes the lineage the ask's."""
    if (plan_repo.lower(), plan_number) == (ask_repo.lower(), ask_number):
        return None
    reader = IntentReader(config["gh"], plan_repo, runner, now, config["call_timeout_seconds"], accounts=GhAccounts(config, runner, now))
    repo, number = plan_repo, plan_number
    for _ in range(6):
        issue = reader._api("repos/%s/issues/%d" % (repo, number), repo)
        parent = parent_of(issue) if isinstance(issue, dict) else None
        if not parent:
            break
        if (repo.lower(), parent) == (ask_repo.lower(), ask_number):
            return None
        number = parent
    return "%s#%d is not the ask's plan: its issue is not %s#%d and does not sit under it (a plan is reviewed under the ask it serves)" % (
        plan_repo, plan_number, ask_repo, ask_number)


def _unit_paths(text):
    """Every path-like token in the plan text: a plan that names a held path anywhere is reviewed in full (err to full; the plan's own
    claim about its areas is not read)."""
    return sorted({m for m in PATH_TOKEN_RE.findall(text or "") if "/" in m or "." in m})


def _ask_facts(config, runner, now, ask_text, default_repo):
    """(repo, number, kind, parent lineage or None, intent, done_when) of the ask a plan serves, from the issue itself: a charter is an
    `epic`, an issue with a parent a `work_item`, else an `ask`."""
    repo, number, _ = parse_plan_source(ask_text, default_repo)
    reader = IntentReader(config["gh"], repo, runner, now, config["call_timeout_seconds"], accounts=GhAccounts(config, runner, now))
    issue = reader._api("repos/%s/issues/%d" % (repo, number), repo)
    if not isinstance(issue, dict):
        raise ReviewError("cannot read the ask %s#%d" % (repo, number))
    parent = parent_of(issue)
    charter = bool(_CHARTER_BLOCK.search(issue.get("body") if isinstance(issue.get("body"), str) else ""))
    kind = "work_item" if parent else ("epic" if charter else "ask")
    found = IssueIntent.from_api(issue)
    return (repo, number, kind, plan_review.lineage_key(repo, parent) if parent else None, found.intent or "", found.done_when or "")


def _abandoned(entries, review):
    """True when the review was abandoned: it takes no verdict, no response, no closing round and no consensus (a new `start` for the ask is how
    it goes on: the lineage's count continues)."""
    return any(e.get("kind") == "abandoned" and e.get("review") == review for e in entries)


def _refuse_if_abandoned(entries, review):
    if _abandoned(entries, review):
        raise ReviewError("refused: review %s was abandoned: it takes no more verdicts, responses, closing round or consensus "
                          "(`plan review start` for the same ask opens a new review and the count continues)" % review)


def _round_of(entries, review):
    rounds = [e for e in entries if e.get("kind") == "round" and e.get("review") == review]
    if not rounds:
        raise ReviewError("no review %s" % review)
    return rounds[-1]


def _sessions_of(entries, review, number):
    return {e["lane"]: e["session"] for e in entries if e.get("kind") == "lane_session" and e.get("review") == review
            and e.get("round") == number}


def _verdicts_of(entries, review, number):
    return {e["lane"]: e for e in entries if e.get("kind") == "lane" and e.get("review") == review and e.get("round") == number}


def _carried(entries, lineage, lane):
    """The lane's own open blocking objections from the lineage's earlier rounds (what its next round must answer)."""
    mine = [e for e in entries if e.get("lineage") == lineage and e.get("kind") == "lane" and e.get("lane") == lane]
    latest = {}
    for entry in mine:
        for obj in entry.get("objections") or []:
            latest[obj["id"]] = dict(obj, lane=lane)
    return [o for o in latest.values() if o.get("severity") == "blocking" and o.get("state") not in plan_review.CLOSED_STATES]


def _launch_lanes(engine, state, cfg, round_entry, text, intent, done_when, now, planner):
    """Launch up to `max_parallel` of the round's waiting lanes (the rest wait for a verdict). Each success is recorded as a `lane_session`."""
    entries = review_entries(state)
    launched = _sessions_of(entries, round_entry["review"], round_entry["round"])
    active = [l for l in launched if l not in _verdicts_of(entries, round_entry["review"], round_entry["round"])]
    room = max(0, cfg["max_parallel"] - len(active))
    workspace = cfg.get("workspace") or os.environ.get("HERDR_WORKSPACE_ID")
    directory = cfg.get("dir") or os.getcwd()
    made = []
    for lane in [l for l in round_entry["lanes"] if l not in launched][:room]:
        name = "planrev-%s-%s-r%d" % (round_entry["review"], lane.lower(), round_entry["round"])
        brief = plan_review.lane_brief(lane, round_entry["plan_ref"], round_entry["plan_hash"], text, intent, done_when,
                                       carried=_carried(entries, round_entry["lineage"], lane), closing=bool(round_entry.get("closing")))
        try:
            done = launch_agent(engine, name, cfg["lane_agent"], directory, workspace, role="reviewer", reports_to=None,
                                issue=round_entry.get("ask_number"), brief=brief)
        except LaunchError as err:
            raise ReviewError("lane %s did not launch (%s): the review stays open and the lane waits" % (lane, _one_line(str(err), 160))) from None
        _review_record(engine, state, {"kind": "lane_session", "lineage": round_entry["lineage"], "review": round_entry["review"],
                                       "round": round_entry["round"], "lane": lane, "session": name, "pane": done.get("pane"),
                                       "by": planner}, now)
        made.append((lane, name, done.get("pane")))
    return made


def _ask_texts(config, runner, now, rnd):
    """(intent, done_when) of the round's ask, read again for the next round's briefs."""
    repo, _, number = rnd["lineage"].rpartition("#")
    got = _ask_facts(config, runner, now, "%s#%s" % (repo, number), config.get("intent_repo"))
    return got[4], got[5]


def review_start(args, runner, now, socket_call=None):
    config = load_config()
    cfg = review_config(config)
    _, planner = review_caller(runner, now, "start a plan review")
    engine, state, _ = build_engine(runner, now, "cli", writable=True, socket_call=socket_call)
    default_repo = config.get("intent_repo")
    ns = types.SimpleNamespace(source=args.source, file=None, ask=args.ask, done_when=None)
    text, label, result = load_plan(ns, runner, now)
    if result["result"] != "pass":
        raise ReviewError("the plan fails `plan check` (%d defect(s)): fix it first" % len(result["defects"]))
    plan_hash = result["plan_hash"]
    ask_repo, ask_number, kind, parent_lineage, ask_intent, ask_done = _ask_facts(config, runner, now, args.ask, default_repo)
    lineage = plan_review.lineage_key(ask_repo, ask_number)
    plan_repo, plan_number, _ = parse_plan_source(args.source, default_repo)
    plan_ref = "%s#%d" % (plan_repo, plan_number)
    problem = _plan_ask_problem(config, runner, now, plan_repo, plan_number, ask_repo, ask_number)
    if problem:
        raise ReviewError("refused: %s" % problem)
    hits = plan_review.path_hits(_unit_paths(text), cfg["held_globs"])
    chosen = {}

    def decide(entries, seq):
        """Everything that decides whether this review may open, and its id, under the ledger lock with the ledger as it stands."""
        for entry in entries:   # a plan source belongs to ONE ask
            if entry.get("kind") == "round" and entry.get("plan_ref") == plan_ref and entry.get("lineage") != lineage:
                raise ReviewError("%s is already under the review of %s: a plan does not move to another ask's count" % (plan_ref, entry["lineage"]))
        mine = [e for e in entries if e.get("lineage") == lineage]
        ok, why = plan_review.may_start(plan_review.lineage_state(mine, plan_hash), plan_hash)
        if not ok:
            raise ReviewError("refused: %s" % why)
        day_ago = now() - 86400
        if not plan_review.starts_today_ok(len([e for e in entries if e.get("kind") == "round" and e.get("start") and e.get("by") == planner
                                               and e.get("ts", 0) >= day_ago]), cfg["starts_per_day"]):
            raise ReviewError("refused: %s has started %d reviews in the last day (the limit)" % (planner, cfg["starts_per_day"]))
        has_consensus = bool(parent_lineage) and any(e.get("kind") == "consensus" and e.get("lineage") == parent_lineage for e in entries)
        tier, tier_why = plan_review.compute_tier(kind, hits, cfg["held_globs"], ask_has_consensus=has_consensus, requested=args.tier)
        lanes = plan_review.lanes_for_tier(tier, held_area=bool(hits))
        used = len([e for e in mine if e.get("kind") == "lane_session"])
        if not plan_review.session_budget_ok(used, len(lanes), cfg["session_cap"]):
            raise ReviewError("refused: the lineage has used %d of its %d lane sessions" % (used, cfg["session_cap"]))
        chosen.update(review="rv%d" % seq, tier=tier, why=tier_why, lanes=lanes)   # the id comes from the appended sequence number
        return {"kind": "round", "lineage": lineage, "review": chosen["review"], "round": 1, "closing": False, "start": True,
                "ask_number": ask_number, "plan_ref": plan_ref, "plan_hash": plan_hash, "scenario_hash": result.get("scenario_hash"),
                "tier": tier, "tier_why": tier_why, "lanes": lanes, "held_area": bool(hits), "by": planner}

    stored = plan_ledger.append_checked(state, now(), decide)
    if engine.audit is not None:
        engine.audit.append(engine.actor, "plan.review.round", lineage, {"review": chosen["review"], "round": 1, "seq": stored["seq"],
                                                                            "chain": stored["hash"]})
    made = _launch_lanes(engine, state, cfg, stored, text, ask_intent, ask_done, now, planner)
    return {"review": chosen["review"], "lineage": lineage, "tier": chosen["tier"], "why": chosen["why"], "lanes": chosen["lanes"],
            "launched": made, "plan_hash": plan_hash}


def _launch_waiting(engine, state, cfg, config, runner, now, rnd):
    """After a verdict: launch the round's waiting lanes (up to `max_parallel` running). Best effort: a launch problem is returned, not raised."""
    entries = review_entries(state)
    sessions, verdicts = _sessions_of(entries, rnd["review"], rnd["round"]), _verdicts_of(entries, rnd["review"], rnd["round"])
    if all(l in sessions for l in rnd["lanes"]) or all(l in verdicts for l in rnd["lanes"]):
        return [], None
    try:
        ns = types.SimpleNamespace(source=rnd["plan_ref"], file=None, ask=None, done_when=None)
        text = load_plan(ns, runner, now)[0]
        made = _launch_lanes(engine, state, cfg, rnd, text, *_ask_texts(config, runner, now, rnd), now, rnd["by"])
        return made, None
    except (ReviewError, PlanInputError, ValueError, ConfigError, IntentError) as err:
        return [], _one_line(str(err), 200)


def review_verdict(args, runner, now, socket_call=None):
    """The fallback path (the daemon's own read of a lane's final message is R3): a lane records its verdict for its own lane."""
    config = load_config()
    cfg = review_config(config)
    engine, state, _ = build_engine(runner, now, "cli", writable=True, socket_call=socket_call)
    entries = review_entries(state)
    _refuse_if_abandoned(entries, args.review)
    rnd = _round_of(entries, args.review)
    caller = identify_caller(engine.herdr, engine.v2, os.environ)
    sessions = _sessions_of(entries, args.review, rnd["round"])
    if sessions.get(args.lane) != caller:
        raise ReviewError("refused: %s is not the session launched for lane %s of this round (that is %s)" % (
            caller, args.lane, sessions.get(args.lane) or "not launched"))
    if caller == rnd["by"]:
        raise ReviewError("refused: the planner cannot record a lane's verdict")
    if args.lane in _verdicts_of(entries, args.review, rnd["round"]):
        raise ReviewError("refused: lane %s already has its verdict for this round" % args.lane)
    text = _read_capped(args.file, REVIEW_TABLE_MAX)
    parsed, why = plan_review.parse_verdict_block(text, rnd["plan_hash"], args.lane)
    if parsed is None:
        raise ReviewError("refused: %s" % why)
    if len(parsed["objections"]) > REVIEW_OBJECTIONS_MAX:
        raise ReviewError("refused: %d objections in one verdict (at most %d): say the strongest" % (len(parsed["objections"]), REVIEW_OBJECTIONS_MAX))
    entry = {"kind": "lane", "lineage": rnd["lineage"], "review": args.review, "round": rnd["round"], "lane": args.lane,
             "verdict": parsed["verdict"], "plan_hash": rnd["plan_hash"], "objections": parsed["objections"], "by": caller}
    def guarded(ledger, seq):
        _refuse_if_abandoned(ledger, args.review)   # under the lock: an abandon that landed meanwhile still wins
        if any(e.get("kind") == "lane" and e.get("review") == args.review and e.get("round") == rnd["round"] and e.get("lane") == args.lane for e in ledger):
            raise ReviewError("refused: lane %s already has its verdict for this round" % args.lane)
        return entry
    stored = plan_ledger.append_checked(state, now(), guarded)
    if engine.audit is not None:
        engine.audit.append(engine.actor, "plan.review.lane", rnd["lineage"], {"review": args.review, "round": rnd["round"], "lane": args.lane,
                                                                                "seq": stored["seq"], "chain": stored["hash"]})
    made, trouble = _launch_waiting(engine, state, cfg, config, runner, now, rnd)
    out = {"recorded": args.lane, "verdict": parsed["verdict"], "objections": len(parsed["objections"]),
           "launched_next": [lane for lane, _, _ in made]}
    if trouble:
        out["waiting_lanes_not_launched"] = trouble
    if rnd.get("closing"):
        out.update(_closing_outcome(engine, state, cfg, config, runner, now, args.review))
    return out


def _closing_outcome(engine, state, cfg, config, runner, now, review):
    """Once every lane of a closing round has reported: write the consensus (every lane of the tier approved the exact final hash and nothing
    blocking is open), or escalate a blocking objection raised on the final text, or say it is still pending. The tally is computed from the
    ledger alone; the consensus is bound to the final plan hash."""
    entries = review_entries(state)
    rnd = _round_of(entries, review)
    if _abandoned(entries, review):
        return {}
    if not rnd.get("closing") or any(l not in _verdicts_of(entries, review, rnd["round"]) for l in rnd["lanes"]):
        return {}
    lineage = [e for e in entries if e.get("lineage") == rnd["lineage"]]
    st = plan_review.lineage_state(lineage, rnd["plan_hash"])
    lane_entries = [e for e in lineage if e.get("kind") == "lane"]
    verdict, why = plan_review.decide_consensus(lane_entries, rnd["lanes"], rnd["plan_hash"], rounds_left=plan_review._rounds_left(st))
    if verdict == "escalate":
        out = _escalate(engine, state, cfg, config, runner, now, rnd, st, "", rnd["by"])
        return {"tally": "escalate", "tally_why": why, "escalated": out["escalated"], "boss": out["boss"], "owner_only": out["owner_only"]}
    if verdict != "consensus":
        return {"tally": verdict, "tally_why": why}

    def write(ledger, seq):
        """Under the lock, from the ledger as it stands: the same tally, and not twice for one hash."""
        _refuse_if_abandoned(ledger, review)
        mine = [e for e in ledger if e.get("lineage") == rnd["lineage"]]
        again, _ = plan_review.decide_consensus([e for e in mine if e.get("kind") == "lane"], rnd["lanes"], rnd["plan_hash"],
                                                rounds_left=plan_review._rounds_left(plan_review.lineage_state(mine, rnd["plan_hash"])))
        if again != "consensus":
            raise ReviewError("the tally changed: no consensus")
        if any(e.get("kind") == "consensus" and e.get("plan_hash") == rnd["plan_hash"] for e in ledger):
            raise ReviewError("a consensus already exists at this hash")
        return {"kind": "consensus", "result": "pass", "pr": "-", "scenario": "-", "lineage": rnd["lineage"], "review": review,
                "plan_hash": rnd["plan_hash"], "plan_ref": rnd["plan_ref"], "tier": rnd["tier"], "lanes": rnd["lanes"],
                "rounds": st["rounds"], "closing_round": rnd["round"], "by": "plan-review"}
    try:
        stored = plan_ledger.append_checked(state, now(), write)
    except ReviewError as err:
        return {"tally": "pending", "tally_why": str(err)}
    if engine.audit is not None:
        engine.audit.append(engine.actor, "plan.review.consensus", rnd["lineage"], {"review": review, "plan_hash": rnd["plan_hash"],
                                                                                    "seq": stored["seq"], "chain": stored["hash"]})
    return {"tally": "consensus", "consensus_at": rnd["plan_hash"]}


def review_close(args, runner, now, socket_call=None):
    """The closing round: the planner publishes the FINAL synthesized revision (`plan check` PASS, a `## Decisions` section); every lane of the tier
    then confirms that exact text or objects. Only an approve at that hash from every lane makes a consensus: no closing round, no consensus.
    Outside the 3-round cap, once per final hash, never with a blocking objection open or an escalation undecided."""
    config = load_config()
    cfg = review_config(config)
    _, planner = review_caller(runner, now, "close a plan review")
    engine, state, _ = build_engine(runner, now, "cli", writable=True, socket_call=socket_call)
    entries = review_entries(state)
    _refuse_if_abandoned(entries, args.review)
    rnd = _round_of(entries, args.review)
    if planner != rnd["by"] and planner != "owner":
        raise ReviewError("refused: only the planner who started the review (%s) closes it" % rnd["by"])
    missing = [l for l in rnd["lanes"] if l not in _verdicts_of(entries, args.review, rnd["round"])]
    if missing:
        raise ReviewError("refused: lanes %s have not reported for round %d" % (", ".join(missing), rnd["round"]))
    ns = types.SimpleNamespace(source=args.plan_ref, file=None, ask=None, done_when=None)
    text, label, result = load_plan(ns, runner, now)
    if result["result"] != "pass":
        raise ReviewError("the final revision fails `plan check` (%d defect(s))" % len(result["defects"]))
    if not DECISIONS_HEADING_RE.search(text):
        raise ReviewError("the final revision has no `## Decisions` section (the synthesis the planner owes: decision, drivers, alternatives, why)")
    plan_repo, plan_number, _ = parse_plan_source(args.plan_ref, config.get("intent_repo"))
    ask_repo, _, ask_number = rnd["lineage"].rpartition("#")
    problem = _plan_ask_problem(config, runner, now, plan_repo, plan_number, ask_repo, int(ask_number))
    if problem:
        raise ReviewError("refused: %s" % problem)
    lanes = plan_review.lanes_for_tier(rnd["tier"], held_area=bool(rnd.get("held_area")))
    used = len([e for e in entries if e.get("lineage") == rnd["lineage"] and e.get("kind") == "lane_session"])
    if not plan_review.session_budget_ok(used, len(lanes), cfg["session_cap"]):
        raise ReviewError("refused: the lineage has used %d of its %d lane sessions" % (used, cfg["session_cap"]))
    plan_ref = "%s#%d" % (plan_repo, plan_number)

    def decide(ledger, seq):
        _refuse_if_abandoned(ledger, args.review)
        mine = [e for e in ledger if e.get("lineage") == rnd["lineage"]]
        later = [e for e in ledger if e.get("kind") == "round" and e.get("review") == args.review and e.get("round") > rnd["round"]]
        if later:
            raise ReviewError("refused: round %d of this review is already open" % later[-1]["round"])
        ok, why = plan_review.may_open_round(plan_review.lineage_state(mine, result["plan_hash"]), closing=True, plan_hash=result["plan_hash"])
        if not ok:
            raise ReviewError("refused: %s" % why)
        return {"kind": "round", "lineage": rnd["lineage"], "review": args.review, "round": rnd["round"] + 1, "closing": True,
                "ask_number": rnd.get("ask_number"), "plan_ref": plan_ref, "plan_hash": result["plan_hash"],
                "scenario_hash": result.get("scenario_hash"), "tier": rnd["tier"], "lanes": lanes,
                "held_area": rnd.get("held_area", False), "by": rnd["by"]}

    stored = plan_ledger.append_checked(state, now(), decide)
    made = _launch_lanes(engine, state, cfg, stored, text, *_ask_texts(config, runner, now, rnd), now, rnd["by"])
    return {"review": args.review, "closing_round": stored["round"], "lanes": lanes, "launched": made, "plan_hash": result["plan_hash"]}


def review_report(args, runner, now):
    """What the review cost and what the reviewers caught: per round and lane the wall time, the verdict and the objections by severity, how many
    the planner accepted and rejected, the objection titles, the totals, and the rounds to consensus. Token and money cost are NOT measured (the
    harness records none yet): it says so, and records the session names so a later measure can join them."""
    config = load_config()
    review_config(config)
    state = ensure_state_dir()
    entries = review_entries(state)
    rnd = _round_of(entries, args.review)
    mine = [e for e in entries if e.get("lineage") == rnd["lineage"]]
    rounds = sorted([e for e in mine if e.get("kind") == "round" and e.get("review") == args.review], key=lambda e: e["round"])
    out_rounds, caught, total_sessions = [], [], 0
    for r in rounds:
        starts = {}
        for e in mine:
            if e.get("kind") == "lane_session" and e.get("review") == args.review and e.get("round") == r["round"]:
                starts.setdefault(e["lane"], e["ts"])
                total_sessions += 1
        lanes = {}
        for e in mine:
            if e.get("kind") == "lane" and e.get("review") == args.review and e.get("round") == r["round"]:
                blocking = len([o for o in e["objections"] if o["severity"] == "blocking"])
                lanes[e["lane"]] = {"verdict": e["verdict"], "blocking": blocking, "advisory": len(e["objections"]) - blocking,
                                    "seconds": e["ts"] - starts.get(e["lane"], e["ts"])}
                for o in e["objections"]:
                    if o["state"] == "open":
                        caught.append({"round": r["round"], "lane": e["lane"], "id": o["id"], "severity": o["severity"], "text": o["text"]})
        responses = [e for e in mine if e.get("kind") == "response" and e.get("review") == args.review and e.get("round") == r["round"]]
        out_rounds.append({"round": r["round"], "closing": bool(r.get("closing")), "lanes": lanes,
                           "accepted": sum(e.get("accepted", 0) for e in responses), "rejected": sum(e.get("rejected", 0) for e in responses)})
    consensus = next((e for e in reversed(mine) if e.get("kind") == "consensus" and e.get("review") == args.review), None)
    end = consensus["ts"] if consensus else max(e["ts"] for e in mine)
    return {"review": args.review, "lineage": rnd["lineage"], "tier": rounds[0]["tier"], "rounds": out_rounds,
            "rounds_to_consensus": len([r for r in rounds if not r.get("closing")]) if consensus else None,
            "consensus": ({"plan_hash": consensus["plan_hash"], "overridden_by": consensus.get("overridden_by")} if consensus else None),
            "escalations": len([e for e in mine if e.get("kind") == "escalation" and e.get("review") == args.review]),
            "sessions": total_sessions, "wall_seconds": end - rounds[0]["ts"],
            "caught": caught[:60], "caught_total": len(caught),
            "cost": "tokens and money are not measured (the harness records none yet); the lane session names are in the ledger to join a later measure"}


def review_status(args, runner, now):
    config = load_config()
    review_config(config)
    state = ensure_state_dir()
    entries = review_entries(state)
    rnd = _round_of(entries, args.review)
    mine = [e for e in entries if e.get("lineage") == rnd["lineage"]]
    st = plan_review.lineage_state(mine, rnd["plan_hash"])
    sessions, verdicts = _sessions_of(entries, args.review, rnd["round"]), _verdicts_of(entries, args.review, rnd["round"])
    lanes = {lane: ("verdict:%s" % verdicts[lane]["verdict"] if lane in verdicts else "running" if lane in sessions else "waiting")
             for lane in rnd["lanes"]}
    return {"review": args.review, "lineage": rnd["lineage"], "round": rnd["round"], "tier": rnd["tier"], "plan_hash": rnd["plan_hash"],
            "rounds_used": st["rounds"], "lanes": lanes, "open_blocking": st["open_blocking"], "open_review": st["open_review"],
            "undecided_escalation": st["undecided"],
            "overridden": st["overridden"].get(rnd["plan_hash"]), "consensus": rnd["plan_hash"] in st["consensus_hashes"],
            "closing": bool(rnd.get("closing")),
            "next": ("respond: the planner answers the open objections" if all(l in verdicts for l in rnd["lanes"]) and st["open_blocking"]
                     else ("the consensus is recorded" if rnd["plan_hash"] in st["consensus_hashes"]
                           else "tally pending: see `plan review close` (the closing round)") if all(l in verdicts for l in rnd["lanes"])
                     else "waiting for the lanes")}


def review_respond(args, runner, now, socket_call=None):
    """The planner answers a finished round: the response table (every open blocking objection named), the new revision (`plan check`
    PASS, a new hash), and the next round for the lanes that still object (plus L1 when the scenarios changed)."""
    config = load_config()
    cfg = review_config(config)
    _, planner = review_caller(runner, now, "respond to a plan review")
    engine, state, _ = build_engine(runner, now, "cli", writable=True, socket_call=socket_call)
    entries = review_entries(state)
    _refuse_if_abandoned(entries, args.review)
    rnd = _round_of(entries, args.review)
    if planner != rnd["by"] and planner != "owner":
        raise ReviewError("refused: only the planner who started the review (%s) answers it" % rnd["by"])
    verdicts = _verdicts_of(entries, args.review, rnd["round"])
    missing = [l for l in rnd["lanes"] if l not in verdicts]
    if missing:
        raise ReviewError("refused: lanes %s have not reported for round %d" % (", ".join(missing), rnd["round"]))
    mine = [e for e in entries if e.get("lineage") == rnd["lineage"]]
    st = plan_review.lineage_state(mine, rnd["plan_hash"])
    if not st["open_blocking"]:
        raise ReviewError("nothing blocking is open: the next step is the closing round (`plan review close`), not another round")
    ok, why = plan_review.may_open_round(st)
    if not ok and st["undecided"]:
        raise ReviewError("refused: %s" % why)
    table = _read_capped(args.table, REVIEW_TABLE_MAX)
    if not ok:   # the rounds are spent: what survives ESCALATES (recorded, and the boss told), it is never dropped
        return _escalate(engine, state, cfg, config, runner, now, rnd, st, table, planner)
    answered = {m.group(1) for m in re.finditer(r"(?m)^\s*([A-Za-z0-9]+/O[0-9]{1,3})\s*:\s*(?:accept|reject)\b", table)}
    absent = [k for k in st["open_blocking"] if k not in answered]
    if absent:
        raise ReviewError("refused: the response table needs one line `<lane>/<id>: accept|reject, why` for each open objection; missing %s"
                          % ", ".join(absent))
    ns = types.SimpleNamespace(source=args.plan_ref, file=None, ask=None, done_when=None)
    text, label, result = load_plan(ns, runner, now)
    if result["result"] != "pass":
        raise ReviewError("the new revision fails `plan check` (%d defect(s))" % len(result["defects"]))
    if result["plan_hash"] == rnd["plan_hash"]:
        raise ReviewError("the revision has the same plan hash: a new round needs a changed plan")
    plan_repo, plan_number, _ = parse_plan_source(args.plan_ref, config.get("intent_repo"))
    ask_repo, _, ask_number = rnd["lineage"].rpartition("#")
    problem = _plan_ask_problem(config, runner, now, plan_repo, plan_number, ask_repo, int(ask_number))
    if problem:
        raise ReviewError("refused: %s" % problem)
    # the next lanes come from the lineage's OPEN objections (their lanes), plus L1 when the scenarios changed: a lane-wide approve that
    # omitted an id leaves it open, and its lane runs again; a round with no lane to run is refused, never opened.
    objecting = {k.split("/", 1)[0] for k in st["open_blocking"]}
    if result.get("scenario_hash") != rnd.get("scenario_hash"):
        objecting.add("L1")
    lanes = sorted(objecting & set(plan_review.LANES))
    if not lanes:
        raise ReviewError("refused: no lane has an open objection to re-run: nothing would review the new round")
    used = len([e for e in mine if e.get("kind") == "lane_session"])
    if not plan_review.session_budget_ok(used, len(lanes), cfg["session_cap"]):
        raise ReviewError("refused: the lineage has used %d of its %d lane sessions" % (used, cfg["session_cap"]))
    plan_ref = "%s#%d" % (plan_repo, plan_number)

    def decide(entries, seq):
        """Under the ledger lock: this review is still at the round the planner answered, and the round may open."""
        _refuse_if_abandoned(entries, args.review)
        later = [e for e in entries if e.get("kind") == "round" and e.get("review") == args.review and e.get("round") > rnd["round"]]
        if later:
            raise ReviewError("refused: round %d of this review is already open" % later[-1]["round"])
        now_state = plan_review.lineage_state([e for e in entries if e.get("lineage") == rnd["lineage"]], rnd["plan_hash"])
        allowed, why_not = plan_review.may_open_round(now_state)
        if not allowed:
            raise ReviewError("refused: %s" % why_not)
        return {"kind": "round", "lineage": rnd["lineage"], "review": args.review, "round": rnd["round"] + 1, "closing": False,
                "ask_number": rnd.get("ask_number"), "plan_ref": plan_ref, "plan_hash": result["plan_hash"],
                "scenario_hash": result.get("scenario_hash"), "tier": rnd["tier"], "lanes": lanes,
                "held_area": rnd.get("held_area", False), "by": rnd["by"]}

    stored = plan_ledger.append_checked(state, now(), decide)
    _review_record(engine, state, {"kind": "response", "lineage": rnd["lineage"], "review": args.review, "round": rnd["round"],
                                   "table_hash": hashlib.sha256(table.encode("utf-8")).hexdigest(), "plan_ref": plan_ref,
                                   "accepted": len(re.findall(r"(?m)^\s*[A-Za-z0-9]+/O[0-9]{1,3}\s*:\s*accept\b", table)),
                                   "rejected": len(re.findall(r"(?m)^\s*[A-Za-z0-9]+/O[0-9]{1,3}\s*:\s*reject\b", table)),
                                   "plan_hash": result["plan_hash"], "by": planner}, now)
    made = _launch_lanes(engine, state, cfg, stored, text, *_ask_texts(config, runner, now, rnd), now, rnd["by"])
    return {"review": args.review, "round": stored["round"], "lanes": lanes, "launched": made, "plan_hash": result["plan_hash"]}


def _boss_of_planner(engine, planner):
    """The agent that decides an escalation: the planner's boss, else the chief of staff (a person planner has none)."""
    discovery = engine.discover(engine.now())
    me = discovery.agents.get(planner)
    boss = boss_name(discovery, me) if me is not None else None
    return boss or resolve_boss(discovery, "chief-of-staff") or "chief-of-staff"


def _escalation_target(engine, planner, already_escalated=False):
    """(boss, owner_only) for an escalation: the planner's boss, else the chief of staff; but a planner that IS that boss (the chief of staff's
    own plan) cannot judge its own plan, so the escalation goes to the owner directly, as does any escalation after the rounds that follow
    an earlier one."""
    boss = _boss_of_planner(engine, planner)
    if already_escalated or boss == planner:
        return "owner", True
    return boss, False


def _tell(engine, recipient, text):
    """The switchboard's own message through the outbox (a sender no agent can be). Never raises: the ledger holds the fact either way."""
    try:
        outcome = engine.send(PROG, recipient, text)
        return bool(outcome.get("sent"))
    except (MessageTooLong, InterruptRefused, Exception):
        return False


def _response_of(table, key):
    match = re.search(r"(?m)^\s*%s\s*:\s*((?:accept|reject)\b.*)$" % re.escape(key), table or "")
    return match.group(1).strip() if match else ""


def _escalate(engine, state, cfg, config, runner, now, rnd, st, table, planner):
    """Record an `escalation` entry for every blocking objection still open (with the planner's response beside it) and tell the boss. After an
    earlier escalation the rounds that follow it are spent: the escalation is then the owner's alone."""
    boss, owner_only = _escalation_target(engine, planner, bool(st["escalated"]))
    told = []
    lanes_entries = [e for e in review_entries(state) if e.get("lineage") == rnd["lineage"] and e.get("kind") == "lane"]
    objections = {}
    for entry in lanes_entries:
        for obj in entry.get("objections") or []:
            objections["%s/%s" % (entry["lane"], obj["id"])] = dict(obj, lane=entry["lane"])
    for key in st["open_blocking"]:
        obj = objections.get(key, {"id": key.split("/", 1)[-1], "lane": key.split("/", 1)[0], "text": ""})
        response = _response_of(table, key)
        def once(entries, seq, key=key, obj=obj, response=response):
            """Under the ledger lock: an objection already escalated and undecided is not escalated (or announced) twice."""
            if key in _escalations(entries, rnd["review"]):
                raise ReviewError("%s is already escalated" % key)
            return {"kind": "escalation", "lineage": rnd["lineage"], "review": rnd["review"], "round": rnd["round"],
                    "objection": key, "lane": obj["lane"], "text": plan_ledger.clip(obj.get("text"), 400),
                    "response": plan_ledger.clip(response, 400), "boss": boss, "owner_only": owner_only,
                    "plan_hash": rnd["plan_hash"], "held_area": bool(rnd.get("held_area")), "by": planner}
        try:
            stored = plan_ledger.append_checked(state, now(), once)
        except ReviewError:
            continue
        if engine.audit is not None:
            engine.audit.append(engine.actor, "plan.review.escalation", rnd["lineage"], {"review": rnd["review"], "objection": key,
                                                                                          "seq": stored["seq"], "chain": stored["hash"]})
        recipient = "chief-of-staff" if owner_only else boss
        told.append((key, _tell(engine, recipient, plan_review.escalation_message(rnd["lineage"], dict(obj, id=key), response))))
    return {"review": rnd["review"], "escalated": [k for k, _ in told], "boss": boss, "owner_only": owner_only,
            "told": [k for k, ok in told if ok], "not_delivered": [k for k, ok in told if not ok],
            "next": "decide: `plan review decide %s --objection <lane/id> --accept|--override --reason ...`" % rnd["review"]}


def _escalations(entries, review):
    """The review's undecided escalations, newest entry per objection."""
    latest = {}
    for entry in entries:
        if entry.get("review") != review:
            continue
        if entry.get("kind") == "escalation":
            latest[entry["objection"]] = entry
        elif entry.get("kind") == "decision":
            latest.pop(entry.get("objection"), None)
    return latest


def review_decide(args, runner, now):
    """Resolve an escalated objection: `--accept` (the planner revises, within the rounds left) or `--override` (with a reason): an override of
    the last open one writes the `consensus` entry, marked `overridden_by` with the reason. The risk lane and held-PR areas: the owner only."""
    config = load_config()
    review_config(config)
    engine, state, _ = build_engine(runner, now, "cli", writable=True)
    got = gates_caller_kind(runner, now)
    if got[0] == "person":
        kind, who = "person", "owner"
    elif got[0] == "agent":
        kind, who = "agent", got[1]
    else:
        raise ReviewError("refused: %s" % got[1])
    action = "override" if args.override else "accept"
    reason = " ".join((args.reason or "").split())
    if len(reason) < plan_ledger.WAIVER_REASON_MIN:
        raise ReviewError("a reason of at least %d characters is required" % plan_ledger.WAIVER_REASON_MIN)
    outcome = {}

    def build(entries, seq):
        _refuse_if_abandoned(entries, args.review)
        escalations = _escalations(entries, args.review)
        esc = escalations.get(args.objection)
        if esc is None:
            raise ReviewError("refused: %s has no undecided escalation in %s (open: %s)" % (args.objection, args.review, ", ".join(sorted(escalations)) or "none"))
        planner = _round_of(entries, args.review)["by"]
        if kind == "agent" and who == planner:
            ok, why = False, "the planner of a plan cannot decide an objection to it: only the owner at a terminal, or a boss that is not the planner"
        else:
            ok, why = plan_review.decision_allowed(action, esc.get("lane"), esc.get("held_area"), esc.get("owner_only"), kind, who == esc.get("boss"))
        if not ok:
            _gates_audit(engine, "plan.review.decide.refused", args.review, {"by": who, "objection": args.objection, "why": why})
            raise ReviewError("refused: %s" % why)
        outcome.update(esc=esc, others=[k for k in escalations if k != args.objection])
        return {"kind": "decision", "lineage": esc["lineage"], "review": args.review, "objection": args.objection, "action": action,
                "reason": plan_ledger.clip(reason, 300), "by": who, "decider": kind}

    stored = plan_ledger.append_checked(state, now(), build)
    esc = outcome["esc"]
    if engine.audit is not None:
        engine.audit.append(engine.actor, "plan.review.decision", esc["lineage"], {"review": args.review, "objection": args.objection,
                                                                                    "action": action, "by": who, "seq": stored["seq"]})
    result = {"decided": args.objection, "action": action, "by": who}
    entries = review_entries(state)
    if action == "override" and not outcome["others"]:
        mine = [e for e in entries if e.get("review") == args.review]
        accepted = [e for e in mine if e.get("kind") == "decision" and e.get("action") == "accept"]
        rnd = _round_of(entries, args.review)
        # the last open objection was overridden and nothing awaits a revision: the consensus is written, marked overridden
        if not accepted or accepted[-1]["seq"] < max(e["seq"] for e in mine if e.get("kind") == "escalation"):
            if not _abandoned(entries, args.review) and not any(e.get("kind") == "consensus" and e.get("plan_hash") == rnd["plan_hash"] for e in entries):
                consensus = plan_ledger.append_entry(state, {"kind": "consensus", "result": "pass", "pr": "-", "scenario": "-",
                                                             "lineage": rnd["lineage"], "review": args.review, "plan_hash": rnd["plan_hash"],
                                                             "plan_ref": rnd["plan_ref"], "tier": rnd["tier"], "lanes": rnd["lanes"],
                                                             "rounds": rnd["round"], "overridden_by": who, "override_reason": plan_ledger.clip(reason, 300),
                                                             "by": "plan-review"}, now())
                result["consensus"] = "written, overridden by %s" % who
                if engine.audit is not None:
                    engine.audit.append(engine.actor, "plan.review.consensus", rnd["lineage"], {"review": args.review, "overridden_by": who,
                                                                                                "seq": consensus["seq"], "chain": consensus["hash"]})
    elif action == "accept":
        _tell(engine, _round_of(entries, args.review)["by"], "Plan review %s: objection %s was accepted by %s: revise the plan (%s). Then `plan review respond`."
              % (args.review, args.objection, who, plan_ledger.clip(reason, 200)))
        result["next"] = "the planner revises and runs `plan review respond` (the rounds after an escalation are limited to %d)" % plan_review.POST_ESCALATION_ROUNDS
    return result


def review_tick(args, runner, now, socket_call=None):
    """Housekeeping over the open reviews (run by a person, an orchestrator, the chief of staff or a timer): a lane with no verdict after
    `lane_timeout_seconds` is relaunched once and then escalated as unavailable; an escalation nobody decided gets a reminder every
    `escalation_hours`. Never counts a stalled lane as an approval."""
    config = load_config()
    cfg = review_config(config)
    review_caller(runner, now, "run the plan review housekeeping")
    engine, state, _ = build_engine(runner, now, "cli", writable=True, socket_call=socket_call)
    entries = review_entries(state)
    done = {"relaunched": [], "unavailable": [], "reminded": []}
    reviews = sorted({e["review"] for e in entries if e.get("kind") == "round"})
    for review in reviews:
        rnd = _round_of(entries, review)
        mine = [e for e in entries if e.get("lineage") == rnd["lineage"]]
        st = plan_review.lineage_state(mine, rnd["plan_hash"])
        if not st["open_review"]:
            continue
        sessions = [e for e in entries if e.get("kind") == "lane_session" and e.get("review") == review and e.get("round") == rnd["round"]]
        times = {}
        for e in sessions:
            times[e["lane"]] = e["ts"]
        verdict_lanes = set(_verdicts_of(entries, review, rnd["round"]))
        for lane, started in sorted(plan_review.stuck_lanes(rnd["lanes"], times, verdict_lanes, now(), cfg["lane_timeout_seconds"]).items()):
            tries = len([e for e in sessions if e["lane"] == lane])
            if tries <= plan_review.LANE_RETRIES:
                name = "planrev-%s-%s-r%d-retry%d" % (review, lane.lower(), rnd["round"], tries)

                def claim(ledger, seq, lane=lane, tries=tries, name=name):
                    """Under the ledger lock: the relaunch is claimed once (two ticks cannot both relaunch the same lane)."""
                    have = [e for e in ledger if e.get("kind") == "lane_session" and e.get("review") == review and e.get("round") == rnd["round"]
                            and e.get("lane") == lane]
                    if len(have) != tries or any(e.get("session") == name for e in have):
                        raise ReviewError("already relaunched")
                    return {"kind": "lane_session", "lineage": rnd["lineage"], "review": review, "round": rnd["round"], "lane": lane,
                            "session": name, "pane": None, "retry": tries, "by": "plan-review"}
                try:
                    plan_ledger.append_checked(state, now(), claim)
                except ReviewError:
                    continue
                try:
                    ns = types.SimpleNamespace(source=rnd["plan_ref"], file=None, ask=None, done_when=None)
                    text = load_plan(ns, runner, now)[0]
                    brief = plan_review.lane_brief(lane, rnd["plan_ref"], rnd["plan_hash"], text, *_ask_texts(config, runner, now, rnd),
                                                   carried=_carried(entries, rnd["lineage"], lane), closing=bool(rnd.get("closing")))
                    launch_agent(engine, name, cfg["lane_agent"], cfg.get("dir") or os.getcwd(),
                                 cfg.get("workspace") or os.environ.get("HERDR_WORKSPACE_ID"), role="reviewer", reports_to=None,
                                 issue=rnd.get("ask_number"), brief=brief)
                    done["relaunched"].append(lane)
                except (LaunchError, PlanInputError, ValueError, ConfigError, IntentError):
                    pass   # the claim stands: the next tick finds the lane still silent with its retry spent, and escalates it
                continue
            boss, owner_only = _escalation_target(engine, rnd["by"])
            key = "%s/unavailable" % lane

            def unavailable(ledger, seq, lane=lane, key=key, boss=boss, owner_only=owner_only):
                if key in _escalations(ledger, review):
                    raise ReviewError("already escalated")
                return {"kind": "escalation", "lineage": rnd["lineage"], "review": review, "round": rnd["round"], "objection": key,
                        "lane": lane, "text": "lane %s gave no verdict after a relaunch" % lane, "response": "", "boss": boss,
                        "owner_only": owner_only, "plan_hash": rnd["plan_hash"], "held_area": bool(rnd.get("held_area")), "by": "plan-review"}
            try:
                plan_ledger.append_checked(state, now(), unavailable)
            except ReviewError:
                continue
            _tell(engine, "chief-of-staff" if owner_only else boss,
                  "Plan review %s: lane %s is unavailable (no verdict after a relaunch); a stalled lane never counts as an approval. "
                  "Decide with `plan review decide %s --objection %s --accept|--override --reason ...`." % (review, lane, review, key))
            done["unavailable"].append((lane, boss))
        for key, esc in _escalations(entries, review).items():
            def remind(ledger, seq, key=key, esc=esc):
                times = [e["ts"] for e in ledger if e.get("kind") == "reminder" and e.get("review") == review and e.get("objection") == key]
                if not plan_review.reminder_due(esc["ts"], max(times) if times else None, now(), cfg["escalation_hours"]):
                    raise ReviewError("not due")
                return {"kind": "reminder", "lineage": rnd["lineage"], "review": review, "objection": key, "by": "plan-review"}
            try:
                plan_ledger.append_checked(state, now(), remind)
            except ReviewError:
                continue
            _tell(engine, "chief-of-staff" if esc.get("owner_only") else esc.get("boss"),
                  "Reminder: plan review %s has an escalation (%s) undecided for over %d hours." % (review, key, cfg["escalation_hours"]))
            done["reminded"].append(key)
        entries = review_entries(state)
    done["unavailable"] = [l for l, _ in done["unavailable"]]
    return done


def review_abandon(args, runner, now):
    config = load_config()
    review_config(config)
    _, who = review_caller(runner, now, "abandon a plan review")
    engine, state, _ = build_engine(runner, now, "cli", writable=True)
    entries = review_entries(state)
    rnd = _round_of(entries, args.review)
    if _abandoned(entries, args.review):
        raise ReviewError("review %s is already abandoned" % args.review)
    reason = " ".join((args.reason or "").split())
    if len(reason) < plan_ledger.WAIVER_REASON_MIN:
        raise ReviewError("a reason of at least %d characters is required" % plan_ledger.WAIVER_REASON_MIN)
    _review_record(engine, state, {"kind": "abandoned", "lineage": rnd["lineage"], "review": args.review, "round": rnd["round"],
                                   "reason": plan_ledger.clip(reason, 300), "by": who}, now)
    closed = []
    for key, esc in sorted(_escalations(review_entries(state), args.review).items()):   # an abandoned review's escalations are closed, so a new start is not refused for ever
        _review_record(engine, state, {"kind": "decision", "lineage": rnd["lineage"], "review": args.review, "objection": key,
                                       "action": "abandoned", "reason": plan_ledger.clip(reason, 300), "by": who, "decider": "abandon"}, now)
        closed.append(key)
    return {"abandoned": args.review, "lineage": rnd["lineage"], "by": who, "escalations_closed": closed}


def cmd_plan_review(args, runner, now, socket_call=None):
    """`plan review start|verdict|respond|status|abandon`: exit 0 done, 2 refused or unusable. One JSON object on stdout with --json."""
    try:
        action = {"start": review_start, "verdict": review_verdict, "respond": review_respond, "status": review_status,
                  "abandon": review_abandon, "decide": review_decide, "tick": review_tick, "close": review_close,
                  "report": review_report}[args.review_action]
        done = (action(args, runner, now, socket_call) if args.review_action in ("start", "respond", "verdict", "tick", "close")
                else action(args, runner, now))
    except (ReviewError, PlanInputError, ValueError, ConfigError, IntentError, plan_ledger.LedgerError, CallerError, IdentityUncertain,
            LaunchError, OSError) as err:
        sys.stderr.write("%s: plan review: %s\n" % (PROG, _one_line(str(err), 400)))
        return EXIT_USAGE
    if getattr(args, "json", False):
        print(json.dumps(done, sort_keys=True))
    else:
        for key, value in done.items():
            print("%s: %s" % (key, value if not isinstance(value, (list, dict)) else json.dumps(value, sort_keys=True)))
    return EXIT_OK
