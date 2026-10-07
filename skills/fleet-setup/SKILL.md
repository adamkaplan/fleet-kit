---
name: fleet-setup
description: How the Chief of Staff commissions a new project — its own GitHub repo, herdr workspace and orchestrator — as a decision the person makes, then a worker's job, then a check that the switchboard watches it. Use when your principal asks for a new project, a new repo, or an orchestrator for a repo that has none. Not for adding work to a project that already has an orchestrator.
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
           2. Create the labels <user>:orchestrator and <user>:awaiting-user in it.
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
project commissioned.
