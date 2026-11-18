# The project's own files (`.spire/`)

The tool is the same everywhere; everything that differs between projects is data in `.spire/`. The installer never touches these files.

```
.spire/
  tool/           the tool, copied here by `spire install` / `spire update` (never edited; replaced whole)
  land.json       sensitive paths, held paths, environment recipes, the step recipes of the area kinds (and, for a small project, its areas)
  areas.json      the project's areas (optional): what each is, the paths and tests it owns, its kind
  box.json        what `spire box` starts (nothing, by default)
  principles.md   the project's law: numbered rules, made once from the tool's default and edited by the project since
  review/         the project's reviewer viewpoints: one `<role>.md` per reviewer it overrides or adds (+ models.json)
.agents/skills/spire-land/   this guide, copied by the same install
```

A file that is not JSON is refused naming it (`spire doctor` reads them the same way); a field nothing reads is ignored, except an unknown TOP-LEVEL key of `land.json` or `areas.json`, which is refused naming it (so `sensitve` cannot silently unseat the Warden). Accepted trade-off: nothing nested is checked; a typo inside `sensitive` or a `held` entry can only land through a change to `.spire/land.json`, which the Warden reads.

## `land.json`

- `areas`: each is `name`, `paths` (globs it owns), optional `exclude`, and `steps` (a command per step; a step with `list` is a test step that runs the area's test files). A change runs **every step of every area it touches**. A path in no area runs the closest enclosing area; a path with no enclosing area runs everything. A project with many areas declares them in `areas.json` instead (below) and writes each kind of area's steps once, as a recipe.
- `recipes`: `{"<kind>": [steps]}`: the steps of every area of that `kind` (areas.json says it). In a recipe `{name}` is the area's name (in any string: `"id": "tests:{name}"`) and `{tests}` is the area's `tests` globs: a word that holds it is repeated once per glob (`":(glob){tests}"` is one git pathspec per glob; `"{tests}"` alone is the globs themselves, which a `bash -c` script reads as `"$@"`). An area's steps are its kind's recipe, then its own `steps` if it has any. A test step marked `"optional": true` whose `list` prints nothing is skipped (a recipe asks every area for its live and PostgreSQL files; most have none; a list that ends in `grep` needs `|| true`); one that is not optional and lists nothing is an error naming the area.
- `sensitive`: `globs`, `nameGlobs` (case-insensitive) and `always`: the paths held and marked sensitive (the Warden reads every change; these do not limit who reads) (credentials, authority, workflows, the lander, the law); a test file is never sensitive unless `always` names it. A reviewer with `calls: ["@sensitive"]` is called when a change touches one.
- `review`: one setting, `models` (where the model registry is; default `.spire/review/models.json`). The council's law is not configurable: it reads `.spire/principles.md` and the reviewers' viewpoints and nothing else.
- `held`: `paths` and a `reason`: a change touching one stops at once with exit 22 for a person to land.
- `environments`: recipes (for example the database a test area needs) that steps name with `environment`. `checks`, `readByChecks`, `sensitive`, `paths`, `warm`: their fields are the ones the examples in `examples/spire-venue/` use.
- `pins`: where a tool's version is DECLARED when that is a file production is built from: `[{"source": "ear/Dockerfile", "from": "python"}]` pins python to the version of that Dockerfile's `FROM python:3.11-slim` line (`from` is the image and the tool). The lander reads it and pins every step to it; one that does not resolve refuses (`tool/versions.mjs` says how). Do not write the version a second time.
- `checks.workerBudgetMB`: the memory one parallel test worker may hold, in MB: a number (every area) or `{"<area>": MB}`. It bounds how many test workers a group starts; each test group of a run prints one line with the count, the measured peak (the most the group's processes held at once, and that divided by the workers) and the MemoryMax in force: set the budget from that peak (the rule and the default are in `tool/workers.mjs`).
- **A step's environment is clean.** Every step starts from nothing, not from the caller's shell: what it keeps is `BASE` and its families in `tool/step-env.mjs` (the one list; the toolchain's own names). A Workroom image's settings, a model relay's address or a port never reach a test. A step that needs more says so itself: `env` (values, as the project's own) and `pass` (names it reads from the host: `"pass": ["CHROME_PATH"]`); one that names an `environment` also gets what that recipe needs (`recipeEnv` there). There is no list of names to withhold: give the step what it needs.
- A step's `cmd` may use `{tool}` (`.spire/tool`), `{root}` (the project root), `{base}`, `{tmp}`, `{bundle}`.

## `areas.json`

The project's areas in one file, read after `land.json` (its `areas` replace `land.json`'s; `exclude` and `harnesses` join the view): `{"exclude": [globs], "harnesses": {...}, "areas": [{"name", "describes", "kind", "paths", "tests"}]}`.

- an area owns its `paths` and its `tests` (globs); its closest-enclosing directory, for a path nothing owns, comes from `paths` only. A test directory is never an enclosing one.
- `exclude`: globs no area but `docs` owns (a document is the docs area's, never a code area's); an area's own `exclude` still holds. The area of kind (or name) `docs` is the one that takes prose.
- `kind`: which recipe of `land.json` is the area's steps. An area with no kind needs its own `steps`.
- `harnesses`: the project's browser-harness policy; the tool does not read it, the project's own scripts do.
- other fields of an area (`describes`, `hubs`) are the project's: nothing in the tool reads them.
- `checks.workerBudgetMB` as a map is read by area name, else by the area's kind (so forty server areas share the `server` budget).

## `principles.md` and `review/`

`principles.md` is numbered rules (`1. **Name.** text`), cited as `P1`. Edit it freely; if you renumber, fix the `enforces:` of the viewpoints. Nothing else holds law: no AGENTS.md section, charter or prompt restates a rule, they point at `spire principles`.

A viewpoint is `.spire/review/<role>.md`: front matter, then a short body (what it looks for, and what is not its job).

```
---
role: warden                 # must be the file's name
calls: ["@all"]              # which paths call it: @all, globs, @prod, @sensitive; [] removes the member
enforces: [10, 11, 12]       # the principle numbers it enforces
reads: ["docs/rubric.md"]    # optional: files given to it whole (a design rubric, a security standard)
---
# Warden
**Can this change be abused...?** ...
```

A file in `review/` whose front matter does not say `role: <its own name>` is not a viewpoint and is not read. `spire principles ROLE` prints a viewpoint whole; the defaults are in `.spire/tool/defaults/review/`.

## `box.json`

All fields are optional; no file (or `{}`) is a checkout and nothing else. A project declares what its box starts (setup steps, services with a start command and a readiness probe, a seed, a capture) in plain commands of its own; the toolchains come from its pins. Every field is in `docs/BOX.md` of the spire-land repository.

## Where the environment comes from

The images carry only what bootstraps: `mise`, `git`, a shell, `curl`, certificates and `gh`. The project's pins say the rest; `spire doctor` shows which are installed and prints the command for each that is not.

## Agent, pod, Workroom

Nothing else is needed: a checkout with `.spire/` and the host's `git`, `bash`, `curl`, `flock` and Node (the pinned one). `spire workroom up` brings the shared environment up (a day's lifetime, listening beyond loopback inside its one container); `spire workroom down` ends it.
