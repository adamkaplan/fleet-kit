# Cloud worker interface (slices A, B, C)

The contract between the three parts of cloud workers (ask #323, plan rev 2.1), so they can be built in parallel.
**A** is the worker image. **B** is the launcher, the reaper and the end hook. **C** is switchboard support for a
remote worker. Names here are the shared vocabulary; a change to one is a change to this file first.
Items marked **OPEN** are not decided.

## 1. Launcher commands (slice B)

One program, `cloud-worker <command>`; every command takes `--json` (one JSON object on stdout; progress goes to stderr).
`<worker>` matches the switchboard's agent name rule. Nothing local changes: local launch, reattach and retire stay as they are.

| Command | Inputs | Output (stdout JSON) | Effect |
|---|---|---|---|
| `create <worker>` | `--repo OWNER/NAME`, `--branch B`, `--idle 10m` (default), `--hard 2h` (default), `--shape S\|M` | the endpoint record (section 3) plus `runtime_id` | starts one pod from the image with the environment of section 2; returns only once the worker answers |
| `stop <worker>` | `--reason TEXT` | `{worker, runtime_id, pushed, wip_branch}` | runs the end hook (section 5), then deletes the pod and its scratch volume |
| `extend <worker>` | `--hard +1h` or an ISO time; `--idle 30m` | the new deadlines | moves the deadlines; never past `--max-hard` (default 8h, **OPEN**: value) |
| `delete <worker>` | `--force` | `{worker, runtime_id, deleted}` | removes the pod and its leftovers without the end hook; refused unless the hook ran or `--force` is given; used by the reaper after a kept pod is resolved |
| `status [<worker>]` | none | one record per worker: `{worker, runtime_id, phase, idle_since, deadlines, cost_estimate_usd}` | read only |

Exit codes: `0` done; `1` the end hook could not push, **the pod is kept** (stop only); `2` bad usage or unknown worker;
`3` the cloud refused (quota, identity, cap); `4` timed out waiting for the worker; `5` the spend cap is reached
(create only; the cap is `CW_CLOUD_SPEND_CAP_USD`, default USD 25 when unset or empty, and an invalid value or an unreadable cost refuses the create); `6` refused: `delete` before the end hook ran without `--force`, or `extend` past `--max-hard`. Every other failure is `1` with a one-line reason on stderr.

The launcher reaches the cluster with an explicit `--context` taken from `CW_KUBE_CONTEXT` (and `--kubeconfig` from `CW_KUBECONFIG` when set). With no context configured it refuses every command (exit `2`) and makes no call; it never uses the active context. A `create` that is interrupted or fails removes the Secret and pod it made, audits `create.interrupted` or `create.failed`, and names anything that could not be removed.
A command is idempotent: a repeated `stop` or `delete` of a worker that is gone exits `0`.

## 2. Worker environment (slices A and B)

Injected at start, as environment variables (all non-secret):
`CW_WORKER` (name), `CW_REPO`, `CW_BRANCH`, `CW_IDLE_SECONDS`, `CW_HARD_DEADLINE` (ISO UTC),
`CW_TOKEN_ENDPOINT` (the URL where the worker mints a short-lived GitHub token, authenticated by the pod's federated
identity), `CW_AUDIT_RUNTIME_ID`. The per-worker server password is the one value injected as a file
(`/run/cw/server-password`, mode 0600, from a cluster secret scoped to the pod), never as an environment variable.

**Never stored**, in the image, an environment variable, a file on the pod's disk beyond the password above, or a log:
a personal access token, a cloud key or client secret, a registry password, a model API key. Tokens are minted at start
and on demand from `CW_TOKEN_ENDPOINT`, live at most 60 minutes, and are held in memory or a tmpfs. Slice A ships a
scan that fails the image build when any stored credential, host key or build user is found.

## 3. Endpoint record and the switchboard mapping (slices B and C)

Owned by the switchboard (slice C), in its state directory, mode 0600 (a JSON file per worker, path built only from the validated worker name). Slice B never touches the file: `create`, `extend`, `stop` and `delete` print their result and C applies it to the record. The record's deadlines are the source of truth; the pod's `CW_HARD_DEADLINE` is its start-time copy and the reaper re-reads the record.

```json
{"worker": "coder7", "url": "https://worker-coder7.example.internal:4096", "auth_ref": "file:<state>/cloud/coder7.password",
 "runtime_id": "pod/coder7-7f9c", "idle_deadline": "2026-10-14T12:10:00Z", "hard_deadline": "2026-10-14T14:00:00Z"}
```

`auth_ref` names where the switchboard host keeps its own copy of the password (a file path the switchboard chooses itself under its state directory; whatever the launcher prints for it is ignored; it is never taken from caller input, mode 0600: it protects only against other local users); the record never holds it. The pod's copy (`/run/cw/server-password`) is a separate file on the pod side. `create` returns the password once, on a file descriptor named in its output, for C to store. C creates the record from `create`'s output and removes it after `stop` or `delete`.

| Switchboard verb | On a cloud worker |
|---|---|
| launch | `create`, then the existing session create and brief, against `url` instead of the loopback service file |
| send / wake | the same session API call as local, against `url` with Basic auth from `auth_ref` |
| read / status / reports | the same reads (and event stream), against `url` |
| retire | `stop`; the retire says the WIP branch if work was pushed, and fails with `stop`'s exit 1 reason if not |

Today `V2Http` accepts only a `http` loopback address and one service file. Slice C must generalise it to one
endpoint per agent, allowing `https` to a non-loopback host only when the record names one (the password goes nowhere
else). Reachability is over a private network and a TLS proxy (A and B own that; **OPEN**: exact topology).

## 4. Audit line (slice B; mirrored into the switchboard audit log by C)

One line per `create`, `stop`, `extend` and `delete`, appended before the action returns, JSON, no secret:

```json
{"ts": "2026-10-14T12:00:00Z", "worker": "coder7", "runtime_id": "pod/coder7-7f9c", "action": "create", "cost_estimate_usd": 0.07, "ok": true, "actor": "operator", "reason": ""}
```

`cost_estimate_usd` is the list-price estimate of the pod so far (zero on create; elapsed hours times the shape's rate
on the others). It is an estimate, not a billed figure. A failed action still writes its line with `"ok": false`. `actor` is taken from the session, never from an argument; `reason` carries `--reason` (for `stop`, `extend` and `delete --force`).

## 5. End hook (slices A and B)

Runs inside the pod on `stop`, a hard deadline, an idle stop and a node drain notice, before anything is deleted:
1. If the working tree has uncommitted changes, commit them (files matching the secret-scan patterns and the ignore list are left out, and the hook reports them) on a WIP commit; if HEAD is on no remote branch, push it to `<worker>/wip`.
2. Push success: report `{pushed: true, wip_branch}` and let the launcher delete the pod.
3. Push failure: **keep the pod**, report the reason, and `stop` exits `1`. The pod stays until `delete --force` or its next stop succeeds. A kept pod still obeys the hard limit's cost cap (**OPEN**: whether a kept pod may exceed the hard limit, and who is alerted).

A clean tree whose HEAD is already pushed pushes nothing and reports `pushed: false`.

## 6. Idle and hard limit

- **Idle** = no tool call and no model activity (a running session or step) for `CW_IDLE_SECONDS`. Inbound
  requests, health checks and event-stream reads do **not** count as activity: a worker that is only being watched is idle.
  A worker mid-task (a long build, a waiting model call) is not idle. The reaper reads this from the worker's own session state.
- **Hard limit** = `CW_HARD_DEADLINE`, absolute. At the deadline the end hook runs and the pod is stopped,
  whatever the activity. `extend` is the only way to move it; the reaper re-reads the record at each pass.
- Both stops are checked at least every minute and take effect within 3 minutes of the limit (plan scenarios S2, S3).
- **OPEN**: how a worker asks for its own extension (a command inside the pod, or only an operator).
