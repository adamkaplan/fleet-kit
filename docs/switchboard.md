# Switchboard: tracking

> Event-driven delivery for Fleet Kit on OpenCode v2 that never interrupts.
> This plan is updated as each PR lands. Last updated 2026-10-02.
>
> It lives at `docs/switchboard.md` on the stack. After PR 1 it is edited only
> on the current top branch, so status updates never rebase lower PRs.

## Status

| PR | Branch | Scope | State |
|---|---|---|---|
| 1 | `switchboard/v2-client` | v2 client, isolated lab, spikes S1–S6 | in progress |
| 2 | `switchboard/inbox` | Inbox, notes vs. wakes, batching | planned |
| 3 | `switchboard/herdr` | `launch`, herdr plugin, badges, toasts, pane status | planned |
| 4 | `switchboard/worker-events` | Worker done or blocked → its orchestrator; live proof A–E | planned |
| 5 | `switchboard/github-events` | GitHub poller | planned |
| 6 | `switchboard/threads` | Thread ledger, relaying your exact words, Chief of Staff rewrite | planned |
| 7 | `switchboard/judges` | Cheap judges that log but don't act ("shadow mode") | planned |
| 8 | `switchboard/v1-move` | Import a v1 session into v2; cutover runbook | planned |

Each PR is opened as soon as it is ready. The whole stack merges to `main` in
one atomic `gh stack merge`, and only once the system is complete.

## Problems

1. **Telephone.** Requests pass from Chief of Staff to orchestrator to worker
   and are retyped at each hop. Nothing preserves your exact words, and text
   sent by a machine looks exactly like your typing.
2. **Serial Chief of Staff.** It has one conversation and handles one request
   at a time, so a question about B waits for A to finish.
3. **Interruptions.** Wakes are typed into the agent's input box.
   - A half-written message of yours can be submitted along with the wake,
     because the empty-box check and the send are not atomic.
   - Wakes also arrive as turns in the middle of your conversation with the
     agent.
4. **Silent completions.** A worker finishing wakes nobody. Its orchestrator
   only finds out on the next timer.

Evidence from real use:

- An idle orchestrator spent 18 overnight wakes in a row concluding "no
  change."
- A Chief of Staff pane missed thousands of wakes because of an unsent draft.
  The only sign was a symbol on its tab.

## Decisions

| Topic | Decision |
|---|---|
| Harness | OpenCode v2 only. Other harnesses come later, as adapters. |
| Plugins | No OpenCode plugin. v2's server API already covers notes, wakes and session state. |
| Location | `bin/fleet-switchboard` (stdlib Python) plus a herdr plugin manifest, both in this repo. |
| Heartbeat | `fleet-heartbeat` is unchanged and keeps serving v1 panes. A pane the switchboard manages never carries the heartbeat's opt-in bell glyph. |
| Machine text | Always a v2 `synthetic` message tagged `[switchboard]`; never typed, never sent as a user message. Whether it shows in the TUI is settled by S3. |
| Thread ledger | Kept locally, promoted to a GitHub issue once a request becomes work (PR 6). |
| Talking to orchestrators | You can talk to any orchestrator directly; the switchboard records it (PR 6). |
| v1 sessions | Imported, not restarted (PR 8). |

## Design

```text
herdr socket ──────────┐  focus, pane lifecycle
OpenCode v2 service ───┤  busy · idle · viewed · your prompts · permission requests
GitHub (PR 5) ─────────┤  comments · PRs · checks · labels
fleet-switchboard send ┘
            │
   fleet-switchboard daemon ── inbox · audit log · delivery rule
            │
   ┌────────┴────────────────┐
 OpenCode v2 API              herdr
 note / wake (synthetic)      badge · toast · pane status
```

### Delivery rule

Items for an agent are collected for 90 s, then delivered in one of two ways.

- **You are engaged with the agent → note.** Engaged means you sent it a
  prompt in the last 10 minutes, or it has a finished reply you have not
  viewed that is less than 15 minutes old. The switchboard calls
  `session.synthetic` with `resume: false`. That starts no turn; the agent sees
  the note alongside your next message.
- **Otherwise → wake.** `session.synthetic` with `resume: true` and
  `delivery: "queue"`. It never cuts into a running turn.
- **Format.** Each delivery is one line:
  `[switchboard] 2 for <agent>: <summary> — run: fleet-switchboard inbox <agent>`.
  Reading the inbox acknowledges the items.
- **Never typed.** The switchboard never calls `herdr agent prompt` and never
  writes into a pane's input box.

### v2 operations used

All go through `opencode api <operation>`, which finds the service and handles
authentication.

| Need | Operation |
|---|---|
| Create a session with a given agent | `session.create` |
| Brief a worker | `session.prompt` with `metadata.from` |
| Note / wake | `session.synthetic` (`resume`, `delivery`) |
| Busy | `session.active` |
| Finished, and whether you have viewed it | `Session.Info.time.idle`, `time.viewed`, `outcome` |
| Your prompts | `session.message.list`: user messages without switchboard metadata |
| Blocked | `permission.request.list`, `form.list` |
| Pending deliveries | `session.inbox.list` / `.cancel` |
| Move a v1 session | `experimental.session.import` (PR 8) |

### herdr's part

- Event source: pane focus and pane lifecycle.
- What you see: an unread count on the pane, and a toast when an item has been
  held more than 15 minutes.
- Pane status for v2 panes, sent with `pane report-agent`, because herdr's own
  OpenCode integration is a v1 plugin.
- Plugin host: its `[[startup]]` hook runs `fleet-switchboard ensure`.

## Lab: isolation from the live fleet

The live fleet stays on v1 throughout.

- **v2 binary.** A pinned version, installed into a private directory that is
  not on `PATH`. Never installed with `npm -g` or the curl installer, because
  the installer replaces the v1 binary.
- **v2 data.** Moved into a scratch directory with `XDG_*` variables. A `HOME`
  override is the fallback; XDG is preferred because it leaves git and gh
  identity intact. Checked with `opencode debug paths`.
- **Panes.** A herdr workspace called "Switchboard Lab", whose environment puts
  the private v2 first on `PATH`.
- **Switchboard config.** Names the v2 binary and its environment.
- **herdr plugin.** Linked to the single herdr server. The daemon manages only
  sessions it launched.
- **Models.** GitHub Copilot, through a one-time device login inside the
  scratch profile. Real credentials are never read or copied.
- **Work.** A local scratch git repo. No GitHub until PR 5.

## Spikes (PR 1)

| # | Spike | Passes when |
|---|---|---|
| S1 | Isolation | Every path from `debug paths` is in scratch; v1 is unchanged; no v1 or v2 state appears outside scratch; the kit's agent files load under v2 with the intended permissions |
| S2 | Access | Python can call `opencode api` for create, get and active. Also measured: latency; whether `GET /api/event` or `session.log` can stream; whether calling HTTP directly is practical |
| S3 | Note | `resume: false` starts no turn, and the model quotes the note after your next message. Tested with both `delivery` values; TUI visibility recorded |
| S4 | Wake | With `resume: true` and `queue`: an idle agent starts a turn; a busy agent runs it after the current turn; a draft in the TUI survives both cases |
| S5 | Observation | Your prompts can be told apart from the switchboard's; `time.idle` and `time.viewed` behave the way the delivery rule assumes |
| S6 | herdr | In the Lab: `herdr agent start --kind opencode -- -s <ses>` works and herdr detects the agent; `report-agent` sets status; plugin link and `[[startup]]` work. Also records which metadata appears in the sidebar |

**Stop rule.** If S3 or S4 fails, work stops and we decide together before
PR 2. No fallback is built ahead of time.

## Live proof (PR 4)

`bin/proof-switchboard live` runs in the Lab, not in CI. It simulates your
actions by typing into Lab panes, and you also do one manual pass.

| | Scenario | Must hold |
|---|---|---|
| A | A draft is in the orchestrator's input box when an item arrives | The draft is intact |
| B | You are mid-conversation with the orchestrator when the coder finishes | Only a note is delivered, no machine turn starts, and the orchestrator's next reply mentions it |
| C | You are away when the coder finishes | The orchestrator is woken within about 2 minutes and runs `inbox` once |
| D | An item arrives in the middle of a turn | It runs after that turn |
| E | 2 hours with no events | Zero machine turns |

A check only counts once we have seen it fail with its safeguard switched off.

## Repo and PR conventions

- Work happens in a git worktree of the existing checkout; no second clone.
  The stack's trunk is `main` at `9d22be4`.
- The push remote is `fork` (adamkaplan/fleet-kit), set with
  `git config gh-stack.remote fork` and passed explicitly as `--remote fork`.
  No other remote is pushed, and `main` is never pushed.
- GitHub operations authenticate as the repo owner, one command at a time,
  with `GH_TOKEN` from `gh auth token --user …`. The machine's active gh
  account is never switched.
- This document is edited only on the current top branch of the stack. Each PR
  updates its own row in Status and adds to the Log.
- Commit messages follow the repo's style (`fleet-switchboard: …`,
  `README: …`, `CI: …`, `docs: …`). Tests pass at every commit.
- `bin/test-switchboard` runs in CI from PR 1. It is stdlib only, uses fakes
  for herdr and `opencode api`, and includes the hygiene checks.
- Docs land in the same PR as the feature they describe. Each PR links this
  document and carries its own evidence.

## Risks

- **v2 is still 2.0.x, and its HTTP API is marked experimental.** We pin the
  version and keep all v2 calls in one client class.
- **v2's event names are not documented.** The switchboard polls first and
  switches to streaming only if S2 shows it works.
- **herdr has no status hook for v2 panes.** The switchboard reports their
  status instead.
- **Stacked PRs are reviewed bottom-up.** A fix to a lower PR cascades upward
  through `gh stack rebase`, and every push re-runs CI.
- **The real cutover (PR 8).** A v2 install outside the Lab shares v1's config
  and data directories and migrates v1 history on its own. The runbook has to
  plan for this.

## Out of scope for now

Claude Code and Copilot adapters; injecting context into individual model
calls; changes to `fleet-heartbeat`.

## Open questions

- S3: are synthetic notes visible in the v2 TUI? If they are, either accept
  visible, labelled notes or revisit the design.
- Who reviews the stack?
- Should this document stay in `main` after the final merge? Precedent: the
  kit's earlier `PROPOSAL.md` was deleted once its work landed. Until we decide
  at merge time, this stays a tracking document.

## Log

- 2026-10-02: plan agreed; this document committed as the first commit of
  PR 1.
