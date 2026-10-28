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

- Q1: May fleet data leave the fleet at all, and by file (a read-only mount, recommended) or by post (a new narrow credential kind in the Venue)? This is raised, not built.
- Q2: May anyone beyond the person and that person's own assistant read the fleet cards? Answered by the Chief of Staff: no. Nothing widens it.
- Q3: May the person's assistant read item texts (titles), or only counts and ids? This is raised, not built.
- Q4: Fleet history stays in the Venue's append-only log forever: acceptable, or a bounded replaced cache? This is raised, not built.
