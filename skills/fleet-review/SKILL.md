---
name: fleet-review
description: How a coder runs the reviewer subagents on its own pull request before offering it, and what they record. Use when your pull request works and you are about to mark it ready, or when you are asked to review one.
---

# Running the reviewers

Make the change work first. Then, in a repository that has review laws (`REVIEW_LAWS.md` at its root; `fleet-switchboard review-laws install <repo path>` adds them), run the
reviewer subagents on your pull request **before you offer it**: `reviewer-code`, `reviewer-architecture`, `reviewer-security`, `reviewer-impact` and `reviewer-test`. Invoke each with the Task
tool and the pull request reference (`owner/repo#N`); they can run side by side.

Each reviewer:
- loads the repository's laws and overlay **from the pull request's base commit** with `fleet-switchboard review laws <pr>`, never from your branch (a pull request cannot rewrite the law it is judged by);
- reads the change and judges it; it **never builds, runs or tests** anything, writes nothing and holds no write command; it has at most 50 turns and answers `verdict: incomplete` rather than ending in silence;
- **returns its verdict as text** (a `verdict:` block with `head:` and one line per finding).

**You record what they returned**, one record per reviewer per head: put the returned block in a file (or pipe it) and run
`fleet-switchboard review record <pr> --reviewer <role> --block <file or ->`. The verdict, the head and the counts are read from the block (a blocking finding that cites no rule counts as a note), and a block for any commit other
than the PR's current head is refused. The record is made by your session, so `gates` labels it as not independent.

Then fix the blocking findings (each cites a rule; a finding that cites none is only a proposal), functional first and then the standards, push, and run the reviewers again on the new head:
a push voids the earlier records. `fleet-switchboard gates <pr>` prints a `reviewers (shadow, not counted)` line with how many of the five recorded at the current head; it does not block you yet.

Their result is feedback for you. It never replaces the independent review agent's verdict, and a hold is never decided by you or by a reviewer: the switchboard's matrix and path floor decide it.
If you were asked to review a pull request rather than to author one, you are the independent reviewer, not one of these five: follow your own brief.
