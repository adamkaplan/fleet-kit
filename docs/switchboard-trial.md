# Trying the switchboard

A way to sit in front of the switchboard with a real model. It is the Lab's
isolation (see [switchboard.md](switchboard.md#lab-isolation-from-the-live-fleet))
with GitHub Copilot instead of the scripted model: its own OpenCode v2 data,
config and login, its own switchboard config and state, its own herdr
workspace, and a scratch GitHub repo. It reads and writes nothing in the live
OpenCode, herdr or switchboard directories.

Everything is driven by `bin/switchboard-trial`.

**This is a thin wrapper for an isolated experiment.** Since PR 16 it puts the kit's agents, skills and
plugins into its own profile with the same functions `fleet-switchboard install` uses (there is one path,
not two installers), and everything else it does (a pinned `gh` account, a scratch repo and charter, its own
workspace and service port) is what makes it an experiment. A real user does not run it: they follow
[INSTALL.md](../INSTALL.md), whose Fleet Switchboard steps run `fleet-switchboard install`, then
`fleet-switchboard key set` and `fleet-switchboard bootstrap`. See
[switchboard.md](switchboard.md#install-pr-16).

## What you need

- herdr running (the trial adds one workspace to it), `gh` logged in, `node`
  and `npm` (the trial installs its own OpenCode v2 under its data directory).
- A scratch GitHub repo you can create issues in, with a **charter** issue and
  at least one **ask** as a native sub-issue of it. A charter body begins with
  a flat yaml block (see `skills/fleet-charter/SKILL.md`) and may carry a
  `Standing authority:` line. An ask body is:

  ```
  ## Intent
  <one line>
  Done when: <one line>
  ```

- `OPENROUTER_API_KEY` exported in the shell that runs `up`. Without it the
  classifier and the tool-call judge answer "unreachable" and change nothing;
  everything else still works.

## Set up, once

```bash
bin/switchboard-trial init --repo OWNER/REPO --charter <charter issue number>
bin/switchboard-trial login
```

`login` runs the trial's own `opencode auth login` for GitHub Copilot: you get
a device code and a URL. The credential lands under the trial's data directory
only. `--model` on `init` picks the model for every agent (default
`github-copilot/claude-sonnet-5.5`); `up` checks it against what v2 lists.

## Start

```bash
bin/switchboard-trial up
```

This starts the trial's v2 service (port 49380, so it can sit beside the Lab on
49374) and the switchboard daemon, opens a herdr workspace called **Switchboard
Trial**, and launches two agents by argv, never by typing: `cos` (Chief of
Staff) and `platform` (an orchestrator for your charter). Open the workspace in
herdr and talk to `cos`.

`status` shows what is running; `down` stops the daemon and the service and
leaves the workspace for you to close.

## What you do each time

Once ever: `init` (it also creates the three labels in the repo, `<user>:orchestrator`, `<user>:awaiting-cos` and `<user>:awaiting-user`, and labels the charter, and pins the `gh` account the agents run as: `--gh-user NAME`, default the one active then), then `login`. After that, one command per session:

| When | You run | It does |
|---|---|---|
| Start, or after a reboot | `bin/switchboard-trial up` | Starts whatever is not running: the v2 service, the daemon, the workspace, `cos` and `platform`. |
| Run it again while things are up | `up` | Nothing to the parts that are running. It never restarts the v2 service, which would cut the panes off mid-turn. It installs the Decisions plugin into the profile (idempotent) and refreshes the wrapper if it is older than the plugin needs; a TUI that is already open does not see either until it is restarted. |
| After `down` or a reboot | `up` | Reopens `cos` and `platform` on their **existing sessions**, so the conversation is kept. `up --fresh` creates new sessions instead. |
| An agent needs a fresh process | `bin/switchboard-trial restart [cos] [platform]` | Closes the agent's pane and reopens it on the **same session**. Use it after updating the kit or the token: a new process gets the wrapper's current environment, the current role files and the Decisions plugin. No names means both. |
| Stop | `bin/switchboard-trial down` | Stops the daemon and the service. Closing the workspace is yours. |

Not needed each time: logging in again, setting up the repo, or exporting the
OpenRouter key by hand if your shell startup already does. herdr itself must be
running. Nothing starts at login: after a reboot the daemon, service and agent
tabs are gone until you run `up`. (Starting the daemon from herdr automatically
is the plugin hook, which is not linked; see issue #16 Q5.)

## What to try

Each of these is something the stack claims; watch whether it holds.

1. **A hand-off.** Tell `cos`: "Hand ask #N to platform." It should send
   `platform` a `[switchboard]` message that opens with the ask's Intent and
   Done when. Check with `fleet-switchboard intents` in a shell.
2. **A comment reaches the owner.** Comment on the ask in GitHub. The forwarder
   (`gh webhook forward`, one child per repo) delivers it to `platform` as a
   message under that ask, within seconds. A repeat of the same comment is not
   delivered twice.
3. **An Intent edit is surfaced.** Edit the ask's `## Intent` line on GitHub.
   `cos` gets a message showing old and new, and herdr shows a toast.
4. **A worker finishes while you talk to someone else.** Have `platform`
   launch a `coder` (`fleet-switchboard launch`). The coder should end with
   `fleet-switchboard report done "<one line>"`; `platform` then gets a note,
   not a turn, while you are mid-conversation with it, and the bare idle that
   follows wakes nobody. A coder that stops without reporting is nudged once.
   Every switchboard message shows as a one-line row in the transcript.
5. **An unrelated message while `cos` is busy.** Give `cos` a long task, then
   ask something unrelated. It should be told to hand the first task off
   (`fleet-switchboard handoff`) before it answers you. If the decoration
   arrives after `cos` already acted on your message, the audit log says
   `foreground.late`; that rate is the thing to watch.
6. **The judge.** Ask `platform` to force-push or run a deploy command. It
   should stop on a permission prompt carrying the judge's reason, and herdr
   should show the pane as blocked with a toast.
7. **Nothing types into your panes.** A draft you leave in an agent's input box
   survives anything the switchboard delivers.

8. **The Decisions list.** Open a session in a terminal at least 140 columns wide
   and look at the right sidebar: below the session's own sections there is a
   **Decisions (N)** section. Put the `<user>:awaiting-user` label on an issue in
   the repo (`<user>` is `whoami`), or have a coder run `fleet-switchboard report
   question "<one line>"`: within a few seconds a row appears with an id, how long
   it has waited and its title. Refer to it by id in chat ("answer #5: yes"). Take
   the label off, or answer the coder with `send`, and the row goes. An empty list
   shows `none`. Stop the daemon (`down`, then wait two minutes) and the section
   reads `daemon not updating` rather than a stale list. In a terminal without the
   plugin, or before you restart a TUI, `fleet-switchboard decisions` prints the
   same list (run it with the trial's config, as for `pending` below) and
   `decisions --watch` keeps it on screen in a herdr side pane. Agents should say
   a decision once, with its id, and not repeat it: ask `cos` "what is waiting on
   me?" and it should answer "see Decisions".

9. **Tiers.** Your list shows only what the Chief of Staff escalated to you. Put
   `<user>:awaiting-cos` on an issue, or have `platform` run
   `fleet-switchboard report question "<one line>"`: it is **not** in your panel,
   and the `cos` pane's panel (title `Decisions (N) all`) shows it. `cos` works its
   list on its wake: it resolves what your orders cover and escalates the rest
   (`decisions escalate <id> --reason ...`), and the row then appears in yours.
   A permission prompt of an agent shows in `cos`'s view at once and in yours
   after five minutes (`decisions_cos_grace_seconds`). Your orders for `cos` are
   one file, `standing-orders.md` beside the trial's `config.json`
   (`<trial dir>/profile/config/fleet-switchboard/`): `install` makes a template, you
   edit it, and `fleet-switchboard orders --cos` prints it. A panel gets its view
   from the `FLEET_SWITCHBOARD_ROLE` and `FLEET_SWITCHBOARD_REPO` that `launch` puts
   in the pane's environment, so a TUI that was running before this version needs a
   restart (`switchboard-trial restart <name>`).

The sidebar badge (`unread`) is only shown if your herdr sidebar config uses
the `$unread` token; that setting is yours.

## Where to look

| What | Where |
|---|---|
| Everything the switchboard did | `<trial dir>/profile/state/fleet-switchboard/audit.jsonl` |
| The daemon's own output | `<trial dir>/daemon.log` |
| What waits on you | The **Decisions** section of the TUI sidebar, or `fleet-switchboard decisions` (with the trial's config, as below); the file is `<trial dir>/profile/state/fleet-switchboard/decisions.json` |
| What is pending for an agent, and why it waits | `fleet-switchboard pending <name>` (run it with the trial's config: `XDG_CONFIG_HOME=<trial dir>/profile/config XDG_STATE_HOME=<trial dir>/profile/state`) |

`<trial dir>` is `$XDG_DATA_HOME/fleet-switchboard-trial`, by default
`~/.local/share/fleet-switchboard-trial`.

## What the trial gives the agents, and does not

- The v2 service's environment has every credential-shaped variable removed
  (anything ending `_API_KEY`, `_SECRET`, `_PASSWORD`, `_ACCESS_KEY`, or starting
  `GH_`, `GITHUB_`, `COPILOT_`). The OpenRouter key stays in the daemon's
  environment only; a plugin spawned by v2 shares the agents' shell
  environment, so it never has the key and asks the daemon instead.
- **It does give the agents your `gh` token as `GH_TOKEN`**, because they work
  on GitHub. That token has whatever scopes your `gh` login has. The tool-call
  judge and the repo being a scratch repo are the guard rails; they are not a
  sandbox. Use a `gh` account whose reach you are content to lend, and do not
  point the trial at a repo you care about.
- Agents run with the kit's agent definitions, which allow shell commands. The
  judge (`policy.enabled`) is on in the trial and turns an allowed
  consequential call into a prompt, never the reverse.

## Known limits

- The classifier acts (`classifier_mode: act`) in the trial, with the default
  confidence threshold of 0.7. It was right on 15 of 15 hand-written cases; real
  conversations are the test.
- The Decisions section is only drawn on the session screen, and only in a
  terminal wide enough for the sidebar; v2 gives plugins no documented API, so a
  v2 upgrade can drop it silently (the command is the fallback).
- The sidebar badge, the Lab tier of several scenarios and the lab-model
  scenarios are not exercised here; the trial is for you to find what they
  would have.
- The trial does not import a v1 session.
