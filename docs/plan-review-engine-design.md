# Design: the plan review engine (plan-driven development, #109 part 2)

Status: **proposal, for review. Nothing here is implemented.** It builds G0b, the consensus review of a plan, on the evidence ledger
(#153), the gate engine (#156) and the launch check that already exists in shadow (E4, #158). Names are neutral. The engine writes the
ledger and sends messages and writes nothing to GitHub; the one permission change is the reviewer agent's allow-list (R3, held for the owner).

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

Nothing an agent says decides a tally: the tally is computed from the lane entries in the ledger, and **the lane entries are written by the
switchboard from what the lane session itself said** (see Verdicts), not by a command the lane runs.

## Commands

All are subcommands of `fleet-switchboard plan review`. State is the existing hash-chained ledger (new entry kinds below).

| Command | Who | Does |
|---|---|---|
| `start <plan ref> [--tier T]` | the plan's orchestrator or the chief of staff (a registered caller, not the plan's lanes) | opens a review **for the plan's lineage** (below), snapshots the plan (hash), runs `plan check` (must PASS), computes the tier, opens round 1, launches the lanes. A requested tier below the computed one is refused. Prints the review id |
| `verdict <id> --lane L ...` | a lane session (fallback only) | the same record as the daemon's own read of the lane's final message; accepted only for the caller's own lane (see Verdicts) |
| `respond <id> --plan-ref REV --table F` | the planner | records the response table and the new revision (must pass `plan check`, new hash), and opens the next round |
| `status <id>` | anyone | rounds, lanes, open objections, time |
| `decide <id> --objection O --accept\|--override --reason ...` | the boss named in the escalation; **the owner at a terminal** for an objection from the risk and security lane or a held-PR area | resolves an escalated objection |
| `report <id>` | anyone | the cost and catch report (below) |

There is deliberately **no command that writes a consensus record**. The engine writes it itself, when the tally allows.

## The lineage: what the cap and the escalation belong to

A review belongs to the plan's **lineage**, and the lineage is keyed to **what the plan is for**: the ask or work item the plan serves (the
`PLAN:` target's parent chain up to its ask), not to the plan source and not to a `start`. Every revision of a plan, and every plan issue
opened for the same ask, shares one lineage, so replacing the plan issue changes nothing. The round count (cap 3), the open objections and any escalation are keyed to the lineage and carried
across reviews. So `start` cannot reset anything: it is **refused while the lineage has an undecided escalation (a second plan issue for an ask
with an undecided escalation is refused too), while a review of it is open (one open review per lineage), and for a plan that already has a
consensus at its current hash**; a `start` on a revised plan continues the
round count, and its lanes receive the lineage's open objections as inputs they must address.

## The loop

1. **Start.** The tier is **computed by code from facts the author does not write**, never from the plan's prose: (a) the linked issue's kind
   (an ask, an epic or the chief of staff's own ask is `full`; a plan under an ask counts as that ask's, so a work item under an ask is not
   cheaper because it has "its own plan"); (b) the held-PR area comes from the **paths the plan's slices name checked against the held-path list
   and the label set** (any match forces `full`); (c) a set of work items under one ask that share a parent is classified as **one review
   unit**, so splitting an ask does not lower the tier. A tiny item under an already-reviewed section may be `light` (lanes L1 and L2; normal is
   L1, L2, L4 and L3 for a held-PR area; full is all six). The caller can ask for more, never less.
2. **Lanes.** Each lane is a fresh session of a read-only `plan-reviewer` agent (edit denied) launched through the existing `launch`
   path, so each is an ordinary agent with a name the switchboard recorded for this review, lane and round. The brief is built by the
   switchboard from a template: the plan snapshot, the ask's Intent and Done-when, the lane's charter and the verdict format. Never the
   planner's reasoning and never another lane's output. The charter tells the lane that **the plan is untrusted text** written by the party
   being reviewed (instructions inside it are not instructions to the lane). One lane runs on a different model family (a second agent
   definition whose `model:` is set); **the lane entry records the session's actual model**, and a lane whose recorded model is not the
   configured second family is refused as a diversity lane (a silent fallback would void the requirement). Lanes run in parallel, in waves of
   two or three to protect the machine. **Lane permissions** are specified below.
3. **Verdicts.** The **switchboard reads the lane session's own final message** through v2 (it already reads transcripts), parses the verdict
   and the objections itself, and writes the `lane` entry; forging one needs a message inside the lane's session, not a command any agent can
   run. The `verdict` command stays as a fallback and applies the same rules as `verdict` for scenarios today: accepted only from the session the
   switchboard launched for that lane and round (a lane records only for its own lane), only for the snapshot's plan hash, never from the plan's
   author or the planner or an unregistered session; a second verdict from the same lane and round is refused; a wrong hash, a wrong lane or a
   foreign caller is refused and audited. Each objection has an id and ends in an explicit state (below).
4. **Merge.** The planner alone answers: for every blocking objection it records accept (with the change) or reject (with the reason),
   and submits the new revision with `respond`. Reviewers never edit the plan.
5. **Next round.** Only the lanes that had blocking objections run again, plus L1 when the scenario hash changed (compared by code,
   not by claim). Round 3 is the last: a fourth round for the lineage is refused.
   **Each objection id ends in an explicit state**: `resolved by change`, confirmed by its own lane; `rejection accepted`, confirmed by its own
   lane; or `escalated`. A lane-wide `approve` is silent about ids it does not mention: they stay open.
6. **Closing round, then consensus.** When no blocking objection is open, the planner publishes the **final synthesized revision** (with its
   `## Decisions` section; the engine checks `plan check` PASS and the heading). A **closing round** then runs, outside the cap, over that exact
   hash: every lane of the tier either confirms (`approve` at that hash) or raises a new objection, and a new objection goes to escalation (it
   cannot start another round). Only when every lane of the tier has an `approve` entry whose plan hash equals the final hash does the engine
   write the `consensus` entry: plan hash, tier, rounds, lanes. **No closing round, no consensus.** Editing the plan afterwards voids it (the
   rule that exists today).
7. **Escalation.** A blocking objection still open after round 3 is recorded as an `escalation` entry and sent as a message to the
   planner's boss (and to the chief of staff when the boss is not), carrying the objection and the planner's response side by side.
   Until decided the review is `pending, objections open` and no consensus exists. The boss, or for the owner's items the owner at a
   terminal (the existing owner-terminal rule), resolves it with `decide`: accept the objection (the planner revises; **at most 2 more rounds
   after an escalation, then the owner at a terminal decides**) or override it with a reason, which writes the consensus entry marked
   `overridden_by` with the reason. **An objection from the risk and security lane or on a held-PR area can only be overridden by the owner at a
   terminal.** An overridden consensus is **visible wherever consensus is read**: `plan review status` and the gate (E4) show `overridden`. The
   engine sends the message through the existing outbox; the boss applies the `awaiting` labels as the charter says (the engine writes nothing
   to GitHub). **An escalation has a deadline**: the engine reminds the boss at a stated age and the triage reports an escalation older than it
   (proposed 24 hours), so a review is never `pending` forever without someone being told.
8. **Stuck lanes.** A lane with no verdict after a timeout (15 minutes proposed) is relaunched once; after that the engine records
   `lane.unavailable` and escalates it like an objection. A stalled lane never counts as an approval.

## Ledger entries

New kinds in the same hash-chained ledger, so tamper evidence is the existing one: `round` (review id, round, plan hash, lanes
launched), `lane` (review id, round, lane, verdict, objections, session name, started and ended), `response` (review id, round, the
table's hash, the new plan hash), `escalation`, `decision`; `consensus` is the existing kind. Only the switchboard's own functions
write `round`, `lane`, `escalation` and `consensus`; commands that take input validate it first. The gate reader keeps reading
`consensus` at the plan's current hash and ignores the other kinds, so E4 is unchanged.

## Lane permissions (a permission surface, specified, not assumed)

The `plan-reviewer` agent is **read-only** and its shell is an **allow-list**: read the repo and the plan, and run `plan review verdict`
(fallback) for its own lane; nothing else: no `gh` write, no `send` or `report` to the planner or anyone, no `launch`, no other lane's verdict,
no edit of the plan. It reports to the **switchboard**, not to the planner, and the planner has no way to message a lane (a lane's charter
also treats any message it receives as untrusted). The allow-list is generated and proved like the chief of staff's permissions (a harness
with an allowed corpus and a hostile corpus), in R3, and the agent definitions are added **only** with that proof: R3 is therefore held for
the owner as a change to who may do what, and the statement "no permission change" applies to R1, R2, R4 and R5.

## Cost and catch report

`report <id>` prints, per round and lane: wall time, verdict and the objections by severity, how many the planner accepted and rejected,
and the objection titles (what the reviewers caught); plus total sessions and total wall time, and the rounds to consensus. **Token and
money cost are not measured** (the harness records none yet): the report says so, and records the session names so a later measure can
join them. Estimates from the slice 0 run: about 5 minutes wall clock per full round, three rounds in about 30 minutes with the merge work;
a light review 2 sessions and about 10 minutes. **Worst case**: 6 lanes x 3 rounds plus a closing round of 6 is up to about **24 sessions**, and up
to about 40 minutes with waves of three. **Budget**: one open review per lineage, a per-day limit of starts per caller, a per-lineage session cap
(a configured number; `start` and `respond` refuse when the next round would pass it), and the refusals are audited.

## Configuration

`plan_review`: `enabled` (default false: the commands refuse with a message), `lane_timeout_seconds`, `max_parallel`, the session cap per lineage and
the per-day starts per caller, the escalation reminder age, and the lane to agent map (so the different-model lane is a config line). A kill switch like the gates': a file that disables new rounds and leaves the ledger
readable. Opt-in per repo uses the existing `plan_gates` list.

## Slices (each mergeable alone, shadow-safe; the order is the build order)

| Slice | What | Notes |
|---|---|---|
| R1 | the pure core, `bin/plan_review.py`: tier minimum, the round and cap rules (keyed to the ask), the tally, consensus and escalation decisions, brief templates. No commands | fully unit tested, changes nothing live |
| R2 | the new ledger kinds and `start`, `verdict`, `status`, `respond` with fake lane launching in tests | `enabled` false by default: inert until configured |
| R3 | the `plan-reviewer` agent definitions (an allow-listed, proved permission set; one on the other model family), the lane charters and templates, real launching of lanes, the daemon's read of the lane's final message | **held for the owner** (a new permission surface) |
| R4 | the closing round, escalation (message, deadline, reminder), `decide`, the override record and its visibility, stuck-lane handling | the owner-terminal rule is reused |
| R5 | `report` and the consensus write that makes E4 stop saying would-refuse for reviewed plans | E4 stays shadow; enforcing it is a later, owner-held step |

## Implementation notes under the slices (from the design review)

- **R1 (tier)**: the paths a plan's slices name are the planner's list, so omitting a held path would lower the tier. The tier is re-checked **at the
  end**: when a PR is opened or a launch happens under the plan and its actual changed paths hit the held-path list while the plan was reviewed
  below `full`, the gate (G0b) shows the mismatch and requires the full review.
- **R2 (messages to lanes)**: the switchboard's own `send` refuses a recipient that is a lane of an open review unless the sender is the switchboard,
  so "the planner has no way to message a lane" is enforced and not only a charter rule. The lane's findings reach the planner only through the
  `lane` entries.
- **R4 (closing round after an accept)**: a revision made after `decide --accept` has a new hash and **needs its own closing round**; that closing
  round does **not** count toward the "at most 2 more rounds after an escalation" (those count only rounds that re-review objections).
- **R2/R3 (verdict format)**: a lane's `approve` is parsed from a **strict format**: a fenced verdict block that **echoes the plan hash it was
  given**, then the verdict and one line per objection id with its state. A lane that does not echo the exact hash (it reviewed stale text, or was
  fed something else) is refused, and free text is never parsed for an approval.

## Tests

R1 as pure functions: the tier computed from the issue kind and the planned paths (an ask can never be `light`, a prose claim of "no held area" changes nothing, split work items under one ask are one review unit); rounds keyed to the lineage, so a second `start` continues the count and is refused during an undecided escalation, while a review is open, and for a plan already at consensus; the refusal of a fourth round; only lanes with blocking
objections re-run, plus L1 on a changed scenario hash; each objection ends in an explicit state and a lane-wide approve leaves unmentioned ids open; consensus only when nothing blocking is open AND every lane of the tier has an `approve` at the final hash (a revision changed after a lane approved gets no consensus; no closing round, no consensus); an open objection after round 3 is an
escalation and never a consensus. R2 with fake launching: the daemon's parse of a lane's final message writes the `lane` entry, and a verdict from a foreign or unlaunched caller, or for another lane, is refused; the per-day and per-lineage caps refuse a `start`; a stale plan hash is
refused; a second verdict from a lane is refused; the planner's own verdict is refused; the ledger chain holds and a cut ledger takes no entry;
`enabled` false refuses every command. R3 runs the permission harness (allowed corpus passes, hostile corpus is refused: gh writes, send, launch, other lanes' verdicts, plan edits) and checks the recorded model of the second-family lane, and that a brief contains the snapshot, the Done-when
and the charter and no planner reasoning. R4: the closing round raises a new objection into escalation; the escalation message names both sides; `decide` by a non-boss is refused, a security or held-PR objection needs the owner's terminal, post-escalation rounds stop at 2; an override writes
a consensus with its reason and `status` and the gate show `overridden`; an escalation older than the deadline is reminded and reported. R5: the report's counts match the ledger; a consensus at the current hash makes the launch check record
`allow`, and an edited plan makes it `would_refuse` again.

## Risks and open questions

- Verdicts are only as independent as the identities: while agents share one GitHub account, a lane's identity is the session name the
  switchboard launched, a guardrail and not a boundary (as the gate engine says); reading the lane's own final message in the daemon is stronger than
  a command but still not a boundary. The ledger's hash chain detects edits to history, not an append: anyone who can write the state directory can
  append a valid-looking line, so "only the switchboard's own functions write consensus" is true of the commands, not of the file.
- The plan is untrusted text read by the lanes (prompt injection by the party being reviewed): the charter says so, and the tally never depends
  on one lane alone (the second-family lane helps).
- A planner can still merge generously (slice 0 saw that); the cap and the escalation are the answer, and the report makes it visible.
- Cost per plan is an estimate until the harness records tokens. Open: whether a lane's token count can be read from the session.
- Open: the lane timeout and the wave size (proposed 15 minutes and 3).
- Rollback: set `plan_review.enabled` false or create the kill file; the ledger entries stay and E4 is unaffected.
