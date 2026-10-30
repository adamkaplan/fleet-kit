# The agent registry: inventory and interface (slice 1 of #903)

This is slice 1 of #903 (work item #906): a document and one test, no product code. Part one lists every reader and writer of an
agent list or record, taken from the code. Part two is the interface of the one registry that slice 2 builds, written so slices 3
and 4 can be coded against it. `bin/test-switchboard` (`TestAgentRegistryInventory`) fails when the code gains a reader or writer
this table misses, or when the spec loses a section.

## Inventory

One row per file and record. `file:line` is the first place in that file that touches the record (lines drift; the test checks the
file and the record, not the line). Primary means the record is the source of truth for something; derived means it can be
rebuilt from another record.

| file:line | record | what it holds | written by | read by | primary or derived |
|---|---|---|---|---|---|
| `bin/fleet-switchboard:1241` | `pane-health.json` | per agent: pane record (`agents`: session, pane, workspace, role...), `states` (live, detached, dead), `missing`, `at` | `pane_health_pass` via `write_pane_health` (daemon, every pass); `reattach` and `retire`/`--undo` edit it | `status` (`pane_health_status_lines`), `reattach`, `retire`, `known_agent` callers, `cmd_read` path of cloud_worker, discovery helpers at :7713 | derived from discovery plus herdr, yet read as if primary |
| `bin/registry.py:26` | `pane-health.json` | the old per-agent pane records, read by `fold_old` (via `_small`) to build the registry's `agents` the first time a state directory has no `registry.json` | nobody (reader only; the daemon still writes the file) | `fold_old`, called by `_migrate` on the first `read` or write | derived (the fold's input; the registry is the record after it) |
| `bin/fleet-switchboard:6512` | `retired.json` | per agent: at, by, reason, session, auto, and `record` (the pane record kept at retirement) | `retire` / `retire --undo`, the daemon's auto-retire on a final report (`pane_health_pass`), `prune_retired` | detection (skips retired), `reattach`, `known_agent`, `retire --undo`, `status`, tester gate (`read_retired_strict`), sweeps, `route` | primary for retirement |
| `bin/registry.py:25` | `retired.json` | the old retirements, read by `_old_retired` at the fold; written back by `_derive` from the registry on every change (old readers see the same format) | `_derive` (derived output) | `_old_retired` in `fold_old` | derived (output) and the fold's input |
| `bin/cloud_worker.py:1359` | `retired.json` | reads the retired session through `known_agent`, which now reads the registry (`registry.read`) | nobody (reader only) | `cmd_read` of the cloud log read | primary (reader, through the registry) |
| `bin/reassign.py:34` | `retired.json` | uses `read_retired` so a reassign refuses a retired worker | nobody (reader only) | `reassign` / `reassign --undo` | primary (reader) |
| `bin/fleet-switchboard:5066` | `last-reports.json` | per sender: last report state and time, last send per recipient | `record_last_report` (`report`, `send`) | the daemon's retire-on-final-report, `status`, `dead_hint`, fact sources, report fold | primary |
| `bin/dead_hint.py:3` | `last-reports.json` | reads one entry for the dead-agent hint | nobody (reader only) | `dead_hint.hint`, called by the pane pass | primary (reader) |
| `bin/fleet-switchboard:4331` | `boss-overrides.json` | per worker: the boss a reassign set, bound to one session | `reassign` (bin/reassign.py, under a lock) | discovery overlay (`Engine.discover`, headless records), `reattach_caller_problem` | primary (clear machinery) |
| `bin/reassign.py:4` | `boss-overrides.json` | the same file: reads and writes the overrides | `run_reassign` | `overlay` | primary (clear machinery) |
| `bin/fleet-switchboard:3382` | `boss-resolution` | no file: `resolve_boss` and `boss_name` map `reports_to` (or the role alias) to a registered agent from the discovery | nobody | sends, reports, decisions, reattach, pane pass, `steers_into_turn` | derived from discovery (clear machinery) |
| `bin/switchboard/decisions_a.py:209` | `boss-resolution` | the decisions list resolves each worker's boss through `resolve_boss` | nobody | the Decisions list (`decisions_from_reports`) | derived (reader) |
| `bin/switchboard/decisions_b.py:459` | `boss-resolution` | the external-decision host resolves a decision's boss through `boss_name` | nobody | external decision sync | derived (reader) |
| `bin/impact_hold.py:426` | `boss-resolution` | the impact hold resolves a boss through `boss_name`/`resolve_boss` | nobody | impact hold routing | derived |
| `bin/lanes.py:197` | `boss-resolution` | the lane rule resolves a boss | nobody | lane resolution | derived |
| `bin/reassign.py:237` | `boss-resolution` | `reassign` resolves the current boss for consent | nobody | `reassign` | derived |
| `bin/fleet-switchboard:2385` | `discovery.agents` | the agents of the newest `discover()`: name, session, pane, role, charter, repo, workspace, cwd, reports_to | `Engine.discover` (each pass and on demand) | the whole daemon: fact routing, status, pane pass, decisions, chiefs lookup, badges, toasts | primary (the live list), derived from herdr and the v2 service |
| `bin/cloud_worker.py:976` | `discovery.agents` | looks up a local agent by name before routing | nobody | `route` | derived (reader) |
| `bin/switchboard/judge.py:53` | `discovery.agents` | the judge's own sessions: the session ids of the discovered agents | nobody | the policy judge's socket | derived (reader) |
| `bin/switchboard/decisions_a.py:209` | `discovery.agents` | the decisions list reads the discovered agents to find each worker's boss | nobody | the Decisions list | derived (reader) |
| `bin/switchboard/decisions_b.py:457` | `discovery.agents` | the external-decision host reads the newest discovery | nobody | external decision sync | derived (reader) |
| `bin/impact_hold.py:401` | `discovery.agents` | looks up the orchestrator of a block | nobody | impact hold | derived (reader) |
| `bin/lanes.py:55` | `discovery.agents` | looks up lanes by agent | nobody | lane resolution | derived (reader) |
| `bin/queue_ejections.py:365` | `discovery.agents` | routes an ejection to an owner | nobody | `route_of` | derived (reader) |
| `bin/reassign.py:164` | `discovery.agents` | finds the worker to reassign | nobody | `reassign` | derived (reader) |
| `bin/fleet-switchboard:138` | `health-records.json` | per agent tool calls, tokens, context, pushes (derived figures) | the daemon via `health_records.Recorder` and `state_write` | the sidebar plugin | derived |
| `bin/health_records.py:5` | `health-records.json` | defines the file and its shape | `Recorder` (called by the daemon) | the sidebar plugin | derived |
| `plugins/fleet-decisions-tui/health.mjs:3` | `health-records.json` | reads it for the sidebar's health block | nobody (reader only) | sidebar health view | derived (reader) |
| `bin/health_records.py:30` | `tool-rate.json` | per session tool calls per minute | plugins/fleet-hooks (`TALLY_FILE`) | `health_records.read_tool_file` | derived |
| `plugins/fleet-hooks/index.js:195` | `tool-rate.json` | writes the tally of tool calls per session | the fleet-hooks plugin (a timer, atomic) | `bin/health_records.py` | derived (writer) |
| `bin/fleet_health.py:5` | `load.json` | per orchestrator load figure and an hourly series | `fleet_health.tick` (daemon) | `status` load line, sidebar | derived |
| `plugins/fleet-decisions-tui/health.mjs:13` | `load.json` | reads the load figures | nobody (reader only) | sidebar health view | derived (reader) |
| `bin/fleet-switchboard:4239` | `decisions.json` | open decisions, notices, held PRs, each naming agents (boss and worker) | the daemon (`DecisionStore`, atomic, when the list changed) | `decisions` command, sidebar, `fleet_health`, `status` | derived |
| `bin/decision_core.py:1032` | `decisions.json` | comment only: names the file as the local store | nobody | nobody | derived |
| `bin/switchboard/judge_request.py:433` | `decisions.json` | reads the open decisions to give the judge the ask's context | nobody (reader only) | the policy judge's request | derived (reader) |
| `bin/switchboard/decisions_a.py:274` | `decisions.json` | comment only: the decisions list never rewrites the projection by itself | nobody | nobody | derived |
| `bin/switchboard/decisions_b.py:281` | `decisions.json` | comment only: names the projection among what the store keeps | nobody | nobody | derived |
| `bin/switchboard/plan_review.py:544` | `boss-resolution` | plan review escalates to the planner's boss through `boss_name`, then `resolve_boss` | nobody | plan review escalation | derived (reader) |
| `bin/switchboard/plan_review.py:542` | `discovery.agents` | plan review reads the newest discovery to find the planner's boss | nobody | plan review escalation | derived (reader) |
| `bin/switchboard/reminders.py:346` | `boss-resolution` | a reminder's recipient is resolved through `resolve_boss` | nobody | reminder delivery | derived (reader) |
| `bin/switchboard/reminders.py:176` | `discovery.agents` | a reminder condition reads the discovered agents (recipient and sender) | nobody | reminder delivery | derived (reader) |
| `bin/switchboard/reminders.py:143` | `last-reports.json` | a reminder's "no report since" condition reads the sender's last report time | nobody | reminder delivery | primary |
| `bin/switchboard/decision.py:86` | `boss-resolution` | the delivery decision compares a send's sender with the observation's boss through `boss_name` | nobody | the delivery decision | derived (reader) |
| `bin/switchboard/facts.py:120` | `discovery.agents` | a fact source reads the discovered agents to find the chiefs of staff | nobody | fact sources | derived (reader) |
| `bin/switchboard/v2_transport.py:24` | `pane-health.json` | comment only: the heartbeat-health bound is named for the derived file; nothing reads its time here | nobody | nobody | derived |
| `bin/switchboard/agents_verify.py:155` | `discovery.agents` | `agents verify` reads the discovered agents to compare each with its registration | nobody | the moved code's own reader | derived (reader) |
| `bin/switchboard/alert_labels.py:83` | `discovery.agents` | the alert-label wake finds a repository's orchestrators among the discovered agents | nobody | the moved code's own reader | derived (reader) |
| `bin/switchboard/bootstrap.py:60` | `discovery.agents` | `bootstrap` reads the discovered agents to find the chief of staff | nobody | the moved code's own reader | derived (reader) |
| `bin/switchboard/answer_delivery.py:592` | `decisions.json` | comment only: the delivery paths wait for the daemon's next write of the projection | nobody | the moved code's own reader | derived (reader) |
| `bin/switchboard/handoff.py:587` | `boss-resolution` | the stop triage resolves a worker's boss by its registered name (`boss_name`) | nobody | the moved code's own reader | derived (reader) |
| `bin/switchboard/handoff.py:325` | `decisions.json` | the handoff reads the decisions file for the open decisions of a worker | nobody | the moved code's own reader | derived (reader) |
| `bin/switchboard/handoff.py:102` | `discovery.agents` | the handoff reads the discovered agents to find the named worker | nobody | the moved code's own reader | derived (reader) |
| `bin/switchboard/handoff.py:642` | `last-reports.json` | comment only: the brief reads the last-reports record `report` wrote | nobody | the moved code's own reader | derived (reader) |
| `bin/switchboard/triage.py:358` | `discovery.agents` | a triage rule reads the discovered agents to find the orchestrators | nobody | the moved code's own reader | derived (reader) |
| `bin/fleet_health.py:298` | `decisions.json` | reads the held PR and waiting decisions per orchestrator | nobody (reader only) | load figures | derived (reader) |
| `plugins/fleet-decisions-tui/decisions.mjs:9` | `decisions.json` | reads the list for the sidebar | nobody (reader only) | Decisions section | derived (reader) |
| `bin/decisions_pull.py:28` | `decisions.json` | reads the cloud daemon's list from a configured source command and rewrites the local file (cleaned, atomic, mode 0600); ages its mtime when the source is down; for a hub source the age is the response's integer `age_seconds` (the hub file's mtime), for a plain file `--age-command`; a lower generation from the same daemon is refused unless `--reset-generation` | the Mac-side pull (`write_atomic`, `mark_stale`) | the Decisions plugin (`decisions.mjs`) | derived |
| `plugins/fleet-decisions-tui/health.mjs:15` | `decisions.json` | reads the list for the health block | nobody (reader only) | sidebar health view | derived (reader) |
| `plugins/fleet-decisions-tui/tui.tsx:2` | `decisions.json` | the TUI section renders it through decisions.mjs | nobody (reader only) | sidebar | derived (reader) |
| `plugins/fleet-decisions-tui/health.mjs:3` | `pane-health.json` | reads which agents sit in which workspace and their state | nobody (reader only) | sidebar health view (the sidebar's own read of the agent list, from its own installed copy of the plugin: a view) | derived (reader) |
| `bin/cloud_worker.py:101` | `cloud-records` | one record per cloud worker in `state/cloud/<name>.json`: run id, pod, repo, branch | `cloud_worker.cmd_launch`, the cloud reaper and `cmd_retire` (stop) | `route`, the cloud log read (`cmd_read`), `status` | primary for cloud workers; slice 2 mirrors them into the registry |
| `bin/switchboard-trial:483` | `decisions.json` | comment and environment only: the trial lab gives the sidebar plugin the daemon's state directory | nobody | nobody (the plugin reads it) | derived |
| `bin/fleet-switchboard:4400` | `headless-records` | launch records `state/headless/<name>.json` and `headless-service.json` (boss per worker) and `headless-route.json` | `launch` (headless), the restart notice | discovery of headless workers, the restart notice, route checks | primary for headless workers |
| `bin/agent_presence.py:25` | `headless-records` | scans `state/headless/*.json` | nobody (reader only) | presence checks | primary (reader) |
| `plugins/fleet-hooks/index.js:407` | `headless-records` | reads the launch record to tell a headless worker | nobody (reader only) | the fleet-hooks plugin | primary (reader) |

Limit of the scan: it matches the record names and the functions that read them in the text of every tracked source under `bin/`, `plugins/` and `cloud/`. A path built from parts, a listing of the state directory or a name held in a variable is not found; the scan can only fail on a name written out. Slice 3's runtime test covers the rest.

`bin/fleet-heartbeat` has no rows: it no longer keeps a live agent listing (`tabs_with_agents`, `discover()`). `registry_targets` (line 102) reads the registry through `registry.read` for identity (who exists, pane, workspace, state) and the `bell` field through `registry.bell_of` (line 121); a read that is not whole nudges nobody. What stays its own is bookkeeping: its nudge state and per-pane health in its own `state.json`, reported by `fleet-heartbeat status --json`.

Not an agent list, so not rows: the cloud log read keeps no list of its own (it reads `pane-health.json` and `retired.json` for one
session, above); sweeps and the hooks plugin appear above only where they read or write one of these records.

## Interface spec

### 1. Read API

`registry.read(state_dir) -> Read` where `Read` is `{"whole": bool, "version": int, "agents": {name: Agent}, "retired": {name: Retired}, "problem": str|None}`.

- `whole` is true only when the file was read, parsed and its version is known. A missing file is whole and empty (nobody registered).
- A read that is not whole (unreadable, torn, invalid, newer version) returns `agents: {}`, `retired: {}`, `whole: false` and a one-line `problem`. A caller that
  acts on the list (the #805 rule) acts only when `whole` is true.
- `registry.get(state_dir, name) -> Agent|None` reads the same way; `None` with a problem is not "absent".
- `registry.get_retired(state_dir, name) -> Retired|None` returns the retired record (session, at, by, reason, `record`), read the same way.
- A consumer that ACTS on a read (the close-pane guard `retire_close_problem`, `reattach`, `reconcile_remove`) fails CLOSED when `whole` is false: it takes no action on a half read.
- `fleet-heartbeat` is a consumer of `Read` and obeys the whole-read rule: a heartbeat pass whose read is not whole nudges NOTHING (fail closed).
- Consumers (slice 2 lists them in its plan): `status`, `reattach`, `retire`/`--undo`, the close-pane guard (`retire_close_problem`), `known_agent`, the cloud log read, the sidebar plugin, `fleet-heartbeat`, decisions, the load line and the health records.
- The state `dead` is never rendered: a view shows a live state or nothing. A dead agent is removed by reconcile on read, not drawn (slice 4's test asserts it).

### 2. Write API

Each call takes the lock, reads, changes, and writes by the atomic rule below, in one step, and returns `{"changed": bool, "problem": str|None}`.

- `registry.retire(state_dir, name, by, reason, auto=False)`: the agent keeps its record under `retired` (session, pane record, at, by, reason) and leaves `agents`.
- `registry.unretire(state_dir, name)`: the reverse (`retire --undo`).
- `registry.close(state_dir, name, by, reason)`: the pane is closed on purpose; as `retire`, with `reason` recorded.
- `registry.reconcile_remove(state_dir, name, evidence, whole)`: removes a dead agent. The registry ITSELF checks the evidence (the agent's recorded state and what the live read lacked: pane, process), not only the caller's `whole` flag; it refuses (`changed: false`) when `whole` is not true or when the evidence does not show the agent dead. It appends an audit line carrying that evidence. A test calls it with `whole=True` for a live agent and requires the refusal.
- `registry.upsert(state_dir, name, agent)`: discovery's and launch's way to add or update an agent. NO CHURN: when nothing changed it writes nothing and returns `changed: false`. An agent retired for the same session is refused (`changed: false`, with a problem). An audit line is written on add, remove, retire and close, and on a REFUSED removal (`reconcile_refused`, with the evidence and the reason, both when the read was not whole and when the evidence does not show the agent dead); never on a no-op. v1.1 note (additive): refusals of a removal are audited too.
- The #870 rule: a retired record with no `session` covers nothing. The fields of a pruned pane record come from `retired[name].record`, for `unretire`, `known_agent`, `reattach` and the cloud log read (slice 2 pins each by a test).
- Cloud workers are agents: a record carries `"kind": "cloud"` and the `run_id` the launcher already keeps (`state/cloud/<name>.json`). Views list them like any agent. `retire`, `close` and `reconcile_remove` work on them as on a local agent, the registry change in one step, and the stop of the pod stays the cloud launcher's act. The cloud launcher and reaper are WRITERS (they `upsert` and `retire`); the cloud log read is a READER.

- Retention and audit file: retired entries older than 4 weeks (`RETIRED_KEEP_SECONDS`) are pruned on a write. Audit lines go to `registry-audit.jsonl` in the state directory.

### 3. The file

`registry.json` in the daemon's state directory (the same directory as `pane-health.json`), mode 0600:

```json
{"version": 1,
 "agents": {"<name>": {"session": "ses_example", "pane": "p_1", "role": "coder", "charter": 903, "repo": "acme-org/widget-service",
                       "workspace": "w_1", "cwd": "/home/user/work", "state": "working", "kind": "local", "run_id": null, "since": 1760000000}},
 "retired": {"<name>": {"at": 1760000100, "by": "orchestrator", "reason": "final report", "session": "ses_example", "auto": false, "record": {}}}}
```

`state` is one of `working`, `blocked`, `idle`, `detached`, `dead`. The fields are exactly those the old readers use (session, pane,
role, charter, repo, workspace, cwd).

### 4. The lock

An exclusive `flock` on `registry.lock` beside the file, held for the whole read-change-write of a write call. Readers take no lock: they
rely on the atomic write. The lock is waited for up to 5 seconds (`LOCK_SECONDS`); a timeout is returned as a problem (`changed: false`), not raised. LOCK ORDER: the registry lock first, then the boss-record lock (`boss-overrides`), never the reverse.

### 5. The atomic write

Write the full new text to `registry.json.<pid>.tmp` (mode 0600), flush and `fsync`, then `rename` it over `registry.json`. A file is never written in place.

### 6. The version rule

`version` is an integer. A reader that finds a version higher than it knows reads nothing: `whole: false`, `agents: {}` and the line
`registry.json is version N; this reader knows M: nothing read`. A writer that finds a newer version writes nothing and says the same. The
version rises only when a field changes meaning or goes away; a new optional field does not raise it.

## Migration rule

Until the last slice, `pane-health.json`, `retired.json` and the other old files in the inventory are derived output of the registry:
written from it, read by no view. Slice 2 folds today's records into the registry with no loss (reattach, `retire --undo` and the cloud
log read give the same result, each with `HERDR_*` and `OPENCODE_*` unset). The old files go only in the last slice. The sidebar plugin is a view that reads `pane-health.json` from its own installed copy: its read is derived output until the last slice, and slice 3's "every view derives" test lists it.

Clear-machinery files by slice (a slice that touches one says so in its plan and its pull request holds for the owner):

- Slice 2: `bin/fleet-switchboard` (`retire`, `reattach`, `known_agent`, `pane_health_pass`) and `bin/reassign.py` (boss overrides); the boss records are not moved, only read.
- Slice 3: none planned; if it edits `resolve_boss` or `boss_name` it says so.
- Slice 4: none planned; its test asserts no view renders `dead`; the reconcile writes only through `registry.reconcile_remove`.

## Freeze

FROZEN: when the product, chief-of-staff and daemon lanes have each commented agreement on #906 (no earlier). After it a change needs all three lanes to agree again.

Status: FROZEN 2026-10-20, v1.1 additive (2026-10-20) (the product, chief-of-staff and daemon lanes agreed on #906).

## Additive note (#1074): endpoint field for an agent without a pane

- **Endpoint field.** A row may carry `endpoint`: an address (a cloud pod, tunnel or in-cluster address) and `kind: orchestrator`. It is written only when given (no `endpoint: null`), no version bump. It is never a secret: a URL with a userinfo part or a credential-shaped query (`token`, `key`, `secret`, ...) is refused and nothing is stored. A row with an endpoint holds no pane.
- **Liveness rule.** A row with no pane is never read as dead for that reason. Reconcile on read removes it only on a whole read, audited, when the endpoint's bounded check answers `dead`; `unknown`, unreachable, failed and partial reads remove nothing. The default check answers `unknown`.
- **Route rule.** `registry.route_for` answers the agent's pane when it has one, else its registry `endpoint`, else none. The send path uses it (#1131): an agent with a pane is routed as before; one with no pane and an endpoint is sent to that endpoint through the cloud channel client (`bin/cloud_client.py`), with the same sender identity and the same held-while-blocked rule; an unknown agent sends nothing and says why. The endpoint must be `https`, or `http` only for an in-cluster service host (`.svc`, `.svc.cluster.local` or a single label), and a redirect from it is refused, never followed. A text over 16 KiB is refused with a named reason. On a half read the send falls through to the old pane path unless the recipient's row is known to be an endpoint row, and is refused only when the old path also finds nothing.
- **One tuple.** `registry.ROW_FIELDS` is the one tuple; discovery, the pane pass's change test and the retirement record derive from it (`EXEMPT` names each exception); a completeness test pins it.

## A row with no pane to judge

One predicate, `registry.no_pane_to_judge(row)`, says a row has no pane to judge: its kind is `cloud` or `orchestrator`, or it holds an endpoint and no pane. Every supervising reader asks it before it reads "no pane" as anything: the daemon's pane pass and its missing count, the pane alerts, the dead hint of `status`, `reattach --all-detached`, the heartbeat's wake targets and the reconcile on read. A test derived from the code fails on any function that reads the dead verdict or the missing count without it.

Such a row is **alive** or **unknown**, never dead. The liveness check (`bin/endpoint_liveness.py`) is ON DEMAND (the doctor's charters check; the daemon pass never calls it). It needs a cloud record (`state/cloud/NAME.json`; none: `unknown`, and a local agent of the same name never makes a cloud row alive) and makes one bounded request through it, `fleet-switchboard read NAME --cloud-only` (tunnel and the pod's own password, handled inside that read, never here); it does not read the row's endpoint value. It answers `alive` with the time, or `unknown` for a failed, slow, refused or partial answer, and `unknown` removes nothing. One time limit (`LIVENESS_SECONDS`) bounds the request and the wait.

A charter's `pane:` line may read `cloud:<agent name>`. `fleet-doctor` reports such a charter live when the row's endpoint answers and unknown when it does not, never as a dead pane. A local agent with a pane is judged as before.
