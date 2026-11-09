# Guard-parity law ids

The `FK-LAW-n` ids that `docs/cloud-guard-parity.md` cites. **This is the guard-parity list, one sentence per id, stating only what
the table needs.** The fuller law text (the rule, the failure behind it, and its checks) is a **draft owned elsewhere**: draft PR #544,
branch `switchboard/fleet-law`, file `FLEET_LAW.md`. It is not in force until the owner approves it; where it differs from this list, it
decides. This file is a lookup, not law. How a change is reviewed is `REVIEW_LAWS.md` (`R-LAW-n`, `R-CODE-n`, `R-SEC-n`, `R-TEST-n`).

`cloud/tests/check-parity-table` reads the ids below: an id the table cites that is not listed fails the check. A listed id the table
never cites is fine. Each entry is one line, `- FK-LAW-n: sentence`.

- FK-LAW-1: A guard sits where the action takes effect (a server rule, a token's scope, a cluster policy); a local wrapper, hook or prompt is only a convenience on top of it.
- FK-LAW-2: Each guard names the environments it covers; an environment it does not cover is a gap written down and assigned, and a new environment ships with a parity table.
- FK-LAW-3: A credential is the narrowest that does the job, and a command names its target and refuses without it, never falling back to a default.
- FK-LAW-4: When a guard cannot read what it decides on, it holds: unreadable is never a pass.
- FK-LAW-5: A rule, a name or a state is written in one place; everything else reads or cites it.
- FK-LAW-6: A check judges a change by what exists before it, never by reading the change it judges.
- FK-LAW-7: Only the owner's explicit clear lowers a guard, and never the agent the guard constrains.
- FK-LAW-8: An operation that cannot do what it was asked says so, names what is left open, and exits non-zero.
