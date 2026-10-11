# Plan: switching gate enforcement on (plan-driven development)

Status: **proposal, for the chief of staff and then the owner. Nothing here is switched on.** The owner approved plan v3.3 with the gates **enforced live from the start**: there is no shadow phase in this plan, and `shadow` is only the soft-disable. It sits after the gate engine
design ([plan-gate-engine-design.md](plan-gate-engine-design.md)) and the shadow-only slices that implement it: `gates-check` and the
mode switches (#156), the `gh` shim probe (#157) and the `launch` check (#158). Names are neutral (`acme/app`).

## 1. What flips, and what does not

**First flip, straight to enforcement (one repo, one gate, two commands):**

| | |
|---|---|
| Repo | **this repository only** (fleet-kit), `mode: "enforce"` |
| Gate | **G1** (every per-PR scenario has a passing proof and a passing verdict from its named evaluator, at this plan hash and this head), required together with G0a (the plan passes `plan check`): `enforce_gates` is the default `["G0a", "G1"]` |
| Commands | `gh pr ready` and `gh pr merge` by a fleet agent through the shim. Not `gh pr create`, not `launch` |
| Reported, not enforced | G4 (CI) and G6 (held PR) at merge: they appear in every check's output as "reported, not enforced" and in the audit as `gate.reported`, and refuse only when the repo lists them under `enforce_gates` (the code for this list is its own small PR, listed in section 8) |

There is **no 20-PR, 5% shadow phase**: the owner approved enforcement from the start. What stands in for it is the way back (section 4), which is tried before
the flip, and the fail-open rule: a check that cannot answer allows. `shadow` remains as the soft-disable (`gates mode <repo> shadow`), not as a phase.

The design says the engine's own repository should stay in shadow, because a fix to the engine would be gated by the engine. This plan asks the owner to change
that rule for fleet-kit (section 9) and keeps the global off file as the escape. If the owner prefers to keep the engine's repo in shadow, the first enforcing
repo is a product repo that opted in, with the same steps.

**Second flip:** add G4 and G6 to `enforce_gates` for fleet-kit once the owner is satisfied, then a second repo. **`launch` stays shadow** until the plan review
engine writes consensus records (nothing does yet).

## 2. Preconditions on the machine (all must hold; `gates preflight` lists the first two)

1. **Evaluators registered** in the switchboard config, under `plan_evaluators`: at least a recorder for `ci` (required: enforcement
   is refused without it), and the agents that may record `reviewer`, `tester` and `owner` verdicts for the scenarios the repo's plans
   name. By default nobody is registered, so no verdict can be recorded: enabling enforcement without this deadlocks every merge.
   Who is registered is the owner's decision (a config edit; the chief of staff cannot do it).
2. **The `ci` recorder works in practice**, and the preflight checks it: list `"ci": ["switchboard"]` and run `gates ci-record <repo#N>` on a PR whose CI is green. The preflight refuses the reserved name until the audit holds a successful record from the last 7 days, so listing it on paper is not enough; plans that name `ci` scenarios reach G1 only through this recorder (or an agent registered for `ci`).
3. **The way out for the chief of staff** (a separate, held PR; see section 5): `gates` (for `gates mode` and `gates off`) and `waive` in cos's
   allowed commands, in the permission generator and in the permission corpus. **Changes to cos's permissions are held for the owner**, so
   this plan only names them. Enforcement is not switched on before this PR is merged and live: cos is the one who can turn it off.
4. **The shim is live** on the machine (the owner fast-forwards the main checkout), and `plan_gates` lists the repo (section 3, step 2:
   first in `shadow`, for the rehearsal of the way back) so the shim starts probing.
5. **The switchboard binary the shim uses** is the main checkout's (the doctor's `switchboard-path` check).
6. **An `owner` scenario needs the owner at a terminal.** A plan scenario that names `owner` (or the old personal spelling, an alias) is judged only by a verdict recorded from the owner's own terminal, so each such scenario holds its PR until the owner does it; plans should name `owner` sparingly.
7. **Review becomes a recorded verdict.** Enforcing in fleet-kit changes how reviews are given: a review comment is not a `reviewer` verdict, so each independent review agent must be registered and must run `verdict <scenario> --as reviewer --result ... --evidence ...` for each scenario it judges. That is a cost on every PR; it is the point of the gate, and the decision in section 9 is made knowingly.
8. **Plans exist for the work**: in `enforce` a PR with no `PLAN:` link fails G0a, so agents must already be writing plans. (There is no shadow week to measure how often that is not so; expect refusals at first, and the way out in section 4.)

## 3. Order of switching on

1. **Land the code first** (section 8): the `enforce_gates` list (own small PR), `waive`, the built-in `ci` recorder, enforcement in the shim, the cos permissions PR.
   Each is its own reviewed PR; the permissions PR is held for the owner.
2. **Opt in and try the way back while the repo is still in `shadow`**: `"plan_gates": {"repos": {"<owner>/fleet-kit": {"mode": "shadow"}}}` (a config edit by the
   owner, or `gates mode <owner>/fleet-kit shadow` by a person at a shell). Run `gates off`, then `gates on`, then `gates mode ... off` and back, so the first time anyone
   uses them is not an incident. (Checks run in shadow for these few minutes only as the rehearsal; it is not an evidence phase.)
3. **Check the preflight:** `gates preflight <owner>/fleet-kit` must say nothing blocks it, and its notes for `reviewer`, `tester` and `owner` must be empty (or accepted)
   for the plans the repo actually has: an unregistered evaluator deadlocks that scenario at merge. Enforcement is refused while `ci` has no recorder.
4. **Flip:** `gates mode <owner>/fleet-kit enforce`. It is refused unless the preflight passes, and a config edited by hand to `enforce` runs as `shadow` until it does
   (logged as `gate.enforce_downgraded`).
5. **Watch (a first-day look replaces the shadow phase's measured timeout rate)**: cos reads `status` (checks, refusals and fail-opens per repo) at the end of the first day and again after the first week; a repo whose fail-opens come mostly from timeouts needs a larger `budget_ms`. `status` shows the day's checks, refusals and fail-opens per repo; the first fail-open of each kind per repo per day alarms cos. A gate that refuses
   something it should not is a rollback (section 4), then a fix.

## 4. Rollback

| Situation | Action | Who | Effect |
|---|---|---|---|
| A gate refuses something it should not | `fleet-switchboard gates mode <repo> shadow` | cos (once its permission is in place) or the owner at a shell | back to recording only, at the next command, no restart |
| A repo should be off entirely | `gates mode <repo> off` (`enabled: false`) | same | the repo behaves as not opted in |
| Everything must stop now | `gates off` (creates `plan-gates.off` in the state directory) | cos, the owner; any local process can also `touch` it | every repo allowed; each such check is logged `gate.killed`; `status` prints it loudly |
| The engine itself is broken | the same, then fix it in a PR | | the engine does not gate its own repair once its repo is off or in shadow |
| A hotfix cannot wait | a cos waiver of G0 to G5 for that PR (`waive`, **not built yet**) with a reason and a retroactive plan within a day; G6 and G8 are never waived | cos | until `waive` exists, use `gates mode <repo> shadow` for the hotfix and back |
| The shim itself misbehaves | remove `plan_gates` from the config: the shim stops probing at once (it reads the config per command) | the owner | zero cost |

Every refusal prints what is missing and how to get out, and the real `gh` is untouched when the check cannot answer: **a timeout, a crash,
an unreadable GitHub or a missing switchboard allow** (fail-open); only the check's exit 10 refuses.

## 5. The held PR for cos's way out (separate, not part of enforcement)

A small PR that adds `gates` and `waive` to the chief of staff's allowed commands in `tools/cos-permissions-proof/gen_permissions.py`,
regenerates the permission list, and extends the corpus with the cases (`gates mode <repo> shadow` allowed; `gates off` allowed; `waive` allowed;
the same words by another role refused). It is a who-may-do-what change: first line `MERGE: captain`, held for the owner. `waive` itself
must be built first (it is in the design, not in the code).

## 6. Cut rule and the next gates

Per gate, after the first 20 enforced PRs (plan section 8): relax or remove a gate that produced **no blocking finding** and added **more than 10 minutes** median;
decided by cos. G4 and G6 at merge come next (`enforce_gates`), then a second repo. This is a review of the gates after use, not a precondition to enforcing.

## 7. Evidence about the live state

Read-only, on the day this plan was written: the live audit log holds **0** `gate.*` and `plan.*` events, and no repository is listed under `plan_gates` in this machine's
switchboard config, so the shim has never probed and `gates-check` has never run here. The main checkout contains the shim probe and the launch check. With no shadow
phase there is no field data to quote: the evidence is the offline tests (the shim never blocks on any exit code but 10, a hung check costs at most its bound, unknown
argument shapes pass through, a broken ledger refuses G1 only in enforce, a hand-edited `enforce` runs as shadow until the preflight passes).

## 8. What is NOT built yet

- **The `enforce_gates` list** (a repo in `enforce` must not refuse for G4 or G6 unless listed): its own small PR, built next.
- **`waive`** (the logged cos waiver for G0 to G5, with its due date and overdue alarm).
- **A daemon pass that runs `gates ci-record`** on open PRs (today it is a command; someone or something must run it, and run it again within 7 days so the preflight stays satisfied). The recorder itself, the owner-terminal verdict and the preflight that refuses an unproven recorder are built (the owner/ci PR).
- **The plan review engine** and its **consensus record** (G0b): nothing writes a `consensus` entry, so the launch check always records `would_refuse` and cannot
  enforce.
- **Plan approval records** (G0c) and the **alignment review record** (G2/G3): untracked, so they cannot block.
- **Enforcement in the shim**: the shim only records; no code path exits non-zero on a refusal yet. That is the enforcement PR itself, and it must add
  `--match-head-commit` to the real merge, refuse `--auto` in enforce, and post-creation checks for `gh pr create`.
- **`plan check`**: an epic claim must name its integration branch (`epic-claim-without-branch`).
- **A reader for the shim's spool** (`gate-failopen.jsonl`): the shim writes it; no daemon code reads it.
- **Tier S** (the small-change fast path), **a `ci` recorder agent**, and the **cos permission change** (section 5).
- **A GitHub required check**, the only real boundary; a proposal to repo owners, not code.
- **Per-gate fail-closed**, after four weeks of clean shadow (the design implements fail-open only).

## 9. Decision for the owner

One decision, with everything needed to make it. **Approve switching fleet-kit to enforcement (G1 at `gh pr ready` and `gh pr merge`) once section 8's code has merged,
with the configuration below, and rule that the engine's own repository may be enforced (the design said it should stay in shadow).**

**Exact configuration lines** (the switchboard config; a person edits it, cos cannot):

```json
"plan_gates": {
  "repos": {
    "<owner>/fleet-kit": {"mode": "enforce", "enforce_gates": ["G0a", "G1"], "budget_ms": 3000}
  }
},
"plan_evaluators": {
  "ci":       ["switchboard"],
  "reviewer": ["<independent-review-agent-1>", "<independent-review-agent-2>"],
  "tester":   ["<coder-agent-a>", "<coder-agent-b>", "<coder-agent-c>"]
}
```

**Who may record which verdict (the recommendation):**

| Evaluator | Who | Why |
|---|---|---|
| `ci` | **the switchboard itself**, reading the head's check runs through `gh`: `gates ci-record <repo#N>` records the verdict (reserved name `switchboard`) only at the exact head and plan hash and only for a complete, green run; not an agent | a machine's verdict should come from the machine; no agent can claim CI passed. The preflight refuses `["switchboard"]` until the audit holds a successful `ci-record` from the last 7 days |
| `reviewer` | **the independent review agents** (the ones that review PRs today), never the PR's writers | independence is the point; the existing author exclusion applies |
| `tester` | **coder sessions other than the PR's author** | a second pair of hands runs the live scenarios. Today the config takes agent names, so each standing coder name is listed; a role-based entry (`role:coder`) is a small build if names change often |
| `owner` | **the owner only, from the owner's own terminal**: a caller with no agent session at all. An agent session, registered or not (the chief of staff included), is refused. There is no registry entry for it | the owner's verdicts are the owner's. All agents share one GitHub login, so the login is not the test; the session identity is (a guard against mistakes, not a boundary) |

**The mandatory CI recorder, and a merge while GitHub Actions is down or blocked.** Enforcement is refused while `ci` has no recorder. If Actions is
blocked or down, the `ci` scenarios cannot pass and `ci` has no verdict to record. **Decision (recommended): use the logged chief-of-staff waiver, with a local
run as its evidence.** A tester who is not the PR's author runs the repository's full suite locally at the head and gives the result (the output's last lines and
the head) to cos; cos waives G1 for that PR with `waive <repo#N> --gates G1 --reason "Actions unavailable: full suite run locally at <head> by <tester>: <result>"`. The waiver is logged, bound to that head
(a push voids it), and carries the one-day retroactive-plan due date. A tester **verdict** cannot stand in for the `ci` verdict, because a `verdict --as ci` is accepted only from the `ci`
recorder. The judgement that the local run is good enough is cos's, not the machine's, and is said so in the waiver. G6 and G8 are never waived. When G4 is later added to `enforce_gates` (section 6) the same outage blocks G4, so the waiver must name `G1,G4`.

**Rollback** (who runs what; each takes effect at the next command, no restart):

| Situation | Command | Who |
|---|---|---|
| a gate refuses something it should not | `fleet-switchboard gates mode <owner>/fleet-kit shadow` | cos, or the owner at a shell |
| the repo should be off | `fleet-switchboard gates mode <owner>/fleet-kit off` | cos, the owner |
| everything must stop now | `fleet-switchboard gates off` (and `gates on` to lift it) | cos, the owner |
| the shim itself misbehaves | remove `plan_gates` from the config | the owner |
| a hotfix cannot wait | a logged cos waiver for G0 to G5 (never G6 or G8), then a plan within a day | cos |

cos can run the first three only once the permissions PR (section 5) is merged and live: **enforcement is not switched on before that**, so the way back is always in
reach of the agent that must use it.

**The exact message an agent sees when it is refused** (one line; the gates it names are the enforced ones that failed):

```
fleet-switchboard: merge of acme/app#12 refused: missing G1: S2: proof (`prove S2`); tester verdict (`verdict S2 --as tester`). Way out: ask the chief of staff for a logged waiver (`fleet-switchboard waive acme/app#12 --gates G1 --reason ...`; never for G6 or G8), or the chief of staff can switch the repo to shadow (`fleet-switchboard gates mode acme/app shadow`).
```

(`ready` reads the same with `ready of ...`.) `waive` is **not built yet** and the permission that lets cos run it is held for the owner: until both exist the waiver half of this line is a path that errors, which is one more reason the enforcement flip waits for them (section 3, step 1). The agent's next step is to record the missing proof and verdict, or to report to its orchestrator and cos; it does not try to get around the gate.

**What the owner is asked to rule on:** the enforcement of fleet-kit with the configuration above; the evaluator registry (who, by name); the CI-down procedure (cos waiver with a non-author local run as evidence);
and that the engine's own repository is enforced, with the global off file as the escape.
