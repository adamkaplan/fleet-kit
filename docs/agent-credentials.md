# Agent credentials for GitHub: inventory, design and migration

Status: the per-repository App installation token in the `gh` and `git` shims is built and merged (#616); the owner's steps that put it to use are not done (#568, under the least-privilege law #540). This document changes no shim, configuration or credential. Clause (4) of the ask, the live proof, is not done: it runs after the owner migrates and is not part of this document.

**The intent.** An agent's GitHub credential for a repository cannot change that repository's settings, rulesets, branch protection or collaborators, nor merge around the ruleset. Only the owner's own login can.

## Scope

In scope: the organisation's repositories where the existing GitHub App ("the App") is installed.

Out of scope and unchanged: The fleet-kit repository itself, and any repository the App is not installed on. Their local agents keep today's login, with the repository ruleset as the server-side guard. No second App and no machine user is created for them.

Cloud workers serve the organisation's repositories only; no task, test or demo of the tooling that runs the workers runs in a cloud worker.

Names below are neutral (`acme-org/widget-api`, `acme-bot`, used only to describe the rejected Option B). No token, key, account or keyring entry of a real installation appears in this file, and a test (`TestAgentCredentialsDoc` in `bin/test-switchboard`) scans it for token-like strings.

## How the scopes were read

- Configuration and code were read: `shims/gh`, `shims/git`, `docs/switchboard.md` (the `gh_users` section), `cloud/launcher/token_mint.py`. The App token mint (#450) and its exact-permission check (#545, merged) are in the launcher (`cloud/launcher/app_token.py`).
- For the stored tokens, `gh auth status` was run with the token masked. It prints scope names only.
- Nothing was written to GitHub. No admin call was tried. What a scope implies is taken from GitHub's documentation: a classic token's `repo` scope grants full control of every repository the user can access, so it can do whatever the user's role on that repository allows, including settings and rulesets where the user is an admin ([scopes for OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)). A fine-grained token or an App installation token is limited to the permissions it was given, and changing settings, rulesets or collaborators needs the `Administration` repository permission ([permissions required for fine-grained tokens](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)).
- Not read: what the cloud token endpoint is configured to grant. That lives on the endpoint's side. The launcher's own check, which refuses any answer that carries other permissions, is in force in the launcher (#545, merged).

## Inventory of credential paths

| # | Path | Where the token comes from | Real scope | Can change repository settings? |
|---|------|----------------------------|------------|---------------------------------|
| 1 | `gh` shim account map (`gh_users`) | `gh auth token --user <account>` at the moment of use, put only in the child's `GH_TOKEN` | Whatever the mapped account's stored token has (row 4). The shim picks the account; it does not narrow the token | Yes, wherever that account is an admin of the repository |
| 2 | `git` shim credential route | No token of its own. Network verbs (`push`, `fetch`, `pull`, `clone`, `ls-remote`) are sent through `gh auth git-credential`, which is row 1 | Same as row 1 | Git itself cannot call the settings API, but the token it is given is the row 1 token: yes in the sense that matters |
| 3 | Token in the shell environment (`GH_TOKEN`, `GITHUB_TOKEN` and the two enterprise names) | Inherited from whoever started the pane | On the machine read: a broad classic token (repository, workflow and organisation administration scopes). The shim replaces it for a repository with a mapping; reads and unmapped repositories fall back to it | Yes (admin of repositories it owns, and organisation administration) |
| 4 | Keyring logins of `gh` | `gh auth login`, stored by the operating system keyring | Broad classic tokens: the owner-account login includes organisation administration; a login for another account has repository access without it | Yes for repositories where that account is admin. The other-account login must stay unused for settings by policy, not by its scope |
| 5 | Cloud token mint (#450; the permission check is in the launcher: #545, merged) | An App installation token for one repository, one hour at most, minted inside the pod from the pod's federated identity | Exactly `contents: write`, `pull_requests: write`, `issues: read`, `metadata: read` (the mint asks for these and checks the answer). The mint refuses an answer with an extra key, a higher level or a missing key. The worker can read issues and their comments of its repository and cannot write issues, labels, merges or settings | No. There is no `Administration` permission. A token a cloud worker holds cannot reach settings |
| 6 | The switchboard's own calls (daemon, sweeps, intent reads) | The same per-account tokens as row 1, the real `gh` run by its path (never through the shim, no marker) | Same as row 4 | Yes in scope, but it is the daemon's own process, not an agent's shell |

Reading the table:

- Five of the six paths (rows 1, 2, 3, 4 and 6) end in a classic `repo` token. A local agent therefore holds, in effect, the owner's admin power over every repository the owner administers.
- The shim's own text says it: the fail-closed rule is "a guardrail, not a boundary: the boundary is which account's token a pane can reach". Choosing the account does not narrow it.
- Only the cloud path (row 5) is narrow by construction, and it is the model the options below copy.
- A token reachable from the shell can also be asked for by name. Whether an agent shell can print it through `gh auth token` was not tested here.
- So the real boundary is which tokens an agent shell can reach. Option C's limit holds only if the owner's broad login is out of the agents' reach for in-scope repositories (migration step 5). That is unverified, and clause (4)'s live proof is what verifies it.

## Design: three options

Option C is recommended; A and B are compared and rejected. All three keep the owner's own login as the only credential that can change settings, rulesets, branch protection and collaborators. Repositories outside the scope above are not touched by any option: no step below edits their entry.

The permission set for an agent on a repository: `contents: write`, `pull_requests: write`, `issues: write`, `checks: read`, `metadata: read`, and never `Administration`. The cloud mint today grants only `contents`, `pull_requests`, `issues` (read) and `metadata`; the owner adds `issues: write` and `checks: read` to the App.

### Option A: a fine-grained token per repository, no Administration (rejected)

One fine-grained personal access token per repository, created by the owner.

| Criterion | Assessment |
|-----------|------------|
| Cost | No money and no new account, but one token per repository by hand. `gh` stores one login per user name, so a token under the owner's name would replace the broad login in the keyring |
| What breaks | Calls that needed broader permissions return 403. Every agent still acts under the owner's name, so the audit trail does not separate agents from the owner |
| Rotation | At most one year, manual, per repository. An expired token fails closed (403) |
| Who merges | The owner's login merges and clears holds. Because the agent acts under the owner's name, GitHub refuses it as its own approver, which hides a real gap |

Rejected because agents keep acting as the owner and every token is a manual, per-repository chore.

### Option B: a separate machine user with write, not admin (rejected)

One machine user per owner account, added as a Write collaborator, with one token in the keyring.

| Criterion | Assessment |
|-----------|------------|
| Cost | An extra account and a seat on private repositories, plus an invite per repository. No shim change |
| What breaks | Little: a Write collaborator cannot change settings, rulesets or collaborators. Anything that relied on admin moves to the owner |
| Rotation | The user's token has its own expiry (up to a year when fine-grained), rotated by hand with one `gh auth login` per user |
| Who merges | The owner's login merges and clears holds. A Write-role user can still approve and merge through the ruleset, so "cannot merge around the ruleset" holds only partly: the independent review here is a fleet gate, not a GitHub rule |

Rejected because it adds an account to run and secure, its token lives as long as a year, and a Write user can merge through a ruleset that requires no approvals.

### Option C: the App installation token for local agents (recommended)

The same kind of GitHub App as the cloud path, installed on the organisation's repositories, minted locally by the shims as a one-hour installation token.

| Criterion | Assessment |
|-----------|------------|
| Cost | No new account and no seat; the App already exists. The shim code is built and merged (see "What C needs"). The App's private key must live on the machine, a stronger secret than a token |
| What breaks | Until the owner maps a repository and supplies the key, nothing changes for it. After that, the failure modes are a mint outage, an unreadable key and clock skew, all of which fail closed. A repository with no installation is refused |
| Rotation | Best of the three: tokens live an hour and need no manual step. The owner rotates the private key on a schedule in the App's settings |
| Who merges | The owner's login merges and clears holds. The App's pull requests carry a bot identity, so the owner can approve them. The App has no bypass of the ruleset and no Administration permission |

## Recommendation

Recommended option: Option C (the App installation token), for the organisation's repositories where the App is installed.

Why:

- GitHub enforces the limit: an installation token without `Administration` cannot change settings, rulesets, branch protection or collaborators, and it is not the owner's identity.
- The token lives an hour and is bound to one repository, with no manual rotation and no new account.
- It reuses the App and the permission model the cloud path already runs on.

### What is built, and what C still needs on top of the App

**What is built.** The shim mint is built and merged (#616): the `gh` and `git` shims mint an App installation token for one repository, short-lived (one hour at most), and fail closed (a mint failure, an unreadable key or a repository with no installation refuses the call and never falls back to another login). The token for a named repository is used only for the Chief of Staff's recorded role (`whoami` carries the role; a missing role counts as not), and a worker that names another mapped repository is refused. It acts only where the owner has mapped a repository; the live proof of it is not done.

What still has to exist for C to protect anything, all of it the owner's:

- The App's private key, kept only where the shim process reads it, never in an agent's shell or environment. A file mode is a convenience, not a guarantee, against an agent running as the same user; the real guard is that the key sits outside the agent's reach, for example under a different user or behind a vault call.
- The repository-to-installation map beside `gh_users`, with the installation ids (kept in the owner's configuration, never in this repository).
- App permissions: `contents: write`, `pull_requests: write`, `issues: write`, `checks: read`, `metadata: read`, and no Administration. The owner adds `issues: write` and `checks: read`.
- The migration steps that take the owner's broad login out of the agents' reach (see "Migration").
- The owner's own login keeps merging and clearing holds.

### Out of scope, unchanged

The fleet-kit repository itself and any repository without the App keep today's local login, with the ruleset as the server-side guard. No second App and no machine user is created for them, and the inventory rows 1 to 4 and 6 stay as they are for them.

Who merges after the change, in scope:

- Agents push branches, open pull requests, label and comment with the App token.
- The owner's login approves, merges and clears holds, and is the only credential that can change settings, rulesets, branch protection or collaborators.

## Migration

Every step that creates or changes a credential is the owner's (see "The owner's own steps"). Agents run none of them and run no command that creates a credential. Steps apply only to the organisation's repositories where the App is installed.

1. The owner confirms the App's installation on each in-scope repository.
2. The shim work is merged (#616): the mint, cache and fail-closed behaviour above. Not done: the change to the cloud mint's expected permissions, below. The cloud mint's exact-permission check is merged and live (#545, `EXPECTED_PERMISSIONS` in `cloud/launcher/app_token.py`): it refuses every cloud token whose permissions differ from its expected map, so that map must be changed to the new set, a top-tier credential change with its own review and the owner's clear, before the App is widened or together with it.
3. Only after that change is merged, the owner adds `issues: write` and `checks: read` to the App's permissions and confirms there is no Administration permission. Widening the App earlier makes the cloud mint refuse every cloud token today, because the permissions no longer match its exact check. If that happens, narrow the App back to its previous permissions.
4. The owner stores the App key where only the shim process reads it, and adds the repository-to-installation map beside `gh_users` in `~/.config/fleet-switchboard/config.json`.
5. The owner removes the broad token from the agents' environment for in-scope repositories, so there is nothing broader to fall back on.
6. An agent, with the owner present, runs the live proof of clause (4): an admin call is refused (403 or 404) and a branch push, pull request, label, comment and a merge through the ruleset still work. The same proof checks that the fleet-kit repository itself is unchanged.

## Cloud worker: reading issues

The cloud mint asks for, and accepts, exactly `contents: write`, `pull_requests: write`, `issues: read`, `metadata: read`. The worker can read issues and their comments of its own repository and cannot write issues, labels, merges or settings. The owner step is one numbered step with two actions, done only after the change is merged: add `issues: read` to the App's permissions, then accept it on each installation. Until then every cloud launch is refused before any pod exists, with a line that names `issues` and this step. This is the cloud token's own set and a different credential from the Mac agent set above.

## Rollback

If the App token breaks agent work on an in-scope repository, return to the current login:

1. Remove that repository's entry from the installation map, or restore the previous `gh_users` entry from the copy kept before step 4. The shims read it on every call, so there is no restart.
2. Restore the inherited token for the agent's shell, if step 5 removed it.
3. The previous keyring login was never deleted by this migration, so it is still there.
4. If the cause was the key, the owner rotates it in the App's settings. If widening the App made cloud tokens refused, narrow the App back to its previous permissions (the cloud mint now needs `contents: write`, `pull_requests: write`, `issues: read`, `metadata: read`; revert the change that added `issues: read` first, then remove it from the App).

Rollback returns to today's behaviour, including admin reach for agents on that repository, so record why and treat it as temporary.

## The owner's own steps

These are the only credential-creating or credential-changing steps. No command here belongs in a script an agent runs.

1. Only after the change to the cloud mint's expected permission map (see Migration, steps 2 and 3), in the App's settings set the permissions to `contents: write`, `pull_requests: write`, `issues: write`, `checks: read`, `metadata: read`, and no Administration.
2. Confirm the App is installed on each in-scope repository (for example `acme-org/widget-api` and `acme-org/widget-web`), and on nothing else.
3. Generate or keep the App's private key; store it outside the agent's reach (a different user, or a vault call the shim makes), never in a file an agent's shell reads, an issue or a message. A file mode alone is not a boundary against the same user.
4. Review each in-scope ruleset: its required checks and approvals are what agents are held to, and the App is not in a bypass list.
5. Add the repository-to-installation map (with the installation ids) to the configuration.
6. Remove the broad inherited token from the agents' shells for in-scope repositories.
7. Put the key's rotation date in a calendar; at rotation, generate a new key in the App's settings and replace the stored one.
8. Leave the fleet-kit repository itself and any repository without the App alone.
