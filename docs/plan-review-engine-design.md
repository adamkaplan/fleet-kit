# Design: the plan review engine (plan-driven development, #109 part 2)

Status: **proposal, for review. Nothing here is implemented.** It builds G0b, the consensus review of a plan, on the evidence ledger
(#153), the gate engine (#156) and the launch check that already exists in shadow (E4, #158). Names are neutral. It adds no
permission change: the engine writes the ledger and sends messages, and writes nothing to GitHub.

## What it does

`fleet-switchboard plan review` runs the adversarial review loop the plan describes (#109): independent reviewer lanes on one
fixed plan snapshot, at most 3 rounds, objections merged only by the planner, a consensus record bound to the plan hash, and an
escalation for an objection that survives round 3. It reports its cost and what the reviewers caught. `launch` keeps checking for the
consensus record in shadow (E4); refusing is a later, owner-held step.

## What is code and what is agent work

| Deterministic code (the switchboard) | Agent work (prompts) |
|---|---|
| plan snapshot and plan hash; `plan check` before every round | the lane charters (what each lane attacks), written as templates |
| which tier applies (a minimum computed from the plan, never self-declared) | the reviewer sessions themselves: reading the plan and writing objections |
| which lanes run in a round, and the round count and the cap of 3 | the planner's revision and its response table (accept or reject, with a reason) |
| launching the lane sessions and building each brief from a template | the synthesis of the final plan, with its Decisions section |
| who may record a lane verdict; binding it to the plan hash; refusing a stale or foreign one | the boss's and the owner's decision on an escalated objection |
| the tally, the consensus record, the escalation record and its message | |
| timeouts, the cost and catch report, the kill switch | |

Nothing an agent says decides a tally: the tally is computed from the lane entries in the ledger.

## Commands

All are subcommands of `fleet-switchboard plan review`. State is the existing hash-chained ledger (new entry kinds below).

| Command | Who | Does |
|---|---|---|
| `start <plan ref> [--tier T]` | planner | snapshots the plan (hash), runs `plan check` (must PASS), computes the minimum tier, opens round 1, launches the lanes. A requested tier below the minimum is refused. Prints the review id |
| `verdict <id> --lane L --verdict approve\|iterate\|reject --objections-file F` | a lane session | records the lane's verdict and its objections (id, severity blocking or advisory, evidence, suggested fix) |
| `respond <id> --plan-ref REV --table F` | planner | records the response table and the new revision (must pass `plan check`, new hash), and opens the next round |
| `status <id>` | anyone | rounds, lanes, open objections, time |
| `decide <id> --objection O --accept\|--override --reason ...` | the boss named in the escalation, or the owner at a terminal | resolves an escalated objection |
| `report <id>` | anyone | the cost and catch report (below) |

There is deliberately **no command that writes a consensus record**. The engine writes it itself, when the tally allows.

## The loop

1. **Start.** Tier is the minimum from the plan source: a plan for an ask, an epic, a held-PR area, or the chief of staff's own ask is
   `full`; a work item with its own plan `normal`; a tiny item under a reviewed section `light` (the plan's tiers: light is lanes L1 and
   L2; normal L1, L2, L4 and L3 for a held-PR area; full all six). The planner can ask for more, never less.
2. **Lanes.** Each lane is a fresh session of a read-only `plan-reviewer` agent (edit denied) launched through the existing `launch`
   path, so each is an ordinary agent with a name the switchboard recorded for this review, lane and round. The brief is built by the
   switchboard from a template: the plan snapshot, the ask's Intent and Done-when, the lane's charter and the verdict format. Never the
   planner's reasoning and never another lane's output. One lane runs on a different model family (a second agent definition whose
   `model:` is set, so launch needs no change). Lanes run in parallel, in waves of two or three to protect the machine.
3. **Verdicts.** A lane's `verdict` is accepted only from the session the switchboard launched for that lane and round (the caller's
   identity is read, as for `verdict` today), only for the snapshot's plan hash, and never from the plan's author or the planner. A
   second verdict from the same lane and round is refused. A wrong hash, a wrong lane or a foreign caller is refused and audited.
4. **Merge.** The planner alone answers: for every blocking objection it records accept (with the change) or reject (with the reason),
   and submits the new revision with `respond`. Reviewers never edit the plan.
5. **Next round.** Only the lanes that had blocking objections run again, plus L1 when the scenario hash changed (compared by code,
   not by claim). Round 3 is the last: a fourth `respond` or `start` for the same review is refused.
6. **Consensus.** Reached when no blocking objection is open, or each open one has a reason its lane then accepted. The engine checks
   that the final revision passes `plan check` and contains a `## Decisions` section (the synthesis the planner owes), then writes the
   `consensus` entry: plan hash, tier, rounds, lanes. Editing the plan afterwards voids it (the rule that exists today).
7. **Escalation.** A blocking objection still open after round 3 is recorded as an `escalation` entry and sent as a message to the
   planner's boss (and to the chief of staff when the boss is not), carrying the objection and the planner's response side by side.
   Until decided the review is `pending, objections open` and no consensus exists. The boss, or for the owner's items the owner at a
   terminal (the existing owner-terminal rule), resolves it with `decide`: accept the objection (the planner revises, outside the cap,
   one more round of that lane) or override it with a reason, which writes the consensus entry marked `overridden_by` with the reason.
   The engine sends the message through the existing outbox; the boss applies the `awaiting` labels as the charter says (the engine
   writes nothing to GitHub).
8. **Stuck lanes.** A lane with no verdict after a timeout (15 minutes proposed) is relaunched once; after that the engine records
   `lane.unavailable` and escalates it like an objection. A stalled lane never counts as an approval.

## Ledger entries

New kinds in the same hash-chained ledger, so tamper evidence is the existing one: `round` (review id, round, plan hash, lanes
launched), `lane` (review id, round, lane, verdict, objections, session name, started and ended), `response` (review id, round, the
table's hash, the new plan hash), `escalation`, `decision`; `consensus` is the existing kind. Only the switchboard's own functions
write `round`, `lane`, `escalation` and `consensus`; commands that take input validate it first. The gate reader keeps reading
`consensus` at the plan's current hash and ignores the other kinds, so E4 is unchanged.

## Cost and catch report

`report <id>` prints, per round and lane: wall time, verdict and the objections by severity, how many the planner accepted and rejected,
and the objection titles (what the reviewers caught); plus total sessions and total wall time, and the rounds to consensus. **Token and
money cost are not measured** (the harness records none yet): the report says so, and records the session names so a later measure can
join them. Estimates from the slice 0 run: about 5 minutes wall clock per full round, three rounds in about 30 minutes with the merge work;
a light review 2 sessions and about 10 minutes.

## Configuration

`plan_review`: `enabled` (default false: the commands refuse with a message), `lane_timeout_seconds`, `max_parallel`, and the lane to agent
map (so the different-model lane is a config line). A kill switch like the gates': a file that disables new rounds and leaves the ledger
readable. Opt-in per repo uses the existing `plan_gates` list.

## Slices (each mergeable alone, shadow-safe; the order is the build order)

| Slice | What | Notes |
|---|---|---|
| R1 | the pure core, `bin/plan_review.py`: tier minimum, the round and cap rules, the tally, consensus and escalation decisions, brief templates. No commands | fully unit tested, changes nothing live |
| R2 | the new ledger kinds and `start`, `verdict`, `status`, `respond` with fake lane launching in tests | `enabled` false by default: inert until configured |
| R3 | the `plan-reviewer` agent definitions (read-only; one on the other model family), the lane charters and templates, real launching of lanes | agent definitions only, no permission change |
| R4 | escalation: the message, `decide`, the override record, stuck-lane handling | the owner-terminal rule is reused |
| R5 | `report` and the consensus write that makes E4 stop saying would-refuse for reviewed plans | E4 stays shadow; enforcing it is a later, owner-held step |

## Tests

R1 as pure functions: the tier minimum (an ask can never be `light`); rounds 1 to 3 and the refusal of a fourth; only lanes with blocking
objections re-run, plus L1 on a changed scenario hash; consensus only when nothing blocking is open; an open objection after round 3 is an
escalation and never a consensus. R2 with fake launching: a verdict from a foreign or unlaunched caller is refused; a stale plan hash is
refused; a second verdict from a lane is refused; the planner's own verdict is refused; the ledger chain holds and a cut ledger takes no entry;
`enabled` false refuses every command. R3 checks the agent definitions are read-only and that a brief contains the snapshot, the Done-when
and the charter and no planner reasoning. R4: the escalation message names both sides; `decide` by a non-boss is refused; an override writes
a consensus with its reason. R5: the report's counts match the ledger; a consensus at the current hash makes the launch check record
`allow`, and an edited plan makes it `would_refuse` again.

## Risks and open questions

- Verdicts are only as independent as the identities: while agents share one GitHub account, a lane's identity is the session name the
  switchboard launched, a guardrail and not a boundary (as the gate engine says).
- A planner can still merge generously (slice 0 saw that); the cap and the escalation are the answer, and the report makes it visible.
- Cost per plan is an estimate until the harness records tokens. Open: whether a lane's token count can be read from the session.
- Open: the lane timeout and the wave size (proposed 15 minutes and 3).
- Rollback: set `plan_review.enabled` false or create the kill file; the ledger entries stay and E4 is unaffected.
