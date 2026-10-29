# ---------------------------------------------------------------- `sent` (#1550)
#
# A sender's own recent messages, each with the time it was sent, the time it was delivered and the time the recipient's next turn began.
# Read-only: no GitHub call, no write, no new state file. Three records the daemon already keeps are joined: the sent-keys ledger (`sent-keys.json`:
# the admission time of each `send:<sender>:<hash>` key, per recipient session), the audit log's `v2.synthetic` lines (the `result` line of a call is the
# delivery; its `intent` line names the recipient and the keys) and the recipient's own transcript (one bounded `message_list`, read through the
# daemon's read budget `v2_reads_per_minute`, never a loop of raw reads). The text of a message is never read or printed.

SENT_LIMIT_MAX = 20            # messages listed at most
SENT_AUDIT_TAIL = 4 << 20      # bytes of the end of audit.jsonl read; a message older than that shows its delivered time as `unknown`
SENT_NOT_YET, SENT_UNKNOWN = "not yet", "unknown"
SENT_NOTE = ("picked up = the start of the recipient's first turn after the delivery; it is a turn start, "
             "not proof that the text was understood.")


def sent_epoch(text):
    """An audit `ts` (ISO, `Z`) as epoch seconds, or None."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def sent_clock(epoch):
    """HH:MMZ in UTC."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%MZ")


def sent_deliveries(state_dir):
    """{key: (recipient, delivered epoch or None, delivered ok)} for the `send:` keys in the tail of the audit log. A key with an intent line and no result line
    maps to (recipient, None, False). Never raises: an unreadable log gives {}."""
    path = Path(state_dir) / "audit.jsonl"
    try:
        with open(str(path), "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - SENT_AUDIT_TAIL))
            lines = handle.read(SENT_AUDIT_TAIL).decode("utf-8", "replace").splitlines()[1 if size > SENT_AUDIT_TAIL else 0:]
    except OSError:
        return {}
    intents, results = {}, {}
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("event") != "v2.synthetic" or not isinstance(entry.get("call"), str):
            continue
        if entry.get("phase") == "intent" and isinstance(entry.get("detail"), dict):
            keys = [k for k in entry["detail"].get("keys") or [] if isinstance(k, str) and k.startswith("send:")]
            if keys:
                intents[entry["call"]] = (entry.get("subject"), keys)
        elif entry.get("phase") == "result":
            results[entry["call"]] = (sent_epoch(entry.get("ts")), entry.get("ok") is True)
    found = {}
    for call, (recipient, keys) in intents.items():
        at, ok = results.get(call, (None, False))
        for key in keys:
            found[key] = (recipient, at, ok)
    return found


def sent_pickup(engine, session, delivered, now, cache):
    """The `time.created` of the earliest assistant message of `session` at or after `delivered`: an epoch, SENT_NOT_YET (the page was read and holds none)
    or SENT_UNKNOWN (it could not be read, the read budget was spent, or the page does not reach back to the delivery). One read per session, kept in `cache`."""
    if session not in cache:
        page = None
        if engine.reads.may_fill(now):   # the daemon's own budget (v2_reads_per_pass, v2_reads_per_minute)
            engine.reads.filled(now)
            try:
                page = engine.v2.message_list(session, limit=READ_LIMIT, type="assistant")
            except V2Error:
                page = None
        cache[session] = page if isinstance(page, list) else None
    page = cache[session]
    if page is None:
        return SENT_UNKNOWN
    turns = [at for at in (_created(m) for m in page if isinstance(m, dict) and m.get("type") == "assistant") if at is not None]
    later = [at for at in turns if at >= delivered]
    if len(page) >= READ_LIMIT and turns and min(turns) > delivered:
        return SENT_UNKNOWN   # a full page that starts after the delivery: the first turn after it may be older than the page
    return min(later) if later else SENT_NOT_YET


def sent_rows(engine, sender, limit, now):
    """The sender's own messages, newest first, at most `limit`: [{recipient, sent, delivered, picked_up}] where a time is epoch seconds or one of the words.
    ONLY keys that start `send:<sender>:` are listed."""
    wanted = re.compile("send:%s:[0-9a-f]{16}" % re.escape(sender))   # a sender `a:b` is not matched by the keys of a sender `a`
    sessions, unknown = SentLedger(engine.state_dir).load()
    mine = []
    for table in (sessions, unknown):
        for session, entries in table.items():
            mine += [(at, key, session) for key, at in entries.items() if wanted.fullmatch(key)]
    mine.sort(key=lambda row: (-row[0], row[1]))
    deliveries = sent_deliveries(engine.state_dir)
    cache, rows = {}, []
    for at, key, session in mine[:limit]:
        recipient, delivered_at, ok = deliveries.get(key, (None, None, False))
        if delivered_at is None:
            delivered = SENT_NOT_YET if key in deliveries else SENT_UNKNOWN   # an intent with no result: not delivered; no audit line at all: out of the tail
        else:
            delivered = delivered_at if ok else SENT_UNKNOWN
        if isinstance(delivered, float):
            picked = sent_pickup(engine, session, delivered, now, cache)
        else:
            picked = SENT_NOT_YET if delivered == SENT_NOT_YET else SENT_UNKNOWN
        rows.append({"recipient": _one_line(str(recipient or SENT_UNKNOWN), 60), "sent": at, "delivered": delivered, "picked_up": picked})
    return rows


def sent_text(value):
    return sent_clock(value) if isinstance(value, (int, float)) else value


def cmd_sent(args, runner, now):
    """`sent [--limit N]`: the caller's own recent messages with their sent, delivered and picked-up times. No GitHub call, no write."""
    limit = args.limit if args.limit is not None else SENT_LIMIT_MAX
    if not 1 <= limit <= SENT_LIMIT_MAX:
        sys.stderr.write("%s: --limit is from 1 to %d\n" % (PROG, SENT_LIMIT_MAX))
        return EXIT_USAGE
    engine, _, _ = build_engine(runner, now, "cli", writable=False)
    if args.sender is not None:
        if os.environ.get(LAB_ENV) != "1":
            sys.stderr.write("%s: --from is only for the Lab (%s=1); an agent is identified by its pane\n" % (PROG, LAB_ENV))
            return EXIT_USAGE
        sender = args.sender
    else:
        sender = identify_caller(engine.herdr, engine.v2, os.environ)
    rows = sent_rows(engine, sender, limit, now())
    if not rows:
        print("no messages")
    for row in rows:
        print("%s  sent %s  delivered %s  picked up %s" % (row["recipient"], sent_text(row["sent"]), sent_text(row["delivered"]), sent_text(row["picked_up"])))
    print(SENT_NOTE)
    return EXIT_OK


def add_sent_parsers(sub):
    sent = sub.add_parser("sent", help="Your own recent messages: when each was sent, delivered and picked up by its recipient")
    sent.add_argument("--limit", type=int, help="How many of your newest messages to list (1 to %d, default %d)" % (SENT_LIMIT_MAX, SENT_LIMIT_MAX))
    sent.add_argument("--from", dest="sender", help="Lab only: the sender's fleet name")
