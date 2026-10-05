# Trying the switchboard

A way to sit in front of the switchboard with a real model. It is the Lab's
isolation (see [switchboard.md](switchboard.md#lab-isolation-from-the-live-fleet))
with GitHub Copilot instead of the scripted model: its own OpenCode v2 data,
config and login, its own switchboard config and state, its own herdr
workspace, and a scratch GitHub repo. It reads and writes nothing in the live
OpenCode, herdr or switchboard directories.

Everything is driven by `bin/switchboard-trial`.

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

Once ever: `init` (it also creates the charter labels in the repo and labels the charter, and pins the `gh` account the agents run as: `--gh-user NAME`, default the one active then), then `login`. After that, one command per session:

| When | You run | It does |
|---|---|---|
| Start, or after a reboot | `bin/switchboard-trial up` | Starts whatever is not running: the v2 service, the daemon, the workspace, `cos` and `platform`. |
| Run it again while things are up | `up` | Nothing to the parts that are running. It never restarts the v2 service, which would cut the panes off mid-turn. |
| After `down` or a reboot | `up` | Reopens `cos` and `platform` on their **existing sessions**, so the conversation is kept. `up --fresh` creates new sessions instead. |
| An agent needs a fresh process | `bin/switchboard-trial restart [cos] [platform]` | Closes the agent's pane and reopens it on the **same session**. Use it after updating the kit or the token: a new process gets the wrapper's current environment and the current role files. No names means both. |
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
   launch a `coder` (`fleet-switchboard launch`). When it finishes, `platform`
   gets a note, not a turn, while you are mid-conversation with it.
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

The sidebar badge (`unread`) is only shown if your herdr sidebar config uses
the `$unread` token; that setting is yours.

## Where to look

| What | Where |
|---|---|
| Everything the switchboard did | `<trial dir>/profile/state/fleet-switchboard/audit.jsonl` |
| The daemon's own output | `<trial dir>/daemon.log` |
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
- The sidebar badge, the Lab tier of several scenarios and the lab-model
  scenarios are not exercised here; the trial is for you to find what they
  would have.
- The trial does not import a v1 session.
