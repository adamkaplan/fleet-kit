---
name: fleet-review
description: How a coder runs the reviewer subagents on its own pull request before offering it, and what they record. Use when your pull request works and you are about to mark it ready, or when you are asked to review one.
---

# Running the reviewers

Make the change work first. Then, in a repository that has review laws (`REVIEW_LAWS.md` at its root; `fleet-switchboard review-laws install <repo path>` adds them), run the
reviewer subagents on your pull request **before you offer it**: `reviewer-code`, `reviewer-architecture`, `reviewer-security`, `reviewer-impact` and `reviewer-test`. Invoke each with the Task
tool and the pull request reference (`owner/repo#N`); they can run side by side.

In a repository with no review laws installed, the ORCHESTRATOR runs the independent review of your pull request: do not run the five reviewers yourself.
That means it reads the change against the repository's own standards and records its verdict at the head, as the independent reviewer would.

Two things decide whether the panel can judge at all:

- **Run it from a checkout at the pull request's head.** A subagent reads the working tree of the session that launched it, not the pull request: from another branch it cannot see the files the change touches and answers `verdict: incomplete`. Switch your session to a worktree at the head (and check `git rev-parse HEAD` equals the head you record) before you invoke the reviewers.
- **Give each reviewer the FULL form of its commands in the prompt**, with the repository and the number: `fleet-switchboard review laws owner/repo#N` (a bare number is refused) and `gh pr diff N --repo owner/repo`, and say "one command per call, no pipes, no redirects".

Run `fleet-switchboard review checkout owner/repo#N` once from the directory the reviewers start in, and tell each reviewer to read the directory it prints (the exact head tree, `pr.diff` and the base copies), then `fleet-switchboard review checkout --remove <directory>` when the review is done.

Each reviewer runs its shell commands **bare**: one command per call, no pipe (`| head`), no redirect (`2>&1`, `>`), no `cd X &&` chain; the permissions refuse anything compound or with a `>` in it, and the commands it needs (`fleet-switchboard review laws <pr>`, `gh pr diff <pr>`, `gh pr view <pr>`, `gh pr checks <pr> --repo <owner/repo>`, `git diff|log|show ...`) print what it needs without them. A reviewer reads the head's check results with `gh pr checks` to judge a claim about behaviour (R-LAW-16); it still never runs, builds or tests anything, and it never opens a browser or watches (`--web`, `-w` and `--watch` are refused). Tell it so when you invoke it.

Each reviewer:
- loads the repository's laws and overlay **from the pull request's base commit** with `fleet-switchboard review laws <pr>`, never from your branch (a pull request cannot rewrite the law it is judged by);
- reads the change and judges it; it **never builds, runs or tests** anything, writes nothing and holds no write command; it has at most 50 turns and answers `verdict: incomplete` rather than ending in silence;
- **returns its verdict as text** (a `verdict:` block with `head:` and one line per finding).

Each reviewer also checks a change that deletes something or raises an alert against three lines:

- Drive the real code path in the tests, never a copied helper.
- Derive every set of kinds from one table, with a completeness test that fails when a kind is missing from it.
- Read the sibling kinds and the consumers yourself.

When a review finding names a gap in one member of a table, list, set or rule family (a row, a kind, an input, a rule), the fix covers every member in the same head: derive the set from the code, never a hand-listed copy, parametrise the tests over it with one mutant per member, and say in the PR body which members were checked; reviewers name the class, not only the instance.

When a function decides from text (an error message, a substring, a pattern, a name list), the first-round finding asks for an allow-list, a structured field or exact equality, and for a completeness test derived from the code, with one mutant per member, so the class is closed in one round and not found one instance at a time. The rule and its example are in `docs/review-laws/structural-rule.md` of the kit.

**For testers and workers who run checks.** An agent that is asked to run a mutation or fault-injection check (a tester or the orchestrator's own worker, never a reviewer, who runs nothing) runs each run through `fleet-switchboard heavy` with a time limit (`heavy -- timeout N <command>`), in its own process group which it stops as a whole when the limit is hit, and runs in the background only as a named job with a time limit and a job record (the job-start rule), never as a loose background process. The wrapper also stops a tree that passes its ceiling, but do not rely on that for a mutant that can loop.

**You record what they returned**, one record per reviewer per head: put the returned block in a file (or pipe it) and run
`fleet-switchboard review record <pr> --reviewer <role> --block <file or ->`. The verdict, the head and the counts are read from the block (a blocking finding that cites no rule counts as a note), and a block for any commit other
than the PR's current head is refused. The record is made by your session, so `gates` labels it as not independent.

Then fix the blocking findings (each cites a rule; a finding that cites none is only a proposal), functional first and then the standards, push, and run the reviewers again on the new head:
a push voids the earlier records. `fleet-switchboard gates <pr>` prints a `reviewers (shadow, not counted)` line with how many of the five recorded at the current head; it does not block you yet.

Their result is feedback for you. It never replaces the independent review agent's verdict, and a hold is never decided by you or by a reviewer: the switchboard's matrix and path floor decide it.
After an install, check that OpenCode registered each reviewer as written: `opencode api GET /api/agent` lists every agent with its `mode` and its rules (a reviewer must show mode `subagent` and the rules its definition declares; `fleet-switchboard agents verify` compares them). An agent whose front matter does not parse is silently registered as a default primary agent with a handful of rules and cannot be invoked as a subagent; quote any `description:` that contains a colon.

If you were asked to review a pull request rather than to author one, you are the independent reviewer, not one of these five: follow your own brief.
