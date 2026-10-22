# cloud-worker (slice B)

Launcher, reaper, end hook and token mint for cloud workers, built to `docs/cloud-worker-interface.md`.

- `cloud-worker create|stop|extend|delete|status|reap` (`cloud_worker.py`): reaches the cloud only through `kubectl`
  (`CW_KUBECTL`, `CW_CLOUD_NAMESPACE`); writes one audit line per action to `CW_CLOUD_AUDIT`
  (default `~/.local/state/cw-cloud/audit.jsonl`, mode 0600). `reap` is one pass (or `--loop SECONDS`, never slower
  than 60); run it from a cron-like scheduler. It stops a pod past its hard deadline, or idle (not busy, last
  activity older than the idle time, read by `kubectl exec <pod> -- cw-worker-activity`; an unreadable answer is not a reason to stop).
  **Managed reaper (opt-in).** The installer's `--with-cloud-reaper` option writes and loads a service from the two
  reaper templates under `templates/` (a macOS LaunchAgent or a systemd user unit; the steps are in
  `docs/cloud-operator-quickstart.md`): it runs `cloud-worker reap --loop 60` at login, restarts after any exit (30 s apart, so a
  crash loop cannot spin) and logs under the switchboard state directory (`cloud-reaper-stderr.log`). It is opt-in, like the
  heartbeat's service, because it needs a cluster login this machine may not have; the default install does not touch it, and the
  installer's uninstall removes it. The template holds no secret: the installer copies only the non-secret `CW_*` settings it finds
  in its own environment (names and file paths, never a token; a password in a URL is refused) and the reaper reads the operator's
  token FILE by path. After the private settings file lands, the reaper reads it as the launcher does. Every ending writes one line
  `<UTC time> reaper exiting: WHY`: end of its pass, stopped by a signal, stopped by an interrupt, an error (a crash in a pass), or
  `cluster login failed: ...` (a login failure exits non-zero). A restarted reaper stops by name and finds no pod for one the last
  pass already stopped, so nothing is stopped twice. Only one reaper runs at a time: it takes an exclusive lock (a 0600 file named for the user's namespace in `~/.local/state/cw-cloud/locks`, found through the password database, so a different audit file, state directory or login (a token file, a kube context, a kubeconfig) is still a rival; one user runs one reaper per namespace. The lock directory itself is checked for owner and mode, its parents are not walked, which is adequate for a lock) before its first pass and a second one exits at once with `not started: another reaper holds the lock` (the kernel drops the lock when a holder dies, however it dies). A stop uses the pod's own run id where the pod carries one. The installer writes the service file only when the target is a plain file of the user's own (never through a symlink), by temp file and rename, and its uninstall removes only a file the template makes (a regular file of the user's own with other content is overwritten by install, by rename, as the installer's own path; a symlink, a non-regular file, a foreign or group-writable file is refused). Path settings must be absolute paths of plain characters. A `path_walk` earlier on `sys.path` (the launcher directory or `PYTHONPATH`) would win over the kit's: that is the same user's own environment, not a boundary. While the Mac sleeps the reaper pauses.
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
`cw-operator` account of `cloud/k8s/operator-rbac.yaml` (one namespaced Role: pods, logs, exec, port-forward, secrets, and statefulsets with get, list, watch, create, delete for a persistent role; no
wildcard, nothing cluster-wide). Every `kubectl` call carries a generated `--kubeconfig` (mode 0600, private temp dir, removed at
exit) whose only credential is the token file's path. It never uses `--token` or an administrator kubeconfig, and refuses
if `CW_KUBECONFIG` or `CW_KUBE_CONTEXT` is also set. A missing or unreadable token file exits 3 (`token unavailable`) with no call.

`create --pull-secret NAME` sets `imagePullSecrets`. `create --run-secret-fd NAME=N` (repeatable) reads each value once from
descriptor `N`, pipes it to `kubectl` on stdin into one Secret `<pod>-run`, and mounts it read-only at `/run/cw/NAME` (0400).
The Secret goes with the pod at `stop`, `delete` and a failed or interrupted `create`; a pod kept because the end hook failed keeps it.
A `stop` by name only (no run id) or a `delete` of a worker whose pod is absent removes its StatefulSet first, then its Secrets: the caller named the worker, so it owns it (a run id exists only to protect another run's objects, and a different run id leaves everything whole). A `delete` whose StatefulSet lookup fails for any reason but NotFound exits non-zero and never answers `deleted`.

After this change merges, an operator must apply the manifest (`kubectl apply -f cloud/k8s/operator-rbac.yaml`) on the owner's word; until then `launch --role orchestrator` is forbidden on the real cluster.

Accepted scope of the role: `cw-operator` can get and list every secret in the workers namespace (a Role cannot limit `list` by label), so the namespace must hold only worker pods and their per-pod secrets; that is enforced in the deployment repository, not here.

Everything that renders `kubectl` output (errors, the end hook's text, audit reasons) passes through one `scrub()`: this run's secret values and any 8+ character piece of one, token-shaped strings, long base64/hex runs, an `Authorization` header of any scheme (any case and spacing) and the `user:password@` of a URL of any scheme are removed, then the text is cut (the launcher, the run record and the driver all use this one function). Limits: the Authorization shape removes the rest of its header line and its folded continuation lines; the URL shape removes the userinfo of a `scheme://` URL only inside its own part (the user and the password hold no `/ ? # @ :`, so host, path, query and fragment survive: scoped packages, `@user` paths, `?email=a@b`, a port), and an unencoded password with those characters only when a `:` follows the user and what follows is not a bare port (every quantifier bounded, so hostile text cannot slow the scan); a secret split by a newline is caught only for pieces of 8 or more characters (the existing rule); two known slip-throughs: a password that starts with digits and then holds `/`, `?` or `#` (it reads as a port), and a user part over 200 characters; a credential that is not in either shape and is not a registered value is not recognised. A failed `create`, `stop` or `delete` that leaves a per-run Secret says so once (audit and stderr) with its name and the exact `kubectl ... delete secret NAME` command, never a value.

## The run record and the report (slice 3)
The record is the launcher's `stop`'s, so every end gets one: `run-task` (normal end, failure, interrupt), the switchboard's `retire`
and a person running `cloud-worker stop`. Before the end hook and the delete, `stop` reads the pod's server password and run secrets from
the pod's own mounts, opens its own loopback port-forward, reads the session's messages and writes `$CW_RUN_RECORD_DIR/<run id>/`
(`--record-dir` overrides; default `runs/` beside the audit file; directory 0700, files 0600; a root that is a symbolic link is refused):
`transcript.jsonl` (each message one JSON line, every string value and key through the one scrub, so a line still parses; at most 5 MiB,
cut with a `TRANSCRIPT CUT` marker line; `CW_RECORD_MAX_BYTES` lowers it; the whole record phase has one deadline, `CW_RECORD_SECONDS`, default 90 s, maximum 240 s: past it the record says `transcript: unavailable (record timed out)` and the stop still deletes), `tally.json` (tool calls by tool name, turns; no content) and
`run.json` (worker, repository, start, end, exit reason, head sha when known; `transcript: unavailable` plus a reason when the server could
not be read, or its answer was over 8 MiB or nested deeper than 100 levels: checked before parsing). A failing record never holds the stop
back and a failing stop never deletes it; `--no-record` skips it. `stop` prints `run record: DIR` once and the audit reason names it. Once the end hook answers, `run.json` is rewritten atomically with `end_hook` (pushed, wip_branch, skipped, note, error); this holds for the reaper's stops too, whose pass output carries the same fields. A `reap` process writes one line `<UTC time> reaper exiting: WHY` when it ends.
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

## The stack create and the fresh-node race

`create --stack` watches a Pending pod for a bounded window (`STACK_RACE_WINDOW_SECONDS`, 600 seconds, with a pause of `STACK_RACE_PAUSE_SECONDS`, 15 seconds, before each recreate; there is no attempt count) and recreates it, with the same
run id and nonce and without touching the per-run secrets, in two cases. First, the node's runtime handler is not yet
known (the exact `FailedCreatePodSandBox` message): the node's runtime is configured by the installer about 30 to 60
seconds after scheduling. Second, the pending pod vanishes: a get that saw the pod then says not found, which live run B
saw when that runtime restart took the Pending pod object with it. Each attempt writes one audit line (`attempt N`,
and `pod vanished` for the second case). Before a recreate the launcher checks that this call's own Secrets are still there: a missing one means an outside deletion (a stop, the reaper), which fails the create without recreating (the check also runs after a recreate and before ready; it narrows the race with a stop in flight and cannot close it, as the pod and Secret deletes are not one step: a stop deletes the pod first and the Secrets later, so the creator can recreate and the Secret deletes land after the last check; if that pod never starts, the create's own wait ends and its cleanup deletes it; if it started and was reported ready, the stop has finished and the only later catch is the reaper at `--hard` (or idle)). A pod that was never seen, and every other failure, is not retried. A recreate starts only if one more pause plus one poll (2 seconds, `WAIT_POLL_SECONDS`) fits in what is left of the window, and a retry's wait is cut to the time left, so the window bounds the wall time (the delete wait of up to 150 s per recreate aside). After the
window the create fails with one message that names the race or the vanish, the attempts made and the seconds spent. See `docs/cloud-worker-interface.md`.

## Driving a cloud worker from the switchboard (`launch --cloud`, `send`, `read`, `status`, `retire`)

The switchboard (`bin/cloud_worker.py`) calls this launcher; the settings below are read from the operator's environment.

| Setting | Meaning |
|---|---|
| `CW_ALLOWED_REPOS` | the repositories (`OWNER/NAME`, separated by commas; matched exactly, ASCII case-insensitively; empty means none; ONE malformed entry refuses the whole list, as the launcher's own `create` does; a malformed `CW_TOOLING_REPO` also refuses; checked before anything is created; `CW_TOOLING_REPO` is refused even if listed |
| `CW_CLOUD_SERVER` (`CW_CLOUD_CA_FILE`) | the cluster: an https URL whose host is a DNS name (`https://NAME[:PORT]`) |
| `CW_CLUSTER_TOKEN_CMD` | a command that prints a cluster token; when unset the default below is used |
| `CW_VAULT_SUBSCRIPTION`, `CW_VAULT_NAME` | the vault that holds the cluster administrator credential (secret `CW_CLUSTER_ADMIN_SECRET`, default `cw-cluster-admin-kubeconfig`; or a command in `CW_CLUSTER_ADMIN_CMD`) |
| `CW_CLUSTER_TOKEN_DURATION` | the token's requested life, default `20m`, at most `60m` (kubectl's own floor is 10 minutes) |
| `CW_GIT_TOKEN_CMD` | prints the one-hour git token for the repository (default: `app_token.py OWNER/NAME`, same settings as `run_task.py`); the managed reaper runs the same command to renew the token every 30 minutes |
| `CW_OPENCODE_BIN` | the `opencode` program (default `opencode` on `PATH`); the launcher runs `opencode auth export` (fixed arguments, scrubbed environment, output through a pipe, in memory) and keeps only the one `github-copilot` entry, as a one-entry array. Refused by name: command missing or failing, no entry, two entries, an inactive entry, an entry that is not `oauth`; nothing is created |
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
  message naming the vault pre-check (`launch --cloud` runs the vault access check itself after a firewall refusal: see docs/cloud-operator-quickstart.md, section 2b) and makes no cluster call.
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
**The cluster-side floor.** Every pod from `render_pod` (the one builder; shapes S and M, with or without a pull secret or run secrets) sets
`spec.activeDeadlineSeconds` = the `--max-hard` cap + `POD_DEADLINE_GRACE_SECONDS` (300 s). The cap, not the initial hard limit, because
Kubernetes cannot raise the field on a running pod, so `extend` within the cap must stay legal; the grace lets the reaper (record, end hook)
act first. The cap's default is 8h, so the floor is always finite. A pod past it is `Failed` with reason `DeadlineExceeded`; `stop` and the
reaper delete a `Failed` pod and its Secrets (the record is still written). The kubelet counts from the pod's start, a little after
the create's own clock, so the floor can trail the stored cap by scheduling time: that is within the grace by design.
`extend` and `max-hard` are measured from the pod's start. A pod whose hook failed is annotated `cw/kept`;
`delete` needs `cw/hook-ran` or `--force` (exit 6). The reaper retries a kept pod each pass (the OPEN item on
exceeding the hard limit and alerting is not decided: it exits 1 from `reap` so a scheduler can alert). The spend cap
is read from `CW_CLOUD_SPEND_CAP_USD`; unset or empty means the prototype's USD 25, and zero, negative, non-numeric,
`nan`, `inf` or a cost that cannot be read refuses `create` (exit 5). The check adds this launch's estimate (hard limit times the shape's rate) to what the running pods already cost. A worker name with `_` maps to `-` plus a 4-hex
hash for the pod name. Shape rates are placeholders (`S` 0.10, `M` 0.20 USD/hour); `create` refuses a spec with a stored credential.

**Not in this slice.** Automatic detection of a node drain notice: the end hook runs on `stop`, the idle stop and the hard limit; a drain is handled today by the operator running `stop --reason "node drain"`. The `cw-worker` service account's cloud identity and what it may do belong to the image/cluster setup (slice A and the platform), not here. `bin/test-cloud-launcher` is not yet wired into CI (the suite lists are CI's own).

**Old names.** The launcher reads and writes only the neutral `cw` names (one table in `tests/test_cw_names.py`). No worker exists under an earlier name, and none is read: a pod or secret left under an old name is invisible to `status` and `reap` and must be removed by hand.

## The git token and its renewal

**The cause.** The token a worker pushes with is a one-hour GitHub App installation token (`app_token.py` refuses anything over 61 minutes),
minted once at launch and stored as the `git-token` key of the per-run Secret `<worker>-run`. Nothing used to renew it, so a worker whose
hard limit is one hour or more would lose push, and the end hook would lose its retire-time push, when that first hour ended.

### How the git token is renewed

The managed reaper's loop (`reap --loop`) does it, as one more step of each pass (`Launcher.renew_git_tokens`):

- **When.** For each running pod, when the token is `GIT_TOKEN_RENEW_AFTER_SECONDS` (1800, so 30 minutes of its life are left) old. The age is the Secret's
  `cw/git-token-minted` annotation, or the Secret's creation time when it has none. A pass that finds the token older than an hour (the machine slept) renews at once.
- **How.** It mints through the SAME route as the launch (`run_git_token_cmd`: `CW_GIT_TOKEN_CMD`, else `app_token.py OWNER/NAME`), so one repository, the exact
  permissions and the 61-minute cap hold by construction, and replaces only the `git-token` key by one merge patch whose body goes on standard input. The patch is bound to the exact Secret that was checked: its body carries that Secret's `metadata.uid` and `metadata.resourceVersion`, so the API server refuses it (Conflict) if the Secret changed or was made again while the mint ran (a relaunch of the same worker gets the same names); the labels are read again after the mint as a second guard. A refusal keeps the old state, writes one audit line (`the secret changed during the mint: not renewed`), names the worker in the report and is retried at the next pass. The token is
  never in an argument, an environment value, a file, an output, an audit line or a record. The pod needs no change: its credential helper and `gh` wrapper read the file at every
  call, and the file is a whole-directory projected volume (no `subPath`) that the kubelet refreshes, usually within a minute or two.
- **For which repository.** The pod's `CW_REPO` must equal the `repo` of the switchboard's worker record (read only, `CW_CLOUD_RECORD_DIR`, which the renewal therefore needs in the reaper's
  environment) and be on `CW_ALLOWED_REPOS`. A missing or unreadable record, a mismatch, a pod that is not running or past its hard limit, or a Secret that is not this run's stops the renewal.
- **The reaper MUST have `CW_CLOUD_RECORD_DIR` set.** The role (worker or orchestrator) comes from the switchboard's worker record only. With the variable unset, or the record missing or
  unreadable, the role reads as `worker`: an orchestrator is then treated as a worker, skipped by the renewal pass without a message, and its token dies at one hour.
- **A pod that is gone.** `stop` and `delete` of a persistent role whose pod `<name>-0` is absent (node loss, eviction, the gap before the controller recreates it) but whose
  StatefulSet remains delete the StatefulSet first, then the Secrets; a StatefulSet of another run id is left whole, and a failed delete names `statefulset/<name>` as REMAINING.
- **The reaper's own needs.** It runs the mint, so it reads the vault settings (`CW_VAULT_SUBSCRIPTION`, `CW_VAULT_NAME`, from the settings file as the launcher does) and needs a signed-in `az` session and a network path the vault accepts.
  Without them every renewal fails closed (below) and workers behave as before: they lose push at one hour.
- **When it fails** (the vault, the lookup, the mint or the patch): the old token stays, one audit line (`action: renew-git-token`, `ok: false`, the reason, never a value) is written, and the next pass tries again. The worker is
  named in the pass's report as `git token not renewed: ...` with exit 1, so its lane can retire it before the token ends. A failure that persists is audited once per CHANGE of reason, not every pass (the report still names the worker every pass); a renewal writes one `ok: true` line, which also marks a failure as cleared, and a `git token renewed` entry.
- **Clock skew.** The age is judged by the reaper's clock against the stamp the reaper itself wrote (or the cluster's creation time): a skew of a few minutes between them shifts the renewal by that much, and a stamp from the future is not trusted (it is renewed at once). The 30-minute margin absorbs a skew of several minutes; a larger one is a clock to fix.

#### Acting on an object later, by name

A place that reads a Secret or pod and acts on it later by name either carries a precondition or says why not. Names come back: a stopped worker's pod and Secret names are
the next run's names.

| Place | Reads | Acts | Precondition, or why none |
| --- | --- | --- | --- |
| The git token renewal | the per-run Secret (labels, `uid`, `resourceVersion`) | one merge patch, by name, after a mint of up to a minute | Yes: the patch body carries the `uid` and `resourceVersion`; the labels are read again after the mint |
| The launcher's cleanup of a failed create (`_cleanup`) | the object's run id (`_mine`) | deletes it by name | Accepted: the run id is read immediately before each delete (no mint, no hook, no wait between), and a create of that name is refused while the old object exists |
| `stop` and the reaper's idle and hard-limit stops (`_remove`) | the pod (its `uid`) at the start, then each object's run id again (`_mine`) | deletes by name, after the end hook (which can take minutes) | Yes for the pod: the delete carries the `uid` read at the start as a precondition (`DeleteOptions.preconditions.uid`), so a pod that took the name meanwhile (the old one was removed by a node loss, an eviction, a preemption, a manual delete, or another stop) is left whole with one refused audit line, with or without a run id. The Secrets are matched by run id just before each delete (a few milliseconds); a stop with NO run id has no id to compare, so a Secret of the same name made in that gap is deleted by name |
| `stop`: `annotate kept` and `annotate hook-ran` | the pod (its `uid`) at the start | one merge patch by name after the hook | Yes: the patch body carries the `uid` (the API server refuses a mismatch). A changed pod is not marked, with one audit line: for `hook-ran` that leaves `delete` refusing (the safe side), for `kept` the failed hook's error is reported with it |
| `delete` (no run id given) | the pod (its `uid`) | deletes the pod (bound to that `uid`) and both Secrets | The pod delete is bound; the two Secrets are deleted by name, by the operator's explicit command, with no run id to compare (`--force` is its own statement). Note: `delete` without `--force` trusts the `hook-ran` marker, which a stop now writes only onto the pod it read |
| `extend` | the pod (its `uid`) | annotates the deadline, bound to that `uid` | Yes: a changed pod is not extended (run it again) |

## Server-side guards: `app_token.py` and `preflight.py`

A guard is enforced where the action takes effect (the repository's rules, the token's scope), not in a wrapper.
These two programs are the server side of that. Neither writes a file; neither prints a secret.

**`app_token.py OWNER/REPO`** prints a one-hour GitHub App installation token for that one repository, and nothing
else, on stdout. It refuses (exit 1, one line on stderr naming the key, never a value) to print a token unless:

- the installation is found, not stored: the mint asks GitHub (`GET /repos/OWNER/REPO/installation`, signed with the App's
  own short-lived token) which installation covers the repository, and refuses unless that answer is one installation whose
  account is the repository owner (case-insensitive) and which is not suspended. A repository the App is not installed on
  is refused with a message that says so, before any pod exists. To cover a new repository, install the App on the account
  that owns it and add the repository to that installation (an App installed on two accounts mints for each of them);
- the answer lists exactly the one repository and expires within 61 minutes; and
- its `permissions` equal exactly `contents: write`, `pull_requests: write`, `issues: read`, `metadata: read` (the one table, `EXPECTED_PERMISSIONS`; the request asks for the same four
  and the answer is checked again): no extra key, no higher level, none missing.

The worker can now read issues and their comments of its own repository (its ask and plan), through `issues: read`.
It cannot write issues, labels, merges or settings; `issues: read` opens no write path, and the worker's `gh` wrapper
still refuses every such write (a second line behind GitHub's own permission). The one owner step, in this order: merge the
change, then add `issues: read` to the App's permissions and accept it on each installation. Until then a launch is refused
before any pod exists, with one line that names `issues` and the owner step (the mint fails closed rather than also
accepting the old set). The exact refusal GitHub gives for a permission granted but not yet accepted is not verified live.

The private key travels only through a pipe to `openssl`; only `https://api.github.com` is used (`CW_GITHUB_API_BASE`
may name a loopback `http` address, for tests).

**`preflight.py OWNER/REPO [--branch NAME] [--accept-classic]`** reads, with GET only, whether the default branch
refuses direct pushes (a `pull_request` rule that requires at least one approving review, or a `merge_queue` rule, in an active ruleset that covers the branch; `evaluate` and `disabled` rulesets and rulesets not covering the branch are refused by name; ref patterns follow GitHub: `*` stops at `/`, `**` crosses it, and a pattern the program does not understand, such as a character class, covers nothing), force-pushes (`non_fast_forward`) and deletion
(`deletion`). Exit 0 prints `preflight ok: <rules>` (with `; WARN open: <actions>` when a new check is open: an App bypass actor, auto-merge, no required status check, a merge queue; each also has its own `preflight: WARN open action <name>: ...` line, and the launch continues; the one table `CHECK_MODES` in `preflight.py` decides WARN or BLOCK, and a flip to BLOCK is a reviewed change); exit 1 names each missing rule; exit 2 means the answer could not
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

Settings (environment): `CW_APP_ID_CMD`, `CW_APP_KEY_CMD` (each a command printing the
value; otherwise read from the vault; `CW_APP_INSTALLATION_CMD` and the vault secret `cw-app-installation-id` are obsolete: accepted, never run), `CW_VAULT_SUBSCRIPTION` and `CW_VAULT_NAME` (no defaults), `CW_OPENSSL`,
`CW_GITHUB_API_BASE` (tests), `CW_PREFLIGHT_TOKEN_CMD` (tests: replaces the mint in the preflight).

**Repository allow-list.** `create` (and the driver, `run-task`, through the same function `check_repo_allowed`) launches only for a repository the owner listed. `CW_ALLOWED_REPOS` is a comma-separated list of `OWNER/NAME`, compared exactly and case-insensitively; no wildcard. Unset, empty, or any entry that is not `OWNER/NAME` shaped (a dot segment, a space, a `*`, an extra slash) refuses everything (fail closed). `CW_TOOLING_REPO` (`OWNER/NAME`) names the tooling's own repository, which is refused even when it is listed; unset adds no extra check, malformed refuses. A refused repository exits `2` with `refused: <repo> is not on the cloud allow-list` before any `kubectl` call, so nothing is created. Both settings are read from the environment of whoever runs the launcher or the driver, like the other `CW_` settings: the operator exports them in the shell (or the service unit) that starts it. An agent running as the same user could set them for a command it runs, so the list is a mistake-guard for the operator, not a boundary; the stops are the token's one-repository scope and the repository's own rules. The check compares spellings: a renamed or transferred repository is not matched by its old name, and an entry or tooling value ending in `.git` is malformed and refuses. The guard table is `docs/cloud-guard-parity.md`.

## Roles: worker and orchestrator

A cloud pod has a role. An idle orchestrator-like session measured about 365 MB resident (a container peak of 456 MiB at boot) and about 1% of a core; the `orchestrator` role is sized from that and is one small always-on pod. Generated from the launcher's tables (`render_roles_doc`); a test pins it.

<!-- roles:begin -->
| Role | Mode | Pod size (requests = limits for a persistent role) | Token set | List-price estimate |
|---|---|---|---|---|
| `worker` | worker: restart Never, hard deadline, idle limit | 2 CPU, 4Gi | contents: write, pull_requests: write, issues: read, metadata: read | USD 0.10 per hour (shape S) |
| `orchestrator` | persistent: one-replica StatefulSet, restart Always, no deadline, no idle limit, ends only on an explicit stop | 250m CPU, 768Mi | issues: write, pull_requests: read, checks: read, metadata: read | USD 0.02 per hour, USD 0.48 per day always on |

The role is chosen by `--role` (a key of the role table; default `worker`); the orchestrator's size is chosen only by the role, never by `--shape`. The orchestrator holds no contents write and never the App key: the launcher and the reaper mint its token. Owner step for its set: the owner adds issues: write, pull_requests: read, checks: read, metadata: read to the App's permissions and accepts them on each installation.
<!-- roles:end -->
