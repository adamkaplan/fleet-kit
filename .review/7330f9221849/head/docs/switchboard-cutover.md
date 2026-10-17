# Cutover runbook: moving the live fleet from OpenCode v1 to v2

This is the runbook for the real move, after the Lab has proved the switchboard
([switchboard.md](switchboard.md)). It is written for a person at a keyboard.
Nothing in it needs an agent to type into another agent's pane, and nothing in
it should be done that way.

The move is one agent at a time. A v1 session is **copied** into v2 with
`fleet-switchboard import-v1`; the v1 session is only read and is never changed,
so the way back is always "keep using it".

Placeholders used below. Replace them with your own values:

| Placeholder | Meaning |
|---|---|
| `$V1` | The absolute path of the OpenCode v1 binary |
| `$V2_REAL` | The absolute path of the pinned OpenCode v2 binary |
| `$V2_HOME` | A private directory that only v2 uses, such as `$HOME/.local/share/fleet-v2` |
| `$V2` | The wrapper script below, by absolute path: the only way v2 is ever started |
| `$BACKUP` | A directory for the backups, outside every OpenCode directory |

## Preconditions

All of these must be true before anything is imported.

- **v2 version.** `$V2 --version` prints the version the design doc's spike
  S6 passed with. A different version means S6 and the Lab scenarios run again
  first, because herdr's v2 integration was tested against one v2 release.
- **herdr version.** At least the `min_herdr_version` in
  `herdr/herdr-plugin.toml`.
- **herdr's OpenCode integration is upgraded for the live fleet, and this is
  why.** herdr gets an agent's status (working, idle, blocked) and its session
  id from an integration that lives inside the agent. For v2 that integration is
  a *TUI plugin*: it runs inside v2's terminal UI, and a v2 pane without it
  reports no status at all, so the switchboard could neither hold a message
  while you are engaged nor see that a worker finished. The integration that
  serves v1 panes is not that plugin, so it must be upgraded
  (`herdr integration install opencode`) in the configuration v2 will read.
  Upgrading changes files v1 may also read. Back them up first (below), then
  check that a v1 pane still shows its status in herdr after the upgrade and
  before you move anything.
- **The switchboard config names both binaries.** In
  `$XDG_CONFIG_HOME/fleet-switchboard/config.json`: `opencode` is `$V2` (an
  absolute path), `opencode_executable` is `$V2_REAL` (the program a pane
  actually runs, because the wrapper `exec`s it), and `v1_opencode` is `$V1`,
  which `import-v1` runs only as `export`. Neither has a default. A bare name
  would be found through `PATH`, and a login shell's `PATH` can put v1 first.
- **The Lab suite is green** at the commit you are deploying:
  `bin/test-switchboard`, and the Lab scenarios you rely on.
- **Nothing is mid-turn.** Each agent you move is idle, and you are not typing
  to it.

## The shared-directory hazard

A bare v2 uses the same config, data and log paths as v1, and on its first start
it migrates v1's history into its own format. Started carelessly, v2 does that
to the live fleet's database. So v2 runs only through a wrapper that moves every
path it uses, and always by absolute path.

```sh
#!/bin/sh
# $V2: start OpenCode v2 confined to its own directory. Never run v2 any other way.
V2_HOME="$HOME/.local/share/fleet-v2"        # your private directory
mkdir -p "$V2_HOME/config" "$V2_HOME/data" "$V2_HOME/state" "$V2_HOME/cache" "$V2_HOME/tmp"
export XDG_CONFIG_HOME="$V2_HOME/config"
export XDG_DATA_HOME="$V2_HOME/data"
export XDG_STATE_HOME="$V2_HOME/state"
export XDG_CACHE_HOME="$V2_HOME/cache"
export TMPDIR="$V2_HOME/tmp"
exec "$V2_HOME/bin/opencode" "$@"             # the pinned binary, by absolute path
```

Then check, before the first real use:

1. `$V2 debug paths` prints only paths under `$V2_HOME`. If one is not, set
   `HOME="$V2_HOME"` in the wrapper too and check again. `HOME` also moves git's
   and gh's configuration, so keep those pointing at the real ones
   (`GIT_CONFIG_GLOBAL`, `GH_CONFIG_DIR`).
2. Run `$V1 debug paths` and compare: no path is in both lists.
3. After starting v2 once through the wrapper, v1's data directory is
   unchanged (compare its modification times with the backup's).

The pinned v2 binary is installed into a private directory that is not on
`PATH`. Do not install it with `npm -g` or the curl installer: the installer
replaces the v1 binary the live fleet is running on.

Sign in to model providers inside `$V2_HOME`, by hand. Do not copy v1's
credential files over.

## Backups

Take them with every agent idle, and verify the restore before you trust them.

1. **v1's data directory** (`$V1 debug paths` names it; it holds the session
   database). A live SQLite file is not safe to copy as a file: use
   `sqlite3 <database> ".backup '$BACKUP/v1.db'"`, or stop every v1 pane first.
2. **v1's config directory**, which includes herdr's v1 integration files.
3. **Fleet Kit's own state**: `fleet-heartbeat`'s state directory and
   `$XDG_CONFIG_HOME/fleet-switchboard/`.
4. **A JSON export of every session you will move**:
   `$V1 export <v1 session id> > $BACKUP/<name>.json`. These are also valid
   inputs for `import-v1 --file`.

**Restore check.** Restore the database copy into a scratch directory, point v1
at it with `XDG_DATA_HOME=<scratch>`, and run `$V1 export <a session id>`. It
must produce JSON with the same message count as the live one. A backup that was
never restored is a hope, not a backup.

## Import, one agent at a time

Order: the Chief of Staff first, then each orchestrator, one at a time. Workers
are not imported: their work lives in GitHub. Let them finish on v1, or start
new ones with `fleet-switchboard launch`.

For each agent:

1. **Find its v1 session id**, and make sure the agent is idle.
2. **Dry run.** Writes nothing; prints what would be imported:

   ```sh
   fleet-switchboard import-v1 --session <v1 id> --name <name> \
       --role <chief-of-staff|orchestrator> --reports-to <boss> --charter <issue> --dry-run
   ```

   Read the lists it prints. `dropped` names every kind of v1 content that v2
   has no place for (step markers, text v1 injected rather than typed, and so
   on), with a count. `filled in` names every value v2 requires that v1 did not
   record. Nothing is lost silently; if a kind surprises you, stop and look.
   A body too long for one command argument is refused with the way out
   (`--tool-output-limit`).
3. **Import**: the same command without `--dry-run`. If the session was already
   imported it says so and does nothing, so repeating it is safe. A child
   session is refused until its parent is in v2.
4. **Open it yourself.** In a new herdr tab, start `$V2 -s <the v2 session id the
   import printed>` in the agent's working directory. You do this; the engine
   never types into a pane, and neither should an agent.
5. **Verify** with this checklist. Every line must hold:

   - [ ] The message count printed by the import equals the number of messages
         in `$V1 export <v1 id>`, and v2's own list has that many:
         `$V2 api session.message.list --param sessionID=<v2 id> --param order=asc`
         (page through it if it pages).
   - [ ] In the agent's pane, `fleet-switchboard intents` lists the open asks
         you expect, the same as GitHub shows under its charter.
   - [ ] `herdr agent get <pane>` shows `agent_status` and an `agent_session`
         equal to the v2 session id, and `fleet-switchboard status` lists the
         agent by name.
   - [ ] The v1 session still opens in v1 and its transcript is unchanged.
   - [ ] You ask the agent one question that depends on its history, and the
         answer shows it has it.
6. **Retire the v1 pane** only after every box is ticked, so that two copies of
   one agent never work on the same ask. The v1 session stays on disk.

## Start the switchboard

After the first agent verifies, and the configuration above is in place:

```sh
fleet-switchboard ensure
fleet-switchboard status
```

`ensure` starts the daemon unless one is running. It only manages sessions that
carry fleet metadata, so v1 panes are invisible to it. The plugin is not linked
(see below), so herdr's startup hook will not start the daemon for you: run
`ensure` again after herdr restarts.

## Keep `fleet-heartbeat` for v1 panes

`fleet-heartbeat` is unchanged and keeps waking v1 panes. Leave it running until
the last v1 pane has moved. A pane the switchboard manages never carries the
heartbeat's opt-in glyph, so the two never both wake one agent. When
`fleet-heartbeat status --json` shows no pane left, stop it.

## Rollback

The v1 session was never touched, so **rollback is to keep using it**: open it
in v1 by absolute path, as before. Nothing in v1 needs repairing.

To remove an imported v2 session safely:

1. Close its herdr pane (a person does this).
2. `$V2 api session.get --param sessionID=<v2 id>` and confirm that
   `metadata.fleet.imported_from` is `v1:<the v1 id>`. Never remove a session
   that lacks it.
3. `session.remove` also deletes the session's child sessions. List them first:
   `$V2 api session.list --param parentID=<v2 id>`.
4. `$V2 api session.remove --param sessionID=<v2 id>`.

The import can be repeated afterwards: with the session gone, it is not found as
"already imported".

If the live config was changed (herdr's integration), restore it from the
backup, and check a v1 pane's status in herdr.

## What not to do

- **Never run `herdr plugin link` without reading issue #16, question 5.** There
  is one herdr server, so a linked plugin's hooks run for every pane, v1 panes
  included. The design log records that the manifest is deliberately not
  linked. Read the question and decide there; this runbook does not link it.
- **Never type into a pane** for an agent: no `herdr agent prompt`, no
  `send-keys`, no `pane run`. Agents talk with `fleet-switchboard send`.
- Never start v2 by a bare name or from a login shell's `PATH`.
- Never run `$V2_REAL` without the wrapper, not even for `--version`: the Lab
  saw a bare `--version` append a line to the log v1 uses.
- Never point `v1_opencode` at anything but the v1 binary, and never run v1 with
  anything but `export` from the switchboard.
- Never import while the agent is mid-turn, or two agents at once.
- Never copy v1's credential files into `$V2_HOME`.

## Go / no-go

Go only if every row is "go". Decide before the first import, and again before
retiring the last v1 pane.

| Check | Go | No-go |
|---|---|---|
| v2 version is the pinned one | `--version` matches S6 | Anything else |
| Wrapper confines v2 | `debug paths` all under `$V2_HOME`; v1's data unchanged | Any shared path |
| Backups restore | The scratch restore exports the right message count | Not tried, or counts differ |
| herdr integration | A v1 pane and a v2 pane both show status | Either shows none |
| Dry run | The dropped and filled-in lists are what you expected | A kind you cannot explain |
| Import checklist | Every box ticked for this agent | Any box open |
| Switchboard | `status` lists the agent; a test note reaches it | The agent is missing |
| Heartbeat | Still running while a v1 pane remains | Stopped too early |
| Rollback | You know the v1 session id and have opened it once | Not tried |
