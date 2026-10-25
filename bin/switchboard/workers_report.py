# ---- PR 13: workers report, and a stop without a report is triaged
#
# docs/switchboard.md, "Workers report (PR 13)". A worker tells its boss with `fleet-switchboard report`. A
# worker whose turn ends without a report since its latest assigning input is an unreported stop: the decision
# model is asked `report_owed`, and the answer nudges the worker once, stays silent, or (a second stop, or no
# answer) wakes the boss with one `worker.stopped`. Everything here is derived from transcripts, nothing stored.

REPORT_OWED_ID = "report_owed"
REPORT_OWED_TEXT = ("Did this turn end with work done, blocked, failed or needing a decision that the boss has "
                    "not been told about?")
REPORT_TRIAGE_DEFAULTS = {
    "owed_threshold": 0.5,  # the probability at which the answer is "owed": the worker is nudged
    "budget_seconds": 3,    # the longest the decision model may take for one stop; no answer fails open
    "stopped_news": "off",  # #96: "off", "shadow" (judge and audit, drop nothing) or "on": judge a worker.stopped that
                            # would wake the Chief of Staff alone, and drop one that is clearly no news
    "news_threshold": 0.15, # the probability of news below which a stopped note is clearly no news
}
NUDGE_TEXT = ("You stopped without telling %s. If your work is done, failed, blocked or needs a decision, run: "
              "%s report <state> \"one line\". If nothing changed since your last report, say nothing. "
              "A run you started that takes more than a few minutes belongs in `%s job start <name> -- <command>`; `%s job status <name>` says whether it is "
              "alive, finished or dead.")


def validate_report_triage_config(config, source):
    raw = config.get("report_triage")
    if raw is None:
        config["report_triage"] = dict(REPORT_TRIAGE_DEFAULTS)
        return
    if not isinstance(raw, dict):
        raise ConfigError("%s: 'report_triage' must be an object, got %r" % (source, type(raw).__name__))
    unknown = sorted(set(raw) - set(REPORT_TRIAGE_DEFAULTS))
    if unknown:
        warn_unknown_config("%s: 'report_triage' contains key(s) this tool does not read: %s (known: %s)"
                          % (source, ", ".join(unknown), ", ".join(REPORT_TRIAGE_DEFAULTS)), 'report_triage', unknown, list(REPORT_TRIAGE_DEFAULTS))
    settings = dict(REPORT_TRIAGE_DEFAULTS, **raw)
    if not _is_number(settings["owed_threshold"]) or not 0 < settings["owed_threshold"] <= 1:
        raise ConfigError("%s: 'report_triage.owed_threshold' must be a number above 0 and at most 1, got %r"
                          % (source, settings["owed_threshold"]))
    if not _is_number(settings["budget_seconds"]) or settings["budget_seconds"] <= 0:
        raise ConfigError("%s: 'report_triage.budget_seconds' must be a positive number, got %r"
                          % (source, settings["budget_seconds"]))
    if settings["stopped_news"] not in ("off", "shadow", "on"):
        raise ConfigError("%s: 'report_triage.stopped_news' must be \"off\", \"shadow\" or \"on\", got %r"
                          % (source, settings["stopped_news"]))
    if not _is_number(settings["news_threshold"]) or not 0 < settings["news_threshold"] < 1:
        raise ConfigError("%s: 'report_triage.news_threshold' must be a number above 0 and below 1, got %r"
                          % (source, settings["news_threshold"]))
    config["report_triage"] = settings


REPORT_LOOKUP_WAIT = 3.0   # seconds a report waits to learn whether its issue exists; slower is accepted


class ReportError(Exception):
    """A report that cannot be made: an unknown state, no line, a line too long, a caller with no boss."""


def report_owed_questions():
    """The one question, by id: a yes/no `noul` like the policy judge's."""
    return {REPORT_OWED_ID: {"type": "noul", "instructions": REPORT_OWED_TEXT,
                             "criteria": {"true": "The turn ended with something the boss has not been told: "
                                                  "work done, blocked, failed, or a decision needed.",
                                          "false": "Nothing happened that the boss needs to hear, or it was "
                                                   "already reported."}}}


def report_owed_state(role, intent, done_when, last_reply, boss):
    return {"role": role or "unknown", "boss": boss, "intent": _clip(intent or "not stated: no ask is recorded", 600),
            "done_when": _clip(done_when or "not stated", 400), "last_reply": _clip(last_reply, 800)}


def assigning_input(engine, observation, boss):
    """(message id, created) of the newest message in the worker's transcript that assigned it work, or None.

    Assigning: a prompt (yours, the fleet's task, or the brief `launch` sent), or a message from its boss
    (`send`, a handoff). Not assigning: a GitHub notice, a decoration, a reminder, a nudge, a toast, a
    report from one of its own workers. Newest first, within READ_LIMIT messages."""
    # Two small typed reads instead of a 200-message page of everything: the newest user message, and the newest of the last few synthetic ones
    # that is a `send` from the boss. The newer of the two assigned it.
    users = engine.v2.message_list(observation.session_id, limit=1, type="user")
    synthetic = engine.v2.message_list(observation.session_id, limit=SYNTHETIC_PAGE, type="synthetic")
    candidates = [m for m in (users if isinstance(users, list) else []) if isinstance(m, dict)]
    candidates += [m for m in (synthetic if isinstance(synthetic, list) else []) if isinstance(m, dict)
                   and any(key.startswith("send:%s:" % boss) for key in _fleet_keys(m))][:1]
    candidates = [m for m in candidates if m.get("id") and _created(m) is not None]
    if not candidates:
        return None
    newest = max(candidates, key=_created)
    return newest.get("id"), _created(newest)


def report_jev(engine, settings):
    """The decision model for triage: the engine's, with a shorter timeout (its budget). None when none is set."""
    base = engine.config.get("jev")
    if not base:
        return None
    capped = dict(base, timeout_seconds=min(base["timeout_seconds"], settings["budget_seconds"]))
    return build_jev(dict(engine.config, jev=capped))


QUIET_OUTCOMES = ("succeeded", "success")  # a stop that ended cleanly: only its idle line may be folded


def triage_stop(engine, source, observation, boss, idle_ms, reply):
    """What an unreported stop calls for: None (reported, too old, or not readable: only the idle rider), or
    {"outcome": "nudge", "nudge": Fact} / {"outcome": "silent"} / {"outcome": "stopped"}. Audited once."""
    now = engine.now()
    if getattr(engine, "light", False):
        return None   # v2_light: no transcript read, so a stop is only its idle rider
    if now - observation.idle_at > engine.config["rider_max_age_seconds"]:
        return None  # a stop that old is not news, and a nudge would be late
    if engine.state_dir is not None:   # a RETIRED worker is finished on purpose: no nudge, no "stopped without reporting" to its boss (no state dir: triaged as before)
        kept = registry_retired(engine.state_dir).get(observation.name)
        if kept is not None and kept.get("session") in (None, observation.session_id):
            return None
    boss_view = engine.discovery.agents.get(boss) if engine.discovery else None
    if boss_view is None:  # the boss cannot be read, so nothing can be said to have been reported: tell it (held)
        verdict = {"outcome": "stopped", "reason": "the boss %s is not running, so its transcript cannot show a report" % boss}
        try:
            engine.audit.append(engine.actor, "report.triage", "stop:%s:%d" % (observation.session_id, idle_ms), {
                "worker": observation.name, "boss": boss, "session": observation.session_id, "idle_ms": idle_ms,
                "question": None, "answer": None, "latency_ms": 0, "outcome": "stopped", "reason": verdict["reason"]})
        except OSError:
            pass
        return verdict
    assigned = assigning_input(engine, observation, boss)
    if assigned is None:
        return None
    assigned_id, assigned_at = assigned
    keys, complete = engine.delivered_keys(boss_view.session_id, assigned_at - SINCE_SLACK)
    prefixes = ("%s:%s:" % (REPORT, observation.name), "send:%s:" % observation.name)   # a `send` to its boss tells it as much as a `report`
    if not fault("ignore-reports") and any(
            key.startswith(prefixes) and (keys.at.get(key) is None or keys.at[key] >= assigned_at) for key in keys):
        return None  # it reported or wrote to its boss since it was last given work: this stop is only a rider
    if not complete:
        return None  # the read did not reach back far enough to say it did not report
    nudge_key = "%s:%s:%s" % (NUDGE, observation.session_id, assigned_id)
    if fault("no-nudge-once"):
        nudge_key += ":%d" % idle_ms  # the fault: every stop is a first stop
    worker_keys, _ = engine.delivered_keys(observation.session_id, assigned_at - SINCE_SLACK)
    sent_at = worker_keys.at.get(nudge_key) if nudge_key in worker_keys else None
    audit = {"worker": observation.name, "boss": boss, "session": observation.session_id, "ask": _issue_of(observation.fleet),
             "assigned": assigned_id, "idle_ms": idle_ms, "question": None, "answer": None, "latency_ms": 0,
             "model": None, "usage": {}, "error": None}
    if nudge_key in worker_keys:
        if sent_at is not None and observation.idle_at < sent_at:
            return None  # this is the stop the nudge answered, not a second one
        verdict = {"outcome": "stopped", "reason": "nudged already for this assignment and stopped again "
                                                   "without reporting"}
    else:
        verdict = ask_report_owed(engine, observation, boss, reply, audit)
        if verdict["outcome"] == "nudge":
            verdict["nudge"] = Fact(
                nudge_key, observation.name, NUDGE, NUDGE_TEXT % (boss, PROG, PROG, PROG), observation.idle_at,
                issue=_issue_of(observation.fleet), batch=False, sender="switchboard")
    audit["outcome"], audit["reason"] = verdict["outcome"], verdict["reason"]
    try:
        engine.audit.append(engine.actor, "report.triage", "stop:%s:%d" % (observation.session_id, idle_ms), audit)
    except OSError:
        pass  # the audit is best effort here, as the judge's is: a full disk must not hide a stopped worker
    return verdict


def ask_report_owed(engine, observation, boss, reply, audit):
    """Ask `report_owed`. {"outcome": "nudge" | "silent" | "stopped", "reason"}; fails open to "stopped"."""
    settings = engine.config["report_triage"]
    questions = report_owed_questions()
    ask = ask_intent(engine, _ask_of(observation.fleet, observation.name)) or {}
    state = report_owed_state(observation.fleet.get("role"), ask.get("intent"), ask.get("done_when"), reply, boss)
    audit["question"] = {"state": state, "ids": list(questions)}
    failure = None
    try:
        jev = report_jev(engine, settings)
        if jev is None:
            failure = "no decision model is configured"
        else:
            result = jev.decide(state, questions)
            audit.update(latency_ms=result["latency_ms"], model=result["model"], usage=result["usage"])
            owed = result["answers"][REPORT_OWED_ID]["noul"]
            audit["answer"] = {REPORT_OWED_ID: owed}
    except JevError as err:
        failure = "decision model unavailable (%s)" % err.reason
        audit.update(latency_ms=err.latency_ms or 0, error=_one_line(str(err), 160))
    if failure:
        if fault("no-fail-open"):
            return {"outcome": "silent", "reason": failure + "; the fault keeps quiet"}
        return {"outcome": "stopped", "reason": failure + ": the boss is told"}
    if owed >= settings["owed_threshold"]:
        return {"outcome": "nudge", "reason": "owed at %.2f, threshold %.2f" % (owed, settings["owed_threshold"])}
    return {"outcome": "silent", "reason": "owed at %.2f, below %.2f" % (owed, settings["owed_threshold"])}
