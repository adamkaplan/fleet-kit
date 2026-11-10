def judge_request(request, config, build_judge):
    """(reply, judge): the reply for one parsed request, and the judge that made it (None when none was needed).

    `build_judge()` makes the PolicyJudge and is called only for a call that is to be judged.
    """
    why = policy_unjudged_reason(config, request)
    if why is not None:
        return policy_reply(request["configured"], why), None
    judge = build_judge()
    return judge.decide(request), judge


def build_policy_judge(config, runner, now):
    """The PolicyJudge over the configured clients: v2, the ask reader, Jev, the audit log."""
    if config["opencode"] is None:
        raise ConfigError("'opencode' is not configured: the judge cannot identify an agent without v2")
    state = ensure_state_dir()
    settings = config["policy"]
    jev_settings = dict(config["jev"], timeout_seconds=min(config["jev"]["timeout_seconds"], POLICY_WARM_SECONDS))
    jev = build_jev(dict(config, jev=jev_settings))
    v2 = V2Client(config["opencode"], config["opencode_env"], runner, min(config["call_timeout_seconds"], POLICY_WARM_SECONDS), transport=config["v2_transport"])
    return PolicyJudge(config, v2, intent_reader_for(config, runner, now), jev, AuditLog(state / "audit.jsonl"), state, now)


def make_judge_warmer(config, runner, now):
    """warm(sessions): refresh each session's judge context, errors counted and audited once, never raised."""
    def warm(sessions):
        done = failed = 0
        try:
            judge = build_policy_judge(config, runner, now)
            for session in sessions:
                try:
                    judge.warm(session)
                    done += 1
                except Exception:
                    failed += 1
            judge.audit.append(POLICY_ACTOR, "policy.warm", "judge", {"sessions": done, "failed": failed})
        except Exception as err:
            sys.stderr.write("%s: judge warm-up skipped: %s\n" % (PROG, _one_line(str(err), 120)))
    return warm


def schedule_judge_warm(engine, discovery):
    """Between passes, now and then, warm the judge for every fleet agent with a session, off the pass's
    thread. At half the cache lifetime, so a context is refreshed before it expires."""
    warm = getattr(engine, "judge_warm", None)
    if warm is None or discovery is None or engine.now() < engine._warm_due:
        return
    ttl = engine.config["policy"]["cache_seconds"] if engine.config.get("policy") else 0
    if not ttl:
        return
    engine._warm_due = engine.now() + max(15, ttl / 2)
    sessions = sorted({o.session_id for o in discovery.agents.values() if o.session_id})
    if sessions:
        threading.Thread(target=warm, args=(sessions,), daemon=True).start()


# ------------------------------------------- the judge socket: the daemon holds the key

# `judge-tool` runs inside whatever process the v2 plugin lives in, and its
# environment is the environment of every agent's shell tool. So it never
# reads the OpenRouter key, and never calls Jev: it sends the request over a
# local unix stream socket to the switchboard DAEMON, which holds the key in
# its own environment, judges, and answers with the same one JSON line. The
# daemon, not v2, holds the key.
POLICY_SOCKET = "judge.sock"


POLICY_READ_SECONDS = 2      # the daemon's wait for a request to arrive


POLICY_CLIENT_SLACK = 0.4    # seconds past the budget `judge-tool` waits for the daemon


POLICY_CONNECTIONS = 16      # judgements the daemon runs at once


# ---- #290 slice 6: what the in-service plugin pushes. One narrow message kind on the judge socket; the daemon only MARKS the session.
PLUGIN_EVENT_MAX = 2048          # bytes of one message


PLUGIN_EVENTS = frozenset(("prompt", "compaction", "idle", "activity"))


PLUGIN_EVENT_KEYS = frozenset(("v", "kind", "event", "sessionID", "at", "person", "id", "dropped"))


PLUGIN_MESSAGE_RE = re.compile(r"^msg_[A-Za-z0-9]{1,80}$")


PLUGIN_SESSION_PER_SECOND = 20   # accepted from one session in one second; the rest are dropped and counted


PLUGIN_TOTAL_PER_SECOND = 100    # accepted in all in one second


def parse_plugin_event(raw):
    """The checked event of one message, or None. Every field is validated against a fixed pattern or enum; anything else is refused whole, never trimmed,
    and no text of the message is ever returned."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > PLUGIN_EVENT_MAX:
        return None
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or set(data) - PLUGIN_EVENT_KEYS or data.get("v") != 1 or data.get("kind") != "event":
        return None
    session, event = data.get("sessionID"), data.get("event")
    if event not in PLUGIN_EVENTS or not isinstance(session, str) or not POLICY_SESSION_RE.match(session):
        return None
    for key in ("at", "dropped"):
        value = data.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10 ** 15):
            return None
    if data.get("person") is not None and not isinstance(data["person"], bool):
        return None
    if data.get("id") is not None and not (isinstance(data["id"], str) and PLUGIN_MESSAGE_RE.match(data["id"])):
        return None
    return {"event": event, "session": session, "dropped": data.get("dropped") or 0}


class EventReceiver:
    """The daemon's end of the plugin's pushes. It makes no read and no write: an accepted event marks the session on the feed (`touch`); the pass thread
    reads. Over-limit messages are dropped and counted (one audit line per change, never queued)."""

    def __init__(self, feed, audit=None, now=time.time, actor="daemon", known=None):
        self.feed, self.audit, self.now, self.actor, self.known = feed, audit, now, actor, known   # `known()`: the session ids of this fleet's agents; any other id is ignored
        self.lock = threading.Lock()
        self.counts = {"accepted": 0, "ignored": 0, "rate_limited": 0, "plugin_dropped": 0}
        self.window, self.per_session, self.total, self.limiting = None, {}, 0, False

    def handle(self, raw):
        """One message. True when it marked a session."""
        event = parse_plugin_event(raw)
        if event is not None and self.known is not None and event["session"] not in self.known():
            event = None   # not a session of this fleet: a valid id proves nothing
        if event is None or self.feed is None:
            with self.lock:
                self.counts["ignored"] += 1
            return False
        moment = self.now()
        with self.lock:
            second = int(moment)
            if self.window != second:
                self.window, self.per_session, self.total = second, {}, 0
            seen = self.per_session.get(event["session"], 0)
            over = seen >= PLUGIN_SESSION_PER_SECOND or self.total >= PLUGIN_TOTAL_PER_SECOND
            if over:
                self.counts["rate_limited"] += 1
                announce, self.limiting = not self.limiting, True
            else:
                self.per_session[event["session"]], self.total = seen + 1, self.total + 1
                self.counts["accepted"] += 1
                self.counts["plugin_dropped"] += event["dropped"]
                announce, self.limiting = False, False
        if over:
            if announce:
                self._audit("event.rate_limited", {"limit_per_session": PLUGIN_SESSION_PER_SECOND, "limit_total": PLUGIN_TOTAL_PER_SECOND})
            return False
        self.feed.touch(event["session"], moment)
        return True

    def status(self):
        with self.lock:
            return dict(self.counts)

    def _audit(self, name, detail):
        if self.audit is not None:
            try:
                self.audit.append(self.actor, name, "v2", detail)
            except OSError:
                pass


def looks_like_plugin_event(raw):
    """True for a request whose JSON object says `kind: event`: it goes to the receiver, never to the judge. Anything else is a judge request as before."""
    if not raw.lstrip().startswith(b"{") or b'"kind"' not in raw:
        return False
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(data, dict) and data.get("kind") == "event"


class PolicyDaemonError(Exception):
    """The daemon could not be reached, or did not answer in time."""


def read_judge_line(conn, limit):
    """Bytes up to a newline or EOF, at most limit + 1 of them; times out with the socket's timeout."""
    data = b""
    while len(data) <= limit and b"\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def ask_judge_daemon(state_dir, text, timeout):
    """The daemon's reply (a dict) to the request text, or PolicyDaemonError. Never reads any environment variable."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        with in_directory(state_dir):
            sock.connect(POLICY_SOCKET)
        sock.sendall(text.encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        raw = read_judge_line(sock, POLICY_STDIN_LIMIT)
    except (socket.timeout, TimeoutError):
        raise PolicyDaemonError("the daemon did not answer in time") from None
    except OSError as err:
        raise PolicyDaemonError("the daemon is not reachable (%s)" % (err.strerror or err)) from None
    finally:
        sock.close()
    try:
        reply = json.loads(raw.decode("utf-8", "replace").splitlines()[0])
    except (ValueError, IndexError):
        raise PolicyDaemonError("the daemon's answer is not JSON") from None
    if not isinstance(reply, dict) or reply.get("outcome") not in POLICY_REQUEST_OUTCOMES:
        raise PolicyDaemonError("the daemon's answer has no usable outcome")
    return reply


def never_looser(reply, configured):
    """The reply, made no looser than `configured`: the client's own guard, as the plugin has its own."""
    if POLICY_REQUEST_OUTCOMES.index(reply["outcome"]) >= POLICY_REQUEST_OUTCOMES.index(configured):
        kept = policy_reply(reply["outcome"], reply.get("reason") or "", reply.get("judged"), reply.get("cached"),
                            reply.get("latency_ms") if isinstance(reply.get("latency_ms"), int) else 0)
        if reply.get("shadow") is True:
            kept["shadow"] = True  # #113: how the plugin learns it need not wait
        return kept
    return policy_reply(configured, "the daemon answered looser than the configured outcome: ignored")


class JudgeServer:
    """The daemon's end of the judge socket: one request and one reply per connection.

    `handle(text)` returns the reply dict. The socket is $STATE/judge.sock, mode
    0600, a stale one replaced; every connection runs in its own thread, so a
    slow judgement never delays a pass or another judgement.
    """

    def __init__(self, state_dir, handle, on_event=None):
        self.state_dir, self.handle, self.on_event = state_dir, handle, on_event
        self.sock = self.thread = None
        self.slots = threading.BoundedSemaphore(POLICY_CONNECTIONS)
        self.stopping = threading.Event()

    def start(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        before = os.umask(0o177)  # the socket is born private, not chmod-ed after anyone could connect
        try:
            with in_directory(self.state_dir):
                try:
                    os.unlink(POLICY_SOCKET)
                except FileNotFoundError:
                    pass
                sock.bind(POLICY_SOCKET)
                os.chmod(POLICY_SOCKET, 0o600)
            sock.listen(POLICY_CONNECTIONS)
            sock.settimeout(0.2)
        except BaseException:
            sock.close()
            raise
        finally:
            os.umask(before)
        self.sock = sock
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(2)
        if self.sock is not None:
            self.sock.close()
            try:
                with in_directory(self.state_dir):
                    os.unlink(POLICY_SOCKET)
            except OSError:
                pass

    def _accept(self):
        while not self.stopping.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self.slots.acquire(blocking=False):
                conn.close()  # too many at once: that caller keeps its configured outcome
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(POLICY_READ_SECONDS)
            raw = read_judge_line(conn, POLICY_STDIN_LIMIT)
            text = raw.decode("utf-8", "replace")
            if self.on_event is not None and looks_like_plugin_event(raw):
                self.on_event(raw)   # a pushed fact: marks a session, no reply is written
                return
            if len(raw) > POLICY_STDIN_LIMIT:
                reply = policy_reply(_guess_configured(text), "unusable request (larger than %d bytes): no change"
                                     % POLICY_STDIN_LIMIT)
            else:
                reply = self.handle(text)
            if reply is None:
                return   # the judge is off here: the socket is only for pushed facts, and a judge request keeps its configured outcome
            conn.sendall((json.dumps(reply, sort_keys=True) + "\n").encode("utf-8"))
        except (OSError, ValueError):
            pass  # a caller that went away or sent nothing in time: nothing to answer
        finally:
            conn.close()
            self.slots.release()


def make_judge_handler(runner, now):
    """handle(text) for the daemon: parse, judge with the daemon's own key and clients, reply. Never raises."""
    def handle(text):
        try:
            request = parse_policy_request(text)
            config = load_config()  # read at each call, so an edit to `policy` takes effect without a restart
            active_faults()
            return judge_request(request, config, lambda: build_policy_judge(config, runner, now))[0]
        except PolicyRequestError as err:
            return policy_reply(_guess_configured(text), "unusable request (%s): no change" % err)
        except Exception as err:  # the judge only tightens: whatever fails, the configured outcome stands
            return policy_reply(_guess_configured(text), "the judge failed (%s: %s): the configured outcome stands"
                                % (type(err).__name__, _one_line(str(err), 120)))
    return handle


def start_judge_server(config, state, runner, now, engine=None):
    """The daemon's judge socket when `policy.enabled`, or when `v2_plugin_events` is "auto" and the event feed runs (the plugin's pushes use it), else None."""
    judging = config["policy"]["enabled"]
    feed = getattr(engine, "feed", None)
    listening = config["v2_plugin_events"] == "auto" and feed is not None
    if not judging and not listening:
        return None
    handle = make_judge_handler(runner, now) if judging else (lambda text: None)
    on_event = None
    if listening:
        def known():
            """This fleet's session ids. Read from the socket thread while the pass thread replaces the discovery: a snapshot of the values, and any
            fault means "none known", so the event is ignored (fail closed)."""
            try:
                discovery = getattr(engine, "discovery", None)
                return {o.session_id for o in list(discovery.agents.values()) if getattr(o, "session_id", None)} if discovery else set()
            except RuntimeError:
                return set()
        engine.event_receiver = EventReceiver(feed, engine.audit, now, known=known)
        on_event = engine.event_receiver.handle
    return JudgeServer(state, handle, on_event).start()


def cmd_judge_tool(args, runner, now, stdin=None):
    """Read one request, print one JSON line, exit 0 whatever happens (invariant 9).

    Decides locally only what needs no model (a read, a configured deny, the
    judge off); everything else goes to the daemon. It reads no environment
    variable of its own accord: the key is the daemon's.
    """
    text = ""
    try:
        text = (stdin or sys.stdin).read(POLICY_STDIN_LIMIT + 1)
        if len(text) > POLICY_STDIN_LIMIT:
            raise PolicyRequestError("the request is larger than %d bytes" % POLICY_STDIN_LIMIT)
        request = parse_policy_request(text)
        config = load_config()
        active_faults()
        why = policy_unjudged_reason(config, request)
        if why is not None:
            reply = policy_reply(request["configured"], why)
        else:
            wait = config["policy"]["budget_ms"] / 1000.0 + POLICY_CLIENT_SLACK
            reply = never_looser(ask_judge_daemon(state_dir_path(), text, wait), request["configured"])
    except PolicyRequestError as err:
        reply = policy_reply(_guess_configured(text), "unusable request (%s): no change" % err)
    except PolicyDaemonError as err:
        reply = policy_reply(request["configured"], "%s: the configured outcome stands" % err)
    except Exception as err:  # nothing the judge does may stop a tool call: the configured outcome stands
        reply = policy_reply(_guess_configured(text), "the judge failed (%s: %s): the configured outcome stands"
                             % (type(err).__name__, _one_line(str(err), 120)))
    try:
        print(json.dumps(reply, sort_keys=True))
        sys.stdout.flush()
    except (OSError, ValueError):
        pass  # the plugin stopped listening
    record_dropped_calls()
    record_mode_flips()
    return EXIT_OK


cmd_judge_tool.direct = True   # no error mapping, no daemon start


POLICY_LIVENESS_SECONDS = 30 * 60   # agents active and no policy.judge for this long is an alarm


def _audit_epoch(entry):
    try:
        return datetime.fromisoformat(str(entry.get("ts")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _percentile(sorted_values, fraction):
    """Nearest-rank percentile of an ascending list (None when empty)."""
    if not sorted_values:
        return None
    return sorted_values[max(0, min(len(sorted_values) - 1, int(math.ceil(fraction * len(sorted_values))) - 1))]


def policy_sample(state_dir, now, window_seconds=3600):
    """Summarise the policy.judge entries of the last `window_seconds`: calls, judged, would-ask, would-deny,
    outcomes actually enforced, and latency p50/p95 (ms). Reads the audit log only."""
    cutoff = now - window_seconds
    calls = judged = would_ask = would_deny = asked = denied = 0
    latencies, last = [], None
    for entry in _audit_tail(state_dir):
        if entry.get("event") != "policy.judge":
            continue
        at = _audit_epoch(entry)
        if at is None:
            continue
        last = at if last is None or at > last else last
        if at < cutoff:
            continue
        detail = entry.get("detail") or {}
        calls += 1
        judged += 1 if detail.get("judged") else 0
        verdict = detail.get("verdict")
        would_ask += verdict == "would-ask"
        would_deny += verdict == "would-deny"
        if not detail.get("shadow"):
            asked += detail.get("outcome") == "ask" and detail.get("configured") != "ask"
            denied += detail.get("outcome") == "deny" and detail.get("configured") != "deny"
        if isinstance(detail.get("latency_ms"), (int, float)):
            latencies.append(detail["latency_ms"])
    latencies.sort()
    return {"window_seconds": window_seconds, "calls": calls, "judged": judged, "would_ask": would_ask,
            "would_deny": would_deny, "tightened_ask": asked, "tightened_deny": denied,
            "latency_p50_ms": _percentile(latencies, 0.5), "latency_p95_ms": _percentile(latencies, 0.95),
            "last_judged_age_seconds": None if last is None else max(0, int(now - last))}


def policy_liveness(config, state_dir, active_agents, now):
    """{state: off|ok|idle|ALARM, line}: the judge must have judged something in the last 30 minutes whenever an
    agent is working. Silence with nobody working is fine; silence with agents working means the plugin is not
    forwarding (the service lacks FLEET_SWITCHBOARD_BIN) and nothing is being judged."""
    if not (config or {}).get("policy", {}).get("enabled"):
        return {"state": "off", "alarm": False, "line": "policy judge  off (policy.enabled is false)"}
    sample = policy_sample(state_dir, now, POLICY_LIVENESS_SECONDS)
    mode = "shadow" if config["policy"].get("shadow") else "enforcing"
    age = sample["last_judged_age_seconds"]
    if sample["calls"]:
        return {"state": "ok", "alarm": False, "sample": sample,
                "line": "policy judge  %s, %d call(s) judged in the last 30 min" % (mode, sample["calls"])}
    seen = "no policy.judge entry yet" if age is None else "last policy.judge %s ago" % format_age(age)
    if active_agents:
        return {"state": "ALARM", "alarm": True, "sample": sample,
                "line": "policy judge  ALARM: %d agent(s) working and no policy.judge in 30 min (%s); the fleet-hooks "
                        "plugin is probably not forwarding (is FLEET_SWITCHBOARD_BIN resolvable?)" % (active_agents, seen)}
    return {"state": "idle", "alarm": False, "sample": sample, "line": "policy judge  %s; no agent working (%s)" % (mode, seen)}


def cmd_policy_report(args, runner, now):
    """`policy-report`: a sample of the judge's work, or with --check its liveness (exit 1 on an alarm)."""
    config = load_config()
    state = state_dir_path()
    if args.check:
        report = collect_status(runner, now)
        result = report.get("policy") or {"state": "unknown", "alarm": False, "line": "policy judge  unknown"}
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(result["line"])
        return EXIT_ERROR if result["alarm"] else EXIT_OK
    sample = policy_sample(state, now(), int(args.minutes * 60))
    sample["shadow"] = bool(config["policy"].get("shadow"))
    if args.json:
        print(json.dumps(sample, sort_keys=True))
        return EXIT_OK
    p50, p95 = sample["latency_p50_ms"], sample["latency_p95_ms"]
    print("policy judge, last %g min (%s)" % (args.minutes, "shadow: recorded, not enforced" if sample["shadow"]
                                            else "enforcing"))
    print("  calls judged   %d (%d reached the model or a rule)" % (sample["calls"], sample["judged"]))
    print("  would-ask      %d    would-deny  %d   (shadow verdicts)" % (sample["would_ask"], sample["would_deny"]))
    print("  enforced       %d ask, %d deny" % (sample["tightened_ask"], sample["tightened_deny"]))
    print("  latency        p50 %s ms, p95 %s ms" % ("-" if p50 is None else p50, "-" if p95 is None else p95))
    return EXIT_OK


cmd_policy_report.direct = True   # no error mapping, no daemon start


def record_mode_flips(environ=None):
    """#113: the plugin tells the next child when a session's cached mode flipped (shadow <-> enforcing): audited as
    `policy.mode`, so the window in which a call ran under the old mode is visible."""
    environ = os.environ if environ is None else environ
    try:
        flips = [f for f in (environ.get("FLEET_SWITCHBOARD_MODE_FLIPS") or "").split(",") if f][:20]
        if flips:
            AuditLog(ensure_state_dir() / "audit.jsonl").append(
                POLICY_ACTOR, "policy.mode", "plugin", {"flips": [_one_line(f, 120) for f in flips]})
    except Exception:
        pass


def record_dropped_calls(environ=None):
    """#113: the plugin, in shadow mode, drops a call it cannot start at once (too many in flight) and tells the next
    child how many through FLEET_SWITCHBOARD_DROPPED: one audit line, so no verdict is lost silently."""
    environ = os.environ if environ is None else environ
    try:
        count = int(environ.get("FLEET_SWITCHBOARD_DROPPED") or 0)
        if count > 0:
            AuditLog(ensure_state_dir() / "audit.jsonl").append(POLICY_ACTOR, "policy.dropped", "plugin", {"count": count})
    except Exception:
        pass  # the audit is best effort here


POLICY_COMMANDS = {"judge-tool": cmd_judge_tool}


def add_policy_parsers(sub):
    report = sub.add_parser("policy-report", help="Summarise the judge's recent work from the audit log: calls, "
                                                   "would-ask/would-deny, latency p50/p95; --check is its liveness")
    report.add_argument("--minutes", type=float, default=60, help="The window to sample (default 60)")
    report.add_argument("--json", action="store_true")
    report.add_argument("--check", action="store_true", help="Liveness: exit 1 when agents are working and nothing "
                                                             "was judged for 30 minutes")
    sub.add_parser("judge-tool", help="Judge one tool call: JSON on stdin, one JSON line out, exit 0 "
                                      "(the fleet-hooks plugin runs this; the judge only tightens)")
