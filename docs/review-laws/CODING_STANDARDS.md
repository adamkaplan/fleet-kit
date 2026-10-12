extends: fleet-kit-review-laws @ review-laws-1

# CODING_STANDARDS

Rules a change must satisfy once it works. Each rule is numbered and short so a review can cite it. A repository edits this copy to add or override rules.

## Rules

- **R-CODE-1 Say it failed.** A failure is reported plainly with its evidence; an error is never swallowed, and success is never implied for work that did not run.
- **R-CODE-2 Fail open or closed on purpose.** A guard that cannot answer either allows (loudly, with an alarm) or refuses (with the reason); which one is a decision written down, never an accident of an exception.
- **R-CODE-3 Atomic writes.** State is written to a temporary file in the same directory, flushed, and renamed into place; a crash never leaves half a file. A log is appended under a lock.
- **R-CODE-4 Check and act together.** A check followed by an action on the same state is one critical section (a lock, a transaction, a conditional write), never two steps another process can interleave.
- **R-CODE-5 Bind the check to what is acted on.** An action runs on the exact version that was checked (a commit, a hash, a revision), and fails if that moved.
- **R-CODE-6 Bounded input.** Everything read from outside (a file, a message, a response, a list) has a size limit and a count limit before it is used.
- **R-CODE-7 Bounded work.** Loops, retries, queues and sessions have a cap, and a refusal when the cap is reached says which cap.
- **R-CODE-8 One source of truth.** A fact lives in one place; a copy is derived, marked as a copy, and checked against the source.
- **R-CODE-9 No special cases.** A rule that needs an exception is the wrong rule: fix the rule. Existing code is not evidence that a pattern is necessary.
- **R-CODE-10 Names state purpose.** A name says what a thing is for; a comment says why, never what the next line does.
- **R-CODE-11 Small and whole.** A change does one thing and leaves the repository working at every commit; unrelated cleanup is a separate change.
- **R-CODE-12 Public-safe text.** In a public repository nothing private is committed or written on the project's issues and pull requests: use neutral names in code, tests, fixtures, docs and messages, and run the repository's name check before pushing.
- **R-CODE-13 Delete, do not disable.** Dead code, stale flags and commented-out blocks are removed; history keeps them.
- **R-CODE-14 Errors carry the next step.** A message names what failed and what the reader can do about it, in one line, without echoing secrets or untrusted text raw.
- **R-CODE-15 Know why before removing.** Code, a check or a rule that looks pointless is understood (its history, its callers) before it is removed.
- **R-CODE-16 Fix the cause.** When the same kind of problem appears twice the cause is elsewhere; fix that, not each symptom.
