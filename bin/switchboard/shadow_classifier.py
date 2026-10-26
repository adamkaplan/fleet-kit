# ------------------------------------------------------- shadow mode in the engine

def _message_text(message):
    text = message.get("text")
    if isinstance(text, str):
        return text
    parts = message.get("content")
    if isinstance(parts, list):
        return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def human_prompts(messages):
    """Your prompts in a transcript read newest first: type `user` without `metadata.fleet`."""
    return [m for m in messages if isinstance(m, dict) and m.get("type") == "user" and _fleet_meta(m) is None]


def _ask_of(fleet, owner=None):
    issue = fleet.get("issue")
    if issue is None:
        return None
    return {"issue": issue, "intent": fleet.get("intent") or "", "owner": owner, "repo": fleet.get("repo")}


def open_asks_of(discovery, cos_name):
    """The asks others hold: a fleet agent with an issue, other than the Chief of Staff.

    Intent text arrives with `fleet-switchboard intents` (PR 6); until then an
    ask has its issue number and owner only.
    """
    asks, seen = [], set()
    for name, other in sorted(discovery.agents.items()):
        ask = _ask_of(other.fleet, name)
        if name != cos_name and ask and (ask["repo"], ask["issue"]) not in seen:
            seen.add((ask["repo"], ask["issue"]))
            asks.append(ask)
    return asks


def _classify_audit(engine, observation, message, previous, request, answer, shadow=True):
    state, questions = request
    detail = {"agent": observation.name, "session": observation.session_id, "message_id": message.get("id"),
              "status": observation.status, "shadow": shadow,
              "question": {"state": state, "options": list(questions[CLASSIFY_ID]["criteria"])},
              "answer": {"choice": answer["choice"], "confident": answer["confident"],
                         "reason": answer["reason"], "jev_choice": answer["jev_choice"],
                         "probabilities": answer["probabilities"]},
              "model": answer["model"], "usage": answer["usage"], "latency_ms": answer["latency_ms"]}
    engine.audit.append(engine.actor, "classify", "classify:%s:%s" % (observation.session_id, message.get("id")), detail)


def shadow_classify(engine, observation, discovery, now):
    """Classify your newest prompt to a Chief of Staff once, and write what Jev said. Acts on nothing."""
    prompts = newest_prompts(engine, observation.session_id)
    if not prompts:
        return False
    message = prompts[0]
    created = _created(message)
    key = "classify:%s:%s" % (observation.session_id, message.get("id"))
    if message.get("id") is None or key in engine._classified:
        return False
    if created is None or now - created > engine.engaged_seconds:
        return False
    engine._classified.add(key)  # one attempt per message, whatever the answer
    asks = ask_intents(engine, open_asks_of(discovery, observation.name))  # PR 8: with their Intent
    if observation.status != "working" and not asks:
        return False  # nothing to choose between: it simply becomes the foreground work
    previous = _message_text(prompts[1]) if len(prompts) > 1 else None
    current = ask_intent(engine, _ask_of(observation.fleet, observation.name))
    request = classify_request(_message_text(message), previous, current, asks)
    answer = classify(engine.jev, _message_text(message), previous, current, asks,
                      engine.config["jev"]["min_confidence"])
    _classify_audit(engine, observation, message, previous, request, answer, shadow=not acting(engine))
    if acting(engine):  # PR 8
        note_decision(engine, observation, message, current, asks, answer)
    return True


def run_shadow_classifier(engine, discovery, now):
    """Each Chief of Staff, once per new prompt. A failure is audited and never stops the pass."""
    if engine.jev is None or engine.audit is None or not (engine.config.get("shadow_judges") or acting(engine)):
        return
    if fault("no-classify"):
        return
    for observation in list(discovery.agents.values()):
        if observation.fleet.get("role") != "chief-of-staff" or observation.problem:
            continue
        try:
            shadow_classify(engine, observation, discovery, now)
        except Exception as err:
            engine.audit.append(engine.actor, "error", observation.name,
                                {"step": "classify", "error": "%s: %s" % (type(err).__name__, _one_line(str(err)))})
