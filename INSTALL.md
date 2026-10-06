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
DO:      macOS:  brew install herdr
         Linux:  the distro package manager, or  mise use -g herdr
VERIFY:  herdr --version
PROVES:  a version prints. 0.9.0 or later.
```

herdr is in homebrew-core, so no tap is needed. If `brew` itself is missing, that
is a `HUMAN` step — stop and say so rather than installing Homebrew unprompted.

On Linux, **check the version, not just presence.** fleet-kit needs 0.9.0 or
later and some distro packages lag — Omarchy, for one, has shipped 0.8.2. If the
packaged herdr is too old, install a current one with `mise use -g herdr` and
make sure it wins on PATH. Do not `herdr update` over a package-managed binary:
the package manager owns that file and will fight you for it.

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
CHECK:   ls ~/.agents/skills/fleet-charter ~/.agents/skills/fleet-coordination ~/.agents/skills/fleet-setup
         All present?  -> skip to STEP 9.
DO:      mkdir -p ~/.agents/skills
         ln -s "$PWD/skills/fleet-charter"     ~/.agents/skills/fleet-charter
         ln -s "$PWD/skills/fleet-coordination" ~/.agents/skills/fleet-coordination
         ln -s "$PWD/skills/fleet-setup"       ~/.agents/skills/fleet-setup
VERIFY:  Copilot CLI:  copilot skill list
         opencode:     ls ~/.agents/skills/fleet-*/SKILL.md
PROVES:  Copilot CLI: all three named under "Personal skills", with their descriptions.
         opencode: all three SKILL.md paths listed, through the links.
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
         opencode v2:  opencode api get /api/agent | grep -o '"chief-of-staff"'
         opencode v1:  opencode agent list | grep '^chief-of-staff '
PROVES:  the agent resolves by name. An unknown-agent error (Copilot) or no match
         (opencode) means it did not install.
```

`opencode --version` tells the generations apart. v1 prints a bare `1.x.y`, and
v2 prints `opencode v2.x.y`. `./bin/fleet-doctor` classifies it on its
`dispatch` line.

Do not verify opencode with `opencode --agent … --help`. OpenCode v2's TUI has
no `--agent` flag, and `--help` exits 0 before anything is resolved, so that
check passes whether or not the agent exists. `opencode api` asks the running
v2 service for the agents it actually loaded. v1 has no `api` subcommand; its
`opencode agent list` loads the same definitions.

How a named agent is started differs by CLI. Copilot CLI and opencode v1 take
`--agent <name>` on the interactive command. The opencode v2 TUI does not. On
v2, create the session with its agent (`opencode api session.create` with
`agent`), then start the TUI on it with `opencode -s <session-id>`. By hand, you
can also start plain `opencode` and pick the agent with `/agents` (or `Ctrl+X`
`A`, or `Shift+Tab` to cycle). The full recipe for starting a coder in a herdr
pane, including how to recover from a failed launch, is in the
`fleet-coordination` skill under "Starting a named agent in a pane".

```
CHECK:   grep -l '__MODEL_ID__\|__PROVIDER__' <agents dir>/*.md
         Nothing listed?  -> done with this step.
HUMAN:   yes, if the person has not said which model. Ask; do not guess.
DO:      Copilot CLI:  sed -i.bak 's|__MODEL_ID__|<model-id>|' ~/.copilot/agents/*.md
         opencode:     sed -i.bak 's|__PROVIDER__/__MODEL_ID__|<provider>/<model-id>|' \
                         ~/.config/opencode/agents/*.md
VERIFY:  the CHECK above lists nothing, then re-run the VERIFY above.
PROVES:  every definition names a real model.
```

**The definitions ship with a placeholder `model:` line** — `__MODEL_ID__` for
Copilot CLI, `__PROVIDER__/__MODEL_ID__` for opencode. Left unsubstituted, the
harness cannot resolve the model and the agent will not start. Use an id the
harness actually lists (`opencode models`, or the Copilot CLI `/model` picker).
Delete the `.bak` files once it works.

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
DO:      herdr integration install <kind>       # e.g. copilot, opencode, claude, codex
VERIFY:  herdr integration status
PROVES:  your harness reports "current", not "not installed" or "outdated".
```

`<kind>` is the harness's herdr name; `herdr integration status` lists them,
and herdr's [integrations doc](https://herdr.dev/docs/integrations/) has the
per-agent details.

If the install fails with `copilot config directory not found`, Copilot CLI has
never been run on this machine and `~/.copilot` does not exist yet. Run the
harness once (start it and quit) and retry.

Install this **before** starting the agents you want supervised. After a herdr
server restart, native agent session restore only works for panes whose
integration was installed before the agent started.

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
CHECK:   macOS:  launchctl list | grep com.fleet-kit.heartbeat
         Linux:  systemctl --user is-enabled fleet-kit-heartbeat.service
         Already loaded / enabled?  -> skip to STEP 12.
DO:      ./bin/fleet-heartbeat init
         mkdir -p ~/.local/bin && ln -sf "$PWD/bin/heartbeat-ack" ~/.local/bin/heartbeat-ack
         macOS:
         ./bin/fleet-heartbeat plist > ~/Library/LaunchAgents/com.fleet-kit.heartbeat.plist
         launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fleet-kit.heartbeat.plist
         Linux:
         mkdir -p ~/.config/systemd/user
         ./bin/fleet-heartbeat unit > ~/.config/systemd/user/fleet-kit-heartbeat.service \
           && systemctl --user daemon-reload \
           && systemctl --user enable --now fleet-kit-heartbeat.service
VERIFY:  ./bin/fleet-heartbeat status
PROVES:  a service line that is not FAULT, and a
         "service manager:" note saying loaded (macOS) or active (Linux).
```

On Linux the service's stderr goes to the journal:
`journalctl --user -u fleet-kit-heartbeat.service`. A user unit stops when the
person logs out unless lingering is on — on a headless box, `loginctl
enable-linger $USER` is a `HUMAN` decision worth raising.

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
list` shows every tab holding an agent, its kind, whether it opts in and whether
the heartbeat can wake it, so if something is not being woken, that command
answers why.

**The heartbeat wakes a pane only when it can positively recognise that agent's
empty input box** — the same check that stops it typing over a half-written
draft or into a dialog. Today that is **opencode** only. Any other
herdr-detected agent (Copilot CLI, Claude Code, Codex, pi, …) with 🔔 in its tab is
never sent anything; `./bin/fleet-heartbeat list` shows it as not wakeable
(`WAKEABLE no`), and `status` names the reason
(`agent_kind_not_wakeable:<kind>`). If the person runs orchestrators in one of
those, tell them this step will not supervise those panes. Supporting another
agent means adding its input-box check with captured fixtures for an empty box,
a draft and a dialog. pi is a candidate: its check exists, pending a fixture of
a pi-native selector or menu that reads as not ready.

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

## The Fleet Switchboard (optional: STEPS 14 to 19)

The Fleet Switchboard is the system around the fleet: it delivers messages
between agents by OpenCode v2's own message queue (never by typing into a
pane), keeps a Chief of Staff, and judges tool calls. Do this part only if the
person asked for it. There is one Chief of Staff, one fleet and one daemon per
user. The Chief of Staff never does work. Two steps need the person (the Copilot
login and a key), and you stop at each. Read `docs/switchboard.md`, "Install
(PR 16)", if anything here surprises you.

---

## STEP 14 — OpenCode v2

```
CHECK:   V2="$HOME/.local/share/fleet-switchboard/v2/node_modules/@opencode/cli/bin/opencode.exe"
         "$V2" --version
         Prints "opencode v2.<minor>.<patch>"?  -> skip to STEP 15.
DO:      npm install --prefix "$HOME/.local/share/fleet-switchboard/v2" \
           --no-audit --no-fund --ignore-scripts @opencode/cli@2.0.22
         (cd "$HOME/.local/share/fleet-switchboard/v2/node_modules/@opencode/cli" && node ./postinstall.mjs)
VERIFY:  "$V2" --version
PROVES:  a version that starts with "opencode v2.". Run it by this absolute path.
```

Never trust the `opencode` on `PATH`: it may be OpenCode v1, which the person's
other fleet runs on. Never use `npm -g` or the curl installer, which replace
that v1 binary. If `node` or `npm` is missing, that is a `HUMAN` step: stop and
say so.

---

## STEP 15 — herdr with plugins

```
CHECK:   herdr plugin list
         Prints a list (even an empty one)?  -> skip to STEP 16.
DO:      update herdr the way STEP 2 installed it. Plugins need 0.9.3 or later.
VERIFY:  herdr --version; herdr plugin list
PROVES:  the version is 0.9.3 or later and `plugin list` exits 0.
```

`install` (the next step) links the Fleet Switchboard's plugin. Do not run
`herdr plugin link` yourself.

---

## STEP 16 — Install it

```
CHECK:   ./bin/fleet-switchboard install --dry-run
         Every line says "already done"?  -> skip to STEP 17.
DO:      show the person the dry run, and read them the herdr plugin notice in it.
         HUMAN: the plugin's hooks run for every pane in herdr, including panes
         that run other agents. They are silent and type nothing, but it is the
         person's call. Wait for a yes, then:
         ./bin/fleet-switchboard install --opencode "$V2"
VERIFY:  ./bin/fleet-switchboard install --opencode "$V2"
PROVES:  every step says "already done" on this second run (the first one says
         "done" or "already done"), and no step says FAILED.
```

`install` is safe to repeat. It finds the v2 binary (it refuses OpenCode v1 and
says how to get v2), writes only the config keys that are missing and never
touches the ones the person edited, puts the kit's agents, skills and plugins
and herdr's OpenCode integration into a **private v2 profile** (by default
`~/.local/share/fleet-switchboard/profile`), links the herdr plugin, installs a
service that keeps the daemon alive (a LaunchAgent on macOS, a systemd user unit
on Linux), starts the daemon, and prints `status`. `install --uninstall` reverses
all of it except the config, the state and the sessions. Add `--no-service` or
`--no-herdr-link` to skip either one, and say so in your report.

The profile is private on purpose. The person's ordinary OpenCode (possibly v1,
which their other fleet may run on) reads `~/.config/opencode`, and `install`
never writes there: v2-shaped files in it can stop every v1 agent at start. The
dry run prints the profile path it would use; read it to the person. Never pass
`--shared-profile` unless the person asks for it by name: it puts the files where
their ordinary OpenCode reads them, and says so.

If `status` begins with "No decision-model key", that is STEP 18, not a failure.

---

## STEP 17 — The Copilot login

```
CHECK:   "$HOME/.local/share/fleet-switchboard/bin/fleet-opencode" auth list
         Lists GitHub Copilot?  -> skip to STEP 18.
HUMAN:   yes. The login is an interactive device-code flow in a browser.
DO:      tell the person to run
         "$HOME/.local/share/fleet-switchboard/bin/fleet-opencode" auth login
         and choose GitHub Copilot. Wait.
VERIFY:  "$HOME/.local/share/fleet-switchboard/bin/fleet-opencode" auth list
PROVES:  GitHub Copilot is listed.
```

Use the wrapper `install` wrote (`fleet-opencode`), not the bare binary: it
points v2 at the private profile and sets the environment the plugins need. The
login is stored in that profile, so the person's ordinary OpenCode is not
affected and does not see it.

---

## STEP 18 — The decision-model key

```
CHECK:   ./bin/fleet-switchboard key status
         Prints "file" or "environment"?  -> skip to STEP 19.
HUMAN:   yes. Only the person has the key.
DO:      tell the person to run ./bin/fleet-switchboard key set and paste the
         key (nothing is echoed; it is saved to a private file, mode 0600).
         Do not ask them to paste it to you. Do not put it in an argument, an
         environment variable of yours, a file you write, or this transcript.
VERIFY:  ./bin/fleet-switchboard key status
PROVES:  "file" (or "environment").
```

Without a key the message classifier and the tool-call judge fail open: the
system looks healthy and nothing is classified or judged. That is why `status`
says so in its first line. Do not call the install done while it does.

---

## STEP 19 — Open the Chief of Staff, and prove it

```
CHECK:   ./bin/fleet-switchboard status
         A "cos" agent listed?  -> it is open; skip bootstrap.
DO:      ./bin/fleet-switchboard bootstrap
VERIFY:  ./bin/fleet-switchboard status
PROVES:  the first line is not "No decision-model key"; the lines `daemon`
         (running), `key` (file or environment), `plugin` (linked) and `service`
         (installed) are all present and say so; and `cos` is listed with a pane.
```

`bootstrap` opens the Chief of Staff in its own herdr workspace and focuses it.
Run again, it resumes the same Chief of Staff if its pane is gone, and refuses
to open a second one if it is still there. The Chief of Staff introduces
itself, reads `status`, and tells the person plainly what is missing (a key,
the GitHub repos to watch) and asks what they want to work on. It sets the
repos up with them; you do not.

---

## When you finish

Report: which steps you ran, which you skipped and why, the full `fleet-doctor`
output (and, if you did STEPS 14 to 19, the full `status` output), and anything
you could not verify.

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

This kit is maintained at **https://github.com/adamkaplan/fleet-kit**.

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
