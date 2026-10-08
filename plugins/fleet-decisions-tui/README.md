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

## The styled panel (issue 67, iteration 1)

The default drawing: the same sections, groups and order, with state you can see at a glance.

- **Top:** `Decisions` and the view label (`all`, `for you`, a repo), then one line of counts:
  `2 need you · 3 PRs to watch · 5 wait on cos (3 over 1h)` (`need human` / `wait on cos` in the Chief of Staff's or a repo's
  panel; `with cos` in a human's).
- **Colours** are the TUI theme's, not fixed ones: **bold** is what needs you; **muted** ids, rules and counts; **warning**
  (yellow-ish) is an age of an hour or more, `over 1h`, `CI pending`; **error** (red) is an age of a day or more and a
  bold `CI failing`; **success** (green) is `CI green`/`approved`; **info** is the PR count. If your theme lacks a colour the text
  is drawn in the base colour; nothing fails.
- **Id line:** the id on the left, how long it has waited on the right. A heads-up shows its PR number (`#2644`).
- **Repos:** `org/repo` shows as `repo` unless another repo in the file has the same name; `org/repo#N` in a headline becomes `repo#N`, and a ref to the group's own repo `#N`.
- **Plain switch:** `FLEET_SWITCHBOARD_PANEL=plain` in the TUI's environment draws the earlier panel. Any error while drawing the styled panel also falls back to it.
- **Roll back:** copy the previous `tui.tsx` and `decisions.mjs` over the installed ones (`<profile>/opencode/fleet-decisions/`) and restart the TUI.

## Using it

OpenCode v2 loads a TUI plugin from `cli.json`, which names a **directory** that holds
`tui.tsx`: in the profile's `opencode/` config directory, `cli.json` is
`{"plugins": ["./fleet-decisions"]}` and `fleet-decisions/` holds `tui.tsx` and `decisions.mjs`.
`bin/switchboard-trial` installs both and is idempotent. A TUI picks the plugin up when it is
restarted (`switchboard-trial restart`). The sidebar exists on the session screen only, and only in
a terminal wide enough to show it.

`decisions.mjs` holds the pure functions (read the file, staleness, the rows) and is tested by
`bin/test-switchboard` with node. `tui.tsx` is the thin layer over them and is checked in the Lab.
