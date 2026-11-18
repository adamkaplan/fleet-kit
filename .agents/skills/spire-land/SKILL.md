---
name: spire-land
description: >-
  Land a change on main with the project's own checks and review, in any project that has a `.spire/` folder:
  `.spire/tool/bin/spire land`, plus check, areas, box, council and doctor. Use whenever you finish a change and
  must deliver it, need to know what a change will run, or a landing stopped (exit code, held, refused).
---

# spire-land: land your change

One command does everything: `.spire/tool/bin/spire` (called `spire` below). It is the same for a Spire specialist,
an agent, a pod and a person. It needs no flags to land a change, and it has no override of a check or of the review. (One flag, `--override`, is the owner's route for a path the project holds for a person: [below](#land-a-change).)

## Before you build: read the law

Run `spire principles`. It prints the project's **principles** (its numbered rules, `.spire/principles.md`) and the **reviewers' viewpoints** (what each reviewer looks for, which paths call it, which principle numbers it enforces). Build against that text: it is the same text the council judges your change by. Do not read anything else for the law, and never keep a copy of it in a prompt, a brief or a doc: point at `spire principles`. ([The law, the reviewers, who edits what](#the-law-the-reviewers-who-edits-what-how-a-change-is-judged) below.)

## Land a change

1. Start from a fresh `origin/main`. Make the change small and whole. **Commit it** (only commits land; a dirty tree stops at exit 2).
2. On a host or Workroom you have not used yet: `spire doctor`. It checks what the tool needs and prints the fix for each problem.
3. Run `spire land`. It rebases onto `origin/main`, then runs the **council** (the review your paths call for) and the tests of the
   **areas** your change touches (a test step the project marks `select: "reach"` runs only the test files your change reaches) at the same time (the project's cheap `first` checks run before the rest), writes the evidence on your commits, re-checks the
   exact combination and pushes `main` as a plain fast-forward. No PR.
4. It ends with `land: landed <sha>` and a last JSON line `{"result":"landed","exit":0,"sha":...}`.
   **End your message with `landed <sha>`** (that sha). Anything else is not landed: say what stopped it (below).

If it stops, the loop is always the same: read what it printed, fix, **commit**, `spire land` again. A red that is not your change is still yours to fix, in this landing or its own small one: never park it ("Fix what you find" in `spire principles`).

| Exit | Meaning | Do |
|---|---|---|
| 0 | landed | report `landed <sha>` |
| 1 | stopped with a plain message (a push refused for a reason that is not a race, a pin the host lacks) | read it; do not retry blindly |
| 2 | dirty tree, nothing to land, bad flag | commit, or fix the flag |
| 11 | rebase conflict (files named) | `git rebase origin/main`, fix, `git rebase --continue`, land again |
| 13 | an area's test or a check failed (name and output tail printed; nothing landed) | fix, commit, land again (`spire land --retest` re-runs only what failed) |
| 14 | held or frozen: a hold or freeze is in force | only a fix or a revert lands while frozen; wait, or land that |
| 20 | only with `LAND_VIA_PR=1`: branch pushed, PR opened, nothing landed | none |
| 22 | **held for a person**: a path of your change is one the project reserves for a person | stop; say so; the owner lands it with `spire land --override "REASON"` (below) |

`held` (22) and a refused push are final answers, not obstacles: nothing was started or pushed. Do not look for a way around them. The one route past a held path is the **owner's**: `spire land --override "REASON"` (REASON: who owns the path and why it lands now). It bypasses that stop and nothing else: the checks and the council run as for any landing, a red check still refuses (13), and every commit that lands carries `Override: REASON` beside its `Land-Checks` and `Land-Council`. It is for a person who owns the path, or an agent that person has told to; a change that holds nothing says the flag is ignored and writes no `Override:`. There is no hand `git push` to main: the owner lands through `land`.

**Who pushes.** By default the push is made with the host's own git identity. With `FLEET_APP_ID` set (the fleet's GitHub App; the key is `FLEET_APP_PRIVATE_KEY`, both from the environment of the land process, e.g. `doppler run -p fleet -c <config> -- spire land`) the push is made by the App with a short-lived installation token for the one repository (contents:write), never by a person. An app that is on and cannot make a token ("app not configured: ...") stops `land` at once with exit 1: there is no fallback to the host's identity. Only `land` and its push read the key; no check, council member or test inherits it, and it is never printed.

## Before you land

- `spire areas`: which areas your change touches, which council members its paths call for (by default all of them), whether a path is held. A path in no area runs the **full** suite (never a silent skip).
- `spire check`: the same tests `land` runs, with no review and no push. `spire check --retest` runs only what failed last time.
- `spire land --dry-run`: everything but the final push.
- Every test of each touched area runs, however long it takes. There is no flag that skips a test, a check or the review, and no flag that lands past a red result.

## The review (council)

The council is the FULL set of default reviewers (`spire principles` lists them): every one reads every change, docs and tests included. A project that does not want a member removes it (or narrows it) in its own `.spire/review/<role>.md` (`calls: []` seats nobody); the default stays full. `spire council` runs it alone.
A finding cites a principle **by number** (`P5`). **Findings are advice, never a gate.** The captain's ruling of 15 Nov 2026, "council findings are always advice and never gates: the agent has discretion and can choose to follow the advice or record why not." The reason, in the captain's words: "the earlier case of 6 rounds of nitpicking wordsmithing should have just been over-ruled by the SWE." A change that has advice (a finding rated `fix` or `block`) **lands in the same run**: the advice is printed ONCE, with a plain line saying it did not block, and recorded in the `Land-Council` trailer (member, rating, principle number: `warden=advice(fix P1)`). Nothing is returned, nothing is asked. You decide, after the landing:

- **follow it**: in a follow-up commit and landing;
- **decline it**: say ONE line why not, optionally with `spire land --why-not "TEXT"` (written as a `Land-Why-Not` trailer), or as a line of the commit body; no flag, rebuttal or reply to the council is required, and there is no round count and no closing of findings. To see the advice earlier, run `spire council` (it prints the advice and exits 0) or `spire land --dry-run`.

A member that could not answer (no model route, an unreadable or empty answer; the call is tried twice) is recorded as `unable to review (reason)` in the same trailer and said in the output; it never stops the land. A **held** path (22) is the one thing that stops a landing, and it is a rule of paths, not of the council.

A `note` is only printed, and a finding that cites no principle number (or one the project does not have) is only a note. The council downgrades such a finding to a note before the verdict is computed. A project with no `.spire/principles.md` is judged against the tool's named default principles (`spire principles` lists them): only a finding citing one of those numbers can stand. The review takes its model route only from the caller's environment: `COUNCIL_MODEL_URL` (the API base: a gateway that holds the key, or the provider) and, when it is not a gateway, `COUNCIL_MODEL_KEY`.

## The box

`spire box up` is **a checkout and nothing else** by default: no database, no browser, no service, no assumption about any language. A project that needs more declares it in `.spire/box.json` (services with a start command and a readiness probe, setup steps, a capture command: every field is in `docs/BOX.md` of the spire-land repository), and the box runs what is declared and nothing more; a launcher that cannot start fails at once with the reason.
`spire box land` brings it up, lands and tears it down on every exit path; `spire box up` keeps one for an edit-and-look loop (`box run`, `box refresh`, `box reset`, `spire capture`, `box status`, `box down ID`).
You never write a project's recipe into a prompt or a script, and never start a database, browser or app of your own for the checks.

## The law, the reviewers, who edits what, how a change is judged

There is **one** law per project and it is written once: `.spire/principles.md`, numbered rules (`P1`, `P2`, ...). It began as spire-land's generic default (one-way-of-doing-things rules: single source, test what you changed, small slices, no override, delete rather than update, ...) and **the project owns it from then on**: its people add, change and delete rules by landing a change like any other. `spire install` copies the default into a new project once; `spire update` never writes it, it only shows you a diff of what the new default changed, for you to adopt or not.

**Reviewers are viewpoints, not law.** Each is a short definition: what it looks for, which paths call it (`calls`), which principle numbers it enforces (`enforces`), files it reads (`reads`). spire-land ships its default viewpoints in `.spire/tool/defaults/review/` (`spire principles` lists them with their `calls`). A project overrides one by writing `.spire/review/<role>.md` (it replaces the default whole: `calls: []` removes the member, `calls: ["src/**"]` narrows it) or adds a reviewer of its own the same way. A reviewer's file never restates a rule: it names numbers.

| | |
|---|---|
| The law | `.spire/principles.md`: numbered rules, written once, **the project's** to edit |
| A viewpoint | `.spire/review/<role>.md` over the tool's default: what it looks for, `calls`, `enforces`, `reads` |
| Who edits what | the project edits both (by landed changes); nobody edits `.spire/tool/` or this skill; an update never writes the project's files |
| Who reads what | builders (people, specialists, agents) run `spire principles` first; the council reads exactly the principles and the viewpoints, at the base commit: nothing else |
| How a change is judged | the reviewers its paths call for each cite a principle **number** per finding; only a finding that **cites a numbered principle of the project's `.spire/principles.md`** is given as advice; one that cites none (or a number the project does not hold, or free text outside the contract, whatever it says or however it is rated) is recorded as a **note**; advice never gates a landing: the agent follows it or says in one line why not |
| Which paths call which | each viewpoint's own `calls` (`@all`, `@sensitive`, `@prod`, globs): `spire principles` shows them; only a project's own file narrows or removes a member |

The law that judges a change is the one on `main` (the base): a change cannot rewrite the rules or the viewpoints it is judged by; change the law in its own change. The project's own files are described in [references/project.md](references/project.md).

## Lint defaults

Per language, a lint set ships in `.spire/tool/defaults/lint/<language>/` (now: `typescript`); `spire install` copies it into a project that declares the language, as the project's own `.spire/lint/<language>/` (LAYOUT.md has the rules). It runs only when `.spire/land.json` says `"lint": ["typescript"]`; run it yourself with `.spire/lint/typescript/run`.

## Pinned toolchains

The project pins its tool versions (`.node-version`, `mise.toml`, `.tool-versions`, `pins` in `land.json`: see `references/project.md`). Every command runs with exactly those versions first in `PATH`, and prints one line saying which.
A version the host lacks **refuses** (exit 1) and says how to install it (`mise install node@22.23.4`); the host's default is never used instead. Do not work around it: install the pin.

## The evidence

Each landed commit carries two trailers: `Land-Checks:` (the areas and checks that ran) and `Land-Council:` (who reviewed and their verdicts, or `none needed`).
The remote's evidence gate refuses a push without them. They are written for you; you never write or edit them. An `Override:` trailer is written only by `land --override` (an owner's landing of a held path); one typed by hand is stripped.
A landing's own record names the tree that landed: `push` writes the gate's line on the final tree, and an earlier commit of a push says `part-of=<the tip's tree>`. `spire attest [RANGE]` audits it (default: the last 300 commits of `origin/main`): for each commit the trailers against its real tree, the push group and any `Override:`; it prints the attested share and the commits that are not attested (`--all` lists every one, `--json` the numbers). Every landing prints its first three lines.

## Care and repair

- `spire doctor`: this host and project, in plain words, with the fix for each problem (git, mise, the project's pins, its `.spire/` files, the remote, your push access).
- `spire update`: bring `.spire/tool/` and this skill to the current version (shows the plan first, and what the new default principles changed; `--apply` writes). Never edit either folder by hand: it is replaced whole.
- The project's own files (`.spire/land.json`, `box.json`, `principles.md`, `review/`) are described in [references/project.md](references/project.md).

## Never

- Never build without reading `spire principles`, and never copy the law into a prompt, brief, charter or doc: point at the skill.
- Never push to `main` yourself, open a PR to get around the tool, or add a trailer by hand.
- Never edit `.spire/tool/` or this folder; report what is wrong.
