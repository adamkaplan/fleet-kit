# remote_worker

Launches a coding worker onto a remote execution service through a **transport**. It is its own package: the switchboard script and the
pane, headless and pod launch paths do not import it and are unchanged. A caller imports it where it needs it.

```python
from remote_worker import launch_remote_worker, load_config, transport_from_config, register_transport, wait_for_end
from remote_worker.floor import FloorTransport

cfg = load_config(os.environ)   # FLEET_REMOTE_TRANSPORT and FLEET_REMOTE_LAND_COMMAND, both required
register_transport("floor", lambda: FloorTransport(base_url=..., token_env="MY_TOKEN_VAR", git_base="https://git.example"))
t = transport_from_config(cfg)
h = launch_remote_worker(t, name="widget-1", repo="acme/widget-api", branch="work/widget-1", task="...", land_command=cfg.land_command)
status = wait_for_end(t, h, timeout=3600)   # status.state "done" carries landed_sha
```

## The transport interface (`transport.py`)

Six methods; each raises `TransportError(reason, retryable)` and nothing else. A transport owns every outside call.

| Method | Meaning |
|---|---|
| `start(spec) -> WorkerHandle` | create the worker and give it `spec.brief`; idempotent per `spec.name` |
| `send(handle, text, message_id=None)` | one more message; the same `message_id` twice delivers once |
| `status(handle) -> WorkerStatus` | read only: `state`, `busy`, `last_update`, `landed_sha` |
| `last_message(handle) -> str` | read only: the worker's last reply (untrusted text) |
| `stop(handle, reason)` | end the worker; safe to repeat |
| `close()` | release the transport's own resources |

States: `queued`, `running`, `idle`, `done`, `failed`, `retired`; the last three are terminal.

## Inputs and outside calls

| Input or call | Checked by | Test |
|---|---|---|
| worker name, `OWNER/NAME` repo, branch, task, idle and hard seconds | `launch.validate_spec`, before any call | `SpecTests.test_each_bad_input_is_refused_before_any_call` |
| land command (configuration `FLEET_REMOTE_LAND_COMMAND`, no default, one printable line) | `load_config`, `validate_spec` | `ConfigTests` |
| transport name (`FLEET_REMOTE_TRANSPORT`) | `transport_from_config` | `ConfigTests.test_registry` |
| base URL (https, or http to loopback), git base (https), token variable | `FloorTransport.__init__` | `FloorTests.test_construction_refuses_unsafe_settings` |
| HTTP request to `<base>/v1/...` with a bearer token (the only network call) | `FloorTransport._call` | `FloorTests` (an in-memory HTTP function) |
| service answers (state, session id, landed sha, size, JSON) | `FloorTransport` | `FloorTests.test_bad_answers_are_refused` |
| message size, handle of another transport | `FloorTransport` | `FloorTests.test_oversize_message...`, `test_foreign_handle...` |

The land command is text the worker is told to run as its last step; this package never runs it. The token is read once from the named
environment variable and appears in no log, error or return value.

## Floor transport and the service's interface v0

`floor.py` follows the service's published transport interface v0. Where the two differ:

- The service takes the repository as a full git URL and a `ref`; this package takes `OWNER/NAME` and a branch, and the transport joins `git_base`.
- The service keeps its own land command and does not take one in a launch request; here it is configuration and goes to the worker in the brief.
  The service detects the end when the worker's message ends with `landed <sha>` (state `done`, `landed_sha` set), so the configured command must be
  one whose result the worker reports in that form.
- A repeated `client_key` (the worker name) returns the first session; `send` with a `message_id` is idempotent. Both are used.
- A send to a finished session is refused by the service (409); it surfaces as a non-retryable `TransportError`. A 429 (busy) is retryable.
- The service answers 404 for a session that is another caller's, so `stop` cannot tell it from one already gone and treats both as stopped.
- The service's retire takes no reason; `stop` does not send it.
- `idle` and `queued` are states of the service; the interface carries them as they are.

## Tests

`python3 -m unittest remote_worker.tests.test_remote_worker` (standard library only, no network, about a millisecond). This file is not yet a
step of the CI workflow.
