# Fleet Kit — a proposal for distributing the 3-tier agent fleet

**Status:** proposal. Nothing is built.
**From:** Fleet Kit orchestrator (wE:p1) · **For:** Adam, via the Chief of Staff
**Date:** 2026-09-17

---

## The ask

**Approve building Release 1: the charter convention plus fleet visibility, as a
cloned repo driven by an install skill. Roughly two weeks of work.**

Two things I need from you before anyone starts, because both can invalidate the
plan:

1. **How many people?** At 5, this works as proposed. At 50, the support model
   and the label scheme both need rethinking before line one.
2. **Is the team on github.com or Enterprise Server?** The queue mechanism is
   built on **native sub-issues**. If GHES doesn't have that API, the core
   invention doesn't work and this proposal needs redesigning, not editing.
   (UNVERIFIED — I cannot test GHES from here.)

And one thing to know before you read further: **the most valuable capability —
unattended supervision — is gated on a third-party maintainer.** See Release 2.

---

## What a teammate actually gets

The kit is not "agents." They have agents. It is a way to run several of them at
once without losing track, and it is three ideas:

1. **Work that survives the terminal.** Every orchestrator owns a GitHub issue;
   its queue is that issue's native sub-issues. Close the laptop, lose the pane,
   come back tomorrow — the state is in GitHub, not in scrollback. Today a
   teammate's agent work dies with the window.
2. **One list of what is blocked on them.** `awaiting-user`, applied *before*
   the agent blocks, because blocking is what removes an agent's ability to say
   it is blocked. One query answers "what needs me?" across every project.
3. **Supervision by divergence, not by asking.** Join what GitHub says the work
   is against what herdr says is alive, and you get: charter open but pane dead,
   pane working but no charter, agent blocked with nobody told. Nobody has to
   remember to check.

The honest threshold: **below three concurrent agents, this is ceremony.** A
person running one agent at a time should not install it. That is a feature of
the pitch, not a caveat to bury — it tells you who the first cohort is.

---

## Three findings that shaped everything else

**1. The doctrine layer already ports. Verified, not hoped.**

```
$ copilot skill list
Personal skills:
  fleet-charter - The orchestrator charter convention...
  herdr-board   - The fleet's adapted conventions...
  spire-herdr-coordination - Explicitly requested Herdr coordination...
```

Copilot CLI discovers personal skills from `~/.agents/skills/` — the exact
directory we already use. Our conventions are cross-harness and we hadn't
checked. This is the single best piece of news in the investigation.

**2. `edit: deny` ports to Copilot, and ports harder.**

`--excluded-tools create edit bash task` removes those tools from the model's
context entirely — not denied at call time, *absent*. I ran it. Copilot also has
root-owned policy hooks that a user cannot disable and that fail closed, which we
have no equivalent for.

**3. Our own `edit: deny` is not structural today. I demonstrated it on myself.**

```
$ cd ~/Code/fleet-kit && python3 -c "open('.edit-deny-probe','w').write(...)"
RESULT: file created via python3 while edit:deny is in force
```

I am an orchestrator with `edit: deny`. `chief-of-staff.md` grants
`python3 *: allow` above the `"*": ask` fallback, so arbitrary code execution
sits above the denial. We describe this guarantee as structural. **It is not.**
This is true today, in the live fleet, independent of anything we ship — and we
should not hand a teammate a guarantee we have not repaired.

---

## Method

I ran commands rather than trusting documents. Tags used throughout:
**VERIFIED** (I ran it and read the output) · **READ** (quoting source I read) ·
**DOCUMENTED** (vendor says so, unconfirmed) · **UNVERIFIED** (could not check).
Full evidence index in Appendix B. Two corrections to the brief, both from live
output: `herdr-board` **is** on PATH now (manual symlink, `~/.local/bin/`), and
the opencode integration is at **v10**, not v11.

---

## Q1 — The minimum installable set

**Four components. One list, used consistently by the rest of this document.**

| # | Component | Ours? | Why load-bearing |
|---|---|---|---|
| 1 | **herdr** + **a harness** (Copilot CLI) + **`gh`** | No | Substrate, agent, durable store. All three already exist or install in one line |
| 2 | **`fleet-charter` skill** | **Yes** | The convention: one issue per orchestrator, sub-issues as queue, `awaiting-user` as the only blocking channel, `pane` as the liveness join |
| 3 | **`fleet-coordination` skill** | **Yes** | The brief and report templates (today's `PROTOCOL.md`). ~70% of it is generic and directly reusable |
| 4 | **Three agent definitions + a launcher** | **Yes** | Tier boundaries. The launcher is not optional — see Q2, enforcement travels with the launch flags on Copilot, not with the file |

Plus `fleet-doctor` (the verifier) and `fleet-census` (the divergence report),
both covered below.

### On whether herdr is really required

Fair challenge, and the answer is conditional. **The convention alone needs only
`gh`** — charters and sub-issues are pure GitHub, and a teammate could run that
in tmux today. herdr becomes load-bearing the moment you want the other two
things: `pane` is the liveness join key that makes divergence detection possible,
and worktree-per-worker is how tier 3 stays isolated.

So: if someone wants *only* the issue convention, tell them to read the skill and
skip the install. The kit is for people who want the supervision.

### Ours by accident — cut

- **herdr-board.** Not ours, and the reason is correctness, not the build. See Q8.
- **fleet-snapshot / fleet-restore.** 1,802 lines, no tests, and restore has
  **never been proven against a real restart**.
- **`gemini-critic`.** Copilot ships `rubber-duck` — a built-in read-only critic.
- **Spire architectural touchstones.** `orchestrator.md` §5 is 18 lines of
  Azure/ACA/Venue specifics the orchestrator injects into *every brief it writes*.
  Meaningless elsewhere; clean single-block excision.

---

## Q2 — Harness-agnosticism: real at one layer, not at another

**Layer 1 — Skills: real, zero adapter.** (VERIFIED, above.) Our doctrine already
runs on their harness. This is where the intellectual content lives, so this is
the layer that matters most.

**Layer 2 — Agent definitions: mechanically translatable.**

| opencode | Copilot CLI |
|---|---|
| `model: github-copilot/claude-opus-5` | `model: claude-opus-5` (in their catalog, VERIFIED) |
| `variant: high` | `reasoningEffort: high` |
| `permission: {edit: deny}` | `tools: [...]` allow-list (DOCUMENTED — see Appendix A) |
| `permission: {bash: {herdr *: allow}}` | `--allow-tool='shell(herdr:*)'` — **moves into launch flags** |
| `permission: {bash: {"*": ask}}` | **No equivalent.** Genuine expressiveness loss |

Gained on Copilot, worth naming: `modelPolicy: "required"` refuses dispatch
rather than silently substituting a model — we have a whole policy section asking
for that behaviour and they have a field for it. And the root-owned
`/etc/github-copilot/policy.d/` hooks, which are enforcement *against* the user
rather than configuration *by* the user.

**Layer 3 — State reporting: this is the real gap, and it was not on the list.**

| | opencode plugin | Copilot hook |
|---|---|---|
| Reports session identity | Yes | Yes |
| Reports `working` / `blocked` / `idle` | **Yes** | **No** |
| Events handled | 14 | 1 (`SessionStart`) |

The Copilot hook calls `pane.report_agent_session` once and exits. It **never
calls `pane.report_agent`**; there is no `state` key in the file (READ,
`herdr-agent-state.sh:69-79`). It is not a reduced subset — it is identity only.

Consequence: opencode panes carry `screen_detection_skipped: true` because herdr
trusted pushed state. A Copilot pane gets none, so herdr falls back to
**screen-scraping the terminal** against three string rules last dated Sep 4,
matching TUI chrome like `"esc to cancel"`. Every supervision feature —
heartbeat send-gating, census liveness — sits on that.

**This is the thing to fix, and it is not ours to fix.** Copilot's hook system
fires all the needed events (`agentStop`, `permissionRequest`, `preToolUse`,
`notification`), and `notification` is even non-blocking, which is better than
opencode's synchronous plugin. But the integration payload is **embedded in the
herdr binary**. So either the herdr maintainer ships it, or we ship a sidecar
hook beside it and own that forever. **No commitment exists from the maintainer.
Nobody has asked.** That is Release 2's critical path and it runs through someone
outside this team.

### Say this, not "agnostic"

> A portable doctrine layer (verified), a mechanically translatable agent
> definition, and a per-harness launcher plus state adapter. On the guarantee
> that matters most — structural inability to edit — it holds on both harnesses.

### The `edit: deny` caveat nobody documented

`--excluded-tools` strips *local* write tools but **MCP write tools survive**. In
my restricted run, `captain-write_to_google_docs_document`,
`captain-create_jira_issue` and `captain-update_jira_issue` were all still
present. **Tool-tier denial is not write denial when MCP is attached** — and this
applies to opencode too. Any tier-1 definition we ship must constrain MCP servers
explicitly.

---

## Q3 — What breaks on someone else's laptop

Scoped to **what we propose to ship**. The heartbeat/snapshot forensics are in
Appendix C so they don't crowd out the operative list.

### For Release 1, five things break

| # | What | Why it bites |
|---|---|---|
| 1 | **Label collisions in a shared repo** | Labels are repo-global. Two teammates configuring the same label name see each other's work. `ak:` is **convention only** — nothing derives or enforces it. Must be a *required install input*, not a documented habit |
| 2 | **The three-label exception** | `orchestrator`, `awaiting-user`, `fleet` are deliberately **un**namespaced *and* queried by literal name in census. So the rule is "namespace everything except these three," and a teammate who namespaces consistently **silently breaks the tooling**. This must ship as code, not a footnote |
| 3 | **`gh` write access** | Creating issues, editing bodies, applying labels all need write/push. Triage is insufficient. Creating labels the first time needs admin |
| 4 | **Native sub-issues** | The queue mechanism depends on it entirely. See the gating question up top |
| 5 | **TCC / MDM** | Real and confirmed. Full Disk Access may be **locked by policy** and not grantable by the user at all. Must be a human gate the agent never retries |

### Two failure *shapes* that must inform the design

**Silent wrong answers.** `fleet-census` hardcodes `REPO = "janus-infra/spire-venue"`
(READ, line 51) with no override. A teammate elsewhere gets `gh` failures folded
into `degraded[]` and **an empty census that reads as "no divergence"** — a tool
reporting health for a fleet it cannot see. Anything we ship must fail loudly on
misconfiguration rather than degrade to a green light.

**Identity stored where nothing protects it.** The heartbeat's opt-in is the 🔔
glyph *in the tab title*; discovery is `\bdirector\b` against the tab label, in
three independently-maintained copies of the rule. `⚠️` is two codepoints — a
stripped U+FE0F or an NFD normalisation **silently un-supervises an orchestrator
with no error anywhere**. Per `fleet-snapshot`'s own comment: *"an orchestrator
running unwatched, which is exactly how one died."*

### The propagation problem, which is the most important one

Agent definitions load once per session and never reload — your observation,
confirmed. The deeper instance: **the package is already internally inconsistent
after 24 hours.**

- `chief-of-staff.md:46` and charter **#459** still say `awaiting-user`'s question
  goes in *"the last comment"*. The `fleet-charter` skill **superseded** that with
  a `## Decision required` section in the body, after a real incident.
- `orchestrator.md:76` documents `--next`; everything else says `--wake-again-in`.
- Our `herdr-board` skill says the binary is not on PATH. It has been since 09:35.

`chief-of-staff.md:148-168` is a well-written warning titled *"Your standing
instructions are a snapshot, not a source of truth."* The package containing that
warning is a live instance of the failure it describes.

**One architectural rule falls directly out of this, and it should govern the
kit's layout:**

> Skills are loaded lazily and re-read per invocation. Agent definitions freeze at
> session start. **Therefore doctrine that changes belongs in skills; only tier
> boundaries and permissions belong in agent definitions.**

That is also why Release 1 must ship a staleness check, not just a `git pull`
instruction. See Q7.

---

## Q4 — Agent-driven install

**Three artifacts, because installation and verification must be separate lanes.**

**1. `INSTALL.md`** — a skill, so Copilot discovers it natively. Every step has
four fields:

```
STEP 4 — Authenticate gh
  DO:      gh auth login --scopes repo
  HUMAN:   yes — browser + device code. Stop and hand over.
  VERIFY:  gh auth status --active && gh api user --jq .login
  PROVES:  a login name is returned, and repo scope is present
```

`PROVES` is what makes this not a README: it states the observable fact that
constitutes success, so an agent cannot mark a step done by reading its own
optimism.

**2. `fleet-doctor`** — one line per check, non-zero exit on any failure. It
re-runs any time, so it doubles as the month-three health check. Critically it
includes **negative tests**: it runs the restricted tier-1 agent and greps the
tool list to confirm `create`/`edit`/`bash` are *absent*. Most installers only
test for presence, which is why broken permission models survive so long.

**3. `bootstrap.md`** — the pasted prompt. Short: read the spec, work the steps,
paste real `VERIFY` output rather than summarising it, stop at anything marked
`HUMAN`, and **stop after two failures on the same obstacle** rather than trying a
third variant. That last rule is our own escalation doctrine and it transfers
unchanged — it is what stops an agent "fixing" a TCC denial by disabling a
security control.

**On verification integrity, honestly:** the installing agent cannot be tier-1 —
it needs bash, write and network — so it *can* edit `fleet-doctor`. The
installer/verifier separation is therefore **convention, not structure**, which is
exactly the weakness I criticise elsewhere. To make it structural, `fleet-doctor`
must be checksummed against the remote and the checksum verified by something the
agent didn't run. That is a small amount of work and it should be in scope.

**Human gates, declared up front so an agent never retries them:** `gh auth
login` · **TCC prompts** (stop, name the dialog and the button) · **Full Disk
Access on MDM** (stop, escalate to IT, **never** suggest a workaround) ·
**Copilot plan tier** (concurrency is plan-gated 2→32 and changes viable topology).

---

## Q5 — Zero infrastructure: true

Nothing is deployed. herdr is a local process on a Unix socket (VERIFIED,
`srw-------`); the harness is a local CLI; the heartbeat is a LaunchAgent; the
board is a local binary with local SQLite; the durable store is GitHub Issues,
which already exists. No servers, no containers, no cloud resources, no network
listeners.

**The caveat that matters:** the shared repo *is* shared infrastructure wearing a
disguise. Labels are a repo-global mutable namespace with no isolation beyond a
naming convention. At one person that is a style preference. At N people it is
the single most likely cause of a confusing failure — which is why Q3 item 1 is
an install-time input rather than documentation.

---

## Q6 — Remote via `herdr --remote`

**The 3-tier model does not currently span machines, and the reason is
structural.** From `herdr --skill` (READ):

> IDs and live agent names are scoped to one server. **Two saved SSH machines can
> both have `w1:p1`** or an agent named `reviewer`.

But the charter's `pane:` field is `wN:pN` with **no machine qualifier**; the
heartbeat holds **one** socket path; census shells out to bare `herdr` against
one inherited context. So `pane: wD:p1` is ambiguous the moment a second server
exists — census would join a charter to the wrong pane and report confident
nonsense.

| Topology | Works? |
|---|---|
| Everything on the laptop | **Yes** — the verified configuration |
| Everything on a remote box, laptop attaches as client | **Yes** — and this is the good story |
| Orchestrator local, workers remote | **No** — pane IDs collide |
| One Chief of Staff across several machines | **No** — needs machine-qualified identity end-to-end |

For the topology that works: **the server keeps** panes, agents, workspaces,
worktrees, running work and session state — close the lid and it all keeps going.
**The user carries** the TUI client and keybindings. **Per-machine, not synced:**
herdr session state and integration hooks, so each server needs its own install —
which doubles the install surface for anyone using a remote box, and the install
spec must say so.

Recommend shipping "whole fleet on one host, attach from anywhere," which needs
no new work. Explicitly do not promise a cross-machine fleet; that requires
machine-qualified identity across the charter field, the heartbeat and census.

**Confidence:** this section is READ plus reasoning. There are **no saved SSH
machines on this box** (VERIFIED) so I exercised none of it. Before promising
remote to anyone, someone should actually do it once.

---

## Q7 — The distribution artifact

**A git repo they clone, whose primary interface is an install skill.**

```
fleet-kit/
  README.md                  bootstrap.md        INSTALL.md
  bin/fleet-doctor           bin/fleet-launch    bin/fleet-census
  skills/fleet-charter/      skills/fleet-coordination/
  agents/copilot/  agents/opencode/
  fleet.toml.example         config/labels.json
```

Two design decisions that come straight out of the findings rather than taste:

**`bin/fleet-launch` is mandatory, not a convenience.** Q2 proved that on Copilot,
enforcement lives in the launch flags, not the agent file. Run `copilot --agent
chief-of-staff` bare and you get a Chief of Staff that can edit. So the kit must
ship the launcher that guarantees `--excluded-tools` is passed, or it ships a
guarantee it has already proven doesn't travel.

**Per-person config goes in `fleet.toml`, never in local commits.** The obvious
approach — "fork it and edit the constants" — guarantees a merge conflict on every
doctrine update, which is precisely the friction that stops people pulling. Given
that Class G is the central risk, that would be choosing the artifact worst suited
to it. Instead: one gitignored config file for the label prefix, repo and paths,
and **`fleet-doctor` checks a version stamp against the remote and warns loudly
when doctrine is stale.** Propagation has to be a mechanism, not a hope.

**Why not the alternatives.** A **brew tap**: we own neither herdr nor the board,
so a tap would carry only our Python while the real dependencies install by other
means — two update mechanisms, no source of truth. A **single script**: works
once on one laptop, can't express human gates, can't re-run as a health check. A
**pasted prompt alone**: worst provenance; the convention has too many sharp edges
(`sub_issue_id` is the DB id not the issue number; `labels=` is an AND;
label-before-block ordering) for an agent to reconstruct from prose.

The entry point is still one line — *clone this, read its INSTALL skill, work the
steps* — but the substance is inspectable before it is trusted, which matters when
you are asking someone to hand an agent broad local authority.

---

## Q8 — What should not be shipped

**Half-built:** `fleet-restore` (never proven against a real restart — a restore
tool that has never restored is a liability dressed as a safety net) ·
`fleet-snapshot` (only meaningful paired with restore, and it writes snapshots
containing **hostname and username**) · census's GHOST class (by its own
admission *"unverified against a real ghost"* — an untested health class is worse
than none, because it produces a green light nobody earned).

**Specific to us:** the Spire touchstones · the `ak:*` labels themselves (the
*rationale* is excellent portable doctrine and should ship; the labels must not) ·
`RESTART.md`, which **self-declares as stale-prone** · the committed snapshot
JSONs · every `#4xx`/`#5xx` issue reference · dated assertions presented as
policy (*"Standing default provider: github-copilot (user-selected 2026-09-16)"*).

**Liabilities in someone else's hands:**

- **herdr-board.** Not for the build reason, which is cheap to fix — one CI job
  or a vendor tag. **For correctness:** its settle guard was verified end-to-end
  for claude, codex and opencode, and **Copilot is not in that list**. The vendor
  is explicit that a runtime herdr never reports `working` for *"would sit
  `working` forever."* Given Layer 3 above, that is the likely outcome on their
  harness — rows stuck with no diagnosis path into a repo we don't own. Point
  teammates at the vendor if they want it; keep it off the install path.
- **`python3 *: allow` in a tier-1 definition.** Shipping an agent that advertises
  `edit: deny` while granting arbitrary code execution hands someone a guarantee
  that is false. Fix before shipping or drop the claim.
- **The heartbeat unfixed.** Its failure mode is *silent cessation of
  supervision*. Here we caught it. On a teammate's laptop nobody is watching the
  watcher.

---

## Plan

Two releases, not three. The original three-phase split put the convention alone
in phase 1, and on reflection that ships below the capability threshold — a
teammate would pay the install cost and get ceremony without the payoff, bounce
off, and never see phase 2. Visibility is the first point where installing is
worth it, so it goes in Release 1.

**Release 1 — the convention and seeing the fleet. ~2 weeks.**

| Work | Est. |
|---|---|
| Extract + de-Spire the two skills; template repo/labels/reviewer | 2d |
| Three agent definitions in both formats, + `bin/fleet-launch` | 2d |
| `fleet-census`: `REPO`/labels/charter-keys → config; one shared discovery module instead of three copies; tests | 3d |
| `INSTALL.md`, `bootstrap.md`, `fleet-doctor` incl. negative tests + checksum + staleness stamp | 3d |
| Pilot with one teammate end to end, fix what that finds | 2d |

Exit: a teammate installs unaided, runs three concurrent orchestrators, and gets
a true divergence report. `fleet-doctor` green.

**Release 2 — unattended operation. Unschedulable today.**

The heartbeat (templated, with the `cli_error` escalation fixed so a dead herdr
path raises a service-level alarm instead of retrying silently forever) plus the
Copilot state adapter. **Critical path runs through the herdr maintainer**, per
Layer 3. First action is not code, it is asking Brede whether he will take the
adapter. Until there is an answer, Release 2 has no date and should not be
promised.

**Not scheduled:** herdr-board, snapshot/restore, cross-machine fleets.

---

## Open questions I could not resolve

These need someone's decision or a number nobody has measured.

1. **Premium request consumption.** A 3-tier fleet with timer-driven wakes on
   `claude-opus-5` at `reasoningEffort: high` is by construction a
   token-consumption machine, multiplied by N teammates, against Copilot's
   quotas. **Nobody has measured this**, including us. It is a plausible hard
   "no" from a budget owner and it should be measured on our own fleet before we
   ask anyone to adopt it.
2. **Security review.** We would be handing N corporate laptops a kit that
   encourages broad local agent authority, requires `gh` write access, and — per
   Q2 — leaves MCP write tools reachable under tool-tier denial. The `/etc`
   policy hook needs separate approval. I think the whole thing needs InfoSec
   eyes before distribution, not after, and on an MDM fleet that is a more likely
   blocker than anything technical here.
3. **Support model.** When a teammate's fleet breaks in month three, who debugs
   it? census has no tests today, the heartbeat fails silently, herdr is
   third-party and pre-1.0. `fleet-doctor` is a detector, not a support plan.
4. **herdr version pinning.** We correctly flag the board's bare-SHA pin as an
   upgrade-story failure. But herdr itself is pre-1.0, installed via Homebrew,
   and has **already caused one `protocol_mismatch` outage here**. A teammate
   running `brew upgrade` at an arbitrary moment could break every fleet on the
   team at once. Same bug class, larger blast radius, no strategy.
5. **Offboarding.** Someone leaves: their charters stay open in a shared repo,
   their label prefix persists forever, their `awaiting-user` issues block with
   nobody to answer. Guaranteed recurring mess at team scale.

---

## Decisions

**Adam:**

1. **Team size and github.com vs GHES** — the two gating questions at the top.
2. **The `python3 *: allow` hole.** Tighten it (losing `python3` convenience in
   the orchestrator tiers), or keep it and stop calling the guarantee structural?
   True today regardless of what we ship.
3. **Label namespacing at team scale.** Per-person prefixes, a fleet repo per
   person, or one team prefix with per-person routing labels? I lean per-person
   prefix as a required install input, but it is a call about how the repos are
   governed.
4. **Is the root-owned `/etc` policy hook acceptable** on corporate laptops? It
   is the strongest tier-1 enforcement available and I would recommend it, but it
   is a machine-wide control and may be MDM-contested.
5. **Measure cost before promising anything** (open question 1). I would not send
   this to a wider team without a number.

**Chief of Staff:**

6. **Fix the internal inconsistencies before packaging** — `chief-of-staff.md:46`
   and #459 versus the `fleet-charter` skill; `--next` versus `--wake-again-in`.
   Packaging known-stale doctrine makes the drift permanent in N copies. I am
   under a no-mutation STOP and cannot touch any of it.
7. **`heartbeat.py` is missing `import sys`** (uses `sys.stderr` at lines 885,
   888; imports are lines 3-17). Live latent bug in the running fleet, not a
   packaging concern.
8. **Someone should ask Brede about the Copilot state adapter.** It is Release
   2's critical path and no one has raised it.

---

## Appendix A — The one test that still needs running

Everything load-bearing here is verified except whether the tier-1 guarantee
travels *with the agent file* or only *with the launch flags*. It needs a write to
`~/.copilot/agents/`, outside my authority. Note this asserts on **the tool list**,
not on `ls` — a filesystem check would pass if the model merely declined, which
would be a false positive on exactly the claim we care about.

```bash
mkdir -p ~/.copilot/agents
cat > ~/.copilot/agents/cos-test.agent.md <<'EOF'
---
description: read-only test
tools: ["view", "grep", "glob"]
model: claude-opus-5
---
You are a read-only orchestrator.
EOF
cd "$(mktemp -d)"
copilot --agent cos-test --allow-all-tools -s \
  -p "Do not use any tools. Reply with ONLY a comma-separated list of every tool available to you."
```

**Pass:** `create`, `edit`, `bash`, `task` absent **and** no MCP write tools
(`*-create_*`, `*-update_*`, `*-write_*`) present. That second clause matters
because of the MCP finding in Q2 — if MCP writes survive a frontmatter allow-list
the same way they survive `--excluded-tools`, the frontmatter guarantee is no
stronger than the flag guarantee and `bin/fleet-launch` must also disable MCP.

**Decides:** whether the kit can ship the guarantee in the artifact, or whether
every launcher must pass flags and that becomes load-bearing in the install spec.

---

## Appendix B — Evidence index

| Claim | Status | Source |
|---|---|---|
| Copilot CLI reads `~/.agents/skills/` | **VERIFIED** | `copilot skill list` — all 3 fleet skills listed |
| `--excluded-tools` removes create/edit/bash/task from context | **VERIFIED** | Two `copilot -p` runs, baseline vs restricted |
| MCP write tools survive `--excluded-tools` | **VERIFIED** | `captain-write_to_google_docs_document` etc. present in restricted run |
| **`python3` writes files under `edit: deny`** | **VERIFIED** | Ran it on myself in `~/Code/fleet-kit`; file created, then removed |
| `claude-opus-5` available on Copilot | **VERIFIED** | `copilot help config`, 28-model catalog |
| Copilot CLI version 1.0.85 | **VERIFIED** | `copilot --version` |
| herdr-board has no releases | **VERIFIED** | `gh release list` empty |
| herdr-board now on PATH via manual symlink | **VERIFIED** | `~/.local/bin/herdr-board`, created 09:35 |
| opencode integration v10, outdated vs v11 | **VERIFIED** | `herdr integration status` |
| No saved SSH machines; remote unexercised | **VERIFIED** | `herdr machine list` |
| herdr socket is local Unix, no listener | **VERIFIED** | `srw-------` |
| Heartbeat plist pins `/usr/local/bin/python3` | **VERIFIED** | `plist:23`; symlink → python.org 3.13 |
| Copilot herdr hook reports no state | **READ** | `~/.copilot/hooks/herdr-agent-state.sh:69-79` |
| Heartbeat pins absolute herdr path; `cli_error` treated as transient | **READ** | `config.json`; `heartbeat.py:363`, `:588-592` |
| `fleet-census` hardcodes `REPO` | **READ** | `fleet-census:51` |
| `heartbeat.py` missing `import sys` | **READ** | lines 885, 888 vs imports 3-17 |
| Pane IDs scoped to one server | **READ** | `herdr --skill` |
| `labels=` is an AND, not an OR | **READ** | vendor test `adopt.rs:1281` |
| Board settle guard verified for claude/codex/opencode only | **READ** | `integration.rs:203-230` |
| Charter convention in practice | **READ** | Issues #459, #456, #468, #471, #472, #474, #496 |
| Copilot frontmatter `tools:` enforcement | **UNVERIFIED** | Appendix A |
| GHES native sub-issues support | **UNVERIFIED** | Not testable here — gating question |
| Remote attach end-to-end | **UNVERIFIED** | No saved machines |
| Premium request cost of a running fleet | **UNVERIFIED** | Never measured, by anyone |

---

## Appendix C — Defects in components not proposed for Release 1

Recorded so the work isn't lost. None of this blocks Release 1; most of it blocks
Release 2.

**Heartbeat (blocks Release 2):**
- Config pins `"herdr": "/opt/homebrew/bin/herdr"`. `load_config` validates only
  that the path *is absolute* — never that it exists or runs. A non-zero exit
  becomes `cli_error`, treated as **transient and retried forever**. Supervision
  ends while every surface looks alive. This is #483 and it is the defining bug
  of the component.
- The fix is already in the environment, unread: herdr exports `HERDR_BIN_PATH`.
- Plist pins `/usr/local/bin/python3` (python.org 3.13, absent on a stock Mac),
  has five `/Users/adkaplan` paths, exists in **two copies**, sets `PATH` without
  `/opt/homebrew/bin`, and routes stderr to `/dev/null` with a 10s throttle — so
  a startup failure retries forever, invisibly.
- `~/.local/bin/heartbeat-ack` is baked literally into every wake prompt, and is
  a **byte copy** rather than a symlink.
- `interval_seconds` in the config is **never read**; four more constants are dead.
  An operator edits it, sees no effect, gets no error.
- `⚠️` already carries 3 false alarms and 0 true positives, so the fleet is trained
  to ignore the one glyph that would report a total outage.

**Census (fixed as part of Release 1):** hardcoded `REPO`; zero tests across 1,166
lines; the director/bell discovery rule duplicated in three files that will drift.

**Snapshot/restore (not shipping):** 1,802 lines, no tests, restore unproven;
snapshots leak hostname and username; state directory mixes config, live state,
logs, backups and seven directories of prototype implementations, and
`heartbeat.py:786` derives the state root from the config's parent so they cannot
be separated without a code change.
