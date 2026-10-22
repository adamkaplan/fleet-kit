# Cloud channel: the hub and the pod command

One document to set up, run and take down the cloud hub and the pod-side command. It describes the code of PRs 1150 and 1152
(`bin/cloud_hub.py`, `bin/cloud_channel.py`, `bin/cloud_channel_server.py`, `bin/cloud_cli.py`, with `bin/cloud_client.py`).

## What they are

- **The hub** (`bin/cloud_hub.py`) is an HTTP listener that receives messages from agent pods and delivers each one as a prompt
  to the recipient agent's OpenCode session. Routes:
  - `POST /message`: JSON body `{"sender", "recipient", "key", "text"}` with `Authorization: Bearer <token>`.
  - `GET /decisions`: serves the JSON object from the file named by `FLEET_CLOUD_HUB_DECISIONS`
    (`{"decisions": [], "errors": []}` if the file is absent), behind the same token check.
  - POST to a path other than /message and GET to a path other than /decisions answer 404. PUT, DELETE and PATCH answer 405 on
    any path. GET /message answers 404. HEAD and OPTIONS are not handled (501).
- **The pod command** (`bin/cloud_cli.py`) runs in an agent pod and sends through the channel (via `bin/cloud_client.py`):
  - `cloud_cli.py send RECIPIENT TEXT...`
  - `cloud_cli.py report KIND TEXT...`, where KIND is one of `done failed blocked question working paused withdrawn`.
    A report goes to the agent named by `FLEET_BOSS`, as the text `report: KIND: TEXT`.
  - `send` text is at most 2000 characters; `report` text at most 300.

The sender is always the agent the token proves, never a name in the body: a body naming another sender is refused 403.

## Set up the hub

Start it with `python3 bin/cloud_hub.py` (from `bin/`'s directory or with it importable; it imports `cloud_channel` and
`cloud_channel_server`). Configure through the environment:

| Name | Meaning |
|---|---|
| `FLEET_CLOUD_CHANNEL_LISTEN` | `host:port` to bind (port 1 to 65535). Unset or malformed: nothing is bound and the process exits with status 2. It may be a non-loopback address; inside a pod bind the pod address. |
| `FLEET_CLOUD_HUB_AGENTS` | Path to the `agents.json` file (below). Read at each message. |
| `FLEET_CLOUD_HUB_SECRETS` | Path to the `secrets.json` file (below). When set, secrets mode is used instead of the cluster TokenReview. |
| `FLEET_CLOUD_HUB_DECISIONS` | Path to the JSON file served at `GET /decisions`. |
| `FLEET_CLOUD_CHANNEL_AUDIENCE` | Optional (TokenReview mode only): the audience the token must be issued for. |

Audit lines go to stdout as JSON lines `{"ts", "line"}`; no token is logged.

### Identity

Default (no `FLEET_CLOUD_HUB_SECRETS`): the hub posts the bearer token to the in-cluster TokenReview API
(`https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews`) using its own service account token and CA from
`/var/run/secrets/kubernetes.io/serviceaccount`. The token must be authenticated and belong to a ServiceAccount in the namespace `fleet-agents`; the ServiceAccount name is the agent name
(lowercase letters, digits, `-`, at most 63 characters). The token must carry the channel audience. By default that is `fleet-switchboard`. If `FLEET_CLOUD_CHANNEL_AUDIENCE` is set, the hub asks the cluster for that audience instead. If the cluster cannot be asked the answer is 503: nothing is let through.

Reserved names (`captain`, `switchboard`, `fleet-switchboard`, `hub`, `admin`) are refused as sender or recipient (403), and a
token that proves a reserved name is refused. Service accounts in the agents' namespace are trusted as agents, so keep reserved
names out of that namespace.

### Secrets mode

When `FLEET_CLOUD_HUB_SECRETS` is set, the hub hashes the bearer token with SHA-256 and compares it
(`hmac.compare_digest`, against every entry) with the digests in `secrets.json`. A match yields the agent whose entry matched, in
namespace `fleet-agents`. The file is read at each request (at most 8 MiB).

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
  single-label host; any other host must be `https`.
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

## Network boundary

The hub speaks plain http; there is no TLS in the listener. The intended deployment is the hub and the agent pods in one
namespace, with a NetworkPolicy admitting only agent pods to the hub port. The code does not require or enforce this. The bearer
token and message text cross that network unencrypted. The hub does not check the source of a request beyond the token.

What the code does accept as an agent (`bin/cloud_channel.py`): a token whose username is `system:serviceaccount:<namespace>:<name>`
(prefix `system:serviceaccount:`) with the namespace exactly `fleet-agents` (the `NAMESPACE` constant) and a name that is a valid
agent name. Any ServiceAccount in that namespace is accepted, wherever the request comes from; any other namespace is refused.

## Take down

The code has no shutdown command or signal handler. Stop the hub by stopping its process (for example the pod or container
running it); it holds no state on disk, so the remembered dedupe keys are lost (see above; the least recently seen key is forgotten first, a repeat refreshes a key). The pod command is one-shot and
needs nothing to take down. To stop one agent receiving, remove its entry from `agents.json` (read at each message: new
messages get 404). To cut one agent off in secrets mode, remove its entry from `secrets.json` (read at each request).
