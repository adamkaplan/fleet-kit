# ---- how an answer reaches the asker, behind the AnswerDelivery port: each class is a path that was always taken
# here, with its refusals and messages unchanged (the callers keep the wording that is theirs).

class IssueDelivery(decision_core.AnswerDelivery):
    """The issue path: a comment signed by the answerer and awaiting-label changes on the issue. The answer is
    {repo, number, add, remove, comment (text or None), author, comment_first}; the labels go first unless
    `comment_first`. Audited before and after, each write on its own."""

    def __init__(self, engine, runner):
        self.engine, self.runner = engine, runner

    def deliver(self, decision, answer):
        engine, runner, repo, number = self.engine, self.runner, answer["repo"], answer["number"]
        steps = [lambda: change_issue_labels(engine, runner, repo, number, answer.get("add", ()), answer.get("remove", ()))]
        if answer.get("comment"):
            comment = lambda: comment_as(engine, runner, repo, number, answer["comment"], answer.get("author"))
            steps = [comment] + steps if answer.get("comment_first") else steps + [comment]
        for step in steps:
            step()


class ReportSendDelivery(decision_core.AnswerDelivery):
    """The report path: a `send` to the worker that asked, which is what clears a report. The answer is {by, to, text,
    ask, repo}; returns the send's outcome ({sent, reason, ...}) and raises what the send raises: the caller words
    the refusal."""

    def __init__(self, engine):
        self.engine = engine

    def deliver(self, decision, answer):
        return self.engine.send(answer["by"], answer["to"], answer["text"], answer["ask"], answer.get("repo"))


class HarnessReplyDelivery(decision_core.AnswerDelivery):
    """The harness path: a reply to an agent's pending permission request (`allow` is `once`, `deny` is `reject`) or
    question (the one field of its form), through v2. The answer is {value, note, by}. Returns the line the command
    prints; raises DecisionError for a refusal."""

    def __init__(self, engine):
        self.engine = engine

    def deliver(self, decision, answer):
        engine, value, note, name = self.engine, answer["value"], answer.get("note"), answer.get("by")
        kind, ident = decision["kind"], decision["id"]
        session, request = decision.get("session"), decision.get("request")
        if not session or not request:
            raise DecisionError("%s names no request to reply to" % ident)
        subject = "request:%s:%s" % (session, request)
        detail = {"decision": ident, "kind": kind, "agent": decision.get("agent"), "by": name,
                  "note": _one_line(note or "", 200)}
        if kind == "permission":
            choice = value.strip().lower()
            if choice not in ("allow", "deny"):
                raise DecisionError("%s is a permission request: answer `allow` or `deny`" % ident)
            with engine._external("decisions.answer", subject, dict(detail, answer=choice)):
                engine.v2.permission_reply(session, request, "once" if choice == "allow" else "reject", note)
            return "answered %s: %s" % (ident, choice)
        forms = engine.v2.form_list(session)
        form = next((f for f in forms if isinstance(f, dict) and f.get("id") == request), None) \
            if isinstance(forms, list) else None
        if form is None:
            raise DecisionError("%s is no longer pending: the agent's form is gone" % ident)
        answer = _form_answer(form.get("fields") or [], value)
        with engine._external("decisions.answer", subject, dict(detail, answer=_one_line(value, 200))):
            engine.v2.form_reply(session, request, answer)
        return "answered %s" % ident


_DECISION_REF = re.compile(r"^(?:([A-Za-z0-9_.-]+)/)?([A-Za-z0-9_.-]+)#([0-9]+)$")


def _decision_ref(ident):
    """(owner or None, repo or None, number) for the issue forms a person types for a decision (`OWNER/REPO#N`, `REPO#N`, `#N`, `N`), else None."""
    text = (ident or "").strip()
    bare = re.fullmatch(r"#?([0-9]+)", text)
    if bare:
        return None, None, int(bare.group(1))
    got = _DECISION_REF.match(text)
    if not got:
        return None
    return (got.group(1).lower() if got.group(1) else None), got.group(2).lower(), int(got.group(3))


def _decisions_for_ref(decisions, owner, repo, number):
    """The decisions about issue `number` whose repository matches the spelling: the full name when an owner is given, the short name when only a repo
    is, any when neither."""
    found = []
    for decision in decisions:
        if decision.get("kind") != "issue" or decision.get("ask") != number:
            continue   # only an ISSUE decision is named by its issue: a permission or a question that is about the issue has an id of its own
        full = str(decision.get("repo") or "").lower()
        short = full.rsplit("/", 1)[-1]
        if owner and full != "%s/%s" % (owner, repo):
            continue
        if not owner and repo and short != repo:
            continue
        found.append(decision)
    return found


def decision_by_id(decisions, ident):
    """The decision an id names. The id the list shows (`fleet-kit#357`, or `#357`) matches exactly, as it always did; the issue forms a person naturally types
    (`OWNER/REPO#N`, `REPO#N`, `#N`) also find it. A spelling that names more than one decision is refused and lists the candidates, never guessed
    (DecisionError); None when none matches (see `decision_miss_hint`). Only the spellings are widened: what follows (the tier check, the caller check, the
    audit) is unchanged."""
    for wanted in (ident, "#" + ident.lstrip("#")):
        for decision in decisions:
            if decision["id"] == wanted:
                return decision
    ref = _decision_ref(ident)
    if ref is None:
        return None
    found = _decisions_for_ref(decisions, *ref)
    if len(found) > 1:
        raise DecisionError("%s names %d decisions: %s. Name one with its owner and repository (OWNER/REPO#%d)" % (
            ident, len(found), ", ".join(sorted(str(d["id"]) for d in found)), ref[2]))
    return found[0] if found else None


def persisted_pr_decision(engine, ident, action, now):
    """(#712) The decision a `pr:` id names in the daemon's persisted file, or None, when the command line's own derivation did not find it: a pull request held
    on its hold labels alone is derived only by the daemon's passes (the hub it reads is not filled in this process). The file is trusted only for "this pull
    request was listed": a missing, unreadable or stale file is a miss, and an escalation of a row that is not yet the owner's is taken as a PR notice, whose path
    reads the pull request NOW and decides from that. Every other action and every check after the lookup see the row as the file lists it."""
    if not str(ident).startswith("pr:") or engine.state_dir is None:
        return None
    report = decisions_report(engine.state_dir, now() if callable(now) else (time.time() if now is None else now))
    if report["stale"]:
        return None
    for row in report["decisions"]:
        if not isinstance(row, dict) or row.get("kind") not in ("captain", "notice") or not str(row.get("id", "")).startswith("pr:"):
            continue
        number = row.get("number")
        full = "pr:%s#%s" % (row.get("repo"), number)
        if ident in (row["id"], full):
            return dict(row, kind="notice") if action == "escalate" and row.get("tier") != "human" else row
    return None


def decision_or_persisted(engine, decisions, ident, action, now):
    """decision_by_id, then (only on a miss) the persisted file for a `pr:` id; None when neither has it."""
    return decision_by_id(decisions, ident) or persisted_pr_decision(engine, ident, action, now)


def decision_miss_hint(decisions, ident):
    """What to add to a "no decision is waiting" error: the ids of the decisions that carry the same number (the right form is one copy away), or ""."""
    ref = _decision_ref(ident)
    if ref is None:
        return ""
    same = sorted({str(d["id"]) for d in decisions if d.get("kind") == "issue" and d.get("ask") == ref[2]})
    return " (a decision numbered %d is listed as %s)" % (ref[2], ", ".join(same)) if same else ""


def refuse_human_tier(engine, decision, action, name, role):
    """#105: the Chief of Staff answers, resolves and supersedes ONLY the decisions of its own tier (`cos`), never one that is
    the owner's (`human`, or any other tier). You, in a plain shell, may do anything. Audited when refused."""
    if role != "chief-of-staff" or action == "escalate":
        return
    if decision.get("tier") != "cos":
        engine.audit.append(engine.actor, "decisions.refused", decision["id"],
                            {"caller": name, "role": role, "action": action, "tier": decision.get("tier")})
        raise DecisionError("%s is on the %s tier: the Chief of Staff %ss only decisions of its own (cos) tier. It is the "
                            "owner's: leave it, or escalate it with a reason" % (
                                decision["id"], decision.get("tier") or "human", action))


def decision_actor(engine, args):
    """(name, role) of who runs a decision write: a fleet agent by its pane (`--from` in the Lab), else (None, None):
    you, in a plain shell. Only the Chief of Staff or you may write; any other fleet agent is refused."""
    if getattr(args, "sender", None) is not None:
        name, role, _ = orders_caller(engine, args)
        return name, role
    try:
        name, role, _ = orders_caller(engine, args)
    except IdentityUncertain:
        raise  # an agent whose identity is uncertain is not "you, in a plain shell"
    except CallerError:
        return None, None
    return name, role


def _issue_target(engine, decision):
    """(repo, number) of the issue a decision is about, or None."""
    if not decision.get("ask"):
        return None
    repo = decision.get("repo") or (engine.intents.repo if engine.intents is not None else None) \
        or engine.config.get("intent_repo")
    watched = decision_repos(engine)
    return (repo or (watched[0] if len(watched) == 1 else None)), decision["ask"]


class DecisionReadError(DecisionError):
    """A decision command could not read what it needs from GitHub: nothing was changed (exit 1, not a usage error)."""


def escalate_notice(engine, runner, decision, reason, name):
    """(#487) `decisions escalate` on a PR notice. A notice is information: it waits on no one, so it never answers "already yours". The pull request is read
    NOW (one `gh pr view`, through its repository's account): when it carries the merge hold at its current head it is escalated exactly as the listed hold
    would be (the file can trail a label by a few minutes); when it does not, the command says it is only a notice and that nothing was escalated; when it
    cannot be read it says so and changes nothing."""
    ident, repo, number = decision["id"], decision.get("repo"), decision.get("number")
    if not str(ident).startswith("pr:") or not repo or isinstance(number, bool) or not isinstance(number, int):
        raise DecisionError("%s is a notice: information only, so nothing was escalated (it ends by itself)" % ident)
    try:
        facts = pr_facts_from(read_pr_json(engine, repo, number))
    except Exception as err:   # an unreadable pull request is not a pull request that holds nothing
        raise DecisionReadError("%s: the pull request %s#%d could not be read (%s): nothing was changed" % (
            ident, repo, number, _one_line(str(err), 120))) from None
    if facts.get("state") != "open":
        raise DecisionError("%s: the pull request is %s, so there is nothing to escalate (a notice ends then)" % (ident, facts.get("state") or "not open"))
    if not facts.get("captain") or not facts.get("head"):
        raise DecisionError("%s is a PR notice: information only, so nothing was escalated. It becomes a decision when the pull request carries the merge hold "
                            "(%s, or `MERGE: captain` first in its body)" % (ident, label_names(engine.config)["merge-captain"]))
    held = {"id": ident, "kind": "captain", "tier": "cos", "repo": repo, "number": number, "head": facts["head"]}
    return escalate_decision(engine, runner, held, reason, name)


def escalate_decision(engine, runner, decision, reason, name):
    """Move a decision to you. An issue (or the issue a report is about): your label on, the Chief of Staff's off. A
    permission request or question: marked yours at once, in escalated-requests.json (disposable; the pass reads it)
    and in the audit log. Returns one line saying what was done."""
    kind, ident = decision["kind"], decision["id"]
    if kind == "notice":
        return escalate_notice(engine, runner, decision, reason, name)   # #487: never "already yours" for information that waits on no one
    if decision.get("tier") == "human":
        return "%s is already yours: nothing changed" % ident
    if kind in ("issue", "report"):
        target = _issue_target(engine, decision)
        if target is None or target[0] is None:
            if kind == "report":
                raise DecisionError("%s is a report that names no issue, so there is nothing to label: put it to "
                                    "the owner in chat" % ident)
            raise DecisionError("%s: cannot tell which repo its issue is in" % ident)
        repo, number = target
        IssueDelivery(engine, runner).deliver(decision, {
            "repo": repo, "number": number, "add": [awaiting_label(engine.config)],
            "remove": [awaiting_cos_label(engine.config)], "author": name,
            "comment": "Escalated to the owner: %s" % reason if reason else None})
        engine._note("decisions.escalate", ident, {"kind": kind, "repo": repo, "issue": number, "by": name,
                                                  "reason": _one_line(reason or "", 200)})
        return "escalated %s to you: %s on %s#%d" % (ident, awaiting_label(engine.config), repo, number)
    if kind == "captain":
        if engine.state_dir is None or not decision.get("head"):
            raise DecisionError("%s cannot be escalated: it names no head" % ident)
        key = "captain:%s#%d@%s" % (decision["repo"], decision["number"], decision["head"])
        with engine._external("decisions.escalate", "request:%s" % key, {"decision": ident, "kind": kind, "by": name,
                                                                          "reason": _one_line(reason or "", 200)}):
            decision_store(engine).mark_escalated(key, engine.now())
        return "escalated %s to you: the MERGE: captain PR is waiting on you" % ident
    if engine.state_dir is None or not decision.get("session") or not decision.get("request"):
        raise DecisionError("%s cannot be escalated: it names no request" % ident)
    key = "%s:%s" % (decision["session"], decision["request"])
    with engine._external("decisions.escalate", "request:%s" % key, {"decision": ident, "kind": kind,
                                                                      "agent": decision.get("agent"), "by": name,
                                                                      "reason": _one_line(reason or "", 200)}):
        decision_store(engine).mark_escalated(key, engine.now())
    return "escalated %s to you: the %s %s is waiting on you" % (ident, decision.get("agent") or "agent", kind)


def _refuse_notice(decision):
    """A PR notice is information: there is nothing to resolve or answer, and it ends when its PR merges or closes."""
    if decision["kind"] == "captain":
        raise DecisionError("%s is a MERGE: captain PR: merge or close the PR (it ends then), or escalate it" % decision["id"])
    if decision["kind"] == "notice":
        raise DecisionError("%s is a PR notice: information, so there is nothing to resolve or answer. It ends when "
                            "the PR merges or closes" % decision["id"])


def resolve_decision(engine, runner, decision, note, name):
    """Take both awaiting labels off the issue. A report is resolved by answering its worker with `send`; its issue's
    labels are the only thing this removes."""
    kind, ident = decision["kind"], decision["id"]
    _refuse_notice(decision)
    if kind in ("permission", "question"):
        raise DecisionError("%s is a %s: answer it with `%s decisions answer %s ...`" % (ident, kind, PROG, ident))
    target = _issue_target(engine, decision)
    if target is None or target[0] is None:
        raise DecisionError("%s is a report that names no issue: answer the worker with `%s send`, which resolves it"
                            % (ident, PROG))
    repo, number = target
    IssueDelivery(engine, runner).deliver(decision, {
        "repo": repo, "number": number, "add": (),
        "remove": [awaiting_cos_label(engine.config), awaiting_label(engine.config)], "author": name,
        "comment": "Resolved by the Chief of Staff: %s" % note if note else None})
    engine._note("decisions.resolve", ident, {"kind": kind, "repo": repo, "issue": number, "by": name,
                                             "note": _one_line(note or "", 200)})
    return "resolved %s: %s and %s are off %s#%d" % (ident, awaiting_cos_label(engine.config),
                                                      awaiting_label(engine.config), repo, number)


def _form_answer(fields, value):
    """{key: value} for the one visible field of a form, or DecisionError when the form is more than a question."""
    visible = [f for f in fields if isinstance(f, dict) and not f.get("hidden") and isinstance(f.get("key"), str)]
    if len(visible) != 1:
        raise DecisionError("this form has %d visible fields (%s): answer it in its own pane, or tell the owner"
                            % (len(visible), ", ".join(f.get("key", "?") for f in visible) or "none"))
    field = visible[0]
    kind = field.get("type")
    if kind == "string":
        return {field["key"]: value}
    if kind == "boolean":
        word = value.strip().lower()
        if word in ("yes", "y", "true", "allow"):
            return {field["key"]: True}
        if word in ("no", "n", "false", "deny"):
            return {field["key"]: False}
        raise DecisionError("this question is yes or no: answer yes or no")
    if kind in ("number", "integer"):
        try:
            return {field["key"]: int(value) if kind == "integer" else float(value)}
        except ValueError:
            raise DecisionError("this question wants a number") from None
    raise DecisionError("this question's field is a %s, which `decisions answer` does not fill: answer it in its "
                        "own pane, or tell the owner" % kind)


def answer_decision(engine, decision, value, note, name):
    """Reply to an agent's pending permission request (`allow` is `once`, `deny` is `reject`) or question (the one
    field of its form), through v2. Audited before and after."""
    kind, ident = decision["kind"], decision["id"]
    _refuse_notice(decision)
    if kind not in ("permission", "question"):
        raise DecisionError("%s is %s: `decisions answer` is for a permission request or a question. Answer a report "
                            "with `%s send`; an issue with a comment, then `decisions resolve`"
                            % (ident, "an issue" if kind == "issue" else "a report", PROG))
    return HarnessReplyDelivery(engine).deliver(decision, {"value": value, "note": note, "by": name})


def read_batch_rows(args):
    """The rows of a `decisions batch`: every --row, then the lines of --file (`-` is stdin), or stdin when neither
    was given and it is piped. Raises BatchError."""
    lines = list(args.row or [])           # an empty --row is refused below; blank lines of a file or stdin are skipped
    more = []
    if args.file is not None:
        try:
            more = (sys.stdin.read() if args.file == "-" else Path(args.file).read_text()).splitlines()
        except OSError as err:
            raise BatchError("cannot read %s: %s" % (args.file, err.strerror or err)) from None
    elif not lines and not sys.stdin.isatty():
        more = sys.stdin.read().splitlines()
    lines.extend(line for line in more if line.strip())
    if not lines:
        raise BatchError("a batch needs rows: --row \"<repo#N> | <finding> | <recommendation>\" (repeat it), or --file")
    if len(lines) > decision_core.BATCH_RAISE_ROWS_MAX:
        raise BatchError("a batch is at most %d rows (this one is %d): raise the rest as a second batch"
                         % (decision_core.BATCH_RAISE_ROWS_MAX, len(lines)))
    rows = [parse_batch_row(line) for line in lines]   # the FULL text: it is cut only when a list or the panel draws it
    for number, row in enumerate(rows, 1):
        for part, text, cap in (("finding", row["finding"], decision_core.BATCH_FINDING_RAISE_MAX),
                                ("recommendation", row["recommendation"], decision_core.BATCH_REC_RAISE_MAX)):
            if len(text) > cap:
                raise BatchError("the %s of row %d is %d characters: it is at most %d; shorten it or split the row, nothing was raised"
                                 % (part, number, len(text), cap))
    return rows


def cmd_decision_batch(args, runner, now):
    """`decisions batch`: an orchestrator raises related findings as ONE issue in ITS OWN repo, labelled for the
    Chief of Staff, with the rows as Markdown sections. Creates nothing anywhere else."""
    try:
        engine, _, config = build_engine(runner, now, "cli")
        name, role, fleet = orders_caller(engine, args)
        if role != "orchestrator":
            raise BatchError("only an orchestrator raises a batch (%s is %s): a worker reports to its orchestrator"
                             % (name, "a %s" % role if role else "not given a role"))
        repo = _fleet_repo(fleet, config)
        if repo is None:
            raise BatchError("%s has no repo in its fleet metadata: a batch is raised in your own repo" % name)
        title = " ".join((args.title or "").split())
        if not title or len(title) > BATCH_TITLE_MAX:
            raise BatchError("--title is one line of 1 to %d characters" % BATCH_TITLE_MAX)
        rows = read_batch_rows(args)
        for row in rows:
            named = row_repo(row["ref"])
            if named is not None and named != repo or named is None and row["ref"].split("#")[0] != repo.split("/")[1]:
                raise BatchError("row %r names an issue outside %s: a batch holds only issues of its own repo, so a "
                                 "finding about a private repo can never land in a public one's batch. Raise it from "
                                 "that repo's orchestrator" % (_one_line(row["ref"], 60), repo))
        body = batch_body(name, rows)
        if len(body) > decision_core.BATCH_BODY_MAX_CHARS:
            raise BatchError("the batch is %d characters: the issue body is at most %d (GitHub's limit is 65536); raise the rest as a second batch, nothing was raised"
                             % (len(body), decision_core.BATCH_BODY_MAX_CHARS))
        subject = "issue:%s" % repo
        with engine._external("decisions.batch", subject, {"repo": repo, "rows": len(rows), "by": name}):
            done = _gh_write(engine, runner, repo, "repos/%s/issues" % repo, "POST",
                             {"title": title, "body": body,
                              "labels": [awaiting_cos_label(config)]})
        try:
            made = json.loads(done.stdout or "")
            number = int(made["number"])
        except (ValueError, KeyError, TypeError):
            raise BatchError("gh created the issue but answered something unreadable: look for it in %s" % repo) from None
    except (BatchError, CallerError, IntentError) as err:
        sys.stderr.write("%s: %s\n" % (PROG, _one_line(str(err), 400)))
        return EXIT_USAGE
    print(issue_tag(repo, number, decision_repos(engine)))
    sys.stderr.write("%s: batch of %d rows raised as %s#%d, waiting on the Chief of Staff\n"
                     % (PROG, len(rows), repo, number))
    try:
        send_poke(state_dir_path())
    except Exception:
        pass
    return EXIT_OK


def batch_answer_rows(decision, args):
    """[(row, outcome text, changed)] for `decisions answer <batch> --as-recommended | --row N=<choice> ...`: a row
    not named keeps its recommendation. Raises DecisionError."""
    rows, wanted = decision["rows"], {}
    if args.as_recommended == bool(args.row):
        raise DecisionError("%s is a batch of %d rows: answer it with --as-recommended, or --row N=<choice> for the "
                            "rows that change (the rest keep their recommendation); not both, not neither"
                            % (decision["id"], len(rows)))
    for item in args.row or []:
        found = re.match(r"^([0-9]+)=(.+)$", item.strip())
        if not found or not " ".join(found.group(2).split()):
            raise DecisionError("%r is not a row answer: write --row N=<choice>" % _one_line(item, 60))
        number = int(found.group(1))
        if not 1 <= number <= len(rows):
            raise DecisionError("row %d does not exist: %s has rows 1 to %d" % (number, decision["id"], len(rows)))
        if number in wanted:
            raise DecisionError("row %d is answered twice" % number)
        wanted[number] = _one_line(found.group(2), BATCH_REC_MAX * 2)
    out = []
    for number, row in enumerate(rows, 1):
        choice = wanted.get(number)
        if choice is None or choice.lower() == "as recommended":
            out.append((row, "as recommended: %s" % row["recommendation"], False))
        else:
            out.append((row, "changed: %s (recommended: %s)" % (choice, row["recommendation"]), True))
    return out


def batch_answer_lines(outcomes, note):
    """The lines of the comment that records a batch answer (the one place its format is built, so a test measures what is posted)."""
    changed = sum(1 for _, _, c in outcomes if c)
    lines = ["Answer to this batch (%d rows): %s." % (len(outcomes), "as recommended" if not changed else
                                                      "%d row%s changed, the rest as recommended"
                                                      % (changed, "" if changed == 1 else "s"))]
    lines += ["%d. %s: %s" % (n, row["ref"], text) for n, (row, text, _) in enumerate(outcomes, 1)]
    if note:
        lines.append("Note: %s" % _one_line(note, 300))
    lines.append("The row issues were not changed by this answer: the orchestrator that raised the batch acts on it.")
    return lines


def answer_batch(engine, runner, decision, args, name):
    """First the note to the orchestrator that raised the batch; only once it is delivered, ONE comment on the batch
    issue recording every row's outcome, then both awaiting labels off it, so the batch and all its rows leave the
    list together. A note that cannot be delivered (agent gone, held, error) refuses the answer and writes nothing:
    the batch stays in the cos tier with its labels and no comment, and the same answer can be run again once, with
    no duplicate note or comment. (A batch with no known raiser, or answered with no caller name, has no one to tell.)
    Never touches a row's issue: a row may name an issue only proposed for closing, and the orchestrator acts."""
    ident = decision["id"]
    target = _issue_target(engine, decision)
    if target is None or target[0] is None:
        raise DecisionError("%s: cannot tell which repo its issue is in" % ident)
    repo, number = target
    outcomes = batch_answer_rows(decision, args)
    changed = sum(1 for _, _, c in outcomes if c)
    lines = batch_answer_lines(outcomes, args.note)
    size = len("\n".join(lines))
    if size > decision_core.BATCH_COMMENT_MAX_CHARS:   # before ANY side effect (the note, the comment, the labels): refused whole, nothing sent or written
        raise DecisionError("%s was not answered: the answer comment would be %d characters and a comment holds at most %d (the rows' recommendations are long); "
                            "nothing was sent or written. Shorten the --row choices or the --note, or answer from the issue itself"
                            % (ident, size, decision_core.BATCH_COMMENT_MAX_CHARS))
    told = ""
    note_key = None
    raised = decision.get("raised_by")
    if raised and name:
        text = "Batch %s (%s#%d) is answered: %s. The record is the comment on it; you act on it, nothing was done to " \
               "the row issues." % (ident, repo, number, "as recommended" if not changed else "%d row(s) changed" % changed)
        note_key = batch_note_key(repo, number, text + "\n" + "\n".join(lines)) if engine.state_dir is not None else None   # the answer's identity: every row's outcome and the note, not counts
        if note_key is not None and note_key in read_batch_notes(engine.state_dir):
            raised = None                  # this very note went on an earlier run whose comment failed: not sent a second time
        try:
            outcome = ReportSendDelivery(engine).deliver(decision, {"by": name, "to": raised, "text": text,
                                                                    "ask": number, "repo": repo}) if raised else {"sent": True}
        except Exception as err:
            raise DecisionError("%s was not answered: the note to %s could not be delivered (%s); its labels stay, the "
                                "batch stays with the Chief of Staff, nothing was commented. Run the same answer again "
                                "once %s can be reached" % (ident, raised, _one_line(str(err), 100), raised)) from None
        if not outcome.get("sent"):
            raise DecisionError("%s was not answered: the note to %s was not delivered (%s); its labels stay, the batch "
                                "stays with the Chief of Staff, nothing was commented. Run the same answer again once "
                                "%s can be reached" % (ident, raised, outcome.get("reason") or outcome.get("action"), raised))
        if raised and note_key is not None:
            try:
                mark_batch_note(engine.state_dir, note_key, engine.now())
            except OSError:
                pass
        told = "; told %s" % raised if raised else "; already told %s" % decision.get("raised_by")
    IssueDelivery(engine, runner).deliver(decision, {
        "repo": repo, "number": number, "add": (), "remove": [awaiting_cos_label(engine.config), awaiting_label(engine.config)],
        "author": name, "comment": "\n".join(lines), "comment_first": True})
    if note_key is not None:               # fully recorded (note sent, comment written): a later answer, even the same text, sends its own note
        try:
            mark_batch_note(engine.state_dir, note_key, None)
        except OSError:
            pass
    engine._note("decisions.answer", ident, {"kind": "batch", "repo": repo, "issue": number, "by": name,
                                            "rows": len(outcomes), "changed": changed,
                                            "note": _one_line(args.note or "", 200)})
    return "answered %s: %d rows (%d as recommended, %d changed); %s and %s are off %s#%d%s" % (
        ident, len(outcomes), len(outcomes) - changed, changed, awaiting_cos_label(engine.config),
        awaiting_label(engine.config), repo, number, told)


def parse_superseding(engine, text):
    """`owner/repo#N`, `name#N` (a watched repo's name) or an issue or pull request URL -> (repo, number, as shown)."""
    text = (text or "").strip()
    found = _GITHUB_REF.match(text)
    if found:
        repo, number = "%s/%s" % (found.group(1), found.group(2)), int(found.group(3))
    else:
        short = _SHORT_REF.fullmatch(text)
        repo = repo_of_tag(short.group(1), decision_repos(engine)) if short else None
        if repo is None:
            raise DecisionError("--by names the superseding issue as owner/repo#N, name#N for a watched repo, or its "
                                "URL (got %r)" % _one_line(text, 60))
        number = int(short.group(2))
    if number < 1:
        raise DecisionError("--by names no issue: %r" % _one_line(text, 60))
    return repo, number, "%s#%d" % (repo, number)


def supersede_decision(engine, runner, decision, shown, note, name):
    """One decision made moot by another issue. An issue: a comment `Superseded by <ref>`, both awaiting labels off.
    A report: the answerer's `send` to its worker naming the superseding issue (what clears a report), then its
    issue's labels off if it has one. A permission request or question is refused. Returns what was done."""
    kind, ident = decision["kind"], decision["id"]
    _refuse_notice(decision)
    if kind in ("permission", "question"):
        raise DecisionError("is a %s: answer it with `%s decisions answer %s ...`" % (kind, PROG, ident))
    said = "Superseded by %s%s" % (shown, ": %s" % _one_line(note, 300) if note else "")
    target = _issue_target(engine, decision)
    both = [awaiting_cos_label(engine.config), awaiting_label(engine.config)]
    if kind == "issue":
        if target is None or target[0] is None:
            raise DecisionError("cannot tell which repo its issue is in")
        repo, number = target
        IssueDelivery(engine, runner).deliver(decision, {"repo": repo, "number": number, "add": (), "remove": both,
                                                         "author": name, "comment": said, "comment_first": True})
        engine._note("decisions.supersede", ident, {"kind": kind, "repo": repo, "issue": number, "by": name,
                                                   "superseded_by": shown})
        return "done: comment and both labels off %s#%d" % (repo, number)
    if not name:
        raise DecisionError("a report is cleared by a send from the boss: run this as the Chief of Staff")
    if not decision.get("ask") or not decision.get("agent"):
        raise DecisionError("is a report that names no issue: a send would clear every question of its worker. "
                            "Answer it with `%s send`" % PROG)
    outcome = ReportSendDelivery(engine).deliver(decision, {
        "by": name, "to": decision["agent"], "ask": decision["ask"], "repo": decision.get("repo"),
        "text": "%s. Your question on this ask is covered there; carry on with that." % said})
    if not outcome.get("sent"):
        raise DecisionError("%s was not sent to %s (%s): the report stays open" % (
            "the note", decision["agent"], outcome.get("reason") or outcome.get("action")))
    engine._note("decisions.supersede", ident, {"kind": kind, "agent": decision["agent"], "by": name,
                                               "superseded_by": shown})
    if target is not None and target[0] is not None:
        try:
            IssueDelivery(engine, runner).deliver(decision, {"repo": target[0], "number": target[1], "add": (),
                                                             "remove": both, "author": name})
        except IntentError as err:
            raise DecisionError("sent to %s, but its issue's labels were not removed: %s" % (decision["agent"], err)) from None
        return "done: sent to %s, both labels off %s#%d" % (decision["agent"], target[0], target[1])
    return "done: sent to %s" % decision["agent"]


def cmd_decision_supersede(args, engine, decisions, name, runner, role=None, now=None):
    """`decisions supersede <id> ... --by <issue>`: per id one line, `done` or `refused: why`; it carries on past a
    failure. Returns the exit code."""
    repo, number, shown = parse_superseding(engine, args.by)
    failed = 0
    for ident in dict.fromkeys(args.ids):
        try:
            decision = decision_or_persisted(engine, decisions, ident, "supersede", now)
            if decision is None:
                raise DecisionError("no decision %s is waiting%s" % (ident, decision_miss_hint(decisions, ident)))
            refuse_human_tier(engine, decision, "supersede", name, role)
            line = supersede_decision(engine, runner, decision, shown, args.note, name)
            print("%s: %s" % (ident, line))
        except (DecisionError, IntentError) as err:
            failed += 1
            print("%s: refused: %s" % (ident, _one_line(str(err), 300)))
        except Exception as err:
            failed += 1
            print("%s: failed: %s" % (ident, _one_line("%s: %s" % (type(err).__name__, err), 300)))
    return EXIT_ERROR if failed else EXIT_OK


DECISION_REFRESH_SECONDS = 6.0    # how long a decision write waits for the daemon's next write of decisions.json
DECISION_REFRESH_STEP = 0.25
_refresh_clock = {"sleep": time.sleep, "monotonic": time.monotonic}   # replaced in tests


def decision_shows_new_state(decisions, action, ident):
    """True when `decisions` (the list as the file shows it) already shows the result of `action` on `ident`: an escalated decision is
    the owner's tier or gone; a resolved, answered or superseded one is gone."""
    found = next((d for d in decisions if d.get("id") == ident), None)
    if found is None:
        return True
    return action == "escalate" and found.get("tier") == "human"


def refreshed_or_honest_line(action, idents, poked, state_dir=None):
    """After a decision write: poke the daemon (done by the caller: `poked`), then wait up to DECISION_REFRESH_SECONDS for the file the
    daemon owns to show the new state of every decision in `idents`, reading it only (the daemon is the one writer: nothing here races it).
    Returns None when the list shows the new state; else ONE honest line saying the change is applied at the next pass, so the person is
    never told the list is current when it is not."""
    state_dir = state_dir or state_dir_path()
    if not poked:
        return ("the change is written; the daemon is not running, so the decision list shows it after the next pass "
                "(start it with `%s ensure`)" % PROG)
    clock = _refresh_clock
    deadline = clock["monotonic"]() + DECISION_REFRESH_SECONDS
    while True:
        report = decisions_report(state_dir, time.time())
        if not report["stale"] and all(decision_shows_new_state(report["decisions"], action, i) for i in idents):
            return None
        if clock["monotonic"]() >= deadline:
            return ("the change is written and applied at the next pass (about a minute at most): the decision list may still "
                    "show the old state until then")
        clock["sleep"](DECISION_REFRESH_STEP)


def finish_decision_write(action, idents):
    """Poke the daemon, wait (bounded) for the list to show the new state, and say so honestly when it does not. Never raises."""
    poked = False
    try:
        poked = send_poke(state_dir_path())  # the daemon looks again now; a miss is harmless
        line = refreshed_or_honest_line(action, [i for i in idents if i], poked)
    except Exception:
        line = None
    if line:
        sys.stderr.write("%s: %s\n" % (PROG, line))


def cmd_decision_write(args, action, runner, now):
    """`decisions escalate|resolve|answer|supersede`: the caller is its pane's fleet agent (`--from` in the Lab) or you."""
    if action == "batch":
        return cmd_decision_batch(args, runner, now)
    try:
        engine, decisions, _ = fresh_engine_decisions(runner, now, writable=True)
        name, role = decision_actor(engine, args)
        if name is not None and role != "chief-of-staff":
            engine.audit.append(engine.actor, "decisions.refused", getattr(args, "id", None) or " ".join(args.ids),
                                {"caller": name, "role": role, "action": action})
            sys.stderr.write("%s: only the Chief of Staff, or you in a plain shell, may %s a decision; %s is %s. "
                             "Raise it with `%s report question \"...\"` and your awaiting-cos label\n"
                             % (PROG, action, name, "a %s" % role if role else "not given a role", PROG))
            return EXIT_USAGE
        if action == "supersede":
            code = cmd_decision_supersede(args, engine, decisions, name, runner, role, now)
            if code == EXIT_OK:  # a failed write has nothing to wait for
                finish_decision_write(action, list(args.ids))
            return code
        decision = decision_or_persisted(engine, decisions, args.id, action, now)
        if decision is None:
            raise DecisionError("no decision %s is waiting%s (see `%s decisions --tier all`)" % (args.id, decision_miss_hint(decisions, args.id), PROG))
        refuse_human_tier(engine, decision, action, name, role)
        if action == "escalate":
            line = escalate_decision(engine, runner, decision, args.reason, name)
        elif action == "resolve":
            line = resolve_decision(engine, runner, decision, args.note, name)
        elif decision["kind"] == "issue" and decision.get("rows"):
            line = answer_batch(engine, runner, decision, args, name)
        elif args.as_recommended or args.row:
            raise DecisionError("%s is not a batch: --as-recommended and --row answer a batch; answer this with its "
                                "own value" % args.id)
        elif args.value is None:
            raise DecisionError("answer %s with a value: `allow` or `deny` for a permission request, the text for a "
                                "question" % args.id)
        else:
            line = answer_decision(engine, decision, args.value, args.note, name)
    except DecisionReadError as err:
        sys.stderr.write("%s: %s\n" % (PROG, _one_line(str(err), 400)))
        return EXIT_ERROR
    except (DecisionError, CallerError) as err:
        sys.stderr.write("%s: %s\n" % (PROG, _one_line(str(err), 400)))
        return EXIT_USAGE
    print(line)
    finish_decision_write(action, [getattr(args, "id", None)])
    return EXIT_OK
