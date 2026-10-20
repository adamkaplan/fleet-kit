# AGENTS.md: working in this repository

fleet-kit is a **public, reusable project**: other people install it and run it for their own fleets. Everything in the
repository, and everything written about it on GitHub (issues, comments, pull requests, reviews), must make sense to a
stranger and hold nothing private.

## Keep it public-safe

- No private product names, repositories, organisations, internal URLs, people, real session ids or private data. Not in
  code, tests, fixtures, scenarios, docs, skills, agent definitions, commit messages or PR text.
- Examples use neutral names: `acme-org/widget-service`, `widget-api`, `acme/api#2`, `ses_example...`, `/home/user`.
- Skills, agents and docs never name the owner. Say "the owner", "the principal" or "the person". A personal name can only
  appear where something functionally needs it (the `LICENSE` copyright line, the repository's own URL), and a name that
  a feature must look for (for example the sweep's `person_names`) is configuration, with a neutral default.
- A fixture is synthetic. When a real case taught something, rewrite it with neutral names and the same shape (the same
  lengths, if a test depends on them).

## Check before you push

`bin/check-public-names` fails when a private name is in a tracked file. The names are NOT in the repo: keep your own list
in `~/.config/fleet-switchboard/private-names.txt` (or point `FLEET_PRIVATE_NAMES_FILE` at one), one entry per line:
a regular expression (case-insensitive), `case:<regex>` (case-sensitive), `ignore:<glob>` (skip a path). A hit prints
`path:line: term #N` and never the term. With no list it says it was skipped. CI runs it when the repository secret
`PRIVATE_NAMES` is set (it is not available to pull requests from forks).

## Tests

Run the suites before you open a PR: `bin/test-switchboard`, `bin/test-fleet-doctor`, `bin/test-fleet-doctor-config`,
`bin/test-fleet-doctor-heartbeat`, `bin/test-heartbeat`, `bin/test-health-records`, `bin/test-gh-hold`, `bin/test-workflow-split` (each prints `RESULT: PASS`), `node --test plugins/fleet-hooks/index.test.mjs` and `node --test plugins/fleet-decisions-tui/health.test.mjs`.
