# ---- #1735: the approval gate G0c. ONE reader of the plan.approved events (live audit, then the rotated audits newest first) and the merge gate's refusal. Reads no GitHub for the events, writes nothing; docs/switchboard.md, "The approval gate G0c".

APPROVAL_SECONDS = 2.0          # the bound of one read of the events
APPROVAL_FILES = 60             # the bound in files (the live audit and the rotated ones)
APPROVER = "the plan's approver (work item: its orchestrator; ask: the Chief of Staff; epic: the Chief of Staff, then the owner)"


def approval_events(state_dir, issue, current_hash, clock=None, seconds=None, files=None, hashes=None):
    """The plan.approved events of `issue` (subject, or detail.issue), as [{"plan_hash"}] newest first, stopping at the first file that holds one at `current_hash` or at any older member of `hashes` (plan_check.plan_hashes, #1713);
    None when a file cannot be read or the bound (2 seconds, 60 files) is passed: the caller reads that as missing."""
    clock = clock or time.monotonic
    limit = clock() + (APPROVAL_SECONDS if seconds is None else seconds)
    base = Path(state_dir)
    paths = [base / "audit.jsonl"] + sorted(base.glob("audit-*.jsonl.gz"), key=lambda p: p.name, reverse=True)
    bound, read = (APPROVAL_FILES if files is None else files), 0
    want, found, parse = str(issue).lower(), [], load_module("record_json").parse_bounded
    try:
        for path in paths:
            mine = []
            try:
                handle = gzip.open(str(path), "rb") if path.name.endswith(".gz") else open(str(path), "rb")
            except FileNotFoundError:
                continue
            if read >= bound:
                handle.close()
                return None
            read += 1
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
                        mine.append({"plan_hash": plan_check.canonical_hash(detail["plan_hash"], hashes)})   # #1713: an event at an older member reads at the current hash
            found.extend(reversed(mine))
            if any(e["plan_hash"] == current_hash for e in mine) or clock() > limit:
                break
        return found if clock() <= limit else None
    except Exception:   # a corrupt gzip raises zlib.error, which is no OSError: every read failure is missing
        return None


def approval_state(events, issue, current_hash, hashes=None):
    """#1735, G0c: (status, detail) from the plan.approved events of `issue`, newest first, each {"plan_hash": ...}; None = they could not be read. Pure: it reads no file.
    #1713: `hashes` is plan_check.plan_hashes(text): an event at ANY member of the pair of the live text holds the current hash.
    ok when an event holds the current hash; missing when none does (the detail names the current hash and the newest approved one), the events were unreadable, or no plan issue is named (fail closed)."""
    if not issue:
        return "missing", "no plan issue named: G0c cannot find an approval"
    if events is None:
        return "missing", "the plan.approved events of %s could not be read in time: refused (fails closed)" % issue
    seen = [plan_check.canonical_hash(e["plan_hash"], hashes) for e in events if isinstance(e, dict) and isinstance(e.get("plan_hash"), str)]
    if current_hash and current_hash in seen:
        return "ok", "approved at plan hash %s" % current_hash
    if seen:
        return "missing", "no approval of %s at its current plan hash %s; the newest approved hash is %s" % (issue, current_hash, seen[0])
    return "missing", "no plan.approved event for %s (current plan hash %s)" % (issue, current_hash)


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


def approval_fact(config, state, pr, source, plan):
    """facts["approval"] when the gate is on for this repo, else None (G0c stays untracked): {"G0c": the row's tracked, status, detail, missing}. The current hash is the plan's own (plan_check.plan_hash)."""
    if not approval_on(config, pr.rsplit("#", 1)[0]):
        return None
    issue = approval_issue(source, config)
    hashes, current = (plan or {}).get("plan_hashes"), (plan or {}).get("plan_hash")
    events = approval_events(state, issue, current, hashes=hashes) if issue and plan else ([] if issue is None else None)
    status, said = approval_state(events, issue, current, hashes)
    return {"G0c": {"tracked": True, "status": status, "detail": said, "missing": [said] if status == "missing" else []}}


def approval_refusal(args, runner, now):
    """#1735: the merge gate's refusal line for a pull request into any base whose plan has no plan.approved event at its current hash, else None.
    Only where the repo's enforce_gates names G0c; a head-bound `G0c` waiver passes it and is audited. Stands after the claim union and before the CI check."""
    try:
        repo, number = parse_pr_ref(args.pr)
        config = load_runnable_config()
        on = approval_on(config, repo)
    except Exception:   # a setting that cannot be read is not an absent setting: the merge is held (a broken config is fixed, not waived)
        return "merge of %s refused: the approval gate setting could not be read: held" % args.pr
    if not on:   # only a clean, absent setting (read, and G0c not named) means the gate is off
        return None
    try:
        ctx = _ledger_context(types.SimpleNamespace(pr=args.pr, plan=None, ask=None), runner, now, False)
        facts = dict(ctx.facts or {}, approval=approval_fact(ctx.config, ctx.state, ctx.pr, ctx.source, ctx.plan))
        row = (facts.get("approval") or {}).get("G0c") or {"status": "missing", "detail": "the plan could not be read"}
        status, said = row["status"], row["detail"]
    except Exception as err:
        return "merge of %s refused: the plan approval could not be judged (%s); the gate fails closed" % (args.pr, _one_line(str(err), 100))
    if status != "missing":
        return None
    head = (ctx.facts or {}).get("head")
    if not ctx.problem and head and "G0c" in plan_ledger.valid_waivers(ctx.entries, ctx.pr, head, now()):
        try:
            AuditLog(ctx.state / "audit.jsonl").append("cli", "plan.approval.waived", ctx.pr, {"pr": ctx.pr, "head": head, "waived": "G0c: " + said})
        except Exception:
            return "merge of %s refused: the waiver of G0c at head %s could not be recorded in the audit; an unaudited waiver is not honoured (the gate fails closed)" % (ctx.pr, head)
        return None
    return "merge of %s refused: %s; ask %s for the approval, or for a logged waiver (`%s waive %s --gates G0c --reason ...`)" % (ctx.pr, said, APPROVER, PROG, ctx.pr)
