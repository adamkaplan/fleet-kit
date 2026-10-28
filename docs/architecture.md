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
7. Today's code breaks some of these rules: the daemon grew as one namespace. Each break is a row of **Known violations**, with its count. A violation that is not a row fails the test; a row with no violation left fails the test (delete it); a dependency count that is not exactly its row's number fails the test (more is a new break; fewer means the row is written lower, and the message names the number). The two `size` rows (the entry and `bin/test-switchboard`) are ceilings with headroom: a file within its ceiling passes, a file over it fails with the number to write and what to do, and a row more than 7,000 lines above its file fails with the lower number to write. The list only shrinks. It must end empty. A move that lowers a count lowers its row in the same pull request.

## Where a new test goes

A new file `tests/switchboard/test_<unit>.py` is preferred, named for the unit it is about, and it is picked up by `bin/test-switchboard` like `test_architecture.py`. Appending to `bin/test-switchboard` is allowed until the test split empties it, within the size row's headroom; when a change would go past the ceiling, the test names where the new tests go (a file under `tests/switchboard/`) or which row to lower.

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
| `brief_advisory.py` | 0 | the pure rules that read an impact brief for the advisory case, the Decisions row and the held-body rationale line (#1215) | |
| `charter_check.py` | 0 | the one check of the endpoints of a command that moves or copies between charters | |
| `ci_runs.py` | 0 | the newest run of each check decides the CI verdict | |
| `cloud_quick.py` | 0 | the shared selection helper of the cloud pre-push quick mode: picks the tests that read the changed files, holds the scan list and the heavy-test list, resolves a scan selector (standard library only) | |
| `closed_labels.py` | 0 | a closed issue never keeps asking for an answer | |
| `dead_hint.py` | 0 | what is known about a dead agent and what to do about it (pure text) | |
| `impact_brief.py` | 0 | the impact brief: what a held change can do, built from the diff and the records only (pure, no clock, no network) (#1215) | |
| `merged_labels.py` | 0 | a merged pull request does not keep its hold | |
| `plan_approval.py` | 0 | the check of an approval sent as a message (pure) | |
| `record_json.py` | 0 | the one bounded-depth parse of untrusted record JSON, shared by every reader of it (#1314) | |
| `red_ci.py` | 0 | which check conclusions are red, green or say nothing, and the once-per-key memory (pure table) | |
| `path_walk.py` | 0 | one walk up from a trusted path, shared by the cloud launcher and the installer | |
| `repo_exclusion.py` | 0 | repositories the owner keeps out of the fleet's view are never watched or read | |
| `report_fold.py` | 0 | folding a report into an earlier send | |
| `resume_brief.py` | 0 | a brief given to `launch --resume` is delivered or the command says it was not | |
| `cloud_channel.py` | 1 | the receiving end of a message between two cloud agents | |
| `cloud_relay.py` | 2 | the hub half of pod, hub and daemon calls for report, prove, verdict and gates: a bounded in-memory queue and the refusals | |
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
| `scratch_dir.py` | 4 | the caller's own scratch directory under the state directory: made private, refused when unsafe, warned at 100 MB | |
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
| `path_suites.py` | 5 | which test suites exercise a changed path, and the local merge `heavy --changed` runs them on | |
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
| `switchboard/v2_transport.py` | 1 | the OpenCode v2 client and its HTTP transport | |
| `switchboard/jev.py` | 1 | the Jev client: the decision model, the classifier and the policy judge's questions | |
| `switchboard/audit.py` | 2 | the audit log | |
| `switchboard/facts.py` | 4 | the fact sources: the world read into facts | |
| `switchboard/decision.py` | 3 | the delivery decision: what is sent, to whom, and refused when too long | |
| `switchboard/engine.py` | 3 | the engine: the single-instance lock and the pass | |
| `switchboard/triage.py` | 4 | the triage pass (rule family T), shadow by default, written nothing to GitHub | |
| `switchboard/alert_labels.py` | 4 | an alert label on an open issue wakes that repository's orchestrator | |
| `switchboard/handoff.py` | 5 | the handoff: a brief, not a fork | |
| `switchboard/agents_verify.py` | 5 | `agents verify`: did the service register our agents as written | |
| `switchboard/answer_delivery.py` | 5 | how an answer reaches the asker, behind the AnswerDelivery port | |
| `cloud_relay_daemon.py` | 5 | the daemon half of the cloud hub relay: pulls pod requests from the hub and runs them with the same rules as the local commands | |
| `fleet-switchboard` | 6 | the entry: today also every section not yet moved to a part (the assembler, the engine, the commands) | |

## The clear's closure follows the parts

The delegated clear refuses a pull request that changes a definition of "the clear's closure" (`bin/delegated_clear.py`, `closure`). The closure is derived over the assembled daemon: the entry plus every part of `bin/fleet-switchboard.parts`, as one namespace. `tools/derive-closure --write` lists in `docs/review-laws/impact-matrix.json` each part that holds a closure definition OR a module-level binding or import the closure reads (`closure_files`), with the definitions as `module_roots[part]` (empty for a part that holds only a binding: listing the part is what refuses an edit of it); `--check` fails in both directions (a name or part missing, a name or part extra) and names the part and the name. A new part is in by derivation, not by a hand list. The manifest is read as data and fails closed (exit 1, the entry named, nothing written): an entry that is not a file, is not `switchboard/<name>.py` (`..`, a directory, an absolute path), is listed twice, a part file the manifest does not list (it is never assembled), and a closure definition defined in two files. `--write` replaces the matrix through a temp file and a rename, and never writes after a failed derivation. `tests/switchboard/test_closure.py` runs the check and pins two floors for the assembled closure from `closure_roots`: 395 definitions and 335 bindings (a falling count fails the test; a floor is raised only in its own commit, with the owner's sign-off), and keeps the entry's own `closure_roots` as they were. After moving a definition to another part, run `tools/derive-closure --write` in the same pull request. For a listed part, any top-level statement edit is refused, as for every module the clear loads.

## Known violations

Kinds (the number is exact, except `size`, a ceiling with headroom: see rule 7): `namespace` (a part uses names the entry defines: one namespace), `entry-import` (a part relies on an import the entry made), `size` (a file over its limit: the ceiling is in lines), `ambiguous` (a name defined by two units and read by a third), `import` (a unit reaches a higher layer by an import or `load_module`), `cycle` (units of one layer that depend on each other).

| From | To | Kind | Count | Why it is allowed today |
|---|---|---|---|---|
| `bin/test-switchboard` | `-` | size | 89,500 | batch-7 bridge: the held group adds ~3,500 lines of tests; lowered again by T1b |
| `switchboard/alert_labels.py switchboard/triage.py` | `-` | cycle | 2 | moved together from the entry: each reads the other, one cycle to untangle |
| `switchboard/answer_delivery.py switchboard/decisions_b.py` | `-` | cycle | 2 | moved together from the entry: each reads the other, one cycle to untangle |
| `cloud_worker.py` | `-` | size | 2,000 | grew past the limit in a merged change; to be split |
| `impact_hold.py` | `-` | size | 2,000 | the brief-bound clear (#1215) took it past the limit; to be split (ceiling = the test's own formula: 1510 lines, plus 500, rounded up to a multiple of 500) |
| `delegated_clear.py` | `-` | size | 1,900 | the registrar lint lives with the clear it protects; a follow-up slice moves it to its own part and lowers the ceiling |
| `switchboard/agents_verify.py` | `fleet-switchboard` | entry-import | 6 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/agents_verify.py` | `fleet-switchboard` | namespace | 7 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/alert_labels.py` | `fleet-switchboard` | entry-import | 5 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/alert_labels.py` | `fleet-switchboard` | namespace | 12 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/alert_labels.py` | `launch_testers.py` | import | 1 | a name the part reads from another unit without a declared edge |
| `switchboard/answer_delivery.py` | `fleet-switchboard` | entry-import | 5 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/answer_delivery.py` | `fleet-switchboard` | namespace | 33 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/audit.py` | `fleet-switchboard` | entry-import | 12 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/audit.py` | `fleet-switchboard` | namespace | 2 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/decision.py` | `fleet-switchboard` | entry-import | 1 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/decision.py` | `fleet-switchboard` | namespace | 8 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/decision.py` | `switchboard/handoff.py` | namespace | 1 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/engine.py` | `fleet-switchboard` | entry-import | 5 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/engine.py` | `fleet-switchboard` | namespace | 1 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/facts.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/facts.py` | `fleet-switchboard` | namespace | 7 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/handoff.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/handoff.py` | `fleet-switchboard` | namespace | 48 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/jev.py` | `fleet-switchboard` | entry-import | 6 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/jev.py` | `fleet-switchboard` | namespace | 5 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/reminders.py` | `switchboard/handoff.py` | namespace | 3 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/triage.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/triage.py` | `fleet-switchboard` | namespace | 34 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/triage.py` | `switchboard/decisions_a.py` | namespace | 3 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/v2_transport.py` | `fleet-switchboard` | entry-import | 15 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/v2_transport.py` | `fleet-switchboard` | namespace | 2 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `fleet-switchboard` | `-` | size | 27,500 | the entry still holds every section not yet moved to a part; each move slice lowers the ceiling |
| `switchboard/decisions_a.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/decisions_b.py` | `fleet-switchboard` | entry-import | 9 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/import_v1.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/judge.py` | `fleet-switchboard` | entry-import | 9 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/judge_request.py` | `fleet-switchboard` | entry-import | 8 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/plan_review.py` | `fleet-switchboard` | entry-import | 7 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/pr_explain.py` | `fleet-switchboard` | entry-import | 3 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/reminders.py` | `fleet-switchboard` | entry-import | 12 | the part relies on a standard-library module the entry imported; it gets its own import line |
| `switchboard/decisions_a.py` | `fleet-switchboard` | namespace | 34 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/decisions_b.py` | `fleet-switchboard` | namespace | 30 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/import_v1.py` | `fleet-switchboard` | namespace | 17 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/judge.py` | `fleet-switchboard` | namespace | 13 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/judge_request.py` | `fleet-switchboard` | namespace | 26 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/plan_review.py` | `fleet-switchboard` | namespace | 27 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/pr_explain.py` | `fleet-switchboard` | namespace | 10 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports |
| `switchboard/reminders.py` | `fleet-switchboard` | namespace | 25 | one namespace: the part was cut out of the entry unchanged and reads the entry's names; its rewiring gives it imports; two reads of untrusted JSON now go through `load_module("record_json")`, one name of the entry more (#1314) |

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
| `cloud_way_back_check.py` | `check:imp` | 3 | uses the module object (a call, an attribute, an argument) |
| `cloud_way_back_check.py` | `check_fixture_shape:imp` | 4 | uses the module object (a call, an attribute, an argument) |
| `cloud_worker.py` | `<module>:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `cloud_worker.py` | `_lib:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `cloud_worker.py` | `_lib:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `cloud_worker.py` | `_registry_note:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `cloud_worker.py` | `_walk:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `cloud_worker.py` | `_walk:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
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
| `external_adapter.py` | `<module>:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `external_adapter.py` | `_load_core:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `external_adapter.py` | `_load_core:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `external_adapter.py` | `_load_core:sys.modules` | 3 | uses `sys` beyond its plain attributes |
| `fleet-switchboard` | `<module>:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `fleet-switchboard` | `Installer.reaper_command_problem:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `Installer.reaper_command_problem:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `fleet-switchboard` | `Installer.reaper_command_problem:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `fleet-switchboard` | `_brief_advisory:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `_brief_advisory:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `fleet-switchboard` | `_brief_advisory:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
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
| `fleet-switchboard` | `validate_app_config:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `validate_app_config:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `fleet-switchboard` | `validate_hold_labels_config:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `fleet-switchboard` | `validate_hold_labels_config:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
| `fleet-switchboard` | `run_daemon:sys.modules` | 1 | uses `sys` beyond its plain attributes |
| `lanes.py` | `<module>:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `merged_labels.py` | `<module>:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `orders_copy.py` | `<module>:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `queue_ejections.py` | `<module>:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `reassign.py` | `<module>:load_module(string)` | 1 | names a loading builtin by a string (a name in a list counts: over-counting by design) |
| `registry.py` | `<module>:exec_module` | 1 | uses an identifier of the import machinery (exec_module) |
| `registry.py` | `<module>:import(importlib)` | 1 | imports a module that can load code by name, or one of its identifiers |
| `registry.py` | `<module>:importlib` | 2 | uses the module object (a call, an attribute, an argument) |
