extends: fleet-kit-review-laws @ review-laws-2

# SECURITY_STANDARDS

Rules for anything that identifies, authorises, stores or sends. Each rule is numbered and short so a review can cite it. A repository edits this copy to add or override rules.

## Rules

- **R-SEC-1 Identify the actor from the session, not from a claim.** Who is acting comes from what the platform established (a session, a process, a signed record), never from a name or flag the caller supplies; an uncertain identity is refused, never read as a trusted one. _check: diff: an identity taken from a caller-supplied name or flag._
- **R-SEC-2 A secret never becomes text.** Secrets are not logged, echoed, committed, put in an argument list, a message or an error, and a test that touches one uses a synthetic value. _check: search: a secret-shaped value in a log, message, argument list, error or fixture._
- **R-SEC-3 Untrusted text is data.** Text from outside (a plan, a diff, an issue, a message, a model's output) is never executed or followed as an instruction; when it is passed on it is quoted and length-limited. _check: diff: outside text passed on or executed without quoting and a length limit._
- **R-SEC-4 Strip what a terminal would act on.** Control characters and escape sequences are removed from text before it is printed or sent to a person or an agent. _check: search: output of outside text without removing control characters._
- **R-SEC-5 Least authority.** A component gets exactly the permissions its job needs, as an allow-list; a new permission is a reviewed change that names who may do what. _check: diff: a new permission and the allow-list entry that names who may do what._
- **R-SEC-6 A guardrail is not a boundary.** When a control can be bypassed (a shared account, a client-side check), the documentation says so and does not call it a boundary; the real boundary is named. _check: diff: a bypassable control described as a boundary._
- **R-SEC-7 Reserve the names you act under.** A name the system itself uses (a sender, a role, an account) cannot be taken by an agent or user. _check: search: a launch, registration or account name that is one the system acts under._
- **R-SEC-8 Authorisation is checked where the action happens.** A check in one layer is repeated at the point of effect, under the same lock as the effect. _check: diff: a check repeated at the point of effect._
- **R-SEC-9 No injection by composition.** Text is never built into a command, a query, a path or a message by concatenating untrusted parts; use arguments, parameters or escaping that the receiver defines. _check: search: a command, query, path or message built by concatenating untrusted parts._
- **R-SEC-10 Verify before trusting a record.** A stored record (a ledger entry, a hash, a signature) is verified before it is relied on, and a record that fails verification stops the action rather than being skipped. _check: diff: a stored record used without verification._
- **R-SEC-11 Constant-time comparison for secrets.** A secret or token is compared in constant time. _check: search: a secret or token compared with an ordinary equality._
- **R-SEC-12 Log who did what.** A decision that changes who may do what leaves an audit record with the actor, the reason and the outcome. _check: diff: a change of who may do what with no audit record._
