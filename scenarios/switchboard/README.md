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
| `cast` | yes | Agent name → `role` (`chief-of-staff`, `orchestrator`, `coder`, `cos-subagent`), optional `reports_to` (another cast name), `issue`, `charter` |
| `issues` | no | Issue number (as a string) → `intent`, optional `done_when`, `parent`. `intent: null` is an issue with no Intent section |
| `steps` | yes | Timed actions: `at` (`0s`, `10s`, `2m`, `2h`; never decreasing), `actor` (`you`, `world` or a cast name), `do`, plus the action's own fields |
| `expect` | yes | What must and must not happen: `that` (the oracle), optional unique `name`, plus the oracle's own fields |
| `control` | yes | Exactly one of `fault` (a known fault name), `baseline` (the system run instead) or `variant` (an object), plus `fails`: the expectations, by `name` or `that`, the control must break |

## The offline runner

`bin/test-switchboard` runs every scenario with the `offline` tier against
stateful fakes of v2 and herdr and a simulated clock: a daemon pass every 5 s,
steps at their times. Expectations are judged on the fakes' state
(transcripts, inbox, model calls), never on the switchboard's report. Then it
re-runs the scenario with the control's fault, and the test fails if any
expectation named in `fails` still passes. Every run also checks that each
write was audited before and after, and that herdr was never asked to type.

In a scenario with the `offline` tier, these kinds may carry only these
fields (plus `why`, a comment), so a typo fails `validate`:

| Kind | Fields |
|---|---|
| step `fact` | `to`, `key`, `summary`; optional `kind`, `issue`, `from`, `batch`. Appends a fact to the Lab fact source, `$STATE/lab-facts.jsonl`, read only when `FLEET_SWITCHBOARD_LAB=1` |
| step `turn` | optional `lasts` (a duration), `steps` (model calls in the turn). A turn the fleet started, so you are not engaged |
| step `message` | `to`, `text`; optional `lasts`. A prompt from you |
| step `pass` | optional `faults`: one pass with these faults on, such as `crash-after-send` |
| step `kill-daemon`, `restart-daemon`, `wait` | none. While the daemon is down only `pass` steps run; a restart forgets everything in memory |
| `delivered` | `to`, `key` (a glob); optional `mode` (`note`, `wake`), `count` (messages in the transcript carrying a match), `keys` (distinct matching keys) |
| `message_count` | `to`, `count`: messages the switchboard wrote to that agent |
| `model_saw` | `agent`, `key`; optional `when` (`any`, `turn_start`, `between_steps`, `after_reply`), `count`: model calls that carried a match |

Lab-only scenarios (the spikes) are not checked field by field; the Lab
runner owns their extra fields.

## Commands

```bash
bin/fleet-scenario validate            # every *.json here; CI runs this
bin/fleet-scenario list
bin/fleet-scenario coverage --enforce  # exit 1 if a problem has no scenario
```

Runs write evidence to `scenario-runs/`, which is not committed.
