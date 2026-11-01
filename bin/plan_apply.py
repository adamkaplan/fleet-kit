"""plan_apply: `fleet-switchboard plan apply` (#1789): an approved plan edit applied to the live plan, refused unless what was approved is what is applied.

Pure functions of text: parse the edit file, apply its pairs, compare hashes. The standard library and plain imports of plan_check and comment_edit
(the signed write of #1714); it reads no name of the entry. The caller passes the lane's name, the issue reader's `_api` callable, the state
directory and the audit. There is ONE plan reader (plan_check.select_plan_text) and ONE hash (plan_check.plan_hash); this module defines neither."""
import hashlib
import os
import re
from datetime import datetime, timezone

import comment_edit
import plan_check
import record_json

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
AUDIT_TAIL = 8 << 20


class ApplyRefused(Exception):
    """A run that is refused before anything was written: (exit status, one line)."""

    def __init__(self, line, code=2):
        super().__init__(line)
        self.line, self.code = line, code


def standard_form_text():
    return "\n".join(STANDARD_FORM)


def short(text):
    return str(text).replace("sha256:", "")[:8]


def parse_pairs(text):
    """([(old, new)], why) from an edit file in the standard form. Raises ApplyRefused naming what is wrong."""
    marks = []
    lines = text.split("\n")
    for number, line in enumerate(lines):
        found = HEADER.match(line)
        if found:
            marks.append((number, found.group(1), int(found.group(2))))
    if not marks:
        if OTHER_FORMS.search(text):
            raise ApplyRefused("the file is not in the standard form:\n" + standard_form_text(), 1)
        raise ApplyRefused("the file has no pair: it needs `===== OLD 1 (verbatim) =====` and `===== NEW 1 (verbatim) =====` headers", 1)
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
            raise ApplyRefused("the pair numbers must run 1, 2, 3 in order: found %s %d where OLD %d was expected" % (old_mark[1], old_mark[2], want), 1)
        if new_mark is None or new_mark[1] != "NEW" or new_mark[2] != want:
            raise ApplyRefused("OLD %d has no NEW %d after it" % (want, want), 1)
        pairs.append([texts[index], texts[index + 1]])
    why, last = "", pairs[-1][1]
    cut = last.rfind("\n\nWhy:")
    if cut >= 0:
        pairs[-1][1], why = last[:cut], last[cut + 2:]
    elif last.startswith("Why:"):
        pairs[-1][1], why = "", last
    for number, (old, new) in enumerate(pairs, 1):
        if not old:
            raise ApplyRefused("OLD %d is empty: it must name the text it replaces" % number, 1)
    return [(old, new) for old, new in pairs], " ".join(why.split())


def expect_live(have, want):
    if have != want:
        raise ApplyRefused("plan apply: refused: --expect-live: the live plan hash is %s and not %s" % (short(have), short(want)))


def expect_file(have, want):
    if have != want:
        raise ApplyRefused("plan apply: refused: --expect-file: the file's sha256 is %s and not %s" % (have[:8], want[:8]))


def apply_pairs(text, pairs):
    """The text after every pair, each OLD found exactly once in the text as the previous pairs left it. Raises ApplyRefused."""
    for number, (old, new) in enumerate(pairs, 1):
        count = text.count(old)
        if count != 1:
            raise ApplyRefused("plan apply: refused: OLD %d occurs %d times at its turn: it must occur once" % (number, count))
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
        raise ApplyRefused("the issue is not owner/repo#N, an issue URL, or N: %s" % comment_edit.one_line(text, 80), 1)
    return repo, int(found.group(2))


def audit_tail(state_dir):
    """The parsed entries of the end of audit.jsonl (a torn first line is dropped); what the signed edit reads for an approval by a send."""
    try:
        with open(os.path.join(state_dir, "audit.jsonl"), "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - AUDIT_TAIL))
            data = handle.read()
    except OSError:
        return []
    out = []
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines()):
        if number == 0 and size > AUDIT_TAIL:
            continue
        try:
            entry = record_json.parse_bounded(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


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
        raise ApplyRefused("plan apply: refused: the issue has a second comment that looks like a plan: a plan is one comment", 1)
    text, source = plan_check.select_plan_text("", comments)
    found = SOURCE_ID.match(source)
    chosen = next((c for c in comments if found and str(c.get("id")) == found.group(1)), None)
    if chosen is None:
        raise ApplyRefused("plan apply: refused: the issue has no plan comment", 1)
    return chosen


def declared(text):
    try:
        return plan_check.declared_clauses(text)
    except plan_check.PlanFormatError as err:
        raise ApplyRefused("plan apply: refused: the plan's clauses cannot be read: %s" % comment_edit.one_line(err, 150), 1) from None


def receipt_text(name, file_sha, count, why, old_hash, new_hash, caller, checked="pass"):
    lines = [plan_check.RECEIPT_MARKER, "plan hash: %s" % new_hash,
             "file: %s (sha256 %s)" % (os.path.basename(name), file_sha[:8]),
             "pairs: %d; the old plan hash was %s" % (count, old_hash)]
    if why:
        lines.append(comment_edit.one_line(why.replace("`", "'"), REASON_LIMIT))
    if checked != "pass":
        lines.append("plan check: FAIL")
    return "\n".join(lines) + "\n\n" + mark_line(caller)


def mark_line(name):
    return "<!-- fleet-switchboard:from=%s -->" % name


def run(args, caller, api, state_dir, audit):
    """(exit status, text). Exit 0 applied, 1 a cause (nothing changed unless the text says so), 2 refused. A refusal's text is its one reason."""
    reader = getattr(api, "__self__", None)
    out = []
    try:
        for flag, value, shape in (("--expect-live", args.expect_live, FULL_HASH), ("--expect-result", args.expect_result, FULL_HASH),
                                   ("--expect-file", args.expect_file, FILE_HASH)):
            if not shape.match(value or ""):
                raise ApplyRefused("plan apply: refused: %s must be the full hash (%s)" % (flag, "sha256:<64 hex>" if shape is FULL_HASH else "64 hex"), 1)
        try:
            with open(args.file, "rb") as handle:
                raw = handle.read()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as err:
            raise ApplyRefused("plan apply: the file cannot be read: %s" % comment_edit.one_line(getattr(err, "strerror", None) or err, 150), 1) from None
        file_sha = hashlib.sha256(raw).hexdigest()
        pairs, why = parse_pairs(text)
        out.extend(describe(pairs))
        repo, number = parse_ref(args.issue, getattr(reader, "repo", None))
        try:
            comments = read_comments(api, repo, number)
        except Exception as err:
            raise ApplyRefused("plan apply: the plan comment cannot be read: %s" % comment_edit.one_line(err, 150), 1) from None
        plan = pick_plan(comments)
        live = plan["body"]
        if comment_edit.marked_name(live) != caller:
            raise ApplyRefused("plan apply: refused: the plan comment is signed %s, you are %s" % (comment_edit.marked_name(live) or "by no lane", caller))
        old_hash = plan_check.plan_hash(live)
        expect_live(old_hash, args.expect_live)
        expect_file(file_sha, args.expect_file)
        result = apply_pairs(live, pairs)
        result = comment_edit.MARKER.sub("", result).rstrip() + "\n\n" + mark_line(caller)
        new_hash = plan_check.plan_hash(result)
        if new_hash != args.expect_result:
            raise ApplyRefused("plan apply: refused: --expect-result: the result's plan hash is %s and not %s" % (short(new_hash), short(args.expect_result)))
        if declared(live) != declared(result):
            raise ApplyRefused("plan apply: refused: the clauses: block of the result differs from the live one (the Done-when copy is the ask's: never edited)")
        approval = next(iter(comment_edit.approval_ids(plan, comments, audit_tail(state_dir))), None)
        url = "https://github.com/%s/issues/%d#issuecomment-%s" % (repo, number, plan["id"])
        audited = (lambda subject, detail: audit.external("cli", "github.comment", subject, detail)) if audit is not None else None
        try:
            comment_edit.edit_in_place(reader, caller, url, result, approval, lambda: audit_tail(state_dir), audited, mark_line, getattr(reader, "repo", repo))
        except comment_edit.CommentEditRefused as err:
            raise ApplyRefused("plan apply: refused: %s" % comment_edit.one_line(err, 300), 1) from None
        except comment_edit.CommentEditFailed as err:
            raise ApplyRefused("plan apply: the patch failed, nothing is changed: %s" % comment_edit.one_line(err.detail, 300), 1) from None
    except ApplyRefused as err:
        return err.code, err.line
    changed = None
    try:
        back = api("repos/%s/issues/comments/%s" % (repo, plan["id"]), repo)
        changed = back.get("body") if isinstance(back, dict) else None
    except Exception:
        changed = None
    if changed != result:
        return 1, "plan apply: the plan comment was patched but reading it back differs from the result: live hash %s" % (plan_check.plan_hash(changed) if isinstance(changed, str) else "unknown")
    done_when = " ".join("(%d) %s" % (i, c) for i, c in enumerate(declared(changed) or [], 1))
    try:
        checked = plan_check.check(changed, done_when or None)["result"]
    except plan_check.PlanFormatError:
        checked = "fail"
    out.append("plan check: %s" % checked.upper())
    out.append("applied at %s; live plan hash %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), new_hash))
    try:
        comment_edit.gh_call(reader, repo, "repos/%s/issues/%d/comments" % (repo, number), "POST",
                             {"body": receipt_text(args.file, file_sha, len(pairs), why, old_hash, new_hash, caller, checked)})
    except comment_edit.CommentEditFailed as err:
        out.append("plan apply: the plan is changed and the receipt is not (%s): post the receipt by hand; live plan hash %s" % (comment_edit.one_line(err.detail, 200), new_hash))
        return 1, "\n".join(out)
    return (0 if checked == "pass" else 1), "\n".join(out)
