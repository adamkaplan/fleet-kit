# fleet-decisions-tui

A read-only OpenCode v2 TUI plugin: a **Decisions (N)** section in the right
sidebar, below the session's own sections, listing what waits on you (docs/switchboard.md,
"Decisions (PR 14)"). Decisions are grouped by repo (a header per repo and a rule
between repos; repos by name, oldest decision first). Each shows its id and how long it has waited on
one line, then its whole title word-wrapped to 34 characters on the lines below. You read it and refer to a decision by its id in chat.

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
