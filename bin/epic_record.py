"""epic_record: an epic-claimed scenario, recorded on its owning issue (#1383). Moved unchanged from bin/plan_ledger.py.

Reaches claim_of and evaluator_of through `_plan_ledger()`: the module the entry already loaded, else plan_ledger.py loaded by path once (no circular import).
"""

import importlib.util
import os
import sys

_PLAN_LEDGER = None


def _plan_ledger():
    """The module the entry loaded (sys.modules["plan_ledger"]: the SAME functions the entry uses), else bin/plan_ledger.py loaded by path once and kept."""
    global _PLAN_LEDGER
    loaded = sys.modules.get("plan_ledger")
    if loaded is not None:
        return loaded
    if _PLAN_LEDGER is None:
        spec = importlib.util.spec_from_file_location("plan_ledger", os.path.join(os.path.dirname(os.path.realpath(__file__)), "plan_ledger.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PLAN_LEDGER = module
    return _PLAN_LEDGER

#
# `claim_of` reads a scenario as `epic` when nobody per pull request can prove it: it is proven after the merges. Its record (a proof and a verdict, `via: "epic"`) names the OWNING ISSUE (the ask issue or the
# work item), the plan hash and a commit of the default branch that holds the merge commit of every merged pull request of the plan. A pull request never carries one.

EPIC_VIA = "epic"
EPIC_STATES = ("unrecorded", "recorded", "stale")


def epic_scenarios(plan):
    """The scenarios of a checked plan that are claimed at the epic, in order."""
    return [s for s in (plan or {}).get("scenarios_parsed") or [] if _plan_ledger().claim_of(s) == "epic"]


def epic_state(scenario, entries, ref, plan_hash, default_head, authors, contains, changed):
    """One epic-claimed scenario's picture on its owning issue `ref`: {id, evaluator, claim, state, status, head, missing[], void[], why}. `state` is `unrecorded` (no proof and verdict of one commit, a fail, a verdict by a writer, or a record whose commit lacks a
    claimant's merge), `recorded` (at `head`; `current` when it is the default branch's head) or `stale` (a file the proof names changed since, or the change cannot be judged). `contains(head)` -> (True, None) or (False, reason);
    `changed(old, new)` -> the files changed, or None when the compare cannot be judged (it counts as stale); `default_head` None (unreadable) makes every record stale. The newest proof decides the commit; the verdict must be at the same commit."""
    sid, evaluator = scenario["id"], _plan_ledger().evaluator_of(scenario)
    out = {"id": sid, "evaluator": evaluator, "claim": "epic", "state": "unrecorded", "status": "missing", "head": None, "current": False, "missing": [], "void": [], "why": None}
    mine = [e for e in entries if e.get("pr") == ref and e.get("scenario") == sid and e.get("via") == EPIC_VIA and e.get("kind") in ("proof", "verdict")]
    here = [e for e in mine if e.get("plan_hash") == plan_hash]
    if mine and not here:
        out["void"].append("record #%d void: plan changed" % mine[-1]["seq"])
    proofs = [e for e in here if e["kind"] == "proof"]
    proof = proofs[-1] if proofs else None
    if proof is None:
        out["missing"].append("proof (`prove %s --pr %s`)" % (sid, ref))
    elif proof.get("result") != "pass":
        out["missing"].append("proof says fail")
    verdicts = [e for e in here if e["kind"] == "verdict" and e.get("evaluator") == evaluator and (proof is None or e.get("head") == proof.get("head"))]
    verdict = verdicts[-1] if verdicts else None
    if verdict is None:
        out["missing"].append("%s verdict at %s (`verdict %s --pr %s --as %s`)" % (evaluator, proof["head"][:8] if proof else "the proof's commit", sid, ref, evaluator))
    elif verdict.get("result") != "pass":
        out["missing"].append("%s verdict says fail" % evaluator)
    elif verdict.get("by") in authors:
        out["missing"].append("the verdict is by a writer of the record (not accepted)")
    if out["missing"]:
        return out
    head = proof["head"]
    ok, why = contains(head)
    if not ok:
        out["void"].append("the record at %s is void: %s" % (head[:8], why))
        out["missing"].append("a record at a commit that holds every merged pull request of the plan (%s)" % why)
        return out
    out.update(head=head, state="recorded", status="ok")
    if default_head is None:
        out.update(state="stale", status="missing", why="the default branch's head could not be read")
    elif head == default_head:
        out["current"] = True
    else:
        files = changed(head, default_head)
        named = set(proof.get("files") or ())
        if files is None:
            out.update(state="stale", status="missing", why="the change from %s to %s cannot be judged" % (head[:8], default_head[:8]))
        elif files and (not named or named & set(files)):
            out.update(state="stale", status="missing", why="%s changed since %s" % (", ".join(sorted(named & set(files)) or sorted(files)[:3]), head[:8]))
        else:
            out["why"] = "no file the proof names changed since %s" % head[:8]
    if out["state"] == "stale":
        out["missing"].append("stale: %s (record it again at the default branch's head)" % out["why"])
    return out


def epic_lines(rows, default_head):
    """The lines `gates` prints for the epic-claimed scenarios: one per scenario, unrecorded, recorded at a head, or stale."""
    lines = []
    for r in rows:
        if r["state"] == "unrecorded":
            text = "unrecorded: %s" % "; ".join(r["missing"] + r["void"])
        elif r["state"] == "stale":
            text = "recorded at %s, STALE: %s" % (r["head"][:8], r["why"])
        else:
            text = "recorded at %s (%s)" % (r["head"][:8], "the default branch's head" if r["current"] else "still valid at %s: %s" % ((default_head or "?")[:8], r["why"]))
        lines.append("  epic %s (%s): %s" % (r["id"], r["evaluator"], text))
    return lines
