"""A merged pull request does not keep its hold (ask #559).

Nothing removed the `merge-captain` and `hold-*` labels when a pull request merged, so the sweep reported the leftovers again and again. This is
the daemon's look at the `pull_request` events the hub already keeps (a `closed` action whose payload says merged): for each such pull request
that carried a fleet merge or hold label, it reads the pull request FRESH (`gh pr view`, the repo's own account) and, only when GitHub says it
is MERGED, removes ONLY those labels (the fleet's `<prefix>:merge-captain` and `<prefix>:hold-*`, by exact name; nothing else) through the
existing label write path, and writes ONE audit line (`merged.labels_removed`: the pull request and the labels).

It fails closed: an event that says merged while the fresh read says open or closed (stale, forged or raced), a failed read, a repository the
switchboard does not watch or excludes, or a pull request whose event carried no fleet label, removes nothing (and a repository that is not
watched, or excluded, is never read). A failed read is audited once per pull request and tried again after a back-off. The reads are bounded
by its own per-pass cap (`calls`: the impact hold's `calls_per_pass`, default `IMPACT_PASS_CALLS`; a budget of its own, spent after the impact hold's), each removal being one write. A merge whose event was missed (the daemon was down: the hub's events are memory only) is not cleaned, and the sweep no longer reports it. It lowers no guard: the pull request has already merged.

The logic is here; `bin/fleet-switchboard` calls `run_pass` from the daemon's pass with a namespace `sb` of its own helpers (`NEEDS`).
"""

RETRY_SECONDS = 300
DONE_MAX = 500   # pull requests remembered as handled (memory only; an event is not replayed)

NEEDS = ("label_names", "CAPTAIN_HOLD_LABELS", "OWNER_HOLD_KEY", "read_pr_json", "change_issue_labels", "load_module", "_one_line", "subprocess")


def fleet_labels(sb, config):
    """The exact label names this module may remove: the fleet's `merge-captain` and every `hold-*` label of its prefix EXCEPT the owner's own hold (#891: only the owner's plain
    clear lifts it, and it stays on a merged pull request as the evidence it merged against one)."""
    names = sb.label_names(config)
    return {names[key] for key in ("merge-captain",) + tuple(sb.CAPTAIN_HOLD_LABELS) if key in names and key != sb.OWNER_HOLD_KEY}


def label_set(raw):
    return {(l.get("name") if isinstance(l, dict) else l) for l in (raw if isinstance(raw, list) else []) if isinstance(l, (dict, str))} - {None}


def watched(sb, engine, repo):
    """True for a repository the switchboard watches and does not exclude."""
    if sb.load_module("repo_exclusion").is_excluded(engine.config, repo):
        return False
    return repo.lower() in {r.lower() for r in engine.repos_in_play()}


def run_pass(sb, engine, now, calls):
    """One pass over the merge events the hub holds. `calls` is the pass's budget of gh calls (a read, and a write per label removed).
    Never raises into the caller. Returns the number of gh calls spent."""
    hub = getattr(engine, "hub", None)
    if hub is None or engine.audit is None or not hasattr(hub, "pr_event_snapshot"):
        return 0
    memory = engine.__dict__.setdefault("merged_labels", {"done": {}, "retry": {}, "said": set()})
    ours = fleet_labels(sb, engine.config)
    spent = 0
    for ref, entry in sorted(hub.pr_event_snapshot().items()):
        if not isinstance(entry, dict) or entry.get("state") != "merged" or entry.get("via") != "event" or ref in memory["done"]:
            continue
        repo, _, number = ref.rpartition("#")
        if not number.isdigit() or not repo:
            continue
        number = int(number)
        carried = sorted(ours & label_set(entry.get("labels")))
        if not carried and sb.label_names(engine.config)[sb.OWNER_HOLD_KEY] not in label_set(entry.get("labels")):   # #891: the owner's own hold is read too, to be said and kept
            _done(memory, ref, now)   # the merge event carried no fleet label: nothing to do, no read
            continue
        if not watched(sb, engine, repo):
            _done(memory, ref, now)   # not watched, or excluded: no read and no write, ever
            continue
        if now < memory["retry"].get(ref, 0):
            continue
        if spent >= calls:
            break   # the rest wait for the next pass
        spent += 1
        try:
            raw = sb.read_pr_json(engine, repo, number)
            fresh_state = raw.get("state") if isinstance(raw, dict) else None
        except Exception as err:   # a failed read removes nothing; said once, tried again later
            memory["retry"][ref] = now + RETRY_SECONDS
            if ref not in memory["said"]:
                memory["said"].add(ref)
                engine.audit.append(engine.actor, "merged.labels_read_failed", ref, {"error": sb._one_line(str(err), 160)})
            continue
        if fresh_state != "MERGED":   # the event said merged and GitHub does not (stale, forged or raced): nothing is removed
            engine.audit.append(engine.actor, "merged.labels_skipped", ref, {"fresh_state": sb._one_line(str(fresh_state), 20)})
            _done(memory, ref, now)
            continue
        present = label_set(raw.get("labels"))
        remove = sorted(ours & present)
        owner_hold = sb.label_names(engine.config)[sb.OWNER_HOLD_KEY]
        if owner_hold in present:   # #891: kept, and said once per merged pull request
            engine.audit.append(engine.actor, "merged.owner_hold_kept", ref, {"pr": ref, "label": owner_hold, "why": "merged against the owner's own hold: only the owner's plain clear lifts it"})
        if remove:
            runner = getattr(engine, "gh_runner", None) or sb.subprocess.run
            try:
                sb.change_issue_labels(engine, runner, repo, number, remove=remove)
            except Exception as err:   # not marked done: tried again after the back-off
                memory["retry"][ref] = now + RETRY_SECONDS
                engine._note("merged_labels.error", ref, {"error": sb._one_line(str(err), 160)})
                spent += len(remove)
                continue
            spent += len(remove)
            engine.audit.append(engine.actor, "merged.labels_removed", ref, {"pr": ref, "labels": remove})
        _done(memory, ref, now)
    return spent


def _done(memory, ref, now):
    memory["done"][ref] = now
    memory["retry"].pop(ref, None)
    memory["said"].discard(ref)
    while len(memory["done"]) > DONE_MAX:
        memory["done"].pop(min(memory["done"], key=memory["done"].get))
