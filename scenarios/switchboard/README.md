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

## Commands

```bash
bin/fleet-scenario validate            # every *.json here; CI runs this
bin/fleet-scenario list
bin/fleet-scenario coverage --enforce  # exit 1 if a problem has no scenario
```

Runs write evidence to `scenario-runs/`, which is not committed.
