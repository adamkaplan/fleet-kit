"""plan_ledger: the switchboard-held evidence ledger and the read-only gates view of plan-driven development (#107).

Two halves, both free of GitHub, v2 and the clock (the caller passes what it read):

  - the ledger: an append-only JSON-lines file in the state directory. Every entry carries the hash of the one before
    it, so an edit, a deletion or a reordering anywhere in the file is found by `read_ledger`. An entry is a PROOF (the
    worker says what it ran and saw for a scenario) or a VERDICT (an evaluator says pass or fail for a scenario). Each is
    bound to a plan hash (a change to the plan voids it) and to a PR head (a push voids it);
  - the gates: `evaluate_gates` turns a checked plan, the ledger and what GitHub says about a PR into one line per gate
    G0a to G8 of the plan, each with its status, who decides it and what is missing. It enforces nothing.

The rules that need a decision of policy are inputs, not choices made here: who may be an evaluator is configuration.
"""
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path

LEDGER_FILE = "plan-ledger.jsonl"
GENESIS = "0" * 64
KINDS = ("proof", "verdict")
RESULTS = ("pass", "fail")
EVIDENCE_LIMIT = 500
FIELD_LIMIT = 200
EVALUATOR_ALIASES = {"adam": "owner"}


class LedgerError(Exception):
    """The ledger cannot be written or read as it must be."""


def _canonical(entry):
    return json.dumps({k: v for k, v in entry.items() if k != "hash"}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def entry_hash(entry):
    """sha256 over the entry's canonical text (its own `hash` excluded), which includes `prev`: the chain."""
    return hashlib.sha256(_canonical(entry).encode("utf-8")).hexdigest()


def clip(text, limit=FIELD_LIMIT):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def read_ledger(state_dir):
    """(entries, problem): every line parsed, and None when the chain holds, else a sentence naming the first break
    (the entries before it are still returned: they are intact, the ones from the break on are not trusted)."""
    path = Path(state_dir) / LEDGER_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], None
    except OSError as err:
        raise LedgerError("cannot read the ledger: %s" % (err.strerror or err)) from None
    entries, prev = [], GENESIS
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            return entries, "line %d is not JSON: the ledger was edited" % number
        if not isinstance(entry, dict) or entry.get("prev") != prev or entry.get("hash") != entry_hash(entry) \
                or entry.get("seq") != len(entries) + 1:
            return entries, "entry %d does not follow from the one before it: the ledger was edited" % (len(entries) + 1)
        entries.append(entry)
        prev = entry["hash"]
    return entries, None


def append_entry(state_dir, entry, now):
    """Add `entry` to the ledger under an exclusive lock: seq, time, prev and hash are set here. Refuses when the
    existing chain is broken (a tampered ledger takes no new entry). Returns the stored entry."""
    if entry.get("kind") not in KINDS or entry.get("result") not in RESULTS:
        raise LedgerError("an entry is a proof or a verdict with a result of pass or fail")
    path = Path(state_dir) / LEDGER_FILE
    fd = os.open(str(path), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        entries, problem = read_ledger(state_dir)
        if problem:
            raise LedgerError("the ledger is not trustworthy, nothing was recorded: %s" % problem)
        stored = dict(entry, seq=len(entries) + 1, ts=int(now), prev=entries[-1]["hash"] if entries else GENESIS)
        stored["hash"] = entry_hash(stored)
        os.write(fd, (json.dumps(stored, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return stored


# ----------------------------------------------------------------------------------- who may do what

def evaluator_of(scenario):
    name = " ".join(str(scenario.get("evaluator", "")).split())
    return EVALUATOR_ALIASES.get(name, name)


def authors_of(entries, pr, notice_senders=()):
    """The agents that wrote this PR: whoever recorded a PROOF for it, and the senders of its notice. An evaluator
    can never be one of them."""
    names = {e.get("by") for e in entries if e.get("pr") == pr and e.get("kind") == "proof"}
    names.update(notice_senders)
    return {n for n in names if n}


def may_record_verdict(evaluator, caller, allowed, authors):
    """(ok, why not). `allowed` maps an evaluator (ci|tester|reviewer|owner) to the agent names that may record its
    verdicts; an evaluator with no names is one nobody may record for yet (the default). The caller must not be a
    writer of the PR."""
    names = (allowed or {}).get(evaluator) or []
    if caller in authors:
        return False, ("%s wrote this PR (recorded its proof or sent its notice): an author cannot record a verdict "
                       "on its own work" % caller)
    if caller not in names:
        if not names:
            return False, ("no agent is registered to record %s verdicts: list its names under `plan_evaluators.%s` "
                           "in the switchboard config" % (evaluator, evaluator))
        return False, "%s is not registered to record %s verdicts" % (caller, evaluator)
    return True, None


# ----------------------------------------------------------------------------------- the gates

GATES = (
    # id, name, scope (P per PR, E per epic, D per deploy), decision owner, tracked here
    ("G0a", "structure", "P", "the switchboard (rule)", True),
    ("G0b", "consensus", "P", "the switchboard records the review loop", False),
    ("G0c", "approval", "P", "the plan's approver (work item: its orchestrator; ask: cos; epic: cos, then the owner)", False),
    ("G1", "proof", "P", "the evaluator named on each scenario", True),
    ("G2/G3", "alignment and code", "P", "the reviewer agent", False),
    ("G4", "checks", "P", "CI, or a second agent's re-run", True),
    ("G5", "canon", "P", "the orchestrator", True),
    ("G6", "held PR", "P", "the owner, through cos", True),
    ("G7", "epic integration", "E", "tester agent and the owner", False),
    ("G8", "production", "D", "the owners; the owner for production", False),
)
UNTRACKED_WHY = {
    "G0b": "the plan review engine is a later slice: no consensus record exists yet",
    "G0c": "approval is not recorded by the switchboard yet",
    "G2/G3": "the alignment review is not recorded by the switchboard yet",
    "G7": "an epic gate: it applies at the integration branch, not to one PR",
    "G8": "a deploy gate: it applies at deploy, not to one PR",
}
CANON_RE = re.compile(r"(?im)^\s*canon:\s*(\S.*)$")


def canon_status(body):
    """(ok, detail) for the G5 checklist line: `canon: <what was updated>`, or `canon: n/a <reason>` with a reason."""
    found = CANON_RE.search(body or "")
    if not found:
        return False, "the PR body has no `canon:` line (docs updated, or `canon: n/a <reason>`)"
    value = found.group(1).strip()
    if re.match(r"(?i)^n/?a\b", value):
        reason = re.sub(r"(?i)^n/?a\b[\s:,-]*", "", value).strip()
        return (len(reason) >= 5, "`canon: n/a` with a reason" if len(reason) >= 5 else "`canon: n/a` needs a reason")
    return (len(value) >= 3, "`canon:` line present" if len(value) >= 3 else "the `canon:` line is empty")


def scenario_state(scenario, entries, pr, plan_hash, head, authors):
    """One scenario's G1 picture: {id, evaluator, proof, verdict, status, missing[], void[]}. A proof or verdict counts
    only for THIS plan hash and THIS head; one for another is shown as void, not hidden."""
    sid, evaluator = scenario["id"], evaluator_of(scenario)
    out = {"id": sid, "evaluator": evaluator, "claim": scenario.get("claim") or "pr", "missing": [], "void": []}
    for kind in ("proof", "verdict"):
        mine = [e for e in entries if e.get("pr") == pr and e.get("scenario") == sid and e.get("kind") == kind
                and (kind == "proof" or e.get("evaluator") == evaluator)]
        fresh = [e for e in mine if e.get("plan_hash") == plan_hash and e.get("head") == head]
        newest = fresh[-1] if fresh else None
        if not newest and mine:
            why = "plan changed" if mine[-1].get("plan_hash") != plan_hash else "head moved"
            out["void"].append("%s #%d void: %s" % (kind, mine[-1]["seq"], why))
        out[kind] = None if not newest else {"seq": newest["seq"], "result": newest["result"], "by": newest.get("by")}
    proof, verdict = out["proof"], out["verdict"]
    if not proof:
        out["missing"].append("proof (`prove %s`)" % sid)
    elif proof["result"] != "pass":
        out["missing"].append("proof says fail")
    if not verdict:
        out["missing"].append("%s verdict (`verdict %s --as %s`)" % (evaluator, sid, evaluator))
    elif verdict["result"] != "pass":
        out["missing"].append("%s verdict says fail" % evaluator)
    elif verdict["by"] in authors:
        out["missing"].append("the verdict is by an author of the PR (not accepted)")
    out["status"] = "ok" if not out["missing"] else "missing"
    return out


def evaluate_gates(plan, entries, ledger_problem, facts, pr):
    """The gate table for one PR. `plan` is {"result", "defects", "scenarios_parsed", "plan_hash"} from the plan checker
    (None when no plan could be found), `facts` is what `gh pr view` said (head, ci, captain, body, notice_senders).

    Returns {"gates", "ready", "untracked", "scenarios", "head", "plan_hash", "ledger_problem"}. `ready` is true when
    every tracked gate that applies to a PR is satisfied; the untracked ones are listed, not counted."""
    head = (facts or {}).get("head")
    authors = authors_of(entries, pr, (facts or {}).get("notice_senders") or ())
    rows, scen_rows = [], []
    for gid, name, scope, owner, tracked in GATES:
        row = {"gate": gid, "name": name, "scope": scope, "decided_by": owner, "tracked": tracked, "status": "untracked",
               "missing": []}
        if scope != "P":
            row["status"] = "n/a"
            row["detail"] = UNTRACKED_WHY.get(gid, "")
        elif not tracked:
            row["detail"] = UNTRACKED_WHY.get(gid, "not recorded yet")
        elif gid == "G0a":
            if plan is None:
                row.update(status="missing", missing=["no plan found: pass --plan, or put `PLAN: <link>` in the PR body"])
            elif plan["result"] != "pass":
                row.update(status="missing", missing=["plan check fails: %d defect(s)" % len(plan["defects"])])
            else:
                row.update(status="ok", detail="plan hash %s" % plan["plan_hash"])
        elif gid == "G1":
            if plan is None or plan["result"] != "pass":
                row.update(status="missing", missing=["needs a plan that passes G0a"])
            elif not head:
                row.update(status="missing", missing=["the PR head is unknown"])
            else:
                claimed = [s for s in plan["scenarios_parsed"] if (s.get("claim") or "pr") == "pr"]
                scen_rows = [scenario_state(s, entries, pr, plan["plan_hash"], head, authors) for s in claimed]
                bad = [s for s in scen_rows if s["status"] != "ok"]
                row["detail"] = "%d of %d claimed scenarios proven and judged at %s" % (len(scen_rows) - len(bad), len(scen_rows), head[:8])
                if bad:
                    row.update(status="missing", missing=["%s: %s" % (s["id"], "; ".join(s["missing"])) for s in bad])
                else:
                    row["status"] = "ok"
                epic = [s["id"] for s in plan["scenarios_parsed"] if (s.get("claim") or "pr") == "epic"]
                if epic:
                    row["detail"] += "; epic-claimed, proven at the integration head: %s" % ", ".join(epic)
        elif gid == "G4":
            ci = (facts or {}).get("ci")
            if ci in ("green", "passing"):
                row.update(status="ok", detail="CI %s at %s" % (ci, (head or "?")[:8]))
            else:
                row.update(status="missing", missing=["CI is %s (needs green at the head, or a second agent's re-run)" % (ci or "unknown")])
        elif gid == "G5":
            ok, detail = canon_status((facts or {}).get("body"))
            row.update(status="ok" if ok else "missing", detail=detail, missing=[] if ok else [detail])
        elif gid == "G6":
            if (facts or {}).get("captain"):
                row.update(status="waiting", missing=["held for the owner (MERGE: captain): the owner decides, through cos"])
            else:
                row.update(status="ok", detail="not a held PR")
        rows.append(row)
    if ledger_problem:
        for row in rows:
            if row["gate"] == "G1" and row["status"] == "ok":
                row.update(status="missing", missing=["the ledger is not trustworthy: %s" % ledger_problem])
    ready = all(r["status"] in ("ok", "n/a", "untracked") for r in rows)
    return {"gates": rows, "ready": ready, "untracked": [r["gate"] for r in rows if r["status"] == "untracked"],
            "scenarios": scen_rows, "head": head, "plan_hash": (plan or {}).get("plan_hash"),
            "ledger_problem": ledger_problem}


def render_gates(report, pr):
    """The lines `gates` prints."""
    lines = ["gates for %s at %s: %s" % (pr, (report.get("head") or "unknown head")[:8],
                                          "READY (every tracked gate is satisfied)" if report["ready"] else "NOT READY"),
             "plan hash: %s" % (report.get("plan_hash") or "none")]
    if report.get("ledger_problem"):
        lines.append("LEDGER: %s" % report["ledger_problem"])
    for row in report["gates"]:
        detail = row.get("detail") or ""
        lines.append("  %-6s %-18s %-9s (%s)%s" % (row["gate"], row["name"], row["status"], row["decided_by"],
                                                   "  " + detail if detail else ""))
        lines.extend("           missing: %s" % m for m in row["missing"])
    for s in report.get("scenarios", []):
        for note in s["void"]:
            lines.append("  %s: %s" % (s["id"], note))
    if report["untracked"]:
        lines.append("not tracked yet (not counted): %s" % ", ".join(report["untracked"]))
    return lines
