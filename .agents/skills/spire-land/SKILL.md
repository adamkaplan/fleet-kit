---
name: spire-land
description: >-
  Land a change on main with the project's own checks and review, in any project that has a `.spire/` folder:
  `.spire/tool/bin/spire land`, plus check, areas, box, council and doctor. Use whenever you finish a change and
  must deliver it, need to know what a change will run, or a landing stopped (exit code, held, refused).
---

# spire-land: land your change

One command does everything: `.spire/tool/bin/spire` (called `spire` below). It is the same for a Spire specialist,
an agent, a pod and a person. It needs no flags to land a change, and it has no override of any kind.

## Before you build: read the law

Run `spire principles`. It prints the project's **principles** (its numbered rules, `.spire/principles.md`) and the **reviewers' viewpoints** (what each reviewer looks for, which paths call it, which principle numbers it enforces). Build against that text: it is the same text the council judges your change by. Do not read anything else for the law, and never keep a copy of it in a prompt, a brief or a doc: point at `spire principles`. ([The law, the reviewers, who edits what](#the-law-the-reviewers-who-edits-what-how-a-change-is-judged) below.)

## Land a change

1. Start from a fresh `origin/main`. Make the change small and whole. **Commit it** (only commits land; a dirty tree stops at exit 2).
2. On a host or Workroom you have not used yet: `spire doctor`. It checks what the tool needs and prints the fix for each problem.
3. Run `spire land`. It rebases onto `origin/main`, then runs the **council** (the review your paths call for) and every test of the
   **areas** your change touches at the same time (the first red result stops the other), writes the evidence on your commits, re-checks the
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
| 15 | a council member could not answer (no model route, unreadable answer) | land again; if it keeps happening, report it |
| 18 | the council has a finding rated `fix` or `block` to close (printed with its member) | fix it, or `--rebut` it (below), commit, land again |
| 20 | only with `LAND_VIA_PR=1`: branch pushed, PR opened, nothing landed | none |
| 22 | **held for a person**: a path of your change is one the project reserves for a person | stop; say so; a person lands it |

`held` (22) and a refused push are final answers, not obstacles: nothing was started or pushed. Do not look for a way around them.

## Before you land

- `spire areas`: which areas your change touches, which council members its paths call for (by default all of them), whether a path is held. A path in no area runs the **full** suite (never a silent skip).
- `spire check`: the same tests `land` runs, with no review and no push. `spire check --retest` runs only what failed last time.
- `spire land --dry-run`: everything but the final push.
- Every test of each touched area runs, however long it takes. There is no flag that skips a test, a check or the review, and no flag that lands past a red result.

## The review (council)

The council is the FULL set of default reviewers (`spire principles` lists them): every one reads every change, docs and tests included. A project that does not want a member removes it (or narrows it) in its own `.spire/review/<role>.md` (`calls: []` seats nobody); the default stays full. `spire council` runs it alone.
A finding cites a principle **by number** (`P5`). One rated `fix` or `block` stops the landing (exit 18) until it is closed in one of two ways:

- **fix it** and commit: its member re-reviews the new diff (only members with an open finding run again);
- **rebut it**: `spire land --rebut "ROLE=P5: why this is not that"`. The rebuttal cites a principle of the project's by number; the member reads it and closes the finding by no longer raising it, or keeps it open. A rebuttal never closes a finding by itself.

The council remembers a finding closed by an accepted rebuttal: when its member raises it again on the same lines, unchanged, the tool drops it (printed `dropped ... closed in round N`). Changing the quoted lines reopens it.

A `note` never stops anything, and a finding that cites no principle number (or one the project does not have) is only a note. The council downgrades such a finding to a note before the verdict is computed, so it never blocks and no round count touches it. A project with no `.spire/principles.md` is judged against the tool's named default principles (`spire principles` lists them): only a finding citing one of those numbers can stand. The review takes its model route only from the caller's environment: `COUNCIL_MODEL_URL` (the API base: a gateway that holds the key, or the provider) and, when it is not a gateway, `COUNCIL_MODEL_KEY`.

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
| How a change is judged | the reviewers its paths call for each cite a principle **number** per finding; only a finding that **cites a numbered principle of the project's `.spire/principles.md`** can stop a landing; one that cites none (or a number the project does not hold, or free text outside the contract, whatever it says or however it is rated) is recorded as a **note** and never blocks, and no count of rounds changes that; a cited `fix`/`block` stops the landing until fixed or rebutted by number |
| Which paths call which | each viewpoint's own `calls` (`@all`, `@sensitive`, `@prod`, globs): `spire principles` shows them; only a project's own file narrows or removes a member |

The law that judges a change is the one on `main` (the base): a change cannot rewrite the rules or the viewpoints it is judged by; change the law in its own change. The project's own files are described in [references/project.md](references/project.md).

## Lint defaults

Per language, a lint set ships in `.spire/tool/defaults/lint/<language>/` (now: `typescript`); `spire install` copies it into a project that declares the language, as the project's own `.spire/lint/<language>/` (LAYOUT.md has the rules). It runs only when `.spire/land.json` says `"lint": ["typescript"]`; run it yourself with `.spire/lint/typescript/run`.

## Pinned toolchains

The project pins its tool versions (`.node-version`, `mise.toml`, `.tool-versions`, ...). Every command runs with exactly those versions first in `PATH`, and prints one line saying which.
A version the host lacks **refuses** (exit 1) and says how to install it (`mise install node@22.23.4`); the host's default is never used instead. Do not work around it: install the pin.

## The evidence

Each landed commit carries two trailers: `Land-Checks:` (the areas and checks that ran) and `Land-Council:` (who reviewed and their verdicts, or `none needed`).
The remote's evidence gate refuses a push without them. They are written for you; you never write or edit them, and there is no `Override:`.

## Care and repair

- `spire doctor`: this host and project, in plain words, with the fix for each problem (git, mise, the project's pins, its `.spire/` files, the remote, your push access).
- `spire update`: bring `.spire/tool/` and this skill to the current version (shows the plan first, and what the new default principles changed; `--apply` writes). Never edit either folder by hand: it is replaced whole.
- The project's own files (`.spire/land.json`, `box.json`, `principles.md`, `review/`) are described in [references/project.md](references/project.md).

## Never

- Never build without reading `spire principles`, and never copy the law into a prompt, brief, charter or doc: point at the skill.
- Never push to `main` yourself, open a PR to get around the tool, or add a trailer by hand.
- Never edit `.spire/tool/` or this folder; report what is wrong.
