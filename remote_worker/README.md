# remote_worker

Launches a coding worker onto a remote execution service through a **transport**. It is its own package: the switchboard script and the
pane, headless and pod launch paths do not import it and are unchanged. A caller imports it where it needs it.

```python
from remote_worker import launch_remote_worker, load_config, transport_from_config, register_transport, wait_for_end
from remote_worker.floor import FloorTransport

cfg = load_config(os.environ)   # FLEET_REMOTE_TRANSPORT and FLEET_REMOTE_LAND_COMMAND, both required
register_transport("floor", lambda: FloorTransport(account="examplestore", floor_id="f1", caller_id="fleet",
                                                   queue_sas_file="/path/queue-sas", container_sas_file="/path/container-sas",
                                                   git_base="https://git.example"))
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
| storage account, floor id, caller id, git base | `FloorTransport.__init__` | `ConfigurationTests.test_unsafe_settings_are_refused`, `test_names_are_configuration` |
| the two SAS files (paths are configuration; regular, small, not group or other readable) | `floor.load_sas_file` | `ConfigurationTests.test_sas_file_rules` |
| `POST` to the command queue (add only, `messagettl=86400` on every message) | `FloorTransport._add_message` | `CarrierTests.test_every_message_carries_the_24_hour_ttl` |
| `PUT` of a payload blob (a command over 48 KiB) | `FloorTransport._put_blob` | `PayloadTests` |
| `GET` of `state.json` (conditional) and `msg/<session>.json` | `FloorTransport._get_blob` | `StateTests` |
| service answers (state, landed sha, JSON, acks) | `FloorTransport.status`, `_read_state` | `StateTests.test_bad_state_answers_are_refused` |
| handle of another transport, message over 256 KiB | `FloorTransport` | `StopAndSessionTests.test_foreign_handle...`, `PayloadTests.test_over_256_kib...` |

The land command is text the worker is told to run as its last step; this package never runs it. Neither SAS token appears in a log, error, repr or
return value; each goes only to its own fixed host (the queue token to the queue, the container token to the blob container).

## Floor transport (`floor.py`): the outbound-only carrier, interface v1

Neither side accepts a connection. Commands are added to a storage queue; state is read back from a blob container.

| Operation | What the carrier does |
|---|---|
| start | add a `launch` command, `seq` 1. The session id and the command id come from the worker name, so a repeated start (even from a new process) is the same launch |
| send | add a `send` command, `seq` + 1. A repeated `message_id` reuses its `cmd_id` and sends nothing more |
| stop | add a `retire` command; nothing if the session is known to have ended |
| status | conditional `GET` of `state.json`, take the session; read the `acks`; add again, unchanged, any command with no ack after 60 s |
| last_message | `GET` of `msg/<session>.json` ("" when the blob does not exist) |

Details a caller needs:

- Numbering is kept in memory. A new process that wants to send to a session an earlier one started calls `adopt(handle, next_seq)`.
- A command that could not be added is kept and added again (same `cmd_id`, same `seq`) before the next command of that session, so no number is skipped.
- A command over 48 KiB has its `brief`, `text` and `env` moved to the blob `payload/<cmd_id>.json`, written first; the message carries its name.
- `command_result(cmd_id)` gives `pending`, `applied`, `duplicate` or `rejected:<code>`, as of the last `status` call. A rejected launch makes `status` raise.
- `floor_alive()` is false when `state.json` is older than 90 s: Floor is unreachable. Do not retire or relaunch on that alone.
- The land command is Floor's own configuration (the project's land tool); it is not a request field. The brief names the configured command, and the
  service detects the end when the worker's message ends with `landed <sha>`.
- The caller never deletes from the container or reads the queue (its tokens do not allow it).
- `messagettl=86400` is set by the sender on every message; the queue's default is 7 days.

## Tests

`python3 -m unittest remote_worker.tests.test_remote_worker remote_worker.tests.test_floor_carrier` (standard library only, no network, a fake HTTP layer). These
files are not yet a step of the CI workflow.
