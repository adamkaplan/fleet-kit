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
import time
from pathlib import Path

LEDGER_FILE = "plan-ledger.jsonl"
HEAD_FILE = "plan-ledger.head"   # {"seq", "hash"} of the newest entry: a second place the chain's end is written
GENESIS = "0" * 64
EVIDENCE_KINDS = ("proof", "verdict", "consensus", "waiver")   # a consensus entry is written by the plan review engine (a later slice)
REVIEW_KINDS = ("round", "lane_session", "lane", "response", "escalation", "decision", "abandoned", "reminder")   # the plan review engine's records (#109)
KINDS = EVIDENCE_KINDS + REVIEW_KINDS
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
    pointer = _read_head(state_dir)
    if pointer is not None and (len(entries) < pointer["seq"] or entries[pointer["seq"] - 1]["hash"] != pointer["hash"]):
        return entries, ("the ledger ends before entry %d, where its head pointer says it should (entries were cut off or the "
                         "file was rewritten)" % pointer["seq"])
    return entries, None


def _read_head(state_dir):
    try:
        data = json.loads((Path(state_dir) / HEAD_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data.get("seq"), int) and isinstance(data.get("hash"), str) and data["seq"] > 0 else None
    except (OSError, ValueError, AttributeError):
        return None


def _write_head(state_dir, entry):
    path = Path(state_dir) / HEAD_FILE
    temp = path.with_name("%s.%d.tmp" % (HEAD_FILE, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps({"seq": entry["seq"], "hash": entry["hash"]}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temp), str(path))


def append_entry(state_dir, entry, now):
    """Add `entry` to the ledger under an exclusive lock: seq, time, prev and hash are set here. Refuses when the
    existing chain is broken (a tampered ledger takes no new entry). Returns the stored entry."""
    return append_checked(state_dir, now, lambda entries, seq: entry)


def append_checked(state_dir, now, build):
    """The same append, with the DECISION inside the critical section: `build(entries, seq)` is called under the exclusive lock with the
    chain as it stands and the sequence number the entry will get, and returns the entry to write (or raises to refuse). A caller that must
    check the ledger and then add to it (one open review per lineage, a review id) does both here, so two writers cannot both pass the check."""
    path = Path(state_dir) / LEDGER_FILE
    fd = os.open(str(path), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        entries, problem = read_ledger(state_dir)
        if problem:
            raise LedgerError("the ledger is not trustworthy, nothing was recorded: %s" % problem)
        entry = build(entries, len(entries) + 1)
        if entry.get("kind") not in KINDS or (entry.get("kind") in EVIDENCE_KINDS and entry.get("result") not in RESULTS):
            raise LedgerError("an entry is a proof, a verdict, a consensus or a waiver with a result of pass or fail, or a plan review record")
        stored = dict(entry, seq=len(entries) + 1, ts=int(now), prev=entries[-1]["hash"] if entries else GENESIS)
        stored["hash"] = entry_hash(stored)
        os.write(fd, (json.dumps(stored, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(fd)
        _write_head(state_dir, stored)
    finally:
        os.close(fd)
    return stored


def append_many(state_dir, entries_in, now):
    """ALL of `entries_in`, or none: every entry is validated first, then the whole batch is chained and written with ONE write under
    the exclusive lock, so a failure (an invalid entry, a broken chain, a failed write) leaves the ledger as it was. Returns the stored
    entries."""
    for entry in entries_in:
        if entry.get("kind") not in KINDS or (entry.get("kind") in EVIDENCE_KINDS and entry.get("result") not in RESULTS):
            raise LedgerError("an entry is a proof, a verdict, a consensus or a waiver with a result of pass or fail, or a plan review record")
    path = Path(state_dir) / LEDGER_FILE
    fd = os.open(str(path), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        have, problem = read_ledger(state_dir)
        if problem:
            raise LedgerError("the ledger is not trustworthy, nothing was recorded: %s" % problem)
        stored, prev = [], have[-1]["hash"] if have else GENESIS
        for number, entry in enumerate(entries_in, start=len(have) + 1):
            one = dict(entry, seq=number, ts=int(now), prev=prev)
            one["hash"] = entry_hash(one)
            stored.append(one)
            prev = one["hash"]
        if stored:
            data = "".join(json.dumps(one, sort_keys=True, ensure_ascii=False) + "\n" for one in stored).encode("utf-8")
            if os.write(fd, data) != len(data):
                raise LedgerError("the ledger write was short: nothing was recorded")
            os.fsync(fd)
            _write_head(state_dir, stored[-1])
    finally:
        os.close(fd)
    return stored


# ----------------------------------------------------------------------------------- who may do what

def evaluator_of(scenario):
    name = " ".join(str(scenario.get("evaluator", "")).split())
    return EVALUATOR_ALIASES.get(name, name)


def authors_of(entries, pr, notice_senders=(), github_author=None):
    """The writers of this PR: the author GitHub reports for it (not a field the writer sets), whoever recorded a PROOF
    for it, and the senders of its notice. An evaluator can never be one of them."""
    names = {e.get("by") for e in entries if e.get("pr") == pr and e.get("kind") == "proof"}
    names.update(notice_senders)
    names.add(github_author)
    return {n for n in names if n}


def known_writers(entries, pr, notice_senders=()):
    """True when the switchboard knows at least one agent that wrote this PR (a proof or a notice): until then a verdict is
    refused, because a writer that has declared nothing could otherwise register as its own evaluator."""
    return bool(authors_of(entries, pr, notice_senders))


def may_record_verdict(evaluator, caller, allowed, authors):
    """(ok, why not). `allowed` maps an evaluator (ci|tester|reviewer|owner) to the agent names that may record its
    verdicts; an evaluator with no names is one nobody may record for yet (the default). The caller must not be a
    writer of the PR."""
    names = (allowed or {}).get(evaluator) or []
    if caller == CI_RECORDER:
        return False, "`%s` is the built-in recorder's name, not an agent: `ci` verdicts are recorded by `gates ci-record`" % CI_RECORDER
    if caller in authors:
        return False, ("%s wrote this PR (recorded its proof or sent its notice): an author cannot record a verdict "
                       "on its own work" % caller)
    if caller not in names:
        if not names:
            return False, ("no agent is registered to record %s verdicts: list its names under `plan_evaluators.%s` "
                           "in the switchboard config" % (evaluator, evaluator))
        return False, "%s is not registered to record %s verdicts" % (caller, evaluator)
    return True, None


# ----------------------------------------------------------------------------------- waivers

WAIVABLE = ("G0a", "G0b", "G0c", "G1", "G2/G3", "G4", "G5")   # G0 to G5: what the chief of staff may waive for a hotfix
NEVER_WAIVABLE = {"G6": "the owner's hold on a held PR", "G7": "the epic integration", "G8": "production"}
WAIVER_REASON_MIN = 20
WAIVER_SECONDS = 24 * 3600   # a waiver is valid this long (the retroactive-plan due date), and only at the head it names


def parse_waiver_gates(text):
    """([gate ids], error): `G0a,G1,G4` (G2 or G3 mean the one `G2/G3` gate). The error names what was refused and why."""
    chosen = []
    for raw in (text or "").replace(" ", "").split(","):
        if not raw:
            continue
        gate = {"G2": "G2/G3", "G3": "G2/G3", "g2/g3": "G2/G3"}.get(raw, raw[:1].upper() + raw[1:])
        if gate in NEVER_WAIVABLE:
            return [], "%s is never waived (%s): refused, nothing was recorded" % (gate, NEVER_WAIVABLE[gate])
        if gate not in WAIVABLE:
            return [], "%r is not a gate that can be waived (%s)" % (raw, ", ".join(WAIVABLE))
        if gate not in chosen:
            chosen.append(gate)
    return (chosen, None) if chosen else ([], "name the gates to waive: --gates G0a,G1,...")


def valid_waivers(entries, pr, head, now):
    """{gate: waiver entry}: the waivers of this PR recorded AT THIS HEAD that have not expired at `now`, newest first win. A
    waiver for another head, for another PR or past its expiry is not here; neither is a gate that can never be waived."""
    found = {}
    if not head:
        return found
    for entry in entries:
        if entry.get("kind") != "waiver" or entry.get("pr") != pr or entry.get("head") != head:
            continue
        if not isinstance(entry.get("expires"), (int, float)) or entry["expires"] <= now:
            continue
        for gate in entry.get("gates") or ():
            if gate in WAIVABLE:
                found[gate] = entry
    return found


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
PER_PR_NONE_RE = re.compile(r"(?im)^[\s#>*-]*per-pr scenarios:\s*none\s+because\s+(\S.*)$")
PLAN_LINK_RE = re.compile(r"(?m)^\s*PLAN:\s*(\S+)")
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


def evaluate_gates(plan, entries, ledger_problem, facts, pr, now=None):
    """The gate table for one PR. `plan` is {"result", "defects", "scenarios_parsed", "plan_hash"} from the plan checker
    (None when no plan could be found), `facts` is what `gh pr view` said (head, ci, captain, body, notice_senders).

    Returns {"gates", "ready", "untracked", "scenarios", "head", "plan_hash", "ledger_problem"}. `ready` is true when
    every tracked gate that applies to a PR is satisfied; the untracked ones are listed, not counted."""
    head = (facts or {}).get("head")
    authors = authors_of(entries, pr, (facts or {}).get("notice_senders") or (), (facts or {}).get("author"))
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
                none_because = PER_PR_NONE_RE.search(plan.get("text") or "")
                if not claimed:
                    # G1 never passes vacuously: no per-PR scenario needs an explicit, reasoned statement
                    if none_because and len(none_because.group(1).strip()) >= 20:
                        row.update(status="n/a", detail="no per-PR scenarios: %s (the epic gate applies)" % none_because.group(1).strip()[:80])
                    else:
                        row.update(status="missing", missing=["no scenario is claimed per PR: state `per-pr scenarios: none because "
                                                              "<reason>` in the plan, or claim one"])
                elif bad:
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
    waived = []
    if now is not None and head and not ledger_problem:   # an untrustworthy ledger honours no waiver
        waivers = valid_waivers(entries, pr, head, now)
        for row in rows:
            entry = waivers.get(row["gate"])
            if entry is not None and row["status"] == "missing":
                row.update(status="waived", was_missing=row["missing"], missing=[],
                           detail="waived (by %s, %s, due %s)" % (entry.get("by") or "?", entry.get("reason") or "no reason",
                                                                time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(entry["expires"]))))
                waived.append(row["gate"])
    ready = all(r["status"] in ("ok", "n/a", "untracked", "waived") for r in rows)
    return {"gates": rows, "ready": ready, "waived": waived, "untracked": [r["gate"] for r in rows if r["status"] == "untracked"],
            "scenarios": scen_rows, "head": head, "plan_hash": (plan or {}).get("plan_hash"),
            "ledger_problem": ledger_problem}


SATISFIED = ("ok", "n/a", "untracked", "waived")


def enforced_view(report, enforced):
    """Add `enforced` (the gates the repo refuses on, or None when it refuses on none: shadow, unlisted or off) and
    `ready_enforced` (those gates are all satisfied) to `report`. The full-board `ready` is left as it was."""
    report["enforced"] = list(enforced) if enforced else None
    rows = {r["gate"]: r for r in report["gates"]}
    open_enforced = [g for g in (enforced or ()) if rows.get(g) and rows[g]["status"] not in SATISFIED]
    report["ready_enforced"] = (not open_enforced) if enforced else report["ready"]
    report["open_enforced"] = open_enforced
    report["open_not_enforced"] = [r["gate"] for r in report["gates"]
                                   if r["gate"] not in (enforced or ()) and r["status"] not in SATISFIED] if enforced else []
    return report


def render_gates(report, pr):
    """The lines `gates` prints."""
    if report.get("enforced"):
        names = ", ".join(report["enforced"])
        state = ("READY for the enforced gates (%s)" % names if report["ready_enforced"]
                 else "NOT READY for the enforced gates (%s): open %s" % (names, ", ".join(report["open_enforced"])))
        if report["open_not_enforced"]:
            state += "; not enforced: %s" % ", ".join(
                "%s %s" % (g, next(r["status"] for r in report["gates"] if r["gate"] == g)) for g in report["open_not_enforced"])
    else:
        state = ("READY on the gates it tracks (this is not approval: see the untracked list)"
                 if report["ready"] else "NOT READY")
    lines = ["gates for %s at %s: %s" % (pr, (report.get("head") or "unknown head")[:8], state) + (
                 "; WAIVED (not passed): %s" % ", ".join(report["waived"]) if report.get("waived") else ""),
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


# ----------------------------------------------------------------------------------- the gate engine's decision (slice 3a)
#
# `gates-check` asks this module one question: given a PR's gate report, the repo's mode and what the check is for, allow
# or refuse? Nothing here reads GitHub, a file or the clock. Nothing calls it from a shim yet and nothing is enforced.

MODES = ("shadow", "enforce")
CHECK_FOR = ("ready", "merge")
DEFAULT_BUDGET_MS = 3000
BUDGET_RANGE = (500, 30000)
# the gates each check requires, among those the engine tracks (G0b, G0c and G2/G3 are untracked and cannot refuse)
REQUIRED_FOR = {"ready": ("G0a", "G1"), "merge": ("G0a", "G1", "G4", "G6")}
DEFAULT_ENFORCED = ("G0a", "G1")   # the gates a repo in `enforce` refuses on unless its `enforce_gates` says otherwise
ENFORCEABLE = ("G0a", "G1", "G4", "G6")
EXIT_ALLOW, EXIT_REFUSE, EXIT_FAIL_OPEN = 0, 10, 3   # only EXIT_REFUSE refuses; every other exit is an allow


def normal_repo(name):
    return str(name or "").strip().lower()


def validate_plan_gates(raw, source):
    """The `plan_gates` section checked and completed: {"repos": {"owner/repo": {"mode", "enabled", "budget_ms"}}}.
    Raises ValueError naming the key. A repo is matched case-insensitively; two entries that differ only by case are refused."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("%s: 'plan_gates' must be an object, got %s" % (source, type(raw).__name__))
    unknown = sorted(set(raw) - {"repos", "ci_record"})
    if unknown:
        raise ValueError("%s: 'plan_gates' has key(s) this tool does not read: %s" % (source, ", ".join(unknown)))
    repos, seen = {}, {}
    for name, entry in (raw.get("repos") or {}).items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
            raise ValueError("%s: 'plan_gates.repos' key %r is not owner/repo" % (source, name))
        if normal_repo(name) in seen:
            raise ValueError("%s: 'plan_gates.repos' lists %s twice (repos match without regard to case)" % (source, name))
        seen[normal_repo(name)] = name
        if not isinstance(entry, dict):
            raise ValueError("%s: 'plan_gates.repos.%s' must be an object" % (source, name))
        extra = sorted(set(entry) - {"mode", "enabled", "budget_ms", "enforce_gates"})
        if extra:
            raise ValueError("%s: 'plan_gates.repos.%s' has key(s) this tool does not read: %s" % (source, name, ", ".join(extra)))
        mode = entry.get("mode", "shadow")
        if mode not in MODES:
            raise ValueError("%s: 'plan_gates.repos.%s.mode' must be shadow or enforce (to switch a repo off, set enabled: false), got %r"
                             % (source, name, mode))
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("%s: 'plan_gates.repos.%s.enabled' must be true or false" % (source, name))
        budget = entry.get("budget_ms", DEFAULT_BUDGET_MS)
        if not (isinstance(budget, int) and not isinstance(budget, bool) and BUDGET_RANGE[0] <= budget <= BUDGET_RANGE[1]):
            raise ValueError("%s: 'plan_gates.repos.%s.budget_ms' must be a whole number from %d to %d"
                             % (source, name, *BUDGET_RANGE))
        gates = entry.get("enforce_gates", list(DEFAULT_ENFORCED))
        if not (isinstance(gates, list) and gates and all(g in ENFORCEABLE for g in gates) and len(set(gates)) == len(gates)):
            raise ValueError("%s: 'plan_gates.repos.%s.enforce_gates' must be a non-empty list of distinct gates from %s "
                             "(default %s), got %r" % (source, name, ", ".join(ENFORCEABLE), ", ".join(DEFAULT_ENFORCED), gates))
        repos[name] = {"mode": mode, "enabled": enabled, "budget_ms": budget, "enforce_gates": [g for g in ENFORCEABLE if g in gates]}
    record = raw.get("ci_record") if raw.get("ci_record") is not None else {}
    if not isinstance(record, dict) or set(record) - {"enabled", "calls_per_pass", "pass_seconds"}:
        raise ValueError("%s: 'plan_gates.ci_record' must be an object with enabled, calls_per_pass and pass_seconds" % source)
    enabled, calls, seconds = record.get("enabled", True), record.get("calls_per_pass", 30), record.get("pass_seconds", 10)
    if not isinstance(enabled, bool):
        raise ValueError("%s: 'plan_gates.ci_record.enabled' must be true or false" % source)
    if not (isinstance(calls, int) and not isinstance(calls, bool) and 4 <= calls <= 100):
        raise ValueError("%s: 'plan_gates.ci_record.calls_per_pass' must be a whole number from 4 to 100" % source)
    if not (isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 1 <= seconds <= 60):
        raise ValueError("%s: 'plan_gates.ci_record.pass_seconds' must be a number from 1 to 60" % source)
    return {"repos": repos, "ci_record": {"enabled": enabled, "calls_per_pass": calls, "pass_seconds": seconds}}


def repo_setting(config, repo):
    """The repo's {"mode", "enabled", "budget_ms"} from the validated config, or None when it is not opted in."""
    for name, entry in ((config.get("plan_gates") or {}).get("repos") or {}).items():
        if normal_repo(name) == normal_repo(repo):
            return dict(entry, name=name)
    return None


CI_RECORDER = "switchboard"   # the reserved name of the built-in `ci` recorder (`gates ci-record`); not an agent
CI_RECORD_FRESH_SECONDS = 7 * 86400


def preflight(config, repo, ci_proven=None, cos_ready=None):
    """(blocking, notes) for switching `repo` to enforce. Blocking: a `ci` recorder must be registered AND able to record. The
    reserved name `switchboard` (the built-in recorder) is accepted only when `ci_proven()` says it has actually recorded
    recently (a successful `gates ci-record` in the audit within CI_RECORD_FRESH_SECONDS); an agent name is accepted as
    registered (it records through `verdict --as ci`). Notes are what the check cannot know: other evaluators are listed when
    unregistered, and the permission change that lets cos use the escape hatch is a manual precondition."""
    evaluators = config.get("plan_evaluators") or {}
    blocking, notes = [], []
    ci = evaluators.get("ci") or []
    if not ci:
        blocking.append("no `ci` recorder is registered: list `%s` (the built-in recorder) or an agent under plan_evaluators.ci" % CI_RECORDER)
    elif CI_RECORDER in ci and not any(n != CI_RECORDER for n in ci) and not (ci_proven() if callable(ci_proven) else ci_proven):
        blocking.append("the built-in `ci` recorder is listed but has not recorded recently: run `fleet-switchboard gates ci-record <repo#N>` "
                        "on a PR whose CI is green (a successful record within 7 days proves it works)")
    if callable(cos_ready):
        lacking = cos_ready()
        if lacking:
            blocking.append("the chief of staff cannot yet turn enforcement off: %s (the cos permission change, #163, must be merged and "
                            "installed before enforcement is switched on)" % lacking)
    notes.append("`owner` verdicts are recorded only from the owner's own terminal (no agent session), never by an agent")
    for kind in ("reviewer", "tester"):
        if not evaluators.get(kind):
            notes.append("no agent is registered for `%s`: a plan that names it cannot get a verdict (register it under "
                         "plan_evaluators.%s)" % (kind, kind))
    if not callable(cos_ready):
        notes.append("manual: the chief of staff must be allowed `gates mode`/`gates off` and `waive` (a permission change held "
                     "for the owner) before enforcement is switched on: cos is the one who can turn it off")
    return blocking, notes


COS_NEEDS = ("fleet-switchboard waive *", "fleet-switchboard gates mode * shadow")   # kept for callers that name them
# What the Chief of Staff MUST be able to run (the way out of a refusal), and what it must NOT be able to run (turning enforcement ON is the
# owner's). Judged by evaluating each probe against the installed definition's shell rules, not by looking for exact resource strings.
COS_MUST_ALLOW = ("fleet-switchboard waive acme/app#1 --gates G1 --reason hotfix", "fleet-switchboard gates mode acme/app shadow", "fleet-switchboard gates off")
COS_MUST_DENY = ("fleet-switchboard gates mode acme/app enforce", "fleet-switchboard gates mode acme/app ENFORCE", "fleet-switchboard gates on",
                 "timeout 30 fleet-switchboard gates mode acme/app enforce", "timeout 30 fleet-switchboard gates mode acme/app ENFORCE", "timeout 5 fleet-switchboard gates on",
                 "timeout 5 fleet-switchboard waive acme/app#1 --gates G6 --reason hotfix",
                 "fleet-switchboard waive acme/app#1 --gates G6 --reason hotfix", "fleet-switchboard waive acme/app#1 --gates G8 --reason hotfix")


def _frontmatter(text):
    """The front matter of a definition (between the first two `---` lines), or None when the text has none."""
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return None
    return text[4:].split("\n---\n", 1)[0]


def _permission_lines(text):
    """The lines of the `permissions:` block of a definition's FRONT MATTER only (body prose is never read). A text with no front matter is
    taken as a bare rule list (a test's or a tool's input)."""
    front = _frontmatter(text)
    if front is None:
        return text.splitlines()
    lines = front.splitlines()
    return lines[lines.index("permissions:") + 1:] if "permissions:" in lines else []


def cos_rules(text):
    """The (action, resource, effect) rules of a definition's `permissions:` list, in order. Two shapes are read: the one-line flow
    form `- {action: shell, resource: "x", effect: allow}` (the installed short list) and the older block form with `action:`,
    `resource:` and `effect:` on separate lines."""
    import json as _json
    import re as _re
    rules, action, resource = [], None, None
    for raw in _permission_lines(text):
        line = raw.strip()
        flow = _re.fullmatch(r"-\s*\{action:\s*(.+?),\s*resource:\s*(\".*\"),\s*effect:\s*(allow|deny|ask)\s*\}", line)
        if flow:
            try:
                rules.append((flow.group(1).strip().strip('"'), _json.loads(flow.group(2)), flow.group(3)))
            except ValueError:
                pass
            continue
        line = line[2:].strip() if line.startswith("- ") else line
        if line.startswith("action:"):
            action = line.split(":", 1)[1].strip().strip('"')
        elif line.startswith("resource:"):
            resource = line.split(":", 1)[1].strip().strip('"')
        elif line.startswith("effect:") and resource is not None:
            rules.append((action or "shell", resource, line.split(":", 1)[1].strip().strip('"')))
            resource = None
    return rules


def _glob_match(pattern, command):
    """OpenCode's whole-value glob: `*` any run, `?` one character; a trailing ` *` also matches the bare command."""
    import re as _re
    rx = "".join(".*" if c == "*" else "." if c == "?" else _re.escape(c) for c in pattern)
    if pattern.endswith(" *"):
        rx = rx[:-len(_re.escape(" ") + ".*")] + "(?: .*)?"
    return _re.fullmatch(rx, command, _re.S) is not None


def cos_allows(rules, command):
    """True when the shell `rules` let `command` run, as v2 decides it: the LAST rule of the shell action (or the base `*`) that matches
    wins, and a command no rule matches is denied. (So a deny only holds when it comes after the allow that would admit the command.)"""
    verdict = "deny"
    for action, resource, effect in rules:
        if action in ("shell", "bash", "*") and _glob_match(resource, command):
            verdict = effect
    return verdict == "allow"


def cos_lacking(text):
    """What the installed chief-of-staff definition (its text, or None when unreadable) gets wrong about the way out of a refusal:
    "" when it can run `waive` (G0 to G5), `gates mode <repo> shadow` and `gates off`, and CANNOT turn enforcement on (`gates mode ...
    enforce`, `gates on`) or waive G6 or G8. A cos that could turn it on is as blocking as one that cannot turn it off."""
    if text is None:
        return "the installed chief-of-staff definition could not be read"
    front = _frontmatter(text)
    if front is not None and any(line.startswith("name:") for line in front.splitlines()):
        return "its front matter has a `name:` key: OpenCode v2 then ignores the whole `permissions:` list"
    rules = cos_rules(text)
    missing = [probe for probe in COS_MUST_ALLOW if not cos_allows(rules, probe)]
    too_much = [probe for probe in COS_MUST_DENY if cos_allows(rules, probe)]
    out = []
    if missing:
        out.append("it does not allow `%s`" % "`, `".join(missing))
    if too_much:
        out.append("it allows `%s` (turning enforcement on, or waiving G6 or G8, is not the chief of staff's)" % "`, `".join(too_much))
    return "; ".join(out)


def effective_mode(config, setting, ci_proven=None, cos_ready=None):
    """(mode, why): the mode a check really runs in. `enforce` runs only when its preflight passes, wherever the mode is
    read (the config file can be edited by hand, so the switch command is not the only way in); otherwise it runs as
    `shadow` and `why` says what blocks it. A repo that is not enforcing has no `why`."""
    if setting["mode"] != "enforce":
        return setting["mode"], None
    blocking, _ = preflight(config, setting.get("name") or "", ci_proven, cos_ready)
    if blocking:
        return "shadow", "enforce is not active: " + "; ".join(blocking)
    return "enforce", None


def decide(report, check_for, mode, enforce_gates=None):
    """(verdict, failed, why): verdict is "allow" or "refuse". `report` is evaluate_gates' output. `failed` lists EVERY required
    gate that is not satisfied, each marked `enforced` (it is in `enforce_gates`, default G0a and G1). In `enforce` only an
    enforced failure refuses; a failure of a gate that is not listed is reported and allowed (G4 and G6 are reported until the
    repo lists them). In `shadow` nothing refuses and every failure is the would-refuse record. A gate that is not tracked or
    does not apply never fails a check."""
    listed = set(enforce_gates or DEFAULT_ENFORCED)
    failed = []
    for row in report["gates"]:
        if row["gate"] in REQUIRED_FOR[check_for] and row["status"] in ("missing", "waiting"):
            failed.append({"gate": row["gate"], "missing": row["missing"][:3], "enforced": row["gate"] in listed})
    if report.get("ledger_problem") and not any(f["gate"] == "G1" for f in failed):
        failed.append({"gate": "G1", "missing": ["the ledger is not trustworthy: %s" % report["ledger_problem"]], "enforced": "G1" in listed})
    verdict = "refuse" if mode == "enforce" and any(f["enforced"] for f in failed) else "allow"
    why = "; ".join("%s: %s%s" % (f["gate"], "; ".join(f["missing"]) or "not satisfied", "" if f["enforced"] else " (reported, not enforced)")
                    for f in failed)
    return verdict, failed, why


def consensus_at(entries, plan_hash):
    """The newest `consensus` entry of the ledger that passed at exactly `plan_hash` (G0b), or None. A record for an earlier
    hash does not count: editing the plan voids it."""
    for entry in reversed(entries):
        if entry.get("kind") == "consensus" and entry.get("plan_hash") == plan_hash and entry.get("result") == "pass":
            return entry
    return None


def consensus_overridden(entries, plan_hash):
    """Who overrode the consensus at exactly `plan_hash` (a person or a boss decided past an open objection), or None: an overridden
    consensus is visible wherever consensus is read."""
    found = consensus_at(entries, plan_hash)
    return found.get("overridden_by") if found else None


def launch_findings(brief, plan, entries):
    """What a launch under this brief lacks, as [{"gate", "missing": [...]}] (E4 of the gate engine design): a `PLAN:` link, a
    plan that passes `plan check` (G0a), and a consensus record at its CURRENT plan hash (G0b). `plan` is the checked plan or
    None. Empty when nothing is lacking."""
    if not PLAN_LINK_RE.search(brief or ""):
        return [{"gate": "G0b", "missing": ["the brief has no `PLAN:` link"]}]
    found = []
    if plan is None:
        return [{"gate": "G0a", "missing": ["the plan the brief links could not be read"]}]
    if plan["result"] != "pass":
        found.append({"gate": "G0a", "missing": ["the plan fails `plan check` (%d defect(s))" % len(plan["defects"])]})
    if consensus_at(entries, plan["plan_hash"]) is None:
        found.append({"gate": "G0b", "missing": ["no consensus record at the plan's current hash %s" % plan["plan_hash"][:19]]})
    return found


def refusal_message(prog, check_for, pr, failed, repo):
    """The ONE line an agent sees when a ready or merge is refused: what is missing, and the two ways out. Only the gates that
    are enforced are named as the reason; gates that are only reported are not."""
    gates = [f for f in failed if f.get("enforced", True)]
    what = "; ".join("%s: %s" % (f["gate"], "; ".join(f["missing"]) or "not satisfied") for f in gates)[:260]
    return ("%s: %s of %s refused: missing %s. Way out: ask the chief of staff for a logged waiver (`%s waive %s --gates %s "
            "--reason ...`; never for G6 or G8), or the chief of staff can switch the repo to shadow (`%s gates mode %s shadow`)."
            % (prog, check_for, pr, what, prog, pr, ",".join(f["gate"] for f in gates) or "G1", prog, repo))
