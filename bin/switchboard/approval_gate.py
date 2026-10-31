# ---- #1735: the approval gate G0c. ONE reader of the plan.approved events (live audit, then the rotated audits newest first) and the merge gate's refusal. Reads no GitHub for the events, writes nothing; docs/switchboard.md, "The approval gate G0c".

APPROVAL_SECONDS = 2.0          # the bound of one read of the events
APPROVAL_FILES = 60             # the bound in files (the live audit and the rotated ones)
APPROVAL_BASES = ("integration",)   # a pull request whose base is this branch, or the repository's default branch, is held to the gate
APPROVER = "the plan's approver (work item: its orchestrator; ask: the Chief of Staff; epic: the Chief of Staff, then the owner)"


def approval_events(state_dir, issue, current_hash, clock=None, seconds=None, files=None):
    """The plan.approved events of `issue` (subject, or detail.issue), as [{"plan_hash"}] newest first, stopping at the first file that holds one at `current_hash`;
    None when a file cannot be read or the bound (2 seconds, 60 files) is passed: the caller reads that as missing."""
    clock = clock or time.monotonic
    limit = clock() + (APPROVAL_SECONDS if seconds is None else seconds)
    base = Path(state_dir)
    paths = [base / "audit.jsonl"] + sorted(base.glob("audit-*.jsonl.gz"), key=lambda p: p.name, reverse=True)
    if len(paths) > (APPROVAL_FILES if files is None else files):
        return None
    want, found, parse = str(issue).lower(), [], load_module("record_json").parse_bounded
    try:
        for path in paths:
            mine = []
            try:
                handle = gzip.open(str(path), "rb") if path.name.endswith(".gz") else open(str(path), "rb")
            except FileNotFoundError:
                continue
            with handle:
                for count, raw in enumerate(handle):
                    if count % 500 == 0 and clock() > limit:
                        return None
                    if b"plan.approved" not in raw:
                        continue
                    try:
                        entry = parse(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    detail = entry.get("detail") if isinstance(entry, dict) else None
                    if not isinstance(entry, dict) or entry.get("event") != "plan.approved" or not isinstance(detail, dict):
                        continue
                    if want in (str(entry.get("subject") or "").lower(), str(detail.get("issue") or "").lower()) and isinstance(detail.get("plan_hash"), str):
                        mine.append({"plan_hash": detail["plan_hash"]})
            found.extend(reversed(mine))
            if any(e["plan_hash"] == current_hash for e in mine) or clock() > limit:
                break
        return found if clock() <= limit else None
    except (OSError, EOFError, ValueError, UnicodeError, RecursionError):
        return None


def approval_issue(source, config):
    """`owner/repo#N` of the plan source a pull request names, or None (no plan link, or one that is no issue)."""
    try:
        repo, number, _ = parse_plan_source(source, (config or {}).get("intent_repo")) if source else (None, None, None)
    except PlanInputError:
        return None
    return "%s#%d" % (repo, number) if repo else None


def approval_on(config, repo):
    """True when the repo's setting (the owner's configuration) enables the gates in `enforce` mode and names G0c in its enforce_gates; any other repo: the gate is off."""
    setting = plan_ledger.repo_setting(config or {}, repo)
    return bool(setting and setting.get("enabled") and setting.get("mode") == "enforce" and "G0c" in (setting.get("enforce_gates") or ()))


def approval_facts(facts, config, state, pr, source, plan):
    """`facts` with the approval facts when the gate is on for this repo, else `facts` as it is (G0c stays untracked). The current hash is the plan's own (plan_check.plan_hash)."""
    if not isinstance(facts, dict) or not approval_on(config, pr.rsplit("#", 1)[0]):
        return facts
    issue = approval_issue(source, config)
    events = approval_events(state, issue, (plan or {}).get("plan_hash")) if issue and plan else ([] if issue is None else None)
    return dict(facts, approval={"issue": issue, "events": events})


def approval_refusal(args, runner, now):
    """#1735: the merge gate's refusal line for a pull request into the integration branch or the default branch whose plan has no plan.approved event at its current hash, else None.
    Only where the repo's enforce_gates names G0c; a head-bound `G0c` waiver passes it and is audited. Stands after the claim union and before the CI check."""
    try:
        repo, number = parse_pr_ref(args.pr)
        config = load_runnable_config()
        if not approval_on(config, repo):
            return None
        reading, gh = load_module("pr_reading"), merge_gate_gh(config, runner, repo)
        base = reading.read_pr(gh, repo, number)["base"]
        if base not in APPROVAL_BASES and base != reading.read_default_branch(gh, repo):
            return None
        ctx = _ledger_context(types.SimpleNamespace(pr=args.pr, plan=None, ask=None), runner, now, False)
        facts = approval_facts(ctx.facts, ctx.config, ctx.state, ctx.pr, ctx.source, ctx.plan)
        status, said = plan_ledger.approval_state(facts["approval"]["events"], facts["approval"]["issue"], (ctx.plan or {}).get("plan_hash")) if facts.get("approval") else ("missing", "the plan could not be read")
    except Exception as err:
        return "merge of %s refused: the plan approval could not be judged (%s); the gate fails closed" % (args.pr, _one_line(str(err), 100))
    if status != "missing":
        return None
    head = (ctx.facts or {}).get("head")
    if not ctx.problem and head and "G0c" in plan_ledger.valid_waivers(ctx.entries, ctx.pr, head, now()):
        try:
            AuditLog(ctx.state / "audit.jsonl").append("cli", "plan.approval.waived", ctx.pr, {"pr": ctx.pr, "head": head, "waived": said})
        except OSError:
            pass
        return None
    return "merge of %s refused: %s; ask %s for the approval, or for a logged waiver (`%s waive %s --gates G0c --reason ...`)" % (ctx.pr, said, APPROVER, PROG, ctx.pr)
