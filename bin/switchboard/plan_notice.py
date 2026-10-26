# ---- #765: a posted or edited plan tells its approver once per hash. Reads only what the triage pass already holds (no GitHub read of its own).

PLAN_WAKE_FILE = "plan-wake.json"
PLAN_WAKE_FACT_KEEP = 2 * 86400


class PlanWakeState:
    """The announced hashes (kept in PLAN_WAKE_FILE so a restart never repeats one) and the facts waiting to be delivered (memory)."""

    def __init__(self, state_dir):
        self.announced, self.facts, self.unapproved, self.problem = {}, {}, [], None
        try:
            raw = json.loads((Path(state_dir) / PLAN_WAKE_FILE).read_text())
            if isinstance(raw, dict) and isinstance(raw.get("announced"), dict):
                self.announced = {k: v for k, v in raw["announced"].items() if isinstance(k, str) and isinstance(v, (int, float))}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as err:
            self.problem = "plan-wake.json cannot be read, so plans of the last 24 hours may be announced again: %s" % _one_line(type(err).__name__, 40)


def plan_wake_state(engine):
    state = getattr(engine, "plan_wake", None)
    if state is None:
        state = engine.plan_wake = PlanWakeState(engine.state_dir)
        if state.problem:
            engine._note("plan_wake.error", "", {"error": state.problem})
    return state


def plan_wake_consensus(engine):
    """The plan hashes with a consensus pass in the local ledger; empty when it cannot be read (a plan is then judged on comments and pull requests alone)."""
    try:
        entries, _ = plan_ledger.read_ledger(engine.state_dir)
    except Exception as err:
        engine._note("plan_wake.error", "", {"error": "the ledger cannot be read for consensus: %s" % _one_line(type(err).__name__, 40)})
        return set()
    return {e.get("plan_hash") for e in entries if e.get("kind") == "consensus" and e.get("result") == "pass" and isinstance(e.get("plan_hash"), str)}


def plan_wake_route(number, items, ctx, discovery, chiefs, owners=None):
    """(recipient names, "ask" or "work item") for the plan of issue `number`, from the held parent links alone (never from the plan's text):
    a sub-issue of a charter is an ask (the Chief of Staff); a sub-issue of such an ask is a work item (its charter's orchestrator). None otherwise."""
    parent = (items.get(number) or {}).get("parent")
    if parent in ctx["charters"]:
        return list(chiefs), "ask"
    if parent in ctx["asks"]:
        charter = (items.get(parent) or {}).get("parent")
        names = sorted(n for n, o in (discovery.agents.items() if discovery else [])
                       if (o.fleet or {}).get("role") == "orchestrator" and (owners or {}).get(n) == charter)   # #969: the registry's charter (`owners`), never the session metadata
        return names, "work item"
    return None


def plan_wake_pass(engine, state, repos, now, budget=None):
    """Announce each new plan hash to its approver once, and list the plans that look unapproved. Never raises; reads nothing from GitHub."""
    wake = plan_wake_state(engine)
    module, check_module = load_triage_plan_open(), load_plan_check()
    discovery = getattr(engine, "discovery", None)
    if discovery is None or discovery.problems:
        return
    chiefs = sorted(n for n, o in discovery.agents.items() if (o.fleet or {}).get("role") == "chief-of-staff")
    consensus, sends, listed, changed = None, None, [], False
    for repo in repos:
        issues_unit, comments_unit = state.units.get((repo, "issues")), state.units.get((repo, "comments"))
        if issues_unit is None or comments_unit is None:
            continue
        raw = {}
        for record in issues_unit["data"]:
            item = triage_norm(record)
            if item is not None and item["state"] == "open" and not item["pr"]:
                raw[item["number"]] = (item, record)
        items = {n: i for n, (i, _) in raw.items()}
        ctx = triage_context(engine, repo, items, discovery)
        by_issue = {}
        for comment in comments_unit["data"]:
            n = _url_number(comment.get("issue_url")) if isinstance(comment, dict) else None
            if n is not None:
                by_issue.setdefault(n, []).append(comment)
        pulls = [r for r in issues_unit["data"] if _is_pr(r)] + list((state.units.get((repo, "merged")) or {}).get("data") or [])
        for number, plan in sorted(module.latest_plans(comments_unit["data"], check_module.HEADING_RE, _url_number).items()):
            if number not in items:
                continue
            route = plan_wake_route(number, items, ctx, discovery, chiefs, registry_owners(engine))
            if route is None:
                continue
            posted = parse_iso(plan.get("updated_at") or plan.get("created_at"))
            if posted is None or now - posted > module.ANNOUNCE_SECONDS:
                continue
            body = plan["body"]
            plan_hash = check_module.plan_hash(body)
            if consensus is None:
                consensus = plan_wake_consensus(engine)
            if (plan_hash in consensus or module.approved_by_comment(plan, plan_hash, by_issue.get(number, []), signed_by_fleet)
                    or module.cited_by_pr(repo, number, plan, pulls, parse_iso)):
                continue
            if sends is None:
                sends = _audit_tail(engine.state_dir)   # #790: the sends the audit already holds, read once per pass and only for a plan no other form approved
            if module.approved_by_send(plan, plan_hash, sends, signed_by_fleet):
                continue
            url = module.plan_url(repo, number, plan["id"])
            key = "%s#%d:%s" % (repo, number, plan_hash)
            names = route[0]
            if key not in wake.announced and names:
                own = raw[number][1]
                source = number
                done_when = parse_intent(own.get("body"))[1]
                if route[1] == "work item":
                    parent_record = (raw.get((items[number] or {}).get("parent")) or (None, {}))[1]
                    if parse_intent(parent_record.get("body"))[1]:
                        source, done_when = items[number]["parent"], parse_intent(parent_record.get("body"))[1]
                stale, defect = False, ""
                try:
                    result = check_module.check(body, done_when, None)
                    passed = result["result"] == "pass"
                    defect = _one_line(str((result.get("defects") or [""])[0]), 160)
                except Exception:
                    passed = False
                if not passed:   # #802: a FAIL is confirmed against ONE fresh read of the issue that supplied the Done-when, never the cached body
                    stale = True
                    hub = getattr(engine, "hub", None)
                    if hub is not None and (budget is None or budget[0] >= 1):
                        if budget is not None:
                            budget[0] -= 1
                        try:
                            fresh = hub._read_one(repo, "issues/%d" % source, now, fresh=True)
                            fresh_done = parse_intent(fresh.get("body"))[1] if isinstance(fresh, dict) else None
                        except Exception:
                            fresh_done = None
                        if fresh_done:
                            stale = False
                            try:
                                result = check_module.check(body, fresh_done, None)
                                passed = result["result"] == "pass"
                                defect = _one_line(str((result.get("defects") or [""])[0]), 160)
                            except Exception:
                                passed = False
                text = module.line(number, url, plan_hash, passed, stale and not passed)
                for name in names:
                    wake.facts["plan:%s:%s" % (key, name)] = (text, now, name, repo, number)
                wake.announced[key] = now
                changed = True
                engine._note("plan_wake.announced", "%s#%d" % (repo, number), {"hash": plan_hash, "to": names, "route": route[1], "check": "PASS" if passed else "FAIL", "stale": bool(stale and not passed), "defect": "" if passed else defect})
            if now - posted >= module.UNAPPROVED_SECONDS:
                listed.append("%s#%d" % (repo, number) if len(repos) > 1 else "#%d" % number)
    wake.unapproved = sorted(listed)
    for old in [k for k, v in wake.announced.items() if now - v > 14 * 86400]:
        del wake.announced[old]
        changed = True
    for old in [k for k, v in wake.facts.items() if now - v[1] > PLAN_WAKE_FACT_KEEP]:
        del wake.facts[old]
    if changed:
        try:
            state_write(Path(engine.state_dir) / PLAN_WAKE_FILE, json.dumps({"announced": wake.announced}, sort_keys=True) + "\n", fsync=True)
        except OSError as err:
            engine._note("plan_wake.error", "", {"error": "plan-wake.json was not saved, so a restart may announce again: %s" % _one_line(type(err).__name__, 40)})


class PlanWakeFacts:
    """The one line each approver is told about a new plan hash: an ordinary fact, so the usual rule picks a note or a wake and a key found in the
    recipient's inbox is never delivered again."""

    __name__ = "plan_wake_facts"

    def __init__(self):
        self.engine = None

    def bind(self, engine):
        self.engine = engine

    def __call__(self):
        wake = getattr(self.engine, "plan_wake", None) if self.engine else None
        if not wake or not wake.facts:
            return []
        return [Fact(key, name, "plan", text, created, issue=number, sender="switchboard", repo=repo)
                for key, (text, created, name, repo, number) in sorted(wake.facts.items())]
