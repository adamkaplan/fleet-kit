# Switchboard scenarios

Each file here is one behaviour the switchboard promises, written as a JSON
scenario. A scenario is the acceptance test for that behaviour: it runs offline
against fakes (`bin/test-switchboard`, in CI) and in the Lab against real
OpenCode v2 and herdr (`bin/proof-switchboard`). Its control switches the
safeguard off, and with the safeguard off the scenario must fail.

The full format, tiers, oracles and catalog are in
[docs/switchboard.md](../../docs/switchboard.md#scenario-testing).

## Fields

| Field | Required | What |
|---|---|---|
| `id` | yes | One or two capital letters and a number, such as `N1` |
| `title` | yes | One line |
| `problem` | yes | Which of problems 1–4 it covers, or `null` |
| `since_pr` | yes | The PR that adds it; every later PR keeps it passing |
| `tiers` | yes | Any of `offline`, `lab-scripted`, `lab-model` |
| `tags` | no | Free labels, such as `spike` |
| `cast` | yes | Agent name → `role` (`chief-of-staff`, `orchestrator`, `coder`, `cos-subagent`), optional `reports_to` (another cast name), `issue`, `charter`, `repo` (`owner/name`, PR 15) |
| `issues` | no | Issue number (as a string) → `intent`, optional `done_when`, `parent`, `authority` (a charter's `Standing authority:` line), `workspace` (a charter's `workspace:` line, PR 15). With `repos`, a key may be `owner/name#N`; a bare number is in the first repo. `intent: null` is an issue with no Intent section |
| `repos` | no | PR 15, offline only. Repo (`owner/name`) → optional `account` (a `gh` account: the only token that can read the repo), `webhook` (false: that account cannot create a webhook), `exists` (false: the repo is not there yet). Gives the play a fake GitHub per repo, a daemon that derives the watched set, and `gh_users` from the accounts. Four steps and four expectations below need it |
| `steps` | yes | Timed actions: `at` (`0s`, `10s`, `2m`, `2h`; never decreasing), `actor` (`you`, `world` or a cast name), `do`, plus the action's own fields |
| `expect` | yes | What must and must not happen: `that` (the oracle), optional unique `name`, plus the oracle's own fields |
| `config` | no | Engine timing the scenario shortens (`batch_seconds`, `engaged_minutes`; positive numbers), applied by both runners, so the Lab need not wait real minutes |
| `control` | yes | Exactly one of `fault` (a known fault name), `baseline` (the system run instead) or `variant` (an object), plus `fails`: the expectations, by `name` or `that`, the control must break |

## The vocabulary

One vocabulary for both runners. `bin/fleet-scenario` holds a field table for
every action and every expectation, and checks each step and expectation
against it in every tier, so a typo fails `validate`. Each may also carry
`why`, a comment for people.

Field names mean the same thing everywhere: `to` is the agent a step sends to,
or the recipient an expectation reads; `agent` is the agent whose own
behaviour is checked (its model calls, screen, status, tools) or acted on;
`key` is a fact key, a glob in an expectation; `text` is literal text; `count`
is how many.

### Steps

| `do` | Actor | Fields (optional in brackets) | What it is |
|---|---|---|---|
| `turn` | a cast member | [`lasts`], [`steps`] | Work the fleet gave the agent: one prompt of `steps` model calls over `lasts`. It is the fleet's, so you are not engaged |
| `message` | you | `to`, `text`, [`lasts`] | A prompt from you |
| `draft` | you | `to`, `text` (one line) | Typed into the agent's input box, not sent |
| `away` | you | none | You stop prompting |
| `permission` | a cast member | none | The agent asks to run a shell command |
| `permission` | you | `to`, [`decision`: `once`, `always`, `reject`] | Your answer to that ask |
| `comment` | world | `issue`, `body`, [`id`], [`from`], [`repo`] | A GitHub comment (PR 5); `repo` (PR 15) names the repo of a play with `repos` (default: the first) |
| `leave` | world | `agent` | PR 15, offline. The agent's pane is gone: it is no longer a live fleet agent, so a repo only it named is no longer watched |
| `edit-intent` | you, world | `issue`, [`intent`], [`done_when`] | An Intent edit (PR 6); `null` removes the line |
| `close-pane` | world | `agent` | Arms a race: the agent's pane closes the moment the engine reads it again just before writing, after it decided to deliver (PR 4). Offline, a herdr runner wrapper; in the Lab, the engine's herdr shim runs `herdr pane close` on the next `agent get` of that pane |
| `delete-state` | world | none | The engine's state directory is deleted (the daemon is down); the next start makes it again (PR 4) |
| `synthetic`, `note`, `wake` | world | `to`, `key`, `text`, [`id`], [`resume`], [`delivery`: `queue`, `steer`] | A v2 synthetic message sent directly, without the engine (spikes). `note` defaults to resume false + steer, `wake` to resume true + queue, `synthetic` to resume false + steer. The same `id` name in one play is the same derived message id |
| `fact` | world | `to`, `key`, `summary`, [`kind`], [`issue`], [`from`], [`batch`] | A fact for the engine: a line in its Lab fact source, `$STATE/lab-facts.jsonl`, read only when `FLEET_SWITCHBOARD_LAB=1` |
| `launch` | world | `agent`, [`brief`], [`new_workspace`] | `fleet-switchboard launch` for that cast member, with the cast's role, `reports_to`, issue and `repo`, into the herdr workspace of the play. The member has no session until the step runs. The brief is the first prompt, once the pane is verified. With `new_workspace` (PR 15, offline) the step runs the command line, `--new-workspace <label>` and the cast's `--charter`, so the charter's label check and the workspace creation are the real ones |
| `pass` | world | [`faults`] | One engine pass with these faults on, such as `crash-after-send` |
| `kill-daemon`, `restart-daemon` | world | none | While the daemon is down only `pass` steps run the engine; a restart forgets everything in memory |
| `tool-call` | a cast member | `tool` (`shell`, `edit`, `webfetch`, `subagent`, `read`, `glob`, `grep`, `skill`, `other`), `arguments`, [`configured`: `allow`, `ask`, `deny`] | The agent makes a tool call; the offline runner sends it through `judge-tool`'s own path against fakes and Jev's replay answers (`scenarios/jev/policy.json`). An `ask` blocks the agent as v2 does (PR 9). Offline only: the Lab tier is owed to the real plugin |
| `import-v1` | world | `agent`, `fixture` | `fleet-switchboard import-v1` for that cast member, from a hand-made v1 export under `scenarios/` (`scenarios/v1-fixtures/`), with the cast's role, `reports_to`, issue and charter as its `metadata.fleet`. The member has no session until the step runs. Then the runner opens a pane on the imported session, the step a person does (PR 10) |
| `report` | a cast member | `state` (`done`, `failed`, `blocked`, `question`, `working`, `paused`), `text` (one line), [`issue`] | `fleet-switchboard report` as that member (PR 13). Offline only |
| `send` | a cast member | `to`, `text` (one line), [`issue`] | `fleet-switchboard send` as that member (PR 14). Offline only |
| `awaiting` | you, world, a cast member | `issue`, `on` (true puts your awaiting-user label on the issue, false takes it off), [`title`] | The label changes on the fake GitHub and its `issues` webhook event reaches the hub (PR 14). Offline only |
| `delete-decisions` | world | none | `decisions.json` is deleted, as in D4; the next pass writes it again (PR 14). Offline only |
| `wait` | world | none | Marks the end of the play |

### Expectations

| `that` | Fields (optional in brackets) | Passes when |
|---|---|---|
| `delivered` | `to`, `key`, [`mode`: `note`, `wake`], [`count`], [`keys`], [`text`] | The recipient's transcript holds synthetic messages whose `metadata.fleet.keys` match `key`: `count` distinct messages (default: at least one), carrying `keys` distinct matching keys, each reaching the model as `mode`, and (with `text`) containing `text`. An item still waiting in v2's inbox is not delivered |
| `message_count` | `to`, `count` | The switchboard wrote `count` distinct messages to the recipient (`metadata.fleet.from` is `switchboard`), in its transcript or waiting in its inbox |
| `model_saw` | `agent`, `key`, [`when`: `any`, `turn_start`, `between_steps`, `after_reply`], [`count`] | `count` model calls (default: at least one) of that phase had a message carrying `key` among their new inputs |
| `no_machine_turn` | `agent` | No model call was triggered by a fleet message |
| `within` | `agent`, `step` (an index), `seconds`, [`key`] | With `key`: the message carrying it was delivered and answered within `seconds` of the step. Without: the agent's next final reply was |
| `draft_intact` | `agent`, `text` | The input box still holds `text`, and no message contains it |
| `pane_runs_v2` | `agent` | herdr names the agent in the pane that shows the session `opencode`, and the pane's process is the configured v2 executable on that session |
| `no_typing` | `agent` | The pane was started by argv, so it has no shell, and nothing was typed: the pane's first process is the v2 itself (Lab: herdr's `process-info`; offline: the fake's launch record and its typing log) |
| `status_is` | `agent`, `status` (`working`, `idle`, `done`, `blocked`, or a list), `at` | herdr's status at that time |
| `tool_outcome` | `agent`, [`tool`], `outcome` (`completed`, `error`) | The agent's last call of `tool` (default `shell`) ended so, and a completed result went back to the model |
| `metadata_roundtrip` | `agent`, `of` (`session`, `message`), [`key` or `text`], [`fleet`] | v2 returns `metadata.fleet` exactly: the cast's, or `fleet`; a message is named by exactly one of `key` or `text` |
| `message_has_intent` | `to`, `issue` | The delivered text carries the issue's Intent lines (PR 6) |
| `message_shows` | `to`, `text` | A message delivered to the recipient contains `text` (PR 6) |
| `intent_gap` | `issue` | `fleet-switchboard status` lists the issue as referenced by an agent but lacking an Intent section (PR 6) |
| `classified` | `agent`, `step`, `as` | Jev classified that message so (PR 7) |
| `handed_off` | `issue` | A brief on the ask and a `cos-subagent` session for it (PR 8) |
| `toast` | [`agent`], [`text`], [`count`] | herdr was asked to show a toast naming `agent` in its title or body and carrying `text` in its body (`count`: exactly that many; default: at least one). In the Lab, read from the engine's herdr shim, which records each `notification show` |
| `repo_watch` | `repo`, `how` (`forwarder`, `polling`, `failed`, `absent`) | PR 15, offline. How the daemon's hub watches the repo at the end of the play; `absent`: it does not |
| `repo_membership` | `repo`, `joined`, `left` | PR 15, offline. The audit shows the daemon started a watch for the repo (`joined`) and stopped it (`left`) |
| `status_says` | `text` | PR 15, offline. The GitHub lines of `fleet-switchboard status` at the end of the play contain `text` |
| `launch_refused` | `agent`, [`text`] | PR 15, offline. The `launch` step with `new_workspace` exited non-zero with `text` in its error, and no herdr workspace and no session were made |
| `policy_outcome` | `agent`, `step`, `outcome` (`allow`, `ask`, `deny`), [`reason`], [`judged`], [`cached`], [`jev_calls`] | What `judge-tool` answered for that `tool-call` step: the outcome, a reason containing `reason`, whether Jev's verdict was used, whether it was cached, and how many times Jev was asked (PR 9; offline) |
| `judged` | `agent`, `question`, [`threshold`] | Jev answers yes about the agent's message (lab-model) |
| `imported_messages` | `agent`, [`count`] | v2 holds the imported session with one message per v1 message (`count`, default: all of them), in v1's order, with v1's timestamps and the text that was typed (PR 10) |
| `lists_asks` | `agent`, `issue` | `fleet-switchboard intents`, run in the pane that shows the agent's session, lists the ask `issue`: the agent is found by its `metadata.fleet` (PR 10) |
| `decisions` | `at`, `count`, [`text`] | At the time `at`, the list `fleet-switchboard decisions` reads from `decisions.json` has `count` decisions (those whose title holds `text`, if given); a file the command calls stale lists none (PR 14). Offline only |
| `decisions_say` | `at`, `text` | At the time `at`, the command's output contains `text` (PR 14). The Lab reads it only when the play ends, so `at` must be the time of the last step there |

**Mode.** Whether a message was a note or a wake is read from what it did,
in both runners, not from the flags it was sent with. A message is a **wake**
when it was the input that called the model: the last new input of a model
call that started a turn from idle, or that ran after the turn's final reply.
It is a **note** when it rode along with another input (your next message),
or was injected between the steps of a running turn. In the Lab the runner
reads this from the scripted model's request log, matched to the transcript;
offline, from the fakes' model calls. So a wake sent with `steer` (the
`steer-not-queue` fault) on a busy agent counts as a note: the agent took it
mid-turn.

### Controls

Exactly one of:

| Control | Fields | What it does |
|---|---|---|
| `fault` | a known fault name | The scenario plays again with `FLEET_SWITCHBOARD_FAULT` set on every engine process; only scenarios that run the engine |
| `variant` | `{"steps": {"<index>": {fields}}, "expect": {"<name or index>": {fields}}, "issues": {"<number>": {fields}}, "repos": {"<owner/name>": {fields}}}` | The scenario plays again with these fields patched in. The patched scenario must itself be valid |
| `baseline` | `fleet-heartbeat` or `timer-heartbeat` | `timer-heartbeat` (offline only, not built in the Lab) is the old timer waking each orchestrator every 30 minutes. `fleet-heartbeat`: today's system delivers instead. The runner, the only code allowed to type and only into Lab panes, types each `fact`'s wake with `herdr agent prompt` (`[heartbeat] <summary>`), and the engine's daemon does not run. The fake herdr's `agent prompt` does what the TUI does: it submits the input box's text followed by the prompt |

plus `fails`: the expectations, by `name` or `that`, the control must break.
A control that breaks none of them proves nothing, and fails the run.

## The offline runner

A scenario with `issues` gets a fake GitHub (`gh api` GETs only) and an `edit-intent` step feeds `intent_change_fact`; `judged` is not evaluated offline.

`bin/test-switchboard` runs every scenario with the `offline` tier against
stateful fakes of v2 and herdr and a simulated clock: a daemon pass every 5 s,
steps at their times. Expectations are judged on the fakes' state
(transcripts, inbox, model calls), never on the switchboard's report. Then it
re-runs the scenario with the control's fault, and the test fails if any
expectation named in `fails` still passes. Every run also checks that each
write was audited before and after, and that herdr was never asked to type.

## The Lab runner

`bin/proof-switchboard run [paths] --tier lab-scripted` plays each scenario
with the `lab-scripted` tier in the Lab, in real time, on fresh v2 sessions,
then plays its control. Every cast member gets a warm-up turn first; that
brief, and every `turn`, are marked as the fleet's (`metadata.fleet`), so
only a `message` counts as you prompting.

A `launch` step runs `fleet-switchboard launch` on the same private config,
which also names `opencode_executable` (the Lab's real v2 binary, because the
wrapper execs it and a pane's process is the binary) and `launch_env` (the
marker that lets the wrapper keep the pane's herdr identity). The runner passes
the Lab workspace id, checks the new pane is inside it, and closes it when the
play ends.

A scenario with a `fact`, `pass` or daemon step runs the real engine,
`bin/fleet-switchboard`, with `XDG_CONFIG_HOME` and `XDG_STATE_HOME` in a
fresh directory under `$LAB/runs/engine/`, `FLEET_SWITCHBOARD_LAB=1`, and a
config whose `opencode` is the Lab wrapper's absolute path. Its daemon polls
every 5 s from the start until the last step's time. Every cast member runs
the Lab TUI in its own pane of the "Switchboard Lab" workspace (more panes
are split there without focus), checked to run the Lab's v2 binary, so the
engine finds it as it finds a fleet agent: herdr agent, its session, the
session's `metadata.fleet.name`. A `fault` control plays again with the fault
on every engine process; `kill-daemon` and `restart-daemon` stop and start
the daemon, and `pass` runs one `run --once`, as the offline runner does.

Expectations are read from the systems that own the facts: the recipient's
v2 transcript and inbox, the scripted model's request log, herdr's status
and pane reads. The engine's output and audit log go into the evidence,
never into a verdict. Evidence: `scenario-runs/<UTC>/<id>/report.md` and
`timeline.jsonl`.

## Commands

```bash
bin/fleet-scenario validate            # every *.json here; CI runs this
bin/fleet-scenario list
bin/fleet-scenario coverage --enforce  # exit 1 if a problem has no scenario
bin/fleet-scenario coverage --enforce --through-pr 4  # only problems due by PR 4; CI runs this
```

Runs write evidence to `scenario-runs/`, which is not committed.
