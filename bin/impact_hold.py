"""The code that decides who may clear, hold or release an impact hold (ask #532, moved unchanged out of bin/fleet-switchboard).

What is here: the clear command and its core (`impact_clear_core`, `cmd_impact_clear`, `neutralise_marker`, the notice to the lane), the daemon's application and release of
the hold (`apply_impact_hold`, `release_impact_hold`, `impact_plan`, the carry decision), the gate's floor fact (`gate_floor_fact`) and the reading and writing of the impact
state. It is listed in docs/review-laws/impact-matrix.json as a top tier path, so a change to it holds for the owner by the floor: the reason it is not inside the daemon file,
which no path rule can single out.

It is a MOVE, not a rewrite: `bind(namespace)` gives each function the globals of bin/fleet-switchboard (the very dict they ran in before), so every helper, constant and
test patch resolves exactly as it did, and puts the functions back in that namespace under their old names. The daemon's registration block (search `impact_hold`) calls
it once; a module that is missing or fails to load stops the daemon loudly (never a daemon without the rule). The functions below are therefore written against the
daemon's globals and are not meant to be called from this module's own namespace. Python 3.9 standard library only.
"""
import types

MOVED = ('rating_at',
         'rehold_reason',
         'gate_floor_fact',
         'read_impact_state',
         'write_impact_state',
         '_impact_state_noted',
         'impact_carry_decision',
         'apply_impact_hold',
         '_consume_record',
         '_labels_ours',
         '_apply_impact_hold_locked',
         'clear_fingerprint',
         'marker_edit',
         'neutralise_marker',
         '_plan_issue_of',
         '_lane_of_issue',
         'clear_lane',
         '_notify_clear_body',
         'notify_clear',
         'authors_hold_labels',
         'impact_clear_core',
         'cmd_impact_clear',
         'cmd_impact_delegated',
         'delegated_clear_checks',
         'delegated_clear_pr_checks',
         'delegated_clear_score',
         'delegated_clear_map',
         '_gh_json',
         'impact_plan',
         'cmd_impact_hold',
         'release_impact_hold',)




def rating_at(engine, pr, head):
    """(rated, problem, entries): THE one reading of "an impact rating is recorded at this head" (#830), for every reader of "is this pull request held": True only when the independent reviewer's
    newest record for EXACTLY this head carries an action (a record with no action rates nothing); `problem` is what is wrong when the ledger cannot be read (the rating is then unknown: not rated)."""
    if engine.state_dir is None or not head:
        return False, None, []
    entries, problem = impact_rating_entries(engine.state_dir, pr, head)   # the ONE filter of "the independent reviewer's records of this pull request at exactly this head" (#835: the delegated clear reads the same `entries`)
    newest = entries[-1] if entries and not problem else None
    return (newest is not None and isinstance(newest.get("impact_action"), str) and bool(newest.get("impact_action"))), problem, ([] if problem else entries)


def rehold_reason(engine, state, repo, pr, head):
    """(#830) THE one rule for "held again at a new head", read by every place that asks whether a pull request is held (the daemon's label pass, the release, the dry run, gate G6
    and G6b): the reason (a string) when a clear is recorded for ANOTHER head and none for this one, the clear is not CARRIED to this head (a pure merge of main) and no impact rating
    exists at this head (`rating_at`: the one reading of that, whatever the caller; it also returns the records, which the delegated clear (#835) scores); else None. A pull request with no recorded clear is None: this rule never holds what was never cleared. `state` is the impact state read whole
    (never an empty one made by a failed read: the callers check the problem first). An unreadable head (none) counts as another head."""
    cleared = (state.get("clears") or {}).get(pr)
    if not isinstance(cleared, dict) or not cleared.get("head") or (head and cleared.get("head") == head):
        return None
    carried = (state.get("carried") or {}).get(pr) or {}
    if head and carried.get("head") == head and carried.get("from") == cleared.get("head") and impact_floor.carry_enabled(engine.config, repo):
        return None
    if head and rating_at(engine, pr, head)[0]:
        return None
    return "the owner's clear was recorded for an earlier head and nothing at this head clears or rates it: held again"   # a literal: the moved functions see only the daemon's globals


def gate_floor_fact(engine, state, repo, number, head):
    """The impact floor read FRESH for this head, for gate G6 (#488): {"decision", "reasons", "cleared", "unreadable"}. The SAME function the
    daemon uses (`pr_floor_report`), one PR read; the owner's clear counts only for this exact head. Any failure is `unreadable`, never
    "no hold": G6 does not pass on a floor it could not read."""
    try:
        report = pr_floor_report(engine, repo, number)
        if not head or report.get("head") != head:
            raise ValueError("the pull request moved while its floor was read (%s, then %s)" % ((head or "?")[:8], (report.get("head") or "?")[:8]))
        clears, problem = read_impact_state(state)
        cleared = (clears["clears"].get(report["pr"]) or {}).get("head") == head
        return {"decision": report["owner_decision"], "reasons": list(report["owner_reasons"]), "cleared": cleared, "unreadable": problem,
                "rehold": None if problem else rehold_reason(engine, clears, repo, report["pr"], head)}   # the OWNER-HOLD decision: the one the daemon's labels follow (#451)
    except Exception as err:   # an unreadable diff, a matrix error, a missing file: all the same answer
        return {"decision": None, "reasons": [], "cleared": False, "rehold": None, "unreadable": _one_line("%s: %s" % (type(err).__name__, err), 150)}


def read_impact_state(state_dir):
    """(state, problem): {"clears", "applied"} and None, or an EMPTY state and what is wrong when the file is corrupt, not an object or over
    IMPACT_STATE_MAX_BYTES. A missing file is an empty state with no problem. A corrupt file is REPORTED, never silently read as empty."""
    path = Path(state_dir) / IMPACT_STATE_FILE
    empty = {"clears": {}, "applied": {}}
    try:
        with open(str(path), "rb") as handle:
            data = handle.read(IMPACT_STATE_MAX_BYTES + 1)
    except FileNotFoundError:
        return empty, None
    except OSError as err:
        return empty, "%s cannot be read: %s" % (IMPACT_STATE_FILE, err.strerror or err)
    if len(data) > IMPACT_STATE_MAX_BYTES:
        return empty, "%s is over %d bytes" % (IMPACT_STATE_FILE, IMPACT_STATE_MAX_BYTES)
    try:
        raw = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError):
        return empty, "%s is not valid JSON" % IMPACT_STATE_FILE
    if not isinstance(raw, dict):
        return empty, "%s is not an object" % IMPACT_STATE_FILE
    found = {k: raw[k] if isinstance(raw.get(k), dict) else {} for k in ("clears", "applied")}
    for extra in ("posted", "matrix_error", "carried", "carry_void"):   # only when the file has them: a state file that never had them reads exactly as it always did
        if isinstance(raw.get(extra), dict):
            found[extra] = raw[extra]   # posted, matrix_error: slice 3 (#451); carried, carry_void: the carry (#510)
    return found, None


def write_impact_state(state_dir, data):
    path = Path(state_dir) / IMPACT_STATE_FILE
    temp = path.with_name("%s.%d.tmp" % (IMPACT_STATE_FILE, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temp), str(path))


def _impact_state_noted(engine, problem):
    """A corrupt, oversized or non-object state file is QUARANTINED, never overwritten: it is renamed aside (`impact-hold.json.corrupt-<time>`) so
    the clears in it can be recovered by hand, the pass starts from an empty state (fails toward hold) and the audit says so. Called under the lock."""
    if not problem:
        return
    path = Path(engine.state_dir) / IMPACT_STATE_FILE
    aside, failed = None, None
    if path.exists():
        aside = path.with_name("%s.corrupt-%d-%d" % (IMPACT_STATE_FILE, int(engine.now()), os.getpid()))
        for n in range(1, 100):   # never replace an earlier quarantined copy: the clears in it are what the quarantine keeps
            if not aside.exists():
                break
            aside = path.with_name("%s.corrupt-%d-%d-%d" % (IMPACT_STATE_FILE, int(engine.now()), os.getpid(), n))
        try:
            os.rename(str(path), str(aside))
        except OSError as err:
            aside, failed = None, "%s" % (err.strerror or err)
    if engine.audit is not None:
        engine._note("impact.state_unreadable", None, {"why": problem, "quarantined": aside.name if aside else None, "quarantine_failed": failed})
    if failed:   # the corrupt file is still there: writing now would overwrite it, so this write waits and is retried
        raise RuntimeError("the corrupt %s could not be set aside (%s): nothing was written" % (IMPACT_STATE_FILE, failed))


def impact_carry_decision(engine, repo, number, head):
    """None when no carry is to be tried (the switch is off, no clear with a fingerprint exists for this PR, or this head was already decided), else the verdict of
    `clear_carry.decide` for the NEW head `head`. Reads GitHub, so it is called WITHOUT the impact-state lock. Never raises."""
    try:
        if not impact_floor.carry_enabled(engine.config, repo):
            return None
        pr = "%s#%d" % (repo, number)
        state, _ = read_impact_state(engine.state_dir)
        cleared = state["clears"].get(pr) or {}
        if not cleared.get("head") or cleared.get("head") == head:
            return None
        if (state.get("carried", {}).get(pr) or {}).get("head") == head or (state.get("carry_void", {}).get(pr) or {}).get("head") == head:
            return None   # decided once per new head: no repeated reads and no repeated audit lines
        runner = getattr(engine, "gh_runner", None) or subprocess.run
        return load_module("clear_carry").decide(impact_carry_get(engine, runner, repo), repo, number, cleared["head"], cleared.get("fingerprint"), head)
    except Exception as err:
        engine._note("impact.carry_error", "%s#%d" % (repo, number), {"error": _one_line("%s: %s" % (type(err).__name__, err), 160)})
        return None


def apply_impact_hold(engine, repo, number, report, record):
    """Write the hold: add `<user>:merge-captain` and `<user>:hold-impact` to the PR, only those missing, never remove anything. Skipped
    when the owner cleared THIS head, or when his clear of an earlier head CARRIES to this one (a pure merge of main, `impact_hold.carry_clear`, off by default: see
    bin/clear_carry.py). Returns the number of gh calls spent. The labels are ensured once per repo per process."""
    carry = impact_carry_decision(engine, repo, number, report["head"])   # reads GitHub: before, and outside, the lock
    with impact_state_lock(engine.state_dir):   # the clear check, the label write and the record are ONE step against an owner clear
        spent = _apply_impact_hold_locked(engine, repo, number, report, record, carry)
    if carry is not None and carry.get("carried"):   # told once the carry is RECORDED, outside the lock (a send is slow)
        try:
            recorded = read_impact_state(engine.state_dir)[0].get("carried", {}).get("%s#%d" % (repo, number)) or {}
            if recorded.get("head") == report["head"]:
                notify_clear(engine, "carry", repo, number, report["head"], previous=carry["from"], body=(record or {}).get("body"))
        except Exception as err:   # the carry is recorded: nothing here may fail the pass
            engine._note("gate.clear_notify_failed", "%s#%d" % (repo, number), {"error": _one_line("%s: %s" % (type(err).__name__, err), 160)})
    return spent


def _consume_record(state, pr, clear=None):
    """Drop the daemon's record of the labels it added to `pr` when an owner's clear (or the revoke of one, or the void of a carry) makes it stale: the clear took those
    labels off, so whatever is on the pull request afterwards is somebody else's. Called in the SAME locked write as the change. With `clear` given (a revoke, a void), a record
    that is NEWER than that clear (the daemon re-applied after it) is kept: those labels are the daemon's. With none (a new clear), the record is always consumed."""
    ours = state["applied"].get(pr)
    if not isinstance(ours, dict):
        return
    if clear is not None and isinstance(ours.get("at"), int) and isinstance(clear.get("at"), (int, float)) and ours["at"] > clear["at"]:
        return
    del state["applied"][pr]


def _labels_ours(names, state, pr):
    """The labels this daemon added to `pr`, from its own record in impact-hold.json: [] when it has none, or when the owner's clear came after it (the clear took
    them off: whatever is there now is somebody else's). A record written before the list existed (`merge_label_added` only) is read as the hold label plus the
    merge label when it says it added that one: the hold label is the fleet's own."""
    ours = state["applied"].get(pr)
    if not isinstance(ours, dict):
        return []
    clear = state["clears"].get(pr)
    if isinstance(clear, dict) and not (isinstance(ours.get("at"), int) and isinstance(clear.get("at"), (int, float)) and clear["at"] < ours["at"]):
        return []
    listed = ours.get("added")
    if isinstance(listed, list):
        return [l for l in listed if l in (names["hold-impact"], names["merge-captain"])]
    return [names["hold-impact"]] + ([names["merge-captain"]] if ours.get("merge_label_added") is True else [])


def _apply_impact_hold_locked(engine, repo, number, report, record, carry=None):
    pr, head = report["pr"], report["head"]
    state, problem = read_impact_state(engine.state_dir)
    _impact_state_noted(engine, problem)   # fails toward hold: a state file nobody can read hides no hold, and the owner can clear again
    cleared = state["clears"].get(pr) or {}
    if cleared.get("head") == head:
        return 0
    carried_before = state.get("carried", {}).get(pr) or {}
    if carried_before.get("head") == head and cleared.get("head") == carried_before.get("from") and impact_floor.carry_enabled(engine.config, repo):
        return 0   # carried already for this head (and the owner has not switched the carry off since)
    if carry is not None and cleared.get("head") == carry["from"]:
        if carry["carried"]:
            state.setdefault("carried", {})[pr] = {"head": head, "from": carry["from"], "fingerprint": carry["fingerprint"], "at": int(engine.now())}
            state.setdefault("carry_void", {}).pop(pr, None)
            write_impact_state(engine.state_dir, state)
            engine.audit.append(engine.actor, "gate.clear_carried", pr, {"head": head, "from": carry["from"], "fingerprint": carry["fingerprint"],
                                                                         "why": "only main was merged in: the pull request's own change is the one the owner cleared"})
            return 0
        state.setdefault("carry_void", {})[pr] = {"head": head, "reason": _one_line(carry["reason"] or "", 200)}
        _consume_record(state, pr, cleared)   # the clear took the daemon's labels off: an older record of them is no longer a claim
        write_impact_state(engine.state_dir, state)
        engine.audit.append(engine.actor, "gate.clear_carry_void", pr, {"head": head, "from": carry["from"], "reason": _one_line(carry["reason"] or "", 200)})
    names = label_names(engine.config)
    raw = read_pr_json(engine, repo, number)   # the labels NOW, under the lock: the record claims only what this write adds, never a label that was already there
    live = raw.get("labels")
    if not isinstance(live, list):
        raise NoticeError("the pull request's labels cannot be read")
    have = {(l.get("name") if isinstance(l, dict) else l) for l in live}
    want = [names["merge-captain"], names["hold-impact"]]
    if report.get("rehold"):   # the hold labels the owner's clear took off (#830): each is put back, and only an owner-tier hold label of the fleet
        allowed = {name for key, name in names.items() if key.startswith("hold-")}
        want += [l for l in (cleared.get("removed") or []) if l in allowed and l not in want]
    missing = [l for l in want if l not in have]
    if not missing:
        return 1
    runner = getattr(engine, "gh_runner", None) or subprocess.run
    spent = 1
    ensured = engine.__dict__.setdefault("impact_labels_ensured", set())
    if repo not in ensured:
        ensure_labels(engine, runner, repo)
        ensured.add(repo)
        spent += len(LABEL_SPECS)
    change_issue_labels(engine, runner, repo, number, add=missing)
    spent += 1
    added = sorted(set(_labels_ours(names, state, pr)) | set(missing))   # a re-apply MERGES into what an earlier apply added: it never forgets one
    state["applied"][pr] = {"head": head, "merge_label_added": names["merge-captain"] in added, "added": added, "at": int(engine.now())}
    write_impact_state(engine.state_dir, state)
    engine.audit.append(engine.actor, "gate.hold_applied", pr, {"head": head, "labels": missing, "reasons": report["reasons"],
                                                                 "floor_paths": report["floor_paths"][:20], "globs": report["globs"][:20]})
    if report.get("rehold"):   # said once per head: the labels were missing, so this is the pass that put them back
        engine.audit.append(engine.actor, "gate.hold_reapplied", pr, {"head": head, "cleared_head": cleared.get("head"), "labels": missing, "why": report["rehold"]})
    return spent


def clear_fingerprint(engine, runner, repo, number, head):
    """(fingerprint, problem): the fingerprint of the pull request's own change at `head`, stored WITH a clear so a later pure merge of main can carry it (bin/clear_carry.py).
    Only when `impact_hold.carry_clear` is on (the default is off: a clear then records nothing new). The ONE function `clear_carry.own_change_fingerprint` computes it, here and at the
    carry. A change that cannot be read in full, or a head that moved while it was read, records NO fingerprint (so nothing carries) and never stops the clear."""
    if not impact_floor.carry_enabled(engine.config, repo):
        return None, None
    cc = load_module("clear_carry")
    try:
        fingerprint, fp_head, _ = cc.own_change_fingerprint(impact_carry_get(engine, runner, repo), repo, number)
    except cc.CarryVoid as err:
        return None, _one_line(str(err), 200)
    except Exception as err:
        return None, _one_line("%s: %s" % (type(err).__name__, err), 200)
    if fp_head != head:
        return None, "the head moved while its change was being read"
    return fingerprint, None


def marker_edit(body):
    """(new body, line before, line after) when the body's FIRST non-empty line says `MERGE: captain` (the very rule `pr_marker` reads), with ONLY that marker changed to
    `MERGE: automatic` and every other byte of the body (the rest of the line and its ending included) as it was. None when the first line is anything else."""
    if pr_marker(body) != "captain":
        return None
    lines = body.splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines) if line.strip())
    line = lines[index]
    at = line.index("MERGE: captain")
    edited = line[:at] + "MERGE: automatic" + line[at + len("MERGE: captain"):]
    lines[index] = edited
    return "".join(lines), line.rstrip("\r\n"), edited.rstrip("\r\n")


def neutralise_marker(engine, runner, repo, number, head, first=None):
    """#522: the owner's clear lifts the whole hold, so the body's first line `MERGE: captain` becomes `MERGE: automatic` (the gate reads it while no merge label says otherwise).
    The ONE place it is done, called by `impact_clear_core` (the owner's command and his words in the chat both end there; a carry of a clear never does). The body is read fresh
    for `head` only (`first` is the clear's own read of that moment, or None to read one), read again right before the write and written only when it is the same, then read back. Best effort (GitHub has no conditional body write, so an edit landing between the last read and the write is not detected). Never raises: a failure is audited and a line says the marker remains,
    for the owner's clear has already stood. Returns the lines to print."""
    pr = "%s#%d" % (repo, number)
    remains = "marker line remains: the first line of the body of %s still says MERGE: captain; edit it by hand"

    def failed(why):
        try:
            engine.audit.append(engine.actor, "gate.clear_marker_failed", pr, {"head": head, "why": _one_line(str(why), 200)})
        except Exception:   # an audit trouble never undoes or fails the clear
            pass
        return [remains % pr + " (%s)" % _one_line(str(why), 120)]

    try:
        first = first if first is not None else read_pr_json(engine, repo, number)
        if first.get("headRefOid") != head:
            return []   # not the head the owner cleared: nothing here is his clear's
        edit = marker_edit(first.get("body"))
        if edit is None:
            return []
        new_body, before, after = edit
        before_text, after_text = (re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", t) for t in (before, after))   # the author's text: never a control character into the terminal or the log
        again = read_pr_json(engine, repo, number)
        if again.get("body") != first.get("body") or again.get("headRefOid") != head:
            return failed("the body or the head changed while it was being edited: nothing was written")
        _gh_write(engine, runner, repo, "repos/%s/pulls/%d" % (repo, number), "PATCH", {"body": new_body})
        if read_pr_json(engine, repo, number).get("body") != new_body:
            return failed("the body read back is not the one written")
        said = ["changed the first line of the body of %s from %s to %s" % (pr, _one_line(before_text, 80), _one_line(after_text, 80))]
        try:
            engine.audit.append(engine.actor, "gate.clear_marker", pr, {"head": head, "before": _one_line(before_text, 200), "after": _one_line(after_text, 200)})
        except Exception:   # the body is already changed and the clear stands: say what is true
            said.append("the audit line of that change could not be written")
        return said
    except Exception as err:   # NoticeError, IntentError, anything: the clear stands
        return failed(err)


def _plan_issue_of(ref, repo):
    """The issue number a plan reference names in `repo` (`owner/repo#N` or a github issue or pull URL, a comment fragment allowed), or None (another repository, anything else)."""
    ref = (ref or "").strip()
    short = _PLAN_REF_SHORT.match(ref)
    if short:
        return int(short.group(2)) if short.group(1) == repo else None
    link = _PLAN_ISSUE_RE.search(ref) if ref.startswith("https://github.com/") else None
    return int(link.group(2)) if link and link.group(1) == repo else None


def _lane_of_issue(engine, repo, issue, orchestrators):
    """("one", name) | ("many", [names], charter) | None: the running orchestrator of the charter that holds `issue` AS IT IS NOW. Each parent is read fresh (a moved ask counts under its current charter), at most 3
    hops; the nearest number in the chain that a running orchestrator owns (its `issue` or `charter`) decides; two or more on that number are NEVER picked between."""
    reader = getattr(engine, "intents", None)
    if reader is None or not orchestrators:
        return None
    chain, current = [issue], issue
    try:
        for _ in range(3):
            if hasattr(reader, "invalidate"):
                reader.invalidate(current, repo)
            current = reader.parent(current, repo)
            if current is None:
                break
            chain.append(current)
    except IntentError:
        pass   # the chain so far is all that can be told
    default_repo = engine.config.get("intent_repo") if len(getattr(engine, "watches", ()) or ()) > 1 else None
    for number in chain:
        owners = []
        for name, agent in sorted(orchestrators.items()):
            fleet = agent.fleet or {}
            theirs = fleet.get("repo") if isinstance(fleet.get("repo"), str) else default_repo
            if repo and theirs and theirs != repo:
                continue
            owned = set()
            for field in ("issue", "charter"):
                value = fleet.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    owned.add(value)
                elif isinstance(value, str) and value.strip().lstrip("#").isdigit():
                    owned.add(int(value.strip().lstrip("#")))
            if number in owned:
                owners.append(name)
        if len(owners) == 1:
            return ("one", owners[0])
        if len(owners) > 1:
            return ("many", owners, number)
    return None


def clear_lane(engine, repo, number, body):
    """(lane, how, extra): the orchestrator that merges the pull request; (None, None, extra) when it cannot be told: never a guess. Two sources, each only when resolvable:
    (1) the PLAN of the pull request, taken through `authoritative_plan` (the function the gates use: the PR body's PLAN line, else the first ledger entry's plan) and only when it PASSES the plan
    check at this moment, up to the charter that holds it as it is now (parents read fresh) and that charter's ONE running orchestrator; (2) the agent that recorded the pull request's notice (its boss
    when it is a worker). When both resolve and AGREE the lane is told; when they DISAGREE neither is (`extra["unclear"]` = [plan lane, notice lane]); when one resolves it is used; none is unknown.
    A charter with two or more running orchestrators is never picked between (`extra["candidates"]`); one nobody runs counts as no plan lane."""
    discovery = engine.discover(engine.now())
    agents = discovery.agents
    orchestrators = {name: agent for name, agent in agents.items() if _is_orchestrator(agent)}
    pr = "%s#%d" % (repo, number)
    extra, plan_lane = {}, None
    if engine.state_dir is not None:
        try:
            entries = plan_ledger.read_ledger(engine.state_dir)[0]
            source, _where = authoritative_plan(entries, pr, body)
            issue = _plan_issue_of(source, repo) if source else None
            if issue is not None and checked_plan(engine.config, source, None, getattr(engine, "gh_runner", None) or subprocess.run, engine.now).get("result") == "pass":
                decided = _lane_of_issue(engine, repo, issue, orchestrators)
                if decided and decided[0] == "one":
                    plan_lane = decided[1]
                elif decided and decided[0] == "many":
                    extra["candidates"], extra["charter"] = decided[1], decided[2]
                    return None, None, extra
        except Exception:   # an unreadable ledger or plan is no plan lane: the notice decides
            plan_lane = None
    notice_lane = None
    item = next((i for i in read_pr_notices(engine.state_dir) if i["repo"] == repo and i["number"] == number), None) if engine.state_dir is not None else None
    for name in (notice_senders(item)[::-1] + ([item["boss"]] if isinstance(item.get("boss"), str) else [])) if item else []:
        agent = agents.get(name)
        if agent is None:
            continue
        boss = name if _is_orchestrator(agent) else boss_name(discovery, agent)
        if boss in orchestrators:
            notice_lane = boss
            break
    if plan_lane and notice_lane and plan_lane != notice_lane:
        extra["unclear"] = [plan_lane, notice_lane]
        return None, None, extra
    if plan_lane:
        return plan_lane, "the pull request's plan" + (" and the notice" if notice_lane else ""), extra
    return notice_lane, "the notice" if notice_lane else None, extra


def _notify_clear_body(engine, abandoned, kind, repo, number, head, reason=None, body=None, previous=None, under_order=None):
    """(the work of `notify_clear`, run on its own thread; `abandoned` is set when the caller gave up waiting: then nothing more is audited) #541: tell the Chief of Staff, and the orchestrator whose lane merges the pull request, at once that the owner cleared it (`kind` "clear": his command or his words in the chat, called
    ONCE from `impact_clear_core`) or that his clear carried to a new head (`kind` "carry": from `previous`, called once from the carry decision). One line. Once per pull request, head and kind (a
    small private file). The caller has already RECORDED the clear; nothing here can undo it: a failure is audited and swallowed. An unknown lane says `lane unknown` and only the Chief of Staff is told:
    nobody is guessed. Returns the audit detail it wrote, or None when nothing was sent."""
    pr = "%s#%d" % (repo, number)
    try:
        if engine.state_dir is None or engine.audit is None:
            return None
        key = "%s@%s:%s" % (pr, head, kind)
        with impact_state_lock(engine.state_dir):   # the command and the daemon are two processes: the check and the mark are one step (the send is outside it)
            seen = _read_small(engine.state_dir, CLEAR_NOTIFIED_FILE)
            if key in seen:
                return None
            seen[key] = {"at": int(engine.now())}
            if len(seen) > CLEAR_NOTIFIED_MAX:
                for old in sorted(seen, key=lambda k: seen[k].get("at", 0))[:len(seen) - CLEAR_NOTIFIED_MAX]:
                    del seen[old]
            _write_small(engine.state_dir, CLEAR_NOTIFIED_FILE, seen)   # before the send: at most once, even when the send fails
        lane, how, extra = clear_lane(engine, repo, number, body)
        unknown = ("lane unclear: plan lane %s, notice by %s" % tuple(extra["unclear"]) if extra.get("unclear") else "lane unknown") + (": charter #%s has %d orchestrators: %s" % (extra.get("charter"), len(extra["candidates"]), ", ".join(extra["candidates"])) if extra.get("candidates") else "")
        if kind == "carry":
            line = "owner's clear carried %s from %s to %s; %s" % (pr, (previous or "?")[:8], head[:8], "%s merges it" % lane if lane else unknown)
        else:
            flat = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", re.sub(r"[\n\r\t]", " ", reason or ""))   # line breaks become spaces, other control characters vanish BEFORE the redaction: a token cannot hide behind one
            words = " ".join(redact_command_text(flat).split())
            if under_order:   # #661: the same line, naming the Chief of Staff as the clearer and the owner's order
                scored = " (score %s, threshold %s)" % (under_order.get("score"), under_order["threshold"]) if under_order.get("threshold") else ""
                line = "%s cleared %s at %s under the owner's standing order %s%s: \"%s\"; %s" % (under_order.get("by") or "the chief of staff", pr, head[:8], under_order["number"], scored, plan_ledger.clip(words, 300), "%s merges it" % lane if lane else unknown)
            else:
                line = "owner cleared %s at %s: \"%s\"; %s" % (pr, head[:8], plan_ledger.clip(words, 300), "%s merges it" % lane if lane else unknown)
        sent, failed = [], []
        for name in ["chief-of-staff"] + ([lane] if lane else []):
            try:
                outcome = engine.send("switchboard", name, line)
                if isinstance(outcome, dict) and outcome.get("sent") is False:
                    failed.append("%s: %s" % (name, _one_line(str(outcome.get("reason") or outcome.get("action") or "not delivered"), 100)))
                else:
                    sent.append(name)
            except Exception as err:   # a send that fails never undoes the clear
                failed.append("%s: %s" % (name, _one_line(str(err), 100)))
        detail = {"head": head, "kind": kind, "lane": lane, "lane_from": how, "sent": sent}
        if extra.get("candidates"):
            detail["candidates"] = extra["candidates"]
        if extra.get("unclear"):
            detail["lane_unclear"] = extra["unclear"]   # the plan's lane and the notice sender's lane differ: neither is told to merge
        if failed:
            detail["failed"] = failed
        if not abandoned.is_set():
            engine.audit.append(engine.actor, "gate.clear_notified" if sent else "gate.clear_notify_failed", pr, detail)
        return detail
    except BaseException as err:   # on this thread nothing may escape, an exit or an interrupt inside a send included
        try:
            if not abandoned.is_set():
                engine.audit.append(engine.actor, "gate.clear_notify_failed", pr, {"head": head, "kind": kind, "error": _one_line("%s: %s" % (type(err).__name__, err), 160)})
        except Exception:
            pass
        return None


def notify_clear(engine, kind, repo, number, head, reason=None, body=None, previous=None, under_order=None):
    """#541: the notice of an owner clear (or of a carry), LAST, after everything else of it is done and bounded by a deadline: the work (lane lookup, sends) runs on its own thread and the
    caller waits at most NOTIFY_CLEAR_DEADLINE_SECONDS. On a timeout, an interrupt or an exception it gives up, audits `gate.clear_notify_failed` once with the reason, and returns None: the clear's
    result, exit status and output are exactly as without a notice. (A send still running when it gives up is left to finish on its daemon thread; it audits nothing more.)"""
    pr = "%s#%d" % (repo, number)
    abandoned, result = threading.Event(), []

    def work():
        result.append(_notify_clear_body(engine, abandoned, kind, repo, number, head, reason, body, previous, under_order))
    worker = threading.Thread(target=work, name="clear-notice", daemon=True)
    why = None
    try:
        worker.start()
        worker.join(NOTIFY_CLEAR_DEADLINE_SECONDS)
        if worker.is_alive():
            why = "timed out after %gs" % NOTIFY_CLEAR_DEADLINE_SECONDS
    except BaseException as err:   # an interrupt while waiting: the clear is already complete
        why = "interrupted (%s)" % type(err).__name__
    if why is None:
        return result[0] if result else None
    abandoned.set()
    try:
        engine.audit.append(engine.actor, "gate.clear_notify_failed", pr, {"head": head, "kind": kind, "error": "notice not sent: " + why})
    except BaseException:
        pass
    return None


def authors_hold_labels(names, raw, already):
    """The fleet's own hold labels the PR in `raw` carries now and `already` does not name: `merge-captain` and every `hold-<reason>` of the fleet's prefix, by exact name. Nothing is
    guessed from a substring or a prefix: only those exact names, only what the read shows (so the audit names what was really on the PR), never a label of another prefix. #742."""
    ours = [name for key, name in names.items() if key == "merge-captain" or key.startswith("hold-")]   # the fleet's label KEYS (a test pins this set to CAPTAIN_HOLD_LABELS + merge-captain), each with the user prefix
    live = raw.get("labels")
    have = {(l.get("name") if isinstance(l, dict) else l) for l in live} if isinstance(live, list) else set()
    return [label for label in ours if label in have and label not in already]


def impact_clear_core(engine, runner, repo, number, who, reason, remove=None, head=None, via=None, verify=None, under_order=None):
    """The owner's clear: record it head-bound (the daemon does not re-add the hold for that head) and remove `hold-impact`, plus (#742) every other
    hold label of the fleet's prefix the PR carries at that head (`merge-captain` and each `hold-<reason>`, whoever added it). A DELEGATED clear (`under_order`)
    keeps the narrower rule: only `hold-impact` and a `merge-captain` the switchboard added come off, and it is refused (audited, nothing written) when the PR
    carries a `merge-captain` the switchboard did not add. Returns the lines to print.

    `remove` (the chat clear, #457) names the labels to take off instead: the caller has already read the PR and checked that each is an owner-tier hold label or the captain
    marker; here every one of them is checked again against the owner-tier set, so no other label can ever be removed. `head` is the head the owner was shown: the clear
    is refused when the PR's head is any other. `verify(raw)` (the chat clear's) is run on the PR this call just read, before anything is written, and raises NoticeError to refuse:
    the check and the effect use ONE read. `via` is said in the audit line. The head-bound clear state is recorded for any clear, even one that removes no impact label: it only
    stops the daemon adding its impact hold for that head. Nothing here ever ADDS a label."""
    pr = "%s#%d" % (repo, number)
    if engine.audit is None:   # a clear is a logged act: with no audit sink it is refused, loudly, before anything changes
        raise RuntimeError("no audit sink: an impact clear is refused rather than made unlogged")
    raw = read_pr_json(engine, repo, number)
    current = raw.get("headRefOid")
    if not isinstance(current, str) or not current:
        raise NoticeError("the PR's head is unknown")
    if head is not None and current != head:
        raise NoticeError("the PR's head moved since the owner was shown it (%s, not %s): nothing was cleared" % (current[:8], head[:8]))
    if verify is not None:
        verify(raw)
    fingerprint, fingerprint_problem = clear_fingerprint(engine, runner, repo, number, current)
    names = label_names(engine.config)
    if remove is not None:
        cc = load_module("chat_clear")
        allowed = {names[key] for key in cc.HOLD_KEYS + (cc.CAPTAIN_KEY,)}
        if not remove or any(label not in allowed for label in remove):
            raise RuntimeError("a clear removes owner-tier hold labels and the captain marker only: refused %r" % (sorted(set(remove) - allowed),))
    with impact_state_lock(engine.state_dir):   # the same lock as the daemon's write: the clear and its removal are one step
        state, problem = read_impact_state(engine.state_dir)
        _impact_state_noted(engine, problem)
        ours = state["applied"].get(pr) or {}
        if remove is None:
            remove = [names["hold-impact"]]
            if ours.get("merge_label_added"):
                remove.append(names["merge-captain"])
            if under_order is None:
                remove += authors_hold_labels(names, raw, remove)   # #742: the OWNER's clear lifts the author's own hold with the floor's, from THIS read (the PR's labels at the cleared head)
            elif names["merge-captain"] in authors_hold_labels(names, raw, remove):
                why = "%s carries a %s label the switchboard did not add: it is a person's own hold, not the Chief of Staff's to lift under an order; the owner clears it" % (pr, names["merge-captain"])
                engine.audit.append(engine.actor, "gate.hold_clear_refused", pr, {"head": current, "by": who, "under_order": under_order["number"], "why": _one_line(why, 300)})
                raise NoticeError(why)   # nothing is written: the check runs before the clear is recorded
        else:
            remove = list(remove)
        head_now = current
        state["clears"][pr] = {"head": head_now, "reason": reason, "by": who, "at": int(engine.now()), "removed": sorted(remove)}   # `removed`: the labels a re-hold at a new head puts back (#830)
        _consume_record(state, pr)   # the labels it took off were the daemon's record: consumed with the clear, in this write
        state.get("carried", {}).pop(pr, None)   # a new clear starts from this head: an earlier carry is not part of it
        state.get("carry_void", {}).pop(pr, None)
        if fingerprint is not None:
            state["clears"][pr]["fingerprint"] = fingerprint
        elif fingerprint_problem:
            state["clears"][pr]["fingerprint_problem"] = fingerprint_problem
        write_impact_state(engine.state_dir, state)   # the clear first: a failed removal never leaves a hold the daemon would re-add
        change_issue_labels(engine, runner, repo, number, remove=remove)
        detail = {"head": head_now, "by": who, "reason": reason, "removed": remove}
        if via:
            detail["via"] = via
        if under_order:   # #661: the Chief of Staff's clear under the owner's standing order: recorded as HER act under that order, never as the owner's
            detail["under_order"] = under_order["number"]
            detail["order"] = plan_ledger.clip(under_order["text"], 300)
            if under_order.get("threshold"):   # #835: a clear by score shows the score and the threshold it was made under
                detail["score"], detail["threshold"] = under_order.get("score"), under_order["threshold"]
        if fingerprint is not None:
            detail["fingerprint"] = fingerprint
        engine.audit.append(engine.actor, "gate.hold_cleared", pr, detail)   # inside the lock
    lines = ["cleared the impact hold of %s at %s: removed %s; the daemon will not re-add it for this head" % (pr, head_now[:8], ", ".join(remove))] + neutralise_marker(engine, runner, repo, number, head_now, raw)
    notify_clear(engine, "clear", repo, number, head_now, reason=reason, body=raw.get("body"), under_order=under_order and dict(under_order, by=who))   # LAST: the state, the labels, the marker line and the audit are all done; it can neither delay nor alter them
    return lines


def delegated_clear_checks(engine, runner, now, kind, number):
    """(order {"number", "text"}, None) when the caller may clear under standing order `number`, else (None, why): the caller is the Chief of Staff's own session (its RECORDED role, never a
    flag or a text), the delegated clear is configured (`delegated_clear`), the order is listed there and really is an order of the charter (read from GitHub). #661."""
    module = load_module("delegated_clear")
    if not module.caller_is_chief(kind):
        return None, "only the Chief of Staff may clear under the owner's standing order (%s is %s)" % (kind[1], "an agent" if kind[0] == "agent" else "not known")
    configured = module.settings(engine.config)
    if configured is None:
        return None, "the delegated clear is not enabled: the owner has not listed a standing order under delegated_clear in the configuration"
    orders, charter = configured
    if number not in orders:
        return None, "order %d is not listed as a delegated clear" % number
    entry = module.entry_of(engine.config, number) or {}
    threshold = entry.get("max_level")   # #835: the highest level she may clear by score; absent = the path-class rules above all
    if entry.get("source") == module.OWNER_FILE:   # the owner's own standing-orders file, read-only: ONE line whose exact text is the bound hash
        try:
            line = module.file_order_line(cos_orders_text_of(read_cos_orders(engine.config)["raw"]), orders[number])
        except (CosOrdersError, OSError) as err:
            return None, "the owner's standing-orders file cannot be read (%s): refused" % _one_line(str(err), 120)
        if line is None:
            return None, "the owner's standing-orders file has no single line that is the text order %d is bound to (it was edited, removed or repeated): refused" % number
        return {"number": number, "text": re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", line), "threshold": threshold, "source": module.OWNER_FILE}, None
    try:
        reader = _intent_reader(runner, now)
        text = module.order_text(fetch_orders(reader, charter, engine.config.get("intent_repo")), number)
    except (IntentError, ConfigError, OSError) as err:
        return None, "the charter's orders cannot be read (%s): refused" % _one_line(str(err), 120)
    if text is None:
        return None, "order %d does not exist on charter #%d" % (number, charter)
    if module.order_hash(text) != orders[number]:   # the order numbers are POSITIONS: the text bound in the configuration must still be the text at that number
        return None, "the text of order %d is no longer the one the configuration is bound to (an order was edited, inserted or removed): the owner re-binds it" % number
    return {"number": number, "text": re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text), "threshold": threshold}, None


def delegated_clear_map(engine, runner, repo, item, base_sha, rule):
    """[reasons] for ONE changed file of the daemon's own (`closure_refusals` over its text before and after and its patch), or None when it cannot be read or mapped. The text comes from the
    git blobs API (the contents API refuses a file this large): the head's blob id is the file's own `sha`, the base's is the file's own `sha` at the base commit. #661."""
    try:
        if not isinstance(base_sha, str) or not base_sha:
            return None   # no merge-base: nothing to read the old text from
        module = load_module("delegated_clear")
        entry = _gh_json(engine, runner, repo, "repos/%s/contents/%s?ref=%s" % (repo, item["filename"], base_sha))
        base_blob = entry["sha"] if isinstance(entry, dict) and entry.get("type") == "file" else None
        if not isinstance(base_blob, str):
            return None
        base = module.decode_blob(_gh_json(engine, runner, repo, "repos/%s/git/blobs/%s" % (repo, base_blob)))
        head = module.decode_blob(_gh_json(engine, runner, repo, "repos/%s/git/blobs/%s" % (repo, item["sha"])))
        if base is None or head is None or not isinstance(item.get("patch"), str):
            return None
        if item["filename"] == rule["daemon_file"]:
            return module.closure_refusals(base, head, item["patch"], module.daemon_roots(rule), set(rule["clear_entry_points"]))
        return module.closure_refusals(base, head, item["patch"], list((rule.get("module_roots") or {}).get(item["filename"]) or []), set(), True)   # any other module the clear loads: its import-time code and the functions the clear runs
    except Exception:
        return None


def _gh_json(engine, runner, repo, path):
    done = _gh_write(engine, runner, repo, path, "GET")
    return json.loads(done.stdout or "null")


def delegated_clear_score(engine, module, report, threshold):
    """{"level", "reasons"} of the score rule for this pull request at the head of `report` (#835): the rating records come from `rating_at`, the ONE reading of "rated at this head" (#830), and are scored by the
    matrix's block and cells (the running install's, never the head's: a change to the matrix file is refused as machinery). A matrix, ledger or rule that cannot be read: the score is unknown, never Low."""
    try:
        rated, problem, entries = rating_at(engine, report["pr"], report["head"])
        if not rated and not problem:
            problem = "no independent impact rating at this head"
        block = module.score_block()
        scored = module.judge(block, impact_floor.load_matrix().get("cells"), entries, report, threshold) if not problem and block is not None else None
    except Exception as err:
        scored, problem = None, "%s: %s" % (type(err).__name__, _one_line(str(err), 80))
    if scored is None:
        scored = {"level": None, "reasons": ["the independent rating cannot be read (%s): the score is unknown, refused" % (problem or "no score rule")]}
    return scored


def delegated_clear_pr_checks(engine, runner, repo, number, threshold=None, info=None):
    """(report, None) when this pull request may be cleared under an order, else (None, why). The owner-hold decision is the daemon's own (`pr_floor_report`: no second rule), the rule (the classes
    never delegated and the clear code) is the matrix's `owner_hold.delegated_clear`; anything unreadable is refused. #661. With a `threshold` (the order's max_level, #835) the SCORE of the
    independent rating at this head is the rule about the pull request's classes; `info` (a dict) gets the score it read, for the audit of a refusal too."""
    try:
        report = pr_floor_report(engine, repo, number)
        rule = (impact_floor.load_matrix().get("owner_hold") or {}).get("delegated_clear")
    except (impact_floor.MatrixError, NoticeError, ValueError) as err:
        return None, "the owner-hold decision or its rule cannot be read (%s): refused" % _one_line(str(err), 140)
    files, get = [], impact_carry_get(engine, runner, repo)
    try:
        for page in range(1, 31):
            got = get("repos/%s/pulls/%d/files?per_page=100&page=%d" % (repo, number, page))
            if not isinstance(got, list):
                raise ValueError("not a list")
            files.extend(f for f in got if isinstance(f, dict))
            if len(got) < 100:
                break
        else:
            files = None   # more than 3000 files: not read in full
    except Exception:
        files = None
    module, base_sha, closure = load_module("delegated_clear"), None, {}
    if files is not None:   # the file list and the decision must be of ONE head: the head read again after the files must still be the one the decision read
        try:
            pull = get("repos/%s/pulls/%d" % (repo, number))
            after, base_sha = (pull.get("head") or {}).get("sha"), (pull.get("base") or {}).get("sha")
            if after == report.get("head") and base_sha and any(f.get("status") == "modified" for f in files):   # the files' patches are against the MERGE-BASE, not the base branch's tip: read the blobs at that commit
                compared = get("repos/%s/compare/%s...%s?per_page=1" % (repo, base_sha, after))
                base_sha = ((compared or {}).get("merge_base_commit") or {}).get("sha")
        except Exception:
            after = None
        if after != report.get("head"):
            return None, "the pull request's head moved while it was being checked (%s, then %s): nothing was cleared" % (str(report.get("head"))[:8], str(after)[:8])
        for item in files:   # a changed file of the daemon's own: each changed LINE is mapped to its definition by the ast of the file it belongs to
            if isinstance(item.get("filename"), str) and impact_floor.matches(item["filename"], (rule or {}).get("closure_files") or []) and not impact_floor.matches(item["filename"], (rule or {}).get("clear_code_paths") or []) and item.get("status") == "modified":
                closure[item["filename"]] = delegated_clear_map(engine, runner, repo, item, base_sha, rule)
    scored = delegated_clear_score(engine, module, report, threshold) if threshold is not None else None
    if scored is not None and info is not None:
        info["score"] = scored["level"]
    reasons = module.refusals(rule, report, files, impact_floor.matches, closure, scored)
    if reasons:
        return None, "; ".join(reasons)
    return report, None


def cmd_impact_clear(args, runner, now):
    """`impact clear <repo#N> --reason TEXT`: the owner's own terminal only (the Chief of Staff cannot: G6 is never waivable by it)."""
    kind = gates_caller_kind(runner, now)
    engine, _, _ = build_engine(runner, now, "cli")
    reason = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", args.reason or "").split())   # control characters never reach the log
    try:
        repo, number = parse_pr_ref(args.pr)
    except NoticeError as err:
        sys.stderr.write("%s: impact clear: %s\n" % (PROG, err))
        return EXIT_USAGE
    under, delegated = getattr(args, "under_order", None), None
    if kind[0] != "person":
        who = kind[1]
        if under is not None:   # #661: the Chief of Staff's clear under the owner's standing order: every check in `delegated_clear_checks`, or a refusal that names it
            delegated, why = delegated_clear_checks(engine, runner, now, kind, under)
            if delegated is None:
                if engine.audit is not None:
                    engine.audit.append(engine.actor, "gate.hold_clear_refused", "%s#%d" % (repo, number), {"by": who, "under_order": under, "why": _one_line(why, 300)})
                sys.stderr.write("%s: impact clear: refused: %s\n" % (PROG, _one_line(why, 400)))
                return EXIT_USAGE
        else:
            if engine.audit is not None:
                engine.audit.append(engine.actor, "gate.hold_clear_refused", "%s#%d" % (repo, number), {"by": who, "why": "not the owner's own terminal"})
            sys.stderr.write("%s: impact clear: refused: only the owner, from the owner's own terminal, clears an impact hold (%s is %s)\n" % (
                PROG, who, "an agent" if kind[0] == "agent" else "not known"))
            return EXIT_USAGE
    elif under is not None:
        sys.stderr.write("%s: impact clear: --under-order is for the Chief of Staff; the owner's own clear needs no order\n" % PROG)
        return EXIT_USAGE
    if len(reason) < 10:
        sys.stderr.write("%s: impact clear: --reason needs at least 10 characters: say why the hold is false\n" % PROG)
        return EXIT_USAGE
    if getattr(args, "revoke", False):   # the owner takes back a clear (and any carry of it): the daemon holds the PR again at its next pass
        try:
            with impact_state_lock(engine.state_dir):
                state, problem = read_impact_state(engine.state_dir)
                _impact_state_noted(engine, problem)
                pr = "%s#%d" % (repo, number)
                had = state["clears"].pop(pr, None)
                carried = state.get("carried", {}).pop(pr, None)
                state.get("carry_void", {}).pop(pr, None)
                if had is not None:
                    _consume_record(state, pr, had)   # an older record of the daemon's labels is stale (a newer one, applied after the clear, is kept)
                if had is not None or carried is not None:   # nothing to take back: nothing is written
                    write_impact_state(engine.state_dir, state)
                    engine.audit.append(engine.actor, "gate.clear_revoked", pr, dict({"by": kind[1], "reason": plan_ledger.clip(redact_command_text(reason), 300),
                                                                                    "cleared_head": (had or {}).get("head"), "carried_head": (carried or {}).get("head")},
                                                                                   **({"under_order": delegated["number"]} if delegated else {})))
        except (ImpactStateBusy, RuntimeError) as err:
            sys.stderr.write("%s: impact clear --revoke: %s; nothing was changed\n" % (PROG, _one_line(str(err), 200)))
            return EXIT_ERROR
        print("revoked the owner's clear of %s%s: the daemon holds it again at its next pass, and nothing carries" % (pr, " (and its carry)" if carried else "") if (had or carried) else "%s has no clear to revoke" % pr)
        return EXIT_OK
    try:
        extra = {}
        if delegated:   # #661: the checks of THIS pull request, at the head they read, and the clear bound to that head
            info = {}
            report, why = delegated_clear_pr_checks(engine, runner, repo, number, delegated.get("threshold"), info)
            if why:
                if engine.audit is not None:
                    engine.audit.append(engine.actor, "gate.hold_clear_refused", "%s#%d" % (repo, number), dict({"by": kind[1], "under_order": delegated["number"], "why": _one_line(why, 300)}, **({"score": info.get("score"), "threshold": delegated["threshold"]} if delegated.get("threshold") else {})))
                sys.stderr.write("%s: impact clear: refused: %s\n" % (PROG, _one_line(why, 600)))
                return EXIT_USAGE
            extra = {"head": report["head"], "via": "order:%d" % delegated["number"], "under_order": dict(delegated, score=info.get("score"))}
        for line in impact_clear_core(engine, runner, repo, number, kind[1], plan_ledger.clip(redact_command_text(reason), 300), **extra):
            print(line)
    except (NoticeError, IntentError) as err:
        sys.stderr.write("%s: impact clear: %s\n" % (PROG, _one_line(str(err), 300)))
        return EXIT_ERROR
    except ImpactStateBusy as err:   # the lock wait ran out: one line, nothing half done
        sys.stderr.write("%s: impact clear: %s; nothing was changed, try again in a moment\n" % (PROG, _one_line(str(err), 200)))
        return EXIT_ERROR
    except RuntimeError as err:      # no audit sink, or the corrupt state file could not be set aside: retrying does not help until it is fixed
        sys.stderr.write("%s: impact clear: refused: %s; nothing was changed\n" % (PROG, _one_line(str(err), 200)))
        return EXIT_ERROR
    return EXIT_OK


def cmd_impact_delegated(args, runner, now):
    """`impact delegated [--json]` (#835): every clear the Chief of Staff made under an order of the owner's, newest last, from the audit: when, the pull request, the head, the score, the threshold, the order and her
    judgement line. A refusal (`gate.hold_clear_refused`) is not a clear and is never listed. Read-only; the owner's own clears are not listed."""
    if getattr(args, "check", False):   # the fleet doctor's check: every configured owner-file order must still be exactly one line of the owner's file (a vanished or changed line is seen now, not at the next clear)
        try:
            config = load_config()   # the configuration only (no engine: the doctor has no running fleet to open)
        except ConfigError as err:
            print("the configuration cannot be loaded: %s" % _one_line(str(err), 200))
            return EXIT_ERROR
        module, bad, seen = load_module("delegated_clear"), [], 0
        raw = config.get(module.CONFIG_KEY)
        if raw:
            try:
                module.validate(raw, "config")
            except ValueError as err:
                print("delegated_clear is malformed: %s" % _one_line(str(err), 200))
                return EXIT_ERROR
        for entry in (raw or {}).get("orders", []) if isinstance(raw, dict) else []:
            if entry.get("source") != module.OWNER_FILE:
                continue
            seen += 1
            try:
                found = module.file_order_line(cos_orders_text_of(read_cos_orders(config)["raw"]), entry["sha256"])
            except (CosOrdersError, OSError) as err:
                bad.append("order %d: the owner's standing-orders file cannot be read (%s)" % (entry["number"], _one_line(str(err), 100)))
                continue
            if found is None:
                bad.append("order %d: no single line of the owner's standing-orders file matches its bound text: the delegation clears nothing" % entry["number"])
        if bad:
            print("; ".join(bad))
            return EXIT_ERROR
        print("%d owner-file order(s) match their bound line" % seen if seen else "no owner-file order is configured")
        return EXIT_OK
    rows = []
    for entry in _audit_tail(ensure_state_dir()):
        detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
        if entry.get("event") == "gate.hold_cleared" and detail.get("under_order") is not None:
            rows.append({"at": entry.get("ts"), "pr": entry.get("subject"), "head": detail.get("head"), "score": detail.get("score"), "threshold": detail.get("threshold"),
                         "order": detail.get("under_order"), "by": detail.get("by"), "judgement": detail.get("reason")})
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=1, sort_keys=True))
    for row in rows if not getattr(args, "json", False) else ():
        print("%s %s at %s: score %s, threshold %s, order %s, by %s: \"%s\"" % (row["at"], row["pr"], str(row["head"])[:8], row["score"] or "-", row["threshold"] or "-", row["order"], row["by"], _one_line(str(row["judgement"]), 200)))
    if not rows and not getattr(args, "json", False):
        print("no clear was made under an order of the owner's")
    return EXIT_OK


def impact_plan(engine, report, labels, state, records, records_problem):
    """What the daemon would do with one judged pull request, as (what, labels): ("add", the labels it would add), ("held", []) already labelled,
    ("cleared", []) the owner cleared this head, ("release", the labels it would remove: only those its own record lists and the pull request has now) or
    ("none", []). The dry run prints this; the daemon's pass makes the same decisions (a test runs both over the same pull requests)."""
    names = label_names(engine.config)
    have = set(labels or ())
    pr = report["pr"]
    if report.get("draft"):
        return "none", []   # the daemon judges no draft pull request
    if report.get("owner_decision") == "hold":
        carried = (state.get("carried", {}).get(pr) or {}).get("head") == report["head"] and impact_floor.carry_enabled(engine.config, report["pr"].rpartition("#")[0])
        if (state["clears"].get(pr) or {}).get("head") == report["head"] or carried:   # the owner's clear, or a clear carried over a pure merge of main (#510)
            return "cleared", []
        missing = [l for l in (names["merge-captain"], names["hold-impact"]) if l not in have]
        return ("add", missing) if missing else ("held", [])
    if not records_problem and rehold_reason(engine, state, pr.rpartition("#")[0], pr, report.get("head")):   # (#830) held again at a new head
        missing = [l for l in (names["merge-captain"], names["hold-impact"]) if l not in have]
        return ("add", missing) if missing else ("held", [])
    if records_problem or not isinstance(state["applied"].get(pr), dict):
        return "none", []
    return "release", [l for l in _labels_ours(names, state, pr) if l in have]


def cmd_impact_hold(args, runner, now):
    """`impact hold --dry-run [--repo R]`: what the daemon would label and release right now (the SAME decision as its pass: the owner-hold rule), over the repos
    under `plan_gates` (read-only). The labels are only ever written by the daemon, and only when `impact_hold.enabled` is true."""
    if not args.dry_run:
        sys.stderr.write("%s: impact hold: only --dry-run is available by hand: the daemon writes the labels, when impact_hold.enabled is true\n" % PROG)
        return EXIT_USAGE
    engine, state, config = build_engine(runner, now, "cli", writable=False)
    repos = [args.repo] if args.repo else sorted(((config.get("plan_gates") or {}).get("repos") or {}))
    names = label_names(config)
    shown = released = 0
    records, records_problem = impact_records(state)
    for repo in repos:
        try:
            has_floor = impact_floor.floor_for(repo, config)[0]
        except impact_floor.MatrixError as err:   # the matrix file cannot be read: say so and stop, never read it as "no floor"
            sys.stderr.write("%s: impact hold: %s\n" % (PROG, _one_line(str(err), 200)))
            return EXIT_ERROR
        if not has_floor:
            continue
        try:
            items = read_open_items(engine, runner, repo)[0]
        except IntentError as err:
            sys.stderr.write("%s: %s: %s\n" % (PROG, repo, _one_line(str(err), 200)))
            continue
        for item in items:
            if not item["pr"]:
                continue
            try:
                report = pr_floor_report(engine, repo, item["number"])
            except Exception as err:
                print("%s#%d  unreadable: %s" % (repo, item["number"], _one_line(str(err), 100)))
                continue
            what, labels = impact_plan(engine, report, item["labels"], read_impact_state(state)[0], records, records_problem)
            reasons = "; ".join(report.get("owner_reasons") or [])[:160]
            if what in ("add", "held", "cleared"):
                shown += 1
                print("%s  %s  %s" % (report["pr"], "cleared by the owner for this head: no label" if what == "cleared" else
                                      ("would add %s" % ", ".join(labels) if labels else "already labelled"), reasons))
            elif what == "release" and labels:
                released += 1
                print("%s  would remove %s (labels its record says the daemon added; the rule no longer holds it)" % (report["pr"], ", ".join(labels)))
    print("%s: %d PR(s) would be held (a dry run: nothing was written; impact_hold is %s)" % (
        PROG, shown, impact_floor.hold_mode(config, repos[0] if repos else "")))
    print("%s: %d PR(s) would have a hold released" % (PROG, released))
    return EXIT_OK


def release_impact_hold(engine, repo, number, report, record):
    """(#451 slice 3) Take the hold off a pull request the owner-hold rule no longer holds: ONLY the labels this daemon added itself, as its own record in
    impact-hold.json lists them (and only when the owner's clear did not come after that record). A label a person or another tool added, a pull request with no
    record of ours, a record that is missing or corrupt: nothing is removed. Everything is re-checked under the lock (the live labels, the impact review record,
    the clear): a decision made a moment ago never outlives a change of mind. Returns the number of gh calls spent."""
    with impact_state_lock(engine.state_dir):   # the same lock as the owner's clear and the daemon's write: the three never interleave
        state, problem = read_impact_state(engine.state_dir)
        if problem:
            _impact_state_noted(engine, problem)
            return 0   # a state we cannot trust removes nothing
        pr = report["pr"]
        names = label_names(engine.config)
        if not isinstance(state["applied"].get(pr), dict):
            return 0
        ours = _labels_ours(names, state, pr)
        rating, rating_problem = impact_review_record(engine.state_dir, pr, report["head"])
        if rehold_reason(engine, state, repo, pr, report["head"]):
            return 0   # held again at a new head (#830): a hold the owner has not cleared at this head is never released
        if rating_problem or (rating and rating[0] == "hold" and impact_floor.class_is_top_tier(impact_floor._matrix_cached(), rating[1])):
            return 0   # an independent hold arrived (or cannot be read) since the decision: it stays
        raw = read_pr_json(engine, repo, number)   # FAILS CLOSED: a failed read raises, nothing is removed and the caller backs off
        if raw.get("headRefOid") != report["head"]:
            return 0   # a push since the decision: the next pass judges the new head
        live = raw.get("labels")   # the labels now, not the list's copy of three minutes ago
        have = {(l.get("name") if isinstance(l, dict) else l) for l in (live if isinstance(live, list) else [])}
        remove = [l for l in ours if l in have]
        runner = getattr(engine, "gh_runner", None) or subprocess.run
        if remove:
            change_issue_labels(engine, runner, repo, number, remove=remove)
        del state["applied"][pr]
        write_impact_state(engine.state_dir, state)
        engine.audit.append(engine.actor, "gate.hold_released", pr, {"head": report["head"], "removed": remove, "reasons": report.get("owner_reasons", [])[:5],
                                                                      "rule": engine.__dict__.get("impact_rule")})
        return len(remove)



def bind(namespace):
    """Rebind every moved function to `namespace` (the daemon's globals), install it there under its own name, and return the names bound."""
    here = globals()
    for name in MOVED:
        original = here[name]
        bound = types.FunctionType(original.__code__, namespace, original.__name__, original.__defaults__, original.__closure__)
        bound.__kwdefaults__ = original.__kwdefaults__
        bound.__dict__.update(original.__dict__)   # the attributes a command declares (`ledger`, `ensure_on_use`, ...)
        bound.__doc__ = original.__doc__
        bound.__module__ = namespace.get("__name__", bound.__module__)
        namespace[name] = bound
    return MOVED
