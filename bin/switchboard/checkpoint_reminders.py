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


CLOUD_DEFAULT_CHECKPOINT = ("25m", "40m")   # #1790: a cloud launch with no CHECKPOINT line and no flag arms this


def arm_cloud_checkpoints(args, runner, now):
    """#1790: after a cloud launch that succeeded, what a local launch arms (set_checkpoint_reminders), else the default 25m/40m.
    One line on stderr; never raises and always returns EXIT_OK: the pod already exists."""
    try:
        engine, state, _ = build_engine(runner, now, "cli")
        boss = args.reports_to
        if boss is None:
            try:
                boss = identify_caller(engine.herdr, engine.v2, os.environ)
            except Exception:
                boss = None
        brief, checkpoint, overdue = getattr(args, "brief_text", None), getattr(args, "checkpoint", None), getattr(args, "overdue", None)
        why = None
        if checkpoint is None and overdue is None and parse_checkpoint(brief) is None:
            why = "the brief's CHECKPOINT line could not be read" if re.search(r"^\s*CHECKPOINT\s*:", brief or "", re.I | re.M) else "no CHECKPOINT line and no flag"
            checkpoint, overdue = CLOUD_DEFAULT_CHECKPOINT
        note = set_checkpoint_reminders(engine, state, args.name, boss, None if args.issue is None else int(args.issue), getattr(args, "repo", None),
                                        brief, checkpoint, overdue, now(), now())
        if why and note and note.startswith("checkpoint reminders"):
            note = "default %s/%s (%s): %s" % (CLOUD_DEFAULT_CHECKPOINT + (why, note))
    except Exception as err:
        note = "no checkpoint reminders: %s" % _one_line(str(err), 200)
    if note:
        sys.stderr.write("%s: %s\n" % (PROG, note))
    return EXIT_OK


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
