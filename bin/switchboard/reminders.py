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
        raw = json.loads(path.read_text())
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
TEXT_CAP = 4000  # mirrors MESSAGE_CAP in bin/fleet-switchboard


def add_reminder(state_dir, to, at, created, text, issue=None, sender=None, repo=None, condition=None, key=None, wake_class=None):
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
        return json.loads(data.decode("utf-8"))
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
    "load_tick": ("load alert", "lane, severity and reasons", "6 hours; merged into the wake message", "test_load_alert_is_keyed_by_the_data_not_the_text"),
    "friction_tick": ("friction wake", "slot (own state)", "per slot", "test_one_wake_per_orchestrator_per_day_not_for_cos_or_coders"),
    "cmd_budget_notice": ("budget notice", "account (own claim)", "1 hour", "test_s2_the_top_spenders_orchestrator_gets_one_message_with_the_agent_and_the_numbers_and_a_second_call_sends_none"),
    "_impact_matrix_episode": ("impact matrix unreadable", "episode (own state)", "per episode", "test_s6_an_unreadable_matrix_changes_nothing_and_says_so_once_for_the_episode"),
    "_impact_post_list": ("owner-hold rule list", "rule version (own state)", "per rule", "test_s8_an_unreadable_decision_or_rule_or_file_list_is_refused_and_nothing_is_lifted"),
    "cmd_remind": ("a reminder the agent set (`remind`)", "its own id (a request, not a finding)", "none: sent as asked", "test_a_reminder_is_a_fact_only_once_it_is_due"),
    "set_checkpoint_reminders": ("checkpoint reminder", "launch (own id)", "once per launch", "test_a_launch_checkpoint_reminder_is_not_a_wait_and_a_workers_own_reminder_is"),
    "notify_drop": ("reminder drop notice", "dropped reminder (removed first)", "once", "test_a_drop_sends_the_setter_one_notice_naming_text_and_reason_audited_once"),
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
    merged.member_lines = {f.key: "- %s: %s" % (_clean_line(wake_class_of(f)), _clean_line(f.summary)) for f in group}
    return [f for f in facts if not wake_class_of(f)] + rest + [merged]


def remove_reminders(state_dir, ids):
    """Drop these ids from reminders.json. Returns the ids that were there."""
    with reminders_lock(state_dir):
        items = read_reminders(state_dir)
        gone = [item["id"] for item in items if item["id"] in ids]
        if gone:
            write_reminders(state_dir, [item for item in items if item["id"] not in ids])
    return gone


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

    def __call__(self):
        facts = []
        everything = read_reminders(self.state_dir)
        for item in self.due():
            kept = self.superseded(item, everything)
            if kept is not None:
                if remove_reminders(self.state_dir, {item["id"]}) and self.engine is not None and self.engine.audit is not None:
                    self.engine.audit.append(self.engine.actor, "reminder.drop_superseded", item["to"],
                                             {"dropped": item["id"], "kept": kept["id"], "key": _one_line(item["key"], 60)})
                continue
            if item.get("condition"):
                found = self.verdict(item)
                if found is None:
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
            if late >= LATE_NOTE_SECONDS:
                note = " (late by %d min)" % int(late // 60)
                text = text[:max(0, TEXT_CAP - len(note))] + note   # the note is applied before the cap and kept: the body is cut, not the note
            facts.append(Fact("reminder:" + item["id"], item["to"], "reminder", text, item["at"],
                              issue=item.get("issue"), batch=False, sender=item.get("from"), repo=item.get("repo")))
            if isinstance(item.get("wake_class"), str):
                facts[-1].wake_class = item["wake_class"]
        return facts


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
    if args.list_mine and args.cancel:
        sys.stderr.write("%s: remind: give --list or --cancel, not both\n" % PROG)
        return EXIT_USAGE
    if args.name or args.when or args.text:
        sys.stderr.write("%s: remind: --list and --cancel take no recipient, time or text\n" % PROG)
        return EXIT_USAGE
    try:
        mine = [item for item in read_reminders(state) if sender in (item.get("to"), item.get("from"))]
    except ReminderError as err:
        sys.stderr.write("%s: remind: %s\n" % (PROG, _one_line(str(err), 200)))
        return EXIT_ERROR
    if args.list_mine:
        mine.sort(key=lambda item: item["at"])
        for item in mine[:REMIND_LIST_MAX]:
            print("%s  %s  to=%s  issue=%s  key=%s  %s" % (item["id"], iso(item["at"]), item["to"], item.get("issue") or "-", _one_line(item.get("key") or "-", 80), _one_line(item["text"], 120)))
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
    if args.list_mine or args.cancel:
        return remind_manage(args, engine, state, sender, moment)
    if not args.name or not args.when or not args.text:
        sys.stderr.write("%s: remind needs <name> <when> <text> (or --list, or --cancel <id>)\n" % PROG)
        return EXIT_USAGE
    try:
        repo, number = resolve_for_sender(engine, config, args.issue, sender)
    except IssueRefError as err:
        sys.stderr.write("%s: %s\n" % (PROG, err))
        return EXIT_USAGE
    if args.key and not (isinstance(sender, str) and sender.strip()):
        sys.stderr.write("%s: --key needs a known sender (an unidentified caller would replace other callers' reminders); nothing armed\n" % PROG)
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
                        " ".join(args.text).strip(), number, sender, repo, condition=condition or None,
                        key=_one_line(args.key, 80) if args.key else None)
    if len(item["text"]) > TEXT_CAP:
        sys.stderr.write("%s: warning: text is cut at %d characters: %d characters over\n" % (PROG, TEXT_CAP, len(item["text"]) - TEXT_CAP))
    engine.audit.append("cli", "reminder.add", args.name, {"id": item["id"], "at": iso(item["at"]),
                                                          "from": sender, "issue": number, "repo": repo})
    print(item["id"])
    sys.stderr.write("%s: reminder for %s %s (now %s)\n" % (PROG, args.name, _print_when(item["at"], moment), _print_when_now(moment)))
    if warning:
        sys.stderr.write(warning)
    return EXIT_OK


cmd_remind.ensure_on_use = True   # the daemon is alive when it runs
