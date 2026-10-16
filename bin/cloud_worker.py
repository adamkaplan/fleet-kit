"""Remote (cloud) workers for the switchboard: slice C of ask #323, built to docs/cloud-worker-interface.md.

A cloud worker is an OpenCode server in a pod. The switchboard keeps one endpoint record per worker (a private JSON file in its state
directory) and reaches the worker through the same commands as a local one (`launch --cloud`, `send`, `read`, `status`, `retire`). The pod is
created and stopped by the launcher program (slice B, `cloud-worker`), which this module only calls.

New logic lives here; bin/fleet-switchboard holds thin hooks that call into it. It is loaded by path (see `load_cloud_worker` there) and
`bind(globals())` hands it the switchboard's own names, so nothing here imports the script.

Security: the server password goes only to the endpoint a record names (a validated `https` URL, or `http` to a loopback address), is read only
from a private file the switchboard itself wrote, and is never logged, printed, audited or put in an error. A request to any other URL is refused
before a byte is sent.
"""
import base64
import contextlib
import http.client
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

H = {}   # the switchboard's own namespace, set by bind()

LAUNCHER_ENV = "FLEET_CLOUD_WORKER_BIN"   # the launcher program; default `cloud-worker` on PATH
DEFAULT_LAUNCHER = "cloud-worker"
CLOUD_DIR = "cloud"
DEFAULT_DIRECTORY = "/workspace"          # the worker's working directory inside the pod (OPEN in the interface; overridable with --dir)
RECORD_MAX = 8192
PASSWORD_MAX = 512
LAUNCH_SECONDS = 900                      # `create` returns only once the worker answers
STOP_SECONDS = 600
STATUS_SECONDS = 15
MESSAGE_MAX = 20000
RECORD_FIELDS = ("worker", "url", "runtime_id", "idle_deadline", "hard_deadline")
EXIT_REASONS = {2: "bad usage or unknown worker", 3: "the cloud refused (quota, identity or cap)", 4: "timed out waiting for the worker",
                5: "the spend cap is reached", 6: "refused (the end hook had not run, or the extension is past the maximum)"}


class CloudError(Exception):
    """A cloud-worker step that did not finish; the message says why in one line."""


def bind(namespace):
    """Receive the switchboard's globals (its classes and helpers). Called by the hook that loads this module."""
    H.clear()
    H.update(namespace)
    _classes.cache = None
    return sys.modules[__name__]


def _one(text, limit=300):
    return H["_one_line"](text, limit)


# ---------------------------------------------------------------- the endpoint record

def cloud_dir(state):
    path = Path(state) / CLOUD_DIR
    if not path.is_dir():
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    found = os.lstat(str(path))
    if stat.S_ISLNK(found.st_mode) or found.st_uid != os.getuid():
        raise CloudError("the cloud directory is not the user's own")
    os.chmod(str(path), 0o700)
    return path


def valid_name(name):
    return isinstance(name, str) and bool(H["NAME_RE"].match(name))


def record_path(state, name):
    """The record file of a worker; the path is built only from the validated name."""
    if not valid_name(name):
        raise CloudError("not a worker name: %r" % (name,))
    return cloud_dir(state) / (name + ".json")


def password_path(state, name):
    if not valid_name(name):
        raise CloudError("not a worker name: %r" % (name,))
    return cloud_dir(state) / (name + ".password")


def endpoint_problem(url):
    """Why `url` may not be an endpoint, or None: `https` to a host, or plain `http` only to a loopback address (a tunnel); no credentials in it."""
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except (ValueError, AttributeError, TypeError):
        return "not a URL"
    if parts.username or parts.password:
        return "the URL carries credentials"
    if not host or parts.path not in ("", "/") or parts.query or parts.fragment:
        return "the URL must be a bare scheme://host[:port]"
    if parts.scheme == "https":
        return None
    if parts.scheme == "http" and host in ("127.0.0.1", "::1") and port:
        return None
    return "the URL must be https (plain http only to a loopback address)"


def _write_private(path, text):
    temp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(str(temp), str(path))
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise


def _read_private(path, limit):
    """The text of a private regular file (ours, mode 0600, not a symlink), at most `limit` bytes; OSError/ValueError otherwise."""
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        found = os.fstat(handle.fileno())
        if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid() or found.st_mode & 0o077:
            raise ValueError("not private to this user")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("too large")
    return raw.decode("utf-8")


def load_record(state, name):
    """The worker's record, or None when there is none. A record that exists but is unusable raises CloudError (never read as 'not a cloud worker')."""
    path = record_path(state, name)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        record = json.loads(_read_private(path, RECORD_MAX))
    except (OSError, ValueError) as err:
        raise CloudError("the record of %s cannot be read (%s)" % (name, _one(str(err), 80))) from None
    if not isinstance(record, dict) or record.get("worker") != name or any(not isinstance(record.get(k), str) for k in ("url", "runtime_id")):
        raise CloudError("the record of %s is not a worker record" % name)
    problem = endpoint_problem(record["url"])
    if problem:
        raise CloudError("the record of %s names an endpoint that is refused: %s" % (name, problem))
    return record


def save_record(state, record):
    _write_private(record_path(state, record["worker"]), json.dumps(record, indent=1, sort_keys=True) + "\n")


def remove_record(state, name):
    for path in (record_path(state, name), password_path(state, name)):
        try:
            os.unlink(str(path))
        except FileNotFoundError:
            pass


def list_records(state):
    base = Path(state) / CLOUD_DIR
    if not base.is_dir():
        return []
    found = []
    for path in sorted(base.glob("*.json")):
        name = path.name[:-5]
        if valid_name(name):
            try:
                record = load_record(state, name)
            except CloudError as err:
                found.append({"worker": name, "problem": str(err)})
                continue
            if record is not None:
                found.append(record)
    return found


def route(name):
    """The record of `name` when it is a cloud worker, else None. Used by the hooks of send, read and retire; a broken record raises."""
    if not valid_name(name):
        return None
    base = H["state_dir_path"]()
    if not (base / CLOUD_DIR / (name + ".json")).exists():
        return None
    return load_record(H["ensure_state_dir"](), name)


# ---------------------------------------------------------------- the transport: V2Http and V2Client, one endpoint per worker

def _classes():
    """(RemoteHttp, RemoteV2): the switchboard's V2Http and V2Client bound to one record. Built once per bind()."""
    if _classes.cache is not None:
        return _classes.cache
    V2Http, V2Client, V2Error, V2Unreachable = H["V2Http"], H["V2Client"], H["V2Error"], H["V2Unreachable"]
    stream_idle = H["V2_STREAM_IDLE_SECONDS"]

    class RemoteHttp(V2Http):
        """V2Http for ONE endpoint named by a record. The password is read from the private file the switchboard wrote and goes only to that
        endpoint; a request for any other URL (an absolute or network-path target) is refused before anything is sent."""

        def __init__(self, record, password_file, timeout, connect=None):
            problem = endpoint_problem(record["url"])
            if problem:
                raise CloudError("endpoint refused: %s" % problem)
            super().__init__(password_file, timeout, connect=connect)
            parts = urllib.parse.urlsplit(record["url"])
            self.host = parts.hostname
            self.port = parts.port or 443
            if connect is None:
                self.connect = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection

        def _load(self):
            try:
                password = _read_private(self.file, PASSWORD_MAX + 2).rstrip("\n")
                if not 0 < len(password) <= PASSWORD_MAX or re.search(r"[\x00-\x1f\x7f]", password):
                    raise ValueError("no usable password")
            except (OSError, ValueError):
                raise V2Unreachable("no usable password file for the remote worker") from None
            token = base64.b64encode(("opencode:" + password).encode("utf-8")).decode("ascii")
            self._info = (self.host, self.port, "Basic " + token)

        @staticmethod
        def _local_target(target):
            if not isinstance(target, str) or not target.startswith("/") or target.startswith("//") or re.search(r"[\x00-\x20\x7f]", target):
                raise V2Error("refused", None, "a remote worker is called by path only; a URL is not accepted")

        def request(self, method, target, body=None):
            self._local_target(target)
            return super().request(method, target, body)

        def stream_lines(self, path, idle_seconds=stream_idle):
            self._local_target(path)
            return super().stream_lines(path, idle_seconds)

    def _no_runner(*_args, **_kwargs):
        raise AssertionError("a remote worker never runs a local program")

    class RemoteV2(V2Client):
        """A V2Client whose every call goes to the record's endpoint: there is NO fallback to the local OpenCode CLI (it would talk to the wrong service)."""

        def __init__(self, record, password_file, timeout=20, connect=None, **kwargs):
            http_route = RemoteHttp(record, password_file, timeout, connect=connect)
            super().__init__("remote:" + record["worker"], {}, _no_runner, timeout, http=http_route, **kwargs)
            self.record = record

        def _cli(self, operation, params, data):
            raise V2Error("unavailable", None, "the remote worker %s does not answer (no local fallback for %s)" % (self.record["worker"], operation))

    _classes.cache = (RemoteHttp, RemoteV2)
    return _classes.cache


_classes.cache = None


def remote_v2(state, record, timeout=20, connect=None):
    return _classes()[1](record, password_path(state, record["worker"]), timeout, connect=connect)


# ---------------------------------------------------------------- the launcher (slice B): one program, called by argv

def launcher_argv():
    return [os.environ.get(LAUNCHER_ENV) or DEFAULT_LAUNCHER]


def run_launcher(runner, command, worker, options=(), timeout=LAUNCH_SECONDS, password_fd=None, cluster=None, secrets=()):
    """Run `cloud-worker <command> <worker> <options> --json`. Returns (exit code, parsed JSON or None, one-line stderr). A launcher that is missing
    or too slow is a CloudError with a plain reason. `cluster` (a Cluster) gives the launcher its login: a token FILE in the cluster's private
    directory, never a value in an argument or the environment. `secrets` [(name, value)] go to the launcher as per-run secrets through
    inherited pipes (`--run-secret-fd NAME=N`): a value is never in an argument, the environment or a file. `runner` is kept for the callers'
    signature; the launcher is started here so that an interrupt reaches IT (it cleans up what it made) before the interrupt goes on."""
    argv = launcher_argv() + [command, worker] + list(options) + ["--json"]
    pass_fds, feeds = [], []
    try:
        if password_fd is not None:
            argv += ["--password-fd", str(password_fd)]
            pass_fds.append(password_fd)
        for name, value in secrets:
            read_end, write_end = os.pipe()
            argv += ["--run-secret-fd", "%s=%d" % (name, read_end)]
            pass_fds.append(read_end)
            feeds.append((read_end, write_end, value))
        env = cluster.env() if cluster is not None else None
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     pass_fds=tuple(pass_fds), env=env, start_new_session=True)
        except OSError as err:
            raise CloudError("cannot run the launcher %s: %s" % (argv[0], err.strerror or err)) from None
    except BaseException:
        for read_end, write_end, _ in feeds:
            _close_quietly(read_end, write_end)
        raise
    for read_end, write_end, value in feeds:
        _close_quietly(read_end)
        threading.Thread(target=_feed, args=(write_end, value), daemon=True).start()
    try:
        out, err = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(child)
        child.communicate()
        raise CloudError("the launcher did not finish %s within %s s" % (command, timeout)) from None
    except BaseException:
        old = _ignore_signals()
        try:
            child.send_signal(signal.SIGINT)   # the launcher removes what it made, and says so
            child.communicate(timeout=180)
        except BaseException:
            _kill_group(child)
        finally:
            _restore_signals(old)
        raise
    try:
        body = json.loads(out) if (out or "").strip() else None
    except ValueError:
        body = None
    clean = cluster.clean if cluster is not None else (lambda text, limit=300: _scrub_lib()(text, (), limit))
    return child.returncode, body if isinstance(body, dict) else None, _one(clean(err or "", 2000))


def _close_quietly(*fds):
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _feed(fd, value):
    try:
        data = value.encode()
        while data:
            data = data[os.write(fd, data):]
    except OSError:
        pass
    finally:
        _close_quietly(fd)


def _kill_group(child):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except (OSError, ProcessLookupError):
            pass
        try:
            child.wait(5)
            return
        except subprocess.TimeoutExpired:
            continue


def _ignore_signals():
    if threading.current_thread() is not threading.main_thread():
        return {}
    return {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM)}


def _restore_signals(old):
    for sig, handler in old.items():
        signal.signal(sig, handler)


def _reason(code, note):
    return "%s%s" % (EXIT_REASONS.get(code, "exit %d" % code), (": " + note) if note else "")


def _audit(engine, event, name, detail):
    if engine is not None and getattr(engine, "audit", None) is not None:
        engine.audit.append(engine.actor, event, name, detail)


def _read_password(read_fd):
    chunks = b""
    while len(chunks) <= PASSWORD_MAX + 1:
        piece = os.read(read_fd, 256)
        if not piece:
            break
        chunks += piece
    text = chunks.decode("utf-8", "replace").strip()
    if not 0 < len(text) <= PASSWORD_MAX or re.search(r"[\x00-\x1f\x7f]", text):
        raise CloudError("the launcher did not hand over a usable server password")
    return text


# ---------------------------------------------------------------- launch

def cmd_launch(args, runner, now):
    """`launch NAME --cloud --repo OWNER/REPO --branch B --agent A`: create the pod, then the session and the brief against its endpoint."""
    LaunchError = H["LaunchError"]
    name = args.name
    if not valid_name(name):
        raise LaunchError("name must match %s, got %r" % (H["NAME_RE"].pattern, name))
    if name.lower() in H["RESERVED_AGENT_NAMES"] or name.startswith(H["LANE_NAME_PREFIX"]):
        raise LaunchError("the name %r is reserved" % name)
    if args.resume:
        raise LaunchError("--cloud and --resume do not combine: a cloud worker is re-reached through its record")
    if getattr(args, "headless", False):
        raise LaunchError("--cloud and --headless do not combine: a cloud worker has its own endpoint, a headless one runs in the local service")
    if not args.agent:
        raise LaunchError("--agent is required")
    repo = H["launch_repo"](args)
    branch = getattr(args, "branch", None)
    if not repo or not branch:
        raise LaunchError("--cloud needs --repo OWNER/REPO and --branch BRANCH")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch) or branch.startswith("-"):
        raise LaunchError("--branch is not a branch name: %r" % branch)
    engine, state, _ = H["build_engine"](runner, now, "cli")
    if load_record(state, name) is not None:
        raise LaunchError("a cloud worker named %s already exists (`%s retire %s` ends it)" % (name, H["PROG"], name))
    brief = H["read_brief"](args.brief_file, sys.stdin)
    options = ["--repo", repo, "--branch", branch]
    for flag, value in (("--idle", args.idle), ("--hard", args.hard), ("--shape", args.shape)):
        if value:
            options += [flag, str(value)]
    read_fd, write_fd = os.pipe()
    try:
        try:
            code, body, note = run_launcher(runner, "create", name, options, LAUNCH_SECONDS, password_fd=write_fd)
        except CloudError as err:
            raise LaunchError(str(err)) from None
        finally:
            os.close(write_fd)
        if code != 0:
            _audit(engine, "cloud.create_failed", name, {"exit": code, "reason": _one(note, 160)})
            raise LaunchError("the cloud worker was not created: %s" % _reason(code, note))
        try:
            password = _read_password(read_fd)
        except CloudError as err:
            _stop_after_failure(engine, runner, name, "launch: no password")
            raise LaunchError(str(err)) from None
    finally:
        os.close(read_fd)
    try:
        record = _record_from_create(name, body)
        _write_private(password_path(state, name), password + "\n")
        record["auth_ref"] = "file:" + str(password_path(state, name))   # the switchboard's own copy: its path never comes from the launcher
        record["repo"], record["branch"] = repo, branch
        if args.reports_to:
            record["reports_to"] = args.reports_to
        save_record(state, record)
    except (CloudError, OSError) as err:
        remove_record(state, name)
        _stop_after_failure(engine, runner, name, "launch: record not stored")
        raise LaunchError("the worker was created but its record could not be stored (%s); stop was requested" % _one(str(err), 100)) from None
    _audit(engine, "cloud.created", name, {"runtime_id": record["runtime_id"], "host": urllib.parse.urlsplit(record["url"]).hostname,
                                           "idle_deadline": record.get("idle_deadline"), "hard_deadline": record.get("hard_deadline")})
    fleet = H["fleet_record"](name, args.role, args.reports_to, None if args.issue is None else int(args.issue), args.charter, repo)
    v2 = remote_v2(state, record, engine.config["call_timeout_seconds"])
    try:
        session = v2.session_create(args.agent, name, args.dir or DEFAULT_DIRECTORY, {"fleet": fleet, "cloud": {"runtime_id": record["runtime_id"]}})["id"]
        record["session"] = session
        save_record(state, record)
        if brief:
            v2.prompt(session, brief, {"fleet": {"from": "switchboard", "brief": True}})
    except (H["V2Error"], OSError, KeyError, TypeError) as err:
        stopped = _stop_after_failure(engine, runner, name, "launch failed: %s" % _one(str(err), 80))
        if stopped:
            remove_record(state, name)
        raise LaunchError("%s (the worker was created; %s)" % (
            _one(str(err)), "it was stopped" if stopped else "stop it with `%s retire %s`" % (H["PROG"], name))) from None
    print(json.dumps({"name": name, "session": session, "runtime_id": record["runtime_id"], "url": record["url"], "cloud": True}, sort_keys=True))
    return H["EXIT_OK"]


def _record_from_create(name, body):
    if body is None:
        raise CloudError("the launcher printed no record")
    if body.get("worker") != name:
        raise CloudError("the launcher answered for another worker")
    record = {key: body.get(key) for key in RECORD_FIELDS}
    if any(not isinstance(record[key], str) or not record[key] for key in ("url", "runtime_id")):
        raise CloudError("the launcher's record has no url or runtime_id")
    problem = endpoint_problem(record["url"])
    if problem:
        raise CloudError("the launcher named an endpoint that is refused: %s" % problem)
    return record


def _stop_after_failure(engine, runner, name, reason):
    """Best effort: stop a pod that a failed launch left. True when stop exited 0."""
    try:
        code, _, _ = run_launcher(runner, "stop", name, ["--reason", reason], STOP_SECONDS)
    except CloudError:
        return False
    _audit(engine, "cloud.stopped", name, {"exit": code, "why": "launch failed"})
    return code == 0


# ---------------------------------------------------------------- send and read

def _session_of(record):
    session = record.get("session")
    if not isinstance(session, str) or not session:
        raise CloudError("%s has no session yet (its launch did not finish)" % record["worker"])
    return session


def cmd_send(args, runner, now, record):
    """The same `send`, for a cloud worker: one prompt to the session on its endpoint, signed with the sender."""
    engine, state, config = H["build_engine"](runner, now, "cli")
    EXIT_USAGE = H["EXIT_USAGE"]
    refusal = H["intent_refusal"](config, args.issue)
    if refusal:
        sys.stderr.write("%s: %s\n" % (H["PROG"], refusal))
        return EXIT_USAGE
    if args.interrupt:
        sys.stderr.write("%s: --interrupt is not supported for a cloud worker yet: nothing sent\n" % H["PROG"])
        return EXIT_USAGE
    text = " ".join(args.text).strip()
    if not text or len(text) > MESSAGE_MAX:
        sys.stderr.write("%s: %s\n" % (H["PROG"], "nothing to send" if not text else "the message is too long"))
        return EXIT_USAGE
    kind = H["gates_caller_kind"](runner, now)
    if kind[0] == "unknown":
        sys.stderr.write("%s: not sent: %s\n" % (H["PROG"], kind[1]))
        return H["EXIT_HELD"]
    sender = "operator" if kind[0] == "person" else kind[1]
    v2 = remote_v2(state, record, config["call_timeout_seconds"])
    meta = {"fleet": {"from": sender}}
    if args.issue is not None:
        meta["fleet"]["issue"] = args.issue if isinstance(args.issue, int) else str(args.issue)
    reply = v2.prompt(_session_of(record), text, meta, delivery="queue")
    _audit(engine, "cloud.send", args.name, {"from": sender, "chars": len(text)})
    message_id = reply.get("id") if isinstance(reply, dict) else None
    print(message_id or "sent")
    sys.stderr.write("%s: delivered to %s\n" % (H["PROG"], args.name))
    return H["EXIT_OK"]


def message_line(message):
    role = str(message.get("type") or message.get("role") or "?")[:20]
    line = "%s: %s" % (role, H["_message_text"](message))
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", " ", line).strip()   # a pod's text never reaches the terminal as an escape sequence


def cmd_read(args, runner, now):
    """`read NAME [--limit N]`: the newest messages of an agent's session, oldest first. A cloud worker is read on its endpoint; a local agent on
    the local service (by the session the switchboard knows)."""
    LaunchError = H["LaunchError"]
    if not valid_name(args.name):
        raise LaunchError("name the agent: `%s read <name>`" % H["PROG"])
    limit = max(1, min(int(args.limit), 200))
    record = route(args.name)
    engine, state, config = H["build_engine"](runner, now, "cli", writable=False)
    if record is not None:
        v2, session = remote_v2(state, record, config["call_timeout_seconds"]), _session_of(record)
    else:
        seen = engine.discover(engine.now()).agents.get(args.name)
        session = seen.session_id if seen else (H["read_pane_health"](state)["agents"].get(args.name) or {}).get("session")
        if not session:
            raise LaunchError("%s is not a fleet agent this switchboard has seen" % args.name)
        v2 = engine.v2
    messages = v2.message_list(session, limit=limit, order="desc") or []
    for message in reversed([m for m in messages if isinstance(m, dict)]):
        print(message_line(message))
    return H["EXIT_OK"]


# ---------------------------------------------------------------- status

def status_lines(runner, state, timeout=STATUS_SECONDS):
    """(lines, rows) for `status`: per cloud worker its record and, from the launcher's read-only `status`, phase and cost. A launcher trouble is
    said on the line, never raised."""
    lines, rows = [], []
    for record in list_records(state):
        row = {"worker": record["worker"]}
        if "problem" in record:
            row["problem"] = record["problem"]
            lines.append("      cloud %s: %s" % (record["worker"], record["problem"]))
            rows.append(row)
            continue
        row.update({key: record.get(key) for key in ("runtime_id", "idle_deadline", "hard_deadline", "session")})
        try:
            code, body, note = run_launcher(runner, "status", record["worker"], (), timeout)
        except CloudError as err:
            code, body, note = -1, None, str(err)
        if code == 0 and body is not None:
            row.update({key: body.get(key) for key in ("phase", "idle_since", "cost_estimate_usd")})
            extra = "phase %s, about $%s so far" % (body.get("phase", "?"), body.get("cost_estimate_usd", "?"))
        else:
            row["launcher_status"] = _reason(code, note)
            extra = "launcher status unavailable (%s)" % _one(row["launcher_status"], 100)
        rows.append(row)
        lines.append("      cloud %s: %s, %s; stops at %s" % (record["worker"], record["runtime_id"], extra, record.get("hard_deadline") or "?"))
    return lines, rows


def status_extra(runner):
    """The hook of `status`: (lines, rows). Never raises."""
    try:
        state = H["state_dir_path"]()
        if not (state / CLOUD_DIR).is_dir():
            return [], []
        return status_lines(runner, state)
    except Exception as err:
        return ["      cloud workers: unreadable (%s)" % _one(str(err), 100)], []


# ---------------------------------------------------------------- retire

def cmd_retire(args, runner, now, record):
    """`retire NAME` for a cloud worker: `stop` (the end hook runs, the pod and its scratch volume go), then the record goes. A stop that exits 1
    (the end hook could not push) keeps the pod AND the record, and the retire fails with that reason."""
    LaunchError = H["LaunchError"]
    engine, state, _ = H["build_engine"](runner, now, "cli")
    kind = H["gates_caller_kind"](runner, now)
    allowed = kind[0] == "person" or (kind[0] == "agent" and ((len(kind) > 2 and kind[2] == "chief-of-staff") or kind[1] == record.get("reports_to")))
    if not allowed:
        _audit(engine, "cloud.retire_refused", args.name, {"why": _one(str(kind[1]), 120)})
        raise LaunchError("refused: only the operator, the Chief of Staff or %s's own boss may retire it" % args.name)
    if args.undo:
        raise LaunchError("a cloud worker's pod is gone once retired: there is nothing to undo")
    reason = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", args.reason or "").split())[:200] or "finished"
    try:
        code, body, note = run_launcher(runner, "stop", args.name, ["--reason", reason], STOP_SECONDS)
    except CloudError as err:
        raise LaunchError("%s: the record is kept" % err) from None
    _audit(engine, "cloud.stopped", args.name, {"exit": code, "reason": reason, "runtime_id": record.get("runtime_id"),
                                                "pushed": (body or {}).get("pushed"), "wip_branch": (body or {}).get("wip_branch")})
    if code == 1:
        sys.stderr.write("%s: %s was not retired: the end hook could not push (%s): pod kept, record kept; `%s retire %s` again once it can\n"
                         % (H["PROG"], args.name, note or "no reason given", H["PROG"], args.name))
        print("%s: pod kept" % args.name)
        return H["EXIT_ERROR"]
    if code != 0:
        raise LaunchError("stop of %s failed: %s: the record is kept" % (args.name, _reason(code, note)))
    remove_record(state, args.name)
    wip, pushed = (body or {}).get("wip_branch"), (body or {}).get("pushed")
    print("%s is retired: its pod and scratch volume are deleted%s" % (
        args.name, "; work was pushed to branch %s" % wip if pushed and wip else "; nothing needed pushing" if pushed is False else ""))
    return H["EXIT_OK"]
