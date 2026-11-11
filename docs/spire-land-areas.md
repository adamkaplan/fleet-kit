# Landing areas (`.spire/land.json`)

Why: continuous integration for this repository takes about 50 minutes for any change, because every change runs the whole matrix (the switchboard suite alone is 8 shards of about 450 s each, one after another about an hour). `.spire/land.json` (project config of the landing tool, version 1) says which suites a change touches, so a landing runs only those. Only `.spire/land.json` and this page are added here; the tool itself (`.spire/tool`) comes from another step.

A change runs every step of every area it touches (a path that matches no area falls to `all`). Every step is runnable from the repository root with no secret. The `**/*.md` files are excluded from every area except `docs`, so a change to a README under `cloud/` runs the doc checks only.

## Areas, steps and measured times

Measured once each in the worker pod (8 CPUs, a shared machine: slower than a CI runner; the CI step times are lower). One area at a time, serially, wall seconds from `date`.

| Area | Paths (outline) | Steps | Measured |
| --- | --- | --- | --- |
| switchboard | `bin/fleet-switchboard`, `bin/switchboard/**`, `bin/*.py` (not cloud, doctor or heartbeat modules), `shims/**`, `tests/**`, `tools/**` | public-names; `tools/guard-set --run`; `heavy --changed` (the suites that read the changed paths); `bin/test-gh-hold` | 0 s; 468 s; 5-1200 s (see below); 23 s |
| cloud | `cloud/**`, `bin/cloud_*.py`, `bin/cloud-*`, `bin/test-cloud-*`, decisions-pull, app-mint | public-names; neutral names; `bin/test-cloud-worker`; `cloud/tests/test-cloud` (docker cases skipped); `bin/test-cloud-launcher`; `heavy --changed` | 0 s; 4 s; 139 s*; 364 s*; 329 s*; the rest derived |
| doctor-heartbeat | `bin/fleet-doctor*`, `bin/fleet-heartbeat`, `heartbeat-ack`, `fleet_health.py`, `health_records.py`, their tests | public-names; heartbeat; health-records; doctor; doctor-config; doctor-heartbeat | 0; 2; 3; 70; 0; 1 s (76 s) |
| plugins | `plugins/**` | public-names; node tests of fleet-hooks and fleet-decisions-tui | under 1 s |
| docs | `docs/**`, `skills/**`, `agents/**`, `*.md`, `**/*.md` | public-names; the doc-consistency classes (architecture document and units, hygiene, worker rules in skills, skills sync, commissioning docs, review laws, agent definitions, architecture safety net) in one `bin/test-switchboard` call; `bin/test-path-suites -k TestSkillsAndDocs`; cloud docs wording | 154 s; under 1 s; under 1 s |
| all | any other path (`.github/**`, other `bin/` tools, `scenarios/**`, `templates/**`, ...) | public-names; guard set; scenario validate and coverage; workflow-split | 468 s; 2 s; 1 s; 2 s |

`*` Red in this pod for reasons of the pod, not of the code: the checkout lies under a group-writable parent (`/work`), which the launcher-link and worker-activity cases refuse; no `kubectl`, so the skipped-case list of `cloud/tests/test-cloud` differs; and four end-hook cases are red on this pod's git settings. The times are the wall time of the run, red or not. They are expected green on a CI runner (not measured there). `bin/test-app-mint` was also red in this pod and is run only through `heavy --changed`.

### Two landings

- A typical landing (one file in `bin/switchboard` and its test): switchboard area: 0 + 468 + 17 (measured: the two suites the change reads, `test_land_repos.py` and `TestLandText`) + 23 = about 510 s, 8.5 minutes, against about 50 minutes of CI.
- A docs-only landing (one file in `docs/`): docs area: about 155 s (154 s of doc-consistency classes, the rest under a second). For a file with a hub of readers the derived `heavy --changed` is not in this area on purpose.

### What was found about `heavy --changed`

It derives the suites that read the changed paths (`bin/path_suites.py`; `heavy --changed --list` prints the choice and runs nothing). For a typical code change it chose 2 suites and ran 17 s. For one line added to `docs/switchboard.md` (read by about 70 suites) it ran 1194 s and reported two failures: `tests/test_installed_guard.py` run directly cannot import `support` (the CI runs it inside `bin/test-switchboard`), and `bin/test-switchboard` with its class selection failed on the merged temporary tree but passed (1650 tests, 961 s) when the same command was run in the checkout. The cause of the second was not found. So the derived step stays in the code areas, where the list is short, and the `docs` area uses a fixed list. The step assumes `{base}` names a commit-ish that `heavy --base` accepts; that is not checked here (the tool is not vendored yet).

Not used: the `list` field of a test step (its contract is not visible from this repository); every step is a plain `cmd`.

## Where the full suite runs

Today the nightly cron of `.github/workflows/test.yml` runs the whole matrix for `main` and, when `origin/integration` exists, for `integration` (the `plan` job builds the refs list: `refs = ["main"] + ["integration"]...`). When the default branch is the only branch: the `integration` entry of the cron refs becomes unneeded (the guard `rev-parse origin/integration` already skips it when the branch is gone, so nothing breaks), the `pull_request` and `push` triggers should keep a full run on `main` (the push to `main`), and the nightly of `main` stays the full run that lands never do. The workflow is not edited here (a later step).
