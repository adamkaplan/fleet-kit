# ---------------------------------------------------------------- `move`: an ask from one charter to another (#347)
# The logic is bin/lanes.py; this is registration and the caller's identity only.

def add_move_parsers(sub):
    move = sub.add_parser("move", help="Move an ask to another orchestrator's charter, with a handover (Chief of Staff or operator)")
    move.add_argument("ask", type=issue_arg, help="The ask: N or OWNER/REPO#N")
    move.add_argument("--to", dest="to_charter", type=issue_arg, required=True, help="The charter it moves to: N or OWNER/REPO#N, in the ask's repo")
    move.add_argument("--from", dest="sender", help="Lab only: the caller's fleet name")


def move_caller(engine, args, runner, now):
    """(name, role) of who runs `move`: its pane's fleet agent, (None, None) at the owner's own terminal (the operator), or an "unidentified" caller that is refused. A mistake guard, not a boundary (docs/switchboard.md). `--from` in the Lab."""
    if args.sender is not None:
        if os.environ.get(LAB_ENV) != "1":
            raise CallerError("--from is only for the Lab (%s=1); an agent is identified by its pane" % LAB_ENV)
        found = engine.discover(engine.now()).agents.get(args.sender)
        if found is None:
            raise CallerError("%s is not a fleet agent the switchboard can see" % args.sender)
        return found.name, (found.fleet or {}).get("role")
    kind = gates_caller_kind(runner, now)   # THE ONE human-terminal check (no copy of it here): a shell that cannot be established as the owner's own terminal is "unknown" and refused
    if kind[0] == "person":
        return None, None   # the owner at a shell: the operator
    if kind[0] == "agent":
        return kind[1], kind[2]
    return "an unidentified caller (%s)" % _one_line(kind[1], 160), "unidentified"


def cmd_move(args, runner, now):
    lanes = load_module("lanes")
    engine, _, config = build_engine(runner, now, "cli")
    if engine.intents is None:
        raise ConfigError("'intent_repo' is not configured in %s: set it to OWNER/REPO" % config_path())
    caller, role = move_caller(engine, args, runner, now)
    try:
        watched = watched_repos(config, discovered_fleets(engine.discover(engine.now())))
        repo, ask = resolve_issue(config, args.ask, None, watched)
        to_repo, charter = resolve_issue(config, args.to_charter, None, watched)
    except IssueRefError as err:
        sys.stderr.write("%s: %s\n" % (PROG, err))
        return EXIT_USAGE
    if repo is None or to_repo != repo:
        sys.stderr.write("%s: move: the ask and the charter must be in the same repository (%s vs %s)\n" % (PROG, repo, to_repo))
        return EXIT_USAGE
    sb = types.SimpleNamespace(engine=engine, **{name: globals()[name] for name in lanes.NEEDS})
    return lanes.run_move(sb, repo, ask, charter, caller, role, print, lambda text: sys.stderr.write(text + "\n"), watched=watched)


cmd_move.ensure_on_use = True   # the daemon is alive when it runs


# ---------------------------------------------------------------- `reassign`: a running worker to another orchestrator (#347)
# The logic is bin/reassign.py; this is registration and the caller's identity only.

def add_reassign_parsers(sub):
    reassign = sub.add_parser("reassign", help="Hand a running worker to another orchestrator, with its current boss's consent (Chief of Staff or operator); --undo restores its own boss")
    reassign.add_argument("worker", help="The worker's fleet name")
    reassign.add_argument("--to", dest="to_boss", help="The orchestrator that becomes its boss")
    reassign.add_argument("--consent-from", dest="consent", help="The worker's CURRENT boss, whose consent is recorded with this command")
    reassign.add_argument("--undo", action="store_true", help="Remove the override: the worker reports to the boss it was launched with again")
    reassign.add_argument("--from", dest="sender", help="Lab only: the caller's fleet name")


def charter_set_refusal(caller, role):
    """The refusal text, or None: only the Chief of Staff or the operator (a plain shell: `caller` None) writes a charter; an agent never writes its own or another's (#969)."""
    if caller is None or role == "chief-of-staff":
        return None
    return "only the chief-of-staff or the operator sets a charter; %s is %s" % (charter_safe(caller), "a %s" % charter_safe(role) if role and role != "unidentified" else "not identified as either")


def charter_safe(text):
    """A given value escaped for every sink (audit, stderr, stdout, registry): at most 40 characters of letters, digits and _ . # - (anything else, a newline or markup included, becomes ?)."""
    return re.sub(r"[^A-Za-z0-9_.#-]", "?", str(text)[:40])


def charter_from_refusal(args):
    """The refusal text, or None: `charter set` never takes `--from` (a named caller), in the Lab or out of it: the caller is always the pane's own agent (#1143)."""
    if getattr(args, "sender", None) is not None:
        return "--from is not accepted by charter set; the caller is identified by its pane"
    return None


def cmd_charter(args, runner, now):
    """`charter set <orchestrator> <issue number | none>`: write the orchestrator's registry `charter`, the one record of it (#969). Audited with who, the old and the new value. The live session's metadata
    (written once at launch, never correctable) is not touched and is never read as a second owner."""
    engine, _, _ = build_engine(runner, now, "cli")
    from_refusal = charter_from_refusal(args)
    if from_refusal:
        engine.audit.append(engine.actor, "charter.refused", "agent:%s" % charter_safe(args.name), {"why": "from", "orchestrator": charter_safe(args.name), "caller": "unknown"})
        sys.stderr.write("%s: charter: %s; nothing was changed\n" % (PROG, from_refusal))
        return EXIT_USAGE
    caller, role = move_caller(engine, args, runner, now)
    audit = lambda event, **detail: engine.audit.append(engine.actor, event, "agent:%s" % charter_safe(args.name), dict(detail, caller=charter_safe(caller or "operator"), orchestrator=charter_safe(args.name)))
    refusal = charter_set_refusal(caller, role)
    if refusal:
        audit("charter.refused", why="caller", role=role)
        sys.stderr.write("%s: charter: %s; nothing was changed\n" % (PROG, refusal))
        return EXIT_ERROR
    safe = charter_safe   # every given value is escaped before any sink (audit, stderr, stdout, registry)
    if not NAME_RE.match(args.name or ""):
        audit("charter.refused", why="lane", given=safe(args.name))
        sys.stderr.write("%s: charter: the lane %r is not a valid agent name; nothing was changed\n" % (PROG, safe(args.name)))
        return EXIT_USAGE
    raw = (args.charter or "").strip().lower().lstrip("#")
    value = None if raw == "none" else (int(raw) if re.fullmatch(r"[0-9]{1,9}", raw) and int(raw) > 0 else False)
    if value is False:
        audit("charter.refused", why="value", given=safe(args.charter or ""))
        sys.stderr.write("%s: charter: give a positive issue number or `none`, not %r; nothing was changed\n" % (PROG, safe(args.charter or "")))
        return EXIT_USAGE
    registry = load_module("registry")
    replace = bool(getattr(args, "replace", False))
    caller_name = safe(caller or "operator")
    decided = {}
    pre = {}
    row0 = registry.read(engine.state_dir)["agents"].get(args.name)
    if value is not None and isinstance(row0, dict):   # #1143: the GitHub reads happen BEFORE the registry lock, bound to the issue number and the repo; the lock only re-checks registry facts
        repo0 = row0.get("repo") or (engine.config.get("intent_repo") if engine.config else None)
        try:
            parent = engine.intents.parent(value, repo0, strict=True)   # None only for the "no parent" answer; any other failure raises
            labelled = None
            is_pr = False
            if parent is None:   # a charter has no parent: the one extra read is its label
                engine.intents.invalidate(value, repo0)   # #1249: the label read is never a cached answer
                record = engine.intents.issue(value, repo0)
                labelled = orchestrator_label(engine.config) in (record.labels or [])
                is_pr = bool(getattr(record, "pull_request", False))
            pre = {"key": (value, repo0), "parent": parent, "labelled": labelled, "pr": parent is None and is_pr}
        except Exception as err:
            pre = {"key": (value, repo0), "error": safe(_one_line(str(err), 80))}

    def guard(doc, row):
        """The registry facts, judged by the registry INSIDE its one lock with the write that follows (no check-then-act gap); no GitHub read here."""
        old = row.get("charter")
        decided["old"] = old
        if old is not None and old != value and not replace:
            return ("--replace", "%s already has charter #%s; changing it needs --replace" % (args.name, old))
        if value is not None and old != value:
            repo = row.get("repo") or (engine.config.get("intent_repo") if engine.config else None)
            holders = sorted(n for n, other in doc["agents"].items() if n != args.name and n not in doc["retired"] and other.get("role") == "orchestrator" and other.get("charter") == value and (other.get("repo") or repo) == repo)
            if holders:
                return ("already held by", "charter #%d is already held by %s" % (value, ", ".join(holders)))
            if pre.get("key") != (value, repo):
                return ("unverified", "cannot read #%d's parent (the repository changed)" % value)
            if "error" in pre:
                return ("unverified", "cannot read #%d's parent (%s)" % (value, pre["error"]))
            if pre["parent"] is not None:
                return ("is an ask", "#%d is an ask, not a charter (it has a parent, #%s)" % (value, pre["parent"]))
            if pre.get("pr"):
                return ("not a charter", "#%d is a pull request, not a charter" % value)
            if not pre["labelled"]:
                return ("not a charter", "#%d is not a charter (no parent, but it lacks the %s label)" % (value, orchestrator_label(engine.config)))
        return None
    got = registry.set_charter(engine.state_dir, args.name, value, caller_name, now=now(), check=guard)
    old = decided.get("old")
    if got["problem"]:
        why = got.get("why") or "unknown"
        audit("charter.refused", why=why, old=old, new=value)
        sys.stderr.write("%s: charter: %s; nothing was changed\n" % (PROG, _one_line(got["problem"], 200)))
        return EXIT_USAGE
    audit("charter.set", old=old, new=value, changed=got["changed"], replace=replace)
    print("charter of %s: %s -> %s%s" % (args.name, old, value, "" if got["changed"] else " (unchanged)"))
    row = registry.read(engine.state_dir)["agents"].get(args.name) or {}
    print("registry row: %s role=%s charter=%s repo=%s" % (args.name, row.get("role"), ("#%s" % row["charter"]) if row.get("charter") is not None else "none", safe(row.get("repo"))))
    return EXIT_OK


cmd_charter.ensure_on_use = True


def add_charter_parsers(sub):
    charter = sub.add_parser("charter", help="Set an orchestrator's recorded charter (the Chief of Staff or the operator; audited)")
    charter_sub = charter.add_subparsers(dest="charter_action", required=True)
    setter = charter_sub.add_parser("set", help="Write the registry's charter of an orchestrator: an issue number, or none",
                                    description="The registry row's `charter` is the one record of which charter an orchestrator owns; the lane lookup reads it. The live session's metadata is written once at launch and "
                                                "cannot be corrected, so it is never read as a second owner. Run by the Chief of Staff or the operator; one audit line with the old and the new value.")
    setter.add_argument("name", help="The orchestrator's fleet name")
    setter.add_argument("charter", help="An issue number, or `none`")
    setter.add_argument("--replace", action="store_true", help="Required to change a charter that is already set (a flag: it takes no value)")
    setter.add_argument("--from", dest="sender", help="Lab only: the caller's fleet name")


def cmd_reassign(args, runner, now):
    module = load_module("reassign")
    engine, _, _ = build_engine(runner, now, "cli")
    caller, role = move_caller(engine, args, runner, now)
    sb = types.SimpleNamespace(engine=engine, **{name: globals()[name] for name in module.NEEDS})
    return module.run_reassign(sb, args.worker, args.to_boss, args.consent, args.undo, caller, role, print, lambda text: sys.stderr.write(text + "\n"))


cmd_reassign.ensure_on_use = True   # the daemon is alive when it runs
