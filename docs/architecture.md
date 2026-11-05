# Architecture of the switchboard
This document is the single source for the units of the switchboard: what each one is for, and what it may depend on. `tests/switchboard/test_architecture.py` reads the tables below and checks them against the code; `bin/test-switchboard` runs that test. Part of ask #1092 (split the switchboard into units of a size a person can read whole, each with one job).

A **unit** is a file: the entry `bin/fleet-switchboard`, a part under `bin/switchboard/` (assembled by the entry into one namespace, listed in `bin/fleet-switchboard.parts`), or a flat module `bin/<name>.py` (loaded by the entry with `load_module("<name>")` or imported by another module). The unit column holds the path relative to `bin/`.

## Rules

1. Every unit has a layer (the table below). A unit may depend on its own layer or a lower one, and on the units named in its **Also may use** column. Nothing else.
2. A dependency is derived, never declared. For a part or the entry it is a name the unit uses that another unit defines at its top level (the parts share one namespace); a name the entry imports and a part relies on is counted apart (kind `entry-import`). For a flat module it is an `import` of another `bin/*.py` module, or `load_module` of one from the daemon.
3. Two units of one layer may not depend on each other in a cycle.
4. No unit file is over 1,500 lines, and no test file (`bin/test-*` and `tests/switchboard/**`) over 2,500 lines.
5. A name two units define and a third reads is an ambiguous owner: which one is meant is not decided by the code, so it is a violation (kind `ambiguous`; the row's To is the owners joined by `+`).
6. Every file of `bin/*.py`, `bin/switchboard/*.py` and the entry is a row of the unit table, and every row is a file.
7. Today's code breaks some of these rules: the daemon grew as one namespace. Each break is a row of **Known violations**, with its count. A violation that is not a row fails the test; a row with no violation left fails the test (delete it); a dependency count that is not exactly its row's number fails the test (more is a new break; fewer means the row is written lower, and the message names the number). The two `size` rows (the entry and `bin/test-switchboard`) are ceilings with headroom: a file within its ceiling passes, a file over it fails with the number to write and what to do, and a row more than 3,000 lines above its file fails with the lower number to write. The list only shrinks. It must end empty. A move that lowers a count lowers its row in the same pull request.

## Where a new test goes

A new file `tests/switchboard/test_<unit>.py` is preferred, named for the unit it is about, and it is picked up by `bin/test-switchboard` like `test_architecture.py`. Every `tests/switchboard/test_*.py` must be imported and have a class bound at module level by `bin/test-switchboard`, or be listed with a reason of 20 or more characters in `tests/switchboard/test_wired_tests.py`. Appending to `bin/test-switchboard` is allowed until the test split empties it, within the size row's headroom; when a change would go past the ceiling, the test names where the new tests go (a file under `tests/switchboard/`) or which row to lower.

## Layers

The **Holds** column is descriptive: only the layer numbers are enforced.

| Layer | Name | Holds |
|---|---|---|
| 0 | base | pure rules and helpers that read nothing of the outside world |
| 1 | clients | the one client for each outside system, nothing else |
| 2 | facts | read the world into facts and records |
| 3 | engine | derive, decide, deliver |
| 4 | passes | one daemon pass each, built on the engine and the facts |
| 5 | domains | one command family each, with its parser registrar |
| 6 | entry | `bin/fleet-switchboard`: parser, dispatch and, until the move is done, what has not moved |

## Units

| Unit | Layer | Job | Also may use |
|---|---|---|---|
(The unit rows are not here: each unit is one file `docs/architecture/units/<unit>.row`, so two changes that add units never edit the same line; see `docs/architecture/units/README.md`. The test reads them as this table's rows.)

## The clear's closure follows the parts

The delegated clear refuses a pull request that changes a definition of "the clear's closure" (`bin/delegated_clear.py`, `closure`). The closure is derived over the assembled daemon: the entry plus every part of `bin/fleet-switchboard.parts`, as one namespace. `tools/derive-closure --write` lists in `docs/review-laws/impact-matrix.json` each part that holds a closure definition OR a module-level binding or import the closure reads (`closure_files`), with the definitions as `module_roots[part]` (empty for a part that holds only a binding: listing the part is what refuses an edit of it); `--check` fails in both directions (a name or part missing, a name or part extra) and names the part and the name. A new part is in by derivation, not by a hand list. The manifest is read as data and fails closed (exit 1, the entry named, nothing written): an entry that is not a file, is not `switchboard/<name>.py` (`..`, a directory, an absolute path), is listed twice, a part file the manifest does not list (it is never assembled), and a closure definition defined in two files. `--write` replaces the matrix through a temp file and a rename, and never writes after a failed derivation. `tests/switchboard/test_closure.py` runs the check and checks the property, never a number: `--check` passes (the matrix equals what the code derives), the entry's closure and every binding it reads lie inside the assembled closure, and the entry's own `closure_roots` are as they were (the owner, 4 November 2026: tests never pin an exact count). After moving a definition to another part, run `tools/derive-closure --write` in the same pull request. For a listed part, any top-level statement edit is refused, as for every module the clear loads.

## Known violations

Kinds (the number is exact, except `size`, a ceiling with headroom: see rule 7): `namespace` (a part uses names the entry defines: one namespace), `entry-import` (a part relies on an import the entry made), `size` (a file over its limit: the ceiling is in lines), `ambiguous` (a name defined by two units and read by a third), `import` (a unit reaches a higher layer by an import or `load_module`), `cycle` (units of one layer that depend on each other).

| From | To | Kind | Count | Why it is allowed today |
|---|---|---|---|---|
| `bin/test-switchboard` | `-` | size | 80,500 | the one test file; whole classes have moved to `tests/switchboard/test_cases_NN.py` (T1, T1b), and the lanes still add tests until the next split, so the row is the real count plus about 3,000 |
| `switchboard/decisions_a.py switchboard/docker_reaper.py switchboard/handoff.py switchboard/job_runs.py switchboard/plan_notice.py switchboard/sweep.py` | `-` | cycle | 6 | moved from the entry unchanged: each part reads the other, one cycle to untangle |
| `switchboard/alert_labels.py` | `switchboard/docker_reaper.py` | namespace | 1 | one namespace: the part reads a name another part defines; its rewiring gives it an import |
| `switchboard/batch_decision.py` | `fleet-switchboard` | namespace | 8 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/batch_decision.py` | `switchboard/decisions_a.py` | namespace | 1 | one namespace: the part reads a name another part defines; its rewiring gives it an import |
| `switchboard/batch_decision.py` | `switchboard/handoff.py` | namespace | 4 | one namespace: the part reads a name another part defines; its rewiring gives it an import |
| `switchboard/batch_decision.py switchboard/decision.py` | `-` | cycle | 2 | moved from the entry unchanged: each part reads the other, one cycle to untangle |
| `switchboard/config_reload.py` | `fleet-switchboard` | entry-import | 2 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/config_reload.py` | `fleet-switchboard` | namespace | 3 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/daemon_restart.py` | `fleet-switchboard` | entry-import | 5 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/daemon_restart.py` | `fleet-switchboard` | namespace | 10 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/disk_prune.py` | `fleet-switchboard` | entry-import | 6 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/disk_prune.py` | `fleet-switchboard` | namespace | 8 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/docker_reaper.py` | `fleet-switchboard` | entry-import | 10 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/gate_spool.py` | `fleet-switchboard` | entry-import | 3 | the part relies on standard-library modules the entry imported (`json`, `os`, `Path`); it gets its own import lines |
| `switchboard/docker_reaper.py` | `fleet-switchboard` | namespace | 14 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/gate_spool.py` | `fleet-switchboard` | namespace | 1 | one namespace: the part reads the entry's `_plain` |
| `switchboard/facts.py` | `switchboard/docker_reaper.py` | namespace | 2 | one namespace: the part reads a name another part defines; its rewiring gives it an import |
| `switchboard/heavy_budget.py` | `cloud_worker.py` | import | 1 | a name the part reads from another unit without a declared edge |
| `switchboard/heavy_budget.py` | `fleet-switchboard` | entry-import | 9 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/heavy_budget.py` | `fleet-switchboard` | namespace | 12 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/heavy_budget.py` | `switchboard/sweep.py` | namespace | 2 | one namespace: the part reads a name another part defines; its rewiring gives it an import |
| `switchboard/labels_ensure.py` | `fleet-switchboard` | entry-import | 4 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/labels_ensure.py` | `fleet-switchboard` | namespace | 19 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/observing.py` | `fleet-switchboard` | entry-import | 3 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/observing.py` | `fleet-switchboard` | namespace | 2 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/orders_cos.py` | `fleet-switchboard` | entry-import | 1 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/orders_cos.py` | `fleet-switchboard` | namespace | 6 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/plan_notice.py` | `fleet-switchboard` | entry-import | 2 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/plan_notice.py` | `fleet-switchboard` | namespace | 7 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/reminders.py` | `switchboard/docker_reaper.py` | namespace | 1 | one namespace: the part reads a name another part defines; its rewiring gives it an import |
| `switchboard/sweep.py` | `fleet-switchboard` | entry-import | 6 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/sweep.py` | `fleet-switchboard` | namespace | 31 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/triage.py` | `switchboard/plan_notice.py` | namespace | 1 | one namespace: the part reads a name another part defines; its rewiring gives it an import |
| `switchboard/triage.py` | `switchboard/sweep.py` | namespace | 6 | one namespace: the part reads a name another part defines; its rewiring gives it an import |
| `switchboard/workers_report.py` | `fleet-switchboard` | namespace | 6 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/workers_report.py` | `switchboard/docker_reaper.py` | namespace | 1 | one namespace: the part reads a name another part defines; its rewiring gives it an import |
| `switchboard/workers_report.py` | `switchboard/handoff.py` | namespace | 3 | one namespace: the part reads a name another part defines; its rewiring gives it an import |
| `switchboard/alert_labels.py switchboard/triage.py` | `-` | cycle | 2 | moved together from the entry: each reads the other, one cycle to untangle |
| `switchboard/answer_delivery.py switchboard/decisions_b.py` | `-` | cycle | 2 | moved together from the entry: each reads the other, one cycle to untangle |
| `impact_hold.py` | `-` | size | 2,000 | the brief-bound clear (#1215) took it past the limit; to be split (ceiling = the test's own formula: 1510 lines, plus 500, rounded up to a multiple of 500) |
| `delegated_clear.py` | `-` | size | 1,900 | the registrar lint lives with the clear it protects; a follow-up slice moves it to its own part and lowers the ceiling |
| `switchboard/reminders.py` | `-` | size | 2,000 | the due-reminder ceiling, the `reminder.waiting` audit line and the late count (#1728) took it past the limit; the waiting and ceiling code moves to its own part in a follow-up and the row is then deleted |
| `cloud_worker.py` | `-` | size | 1,608 | #1619: the module loader and three call sites of bin/cloud_launch_again.py (+4 over integration's 1604); the launch-again code itself lives there; #2135 (the inspect command, +48) and #1572 (the registry note, +26) took it from 1490 to 1564 (limit 1500); a ceiling = largest measured 1564 plus a small margin; #2221 moved the inspect section to bin/cloud_inspect.py |
| `switchboard/agents_verify.py` | `fleet-switchboard` | entry-import | 6 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/agents_verify.py` | `fleet-switchboard` | namespace | 7 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/alert_labels.py` | `fleet-switchboard` | entry-import | 5 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/alert_labels.py` | `fleet-switchboard` | namespace | 11 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/alert_labels.py` | `launch_testers.py` | import | 1 | a name the part reads from another unit without a declared edge |
| `switchboard/answer_delivery.py` | `fleet-switchboard` | entry-import | 5 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/answer_delivery.py` | `fleet-switchboard` | namespace | 26 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/audit.py` | `fleet-switchboard` | entry-import | 12 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/audit.py` | `fleet-switchboard` | namespace | 2 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports; one of them is `refuse_real_location`, the test-run guard of the audit path, which stays in the entry beside the state and config guards it shares its locations with |
| `switchboard/bootstrap.py` | `fleet-switchboard` | entry-import | 4 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/bootstrap.py` | `fleet-switchboard` | namespace | 14 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/caches.py` | `fleet-switchboard` | entry-import | 3 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/caches.py` | `fleet-switchboard` | namespace | 4 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/installer.py` | `fleet-switchboard` | entry-import | 10 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/installer.py` | `fleet-switchboard` | namespace | 57 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/profile_pieces.py` | `fleet-switchboard` | entry-import | 8 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/profile_pieces.py` | `fleet-switchboard` | namespace | 13 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/decision.py` | `fleet-switchboard` | entry-import | 1 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/decision.py` | `fleet-switchboard` | namespace | 4 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/decision.py` | `switchboard/handoff.py` | namespace | 1 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/engine.py` | `fleet-switchboard` | entry-import | 5 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/engine.py` | `fleet-switchboard` | namespace | 1 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/facts.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/facts.py` | `fleet-switchboard` | namespace | 5 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/handoff.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/launch_model.py` | `fleet-switchboard` | entry-import | 1 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/launch_model.py` | `fleet-switchboard` | namespace | 1 | one namespace: the part reads the entry's `LaunchError` and `_one_line` |
| `switchboard/bot_reviews.py` | `fleet-switchboard` | entry-import | 1 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/scratch_dir.py` | `fleet-switchboard` | entry-import | 5 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/bot_reviews.py` | `fleet-switchboard` | namespace | 4 | one namespace: the part reads the entry's `_is_bot`, `_login`, `_id` and `GitHubItem` |
| `switchboard/approval_gate.py` | `fleet-switchboard` | entry-import | 4 | the part relies on standard-library modules the entry imported; it gets its own import lines |
| `switchboard/approval_gate.py` | `fleet-switchboard` | namespace | 7 | one namespace: the part reads seven names the entry defines (the merge gate's, the ledger context's and the audit's helpers) |
| `switchboard/scratch_dir.py` | `fleet-switchboard` | namespace | 10 | one namespace: the part reads the entry's names (`build_engine`, `identify_caller`, `agent_scratch_dir`, `_write_small`); its rewiring gives it imports |
| `switchboard/secret_spans.py` | `fleet-switchboard` | namespace | 2 | one namespace: the finder reads the entry's `_SECRET_PATTERNS` and `redact_command_text` (it must see the very patterns the net applies) |
| `switchboard/handoff.py` | `fleet-switchboard` | namespace | 40 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/jev.py` | `fleet-switchboard` | entry-import | 6 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/jev.py` | `fleet-switchboard` | namespace | 5 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/reminders.py` | `switchboard/handoff.py` | namespace | 3 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/triage.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/triage.py` | `fleet-switchboard` | namespace | 27 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/triage.py` | `switchboard/decisions_a.py` | namespace | 3 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/cockpit.py` | `fleet-switchboard` | entry-import | 8 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/cockpit.py` | `fleet-switchboard` | namespace | 7 | one namespace: the part reads the entry's names (the decision file reader, the state writer, the registry); its rewiring gives it imports |
| `switchboard/claims_note.py` | `fleet-switchboard` | entry-import | 3 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/claims_note.py` | `fleet-switchboard` | namespace | 13 | one namespace: the part reads the entry's names (the pull request records, the plan link and the plan facts) |
| `switchboard/claims_note.py` | `plan_check.py` | import | 1 | a name the part reads from another unit without a declared edge |
| `switchboard/claims_note.py` | `switchboard/plan_notice.py` | namespace | 1 | one namespace: the part reads a name another part defines (`plan_wake_state`); its rewiring gives it an import |
| `switchboard/claims_note.py` | `switchboard/plan_input.py` | namespace | 2 | one namespace: the part reads `parse_plan_source` and `governing_done_when` |
| `switchboard/v2_transport.py` | `fleet-switchboard` | entry-import | 15 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/v2_transport.py` | `fleet-switchboard` | namespace | 2 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `fleet-switchboard` | `-` | size | 24,500 | the entry still holds every section not yet moved to a part; each move slice lowers the ceiling |
| `switchboard/facts.py` | `switchboard/worktree_lifecycle.py` | namespace | 1 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/faults.py` | `fleet-switchboard` | entry-import | 1 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/job_runs.py` | `fleet-switchboard` | entry-import | 11 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/job_runs.py` | `fleet-switchboard` | namespace | 9 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/judge_rule.py` | `fleet-switchboard` | entry-import | 1 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/review_laws.py` | `fleet-switchboard` | entry-import | 3 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/shadow_classifier.py` | `fleet-switchboard` | namespace | 5 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/review_laws.py` | `fleet-switchboard` | namespace | 10 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/standing_authority.py` | `fleet-switchboard` | entry-import | 1 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/verification.py` | `fleet-switchboard` | entry-import | 2 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/work_moves.py` | `fleet-switchboard` | entry-import | 4 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/work_moves.py` | `fleet-switchboard` | namespace | 17 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/worktree_lifecycle.py` | `fleet-switchboard` | entry-import | 3 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/worktree_lifecycle.py` | `fleet-switchboard` | namespace | 13 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/decisions_a.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/decisions_b.py` | `fleet-switchboard` | entry-import | 9 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/import_v1.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/judge.py` | `fleet-switchboard` | entry-import | 9 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/judge_request.py` | `fleet-switchboard` | entry-import | 8 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/plan_review.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/plan_input.py` | `fleet-switchboard` | entry-import | 3 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/review_laws_install.py` | `fleet-switchboard` | entry-import | 3 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/pr_explain.py` | `fleet-switchboard` | entry-import | 3 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/sent_view.py` | `fleet-switchboard` | entry-import | 6 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/branch_ff.py` | `fleet-switchboard` | entry-import | 4 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/reminders.py` | `fleet-switchboard` | entry-import | 12 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/decisions_a.py` | `fleet-switchboard` | namespace | 29 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/decisions_b.py` | `fleet-switchboard` | namespace | 24 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/import_v1.py` | `fleet-switchboard` | namespace | 14 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/judge.py` | `fleet-switchboard` | namespace | 12 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/judge_request.py` | `fleet-switchboard` | namespace | 15 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/plan_review.py` | `fleet-switchboard` | namespace | 23 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/decisions_a.py` | `fleet-switchboard` | namespace | 29 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/decisions_b.py` | `fleet-switchboard` | namespace | 24 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/import_v1.py` | `fleet-switchboard` | namespace | 14 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/judge.py` | `fleet-switchboard` | namespace | 11 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/judge_request.py` | `fleet-switchboard` | namespace | 15 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/plan_review.py` | `fleet-switchboard` | namespace | 23 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/plan_input.py` | `fleet-switchboard` | namespace | 14 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/review_laws_install.py` | `fleet-switchboard` | namespace | 4 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/pr_explain.py` | `fleet-switchboard` | namespace | 10 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/sent_view.py` | `fleet-switchboard` | namespace | 5 | one namespace: the part reads the entry's names (`build_engine`, `identify_caller`, `SentLedger`, `READ_LIMIT`, `_created`); its rewiring gives it imports |
| `switchboard/branch_ff.py` | `fleet-switchboard` | namespace | 11 | one namespace: the part reads the entry's names (`build_engine`, `gates_caller_kind`, `_gh_write`, `_write_small`, `parse_pr_ref`, `load_module`, `_one_line`, `IntentError`, `NoticeError`, `PROG`, `EXIT_USAGE`); its rewiring gives it imports |
| `switchboard/reminders.py` | `fleet-switchboard` | namespace | 19 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports; two reads of untrusted JSON now go through `load_module("record_json")`, one name of the entry more (#1314); the delivery-time judgement of late and stale alerts reads one name of the entry more (#1402) |
| `switchboard/stage_event.py` | `fleet-switchboard` | entry-import | 4 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/stage_event.py` | `fleet-switchboard` | namespace | 4 | one namespace: the part reads the entry's names (`load_module`, the config checks, `datetime`); it gets its own imports |

## Dynamic imports not followed

This is a closed, conservative rule. It over-counts by design and fails closed: a use the test did not foresee is a site, not silence.

**What is a site.** Any `import` or `from ... import` of a module that can load code by name: `importlib` (and `importlib.util`, `importlib.machinery`), `pkgutil`, `runpy`, `pydoc`, `imp`, `zipimport`, `builtins`; any use of a name bound to one of them (the module object itself, however it is reached: a call, an attribute, `getattr`, a subscript, `vars(...)`, `__dict__`, `operator.attrgetter`, an argument, an alias, `m = importlib`); any use of `sys` other than its plain attributes `argv`, `executable`, `exit`, `path`, `platform`, `stderr`, `stdin`, `stdout` and `version_info` (so `sys.modules` however it is reached, `sys.__dict__`, `vars(sys)`, `getattr(sys, ...)`, `m = sys`, `from sys import modules` or `*`, `from X import sys`, `os.sys`, and the module object returned by `__import__("sys")` all count); `builtins` and `__builtins__`; the builtins `__import__`, `exec`, `eval` and `compile`, and the daemon's own `load_module` (also as `obj.load_module(...)` on any object), as a call with a non-constant, relative (leading dot) or package argument, as a reference (a default, a class attribute, a dict value, a conditional, a tuple, a walrus, an argument of `map` or `functools.partial`), or named by a string anywhere (a key, an argument, `.get("__import__")`, `itemgetter`, `getattr(x, "exec")`, `g["__builtins__"]`, a name in a list: any string that is exactly the name of a loading builtin, of `sys` or of one of these modules counts, so `globals()["sys"]` and `getattr(os, "sys")` are sites), or reached as an attribute of any object (`obj.load_module(...)`, `g["__builtins__"].__import__(...)`, `.eval(...)`; `re.compile` is not one); and a relative import of level two or more.

**Identifiers.** Every occurrence of an identifier of the import machinery (the modules above, `import_module`, `find_spec`, `spec_from_file_location`, `spec_from_loader`, `module_from_spec`, `exec_module`, `run_path`, `run_module`, `SourceFileLoader`, `SourcelessFileLoader`, `ExtensionFileLoader`, `zipimporter`, `resolve_name`, `load_source`, `load_compiled`) as a name, an attribute of any object or an import alias is a site, wherever it was imported from (`from helpers import importlib`, `from .compat import import_module`, `pkg.runpy.run_path`, `spec.loader.exec_module`). A local variable that happens to be called `imp` counts: over-counting by design.

**What is followed.** Only a direct call with a constant module name, spelled as such: `__import__("x")`, `importlib.import_module("x")`, `from importlib import import_module` then `import_module("x")`, and `load_module("x")` (also as `obj.load_module("x")` on any object); and the `load_x()` helpers. Everything else is a site, and the import statement of the module still counts as one.

**What is outside the rule**, said plainly: a name re-exported under another name (`from helpers import loader as get`), a name assembled at run time (`'sy' + 's'`, an f-string), and a way to load code that involves none of these modules or builtins, for example a module object handed in by a caller (`def f(m, n): return getattr(m, n)`), a name looked up in `globals()` with a variable key. It is not listed and not chased. Primitives that the rule does not name and the test does not list either: `ctypes`, `marshal`, `types.ModuleType`, `code` and `codeop`, `py_compile` and `compileall`, `pickle`, and `__loader__` and `sys.meta_path` reached other than through `sys`. They are limits of the stated rule, not hidden: a change that uses one is reviewed, not counted.

A site is listed by the function it sits in and its kind, with a count: `import(module)` for an import statement, the module name for a use of the module object, `sys.modules`, or the builtin with `(non-constant)`, `(relative)`, `(reference)` or `(string)`. A new site, a swapped site, a wrong count or a stale row fails the test.

| Unit | Site | Count | What |
|---|---|---|---|
| `agent_presence.py` | `<module>:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `agent_presence.py` | `<module>:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `agent_presence.py` | `<module>:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `agent_reconcile.py` | `<module>:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `agent_reconcile.py` | `_load_registry:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `agent_reconcile.py` | `_load_registry:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `charter_check.py` | `<module>:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `closed_labels.py` | `<module>:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `cloud_cluster.py` | `<module>:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `cloud_cluster.py` | `_lib:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `cloud_cluster.py` | `_lib:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `cloud_cluster.py` | `_walk:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `cloud_cluster.py` | `_walk:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `cloud_cluster.py` | `unknown_report_to:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `cloud_way_back_check.py` | `check:imp` | 3 | uses the module object (a call, an attribute, an argument) |
| `cloud_way_back_check.py` | `check_fixture_shape:imp` | 4 | uses the module object (a call, an attribute, an argument) |
| `cloud_worker.py` | `<module>:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `cloud_worker.py` | `<module>:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `cloud_worker.py` | `<module>:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `cloud_worker.py` | `<module>:sys.modules` | 1 | uses `sys` beyond its plain attributes |
| `cloud_worker.py` | `_load_cluster:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `cloud_worker.py` | `_load_cluster:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `cloud_worker.py` | `_load_cluster:sys.modules` | 1 | uses `sys` beyond its plain attributes |
| `cloud_worker.py` | `_load_inspect:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `cloud_worker.py` | `_load_inspect:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `cloud_worker.py` | `_load_inspect:sys.modules` | 1 | uses `sys` beyond its plain attributes |
| `cloud_worker.py` | `_registry_note:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `cloud_worker.py` | `_first_push:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `cloud_worker.py` | `_retired:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `cloud_worker.py` | `bind:sys.modules` | 1 | uses `sys` beyond its plain attributes |
| `cloud_worker.py` | `model_entry:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `cloud_worker.py` | `model_entry:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `cpu_guard.py` | `slots_text:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `cpu_guard.py` | `slots_text:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `cpu_guard.py` | `slots_text:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `delegated_clear.py` | `<module>:__builtins__(string)` | 1 | the registrar lint only names this in order to refuse it in a registrar; it loads nothing |
| `delegated_clear.py` | `<module>:__import__(string)` | 1 | the registrar lint only names this in order to refuse it in a registrar; it loads nothing |
| `delegated_clear.py` | `<module>:builtins(string)` | 1 | the registrar lint only names this in order to refuse it in a registrar; it loads nothing |
| `delegated_clear.py` | `<module>:compile(string)` | 2 | the registrar lint only names this in order to refuse it in a registrar; it loads nothing |
| `delegated_clear.py` | `<module>:eval(string)` | 1 | the registrar lint only names this in order to refuse it in a registrar; it loads nothing |
| `delegated_clear.py` | `<module>:exec(string)` | 1 | the registrar lint only names this in order to refuse it in a registrar; it loads nothing |
| `delegated_clear.py` | `<module>:import_module(string)` | 1 | the registrar lint only names this in order to refuse it in a registrar; it loads nothing |
| `delegated_clear.py` | `_literal_compile:compile(string)` | 1 | the registrar lint only names this in order to refuse it in a registrar; it loads nothing |
| `delegated_clear.py` | `_registrar_unit.unsafe:builtins` | 2 | the registrar lint only names this in order to refuse it in a registrar; it loads nothing |
| `delegated_clear.py` | `_registrar_unit:__builtins__(string)` | 1 | the registrar lint only names this in order to refuse it in a registrar; it loads nothing |
| `delegated_clear.py` | `_registrar_unit:__import__(string)` | 2 | the registrar lint only names this in order to refuse it in a registrar; it loads nothing |
| `delegated_clear.py` | `_registrar_unit:builtins` | 2 | the registrar lint only names this in order to refuse it in a registrar; it loads nothing |
| `delegated_clear.py` | `_registrar_unit:import_module(string)` | 1 | the registrar lint only names this in order to refuse it in a registrar; it loads nothing |
| `epic_record.py` | `<module>:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `epic_record.py` | `_plan_ledger:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `epic_record.py` | `_plan_ledger:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `epic_record.py` | `_plan_ledger:sys.modules` | 1 | uses `sys` beyond its plain attributes |
| `external_adapter.py` | `<module>:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `external_adapter.py` | `_load_core:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `external_adapter.py` | `_load_core:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `external_adapter.py` | `_load_core:sys.modules` | 3 | uses `sys` beyond its plain attributes |
| `fleet-switchboard` | `<module>:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `fleet-switchboard` | `installed_guard_module:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `installed_guard_module:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `fleet-switchboard` | `installed_guard_module:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `fleet-switchboard` | `_brief_advisory:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `_brief_advisory:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `fleet-switchboard` | `_brief_advisory:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `switchboard/installer.py` | `Installer.reaper_command_problem:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `switchboard/installer.py` | `Installer.reaper_command_problem:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `switchboard/installer.py` | `Installer.reaper_command_problem:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `fleet-switchboard` | `_declared_module:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `_declared_module:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `fleet-switchboard` | `_declared_module:load_module(non-constant)` | 1 | a dynamic load |
| `fleet-switchboard` | `_fleet_parts:compile` | 1 | compiles a part's text |
| `fleet-switchboard` | `_fleet_parts:exec` | 1 | execs text |
| `fleet-switchboard` | `_fleet_parts:exec(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `fleet-switchboard` | `_make_loader.loader:load_module(non-constant)` | 1 | a dynamic load |
| `fleet-switchboard` | `launcher_module:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `launcher_module:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `fleet-switchboard` | `launcher_module:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `fleet-switchboard` | `load_module:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `load_module:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `fleet-switchboard` | `load_module:sys.modules` | 4 | uses `sys` beyond its plain attributes |
| `fleet-switchboard` | `load_review_rule:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `load_review_rule:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `fleet-switchboard` | `lost_worker_tick:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `lost_worker_tick:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `fleet-switchboard` | `lost_worker_tick:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `fleet-switchboard` | `push_watch_tick:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `push_watch_tick:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `fleet-switchboard` | `push_watch_tick:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `fleet-switchboard` | `validate_app_config:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `validate_app_config:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `fleet-switchboard` | `validate_hold_labels_config:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `validate_hold_labels_config:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `fleet-switchboard` | `record_github_calls:load_module(non-constant)` | 1 | a dynamic load (the census module, by a name kept out of the clear's closure) |
| `fleet-switchboard` | `run_daemon:sys.modules` | 1 | uses `sys` beyond its plain attributes |
| `lanes.py` | `<module>:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `merged_labels.py` | `<module>:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `orders_copy.py` | `<module>:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `queue_ejections.py` | `<module>:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `reassign.py` | `<module>:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `registry.py` | `<module>:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `registry.py` | `<module>:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `registry.py` | `<module>:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `header_cache.py` | `<module>:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `header_cache.py` | `<module>:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `header_cache.py` | `<module>:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `heavy_slots.py` | `_load_part:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `heavy_slots.py` | `_load_part:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `heavy_slots.py` | `_load_part:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
