# Design: the gate engine (plan-driven development, slice 3)

Status: **proposal, for review. Nothing here is implemented.** It follows plan v3.3 (#107), sections 3 and 5, and builds on
the plan check (`plan check`), the evidence ledger (`prove`, `verdict`) and the read-only `gates` view (#153). Names are
neutral: `acme/app` is any repository.

## What this slice does

The gates of the plan become something the fleet's own tooling applies at four points: an agent's `gh pr ready` and
`gh pr merge` (and a non-draft `gh pr create`), and `fleet-switchboard launch`. It adds the controls that make that safe to
turn on: a mode per repository (off, shadow, enforcing), a kill switch, a logged waiver for a hotfix, and a fail mode.

It does **not** add the plan review engine (gate G0b), approval records (G0c) or the alignment review record (G2/G3). Those
are later slices; until they exist their gates stay "not tracked" and cannot block anything.

## Principles (from the plan)

1. **A guardrail for fleet-launched workers, not a security boundary.** The real boundary is a GitHub required check, proposed
   to repo owners. The shim can be bypassed (below); it exists to stop the honest mistake.
2. **Fail open, loudly.** If the engine cannot answer, the command proceeds and an alarm reaches cos. A wedged gate must never
   stop the fleet. (Plan: shadow fails open; enforcing fails open for 4 weeks of clean shadow, then closed per gate that has
   proven itself. This design implements **fail-open only**; fail-closed per gate is a later, separately approved step.)
3. **Opt-in per repository.** A repository that has not opted in sees no gate output and no change (plan scenario P7).
4. **Drafts carry no gate.** Converting back to draft is allowed. A draft cannot be merged.
5. **Verdicts come from the switchboard's record**, never from a worker's comment.

## Configuration

One new config key, `plan_gates`:

```json
"plan_gates": {
  "repos": {
    "acme/app":  {"mode": "shadow"},
    "acme/lib":  {"mode": "enforce", "enabled": true}
  }
}
```

- A repo not listed is not opted in: no check, no output.
- `mode`: `shadow` (compute and record "would refuse", never refuse) or `enforce`. There is no `off` value: remove the repo
  or set `enabled: false`.
- `enabled` (default `true` for a listed repo): **the kill switch**. `false` makes the repo behave as not opted in, at the
  next command, with no restart. A global kill switch is the file `plan-gates.off` in the state directory: its existence
  disables every repo. Both are logged each time they take effect (`gate.killed`).
- **Shadow is the soft-disable**: a repo can be switched from `enforce` to `shadow` to stop refusals while keeping the data.

*Open question 1:* the brief said "kill switch (config key, default on)". I read "on" as "the gates are active for a listed
repo", so the switch is `enabled: false`. If "default on" meant the switch itself is armed, the shape is the same with the
polarity named differently.

## Enforcement points

| # | Command | Checked | Where it runs |
|---|---|---|---|
| E1 | `gh pr ready <pr>` | G1 (every per-PR scenario proven and judged at the head) and G2/G3 when they become tracked | the `gh` shim |
| E2 | `gh pr create` without `--draft` | same as E1 for the new PR's plan; a PR with no `PLAN:` line is refused only in `enforce` | the `gh` shim |
| E3 | `gh pr merge <pr>` | G1 to G4 and G6 resolved at the head; a draft cannot be merged | the `gh` shim |
| E4 | `fleet-switchboard launch` (an assignment under a plan) | the brief's `PLAN:` link has a **consensus record at its current plan hash** (G0b); no `PLAN:` is refused only in `enforce` | `launch` itself |

How the shim asks: it runs `fleet-switchboard gates-check <pr> --for ready|merge` (a new, thin, read-only subcommand over the
existing `gates` logic) with a **3 s** limit and reads one of three outcomes from the exit code: `0` allow, `10` refuse (the
one-line reason on stderr is printed, naming the missing gates and the waiver path), anything else (a timeout, a crash, a
missing binary, the daemon down) is treated as **allow** and recorded as a fail-open (see below). The real `gh` then runs
unchanged. The shim never parses what it does not understand: an argument shape it cannot read is allowed, as signing is
today.

`gates-check` first resolves the repository and mode: not opted in, killed, or `shadow` all end in "allow", and shadow
records `gate.would_refuse` with the gates that failed.

## Failure mode: fail-open, with an alarm

| Failure | What happens | Recorded |
|---|---|---|
| the check times out or crashes | allow | `gate.fail_open` with the reason; counted |
| GitHub unreadable (head, CI, body) | allow (an unknown is not a refusal) | `gate.fail_open` |
| the ledger chain is broken | allow, and the broken ledger is the alarm | `gate.fail_open`, `ledger.tampered` |
| the switchboard binary is missing | the shim allows | a spool line the daemon reads (`gate-failopen.jsonl`), since the shim has no audit |
| config unreadable | allow, all repos treated as not opted in | `gate.fail_open` |

More than N fail-opens in an hour (default 3) raises one alarm to cos, using the existing alarm path (a fact, batched). A
fail-open is never silent and never a refusal.

## The waiver

For a hotfix, cos may waive G0 to G5 for one PR. **G6 (held PR) and G8 (production) are never waivable by an agent**: the
command refuses them.

`fleet-switchboard waive <repo#N> --gates G0a,G1,... --reason "<text>"`:

- only an agent whose fleet role is chief-of-staff (identity as for `verdict`: a mistake-guard, not a boundary);
- needs a reason (at least 20 characters) and creates a **retroactive-plan due date** one day out;
- is a ledger entry (kind `waiver`, bound to the PR and head, hash-chained) and an audit line;
- is visible in `gates` as `waived (by cos, reason, due ...)`, so a waived gate does not read as passed;
- raises an alarm to cos when the due date passes with no plan recorded.

A waiver covers the head it was recorded at; a push after it needs a new one (the same rule as a verdict).

## Who is the author: what the engine can and cannot know

All agents in a fleet act on GitHub under **one account**, so the login GitHub reports for a PR's author names that account,
never an agent: it can never match an agent name. (The ledger of #153 also excludes the GitHub author; that only matters for
a repo whose reviewer or tester agents act under a different login, and is otherwise a no-op.) The engine therefore identifies
the author **agent** from what the switchboard itself recorded, in this order:

1. **The signed marker.** Every PR a fleet agent creates through the shim carries `<!-- fleet-switchboard:from=<agent> -->`
   in its body, put there by the shim from the caller's session. The engine reads the marker of the PR body (and of the first
   commit-message-free PR description) as the authoring agent. A body edited by another agent can change the marker, so the
   engine reads the **first recorded** one: the creation is recorded as a ledger entry (`pr_created`, by the session identity,
   at the shim's `gh pr create`) and that record wins over the live body.
2. **The ledger.** Whoever recorded a proof for the PR, and the sender of its `notice pr`, are writers.
3. **The session registry** (#138): the identity of every `prove`, `verdict` and `waive` is the caller's own session
   (`OPENCODE_SESSION_ID` looked up in the switchboard's registry), never an inherited pane variable.

The ban is: an agent in that writer set cannot record a verdict, nor a waiver, for the PR. The set is the union of 1 to 3.

**What stays unprovable**, and is accepted:

- An agent that **wrote code for the PR without any record**: it pushed to the branch, recorded no proof and sent no notice,
  and did not create the PR. Nothing the switchboard holds names it. (Mitigation, not proof: a verdict is refused until some
  writer is known, as in #153; the commit authors cannot help, because every commit is authored by the same account.)
- A caller that **sets another agent's session variable** records as that agent. The session identity is a guard against
  mistakes, not against an agent that tampers with its own environment.
- A PR created **outside the shim** (absolute-path `gh`, the web UI, another clone) has no marker and no creation record; its
  author is unknown until a proof or notice names one.
- **Two agents that coordinate**: one writes, the other reviews, and the writer records nothing. The engine sees two
  unrelated agents. Independence of the reviewer is a property of who the fleet assigns, not something the gate can prove.

So the gate can show that **the recorded writers did not judge their own work**, never that the reviewer is independent of
everyone who touched it. The design says "recorded writer" everywhere for that reason, and `gates` prints the writer set it
used next to each verdict so a reader can see who was excluded.

## What is deliberately not here

- **Tier S** (at most 100 changed lines, 3 scenarios, no held path): classified by the switchboard from the diff. It needs the
  diff size and held-path rules, and is a separate slice; until then every PR in an opted-in repo follows the full gates.
- **G0b/G0c/G2-G3 enforcement**: they stay untracked and cannot refuse (E4 needs the consensus record, so E4 is shadow-only
  until the review engine exists).
- **Fail-closed**, a GitHub required check, and the bypass controls below.

## Bypass classes (accepted residuals)

`gh` by absolute path, `gh api` and GraphQL calls that mark ready or merge, `git push`, the web UI, other tools and MCP,
another clone of the repo. None is blocked here; each is listed so a reader does not assume otherwise. The native
permissions work (#105) closes the shim bypasses for cos and orchestrators. Pushing a PR branch is deliberately **not**
gated: only ready and merge are.

## Tests (all offline, synthetic)

| Area | Cases |
|---|---|
| mode and opt-in | a repo not listed: no output, no change; `shadow` never refuses and records would-refuse; `enforce` refuses; `enabled: false` and the `plan-gates.off` file each disable at the next command and are logged |
| E1/E2 | `ready` refused with G1 missing; allowed with all proven; a draft `create` allowed; a non-draft `create` with no `PLAN:` refused only in enforce |
| E3 | `merge` refused without G1 to G4, or with G6 open; allowed when satisfied; a draft refused; a push after ready voids the verdicts and the merge is refused again |
| E4 | `launch` refused for a `PLAN:` with no consensus at its current hash; allowed with one; an edited plan voids it; no `PLAN:` allowed in shadow |
| shim | each argument shape: `--repo`, a URL, a number from the current repo, `--` handling, an unreadable shape is allowed; the real gh runs unchanged when allowed; exit code and stdout of the real gh are passed through |
| author identity | the creation record wins over an edited body marker; a writer from the ledger, the notice or the marker cannot record a verdict or a waiver; a PR created outside the shim has no writer until a proof names one; `gates` prints the writer set |
| fail-open | timeout, crash, missing binary, daemon down, unreadable GitHub, broken ledger, bad config: each allows and records; N in an hour raises one alarm; a spool line is read by the daemon |
| waiver | only a chief-of-staff caller; G6 and G8 refused; a short reason refused; due date and the overdue alarm; a push voids it; `gates` shows `waived`, not `ok` |
| no regression | the shim still signs comments exactly as before; `gates` stays read-only |

## Rollout

1. Land the design (this document) and agree it.
2. `gates-check`, the mode config, the spool and the alarms, with **every repo in shadow**; the shim calls it.
3. Collect data: the plan requires at least 20 PRs and a false-positive rate under 5% per gate before enforcing, decided by cos.
4. `enforce` per repo, with the kill switch tried first; waiver in place before any repo enforces.
5. E4 (`launch`) once the plan review engine records consensus.

## Open questions

1. The kill switch's polarity (above).
2. Is a **global** `plan-gates.off` file wanted, or per-repo only? (A global file is quick to reach in an incident, but is one
   more thing to forget.)
3. The fail-open alarm threshold (N = 3 an hour) and who receives it (cos only, as proposed).
4. Should `merge` also be gated for repos in `shadow` against `MERGE: captain` PRs (held for the owner), where the existing
   hold already applies? As written, G6 only reports there.
