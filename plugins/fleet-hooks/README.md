# fleet-hooks

One thin OpenCode v2 plugin for the switchboard (docs/switchboard.md,
"Tool-call policy (PR 9)"). It registers `api.permission.hook("evaluate")`,
runs `fleet-switchboard judge-tool` for each evaluation of a shell, edit,
web-fetch or subagent call, and applies the reply only if it is stricter than
the outcome the configured rules chose. It holds no policy.

## The daemon, not v2, holds the key

The plugin runs inside v2's service process, and the environment of that
process is also the environment of every agent's shell tool. So nothing here
may hold the OpenRouter key:

- the plugin gives `judge-tool` a short allow-list of variables (`PATH`,
  `HOME`, `XDG_*`, `TMPDIR`, `LANG`, `FLEET_SWITCHBOARD_*`), never the whole
  environment;
- `judge-tool` reads no environment variable of its own accord and never calls
  Jev; it sends the request over `$STATE/judge.sock` (mode 0600) to the
  switchboard daemon;
- the daemon (`fleet-switchboard run`, started by `ensure`) holds the key in
  its own environment, named by the `jev.key_env` setting, judges, and replies.

So the requirement is: the daemon runs with the key variable set, v2 does not.
With the daemon down, the socket missing, or no answer within
`policy.budget_ms` (default 1500), the configured outcome stands. The daemon
opens the socket only when the switchboard config has `jev` and
`policy.enabled: true`.

## Using it (not installed by anything in this repo)

Give v2's environment `FLEET_SWITCHBOARD_BIN`, the absolute path of
`bin/fleet-switchboard`, and put the plugin where v2 loads local plugins:

1. **In the profile's `plugins/` directory** (verified in the Lab): link or
   copy `index.js` there under a unique name, for example
   `ln -s /path/to/plugins/fleet-hooks/index.js <config>/opencode/plugins/fleet-hooks.js`.
   v2 loads every file in that directory at startup; `GET /api/plugin` then
   lists `fleet.hooks` as `active`. An absolute path in the config's `plugin`
   array was **not** loaded in the Lab, so do not rely on it.
2. **As a package** (not tried): install this directory
   (`npm install /path/to/fleet-hooks` in the profile) and list `"fleet-hooks"`
   in `plugin`.

| Variable | Meaning |
|---|---|
| `FLEET_SWITCHBOARD_BIN` | Absolute path of `bin/fleet-switchboard`. **Unset: the plugin uses `realpath(~/.local/bin/fleet-switchboard)`**, the link `install` makes, so a service auto-started by a TUI with no environment still judges. Set but relative: the plugin does nothing (an explicit value always wins, even a bad one). It is never looked up on `PATH`. |
| `FLEET_SWITCHBOARD_SHIMS` | Absolute path of the `shims` directory (the signing `gh`). **Unset: `<checkout>/shims`**, where `<checkout>` is the directory above the `bin/` that the link above resolves into. Set but relative: the shell hook is off. |
| `FLEET_HOOKS_TIMEOUT_MS` | Hard limit on one call, default 2500. Keep it above `policy.budget_ms`. When it passes, the child is killed and the configured outcome stands. |

### A stale link is not silently trusted

The fallback follows the link wherever it points, including a stale worktree.
`fleet-doctor` (check `switchboard-path`) says so when the resolved switchboard
is not the main checkout (`~/Code/fleet-kit/bin`, or `$FLEET_KIT_MAIN`).
Fix the link with `fleet-switchboard install`; the plugin reads it at service
start, so a running service picks the change up at its next restart.

Tests: `node --test plugins/fleet-hooks/index.test.mjs`.

## Shape (measured in the Lab, v2 2.0.22)

A server plugin is `export default { id, setup: async (api) => {...} }`
(a `server` key is ignored). The hook argument is `{ sessionID, agent, action,
resources, source, effect }`; the plugin tightens it by assigning `effect`
(`ask` or `deny`) and `message`. A deny's message reaches the agent as the tool
error; an ask's message is carried on the pending permission request. The
handler is awaited, so its time adds to the call. A configured `deny` never
reaches the hook.

`JUDGED_ACTIONS` in `index.js` lists the action names forwarded, exactly as
the hook reports them. Measured in the Lab: a shell call is `shell`, a file
write is `edit`, a web fetch is `webfetch` and a subagent launch is `subagent`.
`task` is listed too but was not seen. Any other action keeps the configured
outcome.

## Guarantees

- A reply that is not stricter than the configured outcome is ignored (the
  CLI and the plugin each enforce this). A malformed, late or missing reply
  leaves the configured outcome alone.
- The child is killed after the timeout.
