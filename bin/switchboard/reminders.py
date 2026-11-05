# ---- reminders

def parse_when(text, now):
    """`30m`, `+2h`, `1d` (from now), or an ISO-8601 time with an offset or `Z`. Epoch seconds."""
    match = re.fullmatch(r"\+?([0-9]+)([smhd])", text.strip())
    if match:
        return now + int(match.group(1)) * REMINDER_UNITS[match.group(2)]
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ReminderError("when must be a delay such as 30m, 2h or 1d, or an ISO time with a zone "
                            "such as 2030-01-02T09:00:00Z, got %r" % text) from None
    if moment.tzinfo is None:
        raise ReminderError("when %r has no time zone; add Z or an offset" % text)
    return moment.timestamp()


class ReminderError(FactError):
    """reminders.json cannot be read or written, or a reminder cannot be made."""


@contextmanager
def reminders_lock(state_dir):
    """Serialises the CLI and the daemon around the read-modify-write of reminders.json."""
    fd = os.open(str(Path(state_dir) / REMINDER_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_reminders(state_dir):
    path = Path(state_dir) / REMINDERS
    try:
        raw = load_module("record_json").parse_bounded(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as err:
        raise ReminderError("cannot read %s: %s" % (path, err)) from None
    items = raw.get("reminders") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise ReminderError("%s must hold {\"reminders\": [...]}" % path)
    for item in items:
        if not (isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("to"), str)
                and _is_number(item.get("at")) and isinstance(item.get("text"), str)):
            raise ReminderError("%s has a reminder without id, to, at and text: %r" % (path, item))
        if item.get("condition") is not None and not isinstance(item["condition"], dict):
            raise ReminderError("%s has a reminder whose condition is not an object: %r" % (path, item))
    return items


def write_reminders(state_dir, items):
    """Replace reminders.json atomically, mode 0600: a crash leaves the old file or the new one."""
    path = Path(state_dir) / REMINDERS
    temp = path.with_name("%s.%d.tmp" % (REMINDERS, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"reminders": items}, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(path))
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise


LATE_NOTE_SECONDS = 300
TEXT_CAP = 4000  # mirrors MESSAGE_CAP in bin/fleet-switchboard: a text over it is refused on the command line and carried in parts from `--text-file` (#1355)
TEXT_FILE_MAX = 100000  # characters of a `--text-file`: about 30 parts, well inside the 50-part bound of the parts carrier, so nothing is ever refused after arming


REMINDER_TTL_MAX = 86400   # #1563: the longest ttl `remind --ttl` takes


def parse_ttl(text):
    """`--ttl 30m` (seconds, minutes, hours, days) as whole seconds, 1 to 24 hours; ReminderError otherwise (one line)."""
    match = re.fullmatch(r"([0-9]{1,9})([smhd])", str(text).strip())
    seconds = int(match.group(1)) * REMINDER_UNITS[match.group(2)] if match else 0
    if not match or seconds < 1 or seconds > REMINDER_TTL_MAX:
        raise ReminderError("--ttl must be a duration such as 30m or 2h, from 1s to 24h, got %r" % str(text)[:40])
    return seconds


def ttl_text(seconds):
    """1800 as `30m`: the largest unit that divides it exactly."""
    seconds = int(seconds)
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return "%d%s" % (seconds // size, unit)
    return "%ds" % seconds


def add_reminder(state_dir, to, at, created, text, issue=None, sender=None, repo=None, condition=None, key=None, wake_class=None, ttl=None):
    if not isinstance(to, str) or not to.strip():
        raise ReminderError("a reminder needs the name of the agent it is for")
    if not text.strip():
        raise ReminderError("a reminder needs text")
    item = {"id": uuid.uuid4().hex[:12], "to": to, "at": float(at), "created": float(created),
            "text": text, "issue": issue, "from": sender}
    if repo:
        item["repo"] = repo  # PR 15: the repo `issue` is in
    if wake_class:
        item["wake_class"] = wake_class  # #1275: folded with the sweep and triage items of the same wake
    if condition:
        item["condition"] = dict(condition)  # checked when it is due: {"unless_report_from", "since"} or {"while_running"}
    if key:
        item["key"] = key
    if ttl:
        item["ttl"] = int(ttl)   # #1563: seconds after `at` past which the copy is dropped, audited
    with reminders_lock(state_dir):
        items = read_reminders(state_dir)
        if key and isinstance(sender, str) and sender.strip():   # one pending per owner and key; another owner or key, a fired one and a sender-less one are never touched
            items = [i for i in items if not (i.get("key") == key and i.get("from") == sender and i.get("at", 0) > created)]
        write_reminders(state_dir, items + [item])
    return item


# ---- repeats (#1275): a reminder repeated for the same (kind, subject, state) is delivered once per REPEAT_WINDOW

REPEAT_WINDOW = 6 * 3600
REPEAT_FILE = "reminder-repeats.json"
RECORD_CAP = 1000000   # bytes a state record may have before it is not read


def read_record_file(path, cap=RECORD_CAP):
    """THE safe reader of a small JSON state record: never follows a symlink, never blocks on a FIFO or device, refuses a file that is not
    regular or is over `cap` bytes. Returns the parsed value or None (missing, unsafe, too big or damaged)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            return None
        data = os.read(fd, cap + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return load_module("record_json").parse_bounded(data.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None


def _repeat_read(state_dir):
    raw = read_record_file(Path(state_dir) / REPEAT_FILE)
    return raw if isinstance(raw, dict) else {}


def last_send_at(state_dir, sender, recipient):
    """When `sender` last sent to `recipient` (last-reports.json `send_to`), or None."""
    try:
        entry = _read_small(state_dir, LAST_REPORT_FILE).get(sender, {})
        sent = (entry.get("send_to") or {}).get(recipient)
    except (OSError, AttributeError):
        return None
    return float(sent) if _is_number(sent) else None


def repeat_key(kind, subject, to):
    return "%s|%s|%s" % (kind, subject, to)


def repeat_held(state_dir, to, kind, subject, state, now, first=False, answered_at=None, window=None):
    """True when a reminder of this (kind, subject, state) for `to` is a repeat to hold back. Writes nothing.
    A first sight, a changed state and a `first` report are never held (an owner wait, a held PR and the FIRST report of a dead or blocked agent
    are not repeats). An unchanged state is held for `window` seconds (6 hours) after it was delivered. ANSWERED means `answered_at`: the
    recipient's own send to the item's subject since the finding; it silences an unchanged state for `window` after the answer, never longer.
    A clock that went backwards (now before the record) is no reason to hold: it is due."""
    window = REPEAT_WINDOW if window is None else window
    before = _repeat_read(state_dir).get(repeat_key(kind, subject, to))
    if first or not (isinstance(before, dict) and before.get("state") == state and _is_number(before.get("at"))):
        return False
    if now < before["at"]:
        return False
    if answered_at is not None and answered_at >= before["at"] and now - answered_at < window:
        return True
    return now - before["at"] < window


def repeat_record(state_dir, to, kind, subject, state, now, window=None):
    """Remember that this (kind, subject, state) was delivered at `now`; forgets the keys older than twice the window."""
    window = REPEAT_WINDOW if window is None else window
    with reminders_lock(state_dir):
        known = _repeat_read(state_dir)
        known[repeat_key(kind, subject, to)] = {"state": state, "at": float(now)}
        known = {k: v for k, v in known.items() if isinstance(v, dict) and _is_number(v.get("at")) and not now - v["at"] > 2 * window}
        path = Path(state_dir) / REPEAT_FILE
        temp = path.with_name("%s.%d.tmp" % (REPEAT_FILE, os.getpid()))
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(known, sort_keys=True))
            os.replace(str(temp), str(path))
        except BaseException:
            try:
                os.unlink(str(temp))
            except OSError:
                pass
            raise


def add_repeat_reminder(state_dir, to, at, created, text, kind, subject, state, first=False, answered_at=None, **more):
    """add_reminder behind the repeat rule: returns the item, or None when it is a repeat held back. The repeat is recorded only AFTER the
    reminder was stored: a failed add leaves nothing recorded, so the next tick delivers it."""
    if repeat_held(state_dir, to, kind, subject, state, at, first=first, answered_at=answered_at):
        return None
    item = add_reminder(state_dir, to, at, created, text, **more)
    repeat_record(state_dir, to, kind, subject, state, at)
    return item


# The table of every finding kind sent to the Chief of Staff, by the function that sends it:
# function -> (kind, repeat key, window, test). docs/switchboard.md carries the same table; a test derives the senders from the source.
FINDING_TABLE = {
    "blocked_tick": ("Blocked agent", "agent, recipient, event, held, refused, kind, reason", "6 hours; first report and end line at once", "test_tell_holds_a_repeat_and_passes_the_first_report"),
    "undelivered_wake_tick": ("Undelivered wake", "agent, incident (own state)", "once per incident", "test_the_chief_of_staff_is_told_once_when_the_resume_did_not_deliver"),
    "load_tick": ("load alert", "lane, severity and reasons", "6 hours; merged into the wake message", "test_load_alert_is_keyed_by_the_data_not_the_text"),
    "friction_tick": ("friction wake", "slot (own state)", "per slot", "test_one_wake_per_orchestrator_per_day_not_for_cos_or_coders"),
    "cmd_budget_notice": ("budget notice", "account (own claim)", "1 hour", "test_s2_the_top_spenders_orchestrator_gets_one_message_with_the_agent_and_the_numbers_and_a_second_call_sends_none"),
    "_impact_matrix_episode": ("impact matrix unreadable", "episode (own state)", "per episode", "test_s6_an_unreadable_matrix_changes_nothing_and_says_so_once_for_the_episode"),
    "_impact_post_list": ("owner-hold rule list", "rule version (own state)", "per rule", "test_s8_an_unreadable_decision_or_rule_or_file_list_is_refused_and_nothing_is_lifted"),
    "cmd_remind": ("a reminder the agent set (`remind`)", "its own id (a request, not a finding)", "none: sent as asked", "test_a_reminder_is_a_fact_only_once_it_is_due"),
    "set_checkpoint_reminders": ("checkpoint reminder", "launch (own id)", "once per launch", "test_a_launch_checkpoint_reminder_is_not_a_wait_and_a_workers_own_reminder_is"),
    "notify_drop": ("reminder drop notice", "dropped reminder (removed first)", "once", "test_a_drop_sends_the_setter_one_notice_naming_text_and_reason_audited_once"),
    "comment_window_alert": ("comment window not read to its start alert", "repo, comments, day (own state, open-list-alert.json)", "once a day per repository", "test_s4_a_window_not_read_to_its_start_is_said_once_a_day"),
    "open_list_alert": ("open-item list cut alert", "repo and day (own state, open-list-alert.json)", "once a day per repository", "test_s4_one_alert_a_day_and_one_more_the_next_day"),
    "SweepFacts": ("sweep (a fact, merged per wake)", "set and slot (own key)", "re-alarm hours", "test_s4_three_finding_kinds_of_one_wake_are_one_queued_message_with_three_lines"),
    "TriageFacts": ("triage (a fact, merged per wake)", "set and slot (own key)", "re-alarm hours", "test_s4_three_finding_kinds_of_one_wake_are_one_queued_message_with_three_lines"),
}
# Every state file the gate reads or writes: file -> (who reads it, who writes it)
STATE_FILE_TABLE = {
    REPEAT_FILE: ("repeat_held", "repeat_record"),
    LAST_REPORT_FILE: ("last_send_at", "report and send"),
}
# The sweep, triage and load-alert items of one wake are ONE message (merge_wake_facts); these are the classes it merges
WAKE_KINDS = ("sweep", "triage")
WAKE_LOAD = "load"

_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _clean_line(line):
    return " ".join(_ESCAPES.sub("", str(line)).split())


def merge_wake(items):
    """One message per wake: `items` is [(class, line)]; one line per item, one header with the count. Escape sequences are stripped."""
    lines = ["- %s: %s" % (_clean_line(cls), _clean_line(line)) for cls, line in items]
    return "%d item%s this wake\n%s" % (len(lines), "" if len(lines) == 1 else "s", "\n".join(lines))


def wake_class_of(fact):
    return fact.kind if fact.kind in WAKE_KINDS else getattr(fact, "wake_class", None)


WAKE_MERGE_BUDGET = 3000   # characters of merged text: the cap (4000) less compose's header, group lines and the "and N more" line


def merge_wake_facts(recipient, facts, budget=None):
    """The facts of ONE wake for `recipient` with the sweep, triage and load-alert items folded into one fact (one line per item, newest
    first). Only as many items as FIT in `budget` (header included) are merged; the REST stay separate pending facts for the next message, so
    every finding is delivered over successive messages and none is lost. Everything else (an owner wait, a held PR, a blocked agent's report,
    a send) is passed through untouched. The merged fact keeps `members` (keys) and `member_lines`; deliver acknowledges only the carried ones."""
    budget = WAKE_MERGE_BUDGET if budget is None else budget
    mine = [f for f in facts if wake_class_of(f)]
    if len(mine) < 2:
        return list(facts)
    mine.sort(key=lambda f: (-f.created, f.key))
    take = 0
    for count in range(1, len(mine) + 1):
        if len(merge_wake([(wake_class_of(f), f.summary) for f in mine[:count]])) > budget:
            break
        take = count
    if take < 2:
        return list(facts)
    group, rest = mine[:take], mine[take:]
    keys = [f.key for f in group]
    merged = Fact("wake:" + hashlib.sha256("|".join(keys).encode()).hexdigest()[:12], recipient, "wake",
                  merge_wake([(wake_class_of(f), f.summary) for f in group]), max(f.created for f in group),
                  batch=all(f.batch for f in group), sender="switchboard")
    merged.members = keys
    merged.sent_min = min(f.created for f in group)   # #1402: the oldest member, so an old digest does not look new
    merged.member_lines = {f.key: "- %s: %s" % (_clean_line(wake_class_of(f)), _clean_line(f.summary)) for f in group}
    return [f for f in facts if not wake_class_of(f)] + rest + [merged]


DELAYED_MARK_SECONDS = 1800   # #1468: a reminder copy delivered later than this after it fired is marked "delayed: sent HH:MMZ"


def delayed_text(text, sent_at):
    """The reminder text with the first line "delayed: sent HH:MMZ" (UTC)."""
    return "delayed: sent %sZ\n%s" % (time.strftime("%H:%M", time.gmtime(sent_at)), text)


def merge_reminder_copies(facts):
    """#1468: queued reminder copies with the same sender, recipient and text (keyed or not) become ONE fact with the count and the newest
    sent time. The merged fact carries every copy's key in `members`, so all are acknowledged. Any other fact passes through untouched."""
    groups = {}
    for fact in facts:
        if fact.kind == "reminder" and not getattr(fact, "wake_class", None) and hasattr(fact, "reminder_text"):
            groups.setdefault((fact.sender, fact.recipient, fact.reminder_text), []).append(fact)
    merged_for = {}
    for (sender, recipient, text), group in groups.items():
        if len(group) < 2:
            continue
        newest = max(group, key=lambda f: (f.created, f.key))
        body = "%s (%d copies, newest sent %sZ)" % (text, len(group), time.strftime("%H:%M", time.gmtime(newest.created)))
        if getattr(newest, "delayed", False):
            body = delayed_text(body, newest.created)
        keys = sorted(f.key for f in group)
        merged = Fact("reminder-merge:" + hashlib.sha256("|".join(keys).encode()).hexdigest()[:12], recipient, "reminder", body,
                      newest.created, issue=newest.issue, batch=False, sender=sender, repo=newest.repo)
        merged.members = keys
        for f in group:
            merged_for[id(f)] = merged
    out, seen = [], set()
    for fact in facts:
        merged = merged_for.get(id(fact))
        if merged is None:
            out.append(fact)
        elif id(merged) not in seen:
            seen.add(id(merged))
            out.append(merged)
    return out


# ---- #1402: what is dropped at delivery
DIGEST_REPLACE_AFTER = 6 * 3600      # seconds: a digest older than this is replaced by the current one
MAX_PRESENT_READS = 10               # present-state reads per delivery (one per subject); the rest are delivered unverified
DIGEST_KEY_RE = re.compile(r"^triage:[0-9a-f]{10}:[0-9]+$")
PULL_ALERT_RE = re.compile(r"^red-ci:pr(?:-unowned)?:([^#:\s]+/[^#:\s]+)#([0-9]+):([^:]*):")
ISSUE_ALERT_RE = re.compile(r"^alert:([^#:\s]+/[^#:\s]+)#([0-9]+) (.+):[^:]+$")
STANDING_ALERT_RE = re.compile(r"^alert-standing:([^#:\s]+/[^#:\s]+)#([0-9]+):")
UNVERIFIED_MARK = "[unverified: its present state could not be read] "


def is_digest(fact):
    return fact.kind == "triage" and bool(DIGEST_KEY_RE.match(fact.key))


def alert_subject(fact):
    """(kind, repo, number, expected) of what an alert says is true, or None when it names no subject:
    ("pull", repo, n, head) a red check on that head of an open pull request; ("issue", repo, n, label) an open issue carrying that label;
    ("standing", repo, n, None) an issue with no standing line yet. One table: every member has a reader in `alert_stale`."""
    if fact.kind != "alert":
        return None
    found = PULL_ALERT_RE.match(fact.key)
    if found:
        return ("pull", found.group(1), int(found.group(2)), found.group(3))
    found = STANDING_ALERT_RE.match(fact.key)
    if found:
        return ("standing", found.group(1), int(found.group(2)), None)
    found = ISSUE_ALERT_RE.match(fact.key)
    if found and fact.issue == int(found.group(2)):
        label = found.group(3).split(" ", 1)[1] if " " in found.group(3) else None
        return ("issue", found.group(1), int(found.group(2)), label)
    return None


SUBJECT_ENDPOINT = {"pull": "pulls", "issue": "issues", "standing": "issues"}   # the present record each subject kind is read from


def alert_stale(subject, record, fact, standing):
    """Why the alert is no longer true against its present record, or "" when it still is. Pure."""
    kind, _repo, _number, expected = subject
    if kind == "pull":
        sha = ((record.get("head") or {}).get("sha")) if isinstance(record.get("head"), dict) else None
        if record.get("merged") or record.get("merged_at"):
            return "the pull request is merged"
        if record.get("state") != "open":
            return "the pull request is closed"
        if isinstance(sha, str) and expected and sha.lower() != expected.lower():
            return "the head moved on to %s" % sha[:12]
        return ""
    if record.get("state") == "closed":
        return "the issue is closed"
    if kind == "issue":
        names = [l.get("name") if isinstance(l, dict) else l for l in record.get("labels") or []]
        if expected and "labels" in record and expected not in names:
            return "the label %s is gone" % expected
    if kind == "standing" and standing(record.get("body"), fact.created) is not None:
        return "a standing line is already posted"
    return ""


def digest_replaced(facts, delivered):
    """{key: the current digest} for each digest older than DIGEST_REPLACE_AFTER that a newer digest for the same recipient replaces."""
    newest = {}
    for fact in facts:
        if is_digest(fact):
            top = newest.get(fact.recipient)
            if top is None or (fact.created, fact.key) > (top.created, top.key):
                newest[fact.recipient] = fact
    return {f.key: newest[f.recipient] for f in facts
            if is_digest(f) and newest[f.recipient] is not f and delivered - f.created > DIGEST_REPLACE_AFTER}


def judge_delivery(facts, delivered, read, standing):
    """(kept, dropped [(fact, kind, why)]) for the facts about to be delivered. `read(subject)` is the present record of an alert's subject, None when
    unreadable (it is called at most once per subject by the caller's cache). A read that fails keeps the alert, marked unverified: never a silent drop."""
    kept, dropped, reads = [], [], 0
    replaced = {} if fault("keep-old-digest") else digest_replaced(facts, delivered)
    for fact in facts:
        if fact.key in replaced:
            dropped.append((fact, "digest", "replaced by the current digest %s" % replaced[fact.key].key))
            continue
        subject = alert_subject(fact) if read is not None else None
        if subject is None:
            kept.append(fact)
            continue
        try:
            record = read(subject)
        except Exception:
            record = None
        if not isinstance(record, dict):
            kept.append(Fact(fact.key, fact.recipient, fact.kind, UNVERIFIED_MARK + fact.summary, fact.created, issue=fact.issue, batch=fact.batch,
                             sender=fact.sender, rider=fact.rider, repo=fact.repo))
            continue
        why = alert_stale(subject, record, fact, standing)
        if why and not fault("deliver-stale-alert"):
            dropped.append((fact, "alert", why))
        else:
            kept.append(fact)
    return kept, dropped


def remove_reminders(state_dir, ids):
    """Drop these ids from reminders.json. Returns the ids that were there."""
    with reminders_lock(state_dir):
        items = read_reminders(state_dir)
        gone = [item["id"] for item in items if item["id"] in ids]
        if gone:
            write_reminders(state_dir, [item for item in items if item["id"] not in ids])
    return gone


def cancel_chain(state_dir, sender, key):
    """#1401: drop every WAITING copy (pending or fired, still in the file) of the chain of this sender and key, in one locked step.
    Returns the ids dropped. Another sender's or another key's reminder, and a sender-less one, are never matched; a delivered copy is not in the file."""
    if not key or not isinstance(sender, str) or not sender.strip():
        return []
    with reminders_lock(state_dir):
        items = read_reminders(state_dir)
        gone = [i["id"] for i in items if i.get("key") == key and i.get("from") == sender]
        if gone:
            write_reminders(state_dir, [i for i in items if i["id"] not in gone])
    return gone


DROP_HOOK = None  # test hook: called between the snapshot read and the locked drop


def drop_superseded_item(state_dir, item_id, kept_id):
    """Remove item_id only if kept_id is STILL in the file: one read-modify-write under the lock."""
    if DROP_HOOK is not None:
        DROP_HOOK()
    with reminders_lock(state_dir):
        items = read_reminders(state_dir)
        if not any(i["id"] == kept_id for i in items) or not any(i["id"] == item_id for i in items):
            return False
        write_reminders(state_dir, [i for i in items if i["id"] != item_id])
    return True


def found_reason(why):
    return "merged" if why.endswith(" is merged") else "closed"


class ReminderFacts:
    """`reminder:<id>` facts for reminders that are due. A reminder not yet due is no fact.

    A due reminder skips the batch window: it is one event at an exact time.
    It stays in the file until its key shows in the recipient's transcript or
    inbox (prune_delivered_reminders), so a crash before delivery loses nothing.
    """

    __name__ = "reminder_facts"

    def __init__(self, state_dir):
        self.state_dir, self.engine = Path(state_dir), None
        self.swept = set()   # #1711: ids dropped at a delivery for a newer copy of the same key and recipient

    def bind(self, engine):
        self.engine = engine

    def due(self):
        now = self.engine.now() if self.engine else time.time()
        return [item for item in read_reminders(self.state_dir) if item["at"] <= now]

    def until_holds(self, condition):
        """A reminder's cancel condition (`until_closed` an issue, `until_merged` a pull request, each `owner/repo#N`):
        the reason it is moot, or None (not met, or cannot be read now: the reminder stays)."""
        for key, kind in (("until_closed", "issue"), ("until_merged", "pr")):
            ref = condition.get(key)
            match = re.fullmatch(r"([\w.-]+/[\w.-]+)#([0-9]+)", ref) if isinstance(ref, str) else None
            if match is None or self.engine is None:
                continue
            try:
                state = read_ref_state(self.engine, match.group(1), int(match.group(2)), kind)
            except (NoticeError, OSError, ValueError):
                continue
            if (key == "until_closed" and state in ("closed", "merged")) or (key == "until_merged" and state == "merged"):
                return "%s is %s" % (ref, state)
        return None

    def last_report(self, worker, boss=None):
        """When `worker` last made a report (to anyone) or sent a message to `boss` (epoch seconds, from `last-reports.json`), or None. A time in the
        future is no sign of life: a damaged or forged record cannot silence the wake for good."""
        try:
            entry = _read_small(self.state_dir, LAST_REPORT_FILE).get(worker, {})
            sends = entry.get("send_to") if isinstance(entry.get("send_to"), dict) else {}
            times = [entry.get("at"), sends.get(boss) if isinstance(boss, str) else None]
        except (OSError, AttributeError):
            return None
        now = self.engine.now() if self.engine else time.time()
        times = [float(t) for t in times if _is_number(t) and t <= now + 60]
        return max(times) if times else None

    def overdue_text(self, item):
        """The wake's text with the fact the boss needs: when the worker last reported, or that it has not since the launch."""
        worker, since = item["condition"].get("unless_report_from"), item["condition"].get("since", 0)
        last = self.last_report(worker, item["to"]) if isinstance(worker, str) else None
        if last is None:
            tail = "no report from %s since launch" % _plain(worker, 60)
        else:
            tail = "last report from %s %s ago%s" % (_plain(worker, 60), _age_text(max(0, int(self.engine.now() - last))), "" if not _is_number(since) or last >= since else " (before this launch)")
        return "%s (%s)" % (item["text"], tail)

    def verdict(self, item):
        """For a due conditional reminder: None (cannot tell yet: it stays), "deliver", or the reason to drop it.
        One read of the boss's transcript and inbox, and only for a reminder that is due and has a condition."""
        condition, discovery = item["condition"], self.engine.discovery if self.engine else None
        moot = self.until_holds(condition)
        if moot:
            return moot  # a cancel condition holds: dropped (audited by the caller)
        if discovery is None:
            return None
        worker = condition.get("unless_report_from") or condition.get("while_running")
        if isinstance(worker, str) and worker not in discovery.agents:
            return "%s is gone" % worker if not discovery.problems else None
        if not isinstance(condition.get("unless_report_from"), str):
            return "deliver"
        boss, since = discovery.agents.get(item["to"]), condition.get("since", 0)
        if not _is_number(since):  # a forged or damaged condition is no condition: deliver, audited
            if self.engine is not None and self.engine.audit is not None:
                self.engine.audit.append(self.engine.actor, "reminder.condition_unusable", item["to"],
                                         {"id": item["id"]})
            return "deliver"
        last = self.last_report(worker, item["to"])
        if last is not None and last >= since:   # the durable record `report` writes for EVERY report (a rider, a direct one, to any boss): it needs no read of the boss's transcript, which v2_light never makes and a delivered rider no longer shows
            return "%s reported to its boss since the launch (%s ago)" % (worker, _age_text(max(0, int(self.engine.now() - last))))
        if boss is None:
            return None
        try:
            lines = synthetic_lines(self.engine, boss.session_id, since, waiting=True)
            riders = read_report_riders(self.state_dir)
        except (V2Error, ReminderError):
            return None
        for _, _, _, line in lines:
            match = REPORT_LINE.match(line) or SEND_LINE.match(line)
            if match and match.group(1) == worker:
                return "%s reported to %s since the launch" % (worker, item["to"])
        if any(r.get("from") == worker and r["to"] == item["to"] and r["created"] >= since for r in riders):
            return "%s reported to %s since the launch" % (worker, item["to"])
        return "deliver"

    def says_again(self, item_id, reason, now):
        """#1728: True when `reminder.waiting` is to be written for this reminder now: the first time, a changed reason, or every WAITING_REPEAT seconds."""
        said = self.__dict__.setdefault("_said", {})
        before = said.get(item_id)
        if before is not None and before[0] == reason and 0 <= now - before[1] < WAITING_REPEAT:
            return False
        if len(said) > 1000:
            said.clear()
        said[item_id] = (reason, now)
        return True

    def waiting(self, item_id, recipient, due, reason):
        """#1728: one `reminder.waiting` audit line (id, recipient, minutes past due, a one-line reason) for a due reminder not delivered in this pass."""
        if self.engine is None or self.engine.audit is None:
            return
        now, reason = self.engine.now(), _one_line(reason, 160)
        if self.says_again(item_id, reason, now):
            self.engine.audit.append(self.engine.actor, "reminder.waiting", recipient,
                                     {"id": item_id, "late_min": max(0, int((now - due) // 60)), "reason": reason})

    def ceiling(self, plan, now):
        """#1728: the ceiling step of one plan (see `ceiling_wake`)."""
        return ceiling_wake(self.engine, self, plan, now)

    def notify_drop(self, item, why):
        """#1132: a reminder dropped because its issue closed or its pull request merged tells its setter ONCE. Called only by the caller
        that removed the reminder from the file (`gone` was non-empty), so a second sweep finds nothing to drop and sends nothing."""
        sender = item.get("from")
        match = re.search(r" is (closed|merged)$", why)
        if match is None or not isinstance(sender, str) or not sender.strip() or self.engine is None:
            return
        reason = "the pull request merged" if match.group(1) == "merged" else "the issue closed"
        text = "Reminder %s for %s was dropped, unsent: %s (%s). Its text: %s. Set it with --keep-if-closed to keep it." % (
            item["id"], item["to"], reason, why, item["text"])
        add_reminder(self.state_dir, sender, self.engine.now(), self.engine.now(), text)
        if self.engine.audit is not None:
            self.engine.audit.append(self.engine.actor, "reminder.drop_notice", sender, {"id": item["id"], "why": found_reason(why)})

    def superseded(self, item, items):
        """The newer item of the same owner and key that is DUE or fired, if any (only keyed items with a string sender).
        A newer item still in the future never supersedes: the older one is delivered (late note) so the lane is not left with nothing."""
        sender = item.get("from")
        if not item.get("key") or not isinstance(sender, str) or not sender.strip():
            return None
        now = self.engine.now() if self.engine else time.time()
        newer = [i for i in items if i["id"] != item["id"] and i.get("key") == item["key"] and i.get("from") == item.get("from")
                 and i["at"] <= now and (i.get("created", 0), i["at"]) > (item.get("created", 0), item["at"])]
        return max(newer, key=lambda i: (i.get("created", 0), i["at"])) if newer else None

    def newest_first(self, facts):
        """The reminder facts in the order of their copies' (created, at), newest first, so a delivery sweeps the older copies before it asks about them.
        It has no side effect: `swept` is cleared where a pass starts (`__call__`), not per delivery call."""
        try:
            marks = {"reminder:" + i["id"]: (i.get("created", 0), i["at"]) for i in read_reminders(self.state_dir)}
        except ReminderError:
            return list(facts)   # the recheck itself reads the file again and defers the delivery
        return sorted(facts, key=lambda f: marks.get(str(f.key), (0, 0)), reverse=True)

    def drop_older_copies(self, item, items):
        """#1711: at the delivery of a keyed, unconditional copy, every OTHER older copy of the same key and recipient still in the file
        (whatever its sender, also none) is dropped, one audit line each. Called under the lock with `items` just read. An unkeyed copy,
        another key, another recipient and a copy already delivered (not in the file) are never touched."""
        if not item.get("key") or item.get("condition"):
            return []
        mark = (item.get("created", 0), item["at"])
        older = [i for i in items if i["id"] != item["id"] and i.get("key") == item["key"] and i["to"] == item["to"]
                 and (i.get("created", 0), i["at"]) < mark]
        if not older:
            return []
        gone = {i["id"] for i in older}
        self.swept |= gone
        write_reminders(self.state_dir, [i for i in items if i["id"] not in gone])
        if self.engine is not None and self.engine.audit is not None:
            for i in older:
                self.engine.audit.append(self.engine.actor, "reminder.drop_superseded", i["to"],
                                         {"dropped": i["id"], "kept": item["id"], "key": _one_line(item["key"], 60), "by": "delivered"})
        return sorted(gone)

    def recheck_delivery(self, fact):
        """#1401: the delivery step asks once more, under the lock, whether a reminder fact is still wanted. Returns None (deliver it) or the
        id of the newer copy that replaces it, or "cancelled" when it is no longer in the file; a dropped superseded copy is removed here."""
        if not str(getattr(fact, "key", "")).startswith("reminder:"):
            return None
        item_id = fact.key[len("reminder:"):]
        with reminders_lock(self.state_dir):
            items = read_reminders(self.state_dir)
            item = next((i for i in items if i["id"] == item_id), None)
            if item is None:
                return "swept" if item_id in self.swept else "cancelled"
            kept = self.superseded(item, items)
            if kept is None or kept.get("condition"):   # a gated newer copy may not be delivered: the older one is not dropped for it
                self.drop_older_copies(item, items)
                return None
            write_reminders(self.state_dir, [i for i in items if i["id"] != item_id])
        return kept["id"]

    def __call__(self):
        self.swept.clear()   # #1711: a pass starts here (each source is read once per pass), so `swept` holds only this pass's ids
        facts = []
        everything = read_reminders(self.state_dir)
        for item in self.due():
            ttl = item.get("ttl")
            if _is_number(ttl) and ttl > 0 and (self.engine.now() if self.engine else time.time()) - item["at"] > ttl:
                if remove_reminders(self.state_dir, {item["id"]}) and self.engine is not None and self.engine.audit is not None:
                    self.engine.audit.append(self.engine.actor, "reminder.drop_ttl", item["to"],
                                             {"id": item["id"], "due": iso(item["at"]), "ttl": int(ttl)})
                continue
            kept = self.superseded(item, everything)
            if kept is not None and kept.get("condition") and self.verdict(kept) != "deliver":
                kept = None   # the newer one is gated or unresolved: it may not be delivered, so deliver the older
            if kept is not None:
                if drop_superseded_item(self.state_dir, item["id"], kept["id"]) and self.engine is not None and self.engine.audit is not None:
                    self.engine.audit.append(self.engine.actor, "reminder.drop_superseded", item["to"],
                                             {"dropped": item["id"], "kept": kept["id"], "key": _one_line(item["key"], 60)})
                continue
            unreadable = False
            if item.get("condition"):
                found = self.verdict(item)
                unreadable = found is None and self.engine is not None and self.engine.now() - item["at"] > reminder_bound(self.engine.config, item)   # #1728 ceiling: a condition that cannot be read does not hold a due reminder past its bound
                if unreadable:
                    found = "deliver"
                    done = self.__dict__.setdefault("_ceiled_unreadable", set())   # once per id, on the ReminderFacts instance like ceiling_wake's _ceiled (never on the shared engine)
                    if len(done) > 1000:
                        done.clear()
                    if self.engine.audit is not None and item["id"] not in done:
                        done.add(item["id"])
                        self.engine.audit.append(self.engine.actor, "reminder.ceiling", item["to"], {"id": item["id"], "why": "condition unreadable"})
                if found is None:
                    self.waiting(item["id"], item["to"], item["at"], "condition unreadable")
                    continue
                if found != "deliver":
                    gone = remove_reminders(self.state_dir, {item["id"]})
                    if gone and self.engine is not None and self.engine.audit is not None:
                        self.engine.audit.append(self.engine.actor, "reminder.drop", item["to"],
                                                 {"id": item["id"], "why": found})
                    if gone:
                        self.notify_drop(item, found)
                    continue
            text = self.overdue_text(item) if isinstance(item.get("condition"), dict) and isinstance(item["condition"].get("unless_report_from"), str) and self.engine else item["text"]
            late = (self.engine.now() if self.engine else time.time()) - item["at"]
            raw_text = text
            if late > DELAYED_MARK_SECONDS:
                text = delayed_text(text, item["at"])
            elif late >= LATE_NOTE_SECONDS:
                note = " (late by %d min)" % int(late // 60)
                text = text + note   # #1355: never cut here: an over-cap text is carried in parts by compose, the note ends the last part
            if unreadable:
                text = text + " (condition unreadable)"
            facts.append(Fact("reminder:" + item["id"], item["to"], "reminder", text, item["at"],
                              issue=item.get("issue"), batch=False, sender=item.get("from"), repo=item.get("repo")))
            facts[-1].reminder_text = raw_text
            facts[-1].delayed = late > DELAYED_MARK_SECONDS
            facts[-1].bound = reminder_bound(self.engine.config if self.engine else {}, item)   # #1728: read by steers_into_turn
            if isinstance(item.get("wake_class"), str):
                facts[-1].wake_class = item["wake_class"]
        return facts


WAITING_REPEAT = 1800   # #1728: seconds between two `reminder.waiting` lines for the same reminder and reason


def waiting_reason(engine, plan, now):
    """#1728: why the reminders of this plan do not reach the lane now, or None (a wake that goes in at once, or a steer)."""
    if plan.action != "wake":
        return "%s: %s" % (plan.action, plan.reason)
    observation = plan.observation
    if observation is None or observation.status != "working":
        return None
    age = None
    try:
        if engine.config.get("steer_after_minutes") and engine.state_dir is not None:
            age = load_module("turn_start").age(engine.state_dir, observation.session_id, now, load_module("record_json").parse_bounded)
    except Exception:
        age = None
    if steers_into_turn(engine.config, observation, plan.pending, getattr(engine, "discovery", None), age, now):
        return None
    return "lane in a turn of %d minutes, queued" % (age // 60) if age is not None else "lane in a turn of unknown length, queued"


def reminder_bound(config, item):
    """#1728: the bound in SECONDS of a due reminder: `checkpoint_bound_minutes` (default 5) for one a launch checkpoint set (its condition names
    the worker, or the overdue one to its boss), else `reminder_bound_minutes` (default 10)."""
    condition = item.get("condition") if isinstance(item.get("condition"), dict) else {}
    checkpoint = "while_running" in condition or "unless_report_from" in condition
    key, default = ("checkpoint_bound_minutes", 5) if checkpoint else ("reminder_bound_minutes", 10)
    minutes = config.get(key, default)
    return (minutes if _is_number(minutes) and minutes >= 0 else default) * 60


def late_reminder_counts(state_dir, config, now):
    """#1728: {lane: reminders delivered LATER than their bound in the last 24 hours}, from the audit lines. Each `reminder.add` (id, `at`, recipient)
    is paired with the FIRST `v2.synthetic` line whose keys hold `reminder:<id>`; delivery minus `at` is compared with `reminder_bound`."""
    from datetime import datetime
    parse = load_module("record_json").parse_bounded
    def epoch(text):
        try:
            return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    added, delivered, counts = {}, {}, {}
    try:
        cap = 8 * 1024 * 1024   # only a bounded TAIL of the audit is ever read
        with open(str(state_dir) + "/audit.jsonl", "rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - cap))
            data = handle.read(cap)
            if size > cap:
                data = data.partition(b"\n")[2]   # drop the first partial line
        lines = data.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return counts
    for line in lines:
        try:
            entry = parse(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        detail = entry.get("detail") if isinstance(entry, dict) and isinstance(entry.get("detail"), dict) else {}
        if entry.get("event") == "reminder.add" and detail.get("id") is not None:
            added[str(detail["id"])] = (entry.get("subject"), epoch(detail.get("at")), {"condition": {"unless_report_from": 1} if detail.get("for") else {}})
        elif entry.get("event") == "v2.synthetic":
            for key in detail.get("keys") or []:
                if isinstance(key, str) and key.startswith("reminder:"):
                    delivered.setdefault(key[len("reminder:"):], epoch(entry.get("ts")))
    for item_id, (lane, due, item) in added.items():
        counts.setdefault(lane, 0)
        sent = delivered.get(item_id)
        if due is not None and sent is not None and sent >= now - 86400 and sent - due > reminder_bound(config, item):
            counts[lane] += 1
    return counts


def late_reminder_lines(state_dir, config, now):
    """One `status` line per lane: how many reminders came later than their bound in the last 24 hours."""
    return ["      reminders  %s: %d later than the bound in 24h" % (lane, n) for lane, n in sorted(late_reminder_counts(state_dir, config, now).items())]


def ceiling_wake(engine, source, plan, now):
    """#1728 ceiling: a due reminder of this lane past its bound that this pass would not admit (a batch window) turns the plan into a queued wake,
    carrying every pending fact of the lane, with one `reminder.ceiling` audit line per reminder. A wake already planned (queued behind a turn) is
    delivered by this very pass: only the line is written. A held lane is left alone. Returns the ids written."""
    if plan.action not in ("wait", "wake"):
        return []
    items = {i["id"]: i for i in read_reminders(source.state_dir)}
    late = [(f, items[f.key[len("reminder:"):]]) for f in plan.pending
            if str(f.key).startswith("reminder:") and f.key[len("reminder:"):] in items]
    over = [(f, i) for f, i in late if now - i["at"] > reminder_bound(engine.config, i)]
    done = source.__dict__.setdefault("_ceiled", set())
    over = [(f, i) for f, i in over if i["id"] not in done]
    if not over:
        return []
    if plan.action == "wait":
        for fact in plan.pending:
            fact.batch = False
        plan.action, plan.reason = "wake", "ceiling: a due reminder is past its bound"
    for _, item in over:
        if len(done) > 1000:
            done.clear()
        done.add(item["id"])
        if engine.audit is not None:
            engine.audit.append(engine.actor, "reminder.ceiling", plan.recipient,
                                {"id": item["id"], "late_min": max(0, int((now - item["at"]) // 60)),
                                 "bound_min": int(reminder_bound(engine.config, item) // 60), "delivery": "queued wake"})
    return [i["id"] for _, i in over]


def invariant_break(source, plan, now):
    """#1728 invariant: the unconditional, unexpired due reminders of this lane that are not in its pending facts (an empty list: it holds)."""
    have = {f.key for f in plan.pending}
    return [i for i in source.due() if i["to"] == plan.recipient and not i.get("condition") and not i.get("key") and not i.get("ttl")
            and "reminder:" + i["id"] not in have]


def reminder_waiting(engine, plan, now):
    """#1728: the audit line for each due reminder in `plan` that is not delivered in this pass. Never raises: it must not stop a delivery."""
    try:
        source = next((s for s in engine.sources if isinstance(s, ReminderFacts)), None)
        mine = [f for f in plan.pending if str(f.key).startswith("reminder:")]
        reason = waiting_reason(engine, plan, now) if source is not None and mine else None
        for fact in mine if reason else ():
            source.waiting(fact.key[len("reminder:"):], plan.recipient, fact.created, reason)
        if source is not None and plan.action != "hold":
            for item in invariant_break(source, plan, now):
                source.waiting(item["id"], plan.recipient, item["at"], "invariant broken: a due reminder of a lane neither held nor cancelled is not a pending fact")
            source.ceiling(plan, now)
    except Exception:
        pass


def prune_delivered_reminders(engine, discovery):
    """Forget the due reminders whose key is already in their recipient's transcript or inbox."""
    source = next((s for s in engine.sources if isinstance(s, ReminderFacts)), None)
    if source is None:
        return []
    by_recipient = {}
    for item in source.due():
        by_recipient.setdefault(item["to"], []).append(item)
    delivered = []
    for name, items in sorted(by_recipient.items()):
        observation = discovery.agents.get(name)
        if observation is None:
            continue
        try:
            keys, _ = engine.delivered_keys(observation.session_id, min(i["at"] for i in items) - SINCE_SLACK)
        except V2Error:
            continue
        delivered += [item["id"] for item in items if "reminder:" + item["id"] in keys]
    gone = remove_reminders(source.state_dir, set(delivered)) if delivered else []
    if gone and engine.audit is not None:
        engine.audit.append(engine.actor, "reminder.prune", None, {"ids": gone})
    return gone


# ---- checkpoint reminders: a launch sets them from the brief's CHECKPOINT (docs/switchboard.md, "Self-wakes")

CHECKPOINT_WORKER_TEXT = "checkpoint due: report now, one line"
CHECKPOINT_OVERDUE_TEXT = "%s has not reported since launch: overdue checkpoint"
_CP_UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|[smh])?"
_CP_UNITS = {"s": 1, "m": 60, "h": 3600}
CHECKPOINT_MAX_SECONDS = 30 * 86400  # a checkpoint or overdue further out than this is refused, never an error
CHECKPOINT_MAX_DIGITS = 6


def how_long(seconds):
    """A length of time as words, exact to the second: `15 min`, `1 min 30 s`, `2 h 5 min`, `1 d 2 h`. Never rounds: the line a lane reads must be the stored time."""
    seconds = int(round(abs(seconds)))
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60), ("s", 1)):
        count, seconds = divmod(seconds, size)
        if count:
            parts.append("%d %s" % (count, unit))
    return " ".join(parts) if parts else "0 s"


def reminder_when(at, now):
    """The words the three routes print for one reminder, built from the STORED time and the same `now`: `in 15 min, at 2030-01-02T15:05:05Z` (or `15 min ago`)."""
    return ("in %s" % how_long(at - now) if at >= now else "%s ago" % how_long(now - at)) + ", at %s" % iso(at)


def _print_when_now(now):
    try:
        return iso(now)
    except Exception:
        return str(now)


def _print_when(at, now):
    """`reminder_when`, but the printing never fails a command: a reminder that was made still exits 0 (the plain clock time is the fallback)."""
    try:
        return reminder_when(at, now)
    except Exception:
        return "at %s" % at


def _cp_when(text, moment):
    """parse_when for a checkpoint flag, bounded: too many digits or further than CHECKPOINT_MAX_SECONDS is refused."""
    if re.search(r"[0-9]{%d}" % (CHECKPOINT_MAX_DIGITS + 1), text):
        raise ReminderError("%r is too large; a checkpoint is at most 30 days out" % text[:40])
    at = parse_when(text, moment)
    if not moment - CHECKPOINT_MAX_SECONDS <= at <= moment + CHECKPOINT_MAX_SECONDS:
        raise ReminderError("%r is too far out; a checkpoint is at most 30 days away" % text[:40])
    return at


def _cp_seconds(number, unit, default):
    unit = (unit or default or "")[:1].lower()
    return int(number) * _CP_UNITS[unit] if unit in _CP_UNITS else None


def parse_checkpoint(brief):
    """(checkpoint seconds, overdue seconds) from the brief's CHECKPOINT line, or None. Best effort and never an
    error: 'Report at 30 minutes ...; overdue at 60', 'in 20m, overdue 45m'. A bare overdue number takes the
    unit of the checkpoint; no unit anywhere, a missing part, or an overdue that is not later = None."""
    line = next((l for l in (brief or "").splitlines() if re.match(r"\s*CHECKPOINT\s*:", l, re.I)), None)
    if line is None:
        return None
    over = re.search(r"\boverdue(?:\s+(?:at|after|in|by))?\s+(\d{1,6})\s*" + _CP_UNIT + r"\b", line, re.I)
    if over is None:
        return None
    head = line[:over.start()]
    first = re.search(r"\b(?:at|in|after|within)\s+(\d{1,6})\s*" + _CP_UNIT + r"\b", head, re.I)
    if first is None:
        return None
    checkpoint = _cp_seconds(first.group(1), first.group(2), over.group(2))
    overdue = _cp_seconds(over.group(1), over.group(2), first.group(2))
    if checkpoint is None or overdue is None or checkpoint <= 0 or overdue <= checkpoint:
        return None
    if overdue > CHECKPOINT_MAX_SECONDS:
        return None
    return checkpoint, overdue


def set_checkpoint_reminders(engine, state, name, boss, issue, repo, brief, checkpoint, overdue, launched, moment):
    """Make the launch's two reminders. `checkpoint`/`overdue` are --checkpoint/--overdue (delays or ISO times) or
    None; with neither, the brief's CHECKPOINT line is parsed. Returns one line for the launch output, or None.
    Never raises: the worker is already running."""
    try:
        notes = []
        if boss:  # #102: the overdue wake goes to the registered agent the role name chief-of-staff stands for
            try:
                boss = resolve_boss(engine.discover(engine.now()), boss)
            except Exception:
                pass  # the fleet cannot be read now: the name as given
        if checkpoint is None and overdue is None:
            parsed = parse_checkpoint(brief)
            if parsed is None:
                return ("no checkpoint reminders: no --checkpoint/--overdue and the brief's CHECKPOINT line could "
                        "not be read as 'at <N> minutes ... overdue at <M>'")
            checkpoint, overdue = ("%ds" % parsed[0], "%ds" % parsed[1])
        when_worker = _cp_when(checkpoint, moment) if checkpoint is not None else None
        when_boss = _cp_when(overdue, moment) if overdue is not None and boss else None  # both checked before either is made
        made = []
        if when_worker is not None:
            item = add_reminder(state, name, when_worker, moment, CHECKPOINT_WORKER_TEXT, issue, boss, repo,
                                condition={"while_running": name})
            made.append(("worker", item))
        if overdue is not None:
            if not boss:
                notes.append("no overdue reminder: the launch names no --reports-to")
            else:
                item = add_reminder(state, boss, when_boss, moment, CHECKPOINT_OVERDUE_TEXT % name, issue, name, repo,
                                    condition={"unless_report_from": name, "since": launched})
                made.append(("overdue", item))
        for which, item in made:
            engine.audit.append("cli", "reminder.add", item["to"], {"id": item["id"], "at": iso(item["at"]),
                                                                   "from": item.get("from"), "issue": issue,
                                                                   "repo": repo, "for": "checkpoint-" + which})
        notes[:0] = ["%s reminder %s" % (which, _print_when(item["at"], moment)) for which, item in made]
        return "checkpoint reminders (now %s): " % _print_when_now(moment) + "; ".join(notes) if notes else None
    except (ReminderError, OSError, ValueError, OverflowError) as err:
        return "no checkpoint reminders: %s" % _one_line(str(err), 200)


REMIND_LIST_MAX = 50   # reminders `remind --list` prints


def remind_manage(args, engine, state, sender, moment):
    """`remind --list` (the caller's own pending reminders: set by it or for it) and `remind --cancel <id>` (one of those, by id). Nothing else is
    shown or touched: another agent's reminder is not listed and is refused with the same words as an unknown id."""
    chain_key = getattr(args, "cancel_key", None)
    if args.list_mine and args.cancel:
        sys.stderr.write("%s: remind: give --list or --cancel, not both\n" % PROG)
        return EXIT_USAGE
    if chain_key and (args.list_mine or args.cancel):
        sys.stderr.write("%s: remind: give --cancel-key on its own\n" % PROG)
        return EXIT_USAGE
    if args.name or args.when or args.text:
        sys.stderr.write("%s: remind: --list and --cancel take no recipient, time or text\n" % PROG)
        return EXIT_USAGE
    if chain_key:
        if not (isinstance(sender, str) and sender.strip()):
            sys.stderr.write("%s: remind: --cancel-key needs a known sender\n" % PROG)
            return EXIT_USAGE
        try:
            gone = cancel_chain(state, sender, _one_line(chain_key, 80))
        except ReminderError as err:
            sys.stderr.write("%s: remind: %s\n" % (PROG, _one_line(str(err), 200)))
            return EXIT_ERROR
        if not gone:
            sys.stderr.write("%s: remind: no waiting reminder of yours with key %s\n" % (PROG, _one_line(chain_key, 80)))
            return EXIT_USAGE
        engine.audit.append("cli", "reminder.cancelled", sender, {"key": _one_line(chain_key, 80), "owner": sender, "count": len(gone)})
        print("cancelled %d reminder(s) with key %s" % (len(gone), _one_line(chain_key, 80)))
        return EXIT_OK
    try:
        mine = [item for item in read_reminders(state) if sender in (item.get("to"), item.get("from"))]
    except ReminderError as err:
        sys.stderr.write("%s: remind: %s\n" % (PROG, _one_line(str(err), 200)))
        return EXIT_ERROR
    if args.list_mine:
        mine.sort(key=lambda item: item["at"])
        for item in mine[:REMIND_LIST_MAX]:
            ttl = " ttl %s" % ttl_text(item["ttl"]) if _is_number(item.get("ttl")) and item["ttl"] > 0 else ""
            print("%s  %s  to=%s  issue=%s  key=%s%s  %s" % (item["id"], iso(item["at"]), item["to"], item.get("issue") or "-", _one_line(item.get("key") or "-", 80), ttl, _one_line(item["text"], 120)))
        if len(mine) > REMIND_LIST_MAX:
            print("(%d more not shown)" % (len(mine) - REMIND_LIST_MAX))
        if not mine:
            sys.stderr.write("%s: no pending reminders for %s\n" % (PROG, sender))
        return EXIT_OK
    if args.cancel not in {item["id"] for item in mine}:
        sys.stderr.write("%s: remind: no pending reminder %s of yours (see `%s remind --list`)\n" % (PROG, _one_line(args.cancel, 40), PROG))
        return EXIT_USAGE
    if not remove_reminders(state, {args.cancel}):   # delivered or cancelled between the read and the removal
        sys.stderr.write("%s: remind: reminder %s is already gone\n" % (PROG, _one_line(args.cancel, 40)))
        return EXIT_USAGE
    engine.audit.append("cli", "reminder.cancel", sender, {"id": args.cancel})
    print("cancelled %s" % args.cancel)
    return EXIT_OK


def cmd_remind(args, runner, now):
    engine, state, config = build_engine(runner, now, "cli")
    if args.sender is not None:
        if os.environ.get(LAB_ENV) != "1":
            sys.stderr.write("%s: --from is only for the Lab (%s=1); an agent is identified by its pane\n"
                             % (PROG, LAB_ENV))
            return EXIT_USAGE
        sender = args.sender
    else:
        sender = identify_caller(engine.herdr, engine.v2, os.environ)
    moment = now()
    if args.list_mine or args.cancel or getattr(args, "cancel_key", None):
        return remind_manage(args, engine, state, sender, moment)
    text_file = getattr(args, "text_file", None)   # a caller that builds its own namespace has none
    if text_file and args.text:
        sys.stderr.write("%s: remind takes the text as words or from --text-file, not both; nothing armed\n" % PROG)
        return EXIT_USAGE
    if not args.name or not args.when or not (args.text or text_file):
        sys.stderr.write("%s: remind needs <name> <when> <text> (or --text-file F, or --list, or --cancel <id>)\n" % PROG)
        return EXIT_USAGE
    if text_file:
        text, problem = read_text_file(text_file)
    else:
        text, problem = " ".join(args.text).strip(), None
        if len(text) > TEXT_CAP:   # #1355: never armed to be cut on delivery
            problem = ("the text is %d characters, over the %d-character message cap: nothing armed. "
                       "Put it in a file and use --text-file F, which carries it in full in parts" % (len(text), TEXT_CAP))
    if problem:
        sys.stderr.write("%s: %s\n" % (PROG, problem))
        return EXIT_USAGE
    try:
        repo, number = resolve_for_sender(engine, config, args.issue, sender)
    except IssueRefError as err:
        sys.stderr.write("%s: %s\n" % (PROG, err))
        return EXIT_USAGE
    if args.key and not (isinstance(sender, str) and sender.strip()):
        sys.stderr.write("%s: --key needs a known sender (an unidentified caller would replace other callers' reminders); nothing armed\n" % PROG)
        return EXIT_USAGE
    ttl = None
    if getattr(args, "ttl", None) is not None:
        try:
            ttl = parse_ttl(args.ttl)
        except ReminderError as err:
            sys.stderr.write("%s: remind: %s\n" % (PROG, err))
            return EXIT_USAGE
    condition = {}
    for flag, key in ((args.until_closed, "until_closed"), (args.until_merged, "until_merged")):
        if flag:
            if not re.fullmatch(r"[\w.-]+/[\w.-]+#[0-9]+", flag):
                sys.stderr.write("%s: --until-closed/--until-merged take OWNER/REPO#N, got %r\n" % (PROG, flag[:60]))
                return EXIT_USAGE
            condition[key] = flag
    if number is not None and repo and not condition and not args.keep_if_closed:
        condition["until_closed"] = "%s#%d" % (repo, number)   # a reminder about an issue is moot once it is closed: dropped unsent (`--keep-if-closed` opts out)
    warning = None
    if number is not None and repo and not args.keep_if_closed:
        try:
            ref_state = read_ref_state(engine, repo, number, "issue")
        except Exception:   # the warning never fails a command
            ref_state = "open"   # unreadable: warned as open, never refused
        if ref_state in ("closed", "merged"):
            sys.stderr.write("%s: %s#%d is already closed; a reminder about a closed issue is dropped when it is due. "
                             "Use --keep-if-closed to keep it\n" % (PROG, repo, number))
            return EXIT_USAGE
        warning = ("%s: reminder is dropped if it closes (%s#%d closes or its pull request merges), with one notice to you; "
                   "use --keep-if-closed to keep it\n" % (PROG, repo, number))
    item = add_reminder(state, args.name, parse_when(args.when, moment), moment,
                        text, number, sender, repo, condition=condition or None,
                        key=_one_line(args.key, 80) if args.key else None, ttl=ttl)
    if len(item["text"]) > TEXT_CAP:
        sys.stderr.write("%s: the text is %d characters: it is delivered in parts, each under %d, the last one closing it\n" % (PROG, len(item["text"]), TEXT_CAP))
    engine.audit.append("cli", "reminder.add", args.name, {"id": item["id"], "at": iso(item["at"]),
                                                          "from": sender, "issue": number, "repo": repo,
                                                          **({"key": _one_line(item["key"], 60)} if item.get("key") else {})})
    print(item["id"])
    sys.stderr.write("%s: reminder for %s %s (now %s)\n" % (PROG, args.name, _print_when(item["at"], moment), _print_when_now(moment)))
    if warning:
        sys.stderr.write(warning)
    return EXIT_OK


def read_text_file(path, flag="--text-file", outcome="nothing armed", own_dirs_only=False):
    """(text, None) of a UTF-8 text file for `remind --text-file` and `send --file` (`flag` and `outcome` word the refusals), or (None, why it is refused):
    unreadable, not a regular file, too large, not UTF-8 text, or empty. `own_dirs_only` (send): a symlink is refused unless it resolves inside the
    user's home, the temp directory or the working directory."""
    import tempfile   # local: a module-level name here would be one more entry import (docs/architecture.md counts them)
    name = _one_line(path, 80)
    if own_dirs_only and os.path.islink(path):
        real = os.path.realpath(path)
        roots = [os.path.realpath(r) for r in (os.path.expanduser("~"), tempfile.gettempdir(), os.getcwd()) if r]
        if not any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in roots):
            return None, "%s %s is a symlink that leaves your own directories: %s" % (flag, name, outcome)
    try:
        with open(path, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None, "%s %s is not a regular file: %s" % (flag, name, outcome)
            raw = handle.read(TEXT_FILE_MAX * 4 + 1)   # a character is at most 4 bytes: bounded whatever the file is
    except OSError as err:
        return None, "%s %s cannot be read (%s): %s" % (flag, name, err.strerror or "error", outcome)
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        if len(raw) > TEXT_FILE_MAX * 4:
            text = None
        else:
            return None, "%s %s is not UTF-8 text: %s" % (flag, name, outcome)
    if text is None or len(text) > TEXT_FILE_MAX:
        return None, "%s %s is over %d characters: %s" % (flag, name, TEXT_FILE_MAX, outcome)
    if "\x00" in text:
        return None, "%s %s is not text (it holds a NUL): %s" % (flag, name, outcome)
    if not text:
        return None, "%s %s is empty: %s" % (flag, name, outcome)
    return text, None


cmd_remind.ensure_on_use = True   # the daemon is alive when it runs
cmd_remind.header_cache = True    # #1520


# ---- ci-queue (#1961): a read-only measure of the Actions queue; INLINED here until #1092 (no new flat module). GET only, writes nothing.

CI_QUEUE_GATE_LEGS = ("test (ubuntu-latest)", "test-cloud (ubuntu-latest)", "test (macos-latest)", "test-cloud (macos-latest)")   # bin/merge_gate.py UBUNTU_LEGS + MAC_LEGS (a test holds them equal)
CI_QUEUE_GATE_FEEDERS = ("plan",)   # a job no gate names but that builds the matrix of the jobs a gate does name: never a cut candidate
CI_QUEUE_PAGES = 10                 # pages of 100 read for one run's jobs (bounded)
CI_QUEUE_RUN_PAGES = 40             # pages of 100 read for the run list (bounded)


def _ci_time(text):
    """Epoch seconds of an Actions timestamp, or None when it is absent."""
    if not isinstance(text, str) or not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _ci_peak(spans, now):
    """The most intervals open at one instant. `spans` are (start, end or None); None runs to now. At one instant an end is counted before a start."""
    events = []
    for start, end in spans:
        events.append((start, 1))
        events.append((now if end is None else end, -1))
    open_now = peak = 0
    for _, step in sorted(events):
        open_now += step
        peak = max(peak, open_now)
    return peak


def ci_queue_measure(runs, jobs_by_run, now, hours=24, gate_names=(), docs_only=()):
    """The counts of the window: per run the queue wait (run created to first job started; an unstarted run waits until `now`), the run time and the jobs
    by name; the job-minutes by workflow; the peak of jobs queued at once and the observed ceiling of jobs running at once; the cut, dedupe and move
    candidates with job-minutes per day. `runs` and `jobs_by_run` are the Actions API objects (`created_at`, `started_at`, `completed_at`)."""
    since = now - hours * 3600
    per_run, minutes, queued, running, job_minutes = [], {}, [], [], {}
    seen_heads, repeat_runs, repeat_minutes, move_minutes = set(), 0, 0.0, 0.0
    for run in sorted((r for r in runs if (_ci_time(r.get("created_at")) or 0) >= since), key=lambda r: _ci_time(r["created_at"])):
        created, jobs = _ci_time(run["created_at"]), jobs_by_run.get(run["id"]) or []
        starts = [t for t in (_ci_time(j.get("started_at")) for j in jobs) if t is not None]
        first = min(starts) if starts else None
        wait = (first if first is not None else now) - created
        done = bool(jobs) and all(j.get("completed_at") for j in jobs)
        end = max(_ci_time(j["completed_at"]) for j in jobs) if done else now
        names = {}
        run_minutes = macos_minutes = 0.0
        for job in jobs:
            names[job["name"]] = names.get(job["name"], 0) + 1
            job_start, job_end = _ci_time(job.get("started_at")), _ci_time(job.get("completed_at"))
            queued.append((_ci_time(job.get("created_at")) or created, job_start))
            if job_start is not None:
                running.append((job_start, job_end))
                spent = ((now if job_end is None else job_end) - job_start) / 60.0
                run_minutes += spent
                job_minutes[job["name"]] = job_minutes.get(job["name"], 0.0) + spent
                if "macos" in job["name"]:
                    macos_minutes += spent
        workflow = run.get("name") or "?"
        minutes[workflow] = minutes.get(workflow, 0.0) + run_minutes
        head = (workflow, run.get("event"), run.get("head_sha"))
        if run.get("event") == "pull_request" and run.get("head_sha"):
            if head in seen_heads:
                repeat_runs += 1
                repeat_minutes += run_minutes
            seen_heads.add(head)
        if run["id"] in docs_only:
            move_minutes += macos_minutes
        per_run.append({"id": run["id"], "workflow": workflow, "event": run.get("event"), "wait_seconds": int(round(wait)),
                        "run_seconds": int(round(end - first)) if first is not None else 0, "jobs": len(jobs), "job_names": names})
    scale = 24.0 / hours
    cut = [{"kind": "cut", "what": name, "job_minutes_per_day": int(round(spent * scale))} for name, spent in sorted(job_minutes.items())
           if name not in gate_names and name not in CI_QUEUE_GATE_FEEDERS]
    candidates = cut + [
        {"kind": "dedupe", "what": "%d run(s) on a head that already had a run" % repeat_runs, "job_minutes_per_day": int(round(repeat_minutes * scale))},
        {"kind": "move", "what": "macOS legs of %d run(s) whose change touches no platform path" % len(set(docs_only)), "job_minutes_per_day": int(round(move_minutes * scale))}]
    return {"window_hours": hours, "runs": per_run, "job_minutes_by_workflow": {k: int(round(v)) for k, v in sorted(minutes.items())},
            "peak_queued": _ci_peak(queued, now), "observed_ceiling": _ci_peak(running, now), "published_limit": "not readable by a lane",
            "repeat_runs": repeat_runs, "draft_push_runs": sum(1 for r in runs if r.get("draft") is True), "candidates": candidates}


def ci_queue_lines(report):
    out = ["%-12s %-18s %-14s %8s %8s %5s" % ("run", "workflow", "event", "wait s", "run s", "jobs")]
    for run in report["runs"]:
        out.append("%-12s %-18s %-14s %8d %8d %5d" % (run["id"], run["workflow"], run["event"], run["wait_seconds"], run["run_seconds"], run["jobs"]))
        out.extend("    %s x %d" % (name, count) for name, count in sorted(run["job_names"].items()))
    out.append("job-minutes by workflow (%dh window):" % report["window_hours"])
    out.extend("    %s = %d" % (name, value) for name, value in report["job_minutes_by_workflow"].items())
    out.append("peak queued = %d" % report["peak_queued"])
    out.append('runner limit: "observed" ceiling of %d concurrent jobs; published limit: "%s"' % (report["observed_ceiling"], report["published_limit"]))
    out.append("repeat runs on one head: %d; draft push runs: %d" % (report["repeat_runs"], report["draft_push_runs"]))
    out.append("candidates (job-minutes per day):")
    out.extend("    %s: %s = %d" % (c["kind"], c["what"], c["job_minutes_per_day"]) for c in report["candidates"])
    return out


def ci_queue_get(runner, gh, path, timeout):
    """ONE `gh api` GET (no method flag, no field flag: gh sends GET); the JSON object, or None when the call failed. Never prints the output."""
    try:
        done = runner([gh, "api", path], capture_output=True, text=True, timeout=timeout)
        return json.loads(done.stdout) if done.returncode == 0 else None
    except Exception:   # a timeout, a missing gh or a body that is not JSON: the read failed, nothing else is said (never the output: it could carry a credential)
        return None


def ci_queue_fetch(runner, gh, repo, since, patterns=(), timeout=60):
    """(runs, jobs_by_run, docs_only run ids) of the repository since `since`: one list call per page and one jobs call per run, GET only."""
    runs, jobs_by_run, docs_only = [], {}, set()
    for page in range(1, CI_QUEUE_RUN_PAGES + 1):   # newest first: stop at the first page that reaches back past the window
        data = ci_queue_get(runner, gh, "repos/%s/actions/runs?per_page=100&page=%d" % (repo, page), timeout)
        if data is None:
            raise ReminderError("the run list could not be read")
        listed = data.get("workflow_runs") or []
        runs.extend(r for r in listed if (_ci_time(r.get("created_at")) or 0) >= since)
        if len(listed) < 100 or any((_ci_time(r.get("created_at")) or 0) < since for r in listed):
            break
    for run in runs:
        found = []
        for page in range(1, CI_QUEUE_PAGES + 1):
            data = ci_queue_get(runner, gh, "repos/%s/actions/runs/%s/jobs?filter=all&per_page=100&page=%d" % (repo, run["id"], page), timeout)
            if data is None:
                raise ReminderError("the jobs of run %s could not be read" % run["id"])
            found.extend(data.get("jobs") or [])
            if len(data.get("jobs") or []) < 100:
                break
        jobs_by_run[run["id"]] = found
    heads = {}
    for run in runs:
        if run.get("head_sha") and any("macos" in j["name"] and j.get("started_at") for j in jobs_by_run[run["id"]]):
            heads.setdefault(run["head_sha"], []).append(run["id"])
    for head, ids in (heads.items() if patterns else ()):
        commit = ci_queue_get(runner, gh, "repos/%s/commits/%s" % (repo, head), timeout) or {}
        files = [f.get("filename", "") for f in commit.get("files") or []]
        if files and not any(re.fullmatch(".*".join(re.escape(p) for p in pattern.replace("**", "*").split("*")), name) for name in files for pattern in patterns):
            docs_only.update(ids)
    return runs, jobs_by_run, docs_only


def ci_queue_platform_paths(path):
    """The globs of a macos-platform-paths.txt file (`*` crosses a `/`); [] when the file cannot be read (then no run is a `move` candidate)."""
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeError):
        return []
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def cmd_ci_queue(args, runner, now):
    """`ci-queue [--repo OWNER/REPO] [--hours N] [--until ISO] [--paths-file F] [--json]`: the queue wait, run time, jobs, job-minutes, peak queued and the observed ceiling of the
    Actions runs of the window, and the cut / dedupe / move candidates. Read-only: GET requests only, nothing is written to GitHub."""
    engine, _, config = build_engine(runner, now, "cli", writable=False)
    repo = args.repo or config.get("intent_repo")
    if not repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        sys.stderr.write("%s: ci-queue: give --repo OWNER/REPO\n" % PROG)
        return EXIT_USAGE
    if not 1 <= args.hours <= 168:
        sys.stderr.write("%s: ci-queue: --hours is 1 to 168\n" % PROG)
        return EXIT_USAGE
    try:
        end = _ci_time(args.until) if args.until else now()
    except ValueError:
        sys.stderr.write("%s: ci-queue: --until is an ISO time with a zone\n" % PROG)
        return EXIT_USAGE
    try:
        runs, jobs_by_run, docs_only = ci_queue_fetch(runner, config["gh"], repo, end - args.hours * 3600, ci_queue_platform_paths(args.paths_file))
    except (ReminderError, OSError) as err:
        sys.stderr.write("%s: ci-queue: %s\n" % (PROG, _one_line(str(err), 120)))
        return EXIT_ERROR
    report = ci_queue_measure([r for r in runs if (_ci_time(r.get("created_at")) or 0) <= end], jobs_by_run, end, args.hours,
                              CI_QUEUE_GATE_LEGS, docs_only)
    print(json.dumps(report, indent=1, sort_keys=True) if args.json else "\n".join(ci_queue_lines(report)))
    return EXIT_OK


cmd_ci_queue.direct = True   # reads GitHub only; no daemon start
# ---- queue (#1786)
# The standing launch queue: `launch-queue.jsonl` (the lanes append) and `launch-queue.status.jsonl` (the cloud lane appends), both in the
# state directory. It decides the order, the staleness and the hash check; it never starts a pod. Temporary home: moves with #1092.

QUEUE_FILE = "launch-queue.jsonl"
QUEUE_STATUS_FILE = "launch-queue.status.jsonl"
QUEUE_LINE_MAX = 2000
QUEUE_STALE_SECONDS = 120 * 60
QUEUE_CLOUD_LANE = "fleet-cloud"
QUEUE_REFUSED = 1
QUEUE_KINDS = ("builder", "tester", "measure")
QUEUE_KEYS = ("id", "enqueued", "lane", "name", "kind", "ask", "brief_file", "base_branch", "wip_branch", "role", "reports_to", "shape",
              "idle", "hard", "priority", "plan_issue", "plan_hash_full", "approved_by", "approved_at", "until")
QUEUE_STATES = ("queued", "waiting", "launched", "refused", "stale", "superseded", "finished")
QUEUE_GONE = ("launched", "finished", "superseded")


def queue_append(path, record):
    data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > QUEUE_LINE_MAX + 1:
        raise FactError("a queue line is at most %d bytes" % QUEUE_LINE_MAX)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)   # one O_APPEND write: one line, never half read
    finally:
        os.close(fd)


def queue_lines(path, keys, tail=None):
    """([(line number, record)], [bad line numbers]): a missing file is empty; a bad line is counted, never raised."""
    try:
        with open(str(path), "rb") as handle:
            if tail:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - tail))
            raw = handle.read()
    except FileNotFoundError:
        return [], []
    except OSError:
        return [], [0]
    good, bad = [], []
    for number, line in enumerate(raw.split(b"\n"), 1):
        if not line.strip():
            continue
        try:
            if len(line) > QUEUE_LINE_MAX:
                raise ValueError("long")
            record = load_module("record_json").parse_bounded(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            bad.append(number)
            continue
        if isinstance(record, dict) and all(isinstance(record.get(key), str) for key in keys):
            good.append((number, record))
        else:
            bad.append(number)
    return good, bad


def queue_epoch(text):
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def queue_fold(state_dir):
    """(entries by id in file order, last status by id, bad entry lines, bad status lines): the last line of an id decides."""
    entries, status = {}, {}
    lines, bad = queue_lines(Path(state_dir) / QUEUE_FILE, ("id",))
    for number, rec in lines:
        key = rec["id"]
        if rec.get("op") is None:
            if isinstance(rec.get("name"), str) and (key not in entries or entries[key]["lane"] == rec.get("lane")):
                entries[key] = dict(rec, line=number)
            else:
                bad.append(number)
        elif key in entries and rec.get("lane") == entries[key]["lane"]:
            if rec["op"] == "priority" and isinstance(rec.get("priority"), int):
                entries[key]["priority"] = rec["priority"]
            elif rec["op"] == "cancel":
                entries[key]["cancelled"] = True
    rows, bad_status = queue_lines(Path(state_dir) / QUEUE_STATUS_FILE, ("id", "state"))
    for _, rec in rows:
        status[rec["id"]] = rec
    return entries, status, sorted(bad), bad_status


def queue_view(state_dir, moment):
    """(live entries in launch order, each with age, state and stale; the unreadable line numbers)."""
    entries, status, bad, bad_status = queue_fold(state_dir)
    view = []
    for key, entry in entries.items():
        last = status.get(key) or {}
        if entry.get("cancelled") or last.get("state") in QUEUE_GONE:
            continue
        queued = queue_epoch(entry.get("enqueued") or "")
        until = queue_epoch(entry["until"]) if isinstance(entry.get("until"), str) else None
        age = moment - queued if queued is not None else QUEUE_STALE_SECONDS + 1
        stale = moment > until if until is not None else age > QUEUE_STALE_SECONDS
        refused = last.get("state") == "refused" and last.get("rev", 0) >= entry["line"]
        priority = entry["priority"] if isinstance(entry.get("priority"), int) else 5
        view.append(dict(entry, age=age, state=last.get("state") or "queued", reason=last.get("reason"), stale=stale, skipped=refused, order=(priority, queued or 0, key)))
    view.sort(key=lambda item: item["order"])
    return view, bad + bad_status


def queue_age(seconds):
    return "%dm" % (max(seconds, 0) // 60) if seconds < 7200 else "%dh" % (seconds // 3600)


def queue_note(state_dir, key, state, moment, pod=None, reason=None):
    """One status line of the cloud lane's answers; `rev` is how many lines of the queue it has seen."""
    seen = max([n for n, _ in queue_lines(Path(state_dir) / QUEUE_FILE, ("id",))[0]] or [0])
    rec = {"id": key, "state": state, "at": iso(moment), "rev": seen}
    if pod:
        rec["pod"] = pod
    if reason:
        rec["reason"] = reason
    queue_append(Path(state_dir) / QUEUE_STATUS_FILE, rec)


def queue_add(state_dir, lane, fields, moment):
    """The id of the new entry, or a FactError with the one line of the refusal."""
    view, _ = queue_view(state_dir, moment)
    if any(item["name"] == fields.get("name") for item in view):
        raise FactError("%s is already queued" % fields.get("name"))
    if not isinstance(fields.get("ask"), int):
        raise FactError("--ask is required: the issue number the pod works under")
    if fields.get("kind") not in QUEUE_KINDS:
        raise FactError("kind is one of %s" % ", ".join(QUEUE_KINDS))
    if not (isinstance(fields.get("name"), str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", fields["name"])):
        raise FactError("name is 1 to 64 letters, digits, dots, dashes")
    if not (isinstance(fields.get("brief_file"), str) and fields["brief_file"].startswith("/")):
        raise FactError("brief_file is the absolute path of the brief")
    if fields.get("kind") == "builder":
        issue = fields.get("plan_issue")
        if isinstance(issue, str) and re.fullmatch(r"[0-9]+", issue):
            if not (isinstance(fields.get("repo"), str) and re.fullmatch(r"[^/\s#]+/[^/\s#]+", fields["repo"])):
                raise FactError("plan_issue %s is a bare number: give --repo owner/repo or the whole owner/repo#N" % issue)
            fields = dict(fields, plan_issue="%s#%s" % (fields["repo"], issue))
        elif not (isinstance(issue, str) and re.fullmatch(r"[^/\s#]+/[^/\s#]+#[0-9]+", issue)):
            raise FactError("plan_issue is owner/repo#N, the form of the plan.approved audit event")
    digits = re.sub(r"[^0-9]", "", iso(moment))
    key = "q-%s-%s-%s" % (digits[:8], digits[8:14], fields.get("ask"))
    entry = {k: v for k, v in fields.items() if k in QUEUE_KEYS and v is not None}
    entry.update(id=key, enqueued=iso(moment), lane=lane)
    queue_append(Path(state_dir) / QUEUE_FILE, entry)
    return key


def queue_change(state_dir, lane, key, op, moment, audit, number=None):
    """Priority or cancel by the lane that queued the entry: one line appended, one audit line."""
    entries, _, _, _ = queue_fold(state_dir)
    entry = entries.get(key)
    if entry is None or entry.get("cancelled"):
        raise FactError("no queued entry %s" % key)
    if entry["lane"] != lane:
        raise FactError("%s was queued by %s, not by %s: refused" % (key, entry["lane"], lane))
    if op == "priority" and not (isinstance(number, int) and 0 <= number <= 100):
        raise FactError("priority is a whole number from 0 to 100 (the plan gives no range): %r" % (number,))
    last = queue_fold(state_dir)[1].get(key) or {}
    note = "note: %s is refused (%s); it stays skipped until its lane appends a replacement line" % (key, last.get("reason")) if last.get("state") == "refused" else None
    rec = {"id": key, "op": op, "lane": lane, "at": iso(moment)}
    old = entry.get("priority")
    if op == "priority":
        rec["priority"] = number
    queue_append(Path(state_dir) / QUEUE_FILE, rec)
    if audit is not None:
        audit.append("cli", "queue." + op, entry["name"], {"lane": lane, "id": key, "name": entry["name"], "old": old, "new": number})
    return note


def queue_hash(value):
    """The 64 hex characters of a plan hash given as `sha256:<64 hex>` or as bare 64 hex; None for anything else."""
    text = value[7:] if isinstance(value, str) and value.startswith("sha256:") else value
    return text if isinstance(text, str) and re.fullmatch(r"[0-9a-f]{64}", text) else None


def queue_next(state_dir, free, moment, live_hash, approvals):
    """(lines to print, entries to launch): `live_hash(issue)` is the live plan hash or None; `approvals` is a set of (issue, plan_hash)."""
    out, picked = [], []
    if free <= 0:
        return out, picked
    view, _ = queue_view(state_dir, moment)
    for item in view:
        if len(picked) >= free:
            break
        key, name = item["id"], item["name"]
        if item["stale"]:
            if item["state"] != "stale":
                queue_note(state_dir, key, "stale", moment)
            out.append("stale: %s, queued %s ago" % (name, queue_age(item["age"])))
            continue
        if item["skipped"]:
            continue
        if item["kind"] == "builder":
            full = queue_hash(item.get("plan_hash_full"))
            if full is None:
                queue_note(state_dir, key, "refused", moment, reason="no-plan-hash")
                continue
            try:
                live = queue_hash(live_hash(item.get("plan_issue")))
                if live is None:
                    raise FactError("no 64-hex plan hash")
            except FactError as err:
                why = "hash-unread: %s" % err   # waiting, not refused: a read that failed may succeed at the next slot
                if item["state"] != "waiting" or item.get("reason") != why:
                    queue_note(state_dir, key, "waiting", moment, reason=why)
                out.append("not launched: %s: the live plan hash could not be read: %s" % (name, err))
                continue
            if live != full:
                queue_note(state_dir, key, "refused", moment, reason="hash-mismatch %s" % live[:8])
                out.append("not launched: %s: the live plan hash %s is not the one the entry needs %s" % (name, live[:8], full[:8]))
                continue
            if (item.get("plan_issue"), full) not in approvals:   # approvals hold normalised hashes
                queue_note(state_dir, key, "refused", moment, reason="no-approval")
                continue
        picked.append(item)
        out.append(json.dumps({k: v for k, v in item.items() if k in QUEUE_KEYS}, sort_keys=True))
    return out, picked


def queue_list_lines(state_dir, moment):
    view, bad = queue_view(state_dir, moment)
    lines = ["unreadable: line %d" % number for number in bad]
    for item in view:
        lines.append("%s %s p%s age %s %s%s" % (item["id"], item["name"], item.get("priority"), queue_age(item["age"]), item["state"],
                                                " stale" if item["stale"] else ""))
    return lines


def queue_live_hash(runner, issue):
    """The live plan hash of the plan issue, read the way `plan check` reads it; a FactError naming the cause when it cannot be read."""
    try:
        done = runner([sys.executable, sys.argv[0], "plan", "check", str(issue), "--json"], capture_output=True, text=True, timeout=60)
    except Exception as err:
        raise FactError("%s: %s" % (type(err).__name__, str(err)[:100]))
    try:
        got = load_module("record_json").parse_bounded(done.stdout)
    except Exception:
        raise FactError("plan check printed no JSON: %s" % (done.stdout or "").strip()[:100])
    if not isinstance(got, dict):
        raise FactError("plan check printed no JSON object")
    if got.get("result") == "error":
        raise FactError("plan check says: %s" % str(got.get("error"))[:100])
    if not got.get("plan_hash"):
        raise FactError("plan check gave no plan_hash")
    return got["plan_hash"]


def queue_approvals(state_dir):
    rows, _ = queue_lines(Path(state_dir) / "audit.jsonl", ("event",), tail=8 * 1024 * 1024)
    found = set()
    for _, rec in rows:
        detail = rec.get("detail")
        if rec["event"] == "plan.approved" and isinstance(detail, dict):
            found.add((detail.get("issue"), queue_hash(detail.get("plan_hash"))))
    return found


def cmd_queue(args, runner, now):
    engine, _, _ = build_engine(runner, now, "cli")
    caller = identify_caller(engine.herdr, engine.v2, os.environ)
    state_dir, moment, action = str(engine.state_dir), now(), args.queue_action
    try:
        if action == "add":
            fields = {k: getattr(args, k) for k in QUEUE_KEYS if hasattr(args, k)}
            fields["repo"] = args.repo
            key = queue_add(state_dir, caller, fields, moment)
            print(key)
            try:
                engine.send(caller, QUEUE_CLOUD_LANE, "entry %s queued" % key)
            except Exception:   # the entry is written: a failed wake never fails the add
                print("queue: the wake failed; the entry %s is written" % key)
        elif action in ("priority", "cancel"):
            note = queue_change(state_dir, caller, args.id, action, moment, engine.audit, getattr(args, "number", None))
            print("%s: %s" % (action, args.id))
            if note:
                print(note)
        elif action == "list":
            lines = queue_list_lines(state_dir, moment)
            if lines:
                print("\n".join(lines))
        elif action == "next":
            out, _ = queue_next(state_dir, args.free, moment, lambda issue: queue_live_hash(runner, issue), queue_approvals(state_dir))
            if out:
                print("\n".join(out))
        elif action == "status":
            if caller != QUEUE_CLOUD_LANE:
                raise FactError("a status line is appended by %s" % QUEUE_CLOUD_LANE)
            if args.state not in QUEUE_STATES:
                raise FactError("state is one of %s" % ", ".join(QUEUE_STATES))
            if args.id not in queue_fold(state_dir)[0]:
                raise FactError("no queued entry %s" % args.id)
            queue_note(state_dir, args.id, args.state, moment, pod=args.pod, reason=args.reason)
            print("status: %s %s" % (args.id, args.state))
    except FactError as err:
        print("queue: %s" % err)
        return QUEUE_REFUSED
    return EXIT_OK


def add_queue_parsers(sub):
    queue = sub.add_parser("queue", help="The standing launch queue: add, change, list; the cloud lane reads it at a free slot")
    actions = queue.add_subparsers(dest="queue_action", required=True)
    add = actions.add_parser("add", help="Queue one pod to launch")
    for flag in QUEUE_KEYS:
        if flag not in ("id", "enqueued", "lane"):
            add.add_argument("--" + flag.replace("_", "-"), dest=flag, default=None, type=int if flag in ("ask", "priority", "idle", "hard") else str)
    add.add_argument("--repo", default=None, help="owner/repo for a bare --plan-issue number")
    for action in ("priority", "cancel"):
        one = actions.add_parser(action, help="%s an entry you queued" % action.capitalize())
        one.add_argument("id")
        if action == "priority":
            one.add_argument("number", type=int)
    actions.add_parser("list", help="The queue in launch order")
    nxt = actions.add_parser("next", help="The entries to launch at FREE free slots")
    nxt.add_argument("--free", type=int, required=True)
    status = actions.add_parser("status", help="The cloud lane's answer for an entry")
    status.add_argument("id")
    status.add_argument("state")
    status.add_argument("--pod")
    status.add_argument("--reason")
