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

## Create is interrupt-safe
Ctrl-C, SIGTERM or any exception inside `create` deletes the per-run Secret and the pod it made and writes a
`create.interrupted` (or `create.failed`) audit line naming what was cleaned. If a delete fails, stderr and the audit line
say which pod or secret REMAINS, and the exit is non-zero (130 for a signal).

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
