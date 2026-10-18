# Plan template (plan-driven development)

A plan says **what** the work must achieve, never gratuitous **how**. It is written by the orchestrator before work
starts, checked mechanically (`fleet-switchboard plan check`, gate G0a), reviewed, and approved. Every worker
assignment carries `PLAN: <link>`. This page is the template and the rules `plan check` enforces. The planning skill
(`fleet-planning`) is the short path to this page; this page stays the full reference.

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
8. **Size and tier**. State the size as a **T-shirt in agent time** calibrated to this fleet: **S** under 1 hour, **M** a few hours, **L** about a day,
   **XL** several days, each with what makes it that size (files, areas, risk), never human-days. A light plan may carry just `size: S`. A
   recommendation never prefers the smaller option because it is less work when a larger one simplifies the system: say what each option
   costs in size and choose on the result. Put the line `plan-format: 2` and a `size:` line in the plan (see the table: only plans that opt in are checked).
9. **The integration-branch decision** (stay on main, or `integration/<epic-slug>`).

Approvers: a work item's plan by its orchestrator (cos if it touches a held-PR area); an ask's by cos; an epic or
workspace plan by cos, then the owner.

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
    evaluator: ci     # ci | tester | reviewer | owner
    claim: pr         # optional: pr (default) = proven per PR; epic = proven at the epic's integrated head
```

Optional keys: `claim`, `slice`, `phase`. Anything else is rejected.

**A scenario the tester can only prove after the merge is not claimed by a slice pull request (#579).** With NO `claim` written, a scenario whose evaluator is the tester and whose proof is a `measure` is read by G1 as `claim: epic`: a slice pull request is not asked to prove it, and G1 lists it as `epic-claimed, proven after the merge at the integration head`. Write `claim: pr` to have a pull request claim it anyway; `claim: epic` stays epic. `prove`, `verdict` and `verdict --all` read it the same way (one function, `claim_of`) and refuse it with the message of an explicit `claim: epic`. Every other scenario with no claim is `pr`, as before. The plan's own text decides: the plan hash is unchanged (the canonical text has always read an unwritten claim as `pr`; only G1's reading changes), and `plan check` still requires the scenario's id, text, pass and discriminates.

**A live or tester scenario says who claims it.** `plan template` prints a ready `claim: epic` line with its why under every stub (commented out in a `test`/`ci` stub, live in a `live` or `tester` one): a pull request cannot prove a check that needs the installed system. (A tester `measure` scenario with no claim is still read as epic by G1, #579; the warning asks you to say it.) `plan check` WARNS (never fails; the hash is unchanged) for a scenario whose proof is `live` or whose evaluator is `tester` and that has no `claim` line.

**Owner evaluators are rare.** Name `owner` as an evaluator only where the owner's own eyes are the test (how an infographic looks, an access grant), and give such a scenario `claim: epic` so it is proven once at the epic's integrated head; a routine pull request never waits on the owner. Give each slice's pull request its own light plan (its per-PR scenarios, `claim: pr`, evaluators `ci`, `tester` or `reviewer`), linked by a `PLAN:` first line in the PR body.

### What `plan check` enforces

| Rule | Defect when |
|---|---|
| `id` | missing, not `S<number>` or `U<number>` with an optional lowercase letter (`S11b`, `U3a`: add a scenario in a later revision without renumbering), or used twice. Shown and hashed in the order number, then suffix |
| `covers` | not exactly one `DW-<n>` or `PC-<n>`; or a `DW-n` beyond the ask's Done-when clauses. **`DW-n` is the ask's clause n; `PC-n` is a plan constraint** (a `PC-<n>: ...` line in the plan's constraints section). If the plan lists its constraints, every `PC-n` must be covered by a scenario and a scenario may only cover a listed one (`uncovered-constraint`, `covers-unknown-constraint`); with no list it is a warning. **`U<n>` scenarios are unchanged-behaviour scenarios**: they say what must still hold, and they do not count towards covering a Done-when clause |
| `text` | missing or under 10 characters |
| `proof`, `evaluator` | not one of the listed values |
| proof and evaluator agree | `test` needs `ci`; `screenshot` needs `reviewer` or `owner`; `live` and `measure` need `tester` or `owner` |
| `pass` | missing, under 15 characters, a vague phrase (`works`, `ok`, `it works`, `passes`, ...), or **no concrete observable**. An observable is a status or exit code (`status 200`, `exit code 0`), a value with a unit or a threshold (`within 5 s`, `exactly 1 agent`, `under 200 ms`), a quoted EXPECTED value that is not itself a vague word (`"alpha"`; `"works"` is not), an identifier compared to a value (`count = 3`), or a strong comparison (`byte-identical to the stored fixture`). Zero, or an exact number, of ANY noun (`sends 0 accepts`, `received 0 responses`, `exactly 3 retries`: the number, up to two words, then a noun); a status class (`a 4xx`, `5xx`); `byte equal`, `byte for byte` and `byte-identical`; a count (including zero: `0 occurrences of X in the tree`), `n of n` with equal numbers (`10 of 10 files contain Y`; `3 of 4 people like it` is not one), identical-to-a-named-thing (`identical per file to the baseline`) and a named identifier compared to a value (`count of Z equals 3`) are observables. Hedges (`maybe`, `somehow`, `or so`, `yes fine`, `seems`) fail whatever else the rule says. A bare digit, `=`, `after` or `before` is not enough: `it works for 1 user` and `result = ok and everything fine` fail |
| `discriminates` | missing; `n/a`/`none`/`tbd`; under 10 characters; a meta phrase that only says a wrong build fails (`a wrong build would fail this`); no concrete wrong build or negative case (a stub, an empty result, a missing step, a number, a quoted value); or about 80 percent or more of its words are the pass rule's (the pass rule reworded). A wrong build is recognised by structure: `a build that <does X> ... <an observable outcome>` (`a build that skips the dedupe step finds two rows for one key`), not by a list of verbs. Sharing 60 to 80 percent of the pass rule's words only warns (`discriminates-overlap`): a wrong build is often the right one with one thing changed. Whether the named wrong build is the right one is for the plan review (G0b) |
| evaluator alias | `adam` (an older plan's spelling) is still accepted as `owner`, with a warning to rename it; it is hashed as written |
| evaluator consistency | the evaluator must be able to record the proof: `test` needs `ci`; `screenshot` needs `reviewer` or `owner`; `live` and `measure` need `tester` or `owner`. (A rule of this checker, written here because plan v3.3 does not state it.) |
| coverage | a Done-when clause `DW-n` that no **S** scenario covers |
| unchanged | no **U** scenario, unless the plan says `unchanged: none because <reason>` with a real reason (at least 20 characters and three meaningful words: not `n/a ok`) |
| Done-when | `--ask` (or `--done-when`) is the **authority** for the clauses. If the plan states its own Done-when it must be the same clauses: fewer or different ones fail (`done-when-differs`). A plan can never weaken coverage |
| `size` | only for a plan that carries the line `plan-format: 2` (or higher), so a plan written before the rule is not flagged: a plan with no `size:` line (`missing-size`); a `size:` that does not start with S, M, L or XL (`bad-size`); a size of S or M given in days or weeks, or any size given in human time (`size-in-days`; human-days and person-days are refused for every size) |
| scenario count | warning (not a failure). With a `slice` key on the scenarios: more than 30 in one slice, or more than 80 in total. With no slices: more than 30 in total |

The Done-when clauses are the numbered parts `(1) ... (2) ...` of the ask's `Done when:` line (`DW-n` is clause n); a
line with no `(1)` is one clause.

### Declaring the clauses

A Done-when written as numbered parts `(1) ... (2) ...`, or as a numbered list (one `1.` per line, for `--done-when`),
is split into clauses. A Done-when written as **one sentence with several comma- or semicolon-separated clauses** cannot be
split reliably, and would count as one clause (then `covers: DW-2` is "unknown"). Declare the clauses in a second fenced
block of the plan:

```yaml
clauses:
  DW-1: plans are recorded on GitHub
  DW-2: alignment is reviewed
```

Numbered from 1, in order. The check **verifies** them: joined in order they must reproduce the ask's Done-when text (case,
spacing, punctuation and `(n)` numbers are ignored, so you may split on the Done-when's own separators), else it fails with
`declared clauses do not match the ask`. The ask stays the authority: when the ask already has numbered parts (or a numbered list) those ARE the clauses and a declared list can only repeat them (same count, same text), else `declared-clauses-mismatch`; the declared list is used only to split a one-sentence Done-when. A plan can never declare fewer, merged or different clauses to weaken coverage. Without a declared list, a one-sentence Done-when with many separators prints a warning telling you to
declare it.

### Help while you write

`fleet-switchboard plan check ... --explain` follows every defect and warning with a `WHY` line (the rule) and a `FIX` line (what to do); the
default output and the hashes are unchanged. `fleet-switchboard plan template <owner/repo#N>` (or `--done-when TEXT`) prints a prefilled plan:
`plan-format: 2`, a `size:` placeholder, the ask's Done-when clauses numbered, and one scenario stub per clause plus an unchanged one. Replace every
`<...>` before checking it; it only reads the ask.

### Output, hash, exit codes

`fleet-switchboard plan check <owner/repo#N | issue URL | --file PATH> [--json]` prints `PASS` or `FAIL`, one `DEFECT
<scenario id>: ...` line each, the source (for a comment, its id and author: the latest `Plan (scenarios)` comment counts
whoever wrote it) and **two hashes**. The **scenario hash** is sha256 of the canonical scenario block plus the clause list
(sorted by id, whitespace collapsed, comments and quoting ignored). The **plan hash** is sha256 of the whole plan text,
whitespace collapsed. **Approval (G0c) and consensus (G0b) bind to the plan hash**: editing constraints, scope or risks
changes it, as editing a scenario does; the scenario hash only says the scenarios are unchanged. The text output groups defects by kind with counts (`vague-pass x17`, then up to five lines of each kind; `--json` has them all). Exit 0 pass, 1 fail, 2 unreadable (no block, a malformed block, a
tab in the indentation, too large, GitHub unreadable). It only reads (GET); it never writes GitHub. `--ask` or
`--done-when` supply the Done-when when the plan issue has none of its own.

### The yaml subset

There is no yaml library and nothing is evaluated. The parser reads a flat list of maps with scalar values: plain,
`'single'` or `"double"` quoted (quote a value that starts with `[ { & * ! | > % @` or a backtick), continued on
deeper-indented lines, or a `|`/`>` block. Comments (`# ...`) are allowed: a `#` starts an inline comment only when a space (or the end of the value) follows it, so `see #348` and `issue #12` stay whole. Refused, naming the block line: tabs, nested
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

## The order of the evidence does not matter

A proof (`prove`) and a verdict count when both exist at the same head and plan hash, in either order; the daemon records the `ci` verdict
itself within minutes of green checks. A push voids every proof and verdict, so after one they are made again.
