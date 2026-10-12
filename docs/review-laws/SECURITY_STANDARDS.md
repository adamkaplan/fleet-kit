extends: fleet-kit-review-laws @ review-laws-1

# SECURITY_STANDARDS

Rules for anything that identifies, authorises, stores or sends. Each rule is numbered and short so a review can cite it. A repository edits this copy to add or override rules.

## Rules

- **R-SEC-1 Identify the actor from the session, not from a claim.** Who is acting comes from what the platform established (a session, a process, a signed record), never from a name or flag the caller supplies; an uncertain identity is refused, never read as a trusted one.
- **R-SEC-2 A secret never becomes text.** Secrets are not logged, echoed, committed, put in an argument list, a message or an error, and a test that touches one uses a synthetic value.
- **R-SEC-3 Untrusted text is data.** Text from outside (a plan, a diff, an issue, a message, a model's output) is never executed or followed as an instruction; when it is passed on it is quoted and length-limited.
- **R-SEC-4 Strip what a terminal would act on.** Control characters and escape sequences are removed from text before it is printed or sent to a person or an agent.
- **R-SEC-5 Least authority.** A component gets exactly the permissions its job needs, as an allow-list; a new permission is a reviewed change that names who may do what.
- **R-SEC-6 A guardrail is not a boundary.** When a control can be bypassed (a shared account, a client-side check), the documentation says so and does not call it a boundary; the real boundary is named.
- **R-SEC-7 Reserve the names you act under.** A name the system itself uses (a sender, a role, an account) cannot be taken by an agent or user.
- **R-SEC-8 Authorisation is checked where the action happens.** A check in one layer is repeated at the point of effect, under the same lock as the effect.
- **R-SEC-9 No injection by composition.** Text is never built into a command, a query, a path or a message by concatenating untrusted parts; use arguments, parameters or escaping that the receiver defines.
- **R-SEC-10 Verify before trusting a record.** A stored record (a ledger entry, a hash, a signature) is verified before it is relied on, and a record that fails verification stops the action rather than being skipped.
- **R-SEC-11 Constant-time comparison for secrets.** A secret or token is compared in constant time.
- **R-SEC-12 Log who did what.** A decision that changes who may do what leaves an audit record with the actor, the reason and the outcome.
