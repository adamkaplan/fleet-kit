"""A closed issue never keeps asking for an answer (ask #570).

The decision list no longer holds a closed issue (see `note_awaiting_issue`). This is the other half: when an issue closes while it carries one of
the fleet's awaiting labels, the daemon takes those labels off, so the closed issue stops asking, and tells the Chief of Staff in ONE line so an
unanswered question is not lost silently. It works from the `issues` closed events the hub keeps (memory only), as `bin/merged_labels.py` works
from pull request merge events, and is built the same way:

- It reads the issue FRESH (`hub._read_one(..., fresh=True)`, the repo's own account) and acts only when GitHub says it is CLOSED and is not a pull
  request. An event that says closed while the fresh read says open (a reopened issue, a stale or forged event), a failed read (audited once as
  `closed.labels_read_failed`, tried again after a back-off), and a repository the switchboard does not watch or excludes (never read) remove
  nothing. An event that carried no awaiting label costs no read.
- It removes ONLY the fleet's `<prefix>:awaiting-user`, `<prefix>:awaiting-cos` and `<prefix>:awaiting-live` labels, by exact name, from the FRESH
  labels (never `parked`, a hold label or any other), through the existing label write path, with one audit line (`closed.labels_removed`).
- The Chief of Staff gets ONE message per issue (the issue and the labels removed); a message that cannot be sent is audited and not repeated.
- The reads and writes stay within the pass's cap (the impact hold's `calls_per_pass`); each issue is handled once.

It lowers no guard that the closed issue could still use: a closed issue's question is no longer a decision. The logic is here; `bin/fleet-switchboard`
calls `run_pass` from the daemon's pass with a namespace `sb` of its own helpers (`NEEDS`).
"""

RETRY_SECONDS = 300
DONE_MAX = 500

NEEDS = ("awaiting_label", "awaiting_cos_label", "getpass", "change_issue_labels", "load_module", "_one_line", "subprocess")


def awaiting_names(sb, config):
    """The exact awaiting label names of this fleet's prefix that may be removed."""
    return {sb.awaiting_label(config), sb.awaiting_cos_label(config), "%s:awaiting-live" % sb.getpass.getuser()}


def run_pass(sb, engine, now, calls):
    """One pass over the closed-issue events the hub holds. Never raises into the caller. Returns the number of gh calls spent."""
    hub = getattr(engine, "hub", None)
    if hub is None or engine.audit is None or not hasattr(hub, "closed_issue_snapshot"):
        return 0
    shared = sb.load_module("merged_labels")   # one definition of "watched and not excluded" and of a label set
    memory = engine.__dict__.setdefault("closed_labels", {"done": {}, "retry": {}, "said": set()})
    ours = awaiting_names(sb, engine.config)
    spent = 0
    for ref, entry in sorted(hub.closed_issue_snapshot().items()):
        if ref in memory["done"] or not isinstance(entry, dict):
            continue
        repo, _, number = ref.rpartition("#")
        if not number.isdigit() or not repo:
            continue
        number = int(number)
        if not (ours & shared.label_set(entry.get("labels"))):
            _done(memory, ref, now)   # the closed event carried no awaiting label: nothing to do, no read
            continue
        if not shared.watched(sb, engine, repo):
            _done(memory, ref, now)   # not watched, or excluded: no read and no write, ever
            continue
        if now < memory["retry"].get(ref, 0):
            continue
        if spent >= calls:
            break
        spent += 1
        try:
            data = hub._read_one(repo, "issues/%d" % number, now, fresh=True)
            state = data.get("state") if isinstance(data, dict) else None
        except Exception as err:   # a failed read removes nothing; said once, tried again later
            memory["retry"][ref] = now + RETRY_SECONDS
            if ref not in memory["said"]:
                memory["said"].add(ref)
                engine.audit.append(engine.actor, "closed.labels_read_failed", ref, {"error": sb._one_line(str(err), 160)})
            continue
        if state != "closed" or "pull_request" in data:   # reopened, stale or forged, or a pull request: nothing is removed
            engine.audit.append(engine.actor, "closed.labels_skipped", ref, {"fresh_state": sb._one_line(str(state), 20)})
            _done(memory, ref, now)
            continue
        remove = sorted(ours & shared.label_set(data.get("labels")))
        if remove:
            runner = getattr(engine, "gh_runner", None) or sb.subprocess.run
            try:
                sb.change_issue_labels(engine, runner, repo, number, remove=remove)
            except Exception as err:   # not marked done: tried again after the back-off
                memory["retry"][ref] = now + RETRY_SECONDS
                engine._note("closed_labels.error", ref, {"error": sb._one_line(str(err), 160)})
                spent += len(remove)
                continue
            spent += len(remove)
            engine.audit.append(engine.actor, "closed.labels_removed", ref, {"issue": ref, "labels": remove})
            _tell(sb, engine, ref, remove)
        _done(memory, ref, now)
    return spent


def _tell(sb, engine, ref, labels):
    """ONE line to the Chief of Staff for this issue. A message that cannot be sent is audited, never repeated (the removal is made)."""
    text = "[closed issue] %s closed while it carried %s: those labels were taken off so it stops asking. Reopen it if it was closed by mistake." % (ref, ", ".join(labels))
    try:
        outcome = engine.send("switchboard", "chief-of-staff", text)
        if not outcome.get("sent"):
            engine.audit.append(engine.actor, "closed.notice_not_sent", ref, {"why": sb._one_line("%s (%s)" % (outcome.get("action"), outcome.get("reason")), 160)})
    except Exception as err:
        engine.audit.append(engine.actor, "closed.notice_not_sent", ref, {"why": sb._one_line(str(err), 160)})


def _done(memory, ref, now):
    memory["done"][ref] = now
    memory["retry"].pop(ref, None)
    memory["said"].discard(ref)
    while len(memory["done"]) > DONE_MAX:
        memory["done"].pop(min(memory["done"], key=memory["done"].get))
