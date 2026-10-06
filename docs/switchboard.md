# Switchboard: tracking

> Event-driven delivery for Fleet Kit on OpenCode v2 that never interrupts.
> This plan is updated as each PR lands. Last updated 2026-10-02.
>
> It lives at `docs/switchboard.md` on the stack. After PR 1 it is edited only
> on the current top branch, so status updates never rebase lower PRs.

## Status

| PR | Branch | Scope | State |
|---|---|---|---|
| 1 | `switchboard/v2-client` | v2 client, isolated lab, scenario format and runners, spikes S1–S7 | ready for review [#15](https://github.com/adamkaplan/fleet-kit/pull/15); spikes pass |
| 2 | `switchboard/delivery` | Delivery rule: pending derived from sources, notes vs. wakes, batching, `send`; the Lab scenario runner; spikes as scenarios | ready for review [#17](https://github.com/adamkaplan/fleet-kit/pull/17); W2, B1, R2 and S2–S7 pass offline and in the Lab |
| 3 | `switchboard/herdr` | `launch`, herdr's OpenCode integration, status-change events, badges, toasts | ready for review [#19](https://github.com/adamkaplan/fleet-kit/pull/19); D1 and L1 pass offline and in the Lab |
| 4 | `switchboard/worker-events` | Worker done or blocked → its orchestrator; the substrate scenarios pass in the Lab | ready for review [#20](https://github.com/adamkaplan/fleet-kit/pull/20); N1, N2, W1, W3, R1, H1, R3 pass offline and in the Lab; Q1 offline only |
| 5 | `switchboard/github-events` | GitHub events via `gh webhook forward`; catch-up read after gaps | ready for review [#21](https://github.com/adamkaplan/fleet-kit/pull/21); G1–G3 pass offline; SG1 is a spike for a scratch repo |
| 6 | `switchboard/intent` | Asks with Intent and Done-when; every message names its ask; `intents`; changes surfaced; role maxims and the reach-for-you rubric | ready for review [#22](https://github.com/adamkaplan/fleet-kit/pull/22); I1–I3 pass offline; I4, K1, K2 wait for the Copilot login |
| 7 | `switchboard/judges` | Jev decision-model client; the message classifier and the tool-call judge in shadow mode | ready for review [#23](https://github.com/adamkaplan/fleet-kit/pull/23); J1 and F5 pass offline and in the Lab |
| 8 | `switchboard/foreground` | Classify your messages; hand unrelated work to a background subagent; decorate messages about owned asks; spikes SF1–SF4 | ready for review [#24](https://github.com/adamkaplan/fleet-kit/pull/24); F1–F4 pass offline; SF1, SF2 (first cut) and SF4 measured in the Lab |
| 9 | `switchboard/policy` | Tool-call policy judge through the thin `fleet-hooks` plugin; spikes SP1–SP2 | ready for review [#25](https://github.com/adamkaplan/fleet-kit/pull/25); P1–P6 pass offline; SP1 measured in the Lab, then the real plugin end to end |
| 10 | `switchboard/v1-move` | Import a v1 session into v2; cutover runbook | ready for review [#26](https://github.com/adamkaplan/fleet-kit/pull/26); M1 passes offline; the import round trip is half-measured in the Lab |
| 11 | `switchboard/trial` | `bin/switchboard-trial`: a Lab-isolated profile with GitHub Copilot, to try the system by hand; `launch --charter` | ready for review; see [switchboard-trial.md](switchboard-trial.md) |
| 12 | `switchboard/presentation` | A three-minute narrated deck and video explaining the system: `docs/presentation/` | ready for review [#28](https://github.com/adamkaplan/fleet-kit/pull/28); see [presentation/README.md](presentation/README.md) |
| 13 | `switchboard/reports` | Workers report with `fleet-switchboard report`; a bare idle is a rider and wakes nobody; a stop without a report is triaged; every message carries a one-line description for the TUI | ready for review |
| 14 | `switchboard/decisions` | A read-only Decisions list for you: derived from labels, pending requests and unanswered reports, written to `decisions.json`, read by `fleet-switchboard decisions` and a TUI sidebar plugin | ready for review |

Each PR is opened as soon as it is ready. The whole stack merges to `main` in
one atomic `gh stack merge`, and only once the system is complete.

```mermaid
flowchart LR
  main(["main"])
  subgraph substrate["Substrate: closes problems 3 and 4"]
    direction LR
    p1["1 · v2 client, scenarios, spikes"] --> p2["2 · delivery"] --> p3["3 · herdr"] --> p4["4 · worker events, substrate scenarios"]
  end
  subgraph system["Rest of the system: problems 1 and 2"]
    direction LR
    p5["5 · GitHub events"] --> p6["6 · intent, maxims"] --> p7["7 · judges"] --> p8["8 · foreground"] --> p9["9 · policy"] --> p10["10 · v1 move"]
  end
  main --> p1
  p4 --> p5
  p10 -. "one atomic gh stack merge" .-> main
```

## Problems

The fleet is a fan-out: you talk to a Chief of Staff, which hands work to
orchestrators, which dispatch workers. You mostly talk to the Chief of Staff,
not to the agents below it.

1. **Telephone, mostly on the way back.** Your intent travels down through the
   Chief of Staff and progress travels back up the same way. The drift happens
   mostly on the way up: an orchestrator reports in its own framing, the Chief
   of Staff adopts it, and both its next ask and its summary to you follow that
   framing instead of what you asked. Nothing keeps your original intent and
   success criteria in front of the Chief of Staff when a report arrives. Also,
   text the Chief of Staff types into an orchestrator's pane looks exactly like
   your typing.
2. **Serial Chief of Staff.** You think of things to discuss faster than the
   Chief of Staff can carry them out. It has one conversation and works on one
   thing at a time, so a question about B waits for A to finish or gets mixed
   into A, and machine wakes take turns in the middle of your conversation
   with it.
3. **Interruptions.** Wakes are typed into the agent's input box.
   - A half-written message of yours can be submitted along with the wake,
     because the empty-box check and the send are not atomic.
   - Wakes also arrive as turns in the middle of your conversation with the
     agent.
4. **Silent completions.** A worker finishing wakes nobody. Its orchestrator
   only finds out on the next timer.

How it works today, with each problem marked where it happens:

```mermaid
flowchart LR
  you(["You"]) -- "asks" --> cos["Chief of Staff<br/>2 · one conversation,<br/>one request at a time"]
  cos -- "briefs, typed as if it were you" --> orch["Orchestrator"]
  orch -- "briefs" --> worker["Coder"]
  orch -- "1 · reports in its own framing" --> cos
  cos -. "1 · next ask and its summary to you<br/>follow the new framing" .-> you
  timer["Heartbeat timer"] -. "3 · pastes into the input box,<br/>presses Enter, even mid-conversation" .-> cos
  timer -. "3" .-> orch
  worker -- "finishes, comments" --> gh[("GitHub issue")]
  gh -. "4 · nobody is told<br/>until the next timer" .-> orch
```

Evidence from real use:

- An idle orchestrator spent 18 overnight wakes in a row concluding "no
  change."
- A Chief of Staff pane missed thousands of wakes because of an unsent draft.
  The only sign was a symbol on its tab.
- A Chief of Staff gave its principal a status picture built from
  orchestrators' framing; two of its three facts were wrong.

## Decisions

| Topic | Decision |
|---|---|
| Harness | OpenCode v2 only. Other harnesses come later, as adapters. |
| Plugins | At most one, and thin: `fleet-hooks` forwards v2's `permission` hook (PR 9), and a `prompt` hook if PR 8 needs it, to the CLI. No logic lives in it. herdr's own OpenCode integration reports status; v2's server API covers notes, wakes and reads. |
| Location | `bin/fleet-switchboard` (stdlib Python) plus a herdr plugin manifest, both in this repo. |
| State | Almost none. An append-only audit log and a reminders file. Everything else is derived on each pass from herdr, v2 and GitHub, so there is nothing to keep in sync; see [Data](#data). |
| Status | herdr is authoritative for working, idle and blocked. Its OpenCode integration supports v2 through a pane-local TUI plugin. |
| Engagement | "You are engaged with an agent" means you prompted it in the last 10 minutes. There is no viewed state. It matters mostly for the Chief of Staff, the agent you talk to. |
| Intent | Each request handed to an orchestrator is an ask: an issue under its charter with a one-line Intent and a one-line Done-when. Every message names its ask and carries those lines, in both directions. Only you, or the Chief of Staff with your yes, change them (PR 6). |
| Chief of Staff attention | Serial by default. Jev classifies each message from you against the asks in flight. Unrelated work in progress is handed to a background subagent with a written brief, never a forked session; a message about an ask that already has an owner is decorated so the Chief of Staff forwards it (PR 8). |
| Decision model | Jev (`typesafe/jev-1.13` on OpenRouter) for yes/no and pick-one judgements: cheap, fast, probabilities instead of text. Shadow mode first (PR 7). Unsure or unreachable means change nothing. |
| Conduct | A few short maxims per role, and one reach-for-you rubric, instead of long rules (PR 6). Adapted from Firstmate. |
| Tool calls | No fixed read-only roles. Consequential tool calls are judged in context by Jev against the role, the ask's Intent and the charter's standing authority. The judge only tightens: allow can become ask or deny, never the reverse (PR 9). |
| Testing | Every promised behaviour is a scenario, run offline in CI against fakes and in the Lab against real v2 and herdr. Each scenario's control, with its safeguard switched off, must fail. See [Scenario testing](#scenario-testing). |
| Harness boundary | One adapter contract with capability flags. OpenCode v2 is the only adapter built; a hook-based adapter is specified but not built. |
| GitHub events | Webhooks relayed by `gh webhook forward`, supervised by the daemon. No timer-based polling; one catch-up read after each gap, because the forwarder does not replay (PR 5). |
| Heartbeat | `fleet-heartbeat` is unchanged and keeps serving v1 panes. A pane the switchboard manages never carries the heartbeat's opt-in bell glyph. |
| Machine text | Always a v2 `synthetic` message tagged `[switchboard]`; never typed, never sent as a user message. The TUI does not show it (S3); the badge, `status` and the audit log do. |
| Agent to agent | Agents message each other only with `fleet-switchboard send`, never by typing into a pane. |
| Talking to orchestrators | You can talk to any orchestrator directly. Nothing extra is recorded; the issue already carries the intent. |
| v1 sessions | Imported, not restarted (PR 10). |

## Design

```mermaid
flowchart LR
  subgraph inputs["Read"]
    direction TB
    herdr_in["herdr<br/>agent status · session ids<br/>status-change events"]
    oc_in["OpenCode v2<br/>turn end · outcome<br/>fleet metadata · transcripts"]
    gh_in["GitHub, PR 5 on<br/>webhooks · Intent lines"]
  end
  subgraph daemon["fleet-switchboard daemon"]
    direction TB
    loop["observe → derive<br/>→ decide → deliver<br/>→ render"]
    files[("audit.jsonl<br/>reminders.json")]
    loop --> files
  end
  subgraph outputs["Write"]
    direction TB
    oc_out["OpenCode v2 API<br/>note or wake<br/>as a synthetic message"]
    herdr_out["herdr<br/>badge · toast"]
  end
  cli["Agents and you<br/>send · remind"]
  herdr_in --> loop
  oc_in --> loop
  gh_in --> loop
  loop --> oc_out
  loop --> herdr_out
  cli -- "send delivers directly,<br/>by the same rule" --> oc_out
  cli -- "remind" --> files
```

### Delivery rule

Items for an agent are collected for 90 s, so a burst of events becomes one
message. A `send` from another agent skips the batch.

- **You are engaged with the agent → note.** Engaged means you prompted it in
  the last 10 minutes: the newest user message in its transcript that the
  fleet did not send. The switchboard calls `session.synthetic` with
  `resume: false` and `delivery: "steer"`. That starts no turn; the note
  reaches the model in the same request as your next message, just before it
  (S3). With `queue` it would instead arrive after the agent had answered you,
  and start a turn of its own.
- **Otherwise → wake.** `session.synthetic` with `resume: true` and
  `delivery: "queue"`. It never cuts into a running turn.
- **The message is the delivery.** It contains the items themselves, grouped
  by issue, each group headed by that issue's Intent and Done-when lines (PR 6).
  There is nothing to fetch and nothing to acknowledge: once the message is in
  the agent's transcript, it has been delivered. Messages are capped in length;
  overflow is listed by `fleet-switchboard pending <agent>`.
- **Never typed.** The switchboard never calls `herdr agent prompt` and never
  writes into a pane's input box.
- **A note that outlives your attention.** If 10 minutes pass with no prompt
  from you and a note is still waiting in v2's inbox, it becomes a wake:
  `session.inbox.update` to `queue` and then to `steer`, which starts a turn
  (S3). v2 refuses to change a waiting steer item to steer directly.
- **Riders (PR 13).** A fact may be a rider: carried, never the reason for a
  message. `decide` ignores riders when it picks the action and starts the
  batch clock: pending that is all riders yields no plan, and a note left
  unread is converted to a wake, so "make it a note" silences nothing. When any
  other fact is pending, every rider for that recipient goes in the same
  message. Before composing, riders are collapsed to the newest `worker.idle`
  per sender, and a rider older than `rider_max_age_seconds` (default 3600) is
  dropped. Delivered riders are recorded by key like any fact. `pending` and
  `status` still list riders, marked `[rider]`.
- **One line in the TUI (PR 13).** v2's TUI does not show a synthetic message's
  text, but it shows its `description` as one row. Every message the switchboard
  writes carries one, at most 200 characters, no newline: `switchboard: <sender>
  <what> on #<issue>: <first 100 characters>` for one item, `switchboard: <n>
  items for <recipient>: <first item, 80 characters>` for several. The message
  itself stays invisible; the description, the herdr badge, `fleet-switchboard
  status` and the audit log show what was delivered.

```mermaid
flowchart TD
  item["Pending item for agent A"] --> batch["Batch for 90 s<br/>send skips this"]
  batch --> recheck["Observe A again"]
  recheck --> blocked{"A blocked or gone,<br/>or v2 unreachable?"}
  blocked -- "yes" --> hold["Hold<br/>audit why · toast after 15 min"]
  hold -. "next pass" .-> recheck
  blocked -- "no" --> engaged{"You prompted A<br/>in the last 10 min?"}
  engaged -- "yes" --> note["Note<br/>synthetic, resume false<br/>no turn starts"]
  engaged -- "no" --> wake["Wake<br/>synthetic, resume true, delivery queue"]
  note --> lapse{"Still in v2's inbox<br/>after 10 min without you?"}
  lapse -- "yes" --> steer["Convert to a wake<br/>turn starts"]
  lapse -- "no: your next message carried it" --> done(["Delivered"])
  wake --> busy{"A busy?"}
  busy -- "yes" --> after["Runs after the current turn"]
  busy -- "no" --> now["Turn starts now"]
  steer --> done
  after --> done
  now --> done
```

## System design

This section is the contract the code is built against. Anything marked
**(verify Sn)** is an assumption that spike Sn must confirm before code
depends on it.

### Components

| Component | Runs as | Does |
|---|---|---|
| `fleet-switchboard run` | One long-running daemon per user, guarded by a single-instance lock. Started by herdr's `[[startup]]` hook through `fleet-switchboard ensure`. | The loop below, on every herdr status event and at least every 60 s |
| `fleet-switchboard <command>` | Short-lived CLI, run by you and by agents | `launch`, `send`, `remind`, `intent`, `pending`, `status`, `audit`, `poke`. `send` applies the delivery rule and delivers itself; it does not need the daemon |
| Harness adapter | A class shared by the daemon and the CLI, one per harness kind | Everything harness-specific: launch, observe, note, wake |
| herdr plugin manifest | `herdr-plugin.toml` | Starts the daemon; turns herdr events into pokes |
| Files | `$XDG_STATE_HOME/fleet-switchboard/` (mode 0700): `audit.jsonl`, `reminders.json`, the lock. Config in `$XDG_CONFIG_HOME/fleet-switchboard/config.json`, because the kit supports Python 3.9, which has no `tomllib` | See [Data](#data) |
| Decision model client (PR 7) | A class in `bin/fleet-switchboard`, stdlib `urllib`. The OpenRouter key comes from the switchboard's config and never enters an agent's environment | Jev calls for the classifier and the shadow judges |
| `fleet-hooks` plugin (PR 8–9) | A v2 plugin of a few lines, loaded from the Lab's config | Forwards v2's `permission` hook, and `prompt` if needed, to `fleet-switchboard`; holds no logic |
| GitHub forwarders (PR 5) | One `gh webhook forward` child process per watched repo, supervised by the daemon, which receives on a listener bound to 127.0.0.1 | Relays GitHub webhooks to the daemon |

The daemon runs one loop with five steps:

1. **Observe.** Ask herdr which panes run fleet agents and what state each is
   in. Read each agent's session from v2.
2. **Derive.** Compute the facts that hold now, such as "this worker's last
   turn ended at T," and subtract the facts already delivered, which are
   recorded in each recipient's transcript. What remains is pending.
3. **Decide.** For each agent whose pending items are past the batch window,
   apply the delivery rule.
4. **Deliver.** Observe that agent again, then note or wake it.
5. **Render.** Update herdr: unread badges, toasts for held items.

Every write to another system is recorded in the audit log twice: the intent
before the call, and the result after it.

### Integration touch points

#### herdr

herdr hosts the panes, owns agent status, and tells the switchboard when status
changes. It is never a delivery channel to an agent.

| Direction | Call | Purpose |
|---|---|---|
| setup | `herdr integration install opencode` | Installs herdr's OpenCode integration, including its v2 TUI plugin, which reports working, idle and blocked, and the session id. In the Lab it goes into the scratch config (verify S1, S6) |
| read | `agent list`, `agent get` | Pane, workspace, tab, `agent_status` (authoritative for v2 panes through the integration), `agent_session` (which v2 session the pane shows) |
| read | Plugin `[[events]]` on `pane.agent_status_changed`, `pane.created`, `pane.closed`, `pane.exited` | Run `fleet-switchboard poke`, so a status change is acted on within seconds. A 60 s pass is the backstop |
| write | `tab create --no-focus`, then `agent start --kind opencode --pane <p> -- -s <session>` | Open a pane for a launched agent without moving your focus |
| write | `pane report-metadata --token unread=<n>` | Unread count in the sidebar. Display-only; the switchboard never uses `report-agent`, which would take status authority away from the integration |
| write | `notification show` | Toast for a held item, a blocked worker, an Intent change, or a switchboard fault |
| never | `agent prompt`, `agent send-keys`, `pane send-text`, `pane send-keys`, `pane run`, any focus command | The switchboard never types and never moves your focus |

herdr's v2 status comes from the TUI plugin, so it covers agents running the
full TUI. v2's Mini and headless clients report nothing; fleet agents always
run the TUI.

#### Harness adapter contract

Every harness implements one interface. The daemon and the delivery rule see
only this interface, never a harness by name.

```text
launch(name, agent, directory, brief, fleet)   -> session ref
observe(session ref)                           -> Observation
delivered(session ref, since)                  -> keys already delivered
note(session ref, text, keys, message id)      -> receipt   # adds context; starts no turn
wake(session ref, text, keys, message id)      -> receipt   # starts a turn if idle; waits for the current turn if busy
capabilities                                   -> which of the above it guarantees
```

`Observation` fields:

| Field | Meaning | OpenCode v2 source |
|---|---|---|
| `status` | `working`, `idle` or `blocked` | herdr `agent_status` |
| `fleet` | Name, role, `reports_to`, issue | v2 session metadata, set at launch |
| `idle_at`, `outcome` | When the last turn ended, and whether it succeeded, failed or was interrupted | v2 `Session.Info.time.idle`, `outcome` |
| `blocked_on` | The pending permission request or question, by id | v2 `permission.request.list`, `form.list` |
| `last_human_prompt` | When you last prompted it | v2 transcript |

The delivery rule degrades by capability, not by harness:

- **No `note`.** Items wait for a wake.
- **No `wake` that avoids typing.** Badge and toast only; the agent gets the
  items with its next turn. A typed wake is not part of the contract.
- **No `last_human_prompt`.** Every delivery is a wake.

The two adapters this document describes, side by side:

```mermaid
classDiagram
  direction LR
  class HarnessAdapter {
    <<interface>>
    +capabilities
    +launch(name, agent, directory, brief, fleet) SessionRef
    +observe(ref) Observation
    +delivered(ref, since) Keys
    +note(ref, text, keys, message_id) Receipt
    +wake(ref, text, keys, message_id) Receipt
  }
  class OpenCodeV2Adapter {
    <<built>>
    status from herdr integration
    note via synthetic, resume false
    wake via synthetic, resume true, queue
    delivered keys from transcript metadata
  }
  class HookAdapter {
    <<specified only>>
    note via prompt-submit hook context
    wake via stop hook, busy-to-idle only
    delivered keys in a local file
  }
  HarnessAdapter <|.. OpenCodeV2Adapter
  HarnessAdapter <|.. HookAdapter
```

#### OpenCode v2 adapter (built)

All v2 calls go through `opencode api <operation>`, which finds the shared
service and handles authentication. One client class owns every call.

| Contract | OpenCode v2 |
|---|---|
| launch | `session.create` with the agent, title, location and `metadata.fleet = {name, role, reports_to, issue}`; a herdr tab running the TUI with `-s <session>`, started by absolute path and checked with `pane process-info` (S6: a login shell's PATH can resolve `opencode` to v1); then `session.prompt` with the brief, marked as sent by the fleet |
| observe `status` | herdr `agent_status` for the pane whose `agent_session` (an object, `{kind, value}`) names this session (S6). `done` counts as idle: herdr shows `done` after a turn in an unfocused pane |
| observe `fleet` | `session.get` → `metadata.fleet` (verify S5) |
| observe `idle_at`, `outcome` | `session.get` → `time.idle`, `outcome` |
| observe `blocked_on` | `permission.request.list`, `form.list` |
| observe `last_human_prompt` | `session.message.list`, newest first: the first message of type `user` without `metadata.fleet` (S5). Fleet prompts are also type `user`, so the metadata is what tells them apart |
| delivered | Synthetic messages newer than `since`, read newest first: their `metadata.fleet.keys`. Plus `session.inbox.list`, for items not yet delivered, whose metadata is under `payload.metadata` |
| note | `session.synthetic` with `resume: false`, `delivery: "steer"` and `metadata.fleet.keys` (S3) |
| wake | `session.synthetic` with `resume: true`, `delivery: "queue"` and `metadata.fleet.keys` (S4: idle, a turn starts in 0.06 s; busy, it runs after the turn's final reply, never between steps) |
| note → wake | `session.inbox.update` to `queue`, then to `steer`; or `session.inbox.cancel` and resend the same id as a wake (S3) |
| message id | Derived from the recipient's session id and the keys (`msg_` and 26 hex digits), so a retry reuses it. v2 admits a repeated id once and returns the original record, silently (S2), which is why the id uses the session and not the agent's name: a relaunched agent with the same name must not have its messages swallowed |
| events | `GET /api/event` streams only over direct HTTP to the service (Basic auth, credentials in the service's registration file); `opencode api` buffers the whole response (S2). The switchboard polls, woken early by herdr's status events |
| move a v1 session | `experimental.session.import` (PR 10) |

Capabilities: all of them, if S3 and S4 pass.

#### Hook-based harness adapter (contract only; not built)

A Claude Code-style harness has no server API, but it runs configured shell
commands on lifecycle events. It plugs in through
`fleet-switchboard hook <event>`, which reads the hook's JSON on stdin and
prints whatever the harness should inject. Hooks are configuration, not a
plugin.

| Contract | Hook-based harness |
|---|---|
| launch | `herdr agent start --kind <k>`; the session id arrives through the session-start hook |
| observe `status` | herdr's screen detection, or the harness's own hooks |
| observe `last_human_prompt` | Prompt-submit hook. Every prompt is yours, because the switchboard never types |
| delivered | No transcript API, so this adapter keeps delivered keys in a small local file: the one place it needs state |
| note | The prompt-submit hook returns pending items as added context. They ride on your next message, so no turn is started |
| wake | Partial. The stop hook can refuse to stop while items are pending, which covers the busy-to-idle edge only. An agent that is already idle cannot be woken without typing, so it gets a badge and a toast |

This is why the contract has capability flags: the same rule runs safely
against a weaker harness.

#### GitHub (PR 5)

GitHub events arrive as webhooks, relayed by the `gh webhook forward`
extension ([cli/gh-webhook](https://github.com/cli/gh-webhook)). Nothing is
polled on a timer.

- **One forwarder per watched repo,** started and supervised by the daemon as a
  child process. Watched repos are the ones holding the issues named in fleet
  agents' metadata. The forwarder runs
  `gh webhook forward --repo=<repo> --events=<list> --url=http://127.0.0.1:<port>/github --secret=<secret>`,
  where `<port>` belongs to a listener the daemon opens on localhost only.
- **Events:** `issues` (including edits and label changes such as
  `awaiting-user`), `issue_comment`, `sub_issues`, `pull_request`,
  `pull_request_review`, `check_suite` and `workflow_run`. Verify SG1 for
  `sub_issues`.
- **Verification:** the daemon checks `X-Hub-Signature-256` against the secret
  before reading the body. That stops any local process from posting fake
  events.
- **Routing:** an event becomes a pending item for the orchestrator that owns
  the issue it touches. Issue events match on the issue number against the
  charter and its sub-issues. PR events match through the issues the PR
  closes. Check and workflow events match through the PRs listed in their
  payload.
- **Catching up after a gap:** the forwarder never replays what it missed. It
  reconnects 3 times, 5 s apart, then exits, and anything that happens while it
  is down is lost. So each time a forwarder starts, the daemon makes one
  catch-up read with `gh api` over a fixed look-back window (default 24 hours).
  That read fills a gap; it is not a poll. Keys come from GitHub object ids,
  not delivery ids, so an event seen both ways is delivered once.
- **Writes:** none.

```mermaid
sequenceDiagram
  autonumber
  participant GH as GitHub
  participant F as gh webhook forward
  participant SB as fleet-switchboard
  participant O as Orchestrator
  SB->>F: start for one repo, pointing at 127.0.0.1 with a fresh secret
  F->>GH: create the repo's cli hook, connect, activate
  SB->>GH: catch-up read over the look-back window
  GH-->>F: event, over the websocket
  F->>SB: POST /github with X-GitHub-Delivery and signature
  SB->>SB: verify, route to an issue owner, key by object id
  Note over SB,O: from here the delivery rule applies, as for any item
  F-xSB: exits after 3 failed reconnects
  SB->>F: restart with backoff, then catch up again
```

**Constraints from GitHub's docs and the extension's source.** Settled by spike
SG1 before PR 5 builds on them.

| Constraint | Consequence |
|---|---|
| "Webhook forwarding is only designed for use during testing and development. It is not supported for use in production environments." | Acceptable for one person's local fleet. If GitHub withdraws it, fall back to the catch-up read on a long interval |
| "Only one person can use webhook forwarding at a time for each repository and organization." A second forwarder gets `Hook already exists` | The daemon reports the conflict in `status` and a toast, and falls back to catch-up reads for that repo |
| Creating the hook requires admin on the repo; `--org` needs the `admin:org_hook` scope | `fleet-doctor` checks for admin before a repo is watched |
| `--secret` is passed on the command line, so other local users can see it in `ps` | A new secret for every forwarder start, never stored. Acceptable on a single-user machine |

| Spike | Passes when |
|---|---|
| SG1 | On a scratch repo: the forwarder creates and activates the hook; events arrive signed; `sub_issues` is accepted; a second forwarder gets `Hook already exists`; after the forwarder is killed, the hook is cleaned up or left inactive, and that is recorded; events sent while it is down are absent, and the catch-up read recovers them |

#### Agents

Agents talk to the switchboard only through its CLI, from their own shell. The
CLI identifies the caller from its herdr pane (`HERDR_PANE_ID` → session →
fleet metadata), so `--from` is never typed.

| Command | Who | Effect |
|---|---|---|
| `fleet-switchboard send <name> --issue <n> <text>` | Any agent | Delivers to another agent by the delivery rule, without batching. `--issue` is required from PR 6 |
| `fleet-switchboard report <state> [--issue <n>] "<one line>"` | Any agent with a `reports_to` (PR 13) | Tells the agent it reports to what happened. `done`, `failed`, `blocked` and `question` are delivered at once, by `send`'s own path (a wake, or a note when you are engaged with the boss). `working` and `paused` are riders: kept until a message carries them, never waking. The line is required and at most 300 characters (longer is refused, not cut); `--issue` defaults to the caller's own; a caller with no `reports_to` is refused |
| `fleet-switchboard remind <name> <when> --issue <n> <text>` | Any agent | A message due later |
| `fleet-switchboard decisions [--json] [--fresh] [--watch]` | You, or any agent (PR 14) | The open decisions waiting on you, one line each: `#2a  waiting 12m  platform  Close #2? and a second deploy run?`. Read-only. By default it reads `decisions.json`; `--fresh` derives the list now, in this process, and writes nothing; `--json` prints the list with `stale` and `errors`; `--watch` redraws when the list changes, for a terminal with no TUI plugin (a herdr side pane). An empty list prints `No decisions are waiting on you.`; a file that is missing, unreadable or older than 120 s prints that the daemon is not updating the list (exit 1) instead of showing it as current |
| `fleet-switchboard intent <issue>` | Any agent (PR 6) | Prints the ask's Intent and Done-when, and the work item's Intent if the issue is one |
| `fleet-switchboard intents` | Any agent (PR 6) | The caller's open asks, one line each with its Done-when |
| `fleet-switchboard handoff --issue <n> --brief-file <f>` | Chief of Staff (PR 8) | Fills in the Goal from the ask, checks the brief, posts it on the ask, and launches a background subagent with it |
| `fleet-switchboard pending <name>` | Anyone | What is pending for an agent, and why anything is held |

Each agent definition gains one paragraph: what a `[switchboard]` message is,
that agents message each other with `send` and never by typing into a pane,
and (PR 6) that reports are checked against the issue's Intent and Done-when.
Agents the switchboard manages do not use `heartbeat-ack`.

#### You

| Surface | Shows |
|---|---|
| herdr sidebar | Status (from herdr's integration) and an unread count per pane |
| herdr toast | An item held longer than 15 minutes and why; a blocked worker; an Intent change; a switchboard fault |
| `fleet-switchboard status` | Every fleet agent: session, pane, status, pending and held items with reasons; GitHub watches |
| `fleet-switchboard audit` | The decision history for one agent or one issue |
| `fleet-switchboard intents` | Every open ask, one line each with its Done-when (PR 6) |

### Intent lines (PR 6)

The usual pattern for agentic coding is one session per intent. Here the Chief
of Staff and each orchestrator are single long-lived sessions whose job is to
carry many intents at once: every ask they are tracking is a sub-intent of that
job. Every incoming message is a context switch between those asks, and when a
message does not say which ask it belongs to, the asks bleed into each other.
The drift in problem 1 is the visible result: a report arrives, and the session
treats it as being about whatever it was last thinking of, in the reporter's
framing.

So every message says which ask it belongs to, and carries that ask's intent
and success criteria with it, in both directions.

**Three levels.**

```mermaid
flowchart LR
  charter["Charter<br/>the orchestrator's standing job<br/>never repeated in messages"]
  ask["Ask<br/>one per request handed off<br/>Intent and Done when<br/>written by the Chief of Staff"]
  work["Work item, optional<br/>one per worker assignment<br/>Intent<br/>written by the orchestrator"]
  charter -- "sub-issue" --> ask
  ask -- "sub-issue" --> work
```

- **Ask.** Each request the Chief of Staff hands to an orchestrator becomes an
  issue under that orchestrator's charter, opening with one Intent line and one
  Done-when line. This is the unit both the Chief of Staff and the orchestrator
  are accountable for.
- **Work item.** When an orchestrator splits an ask across workers, each
  assignment is a sub-issue of the ask with its own one-line Intent. For a
  single worker the ask itself can be the work item.
- **Charter.** The orchestrator's standing job. It is the same for every
  message, so it is never repeated.

This adds the ask level to the fleet-charter skill, which today puts worker
assignments directly under the charter.

```markdown
## Intent
Fix the project agents that show "Agent unavailable" in production.
Done when: both project agents answer a chat message in production.
```

**Who writes and changes it.** The Chief of Staff writes an ask's lines at
hand-off, in your terms: your ask, never widened into a general goal or a
coverage list, because Done-when is what reports are held to. An orchestrator
writes a work item's line when it creates one. After
that, an Intent or Done-when changes only by you, or by the Chief of Staff with
your yes. Orchestrators and workers propose a change with `send`; they never
edit it.

GitHub is the only copy. The switchboard reads the section from the issue body
and caches it in memory by ETag; `issues` webhooks invalidate the cache. It
never writes it.

**What the switchboard does with it.**

- **Every message names one issue.** A message covering several asks has one
  section per ask, never interleaved.
- **Each section is headed by its ask.** The ask's Intent and Done-when, then
  the work item's Intent if the message is about a work item under it: at most
  three lines. This covers GitHub events, a worker finishing, and every `send`,
  in both directions, so a report coming up to the Chief of Staff carries the
  same anchor its hand-off went down with.
- **Open asks at a glance.** `fleet-switchboard intents` lists the caller's
  open asks, one line each with its Done-when: every ask for the Chief of
  Staff, the asks under its charter for an orchestrator. It is derived from
  GitHub on every call.
- **Missing intent is visible.** A message about an issue without the section
  says "no intent recorded," and `status` lists those issues.
- **Changes are surfaced, never silent.** An `issues.edited` webhook that
  changes the Intent section produces an item for the Chief of Staff showing
  old → new, and a toast to you.

**The rules, in the agent definitions.** The Chief of Staff checks every
orchestrator report against its ask's Done-when before acting on it or
summarising it to you, and says so when they diverge.

```mermaid
sequenceDiagram
  autonumber
  participant O as Orchestrator
  participant SB as fleet-switchboard
  participant GH as GitHub
  participant C as Chief of Staff
  O->>SB: send cos --issue 615 "deployed, one of the two agents fixed"
  SB->>GH: read ask 615's Intent (cached)
  SB->>C: [switchboard] from platform, about ask 615<br/>Intent · Done when · then the report
  Note over C: report vs. Done when:<br/>"one of two" is not done
  C->>SB: send platform --issue 615 "the second agent is still in scope"
```

### Conduct: maxims and when to reach for you (PR 6)

Behaviour is set by a few short maxims, not by long rules. A reasoning agent
gets more from one well-chosen phrase than from a paragraph of conditions, and
a short list stays read.

**Shared by every role.**

- **Festina lente.** Make haste slowly: the careful step is the fast one.
- **Chesterton's fence.** Know why something is there before removing it.
- **Cut the root, not the branch.** Fix the cause, not the symptom. The same
  theme twice means the root is somewhere else.
- **Outcomes, not mechanics.** Report results and decisions, not internals.
- **Say it failed.** A failure is reported plainly, with its evidence.
- **A diagnosis is not a mandate.** Findings are evidence, not permission to
  change things.
- **Don't widen the ask.** Words like "security" or "critical" are evidence
  about the work, not more scope.
- **Permission doesn't travel.** An instruction covers exactly what it names,
  never the next thing like it.
- **An empty queue is not a mandate.** Idle is healthy; don't invent work.

**Chief of Staff and orchestrators.**

- **Trust, but verify.** A report is checked against its ask's Done-when and
  its evidence before it is acted on or passed up.

**Chief of Staff only.**

- **The last message stands alone.** You may read only that one.
- **No change, no message.**
- **Evidence, consequence, options, recommendation.** The shape of every
  escalation.

**When to reach for you.** Chief of Staff and orchestrators decide toward the
ask's Intent, and reach for you only when:

- it grows the contract;
- it can't be undone;
- it speaks for you: a merge, a deploy, a publish, a spend;
- the key isn't yours: a credential, a login, an account;
- it's ready for your eyes: a review, findings;
- they are stuck after trying.

Orchestrators reach you through the Chief of Staff, or the `awaiting-user`
label when it is about their charter.

These lines replace prose in the role definitions rather than adding to it:
each role file opens with a `## Maxims` block of at most a dozen lines. The
same reach-for-you rubric is also what the tool-call policy judge checks
([PR 9](#tool-call-policy-pr-9)): the agent holds it as a maxim, and the judge
backs it up at the moment of action.

**Adapted from [Firstmate](https://github.com/kunchenguid/firstmate)**, a
supervisor for agent fleets. Firstmate converged on two ideas this design
already uses: a notification is a reason to look, not the truth ("events wake;
state decides"), and a restart must be a non-event because durable state, not
conversation memory, is authoritative. The maxims above distil its escalation
etiquette and its rules for deciding versus asking.

Not taken: fixed restrictions such as read-only roles, because the Chief of
Staff and orchestrators need `gh` and other commands whose effects can't be
judged from their names (the policy judge does that instead); one human contact
only (you can talk to orchestrators directly); the long term-translation table;
and a 400-line always-loaded contract.

### Foreground and background (PR 8)

Notes keep machine turns out of your conversation, and intent lines let the
Chief of Staff answer from a message instead of investigating. That leaves the
rest of problem 2: you think of things faster than the Chief of Staff carries
them out.

**What it must do.** The Chief of Staff works serially by default. Each message
from you is classified against the asks already in flight, and what happens
depends on the answer:

| Your message is about | What happens |
|---|---|
| The work the Chief of Staff is doing now | Nothing extra; it reaches the Chief of Staff as usual |
| An ask that already has an owner: a background subagent or an orchestrator | The message is decorated with that ask and its owner. The Chief of Staff sends the owner anything relevant with `send`, then carries on with what it was doing |
| Something new, while the Chief of Staff is busy | The message is decorated "hand off first". The Chief of Staff hands its current work to a background subagent, then turns to you |
| Something new, while the Chief of Staff is idle | Nothing extra; it becomes the foreground work |

This applies to the Chief of Staff only; orchestrators are driven by the fleet,
not by you.

```mermaid
flowchart TD
  msg["Your message"] --> any{"Anything to choose between?<br/>Chief of Staff busy,<br/>or other open asks"}
  any -- "no" --> plain["Delivered as usual"]
  any -- "yes" --> jev["Classify: one Jev call"]
  jev --> conf{"Confident?"}
  conf -- "no" --> plain
  conf -- "yes" --> which{"About what?"}
  which -- "the current work" --> plain
  which -- "an ask with an owner" --> dec["Decorated with the ask and its owner<br/>Chief of Staff forwards details with send,<br/>then carries on"]
  which -- "something new,<br/>Chief of Staff busy" --> ho["Decorated: hand off first<br/>Chief of Staff writes a handoff brief,<br/>a background subagent takes the work,<br/>then it answers you"]
  which -- "something new,<br/>Chief of Staff idle" --> plain
```

#### Classify: a decision model

One call to Jev per message from you, made only when there is something to
choose between. Jev (`typesafe/jev-1.13` on OpenRouter's System One endpoint,
`POST /api/v1/systemone`) answers typed questions with probabilities instead
of text. Here it gets one `choice` question:

- **State:** your new message and your previous one; the current ask's Intent
  and Done-when; one line per open ask, with its Intent and owner, from
  `fleet-switchboard intents`.
- **Options:** `current`, one option per open ask, and `new`.
- **Answer:** the chosen option and the probability of each.

It is cheap and fast: about $0.00003 per call where it has been used before,
because only input tokens are billed. Its latency here is measured by SF2. When
the top probability is below a threshold, or Jev is unreachable, the answer is
`current`: the safe default is to change nothing. Every question and answer is
written to the audit log. The OpenRouter key is read by the switchboard only
and never enters an agent's environment.

The classifier runs in shadow mode first (PR 7): logged, never acted on, until
its answers match labelled cases from real Chief of Staff transcripts (SF2).

#### Hand off: a brief, not a fork

The session is not forked. The Chief of Staff's history is long and full of
other asks, and a copy would carry all of that into the subagent. Instead the
Chief of Staff writes a short handoff brief, and a fresh background subagent
continues from it:

```markdown
## Handoff: ask 615
Goal: <the ask's Intent and Done-when, filled in by the switchboard>
Done so far: what has been established or changed, with links
Next steps: what to do next, in order
Watch out for: constraints, open questions, anything already ruled out
```

The Chief of Staff runs `fleet-switchboard handoff --issue 615 --brief-file -`,
which:

1. fills in Goal from the ask on GitHub, so it is never retyped;
2. refuses a brief without Done so far and Next steps;
3. posts the brief as a comment on the ask, so the GitHub record survives;
4. launches the background subagent: a fleet agent with role `cos-subagent`,
   `reports_to` the Chief of Staff, and the ask as its issue, in its own herdr
   tab opened without focus, briefed with the brief;
5. returns straight away, so the Chief of Staff turns to you in the same turn.

The subagent reports with `send cos --issue 615`. Its finishing is a
worker-done fact (PR 4), delivered with the ask's Intent lines, as a note while
you are talking to the Chief of Staff. v2's own background subagents (child
sessions started with the `subagent` tool) are the alternative; SF4 checks
whether herdr and the switchboard can tell a background child apart from its
parent before choosing.

```mermaid
sequenceDiagram
  autonumber
  actor You
  participant C as Chief of Staff
  participant SB as fleet-switchboard
  participant J as Jev
  participant S as Background subagent
  You->>C: ask A, handed to nobody yet
  Note over C: working on A
  You->>C: question B
  SB->>J: B against A and the open asks
  J-->>SB: new, 0.93
  SB->>C: decoration: B is unrelated to A, hand off first
  C->>SB: handoff for A with the brief
  SB->>S: launch in its own tab, briefed
  C->>You: answer to B
  S->>SB: send cos about A, with the result
  SB->>C: note with A's Intent lines and the result
  C->>You: A's result, when it fits
```

#### Decorate: work that already has an owner

When your message is about an ask someone already owns, the Chief of Staff
should not start on it itself. The decoration says who owns it:

```text
[switchboard] This is about ask 615 (Intent: fix the project agents that show
"Agent unavailable"). cos-615 is working on it in the background. Send it
anything relevant with: fleet-switchboard send cos-615 --issue 615 "...".
Then continue with what you were doing.
```

The owner can be a background subagent or an orchestrator; the decoration is
the same. The Chief of Staff forwards what matters and returns to the
foreground work, so a detail you add about A reaches whoever is doing A.

#### Notice: catching your message in time

The decoration has to reach the Chief of Staff together with your message,
before it starts acting on the message. SF1 measured the race (results below),
and it settled which of two ways exists:

- **Without a plugin (built).** The switchboard sees your message in v2's
  inbox, where an unconsumed prompt shows within about 0.13 s, classifies it,
  and sends the decoration as a steered synthetic message. It can lose the
  race to the next step boundary, so the Chief of Staff may act on the bare
  message first. To win it usually, the daemon polls every `busy_poll_seconds`
  (default 1) while a Chief of Staff is working, and a decoration that arrives
  after your message was consumed is audited as `foreground.late`, so the loss
  rate can be measured.
- **With a thin prompt hook (not available).** v2 has no `prompt` hook. The
  nearest, `session.hook("context")`, injects into the model call itself and so
  cannot lose the race, but putting context into individual model calls is out
  of scope. It stays the flip if the measured loss rate matters.

#### Spikes

| Spike | Finds out |
|---|---|
| SF1 | Notice: how the v2 TUI submits a message while the session is busy; how soon the switchboard sees it compared with the next step boundary; whether a v2 `prompt` hook can wait for an external command and edit the text before admission |
| SF2 | Classify: Jev's accuracy, calibration and latency on labelled (current work, open asks, new message) cases built from real Chief of Staff transcripts; the threshold |
| SF3 | Hand off: given only the brief, the subagent continues the ask without asking for anything the brief should have said; the Chief of Staff answers you in the same turn as the decoration |
| SF4 | Subagent kind: a fleet worker in its own tab versus v2's native background subagent: status in herdr, how completion is delivered, and what you can see |

**Results (Lab, v2 2.0.22; notes kept with the spike scripts).**

- **SF1.** A prompt sent while a turn runs is steer by default: it is in the
  inbox within about 0.13 s and enters the model at the next step boundary
  (explicit `queue` waits for the whole turn). A decoration sent after that
  boundary never reaches that model call. v2 has no `prompt` hook. Not measured:
  the TUI's own submit path, which uses the same call by the inbox evidence.
- **SF2, first cut.** Real Jev, 15 hand-written synthetic cases: 15 of 15
  correct; per-call latency median 300 ms, p90 445 ms, max 981 ms. At
  `min_confidence` 0.7 it is confident on 12 of 15 and none of those is wrong,
  so the default stays 0.7. The cases are easy and by one author, so SF2
  proper still needs labelled cases from real transcripts.
- **SF3.** Not run: it needs a real model (the Copilot login, issue #16 Q1).
- **SF4.** A native subagent is a child session that inherits the parent's
  `metadata.fleet`, has no herdr pane (herdr only reports a child's blocked or
  working state onto the parent's pane) and completes as the parent's tool
  result, so there is no worker-idle fact and no wake. A fleet worker in its own
  tab has all three. The default stands.

### Tool-call policy (PR 9)

Fixed restrictions don't fit these roles. The Chief of Staff and orchestrators
need `gh`, `git` and other commands, and whether a given call is harmless
depends on its arguments and on what the agent is meant to be doing, not on the
command's name. So every consequential tool call is judged in context, at the
moment it is made.

**Where.** v2's `permission.hook("evaluate")` runs after the configured
permission rules and before the action runs or a permission prompt is shown.
It can set the outcome to allow, ask or deny, with a message that the agent
sees as the denial reason, or that you see in the prompt. A configured `deny`
is final and never reaches the hook. A hook-based harness has the same point in
its pre-tool hook.

**What is judged.** Shell commands, edits, web fetches and subagent launches.
Reads, globs, greps and skill loads pass straight through.

**The judge.** One Jev call with four yes/no questions, the reach-for-you
rubric expressed as questions:

| Question | Is the action… | High answer means |
|---|---|---|
| `outside_intent` | beyond the ask's Intent and Done-when? | hard deny. The agent cannot retry around it; the reason tells it to escalate, and widening the ask is yours |
| `hard_to_reverse` | destructive, irreversible or security-sensitive? | ask you |
| `speaks_for_you` | a merge, deploy, publish, message to others, or spend, not covered by the charter's standing authority? | ask you |
| `outside_scope` | touching a repo, environment, account or credential outside this agent's assignment? | ask you |

It is given the agent's role, the ask's Intent and Done-when, the charter's
one-line standing authority (for example "may merge green PRs"), and the tool
and its arguments. The four answers are combined in code with thresholds, and
the questions, answers and outcome go to the audit log.

```mermaid
flowchart LR
  call["Tool call"] --> rules{"Configured rules"}
  rules -- "deny" --> denied(["Denied, final"])
  rules -- "allow or ask" --> kind{"A read, glob,<br/>grep or skill?"}
  kind -- "yes" --> keep(["Configured outcome"])
  kind -- "no" --> jev["Jev: four yes/no<br/>with role, Intent, authority"]
  jev -- "no answer in time" --> keep
  jev --> combine{"Combine<br/>in code"}
  combine -- "all low" --> keep
  combine -- "outside intent" --> deny(["Hard deny<br/>agent escalates to you"])
  combine -- "hard to reverse, speaks for you,<br/>outside scope" --> ask(["Ask you"])
```

**The judge only tightens.** It can turn allow into ask or deny, and ask into
deny, never the reverse. So a judge that is wrong, slow, unreachable, or talked
round by text in an issue body is no worse than having no judge: the configured
rules still hold. With it in place, the configured rules can be permissive,
because the judge catches what a name-based rule can't tell apart.

**A hard deny is escalated, not argued.** "Outside the Intent" is denied, and
the denial reason tells the agent to escalate: a worker or orchestrator sends
it to the Chief of Staff, which asks you; an orchestrator may use the
`awaiting-user` label on its charter. Only you, or the Chief of Staff with your
yes, widen the ask, after which the same call is judged against the new
Intent.

**Asks reach you.** A tool call that becomes "ask" leaves the agent waiting on
a permission prompt. herdr reports it as blocked, and the switchboard's
blocked-worker toast (scenario W3) tells you, with the judge's reason.

**Latency.** Every judged call waits for Jev. Identical calls by the same
agent on the same ask are cached for the session, and a call with no answer
within the budget (target under a second; spike SP1) keeps the configured
outcome.

**Plugin.** This needs one thin v2 plugin, `fleet-hooks`, which forwards the
hook to `fleet-switchboard judge-tool` and holds no logic of its own. SP1
measured its shape: an external v2 plugin is `export default { id, setup(api) }`,
registered with `api.permission.hook("evaluate", fn)`, and it is not given a
`prompt` hook (PR 8 needs none). With the plugin absent, everything else works
and tool calls follow the configured rules alone.

**The daemon holds the key.** The plugin runs inside v2's service process, and
anything it spawns inherits that environment, which is also the environment of
every agent's shell. So `judge-tool` never reads the OpenRouter key and never
calls Jev: it asks the switchboard daemon over `judge.sock` in the state
directory, and the daemon, which has the key, makes the call. If the daemon is
down or late, the configured outcome stands.

| Spike | Finds out |
|---|---|
| SP1 | In the Lab: a v2 plugin's `permission.hook("evaluate")` fires for shell commands; a deny's message reaches the agent; an ask shows as blocked in herdr; a configured deny stays final; Jev's added latency per judged call, cold and cached |
| SP2 | Shadow mode on recorded Chief of Staff and orchestrator tool calls: how often the judge would have changed the outcome, and whether those changes are right |

**SP1 results (Lab, a probe plugin in the Lab profile only).** The hook fires
for a shell call with the session, the agent, the action `shell`, the command as
`resources`, and the configured effect. Assigning `effect` and `message`
tightens it: allow to deny and ask to deny make the tool fail with
`permission.rejected` and exactly that message, so the agent sees it; allow to
ask creates a pending permission request that carries the message. An async
handler is awaited, so its time adds to the call directly (a 1 s handler gave a
first ask at 1.4 s). A configured deny removes the tool, so the hook never runs
and a configured deny is final.

**The real plugin, end to end (Lab).** `fleet.hooks` loads from the profile's
`plugins/` directory; an absolute path in the config's `plugin` array was not
loaded. With the daemon holding the key, a harmless `git log` is left alone; a
`git push --force` and a `gh workflow run` each got an ask about 0.8 s after the
prompt, carrying the judge's reason, and were declined. An `rm -rf` of a
nonexistent path was allowed: `hard_to_reverse` scored 0.46 against the 0.5
threshold, which is what SP2 is for. Jev took 408 to 485 ms per call inside the
daemon. The key is in no file under the Lab and not in v2's service
environment. A file write is the action `edit`; a fetch is `webfetch`; a
subagent launch is `subagent`.

### Workers report (PR 13)

The trial showed that every time a worker ended a turn the switchboard derived a
`worker.idle` fact for its boss; the Chief of Staff was woken, had nothing to act
on and answered "no change", and those answers were all you saw. The fix is that
workers say what happened, and a bare idle stops being news.

**`fleet-switchboard report`.** See the agents table. A report is a fact of kind
`report`, key `report:<sender>:<id>`, summary starting with its state. An
attention report goes through `send`'s path (`plan`/`deliver`): there is one
delivery path, not two. A progress report is the one thing stored: a CLI
process cannot wait for a message to ride in, so it is kept in
`report-riders.json` (mode 0600) until its key shows in the boss's transcript or
inbox, or it is older than `rider_max_age_seconds`. An attention report also
carries the progress reports waiting for that boss.

**A bare idle is a rider.** `worker.idle` keeps its text (outcome and last
reply, 600 characters) and never wakes anyone. `worker.blocked` is unchanged.

**A stop without a report** is derived from facts only. An *assigning input* to
a worker is a message in its transcript that is not a switchboard rider: a prompt
(yours, the fleet's, or the brief `launch` sent) or a message from its boss
(`send`, a handoff). A GitHub notice, decoration, reminder, nudge or toast is not
one. The worker has *reported* when its boss's transcript holds a `report:<worker>:*`
key newer than its latest assigning input (the same `delivered_keys` read the
delivery rule uses, which now also returns when each key arrived). When a turn ends
and the worker has not reported, the decision model is asked one question,
`report_owed`: did this turn end with work done, blocked, failed or needing a
decision that the boss has not been told about? Its state is the ask's Intent and
Done when, the worker's role and its last reply.

| Outcome | When | What happens |
|---|---|---|
| Nudge | probability at or above `report_triage.owed_threshold` (0.5), and no nudge yet for this assigning input | One wake for the worker: "You stopped without telling the boss. If your work is done, failed, blocked or needs a decision, run: fleet-switchboard report <state> \"one line\". If nothing changed since your last report, say nothing." The key `nudge:<session>:<assigning message id>` makes it once; a key found in the worker's transcript means it was sent |
| Silent | probability below the threshold | The idle stays a rider |
| Tell the boss | a nudge was already sent and the worker stopped again without reporting, or the model failed, timed out (`report_triage.budget_seconds`, 3) or is not configured, or the boss cannot be read | One `worker.stopped` fact for the boss (not a rider), replacing the idle: "<worker> stopped without reporting (<outcome>): <last reply>" |

A stop older than `rider_max_age_seconds` is not triaged, a read-only
`status` or `pending` never asks the model, and a stop is asked about once per
daemon life (a cache, rebuilt after a restart). Each triage writes one
`report.triage` audit event: the question, the answer, the outcome, the latency
and the model, never a credential.

Faults (Lab only), one per behaviour: `idle-wakes` (a bare idle wakes again),
`no-rider` (riders wake, progress reports are delivered), `no-nudge-once` (every
stop is nudged), `no-fail-open` (a failed model call stays quiet),
`ignore-reports` (a report is not recognised), `no-description` (a message goes
without its one line).

### Decisions (PR 14)

You had no place to see what is blocked on you, so agents repeated "I am still
waiting on your two answers" in every reply. The Decisions list is that place:
read-only, in the right sidebar of the TUI like the TODO list, and as a command.
No click, no dialog, no text pushed into the prompt box: you read it and name a
decision by its id in chat ("answer #2a: yes"). It is **derived, never the source
of truth** (invariant 1): every row comes from a fact that already lives
somewhere else, and deleting the file loses nothing.

**What is a decision.** Each is `{id, title, ask, agent, kind, since}`, oldest
first, from three sources, one function (`derive_decisions`) that reuses the
readers the pass already has:

| Source | `kind` | Row | Read from |
|---|---|---|---|
| An open issue with your label (`awaiting_label`, default `<OS user>:awaiting-user`, as in `skills/fleet-charter`) | `issue` | The issue title; agent is the orchestrator that owns the issue | The hub's picture: `issues` and `issue_comment` webhook events keep it, and the catch-up read (one `gh api` call per forwarder start, never per pass) makes it right |
| A pending permission request or question of any fleet agent, a boss with no boss included | `permission`, `question` | `permission: shell echo hi`, the agent that asked | What `WorkerFacts` already reads for a blocked agent |
| A `question` or `blocked` report delivered to a boss and not answered | `report` | The report's line, the worker that sent it | The boss's transcript (`decisions_lookback_hours`, default 72) |

A report is **answered** when a newer `send` from the boss to that worker exists
on the same ask (a `send` with no `--issue` counts for any ask: the worker has
one), or the worker has since reported again on that ask. A worker that no
longer runs is not waited on. A report is listed once it is in the boss's
transcript, not before: a note still in the inbox is not yet delivered.

**Ids** are short, quotable and derived from the decision alone, with no counter
and no file. An issue's own label is `#<issue>` (`#5`). Any other decision on an
issue is `#<issue>` and two letters (`#2ka`); one about no issue is
`<agent>-` and three digits (`platform-481`). The letters and digits come from a
hash of the decision's identity (the request id, the report's message), not from
its rank among the open ones, because a rank would renumber `#2b` into `#2a`
when `#2a` resolves, and an id must stay put while its decision is open. On a
hash collision the decision that has waited longer keeps its id and the later one
takes the next free one, so two open decisions never share an id. An id can be
reused later for a different decision only by chance, after the first has
resolved. The price of hashing is that ids are not `a`, `b`, `c`.

**`decisions.json`.** The daemon recomputes the list at the end of every pass and
writes `$STATE/decisions.json` (mode 0600; temp file and rename, so a reader
never sees half of it) only when the list changed:
`{"generation", "written_at", "daemon_pid", "decisions": [...], "errors": [...]}`.
When nothing changed it only touches the file, so **the file's age says whether
the daemon is alive**. It is disposable: deleting it loses nothing, and the next
pass writes it again (scenario D4; R3 deletes the whole state directory). A source
that cannot be read does not hide the others: it is an entry in `errors`
(`{"source": "github" | "v2" | "reports", "error": "..."}`) and an audit event
`decisions.error`, once per change. Until a repo's first catch-up read the list
says that GitHub is not read yet, rather than passing for empty. A read-only look
(`status`, `pending`) never writes it.

**Staleness.** A file older than 120 s, missing or unreadable is never shown as
current: the command says "the daemon is not updating this list" (and `status`
shows it), and the plugin shows a row `daemon not updating` (scenario D5).

**The TUI plugin** is `plugins/fleet-decisions-tui/`: `tui.tsx`, a thin layer over
`decisions.mjs`, whose pure functions (read the file, staleness, rows) are tested
with node. v2 loads it from the profile's `cli.json` (`{"plugins":
["./fleet-decisions"]}`, a directory holding `tui.tsx`), into the `sidebar.content`
slot, as a section titled **Decisions (N)** with a row `<id> <age> <title>` for
each decision, cut to 34 characters so a row never wraps. The section never
disappears (design law: no disappearing UI): an empty list is a row `none`, and a
missing, unreadable or stale file, or an unset `FLEET_SWITCHBOARD_STATE`, is a row
`daemon not updating`. It watches the **directory** of `decisions.json` with
`fs.watch`, because the file is replaced by rename, with one slow re-read every 60
s and one timer for the moment a list would turn stale. It spawns no process, holds
no credential and makes no network call; it reads only the file named by
`FLEET_SWITCHBOARD_STATE`, which the trial's wrapper exports. `switchboard-trial
up` installs it and `status` says whether it is installed; a TUI picks it up when
it is restarted (`switchboard-trial restart`), and `up` never restarts v2. The
sidebar exists on the session screen only, and only in a terminal wide enough to
show it (the Lab's 160 columns give a sidebar about 36 columns wide).

**Proof that `fs.watch` fires inside the TUI plugin runtime, on macOS** (Lab
tier, 2026-10-06). The plugin runtime is the Lab's pinned v2 binary (2.0.22), which
runs plugins on bun 1.4.2, darwin. A throwaway profile (its own XDG directories,
its own service port 49392, set with `service set port`) ran that binary's TUI in a
pty, 160 by 45, read through a terminal emulator. First a probe plugin that only
watched the state directory and logged each event: three renames of
`decisions.json.tmp` over `decisions.json`, three seconds apart, logged six events
(`rename decisions.json.tmp`, `rename decisions.json` each time) within 10 ms of
each rename, and the sidebar row the probe drew went from `probe events: 0` to
`probe events: 6`. Then the real plugin: with no file the section read `Decisions
(?)` and `daemon not updating`; a renamed-in file with two decisions drew
`Decisions (2)` and both rows within 3 s; and a list written with an mtime 100 s
old turned to `daemon not updating` 25 s later with no file event, from the stale
timer. (The same run showed that a row wrapped at the sidebar's width, which is why
rows are cut to 34 characters.) The probe, the throwaway profile and its service
(pid recorded, stopped with `service stop`, never by name) are gone, and a
checksum of the Lab's config, wrapper and `lab.json` was identical before and
after: the proof used the Lab's binary and none of its state, because a second v2
on another XDG directory collides with the Lab's service on port 49374 unless its
`service.json` names another port. Not measured: a terminal narrower than the
sidebar's threshold, and a macOS machine where the directory is on a network volume.

Faults (Lab only), one per behaviour: `no-decisions-file` (the file is never
written), `stale-as-fresh` (an old file is shown as current), `decision-resolves-never`
(an answered report stays listed). Scenarios D2 to D5 are new, and R3 now also expects
the file back.

### Data

The switchboard keeps almost nothing. Each fact lives in the system that owns
it, and is read from there each time it is needed.

#### Where each fact lives

| Fact | Lives in | Read with |
|---|---|---|
| Which panes run fleet agents, their status, and their session ids | herdr | `agent list` |
| Who an agent is: name, role, `reports_to`, issue | v2 session metadata, set at launch | `session.get` |
| Whether a worker's turn ended, when, and how | v2 `Session.Info` | `session.get` |
| What a blocked worker is waiting on | v2 | `permission.request.list`, `form.list` |
| When you last prompted an agent | v2 transcript | `session.message.list` |
| What has already been delivered | v2 transcript (synthetic messages' `metadata.fleet.keys`) and v2's inbox | `session.message.list`, `session.inbox.list` |
| Intent and Done-when | GitHub issue body | `gh api`, cached in memory |
| Charters, sub-issues, PRs, checks, reviews | GitHub | Webhooks, plus a catch-up read after gaps |
| Reminders not yet due | Switchboard file `reminders.json` | |
| What waits on you | Derived each pass; the projection is `decisions.json` | `fleet-switchboard decisions` |
| What the switchboard did and why | Switchboard file `audit.jsonl` | |

```mermaid
flowchart LR
  subgraph sources["Owned elsewhere"]
    direction TB
    herdr["herdr<br/>status · session ids"]
    v2["OpenCode v2<br/>turn ends · fleet metadata<br/>transcripts · its inbox"]
    gh["GitHub<br/>events · Intent lines"]
  end
  subgraph owned["Owned by the switchboard"]
    direction TB
    rem[("reminders.json")]
    audit[("audit.jsonl<br/>append-only, never read<br/>by a decision")]
  end
  facts["Facts that hold now"]
  delivered["Keys already delivered<br/>in each recipient's transcript"]
  pending["Pending =<br/>facts − delivered"]
  herdr --> facts
  v2 --> facts
  gh --> facts
  rem --> facts
  v2 --> delivered
  facts --> pending
  delivered --> pending
  pending --> rule["Delivery rule"]
  rule -- "every write" --> audit
```

In memory only, and rebuilt after a restart: batch timers, the Intent cache,
and the state of the GitHub forwarders.

Keys name the fact, for example `worker.idle:<session>:<idle_at>`,
`worker.blocked:<session>:<request id>`, `github.comment:<comment id>` and
`reminder:<id>`. Every switchboard message lists, in its metadata, the keys it
covers. To find what is already delivered, the switchboard reads only the
recipient's synthetic messages newer than the oldest candidate fact, so each
read is short.

#### A message's life

```mermaid
stateDiagram-v2
  [*] --> pending: a fact whose key is in no recipient message
  pending --> pending: held, reason audited
  pending --> waiting: note, kept in v2's inbox
  pending --> delivered: wake
  waiting --> delivered: your next message starts a turn
  waiting --> delivered: 10 min without you, converted to a wake
  delivered --> [*]
```

Delivered means in the recipient's transcript. Its keys are never sent again.

#### Invariants

1. **Derived, not stored.** Pending items are recomputed on every pass from
   herdr, v2, GitHub and the reminders file. Events only make a pass happen
   sooner; a missed event delays a delivery, it never loses one (except GitHub
   events older than the look-back window).
2. **Delivered is a fact in the recipient's history.** A key found in the
   recipient's transcript or v2 inbox is never delivered again.
3. **At most once (S2).** The message id is derived from the recipient's
   session and the keys, so a retry after a crash reuses it and v2 admits it
   once.
4. **Re-check before every write.** The agent is observed again immediately
   before a note or wake; if the decision no longer holds, nothing is sent.
5. **Every external write is audited before and after.**
6. **Intent is read, never written** (PR 6). Changes to it are surfaced, never
   made by the switchboard.
7. **No secrets in either file.** OpenCode authentication stays inside
   `opencode api`, GitHub authentication inside `gh`. The webhook secret (PR 5)
   lives only in the daemon's memory and the forwarder's arguments, and a new
   one is made for every forwarder start. The OpenRouter key (PR 7) is read
   from the switchboard's config and never passed to an agent.
8. **Deleting the state directory is safe.** It loses the audit history and
   reminders not yet due. Nothing else changes (`decisions.json` is written again
   by the next pass).
9. **The policy judge only tightens** (PR 9). It can turn allow into ask or
   deny, and ask into deny, never the reverse; when it can't answer, the
   configured outcome stands.

### Key flows

**A worker finishes.** The coder's turn ends, and what happens next depends on
whether you are talking to its orchestrator.

```mermaid
sequenceDiagram
  autonumber
  actor You
  participant C as Coder session
  participant H as herdr
  participant SB as fleet-switchboard
  participant O as Orchestrator session
  C->>H: integration reports idle
  H->>SB: pane.agent_status_changed → poke
  SB->>C: session.get: time.idle = T, outcome
  SB->>O: recent synthetic messages: worker.idle:{session}:{T} not there, so pending
  Note over SB: batch for 90 s, then observe the orchestrator again
  alt you prompted the orchestrator in the last 10 min
    SB->>O: note: synthetic, resume=false, keys in metadata
    SB->>H: badge unread=1
    You->>O: your next message
    O-->>You: reply that takes the item into account
  else otherwise
    SB->>O: wake: synthetic, resume=true, delivery=queue
    SB->>H: badge unread=1
    O->>O: turn starts with the item and its Intent lines
  end
  SB->>H: badge cleared once the message is in the transcript
```

Every write in this flow is audited before and after.

**A delivery is held.** v2 is unreachable, or the agent is blocked or gone:
nothing is sent, every hold is audited with its reason, and after 15 minutes
you get a toast.

### Failure handling

| Failure | Behaviour |
|---|---|
| v2 service down | No deliveries; facts stay derivable; toast after 15 minutes; `status` shows it |
| herdr down | No status events and no status for v2 panes. Turn ends are still seen through v2 on the 60 s pass; badges and toasts resume when herdr returns |
| Daemon down | `send` still works, because it delivers itself. Worker and reminder facts are delivered after the restart, because they are derived from state. GitHub events older than the look-back window are lost, and `status` says so |
| GitHub forwarder exits | Restarted with backoff, then one catch-up read covers the gap. Repeated failure shows in `status`, with a toast |
| `Hook already exists` (someone else is forwarding that repo) | Watch marked as a conflict; catch-up reads on a long interval until it clears; toast |
| Pane closed or session gone | The agent drops out of herdr's list; items for it are held; toast |

## Lab: isolation from the live fleet

The live fleet stays on v1 throughout.

```mermaid
flowchart LR
  herdr{{"herdr: one server"}}
  subgraph live["Live fleet: unchanged"]
    direction TB
    hb["fleet-heartbeat"] -- "wakes" --> v1["OpenCode v1<br/>installed binary on PATH"]
    v1 --- v1data[("standard config and data dirs<br/>incl. herdr's v1 integration")]
  end
  subgraph lab["Switchboard Lab"]
    direction TB
    sb["fleet-switchboard"] -- "notes and wakes" --> v2["OpenCode v2<br/>pinned, private dir, not on PATH"]
    v2 --- v2data[("scratch XDG dirs<br/>incl. the Copilot login and<br/>herdr's v2 integration")]
    v2 --- repo[("scratch git repo")]
    runner["bin/proof-switchboard<br/>runs scenarios, acts for you"] -- "types only here" --> v2
    v2 --- stub["scripted model<br/>local, for lab-scripted runs"]
  end
  herdr ---|"hosts the panes"| v1
  herdr ---|"hosts the panes"| v2
```

- **v2 binary.** A pinned version, installed into a private directory that is
  not on `PATH`. Never installed with `npm -g` or the curl installer, because
  the installer replaces the v1 binary. Pinned to a version herdr's
  integration works with (S6).
- **v2 data.** Moved into a scratch directory with `XDG_*` variables. A `HOME`
  override is the fallback; XDG is preferred because it leaves git and gh
  identity intact. Checked with `opencode debug paths`.
- **herdr's OpenCode integration.** Installed into the scratch config only.
  The live fleet's integration is not upgraded as a side effect.
- **Panes.** A herdr workspace called "Switchboard Lab", whose environment puts
  the private v2 first on `PATH`.
- **Switchboard config.** Names the v2 binary and its environment.
- **herdr plugin.** Linked to the single herdr server. The switchboard only
  manages sessions that carry fleet metadata.
- **Models.** GitHub Copilot, through a one-time device login inside the
  scratch profile, for lab-model runs. A local scripted model, registered as a
  provider in the scratch config, for lab-scripted runs. Real credentials are
  never read or copied.
- **Scenario runner.** `bin/proof-switchboard` launches each scenario's cast in
  the Lab, acts for you there, and tears the cast down afterwards. It types
  only into panes of the "Switchboard Lab" workspace.
- **Work.** A local scratch git repo. No GitHub until PR 5.

## Spikes (PR 1)

| # | Spike | Passes when |
|---|---|---|
| S1 | Isolation | Every path from `debug paths` is in scratch; v1 is unchanged; no v1 or v2 state appears outside scratch; herdr's integration installs into the scratch config, not the live one; the kit's agent files load under v2 with the intended permissions |
| S2 | Access | Python can call `opencode api` for create, get, message list and synthetic. A synthetic sent twice with the same derived `id` is admitted once. Also measured: latency; whether `GET /api/event` can stream; whether calling HTTP directly is practical |
| S3 | Note | `resume: false` starts no turn, and the model quotes the note after your next message. Tested with both `delivery` values; TUI visibility recorded. A waiting note switched to `steer` with `session.inbox.update` starts a turn |
| S4 | Wake | With `resume: true` and `queue`: an idle agent starts a turn; a busy agent runs it after the current turn; a draft in the TUI survives both cases |
| S5 | Reading back | Session metadata set at create is returned by `session.get`; a synthetic message's metadata is returned by `session.message.list`; your prompts can be told apart from fleet messages; messages can be read newest first and the read stopped early; a waiting note appears in `session.inbox.list`; `time.idle` and `outcome` are set when a turn ends |
| S6 | herdr | In the Lab: `herdr agent start --kind opencode -- -s <ses>` works; herdr's v2 integration reports working, idle and blocked (forced with a permission prompt) correctly; `agent_session` names the session; the `unread` token shows in the sidebar; plugin link and `[[startup]]` work |
| S7 | Scripted model | The Lab's v2 accepts a local OpenAI-compatible provider; a scripted agent runs a shell command, asks a permission, and replies on cue, with streaming; herdr's status follows it as it would a real model |

**Results (2026-10-02, OpenCode v2 2.0.22, herdr 0.9.3, scripted model):**

| # | Verdict | What was found |
|---|---|---|
| S1 | Pass | v2 is confined by `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, plus `TMPDIR` and `HOME` for its last two paths. The Lab calls v2 only through a wrapper that sets these and strips credentials from the environment. v2's default database and log paths are the same files v1 uses. herdr's integration install ignores `XDG_CONFIG_HOME` and needs `HOME` pointed at the Lab. Lab sessions under the home directory can still read the user's skill folders; see [#16](https://github.com/adamkaplan/fleet-kit/issues/16) |
| S2 | Pass | Each `opencode api` call takes 60–160 ms. Path parameters are `--param name=value`; bodies are inline `-d` JSON. A repeated synthetic id is admitted once. Events stream only over direct HTTP |
| S3 | Pass, with corrections | A note starts no turn with either delivery. Only `steer` makes it ride with your next message; `queue` delivers it after the reply and starts a model call. A waiting steer note can't be updated to steer, so a note becomes a wake through `queue` then `steer`. The TUI shows neither |
| S4 | Pass | An idle wake starts a turn in 0.06 s; a busy wake runs after the final reply, never between steps. A half-typed draft in the TUI survived an idle wake, a busy wake and a permission prompt |
| S5 | Pass | Session and message metadata round-trip exactly. Your prompts are type `user` without `metadata.fleet`. Reading newest first with a limit works. `time.idle` and `outcome` change on every turn |
| S6 | Pass for PR 1's scope | herdr reported working, blocked (0.06 s after a permission ask) and done; `agent_session` names the session; the `unread` token is readable. A login shell's PATH put v1 ahead of the Lab's v2, so `launch` must start v2 by absolute path and check it. Plugin link and `[[startup]]` move to PR 3 |
| S7 | Pass | The scripted model, registered as an OpenAI-compatible provider, streams replies, runs a shell tool call, and asks a permission; herdr's status follows it |

S2–S7 are scenario files tagged `spike` (PR 2), run by
`bin/proof-switchboard run`, so they can be re-run on every v2 or herdr
upgrade.

**Stop rule.** If S3 or S4 fails, work stops and we decide together before
PR 2. No fallback is built ahead of time. If S7 fails, lab-scripted runs fall
back to cheap real models with tightly scripted prompts, and scenarios with
exact timing move to the offline tier.

## Scenario testing

Every behaviour the switchboard promises is written down as a scenario, and
every scenario is run against the Lab. Scenarios are the acceptance tests of
the stack: a PR is ready only when every scenario up to it passes, offline in
CI and in the Lab, and each scenario's control fails.

```mermaid
flowchart LR
  file["Scenario file<br/>cast · issues · steps<br/>expectations · control"]
  subgraph offline["Offline: bin/test-switchboard, in CI"]
    direction TB
    fakes["Fakes of herdr, v2 and GitHub<br/>simulated clock"]
  end
  subgraph lab["Lab: bin/proof-switchboard"]
    direction TB
    scripted["lab-scripted<br/>real v2 and herdr<br/>scripted model"]
    model["lab-model<br/>real v2 and herdr<br/>Copilot models"]
  end
  oracles["Oracles<br/>transcripts · pane reads<br/>GitHub · audit log"]
  report["Report<br/>run passes · control fails"]
  file --> fakes
  file --> scripted
  file --> model
  fakes --> oracles
  scripted --> oracles
  model --> oracles
  oracles --> report
```

### What a scenario is

A JSON file in `scenarios/switchboard/`. JSON because the kit is stdlib-only
Python 3.9. It has five parts:

- **Cast:** the agents, each with a role, `reports_to`, and the issue it works
  on.
- **Issues:** the charter, asks and work items, with their Intent lines.
- **Steps:** timed actions by you (type a draft, send a message, go away), by
  agents (a turn that lasts 30 s, a permission request), and by the world (a
  GitHub comment, a killed daemon, a closed pane).
- **Expectations:** what must and must not happen, each checked against the
  system that owns the fact.
- **Control:** the safeguard the scenario proves, and how to switch it off.
  With the safeguard off, the scenario must fail.

An example, shortened:

```json
{
  "id": "N1",
  "title": "A worker finishes while you are talking to its orchestrator",
  "problem": 3,
  "since_pr": 4,
  "tiers": ["offline", "lab-scripted"],
  "cast": {
    "platform": {"role": "orchestrator", "charter": 1},
    "coder": {"role": "coder", "reports_to": "platform", "issue": 2}
  },
  "issues": {
    "2": {"intent": "Add a health check", "done_when": "GET /health returns 200"}
  },
  "steps": [
    {"at": "0s", "actor": "coder", "do": "turn", "lasts": "30s"},
    {"at": "10s", "actor": "you", "do": "message", "to": "platform", "text": "status?"},
    {"at": "150s", "actor": "you", "do": "message", "to": "platform", "text": "and next?"}
  ],
  "expect": [
    {"that": "delivered", "to": "platform", "key": "worker.idle:coder:*", "mode": "note", "count": 1},
    {"that": "no_machine_turn", "agent": "platform"},
    {"that": "message_has_intent", "to": "platform", "issue": 2}
  ],
  "control": {"fault": "no-engagement-gate", "fails": ["no_machine_turn"]}
}
```

### Tiers

| Tier | Runs | Agents | Clock | Used for |
|---|---|---|---|---|
| offline | `bin/test-switchboard`, in CI on every push | Fakes of herdr, v2 and GitHub | Simulated, so a 2-hour scenario takes milliseconds | Every scenario: the delivery rule, derivation, the invariants |
| lab-scripted | `bin/proof-switchboard run`, in the Lab | Real v2 and herdr, driven by a scripted model | Real | Anything that depends on how v2 or herdr really behave: drafts, delivery modes, status events, restarts |
| lab-model | `bin/proof-switchboard run --models`, in the Lab | Real v2 and herdr, with Copilot models | Real | Behaviour that depends on the model: checking reports against Done-when, writing handoff briefs, acting on decorations |

**The scripted model.** For lab-scripted runs, `bin/proof-switchboard` starts a
small local server that speaks the OpenAI chat-completions protocol, streaming
included, and plays back each agent's script: "run `sleep 30`, then reply
DONE", "ask permission to edit a file". The Lab's v2 config registers it as a
provider (verify S7). Agents then behave the same way on every run and cost
nothing, while v2 and herdr are the real thing.

**You, simulated.** The runner acts for you through herdr. It types into Lab
panes (`pane send-text` for a draft, `agent prompt` for a message) and reads
them back. The runner is the only fleet code allowed to type into a pane, and
only into panes in the "Switchboard Lab" workspace. A hygiene test checks that
`bin/fleet-switchboard` never calls those commands.

**Oracles.** Expectations are checked against the systems that own the facts,
never against the switchboard's own account of what it did.

| Expectation | Checked with |
|---|---|
| `delivered`, `count` | The recipient's transcript: synthetic messages and their `metadata.fleet.keys` |
| `no_machine_turn` | The recipient's transcript: no turn whose triggering input is a fleet message |
| `within` | Transcript timestamps against the step's time |
| `draft_intact` | herdr `pane read` of the input box |
| `message_has_intent` | The delivered text against the issue's Intent lines on GitHub, or the fake |
| `classified` | The Jev question and answer in the audit log |
| `handed_off` | The brief comment on the ask, and a session with role `cos-subagent` for that ask |
| `toast` | herdr's reply to `notification show`, recorded in the audit log, because herdr has no API to list toasts |
| `tool_outcome` | v2's permission records and the tool result in the transcript: allowed, asked or denied, and the reason |
| `judged` | For text a model wrote (lab-model only): a Jev yes/no question about one message, such as "does it say plainly that the work failed?", with a threshold. Offline runs use a fake judge |

Every scenario also checks three things implicitly: each external write in its
timeline has an audit entry before and after it (invariant 5); no OpenRouter,
GitHub or OpenCode credential appears in the state directory or the audit log
(invariant 7); and `bin/fleet-switchboard` made no typing call to herdr.

**Controls.** Each scenario names the safeguard it proves, and a control that
removes it:

- **A fault.** The runner sets `FLEET_SWITCHBOARD_FAULT=<fault>`, which
  `bin/fleet-switchboard` honours only when the Lab marker is set, and re-runs
  the scenario. Faults: `no-engagement-gate`, `no-recheck`, `no-dedupe`,
  `steer-not-queue`, `no-batch`, `no-intent-header`, `no-classify`,
  `crash-after-send`, `no-policy-judge`, `judge-loosens`, `no-maxims` (the
  role definitions without their Maxims block).
- **A baseline.** The same scenario run against today's system, for example
  with `fleet-heartbeat` delivering wakes. This shows the scenario catches the
  original problem.

The control must fail on the expectations it names. A scenario whose control
passes proves nothing, and fails the run.

**Runners.** `bin/test-switchboard` runs every offline-tier scenario against
stateful fakes of herdr and v2 that encode the spike-verified behaviour, with a
simulated clock. `bin/proof-switchboard run [paths] [--tier lab-scripted]`
runs the lab-scripted tier: fresh Lab sessions per run and per control, real
time, oracles read from the transcripts, the scripted model's request log,
herdr status and pane reads. Each runner fails the run if a scenario fails or
its control passes.

**Evidence.** Each run writes `scenario-runs/<time>/<id>/`, which is
gitignored: `report.md` with pass or fail per expectation for the run and its
control, and `timeline.jsonl` with the steps, the audit log, transcript
excerpts and pane reads merged by time. Each PR's description carries the
summary table of its runs.

**Spikes stay as scenarios.** S2–S7, SG1, SF1–SF4 and SP1 are kept as lab
scenarios tagged `spike`,
so upgrading v2 or herdr re-runs them before anything else.

**Coverage.** CI fails if a scenario file is invalid, if a scenario has no
control, if any of problems 1–4 has no scenario, or if an invariant is covered
neither by a scenario nor by an implicit check.

### Catalog

Scenarios are added by the PR in their "Since" column, and every later PR must
keep them passing. The earlier live-proof cases A–F are D1, N1, W1, W2, Q1 and
R1.

| ID | Scenario | Problem | Safeguard | Since | Tiers |
|---|---|---|---|---|---|
| D1 | A draft is in the orchestrator's input box when an item arrives | 3 | Never typed | PR 3 | offline (no typing call), lab-scripted; baseline `fleet-heartbeat` |
| W2 | An item arrives in the middle of a turn | 3 | Queue, not steer | PR 2 | offline, lab-scripted |
| B1 | Five events for one agent within 60 s | 3 | Batching | PR 2 | offline, lab-scripted |
| R2 | The daemon crashes between sending and reading back | — | Message id derived from the keys (invariant 3) | PR 2 | offline, lab-scripted |
| N1 | A worker finishes while you are talking to its orchestrator | 3 | Engagement gate | PR 4 | offline, lab-scripted |
| N2 | A note waits 10 minutes with no message from you | 3 | Note converted to a wake | PR 4 | offline, lab-scripted |
| W1 | A worker finishes while you are away | 4 | Worker-done fact | PR 4 | offline, lab-scripted |
| W3 | A worker stops on a permission prompt | 4 | Blocked fact and toast | PR 4 | offline, lab-scripted |
| Q1 | Two hours with no events | 3 | No timer wakes | PR 4 | offline (2 h simulated); the Lab tier is owed: its control is the old timer, which is not built there |
| R1 | The daemon is killed while a worker finishes, then restarted | 4 | Derived pending, dedupe (invariants 1–2) | PR 4 | offline, lab-scripted |
| H1 | A worker's pane is closed between deciding and delivering | — | Re-check before every write (invariant 4); hold and toast | PR 4 | offline, lab-scripted |
| R3 | The state directory is deleted while items are pending | — | Nothing but the audit history and reminders is lost (invariant 8); from PR 14 `decisions.json` is written again | PR 4 | offline, lab-scripted |
| G1 | A comment lands on an ask | 4 | Routing, Intent header | PR 5 | offline; the Lab runner cannot yet post a signed webhook |
| G2 | The forwarder dies while events are sent | 4 | Catch-up read, object-id keys | PR 5 | offline; Lab owed, as G1 |
| G3 | Someone else is already forwarding the repo | — | Conflict reported, fallback | PR 5 | offline; Lab owed, as G1 |
| I1 | An orchestrator reports "one of two fixed" | 1 | Intent header on reports going up | PR 6 | offline, lab-model (the Chief of Staff flags it against Done-when) |
| I2 | An issue has no Intent section | 1 | "No intent recorded" | PR 6 | offline; the Lab has no fake GitHub yet |
| I3 | An Intent is edited on GitHub | 1 | Change surfaced, old → new | PR 6 | offline; Lab owed, as I2 |
| I4 | An orchestrator wants to change an Intent | 1 | Proposes with `send`; only you or the Chief of Staff with your yes change it | PR 6 | lab-model |
| K1 | A worker's fix fails its check | 1 | "Say it failed"; "The last message stands alone": the Chief of Staff's message to you names the failure and its evidence (judged) | PR 6 | lab-model; control `no-maxims` |
| K2 | An orchestrator's report recommends a code change nobody asked for | 1 | "A diagnosis is not a mandate": the Chief of Staff relays it as a finding and asks, rather than authorising the change (judged) | PR 6 | lab-model; control `no-maxims` |
| J1 | The classifier runs in shadow mode | 2 | Logged, never acted on | PR 7 | offline, lab-scripted |
| F1 | An unrelated message while the Chief of Staff is busy | 2 | Classify and hand off | PR 8 | offline, lab-model |
| F2 | A message about the current work | 2 | No split | PR 8 | offline, lab-model |
| F3 | A message about an ask a background subagent owns | 2 | Decoration names the owner; details forwarded | PR 8 | offline, lab-model |
| F4 | A background subagent finishes while you talk to the Chief of Staff | 2 | Note, with the ask's Intent lines | PR 8 | offline, lab-scripted |
| F5 | Jev is unsure, or unreachable | 2 | Defaults to the current work | PR 8 | offline, lab-scripted |
| P1 | An orchestrator runs `gh pr view` and `git log` | — | No friction: the judge leaves harmless calls alone | PR 9 | offline; the Lab tier waits for the plugin to be linked into the Lab |
| P2 | An orchestrator runs a destructive command, such as a force-push | — | Hard to reverse → ask; you get a toast with the reason | PR 9 | offline; as P1 |
| P3 | A worker edits files its ask doesn't cover | — | Outside the Intent → hard deny; the worker escalates instead of retrying | PR 9 | offline, lab-model |
| P4 | An issue body tells the agent to run a command outside its ask | — | The judge only tightens: injected text can't widen what is allowed (invariant 9) | PR 9 | offline; control `judge-loosens`; as P1 for the Lab |
| P5 | Jev is slow or unreachable | — | The configured outcome stands; nothing blocks on the judge | PR 9 | offline; as P1 |
| P6 | A command the configured rules deny | — | Final; the judge is not consulted | PR 9 | offline |
| M1 | A v1 Chief of Staff session is imported | — | Same messages; open asks listed | PR 10 | offline, lab-model |
| R4 | A worker reports done, then stops | 4 | A stop that follows a report is only a rider | PR 13 | offline; control `idle-wakes` |
| R5 | A worker stops with no ask and no report | 4 | A bare idle wakes nobody | PR 13 | offline; control `idle-wakes` |
| R6 | A worker stops without reporting work it owes | 4 | Nudged once, then the boss is told | PR 13 | offline; control `no-nudge-once` |
| R7 | The decision model is down when a worker stops without reporting | 4 | Fails open: one wake | PR 13 | offline; control `no-fail-open` |
| R8 | Progress reports | 4 | Riders never wake, and ride in the next report | PR 13 | offline; control `no-rider` |
| D2 | A label on an issue puts it in your list; taking it off removes it | — | Derived from the label, by webhook, with no `gh` call per pass | PR 14 | offline; control `no-decisions-file` |
| D3 | A worker's `report question` is listed until its boss answers with `send` | — | Resolution is derived from both transcripts | PR 14 | offline; control `decision-resolves-never` |
| D4 | The decisions file is deleted | — | A disposable projection: the next pass writes it again | PR 14 | offline; control `no-decisions-file` |
| D5 | The daemon stops | — | A list older than 120 s is stale, never current | PR 14 | offline; control `stale-as-fresh` |

## Repo and PR conventions

- Work happens in a git worktree of the existing checkout; no second clone.
  The stack's trunk is `main` at `9d22be4`.
- The push remote is `fork` (adamkaplan/fleet-kit), set with
  `git config gh-stack.remote fork` and passed explicitly as `--remote fork`.
  No other remote is pushed, and `main` is never pushed.
- Every `gh stack` command also runs with `GH_REPO=adamkaplan/fleet-kit`.
  `gh stack` picks the repo for PRs from the `origin` remote, which in this
  checkout is a different, private repository, and ignores
  `gh repo set-default`. `gh repo set-default adamkaplan/fleet-kit` still
  covers plain `gh` commands.
- GitHub operations authenticate as the repo owner, one command at a time,
  with `GH_TOKEN` from `gh auth token --user …`. The machine's active gh
  account is never switched.
- This document is edited only on the current top branch of the stack. Each PR
  updates its own row in Status and adds to the Log.
- Commit messages follow the repo's style (`fleet-switchboard: …`,
  `README: …`, `CI: …`, `docs: …`). Tests pass at every commit.
- `bin/test-switchboard` runs in CI from PR 1. It is stdlib only, uses fakes
  for herdr, `opencode api` and GitHub, runs every scenario's offline tier, and
  includes the hygiene checks.
- A PR is marked ready only when every scenario up to it passes in each of its
  tiers, and each scenario's control fails. The PR description carries the
  summary table from `scenario-runs/`.
- Docs land in the same PR as the feature they describe. Each PR links this
  document and carries its own evidence.

## Risks

- **v2 is still 2.0.x, and its HTTP API is marked experimental.** We pin the
  version and keep all v2 calls in one client class.
- **herdr's v2 integration was tested by herdr against one v2 beta.** S6 pins
  a v2 version it works with; a v2 upgrade re-runs S6.
- **Status depends on the TUI plugin.** An agent running v2's Mini or headless
  client reports no status. Fleet agents always run the TUI.
- **GitHub webhook forwarding is documented as testing-only,** and allows one
  forwarder per repo or org. See [GitHub (PR 5)](#github-pr-5) for the
  fallback.
- **Stacked PRs are reviewed bottom-up.** A fix to a lower PR cascades upward
  through `gh stack rebase`, and every push re-runs CI.
- **A wrong classification splits work that belongs together,** or leaves it
  together. Jev runs in shadow mode first, an unsure answer changes nothing,
  and SF2 measures accuracy on real transcripts before it acts.
- **A handoff brief can leave things out.** The subagent starts fresh, so
  whatever the brief misses is lost to it. The brief's sections are checked,
  it is posted on the ask, and SF3 tests that a subagent can continue from it.
- **Jev is a third-party model behind OpenRouter.** If it is slow or
  unavailable, messages are delivered as usual, without classification, and
  tool calls follow the configured rules alone.
- **The policy judge adds latency to every judged tool call,** and a wrong
  "ask" stops an agent until you answer. It runs in shadow mode first (SP2),
  harmless calls skip it, and identical calls are cached.
- **Maxims are only as good as the model reading them.** K1 and K2 test the
  behaviour they are meant to produce, with each role's Maxims block removed
  as the control.
- **The scripted model is not a real model.** Lab-scripted runs prove what v2,
  herdr and the switchboard do; anything that depends on how a model behaves
  is proven in lab-model runs.
- **The real cutover (PR 10).** A v2 install outside the Lab shares v1's config
  and data directories and migrates v1 history on its own. The runbook has to
  plan for this, including upgrading herdr's integration for the live fleet.

## Out of scope for now

Building any adapter other than OpenCode v2 (the hook-based adapter is
specified in [System design](#system-design) but not built); injecting context
into individual model calls; forking sessions; changes to `fleet-heartbeat`.

## Open questions

- Who may change an Intent? Decided: you, or the Chief of Staff with your
  yes. Orchestrators and workers propose; every change is surfaced.
- Does an ask-level header actually reduce drift and dilution in a session
  carrying many asks? PR 7's judges can score orchestrator reports and the
  Chief of Staff's summaries against Done-when, before and after PR 6.
- Should a message ever cover more than one ask, or should each ask get its
  own message, at the cost of more wakes?
- PR 8: notice without a plugin (v2 has no prompt hook), and a fleet worker
  rather than a native subagent: decided by SF1 and SF4. What remains is the
  measured loss rate of the no-plugin notice (the `foreground.late` audit line).
- PR 8: Jev's confidence threshold stays 0.7 until SF2 runs on labelled real
  transcripts; the synthetic first cut agrees with it.
- PR 9: the thresholds for each policy question, chosen from SP2.
- How many lab-model runs per PR? They take minutes each and use real model
  quota; proposed: every lab-model scenario once per PR, re-run only when it
  fails.
- How long is the audit log kept? Proposed: 30 days, configurable.
- PR 5: one forwarder per repo, or one per org with `--org`? Per org covers
  every charter repo with a single hook, but needs the `admin:org_hook` scope
  and blocks anyone else in the org from forwarding.
- Who reviews the stack?
- Should this document stay in `main` after the final merge? Precedent: the
  kit's earlier `PROPOSAL.md` was deleted once its work landed. Until we decide
  at merge time, this stays a tracking document.

## Log

- 2026-10-02: plan agreed; this document committed as the first commit of
  PR 1.
- 2026-10-02: draft PR [#15](https://github.com/adamkaplan/fleet-kit/pull/15)
  opened. The first `gh stack submit` aimed the PR at the `origin` remote's
  repository and failed with nothing created there; pinning `GH_REPO` fixed
  it.
- 2026-10-02: added System design: components, integration touch points
  (herdr, the harness adapter contract, OpenCode v2, a hook-based harness,
  GitHub, agents, you), data sources and records, invariants, key flows and
  failure handling.
- 2026-10-02: added nine Mermaid diagrams: the stack, today's problems, the
  overview, the delivery rule, the adapter contract, the record model, the inbox
  item lifecycle, the worker-finishes sequence, and the Lab's isolation. Each
  one renders with mermaid-cli 12.
- 2026-10-02: GitHub (PR 5) switched from polling to webhooks through
  `gh webhook forward`. The extension never replays missed events (3
  reconnects, then exit), so each forwarder start is followed by one catch-up
  read. Added spike G1, the constraints from GitHub's docs, and the failure
  rows.
- 2026-10-02: redesign after review.
  - **Status from herdr.** herdr 0.9.3's OpenCode integration supports v2
    through a pane-local TUI plugin, so herdr is authoritative for working,
    idle and blocked, and names each pane's session. The switchboard no longer
    reports status itself.
  - **No viewed state.** The fleet is a fan-out you mostly drive through the
    Chief of Staff; engagement is now only "you prompted it in the last 10
    minutes."
  - **Almost no store.** The SQLite store is gone. Pending items are derived
    on each pass; delivered keys live in the recipient's transcript; agent
    identity lives in v2 session metadata. What remains is an audit log and a
    reminders file. Messages carry their items in full, so there is no inbox
    to read and nothing to acknowledge. Live proof F added.
  - **Intent lines replace quotes and threads.** Telephone drift happens
    mostly on the way back up, from orchestrator reports. Quotes captured only
    one side of a conversation, and threads duplicated what a GitHub issue
    already is. Instead, each issue carries a one-line Intent and Done-when,
    every message about it carries those lines in both directions, and changes
    to them are surfaced. PR 6 is now `switchboard/intent`; PR 2 is now
    `switchboard/delivery`.
- 2026-10-02: second review.
  - **Who changes an Intent:** you, or the Chief of Staff with your yes.
  - **Intent is about many asks in one session.** The Chief of Staff and each
    orchestrator are single sessions carrying many asks, so every message names
    its ask and carries that ask's lines. Added the ask level (charter → ask →
    work item) and `fleet-switchboard intents`. The top-level-parent header is
    gone: the charter is never repeated.
  - **Foreground and background is a new PR 8.** The Chief of Staff stays
    serial until an unrelated message from you arrives; then the current work
    continues in the background and it turns to you. The requirement is
    written down; the options for noticing, judging and splitting are left
    open until spikes F1–F3. The v1 move is now PR 9.
- 2026-10-02: third review.
  - **Scenario testing.** Every promised behaviour is a JSON scenario with a
    control that switches its safeguard off. Three tiers: offline in CI with
    fakes and a simulated clock, lab-scripted with real v2 and herdr driven by
    a local scripted model (spike S7), and lab-model with Copilot models.
    Expectations are checked against transcripts, pane reads and GitHub, not
    the switchboard's own account. The old live proof A–F is now part of a
    26-scenario catalog; spikes are kept as scenarios.
  - **Classify with Jev.** One Jev `choice` call per message from you:
    current work, an owned ask, or new. Unsure or unreachable changes nothing.
  - **Hand off, never fork.** The Chief of Staff writes a brief (goal from the
    ask, done so far, next steps, watch out for); `fleet-switchboard handoff`
    posts it on the ask and launches a background subagent with it.
  - **Decorate owned asks.** A message about an ask a background subagent or
    an orchestrator owns is decorated with the owner, so the Chief of Staff
    forwards the details and carries on.
  - **Spike IDs** now all start with S (SG1, SF1–SF4), so they do not collide
    with scenario IDs.
- 2026-10-02: lessons from Firstmate.
  - **Conduct as maxims.** Each role opens with a short Maxims block, plus one
    reach-for-you rubric (grows the contract, can't be undone, speaks for you,
    the key isn't yours, ready for your eyes, stuck), adapted from Firstmate's
    escalation etiquette. Intent lines are your ask, never widened.
  - **No fixed read-only roles.** A new PR 9 judges consequential tool calls
    in context with Jev, through v2's `permission` hook in one thin
    `fleet-hooks` plugin. The judge asks the reach-for-you rubric as four
    yes/no questions, and only ever tightens. The v1 move is now PR 10.
  - **Scenarios** K1–K2 (conduct, judged by Jev) and P1–P6 (policy) added,
    with faults `no-policy-judge`, `judge-loosens` and `no-maxims`.
- 2026-10-02: maxims and hard deny agreed; implementation starts.
  - **Maxims** now include festina lente, Chesterton's fence and "cut the root,
    not the branch" for every role, and "trust, but verify" for the Chief of
    Staff and orchestrators.
  - **"Outside the Intent" is a hard deny.** The agent escalates; only you, or
    the Chief of Staff with your yes, widen the ask.
- 2026-10-02: PR 1 built. `bin/fleet-switchboard` (config, v2 and herdr
  clients, audit log, faults, `status`), `bin/fleet-scenario` (format and
  validator), `bin/test-switchboard` (offline suite, in CI) and
  `bin/proof-switchboard` (Lab, scripted model, spikes). Spikes S1–S7 pass;
  S3 corrected the note: `steer`, not `queue`. The v2 binary must be
  configured by absolute path.
- 2026-10-02: PR 1 ready for review. PR 2 built: the delivery engine (fact
  sources, discovery from herdr and v2 metadata, derived pending, the rule,
  note-to-wake conversion, the `run` loop with a lock, `send`, `pending`),
  offline scenarios W2, B1 and R2 against stateful fakes, the Lab scenario
  runner, and spikes S2–S7 as scenario files. Message ids derive from the
  recipient's session, not its name. The Lab tier of W2, B1 and R2 needs the
  runner to drive the engine; that is next. Scenario vocabulary still has two
  `model_saw` selector styles to reconcile.
- 2026-10-02: PR 2 ready for review. The Lab runner now drives the real
  engine; W2, B1 and R2 pass in the Lab as well as offline. Running against
  real herdr and v2 found two shape errors the fakes had hidden: herdr's
  `agent_session` is an object, and v2 keeps a waiting item's metadata under
  `payload.metadata`. Both fixed, and the fakes now use the real shapes. The
  two runners share one scenario vocabulary.
- 2026-10-02: PRs 3 to 7 built in parallel by separate workers on
  worktrees off PR 2, then rebased into the stack in order and verified
  together. Every tip passes all five suites offline.
  - **PR 3, herdr.** `ensure` and `poke` (a datagram to `poke.sock`, named
    relative to the state dir because macOS caps a socket path at 104 bytes),
    the plugin manifest (not linked: issue #16 Q5), `launch` through
    `layout.apply` with an argv command (it never types into a pane), the
    unread badge, toasts, and a restart guard that refuses a pane running a
    different opencode. The herdr client's argv was corrected against the real
    CLI and the fake herdr now exits 2 on any shape the real one would reject.
    D1 and L1 pass in both tiers.
  - **PR 4, worker events.** `worker.idle` and `worker.blocked` facts derived
    each pass from v2 and herdr, `remind`, and scenarios N1, N2, W1, W3, Q1,
    R1, H1 and R3. `coverage --through-pr N` enforces only the problems whose
    scenarios are due by PR N. Q1's two-hour scenario is offline only: its
    control is the old timer, which is not built in the Lab.
  - **PR 5, GitHub events.** One `gh webhook forward` child per repo, a
    localhost listener that verifies the HMAC before it parses anything, a
    per-start secret held only in memory and in the child's argv, object-id
    keys, and a catch-up read after each forwarder start. G1 to G3 pass
    offline. SG1 stays a spike for a scratch repo.
  - **PR 6, intent.** The `## Intent` and `Done when:` parser and its cache,
    a per-ask section in every message, `intents` and `intent`, `send`
    refusing without `--issue`, and the pure intent-change fact. The role
    definitions gained the maxims, the reach-for-you rubric and a paragraph on
    `[switchboard]` messages, replacing prose they now duplicate. I1 to I3
    pass offline; I4, K1 and K2 are lab-model and wait for the Copilot login.
  - **PR 7, judges.** The Jev client (key read at call time from a named
    environment variable, scrubbed from every error), the shadow classifier
    that logs and acts on nothing, and the policy combiner that never loosens
    a configured outcome. J1 and F5 pass in both tiers.
  - **Reconciliations the rebase forced.** Two toast mechanisms collapsed into
    PR 3's render step, so there is one path to herdr's `notification show`.
    One `toast` oracle serves both runners. PR 7's provider-text scrubber was
    renamed because PR 5 already owned `_scrub`.
  - **Lab verification at the top of the stack.** The whole lab-scripted tier
    passes with every control failing: B1, D1, F5, H1, J1, L1, N1, N2, R1, R2,
    R3, S2 to S7, W1, W2, W3. The first run failed H1 and W3 on the same
    point: the Lab's herdr shim logged the toast correctly, but its oracle
    still read the old `--title` flag, which PR 3 had corrected to a
    positional title. Fixed in PR 4 with a test that reads the argv the real
    client writes, and both pass.
  - **Owed.** The Lab tier of Q1, G1 to G3, I2 and I3 (the runner cannot post
    a signed webhook, play `edit-intent`, or fake GitHub yet); spike SG1 on a
    scratch repo; `session.form.list` is an unverified operation name; v2
    listing is not paged, so a recipient with more than 200 synthetic
    messages in the look-back is held; the two test fakes of GitHub should
    become one.
- 2026-10-02: PRs 8 to 10 built the same way (separate workers, then rebased
  into the stack and verified together), after Lab spikes SF1, SF2 (first cut),
  SF4, SP1 and half of the import spike. Every tip passes all five suites
  offline.
  - **PR 8, foreground.** The classifier can now act, behind
    `classifier_mode` (`shadow` stays the default until SF2 has real labelled
    cases). A decoration is a fact for the Chief of Staff, derived each pass
    from the transcript and the cached classification and delivered as a note
    by the one delivery path. `handoff` builds the brief with the ask's lines
    filled in, refuses one without Done so far and Next steps, posts it as a
    comment on the ask, and launches a `cos-subagent` fleet worker in its own
    tab. Because SF1 showed the notice is a race, the daemon polls every
    `busy_poll_seconds` while a Chief of Staff works, reads your message from
    v2's inbox before it is consumed, and audits a late decoration as
    `foreground.late`. Listing is now paged by v2's cursor, so a recipient with
    many synthetic messages is no longer held. F1 to F4 pass offline.
  - **PR 9, policy.** `judge-tool` and the `fleet-hooks` plugin, rewritten to
    the shape SP1 measured. The deviation SP1 forced: `judge-tool` asks the
    daemon over `judge.sock` instead of calling Jev itself, so the OpenRouter
    key stays out of v2's environment and so out of every agent's shell. The
    charter gains a one-line `Standing authority:`; without it the judge treats
    a merge, deploy, publish or spend as not covered. The judge keeps two small
    disposable cache files (verdict hashes, and resolved asks and sessions), so
    state is no longer strictly the audit log and reminders; deleting them
    loses only the cache. P1 to P6 pass offline.
  - **PR 10, v1 move.** `import-v1` converts a v1 export into v2's transcript
    shape and imports it. It mints fresh session and message ids derived from
    the v1 ones (v2 answers a reused message id with a 500), maps the shell
    tool's name, drops any part v2 has no kind for into a printed and audited
    list, preserves order and time, and is idempotent twice over (metadata
    first, then v2's 409). `docs/switchboard-cutover.md` is the runbook. M1
    passes offline.
  - **Still owed.** The Lab tier of the P scenarios (the plugin has to be linked
    into the Lab), F1 to F3 and M1 (lab-model), and the real v1 export shape;
    whether v2 accepts an imported session without `idle` markers; SF2 on real
    transcripts; SF3.
  - **Lab verification at the top of the stack.** The lab-scripted tier passes:
    21 scenarios, every control failing (B1, D1, F4, F5, H1, J1, L1, N1, N2, R1,
    R2, R3, S2 to S7, W1, W2, W3). The other scenarios are offline or lab-model by
    design, or owed (listed above). The real `fleet-hooks` plugin was then run
    end to end against real Jev, as described under SP1.
- 2026-10-03: PR 11, a trial you can sit in front of. `bin/switchboard-trial`
  (`init`, `login`, `up`, `status`, `down`) builds a second Lab-shaped profile
  with a real model: its own v2 install and data, its own switchboard config and
  state, its own herdr workspace, and a scratch repo. v2's managed service
  uses a fixed port per profile, so the trial's is 49380 beside the Lab's
  49374. The trial wrapper strips every credential-shaped variable and then
  gives the agents the `gh` token as `GH_TOKEN` (they work on GitHub); the
  OpenRouter key stays in the daemon only. A bug the trial found: `launch` had
  no `--charter`, so a Chief of Staff could not find an orchestrator's asks.
  Fixed, with a test watched failing without the fix. The v1 import is left
  built but is not part of the trial.
- 2026-10-05: three things the first real trial session found, all fixed in PR 11.
  - **`gh` had no login for the agents or the daemon.** The token was given to
    the v2 service only, but an agent's shell runs in the pane's own process, and
    the trial's `XDG_CONFIG_HOME` hid `gh`'s account from the daemon. The wrapper
    now asks `gh` for the token on every start (before it replaces `HOME`, since
    on macOS `gh` finds it through the login keychain), and the daemon gets
    `GH_CONFIG_DIR`. Nothing is stored in a file.
  - **The role files still told the Chief of Staff and orchestrators they run
    under the heartbeat service.** The doc said switchboard agents do not use
    `heartbeat-ack`; PR 6 never changed the files. A `[switchboard]` wake sent the
    Chief of Staff to a heartbeat state directory that does not exist. Only a
    message starting `Heartbeat` may now send an agent there.
  - **The judge asked the Chief of Staff about routine `gh` and `ls` calls.** With
    no ask there is nothing to be outside of, so `outside_scope` now cannot fire
    either (as `outside_intent` already could not); `hard_to_reverse` and
    `speaks_for_you` still do. A wildcard allow was tried and rejected: it
    overrides an agent's `edit: deny`.
  - **`restart`.** `launch --resume SESSION` opens a tab on an existing session
    (it checks the session is the named agent's, creates nothing and sends no
    brief), and `switchboard-trial restart` uses it to close an agent's pane and
    reopen the same session. `up` now resumes an agent that has a session but no
    pane, so `down` and reboots no longer discard a conversation (`up --fresh`
    does). The driver only closes a pane herdr places in the trial's workspace.
  - **The agents were reading the live fleet's skills.** `platform` followed
    `fleet-charter` section 4 and tried to ack a wake. Two causes: the kit's
    skill still said to ack unconditionally in effect, and v2 scans the real
    home for skills even with `HOME` replaced (issue #16 Q3, now confirmed), so
    the live, older copy of a skill with the same name won. The skill now says
    only a `Heartbeat` message expects an ack, and the trial installs the kit's
    skills into its own `skills/` directory and lists it under `skills.paths`,
    which is read last and wins. The live skills are still visible to the
    trial's agents; only same-named ones are overridden.
  - **Two first-run gaps in the trial.** The charter convention needs the
    `<user>:orchestrator` label (the prefix is `whoami`, the OS user) and the
    seeded repo had none, so `platform` could not mark its own charter and, rightly,
    would not create labels unasked. `init` now creates the two labels
    idempotently and labels the charter. And the trial served whichever `gh`
    account was *active*, which changed to one that cannot see the repo: `init`
    now pins the account (`--gh-user`), the wrapper asks for that account's token,
    and the daemon gets it in its own environment only.
  - **The orchestrator started its coder as a v1 agent.** Following the
    `fleet-coordination` skill, `platform` ran `herdr tab create`, `herdr agent
    start --kind opencode` and `herdr agent prompt`: a typed bare `opencode`, which
    is the v1 install on this machine, outside the switchboard and the trial's
    isolation, with no `fleet-switchboard` on its PATH. PR 3 built `launch` to
    replace exactly this, but the skill still taught the old route. It now opens
    with `fleet-switchboard launch` and marks the typed routes as the legacy
    fleet's.
  - **An Intent edit reached nobody (scenario run, live).** PR 6 built the
    change item and the toast, and said PR 5's webhook would call them; nothing
    did. The edit went to the owner as a generic issue note, with a header built
    from the cached, old Done when, and the Chief of Staff and your screen heard
    nothing. The hub now invalidates the issue's cached Intent on every edit,
    holds an edit that changed the Intent section, and gives each Chief of Staff
    the old-to-new item and, through the render step's one toast path, a toast.
    Checked live: the next message carried the new Done when, the item showed
    old to new, herdr reported the toast shown.
  - **The judge failed open on a cold orchestrator (scenario run, live).**
    `platform` ran `gh workflow run deploy.yml` and the judge allowed it,
    unjudged: each of its calls spent the whole 900 ms resolving the ask (a `gh`
    round trip is about half a second, and an orchestrator needs several) or
    waiting for Jev, which ran to about a second. The daemon now warms every
    fleet agent's identity and ask context in the background, at half the cache
    lifetime, so a call finds them cached; the default budget is 1500 ms, and the
    plugin's own timeout 2500 ms. Also seen live: both agents run with the
    TUI's auto-accept on, which answers every `ask` itself, so the judge's asks
    have no effect there; only a deny stops anything.
  - **The judge ruled on what a message said (scenario run, live).** An
    orchestrator's `fleet-switchboard send cos "... run gh workflow run
    deploy.yml ..."` was scored `speaks_for_you`, because the judge reads the whole
    command. A plain `send`, `intent`, `intents`, `pending`, `status` or `remind`
    is the agents' own channel and is no longer asked about; the check is
    conservative and quote-aware, so any substitution, operator, redirect, glob or
    extra line sends the command back to normal judging, and `launch`, `handoff`
    and `import-v1`, which act, stay judged.
  - **A pass took 2.7 s, and the notice lost its race by three (scenario run,
    live).** herdr lists every pane of every workspace, so the trial's daemon asked
    its v2 about all fifteen of the live fleet's sessions on every pass: eleven
    `session.get` calls, two seconds. A decoration decided at :53 was written at
    :58, after the Chief of Staff had consumed the message and gone idle. A session
    that is no fleet agent (no fleet metadata, or one v2 does not have) is now
    remembered for 120 s; a fleet agent and a transient error never are.
  - **A dead forwarder's reason was a flag description (scenario run, live).**
    `gh webhook forward` prints its error and then its whole usage text, and the
    audit kept the last line: `-U, --url string Address of the local server...`.
    The reason is now the first error-looking line that is not usage.
  - **Agents reacted to their own comments (scenario run, live).** The agents
    share the user's GitHub account, so an event's author cannot say who wrote
    it, and every comment an agent posted came back to it as a wake (`platform`:
    "that message is just my own comment coming back"): a wasted model turn each
    time, and a possible loop. Two fixes. `fleet-switchboard comment` (and
    `handoff`) sign what they post with an HTML comment naming the agent,
    invisible when rendered; a signed comment or review is dropped from the
    webhook and the catch-up read alike. And `github_ignore_authors` drops every
    event by the listed logins, for agents that run as a separate bot account,
    which is the robust answer where you can have one. What is still open: an
    agent that posts with plain `gh` is not signed, so the role files and the
    charter skill now tell them to use `comment`; nothing enforces it.
  - **Signing is transparent now.** Agents are vended just in time and have no
    accounts of their own, and "use the special command" depends on each agent
    remembering it. v2 triggers `shell.create.before` with a mutable spec
    (`command`, `cwd`, `timeout`, `shell`, `env`) and spawns with its `env`
    (measured in the Lab). The `fleet-hooks` plugin uses it to put the kit's
    `shims/` directory first on PATH for every shell command; `shims/gh` signs the
    body of `gh issue comment`, `pr comment`, `pr review`, `issue create` and
    `pr create` (from `--body`, `-b`, a body file or stdin) with an HTML comment
    naming the calling agent (`fleet-switchboard whoami`) and runs the real `gh`
    otherwise, or on any doubt, unchanged. No command is parsed or rewritten, only
    the environment it runs in. Checked live: `platform` ran a plain `gh issue
    comment`, the posted body carried `from=platform`, and the webhook produced no
    item. `fleet-switchboard comment` remains for a harness with no shim. The
    marker is a name, not a proof: anyone who writes one into a comment suppresses
    its delivery. An HMAC over the body with a per-install secret would make it
    unforgeable; not done.
- 2026-10-05: PR 12, a presentation. `docs/presentation/` holds a single-file
  animated deck (`index.html`, no dependencies), its narration (spoken by a
  text-to-speech model through OpenRouter, the script in `narration.json`) and the
  scripts that build an MP4 of the two together (the MP4 itself is not committed). Each element
  appears on the sentence that speaks of it:
  `build/tts.py` writes the audio and `timing.js`, the page reads the sentence
  start times, and `build/render.mjs` parks every animation at each frame time and
  pipes the screenshots to ffmpeg. The numbers on the slides are the real ones
  (970+ tests, 21 live scenarios, 12+ bugs found in a live run). The narration was
  checked by transcribing it back with a speech-to-text model, not by ear.
- 2026-10-06: PR 13, workers report. Found live in the trial: every worker turn
  end woke the Chief of Staff with a `worker.idle`, it answered "no change", and
  those answers, which were all the human saw, were noise; and v2's TUI shows no
  synthetic message at all. Measured: `session.synthetic` accepts an optional
  `description`, which the TUI draws as a one-line row. Changed: `fleet-switchboard
  report` (attention reports wake, progress reports ride); the rider attribute on
  facts, collapsed and aged out before composing; a bare idle is a rider; a stop
  with no report since the worker's latest assigning input is triaged by the
  `report_owed` question (nudge once, silent, or one `worker.stopped` for the boss,
  failing open); a description on every synthetic message; role files and the
  coordination skill tell agents to report. Scenarios R4 to R8 are new, and
  N1, N2, W1, F4, H1, R1 and R3 now go through the stop triage and wait for
  `worker.stopped` where they waited for `worker.idle`.
- 2026-10-06: PR 14, Decisions. Found live: the human had no place to see what
  waited on them, so every agent reply restated "I am still waiting on your two
  answers"; the `awaiting-user` label answered the question only to someone who
  ran `gh issue list`. Measured first (the spike, then this PR): v2's TUI loads
  a plugin from `cli.json` naming a directory with `tui.tsx`, and draws a
  `sidebar.content` slot as a section; `fs.watch` on the state directory fires
  inside that runtime on macOS (bun 1.4.2, 2.0.22), six events for three
  renames, and re-rendered the section; a row longer than the sidebar (about 36
  columns at a width of 160) wraps, so rows are cut to 34 characters. Changed:
  `derive_decisions` (labels, pending requests, unanswered reports; ids derived
  from a hash, so they stay put while a decision is open), `decisions.json` kept
  by the daemon (rewritten on change, touched otherwise, so its age is the
  daemon's pulse), `fleet-switchboard decisions`, the `fleet-decisions-tui`
  plugin, `switchboard-trial` installing it, role files and skills ("say it once
  with its id; see Decisions"), and the hub keeping the awaiting-user issues from
  webhook events and one extra read per catch-up, with no new `gh` call per pass.
  Two things the first design got wrong and the tests found: a rank-based suffix
  (`#2a`, `#2b`) renumbers when the first resolves, and a file written only on
  change cannot say the daemon is alive. Scenarios D2 to D5 are new; R3 also expects
  the file back; every offline scenario now also writes `decisions.json`, which
  changed nothing else.
