# The Cockpit feed

`fleet-switchboard cockpit --json` prints, and the daemon keeps `$STATE/cockpit.json` as, a read-only picture of what the fleet needs from a person. It is an allowlist: only the fields below leave the fleet, all plain text, every string capped, at most 50 items per card, newest first.

## Contract

- `schema_version` is 1. `generation` is an integer that rises on every content change. `produced_at` is set on every rewrite.
- The file is mode 0600, written atomically, rewritten on change and at least every 60 seconds.
- Ids match `^[A-Za-z0-9._:-]{1,128}$` and hold no repository or number: `dec:<16 hex>`, `pr:<16 hex>`, `lane:<orchestrator name>`, `blk:<16 hex>`, `hu:<16 hex>`.
- `etag` is the first 16 hex characters of the sha256 of the item's canonical JSON without its `etag`.
- Control characters are removed; tokens, file paths and diffs are replaced or dropped.
- The feed makes no network call, holds no credential and sends nothing back. No verb that answers, resolves or defers a decision is used by it. It writes `cockpit.json` only; `decisions.json` is untouched.

## Fields

| card | fields |
| --- | --- |
| decisions | id, etag, since, title, repo, agent, kind |
| held_prs | id, etag, repo, number, title, holds, reason, since |
| lanes | id, etag, state, since, last_seen |
| blocked | id, etag, blocked_since, blocked_on, reason |
| heads_up | id, etag, repo, number, note, sender, since |

Left out because no source exists without a GitHub call: pull request state, check results and review state.

## Who can read it

The person who owns the fleet, and the assistant that person runs for themselves.

## Questions

- Q1: Should a person be able to answer a decision from the Cockpit? This is raised, not built.
- Q2: Should the feed carry check results for a held pull request? This is raised, not built.
- Q3: Should the feed be pushed to a device instead of read? This is raised, not built.
- Q4: Should a card carry a longer description than its capped title? This is raised, not built.
