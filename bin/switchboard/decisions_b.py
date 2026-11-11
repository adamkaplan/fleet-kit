# ---- the sources behind the DecisionSource port. Each wraps the reader that was always called here (looked up by
# name at call time, so a replaced reader is used); a later adapter registers a class of its own.

class GitHubIssuesSource(decision_core.DecisionSource):
    name = "github"

    def __init__(self, engine):
        self.engine = engine

    def read_open(self, now):
        return decisions_from_github(self.engine, now)


class HarnessRequestsSource(decision_core.DecisionSource):
    name = "v2"

    def __init__(self, engine):
        self.engine = engine

    def read_open(self, now):
        return decisions_from_requests(self.engine, now)


class ReportsSource(decision_core.DecisionSource):
    name = "reports"

    def __init__(self, engine):
        self.engine = engine

    def read_open(self, now):
        return decisions_from_reports(self.engine, now)


# ---- #105: an order the Chief of Staff records or removes is a heads-up to the owner (the visibility half of letting it)

ORDER_NOTICES = "order-notices.json"


ORDER_NOTICES_LOCK = "order-notices.lock"


ORDER_NOTICE_TTL_SECONDS = 86400  # a heads-up about an order drops after a day, or at once when the owner acks it


@contextmanager
def order_notices_lock(state_dir):
    fd = os.open(str(Path(state_dir) / ORDER_NOTICES_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_order_notices(state_dir):
    """The recorded order heads-ups ([] for none). Raises NoticeError for a file that is not one."""
    path = Path(state_dir) / ORDER_NOTICES
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as err:
        raise NoticeError("cannot read %s: %s" % (path, err)) from None
    items = raw.get("notices") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise NoticeError("%s must hold {\"notices\": [...]}" % path)
    return [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), str) and isinstance(i.get("repo"), str)
            and isinstance(i.get("charter"), int) and _is_number(i.get("created"))]


def write_order_notices(state_dir, items):
    path = Path(state_dir) / ORDER_NOTICES
    temp = path.with_name("%s.%d.tmp" % (ORDER_NOTICES, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"notices": items}, handle, indent=1, sort_keys=True)
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


def record_order_notice(state_dir, repo, charter, action, text, caller, ordered_by, now):
    """Record that `caller` (the Chief of Staff) `action`ed (add or remove) an order on `repo#charter`. Expired ones are
    dropped here. Returns the item."""
    with order_notices_lock(state_dir):
        items = [i for i in read_order_notices(state_dir) if now - i["created"] < ORDER_NOTICE_TTL_SECONDS]
        taken = {i["id"] for i in items}
        base = "order:%s#%d.%d" % (repo.split("/")[-1], charter, int(now))
        ident, n = base, 1
        while ident in taken:
            n += 1
            ident = "%s-%d" % (base, n)
        item = {"id": ident, "repo": repo, "charter": charter, "action": action, "text": text, "from": caller,
                "ordered_by": ordered_by, "created": now}
        write_order_notices(state_dir, items + [item])
    return item


def ack_order_notice(state_dir, ident, window=None, now=None):
    """Drop one heads-up, or a whole burst when `ident` is a group id (`<first id>+<count>`). Returns True when something was dropped, False when
    nothing matched, and the string `changed:<n>` when the burst the id names now has n members (not `count`): nothing is dropped, so an order that
    joined after the owner saw the row is never dropped unseen. Groups are made the way the panel's rows are: from the unexpired orders only and
    with the same window. An old single id still drops just that one."""
    with order_notices_lock(state_dir):
        items = read_order_notices(state_dir)
        gone = {ident}
        m = _GROUP_ID.match(ident)
        if m and not any(i["id"] == ident for i in items):
            live = [i for i in items if (now if now is not None else time.time()) - i["created"] < ORDER_NOTICE_TTL_SECONDS]
            for group in order_groups(live, ORDER_BURST_MINUTES * 60 if window is None else window):
                if group[0]["id"] == m.group(1):
                    if len(group) != int(m.group(2)):
                        return "changed:%d" % len(group)
                    gone = {g["id"] for g in group}
                    break
        kept = [i for i in items if i["id"] not in gone]
        if len(kept) == len(items):
            return False
        write_order_notices(state_dir, kept)
    return True


ORDER_BURST_MINUTES = 10   # orders of the same action by the same actor on the same charter within this many minutes of the first are ONE heads-up


_GROUP_ID = re.compile(r"^(order:.+)\+([0-9]+)$")


def order_burst_seconds(config):
    value = (config or {}).get("order_notice_burst_minutes", ORDER_BURST_MINUTES)
    return 0 if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 else int(value * 60)


def order_groups(items, window):
    """[[item, ...]] oldest first: consecutive orders of the same action by the same actor on the same charter, each within `window` seconds of the
    FIRST of its group, form one group (a window of 0 groups nothing)."""
    groups, open_by_key = [], {}
    for item in sorted(items, key=lambda i: (i["created"], i["id"])):
        key = (item["repo"], item["charter"], item.get("action"), item.get("from"))
        group = open_by_key.get(key)
        if window > 0 and group is not None and item["created"] - group[0]["created"] <= window:
            group.append(item)
        else:
            group = [item]
            open_by_key[key] = group
            groups.append(group)
    return groups


def order_group_id(group):
    return group[0]["id"] if len(group) == 1 else "%s+%d" % (group[0]["id"], len(group))


def decisions_from_order_notices(engine, now):
    """(entries, problems): each unexpired order heads-up as an entry of kind `notice` (tier `human`, id `order:<repo>#N.<time>`):
    information for the owner, never a decision, never counted. Read-only: an expired one is only left out here.

    The headline LEADS WITH WHAT DIFFERS (`<repo>#<charter> order +: <text> (cos)`, `+` added, `-` removed), so the number is never what a narrow
    row cuts; the text is. A burst (orders of one action by one actor on one charter within `order_notice_burst_minutes`) is ONE entry,
    `<repo>#<charter>: 19 orders added by cos: <first words>`, whose id is the first id plus the count and whose note lists the member ids."""
    state_dir = getattr(engine, "state_dir", None)
    if state_dir is None:
        return [], []
    try:
        items = read_order_notices(state_dir)
    except NoticeError as err:
        return [], [str(err)]
    window = order_burst_seconds(getattr(engine, "config", None))
    live = [i for i in items if now - i["created"] < ORDER_NOTICE_TTL_SECONDS]
    entries = []
    for group in order_groups(live, window):
        first = group[0]
        short, sign = first["repo"].split("/")[-1], "+" if first.get("action") == "add" else "-"
        who = first.get("from") or "cos"
        text = first.get("text") or ""
        if len(group) == 1:
            headline = decision_core.fit_lines("%s#%d order %s: %s (%s)" % (short, first["charter"], sign, text, who))
            note = "not ack'd: `%s notice ack %s` drops it" % (PROG, first["id"])
        else:
            word = "added" if sign == "+" else "removed"
            headline = decision_core.fit_lines("%s#%d: %d orders %s by %s: %s" % (short, first["charter"], len(group), word, who, text))
            note = "not ack'd: `%s notice ack %s` drops all %d (members: %s)" % (PROG, order_group_id(group), len(group), ", ".join(g["id"] for g in group))
        entries.append({"id": order_group_id(group), "title": decision_core.line_of(headline, decision_core.DECISION_TITLE_MAX),
                        "headline": headline, "ask": None, "repo": first["repo"], "agent": first.get("from"),
                        "kind": "notice", "tier": "human", "since": iso(first["created"]), "number": first["charter"],
                        "url": "https://github.com/%s/issues/%d" % (first["repo"], first["charter"]),
                        "state": None, "additions": None, "deletions": None, "files": None, "ci": None,
                        "review": None, "stale": False, "note": note, "merge_after": None})
    return entries, []


class PrNoticesSource(decision_core.DecisionSource):
    name = "notices"
    entries = True

    def __init__(self, engine):
        self.engine = engine

    def read_open(self, now):
        return decisions_from_notices(self.engine, now)


class OrderNoticesSource(decision_core.DecisionSource):
    name = "order-notices"
    entries = True

    def __init__(self, engine):
        self.engine = engine

    def read_open(self, now):
        return decisions_from_order_notices(self.engine, now)


# The registry, in the order the list is built: sources of items (folded and numbered) in registration order, then
# the sources of finished entries. A later adapter appends a factory (`factory(engine)` -> a DecisionSource).
DECISION_SOURCE_REGISTRY = [GitHubIssuesSource, HarnessRequestsSource, ReportsSource, PrNoticesSource, OrderNoticesSource]


def register_decision_source(factory):
    """Add a source (a class or any `factory(engine)` returning a DecisionSource) after the ones already there."""
    DECISION_SOURCE_REGISTRY.append(factory)
    return factory


def decision_sources(engine):
    return [factory(engine) for factory in DECISION_SOURCE_REGISTRY]


def read_source(engine, source, now, errors):
    """(found, problems) of one source: a source that fails is one error entry (audited once per change) and the
    others are still listed. Every problem is appended to `errors`."""
    try:
        found, problems = source.read_open(now)
    except Exception as err:
        found, problems = [], [_one_line("%s: %s" % (type(err).__name__, err), 200)]
    for problem in problems:
        errors.append(dict({"source": source.name, "error": _one_line(problem, 200)},
                           **({"boss": problem.boss} if getattr(problem, "boss", None) else {})))
        engine._note("decisions.error", source.name, {"error": _one_line(problem, 200)})
    return found, problems


def derive_decisions(engine, now):
    """(decisions, errors): every open decision, oldest first, and what could not be read.

    Each decision is {id, title, headline, ask, repo, agent, kind, since, tier} (`headline`: what the panel shows;
    an issue's decision may add `reported_by`, the agents whose report on it was folded in; PR 18: `cos`, `human` or `orchestrator`; a
    permission request or question also names its `session` and `request`). A source that fails is an error entry and the
    others are still listed (audited once per change). Call after `engine.discover` and a fact collection."""
    items, errors = [], []
    sources = decision_sources(engine)
    for source in (x for x in sources if not x.entries):
        found, problems = read_source(engine, source, now, errors)
        items.extend(found)
    ids = assign_decision_ids(items, decision_repos(engine))
    engine.decision_bosses = {ident: item["boss"] for ident, item in zip(ids, items) if item.get("boss")}  # memory only
    engine.decision_seeds = {ident: item["seed"] for ident, item in zip(ids, items)}  # memory only: the sync engine's keys
    watched = decision_repos(engine)
    home = engine.config.get("intent_repo") or (watched[0] if len(watched) == 1 else None)  # a bare issue's repo
    decisions = decision_core.build_entries(
        items, ids, home, lambda repo, number: cached_issue_title(engine, repo, number, {}),
        tier_flat=fault("tier-flat"), known_parent=chain_parent_lookup(engine))
    for source in (x for x in sources if x.entries):  # PR notices: information rather than decisions (never folded, never counted)
        found, problems = read_source(engine, source, now, errors)
        decisions.extend(found)
    decision_core.fold_reports_into_prs(decisions)   # a report naming a `pr:` decision is that PR's question: one row, not two
    decision_core.order_decisions(decisions)
    decision_core.annotate_order(decisions)   # optional order fields, only when some decision says what it waits for
    return decisions, errors


class LocalStore(decision_core.DecisionStore):
    """What the Switchboard keeps about decisions on this machine: the projection decisions.json and the
    escalated-requests file. Today's behaviour, behind the DecisionStore port; the functions it calls are unchanged."""

    def __init__(self, engine):
        self.engine = engine

    def read(self, now):
        return decisions_report(self.engine.state_dir, now)

    def publish(self, decisions, errors, now, provisional=False):
        """Write decisions.json when the list changed (or the file is missing or unreadable), atomically, mode
        0600; otherwise touch it, so its age says the daemon is alive. Returns True when written. `provisional` (#467 slice 3: the early write of a pass that is
        still reading) is kept in the file until the end of the pass publishes the final list: a change of that flag alone is a write."""
        engine = self.engine
        path = engine.state_dir / DECISIONS_FILE
        current = read_decisions_file(path)
        if current is not None and current["decisions"] == decisions and current["errors"] == errors and (current.get("provisional") is True) == bool(provisional):
            os.utime(str(path), (now, now))
            return False
        known = current.get("generation") if current is not None else None
        same = current is not None and current["decisions"] == decisions and current["errors"] == errors   # only the provisional flag changes: the list is the same generation
        engine.decisions_generation = max(engine.decisions_generation, known if isinstance(known, int) else 0) + (0 if same and isinstance(known, int) else 1)
        body = {"generation": engine.decisions_generation, "written_at": iso(now), "daemon_pid": os.getpid(),
                "decisions": decisions, "errors": errors}
        if provisional:
            body["provisional"] = True
        temp = path.with_name("%s.%d.tmp" % (DECISIONS_FILE, os.getpid()))
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(body, handle, indent=1, sort_keys=True)
                handle.write("\n")
            os.utime(str(temp), (now, now))
            os.replace(str(temp), str(path))
        except BaseException:
            try:
                os.unlink(str(temp))
            except OSError:
                pass
            raise
        return True  # not audited: a projection of facts, and the audit records writes to other systems

    def escalated(self):
        return read_escalated(self.engine.state_dir)

    def mark_escalated(self, key, when):
        mark_escalated(self.engine.state_dir, key, when)

    def prune_escalated(self, pending):
        return prune_escalated(self.engine.state_dir, pending)


class NullExternal(decision_core.ExternalDecisionAdapter):
    """The adapter with nothing behind it: healthy, takes every push as a no-op and has no changes. What the
    Switchboard uses while no external adapter is configured, so nothing leaves the machine."""

    def health(self):
        return None

    def push(self, operation, idempotency_key):
        return {"external_id": operation.get("external_id")}

    def lookup(self, key):
        return None

    def pull_changes(self, cursor):
        return [], cursor


NULL_EXTERNAL = NullExternal()


def decision_store(engine):
    return LocalStore(engine)


def decision_external(engine):
    """The external adapter of this engine: its `external` when one was set, else the NullExternal."""
    return getattr(engine, "external", None) or NULL_EXTERNAL


# ---- external decision systems: OFF by default (external_decisions.enabled). The engine is decision_sync's; this
# is the registry of adapters, the host that gives it audit and delivery, and the call at the end of a pass.

EXTERNAL_ADAPTERS = {}


def register_external_adapter(name, factory):
    """Register an adapter under the name `external_decisions.adapter` selects: `factory(settings, mapping,
    state_dir)` returns an ExternalDecisionAdapter. `mapping` is the parsed local mapping file."""
    EXTERNAL_ADAPTERS[name] = factory
    return factory


MAPPING_ADAPTER = "mapping"   # the generic adapter's name in `external_decisions.adapter`


def build_mapping_adapter(settings, mapping, state_dir):
    """The factory of the generic adapter: built from the parsed mapping file, its key source run ONCE here (a command
    source runs now, with its own timeout, and the key is held in memory only). Any failure raises one line that
    never holds the key; the caller shows it as `down: <why>` and tries again only after a backoff."""
    module = load_external_adapter()
    try:
        adapter = module.McpRestAdapter(mapping)
        reason = adapter.start()
    except Exception as err:
        raise RuntimeError(module.scrub(_one_line("%s" % err, 160)))
    if reason:
        raise RuntimeError(module.scrub(_one_line(reason, 160), adapter.keys.secrets()))
    return adapter


register_external_adapter(MAPPING_ADAPTER, build_mapping_adapter)


def judge_inbound_answer(engine, decision, answer, role):
    """Whether an answer that came from outside may be applied, judged as the Chief of Staff's own `decisions answer`
    is: the same four questions, put to the same decision model, with the owner's standing orders as the authority.
    {"verdict": "allow" | "deny" | "unavailable", "reason": one line}.

    Only the owner's agent is judged (the project agent counts as an orchestrator's, the human as the human's: no
    orders are involved). It can only tighten: it is asked after role authority has accepted the answer. It fails
    safe: orders that are missing, empty or unusable, a judge that is not on, or a model that cannot answer is never
    an allow (the first two and the third are a deny, the last is `unavailable`, which the sync engine retries a
    bounded number of times). Nothing is written, and the model is never reached unless the judge is on."""
    if role != "owner_agent":
        return {"verdict": "allow", "reason": "no standing orders are involved for this role"}
    if fault("answer-unjudged"):
        return {"verdict": "allow", "reason": "answer-unjudged fault"}
    config = engine.config
    if not config["policy"]["enabled"] or not config.get("jev") or fault("no-policy-judge"):
        return {"verdict": "deny", "reason": "the owner's standing orders cannot be consulted: the policy judge is not "
                                             "on, so the owner's agent's answer goes to the owner"}
    try:
        orders = read_cos_orders(config)["text"]
    except CosOrdersError as err:
        return {"verdict": "deny", "reason": "the owner's standing orders cannot be used (%s)" % _one_line(str(err), 120)}
    if not orders:
        return {"verdict": "deny", "reason": "no standing orders are stated, so nothing authorises the owner's agent to "
                                             "decide this: it goes to the owner"}
    text = _one_line(answer.get("text") or "", 600)
    chosen = " --option %s" % answer["option_id"] if answer.get("option_id") else ""
    arguments = "%s decisions answer %s%s %s [the decision it acts on: %s by %s, %s tier: %s]" % (
        PROG, decision["id"], chosen, json.dumps(text), decision.get("kind"), decision.get("agent"),
        decision.get("tier"), _one_line(decision.get("title"), 300))
    arguments = redact_command_text(arguments, jev_secrets(config, os.environ))
    state = policy_state("chief-of-staff", "not stated: no ask is recorded", "not stated", orders, "shell", arguments)
    thresholds = dict(config["policy"]["thresholds"])
    thresholds["outside_intent"] = thresholds["outside_scope"] = 2.0   # the Chief of Staff has no ask: as in the judge
    try:
        capped = dict(config["jev"], timeout_seconds=min(config["jev"]["timeout_seconds"], POLICY_WARM_SECONDS))
        combined = judge_tool_call(build_jev(dict(config, jev=capped)), "allow", state, thresholds)
    except Exception as err:
        return {"verdict": "unavailable", "reason": "the judge failed: %s" % _one_line(str(err), 120)}
    if "answers" not in combined:   # judge_tool_call lets the configured outcome stand when the model gave no answer
        return {"verdict": "unavailable", "reason": _one_line(combined["reason"], 200)}
    if combined["outcome"] == "allow":
        return {"verdict": "allow", "reason": "within the owner's standing orders"}
    fired = "; ".join(POLICY_FIRED_TEXT[n] for n in combined["fired"] if n in POLICY_FIRED_TEXT)
    return {"verdict": "deny", "reason": "outside the owner's standing orders (%s): it goes to the owner" % (fired or "judge")}


class ExternalSyncHost(decision_sync.Host):
    """What the sync engine needs of the Switchboard: the audit log, and the asker's own delivery paths."""

    def __init__(self, engine):
        self.engine = engine

    def audit(self, event, subject, detail):
        if self.engine.audit is not None:
            self.engine.audit.append(self.engine.actor, event, subject, detail)

    def _runner(self):
        return self.engine.gh_runner or subprocess.run

    def _boss(self, target):
        discovery = self.engine.discovery or self.engine.discover(self.engine.now())
        found = discovery.agents.get(target.get("agent")) if target.get("agent") else None
        boss = boss_name(discovery, found) if found is not None else None
        if boss is None:
            raise DecisionError("%s: there is no running agent to tell" % (target.get("display_id") or target.get("id")))
        return boss

    def judge_answer(self, entry, intent):
        return judge_inbound_answer(self.engine, entry, intent, intent.get("answered_by"))

    def deliver_answer(self, entry, intent):
        engine, kind = self.engine, entry["kind"]
        text = _one_line(intent.get("text") or "", 600)
        role = (intent.get("answered_by") or "someone").replace("_", " ")
        said = "Answered outside by the %s%s" % (role, ": " + text if text else " (option %s)" % intent.get("option_id"))
        if kind == "report":
            outcome = ReportSendDelivery(engine).deliver(entry, {"by": self._boss(entry), "to": entry["agent"],
                                                                 "text": said, "ask": entry.get("ask"),
                                                                 "repo": entry.get("repo")})
            if not outcome.get("sent"):
                raise DecisionError("the answer was not sent to %s (%s)" % (
                    entry["agent"], outcome.get("reason") or outcome.get("action")))
        elif kind == "issue" and entry.get("rows"):
            rows = ["%s=%s" % (n, c) for n, c in sorted((intent.get("rows") or {}).items(), key=lambda kv: int(kv[0]))]
            args = types.SimpleNamespace(as_recommended=not rows, row=rows, note=said)
            answer_batch(engine, self._runner(), entry, args, engine.actor)
        elif kind == "issue":
            target = _issue_target(engine, entry)
            if target is None or target[0] is None:
                raise DecisionError("%s: cannot tell which repo its issue is in" % entry["id"])
            IssueDelivery(engine, self._runner()).deliver(entry, {
                "repo": target[0], "number": target[1], "add": (),
                "remove": [awaiting_cos_label(engine.config), awaiting_label(engine.config)],
                "author": engine.actor, "comment": said})
        elif kind == "question":
            HarnessReplyDelivery(engine).deliver(entry, {"value": text or str(intent.get("option_id") or ""),
                                                          "note": said, "by": engine.actor})
        else:
            raise DecisionError("%s is a %s: it is not answered from outside" % (entry["id"], kind))

    def deliver_note(self, target, text):
        outcome = ReportSendDelivery(self.engine).deliver(target, {
            "by": self._boss(target), "to": target.get("agent"), "text": text, "ask": target.get("ask"),
            "repo": target.get("repo")})
        if not outcome.get("sent"):
            raise DecisionError("the note was not sent to %s (%s)" % (
                target.get("agent"), outcome.get("reason") or outcome.get("action")))


def external_settings(config):
    section = (config or {}).get("external_decisions") or {}
    return section if section.get("enabled") else None


def sync_for(engine):
    """The sync engine of this daemon, built once; None while external decisions are off (nothing is called), or
    (None, reason) is never returned: a build that fails is `engine.external_sync_error`, shown as a status."""
    settings = external_settings(engine.config)
    if settings is None or engine.state_dir is None:
        return None
    held = getattr(engine, "external_sync", None)
    if held is not None:
        return held
    now = engine.now()
    if now < getattr(engine, "external_sync_retry_at", 0):
        return None   # a failed build is not tried again until its backoff has passed (a key command is not rerun per pass)
    try:
        factory = EXTERNAL_ADAPTERS.get(settings["adapter"])
        if factory is None:
            raise RuntimeError("no adapter named %r is registered" % settings["adapter"])
        try:
            mapping = json.loads(Path(settings["mapping_file"]).read_text())
        except (OSError, ValueError) as err:
            raise RuntimeError("the mapping file cannot be read: %s" % (getattr(err, "strerror", None) or "not JSON"))
        adapter = factory(settings, mapping, engine.state_dir)
        engine.external_sync = decision_sync.SyncEngine(settings, engine.state_dir, adapter, ExternalSyncHost(engine),
                                                       engine.now)
        engine.external_sync_error = None
        engine.external_sync_fails = 0
        ExternalSyncHost(engine).audit("external.started", "external", {"adapter": settings["adapter"]})
    except Exception as err:
        fails = getattr(engine, "external_sync_fails", 0) + 1
        engine.external_sync_fails = fails
        engine.external_sync_error = _one_line("%s" % err, 160)
        engine.external_sync_retry_at = now + min(settings["retry_max_seconds"],
                                                  settings["retry_base_seconds"] * 2 ** (fails - 1))
        try:
            ExternalSyncHost(engine).audit("external.down", "external", {"reason": engine.external_sync_error,
                                                                         "attempt": fails})
        except Exception:
            pass
        return None
    return engine.external_sync


def sync_external(engine, decisions, errors, now):
    """The end of a pass for a project that is switched on: mirror the list outward and take answers back. Returns
    the error entries to show (the adapter down, an outbox past its alarm). Never raises and never changes `decisions`:
    a down adapter cannot hide a local decision or stop local answering."""
    if external_settings(engine.config) is None:
        return []
    found = []
    try:
        sync = sync_for(engine)
        if sync is None:
            reason = getattr(engine, "external_sync_error", None) or "no state directory"
            _write_external_status(engine, {"state": "down", "reason": reason, "outbox": 0, "last_pull": None})
            return [{"source": "external", "error": "external decision system is down: %s" % reason}]
        status = sync.cycle(decisions, errors, getattr(engine, "decision_seeds", {}) or {},
                            getattr(engine, "decision_bosses", {}) or {})
        if status["state"] == "down":
            found.append({"source": "external", "error": "external decision system is down: %s" % status["reason"]})
        if status.get("alarm"):
            found.append({"source": "external", "error": "the outbox to the external decision system is past %d waiting "
                          "operations (none is dropped)" % engine.config["external_decisions"]["outbox_alarm_depth"]})
    except Exception as err:
        found.append({"source": "external", "error": _one_line("external decisions: %s: %s" % (type(err).__name__, err), 200)})
    return found


class EngineWorkHost(work_sync.Host):
    """What the work pump reads from this engine: the discovered fleet agents, the reports their bosses hold (the
    transcript reads the decisions already make, cached), the intent cache the daemon already holds, and the
    decision link table. Nothing here reads GitHub."""

    def __init__(self, engine):
        self.engine = engine

    def agents(self):
        discovery = self.engine.discovery
        found = []
        for name, obs in sorted((discovery.agents if discovery is not None else {}).items()):
            fleet = obs.fleet or {}
            found.append({"name": name, "role": fleet.get("role"), "reports_to": _boss_of(obs),
                          "repo": _fleet_repo(fleet, self.engine.config), "issue": _issue_of(fleet),
                          "status": obs.status, "intent": fleet.get("intent")})
        return found

    def reports(self):
        engine, discovery = self.engine, self.engine.discovery
        if discovery is None:
            return []
        since = engine.now() - engine.config["decisions_lookback_hours"] * 3600
        repos, found = decision_repos(engine), []
        for boss in sorted({boss_name(discovery, o) for o in discovery.agents.values()} - {None}):
            obs = discovery.agents.get(boss)
            if obs is None or obs.problem:
                continue
            for created, _, number, line in synthetic_lines(engine, obs.session_id, since):
                match = REPORT_LINE.match(line)
                if match:
                    sender = discovery.agents.get(match.group(1))
                    found.append({"sender": match.group(1), "issue": int(match.group(3)) if match.group(3) else None,
                                  "state": match.group(4), "text": match.group(5), "at": (created, number),
                                  "repo": repo_of_tag(match.group(2), repos)
                                  or (_fleet_repo(sender.fleet, engine.config) if sender else None)})
        source = _report_rider_source(engine)
        for item in (source.entries() if source is not None else []):
            state, _, text = item["summary"].partition(": ")
            sender = discovery.agents.get(item.get("from"))
            if item["created"] >= since and sender is not None:
                found.append({"sender": sender.name, "issue": item.get("issue"), "state": state, "text": text,
                              "at": (item["created"], 1 << 60),
                              "repo": item.get("repo") or _fleet_repo(sender.fleet, engine.config)})
        return found

    def title_of(self, repo, issue):
        return cached_issue_title(self.engine, repo, issue, {})

    def links(self):
        sync = getattr(self.engine, "external_sync", None)
        return dict(getattr(sync, "links", None) or {})

    def audit(self, event, subject, detail):
        if self.engine.audit is not None:
            self.engine.audit.append(self.engine.actor, event, subject, detail)


def work_sync_external(engine, now):
    """The end of a pass, after the decision sync: mirror worker assignments as work items when `work_items` is on and
    the adapter in use has a work sink. Never raises; nothing at all happens while either is off."""
    settings = external_settings(engine.config)
    if settings is None or not settings.get("work_items") or engine.state_dir is None:
        return
    try:
        sync = getattr(engine, "external_sync", None)
        sink = getattr(getattr(sync, "adapter", None), "work", None)
        if sink is None:
            return
        pump = getattr(engine, "work_pump", None)
        if pump is None:
            pump = engine.work_pump = work_sync.WorkSync(settings, engine.state_dir, sink, EngineWorkHost(engine), engine.now)
        pump.cycle()
    except Exception as err:
        try:
            engine._note("work.error", "pump", {"error": _one_line("%s: %s" % (type(err).__name__, err), 200)})
        except Exception:
            pass


def work_status_line(config, state_dir, now):
    """`work items: 3 open, last write 12s ago, 0 errors` (None when work items are off)."""
    settings = external_settings(config)
    if settings is None or not settings.get("work_items"):
        return None
    body = decision_sync._read_json(str(Path(state_dir) / work_sync.STATUS_FILE))
    if body is None:
        return "work items  waiting for the daemon's first pass"
    last = body.get("last_write")
    return "work items  %s open, last write %s, %s error(s)%s" % (
        body.get("open", 0), "%s ago" % format_age(now - last) if last else "never", body.get("errors", 0),
        "; ALARM: writes are piling up" if body.get("alarm") else "")


def _write_external_status(engine, status):
    try:
        decision_sync._write_json(str(Path(engine.state_dir) / decision_sync.STATUS_FILE),
                                  dict(status, written_at=engine.now()))
    except OSError:
        pass


def external_status_line(config, state_dir, now):
    """The one line `status` and `decisions` show: `external  ok; outbox N; last pull 12s ago`, or `down: why`. None
    when external decisions are off (nothing is printed then)."""
    if external_settings(config) is None:
        return None
    body = decision_sync._read_json(str(Path(state_dir) / decision_sync.STATUS_FILE))
    if body is None:
        return "external  waiting for the daemon's first pass"
    pulled = body.get("last_pull")
    text = "%s; outbox %s; last pull %s" % ("ok" if body.get("state") == "ok" else "down: %s" % body.get("reason"),
                                            body.get("outbox", 0), "%s ago" % format_age(now - pulled) if pulled else "never")
    failures = body.get("permanent_failures") or 0
    waiting = body.get("settled_waiting")
    if isinstance(waiting, int) and not isinstance(waiting, bool) and waiting > 0:   # decided outside, answer not here yet: never invisible if it never arrives
        oldest = body.get("settled_oldest")
        text += "; settled outside, waiting for its answer: %d%s" % (waiting, " (oldest %s)" % format_age(max(0, now - oldest)) if isinstance(oldest, (int, float)) and not isinstance(oldest, bool) else "")
    return ("external  " + text + ("; %d permanent failure(s)" % failures if failures else "")
            + ("; ALARM: outbox is deep" if body.get("alarm") else ""))
