# cloud-worker (slice B)

Launcher, reaper, end hook and token mint for cloud workers, built to `docs/cloud-worker-interface.md`.

- `cloud-worker create|stop|extend|delete|status|reap` (`cloud_worker.py`): reaches the cloud only through `kubectl`
  (`CW_KUBECTL`, `CW_CLOUD_NAMESPACE`); writes one audit line per action to `CW_CLOUD_AUDIT`
  (default `~/.local/state/cw-cloud/audit.jsonl`, mode 0600). `reap` is one pass (or `--loop SECONDS`, never slower
  than 60); run it from a cron-like scheduler. It stops a pod past its hard deadline, or idle (not busy, last
  activity older than the idle time, read by `kubectl exec <pod> -- cw-worker-activity`; an unreadable answer is not a reason to stop).
- `end_hook.py`, installed in the image as `cw-end-hook`: WIP commit, push to `<worker>/wip`, one JSON line.
- `token_mint.py`, installed as `cw-token-mint`: mints a token at start and on demand, refuses more than 3600 s,
  writes it to a tmpfs file only.
- Tests: `bin/test-cloud-launcher` (fake `kubectl`, fake clock, real `git`, loopback HTTP; no cloud).

## What the image (slice A) must provide
`cw-end-hook` and `cw-worker-activity` (prints `{"busy": bool, "last_activity": "<ISO UTC>"}` from the worker's
own session state, never from inbound requests), `cw-token-mint` run at start, a readiness probe on `:4096/health`.

## Cluster context
Every `kubectl` call passes `--context "$CW_KUBE_CONTEXT"`, and `--kubeconfig "$CW_KUBECONFIG"` when that is set. If
`CW_KUBE_CONTEXT` is unset or empty, every command refuses to run (exit 2) and calls nothing: the active context is never
used, because it moves when you work in other clusters.

## Service-account login, per-run secrets, image pull secret
With `CW_CLOUD_SERVER` and `CW_CLOUD_TOKEN_FILE` set (and optionally `CW_CLOUD_CA_FILE`) the launcher logs in as the
`cw-operator` account of `cloud/k8s/operator-rbac.yaml` (one namespaced Role: pods, logs, exec, port-forward, secrets; no
wildcard, nothing cluster-wide). Every `kubectl` call carries a generated `--kubeconfig` (mode 0600, private temp dir, removed at
exit) whose only credential is the token file's path. It never uses `--token` or an administrator kubeconfig, and refuses
if `CW_KUBECONFIG` or `CW_KUBE_CONTEXT` is also set. A missing or unreadable token file exits 3 (`token unavailable`) with no call.

`create --pull-secret NAME` sets `imagePullSecrets`. `create --run-secret-fd NAME=N` (repeatable) reads each value once from
descriptor `N`, pipes it to `kubectl` on stdin into one Secret `<pod>-run`, and mounts it read-only at `/run/cw/NAME` (0400).
The Secret goes with the pod at `stop`, `delete` and a failed or interrupted `create`; a pod kept because the end hook failed keeps it.

Accepted scope of the role: `cw-operator` can get and list every secret in the workers namespace (a Role cannot limit `list` by label), so the namespace must hold only worker pods and their per-pod secrets; that is enforced in the deployment repository, not here.

Everything that renders `kubectl` output (errors, the end hook's text, audit reasons) passes through one `scrub()`: this run's secret values and any 8+ character piece of one, token-shaped strings and long base64/hex runs are removed, then the text is cut. A failed `create`, `stop` or `delete` that leaves a per-run Secret says so once (audit and stderr) with its name and the exact `kubectl ... delete secret NAME` command, never a value.

## The run record and the report (slice 3)
The record is the launcher's `stop`'s, so every end gets one: `run-task` (normal end, failure, interrupt), the switchboard's `retire`
and a person running `cloud-worker stop`. Before the end hook and the delete, `stop` reads the pod's server password and run secrets from
the pod's own mounts, opens its own loopback port-forward, reads the session's messages and writes `$CW_RUN_RECORD_DIR/<run id>/`
(`--record-dir` overrides; default `runs/` beside the audit file; directory 0700, files 0600; a root that is a symbolic link is refused):
`transcript.jsonl` (each message one JSON line, every string value and key through the one scrub, so a line still parses; at most 5 MiB,
cut with a `TRANSCRIPT CUT` marker line; `CW_RECORD_MAX_BYTES` lowers it; the whole record phase has one deadline, `CW_RECORD_SECONDS`, default 90 s, maximum 240 s: past it the record says `transcript: unavailable (record timed out)` and the stop still deletes), `tally.json` (tool calls by tool name, turns; no content) and
`run.json` (worker, repository, start, end, exit reason, head sha when known; `transcript: unavailable` plus a reason when the server could
not be read, or its answer was over 8 MiB or nested deeper than 100 levels: checked before parsing). A failing record never holds the stop
back and a failing stop never deletes it; `--no-record` skips it. `stop` prints `run record: DIR` once and the audit reason names it. Once the end hook answers, `run.json` is rewritten atomically with `end_hook` (pushed, wip_branch, skipped, note, error); this holds for the reaper's stops too, whose pass output carries the same fields. A `reap` process writes one line `reaper exiting: WHY` when it ends.
Full contract: `docs/cloud-worker-interface.md`.

**What the scrub covers (and not).** Removed: a secret's whole value, base64, base64url, hex, percent-encoding and JSON escapes of the whole
value, and any piece of 8+ characters of those (split over fields, lines or 8-character pieces); token shapes; keys as well as values.
Not covered: per-character escapes (`\uXXXX` or `%XX` one character at a time) and pieces under 8 characters. Everything is reduced to
printable ASCII, so non-ASCII text in a transcript or report becomes `?`.

**Retention.** Records stay until you remove them. `cloud-worker prune-records [--days N]` (default 30, or `CW_RECORD_KEEP_DAYS`) removes
only this tool's per-run directories older than that; run it from cron for a rolling window.

The worker writes its final report as ONE line to `/home/worker/report` (the driver adds this to the brief). `cloud-worker report NAME`
returns it through `kubectl exec`: scrubbed, one line, at most 2000 printable ASCII characters (control characters, escapes, bidi marks and
non-ASCII become `?`), audited, with no switchboard command inside the pod. It refuses a symbolic link or any file that is not a regular
file (exit 3, `report refused`), and exits 3 (`no report yet`) when the file is missing or empty.

## Create is interrupt-safe
Ctrl-C, SIGTERM or any exception inside `create` deletes the per-run Secret and the pod it made and writes a
`create.interrupted` (or `create.failed`) audit line naming what was cleaned. If a delete fails, stderr and the audit line
say which pod or secret REMAINS, and the exit is non-zero (130 for a signal).

## Driving a cloud worker from the switchboard (`launch --cloud`, `send`, `read`, `status`, `retire`)

The switchboard (`bin/cloud_worker.py`) calls this launcher; the settings below are read from the operator's environment.

| Setting | Meaning |
|---|---|
| `CW_ALLOWED_REPOS` | the repositories (`OWNER/NAME`, separated by commas; matched exactly, ASCII case-insensitively; empty means none; ONE malformed entry refuses the whole list, as the launcher's own `create` does; a malformed `CW_TOOLING_REPO` also refuses; checked before anything is created; `CW_TOOLING_REPO` is refused even if listed |
| `CW_CLOUD_SERVER` (`CW_CLOUD_CA_FILE`) | the cluster API address |
| `CW_CLUSTER_TOKEN_CMD` | a command that prints a cluster token; when unset the default below is used |
| `CW_VAULT_SUBSCRIPTION`, `CW_VAULT_NAME` | the vault that holds the cluster administrator credential (secret `CW_CLUSTER_ADMIN_SECRET`, default `cw-cluster-admin-kubeconfig`; or a command in `CW_CLUSTER_ADMIN_CMD`) |
| `CW_CLUSTER_TOKEN_DURATION` | the token's requested life, default `20m`, at most `60m` (kubectl's own floor is 10 minutes) |
| `CW_GIT_TOKEN_CMD` | prints the one-hour git token for the repository (default: `app_token.py OWNER/NAME`, same settings as `run_task.py`) |
| `CW_MODEL_AUTH_FILE` | the OpenCode auth file; only its `github-copilot` entry is used |
| `CW_PULL_SECRET` | optional: the image pull secret name passed to `create` |

- **A cluster token per use.** Every command that needs the cluster mints its own token at that moment: the administrator
  kubeconfig is read from the vault by command and given to `kubectl create token cw-operator` on a pipe (never a file or
  an argument). The token is checked (a JWT's `exp` and `iat` are read; a lifetime over 60 minutes is refused; for a token
  that is not a JWT the requested duration is trusted: a custom `CW_CLUSTER_TOKEN_CMD` that prints a non-JWT token is trusted at its
  requested life, so "never over 60 minutes" holds only when the token's `exp` can be read). It is written once to a 0600 file
  (`O_EXCL`) in a private 0700 directory `$TMPDIR/cwtok-<uid>/cmd-<pid>-<random>/` that the launcher's token-file login needs. The
  file lives for the WHOLE command (for `launch` that includes the tunnel phase, where the session and brief are sent): at most 20
  minutes of token (the requested life), not only until the last `kubectl` call. The directory is removed when the command ends,
  however it ends (normal, failure, SIGINT, SIGTERM, SIGHUP, SIGQUIT). `TMPDIR` of the caller's environment chooses where `cwtok-<uid>`
  goes; it is refused unless it is the user's own, mode 0700 and not a link, but an agent that can set `TMPDIR` can choose the place.
  A command killed with SIGKILL leaves its directory AND its tunnel until the next command: that command removes any directory of
  a dead process or older than an hour, stops the `kubectl port-forward` recorded in it (only while the pid still has the recorded
  start time and is a `kubectl port-forward` using that directory's kubeconfig; a reused pid is never signalled) and says so once.
  No token is cached or kept between commands. Nothing else (no other context or credential) is ever tried.
  A mint happens once per command; `status` mints one only when a cloud worker needs a probe (none for a broken record or a worker
  past its hard deadline), so a poller of `status` causes one vault read and one mint per call.
- `CW_CLUSTER_TOKEN_CMD` and `CW_CLUSTER_ADMIN_CMD` run through `sh -c` from the caller's environment, like `TMPDIR`: they are guardrails of the operator's own account, not a boundary against an agent that can set the environment.
- **A missing vault is a refusal.** When the vault is unreachable or the credential unreadable, the command exits 2 with a
  message naming the vault pre-check (the operator runs the platform team's firewall check first) and makes no cluster call.
- **`launch --cloud`** creates the session in `/work` (the scratch volume; `--dir` overrides) and puts a setup preamble before the brief: clone the repository into `/work/repo`, switch to the task branch, work only there (the end hook pushes from `/work/repo`). It checks the allow-list, mints the token, reads the model entry and the git token, and hands both to
  `create` as per-run secrets through pipes (`--run-secret-fd`), then sends the session and the brief through a tunnel.
- **`send`, `read`, `status`** each open a `kubectl port-forward` to the pod for the length of the command and kill it
  (its whole process group) before returning, also on an interrupt. No tunnel is stored; the record's `url` is not used for traffic.
  What is visible while it runs: the loopback port number and the pod name are in the process's arguments (`ps`), and any local user can
  open a connection to the loopback port, but the server asks for the worker's password, which only this process sends. Nothing secret
  is in an argument or the environment (the kubeconfig holds only the path of the token file).
- **One name, one kind.** `launch --cloud NAME` is refused when a local agent has that name, and a local `launch NAME` is refused when
  a cloud record has it, so `send`, `read` and `retire` by name cannot be routed to the wrong one. A failed or cut `launch --cloud` stops
  the pod with the run id of its own `create` (`--run-id`); a launcher whose `stop` takes no `--run-id` yet is stopped by name and the audit line says so.
- **`retire`** runs the launcher's `stop` (end hook, then pod and secrets); a failing end hook keeps the pod. No token or tunnel is kept.

## Decisions (for the PR description)
**v2 server auth/TLS (the contract's OPEN item).** Per-worker random password (32 bytes, `secrets`), stored only as
a cluster Secret scoped to the pod and mounted at `/run/cw/server-password` (0600); Basic auth, as the local v2
service. The worker listens inside the pod network; a TLS-terminating proxy at `https://worker-<pod>.<domain>:4096`
presents a certificate from the platform's internal CA over a private network. The switchboard verifies it against that
CA bundle, and sends the password only to the `https` host named in the endpoint record. Rejected: a mesh with
per-pod mTLS (more moving parts for one client), and a shared password (one leak opens every worker).
The password reaches slice C once, on a file descriptor it passes with `--password-fd`; never in stdout, args or audit.

**Other choices where the contract was silent.** Deadlines live on the pod as annotations (`cw/hard-deadline`,
`cw/idle-seconds`); the reaper honours the record's `hard_deadline` read-only when `CW_CLOUD_RECORD_DIR` is set.
`extend` and `max-hard` are measured from the pod's start. A pod whose hook failed is annotated `cw/kept`;
`delete` needs `cw/hook-ran` or `--force` (exit 6). The reaper retries a kept pod each pass (the OPEN item on
exceeding the hard limit and alerting is not decided: it exits 1 from `reap` so a scheduler can alert). The spend cap
is read from `CW_CLOUD_SPEND_CAP_USD`; unset or empty means the prototype's USD 25, and zero, negative, non-numeric,
`nan`, `inf` or a cost that cannot be read refuses `create` (exit 5). The check adds this launch's estimate (hard limit times the shape's rate) to what the running pods already cost. A worker name with `_` maps to `-` plus a 4-hex
hash for the pod name. Shape rates are placeholders (`S` 0.10, `M` 0.20 USD/hour); `create` refuses a spec with a stored credential.

**Not in this slice.** Automatic detection of a node drain notice: the end hook runs on `stop`, the idle stop and the hard limit; a drain is handled today by the operator running `stop --reason "node drain"`. The `cw-worker` service account's cloud identity and what it may do belong to the image/cluster setup (slice A and the platform), not here. `bin/test-cloud-launcher` is not yet wired into CI (the suite lists are CI's own).

**Old names.** The launcher reads and writes only the neutral `cw` names (one table in `tests/test_cw_names.py`). No worker exists under an earlier name, and none is read: a pod or secret left under an old name is invisible to `status` and `reap` and must be removed by hand.

## Server-side guards: `app_token.py` and `preflight.py`

A guard is enforced where the action takes effect (the repository's rules, the token's scope), not in a wrapper.
These two programs are the server side of that. Neither writes a file; neither prints a secret.

**`app_token.py OWNER/REPO`** prints a one-hour GitHub App installation token for that one repository, and nothing
else, on stdout. It refuses (exit 1, one line on stderr naming the key, never a value) to print a token unless:

- the answer lists exactly the one repository and expires within 61 minutes, and
- its `permissions` equal exactly `contents: write`, `pull_requests: write`, `metadata: read`: no extra key, no higher
  level, none missing.

The private key travels only through a pipe to `openssl`; only `https://api.github.com` is used (`CW_GITHUB_API_BASE`
may name a loopback `http` address, for tests).

**`preflight.py OWNER/REPO [--branch NAME] [--accept-classic]`** reads, with GET only, whether the default branch
refuses direct pushes (a `pull_request` rule that requires at least one approving review, or a `merge_queue` rule, in an active ruleset that covers the branch; `evaluate` and `disabled` rulesets and rulesets not covering the branch are refused by name; ref patterns follow GitHub: `*` stops at `/`, `**` crosses it, and a pattern the program does not understand, such as a character class, covers nothing), force-pushes (`non_fast_forward`) and deletion
(`deletion`). Exit 0 prints `preflight ok: <rules>`; exit 1 names each missing rule; exit 2 means the answer could not
be read. A branch protected only by classic branch protection fails closed (a read-only token cannot see its review
requirement); `--accept-classic` is the explicit, owner-approved override and prints a warning. It overrides only that one failure (the review requirement on a branch GitHub reports as protected), never an unreadable answer, an unprotected branch, or a missing `non_fast_forward` or `deletion` rule. Redirects are never followed, and `OWNER/REPO` may not contain a `.` or `..` segment. It cannot see bypass
actors: the live refusal check on a disposable repository is the real proof.

*Organization rulesets.* An App installation token can never read an organization ruleset (GitHub asks for the
organization `Administration` permission at write; this project does not grant it). When the read of an organization
ruleset is refused with HTTP 403, that ruleset is not counted and the output says so, one line each:
`organization ruleset <id>: not readable with this token, not counted`. The repository passes on the rules the
preflight can verify. If it fails only because those rules are not counted (exit 1: `protection comes from an
organization ruleset this token cannot read; an owner approval entry is needed`), it is enabled by one entry in
`cloud/launcher/repo-approvals.json`, which ships as `{}` (nothing is enabled). The launcher and the preflight run from
the operator's main checkout, so a changed copy in a worker's branch is not the one that counts. The file is read from the
real location of the program (a symlink to it is followed, and a file beside the link is ignored): there is no setting, flag or other path for it, and an approval can arrive only through a pull request that
changes that file, which needs the owner's clear. To add one, compute the key (the repository is public, so only a hash
of the lowercased `owner/name` is stored; the hash is not a secret: it avoids listing names in clear but discloses that a
named repository is approved):

```sh
python3 -c 'import hashlib;print(hashlib.sha256(b"owner/name".lower()).hexdigest())'
```

```json
{"<that hash>": {"approved_by": "<the owner's login>", "approved_at": "2026-10-16", "reason": "<one neutral line>", "pr": "<the number of the pull request that adds this entry>"}}
```

All four fields must be non-empty one-line strings (no control or bidirectional characters); a repeated key, at any level, makes the file unreadable (exit 2). The file is read only when the sole reason for failing is the
unreadable organization rulesets (everything missing would be supplied by the rules they carry). A matching entry passes
with a line on every run: `preflight ok: protection NOT VERIFIED (organization ruleset <id> unreadable); owner-approved
by <who> on <date> (pr #<n>): <reason>`. A file or matching entry that cannot be read exits 2; an entry for another
repository is ignored. The approval never covers a rule the preflight can see is missing, an unprotected repository, or
a readable ruleset that is not enforced. Any other failed read (404, 500, a dropped connection, odd JSON, too large) of
any ruleset, and a 403 on a repository's own ruleset, still exits 2.

Residual: an agent that runs as the owner's operating-system user can edit any local file, including this installed
program. The approvals file is no weaker than the code itself; the repository's review gate and the owner's clear are
what protect both.

Settings (environment): `CW_APP_ID_CMD`, `CW_APP_KEY_CMD`, `CW_APP_INSTALLATION_CMD` (each a command printing the
value; otherwise read from the vault), `CW_VAULT_SUBSCRIPTION` and `CW_VAULT_NAME` (no defaults), `CW_OPENSSL`,
`CW_GITHUB_API_BASE` (tests), `CW_PREFLIGHT_TOKEN_CMD` (tests: replaces the mint in the preflight).

**Repository allow-list.** `create` (and the driver, `run-task`, through the same function `check_repo_allowed`) launches only for a repository the owner listed. `CW_ALLOWED_REPOS` is a comma-separated list of `OWNER/NAME`, compared exactly and case-insensitively; no wildcard. Unset, empty, or any entry that is not `OWNER/NAME` shaped (a dot segment, a space, a `*`, an extra slash) refuses everything (fail closed). `CW_TOOLING_REPO` (`OWNER/NAME`) names the tooling's own repository, which is refused even when it is listed; unset adds no extra check, malformed refuses. A refused repository exits `2` with `refused: <repo> is not on the cloud allow-list` before any `kubectl` call, so nothing is created. Both settings are read from the environment of whoever runs the launcher or the driver, like the other `CW_` settings: the operator exports them in the shell (or the service unit) that starts it. An agent running as the same user could set them for a command it runs, so the list is a mistake-guard for the operator, not a boundary; the stops are the token's one-repository scope and the repository's own rules. The check compares spellings: a renamed or transferred repository is not matched by its old name, and an entry or tooling value ending in `.git` is malformed and refuses. The guard table is `docs/cloud-guard-parity.md`.
