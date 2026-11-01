"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): bodies_and_comments."""
import copy
import datetime
import hashlib
import importlib.machinery
import importlib.util
import importlib
from support.paths import BIN, REPO
from support.installs_and_keys import ORDERS_CHARTER_BODY
from support.loader import _load, sb
from support.paths import BIN
from support.policy_and_import import convert


PLAN_CLAUSES = ["first clause is stated here", "second clause is stated here"]


APPROVAL_DW = "(1) %s (2) %s" % tuple(PLAN_CLAUSES)


def plan_body(clauses=PLAN_CLAUSES, extra=""):
    lines = "\n".join('  DW-%d: "%s"' % (i, c) for i, c in enumerate(clauses, 1))
    return "## Plan (scenarios)\n\nsize: S\n%s\n\n```yaml\nclauses:\n%s\n```\n" % (extra, lines)


def sb_form_args(form):
    """(delivery, resume) of a form name, from the measurement helper's own table."""
    return importlib.import_module("support.delivery_measure").FORM_ARGS[form]


def sb_form_arg_names():
    return list(importlib.import_module("support.delivery_measure").FORM_ARGS)


def edited_body(intent, done):
    return "## Intent\n%s\nDone when: %s\n" % (intent, done)


def _utc(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def intent_body(intent, done_when, revision=0):
    """An issue body: an `## Intent` section when either line is given."""
    parts = ["Some text before.\n"]
    if intent is not None or done_when is not None:
        parts.append("## Intent\n")
        if intent is not None:
            parts.append(intent + "\n")
        if done_when is not None:
            parts.append("Done when: %s\n" % done_when)
    if revision:
        parts.append("\n## Notes\nrevision %d\n" % revision)
    return "".join(parts)


REAL_INTENT_ISSUE, REAL_INTENT_PARENT = sb.IntentReader.issue, sb.IntentReader.parent   # before any rig patches them


def converted_body():
    out = convert()
    return copy.deepcopy(out["info"]), copy.deepcopy(out["messages"])


def sweep_comment(number, body, at, cid=None):
    out = {"issue_url": "https://api.github.com/repos/acme/api/issues/%d" % number, "body": body, "created_at": at}
    if cid is not None:
        out["id"] = cid
    return out


def tri_comment(clock_now, number, body, hours_ago=1.0, repo="acme/api"):
    return {"issue_url": "https://api.github.com/repos/%s/issues/%d" % (repo, number), "body": body,
            "created_at": sb.iso(clock_now - hours_ago * 3600)}


TRI_HEAD_A, TRI_HEAD_B = "a" * 40, "b" * 40


REAL_FLOOR_BRIEF = sb.floor_brief   # #1215 PC-9: the real function, for the tests that must not run on the fixture brief


def source_orders(n=19):
    return ["rule number %d for the lane (ordered by alice, 2026-11-%02d)" % (i, i) for i in range(1, n + 1)]


def charter_with(orders, extra=""):
    return ORDERS_CHARTER_BODY + "\n## Standing orders\n\n" + "".join("- %s\n" % o for o in orders) + extra


def hex_id(name):
    return hashlib.sha256(name.encode()).hexdigest()


plan_review = _load("t_plan_review", BIN / "plan_review.py")


CLAIM_RULE = ("A pull request that proves only some of a plan's scenarios puts `CLAIMS: S3 S4 S5` directly under its `PLAN:` line: the proof gate (G1) counts only those, "
              "the others stay claimed by the epic, and a claim naming an undeclared scenario is refused (the union of claims is enforced at the ask's last merge). "
              "So every per-pull-request scenario must be claimed by some pull request (else the last merge is refused; a `CU` waiver passes).")


CLAIM_SENTENCE = "So every per-pull-request scenario must be claimed by some pull request (else the last merge is refused; a `CU` waiver passes)."
