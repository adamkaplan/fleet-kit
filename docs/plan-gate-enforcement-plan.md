# Plan: switching gate enforcement on (plan-driven development)

Status: **proposal, for the chief of staff and then the owner. Nothing here is switched on.** It sits after the gate engine
design ([plan-gate-engine-design.md](plan-gate-engine-design.md)) and the shadow-only slices that implement it: `gates-check` and the
mode switches (#156), the `gh` shim probe (#157) and the `launch` check (#158). Names are neutral (`acme/app`).

## 1. What flips, and what does not

**First flip (one repo, one gate, two commands):**

| | |
|---|---|
| Repo | **this repository only** (fleet-kit), `mode: "enforce"` |
| Gate | **G1** (every per-PR scenario has a passing proof and a passing verdict from its named evaluator, at this plan hash and this head), required by G0a (the plan passes `plan check`) |
| Commands | `gh pr ready` and `gh pr merge` by a fleet agent through the shim. Not `gh pr create`, not `launch` |
| Not enforced | G4 (CI) and G6 (held PR) at merge stay report-only in the first week; G0b, G0c, G2/G3, G7, G8 are not tracked |

Why fleet-kit and why G1 first: it is the repo whose owner is the author of the design, G1 is the gate with the most data behind it
(the ledger and verdicts exist), and a mistake here is cheap to see and to switch off. The design says the engine's own repository
should stay in shadow (a fix to the engine would be gated by the engine); **this plan asks the owner to change that rule for fleet-kit
only after the kill switch has been tried (section 4), and to keep the global off file as the escape**. If the owner prefers to keep
the engine's repo in shadow, the first enforcing repo is a product repo that opted in, with the same steps.

**Second flip (after the cut rule is satisfied, section 6):** add G4 and G6 to the merge check, then a second repo. **`launch` stays
shadow** until the plan review engine writes consensus records (nothing does yet).

## 2. Preconditions on the machine (all must hold; `gates preflight` lists the first two)

1. **Evaluators registered** in the switchboard config, under `plan_evaluators`: at least a recorder for `ci` (required: enforcement
   is refused without it), and the agents that may record `reviewer`, `tester` and `owner` verdicts for the scenarios the repo's plans
   name. By default nobody is registered, so no verdict can be recorded: enabling enforcement without this deadlocks every merge.
   Who is registered is the owner's decision (a config edit; the chief of staff cannot do it).
2. **A `ci` recorder exists in practice**, not only on paper: an agent that reads the PR's CI result at the head and records the `ci`
   verdict (`verdict <scenario> --as ci`). Until one is running, plans that name `ci` scenarios cannot reach G1.
3. **The way out for the chief of staff** (a separate, held PR; see section 5): `gates` (for `gates mode` and `gates off`) and `waive` in cos's
   allowed commands, in the permission generator and in the permission corpus. **Changes to cos's permissions are held for the owner**, so
   this plan only names them. Enforcement is not switched on before this PR is merged and live: cos is the one who can turn it off.
4. **The shim is live** on the machine (the owner fast-forwards the main checkout), and `plan_gates` lists the repo in `shadow` first
   (section 3) so the shim starts probing.
5. **The switchboard binary the shim uses** is the main checkout's (the doctor's `switchboard-path` check).
6. **Plans exist for the work**: in `enforce` a PR with no `PLAN:` link fails G0a, so agents must already be writing plans. (First week:
   shadow records how often that is not so.)

## 3. Order of switching on

1. **Shadow first, in config:** `"plan_gates": {"repos": {"<owner>/fleet-kit": {"mode": "shadow"}}}` (the owner edits it, or a person at a
   shell runs `gates mode <owner>/fleet-kit shadow`). From the next `gh pr ready` or `gh pr merge` by an agent the shim records;
   `gates-check` records `gate.would_refuse` and `gate.fail_open`; `status` shows the day's counts per repo.
2. **Collect.** The plan's bar is **at least 20 PRs and a false-positive rate under 5% per gate**, decided by cos. A false positive is a
   `would_refuse` for a PR that was in fact fine (the evidence was there, or the gate misread it). Each `would_refuse` names the gates
   and what is missing, so cos can classify it. Fail-opens are counted: a repo whose timeouts exceed 5% is not eligible.
3. **Try the kill switch while still in shadow** (section 4), so the first time anyone uses it is not an incident.
4. **Flip G1 at ready and merge:** `gates mode <owner>/fleet-kit enforce`, which is **refused** unless the preflight passes; a config edited
   by hand to `enforce` runs as `shadow` until it does (logged as `gate.enforce_downgraded`).
5. **Watch for a week**, then decide on G4 and G6 (section 6).

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

After the first 20 enforced PRs, per gate (plan section 8): relax or remove a gate that produced **no blocking finding** and added **more than 10
minutes** median; decided by cos. G4 and G6 at merge come next if G1 shows a false-positive rate under 5%; a second repo only after that.

## 7. Shadow evidence so far

Read from the live audit log on the day this plan was written (a read-only count of `gate.*` and `plan.*` events):

| Event | Count |
|---|---|
| `gate.would_refuse`, `gate.fail_open`, `gate.refused`, `gate.killed`, `plan.proof`, `plan.verdict` | **0** |

**There is no shadow evidence yet, and the reason is plain, not a finding:** no repository is listed under `plan_gates` in this machine's
switchboard config, so the shim does not probe and `gates-check` has never run here. The main checkout is at the commit that contains the shim
probe, and the launch check (#158) is not live yet. Step 1 of section 3 is therefore the first real action, and the one-day summary this plan was meant
to quote can only be produced after it. The offline tests are the only evidence of behaviour today (the shim never blocks on any exit code, a hung check
costs at most its bound, unknown argument shapes pass through).

## 8. What is NOT built yet

- **`waive`** (the logged cos waiver for G0 to G5, with its due date and overdue alarm).
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

## 9. Decisions asked of cos, then the owner

1. Is fleet-kit the right first repo, given the design's "the engine's repository stays in shadow" rule? (Section 1.)
2. Who is registered for each evaluator, and who runs the `ci` recorder? (Section 2.)
3. Approve building `waive` and the held permissions PR before enforcement. (Sections 5 and 8.)
4. Approve the shadow bar (20 PRs, under 5% false positives per gate) and who classifies a `would_refuse`.
