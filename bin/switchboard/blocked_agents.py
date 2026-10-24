# ---- the audited resume (docs/switchboard.md, "A blocked agent is reported"). ONE function resumes an agent whose turn has ended: the
# `resume` command and the blocked-report pass both call `resume_ended_turn`, so the refusals, the rate limit and the audit line are the same.

RESUME_STATE = "resumes.json"   # {agent: {"at": epoch}}: the last resume of each agent (the rate limit; an ordinary state row)
RESUME_SECONDS = 600            # one resume per agent per 10 minutes
RESUME_TEXT = "Your last turn ended without a finished answer (for example a provider error). Carry on from where you stopped."
RESUME_REASON = "its turn ended and nothing is open"
RESUME_COMMAND = PROG + " resume"   # the one spelling the report, the documentation, the help and the owner's grant use
PROVIDER_ERROR = re.compile(r"\b499\b|ECONNRESET|time(?:d)?[ -]?out", re.I)


class ResumeRefused(Exception):
    """The resume was not sent: the reason is the message, and nothing was written."""


def last_turn_error(engine, session_id):
    """The error text of the session's newest message, or "" (none, or it could not be read)."""
    try:
        newest = (engine.v2.message_list(session_id, limit=1, order="desc") or [None])[0]
    except Exception:
        return ""
    error = (newest.get("error") or (newest.get("info") or {}).get("error")) if isinstance(newest, dict) else None
    return _one_line(json.dumps(error) if not isinstance(error, str) else error, 300) if error else ""


def resume_ended_turn(engine, caller, name, reason, discovery=None):
    """Send the resume text to `name`'s session, only when its turn has ended and nothing is open. Raises ResumeRefused (nothing sent, nothing
    audited) for a missing or unreadable agent, a working session, an open permission request or question (that goes to the owner), and an agent
    resumed less than RESUME_SECONDS ago. Otherwise one `resume` audit line (who, whom, why) and the send; returns the message id."""
    caller, reason = blocked_sender(caller), _one_line(reason or RESUME_REASON, 200)
    now = engine.now()
    discovery = discovery if discovery is not None else engine.discover(now)
    seen = discovery.agents.get(name)
    if seen is None:
        raise ResumeRefused(discovery.missing(name))
    seen, why = engine.reobserve(seen, now)   # a read before a write
    if seen is None:
        raise ResumeRefused(why)
    if seen.conflict or seen.problem:
        raise ResumeRefused(seen.problem or "%s is claimed by more than one session" % name)
    if seen.status == "working":
        raise ResumeRefused("%s is working: nothing is sent to a session that is working" % name)
    if seen.status not in ("idle", "blocked", "working"):
        raise ResumeRefused("%s has status %s in herdr: only an ended turn is resumed" % (name, seen.status))
    try:
        opened = [("permission request", engine.v2.permission_list(seen.session_id)), ("question", engine.v2.form_list(seen.session_id))]
    except V2Error as err:
        raise ResumeRefused("v2 unreachable, so an open request cannot be ruled out: %s" % _one_line(str(err), 120)) from None
    kinds = [kind for kind, found in opened if found]
    if kinds:
        raise ResumeRefused("%s has an open %s: that goes to the owner, nothing is sent" % (name, " and an open ".join(kinds)))
    taken = {}

    def take(state):   # the rate record, under the lock: a second resume inside the window finds this one
        last = state.get(name, {}).get("at")
        taken["last"] = last
        if _is_number(last) and now - last < RESUME_SECONDS:
            return None
        state[name] = {"at": now}
        taken["ok"] = True
        return state
    blocked_update(engine.state_dir, RESUME_STATE, take)
    if not taken.get("ok"):
        raise ResumeRefused("%s was resumed %d minutes ago: one resume per agent per %d minutes" % (name, int((now - taken["last"]) // 60), RESUME_SECONDS // 60))
    keys = ["resume:%d" % now]
    message_id = derive_message_id(seen.session_id, keys)
    try:
        engine.v2.synthetic(seen.session_id, RESUME_TEXT, {"fleet": {"keys": keys, "from": "switchboard"}}, True, "queue", message_id,
                            "Resume: your turn ended, carry on")
    except Exception:
        blocked_update(engine.state_dir, RESUME_STATE, lambda s: dict(s, **{name: {"at": taken["last"]}}) if _is_number(taken["last"]) else {k: v for k, v in s.items() if k != name})
        raise
    engine.reads.forget(seen.session_id)
    if engine.audit is not None:
        engine.audit.append(engine.actor, "resume", name, {"by": caller, "reason": reason, "session": seen.session_id, "message_id": message_id})
    return message_id


def resume_first_recovery(engine, discovery, seen, reported):
    """The automatic first recovery of the blocked report: a stop whose turn error holds HTTP 499, ECONNRESET or a timeout is resumed once,
    through `resume_ended_turn` (the same refusals, rate limit and audit line), before it is reported. True when it was resumed; never raises."""
    try:
        error = last_turn_error(engine, seen.session_id) if reported is None else ""
        found = PROVIDER_ERROR.search(error)
        if not found:
            return False
        resume_ended_turn(engine, "switchboard", seen.name, "the blocked report found a provider error (%s) after an ended turn" % found.group(0), discovery)
        return True
    except Exception:
        return False


def ended_turn_text(name, resumed):
    """What the blocked report says of an ended turn with nothing open: the real command, or that it was just resumed."""
    if resumed:
        return ("an ended turn after a provider error: the switchboard resumed it once just now (`%s %s`, audited); "
                "a second stop within %d minutes is reported as usual" % (RESUME_COMMAND, name, RESUME_SECONDS // 60))
    return ("an ended turn with nothing open (the terminal shows it blocked, often after a provider error; not a real question): "
            "the Chief of Staff may resume it with `%s %s`, which is audited and sends nothing while it works or has anything open" % (RESUME_COMMAND, name))


# ---- a long block is reported (docs/switchboard.md, "A blocked agent is reported"). `hold_reason` holds a message for an agent
# the terminal shows as blocked: nothing is typed into a prompt, and that is right. The missing part was the report. Once per
# block (repeated at most every `repeat_minutes`, closed with one final line) its boss and the Chief of Staff are told. It reads
# only what the pass already has (the observations, the held plans, the requests WorkerFacts read, reminders.json): no `gh` call.

BLOCKED_ALERT_DEFAULTS = {"minutes": 20, "repeat_minutes": 60}
BLOCKED_ALERT_STATE = "blocked-alert.json"


def validate_blocked_alert_config(config, source):
    """`blocked_alert`: absent or null is the defaults (on); false is off; else {"minutes", "repeat_minutes"}, numbers above 0."""
    if "blocked_alert" not in config or config["blocked_alert"] is None:
        return
    raw = config["blocked_alert"]
    if raw is False:
        return
    if not isinstance(raw, dict):
        raise ConfigError("%s: 'blocked_alert' must be false, null or an object, got %r" % (source, type(raw).__name__))
    unknown = sorted(set(raw) - set(BLOCKED_ALERT_DEFAULTS))
    if unknown:
        warn_unknown_config("%s: 'blocked_alert' has key(s) this tool does not read: %s" % (source, ", ".join(unknown)), 'blocked_alert', unknown, list(BLOCKED_ALERT_DEFAULTS))
    section = dict(BLOCKED_ALERT_DEFAULTS, **raw)
    for key in BLOCKED_ALERT_DEFAULTS:
        if not _is_number(section[key]) or section[key] <= 0:
            raise ConfigError("%s: 'blocked_alert.%s' must be a number of minutes above 0, got %r" % (source, key, section[key]))
    config["blocked_alert"] = section


def blocked_alert_section(config):
    """The settings, or None when the alert is off."""
    raw = (config or {}).get("blocked_alert", {})
    if raw is False:
        return None
    return dict(BLOCKED_ALERT_DEFAULTS, **(raw or {}))


def blocked_kind(engine, observation, resumed=False):
    """What a blocked agent waits on, in words, as the pass read it: an open permission request or question, or (nothing open) an ended turn."""
    source = next((s for s in engine.sources if isinstance(s, WorkerFacts)), None)
    if source is None:
        return "a permission request or a question (not read)"
    kinds = sorted({kind for who, kind, _ in source.requests if who.session_id == observation.session_id})
    if kinds:
        return "an open %s; it goes to the owner" % " and an open ".join("%s request" % k if k == "permission" else k for k in kinds)
    return ended_turn_text(observation.name, resumed)


BLOCKED_HELD_SENDS = "blocked-held-sends.json"   # {agent: {"rows": [{"from", "at"}]}}: sends refused while it was blocked (a `send` stores nothing, so the report counts them here)
BLOCKED_LOCK = "blocked-alert.lock"
BLOCKED_HELD_KEEP = 100      # rows kept per agent: the recorded case held 11 messages in 2.5 hours, so this is about nine times that
BLOCKED_HELD_AGENTS = 64     # agents kept in the file: the number blocked at once in a fleet is a handful; 64 is far above any real fleet
BLOCKED_LINE_SENDERS = 10    # senders named in a line, then "and N more"
BLOCKED_LINE_REMINDERS = 5   # reminders named in a line, then "and N more"
BLOCKED_LINE_CHARS = 1200    # the whole line


@contextmanager
def blocked_lock(state_dir):
    """Serialises the `send` command and the daemon around every read-modify-write of blocked-alert.json and blocked-held-sends.json."""
    fd = os.open(str(Path(state_dir) / BLOCKED_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def blocked_update(state_dir, name, change):
    """The one write path of the two state files: read, `change(state)` and write, under `blocked_lock`, through the atomic `_write_small`.
    `change` returns the new state, or None to write nothing. Returns the state as it was written (or read)."""
    with blocked_lock(state_dir):
        state = _read_small(state_dir, name)
        new = change(state)
        if new is None:
            return state
        _write_small(state_dir, name, new)
        return new


def note_held_send(state_dir, recipient, sender, now, observation):
    """A `send` held for a blocked agent is remembered (who, when) so a long block can name it. Never raises: the delivery path is not its
    business, and the hold itself is audited (`hold`), so a row lost to a full disk costs the count, not the message's record."""
    if state_dir is None or observation is None or observation.status != "blocked" or hold_reason(observation) is None:
        return

    def add(state):
        entry = state.get(recipient)
        rows = [r for r in (entry.get("rows") if isinstance(entry, dict) else None) or [] if isinstance(r, dict)]
        state[recipient] = {"rows": (rows + [{"from": blocked_sender(sender), "at": now}])[-BLOCKED_HELD_KEEP:]}
        if len(state) > BLOCKED_HELD_AGENTS:   # the oldest agents' rows go first
            newest = sorted(state, key=lambda n: max([r.get("at", 0) for r in state[n].get("rows", []) if _is_number(r.get("at"))] or [0]), reverse=True)
            state = {n: state[n] for n in newest[:BLOCKED_HELD_AGENTS]}
        return state
    try:
        blocked_update(state_dir, BLOCKED_HELD_SENDS, add)
    except OSError:
        pass


def _named(items, limit):
    shown = list(items)[:limit]
    return ", ".join(shown) + (" and %d more" % (len(items) - limit) if len(items) > limit else "")


def blocked_sender(who):
    """The one gate for a sender name that a caller claimed: the daemon's own agent-name pattern (NAME_RE, full match, so a trailing newline fails), else "unknown".
    Every name in the report line, the end line, the reminder, the audit and the state file passes through it."""
    return who if isinstance(who, str) and NAME_RE.fullmatch(who) else "unknown"


def _sender_names(who):
    return sorted({blocked_sender(w) for w in who})


def blocked_line(engine, observation, since, now, held, refused, kind):
    """`held` is one sender per message the daemon holds (delivered when the block ends); `refused` one sender per send that was refused
    and stored nowhere (the sender must send again). The two are said apart."""
    minutes = int((now - since) // 60)
    reminders = [r for r in read_reminders(engine.state_dir) if r.get("to") == observation.name and r.get("from") == observation.name
                 and isinstance(r.get("at"), (int, float)) and since <= r["at"] <= now]
    text = "%s has been blocked for %d minutes (first seen blocked at %s) on %s." % (observation.name, minutes, iso(since), kind)
    if held or not refused:
        text += " Held for it: %d message%s from %s." % (len(held), "" if len(held) == 1 else "s", _named(_sender_names(held), BLOCKED_LINE_SENDERS) or "nobody")
    if refused:
        text += " %d refused send%s the senders must repeat (not stored; ask %s)." % (len(refused), "" if len(refused) == 1 else "s", _named(_sender_names(refused), BLOCKED_LINE_SENDERS))
    if reminders:
        text += " Its own reminders due during the block: %s." % _named(["%s due %s" % (r["id"], iso(r["at"])) for r in reminders], BLOCKED_LINE_REMINDERS)
    return _one_line(text, BLOCKED_LINE_CHARS)


def _refused_senders(entry):
    rows = entry.get("rows") if isinstance(entry, dict) else None
    return [blocked_sender(r.get("from")) for r in (rows if isinstance(rows, list) else [])[-BLOCKED_HELD_KEEP:] if isinstance(r, dict)]


def blocked_tick(engine, discovery, held):
    """The end of a pass, after delivery: report a block that has lasted `minutes` with a message held for the agent, once, then at most
    every `repeat_minutes`; one final line when it ends. Returns [(agent, "report" | "repeat" | "end")]."""
    section = blocked_alert_section(engine.config)
    if section is None or discovery is None or engine.state_dir is None:
        return []
    now, state, done = engine.now(), blocked_update(engine.state_dir, BLOCKED_ALERT_STATE, lambda _: None), []
    waiting = {n for n, o in discovery.agents.items() if o.status == "blocked"}
    ended_refused = {}

    def forget(found):   # a send refused for an agent that is no longer blocked is forgotten, after the end line has named its senders
        if set(found) - waiting:
            ended_refused.update({n: v for n, v in found.items() if n not in waiting})
            return {n: v for n, v in found.items() if n in waiting}
        return None
    refused = blocked_update(engine.state_dir, BLOCKED_HELD_SENDS, forget)
    cos = next((n for n, o in sorted(discovery.agents.items()) if (o.fleet or {}).get("role") == "chief-of-staff"), None)

    def tell(observation, text, event, detail):
        boss = boss_name(discovery, observation)
        boss_seen = discovery.agents.get(boss) if boss else None
        recipients = []
        if boss and boss_seen is not None and not hold_reason(boss_seen):
            recipients.append(boss)   # a boss that is itself blocked or gone is not told: the Chief of Staff alone is
        if cos and cos not in recipients:
            recipients.append(cos)
        recipients = [r for r in recipients if r != observation.name]
        for who in recipients:
            add_reminder(engine.state_dir, who, now, now, "Blocked agent: " + text)
        if engine.audit is not None:
            engine.audit.append(engine.actor, event, observation.name, dict(detail, told=recipients, text=_one_line(text, 400)))

    changed = False
    for name, seen in sorted(discovery.agents.items()):
        entry = state.get(name)
        if seen.status != "blocked":
            continue
        if not isinstance(entry, dict) or entry.get("session") != seen.session_id or not _is_number(entry.get("since")):
            entry = state[name] = {"session": seen.session_id, "since": now}
            changed = True
        plan = held.get(name)
        messages = [blocked_sender(fact.sender) for fact in (plan.pending if plan is not None else [])]
        refusals = _refused_senders(refused.get(name))
        entry["held"], entry["refused"] = len(messages), len(refusals)   # what the end line may say: the daemon's messages arrive, the refused ones never do
        if not messages and not refusals:
            continue
        last = entry.get("reported")
        if last is None and now - entry["since"] >= section["minutes"] * 60:
            event = "blocked.report"
        elif last is not None and now - last >= section["repeat_minutes"] * 60:
            event = "blocked.repeat"
        else:
            continue
        kind = blocked_kind(engine, seen, resume_first_recovery(engine, discovery, seen, last))
        tell(seen, blocked_line(engine, seen, entry["since"], now, messages, refusals, kind),
             event, {"since": iso(entry["since"]), "held": len(messages), "refused": len(refusals)})
        entry["reported"] = now
        blocked_update(engine.state_dir, BLOCKED_ALERT_STATE, lambda _: state)   # after each one: a crash repeats at most one
        done.append((name, event.split(".")[1]))
    for name in sorted(set(state) - waiting):
        entry = state.pop(name)   # the block ended, or the agent is gone
        changed = True
        if not isinstance(entry, dict) or entry.get("reported") is None:
            continue
        seen = discovery.agents.get(name)
        if seen is None:
            continue   # gone from the fleet: nobody to say it about
        refusals = _refused_senders(ended_refused.get(name))
        had_held = bool(entry.get("held")) or (name in held and bool(held[name].pending))
        still = name in held and bool(held[name].pending)
        released = had_held and not still   # what is known is only that the daemon holds them no longer: not that a delivery happened
        parts = []
        if still:
            parts.append("its messages are still held: %s" % _one_line(held[name].reason, 200))
        elif had_held:
            parts.append("the messages the daemon held for it are no longer held")
        if refusals:
            parts.append("%d refused send%s never stored and NOT delivered: ask %s to send again" % (
                len(refusals), "" if len(refusals) == 1 else "s", _named(_sender_names(refusals), BLOCKED_LINE_SENDERS)))
        text = "%s was blocked from %s to %s (%d minutes); the block has ended%s." % (
            name, iso(entry["since"]), iso(now), int((now - entry["since"]) // 60), (" and " + "; ".join(parts)) if parts else "")
        tell(seen, text, "blocked.end", {"since": iso(entry["since"]), "no_longer_held": released, "refused": len(refusals)})
        done.append((name, "end"))
    if changed or done:
        blocked_update(engine.state_dir, BLOCKED_ALERT_STATE, lambda _: state)
    return done


def add_budget_notice_parsers(sub):
    notice = sub.add_parser("budget-notice", help="Tell the orchestrator of the top spender that a GitHub account is low on budget (the gh shim runs this; at most once an hour per account)",
                            description="`budget-notice --account NAME`: when the account's fresh reading is below the low-budget line, names the agent that made the most agent-shell "
                                        "GitHub calls in the last hour to that agent's orchestrator (the Chief of Staff when it has none), in one message, at most once an hour per account. "
                                        "It takes only the account's name: the budget, the ranking and the text all come from the daemon's own records, so a call to it cannot invent "
                                        "a message. There is no per-agent cap: the shim only makes that agent's reads that are not answered from the cache wait a few seconds while the "
                                        "account is low.")
    notice.add_argument("--account", required=True, help="The gh account's name as `status` shows it")


def cmd_budget_notice(args, runner, now):
    """One message, only when the account is low, only once an hour per account, to the orchestrator of the top spender. Exit 0 whether or not a message went: the shim
    never waits on the answer, and a quiet account is not an error."""
    calls = load_module("agent_calls")
    state = ensure_state_dir()
    account = args.account
    moment = now()
    low = calls.low_budget(str(state), account, moment)
    top = calls.top_spender(str(state), account, moment) if low is not None else None
    if low is None or top is None:
        print("no message: %s is not below the low-budget line, or nobody spent on it in the last hour" % _one_line(account, 40))
        return EXIT_OK
    engine, state, _config = build_engine(runner, now, "cli")
    discovery = engine.discover(moment)
    if discovery is None or discovery.problems:
        print("no message: the agent list cannot be read")   # nothing was claimed: the next call tries again
        return EXIT_OK
    seen = discovery.agents.get(top[0])
    boss = boss_name(discovery, seen) if seen is not None else None
    chiefs = chiefs_of_staff(discovery)
    to = boss if boss in discovery.agents else (chiefs[0] if len(chiefs) == 1 else None)
    if to is None:
        print("no message: %s has no orchestrator or Chief of Staff to tell" % top[0])
        return EXIT_OK
    if not calls.claim_notice(str(state), account, moment):
        print("no message: one already went for this account in the last hour")
        return EXIT_OK
    text = calls.notice_text(_one_line(account, 40), low[0], low[1], top[0], top[1], top[2])
    try:
        item = add_reminder(state, to, moment, moment, text)
    except Exception:
        calls.release_notice(str(state), account)   # the hour was claimed and no message was made: give it back
        raise
    engine.audit.append("cli", "budget.notice", to, {"account": _one_line(account, 40), "agent": top[0], "id": item["id"]})
    print("told %s about %s" % (to, top[0]))
    return EXIT_OK


cmd_budget_notice.direct = True   # run by the gh shim: no daemon start, no error mapping
