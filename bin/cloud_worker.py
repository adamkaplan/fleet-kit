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
import datetime
import fcntl
import http.client
import importlib.util
import json
import os
import re
import shlex
import shutil
import secrets as secrets_module
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

_OWN_BIN = Path(__file__).resolve().parent   # fixed at import
LAUNCHER_ENV = "FLEET_CLOUD_WORKER_BIN"   # the launcher program; default `cloud-worker` on PATH
DEFAULT_LAUNCHER = "cloud-worker"
CLOUD_DIR = "cloud"
DEFAULT_DIRECTORY = "/work"   # the session's directory in the pod: the scratch volume (writable by the image's user via fsGroup); /workspace does not exist there
CLOUD_WORKER_AGENT = "worker"   # the agent the image defines (cloud/worker/agents/worker.md): a cloud session always runs it; the local --agent name is only recorded
# The options of the launcher's `create` that `launch --cloud` hands over: ONE table. Taken from the launcher on main (commit fb4b9f2,
# cloud/launcher/cloud_worker.py `create`: --shape S|M, --stack); a name here that the launcher does not know makes its create fail (exit 2).
CREATE_OPTION = {"repo": "--repo", "branch": "--branch", "run_id": "--run-id", "idle": "--idle", "hard": "--hard", "shape": "--shape", "stack": "--stack", "agent": "--agent", "boss": "--boss", "stack_model_secret": "--stack-model-secret"}
LAUNCHER_SHAPES = ("XS", "S", "M", "O")   # the launcher's SHAPES keys (O: the orchestrator row, 250m and 768 MiB, meant for --agent orchestrator; a worker on it is likely to be killed for memory); the launcher's own default (XS; S with --stack; O with --agent orchestrator) applies when --shape is not given
LAUNCHER_STACK_SHAPES = ("S", "M")   # the rows with stack: true; --stack with another shape is refused here, before any call
POD_DIR_ROOTS = ("/work", "/home/worker")   # the only roots a cloud session directory may be under (a local path does not exist in the pod)
WORK_REPO = "/work/repo"      # where the worker clones the repository: the launcher's end hook (CW_WORKDIR default) pushes from here
SETUP_PREAMBLE = """Setup (do this first, before the task below):
- Clone https://github.com/%(repo)s.git into /work/repo. The pod already holds the git credentials: never print, write or commit a token.
- In /work/repo switch to the branch %(branch)s (create it from the default branch if it does not exist yet).
- Do ALL your work in /work/repo, and nothing outside it: the end hook pushes from there when this worker is retired.

Task:
"""
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
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)   # O_NONBLOCK: a FIFO in the record directory must not block the open
    with os.fdopen(fd, "rb") as handle:
        found = os.fstat(handle.fileno())
        if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid() or found.st_mode & 0o077:
            raise ValueError("not private to this user")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("too large")
    return raw.decode("utf-8")


WATCH_MAX_DEPTH = 64        # the deepest [ / { nesting a cloud record may have (a real record is about 5 deep)


class TooDeep(ValueError):
    """The text nests deeper than WATCH_MAX_DEPTH."""


def nesting_too_deep(text, limit=None):
    """True when `text` nests [ or { deeper than the limit. One iterative pass, O(n), no recursion; brackets inside JSON strings do not count
    (backslash escapes honoured). Never depends on the interpreter's recursion limit."""
    limit = WATCH_MAX_DEPTH if limit is None else limit
    depth, in_string, escaped = 0, False, False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "[" or ch == "{":
            depth += 1
            if depth > limit:
                return True
        elif ch == "]" or ch == "}":
            depth -= 1
    return False


def parse_bounded(text):
    """json.loads of untrusted text with the same depth limit on every Python (the ONE parse of a record the daemon did not write)."""
    if nesting_too_deep(text):
        raise TooDeep("nested deeper than %d" % WATCH_MAX_DEPTH)
    try:
        return json.loads(text)
    except RecursionError:   # defence in depth
        raise TooDeep("recursion limit") from None


def read_record_file(path, max_bytes):
    """The ONE safe read of a file in the cloud record directory (#1097, the family of #1073): (record dict, None) or (None, kind). Never blocks (O_NONBLOCK,
    so a FIFO cannot hang the open), never follows a symlink (O_NOFOLLOW), reads only a regular file (fstat), at most max_bytes. Never raises.
    kind: not_regular, too_large, not_utf8, not_json, wrong_shape."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None, "not_regular"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "not_regular"
        raw = b""
        while len(raw) <= max_bytes:
            chunk = os.read(fd, max_bytes + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    except OSError:
        return None, "not_regular"
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        return None, "too_large"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not_utf8"
    try:
        record = parse_bounded(text)
    except TooDeep:
        return None, "too_deep"
    except ValueError:
        return None, "not_json"
    if not isinstance(record, dict):
        return None, "wrong_shape"
    return record, None


def load_record(state, name):
    """The worker's record, or None when there is none. A record that exists but is unusable raises CloudError (never read as 'not a cloud worker')."""
    path = record_path(state, name)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        record = parse_bounded(_read_private(path, RECORD_MAX))
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


WATCH_MAX_RECORDS = 50      # the most valid records one pass reads
WATCH_MAX_BYTES = 65536     # the most bytes read from one record
WATCH_MAX_FILES = 200       # the most files one pass examines, valid or not
WATCH_SECONDS = 1.0         # the most monotonic time one pass spends
SKIP_KINDS = ("bad_name", "not_regular", "symlink", "too_large", "not_utf8", "not_json", "wrong_shape", "too_deep")


def _read_one(path, max_bytes):
    """One record file: (record, None) or (None, skip kind). Never blocks: O_NONBLOCK (a FIFO with no writer opens at once), O_NOFOLLOW, regular files only."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as err:
        import errno
        return None, ("symlink" if err.errno == errno.ELOOP else "not_regular")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "not_regular"
        raw = b""
        while len(raw) <= max_bytes:
            chunk = os.read(fd, max_bytes + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    except OSError:
        return None, "not_regular"
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        return None, "too_large"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not_utf8"
    try:
        record = parse_bounded(text)
    except TooDeep:
        return None, "too_deep"
    except ValueError:
        return None, "not_json"
    if not isinstance(record, dict):
        return None, "wrong_shape"
    return record, None


def read_records_bounded(state, max_records=WATCH_MAX_RECORDS, max_bytes=WATCH_MAX_BYTES, max_files=WATCH_MAX_FILES,
                         seconds=WATCH_SECONDS, cursor=None, clock=None):
    """The cloud record directory read for the per-pass watch. Read-only; never raises on a bad file, never blocks on one.
    {"records": [...], "skipped": n, "skips": {kind: n}, "capped": bool, "bound": None|"records"|"files"|"time", "cursor": last name examined}.
    The start rotates: reading resumes after `cursor`, so every record gets a turn."""
    import time as _time
    clock = clock or _time.monotonic
    base = Path(state) / CLOUD_DIR
    out = {"records": [], "skipped": 0, "skips": {k: 0 for k in SKIP_KINDS}, "capped": False, "bound": None, "cursor": cursor}
    if not base.is_dir():
        return out
    names = sorted(n for n in os.listdir(str(base)) if n.endswith(".json"))
    if cursor is not None:
        after = [n for n in names if n > cursor]
        names = after + [n for n in names if n <= cursor]
    deadline = clock() + seconds
    examined = 0
    for name in names:
        if len(out["records"]) >= max_records:
            out["bound"] = "records"
        elif examined >= max_files:
            out["bound"] = "files"
        elif clock() > deadline:
            out["bound"] = "time"
        if out["bound"]:
            out["capped"] = True
            break
        examined += 1
        out["cursor"] = name
        kind = None
        if not valid_name(name[:-5]):
            kind = "bad_name"
        else:
            record, kind = _read_one(str(base / name), max_bytes)
        if kind:
            out["skips"][kind] += 1
            out["skipped"] += 1
        else:
            out["records"].append(record)
    return out


def _reserved(name):
    """True when another live command is launching `name` right now: its `NAME.launching` file is flock-ed. A leftover file nobody holds (a command
    that was killed: the kernel released its lock) means nothing."""
    path = H["state_dir_path"]() / CLOUD_DIR / (name + ".launching")
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except OSError:
        return True   # held by a live command
    finally:
        os.close(fd)


@contextlib.contextmanager
def reservation(state, name):
    """Holds the name for the length of a `launch --cloud` with an flock on `cloud/NAME.launching`, so two launches of one name (or a local launch
    meanwhile, see refuse_local_launch) cannot both pass the checks and then both create. A lock dies with its process, so a killed command never
    holds a name, and the holder removes the file as it leaves (a later holder re-checks that the file it locked is still the one at the path). A refusal here is intended: the name is taken."""
    path = cloud_dir(state) / (name + ".launching")
    for _ in range(5):
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as err:
            raise CloudError("the launch reservation of %s cannot be made (%s); nothing was created" % (name, err.strerror or err)) from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise CloudError("%s is being launched by another command right now; nothing was created" % name) from None
        try:   # the holder before us unlinks the file as it leaves: the lock counts only if the file we hold is still the one at the path
            if os.fstat(fd).st_ino == os.stat(str(path)).st_ino:
                break
        except OSError:
            pass
        os.close(fd)
    else:
        raise CloudError("the launch reservation of %s is contended; nothing was created" % name)
    try:
        yield
    finally:
        try:
            os.unlink(str(path))   # while the lock is still held
        except OSError:
            pass
        os.close(fd)   # releases the lock


def refuse_local_launch(name):
    """Called first by a LOCAL `launch`: a name that has a cloud record is refused, because `route` would send every later send/read/retire for
    the new local agent into the pod. No cluster or herdr call: the record is a file. A broken record also refuses (it still holds the name)."""
    if valid_name(name or "") and _reserved(name):
        raise CloudError("a cloud worker named %s is being launched right now: a local agent cannot take its name; nothing was launched" % name)
    if valid_name(name or "") and (H["state_dir_path"]() / CLOUD_DIR / (name + ".json")).exists():
        raise CloudError("a cloud worker named %s exists: a local agent cannot take its name (send, read and retire by name would reach the "
                         "pod); `%s retire %s` ends it first; nothing was launched" % (name, H["PROG"], name))


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


# ---------------------------------------------------------------- the launcher, the settings, the vault recovery, the cluster and the tunnel (bin/cloud_cluster.py)

class Refusal(CloudError):
    """A refusal before anything is created or called: the command exits 2 and says why in one line."""


class _Stayed:
    """What bin/cloud_cluster.py reads of this file: `D[name]` is this file's global `name` AT THE CALL, so a patch of it here reaches that code."""
    def __getitem__(self, name):
        return globals()[name]


def _load_cluster():
    """Load bin/cloud_cluster.py (beside this file; a part of it) with `D` set, and bind each of its names here (a name patched here does not reach it: patch it there)."""
    spec = importlib.util.spec_from_file_location("cloud_cluster", str(_OWN_BIN / "cloud_cluster.py"))
    module = importlib.util.module_from_spec(spec)
    module.D = _Stayed()
    sys.modules["cloud_cluster"] = module
    spec.loader.exec_module(module)
    return module


CLUSTER_NAMES = (
    "checkout_launcher_path", "_WALK", "_walk", "_checkout_launcher_problem", "LAUNCHER_ORDER", "launcher_argv", "STACK_MODEL_KEY_LIMIT",
    "run_launcher", "LAUNCHER_OUTPUT_LIMIT", "_read_capped", "_close_quietly", "_feed", "_kill_group", "_ignore_signals", "_restore_signals",
    "_reason", "_audit", "_audit_refused_preflight", "_read_password", "VAULT_CHECK", "FIREWALL_MARKERS", "VAULT_LOCK_NAME", "VAULT_LOCK_GRACE",
    "VAULT_LOCK_POLL", "VAULT_OWNER", "MAX_TOKEN_SECONDS", "HANDLED_SIGNALS", "TOKEN_CMD_SECONDS", "CLUSTER_DROP", "POD_RE", "MODEL_PROVIDER", "_LIB",
    "_lib", "_scrub_lib", "use_settings", "_OWNER_RE", "_NAME_RE", "_repo_key", "check_allowed", "_capture", "_jwt_lifetime", "_duration",
    "firewall_refused", "VaultRecovery", "_token_refusal", "cluster_token", "_cluster_token_bounded", "private_root", "_ps", "stop_recorded_tunnels",
    "sweep_stale", "Cluster", "tunnel",
)
cloud_cluster = _load_cluster()
for _name in CLUSTER_NAMES:
    globals()[_name] = getattr(cloud_cluster, _name)



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
    """The live model credential: the ONE provider entry of OpenCode's own store (`opencode auth export`, in memory), as a one-element JSON array."""
    path = Path(__file__).resolve().parent.parent / "cloud" / "launcher" / "model_credential.py"
    spec = importlib.util.spec_from_file_location("cw_model_credential", str(path))
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as err:
        raise CloudError("the model credential reader is not found beside this program (%s)" % _one(str(err), 80)) from None
    try:
        return module.read_provider_entry(MODEL_PROVIDER)
    except module.ModelCredentialError as err:
        raise Refusal(str(err)) from None


def stack_model_secret(env=None):
    """The stack-only model credential of a `--stack` launch: (secret NAME, value), or None when CW_STACK_MODEL_SECRET is unset. The value is read from
    the vault (`az keyvault secret show`, the settings file's vault) into memory only, trimmed, size-capped; a missing, empty, over-cap or unreadable
    secret is a Refusal naming the secret's NAME (never a value). Called BEFORE any cluster call, so a refusal leaves nothing made."""
    env = dict(os.environ if env is None else env)
    name = (env.get("CW_STACK_MODEL_SECRET") or "").strip()
    if not name:
        return None
    sub, vault = (env.get("CW_VAULT_SUBSCRIPTION") or "").strip(), (env.get("CW_VAULT_NAME") or "").strip()
    why = None
    ran = None
    if not sub or not vault:
        why = "CW_VAULT_SUBSCRIPTION and CW_VAULT_NAME are not set"
    else:
        ran = _capture(["az", "keyvault", "secret", "show", "--subscription", sub, "--vault-name", vault, "--name", name, "--query", "value", "-o", "tsv"],
                       env={k: v for k, v in env.items() if k not in CLUSTER_DROP}, limit=STACK_MODEL_KEY_LIMIT + 1)
        if ran is None or ran[0] != 0:
            why = "could not be read from the vault"
        elif len(ran[1]) > STACK_MODEL_KEY_LIMIT:
            why = "is over the %d byte cap" % STACK_MODEL_KEY_LIMIT
        elif not ran[1].strip():
            why = "is empty"
    if why:
        raise Refusal("the stack model secret %s %s; nothing was created" % (name, why))
    return name, ran[1].strip()


git_token_sleep = time.sleep   # tests replace it


def git_token(repo, cluster, role="worker"):
    """The one-hour git token for ONE repository: CW_GIT_TOKEN_CMD, else the mint (cloud/launcher/app_token.py REPO), as the driver does.
    The default mint gets `--role ROLE` for any role but the worker's (the same rule as the launcher's git_token_cmd); a custom command runs unchanged."""
    raw = os.environ.get("CW_GIT_TOKEN_CMD")
    argv = shlex.split(raw) if raw else [sys.executable, str(Path(__file__).resolve().parent.parent / "cloud" / "launcher" / "app_token.py"), repo]
    if not raw and role != "worker":
        argv += ["--role", role]
    env = {k: v for k, v in os.environ.items() if k not in CLUSTER_DROP}

    def last(ran):
        return _one(cluster.clean(ran[2].strip().splitlines()[-1], 200), 200) if ran and ran[2].strip() else "no message"

    # The retry rule (a copy of the launcher's tempfail_tries: this program loads the launcher by path only for other work, and a test
    # runs this function without it): again ONLY on status 75 (a custom command too), 4 tries, waits of 2, 5 and 10 seconds.
    tries = 0
    while True:
        tries += 1
        ran = _capture(argv, env=dict(env, GH_REPO=repo))
        if not ran or ran[0] != 75 or tries >= 4:
            break
        wait = (2, 5, 10)[tries - 1]
        print("git token try %d of 4 failed: %s; trying again in %d seconds" % (tries, last(ran), wait), file=sys.stderr)
        git_token_sleep(wait)
    token = (ran[1] if ran and ran[0] == 0 else "").strip()
    if not token or len(token) > 4096:
        why = ""
        if ran and ran[2].strip():
            why = ": " + last(ran)
        after = " after %d tries" % tries if tries > 1 else ""
        raise Refusal("the git token command gave no usable token%s%s; nothing was created" % (after, why))
    return token


# ---------------------------------------------------------------- launch

@refusals
def cmd_launch(args, runner, now):
    """`launch --cloud`: the name is reserved (O_EXCL) from before the local-agent and record checks until the launch ends, so no second command can pass
    the same checks in between."""
    with contextlib.ExitStack() as stack:
        return _launch(args, runner, now, lambda state: stack.enter_context(reservation(state, args.name)))


def _launch(args, runner, now, reserve):
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
    if args.agent == "orchestrator" and not args.reports_to:
        raise Refusal("--agent orchestrator needs --reports-to BOSS (the pod's FLEET_BOSS); nothing was created")
    if args.dir:
        given = str(args.dir)   # a normal absolute path (no `..`, no doubled or trailing slash) under a pod root
        if given != os.path.normpath(given) or not any(given == root or given.startswith(root + "/") for root in POD_DIR_ROOTS):
            raise LaunchError("--dir %r is not a path in the pod: with --cloud it must be an absolute path under %s (a local directory does not exist "
                              "there); leave --dir out to use %s; nothing was created" % (_one(given, 80), " or ".join(POD_DIR_ROOTS), DEFAULT_DIRECTORY))
    repo = H["launch_repo"](args)
    branch = getattr(args, "branch", None)
    if not repo or not branch:
        raise LaunchError("--cloud needs --repo OWNER/REPO and --branch BRANCH")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch) or branch.startswith("-"):
        raise LaunchError("--branch is not a branch name: %r" % branch)
    choice = H["launch_model_cloud"](args, MODEL_PROVIDER) if (getattr(args, "model", None) is not None or getattr(args, "reasoning", None) is not None) else {}   # #1642: before any setting, token or cluster call
    source = use_settings()   # before any setting is read: the allow-list, the preflight and the launcher all read this one source
    print("%s: cloud settings from %s" % (H["PROG"], source), file=sys.stderr)
    check_allowed(repo)   # before anything is read, minted or created
    if getattr(args, "stack", False) and args.shape and args.shape not in LAUNCHER_STACK_SHAPES:   # exact membership in a tuple; after the allow-list, before the preflight, any token or call
        raise Refusal("--shape %r is not a pod shape (%s); nothing was created" % (str(args.shape)[:40], " or ".join(LAUNCHER_STACK_SHAPES)))
    try:   # the repository preflight, before any token is minted; FAIL CLOSED: a missing, killed or silent program refuses (stdout stays the one JSON line)
        print(_lib().check_repo_preflight(repo, {k: v for k, v in os.environ.items() if k not in CLUSTER_DROP}), file=sys.stderr)
    except _lib().CloudError as err:
        _audit_refused_preflight(name, err)
        raise Refusal(str(err)) from None
    engine, state, _ = H["build_engine"](runner, now, "cli")
    reserve(state)
    local = engine.discover(engine.now()).agents.get(name)
    if local is not None:   # `route` prefers a cloud record: every later send/read/retire for the local agent would go INTO the pod
        raise LaunchError("a local fleet agent named %s exists: a cloud worker cannot take its name (send, read and retire by name would reach "
                          "the pod instead of the local agent); retire it or pick another name; nothing was created" % name)
    if load_record(state, name) is not None:
        raise LaunchError("a cloud worker named %s already exists (`%s retire %s` ends it)" % (name, H["PROG"], name))
    brief = H["read_brief"](args.brief_file, sys.stdin)
    run_id = secrets_module.token_hex(16)   # this command's nonce: recorded on what `create` makes, so a later stop can say it is ours
    options = [CREATE_OPTION["repo"], repo, CREATE_OPTION["branch"], branch, CREATE_OPTION["run_id"], run_id]
    for key, value in (("idle", args.idle), ("hard", args.hard), ("shape", args.shape)):
        if value:
            options += [CREATE_OPTION[key], str(value)]
    if args.agent == "orchestrator":   # only on request: a worker launch hands the launcher exactly what it always did
        options += [CREATE_OPTION["agent"], args.agent, CREATE_OPTION["boss"], args.reports_to]
    hub = bool((os.environ.get("CW_HUB_CHANNEL_URL") or "").strip() or (os.environ.get("CW_HUB_REGISTRY_SECRET") or "").strip() or args.agent == "orchestrator")   # the hub setting, or an orchestrator (the launcher registers its entry always: it needs the session)
    if hub and args.agent != "orchestrator":   # a worker joins the hub too: the launcher reads the setting itself, it only needs the boss
        options += [CREATE_OPTION["boss"], args.reports_to] if args.reports_to else []
    if getattr(args, "stack", False):   # only on request: a launch without --stack hands the launcher exactly what it always did
        options.append(CREATE_OPTION["stack"])
    if os.environ.get("CW_PULL_SECRET"):
        options += ["--pull-secret", os.environ["CW_PULL_SECRET"]]
    stack_model = stack_model_secret() if getattr(args, "stack", False) else None   # before any cluster call; without --stack the setting is ignored
    if stack_model:
        options += [CREATE_OPTION["stack_model_secret"], stack_model[0]]
    launcher_argv()   # a launcher that cannot be found refuses here (exit 2), before the vault, the cluster or the launcher is touched
    recovery = VaultRecovery(cloud_dir(state) / VAULT_LOCK_NAME, lambda detail: _audit(engine, "cloud.vault_check", name, detail))
    with Cluster(recovery) as cluster:
        model = model_entry()
        cluster.add(model)
        token = git_token(repo, cluster, args.agent if args.agent in _lib().AGENTS else "worker")   # the first token is minted under the role of the launch
        cluster.add(token)
        run_secrets = [("model-auth", model), ("git-token", token)]
        if stack_model:
            cluster.add(stack_model[1])
            run_secrets.append(("stack-model-key", stack_model[1]))
        read_fd, write_fd = os.pipe()
        try:
            try:
                code, body, note = run_launcher(runner, "create", name, options, LAUNCH_SECONDS, password_fd=write_fd, cluster=cluster,
                                                secrets=run_secrets)
            except CloudError as err:
                _stop_after_failure(engine, runner, name, run_id, "launch: the launcher did not finish", cluster)   # a pod made just before the cut is stopped too
                raise LaunchError(str(err)) from None
            except BaseException:
                os.close(write_fd)
                write_fd = None
                _stop_after_failure(engine, runner, name, run_id, "launch interrupted", cluster)
                raise
            finally:
                if write_fd is not None:
                    os.close(write_fd)
            del model, token, run_secrets
            if code != 0:
                _audit(engine, "cloud.create_failed", name, {"exit": code, "reason": _one(note, 160)})
                raise LaunchError("the cloud worker was not created: %s" % _reason(code, note))
            try:
                password = _read_password(read_fd)
            except CloudError as err:
                _stop_after_failure(engine, runner, name, run_id, "launch: no password", cluster)
                raise LaunchError(str(err)) from None
            cluster.add(password)
        finally:
            os.close(read_fd)
        try:
            record = _record_from_create(name, body)
            _write_private(password_path(state, name), password + "\n")
            record["auth_ref"] = "file:" + str(password_path(state, name))   # the switchboard's own copy: its path never comes from the launcher
            record["repo"], record["branch"] = repo, branch
            if stack_model:
                record["stack_model_secret"], record["stack_model_key"] = stack_model[0], "delivered"   # the name and that it was delivered; nothing else
            record["agent"] = args.agent   # the local role name, for the operator: the pod runs CLOUD_WORKER_AGENT
            if args.reports_to:
                record["reports_to"] = args.reports_to
            save_record(state, record)
        except (CloudError, OSError) as err:
            remove_record(state, name)
            _stop_after_failure(engine, runner, name, run_id, "launch: record not stored", cluster)
            raise LaunchError("the worker was created but its record could not be stored (%s); stop was requested" % _one(cluster.clean(str(err), 100), 100)) from None
        _audit(engine, "cloud.created", name, {"runtime_id": record["runtime_id"], "host": urllib.parse.urlsplit(record["url"]).hostname,
                                               "idle_deadline": record.get("idle_deadline"), "hard_deadline": record.get("hard_deadline")})
        try:   # a local agent may have taken the name while the pod was being made; discovery that cannot answer counts as a failure (cannot tell)
            taken = engine.discover(engine.now()).agents.get(name) is not None
            problem = "a local fleet agent named %s appeared while the pod was being made" % name if taken else None
        except Exception as err:
            problem = "the check for a local agent of that name failed (%s)" % type(err).__name__
        if problem:
            stopped = _stop_after_failure(engine, runner, name, run_id, "launch: " + _one(problem, 100), cluster)
            if stopped:
                remove_record(state, name)
            raise LaunchError("%s: %s (the worker %s was created; %s)" % (
                name, problem, name, "it was stopped and its record dropped" if stopped else "the stop failed: the pod and its record are kept; stop it with `%s retire %s`" % (H["PROG"], name)))
        fleet = H["fleet_record"](name, args.role, args.reports_to, None if args.issue is None else int(args.issue), args.charter, repo, *((None, None, choice) if choice else ()))
        try:
            with tunnel(cluster, record) as live:
                v2 = remote_v2(state, live, engine.config["call_timeout_seconds"])
                if choice:   # #1655: the pod's own model list, read once, before the session and the brief
                    _model_served(engine, name, choice["model"], v2)
                session = v2.session_create(args.agent if args.agent == "orchestrator" else CLOUD_WORKER_AGENT, name, args.dir or DEFAULT_DIRECTORY,
                                            {"fleet": fleet, "cloud": {"runtime_id": record["runtime_id"], "agent": CLOUD_WORKER_AGENT}},
                                            **(H["launch_model_kwargs"](choice) if choice else {}))["id"]
                record["session"] = session
                save_record(state, record)
                if hub:   # the hub entry gets the session id (the launcher's one verb for it; the cluster login is the launcher's own)
                    hcode, _, hnote = run_launcher(runner, "hub-session", name, ["--session", session], 120, cluster=cluster)
                    if hcode != 0:
                        raise CloudError("the hub entry could not be given the session: %s" % _reason(hcode, hnote))
                if brief:
                    v2.prompt(session, SETUP_PREAMBLE % {"repo": repo, "branch": branch} + brief, {"fleet": {"from": "switchboard", "brief": True}})
        except (H["V2Error"], H["V2Unreachable"], CloudError, OSError, KeyError, TypeError) as err:
            stopped = _stop_after_failure(engine, runner, name, run_id, "launch failed: %s" % _one(cluster.clean(str(err), 80), 80), cluster)
            if stopped:
                remove_record(state, name)
            raise LaunchError("%s (the worker was created; %s)" % (
                _one(cluster.clean(str(err))), "it was stopped" if stopped else "stop it with `%s retire %s`" % (H["PROG"], name))) from None
        except BaseException:
            if _stop_after_failure(engine, runner, name, run_id, "launch interrupted", cluster):
                remove_record(state, name)
            raise
    _registry_note(engine, "upsert", state, name, {"session": session, "repo": repo, "role": args.role, "kind": "cloud", "run_id": run_id, "state": "working"})   # a cloud worker is an agent (#903)
    print(json.dumps({"name": name, "session": session, "runtime_id": record["runtime_id"], "url": record["url"], "cloud": True}, sort_keys=True))
    return H["EXIT_OK"]


def _model_served(engine, name, model, v2):
    """#1655: ONE read of the pod's model list; a model its provider does not serve (providerID AND id) is refused with one line that lists at most
    twelve served ids of the provider that match the model-id pattern (never other pod text); the failure path of _launch stops the pod."""
    provider, _, mid = model.partition("/")
    try:
        listed = v2.model_list()
    except (H["V2Error"], H["V2Unreachable"], OSError):
        listed = None
    if not isinstance(listed, list):
        raise CloudError("--model %s could not be verified: the pod's model list could not be read" % model)
    rows = [m for m in listed[:2000] if isinstance(m, dict) and m.get("providerID") == provider and isinstance(m.get("id"), str)]
    if any(m["id"] == mid for m in rows):
        return
    ids = sorted({m["id"] for m in rows if H["LAUNCH_MODEL_RE"].fullmatch(m["id"])})[:12]
    _audit(engine, "cloud.model_not_served", name, {"model": model})
    raise CloudError("--model %s is not served by the pod's provider: it serves at most 12 of: %s" % (model, ", ".join(ids) or "none"))


def _registry_note(engine, what, state, name, *args):
    """The cloud launcher and reaper are WRITERS of the agent registry: `upsert` on launch, `retire` once the pod is stopped. The stop itself stays
    this launcher's act; a registry that cannot be written is said in the audit, never raised (the pod's own record is still the launcher's)."""
    try:
        registry = H["load_module"]("registry")
        got = registry.upsert(state, name, args[0]) if what == "upsert" else registry.retire(state, name, "cloud-worker", args[0])
        if got["problem"]:
            _audit(engine, "cloud.registry_error", name, {"why": _one(got["problem"], 120)})
    except Exception as err:
        _audit(engine, "cloud.registry_error", name, {"why": _one(str(err), 120)})


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


def _stop_after_failure(engine, runner, name, run_id, reason, cluster):
    """Best effort: stop a pod that a failed or interrupted launch left (the launcher deletes the pod and its per-run secret). The stop carries this
    command's run id, so a worker of the same name that is not this command's is left whole. A launcher whose `stop` does not take --run-id yet is
    stopped by name as before, and the audit line says which it was. True when stop exited 0."""
    old = _ignore_signals()   # a second Ctrl-C must not abandon the cleanup
    by = "run-id"
    try:
        code, _, note = run_launcher(runner, "stop", name, ["--reason", reason, "--run-id", run_id], STOP_SECONDS, cluster=cluster)
        if code == 2 and "--run-id" in (note or ""):   # the launcher does not know the option: usage error, nothing was stopped
            by = "name (the launcher's stop takes no --run-id)"
            code, _, note = run_launcher(runner, "stop", name, ["--reason", reason], STOP_SECONDS, cluster=cluster)
    except CloudError:
        return False
    finally:
        _restore_signals(old)
    _audit(engine, "cloud.stopped", name, {"exit": code, "why": "launch failed", "stopped_by": by, "run_id": run_id[:8]})
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


def _retired():
    """The read of a retired worker and the kept boss (bin/cloud_retired.py, beside this file), bound to the names it works with."""
    module = H["load_module"]("cloud_retired")
    module.bind(dict(H, _write_private=_write_private, _read_private=_read_private, read_record_file=read_record_file, parse_bounded=parse_bounded,
                     TooDeep=TooDeep, CloudError=CloudError, _lib=_lib, _scrub_lib=_scrub_lib, _may_retire=_may_retire))
    return module


def _keep_boss(state, record):
    """Keep the boss of a worker whose record goes (cloud_retired.keep_boss). A removal never fails for it, not even when the part cannot be loaded."""
    try:
        _retired().keep_boss(state, record)
    except Exception:   # nothing about the boss file may fail a removal
        pass


@refusals
def cmd_read(args, runner, now):
    """`read NAME [--limit N]`: the newest messages of an agent's session, oldest first. A cloud worker is read on its endpoint; a local agent on
    the local service (by the session the switchboard knows)."""
    LaunchError = H["LaunchError"]
    if not valid_name(args.name):
        raise LaunchError("name the agent: `%s read <name>`" % H["PROG"])
    limit = max(1, min(int(args.limit), 200))
    record = route(args.name)
    if record is None and getattr(args, "cloud_only", False):
        raise LaunchError("%s has no cloud record" % args.name)
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
        known = {} if seen else H["known_agent"](state, args.name)
        if known.get("kind") == "cloud":   # a retired cloud worker: its session lived on its own pod, never on this service
            known = {}
        session = seen.session_id if seen else known.get("session")
        if not session:
            retired = _retired().retired_run(args.name)   # nothing live answers to the name: a retired cloud worker's run record, last (#1546)
            if retired is not None:
                return _retired().read_run_record(args.name, retired, state, runner, now)
            raise LaunchError("%s is not a fleet agent this switchboard has seen" % args.name)
        v2 = engine.v2
    messages = v2.message_list(session, limit=limit, order="desc") or []
    for message in reversed([m for m in messages if isinstance(m, dict)]):
        print(message_line(message))
    return H["EXIT_OK"]


# ---------------------------------------------------------------- status

def _ended(record, clock=None):
    """True when the worker's hard deadline has passed: its pod is gone by design, so there is nothing to probe (and no token to mint for it)."""
    text = record.get("hard_deadline")
    try:
        when = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return False
    return when.timestamp() < (time.time() if clock is None else clock)


# ---------------------------------------------------------------- the one cluster read (the status summary)

SNAPSHOT_SECONDS = 30   # the bound of the WHOLE cloud summary read, ONE monotonic deadline: each step (the vault read, the token create, the launcher
#                         run) gets what is left of it, so the login and the launcher together never pass it (a step that starts after it refuses)


def _safe(value, default="Unknown"):
    """A cluster-supplied word that is printed (a pool, a size, a phase): the launcher's `safe_value` rule, applied again here (defence in depth)."""
    return _lib().safe_value(value) if value else default


def _cluster_snapshot(runner, cluster, timeout):
    """ONE read of the cluster: the launcher's read-only `snapshot` command (the pods of the namespace and, when the login may list them, the
    nodes). A failure is a CloudError with one line of reason: the caller says it, never hides it."""
    code, body, note = run_launcher(runner, "snapshot", "", (), timeout, cluster=cluster)
    if code != 0:
        raise CloudError("the cluster was not read (%s)" % _reason(code, note))
    if not isinstance(body, dict):
        raise CloudError("the cluster was not read (the launcher printed no snapshot)")
    return body


def _pod_known(record):
    """INVARIANT: a cloud worker record's runtime_id is `pod/<name>` (the form the launch writes; `tunnel` reads the same one). A record with any
    other form cannot be matched to a pod by name, so its pod is UNKNOWN: it is never called ended or gone, and `cloud-clear` never removes it."""
    return bool(POD_RE.fullmatch(record.get("runtime_id") or ""))


def _pod_of(record, snapshot):
    """The pod of the snapshot that belongs to the record (by the record's pod name and by the worker annotation), or None when there is none.
    Only for a record with `_pod_known`: for any other, None would read as gone, so the callers check that first."""
    runtime = (record.get("runtime_id") or "").strip()
    name = runtime.rsplit("/", 1)[-1] if "/" in runtime else runtime
    worker = record.get("worker")
    for pod in ((snapshot or {}).get("pods") or []):
        if not isinstance(pod, dict):
            continue
        if (pod.get("name") and pod.get("name") == name) or (pod.get("worker") and worker and pod.get("worker") == worker):
            return pod
    return None


def _age(seconds):
    """A span of time as one short word: 5d3h, 3h12m, 40m."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return "%dd%dh" % (days, hours)
    if hours:
        return "%dh%dm" % (hours, rest // 60)
    return "%dm" % (rest // 60)


def _node_age(node, now):
    """The node's age in seconds, or None when its creation time is not readable."""
    try:
        when = datetime.datetime.strptime(str(node.get("created")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None
    return max(0, int(now - when.timestamp()))


def _pods_by_phase(snapshot):
    """The pods of the snapshot by phase."""
    phases = {}
    for pod in ((snapshot or {}).get("pods") or []):
        phase = _safe(pod.get("phase")) if isinstance(pod, dict) else "Unknown"
        phases[phase] = phases.get(phase, 0) + 1
    return phases


def _summary_line(snapshot, now):
    """The ONE cloud summary line: the nodes by pool and size with their ages, and the pods of the namespace by phase. When the login may not
    LIST the nodes, the node count comes from the POD PLACEMENT (the distinct node names of the pods) and the line says so: it cannot see the
    idle nodes."""
    pods = (snapshot or {}).get("pods") or []
    phases = _pods_by_phase(snapshot)
    order = ("Running", "Pending", "Succeeded", "Failed", "Unknown")   # the phases in the order the line shows them
    pod_text = ", ".join("%s %d" % (phase, phases[phase]) for phase in sorted(phases, key=lambda p: (order.index(p) if p in order else len(order), p)))
    if (snapshot or {}).get("nodes_read"):
        groups = {}
        for node in (snapshot.get("nodes") or []):
            groups.setdefault(_safe(node.get("pool"), "unknown"), {}).setdefault(_safe(node.get("size"), "unknown"), []).append(node)
        parts = []
        for pool in sorted(groups):
            sizes = groups[pool]
            total = sum(len(v) for v in sizes.values())
            ages = [a for a in (_node_age(node, now) for node in [n for v in sizes.values() for n in v]) if a is not None]
            oldest = _age(max(ages)) if ages else "unknown"
            parts.append("%s %d x %s, oldest %s" % (pool, total, ", ".join("%d x %s" % (len(sizes[s]), s) for s in sorted(sizes)), oldest))
        nodes_text = "nodes %d (%s)" % (len(snapshot.get("nodes") or []), "; ".join(parts)) if parts else "nodes 0"
    else:
        names = {pod.get("node") for pod in pods if isinstance(pod, dict) and pod.get("node")}
        nodes_text = ("nodes at least %d carry workers (from pod placement; pool, size and age need list on nodes; idle nodes are not visible)"
                      % len(names))
    return "cloud: %s; pods %d (%s)" % (nodes_text, len(pods), pod_text)


def status_lines(runner, state, timeout=STATUS_SECONDS):
    """(lines, rows) for `status`: ONE cloud summary line from ONE cluster read, then per cloud worker its record and, from the launcher's
    read-only `status`, phase and cost, and whether the worker answers through a tunnel that exists only for that probe. A record whose hard
    deadline has passed and whose pod is gone is ENDED (the deadline is the time it ended); a record whose pod still stands says so. A trouble is
    said on the line, never raised."""
    records = list_records(state)
    lines, rows = [], []
    cluster, snapshot, reason = None, None, None
    started = time.monotonic()
    deadline = started + SNAPSHOT_SECONDS   # ONE deadline for the whole read: the login's commands and the launcher run share it
    left = lambda: deadline - time.monotonic()
    try:
        # the summary line is printed with or without records: a cleared fleet still shows what the cluster holds, or why it is not read
        try:
            cluster = Cluster(timeout=left).__enter__()
            if left() <= 0:
                raise CloudError("the read ran out of its %d s" % SNAPSHOT_SECONDS)
            snapshot = _cluster_snapshot(runner, cluster, max(1, int(left())))
        except CloudError as err:
            reason = _one(str(err), 300)
        if snapshot is None:
            lines.append("      cloud: cluster not read (%s) after %d s; nodes and pods unknown"
                         % (reason or "the launcher read nothing", int(time.monotonic() - started)))
        else:
            lines.append("      " + _summary_line(snapshot, time.time()))
        for record in records:
            row = {"worker": record["worker"]}
            if "problem" in record:
                row["problem"] = record["problem"]
                lines.append("      cloud %s: %s" % (record["worker"], record["problem"]))
                rows.append(row)
                continue
            row.update({key: record.get(key) for key in ("runtime_id", "idle_deadline", "hard_deadline", "session")})
            if _ended(record):
                pod = _pod_of(record, snapshot) if snapshot is not None else None
                if snapshot is not None and not _pod_known(record):   # its pod cannot be told: never "ended"
                    row["launcher_status"] = "pod unknown"
                    lines.append("      cloud %s: %s, past its hard deadline %s (pod unknown: the runtime id is not pod/<name>)"
                                 % (record["worker"], record["runtime_id"], record.get("hard_deadline")))
                elif snapshot is None:   # the cluster was not read: such a record is NEVER called ended
                    row["launcher_status"] = "no cluster reached"
                    lines.append("      cloud %s: %s, past its hard deadline %s (not queried; no cluster reached)"
                                 % (record["worker"], record["runtime_id"], record.get("hard_deadline")))
                elif pod is None:
                    row["launcher_status"] = "ended"
                    row["ended_at"] = record.get("hard_deadline")
                    lines.append("      cloud %s: %s, ended at %s (no pod), past its hard deadline"
                                 % (record["worker"], record["runtime_id"], record.get("hard_deadline")))
                else:
                    row["launcher_status"] = "past its hard deadline"
                    row["phase"] = pod.get("phase")
                    lines.append("      cloud %s: %s, pod still present past its hard deadline %s (phase %s)"
                                 % (record["worker"], record["runtime_id"], record.get("hard_deadline"), _safe(pod.get("phase"), "?")))
                rows.append(row)
                continue
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
                extra = "phase %s, about $%s so far" % (_safe(body.get("phase"), "?"), body.get("cost_estimate_usd", "?"))
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

def _may_retire(kind, record):
    """THE caller rule of a cloud worker's removal, factored ONCE: the operator (a person at a shell), the Chief of Staff, or the worker's own
    boss. `retire` and `cloud-clear` both use this one function; neither keeps a copy of it."""
    return kind[0] == "person" or (kind[0] == "agent" and ((len(kind) > 2 and kind[2] == "chief-of-staff") or kind[1] == record.get("reports_to")))


@refusals
def cmd_retire(args, runner, now, record):
    """`retire NAME` for a cloud worker: `stop` (the end hook runs, the pod and its scratch volume go), then the record goes. A stop that exits 1
    (the end hook could not push) keeps the pod AND the record, and the retire fails with that reason."""
    LaunchError = H["LaunchError"]
    engine, state, _ = H["build_engine"](runner, now, "cli")
    kind = H["gates_caller_kind"](runner, now)
    if not _may_retire(kind, record):
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
        sys.stderr.write("%s: %s was not retired: the end hook could not push (%s): pod kept, record kept; `%s retire %s` again once it can "
                         "(only if there is nothing to save, `cloud-worker delete %s --force` is the explicit last resort: it discards the pod and its work)\n"
                         % (H["PROG"], args.name, note or "no reason given", H["PROG"], args.name, args.name))
        print("%s: pod kept" % args.name)
        return H["EXIT_ERROR"]
    if code != 0:
        raise LaunchError("stop of %s failed: %s: the record is kept" % (args.name, _reason(code, note)))
    # The run record (slice 3, #638) is written by the launcher's stop; no call is needed here when it lands: this is its hook point (after the
    # end hook, before the endpoint record goes).
    _registry_note(engine, "retire", state, args.name, reason)
    wip, pushed = (body or {}).get("wip_branch"), (body or {}).get("pushed")
    if isinstance(body, dict) and body.get("pod_gone") is True:   # only the launcher's explicit reply: an unreadable pod never says it
        got = body.get("secrets_removed")
        got = [x for x in got if isinstance(x, str)][:10] if isinstance(got, list) else []
        _audit(engine, "cloud.name_freed", args.name, {"why": "pod gone", "runtime_id": record.get("runtime_id"), "secrets_removed": got})
        _keep_boss(state, record)
        remove_record(state, args.name)
        print("%s is retired: its pod was already gone; the name is free to launch again; %s" % (
            args.name, "Secrets removed: " + ", ".join(got) if got else "no Secrets were left"))
        return H["EXIT_OK"]
    _keep_boss(state, record)
    remove_record(state, args.name)
    print("%s is retired: its pod and scratch volume are deleted%s" % (
        args.name, "; work was pushed to branch %s" % wip if pushed and wip else "; nothing needed pushing" if pushed is False else ""))
    return H["EXIT_OK"]


# ---------------------------------------------------------------- cloud-clear: the records whose pod is gone

@refusals
def cmd_cloud_clear(args, runner, now):
    """`cloud-clear [--dry-run]`: every cloud worker record whose pod is gone AND whose hard deadline has passed is removed (the record and its
    password file), and every one it removes is named. A record with a pod, or one not past its deadline, is never touched. It refuses when the
    cluster could not be read: it never removes a record it cannot prove is gone. The caller rule is `retire`'s (the operator, the Chief of Staff
    or the worker's own boss)."""
    engine, state, _ = H["build_engine"](runner, now, "cli")
    kind = H["gates_caller_kind"](runner, now)
    records = [r for r in list_records(state) if "problem" not in r]
    if not records:
        print("no cloud worker records")
        return H["EXIT_OK"]
    try:
        with Cluster(timeout=SNAPSHOT_SECONDS) as cluster:
            snapshot = _cluster_snapshot(runner, cluster, SNAPSHOT_SECONDS)
    except CloudError as err:
        raise H["LaunchError"]("the cluster could not be read (%s); nothing was cleared" % _one(str(err), 200)) from None
    dry_run = bool(getattr(args, "dry_run", False))
    cleared = 0
    for record in records:
        name = record["worker"]
        if not _ended(record):   # not past its hard deadline: it may still be working
            continue
        if _pod_of(record, snapshot) is not None or not _pod_known(record):   # its pod still stands, or cannot be told: the record stays
            continue
        if not _may_retire(kind, record):
            print("skipped %s (not your worker)" % name)
            continue
        if dry_run:
            print("would clear %s (pod gone, deadline %s)" % (name, record.get("hard_deadline")))
            continue
        _keep_boss(state, record)
        remove_record(state, name)
        _audit(engine, "cloud.cleared", name, {"runtime_id": record.get("runtime_id"), "hard_deadline": record.get("hard_deadline")})
        _registry_note(engine, "retire", state, name, "cleared: the pod is gone and the hard deadline has passed")
        print("cleared %s (pod gone, deadline %s)" % (name, record.get("hard_deadline")))
        cleared += 1
    if not cleared and not dry_run:
        print("nothing to clear")
    return H["EXIT_OK"]
