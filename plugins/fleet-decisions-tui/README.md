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
- An empty list shows a row `none`. A file that is missing, unreadable, older than 120 s,
  or a plugin that cannot tell where the state directory is shows `daemon not updating`. The
  section is never absent.
- It watches the **directory** of the file (the daemon replaces it by rename) with
  `fs.watch`, with one slow re-read every 60 s and one check for the moment a list would turn
  stale. It spawns no process, holds no credential and makes no network call.
- It finds the file in the switchboard's state directory: `FLEET_SWITCHBOARD_STATE` when it
  is set (`bin/switchboard-trial` sets it in the wrapper that starts v2), otherwise the
  directory the daemon writes to by default, `$XDG_STATE_HOME/fleet-switchboard` or
  `$HOME/.local/state/fleet-switchboard`. A real install sets nothing, so the default is what runs.

## Using it

OpenCode v2 loads a TUI plugin from `cli.json`, which names a **directory** that holds
`tui.tsx`: in the profile's `opencode/` config directory, `cli.json` is
`{"plugins": ["./fleet-decisions"]}` and `fleet-decisions/` holds `tui.tsx` and `decisions.mjs`.
`bin/switchboard-trial` installs both and is idempotent. A TUI picks the plugin up when it is
restarted (`switchboard-trial restart`). The sidebar exists on the session screen only, and only in
a terminal wide enough to show it.

`decisions.mjs` holds the pure functions (read the file, staleness, the rows) and is tested by
`bin/test-switchboard` with node. `tui.tsx` is the thin layer over them and is checked in the Lab.
