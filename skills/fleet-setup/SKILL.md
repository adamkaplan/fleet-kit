---
name: fleet-setup
description: How the Chief of Staff commissions a new project — its own GitHub repo, herdr workspace and orchestrator — as a decision the person makes, then a worker's job, then a check that the switchboard watches it. Use when your principal asks for a new project, a new repo, or an orchestrator for a repo that has none. Also how to split an overloaded orchestrator's work into a second lane on the same repo. Not for adding work to a project that already has an orchestrator with room for it.
---

# Commissioning a project

**You are the Chief of Staff.** This document is for you. A project is one repo,
one herdr workspace and one orchestrator; its coders are tabs in that workspace.
Projects are independent: different repos, often different GitHub owners and
different `gh` accounts, one switchboard, one fleet.

## How to work this document

1. **Work the steps in order.** Each has a `CHECK`, a `DO` and a `VERIFY`. If a
   check is already satisfied, say so and move on.
2. **You commission; you never create.** You do not run `gh repo create`, `gh repo
   delete` or `gh repo edit --visibility`, and you do not write the project's code.
   A worker does the creating, after your principal has said yes. The tool-call
   judge treats those commands as hard to reverse whatever the thresholds say, so
   an agent that runs one is stopped to ask.
3. **Run every `VERIFY` and show its real output.** Never report a project as
   commissioned that you did not see watched.
4. **A token is never text.** Do not run `gh auth switch`, do not print a token,
   do not put one in a brief, an issue or a message. The switchboard hands each
   agent its account's token in its environment; that is the only place it is.
5. **Two failures on the same obstacle and you stop.** Report both and what you
   think is wrong.

---

## STEP 1 — Check which account owns the repo

```
CHECK:   fleet-switchboard status --json
         Read github.detail: for a repo of the same owner, "account" names the gh account.
         The map itself is gh_users in the switchboard's config.json:
           {"acme/api": "alice-bot", "acme/*": "acme-ci"}   (a repo entry, then an owner entry)
         gh auth status   lists the accounts gh is logged in as.
DO:      Decide the account for <owner>/<repo>:
           - an entry for the repo or its owner exists  -> that is the account;
           - none exists and the person named one       -> use it, and tell them the exact
             line to add to gh_users (the daemon reloads its config on its own a few
             seconds after the edit);
           - none exists and none was named             -> ask which account, in STEP 2.
         Do not edit the config yourself, and never switch the active gh account.
VERIFY:  The account name is a login that `gh auth status` lists.
PROVES:  The new project's agents will work as an account that can create the repo.
```

If the owner has no entry and the daemon has no default token, `launch` refuses
and names the entry it wants. That refusal is the check working: add the entry.

---

## STEP 2 — Record the decision for the person

```
CHECK:   Has the person already said yes to exactly this? "Create <owner>/<repo>
         (private) under account <acct>?"   Yes -> STEP 3.
DO:      Record the question where the person will see it, in this order:
         1. A repo already exists for this person: open an issue in it, labelled
            <user>:awaiting-user, titled "Decision: create <owner>/<repo> (private)
            under account <acct>?", with a Decision required section: what, why,
            the account, that it is private, and what you will do on yes and on no.
              gh issue create --repo <existing> --label "<user>:awaiting-user" \
                --title "..." --body-file -
         2. An orchestrator is already working with this person: ask it to put the
            question to them (`fleet-switchboard report question "..."` is how it
            says it).
         3. Neither exists: ask in chat, in one message that stands alone.
         Then stop. Do not start STEP 3 on your own guess.
VERIFY:  The issue URL, or the question as asked.
PROVES:  The person can answer without reading anything else.
```

Never answer a `<user>:awaiting-user` question for the person.

---

## STEP 3 — On a yes, commission the workspace and the orchestrator

```
CHECK:   The person's yes, quoted. The local directory for the checkout exists:
           mkdir -p <checkout>
         The workspace label is chosen: it is what the person is shown, and the
         charter's `workspace:` line must match it exactly.
DO:      Launch the orchestrator into a new workspace, with a brief on stdin:
           fleet-switchboard launch <name> --agent orchestrator --role orchestrator \
             --dir <checkout> --new-workspace "<label>" --repo <owner>/<repo> \
             --reports-to chief-of-staff --brief-file -
         The brief says, in this order:
           1. Create <owner>/<repo> as a private repo, as the account you were given
              (gh repo create <owner>/<repo> --private). The judge will ask the person
              once about that command: that is expected; wait for the answer.
           2. Create the labels in it: `fleet-switchboard labels ensure --repo <owner>/<repo>`
              (<user>:orchestrator, <user>:awaiting-cos, <user>:awaiting-user).
           3. Open the charter issue (the fleet-charter skill), with `workspace: <label>`
              exactly as above, and label it <user>:orchestrator.
           4. Report: fleet-switchboard report done "repo, labels and charter are up: <charter url>".
VERIFY:  The launch prints a session and a pane. `herdr workspace list` shows <label>.
PROVES:  One orchestrator, in its own workspace, working as the repo's account.
```

If `launch` refuses, say what it said. A mismatched label, a missing `gh_users`
entry and a name already running are all refusals with their reason; none is a
reason to try another way.

---

## STEP 4 — Confirm the repo is watched

```
CHECK:   Wait for the orchestrator's report, or its charter issue to exist.
DO:      fleet-switchboard status
VERIFY:  The line for <owner>/<repo> under github: how it is watched, its account,
         its agents. Paste it.
PROVES:  "forwarder", the account you expect, the orchestrator listed under agents.
```

Say plainly what the line says:

- **forwarder** — events arrive as they happen.
- **polling every Ns** — the account cannot create a webhook (it needs admin on the
  repo), so the repo is read on a poll. Events still arrive, with up to N seconds'
  delay. Tell the person once; you cannot fix a permission.
- **failed** — nothing can read the repo. Before the orchestrator has created it,
  that is expected, and the switchboard retries on its own backoff and starts the
  forwarder when the repo appears. After the orchestrator says it is done, it is a
  fault: report it, with the status line.
- **not yet** — the daemon has not made its next pass. Wait one pass, look again.

A repo that is not listed at all is not watched: say so, and do not call the
project commissioned. When a repo that already exists joins, run
`fleet-switchboard labels ensure --repo <owner>/<repo>` yourself. Your standing orders
are the owner's one file, `standing-orders.md` beside the switchboard config
(`fleet-switchboard orders --cos`): with none, say once that nothing is answered for them.

---

## Splitting a project into lanes

A lane is one orchestrator and its charter. Use this when one orchestrator holds more
asks than it moves (several orchestrators on one repo): the project stays one repo, but
the asks are shared out. Split a lane under the owner's standing order that lets you commission new orchestrators without asking, then tell the person after, citing that order (`orders --cos` or `orders list --charter <n>` shows it); with no such standing order, propose the split first and act only after the person agrees (quote their agreement). This section is only how. The
person's words are never changed on the way, and the new lane starts with what the old
one knew.

Two commands below, `fleet-switchboard orders copy` and `fleet-switchboard move`, are
run by the operator (the person at a shell), or by you once your permission list allows
them. Until it does, they are the operator's: ask the person to run them, give them the
exact command, and do not claim the step is done before you have seen its output.

### Lane step 1 — The workspace and the orchestrator

```
CHECK:   Which case of the rule above holds: the standing order, read and cited, or the
         person's agreement, quoted. The repo is already watched (STEP 4 above), and its
         account is already set (STEP 1).
DO:      Launch the new orchestrator into its OWN workspace on the same repo, with a
         brief on stdin:
           fleet-switchboard launch <name> --agent orchestrator --role orchestrator \
             --dir <checkout> --new-workspace "<label>" --repo <owner>/<repo> \
             --reports-to chief-of-staff --brief-file -
         The checkpoint line of the brief is machine-readable, in exactly this form
         (`launch` reads it and sets both reminders):
           CHECKPOINT: Report at 30 minutes (kind working, one line); overdue at 60.
VERIFY:  The launch prints a session and a pane, and says it set the checkpoint reminders.
PROVES:  A second orchestrator, in its own workspace, working as the repo's account.
```

### Lane step 2 — The charter

```
CHECK:   The new orchestrator's brief asks it to open its own charter (the
         fleet-charter skill) in the same repo.
DO:      The charter's `workspace:` line is the new workspace's label, exactly, and
         its name differs from the old lane's. Its own `## Standing orders` section
         stays empty until step 4.
VERIFY:  The charter issue exists and `fleet-switchboard status` lists the new
         orchestrator with its charter number.
PROVES:  The new lane has somewhere for asks to live.
```

### Lane step 3 — The labels

```
CHECK:   `fleet-switchboard labels ensure --repo <owner>/<repo>` (it only adds what is
         missing, so it is safe to run again).
DO:      Put the orchestrator label on the new charter. Every ask keeps the labels it
         has.
VERIFY:  The new charter carries <user>:orchestrator.
PROVES:  The switchboard can tell the new charter from an ask.
```

### Lane step 4 — The standing orders, copied

```
CHECK:   Read the sibling charter's orders: fleet-switchboard orders list --charter <old charter>
DO:      Run this (the operator's command, or yours once your permissions allow it).
         It copies them word for word from the sibling, with their original dates:
           fleet-switchboard orders copy --source <old charter> --charter <new charter>
         The command carries each order exactly as the person gave it and prints what it
         did, one line per order and one summary line. Never retype, shorten or reword an
         order, and never add one the sibling does not hold: the new lane has the
         person's existing words and nothing more. An order that fails is named; run
         the command again and it copies only what is missing.
VERIFY:  The summary line says how many copied and how many failed (none failed), and
         `orders list --charter <new charter>` matches the sibling's list.
PROVES:  The new lane works under the same standing orders as the old one.
```

### Lane step 5 — Coders finishing under their old boss

```
CHECK:   fleet-switchboard status: which coders work on the asks that will move.
DO:      Leave them. A coder that is working keeps its recorded boss: it finishes
         under the orchestrator that launched it, reports to it, and is retired by it.
         Only new work launched after the move reports to the new orchestrator.
VERIFY:  After the move, `fleet-switchboard status` shows each of those coders still
         reporting to the old orchestrator.
PROVES:  Nothing a worker was told, or owes, changed under it.
```

### Lane step 6 — Move the asks, with their handover

```
CHECK:   The asks to move are named, each open, each under the old charter.
DO:      One command per ask (the operator's command, or yours once your permissions
         allow it):
           fleet-switchboard move <ask> --to <new charter>
         It re-parents the ask (never its text), carries the old orchestrator's
         reminders on it to the new one, sends the new orchestrator a handover note and
         the old one a notice naming the coders still finishing, and prints the asks the
         old charter still holds. Read that list: an ask nobody named is still there.
         A handover note has these six sections, and the move writes them for you:
           ## State
           ## Pull requests and heads
           ## Branches
           ## Workers
           ## Waits
           ## Reminders
VERIFY:  The new orchestrator's own list of asks (`fleet-switchboard intents`) shows it.
PROVES:  The ask is under the new lane and nothing about it was lost.
```

