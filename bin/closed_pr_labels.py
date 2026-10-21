"""A pull request closed without merging is released like a merged one (ask #963).

The merged clean-up (`bin/merged_labels.py`, #559) is scoped to a merge event, and the open release (`release_impact_hold` in `bin/impact_hold.py`, #451) to a pull
request the pass still judges: a pull request closed UNMERGED (replaced by a successor, say) fell between the two and kept its `merge-captain` and `hold-*` labels, and
the sweep's S9 finding reported it for ever. This is the daemon's look at the `pull_request` events the hub already keeps (a `closed` action whose payload says it was
not merged): for each such pull request the daemon's OWN record (`impact-hold.json`, `applied[pr].added`, read by `_labels_ours`) lists labels for, it reads the pull
request FRESH and, only when GitHub says it is CLOSED (not MERGED, not OPEN), removes ONLY the labels that record lists and the pull request still carries, through the
label write path, drops the record, and writes ONE audit line (`closed.labels_removed`: the pull request and the labels).

Unlike the merged clean-up it never removes a label by name alone: a label a person put on, a hold label the record did not add and a pull request with no record stay. The
owner's own hold (`hold-owner`) is never removed, whatever the record lists (#891). It fails closed: a record that cannot be read, a failed read (audited once, tried again
after a back-off), a fresh state that is not exactly CLOSED, or a repository that is not watched or is excluded removes nothing. Dropping the record makes a second pass
write nothing. The reads share the pass's cap of gh calls (a read, and a write per label removed). It lowers no guard: the pull request is closed.

The logic is here; `bin/fleet-switchboard` calls `run_pass` from the daemon's pass with a namespace `sb` of its own helpers (`NEEDS`).
"""

RETRY_SECONDS = 300
DONE_MAX = 500

NEEDS = ("label_names", "OWNER_HOLD_KEY", "read_pr_json", "change_issue_labels", "load_module", "_one_line", "subprocess",
         "read_impact_state", "write_impact_state", "impact_state_lock", "_labels_ours")


def recorded(sb, engine, ref):
    """(labels, problem): what the daemon's record says it added to `ref`, never the owner's hold; ([], reason) when the record cannot be trusted."""
    state, problem = sb.read_impact_state(engine.state_dir)
    if problem:
        return [], problem
    names = sb.label_names(engine.config)
    return [l for l in sb._labels_ours(names, state, ref, wide=True) if l != names[sb.OWNER_HOLD_KEY]], None


def label_set(raw):
    return {(l.get("name") if isinstance(l, dict) else l) for l in (raw if isinstance(raw, list) else []) if isinstance(l, (dict, str))} - {None}


def run_pass(sb, engine, now, calls):
    """One pass over the closed-unmerged events the hub holds. Never raises into the caller. Returns the number of gh calls spent."""
    hub = getattr(engine, "hub", None)
    if hub is None or engine.audit is None or not hasattr(hub, "pr_event_snapshot"):
        return 0
    memory = engine.__dict__.setdefault("closed_pr_labels", {"done": {}, "retry": {}, "said": set()})
    merged = sb.load_module("merged_labels")
    spent = 0
    for ref, entry in sorted(hub.pr_event_snapshot().items()):
        if not isinstance(entry, dict) or entry.get("state") != "closed" or entry.get("via") != "event" or memory["done"].get(ref) == entry.get("at"):
            continue
        repo, _, number = ref.rpartition("#")
        if not number.isdigit() or not repo:
            continue
        number = int(number)
        if not merged.watched(sb, engine, repo):
            _done(memory, ref, entry)
            continue
        ours, problem = recorded(sb, engine, ref)
        if problem:   # a record nobody can trust removes nothing; said once, tried again next pass
            if ref not in memory["said"]:
                memory["said"].add(ref)
                engine.audit.append(engine.actor, "closed.labels_record_unreadable", ref, {"error": sb._one_line(problem, 160)})
            continue
        if not ours:
            _done(memory, ref, entry)   # no record of the daemon's labels: nothing to do, no read
            continue
        if now < memory["retry"].get(ref, 0):
            continue
        if spent >= calls:
            break
        spent += 1
        try:
            raw = sb.read_pr_json(engine, repo, number)
            fresh_state = raw.get("state") if isinstance(raw, dict) else None
        except Exception as err:
            memory["retry"][ref] = now + RETRY_SECONDS
            if ref not in memory["said"]:
                memory["said"].add(ref)
                engine.audit.append(engine.actor, "closed.labels_read_failed", ref, {"error": sb._one_line(str(err), 160)})
            continue
        if fresh_state != "CLOSED":   # merged, reopened, stale or unreadable: nothing is removed
            engine.audit.append(engine.actor, "closed.labels_skipped", ref, {"fresh_state": sb._one_line(str(fresh_state), 20)})
            _done(memory, ref, entry)
            continue
        present = label_set(raw.get("labels"))
        try:
            with sb.impact_state_lock(engine.state_dir):   # the record is read again under the lock: the removal and its consumption are one step
                state, problem = sb.read_impact_state(engine.state_dir)
                if problem:
                    continue
                names = sb.label_names(engine.config)
                listed = [l for l in sb._labels_ours(names, state, ref, wide=True) if l != names[sb.OWNER_HOLD_KEY]]
                remove = [l for l in listed if l in present]
                runner = getattr(engine, "gh_runner", None) or sb.subprocess.run
                if remove:
                    sb.change_issue_labels(engine, runner, repo, number, remove=remove)
                    spent += len(remove)
                state["applied"].pop(ref, None)
                sb.write_impact_state(engine.state_dir, state)
        except Exception as err:   # not marked done: tried again after the back-off
            memory["retry"][ref] = now + RETRY_SECONDS
            engine._note("closed_pr_labels.error", ref, {"error": sb._one_line(str(err), 160)})
            continue
        if remove:
            engine.audit.append(engine.actor, "closed.labels_removed", ref, {"pr": ref, "labels": remove})
        _done(memory, ref, entry)
    return spent


def _done(memory, ref, entry):
    memory["done"][ref] = entry.get("at")
    memory["retry"].pop(ref, None)
    memory["said"].discard(ref)
    while len(memory["done"]) > DONE_MAX:
        memory["done"].pop(next(iter(memory["done"])))
