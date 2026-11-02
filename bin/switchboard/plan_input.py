# ---------------------------------------------------------------- `plan check` (#135, gate G0a)

PLAN_URL_RE = re.compile(r"^https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([0-9]+)(?:#issuecomment-([0-9]+))?/?$")
PLAN_COMMENT_PAGES = 10


def parse_plan_source(text, default_repo=None):
    """(repo, number, comment id or None) from `owner/repo#N`, an issue URL (a `#issuecomment-ID` picks that comment),
    or a bare `N` / `#N` with `default_repo`. Raises PlanInputError."""
    text = (text or "").strip()
    found = PLAN_URL_RE.match(text)
    if found:
        return found.group(1), int(found.group(2)), int(found.group(3)) if found.group(3) else None
    found = re.match(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$", text) or re.match(r"^()#?([0-9]+)$", text)
    if not found or int(found.group(2)) < 1:
        raise PlanInputError("%r is not a plan: write owner/repo#N, an issue URL, or --file PATH" % text)
    repo = found.group(1) or default_repo
    if not repo:
        raise PlanInputError("%r names no repo and no intent_repo is configured: write owner/repo#N" % text)
    return repo, int(found.group(2)), None


def plan_comment_choice(body, comments):
    """(text, source, defects): the ONE rule every reader of an issue's plan applies: the plan select_plan_text picks, with the comment-level
    defects (a second plan-looking comment naming both ids, a bad receipt) RETURNED beside it, never kept in a global."""
    text, source = plan_check.select_plan_text(body, comments)
    return text, source, plan_check.plan_comment_defects(comments)


def second_plan_defects(defects):
    return [d for d in defects if d.get("code") == "second-plan-comment"]


def fetch_plan(reader, repo, number, comment_id=None):
    """(text, source label, done_when of the issue, comment-level defects) read from GitHub with GETs only. `reader` has `_api(path, repo)`.
    The defects are computed from every comment of the issue also when `comment_id` names one: naming a comment does not skip the report."""
    try:
        issue = reader._api("repos/%s/issues/%d" % (repo, number), repo)
        comments = []
        for page in range(1, PLAN_COMMENT_PAGES + 1):
            got = reader._api("repos/%s/issues/%d/comments?per_page=100&page=%d" % (repo, number, page), repo)
            if not isinstance(got, list):
                raise PlanInputError("the comments of %s#%d did not come back as a list" % (repo, number))
            comments.extend(got)
            if len(got) < 100:
                break
    except IntentError as err:
        raise PlanInputError("cannot read %s#%d: %s" % (repo, number, err)) from None
    body = issue.get("body") if isinstance(issue, dict) and isinstance(issue.get("body"), str) else ""
    defects = plan_check.plan_comment_defects(comments)
    if comment_id is not None:
        picked = [c for c in comments if isinstance(c, dict) and c.get("id") == comment_id]
        if not picked:
            raise PlanInputError("%s#%d has no comment %d" % (repo, number, comment_id))
        text, source = picked[0].get("body") or "", "comment %d%s" % (comment_id, plan_check.comment_author(picked[0]))
    else:
        text, source, defects = plan_comment_choice(body, comments)
    return text, "%s of %s#%d" % (source, repo, number), parse_intent(body)[1], defects


def _parent_ask_done_when(reader, repo, number):
    """The Done-when of the ASK that is the native sub-issue parent of issue `number` of `repo`, or None. Like `IntentReader.ask_of`, a parent counts as the ask only
    when it has a parent of its own (the charter): the issue under a charter is itself the ask, and a charter's Done-when is never borrowed. Anything that is not
    "the ask of this repository's work item" is REFUSED (PlanInputError), never read as another issue: a parent in another repository (the parent's repository comes
    from the API payload, the reader's `parent()` keeps only the number), a pull request, the issue itself, and a cycle."""
    where = "%s#%d" % (repo, number)

    def parent_payload(of):
        try:
            found = reader._api("repos/%s/issues/%d/parent" % (repo, of), repo)
        except IntentError as err:
            if err.reason == "not_found":
                return None
            raise
        if not isinstance(found, dict) or not isinstance(found.get("number"), int):
            raise PlanInputError("the parent of %s#%d did not come back as an issue" % (repo, of))
        if found.get("pull_request"):
            raise PlanInputError("the parent of %s#%d is a pull request, not an ask" % (repo, of))
        url = str(found.get("repository_url") or "").lower().rstrip("/")
        if not url.endswith("/repos/" + repo.lower()):
            raise PlanInputError("the parent of %s#%d (#%d) is in another repository (%s): a plan reads the Done-when of an ask of its own repository only"
                                 % (repo, of, found["number"], url.rsplit("/repos/", 1)[-1] or "unknown"))
        return found
    try:
        parent = parent_payload(number)
        if parent is None:
            return None
        if parent["number"] == number:
            raise PlanInputError("%s is its own parent" % where)
        grand = parent_payload(parent["number"])
        if grand is None:
            return None
        if grand["number"] in (number, parent["number"]):
            raise PlanInputError("the parent chain of %s loops back on itself" % where)
        return reader.issue(parent["number"], repo).done_when
    except IntentError as err:
        raise PlanInputError("cannot read the parent ask of %s: %s" % (where, err)) from None


def load_plan(args, runner, now):
    """(text, source label, result) of the plan `args` names (source, file, ask, done_when), checked. Raises
    PlanInputError, ValueError (an unreadable block) or the readers' errors."""
    try:
        plan_check.MAX_BYTES
    except Exception as err:  # the module is missing or broken: say so, never a traceback
        raise PlanInputError("the plan checker (bin/plan_check.py) cannot be loaded: %s" % _one_line(str(err), 150)) from None
    done_when = args.done_when
    plan_own = None
    comment_defects = []
    if args.file:
        if args.source:
            raise PlanInputError("give a plan source or --file, not both")
        try:
            with open(args.file, "r", encoding="utf-8") as handle:
                text = handle.read(plan_check.MAX_BYTES * 4 + 1)
        except (OSError, UnicodeDecodeError) as err:
            raise PlanInputError("cannot read %s: %s" % (args.file, getattr(err, "strerror", None) or err)) from None
        if len(text) > plan_check.MAX_BYTES * 4:
            raise PlanInputError("%s is larger than %d bytes" % (args.file, plan_check.MAX_BYTES * 4))
        source = "file %s" % args.file
        plan_own = parse_intent(text)[1]
        done_when = done_when or plan_own
        plan_own = plan_own if done_when != plan_own else None
    else:
        if not args.source:
            raise PlanInputError("give a plan: owner/repo#N, an issue URL, or --file PATH")
        config = load_config()
        repo, number, comment_id = parse_plan_source(args.source, config.get("intent_repo"))
        reader = IntentReader(config["gh"], repo, runner, now, config["call_timeout_seconds"],
                              accounts=GhAccounts(config, runner, now))
        text, source, issue_done, comment_defects = fetch_plan(reader, repo, number, comment_id)
        if args.ask and not done_when:
            ask_repo, ask_number, _ = parse_plan_source(args.ask, repo)
            done_when = reader.issue(ask_number, ask_repo).done_when  # the ask is the authority
            plan_own = issue_done
        elif done_when:
            plan_own = issue_done if issue_done and issue_done != done_when else None
        else:
            done_when = issue_done
            if not done_when:   # a work item states no Done-when of its own: its ask's is the authority (the sub-issue parent, never a "Work item of" line)
                done_when = _parent_ask_done_when(reader, repo, number)
    if args.ask and args.file and not args.done_when:
        raise PlanInputError("--ask needs a plan read from GitHub; for --file give --done-when TEXT")
    result = plan_check.check(text, done_when, plan_own)
    if comment_defects:
        result["defects"] = list(result["defects"]) + list(comment_defects)
        result["result"] = "fail"
    return text, source, result


def cmd_plan_check(args, runner, now):
    """`plan check`: read a plan, check it, print PASS or FAIL with one line per defect and the content hash. Exit 0
    pass, 1 fail, 2 unreadable. Reads only; never writes GitHub."""
    def finish(code, payload, lines, error=None):
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        elif error:
            sys.stderr.write("%s: plan check: %s\n" % (PROG, error))
        else:
            print("\n".join(lines))
        return code

    try:
        text, source, result = load_plan(args, runner, now)
    except (PlanInputError, ValueError, ConfigError, IntentError) as err:
        return finish(EXIT_USAGE, {"result": "error", "error": _one_line(str(err), 300)}, [], _one_line(str(err), 300))
    result["source"] = source
    return finish(EXIT_OK if result["result"] == "pass" else EXIT_ERROR, result, plan_check.render(result, source, explaining=getattr(args, "explain", False)))


def cmd_plan_template(args, runner, now):
    """`plan template <ask>` (or `--done-when TEXT`): print a prefilled plan for the ask: its Done-when clauses numbered and one scenario stub
    per clause. Reads only (a GET of the ask); never writes GitHub. Exit 0, or 2 when the ask or its Done-when cannot be read."""
    try:
        done_when, source = args.done_when, None
        if not done_when:
            if not args.ask:
                raise PlanInputError("give the ask (owner/repo#N) or --done-when TEXT")
            config = load_config()
            repo, number, _ = parse_plan_source(args.ask, config.get("intent_repo"))
            reader = IntentReader(config["gh"], repo, runner, now, config["call_timeout_seconds"], accounts=GhAccounts(config, runner, now))
            done_when, source = reader.issue(number, repo).done_when, "%s#%d" % (repo, number)
        clauses = plan_check.clauses_of(done_when)
        if not (done_when or "").strip() or not clauses:
            raise PlanInputError("the ask has no Done-when line: nothing to prefill (write the plan by hand, or add the Done-when first)")
    except (PlanInputError, ValueError, ConfigError, IntentError) as err:
        sys.stderr.write("%s: plan template: %s\n" % (PROG, _one_line(str(err), 300)))
        return EXIT_USAGE
    print("\n".join(plan_check.template(clauses, _plain(source, 120) if source else None)))
    return EXIT_OK

# ---- plan apply (#1789)
import hashlib
import os
from datetime import datetime, timezone
# The standard form of an edit file: the lines the planning skill, docs/plan-template.md and the help of `plan apply` carry word for word (S7).
STANDARD_FORM = (
    "Lines before the first header are a note and are read by nobody.",
    "Each pair is `===== OLD <n> (verbatim) =====` then the old text, then `===== NEW <n> (verbatim) =====` then the new text.",
    "The numbers run 1, 2, 3 in order.",
    "The text of each ends at the line before the next header; its final newline is not part of it.",
    "The paragraph that starts `Why:` after the last NEW is the receipt's reason.",
    "The file has no other header.",
)
HEADER = re.compile(r"^===== (OLD|NEW) ([0-9]+) \(verbatim\) =====$")
OTHER_FORMS = re.compile(r"(?m)^(?:EDIT [0-9]+\b|OLD:|NEW:|=== (?:OLD|NEW) [0-9]+ ===)")
REF = re.compile(r"^(?:(?:https://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:#|/issues/|/pull/))?([0-9]{1,9})(?:#issuecomment-[0-9]+)?$")
SOURCE_ID = re.compile(r"^comment ([0-9]+)")
FULL_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
FILE_HASH = re.compile(r"^[0-9a-f]{64}$")
REASON_LIMIT = 300


class PlanApplyRefused(Exception):
    """A run that is refused before anything was written: (exit status, one line)."""

    def __init__(self, line, code=2):
        super().__init__(line)
        self.line, self.code = line, code


def standard_form_text():
    return "\n".join(STANDARD_FORM)


def hash8(text):
    return str(text).replace("sha256:", "")[:8]


def parse_pairs(text):
    """([(old, new)], why) from an edit file in the standard form. Raises PlanApplyRefused naming what is wrong."""
    marks = []
    lines = text.split("\n")
    for number, line in enumerate(lines):
        found = HEADER.match(line)
        if found:
            marks.append((number, found.group(1), int(found.group(2))))
    if not marks:
        if OTHER_FORMS.search(text):
            raise PlanApplyRefused("the file is not in the standard form:\n" + standard_form_text(), 1)
        raise PlanApplyRefused("the file has no pair: it needs `===== OLD 1 (verbatim) =====` and `===== NEW 1 (verbatim) =====` headers", 1)
    texts = []
    for index, (number, kind, n) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(lines)
        texts.append("\n".join(lines[number + 1:end]))
    pairs = []
    for index in range(0, len(marks), 2):
        want = index // 2 + 1
        old_mark = marks[index]
        new_mark = marks[index + 1] if index + 1 < len(marks) else None
        if old_mark[1] != "OLD" or old_mark[2] != want:
            raise PlanApplyRefused("the pair numbers must run 1, 2, 3 in order: found %s %d where OLD %d was expected" % (old_mark[1], old_mark[2], want), 1)
        if new_mark is None or new_mark[1] != "NEW" or new_mark[2] != want:
            raise PlanApplyRefused("OLD %d has no NEW %d after it" % (want, want), 1)
        pairs.append([texts[index], texts[index + 1]])
    why, last = "", pairs[-1][1]
    cut = last.rfind("\n\nWhy:")
    if cut >= 0:
        pairs[-1][1], why = last[:cut], last[cut + 2:]
    elif last.startswith("Why:"):
        pairs[-1][1], why = "", last
    for number, (old, new) in enumerate(pairs, 1):
        if not old:
            raise PlanApplyRefused("OLD %d is empty: it must name the text it replaces" % number, 1)
    return [(old, new) for old, new in pairs], " ".join(why.split())


def expect_live(have, want):
    if have != want:
        raise PlanApplyRefused("plan apply: refused: --expect-live: the live plan hash is %s and not %s" % (hash8(have), hash8(want)))


def expect_file(have, want):
    if have != want:
        raise PlanApplyRefused("plan apply: refused: --expect-file: the file's sha256 is %s and not %s" % (have[:8], want[:8]))


def apply_pairs(text, pairs):
    """The text after every pair, each OLD found exactly once in the text as the previous pairs left it. Raises PlanApplyRefused."""
    for number, (old, new) in enumerate(pairs, 1):
        count = text.count(old)
        if count != 1:
            raise PlanApplyRefused("plan apply: refused: OLD %d occurs %d times at its turn: it must occur once" % (number, count))
        text = text.replace(old, new)
    return text


def describe(pairs):
    """The pairs as parsed, one line per text: the number, the first 80 characters, the length."""
    out = []
    for number, (old, new) in enumerate(pairs, 1):
        for kind, text in (("OLD", old), ("NEW", new)):
            out.append("%s %d (%d characters): %s" % (kind, number, len(text), " ".join(text.split())[:80]))
    return out


def parse_ref(text, home_repo):
    found = REF.match((text or "").strip())
    repo = found.group(1) if found and found.group(1) else home_repo
    if not found or not repo:
        raise PlanApplyRefused("the issue is not owner/repo#N, an issue URL, or N: %s" % edit_one_line(text, 80), 1)
    return repo, int(found.group(2))


def read_comments(api, repo, number):
    comments = []
    for page in range(1, 11):
        got = api("repos/%s/issues/%d/comments?per_page=100&page=%d" % (repo, number, page), repo)
        if not isinstance(got, list):
            break
        comments.extend(c for c in got if isinstance(c, dict))
        if len(got) < 100:
            break
    return comments


def pick_plan(comments):
    """The one plan comment as plan_check selects it (a second plan-looking comment is refused)."""
    if any(d["code"] == "second-plan-comment" for d in plan_check.plan_comment_defects(comments)):
        raise PlanApplyRefused("plan apply: refused: the issue has a second comment that looks like a plan: a plan is one comment", 1)
    text, source = plan_check.select_plan_text("", comments)
    found = SOURCE_ID.match(source)
    chosen = next((c for c in comments if found and str(c.get("id")) == found.group(1)), None)
    if chosen is None:
        raise PlanApplyRefused("plan apply: refused: the issue has no plan comment", 1)
    return chosen


def declared_clauses(text):
    try:
        return plan_check.declared_clauses(text)
    except plan_check.PlanFormatError as err:
        raise PlanApplyRefused("plan apply: refused: the plan's clauses cannot be read: %s" % edit_one_line(err, 150), 1) from None


def receipt_text(name, file_sha, count, why, old_hash, new_hash, caller, mark, checked="pass"):
    lines = [plan_check.RECEIPT_MARKER, "plan hash: %s" % new_hash,
             "file: %s (sha256 %s)" % (os.path.basename(name), file_sha[:8]),
             "pairs: %d; the old plan hash was %s" % (count, old_hash)]
    if why:
        lines.append(edit_one_line(why.replace("`", "'"), REASON_LIMIT))
    if checked != "pass":
        lines.append("plan check: FAIL")
    return "\n".join(lines) + "\n\n" + mark(caller)


def apply_run(args, caller, api, state_dir, audit, fleet_mark):
    """(exit status, text). Exit 0 applied, 1 a cause (nothing changed unless the text says so), 2 refused. A refusal's text is its one reason."""
    reader = getattr(api, "__self__", None)
    out = []
    try:
        for flag, value, shape in (("--expect-live", args.expect_live, FULL_HASH), ("--expect-result", args.expect_result, FULL_HASH),
                                   ("--expect-file", args.expect_file, FILE_HASH)):
            if not shape.match(value or ""):
                raise PlanApplyRefused("plan apply: refused: %s must be the full hash (%s)" % (flag, "sha256:<64 hex>" if shape is FULL_HASH else "64 hex"), 1)
        try:
            with open(args.file, "rb") as handle:
                raw = handle.read()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as err:
            raise PlanApplyRefused("plan apply: the file cannot be read: %s" % edit_one_line(getattr(err, "strerror", None) or err, 150), 1) from None
        file_sha = hashlib.sha256(raw).hexdigest()
        pairs, why = parse_pairs(text)
        out.extend(describe(pairs))
        repo, number = parse_ref(args.issue, getattr(reader, "repo", None))
        try:
            comments = read_comments(api, repo, number)
        except Exception as err:
            raise PlanApplyRefused("plan apply: the plan comment cannot be read: %s" % edit_one_line(err, 150), 1) from None
        plan = pick_plan(comments)
        live = plan["body"]
        if marked_name(live) != caller:
            raise PlanApplyRefused("plan apply: refused: the plan comment is signed %s, you are %s" % (marked_name(live) or "by no lane", caller))
        old_hash = plan_check.plan_hash(live)
        expect_live(old_hash, args.expect_live)
        expect_file(file_sha, args.expect_file)
        result = apply_pairs(live, pairs)
        result = MARKER.sub("", result).rstrip() + "\n\n" + fleet_mark(caller)
        new_hash = plan_check.plan_hash(result)
        if new_hash != args.expect_result:
            raise PlanApplyRefused("plan apply: refused: --expect-result: the result's plan hash is %s and not %s" % (hash8(new_hash), hash8(args.expect_result)))
        if declared_clauses(live) != declared_clauses(result):
            raise PlanApplyRefused("plan apply: refused: the clauses: block of the result differs from the live one (the Done-when copy is the ask's: never edited)")
        approval = next(iter(approval_ids(plan, comments, _audit_tail(state_dir))), None)
        url = "https://github.com/%s/issues/%d#issuecomment-%s" % (repo, number, plan["id"])
        audited = (lambda subject, detail: audit.external("cli", "github.comment", subject, detail)) if audit is not None else None
        try:
            edit_in_place(reader, caller, url, result, approval, lambda: _audit_tail(state_dir), audited, fleet_mark, getattr(reader, "repo", repo))
        except CommentEditRefused as err:
            raise PlanApplyRefused("plan apply: refused: %s" % edit_one_line(err, 300), 1) from None
        except CommentEditFailed as err:
            raise PlanApplyRefused("plan apply: the patch failed, nothing is changed: %s" % edit_one_line(err.detail, 300), 1) from None
    except PlanApplyRefused as err:
        return err.code, err.line
    changed = None
    try:
        back = api("repos/%s/issues/comments/%s" % (repo, plan["id"]), repo)
        changed = back.get("body") if isinstance(back, dict) else None
    except Exception:
        changed = None
    if changed != result:
        return 1, "plan apply: the plan comment was patched but reading it back differs from the result: live hash %s" % (plan_check.plan_hash(changed) if isinstance(changed, str) else "unknown")
    done_when = " ".join("(%d) %s" % (i, c) for i, c in enumerate(declared_clauses(changed) or [], 1))
    try:
        checked = plan_check.check(changed, done_when or None)["result"]
    except plan_check.PlanFormatError:
        checked = "fail"
    out.append("plan check: %s" % checked.upper())
    out.append("applied at %s; live plan hash %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), new_hash))
    try:
        gh_call(reader, repo, "repos/%s/issues/%d/comments" % (repo, number), "POST",
                             {"body": receipt_text(args.file, file_sha, len(pairs), why, old_hash, new_hash, caller, fleet_mark, checked)})
    except CommentEditFailed as err:
        out.append("plan apply: the plan is changed and the receipt is not (%s): post the receipt by hand; live plan hash %s" % (edit_one_line(err.detail, 200), new_hash))
        return 1, "\n".join(out)
    return (0 if checked == "pass" else 1), "\n".join(out)
