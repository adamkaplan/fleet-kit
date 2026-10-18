"""Verdicts for a work item that has no pull request (#770, plan: fleet-kit#777).

A PR's verdicts are bound to a writer set the evaluator cannot choose (its GitHub author, its proofs, its notice) and to one commit (its head). A work item has
neither, so this module gives it both, WITHOUT touching a PR rule:

  - the writer set of an item (`excluded`): the issue's GitHub author, every agent that recorded an item proof, the orchestrator that registered the tester,
    and every agent of the fleet that is not a tester and works the item or its ask or reports to that orchestrator (and any agent in one of their sessions);
  - the reference commit (`pinned`): the tip of the default branch when the orchestrator registered a tester, stored in the registration, never read again;
  - the registration (`registration_problem`): a ledger entry the switchboard writes at `launch` and nobody else (kind `item_tester`), good for one item,
    one session and `TTL` seconds;
  - the CI verdict (`ci_decision`): only the repository's own checks, `plan_gates.repos.<repo>.item_checks` (default: none), by name AND app.

Pure: it reads plain values and returns plain values. No file, no GitHub, no clock of its own, no agent.
"""
import re

KIND = "item_tester"            # the ledger kind of a registration; written by `launch` only
TTL = 24 * 3600                 # a registration is good for this long; a longer job gets a new launch
CHECK_KEYS = ("name", "app")
MAX_CHECKS = 20


def item_ref(repo, number):
    return "%s#%d" % (repo, number)


def validate_item_checks(raw, source):
    """`plan_gates.repos.<repo>.item_checks`: absent or [] means no check is accepted (the default). Otherwise a list of {"name", "app"}: the check run's name and the slug of
    the GitHub App that owns it, both required. Returns the completed list; raises ValueError naming the key."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_CHECKS:
        raise ValueError("%s: 'item_checks' must be a list of at most %d {name, app} objects" % (source, MAX_CHECKS))
    out = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != set(CHECK_KEYS) or not all(isinstance(entry[k], str) and 0 < len(entry[k].strip()) <= 100 for k in CHECK_KEYS):
            raise ValueError("%s: each 'item_checks' entry must be exactly {name, app}, two non-empty strings (the check run's name and its GitHub App slug)" % source)
        item = {k: entry[k].strip() for k in CHECK_KEYS}
        if item in out:
            raise ValueError("%s: 'item_checks' lists %s/%s twice" % (source, item["name"], item["app"]))
        out.append(item)
    return out


def registrations(entries, item):
    """The `item_tester` entries of `item`, oldest first."""
    return [e for e in entries if e.get("kind") == KIND and e.get("pr") == item]


def pinned(entries, item):
    """The reference commit of `item`: the one stored in its NEWEST registration, or None. It changes only by a new registration, and every verdict at the old commit
    then reads `head moved` (G1's own rule)."""
    regs = registrations(entries, item)
    return regs[-1].get("head") if regs else None


def registration_problem(entries, item, tester, session, now):
    """None when `tester` (in `session`) holds a live registration for THIS item, else the sentence why not. Live: its newest entry names this session and is under TTL old."""
    mine = [e for e in registrations(entries, item) if e.get("tester") == tester]
    if not mine:
        return "%s is not registered as a tester for %s: its orchestrator registers it by launching it with --issue for this item" % (tester, item)
    entry = mine[-1]
    if session is None or entry.get("session") != session:
        return "%s runs in another session than the one registered for %s" % (tester, item)
    if not isinstance(entry.get("ts"), (int, float)) or now - entry["ts"] > TTL:
        return "registration expired: %s was registered for %s more than %d hours ago; its orchestrator launches a tester again" % (tester, item, TTL // 3600)
    return None


def excluded(item, number, ask, issue_author, entries, fleets, sessions):
    """The writers of the item: who may NEVER record its verdicts. `fleets` is {name: metadata.fleet}, `sessions` {name: session id}, `ask` the item's parent issue
    number (or None). A registered tester is not excluded for reporting on the item; it is excluded only if it recorded an item proof (an author)."""
    names = {issue_author}
    names.update(e.get("by") for e in entries if e.get("pr") == item and e.get("kind") == "proof")
    orchestrators = {e.get("orchestrator") for e in registrations(entries, item)} - {None}
    names.update(orchestrators)
    for name, fleet in (fleets or {}).items():
        if not isinstance(fleet, dict) or fleet.get("role") == "tester":
            continue
        works_it = fleet.get("issue") is not None and fleet.get("issue") in (number, ask)
        if works_it or fleet.get("reports_to") in orchestrators:
            names.add(name)
    names = {n for n in names if n}
    mine = {(sessions or {}).get(n) for n in names} - {None}
    names.update(n for n, s in (sessions or {}).items() if s in mine)
    return names


def verdict_problem(item, caller, entries, writers, session, now, head):
    """None when `caller` may record a verdict for `item` at `head`, else the sentence why not. The caller must also be a launched tester (the caller checks
    `launch_testers.launched` and the lane rule, which are the PR path's unchanged ones)."""
    if not writers:
        return "no writer of %s is known to the switchboard: the issue's author could not be read, so no one can be excluded" % item
    if caller in writers:
        return "%s wrote or runs %s (its author, notifier, orchestrator or the orchestrator's coder, or it recorded a proof): it cannot record a verdict on it" % (caller, item)
    why = registration_problem(entries, item, caller, session, now)
    if why:
        return why
    pin = pinned(entries, item)
    if not pin:
        return "%s has no reference commit" % item
    if head != pin:
        return "--head %s is not the reference commit of %s (%s): a verdict is judged at the commit pinned at registration; nothing was recorded" % (
            (head or "?")[:12], item, pin[:12])
    return None


def claim_problem(scenario, number, plan_is_own):
    """None when the item may claim `scenario` (the caller has already refused any scenario `claim_of` does not read as `pr`), else why not. A plan that is not the item's own
    (an ask's plan, a section of it) is claimed only through the scenarios sliced to this item."""
    if plan_is_own:
        return None
    sliced = " ".join(str(scenario.get("slice", "")).split()).lstrip("#")
    if sliced == str(number):
        return None
    return "%s is not sliced to this item (slice: %s): an item claims only its own plan's scenarios, or the scenarios of a shared plan marked `slice: #%d`" % (
        scenario.get("id", "?"), sliced or "none", number)


def ci_decision(runs, checks):
    """(ok, seen, why) for the check runs at the reference commit. `runs` is the Checks API's list ({"name", "status", "conclusion", "app": {"slug"}}). Every listed
    {name, app} must have a run, every such run completed, and every one a success; a run with the right name from another app, or any other check, counts for nothing.
    With no listed check nothing is ever accepted."""
    if not checks:
        return False, [], "no item_checks are configured for this repository: no CI verdict is accepted"
    seen = []
    for want in checks:
        mine = [r for r in (runs or []) if isinstance(r, dict) and r.get("name") == want["name"]
                and isinstance(r.get("app"), dict) and r["app"].get("slug") == want["app"]]
        seen.append({"name": want["name"], "app": want["app"], "runs": len(mine)})
        if not mine:
            return False, seen, "no run of %s by %s at the reference commit" % (want["name"], want["app"])
        if any(r.get("status") != "completed" for r in mine):
            return False, seen, "%s is not complete at the reference commit" % want["name"]
        if any(r.get("conclusion") != "success" for r in mine):
            return False, seen, "%s did not succeed at the reference commit" % want["name"]
    return True, seen, None


def lane_ok(chain, charter):
    """True when the orchestrator's charter is the item, its ask or one of its parents (`chain` is the issue numbers from the item up). Fail closed on anything unreadable."""
    return isinstance(charter, int) and not isinstance(charter, bool) and charter in (chain or ())


def sha_ok(text):
    return isinstance(text, str) and re.fullmatch(r"[0-9a-f]{40}", text) is not None
