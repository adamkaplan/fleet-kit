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
| `bin/cloud_worker.py:1174` | `pane-health.json` | reads `agents[name]` for the session of a local agent | nobody (reader only) | `cmd_read` of the cloud log read, through `known_agent` | derived (reader) |
| `bin/fleet-switchboard:6512` | `retired.json` | per agent: at, by, reason, session, auto, and `record` (the pane record kept at retirement) | `retire` / `retire --undo`, the daemon's auto-retire on a final report (`pane_health_pass`), `prune_retired` | detection (skips retired), `reattach`, `known_agent`, `retire --undo`, `status`, tester gate (`read_retired_strict`), sweeps, `route` | primary for retirement |
| `bin/cloud_worker.py:1174` | `retired.json` | reads the retired session through `known_agent` | nobody (reader only) | `cmd_read` of the cloud log read | primary (reader) |
| `bin/reassign.py:34` | `retired.json` | uses `read_retired` so a reassign refuses a retired worker | nobody (reader only) | `reassign` / `reassign --undo` | primary (reader) |
| `bin/fleet-switchboard:5066` | `last-reports.json` | per sender: last report state and time, last send per recipient | `record_last_report` (`report`, `send`) | the daemon's retire-on-final-report, `status`, `dead_hint`, fact sources, report fold | primary |
| `bin/dead_hint.py:3` | `last-reports.json` | reads one entry for the dead-agent hint | nobody (reader only) | `dead_hint.hint`, called by the pane pass | primary (reader) |
| `bin/fleet-switchboard:4331` | `boss-overrides.json` | per worker: the boss a reassign set, bound to one session | `reassign` (bin/reassign.py, under a lock) | discovery overlay (`Engine.discover`, headless records), `reattach_caller_problem` | primary (clear machinery) |
| `bin/reassign.py:4` | `boss-overrides.json` | the same file: reads and writes the overrides | `run_reassign` | `overlay` | primary (clear machinery) |
| `bin/fleet-switchboard:3382` | `boss-resolution` | no file: `resolve_boss` and `boss_name` map `reports_to` (or the role alias) to a registered agent from the discovery | nobody | sends, reports, decisions, reattach, pane pass, `steers_into_turn` | derived from discovery (clear machinery) |
| `bin/impact_hold.py:426` | `boss-resolution` | the impact hold resolves a boss through `boss_name`/`resolve_boss` | nobody | impact hold routing | derived |
| `bin/lanes.py:197` | `boss-resolution` | the lane rule resolves a boss | nobody | lane resolution | derived |
| `bin/reassign.py:237` | `boss-resolution` | `reassign` resolves the current boss for consent | nobody | `reassign` | derived |
| `bin/fleet-switchboard:2385` | `discovery.agents` | the agents of the newest `discover()`: name, session, pane, role, charter, repo, workspace, cwd, reports_to | `Engine.discover` (each pass and on demand) | the whole daemon: fact routing, status, pane pass, decisions, chiefs lookup, badges, toasts | primary (the live list), derived from herdr and the v2 service |
| `bin/cloud_worker.py:976` | `discovery.agents` | looks up a local agent by name before routing | nobody | `route` | derived (reader) |
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
| `bin/fleet_health.py:298` | `decisions.json` | reads the held PR and waiting decisions per orchestrator | nobody (reader only) | load figures | derived (reader) |
| `plugins/fleet-decisions-tui/decisions.mjs:9` | `decisions.json` | reads the list for the sidebar | nobody (reader only) | Decisions section | derived (reader) |
| `plugins/fleet-decisions-tui/health.mjs:15` | `decisions.json` | reads the list for the health block | nobody (reader only) | sidebar health view | derived (reader) |
| `plugins/fleet-decisions-tui/tui.tsx:2` | `decisions.json` | the TUI section renders it through decisions.mjs | nobody (reader only) | sidebar | derived (reader) |
| `plugins/fleet-decisions-tui/health.mjs:3` | `pane-health.json` | reads which agents sit in which workspace and their state | nobody (reader only) | sidebar health view (the sidebar's own read of the agent list, from its own installed copy of the plugin: a view) | derived (reader) |
| `bin/cloud_worker.py:101` | `cloud-records` | one record per cloud worker in `state/cloud/<name>.json`: run id, pod, repo, branch | `cloud_worker.cmd_launch`, the cloud reaper and `cmd_retire` (stop) | `route`, the cloud log read (`cmd_read`), `status` | primary for cloud workers; slice 2 mirrors them into the registry |
| `bin/fleet-heartbeat:506` | `heartbeat-agent-list` | `Herdr.tabs_with_agents`: the supervised-pane set from `herdr agent list`. IDENTITY (who exists) derives from the registry in slice 3; BOOKKEEPING (its nudge state and per-pane health in its own `state.json`, `fleet-heartbeat status --json`) stays its own | `fleet-heartbeat` (bookkeeping only) | `fleet-heartbeat status`, `fleet-doctor` through that output | derived: a view of the registry for identity |
| `bin/fleet-heartbeat:825` | `discovery.agents` | the heartbeat's own `Herdr.discover()` of panes, for its per-pane health. IDENTITY from the registry in slice 3; the per-pane health and nudge state are BOOKKEEPING | `fleet-heartbeat` (bookkeeping only) | `fleet-heartbeat` | derived: a view of the registry for identity |
| `bin/switchboard-trial:483` | `decisions.json` | comment and environment only: the trial lab gives the sidebar plugin the daemon's state directory | nobody | nobody (the plugin reads it) | derived |
| `bin/fleet-switchboard:4400` | `headless-records` | launch records `state/headless/<name>.json` and `headless-service.json` (boss per worker) and `headless-route.json` | `launch` (headless), the restart notice | discovery of headless workers, the restart notice, route checks | primary for headless workers |
| `bin/agent_presence.py:25` | `headless-records` | scans `state/headless/*.json` | nobody (reader only) | presence checks | primary (reader) |
| `plugins/fleet-hooks/index.js:407` | `headless-records` | reads the launch record to tell a headless worker | nobody (reader only) | the fleet-hooks plugin | primary (reader) |

Limit of the scan: it matches the record names and the functions that read them in the text of every tracked source under `bin/`, `plugins/` and `cloud/`. A path built from parts, a listing of the state directory or a name held in a variable is not found; the scan can only fail on a name written out. Slice 3's runtime test covers the rest.

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
- `registry.upsert(state_dir, name, agent)`: discovery's and launch's way to add or update an agent. NO CHURN: when nothing changed it writes nothing and returns `changed: false`. An audit line is written only on add, remove, retire and close, never on a no-op.
- The #870 rule: a retired record with no `session` covers nothing. The fields of a pruned pane record come from `retired[name].record`, for `unretire`, `known_agent`, `reattach` and the cloud log read (slice 2 pins each by a test).
- Cloud workers are agents: a record carries `"kind": "cloud"` and the `run_id` the launcher already keeps (`state/cloud/<name>.json`). Views list them like any agent. `retire`, `close` and `reconcile_remove` work on them as on a local agent, the registry change in one step, and the stop of the pod stays the cloud launcher's act. The cloud launcher and reaper are WRITERS (they `upsert` and `retire`); the cloud log read is a READER.

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
rely on the atomic write. LOCK ORDER: the registry lock first, then the boss-record lock (`boss-overrides`), never the reverse.

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
