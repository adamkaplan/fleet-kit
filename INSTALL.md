# Installing Fleet Kit

**You are an agent. This document is for you.** A person has pointed you at it
and expects you to work through it and report back.

## How to work this document

1. **Work the steps in order.** Each has a `CHECK`. If the check is already
   satisfied, say so and move on — do not redo it. The person running this is an
   engineer with a working machine, not a fresh laptop. Expect to skip several
   steps entirely.
2. **Run every `VERIFY` and paste its real output.** Not a summary, not a
   paraphrase — the actual text. `PROVES` tells you what in that output counts as
   success. If you did not observe it, the step did not pass.
3. **Never report success you did not see.** If a command fails, say what failed
   and stop. A half-finished install that is reported as done is worse than one
   that is reported as broken.
4. **Stop at `HUMAN`.** Say exactly what the person must do and wait. Do not try
   to work around it.
5. **Two failures on the same obstacle and you stop.** Report both attempts and
   what you think is wrong. Do not try a third variation. Changing a flag is not
   a new idea.
6. **Do not improvise scope.** If something here is wrong or missing, say so.
   Do not fix it by inventing a step.

This is safe to re-run. If it dies halfway, start again from the top — the checks
will carry you back to where you stopped.

---

## STEP 1 — Confirm the kit and derive the prefix

```
CHECK:   pwd; ls README.md bin/fleet-doctor
         Not in the fleet-kit repo?  -> ask the person where they cloned it. Stop.
DO:      FLEET_PREFIX="$(whoami)"
VERIFY:  echo "prefix: $(whoami)"
PROVES:  a short username, no spaces. This is the label prefix for everything
         that follows.
```

The prefix is derived, never chosen. Do not ask the person what they would like
it to be, and do not use their initials or their GitHub handle. On a corporate
Mac the username is already unique company-wide and already length-capped, which
is exactly what a label prefix has to be.

---

## STEP 2 — herdr

```
CHECK:   command -v herdr && herdr --version
         Present?  -> skip to STEP 3.
DO:      brew install herdr
VERIFY:  herdr --version
PROVES:  a version prints. 0.9.0 or later.
```

herdr is in homebrew-core, so no tap is needed. If `brew` itself is missing, that
is a `HUMAN` step — stop and say so rather than installing Homebrew unprompted.

---

## STEP 3 — An agent CLI

```
CHECK:   command -v copilot && copilot --version
         Present?  -> skip to STEP 4.
HUMAN:   yes, if missing. Do not guess an install method.
DO:      stop and tell the person to install GitHub Copilot CLI
VERIFY:  copilot --version
PROVES:  a version prints.
```

If the person uses a different harness — opencode, Claude Code — that is fine and
the rest of this still applies. Note which one, because STEP 8 and STEP 9 depend
on it.

---

## STEP 4 — GitHub authentication

```
CHECK:   gh auth status --active
         logged in with 'repo' scope?  -> skip to STEP 5.
         logged in without it?         -> gh auth refresh --scopes repo
         not logged in?                -> DO below
DO:      gh auth login --scopes repo
HUMAN:   yes — browser and device code. Stop and hand over.
VERIFY:  gh auth status --active && gh api user --jq .login
PROVES:  a login name comes back, and the token carries 'repo'.
```

Most engineers are already authenticated, so expect to skip this. Being logged in
is not the same as holding the `repo` scope — that middle branch is the one worth
reading carefully. `gh auth refresh` adds the scope to the existing session
rather than dragging someone through a login they did not need.

---

## STEP 5 — Choose the repository

```
CHECK:   is FLEET_REPO already set, or ~/.config/fleet-kit/config present?
         Yes?  -> use it, skip to STEP 6.
HUMAN:   yes. Only the person can decide this.
DO:      ask: "Which GitHub repo should hold your charters and queue?
              Format OWNER/REPO. It can be a product repo you already work in,
              or a repo of your own — charters are about your agents, not about
              the product."
VERIFY:  gh api repos/<their answer> --jq '.full_name, .permissions.push'
PROVES:  the full name comes back and push is true.
```

If push is false, stop. Write access is the floor — charters, sub-issues and
labels all require it. Triage is not enough.

---

## STEP 6 — Write the config

```
CHECK:   cat ~/.config/fleet-kit/config 2>/dev/null
         Already has FLEET_REPO and FLEET_PREFIX?  -> skip to STEP 7.
DO:      mkdir -p ~/.config/fleet-kit
         printf 'FLEET_REPO=%s\nFLEET_PREFIX=%s\n' "<owner/repo>" "$(whoami)" \
           > ~/.config/fleet-kit/config
VERIFY:  cat ~/.config/fleet-kit/config
PROVES:  two lines, the repo from STEP 5 and the prefix from STEP 1.
```

---

## STEP 7 — Create the labels

```
CHECK:   gh label list --repo "$FLEET_REPO" --limit 200 \
           | grep -E "^$(whoami):(orchestrator|awaiting-user)"
         Both present?  -> skip to STEP 8.
DO:      gh label create "$(whoami):orchestrator" --repo "$FLEET_REPO" \
           --color B60205 --description "Charter issue. One per orchestrator."
         gh label create "$(whoami):awaiting-user" --repo "$FLEET_REPO" \
           --color FBCA04 --description "Blocked on a decision only the owner can make."
VERIFY:  gh label list --repo "$FLEET_REPO" --limit 200 | grep "^$(whoami):"
PROVES:  both labels listed, both carrying the prefix.
```

Create only these two. Project labels come later, as work arrives.

**Every label carries the prefix, including these.** Labels are repo-wide. If two
people share a repo and both create an unprefixed structural label, each one's
tooling reads the other's charters — and because agent pane addresses are unique
only within a single machine, the tooling will match a colleague's charter to a
local pane and report something confident and wrong.

If a label already exists **without** the prefix, leave it alone. It belongs to
someone else or predates this. Do not rename it, do not adopt it, do not delete
it. Create the prefixed one alongside.

---

## STEP 8 — Install the skills

```
CHECK:   ls ~/.agents/skills/fleet-charter ~/.agents/skills/fleet-coordination
         Both present?  -> skip to STEP 9.
DO:      mkdir -p ~/.agents/skills
         ln -s "$PWD/skills/fleet-charter"     ~/.agents/skills/fleet-charter
         ln -s "$PWD/skills/fleet-coordination" ~/.agents/skills/fleet-coordination
VERIFY:  copilot skill list
PROVES:  both named under "Personal skills", with their descriptions.
```

**Symlink, do not copy.** `~/.agents/skills/` is read natively by Copilot CLI,
opencode and Claude Code, so the same files serve every harness. Linking means a
later `git pull` in this repo updates the doctrine everywhere at once. Copying
means you have forked it and will drift.

If the person uses a harness that does not read `~/.agents/skills/`, say so and
stop rather than guessing where its skills live.

---

## STEP 9 — Install the agent definitions

```
CHECK:   Copilot CLI:  ls ~/.copilot/agents/
         opencode:     ls ~/.config/opencode/agents/
         The three definitions present?  -> skip to STEP 10.
DO:      Copilot CLI:  mkdir -p ~/.copilot/agents && cp agents/copilot/*.md ~/.copilot/agents/
         opencode:     mkdir -p ~/.config/opencode/agents && cp agents/opencode/*.md ~/.config/opencode/agents/
VERIFY:  Copilot CLI:  copilot --agent chief-of-staff -p "Reply with only your role name." -s
         opencode:     opencode --agent chief-of-staff --help
PROVES:  the agent resolves by name. An unknown-agent error means it did not install.
```

Copy rather than symlink here, and this is the one place the asymmetry is
deliberate. Agent definitions are read once when a session starts and never
reloaded, so linking them buys nothing — a running agent will not see a change
either way. Definitions are also the one artifact a person is likely to tune to
their own taste, and a copy is theirs to edit without fighting `git pull`.

That split is worth understanding rather than just following: **doctrine that
changes belongs in the skills, which are re-read on every use. Only role
boundaries belong in the agent definitions.** If you find yourself wanting to
edit a definition to change how the fleet behaves, the change probably belongs in
a skill.

---

## STEP 10 — Let herdr read agent state

```
CHECK:   herdr integration status
         Your harness shown as "current"?  -> skip to STEP 11.
DO:      herdr integration install copilot      # or: opencode, claude, codex
VERIFY:  herdr integration status
PROVES:  your harness reports "current", not "not installed" or "outdated".
```

This is how herdr knows whether an agent is working, blocked or idle — which is
what makes supervision possible at all.

Be aware of what you are installing. For opencode this reports real state. **For
Copilot CLI it currently reports session identity only**, so herdr falls back to
reading the terminal to infer state. That works, less reliably. It is a known
rough edge, it is upstream in herdr rather than in this kit, and it is not
something to try to fix here.

If `herdr integration status` shows an *outdated* integration for a harness the
person actually uses, re-running `install` updates it. Leave the other runtimes
alone.

---

## STEP 11 — The heartbeat (optional, but it is the point)

Without this, orchestrators stop when they finish a thought and wait for you.
With it, they wake themselves on their own schedule and you stop being the thing
that keeps the fleet moving.

```
CHECK:   launchctl list | grep com.fleet-kit.heartbeat
         Already loaded?  -> skip to STEP 12.
DO:      ./bin/fleet-heartbeat init
         mkdir -p ~/.local/bin && ln -sf "$PWD/bin/heartbeat-ack" ~/.local/bin/heartbeat-ack
         ./bin/fleet-heartbeat plist > ~/Library/LaunchAgents/com.fleet-kit.heartbeat.plist
         launchctl load ~/Library/LaunchAgents/com.fleet-kit.heartbeat.plist
VERIFY:  ./bin/fleet-heartbeat status
PROVES:  a service line that is not FAULT, and the state directory printed.
```

`init` refuses to write a config pointing at a herdr it cannot actually run, so a
successful `init` is itself evidence. If it refuses, read what it says — it has
found a real problem and starting anyway would give you a service that looks
healthy and supervises nothing.

Make sure `~/.local/bin` is on PATH, or the agents will be told to run an ack
command they cannot reach.

```
HUMAN:   yes, one step. An orchestrator opts in by having 🔔 in its tab label.
         Nothing is supervised until you add it.
```

That is the whole opt-in mechanism: one glyph, one rule. `./bin/fleet-heartbeat
list` shows every tab holding an agent and whether it opts in, so if something is
not being woken, that command answers why.

---

## STEP 12 — Charter template (optional, 5 seconds)

```
CHECK:   ls templates/charter.md
DO:      nothing to install. Tell the person it is there.
PROVES:  they know where to start their first charter rather than
         writing one from a blank issue.
```

---

## STEP 13 — Prove it

```
CHECK:   none — always run this.
DO:      ./bin/fleet-doctor
VERIFY:  ./bin/fleet-doctor; echo "exit=$?"
PROVES:  exit 0, and no line beginning FAIL.
```

`WARN` lines do not fail the run and are usually fine to leave. Read them to the
person anyway — a warning about an outdated integration or an empty charter list
is worth them knowing about.

If anything FAILs, paste the whole output. Most failures print the exact remedy
command in the message. **Do not run a remedy that creates or changes something
in GitHub without showing the person first.**

---

## When you finish

Report: which steps you ran, which you skipped and why, the full `fleet-doctor`
output, and anything you could not verify.

Then stop. **Do not create a charter or start an orchestrator** — installing the
kit and using it are different things, and the person decides when to begin.

## When you get stuck

Say which step, what you ran, and both failures verbatim. Name what you think is
wrong and what you would need to get past it.

Do not widen your own access, do not disable a security control, and do not
invent a step that is not in this document. Being blocked and saying so clearly
is a good outcome. Guessing is not.

---

## Send back what you learned

This kit is maintained at **https://github.com/dw-aura/fleet-kit.git**.

If you hit something material while installing — a step that was wrong, an
assumption that did not hold on this machine, a check that passed when it should
have failed — **open a pull request.** Not an issue you file and forget: a
change, against the file that was wrong.

The reasoning is the same one this whole system rests on. A correction that stays
on one laptop has to be rediscovered by every person who installs after you, and
they will each pay the same cost you just paid. The install is the moment you
know something the document does not.

**Worth a PR:**

- A step that failed, and what actually worked.
- A `CHECK` that reported success while the thing was broken. This is the worst
  class of bug in an install and the easiest to notice exactly once.
- A `VERIFY` whose output did not mean what `PROVES` claimed.
- A prerequisite this document assumes and does not state.
- A platform difference — a different chip, an older OS, a managed machine with
  a control this did not anticipate.
- A place the wording sent you down the wrong path even though it was accurate.

**Not worth a PR:** your own repo name, your own label prefix, anything in your
config. Those are supposed to differ.

Keep it small and say what happened. One sentence of "here is what I ran, here is
what I got, here is what fixed it" is worth more than a rewrite, and it is the
part a reviewer cannot reconstruct. If you are not sure whether something is
general or particular to your machine, send it anyway and say you are not sure —
deciding that is a reviewer's job, not a blocker for you.

If you changed something to get the install working, that change is already
written. Sending it costs you one more command.
