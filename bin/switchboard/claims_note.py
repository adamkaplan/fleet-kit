# ---- #1716: a pull request whose `CLAIMS:` line leaves scenarios of its plan out is told once per body. Reads only the units the triage pass already holds (open and
# merged pull requests with their bodies, the plan comment): no GitHub read of its own. It informs and enforces nothing (the gates and the merge refusal are what they were).

CLAIMS_NOTE_FILE = "claims-note.json"
CLAIMS_NOTE_KEEP = 14 * 86400


class ClaimsNoteState:
    """The announced keys (`repo#number:hash of the body`, kept in CLAIMS_NOTE_FILE so a restart never repeats one) and the reasons already audited (memory)."""

    def __init__(self, state_dir):
        self.announced, self.said, self.problem = {}, set(), None
        try:
            raw = load_module("record_json").parse_bounded((Path(state_dir) / CLAIMS_NOTE_FILE).read_text())
            if isinstance(raw, dict) and isinstance(raw.get("announced"), dict):
                self.announced = {k: v for k, v in raw["announced"].items() if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as err:
            self.problem = "claims-note.json cannot be read, so a notice may be sent again: %s" % _one_line(type(err).__name__, 40)


def claims_note_state(engine):
    state = getattr(engine, "claims_note", None)
    if state is None:
        state = engine.claims_note = ClaimsNoteState(engine.state_dir)
        if state.problem:
            engine._note("claims_note.error", "", {"error": state.problem})
    return state


def claims_note_text(repo, number, plan_ref, ids):
    """The one line the pull request's recipients read: the pull request, the plan, the ids left out and the two ways to fix them."""
    return ("PR %s#%d: its `CLAIMS:` line leaves out %s of the plan %s, and no other open or merged pull request of the plan claims %s, as far as the held units show; "
            "add them to the `CLAIMS:` line, or another pull request of the ask claims them (this only informs: no gate changed)"
            % (repo, number, ", ".join(ids), plan_ref, "it" if len(ids) == 1 else "them"))


def claims_note_plan(engine, state, repos, prepo, pnumber, cache):
    """(checked plan or None, why not) of the plan issue `prepo#pnumber`, from the held units alone."""
    if (prepo, pnumber) in cache:
        return cache[(prepo, pnumber)]
    found = (None, "the units of %s are not held" % prepo)
    issues_unit, comments_unit = state.units.get((prepo, "issues")), state.units.get((prepo, "comments"))
    if issues_unit is not None and comments_unit is not None:
        check_module = load_plan_check()
        comment = load_triage_plan_open().latest_plans(comments_unit["data"], check_module.HEADING_RE, _url_number).get(pnumber)
        records = {_number(r): r for r in issues_unit["data"] if isinstance(r, dict) and not _is_pr(r)}
        own = records.get(pnumber)
        if comment is None or own is None:
            found = (None, "no plan comment or no plan issue is held for %s#%d" % (prepo, pnumber))
        else:
            done_when = parse_intent(own.get("body"))[1] or parse_intent((records.get(parent_of(own)) or {}).get("body"))[1]
            try:
                result = check_module.check(comment["body"], done_when, None)
                parsed = check_module.parse_scenarios(check_module.scenario_block(comment["body"]))
                found = (parsed, "") if result["result"] == "pass" else (None, "the plan does not read as pass")
            except Exception:
                found = (None, "the plan does not read as pass")
    cache[(prepo, pnumber)] = found
    return found


def claims_note_pass(engine, state, repos, now):
    """One notice per open pull request and body hash, to the recipients `GitHubHub.route_of` finds from the held facts of the pull request. Never raises."""
    try:
        note, hub, discovery = claims_note_state(engine), getattr(engine, "hub", None), getattr(engine, "discovery", None)
        if hub is None or discovery is None or discovery.problems:
            return
        sent, cache = False, {}
        default_repo = engine.config.get("intent_repo")
        if not hub._notices:   # route_of reads them and the hub's own pass may not have run yet; never replace the ones it holds
            hub._notices = hub._load_notices()
        for repo in repos:
            issues_unit, merged_unit = state.units.get((repo, "issues")), state.units.get((repo, "merged"))
            if issues_unit is None or merged_unit is None:
                continue
            opened = [r for r in issues_unit["data"] if _is_pr(r) and r.get("state", "open") == "open"]
            merged = [r for r in (merged_unit["data"] or []) if isinstance(r, dict) and (r.get("merged_at") or r.get("merged"))]   # the recent closed list also holds those closed without a merge: they claim nothing
            for record in opened:
                number = _number(record)
                if number is None or not isinstance(record.get("body"), str):
                    continue
                key = "%s#%d:%s" % (repo, number, hashlib.sha256(record["body"].encode("utf-8", "replace")).hexdigest()[:16])
                if key in note.announced:
                    continue
                ids, why, plan_ref = claims_note_ids(engine, state, repos, repo, number, record, opened, merged, default_repo, cache)
                if not ids:
                    if why and (key, why) not in note.said:
                        note.said.add((key, why))
                        engine._note("claims_note.skipped", "%s#%d" % (repo, number), {"why": why})
                    continue
                item = GitHubItem("github.pr:%s" % key, "github.pr", "PR #%d" % number, repo, _pr_refs(record, number)[0], [], _login(record), now, [number], True)
                try:
                    owner, _ = hub.route_of(item, discovery.agents, now)
                except GitHubUnavailable as err:   # one failed read skips this pull request only: the state write below still happens
                    why = "GitHub unavailable: %s" % _one_line(str(err), 120)
                    if (key, why) not in note.said:
                        note.said.add((key, why))
                        engine._note("claims_note.skipped", "%s#%d" % (repo, number), {"why": why})
                    continue
                if not owner:
                    if (key, "no recipient") not in note.said:
                        note.said.add((key, "no recipient"))
                        engine._note("claims_note.skipped", "%s#%d" % (repo, number), {"why": "no recipient: the daemon resolves no lane for the pull request's own events (not sent)"})
                    continue
                text = claims_note_text(repo, number, plan_ref, ids)
                plan_wake_state(engine).facts["claims:%s:%s" % (key, owner)] = (text, now, owner, repo, number)   # delivered as an ordinary plan fact
                note.announced[key], sent = now, True
                engine._note("claims_note.sent", "%s#%d" % (repo, number), {"to": owner, "ids": ids})
        old = [k for k, v in note.announced.items() if now - v > CLAIMS_NOTE_KEEP]
        for k in old:
            del note.announced[k]
        if sent or old:
            state_write(Path(engine.state_dir) / CLAIMS_NOTE_FILE, json.dumps({"announced": note.announced}, sort_keys=True) + "\n", fsync=True)
    except Exception as err:
        engine._note("claims_note.error", "", {"error": _one_line("%s: %s" % (type(err).__name__, err), 160)})


def claims_note_ids(engine, state, repos, repo, number, record, opened, merged, default_repo, cache):
    """(ids left out, why none, plan ref): the one rule is `plan_ledger.omitted_claims` over the other open and merged pull requests of the same plan."""
    body = record["body"]
    link = PLAN_LINE_RE.search(plan_ledger.visible_body(body))
    if not link:
        return [], "the pull request has no plan link", None
    try:
        ref = link.group(1).strip("<>.,")
        prepo, pnumber, _ = parse_plan_source(ref, default_repo or repo)
    except Exception:
        return [], "the plan link cannot be read", None
    scenarios, why = claims_note_plan(engine, state, repos, prepo, pnumber, cache)
    if scenarios is None:
        return [], why, None
    if plan_ledger.claim_text(body) is None:
        return [], "the pull request has no CLAIMS line", None
    if not plan_ledger.covered_ids(scenarios, [body]):
        return [], "the CLAIMS line is refused (it covers nothing)", None
    others = []
    for state_name, group in (("open", opened), ("merged", merged)):
        for other in group:
            if other is record or _number(other) == number or not isinstance(other.get("body"), str):
                continue
            found = PLAN_LINE_RE.search(plan_ledger.visible_body(other["body"]))
            if found and _same_plan(found.group(1).strip("<>.,"), ref, default_repo or repo):
                others.append((state_name, other["body"]))
    return plan_ledger.omitted_claims(scenarios, body, others), "", ref
