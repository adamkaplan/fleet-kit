# Evaluating the judge (`bin/judge-eval`)

The policy judge's quality is measured, not guessed. `bin/judge-eval` replays a labelled set of real tool calls through
the judge's own functions and reports a
confusion matrix, precision and recall for allow, ask and deny, latency p50/p95, cost per call and a per-category
breakdown. The label standard is `docs/judge-rubric.md`.

**What is shared with the live judge, and what is not.** Shared, called directly from `bin/fleet-switchboard`: the four
questions, `policy_state` (the state the model sees), `redact_command_text` (credentials are taken out of arguments and,
for the session-context experiments, of the brief and turns), the model client, `judge_tool_call` and `combine_policy`
with the shipped default thresholds (and the no-ask rule that switches the intent and scope questions off). The routine
pre-filters (a plain `send`, the repo-commissioning rule) are applied only when the experiment enables them. **Not
shared:** the daemon and its socket, the verdict and context caches, identifying the agent from its session, resolving
the ask and the standing authority from GitHub (a row carries only the Intent and Done-when it was judged with; a row with
no authority text is judged as "none stated"), and the budget. So the numbers measure the model and the questions, not
the plumbing around them.

**The rows are private.** They hold real arguments and intents. Keep them, the offline cache, the context file and the
misclassified list outside the repo; the script refuses to write them inside it. Only counts go on GitHub.

```
bin/judge-sample --out sample.jsonl                       # extract shadow verdicts to label
judge-eval import --sample sample.jsonl --labels labels.jsonl --out rows.jsonl
judge-eval fetch-context rows.jsonl --out context.json    # the agent's brief and last turns (read-only v2 reads)
judge-eval run rows.jsonl --experiment baseline --offline-cache cache.jsonl --misclassified wrong.jsonl
judge-eval run rows.jsonl --experiment session-context --context context.json --offline-cache cache.jsonl
judge-eval sweep rows.jsonl --offline-cache cache.jsonl   # thresholds, no model calls
judge-eval experiments                                    # the named configs and how each differs from baseline
```

`--offline-cache` answers from the file when it already has the request, and records new answers: re-running an
experiment, or sweeping thresholds, spends nothing. `--stub` never touches the network (tests). `--max-calls`
(default 200) and `--max-cost` bound the spend; `--concurrency` bounds parallelism; `--seed` and `--limit` make a
sample deterministic.

## Experiments

An experiment is a named config in `EXPERIMENTS`; a change is a diff against `baseline`:

| name | change |
|---|---|
| `baseline` | today's judge: the four questions, shipped thresholds, routine pre-filters on |
| `session-context` | the state also carries the agent's brief (its first instruction) and its last 3 turns before the call |
| `routine-wording` | the intent and scope questions say that routine work in the agent's own branch is inside its task |
| `context-and-wording` | both |
| `no-prefilters` | the routine rules that skip the model are off |
| `strict-thresholds` | the three soft questions fire at 0.97 |

## Reading the numbers

- A share of false asks stated from this set (for example "about 3 of 4 asks are false") is an **upper bound**: the sample is drawn from calls the judge flagged, so it over-represents its mistakes.
- A sample drawn from shadow verdicts is **skewed**: almost every row is a call the judge flagged. Precision of ask and
  deny, and the false-ask rate on allow, are the meaningful numbers; deny recall needs true denies in the set.
- A threshold chosen on the labelled set is tuned to it. Confirm on rows not used to choose it.
- Cost and latency come from the live replay; cached rows report the latency recorded when they were first asked.
