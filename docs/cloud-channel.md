# Cloud channel: the hub and the pod command

One document to set up, run and take down the cloud hub and the pod-side command. It describes the code of PRs 1150 and 1152
(`bin/cloud_hub.py`, `bin/cloud_channel.py`, `bin/cloud_channel_server.py`, `bin/cloud_cli.py`, with `bin/cloud_client.py`).

## What they are

- **The hub** (`bin/cloud_hub.py`) is an HTTP listener that receives messages from agent pods and delivers each one as a prompt
  to the recipient agent's OpenCode session. Routes:
  - `POST /message`: JSON body `{"sender", "recipient", "key", "text"}` with `Authorization: Bearer <token>`.
  - `GET /decisions`: serves the JSON object from the file named by `FLEET_CLOUD_HUB_DECISIONS`
    (`{"decisions": [], "errors": []}` if the file is absent), behind the same token check and the same reserved-name refusal as
    POST (a token of a reserved service account other than the captain is refused 401 here too). The answer is scoped per caller: the captain sees the whole file; any other agent sees only the
    decisions whose `"agent"` field equals its pinned name (a decision with no `"agent"` field is the captain's alone; no `errors`).
    This is a trust choice: the captain is the orchestrator's own boss identity, and no pod can obtain that name (it is a reserved
    name: refused on send, and a secrets file that names it refuses everyone).
  - POST to a path other than /message and GET to a path other than /decisions answer 404. PUT, DELETE and PATCH answer 405 on
    any path. GET /message answers 404. HEAD and OPTIONS are not handled (501).
- **The pod command** (`bin/cloud_cli.py`) runs in an agent pod and sends through the channel (via `bin/cloud_client.py`):
  - `cloud_cli.py send RECIPIENT TEXT...`
  - `cloud_cli.py report KIND TEXT...`, where KIND is one of `done failed blocked question working paused withdrawn`.
    A report goes to the agent named by `FLEET_BOSS`, as the text `report: KIND: TEXT`.
  - The relay's PR-to-issue link comes from the PR body's closing keywords (and the plan's issue). It is not a security boundary: the
    prove holder check and the verdict evaluator registry are. A refused relay call is forgotten by the hub, so the same call_id may be
    sent again once the cause is fixed; a call_id reused with a different command, pr or head is 409.
  - `send` text is at most 2000 characters; `report` text at most 300.
  - Relay timing: the hub waits 30 s (`cloud_relay.WAIT`) for the daemon's answer. The daemon pulls the relay on its own loop (a
    thread, `cloud_relay_daemon.CADENCE` = 5 s), not after each engine pass, so a long engine pass does not delay an answer. If a pull is
    still late the pod's call gets 504; that is safe: the entry stays queued and the same call_id is replay-safe.
  - Relay configuration that must agree: the hub's agents map decides which issue and repo a pod is bound to (pod-binding), and the
    daemon's `relay_holders` config decides which pod may prove for that issue. If they disagree, the prove is refused 403 (fail closed).
  - `FLEET_CLOUD_HUB_DAEMON` must not name a reserved agent (`captain`, `switchboard`, ...): the hub refuses to start with a ValueError
    (nothing is bound, the process exits non-zero) rather than run with a daemon credential the reserved-name check would lock out.
  - The relay's reasons to a pod are fixed or allow-listed text, clipped under 2000 characters; the detail stays in the daemon's audit.
  - Relay daemon bounds (#1225): one hub request has a TOTAL deadline of 30 s wall clock (`READ_DEADLINE`); past it the read is a failed
    pass (`relay.pass_failed`) and the next pass retries. The live PR head is read BEFORE the ledger lock under a 10 s timeout (late: 504,
    nothing written); under the lock the daemon only compares it with the head the request judged, so no GitHub call holds the lock.
    Worst case per pass: 10 requests, each at most 4 GitHub context reads (report 1, gates 2, prove 3, verdict 4), so at most 40.
  - Report is at-most-once per call id: claimed (pending), delivered, marked done. A report CAN BE LOST (a crash or an error between claim
    and delivery) but is never duplicated; for `report` the ask's "exactly one" reads "at most one". A replay of a pending claim answers 409
    "lost or still pending: resend with a NEW call_id" and never delivers again.
  - Relay pass wall clock: every hub request (connect, headers, body, response; GET and POST) has one 30 s deadline (`READ_DEADLINE`), so
    a hub trickling headers is cut too; a pass has a 60 s budget (`PASS_BUDGET`) after which no new request is started (the rest wait for the
    next pass). Worst case for the hub transport of one pass: 60 s + the 30 s of the last request = 90 s, plus the GitHub reads of the request
    being handled (the 40 context reads above). Claims are in `relay_reports.json`, apart from `relay_calls.json`, so a pruned or missing
    `relay_calls.json` cannot cause a second delivery; only deleting `relay_reports.json` itself, or more than 50000 newer reports, can.
  - A refused relay call writes one audit line (`relay.refused`, `by` and `pr` clipped) on purpose; the ledger and files stay unchanged.

The sender is always the agent the token proves, never a name in the body: a body naming another sender is refused 403.

## Set up the hub

In a cluster the hub is defined by `cloud/k8s/cloud-hub.yaml` (see `docs/cloud-operator-quickstart.md`, section 4c). Install it once with `kubectl create -f cloud/k8s/cloud-hub.yaml`; never apply the whole file again: applying the whole file again resets the registry Secret to empty objects and deregisters every live agent until the launcher registers them again. Change the image with `kubectl -n cw set image deployment/cloud-hub hub=<registry>/<repository>@sha256:<digest>`. Roll back with `kubectl -n cw delete deployment,service -l app.kubernetes.io/part-of=cloud-hub`; adding `secret` to that list also deletes both Secrets (the registry and the per-agent passwords), and running agents cannot message until they are relaunched. An agent's URL has the form `http://<pod name>.cw-agents.cw.svc.cluster.local:4096`. The network boundary of the hub (which pods may reach it, which hosts it may reach) is enforced by the namespace's network policies, which are written in a separate manifest file under ask #1256 (cloud/k8s/cloud-hub-network.yaml); until that file is merged the policies exist only as applied in the cluster.

Start it with `python3 bin/cloud_hub.py` (from `bin/`'s directory or with it importable; it imports `cloud_channel` and
`cloud_channel_server`). Configure through the environment:

| Name | Meaning |
|---|---|
| `FLEET_CLOUD_CHANNEL_LISTEN` | `host:port` to bind (port 1 to 65535). Unset or malformed: nothing is bound and the process exits with status 2. It may be a non-loopback address; inside a pod bind the pod address. |
| `FLEET_CLOUD_HUB_AGENTS` | Path to the `agents.json` file (below). Read at each message. |
| `FLEET_CLOUD_HUB_SECRETS` | Path to the `secrets.json` file (below). When set, secrets mode is used instead of the cluster TokenReview. |
| `FLEET_CLOUD_HUB_DECISIONS` | Path to the JSON file served at `GET /decisions`. |
| `FLEET_CLOUD_AGENT_NAMESPACE` | Required when listening: the namespace agent accounts belong to (a namespace name; the manifest sets `cw`). Read once at start; missing or malformed stops the hub with `cloud-hub: FLEET_CLOUD_AGENT_NAMESPACE missing or not a namespace name`. |
| `FLEET_CLOUD_CHANNEL_AUDIENCE` | Optional (TokenReview mode only): the audience the token must be issued for. |

Audit lines go to stdout as JSON lines `{"ts", "line"}`; no token is logged.

### Identity

Default (no `FLEET_CLOUD_HUB_SECRETS`): the hub posts the bearer token to the in-cluster TokenReview API
(`https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews`) using its own service account token and CA from
`/var/run/secrets/kubernetes.io/serviceaccount`. The token must be authenticated and belong to a ServiceAccount in the namespace named by `FLEET_CLOUD_AGENT_NAMESPACE` (value `cw` in the manifest); the ServiceAccount name is the agent name
(lowercase letters, digits, `-`, at most 63 characters). The token must carry the channel audience. By default that is `fleet-switchboard`. If `FLEET_CLOUD_CHANNEL_AUDIENCE` is set, the hub asks the cluster for that audience instead. If the cluster cannot be asked the answer is 503: nothing is let through.

Reserved names (`captain`, `switchboard`, `fleet-switchboard`, `hub`, `admin`) are refused as sender or recipient (403), and a
token that proves a reserved name is refused. Service accounts in the agents' namespace are trusted as agents, so keep reserved
names out of that namespace.

### Cause of the namespace setting (ask #1273)

The code once pinned a constant named `fleet-agents` as the namespace a token must come from, while the hub runs in, and the launcher makes agent accounts for, the namespace `cw`. The constant is gone: the namespace is the one setting `FLEET_CLOUD_AGENT_NAMESPACE`. Nothing was broken: the prefix is only a label in secrets mode (the live hub's mode); in TokenReview mode an account of `cw` would have been refused and one of the old name accepted. Registries written by the launcher hold no namespace; one written by hand that names the old namespace must be rewritten before TokenReview mode is turned on.

### Secrets mode

When `FLEET_CLOUD_HUB_SECRETS` is set, the hub hashes the bearer token with SHA-256 and compares it
(`hmac.compare_digest`, against every entry) with the digests in `secrets.json`. A match yields the agent whose entry matched, in
the namespace named by `FLEET_CLOUD_AGENT_NAMESPACE` (the prefix is only a label in secrets mode). The file is read at each request (at most 8 MiB).

It refuses every request (401 or 503) if the file is missing, empty or malformed, is not an object, holds a duplicate digest, or
names a reserved agent.

What it does not protect, as far as the code shows:
- It is only a check of a shared secret held in the file. It does not use the cluster, so a token does not expire, is not bound
  to an audience and cannot be revoked other than by editing the file.
- Anyone who reads a pod's secret can send as that agent; the file holds only digests, but nothing here protects the secret in the pod.
- It adds no transport protection: the token travels in plain http (see Network boundary).

### agents.json (`FLEET_CLOUD_HUB_AGENTS`)

A JSON object keyed by agent name:

```json
{
  "<agent>": {
    "url": "http://<agent-host>:<port>",
    "session": "<session id>",
    "password_file": "/path/to/password-file",
    "issue": "<optional>"
  }
}
```

- `url`: `http` or `https`. `http` is accepted only for loopback, `localhost`, a `.svc` or `.svc.cluster.local` host, or a bare
  single-label host; any other host must be `https`. The host is parsed with the `ipaddress` module: a literal address passes only if
  it is loopback (link-local, unspecified, multicast and private addresses are refused), and a name with an all-digit label, a `0x`
  label or no letter (integer, octal, hex and dotless forms of an address) is refused.
- `session`: letters, digits, `_`, `-`, 1 to 100 characters.
- `password_file`: file holding the agent's OpenCode password (at most 4096 characters, no control characters, trailing newline
  removed); sent as Basic auth with user `opencode`.
- `issue`: optional; named in the message header and sent in the prompt metadata.

A recipient not in the file is refused 404. A bad record is a 503. The hub posts to `<url>/api/session/<session>/prompt` with
delivery `queue` and a 10 second timeout; a non-2xx answer from the agent is a 503.

### secrets.json (`FLEET_CLOUD_HUB_SECRETS`)

A JSON object `{"<agent>": "<sha256 hex of that pod's secret>"}`. Digests are compared stripped and lowercase. Never put the
secret itself in the file.

```json
{ "<agent>": "<64 hex characters>" }
```

## Set up an agent pod

| Name | Meaning |
|---|---|
| `FLEET_AGENT_NAME` | This agent's name (the sender). Required. |
| `FLEET_BOSS` | The agent that `report` goes to. Required for `report`. |
| `FLEET_CLOUD_CHANNEL_URL` | `http` or `https` URL of the hub, without user information. The command posts to this URL exactly as given, so it must include the `/message` path; redirects are refused. |
| `FLEET_CLOUD_ALLOW_HTTP_HOST` | Optional. The command refuses to send the token over plain `http` unless the host is loopback or is exactly this host (one line, exit 2, no request). |
| `FLEET_CLOUD_TOKEN_FILE` | Optional path to the token file; default `/var/run/secrets/tokens/fleet-switchboard`. |

The token file is read at each try: ASCII token characters only, at most 4096 bytes. The token is never printed.

## Run the pod command

```
python3 bin/cloud_cli.py send RECIPIENT TEXT...
python3 bin/cloud_cli.py report KIND TEXT...
```

It prints one line (`sent to <recipient>` or `reported <kind> to <boss>`, or the refusal) and exits:

| Exit | Meaning |
|---|---|
| 0 | Delivered (a duplicate also counts as delivered). |
| 1 | Refused or unavailable: the channel answered an error, or no token file, or retries ran out. |
| 2 | Bad usage or missing environment: wrong arguments, empty text, text over the limit, bad KIND, `FLEET_AGENT_NAME`, `FLEET_BOSS` or `FLEET_CLOUD_CHANNEL_URL` missing or invalid. |

The command makes up to 3 tries with one key. It retries only on a transport failure or a 5xx, with a backoff of 0.5 s doubling, and a 4xx is returned at once. Each request has a 10 second timeout.

## Delivery framing and dedupe limits

- Framing: the prompt text is a fixed header naming the verified sender (and the issue, if the agent record has one), then the
  body between `<<<BEGIN UNTRUSTED MESSAGE>>>` and `<<<END UNTRUSTED MESSAGE>>>`, marked as an untrusted message from that agent,
  not an operator instruction. Control characters other than newline and tab are stripped from the body first.
- Limits: `text` at most 20000 characters; `key` 1 to 200 printable ASCII characters without spaces; request body at most
  32768 bytes (413 above); a token over 4096 characters is refused 401; at most 32 requests are handled at once (one more is closed
  with no reply body).
- Dedupe: the key is scoped to the verified sender and recipient. A repeat answers 200 with `"duplicate": true` and is not
  delivered again. If delivery fails, the key is forgotten so a retry delivers.
- The remembered keys are held in memory only, at most 10000 pairs, the least recently seen key is forgotten first (a repeat refreshes a key). A hub restart forgets all of them, so
  a duplicate is possible after a restart (and after a key ages out of the 10000).

## Hardening guards

Each guard has a test that fails when it is removed (in `bin/test-cloud-hub` and `bin/test-cloud-cli`):

- **Framing.** Both markers of each message carry a fresh random nonce the body cannot know. A body that holds the hub's header
  shape (`[Message from agent`) or either marker is refused (503 to the sender, nothing delivered). Control characters, C1 controls,
  U+2028/U+2029 and bidirectional controls are stripped from the body.
- **Decisions read.** Same reserved-name refusal as send, and scoped per agent (see above).
- **Agent URL host.** See the `url` rule above.
- **Plain http from the command.** Refused unless loopback, a `.svc` / `.svc.cluster.local` name, or named in `FLEET_CLOUD_ALLOW_HTTP_HOST`. For an in-cluster hub over
  http, set that variable to the hub's host name.
- **Dedupe.** The key holds the token-pinned sender; each sender keeps at most 1000 remembered keys (its own oldest is forgotten
  first), and the global cap forgets the oldest key of the sender holding the most, so one sender cannot flush another's.
- **Command limits.** 10 second timeout, 64 KiB cap on a reply body, one-line output, sender stripped, the hub's reason shown in a
  refusal, default token path `/var/run/secrets/tokens/fleet-switchboard`.

## Network boundary

The hub speaks plain http; there is no TLS in the listener. The intended deployment is the hub and the agent pods in one
namespace, with a NetworkPolicy admitting only agent pods to the hub port. The code does not require or enforce this. The bearer
token and message text cross that network unencrypted. The hub does not check the source of a request beyond the token.
The network policies that implement this boundary are in cloud/k8s/cloud-hub-network.yaml (the six policies exactly as applied). Two limits of those policies, as applied: `allow-egress` selects every pod of the namespace, so the hub itself may also reach the internet on ports 80 and 443 outside the excepted private ranges; and no cluster or pod CIDR is excepted, which is acceptable only because the cluster's ranges lie inside the private ranges that are excepted (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10).

What the code does accept as an agent (`bin/cloud_channel.py`): a token whose username is `system:serviceaccount:<namespace>:<name>`
(prefix `system:serviceaccount:`) with the namespace exactly the one named by `FLEET_CLOUD_AGENT_NAMESPACE` (value `cw` in the manifest; read once at start) and a name that is a valid
agent name. Any ServiceAccount in that namespace is accepted, wherever the request comes from; any other namespace is refused.

## Take down

The code has no shutdown command or signal handler. Stop the hub by stopping its process (for example the pod or container
running it); it holds no state on disk, so the remembered dedupe keys are lost (see above; the least recently seen key is forgotten first, a repeat refreshes a key). The pod command is one-shot and
needs nothing to take down. To stop one agent receiving, remove its entry from `agents.json` (read at each message: new
messages get 404). To cut one agent off in secrets mode, remove its entry from `secrets.json` (read at each request).
