"""comment_edit: `fleet-switchboard comment --edit URL` (#1714): the caller's own comment replaced in place, signed once.

Pure: the standard library and plain imports of plan_check, triage_plan_open and record_json. It reads no name of the entry; the caller passes the
gh engine (its intents reader), the caller's fleet name, the url, the text, the approval id, the audit entries and the function that audits a write.
The lanes share one GitHub account, so 'own' means the body carries the caller's own `from=NAME` marker. Raises CommentEditRefused before any write
and CommentEditFailed when gh fails."""
import json
import re
import subprocess

import plan_check
import record_json
import triage_plan_open

MARKER = re.compile(r"<!--\s*fleet-switchboard:from=([A-Za-z0-9_-]{1,64})\s*-->")
COMMENT_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/([0-9]{1,9})#issuecomment-([0-9]{1,15})$")
PLAN_MARK = "## Plan (scenarios)"


class CommentEditRefused(Exception):
    """An edit that was refused before anything was written."""


class CommentEditFailed(Exception):
    """gh did not answer or failed: (kind, detail)."""

    def __init__(self, kind, detail):
        super().__init__(detail)
        self.kind, self.detail = kind, detail


def marked_name(body):
    """The lane name of the first marker of the body (as the entry's signed_by_fleet reads it), or None."""
    found = MARKER.search(body) if isinstance(body, str) else None
    return found.group(1) if found else None


def one_line(text, limit=300):
    return " ".join(str(text or "").split())[:limit]


def gh_call(reader, repo, path, method, payload=None):
    """One `gh api` call through the configured gh reader (as post_ask_comment runs it); the parsed answer."""
    env = reader.env_for(repo)
    argv = [reader.gh, "api", path] + (["--method", method, "--input", "-"] if payload is not None else [])
    try:
        done = reader.runner(argv, input=json.dumps(payload) if payload is not None else None, capture_output=True, text=True,
                             timeout=reader.timeout, **({"env": env} if env is not None else {}))
    except subprocess.TimeoutExpired:
        raise CommentEditFailed("unavailable", "gh did not answer within %ss" % reader.timeout) from None
    except OSError as err:
        raise CommentEditFailed("unavailable", "cannot run %s: %s" % (reader.gh, err.strerror or err)) from None
    if done.returncode != 0:
        detail = one_line(done.stderr) or one_line(done.stdout) or "exit %d" % done.returncode
        raise CommentEditFailed("gh", reader.accounts.scrub(detail) if reader.accounts else detail)
    try:
        return record_json.parse_bounded(done.stdout)
    except ValueError:
        return {}


def approval_ids(plan, comments, entries):
    """The ids that show the approval of the CURRENT hash of the plan comment: approving comments' ids (digits), approving sends' ids (msg_...)."""
    plan_hash = plan_check.plan_hash(plan["body"])
    found = [str(c["id"]) for c in comments if isinstance(c, dict) and triage_plan_open.approved_by_comment(plan, plan_hash, [c], marked_name)]
    for entry in entries:
        detail = entry.get("detail") if isinstance(entry, dict) else None
        keys = (detail.get("keys") or []) if isinstance(detail, dict) else []
        if triage_plan_open.approved_by_send(plan, plan_hash, [entry], marked_name) and isinstance(keys, list) and keys:
            found.append(str(keys[0]).split(":")[-1])
    return found


def edit_in_place(reader, caller, url, text, approval, entries, audited, mark_line, home_repo):
    """Replace the body of the caller's own comment `url` with `text`, signed once. Returns (comment url, lapse line or None).
    `audited(subject, detail)` is a context manager that yields a dict; `entries()` returns the audit tail; `mark_line(name)` the marker line."""
    found = COMMENT_URL.match(url or "")
    if not found:
        raise CommentEditRefused("not a comment url (https://github.com/OWNER/REPO/issues/N#issuecomment-ID): %s" % one_line(url, 80))
    repo, number, ident = found.group(1), int(found.group(2)), found.group(3)
    path = "repos/%s/issues/comments/%s" % (repo, ident)
    posted = gh_call(reader, repo, path, "GET")
    old = posted.get("body") if isinstance(posted, dict) else None
    if not isinstance(old, str):
        raise CommentEditRefused("comment %s could not be read" % ident)
    if marked_name(old) != caller:
        raise CommentEditRefused("comment %s is not yours: it is signed %s, you are %s" % (ident, marked_name(old) or "by no lane", caller))
    lapse = None
    if PLAN_MARK in old:
        plan = {"id": int(ident), "body": old, "created_at": posted.get("created_at"), "user": posted.get("user")}
        listed = gh_call(reader, repo, "repos/%s/issues/%d/comments?per_page=100" % (repo, number), "GET")
        comments = [c for c in listed if isinstance(c, dict)] if isinstance(listed, list) else []
        ids = approval_ids(plan, comments, entries())
        if ids and not approval:
            raise CommentEditRefused("comment %s holds the approved plan of #%d (approval %s): give --approval ID to edit it" % (ident, number, ids[0]))
        if ids and approval not in ids:
            raise CommentEditRefused("approval %s does not show the approval of the current plan of #%d (approval %s does)" % (approval, number, ids[0]))
        if ids:
            lapse = "approval %s lapses with the new plan hash" % approval
    elif approval:
        raise CommentEditRefused("comment %s is not a plan: --approval is not needed" % ident)
    body = MARKER.sub("", text).rstrip() + "\n\n" + mark_line(caller)
    subject = "issue:%d" % number if repo == home_repo else "issue:%s#%d" % (repo, number)
    with audited(subject, {"repo": repo, "issue": number, "comment": int(ident), "edit": True, "chars": len(body)}) as result:
        gh_call(reader, repo, path, "PATCH", {"body": body})
        back = gh_call(reader, repo, path, "GET")
        if not isinstance(back, dict) or back.get("body") != body:
            raise CommentEditFailed("gh", "the comment read back differs from the text written")
        result["url"] = back.get("html_url") if isinstance(back.get("html_url"), str) else url
    return result["url"], lapse
