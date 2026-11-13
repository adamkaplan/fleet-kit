"""The transport interface: the only thing a remote-worker launch needs from the remote service.

A transport is any object with the six methods of `Transport`. It owns every outside call (network, credentials, the service's own protocol);
nothing else in this package makes one. A transport never prints, logs or returns a credential.
"""
from dataclasses import dataclass, field
from typing import Optional, Protocol, Tuple, runtime_checkable

STATES = ("queued", "running", "idle", "done", "failed", "retired")   # idle: between rounds, waiting for a message
TERMINAL_STATES = ("done", "failed", "retired")


class TransportError(Exception):
    """A transport call failed. `retryable` says whether the same call may be tried again; `reason` is one line with no secret."""

    def __init__(self, reason: str, retryable: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


@dataclass(frozen=True)
class WorkerSpec:
    """What to run. Built and checked by `launch.validate_spec`; a transport may rely on every field being valid."""
    name: str                     # the worker's name: lower-case letters, digits, `-` and `_`
    repo: str                     # `OWNER/NAME` of the git repository the worker serves
    branch: str                   # the branch the worker's work is committed on
    task: str                     # the task text, as the person wrote it
    brief: str                    # the full text sent to the worker: setup, task and the closing step (built by `build_brief`)
    land_command: str             # the command the worker runs when the work is finished: configuration, never fixed in code
    idle_seconds: Optional[int] = None   # stop after this long with no activity (None: the service's default)
    hard_seconds: Optional[int] = None   # stop after this long in all (None: the service's default)
    labels: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)   # free key/value pairs the transport may record
    env: Tuple[Tuple[str, str], ...] = field(default_factory=tuple, repr=False)   # variables for the worker's tools (a short-lived token): secret, never in a repr or a log


@dataclass(frozen=True)
class WorkerHandle:
    """A started worker. `worker_id` is the transport's own id for it and is opaque to this package."""
    name: str
    worker_id: str
    transport: str


@dataclass(frozen=True)
class WorkerStatus:
    state: str                    # one of STATES
    busy: bool = False            # true while queued or running
    last_update: str = ""         # RFC 3339 time of the worker's last activity ("" when unknown); it moves while the worker works
    landed_sha: str = ""          # 40 hex characters once state is `done`, else ""


@runtime_checkable
class Transport(Protocol):
    """The six calls. Each raises `TransportError` on failure and nothing else (any other exception is a bug in the transport)."""

    name: str   # the transport's own short name, recorded in the handle

    def start(self, spec: WorkerSpec) -> WorkerHandle:
        """Create the worker and give it `spec.brief`. Return once the service has accepted it, not once the work is done.
        Idempotent per `spec.name`: a repeated start of the same name returns the first worker and starts nothing, so a retry after a lost answer is safe."""

    def send(self, handle: WorkerHandle, text: str, message_id: Optional[str] = None) -> None:
        """Deliver one more message to a live worker. With a `message_id`, a repeat of the same id delivers nothing more (a retry is safe)."""

    def status(self, handle: WorkerHandle) -> WorkerStatus:
        """Read-only: the worker's state, whether it is busy, its last activity time and the landed sha."""

    def last_message(self, handle: WorkerHandle) -> str:
        """Read-only: the worker's last reply text ("" before the first). It is text the worker chose, so untrusted."""

    def stop(self, handle: WorkerHandle, reason: str) -> None:
        """End the worker and release what it holds. Safe to repeat: stopping a worker that is gone succeeds."""

    def close(self) -> None:
        """Release the transport's own resources (connections). Safe to repeat."""
