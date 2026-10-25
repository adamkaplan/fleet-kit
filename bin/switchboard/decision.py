# ------------------------------------------------------------ the decision

class MessageTooLong(Exception):
    """A message that would be cut at the delivery cap: refused to the sender, nothing delivered."""


class InterruptRefused(Exception):
    """`send --interrupt` from anyone but the recipient's boss."""


class Plan:
    """What the delivery rule says for one recipient now.

    action: "wait" (batching), "hold" (reason says why), "note" or "wake".
    """

    def __init__(self, recipient, action, reason, pending, observation=None, ready_at=None):
        self.recipient, self.action, self.reason = recipient, action, reason
        self.pending, self.observation, self.ready_at = pending, observation, ready_at
        self.folded = False   # #352: a report folded into the sender's earlier send: a note, never a second wake

    def to_json(self):
        return {"recipient": self.recipient, "action": self.action, "reason": self.reason,
                "ready_at": iso(self.ready_at), "pending": [fact.to_json() for fact in self.pending]}


def hold_reason(observation, missing=None):
    """Why nothing may be written to this agent now, or None."""
    if observation is None:
        return missing or "not found"
    if observation.conflict:
        return "%s is claimed by more than one session (%s); fix the fleet metadata" % (
            observation.name, ", ".join([observation.session_id] + observation.conflict))
    if observation.problem:
        return observation.problem
    if observation.status == "blocked":
        return "%s is blocked on a permission request or a question" % observation.name
    if observation.status not in ("idle", "working"):
        return "%s has status %s in herdr" % (observation.name, observation.status)
    return None


def decide(recipient, pending, observation, now, batch_seconds, engaged_seconds, missing=None):
    """The delivery rule for one recipient's pending facts. Reads nothing; writes nothing.

    Batch: wait until the oldest pending fact is `batch_seconds` old, unless a
    pending fact skips batching (`send`), or under `no-batch`. Then hold if
    the agent is gone, blocked, unknown or unreadable; else a note if you are
    engaged with it, else a wake.
    """
    pending = sorted(pending, key=lambda fact: (fact.created, fact.key))
    # PR 17: standing orders go first and alone, at once, as a note (a wake only under `orders-wake`)
    standing = [fact for fact in pending if fact.orders]
    if standing:
        reason = hold_reason(observation, missing)
        if not reason:
            return Plan(recipient, "wake" if fault("orders-wake") else "note",
                        "standing orders are noted at once, and never wake anyone", standing, observation)
        pending = [fact for fact in pending if not fact.orders]
        if not pending:
            return Plan(recipient, "hold", reason, standing, observation)
    # PR 13: riders are carried, never the reason for a message. Only the others choose the action
    # and start the batch clock; when there is any other, every rider goes with it.
    chosen = pending if fault("no-rider") else [fact for fact in pending if not fact.rider]
    if not chosen:
        return None
    if not fault("no-batch") and all(fact.batch for fact in chosen):
        ready_at = chosen[0].created + batch_seconds
        if now < ready_at:
            return Plan(recipient, "wait", "batching until %s" % iso(ready_at), pending, observation, ready_at)
    reason = hold_reason(observation, missing)
    if reason:
        return Plan(recipient, "hold", reason, pending, observation)
    if observation.engaged(now, engaged_seconds):
        return Plan(recipient, "note", "you prompted %s at %s" % (recipient, iso(observation.last_human_prompt)),
                    pending, observation)
    return Plan(recipient, "wake", "no prompt from you in the engagement window", pending, observation)


def steers_into_turn(config, observation, carried, discovery=None):
    """A wake to an agent that is working goes in as `steer` (with `resume`, so one that outlives its turn starts
    the next at once): the running turn takes it at its next safe point instead of after the turn. Only a
    message that carries a `send` from the agent's own boss; `send_to_working` "queue" turns it off. Measured in
    a throwaway v2 2.0.24: a steer lands after the in-flight tool call, before the next model request."""
    return (config.get("send_to_working", "steer") == "steer" and observation.status == "working"
            and any(fact.kind == "send" and fact.sender and fact.sender == boss_name(discovery, observation) for fact in carried))


def clock_utc(epoch):
    """HH:MMZ (UTC) of an epoch: the time a message was sent or delivered, as the header shows it."""
    try:
        return time.strftime("%H:%MZ", time.gmtime(epoch))
    except (OverflowError, ValueError, OSError, TypeError):
        return "??:??Z"   # a time that is not a time never stops a message


def superseded_by(facts):
    """{key of an older undelivered `send`: the newer `send` that replaces it}: the same sender, about the same issue (repo and number), to the same
    recipient. Never across senders or issues, and a send with no issue is never superseded. The older text is KEPT (the caller marks it, never drops it)."""
    newest = {}
    for fact in facts:
        if fact.kind == "send" and fact.sender and fact.issue:
            group = (fact.recipient, fact.sender, fact.repo, fact.issue)
            if group not in newest or (fact.created, fact.key) > (newest[group].created, newest[group].key):
                newest[group] = fact
    found = {}
    for fact in facts:
        if fact.kind == "send" and fact.sender and fact.issue:
            top = newest[(fact.recipient, fact.sender, fact.repo, fact.issue)]
            if top is not fact:
                found[fact.key] = top
    return found


NEWEST_FIRST_MARK = " [newest first]"


def compose_order(fact):
    """#1275 ordering rule: every pass-through fact (owner wait, held PR, first report, send) sorts BEFORE the merged wake fact whatever its age,
    newest first among themselves; the merged fact (the reminders) is LAST."""
    reminder = bool(getattr(fact, "members", None)) or fact.kind in ("sweep", "triage") or bool(getattr(fact, "wake_class", None))   # the merged fact and the sweep/triage/load items left over
    return (reminder, -fact.created, fact.key)


def compose(recipient, facts, cap=MESSAGE_CAP, section=None, repos=None, delivered=None, resume=None):
    """One message for `facts`, newest first (a newer result is never held behind older reminders). Returns (text, the facts it carries). With `delivered` (epoch) the header says when the
    message was sent (the carried facts' creation) and when it is delivered; a send that a NEWER one from the same sender about the
    same issue replaces (`superseded_by`) stays in the message, marked, so no key is left out of what is acknowledged.

    The first line names the count and the first item; each item follows in
    full. Items that do not fit under `cap` stay pending: the message ends with
    how many, and they go in the next message. A single item longer than the
    cap is cut, and says so. With `section` (PR 6, see sectioned_lines) every
    item that names an issue sits under its ask's Intent lines.
    """
    # #1275: pass-through facts (owner wait, held PR, first report, send: all that is not a merged wake) first, newest first; the merged wake (reminders, and the wake items not merged yet) LAST whatever its age
    facts = sorted(facts, key=compose_order)
    if not facts:
        raise ValueError("nothing to compose")
    if facts[0].orders:  # PR 17: the orders note, whole: every order
        return orders_note(recipient, facts[0], repos), facts[:1]

    replaced = {}
    if section is not None:   # one answer per ask for the whole compose (the retries under the cap must not turn a full header into a brief one)
        _asked, _section = {}, section
        section = lambda issue, repo=None: _asked.setdefault((issue, repo), _section(issue, repo))

    def item(fact):
        where = " (issue %s)" % issue_tag(fact.repo, fact.issue, repos) if fact.issue else ""
        body = fact.summary.strip().replace("\n", "\n  ")
        mark = ""
        if fact.key in replaced:
            mark = " [superseded by the newer message from %s sent %s]" % (fact.sender, clock_utc(replaced[fact.key].created))
        return "- %s from %s%s%s: %s" % (fact.kind, fact.sender or "the fleet", where, mark, body)

    def render(count):
        lead = next((fact for fact in facts if not fact.rider), facts[0])
        taken = facts[:count]
        replaced.clear()
        replaced.update(superseded_by(taken))
        when = ""
        if delivered is not None:
            first, last = clock_utc(min(f.created for f in taken)), clock_utc(max(f.created for f in taken))
            when = " (sent %s, delivered %s)" % (first if first == last else "%s-%s" % (first, last), clock_utc(delivered))
        waiting = sorted({t for f in taken for t in f.supersedes})
        if waiting:   # the earlier messages stay where they are, unchanged: this says only that the newer one replaces them
            names = sorted({f.sender for f in taken if f.supersedes and f.sender})
            when = (when[:-1] + "; " if when else " (") + "supersedes %d earlier message%s from %s on this issue, still waiting: sent %s)" % (
                len(waiting), "" if len(waiting) == 1 else "s", ", ".join(names) or "the same sender", ", ".join(clock_utc(t) for t in waiting[:5]))
        lead_text = _one_line(lead.summary, 200)
        while lead_text.rstrip().endswith(NEWEST_FIRST_MARK.strip()):   # an agent's text must never forge the daemon's mark (numbering): only the count decides it
            lead_text = lead_text.rstrip()[:-len(NEWEST_FIRST_MARK.strip())].rstrip()
        head = "[switchboard] %d for %s%s: %s" % (count, recipient, when, lead_text)
        if count > 1:
            head += NEWEST_FIRST_MARK   # #1275: a reader of the transcript puts the lines back in time order
        idle = [fact for fact in taken if fact.kind == WORKER_IDLE and fact.rider and getattr(fact, "clean", False)]
        shown = [fact for fact in taken if fact not in idle] if len(idle) < len(taken) else taken
        if shown is taken:
            idle = []  # nothing but idle riders: there is no news to fold them under
        lines = [head] + (sectioned_lines(shown, section, item) if section else [item(fact) for fact in shown])
        if idle:  # no-change worker lines are ONE quiet line next to real news (the keys are still carried)
            names = sorted({fact.sender or "a worker" for fact in idle})
            lines.append("- quiet: %d worker finish%s with no change (%s)" % (
                len(idle), "" if len(idle) == 1 else "es", ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")))
        rest = len(facts) - count
        if rest:
            lines.append("\u2026 and %d more: run fleet-switchboard pending %s" % (rest, recipient))
        return "\n".join(lines)

    for count in range(len(facts), 0, -1):
        text = render(count)
        if len(text) <= cap:
            return text, facts[:count]
    if facts[0].kind != "send" and not getattr(facts[0], "members", None):   # #1302: a finding over the cap is carried over successive messages, never cut
        part = finding_part(recipient, facts[0], facts[1:], cap, section, repos, delivered, resume)
        if part is not None:
            return part
    marker = "\n[cut at %d characters]" % cap
    return text[:cap - len(marker)] + marker, facts[:1]


FINDING_MAX_PARTS = 50   # a finding is carried over at most this many messages; beyond it the rest is refused (named, audited), never dropped silently
LINE_CUT_MARK = " \u2026[line cut]"
FINDING_REFUSED = []     # (key, kind, text refused): the daemon writes each to the audit as 'finding-refused' and empties this list


def finding_parts(fact, budget):
    """The summary of one finding as chunks of WHOLE lines, each at most `budget` characters (a single longer line is cut with a visible
    mark: the only case that shortens; the daemon audits the full line). Pure and deterministic: the same fact gives the same chunks, so the
    continuation is derived from how many parts were delivered. Returns (chunks, refused_text, cut_lines)."""
    lines, cut = [], []
    for line in fact.summary.strip().split("\n"):
        line = "".join(c if c.isprintable() else " " for c in line).rstrip()
        if len(line) > budget:
            cut.append(line)
            line = line[:budget - len(LINE_CUT_MARK)] + LINE_CUT_MARK
        lines.append(line)
    chunks, now, size = [], [], 0
    for line in lines:
        if now and size + len(line) + 3 > budget:
            chunks.append("\n".join(now))
            now, size = [], 0
        now.append(line)
        size += len(line) + 3
    if now:
        chunks.append("\n".join(now))
    refused = ""
    if len(chunks) > FINDING_MAX_PARTS:
        refused = "\n".join(chunks[FINDING_MAX_PARTS:])
        chunks = chunks[:FINDING_MAX_PARTS]
    return chunks, refused, cut


def finding_part(recipient, fact, rest, cap, section, repos, delivered, resume):
    """#1302: the next part of an over-cap finding. `resume` maps a key to how many parts were already delivered (persisted by the daemon,
    so a restart continues). The carried fact has `partial=(i, n)` when more parts follow (its key must NOT be acknowledged yet)."""
    done = (resume or {}).get(fact.key, 0)
    probe = copy.copy(fact)
    probe.summary = "(part 99 of 99)\n"
    fixed = len(compose(recipient, [probe], 10 ** 9, section, repos, delivered, None)[0])
    budget = max(200, cap - fixed - 400)   # the header repeats the first 200 characters; the item indents every line; room for the part label and the refusal note
    chunks, refused, cut = finding_parts(fact, budget)
    if len(chunks) < 2:
        if not cut:
            return None
        marker = "\n[cut at %d characters]" % cap   # one line over the cap: the only cut, marked; the full line goes to the audit
        whole = copy.copy(fact)
        whole.cut_lines = cut
        text = compose(recipient, [whole], 10 ** 9, section, repos, delivered, None)[0]
        return text[:cap - len(marker)] + marker, [whole]
    n = len(chunks)
    index = min(done, n - 1)
    short = copy.copy(fact)
    last = index == n - 1
    tail = "\n(%d characters of this finding past the %d-part limit were refused; the full text is in the audit)" % (len(refused), FINDING_MAX_PARTS) if refused and last else ""
    short.summary = "(part %d of %d)\n%s%s" % (index + 1, n, chunks[index], tail)
    if len(short.summary) + fixed > cap:
        short.summary = short.summary[:max(0, cap - fixed - 1)]
    if not last:
        short.partial = (index + 1, n)
    short.cut_lines, short.refused = (cut, refused) if index == 0 else ([], "")
    text, carried = compose(recipient, [short], 10 ** 9, section, repos, delivered, None)
    if len(text) > cap:
        text = text[:cap - 1]
    return text, [short]


ORDERS_CLOSING = ("These are the owner's standing instructions for this charter. Act on them without asking "
                  "again, within their words. They cover nothing they do not name.")
COS_ORDERS_CLOSING = ("These are the owner's standing orders to you. Answer for the owner only within their words; "
                      "anything they do not name goes to the owner.")


def orders_note(recipient, fact, repos=None):
    """The standing-orders note: each order on its own line, then the closing line. Never cut. PR 18: the Chief of
    Staff's own orders (key `orders:cos:...`) are the file's text as the owner wrote it."""
    if fact.key.startswith("%s:cos:" % ORDERS_KIND):
        return "\n".join(["[switchboard] standing orders for %s (the owner's, to you):" % recipient,
                          fact.summary.strip(), COS_ORDERS_CLOSING])
    lines = ["[switchboard] standing orders for %s (charter %s):" % (recipient, issue_tag(fact.repo, fact.issue, repos))]
    lines.extend("- %s" % order for order in fact.summary.splitlines() if order.strip())
    lines.append(ORDERS_CLOSING)
    return "\n".join(lines)


def is_withdrawal(summary):
    """A `withdrawn` report rider never ages out: only a message carrying it (or its worker going) drops it."""
    return (summary or "").startswith("withdrawn: ")


def collapse_riders(facts, now, max_age):
    """PR 13: riders cut to what is still worth carrying. A rider older than `max_age` seconds is
    dropped, and only the newest `worker.idle` of each sender stays. Facts that are not riders pass."""
    newest = {}
    for fact in facts:
        if fact.rider and fact.kind == WORKER_IDLE:
            known = newest.get(fact.sender)
            if known is None or (fact.created, fact.key) > (known.created, known.key):
                newest[fact.sender] = fact
    kept = []
    for fact in facts:
        if not fact.rider:
            kept.append(fact)
        elif now - fact.created > max_age and not is_withdrawal(fact.summary):
            continue
        elif fact.kind == WORKER_IDLE and newest.get(fact.sender) is not fact:
            continue
        else:
            kept.append(fact)
    return kept


DESCRIBED_AS = {"send": "message", "report": "report", "worker.idle": "finished", "worker.stopped": "stopped",
                "worker.blocked": "blocked", "nudge": "nudge", "sweep": "sweep", "triage": "triage", "plan": "plan posted", "alert": "alert issue", "disk": "disk guard", "janitor": "janitor preview"}


def _line_of(text, limit):
    """`text` as one line of at most `limit` characters, with anything shaped like a bearer
    credential or a provider key taken out."""
    return decision_core.line_of(text, limit)


def describe_items(recipient, facts, repos=None):
    """The one human line v2's TUI shows for a message (PR 13): `switchboard: <sender> <what> on #<issue>:
    <first 100 characters>` for one item, `switchboard: <n> items for <recipient>: <first item, 80 characters>`
    for several. At most DESCRIPTION_MAX characters, never a newline."""
    facts = sorted(facts, key=lambda fact: (fact.created, fact.key))
    if all(fact.orders for fact in facts):
        return "switchboard: standing orders for %s" % recipient
    lead = next((fact for fact in facts if not fact.rider), facts[0])
    if len(facts) == 1:
        where = " on %s" % issue_tag(lead.repo, lead.issue, repos) if lead.issue else ""
        line = "switchboard: %s %s%s: %s" % (lead.sender or "the fleet", DESCRIBED_AS.get(lead.kind, lead.kind),
                                             where, _line_of(lead.summary, 100))
    else:
        line = "switchboard: %d items for %s: %s" % (len(facts), recipient, _line_of(lead.summary, 80))
    return _one_line(line, DESCRIPTION_MAX)
