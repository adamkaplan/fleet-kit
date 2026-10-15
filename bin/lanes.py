"""Lanes: move an ask from one orchestrator's charter to another's, with a handover (ask #347, slice 1).

An ask is a native sub-issue of exactly one charter, so a move is ONE write: `POST repos/R/issues/B/sub_issues` with
`{"sub_issue_id": <the ask's id>, "replace_parent": true}`. GitHub swaps the parent link in one step: the ask is never
left with no parent, and a refused write leaves it under its old charter. The write is verified by reading the ask's
parent back. The move writes the parent link only: never the ask's body, title, labels or comments.

Everything around the write is explicit and reported by step: the reminders the old orchestrator held on the ask are
carried to the new one, the new orchestrator gets a handover note (state, pull requests and heads, branches, workers,
waits, reminders), the old one a short notice naming the workers still finishing under it (their recorded boss does
not change), and the old charter's remaining open asks are listed so none is forgotten.

The logic is here; `bin/fleet-switchboard` only registers the command and hands this module a namespace `sb` of the
switchboard's own helpers (`NEEDS`) and its `engine`, so this module imports nothing from it.
"""

import re

OPERATOR = "operator"
HANDOVER_SECTIONS = ("State", "Pull requests and heads", "Branches", "Workers", "Waits", "Reminders")
PR_LIMIT = 10          # pull requests read for the note: one read each
WAIT_LABEL = re.compile(r"await|hold|parked|block|wait", re.IGNORECASE)

# what the command hands in, by name (the switchboard's helpers, classes and constants this module uses)
NEEDS = ("PROG", "EXIT_OK", "EXIT_ERROR", "EXIT_USAGE", "IntentError", "MessageTooLong", "InterruptRefused", "ReminderError",
         "_CHARTER_BLOCK", "NO_INTENT", "_fleet_repo", "_is_issue_number", "_one_line", "boss_name", "issue_tag", "iso",
         "read_reminders", "write_reminders", "reminders_lock", "_gh_write", "orchestrator_label", "load_module")


class MoveRefused(Exception):
    """The move cannot be made, and nothing was written."""


def charter_orchestrators(sb, discovery, repo, charter):
    """The names of the live orchestrators whose charter is `charter` in `repo`, sorted."""
    found = []
    for agent in discovery.agents.values():
        fleet = agent.fleet or {}
        if fleet.get("role") == "orchestrator" and fleet.get("charter") == charter \
                and sb._fleet_repo(fleet, sb.engine.config) == repo:
            found.append(agent.name)
    return sorted(found)


def check_caller(caller, role):
    """Only the Chief of Staff or the operator (a plain shell: `caller` is None) moves an ask. The refusal text, or None."""
    if caller is None or role == "chief-of-staff":
        return None
    if role == "unidentified":
        return "only the chief-of-staff or the operator moves an ask; this is %s" % caller
    return "only the chief-of-staff or the operator moves an ask; %s is %s" % (caller, "a %s" % role if role else "not given a role")


def shared(sb):
    """The one endpoint check (bin/charter_check.py), shared with `orders copy`."""
    return sb.load_module("charter_check")


def read_issue(sb, repo, number):
    try:
        return shared(sb).read_issue(sb, repo, number)
    except shared(sb).Refused as problem:
        raise MoveRefused(str(problem)) from None


def parent_of(sb, repo, number):
    """The parent issue number read now (never from the cache), or None. IntentError when GitHub cannot say."""
    try:
        found = sb.engine.intents._api("repos/%s/issues/%d/parent" % (repo, number), repo)
    except sb.IntentError as err:
        if err.reason == "not_found":
            return None
        raise
    number = found.get("number") if isinstance(found, dict) else None
    return number if isinstance(number, int) else None


def has_charter_block(sb, raw):
    return shared(sb).has_charter_block(sb, raw)


def label_names(raw):
    return [l for l in (l.get("name") if isinstance(l, dict) else l for l in raw.get("labels") or [] if isinstance(l, (dict, str)))]


def require_open_issue(sb, raw, what, repo, number):
    try:
        shared(sb).require_open_issue(raw, what, repo, number)
    except shared(sb).Refused as problem:
        raise MoveRefused(str(problem)) from None


def is_charter(sb, raw):
    return shared(sb).is_charter(sb, raw)


def check_repo(sb, repo, watched):
    try:
        shared(sb).check_repo(sb, repo, watched)
    except shared(sb).Refused as problem:
        raise MoveRefused(str(problem)) from None


def plan_move(sb, repo, ask, to_charter):
    """Read what the move needs and refuse what it must not do, before anything is written. Returns the plan dict.
    The ask is an open issue (not a pull request, not a charter, not a work item) under a charter; the target is an open issue
    (not a pull request) that is a charter, and is not the ask itself."""
    if ask == to_charter:
        raise MoveRefused("%s#%d cannot be moved to itself" % (repo, ask))
    raw = read_issue(sb, repo, ask)
    require_open_issue(sb, raw, "the ask", repo, ask)
    if has_charter_block(sb, raw) or sb.orchestrator_label(sb.engine.config) in label_names(raw):
        raise MoveRefused("%s#%d is a charter, not an ask" % (repo, ask))
    try:
        try:
            shared(sb).require_charter(sb, repo, to_charter, "the target")
        except shared(sb).Refused as problem:
            raise MoveRefused(str(problem)) from None
        old = parent_of(sb, repo, ask)
        if old is None:
            raise MoveRefused("%s#%d has no parent charter: it is not an ask of any charter" % (repo, ask))
        if not is_charter(sb, read_issue(sb, repo, old)):
            raise MoveRefused("%s#%d is a work item (its parent #%d is not a charter): move its ask" % (repo, ask, old))
    except sb.IntentError as err:
        raise MoveRefused("cannot read the charters: %s" % sb._one_line(str(err), 160)) from None
    return {"repo": repo, "ask": ask, "id": raw["id"], "old": old, "new": to_charter, "state": raw.get("state"),
            "title": raw.get("title") or "", "labels": [l for l in label_names(raw) if isinstance(l, str)]}


def pull_requests(sb, repo, ask):
    """[(number, head sha, head ref, title)] of the open pull requests that reference the ask (its timeline), or a
    string saying why they could not be read."""
    reader = sb.engine.intents
    try:
        events = reader._api("repos/%s/issues/%d/timeline?per_page=100" % (repo, ask), repo)
        numbers = []
        for event in events if isinstance(events, list) else []:
            source = ((event.get("source") or {}).get("issue") or {}) if isinstance(event, dict) else {}
            number = source.get("number")
            if event.get("event") == "cross-referenced" and source.get("pull_request") and isinstance(number, int) \
                    and source.get("state") == "open" and number not in numbers:
                numbers.append(number)
        found = []
        for number in numbers[:PR_LIMIT]:
            pull = reader._api("repos/%s/pulls/%d" % (repo, number), repo)
            head = pull.get("head") or {}
            found.append((number, str(head.get("sha") or "?"), str(head.get("ref") or "?"), str(pull.get("title") or "")))
        return found
    except sb.IntentError as err:
        return "not read (%s)" % sb._one_line(str(err), 100)


def ask_workers(sb, discovery, repo, ask):
    """[(name, boss)] of the agents working on the ask or one of its work items (their issue is the ask or a child of it)."""
    reader = sb.engine.intents
    found = []
    for agent in sorted(discovery.agents.values(), key=lambda a: a.name):
        fleet = agent.fleet or {}
        if fleet.get("role") in ("orchestrator", "chief-of-staff") or not sb._is_issue_number(fleet.get("issue")):
            continue
        if sb._fleet_repo(fleet, sb.engine.config) not in (repo, None):
            continue
        try:
            owner = reader.ask_of(fleet["issue"], repo)[0]
        except sb.IntentError:
            owner = fleet["issue"]
        if owner == ask:
            found.append((agent.name, sb.boss_name(discovery, agent)))
    return found


def held_reminders(items, repo, ask, owner):
    return [item for item in items if owner is not None and item.get("issue") == ask and item.get("to") == owner
            and item.get("repo") in (None, repo)]


def move_reminders(sb, repo, ask, old_owner, new_owner):
    """Carry the old orchestrator's reminders on the ask to the new one; with no new orchestrator running they are
    cancelled (a reminder for nobody would fire into nothing). Returns (verb, [reminders as they were])."""
    state = sb.engine.state_dir
    if state is None or old_owner is None:
        return "carried", []
    with sb.reminders_lock(state):
        items = sb.read_reminders(state)
        mine = held_reminders(items, repo, ask, old_owner)
        before = [dict(item) for item in mine]
        if not mine:
            return "carried", []
        if new_owner is None:
            ids = {item["id"] for item in mine}
            sb.write_reminders(state, [item for item in items if item["id"] not in ids])
            return "cancelled", before
        for item in mine:
            item["to"] = new_owner
        sb.write_reminders(state, items)
    return "carried", before


def handover_note(sb, plan, caller, workers, prs, tag):
    """The handover for the new orchestrator: one text, the six sections each present even when empty. The reminders
    section is filled in by the caller (`{reminders}` is replaced once they have been moved)."""
    waits = [l for l in plan["labels"] if WAIT_LABEL.search(l)]
    lines = ["Handover: ask %s moved from charter #%d to your charter #%d by %s." % (tag, plan["old"], plan["new"], caller),
             "Its Intent, Done when, comments and plan are unchanged: read them on the issue.", ""]
    lines.append("## %s" % HANDOVER_SECTIONS[0])
    lines.append("%s is %s: %s" % (tag, plan["state"], clean(sb, plan["title"]) or "(no title)"))
    lines.append("## %s" % HANDOVER_SECTIONS[1])
    if isinstance(prs, str):
        lines.append("- %s" % prs)
    elif prs:
        lines.extend("- #%d at %s: %s" % (n, clean(sb, sha, 40), clean(sb, title)) for n, sha, _, title in prs)
    else:
        lines.append("- none open")
    lines.append("## %s" % HANDOVER_SECTIONS[2])
    refs = [ref for _, _, ref, _ in prs] if not isinstance(prs, str) else []
    lines.extend("- %s" % clean(sb, ref) for ref in refs) if refs else lines.append("- none known")
    lines.append("## %s" % HANDOVER_SECTIONS[3])
    if workers:
        lines.append("Still finishing under their old boss (not re-bossed):")
        lines.extend("- %s (reports to %s)" % (name, boss or "nobody") for name, boss in workers)
    else:
        lines.append("- none running")
    lines.append("## %s" % HANDOVER_SECTIONS[4])
    lines.extend("- label: %s" % clean(sb, w, 60) for w in waits) if waits else lines.append("- none recorded")
    lines.append("## %s" % HANDOVER_SECTIONS[5])
    lines.append("{reminders}")
    return "\n".join(lines)


def clean(sb, text, limit=120):
    """Text that came from GitHub, as one short line (no control characters) before it goes into a message."""
    return sb._one_line(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(text)), limit)


def reminders_section(sb, verb, held):
    if not held:
        return "- none held by the old orchestrator"
    return "\n".join("- %s %s at %s: %s" % (verb, item["id"], sb.iso(item["at"]), sb._one_line(item.get("text") or "", 100))
                     for item in held)


def old_notice(plan, caller, new_owner, workers, reminder_text, tag):
    lines = ["Ask %s moved to charter #%d (%s) by %s: it is no longer yours." % (
        tag, plan["new"], new_owner or "no orchestrator running there", caller)]
    if workers:
        lines.append("Workers still finishing under you (their boss is unchanged): %s." % ", ".join(name for name, _ in workers))
    else:
        lines.append("No workers were running on it.")
    lines.append(reminder_text)
    return " ".join(lines)


def send_notice(sb, caller, recipient, text, plan):
    """One message to `recipient`. None when delivered, else why not."""
    try:
        outcome = sb.engine.send(caller or OPERATOR, recipient, text, plan["ask"], plan["repo"])
    except Exception as err:   # a recipient that cannot be reached must not hide the rest of the report
        return sb._one_line("%s: %s" % (type(err).__name__, err), 160)
    return None if outcome.get("sent") else "%s (%s)" % (outcome.get("action"), outcome.get("reason"))


def run_move(sb, repo, ask, to_charter, caller, role, out, err, watched=None):
    """Move `repo#ask` to charter `to_charter`. `caller` is the fleet name (None for the operator's plain shell).
    Exit code: 0 done; 1 the caller is refused (audited), the write failed (still under the old charter) or the ask is
    moved and a later step failed (the output says which); 2 the move cannot be made (nothing changed)."""
    engine = sb.engine
    subject = "issue:%s#%d" % (repo, ask)
    refusal = check_caller(caller, role)
    if refusal:
        engine.audit.append(engine.actor, "lanes.move.refused", subject,
                            {"caller": caller, "role": role, "repo": repo, "to": to_charter})
        err("%s: move: %s; nothing was changed" % (sb.PROG, refusal))
        return sb.EXIT_ERROR
    try:
        check_repo(sb, repo, watched)
        plan = plan_move(sb, repo, ask, to_charter)
    except MoveRefused as problem:
        err("%s: move: %s; nothing was changed" % (sb.PROG, problem))
        return sb.EXIT_USAGE
    old, new = plan["old"], to_charter
    if old == new:
        out("ask %s#%d is already under charter #%d: nothing to do" % (repo, ask, new))
        return sb.EXIT_OK
    discovery = engine.discover(engine.now())
    old_names, new_names = charter_orchestrators(sb, discovery, repo, old), charter_orchestrators(sb, discovery, repo, new)
    old_owner, new_owner = (old_names[0] if old_names else None), (new_names[0] if new_names else None)
    workers = ask_workers(sb, discovery, repo, ask)
    prs = pull_requests(sb, repo, ask)
    tag = sb.issue_tag(repo, ask, engine.repos_in_play())
    who = caller or OPERATOR
    failed = []
    # step 1, the one write: a refusal leaves the ask under its old charter. The old parent is read again first: if someone moved
    # the ask since the plan was read, nothing is written and the handover never names the wrong charter.
    try:
        if parent_of(sb, repo, ask) != old:
            err("%s: move: %s was moved by someone else while this was being prepared; nothing was changed. Run it again" % (sb.PROG, tag))
            return sb.EXIT_ERROR
    except sb.IntentError as problem:
        err("%s: move failed at step 'check' (the parent could not be read: %s); nothing was changed" % (sb.PROG, sb._one_line(str(problem), 160)))
        return sb.EXIT_ERROR
    try:
        with engine._external("lanes.move", subject, {"caller": who, "repo": repo, "ask": ask, "from": old, "to": new}):
            sb._gh_write(engine, engine.intents.runner, repo, "repos/%s/issues/%d/sub_issues" % (repo, new), "POST",
                         {"sub_issue_id": plan["id"], "replace_parent": True})
    except sb.IntentError as problem:
        err("%s: move failed at step 're-parent' (the write to charter #%d): %s; the ask is still under charter #%d and "
            "nothing else was done" % (sb.PROG, new, sb._one_line(str(problem), 200), old))
        return sb.EXIT_ERROR
    # step 2, verify: read the parent back, never from the cache.
    engine.intents.invalidate(ask, repo)
    try:
        now_parent = parent_of(sb, repo, ask)
    except sb.IntentError as problem:
        now_parent = new
        failed.append("verify (the parent could not be read back: %s)" % sb._one_line(str(problem), 120))
    if now_parent != new:
        err("%s: move failed at step 'verify': the write was accepted but the ask reads as under charter %s, not #%d; "
            "nothing else was done: look at %s before moving it again"
            % (sb.PROG, "#%d" % now_parent if now_parent else "(none)", new, tag))
        return sb.EXIT_ERROR
    out("moved ask %s from charter #%d to charter #%d" % (tag, old, new))
    # step 3, reminders.
    verb, held = "carried", []
    try:
        verb, held = move_reminders(sb, repo, ask, old_owner, new_owner)
        out("reminders: %d %s%s" % (len(held), verb, " (%s)" % ", ".join(r["id"] for r in held) if held else ""))
        if verb == "cancelled":
            for r in held:
                out("  cancelled, to recreate: %s at %s: %s" % (r["id"], sb.iso(r["at"]), clean(sb, r.get("text") or "", 200)))
    except (sb.ReminderError, OSError) as problem:
        failed.append("reminders (%s)" % sb._one_line(str(problem), 120))
    reminder_text = ("%d reminder(s) of yours on it were %s." % (len(held), verb)) if held else "You held no reminders on it."
    # step 4, the two notices.
    note = handover_note(sb, plan, who, workers, prs, tag).replace("{reminders}", reminders_section(sb, verb, held))
    for owner, text, label, charter in ((new_owner, note, "handover note", new),
                                        (old_owner, old_notice(plan, who, new_owner, workers, reminder_text, tag), "notice", old)):
        if owner is None:
            out("%s: no orchestrator is running charter #%d, so no one was told" % (label, charter))
            continue
        why = send_notice(sb, caller, owner, text, plan)
        if why:
            failed.append("%s to %s (%s)" % (label, owner, why))
        else:
            out("%s: sent to %s" % (label, owner))
    # step 5, what the old charter still holds.
    try:
        remaining = [a for a in engine.intents.open_asks(old, repo) if a.number != ask]
        out("charter #%d still holds %d open ask(s)%s" % (old, len(remaining), ":" if remaining else ""))
        for found in remaining:
            out("  %s  %s | Done when: %s" % (sb.issue_tag(repo, found.number, engine.repos_in_play()),
                                              found.intent or sb.NO_INTENT, found.done_when or "not recorded"))
    except sb.IntentError as problem:
        failed.append("remaining asks (%s)" % sb._one_line(str(problem), 120))
    if failed:
        err("%s: the ask is moved, but these steps failed: %s" % (sb.PROG, "; ".join(failed)))
        return sb.EXIT_ERROR
    return sb.EXIT_OK
