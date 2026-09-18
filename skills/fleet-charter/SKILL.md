---
name: fleet-charter
description: The charter convention — the single GitHub issue that carries an orchestrator's identity, scope and queue. Use when opening or maintaining a charter issue, queuing work as native sub-issues, applying or removing the `<user>:awaiting-user` label, keeping the `pane` field current, or handing a charter back at completion.
---

# Fleet Charter Convention

You are an orchestrator. This is how you stay legible to the Chief of Staff and
to the person you report to. None of the fleet's supervision works unless you
comply with it, because all of it is a join between what GitHub says your work is
and what herdr says about whether you are alive.

All charter records live in one repo, written here as `OWNER/REPO`. Use the repo
your fleet was installed against.

Labels are namespaced with your username, so two people sharing a repo do not
read each other's work as their own. Derive the prefix, do not choose it:
`whoami` answers it. Every label below carries it, structural ones included.

## 1. Open a charter before you start work

One issue, labelled `<user>:orchestrator`, opening with a fenced yaml block as
the very first thing in the body:

````
```yaml
orchestrator: <short-stable-name>
workspace: <exact herdr workspace label>
pane: <wN:pN>
reports_to: <github-handle of the person you report to>
status: active
```
````

Rules that matter:

- The block is machine-read. Flat `key: value` lines only. Do not reorder-proof
  it with nesting, comments, or prose inside the fence.
- `workspace` must match your herdr workspace label exactly. It is the only name
  the person you report to is shown; they cannot see `wN:pN`.
- `status` is `active`, `paused`, or `retired`. Nothing else.
- Below the block: your charter (what you own), your authority, and your STOP
  list. Keep it short enough to stay true.

An issue that names your pane but carries no `<user>:orchestrator` label is not a
charter. It is one label away from being one, and until that label is on, nothing
supervising the fleet can see it.

## 2. Sub-issues are your queue

Every coder assignment is a **native sub-issue** of your charter. Not a checkbox,
not a comment, not a local todo file.

```
gh issue create --repo OWNER/REPO --title "<assignment>" --body "<brief>"
# sub_issue_id is the issue's DATABASE id, not its number. Fetch it, do not guess:
id=$(gh api repos/OWNER/REPO/issues/<new-number> --jq .id)
gh api repos/OWNER/REPO/issues/<charter-number>/sub_issues -F sub_issue_id=$id
```

This gives a progress rollup for free and makes your queue readable without
anyone scraping your terminal. Close a sub-issue when its work is done and
verified, not when it is dispatched. An open sub-issue is a live claim that work
is outstanding; a charter that reports itself idle while its queue is still open
reads as an orchestrator that lost track of its own work, and someone will come
and check.

Milestones group *programs* that span several orchestrators. They are never your
per-orchestrator container.

## 3. `<user>:awaiting-user` is the fleet's only blocking channel

Apply `<user>:awaiting-user` **the moment** you need a decision only the person
you report to can make.

**Order matters, and it is not optional: label first, ask second.** Apply the
label and write the `## Decision required` section *before* you ask the question
and block on it — never after. Once you ask and block, your pane stops accepting
prompts and you can no longer be reached to do anything, including labelling
yourself. Blocking is what removes your ability to say you are blocked. Treat the
label as part of asking the question, not as a follow-up to it: a question asked
only in a pane, with no label and no `## Decision required` section on the issue,
was never actually asked — the pane is not a channel anyone monitors, and by the
time you are sitting in it waiting, you have already lost the only mechanism that
would let anyone tell you to raise your hand.

The decision itself lives in a `## Decision required` section **at the top of the
issue body**, maintained by editing the body (`gh issue edit --body`), not by
posting a comment. State it so it can be answered without reading the thread:
what you need decided, the options, your recommendation, and what stays stopped
until it is answered.

```
gh issue edit <number> --body "$(printf '## Decision required\n\n%s\n\n---\n\n%s' \
  "<question, options, recommendation, what stays stopped>" \
  "<rest of the existing body>")"
```

Why the body and not the last comment: "the question is the last comment" does
not survive a thread with automated reviewers. A reviewer bot can append
deliberation within the same minute as your attempt to post a decision, and bury
it — repeatedly, faster than you can repost. Pinning the decision to the top of
the body is the workaround someone had to invent under fire, and it is why this
convention reads the way it does. The body is stable; the comment tail is a race
anyone else posting to the thread can win.

You may still post the question as a comment as an optional supplement — it can
help surface the ping — but the `## Decision required` section in the body is the
record of record. Update it in place if the question changes; do not leave a
stale decision at the top while a newer one sits buried in comments.

Remove the label as soon as you have an answer, and remove or update the
`## Decision required` section so the body does not go on claiming a decision is
still open.

`gh issue list --label <user>:awaiting-user` is the complete and durable answer
to "what is waiting on me?" across the whole fleet. If you sit blocked without
the label, your blocker is invisible to the only person who can clear it. If you
leave the label on after an answer, you have poisoned the one list they trust.

Detection of silent blocking exists, but it is a backstop, not a substitute for
the ordering rule above. It catches the failure after the fact; it does not make
the block visible in time, and it does not restore your ability to have asked the
question properly. Label first.

Never answer an `<user>:awaiting-user` question on their behalf, and never let a
coder answer one for them.

## 4. Keep the pane field current — this is how death is detected

Your charter outlives your pane. When you are relaunched into a different pane,
**your first action is to update the `pane` field**, before you resume work.

A charter whose `pane` points at a pane that is gone, or at a bare shell with no
agent, is how the fleet detects that an orchestrator died. That detection is only
correct if living orchestrators keep the field honest. A stale `pane` on a healthy
charter makes a live orchestrator look dead; a fresh `pane` on an abandoned one
makes a corpse look alive. Both cost someone real time.

Pane addresses are only unique within one herdr server. A `wN:pN` from another
machine is a different agent wearing the same name, which is the other reason the
label prefix is not optional.

If your fleet runs a heartbeat that expects an acknowledgement, send exactly one
per wake and let the cadence tell the truth: short when work is in flight, long
when there is genuinely nothing to do. Acking idle while your charter still has
open sub-issues is how you idle yourself out of existence — the failure mode that
looks healthiest from the outside.

## 5. Hand back at completion

When the charter's work is finished:

1. Close every sub-issue with its disposition — done, deferred, or dropped and
   why. Do not close a sub-issue by silence.
2. Set `status: retired` in the yaml block.
3. Post a final comment: what shipped, what did not, where the evidence is, and
   anything the next orchestrator inherits.
4. Close the charter.

Retire the charter before you retire the pane. A charter left `active` behind a
dead pane reads as a failure, not a finish.

## Reporting up

The Chief of Staff pushes blockers upward and stays quiet otherwise. Give it the
same courtesy: raise blockers and divergences, not status. Your charter and its
sub-issues already carry your status. If someone has to ask you what you are
doing, the convention has already failed.
