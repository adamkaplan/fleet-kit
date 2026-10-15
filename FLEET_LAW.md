# FLEET_LAW

**DRAFT, for the owner's edits. Not in force until the owner approves the text.**

The laws every change to FleetKit is designed and reviewed against. They say how FleetKit itself is built; **how a change is
reviewed, and what blocks it, is `REVIEW_LAWS.md`** (`docs/review-laws/`), which this file points to and never copies: a rule id
lives in one file only. A reviewer cites a law here by its id (`FK-LAW-3`) the way it cites a review rule (R-LAW-1).

Each law has the rule, **Why:** the real failure it comes from (an issue or pull request in this repository), and **Check:** how it
is checked (`test`, `reviewer`, `gate` or `note only`), so a law is never a wish. A law with no failure behind it, or no check, does
not belong here. An **environment** is a place FleetKit's work runs: a person's workstation, a cloud worker, CI.

## FK-LAW-1 Enforce where the action takes effect

A guard sits where the action takes effect: a server-side ruleset or branch protection, a token's scope, a cluster policy, a gate that
reads the live state. A local wrapper, hook or prompt is only a convenience on top of it, never the guard itself.

**Why:** #488, #489: a change to a top-tier path could merge unlabelled because the guard waited for a label that a daemon applied
later; the gate now reads the floor itself, at the exact head. #323: a cloud worker has none of a workstation's local wrappers, so
whatever lived only in them was not there.

**Check:** gate (G6 reads the impact floor at the head); reviewer (architecture asks where a new guard takes effect).

## FK-LAW-2 Every guard names its environments

Each guard states the environments it covers. An environment a guard does not cover is a gap, written down and assigned. A new
environment ships only with a parity table: every guard against that environment, each marked covered or a gap with an owner.

**Why:** #323, #370: the cloud worker has no shims, no tool-call judge and no agent definition; its guards are its box (a short-lived
repository-scoped token, no cluster credential, a spend cap). The same guard list looked complete from the workstation.

**Check:** reviewer (a change that adds a guard or an environment shows the table); note only for the table's completeness.

## FK-LAW-3 Least privilege, and an explicit target

A credential is the narrowest that does the job: one repository, short-lived, minted when it is used. A command that reaches another
system names its target (account, repository, cluster context) and refuses when it is missing; it never falls back to a default, and
never reads the machine's current one.

**Why:** #473: a worker launched with no recorded repository fell back to the default repository's account and was denied as the wrong account on its first push. #450: no standing
credentials, tokens minted per use. #370: the cloud launcher refuses without a named cluster context, because the active one moves
when work happens in another cluster.

**Check:** test (a missing target is refused with nothing called); reviewer (security).

## FK-LAW-4 Fail closed

When a guard cannot read what it decides on (a rule table, a ledger, a diff, an identity), it holds. Unreadable is never a pass, and
an uncertain identity is refused, not guessed.

**Why:** #489: the gate that cannot read the floor at the head holds, rather than passing a change it cannot judge. #138: a mixed tool
environment after a restart made one agent speak as another; an identity that cannot be told apart is refused.

**Check:** test (each guard has a case where its input is unreadable and the result is a hold); reviewer (security, architecture).

## FK-LAW-5 One source of truth per rule

A rule, a name or a state is written in one place; everything else reads it or cites it. A copy drifts, and the drift is how a guard
quietly stops guarding.

**Why:** #102: the Chief of Staff's role name was resolved in several places until it was resolved in one. #133: the owner-hold state
lived in a first line of a description, where it had to be parsed; it moved to labels, and the old first-line reading is allowed only for a
fixed transition window, never as a second source for good. #462: the owner-hold
rule became data read by one decision function.

**Check:** reviewer (architecture asks where else the rule is written); test (the rule's data has one reader).

## FK-LAW-6 A check never reads the change it judges from the change

A test, a reviewer and a gate judge a change by what exists before it: the base branch's laws, the data the rule reads, not the diff
of the pull request that happens to run them. A pull request cannot rewrite the rule it is judged by.

**Why:** #455: tests that asked `git diff` about the paths a pull request changed failed every later pull request that touched
those paths. `REVIEW_LAWS.md` already has its reviewers read the laws from the base branch for the same reason.

**Check:** reviewer (test); test (no test asserts anything about the changed paths of the branch it runs on).

## FK-LAW-7 Only the owner's clear lowers a guard

Raising a guard is open to anyone with the role. Lowering one (an allow rule, a hold, a floor path, a limit) is made only with the
owner's explicit clear, and never by the agent the guard constrains. Whether a guard holds never depends on a label being applied in
time: the rule and the gate hold it.

**Why:** #451: the owner's clear is reserved for credentials, identity and who may do what. #462, #520: the rule is data, one
function decides it, and the daemon re-judges waiting pull requests against it.

**Check:** gate (the owner-hold floor, `REVIEW_LAWS.md` R-LAW-13); test (a top-tier path change holds with its file named).

## FK-LAW-8 Say what was not done

An operation that cannot do what it was asked says so, names what is left open, and exits non-zero. It never drops the work in
silence and never reports success it did not see.

**Why:** #512: a brief given to a resume was accepted and dropped, so the agent answered the last thing it had seen and a long red
CI followed. The fix (#513) delivers it or fails and says the pane is open without it.

**Check:** test (each failure path has a case that asserts the message and the exit status); reviewer (code).
