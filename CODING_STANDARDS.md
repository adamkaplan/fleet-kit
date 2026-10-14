extends: fleet-kit-review-laws @ review-laws-2

# CODING_STANDARDS

Rules a change must satisfy once it works. Each rule is numbered and short so a review can cite it. A repository edits this copy to add or override rules.

## Rules

- **R-CODE-1 Say it failed.** A failure is reported plainly with its evidence; an error is never swallowed, and success is never implied for work that did not run. _check: diff: an exception handler that drops the error, or a success message for work that did not run._
- **R-CODE-2 Fail open or closed on purpose.** A guard that cannot answer either allows (loudly, with an alarm) or refuses (with the reason); which one is a decision written down, never an accident of an exception. _check: diff: every guard states its fail mode in a comment or a test._
- **R-CODE-3 Atomic writes.** State is written to a temporary file in the same directory, flushed, and renamed into place; a crash never leaves half a file. A log is appended under a lock. _check: search: a write to a final path without a temporary file and a rename._
- **R-CODE-4 Check and act together.** A check followed by an action on the same state is one critical section (a lock, a transaction, a conditional write), never two steps another process can interleave. _check: diff: a check and its action are inside one lock, transaction or conditional write._
- **R-CODE-5 Bind the check to what is acted on.** An action runs on the exact version that was checked (a commit, a hash, a revision), and fails if that moved. _check: diff: the action names the checked version (a hash, a commit, a revision)._
- **R-CODE-6 Bounded input.** Everything read from outside (a file, a message, a response, a list) has a size limit and a count limit before it is used. _check: search: a read of outside input without a size or count limit._
- **R-CODE-7 Bounded work.** Loops, retries, queues and sessions have a cap, and a refusal when the cap is reached says which cap. _check: search: a loop, retry or queue without a cap._
- **R-CODE-8 One source of truth.** A fact lives in one place; a copy is derived, marked as a copy, and checked against the source. _check: diff: a second copy of a fact with no check against the source._
- **R-CODE-9 No special cases.** A rule that needs an exception is the wrong rule: fix the rule. Existing code is not evidence that a pattern is necessary. _check: note only: a judgement about design._
- **R-CODE-10 Names state purpose.** A name says what a thing is for; a comment says why, never what the next line does. _check: note only: a judgement about naming._
- **R-CODE-11 Small and whole.** A change does one thing and leaves the repository working at every commit; unrelated cleanup is a separate change. _check: diff: unrelated files or concerns in one change._
- **R-CODE-12 Public-safe text.** In a public repository nothing private is committed or written on the project's issues and pull requests: use neutral names in code, tests, fixtures, docs and messages, and run the repository's name check before pushing. _check: search: the repository's name check passes on the change._
- **R-CODE-13 Delete, do not disable.** Dead code, stale flags and commented-out blocks are removed; history keeps them. _check: search: commented-out code or a flag with no use._
- **R-CODE-14 Errors carry the next step.** A message names what failed and what the reader can do about it, in one line, without echoing secrets or untrusted text raw. _check: note only: a judgement about message quality._
- **R-CODE-15 Know why before removing.** Code, a check or a rule that looks pointless is understood (its history, its callers) before it is removed. _check: note only: a judgement; the history and callers are the evidence._
- **R-CODE-16 Fix the cause.** When the same kind of problem appears twice the cause is elsewhere; fix that, not each symptom. _check: note only: a judgement about recurrence._
