---
name: fleet-planning
description: How to write a plan before work starts, check it, and get it challenged. Use when an ask, a work item or an epic needs a plan, when writing or revising the plan's scenarios, when a brief must link a plan, or when you review a plan.
---

# Planning

A plan says **what** the work must achieve and how anyone will know, never gratuitous **how**. It is written before
work starts, checked by a command, challenged by independent reviewers, and approved. The full rules and every check
`plan check` enforces are in `docs/plan-template.md` (read it when a check fails); this page is the short path.

## When a plan is required

Every worker assignment links one: `PLAN: <link>` in the brief, to its own plan or to a section of an ask's, epic's or
workspace's plan. No plan, no assignment. A tiny work item under an already-reviewed plan section links that section.
Who approves: a work item's plan, its orchestrator (the Chief of Staff if it touches a held-PR area); an ask's, the Chief of
Staff; an epic or workspace plan, the Chief of Staff, then the owner.

## The method, in order

1. **Done-when**: copy the ask's clauses, numbered `(1) ... (2) ...`. They are the authority; never reword them.
2. **Clauses to scenarios**: every clause needs at least one behaviour scenario (`S`); behaviour that must NOT change is a
   `U` scenario. Each scenario has `covers` (`DW-n` or `PC-n`), `text` (Given/when/then), `proof` (`test | live | measure |
   screenshot`), `pass` (a concrete observable: a status, a number, a quoted value), `discriminates` (the specific wrong
   build that would FAIL it), `evaluator` (`ci | tester | reviewer | owner`, able to record that proof), and optionally
   `claim: epic` for what only an integrated head can prove (name the integration branch in the plan).
   **Owner evaluators are rare**: only where the owner's own eyes are the test (an infographic's look, an access grant), proved once with `claim: epic`; never for a routine PR.
3. **Constraints** (`PC-n`): what must hold and why (a shared contract, a security rule, a held path). Nothing else is a
   constraint.
4. **Out of scope**, **areas touched** (and whether any is held-PR), **risks and rollback**, **open questions**, **size and
   tier**, and **the integration-branch decision** (stay on main, or `integration/<epic-slug>`).

## Check it

`fleet-switchboard plan check <owner/repo#N> [--ask <owner/repo#N>] [--json]` prints `PASS` or `FAIL`, one defect per line,
and the plan hash (approval and review bind to the hash; editing anything changes it). Fix every defect before asking
anyone to read it. It only reads GitHub.

## The form

Put it in the repo the work concerns. **A short issue body** (the one-paragraph outcome, the Done-when, the link to the
detail) plus **one detail comment** whose first line is a heading containing `Plan (scenarios)`: the plan text and the
`scenarios:` yaml block. Revise by posting a new such comment (the latest one counts). Keep one plan per ask; sections for
work items.

## Challenge it

When available: a `plan review` command launches independent read-only reviewers and records their verdicts (not built yet:
until then ask the Chief of Staff for a hand-run review). The tier is computed from the ask or epic the plan serves, not
chosen per item. Expect up to three rounds; an objection still standing after round three goes up to the Chief of Staff and
then the owner, never dropped.

## What the reviewers attack

- **Requirements and coverage**: a Done-when clause with no scenario; vague words; a missing unchanged-behaviour scenario.
- **Scenario adversary**: a bad build that passes every scenario (an empty or stubbed implementation); a pass rule nobody
  can decide; an evaluator who cannot record the proof.
- **Risk and security**: no rollback or a half-applied state; a contract change; a held-PR area; who can forge a verdict.
- **Scope and what-not-how**: a step that dictates the design; no out-of-scope; creep; overlap with another plan.
- **Cost and sequencing**: a smaller plan that meets the same Done-when; order and parallelism; an integration branch that
  is not warranted.
- **The owner's view**: would the owner be surprised; does it solve the asked problem or a neighbouring one.

## Good and bad (neutral names)

Good: Done-when (1) "a project with no agents lists none, and after commissioning lists exactly one".

```yaml
scenarios:
  - id: S1
    covers: DW-1
    text: Given a project with no agents, when an owner commissions one, then it is listed
    proof: test
    pass: the list call returns status 200 and exactly 1 agent named "alpha"
    discriminates: a build that creates nothing returns an empty list and fails this
    evaluator: ci
  - id: U1
    covers: PC-1
    text: Existing projects are listed exactly as before this change
    proof: test
    pass: the list output is byte-identical to the stored fixture
    discriminates: a change in ordering or fields shows as a diff against the fixture
    evaluator: ci
```

Bad, each fails `plan check` or a reviewer:
- `pass: works`: a wrong build also "works". Say what is observed.
- `discriminates: a wrong build would fail this`: it names no wrong build, so a stub could satisfy it.
- a plan whose scenarios only cover clause 1 of two: clause 2 has no proof.
- "add a `commission()` method to `ProjectService`": that is how, not what; say the observable outcome.

## After the plan

Link it in every brief (`PLAN: <link>`). The worker proves each scenario with `fleet-switchboard prove S1 --pr <pr>
--evidence <link-or-what-you-ran>`, an evaluator records `verdict`, and `fleet-switchboard gates <pr>` shows what is still
open. A hotfix may be waived by the Chief of Staff, logged; the plan is then due retroactively within a day.
