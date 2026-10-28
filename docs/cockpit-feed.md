# The Cockpit feed

`fleet-switchboard cockpit --json` prints, and the daemon keeps `$STATE/cockpit.json` as, a read-only picture of what the fleet needs from a person. It is an allowlist: only the fields below leave the fleet, all plain text, every string capped, at most 50 items per card, newest first.

## Contract

- `schema_version` is 1. `generation` is an integer that rises on every content change. `produced_at` is set on every rewrite.
- The file is mode 0600, written atomically, rewritten on change and at least every 60 seconds.
- Ids match `^[A-Za-z0-9._:-]{1,128}$` and hold no repository or number: `dec:<16 hex>`, `pr:<16 hex>`, `lane:<orchestrator name>`, `blk:<16 hex>`, `hu:<16 hex>`.
- `etag` is the first 16 hex characters of the sha256 of the item's canonical JSON without its `etag`.
- Control and format characters (Unicode category Cf: bidi overrides, zero-width characters) are removed; tokens, file paths and diffs are replaced or dropped.
- The feed makes no network call, holds no credential and sends nothing back. No verb that answers, resolves or defers a decision is used by it. It writes `cockpit.json` only; `decisions.json` is untouched.

## Sources: an unreadable source is not an all-clear

The document carries `sources`: for each of the three places the feed reads (`decisions` = `decisions.json`, `registry` = `registry.json`, `reports` = `last-reports.json`) either `ok` or `unreadable` (missing, damaged, a symlink or special file, or over the size cap). An unreadable source reads as empty cards, so `sources` is how a reader tells that from a quiet fleet.

- The verb prints the document and exits status 3 when any source is unreadable (0 otherwise).
- The daemon writes nothing when any source is unreadable: the last good `cockpit.json` stays as it was, and ages (see Freshness).
- If the scrub itself fails, the feed is withheld (fail closed): the verb prints one line on stderr, no document, and exits status 4; the daemon raises the error for the pass to report and keeps the last good file.

## Freshness

The file is the thing that goes stale. Its `produced_at` is the freshness clock: a reader treats a file whose `produced_at` is more than 180 seconds old as stale (the daemon rewrites at least every 60 seconds, so a dead daemon shows within three minutes). The verb always stamps `produced_at` with the moment it runs, so its own time proves nothing about the daemon: read the file to judge freshness.

## Fields

| level | keys |
| --- | --- |
| document | schema_version, generation, produced_at, sources, cards |
| sources | decisions, registry, reports |

| card | fields |
| --- | --- |
| decisions | id, etag, since, title, repo, agent, kind |
| held_prs | id, etag, repo, number, title, holds, reason, since |
| lanes | id, etag, state, since, last_seen |
| blocked | id, etag, blocked_since, blocked_on, reason |
| heads_up | id, etag, repo, number, note, sender, since |

A held PR's `reason` is the item's `rationale`, else its `note`, else its `title`: agent prose, scrubbed and capped at 200 characters.

Left out because no source exists without a GitHub call: pull request state, check results and review state.

## What the scrub promises, and what it does not

The scrub is best effort: a denylist applied to every string. It replaces URLs that carry a login, database URLs, email addresses, IP:port pairs, `name=value` pairs for password, secret, token, key and login names, Authorization and Bearer values, credential headers, PEM header lines, common vendor key shapes, long base64, hex and token-like strings, and file paths, and it drops diff lines. It cannot know every secret.

Item title text is outside the scrub's promise. A title is free text that a person or an agent wrote, and anything can be in it. Whether the person's assistant may read titles at all is Q3, raised to the person, not built.

## Who can read it

The person who owns the fleet, and the assistant that person runs for themselves.

## Questions

- Q1: May fleet data leave the fleet at all, and by file (a read-only mount, recommended) or by post (a new narrow credential kind on the reader's side)? This is raised, not built.
- Q2: May anyone beyond the person and that person's own assistant read the fleet cards? Answered by the Chief of Staff: no. Nothing widens it.
- Q3: May the person's assistant read item texts (titles), or only counts and ids? This is raised, not built.
- Q4: Fleet history stays in the reader's append-only log forever: acceptable, or a bounded replaced cache? This is raised, not built.
