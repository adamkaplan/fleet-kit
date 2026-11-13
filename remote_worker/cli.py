"""The command line of the remote-worker package: `python3 -m remote_worker <launch|status|last-message|send|retire> ...`.

This is the code behind the switchboard's `launch --remote` route (see WIRING.md) and works alone. Everything it needs is in one JSON
configuration file whose path is in `FLEET_REMOTE_CONFIG` (or `--config`):

    {"backend": "floor", "land_command": "<the command a finished worker runs>",
     "floor": {"account": "examplestore", "floor_id": "f1", "caller_id": "fleet", "queue_sas_file": "/path/q", "container_sas_file": "/path/c",
               "git_base": "https://git.example"},
     "repo_token_command": ["mint-token", "acme/widget-api", "--fd", "{fd}"], "repo_token_env": "REPO_TOKEN", "state_dir": "/home/user/.remote-workers"}

Inputs and outside calls (each has a test in tests/switchboard/test_remote_cli.py):
- the config file (a small regular file; keys and types checked; no default for the land command);
- the token command: an argument list run with no shell, once per launch, stdin closed, a time limit. One argument carries the placeholder `{fd}`,
  replaced by the number of a pipe the command writes the token to (its standard output and error are discarded); the token must be plain token
  characters. It goes only into the launch command's `env`; it is never stored, printed, logged or put in an error;
- the task and message text files (a path or `-` for standard input, at most 100 000 bytes);
- the worker record: one small file per worker in `state_dir` (name, session id, next command number: no secret), written 0600 by rename;
- the transport's own calls (see floor.py).
Output is one JSON line on standard output; a refusal is one line on standard error and exit code 1.
"""
import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Dict, Mapping, Optional

from .launch import NAME_RE, ConfigError, SpecError, launch_remote_worker, _check_land
from .transport import Transport, TransportError, WorkerHandle

ENV_CONFIG = "FLEET_REMOTE_CONFIG"
MAX_CONFIG = 65536
MAX_TEXT_FILE = 100_000
MAX_RECORD = 4096
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.~+/=:-]{8,4096}$")
TOKEN_SECONDS = 30
FD_PLACEHOLDER = "{fd}"
DEFAULT_TOKEN_ENV = "REPO_TOKEN"
FLOOR_KEYS = ("account", "floor_id", "caller_id", "queue_sas_file", "container_sas_file", "git_base")


class CliError(Exception):
    """A refusal: one line, never carrying a token or a task text."""


def _read_small(path: str, limit: int, what: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise CliError("%s cannot be opened" % what) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise CliError("%s is not a regular file of at most %d bytes" % (what, limit))
        return os.read(fd, limit + 1)
    finally:
        os.close(fd)


def load_file_config(path: str) -> dict:
    """Read and check the configuration file. Returns the checked mapping; raises CliError."""
    try:
        data = json.loads(_read_small(path, MAX_CONFIG, "the configuration file").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CliError("the configuration file is not JSON") from None
    if not isinstance(data, dict):
        raise CliError("the configuration file must hold one JSON object")
    backend = data.get("backend")
    if not isinstance(backend, str) or not NAME_RE.match(backend):
        raise CliError("configuration: backend is missing")
    land = data.get("land_command")
    if not isinstance(land, str) or not land.strip():
        raise CliError("configuration: land_command is not set")
    try:
        _check_land(land.strip(), ConfigError)
    except ConfigError as e:
        raise CliError("configuration: %s" % e) from None
    carrier = data.get(backend)
    if not isinstance(carrier, dict):
        raise CliError("configuration: the %s settings are missing" % backend)
    if backend == "floor" and any(not isinstance(carrier.get(k), str) or not carrier.get(k) for k in FLOOR_KEYS):
        raise CliError("configuration: the floor settings are incomplete (%s)" % ", ".join(FLOOR_KEYS))
    cmd = data.get("repo_token_command")
    if cmd is not None and (not isinstance(cmd, list) or not cmd or any(not isinstance(a, str) or not a or "\x00" in a for a in cmd)):
        raise CliError("configuration: repo_token_command must be a non-empty list of strings")
    env_name = data.get("repo_token_env", DEFAULT_TOKEN_ENV)
    if not isinstance(env_name, str) or not re.match(r"^[A-Z][A-Z0-9_]{0,63}$", env_name):
        raise CliError("configuration: repo_token_env is not an upper-case name")
    state_dir = data.get("state_dir")
    if not isinstance(state_dir, str) or not os.path.isabs(state_dir):
        raise CliError("configuration: state_dir must be an absolute path")
    return data


def mint_token(argv, run: Callable = subprocess.run, seconds: int = TOKEN_SECONDS) -> str:
    """Run the configured token command once and return its token, read from a pipe the command writes to. Nothing of its output is ever put in an error.

    The command names the pipe with the placeholder `{fd}` in one of its arguments (for example `--fd {fd}`): it is replaced by the number of the
    write end, which the child inherits. The command prints nothing on standard output; its standard output and error are discarded."""
    if not any(FD_PLACEHOLDER in a for a in argv):
        raise CliError("the token command has no %s placeholder" % FD_PLACEHOLDER)
    r, w = os.pipe()
    try:
        os.set_inheritable(w, True)
        cmd = [a.replace(FD_PLACEHOLDER, str(w)) for a in argv]
        try:
            done = run(cmd, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=seconds,
                       pass_fds=(w,), close_fds=True)
        except subprocess.TimeoutExpired:
            raise CliError("the token command ran over %d seconds" % seconds) from None
        except OSError:
            raise CliError("the token command could not be run") from None
        if done.returncode != 0:
            raise CliError("the token command failed (exit %d)" % done.returncode)
        os.close(w)
        w = -1
        os.set_blocking(r, False)
        try:
            data = os.read(r, 8192)
        except BlockingIOError:
            data = b""
    finally:
        os.close(r)
        if w >= 0:
            os.close(w)
    token = data.decode("utf-8", "replace").strip()
    if not TOKEN_RE.match(token):
        raise CliError("the token command did not deliver a plain token")
    return token


class Records:
    """One small file per worker in the state directory: name, session id, next command number. No secret."""

    def __init__(self, directory: str):
        self.dir = directory

    def _path(self, name: str) -> str:
        if not NAME_RE.match(name):
            raise CliError("not a worker name")
        return os.path.join(self.dir, name + ".json")

    def load(self, name: str) -> Optional[dict]:
        path = self._path(name)
        if not os.path.lexists(path):
            return None
        try:
            rec = json.loads(_read_small(path, MAX_RECORD, "the worker record").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise CliError("the worker record is not JSON") from None
        if not isinstance(rec, dict) or rec.get("name") != name or not isinstance(rec.get("worker_id"), str) \
                or not isinstance(rec.get("next_seq"), int) or isinstance(rec.get("next_seq"), bool) or rec["next_seq"] < 2:
            raise CliError("the worker record is not valid")
        return rec

    def save(self, name: str, handle: WorkerHandle, next_seq: int, repo: str = "", branch: str = "") -> None:
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        rec = {"name": name, "worker_id": handle.worker_id, "transport": handle.transport, "next_seq": next_seq, "repo": repo, "branch": branch}
        fd, tmp = tempfile.mkstemp(prefix=".rec-", dir=self.dir)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(rec))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path(name))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def make_floor(settings: Mapping[str, str]) -> Transport:
    from .floor import FloorTransport
    return FloorTransport(account=settings["account"], floor_id=settings["floor_id"], caller_id=settings["caller_id"],
                          queue_sas_file=settings["queue_sas_file"], container_sas_file=settings["container_sas_file"], git_base=settings["git_base"])


BACKENDS: Dict[str, Callable[[Mapping[str, str]], Transport]] = {"floor": make_floor}


def _read_text(path: str, stdin) -> str:
    if path == "-":
        raw = stdin.read(MAX_TEXT_FILE + 1)
        raw = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(raw) > MAX_TEXT_FILE:
            raise CliError("the text is over %d bytes" % MAX_TEXT_FILE)
    else:
        raw = _read_small(path, MAX_TEXT_FILE, "the text file")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CliError("the text is not UTF-8") from None


def _seconds(text: Optional[str], label: str) -> Optional[int]:
    if text is None:
        return None
    if not re.fullmatch(r"[0-9]{1,9}", text):
        raise CliError("%s must be a whole number of seconds" % label)
    return int(text)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="remote_worker", description="Launch and tend a worker on a remote execution service.")
    p.add_argument("--config", help="the configuration file (default: $%s)" % ENV_CONFIG)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("launch", help="start a worker")
    a.add_argument("name")
    a.add_argument("--repo", required=True, help="OWNER/NAME")
    a.add_argument("--branch", required=True)
    a.add_argument("--task-file", required=True, help="the task text: a file, or - for standard input")
    a.add_argument("--idle", help="stop after this many idle seconds")
    a.add_argument("--hard", help="stop after this many seconds in all")
    for cmd, helptext in (("status", "the worker's state"), ("last-message", "the worker's last reply"), ("retire", "stop the worker")):
        sub.add_parser(cmd, help=helptext).add_argument("name")
    s = sub.add_parser("send", help="send the worker one more message")
    s.add_argument("name")
    s.add_argument("--text-file", required=True, help="a file, or - for standard input")
    s.add_argument("--message-id", help="a repeat of the same id delivers nothing more")
    return p


def _call(transport, method: str, *args):
    """A method only some transports have (the numbering and the heartbeat of a queue carrier): None when this transport has no such method."""
    fn = getattr(transport, method, None)
    return fn(*args) if fn else None


def run(args, config: dict, stdin, backends: Mapping[str, Callable], runner: Callable) -> dict:
    backend = config["backend"]
    if backend not in backends:
        raise CliError("no backend named %s" % backend)
    records = Records(config["state_dir"])
    transport = backends[backend](config[backend])
    try:
        name = args.name
        if args.command == "launch":
            task = _read_text(args.task_file, stdin)
            env = ()
            if config.get("repo_token_command"):
                env = ((config.get("repo_token_env", DEFAULT_TOKEN_ENV), mint_token(config["repo_token_command"], runner)),)
            handle = launch_remote_worker(transport, name, args.repo, args.branch, task, config["land_command"],
                                          _seconds(args.idle, "--idle"), _seconds(args.hard, "--hard"), env=env)
            before = records.load(name)
            records.save(name, handle, max(_call(transport, "next_seq", handle) or 2, before["next_seq"] if before else 2), args.repo, args.branch)
            return {"name": name, "session_id": handle.worker_id, "state": "queued"}
        rec = records.load(name)
        if rec is None:
            raise CliError("no record of a worker named %s" % name)
        handle = WorkerHandle(name=name, worker_id=rec["worker_id"], transport=rec["transport"])
        _call(transport, "adopt", handle, rec["next_seq"])
        if args.command == "status":
            st = transport.status(handle)
            return {"name": name, "state": st.state, "busy": st.busy, "last_update": st.last_update, "landed_sha": st.landed_sha,
                    "floor_alive": _call(transport, "floor_alive")}
        if args.command == "last-message":
            return {"name": name, "text": transport.last_message(handle)}
        if args.command == "send":
            transport.send(handle, _read_text(args.text_file, stdin), args.message_id)
        else:
            transport.stop(handle, "retired by the caller")
        records.save(name, handle, _call(transport, "next_seq", handle) or rec["next_seq"], rec.get("repo", ""), rec.get("branch", ""))
        return {"name": name, "ok": True}
    finally:
        transport.close()


def main(argv=None, environ: Optional[Mapping[str, str]] = None, stdin=None, stdout=None, stderr=None,
         backends: Optional[Mapping[str, Callable]] = None, runner: Callable = subprocess.run) -> int:
    environ = os.environ if environ is None else environ
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    try:
        path = args.config or environ.get(ENV_CONFIG)
        if not path:
            raise CliError("no configuration file: set %s or pass --config" % ENV_CONFIG)
        result = run(args, load_file_config(path), stdin, backends if backends is not None else BACKENDS, runner)
    except (CliError, SpecError, ConfigError) as e:
        stderr.write("remote_worker: %s\n" % e)
        return 1
    except TransportError as e:
        stderr.write("remote_worker: %s%s\n" % (e.reason, " (retryable)" if e.retryable else ""))
        return 1
    stdout.write(json.dumps(result) + "\n")
    return 0
