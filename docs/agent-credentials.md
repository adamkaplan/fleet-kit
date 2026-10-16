# Agent credentials for GitHub: inventory, design and migration

Status: design for decision (#568, under the least-privilege law #540). Nothing here changes a shim, a configuration or a credential. Clause (4) of the ask, the live proof, runs after the owner migrates and is not part of this document.

**The intent.** An agent's GitHub credential for a repository cannot change that repository's settings, rulesets, branch protection or collaborators, nor merge around the ruleset. Only the owner's own login can.

Names below are neutral (`acme-org/widget-api`, `agent-bot`). No token, key, account or keyring entry of a real installation appears in this file, and a test (`bin/test-agent-credentials-doc`) scans it for token-like strings.

## How the scopes were read

- Configuration and code were read: `shims/gh`, `shims/git`, `docs/switchboard.md` (the `gh_users` section), `cloud/launcher/token_mint.py` and `cloud/launcher/app_token.py`.
- For the stored tokens, `gh auth status` was run with the token masked. It prints scope names only.
- Nothing was written to GitHub. No admin call was tried. What a scope implies is taken from GitHub's documentation: a classic token's `repo` scope grants full control of every repository the user can access, so it can do whatever the user's role on that repository allows, including settings and rulesets where the user is an admin ([scopes for OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)). A fine-grained token or an App installation token is limited to the permissions it was given, and changing settings, rulesets or collaborators needs the `Administration` repository permission ([permissions required for fine-grained tokens](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)).
- Not read: what the cloud token endpoint is configured to grant. That lives on the endpoint's side. The launcher's own check, which refuses any answer that carries other permissions, is read from code.

## Inventory of credential paths

| # | Path | Where the token comes from | Real scope | Can change repository settings? |
|---|------|----------------------------|------------|---------------------------------|
| 1 | `gh` shim account map (`gh_users`) | `gh auth token --user <account>` at the moment of use, put only in the child's `GH_TOKEN` | Whatever the mapped account's stored token has (row 4). The shim picks the account; it does not narrow the token | Yes, wherever that account is an admin of the repository |
| 2 | `git` shim credential route | No token of its own. Network verbs (`push`, `fetch`, `pull`, `clone`, `ls-remote`) are sent through `gh auth git-credential`, which is row 1 | Same as row 1 | Git itself cannot call the settings API, but the token it is given is the row 1 token: yes in the sense that matters |
| 3 | Token in the shell environment (`GH_TOKEN`, `GITHUB_TOKEN` and the two enterprise names) | Inherited from whoever started the pane | On the machine read: a classic token with `repo`, `workflow`, `gist` and `admin:org`. The shim replaces it for a repository with a mapping; reads and unmapped repositories fall back to it | Yes (admin of repositories it owns; organisation admin through `admin:org`) |
| 4 | Keyring logins of `gh` | `gh auth login`, stored by the operating system keyring | Classic tokens. The owner-account login: `repo`, `workflow`, `gist`, `admin:org`. A login for another account: `repo`, `workflow`, `gist`, `read:org` and a key-management scope | Yes for repositories where that account is admin. The other-account login must stay unused for settings by policy, not by its scope |
| 5 | Cloud token mint (#450, #545) | An App installation token for one repository, one hour at most, minted inside the pod from the pod's federated identity | Exactly `contents: write`, `pull_requests: write`, `metadata: read`. The mint refuses an answer with an extra key, a higher level or a missing key | No. There is no `Administration` permission. A token a cloud worker holds cannot reach settings |
| 6 | The switchboard's own calls (daemon, sweeps, intent reads) | The same per-account tokens as row 1, marked `FLEET_SWITCHBOARD_OWN_GH` | Same as row 4 | Yes in scope, but it is the daemon's own process, not an agent's shell |

Reading the table:

- Four of the six paths end in a classic `repo` token. A local agent therefore holds, in effect, the owner's admin power over every repository the owner administers.
- The shim's own text says it: the fail-closed rule is "a guardrail, not a boundary: the boundary is which account's token a pane can reach". Choosing the account does not narrow it.
- Only the cloud path (row 5) is narrow by construction, and it is the model the options below copy.
- A token reachable from the shell can also be asked for by name. Whether an agent shell can print it through `gh auth token` was not tested here.

## Design: three options

All three keep the owner's own login as the only credential that can change settings, rulesets, branch protection and collaborators. Repositories on another account are not touched by any option: they keep their current read-only use and no step below edits their entry.

The common permission set for an agent is the cloud mint's: `contents: write`, `pull_requests: write`, `metadata: read`. Labels and comments on issues also need `issues: write`; the owner decides whether to add it, and none of these carry `Administration`.

### Option A: a fine-grained token per repository, no Administration

One fine-grained personal access token per repository, created by the owner, with the permissions above.

| Criterion | Assessment |
|-----------|------------|
| Cost | No money and no new account. One token per repository to create by hand, and a fine-grained token cannot span two owners. `gh` stores one login per user name, so a token for the owner's name would replace the broad login in the keyring: the owner would keep the broad one outside the agents' reach |
| What breaks | Anything the broad token did that now needs another permission (workflow files, organisation reads). Fewer actions than today, so some calls return 403 and need a decision. Every agent still acts under the owner's name, so the audit trail does not separate agents from the owner |
| Rotation | At most one year, set by the owner at creation; manual, per repository, with a calendar reminder. An expired token fails closed (403) |
| Who merges | The owner's own login merges and clears holds. An agent token can merge only a pull request the ruleset already allows. Because the agent acts under the owner's name, GitHub refuses it as its own approver, which hides a real gap |

### Option B: a separate machine user with write, not admin

One extra GitHub account (`agent-bot`), added to each repository as a collaborator with the Write role, with its own token in the keyring under its own name.

| Criterion | Assessment |
|-----------|------------|
| Cost | One extra account, no money for public repositories. A seat on private organisation repositories. A one-time setup per repository (invite and accept). It fits the existing `gh_users` map as a new account name, so no shim code changes |
| What breaks | Little. A Write collaborator cannot change settings, rulesets, branch protection or collaborators, which is the intent, and everything else agents do (push, pull request, label, comment) is allowed. Anything that relied on admin power breaks and moves to the owner. Pull requests now show a different author, which is useful: the owner can approve what an agent wrote |
| Rotation | The machine user's token has its own expiry (fine-grained, up to a year) or a classic token with no expiry, which the owner should avoid. Rotating means one new token and one `gh auth login` in the keyring; revoking the user's access is one step per repository, or deleting the account |
| Who merges | The owner's own login approves and merges, or clears holds. The machine user's pull requests can be approved by the owner without the self-approval problem, so the ruleset's review rule keeps working. The machine user itself does not hold bypass |

### Option C: the App installation token for local agents too

Use the same kind of GitHub App as the cloud path, installed on each repository with the three permissions, and mint a one-hour installation token for the local shim to use.

| Criterion | Assessment |
|-----------|------------|
| Cost | One App, installed per repository, no seat. Needs code: the `gh` shim and the `git` shim would mint a token instead of reading the keyring. That change is top tier and is a separate work item. The App's private key must live on the machine, which is a stronger secret than a token |
| What breaks | Until the shim work lands, everything. After it, the failure modes are a mint outage, a key that cannot be read, and clock skew. A call with no mapping stays refused as today |
| Rotation | Best of the three: tokens live an hour and need no manual step. The private key is rotated by the owner on a schedule, and the owner can generate a new one in the App's settings |
| Who merges | The owner's own login merges and clears holds. The App's pull requests carry a bot identity, so the owner can approve them. The App has no bypass of the ruleset |

## Recommendation

Recommended option: Option B (a separate machine user with write, not admin).

Why:

- It is the only option that needs no code change and still separates the agent's identity from the owner's, so the review rule keeps working and the audit trail says which actor did what.
- It meets the intent with GitHub enforcing the limit, by role, rather than a shim that tries to guess.
- It fits the existing account map (`gh_users`): the machine user is one more account name.
- Option C is a better end state, with an hour of lifetime and no manual rotation, but it needs a top-tier shim change first. Take it as the follow-up once B is in place. Option A leaves agents acting under the owner's name and a manual token per repository.

Who merges with which credential after the change:

- Agents push branches, open pull requests, add labels and comments with the machine user's token.
- The owner's own login approves, merges and clears holds, and is the only credential that can change settings, rulesets, branch protection or collaborators.
- Repositories on another account are not part of this change.

## Migration

Steps are in order. Every step that creates or changes a credential is the owner's (see "The owner's own steps"). Agents run none of them and run no command that creates a credential.

1. The owner creates the machine user and a token for it with `contents: write`, `pull_requests: write`, `metadata: read` (and `issues: write` if wanted).
2. The owner invites the machine user to each repository as a collaborator with the Write role, and the user accepts.
3. The owner adds the machine user to the keyring with `gh auth login` in a shell the agents do not share.
4. The owner adds the account name to `gh_users` for each repository, or for `owner/*`, in `~/.config/fleet-switchboard/config.json`. The `git` shim follows the same map.
5. The owner removes the broad token from the agents' environment: the inherited `GH_TOKEN` and `GITHUB_TOKEN` of the pane, so the shim has nothing broader to fall back on for reads.
6. An agent, with the owner present, runs the live proof of clause (4): an admin call is refused (403 or 404) and a branch push, pull request, label, comment and a merge through the ruleset still work.

## Rollback

If the new credential breaks agent work, return to the current one, in this order:

1. Restore the previous `gh_users` entry in the configuration file (keep a copy before step 4). The shims read it on every call, so there is no restart.
2. Restore the inherited token for the agent's shell, if step 5 removed it.
3. The previous keyring login was never deleted by this migration, so it is still there.
4. Remove the machine user from the repository (the owner), and revoke its token, if the cause was the account and not the map.

Rollback returns to today's behaviour, including admin reach for agents, so record why it was needed and treat it as temporary.

## The owner's own steps

These are the only credential-creating or credential-changing steps. No command here belongs in a script an agent runs.

1. Create the account for the agent (once), with an email the owner controls, and turn on two-factor authentication.
2. In that account's settings, create a fine-grained token limited to the chosen repositories, with the three permissions above and an expiry of no more than a year. Store it only in the keyring, never in a file, an issue or a message.
3. In each repository's settings, add the account as a collaborator with the Write role. Never Maintain or Admin.
4. Check each ruleset: the review rule is on, and the machine user is not in a bypass list.
5. Run `gh auth login` for the machine user in the owner's own terminal.
6. Edit the account map and remove the inherited broad token from the agents' shells.
7. Put the token's expiry date in a calendar. At expiry, repeat step 2 and step 5.
8. Leave repositories on the other account alone.
