# fleet-decisions-tui

A read-only OpenCode v2 TUI plugin: a **Decisions (N)** section in the right
sidebar, below the session's own sections, listing what waits on you (docs/switchboard.md,
"Decisions (PR 14)"). Decisions are grouped by repo (a header per repo and a rule
between repos; repos by name, oldest decision first). Each shows its id and how long it has waited on
one line, then its whole title word-wrapped to 34 characters on the lines below. You read it and refer to a decision by its id in chat.

- **Whose panel it is (PR 18).** The title says which view: `Decisions (3) all`, `Decisions (2) acme/api`,
  `Decisions (1) for you`. A pane whose role is `chief-of-staff` shows every tier and repo; a pane that names a
  repo shows that repo's decisions (every tier); any other pane, such as a TUI that is not a fleet agent,
  shows the `human` tier only. The role and repo come from `FLEET_SWITCHBOARD_ROLE` and
  `FLEET_SWITCHBOARD_REPO`, which `fleet-switchboard launch` and `bootstrap` put in the pane's environment
  (not secrets). A TUI that was already running has neither the variables nor this version of the plugin:
  restart it.
- **Clicking (issue 66).** A decision row links to its issue and a heads-up row to its PR
  (`https://github.com/<repo>/issues/<ask>`, `.../pull/<number>`; built from the repo and number only, each
  strictly validated, never from a title). A row with no repo or no number has no link. The `id age` line is an
  OSC 8 hyperlink: Ctrl-click (herdr; Cmd-click in iTerm2 outside herdr) opens it in the browser; a terminal that ignores OSC 8
  shows plain text. A plain click on the row copies the link with OSC 52 and shows a toast (a terminal that
  blocks OSC 52 gets the toast with the link). Still no process, no network.
- It reads `decisions.json`, which the switchboard daemon rewrites when the list
  changes and touches on every pass. It never derives anything itself.
- An empty list shows a row `none`. A file that is missing, unreadable, older than 180 s,
  or a plugin that cannot tell where the state directory is shows `daemon not updating`. The
  section is never absent.
- It watches the **directory** of the file (the daemon replaces it by rename) with
  `fs.watch`, with one slow re-read every 60 s and one check for the moment a list would turn
  stale. It spawns no process, holds no credential and makes no network call.
- It finds the file in the switchboard's state directory: `FLEET_SWITCHBOARD_STATE` when it
  is set (`bin/switchboard-trial` sets it in the wrapper that starts v2), otherwise the
  directory the daemon writes to by default, `$XDG_STATE_HOME/fleet-switchboard` or
  `$HOME/.local/state/fleet-switchboard`. A real install sets nothing, so the default is what runs.

## The styled panel (issue 67)

The default drawing, built by subtraction: only what has something in it, blank lines instead of rules.

- **Top:** `Decisions` and the view label, then counts: `2 need you · 3 PRs · 5 with cos (3 over 1h)` (`need human` / `on cos` in the Chief
  of Staff's or a repo's panel). A zero count is left out.
- **What needs you:** two lines, the headline in **bold** and a muted id with its age on the right. **Heads-up:** one line, `#N` and the
  headline, age on the right; the number is green (CI green and approved) or red (CI failing). The Chief of Staff's cos tier is one line per repo.
- **Colours** are the TUI theme's, not fixed ones: muted ids and counts; warning for an age of a day or more and `over 1h`; success/error for the PR number.
  A theme that lacks a colour gets the base colour; nothing fails.
- **Repos:** a repo name once per group, `org/repo` shown as `repo` unless another repo in the file has the same name.
- **Plain switch:** `FLEET_SWITCHBOARD_PANEL=plain` in the TUI's environment draws the earlier panel. Any error while drawing the styled panel also falls back to it.
- **Roll back:** copy the previous `tui.tsx` and `decisions.mjs` over the installed ones (`<profile>/opencode/fleet-decisions/`) and restart the TUI.

**Iteration 3** adds a stable theme colour per project (header and PR numbers), one-cell marks (`◆` PR, `✓`/`✗` ready/failing in
green/red, `?` question, `¶` report, `⧖` a day or more), bold for what needs you, italic muted ids and ages, more space between
sections, and a large PR's size in the theme's diff colours. Marks are single-cell on purpose (two-cell emoji break the alignment).

**Kind marks and links:** `◆` PR, `○` issue, `?` question, `¶` report. `N PRs` and `N on cos` in the summary line, and each folded repo line in
Waits on cos, are links to GitHub searches (open PRs of those repos; open issues with the `<user>:awaiting-cos` label). A report or prompt
is not an issue, so a link covers only the issue-kind decisions of its count. The label's user is the OS user from the TUI's environment.

## Using it

OpenCode v2 loads a TUI plugin from `cli.json`, which names a **directory** that holds
`tui.tsx`: in the profile's `opencode/` config directory, `cli.json` is
`{"plugins": ["./fleet-decisions"]}` and `fleet-decisions/` holds `tui.tsx` and `decisions.mjs`.
`bin/switchboard-trial` installs both and is idempotent. A TUI picks the plugin up when it is
restarted (`switchboard-trial restart`). The sidebar exists on the session screen only, and only in
a terminal wide enough to show it.

`decisions.mjs` holds the pure functions (read the file, staleness, the rows) and is tested by
`bin/test-switchboard` with node. `tui.tsx` is the thin layer over them and is checked in the Lab.

A `MERGE: captain` PR row is linked and named like an issue row: it links to its pull request (built from its repo and number with the same checks as an issue link) and shows the short ref `repo#N` (the repo's full name when another repo shares the short one), never the internal key `pr:org/repo#N`, so its age stays on the line.

## The workspace health block (ask #357)

Above the Decisions rows, one dense block per workspace that has an orchestrator (or the Chief of Staff), hottest first, two lines each:

```
●! alpha     work ▁▂▃▄▅▆▇█▇▆▅▄▃▂▁≈
▁▂▃▅▂▇▄█ q6 s3 d1 c2/3 p2 ctx45%
```

- **Line 1:** a state dot and word, `!` when the load alert (#348) is on for it, the name, and the tool calls per minute over 15 minutes as a sparkline
  (`≈`: approximate, permission evaluations). State words: `work`, `idle`, `wait` (a decision waits on the owner), `dead`, `?` when it cannot be told.
  An attached pane is `work` or `idle` from the tool calls when they are recorded, else from the load record (coders busy, or asks moved this hour).
- **Line 2:** asks moved per hour so far today (UTC; `·` is an hour with no record), then `q` open asks, `s` stalled over 2 h, `d` decisions waiting on it,
  `c` coders busy/total, `p` pushes this hour, `ctx` context use. A figure whose source is missing, unreadable or too old reads `not recorded`.
  A line is cut at 34 characters, so a row with several missing figures shows the first ones. The Chief of Staff has no load row: its row carries its state word
  (`wait` while anything waits on the owner) and `dN waiting on you`, the open owner-tier decisions.
- **Legend:** four dim lines under the heading name `q s d c p ctx moved` and the state words.
- **Activity series:** tool calls from the `fleet-hooks` counter where it has the workspace's sessions (`≈`), else steps ended per minute from the step
  events the daemon already receives (`≈steps`: no plugin, no restart of any session). With neither the row says why: `restart for counter` (the counter
  runs, but those sessions started before it was installed). `ctx no step seen yet` means the usage source is on but that session has not ended a step
  since the daemon started. `cN/M` counts a workspace's coders without the retired and those that made their final `done`/`failed` report.
- **Why a figure is absent:** `tool calls: plugin not installed` (no `tool-rate.json`: the installed `fleet-hooks` copy predates the counter; the kit's
  own install step, which copies `plugins/fleet-hooks`, puts it in the profile; this block only reads) or `plugin not running` (the file is stale);
  `ctx` under `v2_light`: usage comes from the `session.step.ended` events the daemon already receives (their token and cost figures; no read of history), and reads `ctx: off: v2_light` only while no event has carried figures; `p 0` for a repository whose forwarder is running (`github-watch.json`) with no push in the
  hour, `p not recorded` only for one with no running forwarder. `health-records.json` carries these as `sources` and `pushes_watched`.
- **Sources (all read from the state directory; nothing polled, nothing spawned):** `health-records.json` (bin/health_records.py: tool calls, tokens, context,
  pushes), `load.json` (the load alert's figures and today's hourly series), `pane-health.json` (which agents sit in which workspace, and whether the pane
  is alive) and `decisions.json` (what waits on the person). Hottest = alert, then stalled asks, open asks, recent tool calls.
- **The producers are separate changes:** `bin/health_records.py` (#373) and the load alert's `load.json` (#348) land on their own; until they do, the figures they carry read `not recorded`.
- **Not drawn:** a workspace with no orchestrator; the whole block when none of those files can be read. `FLEET_SWITCHBOARD_HEALTH=off` hides it.
  A fault while drawing it draws nothing: the Decisions never depend on it. It is a separate file (`health.mjs`) that the installer copies with the other two.
- Tests: `node --test plugins/fleet-decisions-tui/health.test.mjs`.
