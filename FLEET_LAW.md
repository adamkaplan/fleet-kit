# FLEET_LAW

**DRAFT, for the owner's edits. Not in force until the owner approves the text.**

The laws every change to fleet-kit is designed and reviewed against. They say how fleet-kit itself is built. **How a change is
reviewed, and what blocks it, is `REVIEW_LAWS.md`** (`docs/review-laws/`), and the general standards are `CODING_STANDARDS.md`,
`SECURITY_STANDARDS.md` and `TEST_STANDARDS.md`: this file points to them and never copies a rule, so a rule id lives in one file only.
Where a law here builds on a standard it says which, and states only what is particular to fleet-kit. A reviewer cites a law by its id
(`FK-LAW-3`) the way it cites a standard (`R-SEC-5`).

A law has three parts, each running until the next blank line: the rule; **Why:** the real failure it comes from, as issue or pull request
numbers in this repository; and **Check:** one line per check, written `<kind>: <what is asserted>`, where the kind is `test` (a test
in the repository), `gate` (a merge or plan gate), `reviewer` (a review agent's finding) or `note only` (a judgement, never a block). The
kinds are this file's own. A law with no failure behind it,
or no check, does not belong here. An **environment** is a place fleet-kit's work runs: a person's workstation, a cloud worker, CI.

## FK-LAW-1 Enforce where the action takes effect

A guard sits where the action takes effect: a server-side ruleset or branch protection, a token's scope, a cluster policy, a gate that
reads the live state. A local wrapper, hook or prompt is only a convenience on top of it, never the guard itself. Whether a guard holds
never depends on a label or a record being written in time: the rule and the gate hold it.

Builds on R-SEC-8 (a check is repeated at the point of effect); the particular part here is that the point of effect is server side.

**Why:** #488, #489: a change to a top-tier path could merge unlabelled, because the guard waited for a label that a daemon applied
later; the gate now reads the floor itself, at the exact head. #323: a cloud worker has none of a workstation's local wrappers, so
whatever lived only in them was not there.

**Check:**
- gate: G6 reads the impact floor for the exact head and does not wait for a label (`TestGateFloorFresh`, #489).
- reviewer: architecture asks where a new guard takes effect.

## FK-LAW-2 Every guard names its environments

Each guard states the environments it covers. An environment a guard does not cover is a gap, written down and assigned. A new
environment ships only with a parity table: every guard against that environment, each marked covered or a gap with an owner.

**Why:** #323, #370: the cloud worker has no shims, no tool-call judge and no agent definition; its guards are its box (a short-lived
repository-scoped token, no cluster credential, a spend cap). The same guard list looked complete from the workstation.

**Check:**
- reviewer: a change that adds a guard or an environment shows the parity table.
- note only: whether the table is complete.

## FK-LAW-3 Least privilege, and an explicit target

A credential is the narrowest that does the job: one repository, short-lived, minted when it is used. A command that reaches another
system names its target (account, repository, cluster context) and refuses when it is missing; it never falls back to a default, and
never reads the machine's current one.

Builds on R-SEC-5 (least authority); the particular part here is the explicit target and the refusal of a default fallback.

**Why:** #473: a worker launched with no recorded repository fell back to the default repository's account and was denied as the wrong
account on its first push. #450: no standing credentials, tokens minted per use. #370: the cloud launcher refuses without a named
cluster context, because the active one moves when work happens in another cluster.

**Check:**
- test: a launch with no account mapped for its owner and no default is refused, naming the missing entry (`TestLaunchIntoARepo.test_an_unmapped_owner_with_no_default_is_refused_naming_the_missing_entry`, #473).
- reviewer: security checks the scope of every credential a change adds.

## FK-LAW-4 Fail closed

When a guard cannot read what it decides on (a rule table, a ledger, a diff, an identity), it holds. Unreadable is never a pass, and
an uncertain identity is refused, not guessed.

Narrows R-CODE-2, which lets a guard fail open or closed on purpose: in fleet-kit a guard fails closed. Builds on R-SEC-1 and R-SEC-10.

**Why:** #489: the gate that cannot read the floor at the head holds, rather than passing a change it cannot judge. #138: a mixed tool
environment after a restart made one agent speak as another; an identity that cannot be told apart is refused.

**Check:**
- test: the gate does not pass when the impact floor cannot be read (`TestGateFloorFresh.test_an_unreadable_floor_does_not_pass`, #489).
- reviewer: security and architecture look for a guard whose unreadable input passes.

## FK-LAW-5 One source of truth per rule

A rule, a name or a state is written in one place; everything else reads it or cites it. A copy drifts, and the drift is how a guard
quietly stops guarding.

Builds on R-CODE-8; the particular part here is that a guard's rule is data read by one decision function.

**Why:** #102: the Chief of Staff's role name was resolved in several places until it was resolved in one. #133: the owner-hold state
lived in a first line of a description, where it had to be parsed; it moved to labels, and the old first-line reading is allowed only for a
fixed transition window, never as a second source for good. #462: the owner-hold rule became data read by one decision function.

**Check:**
- test: the decision code names no path or class and agrees with the data file (`TestOwnerHoldRule.test_s1_the_decision_code_names_no_path_or_class_and_agrees_with_the_file`, #462).
- reviewer: architecture asks where else a rule is written.

## FK-LAW-6 A check never reads the change it judges from the change

A test, a reviewer and a gate judge a change by what exists before it: the base branch's laws, the data the rule reads, not the diff
of the pull request that happens to run them. A pull request cannot rewrite the rule it is judged by.

**Why:** #455: tests that asked `git diff` about the paths a pull request changed failed every later pull request that touched
those paths. `REVIEW_LAWS.md` already has its reviewers read the laws from the base branch for the same reason.

**Check:**
- reviewer: the test reviewer rejects a test that asserts anything about the changed paths of the branch it runs on (#455).

## FK-LAW-7 Only the owner's clear lowers a guard

Raising a guard is open to anyone with the role. Lowering one (an allow rule, a hold, a floor path, a limit) is made only with the
owner's explicit clear, and never by the agent the guard constrains.

**Why:** #451: the owner's clear is reserved for credentials, identity and who may do what. #462, #520: the rule is data, one function
decides it, and the daemon re-judges waiting pull requests against it.

**Check:**
- gate: the owner-hold floor (the path floor of R-LAW-13) holds a change to a top-tier path for the owner, with the file named.
- note only: that the agent a guard constrains never lowers it is judged by the reviewing side; no gate asserts it today.

## FK-LAW-8 Say what was not done

An operation that cannot do what it was asked says so, names what is left open, and exits non-zero. It never drops the work in
silence and never reports success it did not see.

Builds on R-CODE-1 (say it failed); the particular part here is naming what is left open.

**Why:** #512: a brief given to a resume was accepted and dropped, so the agent answered the last thing it had seen and a long red
CI followed. The fix (#513) delivers it or fails and says the pane is open without it.

**Check:**
- test: a resume whose brief cannot be sent exits 1 and says the pane is open without the brief (`TestLaunch.test_s3_a_failed_send_exits_1_and_says_the_pane_is_open_without_the_brief`, #513).

## FK-LAW-9 Production is deployed from the head of main and nothing else

The owner's rule, in his words: "Do not ever deploy to production not from HEAD of Main. That introduces variablility that is difficult to sort out later." Production is deployed from the head of main and from nothing else: no release or other side branch, no cherry-pick, no revert made on a side branch. When main is not shippable, the fix goes into main.

**Why:** the owner's words above: a deploy from anywhere but the head of main introduces a variation between what is on main and what runs, and that variation is hard to sort out later. The incident behind it lives in the team repository, not in this one, and is not named here: this repository has no failure of its own to cite for this law.

**Check:**
- note only: a deploy is planned from the head of main (the reviewer and the Chief of Staff look).
- gate: a deploy workflow check, owned by the Chief of Staff: not claimed here.
