# Plan template (plan-driven development)

A plan says **what** the work must achieve, never gratuitous **how**. It is written by the orchestrator before work
starts, checked mechanically (`fleet-switchboard plan check`, gate G0a), reviewed, and approved. Every worker
assignment carries `PLAN: <link>`. This page is the template and the rules `plan check` enforces. The planning skill
(`fleet-planning`) is a later slice; until then use this page.

## What "what, not how" means

Say the outcome and the observable that proves it. Do not say which function to write, which library to use, or how to
structure the code, unless the constraint is real (a shared contract, a held path, a security rule): then it is a
constraint (`PC-n`) with its reason.

| What (good) | How (leave out) |
|---|---|
| a project with no agents lists none, and after commissioning lists exactly one | add a `commission()` method to `ProjectService` |
| the reply arrives within 5 s | use a background queue with a 2 s poll |
| existing projects are listed exactly as before | do not touch `list()` |

## Sections of a plan

Recorded as an issue, or a section of one, **in the repo the work concerns**. Required:

1. **Done-when**: the ask's clauses, copied, numbered `(1) ... (2) ...` as in the ask.
2. **Scenarios**: the yaml block below, including unchanged (U) ones.
3. **Constraints** (`PC-n`): what must hold, and why.
4. **Out of scope**.
5. **Areas touched**, and whether any is a held-PR area.
6. **Risks and rollback**.
7. **Open questions**.
8. **Size and tier**.
9. **The integration-branch decision** (stay on main, or `integration/<epic-slug>`).

Approvers: a work item's plan by its orchestrator (cos if it touches a held-PR area); an ask's by cos; an epic or
workspace plan by cos, then Adam.

## The scenarios block

The plan has exactly one fenced block whose first line is `scenarios:`. Put it in the issue body, or in a comment whose
first line is a heading containing `Plan (scenarios)` (the **latest** such comment wins; use it to revise).

```yaml
scenarios:
  - id: S1            # S = behaviour, U = unchanged behaviour that must still hold; unique
    covers: DW-1      # a Done-when clause (DW-n) or a plan constraint (PC-n)
    text: Given a project with no agents, when an owner commissions one, then it is listed
    proof: test       # test | live | measure | screenshot
    pass: the list call returns status 200 and exactly 1 agent named "alpha"
    discriminates: a build that creates nothing returns an empty list and fails this
    evaluator: ci     # ci | tester | reviewer | adam
    claim: pr         # optional: pr (default) = proven per PR; epic = proven at the epic's integrated head
```

Optional keys: `claim`, `slice`, `phase`. Anything else is rejected.

### What `plan check` enforces

| Rule | Defect when |
|---|---|
| `id` | missing, not `S<n>` or `U<n>`, or used twice |
| `covers` | not `DW-<n>` or `PC-<n>`; or a `DW-n` beyond the ask's Done-when clauses |
| `text` | missing or under 10 characters |
| `proof`, `evaluator` | not one of the listed values |
| proof and evaluator agree | `test` needs `ci`; `screenshot` needs `reviewer` or `adam`; `live` and `measure` need `tester` or `adam` |
| `pass` | missing, under 15 characters, a vague phrase (`works`, `ok`, `it works`, `passes`, ...), or **no concrete observable**. An observable is a status or exit code (`status 200`, `exit code 0`), a value with a unit or a threshold (`within 5 s`, `exactly 1 agent`, `under 200 ms`), a quoted EXPECTED value that is not itself a vague word (`"alpha"`; `"works"` is not), an identifier compared to a value (`count = 3`), or a strong comparison (`byte-identical to the stored fixture`). A bare digit, `=`, `after` or `before` is not enough: `it works for 1 user` and `result = ok and everything fine` fail |
| `discriminates` | missing; `n/a`/`none`/`tbd`; under 10 characters; a meta phrase that only says a wrong build fails (`a wrong build would fail this`); no concrete wrong build or negative case (a stub, an empty result, a missing step, a number, a quoted value); or about 60 percent or more of its words are the pass rule's. Whether the named wrong build is the right one is for the plan review (G0b) |
| evaluator consistency | the evaluator must be able to record the proof: `test` needs `ci`; `screenshot` needs `reviewer` or `adam`; `live` and `measure` need `tester` or `adam`. (A rule of this checker, written here because plan v3.3 does not state it.) |
| coverage | a Done-when clause `DW-n` that no **S** scenario covers |
| unchanged | no **U** scenario, unless the plan says `unchanged: none because <reason>` with a real reason (at least 20 characters and three meaningful words: not `n/a ok`) |
| Done-when | `--ask` (or `--done-when`) is the **authority** for the clauses. If the plan states its own Done-when it must be the same clauses: fewer or different ones fail (`done-when-differs`). A plan can never weaken coverage |
| size | warning (not a failure) over 30 scenarios: split the plan by slice |

The Done-when clauses are the numbered parts `(1) ... (2) ...` of the ask's `Done when:` line (`DW-n` is clause n); a
line with no `(1)` is one clause.

### Output, hash, exit codes

`fleet-switchboard plan check <owner/repo#N | issue URL | --file PATH> [--json]` prints `PASS` or `FAIL`, one `DEFECT
<scenario id>: ...` line each, the source (for a comment, its id and author: the latest `Plan (scenarios)` comment counts
whoever wrote it) and **two hashes**. The **scenario hash** is sha256 of the canonical scenario block plus the clause list
(sorted by id, whitespace collapsed, comments and quoting ignored). The **plan hash** is sha256 of the whole plan text,
whitespace collapsed. **Approval (G0c) and consensus (G0b) bind to the plan hash**: editing constraints, scope or risks
changes it, as editing a scenario does; the scenario hash only says the scenarios are unchanged. Exit 0 pass, 1 fail, 2 unreadable (no block, a malformed block, a
tab in the indentation, too large, GitHub unreadable). It only reads (GET); it never writes GitHub. `--ask` or
`--done-when` supply the Done-when when the plan issue has none of its own.

### The yaml subset

There is no yaml library and nothing is evaluated. The parser reads a flat list of maps with scalar values: plain,
`'single'` or `"double"` quoted (quote a value that starts with `[ { & * ! | > % @` or a backtick), continued on
deeper-indented lines, or a `|`/`>` block. Comments (`# ...`) are allowed. Refused, naming the block line: tabs, nested
lists or maps, flow lists `[a, b]`, anchors and tags, duplicate keys, a second document, over 100 KB or 200 scenarios.

## A good example

See the block above, with a second behaviour scenario and an unchanged one:

```yaml
scenarios:
  - id: S1
    covers: DW-1
    text: Given a project with no agents, when an owner commissions one, then it is listed
    proof: test
    pass: the list call returns status 200 and exactly 1 agent named "alpha"
    discriminates: a build that creates nothing returns an empty list and fails this
    evaluator: ci
  - id: S2
    covers: DW-2
    text: Given a commissioned agent, when the owner sends a chat message, then it answers
    proof: live
    pass: the reply arrives within 5 s and is longer than 0 characters
    discriminates: a stub that returns a canned empty reply fails this
    evaluator: tester
  - id: U1
    covers: PC-1
    text: Existing projects with agents are listed exactly as before this change
    proof: test
    pass: the golden list output is byte-identical to the stored fixture
    discriminates: a change in ordering or fields shows as a diff against the fixture
    evaluator: ci
```

## Three bad examples

**1. A vague pass rule.** `pass: works` fails: a wrong build also "works". Say what is observed: a number, a status, a
quoted value, a comparison.

```yaml
scenarios:
  - id: S1
    covers: DW-1
    text: Given a project with no agents, when an owner commissions one, then it is listed
    proof: test
    pass: works
    discriminates: a build that creates nothing returns an empty list and fails this
    evaluator: ci
```

**2. No discriminating observable.** `discriminates: a wrong build would fail this` (or `n/a`, or the pass rule reworded) fails:
nothing says what the wrong build DOES, so a vacuous test would satisfy the scenario. Name the wrong build: "a stub that
returns an empty list".

```yaml
scenarios:
  - id: S1
    covers: DW-1
    text: Given a project with no agents, when an owner commissions one, then it is listed
    proof: test
    pass: the list call returns status 200 and exactly 1 agent named "alpha"
    discriminates: a wrong build would fail this
    evaluator: ci
```

**3. An uncovered Done-when clause.** With `Done when: (1) ... (2) ...`, a plan whose scenarios only say `covers: DW-1`
fails with `DW-2 is not covered by any S scenario`. Every clause needs a scenario that proves it.
