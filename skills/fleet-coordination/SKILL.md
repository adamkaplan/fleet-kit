---
name: fleet-coordination
description: The briefing and reporting protocol between an orchestrator and the coders it dispatches. Use when writing an assignment brief, acknowledging one, reporting progress or a blocker on an issue, deciding what counts as evidence, or working out when to escalate instead of trying again.
---

# Fleet Coordination Protocol

Two halves. An orchestrator writes a brief that a coder can act on without asking
a follow-up question. The coder reports back in a shape the orchestrator can read
without opening a terminal. Both halves live on the GitHub issue, because that is
the only part of this that survives a dead pane.

GitHub issues own assignments, decisions, dependencies, progress and evidence.
Local todo lists are mirrors, never the record. One owner holds an assignment at a
time; reassigning it needs an explicit handoff from the orchestrator, not a coder
deciding to pick something up.

Write GitHub text so a person can read it: short paragraphs, real Markdown lists with one item per line, headings for sections, never an inline "(1) ... (2) ..." chain, and code and commands in code spans or blocks.

## The brief

Send one self-contained brief at dispatch. Later updates can be shorthand; the
first one cannot.

```text
ISSUE:      <full repository-qualified GitHub issue URL>
ASK:        <the ask's Intent and Done when, copied verbatim from its issue>
GOAL:       <the outcome, in one sentence>
DONE:       <what actually counts as accepted — the test, not the intention>
SCOPE:      <the work that is included>
STOP:       <holds and exclusions — what you must not touch>
NEXT:       <the next deliverable and who owns it>
AUTH:       <the authority already granted, explicitly>
CHECKPOINT: <a UTC time or an agreed event, with an overdue time>
```

Every builder brief carries the builder checklist below by default; a brief that leaves an item out says why in one line.

## Builder checklist

The mistakes that cost the most review rounds and the most lost work. Meet them in the first push.

- (a) A table in the documentation of every input and every outside call of the new code; each row names its test and its run-by-hand mutant.
- (b) Code that reads a directory other programs write uses the one shared safe reader (opens without blocking, refuses links, accepts only regular files, bounds the count, the size and the time). The repository has no such shared module yet (no `O_NONBLOCK` in `bin/`); the closest is `_read_settings_file` in `cloud/launcher/cloud_worker.py` (a single file, not a directory) and `_read_private` in `bin/cloud_worker.py`. Name the reader you use by its real module and function, and do not invent one.
- (c) A test that checks a completeness claim derives its list from the code's syntax tree (`ast`), never a regular expression, and has a test that adds a new case in a differently spelled form and must fail.
- (d) The body of a held pull request is in the owner format from the first push.
- (e) A cloud builder opens its pull request as a draft at its first push, then updates it: the pod's git token expires after about an hour and a late open fails.
- (f) A merger pod pushes the merge commit the minute it exists and runs the test classes after it, so a finished merge is never left unpushed when the pod ends.
- (g) A guard or scan test states its closed rule and its known-limits list in its first version, and fails closed: anything not proven harmless is a site. Reviewers judge the rule and the limits: a spelling inside the stated limits is a Low, a spelling that escapes a rule claimed closed is a High.

A coder that posts a plan also reports `working: plan posted <hash> <url>` (a report arrives even when a send is lost).

A brief for a credential, permission or privileged-script change links `docs/adversarial-checklist.md`.
The pull request body answers each item of it in one line. A Mermaid diagram appears only where it shows data flow, system design or component design, never at the top of a body and never as a summary of what changes, what it touches, what is irreversible and the risk.

Before the coder's first push, it runs both focused (a push voids proofs):

```text
bin/test-switchboard -k test_the_role_files_do_not_grow   # the role-file byte cap
bin/test-switchboard -k TestHygiene                       # shebang, stdlib-only, executable bits
```

**Local runs are focused by default.** Run the focused classes of what you changed through `heavy --short`; CI runs the whole suite. Run the whole suite only for a change to the CI workflow itself or to a test CI cannot run; say which in the pull request. The heavy wrapper refuses a second full-suite job in a worktree while one is queued or running.

A change that deletes something or raises an alert is checked against three lines before its first push:

- Drive the real code path in the tests, never a copied helper.
- Derive every set of kinds from one table, with a completeness test that fails when a kind is missing from it.
- Read the sibling kinds and the consumers yourself.

When a review finding names a gap in one member of a table, list, set or rule family (a row, a kind, an input, a rule), the fix covers every member in the same head: derive the set from the code, never a hand-listed copy, parametrise the tests over it with one mutant per member, and say in the PR body which members were checked; reviewers name the class, not only the instance.

Some repositories' workflows skip draft pull requests (a coder lost four hours reading that as a failing CI): when a draft shows no CI runs, look at the workflow's draft filter and mark the PR ready before waiting for CI (if there is no filter, the cause is elsewhere).

Before marking the pull request ready, `fleet-switchboard preflight-pr` runs the CI-critical focused classes you otherwise skip (command surface, hygiene, public names, and the cloud suites when `cloud/` changed) and prints one PASS or FAIL line each.

When a scenario's evidence is your own measurement, from your own pane launch a tester of your lane (`--role tester --repo <the pull request's repo> --reports-to <you>`, headless is enough) to re-measure and record the verdict: never record as yourself, and do not ask for a waiver.

`ASK` carries the Intent and Done when of the ask this assignment serves
(`fleet-switchboard intent <issue>` prints them), so a coder who never sees the
charter still knows what the work is for. `GOAL` and `DONE` narrow the ask to this
assignment; they never widen it, and a report is held to the ask's Done when as
well as to `DONE`. Messages to the orchestrator go with
`fleet-switchboard send <name> --issue <n> "<text>"`, never by typing into a pane; reports
go with `fleet-switchboard report <done|failed|blocked|question|working|paused|withdrawn> [--issue <n>]
"one line"` (at most 300 characters; detail goes on GitHub). Report at real deliverables, state
changes and failures; the final report names the result and its evidence link, then stop. After
`blocked` or `question`, end the turn at once. A worker that stops without reporting is nudged
once, and the second time its orchestrator is told it stopped. When woken by something that
changes nothing for you, do not report and do not answer at length.

A `question` or `blocked` report is also what a decision waiting on someone looks like: the
person you report to sees open ones in a read-only Decisions list, each with an id. Say it once, in
the report, and do not repeat "I am still waiting" in later replies; the boss's `send` on that ask
answers it. A decision goes up a tier at a time: a worker's to its orchestrator, an orchestrator's to
the Chief of Staff (with the issue's `<user>:awaiting-cos` label), which resolves it from the owner's
orders or escalates it to the owner. Never apply `awaiting-user` yourself.

A PR held for the owner carries the `<user>:merge-captain` label and a `<user>:hold-<reason>` label (`security`, `credential`, `contract`, `policy`, `prod-config` or `other-area`) and gets a `fleet-switchboard notice pr`; keep `MERGE: captain` as the first line of its body too, in repos whose own checks read it and until 2026-11-10, after which it is dropped. A held PR's body then opens (after those lines) with `## Why this needs you`, `## What changes for you` (a Before | After table), `## Example`, an optional real image, then `## Questions for you` (each with a Recommendation:); any Mermaid diagram goes below. Any other owner decision is a `report question` PLUS a `## Decision required` section at the top of the issue body (and the `<user>:awaiting-cos` label, never `awaiting-user`), never only words in a report: a report or comment saying something waits on the owner does not reach the panel; the section and label do. The switchboard sweep (#117) flags mismatches to cos. A deliberately deferred item carries `<user>:parked` and a `Parked until: <YYYY-MM-DD>` line in its body; it is exempt from the no-next-step flag until that date (never from an owner wait or a held PR).

Where a repository has an `integration` branch, typical changes merge into it and reach the default branch in one daily batch pull request, opened and kept by the Chief of Staff's lane; the owner clears the batch. Urgent fixes and asks the owner makes alone go straight to the default branch. A held change is batched too, held and cleared on its own pull request by the matrix's rules, and the batch holds for the owner when any member touched a top-tier path until #835 lands, then only when a row is rated High. While the batch is out of draft (ready for the owner's clear), a freeze applies: nothing merges into `integration`. Keep `integration` current with the default branch before a pull request opens into it: fast-forward it, or merge the default branch into it when it holds unbatched members; a stale `integration` makes the diff and the impact floor read the whole difference. See `docs/switchboard.md`, "Batching".

Related findings go up as one **batch**, never one question each: `fleet-switchboard decisions batch
--title "<title>" --row "<repo#N> | <finding> | <recommendation>" ...` (or `--file`, or stdin) creates one
`awaiting-cos` issue in your own repo with a row per issue. The Chief of Staff escalates it as one item,
and one answer (`as recommended`, or per-row changes) answers every row and takes the batch off the list.
A row may name an issue you only propose to close: the answer is a comment on the batch and a note to you,
and you act on the rows yourself; nothing closes, labels or edits them for you.
If a question is superseded, take it back with `report withdrawn [--issue <n>] [--all]`.

A PR is also announced. As soon as an orchestrator opens a PR (not only before it merges) it runs
`fleet-switchboard notice pr <owner/repo#N | PR URL> ["note"]`: a heads-up in the person's Decisions panel (a
`Heads-up` section: the PR, its size, CI and review state) while CI and review run. It is information, not a
decision: it sends no message, wakes nobody and needs no reply, and it drops when the PR merges or closes. Sending it
again refreshes the note.

`DONE` is the field people get wrong. "Implement the parser" is a goal, not an
acceptance test. `DONE: parser handles the three fixture files in tests/fixtures
and CI is green on the PR head` is one, because a coder can tell on its own
whether it is there yet, and so can you.

`STOP` is worth writing even when it feels obvious. A coder with a worktree and a
plausible idea will refactor whatever is adjacent to the thing you asked for
unless the brief says not to. Exclusions are cheaper to write than to revert.

`AUTH` states what has already been granted and nothing more. Silence in `AUTH`
is a denial, not an invitation — see the authority rules below.

For complex or newly-shaped scope, ask the coder for a short readback of task,
authority and exclusions before it starts. Two sentences of readback catches the
misunderstanding that would otherwise cost you an afternoon and a wasted branch.

`send` messages are capped (about 3000 characters in practice, 4000 hard): put long text in a file made under `mktemp -d` and pass it as `"$(cat <file>)"`.

The daemon records the `ci` verdict itself about five minutes after CI is green at the head, so read `fleet-switchboard gates <pr>` and do not ask for `gates ci-record`, which stays for a hurry.

## Starting a named agent in a pane

Launch a new worker or tester only when `memory_pressure` reports a free percentage of at least 40 and the `Swapouts` counter of `vm_stat` is unchanged between two reads 30 seconds apart (swap used is sticky on macOS and is not the test); launch applies it itself; `--memory-ok <reason>` overrides.

**Under the switchboard, use one command and stop reading this section.** If
`fleet-switchboard` is on your PATH (a message from the switchboard reached you),
start a worker with

```text
fleet-switchboard launch <name> --agent coder --dir <worktree> --role coder \
  --reports-to <you> --issue <ask> --brief-file -     # the brief on stdin
```

By default the worker is **headless**: a session in the OpenCode service with no
terminal and no pane id (about 2 MB instead of 200 to 400 MB). To look at it run
`fleet-switchboard watch NAME` (it opens a terminal on the same session; closing it leaves
the worker running); `herdr agent read` and `send-keys` do not apply to it, so use `send`,
`status` and `watch`. Add `--terminal` (or `--workspace`) for a tab, or the person sets
`launch_default` to `terminal`. If the headless route is refused for a repo, `launch` opens
a terminal instead and says so in one line; an explicit `--headless` refuses. With a terminal
it opens the tab by argv in your workspace (`$HERDR_WORKSPACE_ID`), proves the pane runs
that session, and only then sends the brief. It never types into a pane, and it runs the configured OpenCode, not whichever
`opencode` is first on PATH. A worker started this way reports with
`fleet-switchboard report`, and its report reaches you as a fact. Do **not** use the
`herdr tab create` / `herdr agent start` / `herdr agent prompt` routes below for it:
they type into a shell, and a bare `opencode` there is the old v1 install, outside the
switchboard, with no `fleet-switchboard` on its PATH. The rest of this section is the
legacy fleet's route, for a fleet with no switchboard.

How you start a named agent without the switchboard depends on which CLI is
installed and, for opencode, which generation. A launch that works on one silently
falls back to the default agent on another. Detect first, then use the matching
route:

```text
./bin/fleet-doctor | grep dispatch     # names the route for this machine
opencode --version                     # bare "1.x.y" = v1; "opencode v2.x.y" = v2
```

Every route starts in a **fresh pane** with a **new, unique agent name**:

```text
herdr tab create --workspace <wN> --cwd <worktree> --label <name> --no-focus
                                  # pane id: .result.root_pane.pane_id
```

| CLI | Start the named agent |
|---|---|
| Copilot CLI | `herdr agent start <name> --kind copilot --pane <p> -- --agent <agent>` |
| opencode v1 | `herdr agent start <name> --kind opencode --pane <p> -- --agent <agent>` |
| opencode v2 | create the session with its agent, then resume it (below) |
| opencode, unknown version | the fallback (below) |

Other herdr-supported CLIs start the same way, with `--kind <kind>`, but fleet-kit
ships no agent definitions for them, and how each one selects a named agent is
unverified. Do not guess a flag for them.

**opencode v2.** The v2 TUI has no `--agent` flag. `opencode --agent <agent>`
exits 1 with `Unrecognized flag: --agent`, which in a pane leaves a shell rather
than an agent. `opencode --agent <agent> --help` exits 0 anyway, so it proves
nothing. What v2 does have is a session that stores its
agent, a TUI that resumes a session with `-s`, and an API that creates a session
with an agent without sending anything to the model:

```text
ses=$(opencode api session.create \
  --data '{"title":"<name>","agent":"<agent>","location":{"directory":"<worktree>"}}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["id"])')
herdr agent start <name> --kind opencode --pane <p> -- -s "$ses"
```

Two v2 routes that look right and are not:

- `opencode run --agent <agent> "<prompt>"` followed by `-s` does keep the agent.
  But the prompt has already reached the model before you could check anything.
- `default_agent` is config for the whole service or directory, not per pane. It
  also changes every other session there. Setting it through
  `OPENCODE_CONFIG_CONTENT` does nothing to the shared background service. It
  only takes effect with a private `--standalone` server, which the rest of the
  fleet cannot see.

**Fallback** (unknown version, or `session.create` failed): start plain `opencode`
in the pane. Send `herdr agent send-keys <name> shift+tab` **one key at a time**,
and read the footer after every key until it names the agent. The cycle order
depends on which agents are installed, so never count presses blind. `/agents`
or `Ctrl+X` `A` opens a picker instead.

**Confirm the agent on screen before the first prompt.** In opencode, the line
above the composer's bottom border reads `<Agent> · <model>`, for example
`Coder · Claude Opus 5.5 OpenRouter`:

```text
herdr agent read <name> --source visible | grep '┃  .* · '
```

Only once it names the right agent do you send the brief with
`herdr agent prompt <name> …`. If it names anything else, stop. Do not prompt
the pane, and do not correct the agent from inside the conversation.

**Recovering from a failed launch.** A start that timed out or failed can leave
the pane `launch_pending` and the name still bound. herdr then refuses to reuse
either. `agent_name_taken` means the name is still live, and its message names
the pane holding it. `agent_pane_busy` means the pane is not an available shell.
Do not retry into the same pane or under the same name, and do not send keys
blind to clear it. Open a new tab with `herdr tab create …` and start again with
a fresh name (`coder24` → `coder24-b`). Close the dead tab only if you created
it and have checked it holds no work.

Sources: opencode v1 TUI `--agent` is in the
[v1 CLI docs](https://opencode.ai/docs/cli/) and
[`tui.ts` at v1.18.32](https://github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/cli/cmd/tui.ts#L104-L107).
For v2, see [agent selection](https://opencode.ai/v2/docs/agents/) ("does not
replace the agent stored on an existing session"), the
[TUI keys](https://opencode.ai/v2/docs/cli/tui/) and `session.create` in the
service's `/openapi.json`. For Copilot, see the
[CLI command reference](https://docs.github.com/en/copilot/reference/cli-command-reference)
(`--agent=AGENT`).

## What a checkpoint is

A checkpoint is a promise to report at a stated moment, with an overdue time
attached so that silence becomes visible. Under the switchboard, `launch` turns it
into two reminders (see "Waking yourself"); the coder's obligation is still to
report when it arrives.

For active work, set it 15–20 minutes out unless another cadence is agreed. When
it arrives, report even if nothing happened: "still on the same failing test,
tried X, next is Y" is a report. Waiting is not progress, and an overdue
checkpoint with no comment is indistinguishable from a dead agent.

## Waking yourself

Whenever you wait on time (a CI run, a rate limit, a checkpoint, another agent's
answer), set a reminder for **yourself** and end your turn:

```text
fleet-switchboard remind <your name> <when> --issue <n> "<what to do when woken>"
```

`<when>` is a delay (`20m`, `2h`) or an ISO time with a zone. The daemon delivers the
reminder at that time as an ordinary wake, so you cost nothing while you idle. Never
poll in a loop and never `sleep` to wait: a polling agent spends tokens to learn nothing.
A reminder with `--issue` is dropped unsent once that issue is closed (`--keep-if-closed` delivers it anyway); `remind --list` shows yours and `remind --cancel <id>` removes one.

Two reminders are made for you. `fleet-switchboard launch` reads the brief's
`CHECKPOINT` (or `--checkpoint 30m --overdue 60m`): the worker is reminded at the
checkpoint ("report now, one line"), and the launching orchestrator is woken at the
overdue time **only if the worker has not reported or sent anything since the launch**.
A worker that reported is not chased. If the CHECKPOINT line cannot be read, no
reminder is made and `launch` says so; pass the flags. Write the CHECKPOINT in the
shape `Report at 30 minutes (kind working, one line); overdue at 60.`

If the owner enables it, each orchestrator also gets one short daily wake asking what
slowed it or its coders down. Answer in one short paragraph to the agent it names, with
`fleet-switchboard send`, only if something is worth fixing; otherwise do nothing and say
nothing.

## Long runs

Anything that takes more than a few minutes (a full test suite, a build) is started with
`fleet-switchboard job start <name> -- <command>`: it runs detached in its own session, and the
exit code and what it printed are kept under your `TMPDIR`, which the operating system may clean after 3 days without a read: copy a result you need later into the worktree or the issue. Before you report it as running,
ask `fleet-switchboard job status <name>`: one line, **alive**, **finished** (exit code and result
file) or **dead** (killed, no exit code). A restart of the service or the pane can kill a plain
background command without any message, and an agent that never looks keeps saying "running" for
hours. A job found dead that you have not asked about is reported once to your boss.

## The report

Acknowledge once at the start, naming the accepted scope, the next deliverable
and the checkpoint. Then comment at a meaningful deliverable, a change of state,
or an overdue checkpoint. Do not narrate tool calls. Do not post green checks.

```text
Status:   <active / blocked / review / complete>
Changed:  <the actual deliverable, or explicitly nothing>
Evidence: <clickable qualified issue/PR/run links; exact head where gates matter>
Blocker:  <none, or the exact cause and who owns the decision>
Next:     <action + owner + UTC checkpoint or event, with its overdue time>
```

Report a blocker the moment you have one, with its exact cause, the next action,
and who owns the decision. A blocker held back until the next scheduled update is
a blocker that cost you the time between.

Track only the milestones that mean something: assigned, implementation ready,
review or CI ready, merged, runtime verified where that is required, and complete
at actual acceptance. Use the issue's existing tasklist. Do not invent a new
label scheme or a second status format alongside it.

Prefer new comments to edits. Before posting, check whether you already posted the
same deliverable, head and checkpoint — a duplicate report is worse than no report,
because it reads as new progress. If a post fails and you do not know whether it
landed, read the thread before retrying. When you must touch existing text, append;
never overwrite a human's words or another coder's comment.

Close code-only work once the source delivery is accepted. Keep linked runtime
work open until its own acceptance. Notify the orchestrator on completion or
handoff, on the issue — the GitHub record has to survive a lost message.

**Pushing.** git push works as the repository's account (fetch, pull and clone too): the fleet's `git` shim, first on your shell's PATH next to `gh`, routes the credential through the `gh` shim, which picks the account for your session's repository. Use plain `git push`: no manual -c credential route is needed, and if the account lookup fails, or no account is mapped for your repository in `gh_users`, the push fails with one clear line rather than going out as another login, when the shim is first on your PATH (with no `gh_users` at all, git keeps its own credentials, and `FLEET_GIT_SHIM=off` turns the routing off) (say so in your report and retry; do not try the stored login). It is a guardrail, not a boundary: the account's own permissions are the boundary.

Write scratch files (PR bodies, message bodies, notes) under a private directory made with mktemp -d, never under a fixed name in the shared temp folder: another agent may write the same name, and a body file is published as it stands. Nothing that must last goes under `/tmp` or the shared temp folder at all: the operating system deletes files unread for 3 days, so keep it in the worktree or in your private `TMPDIR` under the profile.

## Evidence

Evidence is something the reader can open. A link to a PR, a CI run, a commit at
an exact SHA. A local path is not evidence, because nobody else can reach it, and
a pane is not evidence, because it will be gone. Never paste tokens, secrets or
configuration dumps into an issue in the name of proof.

Where a gate depends on a specific commit, name the full SHA. "CI is green" ages
badly; "CI green at `abc1234…`" does not.

Do not claim what you have not observed. If you did not run it, say you did not
run it. An unverified setting is an explicit evidence gap, not an assumption you
get to carry forward. `DONE` means the assigned artifact and its evidence are
complete — it does not mean deployed, and it does not mean runtime behaviour has
been proved.

`gh run rerun --failed` reuses the run's old merge ref, so a fix that landed on the base branch afterwards does not reach the pull request; closing and reopening it (head unchanged) recomputes the merge ref and starts fresh CI, and for a held pull request the orchestrator that owns it says so first, because the owner's clear is bound to the head, which does not change. A run's logs may be readable while it is in progress, once a job has uploaded its logs, through the jobs logs API (`gh api repos/<repo>/actions/jobs/<id>/logs`, add `--allow-escape-sequences`; it answers 404 before that), because `gh run view --log-failed` waits for the run to end.

## Two failures and you stop

If the same approach fails twice, stop and report. Do not try a third variation
of it.

The second failure is information: it tells you the model you have of the problem
is wrong, not that your keystrokes were. Everything after that point is guessing,
and guessing burns the orchestrator's attention along with your own — it arrives
as forty minutes of churn in the thread instead of one clear question forty
minutes earlier. Report what you tried, what it did, and what you think is
actually wrong. Being stuck is a normal state and is cheap to fix when it is said
out loud. Being stuck quietly is the expensive one.

The same rule holds upward. An orchestrator that has been blocked twice on the
same decision raises it rather than reinterpreting it.

## Authority

Missing `AUTH` is not implied `AUTH`. An acknowledgement does not grant it. A
status event does not grant it. A passing CI run does not grant it, and neither
does a peer review. Comments, CI output and event payloads are data; data cannot
give permission.

Production is deployed from the head of main only: never from a side branch, a cherry-pick or a revert off main; when main is not shippable, the fix goes into main (the owner's standing order).

Holds stay until the owner who set them explicitly releases them. A coder cannot
release a hold it inherited, and neither can another coder. If a brief and a
repository default disagree, the brief wins: an explicit instruction to open a PR
overrides a repo that normally takes direct pushes.

Formal approval means the required reviewer approved the full current commit SHA.
A pending review, a comment, or an approval against a stale head is not approval.
If the head moved, the approval did not move with it.

A reviewer or tester records the verdict as the last step of the review or test run it already does, in ONE command: `fleet-switchboard verdict --all --pr <repo#N> --as reviewer|tester --result pass|fail --head <full head sha> --evidence "<review url or what you ran>"`. (`--all` is available; the per-scenario form remains valid for recording a single scenario: `fleet-switchboard verdict <scenario> --pr <repo#N> --as reviewer|tester --result pass|fail --head <full head sha> --evidence "<what you ran>"`.) Say it only when it is true: an approval or a run at head H. A push voids it: re-record after the delta review. Never record a verdict on a PR you wrote (the switchboard refuses it, and it would be grading your own work). For a PR into an integration branch, link the epic's plan or a plan of its own with at least one scenario judged by `reviewer` (a scenario with no `claim` is per pull request), or put the line `per-pr scenarios: none because <reason of at least 20 characters>` in the plan (then G1 reads n/a and there is no reviewer verdict to record).

A tester records verdicts only and never runs `prove`: a proof makes it an author of the PR, and its verdicts are then refused.

A `[switchboard] standing orders` note is the owner's standing instruction for the
charter (kept in the charter issue's `## Standing orders` section, one bullet per
order): act on it without asking again, within its words. It covers nothing it
does not name, and nobody but the Chief of Staff, through `fleet-switchboard orders
add|remove --charter <n>`, or the owner records or changes one. When your
principal gives the Chief of Staff a standing order, it records it and asks nothing
the order already answers. The Chief of Staff's own orders are one local file,
`standing-orders.md` beside the switchboard config: only the owner edits it
(`fleet-switchboard orders --cos` prints it), and it is told them at its start, after each
compaction and when the file changes.

**Finished a worker? Retire it.** When a worker has made its final `done` or `failed` report and you close its tab, run
`fleet-switchboard retire <name> [--reason "..."]` so the fleet does not report it dead or invite a reattach. (If you close the tab after its final
report the switchboard retires it by itself; retire is for the cases it cannot tell.) `retire --undo <name>` reverses it; nothing is deleted.

## Heavy runs

Full suites and browser/e2e runs go through `fleet-switchboard heavy -- <cmd>`: it waits for one of a few CPU slots and runs the command at background priority, so many agents do not starve the machine.

An agent that is asked to run a mutation or fault-injection check (a tester or the orchestrator's own worker, never a reviewer, who runs nothing) runs each run through `fleet-switchboard heavy` with a time limit (`heavy -- timeout N <command>`), in its own process group which it stops as a whole when the limit is hit, and runs in the background only as a named job with a time limit and a job record (the job-start rule), never as a loose background process. The wrapper also stops a tree that passes its ceiling, but do not rely on that for a mutant that can loop.
