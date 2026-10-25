---
name: fleet-planning
description: How to write a plan before work starts, check it, and get it challenged. Use when an ask, a work item or an epic needs a plan, when writing or revising the plan's scenarios, when a brief must link a plan, or when you review a plan.
---

# Planning

A plan says **what** the work must achieve and how anyone will know, never gratuitous **how**. It is written before
work starts, checked by a command, challenged by independent reviewers, and approved. Every rule and check is in
`docs/plan-template.md` (read it when a check fails).

## When a plan is required

Every worker assignment links one: `PLAN: <link>` in the brief, to its own plan or to a section of an ask's, epic's or
workspace's plan. No plan, no assignment. A tiny work item under an already-reviewed plan section links that section.
Who approves: a work item's plan, its orchestrator (the Chief of Staff if it touches a held-PR area); an ask's, the Chief of
Staff; an epic or workspace plan, the Chief of Staff, then the owner.

## The method, in order

1. **Done-when**: copy the ask's clauses, numbered `(1) ... (2) ...`. They are the authority; never reword them.
   A work item with no Done-when of its own takes the Done-when of its parent ask (its native sub-issue parent): `plan check` and the structure gate (G0a) read it there, and a Done-when the work item carries itself must still match exactly.
   A pull request that proves only some of a plan's scenarios puts `CLAIMS: S3 S4 S5` directly under its `PLAN:` line: the proof gate (G1) counts only those, the others stay claimed by the epic, and a claim naming an undeclared scenario is refused (the other pull requests' claims are listed, not yet enforced: #976 adds the check at the ask's last merge).
2. **Clauses to scenarios**: every clause needs at least one behaviour scenario (`S`); behaviour that must NOT change is a
   `U` scenario. Each scenario has `covers` (`DW-n` or `PC-n`), `text` (Given/when/then), `proof` (`test | live | measure |
   screenshot | review`; `review` quotes text; this check is a net: the plan's reviewer is the guard), `pass` (a concrete observable: a status, a number, a quoted value), `discriminates` (the specific wrong
   build that would FAIL it), `evaluator` (`ci | tester | reviewer | owner`, able to record that proof), and optionally
   `claim: epic` for what only an integrated head can prove (name the integration branch in the plan).
   **Owner evaluators are rare**: only where the owner's own eyes are the test, proved once with `claim: epic`; never for a routine PR.

   **Own measurement:** launch a tester of your lane (`--role tester --repo <the pull request's repo> --reports-to <you>`, headless) to re-measure and record; never record as yourself.
3. **Constraints** (`PC-n`): what must hold and why (a shared contract, a security rule, a held path). Nothing else is a
   constraint.
4. **Out of scope**, **areas touched** (and whether any is held-PR), **risks and rollback**, **open questions**, **size and
   tier**, and **the integration-branch decision** (stay on main, or `integration/<epic-slug>`).
   **Size** is a T-shirt in AGENT time, never human-days: S under 1 hour, M a few hours, L about a day, XL several days, each with what makes it that
   size. Never prefer the smaller option merely because it is less work when a larger one
   simplifies the system. Put `plan-format: 2` and a `size:` line in the plan; `plan check` flags a missing size and days for work under L (a light plan may carry just `size: S`).

## Check it

`fleet-switchboard plan check <owner/repo#N> [--ask <owner/repo#N>] [--json]` prints `PASS` or `FAIL`, one defect per line,
and the plan hash (editing anything changes it). Before you send a draft, run `plan check --file <plan> --done-when <the
issue's Done-when>` (the issue body needs `## Intent` before `Done when:`; format 2 needs a `clauses:` and a `scenarios:` yaml block) and fix every defect.

## The form

Put it in the repo the work concerns. **A short issue body** (the one-paragraph outcome, the Done-when, the link to the
detail) plus **one detail comment** whose first line is a heading containing `Plan (scenarios)`: the plan text and the
`scenarios:` yaml block. Revise by posting a new such comment (the latest counts). One plan per ask; sections for work items.

## Challenge it

When available: a `plan review` command launches independent read-only reviewers and records their verdicts (not built yet: ask the Chief of Staff for a hand-run review). The tier is computed from the ask or epic the plan serves, not
chosen per item. Up to three rounds; an objection standing after that goes to the Chief of Staff, then the owner.

## What the reviewers attack

- **Requirements and coverage**: a Done-when clause with no scenario; vague words; a missing unchanged-behaviour scenario.
- **Scenario adversary**: a bad build that passes every scenario (an empty or stubbed implementation); a pass rule nobody
  can decide; an evaluator who cannot record the proof.
- **Risk and security**: no rollback or a half-applied state; a contract change; a held-PR area; who can forge a verdict.
- **Scope and what-not-how**: a step that dictates the design; no out-of-scope; creep; overlap with another plan.
- **Cost and sequencing**: a smaller plan that meets the same Done-when; order and parallelism; an integration branch that
  is not warranted.
- **The owner's view**: would the owner be surprised; does it solve the asked problem or a neighbouring one.

## Good and bad

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

Bad: `pass: works`, `discriminates: a wrong build would fail this`, or "add a `commission()` method" (how, not what).

## Wordings `plan check` accepts

- every declared `DW`/`PC` is covered by a scenario;
- `discriminates` names a concrete wrong build (a stub that returns an empty list);
- `pass` has an observable number or status (`exit status is 0`);
- the declared clauses joined equal the ask's Done-when;
- check a slice with `--ask <its own work item>`.

## After the plan

Link it in every brief (`PLAN: <link>`). The worker proves each scenario with `fleet-switchboard prove S1 --pr <pr>
--evidence <link-or-what-you-ran>`, an evaluator records `verdict`, and `fleet-switchboard gates <pr>` shows what is still
open. The order does not matter (a `ci` verdict recorded before your `prove` counts once the proof exists): a coder's only extra step is `prove`. A push voids every proof and
verdict. A hotfix may be waived by the Chief of Staff, logged; the plan is then due retroactively within a day.
`CARRIES: #N`: see plan-template.
