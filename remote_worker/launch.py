"""Launch a remote worker through a transport.

`launch_remote_worker` checks every input, builds the brief (the closing step is the configured land command), starts the worker through the
transport and returns its handle. It runs no command itself and holds no credential: the land command is text handed to the worker, and the
transport is the only outside call.
"""
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Optional

from .transport import STATES, TERMINAL_STATES, Transport, TransportError, WorkerHandle, WorkerSpec, WorkerStatus

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
MAX_TASK = 100_000
MAX_LAND = 500
MAX_SECONDS = 7 * 24 * 3600
ENV_TRANSPORT = "FLEET_REMOTE_TRANSPORT"
ENV_LAND = "FLEET_REMOTE_LAND_COMMAND"


class SpecError(ValueError):
    """An input was refused before any call was made."""


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    transport: str        # the name a transport factory was registered under
    land_command: str     # the command a finished worker runs; no default: an unset value refuses the launch


def load_config(env: Mapping[str, str]) -> Config:
    """Read the two settings from a mapping (the caller passes `os.environ` or its own settings). Both are required."""
    transport = (env.get(ENV_TRANSPORT) or "").strip()
    land = (env.get(ENV_LAND) or "").strip()
    if not transport:
        raise ConfigError("%s is not set" % ENV_TRANSPORT)
    if not land:
        raise ConfigError("%s is not set" % ENV_LAND)
    _check_land(land, ConfigError)
    return Config(transport=transport, land_command=land)


def _check_land(land: str, exc):
    if len(land) > MAX_LAND or any(ord(c) < 32 or ord(c) == 127 for c in land):
        raise exc("the land command must be one printable line of at most %d characters" % MAX_LAND)


def _opt_seconds(label: str, value: Optional[int]):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SECONDS:
        raise SpecError("%s must be a whole number of seconds from 1 to %d" % (label, MAX_SECONDS))


def build_brief(repo: str, branch: str, task: str, land_command: str) -> str:
    """The text the worker receives: setup, the task, then the closing step. The task is quoted as given, after the setup."""
    return (
        "Setup (do this first):\n"
        "- Work in a clone of the git repository %s, on the branch %s (create it from the default branch if it does not exist).\n"
        "- Never print, write or commit a credential.\n\n"
        "Task:\n%s\n\n"
        "When the work is finished and committed, run this command as the last step and report its result in one line:\n"
        "%s\n" % (repo, branch, task, land_command)
    )


def validate_spec(name: str, repo: str, branch: str, task: str, land_command: str, idle_seconds=None, hard_seconds=None, labels=()) -> WorkerSpec:
    """Check every input and return the spec. Raises SpecError naming the input (never its value for the task)."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise SpecError("name must match %s" % NAME_RE.pattern)
    if not isinstance(repo, str) or not REPO_RE.match(repo) or repo.endswith(".git") or ".." in repo:
        raise SpecError("repo must be OWNER/NAME")
    if not isinstance(branch, str) or not BRANCH_RE.match(branch) or ".." in branch or branch.endswith(("/", ".lock")):
        raise SpecError("branch is not a plain branch name")
    if not isinstance(task, str) or not task.strip() or len(task) > MAX_TASK:
        raise SpecError("task must be non-empty text of at most %d characters" % MAX_TASK)
    if not isinstance(land_command, str) or not land_command.strip():
        raise SpecError("land command is empty")
    _check_land(land_command, SpecError)
    _opt_seconds("idle_seconds", idle_seconds)
    _opt_seconds("hard_seconds", hard_seconds)
    pairs = tuple((str(k), str(v)) for k, v in dict(labels).items()) if isinstance(labels, Mapping) else tuple(labels)
    return WorkerSpec(name=name, repo=repo, branch=branch, task=task, brief=build_brief(repo, branch, task, land_command.strip()),
                      land_command=land_command.strip(), idle_seconds=idle_seconds, hard_seconds=hard_seconds, labels=pairs)


def launch_remote_worker(transport: Transport, name: str, repo: str, branch: str, task: str, land_command: str,
                         idle_seconds=None, hard_seconds=None, labels=()) -> WorkerHandle:
    """Validate, then start. Raises SpecError before any call; a TransportError from `start` passes through unchanged.
    A handle whose name does not match the request is a transport bug: the worker is stopped and the launch fails."""
    spec = validate_spec(name, repo, branch, task, land_command, idle_seconds, hard_seconds, labels)
    handle = transport.start(spec)
    if not isinstance(handle, WorkerHandle) or handle.name != spec.name:
        if isinstance(handle, WorkerHandle):
            try:
                transport.stop(handle, "launch: transport returned another worker's name")
            except TransportError:
                pass
        raise TransportError("the transport returned a handle that is not this worker's", retryable=False)
    return handle


def wait_for_end(transport: Transport, handle: WorkerHandle, timeout: float, poll: float = 5.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> WorkerStatus:
    """Poll `status` until a terminal state or `timeout` seconds. On timeout returns the last status (not terminal); a retryable
    TransportError counts as one missed poll, any other propagates."""
    deadline = clock() + timeout
    last = WorkerStatus(state="queued", busy=True)
    while True:
        try:
            last = transport.status(handle)
        except TransportError as e:
            if not e.retryable:
                raise
        if last.state in TERMINAL_STATES or last.state not in STATES:
            return last
        if clock() + poll > deadline:
            return last
        sleep(poll)


_FACTORIES: Dict[str, Callable[[], Transport]] = {}


def register_transport(name: str, factory: Callable[[], Transport]) -> None:
    """Make a transport available by name. A name registered twice is refused: no silent replacement."""
    if not NAME_RE.match(name):
        raise ConfigError("transport name must match %s" % NAME_RE.pattern)
    if name in _FACTORIES:
        raise ConfigError("transport %r is already registered" % name)
    _FACTORIES[name] = factory


def transport_from_config(config: Config) -> Transport:
    try:
        return _FACTORIES[config.transport]()
    except KeyError:
        raise ConfigError("no transport registered as %r" % config.transport) from None
