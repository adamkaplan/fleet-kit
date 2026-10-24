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
7. Today's code breaks some of these rules: the daemon grew as one namespace. Each break is a row of **Known violations**, with its count. A violation that is not a row fails the test; a row with no violation left fails the test (delete it); a count that went over its ceiling fails the test. The list only shrinks. It must end empty.

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
| `awaiting_live.py` | 0 | the age cap on the `awaiting-live` exemption from triage | |
| `charter_check.py` | 0 | the one check of the endpoints of a command that moves or copies between charters | |
| `ci_runs.py` | 0 | the newest run of each check decides the CI verdict | |
| `closed_labels.py` | 0 | a closed issue never keeps asking for an answer | |
| `dead_hint.py` | 0 | what is known about a dead agent and what to do about it (pure text) | |
| `merged_labels.py` | 0 | a merged pull request does not keep its hold | |
| `path_walk.py` | 0 | one walk up from a trusted path, shared by the cloud launcher and the installer | |
| `repo_exclusion.py` | 0 | repositories the owner keeps out of the fleet's view are never watched or read | |
| `report_fold.py` | 0 | folding a report into an earlier send | |
| `resume_brief.py` | 0 | a brief given to `launch --resume` is delivered or the command says it was not | |
| `cloud_channel.py` | 1 | the receiving end of a message between two cloud agents | |
| `cloud_client.py` | 1 | the sending end of a message or report through the cloud channel | |
| `external_adapter.py` | 1 | a generic client for an outside system that holds decisions and work items | |
| `agent_calls.py` | 2 | GitHub calls made from agents' shells, counted per account and per hour | |
| `agent_presence.py` | 2 | the one rule for the presence of a worker that has no terminal | |
| `agent_reconcile.py` | 2 | what a view does with an agent that has no live data | |
| `endpoint_liveness.py` | 2 | the liveness of an agent row that has no pane to judge (on demand only) | |
| `health_records.py` | 2 | health recorders: what the daemon has already read, turned into records | |
| `pr_events.py` | 2 | `pr-state` read from the forwarder's webhook events | |
| `pr_reading.py` | 2 | the one reading of a pull request that `merge-gate` and the clear packet share | |
| `registry.py` | 2 | the one agent registry (interface frozen in `docs/agent-registry.md`) | |
| `decision_core.py` | 3 | the pure core of the Decision subsystem | |
| `cpu_guard.py` | 4 | the CPU guard: numbers, a clock and a state directory in; a verdict out | |
| `decision_sync.py` | 4 | the sync engine between the Decision list and an external decision system | |
| `decisions_pull.py` | 4 | rewrite the local decisions file from a remote source | |
| `disk_janitor.py` | 4 | the temp-directory janitor | |
| `fleet_health.py` | 4 | the load of each orchestrator and the threshold alert | |
| `queue_ejections.py` | 4 | a pull request ejected from the merge queue wakes its orchestrator | |
| `spotlight_guard.py` | 4 | is each guarded folder still kept out of the Spotlight index | |
| `switchboard/reminders.py` | 4 | the reminders file, `remind`, checkpoint reminders, budget notice and the GitHub event constants | |
| `triage_digest.py` | 4 | the stale digest's memory and rules | |
| `triage_ownerless.py` | 4 | ownerless, quiet issues of a team repository | |
| `triage_plan_open.py` | 4 | a plan or work-item issue still open after the pull request that implements it merged | |
| `work_sync.py` | 4 | one external work item per worker assignment, for an outside dashboard | |
| `chat_clear.py` | 5 | clearing a held PR by saying so in the owner's chat: the offers and the checks | |
| `clear_carry.py` | 5 | an owner's clear carries over a pure merge of main | |
| `cloud_channel_server.py` | 5 | an HTTP listener in front of the cloud channel | |
| `cloud_cli.py` | 5 | the pod-side command to send and report through the cloud channel | |
| `cloud_cost_report.py` | 5 | USD per day of the cloud fleet, from the launcher's audit lines (read only) | |
| `cloud_hub.py` | 5 | the cloud switchboard's front door | |
| `cloud_way_back_check.py` | 5 | a read-only check of the way back from the cloud to local | |
| `cloud_worker.py` | 5 | remote (cloud) workers for the switchboard | |
| `delegated_clear.py` | 5 | the Chief of Staff's clear under the owner's standing order | |
| `endpoint_send.py` | 5 | the send path's route to an agent that holds no pane | |
| `heavy_slots.py` | 5 | CPU slots for heavy commands (`fleet-switchboard heavy`) | |
| `impact_floor.py` | 5 | the impact hold's deterministic floor and decision table | |
| `impact_hold.py` | 5 | who may clear, hold or release an impact hold | |
| `item_verdicts.py` | 5 | verdicts for a work item that has no pull request | |
| `lanes.py` | 5 | move an ask from one orchestrator's charter to another's, with a handover | |
| `launch_testers.py` | 5 | testers from the launch record | |
| `merge_gate.py` | 5 | the fleet's own merge queue | |
| `orders_copy.py` | 5 | `orders copy`: a charter's standing orders copied to another charter | |
| `plan_check.py` | 5 | the mechanical check of a plan's scenarios | |
| `plan_ledger.py` | 5 | the evidence ledger and the gates view of plan-driven development | |
| `plan_review.py` | 5 | the plan review engine's pure core | |
| `reassign.py` | 5 | reassign a running worker to another orchestrator | |
| `reds.py` | 5 | classify a pull request's failing tests | |
| `review_checkout.py` | 5 | a pull request's exact head tree, diff and base copies in one private directory | |
| `switchboard/decisions_a.py` | 5 | the escalated-request, PR-notice and order-notice sections of the decisions code | |
| `switchboard/decisions_b.py` | 5 | the external-decision host and the sources behind the decision port | |
| `switchboard/import_v1.py` | 5 | the v1 import converter, the v2 shape checker and the `import-v1` command | |
| `switchboard/judge.py` | 5 | the policy judge's socket, request handling and replies | |
| `switchboard/judge_request.py` | 5 | the policy judge's request: what is asked and with what context | |
| `switchboard/plan_review.py` | 5 | the `plan review` command family (slice R2) | |
| `switchboard/pr_explain.py` | 5 | `pr-explain` and the checks of a held pull request body | |
| `fleet-switchboard` | 6 | the entry: today also every section not yet moved to a part (the assembler, the engine, the commands) | |

## Known violations

Kinds (the number is exact: see rule 7): `namespace` (a part uses names the entry defines: one namespace), `entry-import` (a part relies on an import the entry made), `size` (a file over its limit: the ceiling is in lines), `ambiguous` (a name defined by two units and read by a third), `import` (a unit reaches a higher layer by an import or `load_module`), `cycle` (units of one layer that depend on each other).

| From | To | Kind | Count | Why it is allowed today |
|---|---|---|---|---|
| `bin/test-switchboard` | `-` | size | 90,747 | the one test file; the test split (ST0 and its follow-ups) moves its fakes and classes beside the units |
| `fleet-switchboard` | `-` | size | 30,597 | the entry still holds every section not yet moved to a part; each move slice lowers the ceiling |
| `switchboard/decisions_a.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/decisions_b.py` | `fleet-switchboard` | entry-import | 9 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/import_v1.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/judge.py` | `fleet-switchboard` | entry-import | 9 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/judge_request.py` | `fleet-switchboard` | entry-import | 8 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/plan_review.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/pr_explain.py` | `fleet-switchboard` | entry-import | 3 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/reminders.py` | `fleet-switchboard` | entry-import | 10 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/decisions_a.py` | `fleet-switchboard` | namespace | 39 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/decisions_b.py` | `fleet-switchboard` | namespace | 40 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/import_v1.py` | `fleet-switchboard` | namespace | 21 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/judge.py` | `fleet-switchboard` | namespace | 17 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/judge_request.py` | `fleet-switchboard` | namespace | 30 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/plan_review.py` | `fleet-switchboard` | namespace | 30 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/pr_explain.py` | `fleet-switchboard` | namespace | 10 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/reminders.py` | `fleet-switchboard` | namespace | 30 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |

## Dynamic imports not followed

The derivation follows `import`, `from . import x`, `from .x import y`, and a constant-string `__import__("x")`, `importlib.import_module("x")` and `load_module("x")`, however the call is spelled or bound (`imp = __import__`, `from importlib import import_module as im`, `getattr(importlib, "import_module")`), and the `load_x()` helpers. It does not follow, and lists here by the enclosing function and the kind of call, with a count: a non-constant argument, a load by path or by text (`exec`, `eval`, `compile`, `find_spec`, `spec_from_file_location`, `SourceFileLoader`, `run_path`) and a relative import of level two or more. A new site, a swapped site, a wrong count or a stale row fails the test.

| Unit | Site | Count | What |
|---|---|---|---|
| `agent_reconcile.py` | `_load_registry:spec_from_file_location` | 1 | loads a module by path |
| `cloud_worker.py` | `_lib:spec_from_file_location` | 1 | loads a module by path |
| `cloud_worker.py` | `_walk:spec_from_file_location` | 1 | loads a module by path |
| `cloud_worker.py` | `model_entry:spec_from_file_location` | 1 | loads a module by path |
| `cpu_guard.py` | `slots_text:spec_from_file_location` | 1 | loads a module by path |
| `external_adapter.py` | `_load_core:spec_from_file_location` | 1 | loads a module by path |
| `fleet-switchboard` | `Installer.reaper_command_problem:spec_from_file_location` | 1 | loads a module by path |
| `fleet-switchboard` | `_declared_module:load_module(non-constant)` | 1 | loads the module its argument names |
| `fleet-switchboard` | `_declared_module:spec_from_file_location` | 1 | loads a module by path |
| `fleet-switchboard` | `_fleet_parts:compile` | 1 | compiles a part's text |
| `fleet-switchboard` | `_fleet_parts:exec` | 1 | runs a part's code in the entry's namespace |
| `fleet-switchboard` | `_make_loader.loader:load_module(non-constant)` | 1 | loads the module its argument names |
| `fleet-switchboard` | `launcher_module:spec_from_file_location` | 1 | loads a module by path |
| `fleet-switchboard` | `load_module:spec_from_file_location` | 1 | loads a module by path |
| `fleet-switchboard` | `load_review_rule:spec_from_file_location` | 1 | loads a module by path |
| `fleet-switchboard` | `validate_app_config:spec_from_file_location` | 1 | loads a module by path |
| `fleet-switchboard` | `validate_hold_labels_config:spec_from_file_location` | 1 | loads a module by path |
