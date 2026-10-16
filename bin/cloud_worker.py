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


# ---------------------------------------------------------------- the cluster, per use: a token minted for ONE command, a tunnel for ONE command

class Refusal(CloudError):
    """A refusal before anything is created or called: the command exits 2 and says why in one line."""


VAULT_CHECK = ("run the platform team's firewall check for the vault (the vault pre-check) first; nothing was changed and no other credential "
               "was tried")
MAX_TOKEN_SECONDS = 3600
TOKEN_CMD_SECONDS = 60
CLUSTER_DROP = ("CW_KUBE_CONTEXT", "CW_KUBECONFIG", "KUBECONFIG", "CW_CLOUD_SERVER", "CW_CLOUD_TOKEN_FILE", "CW_CLOUD_CA_FILE",
                "CW_CLUSTER_TOKEN_CMD", "CW_CLUSTER_ADMIN_CMD")
POD_RE = re.compile(r"pod/([a-z0-9][a-z0-9.-]{0,100})")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
MODEL_PROVIDER = "github-copilot"
_LIB = {}


def _lib():
    """The launcher's own module (cloud/launcher/cloud_worker.py), loaded once by path: its ONE scrub function and secret forms are used here,
    there is no second copy."""
    if not _LIB:
        path = Path(__file__).resolve().parent.parent / "cloud" / "launcher" / "cloud_worker.py"
        spec = importlib.util.spec_from_file_location("cw_launcher_lib", str(path))
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except (OSError, SyntaxError) as err:
            raise CloudError("the launcher library is not found beside this program (%s)" % _one(str(err), 80)) from None
        _LIB["m"] = module
    return _LIB["m"]


def _scrub_lib():
    return _lib().scrub


def allowed_repos():
    return [x for x in re.split(r"[,\s]+", os.environ.get("CW_ALLOWED_REPOS", "")) if x]


def check_allowed(repo):
    """The allow-list (#639 owns the real one; this is the thin check until it lands): only a repository named in CW_ALLOWED_REPOS, exactly,
    can be launched, and never the tooling repository (CW_TOOLING_REPO) even if listed. Runs before anything is created."""
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo) or repo.split("/")[0] in (".", "..") or repo.split("/")[1] in (".", ".."):
        raise Refusal("not a repository (OWNER/NAME): %r; nothing was created" % (repo,))
    listed = allowed_repos()
    if not listed:
        raise Refusal("CW_ALLOWED_REPOS is empty: no repository can be launched; nothing was created")
    if repo.lower() == (os.environ.get("CW_TOOLING_REPO") or "").strip().lower():
        raise Refusal("%s is the tooling repository: cloud workers serve product repositories only; nothing was created" % repo)
    if repo not in listed:
        raise Refusal("%s is not on the allow-list (CW_ALLOWED_REPOS); nothing was created" % repo)


def _capture(argv, env=None, feed=None, timeout=TOKEN_CMD_SECONDS, limit=1000000):
    """Run a short helper (a vault read, a mint). `feed` is a text handed over a pipe: the argument `{fd}` in argv names it, so it is never in an
    argument, the environment or a file. Returns (exit code, stdout, stderr), or None when it could not run or was too slow."""
    pass_fds, writer = (), None
    if feed is not None:
        read_end, write_end = os.pipe()
        argv = [a.replace("{fd}", str(read_end)) for a in argv]
        pass_fds = (read_end,)
        writer = (read_end, write_end)
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=pass_fds,
                                 env=env, start_new_session=True)
    except OSError:
        if writer:
            _close_quietly(*writer)
        return None
    if writer:
        _close_quietly(writer[0])
        threading.Thread(target=_feed, args=(writer[1], feed), daemon=True).start()
    try:
        out, err = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(child)
        child.communicate()
        return None
    except BaseException:
        _kill_group(child)
        raise
    return child.returncode, out[:limit + 1], err


def _jwt_lifetime(token, now):
    """Seconds the token lives, read from its exp (and iat) claim when it is a JWT; None when it is not one (the requested duration is trusted)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        body = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
        exp = claims["exp"]
        iat = claims.get("iat")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or (iat is not None and (isinstance(iat, bool) or not isinstance(iat, (int, float)))):
        return None
    return exp - (iat if iat is not None else now)


def _duration(env):
    """The token's requested life: CW_CLUSTER_TOKEN_DURATION (default 20m: the shortest that covers a create and a stop; kubectl's own floor is
    10m), never over 60m."""
    text = (env.get("CW_CLUSTER_TOKEN_DURATION") or "20m").strip()
    found = re.fullmatch(r"(\d{1,3})([mh])", text)
    minutes = int(found.group(1)) * (60 if found.group(2) == "h" else 1) if found else 0
    if not 1 <= minutes <= 60:
        raise Refusal("CW_CLUSTER_TOKEN_DURATION must be between 1m and 60m (got %r); nothing was called" % text[:20])
    return "%dm" % minutes


def cluster_token(env=None, now=None):
    """A cluster token of at most one hour, minted NOW, in memory. CW_CLUSTER_TOKEN_CMD (a command that prints one), else the default: the cluster
    administrator kubeconfig is read from the vault (CW_CLUSTER_ADMIN_CMD, else `az keyvault secret show` with CW_VAULT_SUBSCRIPTION and
    CW_VAULT_NAME) and `kubectl create token cw-operator --duration=20m` is run with it on a pipe. Nothing is cached or kept. A failure is a
    Refusal that names the vault pre-check; no other credential or context is ever tried."""
    env = dict(os.environ if env is None else env)
    clock = time.time() if now is None else now

    def fail(why):
        raise Refusal("the cluster token could not be obtained (%s): %s" % (why, VAULT_CHECK)) from None
    custom = (env.get("CW_CLUSTER_TOKEN_CMD") or "").strip()
    if custom:
        ran = _capture(["sh", "-c", custom], env=env)
        if ran is None or ran[0] != 0:
            fail("the token command failed")
        token = ran[1].strip()
    else:
        admin = (env.get("CW_CLUSTER_ADMIN_CMD") or "").strip()
        if admin:
            argv = ["sh", "-c", admin]
        else:
            sub, vault = (env.get("CW_VAULT_SUBSCRIPTION") or "").strip(), (env.get("CW_VAULT_NAME") or "").strip()
            if not sub or not vault:
                fail("set CW_VAULT_SUBSCRIPTION and CW_VAULT_NAME")
            argv = ["az", "keyvault", "secret", "show", "--subscription", sub, "--vault-name", vault,
                    "--name", env.get("CW_CLUSTER_ADMIN_SECRET") or "cw-cluster-admin-kubeconfig", "--query", "value", "-o", "tsv"]
        ran = _capture(argv, env=env)
        if ran is None or ran[0] != 0 or not ran[1].strip() or len(ran[1]) > 1000000:
            fail("the administrator credential could not be read from the vault")
        admin_config = ran[1]
        minted = _capture([env.get("CW_KUBECTL", "kubectl"), "--kubeconfig", "/dev/fd/{fd}", "-n", env.get("CW_CLOUD_NAMESPACE", "cw"), "create",
                           "token", "cw-operator", "--duration=" + _duration(env)], env=dict(env, KUBECONFIG=os.devnull), feed=admin_config)
        del admin_config
        if minted is None or minted[0] != 0:
            fail("the token could not be minted")
        token = minted[1].strip()
    if not token or len(token) > 8192 or re.search(r"\s", token) or re.search(r"[\x00-\x1f\x7f]", token):
        fail("the answer is not a token")
    lifetime = _jwt_lifetime(token, clock)
    if lifetime is not None and not 0 < lifetime <= MAX_TOKEN_SECONDS:
        raise Refusal("the cluster token lives %d seconds, over the 60 minutes allowed (or already expired); refused, nothing was called" % lifetime)
    return token


def private_root():
    """The per-user directory (0700, ours, not a link) under the temp dir that holds the token files of running commands. One that has another
    owner, another mode or is a link is REFUSED, never repaired."""
    root = Path(tempfile.gettempdir()) / ("cwtok-%d" % os.getuid())
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        found = os.lstat(str(root))
    except OSError as err:
        raise Refusal("the private directory for the cluster token cannot be made (%s); nothing was called" % (err.strerror or err)) from None
    if stat.S_ISLNK(found.st_mode) or not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid() or stat.S_IMODE(found.st_mode) != 0o700:
        raise Refusal("the private directory %s is not the user's own with mode 0700; refused, nothing was called" % root)
    return root


def sweep_stale(root):
    """At the start of every command: remove what a command killed by SIGKILL left: a `cmd-<pid>-*` directory of ours whose process is gone, or
    that is older than the token's longest life. Nothing else is touched. Says so once when it removed one (never what was in it)."""
    removed = 0
    for entry in root.iterdir():
        match = re.fullmatch(r"cmd-(\d+)-[A-Za-z0-9_]+", entry.name)
        try:
            found = os.lstat(str(entry))
        except OSError:
            continue
        if not match or not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid():
            continue
        dead = False
        try:
            os.kill(int(match.group(1)), 0)
        except ProcessLookupError:
            dead = True
        except OSError:
            pass
        if dead or time.time() - found.st_mtime > MAX_TOKEN_SECONDS:
            shutil.rmtree(str(entry), ignore_errors=True)
            removed += 1
    if removed:
        sys.stderr.write("%s: removed %d leftover cluster-token directory of an earlier command that was killed\n" % (H["PROG"], removed))


class Cluster:
    """The login for ONE command. `with Cluster() as cluster:` mints a token, writes it to a 0600 O_EXCL file in a private 0700 directory (the only
    place it touches disk: the launcher's tokenFile mechanism needs a file), and removes the directory on every way out: normal end, failure,
    SIGINT and SIGTERM. A stale directory of a killed command is swept at the start of every command. No token is kept between commands."""

    def __init__(self):
        self.known = set()
        self.dir = None
        self.kubeconfig = None
        self.token_file = None
        self.server = None
        self.old = {}

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            self.old = {signal.SIGTERM: signal.signal(signal.SIGTERM, _terminate)}
        try:
            server = (os.environ.get("CW_CLOUD_SERVER") or "").strip()
            if not server:
                raise Refusal("CW_CLOUD_SERVER is not set: no cluster to reach; nothing was changed")
            root = private_root()
            sweep_stale(root)
            token = cluster_token()
            self.add(token)
            self.dir = tempfile.mkdtemp(prefix="cmd-%d-" % os.getpid(), dir=str(root))
            self.server = server
            self.token_file = os.path.join(self.dir, "token")
            fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(token)
            del token
            cluster = {"server": server}
            ca = (os.environ.get("CW_CLOUD_CA_FILE") or "").strip()
            if ca:
                cluster["certificate-authority"] = os.path.abspath(ca)
            config = {"apiVersion": "v1", "kind": "Config", "current-context": "cw", "clusters": [{"name": "cw", "cluster": cluster}],
                      "users": [{"name": "cw", "user": {"tokenFile": self.token_file}}],
                      "contexts": [{"name": "cw", "context": {"cluster": "cw", "user": "cw"}}]}
            self.kubeconfig = os.path.join(self.dir, "config")
            fd = os.open(self.kubeconfig, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(config, handle)
        except OSError as err:   # a full disk or a refused file: nothing partial is left
            self.close()
            raise Refusal("the token file cannot be written safely (%s); nothing was called" % (err.strerror or err)) from None
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_):
        self.close()
        return False

    def close(self):
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = None
        for sig, handler in self.old.items():
            signal.signal(sig, handler)
        self.old = {}

    def add(self, *values):
        for value in values:
            if value:
                self.known.update(_lib().secret_forms(value))

    def clean(self, text, limit=300):
        return _lib().scrub(text, self.known, limit)

    def env(self):
        """The environment of a launcher or kubectl child: this one's login only (a token FILE path, never a value); no other login is passed on."""
        env = {k: v for k, v in os.environ.items() if k not in CLUSTER_DROP}
        env.update({"CW_CLOUD_SERVER": self.server, "CW_CLOUD_TOKEN_FILE": self.token_file, "KUBECONFIG": os.devnull})
        if os.environ.get("CW_CLOUD_CA_FILE"):
            env["CW_CLOUD_CA_FILE"] = os.environ["CW_CLOUD_CA_FILE"]
        return env


def _terminate(signum, frame):
    raise KeyboardInterrupt


@contextlib.contextmanager
def tunnel(cluster, record):
    """A `kubectl port-forward` to the worker's pod that exists for the `with` block only. Yields the record with its url set to the loopback address
    chosen now (never stored). The child has its own process group and is killed on every way out, interrupts included."""
    found = POD_RE.fullmatch(record.get("runtime_id") or "")
    if not found:
        raise CloudError("the record of %s names no pod" % record.get("worker"))
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    argv = [os.environ.get("CW_KUBECTL", "kubectl"), "--kubeconfig", cluster.kubeconfig, "--context", "cw", "-n", os.environ.get("CW_CLOUD_NAMESPACE", "cw"),
            "port-forward", "pod/" + found.group(1), "%d:4096" % port, "--address", "127.0.0.1"]
    log_path = os.path.join(cluster.dir, "forward-%d.log" % port)
    with open(log_path, "wb") as log:
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=cluster.env(), start_new_session=True)
        except OSError as err:
            raise CloudError("cannot open the tunnel: %s" % (err.strerror or err)) from None
    try:
        deadline = time.monotonic() + float(os.environ.get("CW_TUNNEL_TIMEOUT") or 30)
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                pass
            if child.poll() is not None or time.monotonic() >= deadline:
                try:
                    with open(log_path, "rb") as log:
                        why = log.read(400).decode("utf-8", "replace")
                except OSError:
                    why = ""
                raise CloudError("the tunnel to %s did not open%s" % (record.get("worker"), (": " + _one(cluster.clean(why), 160)) if why.strip() else ""))
            time.sleep(0.1)
        yield dict(record, url="http://127.0.0.1:%d" % port)
    finally:
        _kill_group(child)


def refusals(function):
    """A Refusal is exit 2 with its one line; nothing else about the command changes."""
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Refusal as err:
            sys.stderr.write("%s: %s\n" % (H["PROG"], err))
            return H["EXIT_USAGE"]
    wrapped.__name__ = function.__name__
    wrapped.__doc__ = function.__doc__
    return wrapped


def model_entry():
    """The github-copilot entry of the OpenCode auth file (the driver's rule), as one JSON text; nothing else of the file."""
    path = os.environ.get("CW_MODEL_AUTH_FILE") or os.path.expanduser("~/.local/share/opencode/auth.json")
    try:
        with open(path) as handle:
            entry = json.load(handle).get(MODEL_PROVIDER)
    except (OSError, ValueError, AttributeError):
        entry = None
    if not entry or not isinstance(entry, dict):
        raise Refusal("the model auth file is missing, unreadable or has no %s entry; nothing was created" % MODEL_PROVIDER)
    return json.dumps({MODEL_PROVIDER: entry})


def git_token(repo, cluster):
    """The one-hour git token for ONE repository: CW_GIT_TOKEN_CMD, else the mint (cloud/launcher/app_token.py REPO), as the driver does."""
    raw = os.environ.get("CW_GIT_TOKEN_CMD")
    argv = shlex.split(raw) if raw else [sys.executable, str(Path(__file__).resolve().parent.parent / "cloud" / "launcher" / "app_token.py"), repo]
    env = {k: v for k, v in os.environ.items() if k not in CLUSTER_DROP}
    ran = _capture(argv, env=dict(env, GH_REPO=repo))
    token = (ran[1] if ran and ran[0] == 0 else "").strip()
    if not token or len(token) > 4096:
        why = ""
        if ran and ran[2].strip():
            why = ": " + _one(cluster.clean(ran[2].strip().splitlines()[-1], 200), 200)
        raise Refusal("the git token command gave no usable token%s; nothing was created" % why)
    return token


# ---------------------------------------------------------------- launch

@refusals
def cmd_launch(args, runner, now):
    """`launch NAME --cloud --repo OWNER/REPO --branch B --agent A`: the allow-list first, then (with a cluster token minted for this command) the
    model entry and a one-hour git token go to the launcher as per-run secrets, the pod is created, and the session and the brief are sent through
    a tunnel that exists only for those calls."""
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
    check_allowed(repo)   # before anything is read, minted or created
    engine, state, _ = H["build_engine"](runner, now, "cli")
    if load_record(state, name) is not None:
        raise LaunchError("a cloud worker named %s already exists (`%s retire %s` ends it)" % (name, H["PROG"], name))
    brief = H["read_brief"](args.brief_file, sys.stdin)
    options = ["--repo", repo, "--branch", branch]
    for flag, value in (("--idle", args.idle), ("--hard", args.hard), ("--shape", args.shape)):
        if value:
            options += [flag, str(value)]
    if os.environ.get("CW_PULL_SECRET"):
        options += ["--pull-secret", os.environ["CW_PULL_SECRET"]]
    with Cluster() as cluster:
        model = model_entry()
        cluster.add(model)
        token = git_token(repo, cluster)
        cluster.add(token)
        read_fd, write_fd = os.pipe()
        try:
            try:
                code, body, note = run_launcher(runner, "create", name, options, LAUNCH_SECONDS, password_fd=write_fd, cluster=cluster,
                                                secrets=[("model-auth", model), ("git-token", token)])
            except CloudError as err:
                raise LaunchError(str(err)) from None
            except BaseException:
                os.close(write_fd)
                write_fd = None
                _stop_after_failure(engine, runner, name, "launch interrupted", cluster)
                raise
            finally:
                if write_fd is not None:
                    os.close(write_fd)
            del model, token
            if code != 0:
                _audit(engine, "cloud.create_failed", name, {"exit": code, "reason": _one(note, 160)})
                raise LaunchError("the cloud worker was not created: %s" % _reason(code, note))
            try:
                password = _read_password(read_fd)
            except CloudError as err:
                _stop_after_failure(engine, runner, name, "launch: no password", cluster)
                raise LaunchError(str(err)) from None
            cluster.add(password)
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
            _stop_after_failure(engine, runner, name, "launch: record not stored", cluster)
            raise LaunchError("the worker was created but its record could not be stored (%s); stop was requested" % _one(cluster.clean(str(err), 100), 100)) from None
        _audit(engine, "cloud.created", name, {"runtime_id": record["runtime_id"], "host": urllib.parse.urlsplit(record["url"]).hostname,
                                               "idle_deadline": record.get("idle_deadline"), "hard_deadline": record.get("hard_deadline")})
        fleet = H["fleet_record"](name, args.role, args.reports_to, None if args.issue is None else int(args.issue), args.charter, repo)
        try:
            with tunnel(cluster, record) as live:
                v2 = remote_v2(state, live, engine.config["call_timeout_seconds"])
                session = v2.session_create(args.agent, name, args.dir or DEFAULT_DIRECTORY, {"fleet": fleet, "cloud": {"runtime_id": record["runtime_id"]}})["id"]
                record["session"] = session
                save_record(state, record)
                if brief:
                    v2.prompt(session, brief, {"fleet": {"from": "switchboard", "brief": True}})
        except (H["V2Error"], H["V2Unreachable"], CloudError, OSError, KeyError, TypeError) as err:
            stopped = _stop_after_failure(engine, runner, name, "launch failed: %s" % _one(cluster.clean(str(err), 80), 80), cluster)
            if stopped:
                remove_record(state, name)
            raise LaunchError("%s (the worker was created; %s)" % (
                _one(cluster.clean(str(err))), "it was stopped" if stopped else "stop it with `%s retire %s`" % (H["PROG"], name))) from None
        except BaseException:
            if _stop_after_failure(engine, runner, name, "launch interrupted", cluster):
                remove_record(state, name)
            raise
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


def _stop_after_failure(engine, runner, name, reason, cluster):
    """Best effort: stop a pod that a failed or interrupted launch left (the launcher deletes the pod and its per-run secret). True when stop exited 0."""
    old = _ignore_signals()   # a second Ctrl-C must not abandon the cleanup
    try:
        code, _, _ = run_launcher(runner, "stop", name, ["--reason", reason], STOP_SECONDS, cluster=cluster)
    except CloudError:
        return False
    finally:
        _restore_signals(old)
    _audit(engine, "cloud.stopped", name, {"exit": code, "why": "launch failed"})
    return code == 0


# ---------------------------------------------------------------- send and read

def _session_of(record):
    session = record.get("session")
    if not isinstance(session, str) or not session:
        raise CloudError("%s has no session yet (its launch did not finish)" % record["worker"])
    return session


@refusals
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
    meta = {"fleet": {"from": sender}}
    if args.issue is not None:
        meta["fleet"]["issue"] = args.issue if isinstance(args.issue, int) else str(args.issue)
    session = _session_of(record)
    with Cluster() as cluster, tunnel(cluster, record) as live:
        reply = remote_v2(state, live, config["call_timeout_seconds"]).prompt(session, text, meta, delivery="queue")
    _audit(engine, "cloud.send", args.name, {"from": sender, "chars": len(text)})
    message_id = reply.get("id") if isinstance(reply, dict) else None
    print(message_id or "sent")
    sys.stderr.write("%s: delivered to %s\n" % (H["PROG"], args.name))
    return H["EXIT_OK"]


def message_line(message):
    role = str(message.get("type") or message.get("role") or "?")[:20]
    line = "%s: %s" % (role, H["_message_text"](message))
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", " ", line).strip()   # a pod's text never reaches the terminal as an escape sequence


@refusals
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
        session = _session_of(record)
        with Cluster() as cluster, tunnel(cluster, record) as live:
            messages = remote_v2(state, live, config["call_timeout_seconds"]).message_list(session, limit=limit, order="desc") or []
        for message in reversed([m for m in messages if isinstance(m, dict)]):
            print(message_line(message))
        return H["EXIT_OK"]
    else:   # a local agent, on the local service
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
    """(lines, rows) for `status`: per cloud worker its record and, from the launcher's read-only `status`, phase and cost, and whether the worker
    answers through a tunnel that exists only for that probe. One cluster token serves the whole command. A trouble is said on the line, never raised."""
    records = list_records(state)
    lines, rows = [], []
    cluster = None
    try:
        if any("problem" not in r for r in records):
            try:
                cluster = Cluster().__enter__()
            except CloudError as err:
                cluster, lines = None, ["      cloud workers: no cluster reached (%s)" % _one(str(err), 300)]
        for record in records:
            row = {"worker": record["worker"]}
            if "problem" in record:
                row["problem"] = record["problem"]
                lines.append("      cloud %s: %s" % (record["worker"], record["problem"]))
                rows.append(row)
                continue
            row.update({key: record.get(key) for key in ("runtime_id", "idle_deadline", "hard_deadline", "session")})
            if cluster is None:
                row["launcher_status"] = "no cluster reached"
                rows.append(row)
                lines.append("      cloud %s: %s, not queried (no cluster reached); stops at %s" % (record["worker"], record["runtime_id"], record.get("hard_deadline") or "?"))
                continue
            try:
                code, body, note = run_launcher(runner, "status", record["worker"], (), timeout, cluster=cluster)
            except CloudError as err:
                code, body, note = -1, None, str(err)
            if code == 0 and body is not None:
                row.update({key: body.get(key) for key in ("phase", "idle_since", "cost_estimate_usd")})
                extra = "phase %s, about $%s so far" % (body.get("phase", "?"), body.get("cost_estimate_usd", "?"))
            else:
                row["launcher_status"] = _reason(code, note)
                extra = "launcher status unavailable (%s)" % _one(row["launcher_status"], 100)
            row["answers"] = _answers(state, cluster, record, timeout)
            rows.append(row)
            lines.append("      cloud %s: %s, %s; %s; stops at %s" % (record["worker"], record["runtime_id"], extra,
                                                                 "answers" if row["answers"] else "does not answer", record.get("hard_deadline") or "?"))
    finally:
        if cluster is not None:
            cluster.close()
    return lines, rows


def _answers(state, cluster, record, timeout):
    """True when the worker's server answers its info request, through a tunnel open for this probe only."""
    try:
        with tunnel(cluster, record) as live:
            status, _ = remote_v2(state, live, timeout).http.request("GET", "/api/info")
            return status == 200
    except Exception:
        return False


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

@refusals
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
        with Cluster() as cluster:
            code, body, note = run_launcher(runner, "stop", args.name, ["--reason", reason], STOP_SECONDS, cluster=cluster)
    except Refusal:
        raise
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
    # The run record (slice 3, #638) is written by the launcher's stop; no call is needed here when it lands: this is its hook point (after the
    # end hook, before the endpoint record goes).
    remove_record(state, args.name)
    wip, pushed = (body or {}).get("wip_branch"), (body or {}).get("pushed")
    print("%s is retired: its pod and scratch volume are deleted%s" % (
        args.name, "; work was pushed to branch %s" % wip if pushed and wip else "; nothing needed pushing" if pushed is False else ""))
    return H["EXIT_OK"]
