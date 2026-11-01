"""The launcher process, the settings reader, the vault recovery, the cluster client and the tunnel of the cloud launch (ask #1647).

A part of bin/cloud_worker.py (the one that loads it and re-exports every name defined here); it holds the code that file needs to call the launcher
and the cluster, so that file stays within its size row. The text of each function is the text that stood in bin/cloud_worker.py: the only change
is that a name which stayed behind there is read as `D["name"]`, and `D` reads that file's own globals at each call, so a name patched on
bin/cloud_worker.py reaches this code. A name defined here and patched on bin/cloud_worker.py does not: patch it on this module.
"""
import contextlib
import datetime
import fcntl
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

D: object   # the names that stayed in bin/cloud_worker.py, set by its loader before this file runs


def checkout_launcher_path():
    """The launcher of the switchboard's OWN checkout: <checkout>/cloud/launcher/cloud-worker, found relative to this file (the checkout's real path:
    a link in the checkout's location is followed once, here, so the walk judges the real parents)."""
    return Path(D["__file__"]).resolve().parent.parent / "cloud" / "launcher" / "cloud-worker"


_WALK = []


def _walk():
    """The one walk (bin/path_walk.py, beside this file), loaded once by path from where THIS module was imported."""
    if not _WALK:
        spec = importlib.util.spec_from_file_location("path_walk", str(D["_OWN_BIN"] / "path_walk.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _WALK.append(module)
    return _WALK[0]


def _checkout_launcher_problem(path):
    """Why the checkout's launcher is not used, or None. The file and every parent go through the one walk of bin/path_walk.py (`entry_problem`,
    `parents_problem`: lstat, never followed). The entry itself is the kit's `cloud-worker` link to its sibling `cloud_worker.py` (a bare file name in
    the same directory): that target is the file judged: a regular file, executable, the user's, not group- or world-writable. A plain file is judged as it is."""
    walk = _walk()
    entry_problem, parents_problem = walk.entry_problem, walk.parents_problem
    try:
        found = os.lstat(path)
    except OSError:
        return "is not there"
    target = path
    if stat.S_ISLNK(found.st_mode):
        link = os.readlink(path)
        if not link or "/" in link or link in (".", ".."):
            return "is a link that does not name a file beside it"
        target = os.path.join(os.path.dirname(path), link)
        try:
            found = os.lstat(target)
        except OSError:
            return "is a link to a file that is not there"
    if not stat.S_ISREG(found.st_mode):
        return "is not a regular file"
    if not found.st_mode & 0o100:
        return "is not executable"
    return entry_problem(found, "itself") or parents_problem(str(target))


LAUNCHER_ORDER = ("variable", "checkout", "path")   # the lookup's sources, first wins; none left is a refusal (tests derive the ordering rules from this)


def launcher_argv():
    """The launcher program: the variable FLEET_CLOUD_WORKER_BIN when set (it wins, unchanged); else the launcher of this checkout when it is a safe
    executable; else `cloud-worker` on the PATH; else a refusal naming the variable and the checkout path looked in (exit 2, nothing called)."""
    given = os.environ.get(D["LAUNCHER_ENV"])
    if given:
        return [given]
    own = checkout_launcher_path()
    if _checkout_launcher_problem(str(own)) is None:
        return [str(own)]
    if shutil.which(D["DEFAULT_LAUNCHER"]):
        return [D["DEFAULT_LAUNCHER"]]
    raise D["Refusal"]("no cloud launcher found: set %s to the launcher program, or run the switchboard from a checkout that holds %s (that file %s), "
                  "or put `%s` on the PATH; nothing was called" % (D["LAUNCHER_ENV"], own, _checkout_launcher_problem(str(own)), D["DEFAULT_LAUNCHER"]))


STACK_MODEL_KEY_LIMIT = 16384   # bytes: a model API key is far smaller


def run_launcher(runner, command, worker, options=(), timeout=D["LAUNCH_SECONDS"], password_fd=None, cluster=None, secrets=()):
    """Run `cloud-worker <command> <worker> <options> --json`. Returns (exit code, parsed JSON or None, one-line stderr). A launcher that is missing
    or too slow is a CloudError with a plain reason. `cluster` (a Cluster) gives the launcher its login: a token FILE in the cluster's private
    directory, never a value in an argument or the environment. `secrets` [(name, value)] go to the launcher as per-run secrets through
    inherited pipes (`--run-secret-fd NAME=N`): a value is never in an argument, the environment or a file. `runner` is kept for the callers'
    signature; the launcher is started here so that an interrupt reaches IT (it cleans up what it made) before the interrupt goes on."""
    argv = launcher_argv() + [command] + ([worker] if worker else []) + list(options) + ["--json"]
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
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     pass_fds=tuple(pass_fds), env=env, start_new_session=True)
        except OSError as err:
            raise D["CloudError"]("cannot run the launcher %s: %s" % (argv[0], err.strerror or err)) from None
    except BaseException:
        for read_end, write_end, _ in feeds:
            _close_quietly(read_end, write_end)
        raise
    for read_end, write_end, value in feeds:
        _close_quietly(read_end)
        threading.Thread(target=_feed, args=(write_end, value), daemon=True).start()
    # the launcher's output is read by two threads, each keeping at most LAUNCHER_OUTPUT_LIMIT bytes (the rest is read and dropped, so the child
    # never blocks on a full pipe and memory stays bounded); an output over the limit is a bad read, never a partial parse
    kept = {"out": [b"", False], "err": [b"", False]}
    readers = [threading.Thread(target=_read_capped, args=(stream, kept[key]), daemon=True)
               for key, stream in (("out", child.stdout), ("err", child.stderr))]
    for reader in readers:
        reader.start()
    try:
        child.wait(timeout=timeout)
        for reader in readers:
            reader.join(timeout=10)
    except subprocess.TimeoutExpired:
        _kill_group(child)
        child.wait()
        raise D["CloudError"]("the launcher did not finish %s within %s s" % (command, timeout)) from None
    except BaseException:
        old = _ignore_signals()
        try:
            child.send_signal(signal.SIGINT)   # the launcher removes what it made, and says so
            child.wait(timeout=180)
        except BaseException:
            _kill_group(child)
        finally:
            _restore_signals(old)
        raise
    out, err = kept["out"][0].decode("utf-8", "replace"), kept["err"][0].decode("utf-8", "replace")
    if kept["out"][1]:
        raise D["CloudError"]("the launcher's output for %s was over %d bytes: not read" % (command, LAUNCHER_OUTPUT_LIMIT))
    try:
        body = json.loads(out) if (out or "").strip() else None
    except ValueError:
        body = None
    clean = cluster.clean if cluster is not None else (lambda text, limit=300: _scrub_lib()(text, (), limit))
    return child.returncode, body if isinstance(body, dict) else None, D["_one"](clean(err or "", 2000))


LAUNCHER_OUTPUT_LIMIT = 4000000   # bytes of the launcher's stdout (a snapshot of a large cluster is far below it)


def _read_capped(stream, slot, limit=None):
    """Read `stream` to its end into slot[0], keeping at most the limit; slot[1] says the limit was passed. The rest is read and dropped."""
    limit = LAUNCHER_OUTPUT_LIMIT if limit is None else limit
    chunks, size = [], 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            if size <= limit:
                chunks.append(chunk)
            size += len(chunk)
    except (OSError, ValueError):
        pass
    data = b"".join(chunks)
    slot[0], slot[1] = data[:limit], size > limit


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
    return {s: signal.signal(s, signal.SIG_IGN) for s in HANDLED_SIGNALS}


def _restore_signals(old):
    for sig, handler in old.items():
        signal.signal(sig, handler)


def _reason(code, note):
    return "%s%s" % (D["EXIT_REASONS"].get(code, "exit %d" % code), (": " + note) if note else "")


def _audit(engine, event, name, detail):
    if engine is not None and getattr(engine, "audit", None) is not None:
        engine.audit.append(engine.actor, event, name, detail)


def _audit_refused_preflight(name, err):
    """A launch refused because no token could be minted writes ONE line in the launcher's own audit file (its audit_path and write_audit,
    the Launcher.audit line shape), action create.failed; the launch has no Launcher object yet. Never raises, never a value."""
    text = str(err)
    if "no token could be minted" not in text:
        return
    try:
        lib = _lib()
        reason = text[text.index("no token could be minted"):]
        reason = "preflight: " + lib.scrub(reason, (), 400).split("; nothing was created")[0]
        import pwd
        lib.write_audit(lib.audit_path(os.environ), {"ts": lib.iso(datetime.datetime.now(datetime.timezone.utc)),
                                                     "worker": name, "runtime_id": "", "action": "create.failed", "cost_estimate_usd": 0.0,
                                                     "ok": False, "actor": pwd.getpwuid(os.getuid()).pw_name, "reason": reason})
    except Exception:
        pass


def _read_password(read_fd):
    chunks = b""
    while len(chunks) <= D["PASSWORD_MAX"] + 1:
        piece = os.read(read_fd, 256)
        if not piece:
            break
        chunks += piece
    text = chunks.decode("utf-8", "replace").strip()
    if not 0 < len(text) <= D["PASSWORD_MAX"] or re.search(r"[\x00-\x1f\x7f]", text):
        raise D["CloudError"]("the launcher did not hand over a usable server password")
    return text




VAULT_CHECK = ("run the platform team's firewall check for the vault (the vault pre-check) first; nothing was changed and no other credential "
               "was tried")
# What a vault refusal FOR THE FIREWALL looks like: the text `az` prints for the vault's data plane (Microsoft documents both: the message and the inner
# error code). Matched case-insensitively in the error text only (never printed). Any other vault error (no permission, no such secret, not signed
# in, unreachable) matches neither and is never retried and never runs the check.
FIREWALL_MARKERS = ("client address is not authorized and caller is not a trusted service", "forbiddenbyfirewall")
VAULT_LOCK_NAME = "vault-check.lock"
VAULT_LOCK_GRACE = 45          # on top of the check's own run limit: how long a second launch waits for the lock
VAULT_LOCK_POLL = 0.2
VAULT_OWNER = "the owner of the vault access check"
MAX_TOKEN_SECONDS = 3600
HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
TOKEN_CMD_SECONDS = 60
CLUSTER_DROP = ("CW_KUBE_CONTEXT", "CW_KUBECONFIG", "KUBECONFIG", "CW_CLOUD_SERVER", "CW_CLOUD_TOKEN_FILE", "CW_CLOUD_CA_FILE",
                "CW_CLUSTER_TOKEN_CMD", "CW_CLUSTER_ADMIN_CMD")
POD_RE = re.compile(r"pod/([a-z0-9][a-z0-9.-]{0,100})")
MODEL_PROVIDER = "github-copilot"
_LIB = {}


def _lib():
    """The launcher's own module (cloud/launcher/cloud_worker.py), loaded once by path: its ONE scrub function and secret forms are used here,
    there is no second copy."""
    if not _LIB:
        path = Path(D["__file__"]).resolve().parent.parent / "cloud" / "launcher" / "cloud_worker.py"
        spec = importlib.util.spec_from_file_location("cw_launcher_lib", str(path))
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except (OSError, SyntaxError) as err:
            raise D["CloudError"]("the launcher library is not found beside this program (%s)" % D["_one"](str(err), 80)) from None
        _LIB["m"] = module
    return _LIB["m"]


def _scrub_lib():
    return _lib().scrub


def use_settings():
    """THE settings source of every cloud command: the launcher library's one reader (`apply_settings`): the private settings file when it
    exists, else the environment as before. Returns the source (the file's path or "environment"). A refusal is exit 2, nothing called."""
    try:
        return _lib().apply_settings()
    except _lib().CloudError as err:
        raise D["Refusal"](str(err).replace("refused: ", "", 1)) from None


_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")


def _repo_key(text):
    """OWNER/NAME lower-cased (ASCII), or None when it is not exactly that shape. Identical in behaviour to the launcher's `_repo_key`."""
    owner, sep, name = (text or "").partition("/")
    if not sep or not _OWNER_RE.fullmatch(owner) or not _NAME_RE.fullmatch(name) or set(name) == {"."} or name.lower().endswith(".git"):
        return None
    return (owner + "/" + name).lower()


def check_allowed(repo, env=None):
    """The allow-list. A COPY of the launcher's `check_repo_allowed` (cloud/launcher/cloud_worker.py, #659) with the same behaviour, so the two
    entry points accept the same repositories from the same variable (TestSameAllowList in bin/test-cloud-worker runs both over the same inputs).
    TODO(follow-up once #659 is in the base): replace this copy by a call to `_lib().check_repo_allowed(repo, env)`.
    CW_ALLOWED_REPOS: comma-separated OWNER/NAME, exact, ASCII case-insensitive; unset, empty or ANY malformed entry refuses the whole list.
    CW_TOOLING_REPO: refused even when listed; unset adds no check, a malformed value refuses. Runs before anything is created."""
    env = os.environ if env is None else env
    raw = (env.get("CW_ALLOWED_REPOS") or "").strip()
    if not raw:
        raise D["Refusal"]("%s is not on the cloud allow-list (CW_ALLOWED_REPOS is unset or empty); nothing was created" % D["_one"](str(repo), 120))
    allowed = [_repo_key(x.strip()) for x in raw.split(",")]
    if None in allowed:
        raise D["Refusal"]("CW_ALLOWED_REPOS has an entry that is not OWNER/NAME; nothing was created")
    tooling = (env.get("CW_TOOLING_REPO") or "").strip()
    tkey = _repo_key(tooling) if tooling else None
    if tooling and tkey is None:
        raise D["Refusal"]("CW_TOOLING_REPO is not OWNER/NAME; nothing was created")
    key = _repo_key(repo) if isinstance(repo, str) else None
    if key is None or key not in allowed or key == tkey:
        raise D["Refusal"]("%s is not on the cloud allow-list; nothing was created" % D["_one"](str(repo), 120))


def _capture(argv, env=None, feed=None, timeout=TOKEN_CMD_SECONDS, limit=1000000):
    """Run a short helper (a vault read, a mint). `feed` is a text handed over a pipe: the argument `{fd}` in argv names it, so it is never in an
    argument, the environment or a file. Returns (exit code, stdout, stderr), or None when it could not run or was too slow. The one
    implementation is the launcher's own (the reaper's refresh runs the same pipe)."""
    return _lib().capture_command(argv, env=env, feed=feed, timeout=timeout, limit=limit)


def _jwt_lifetime(token, now):
    """Seconds the token lives, read from its exp (and iat) claim when it is a JWT; None when it is not one (the requested duration is trusted)."""
    return _lib().jwt_lifetime(token, now)


def _duration(env):
    """The token's requested life: CW_CLUSTER_TOKEN_DURATION (default 20m: the shortest that covers a create and a stop; kubectl's own floor is
    10m), never over 60m."""
    text = (env.get("CW_CLUSTER_TOKEN_DURATION") or "20m").strip()
    found = re.fullmatch(r"(\d{1,3})([mh])", text)
    minutes = int(found.group(1)) * (60 if found.group(2) == "h" else 1) if found else 0
    if not 1 <= minutes <= 60:
        raise D["Refusal"]("CW_CLUSTER_TOKEN_DURATION must be between 1m and 60m (got %r); nothing was called" % text[:20])
    return "%dm" % minutes


def firewall_refused(ran):
    """True when a vault read's result (exit code, stdout, stderr; None when it could not run) is the vault's FIREWALL refusing this machine."""
    return _lib().firewall_refused(ran)


class VaultRecovery:
    """After a vault read was refused for the firewall: run the vault access check ONCE, then read again ONCE. One exclusive file lock (0600, in the
    private state directory) is held around the check and the read after it, because the check has no lock of its own and two runs at once are
    untested. A launch that has to WAIT for the lock reads the vault again first: the run it waited for may have fixed it, and then it does not run
    the check. `audit(detail)` writes the one audit line of a run (an outcome, the exit code and the counts: never an address)."""

    def __init__(self, lock_path, audit=None, sleep=time.sleep, clock=time.monotonic):
        self.lock_path, self.audit, self.sleep, self.clock = str(lock_path), audit, sleep, clock

    def _lock(self):
        problem = _lib().parents_problem(os.path.abspath(self.lock_path))   # the same walk as the checkout's: the lock file's parents
        if problem:
            raise D["Refusal"]("the vault check lock %s; refused, nothing was called" % problem)
        try:
            fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            found = os.fstat(fd)
            if found.st_uid != os.getuid() or not stat.S_ISREG(found.st_mode):
                os.close(fd)
                raise D["Refusal"]("the vault check lock is not the user's own regular file; refused, nothing was called")
            os.fchmod(fd, 0o600)
        except OSError as err:
            raise D["Refusal"]("the vault check lock cannot be made (%s); nothing was called" % (err.strerror or err)) from None
        waited, deadline = False, self.clock() + _lib().VAULT_CHECK_SECONDS + _lib().VAULT_CHECK_GRACE + VAULT_LOCK_GRACE
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd, waited
            except OSError:
                waited = True
            if self.clock() >= deadline:
                os.close(fd)
                raise D["Refusal"]("the vault check lock %s was not freed in time (another launch is running the vault access check); nothing was created" % VAULT_LOCK_NAME)
            self.sleep(VAULT_LOCK_POLL)

    def __call__(self, read, env):
        lib = _lib()
        try:
            lib.vault_check_ready(env)   # a missing key or a bad checkout refuses here, before the lock, and runs nothing
        except lib.CloudError as err:
            raise D["Refusal"](str(err).replace("refused: ", "", 1)) from None
        fd, waited = self._lock()
        try:
            if waited:   # the run we waited for may have fixed it
                ran = read()
                if not firewall_refused(ran):
                    return ran
            try:
                result = lib.run_vault_check(env)
            except lib.CloudError as err:
                raise D["Refusal"](str(err).replace("refused: ", "", 1)) from None
            if self.audit is not None:
                self.audit({"exit": result.code, "outcome": result.outcome, "rules_before": result.before, "rules_after": result.after})
            code = result.effective
            if not lib.VAULT_CHECK_CODES[code][1]:
                raise D["Refusal"]("the vault access check stopped this launch (exit %s: %s); ask %s; it adds at most one address and never removes one, "
                              "so nothing more is done here; nothing was created" % (result.code if result.code is not None else "none", lib.VAULT_CHECK_CODES[code][2], VAULT_OWNER))
            if code == 10:
                sys.stderr.write("%s: the vault access check added an address: %s: forward this line to whoever records it\n"
                                 % (D["H"]["PROG"], result.line or "its count line was not found in its output; ask " + VAULT_OWNER))
            return read()
        finally:
            os.close(fd)   # releases the lock


def _token_refusal(err):
    """The refusal of a cluster token that could not be made: the wording of the step that failed, never a value."""
    if err.kind == "too-long":
        return "the cluster token lives %d seconds, over the 60 minutes allowed (or already expired); refused, nothing was called" % err.lifetime
    why = {"token-command-failed": "the token command failed",
           "vault-settings-missing": "set CW_VAULT_SUBSCRIPTION and CW_VAULT_NAME",
           "admin-unreadable": "the administrator credential could not be read from the vault" +
           (" even after the vault access check; ask " + VAULT_OWNER if err.recovered else ""),
           "mint-failed": "the token could not be minted",
           "not-a-token": "the answer is not a token"}.get(err.kind, "the token could not be obtained")
    return "the cluster token could not be obtained (%s): %s" % (why, VAULT_CHECK)


def cluster_token(env=None, now=None, recovery=None):
    """A cluster token of at most one hour, minted NOW, in memory. CW_CLUSTER_TOKEN_CMD (a command that prints one), else the default: the cluster
    administrator kubeconfig is read from the vault (CW_CLUSTER_ADMIN_CMD, else `az keyvault secret show` with CW_VAULT_SUBSCRIPTION and
    CW_VAULT_NAME) and `kubectl create token cw-operator --duration=20m` is run with it on a pipe. Nothing is cached or kept. A failure is a
    Refusal that names the vault pre-check; no other credential or context is ever tried. With `recovery` (launch only), a vault read refused for
    the firewall runs the vault access check once and reads once more. The route itself is the launcher's own function (the reaper's refresh
    runs the same one): this is the thin caller that keeps the wording and the recovery."""
    return _cluster_token_bounded(env, now, recovery, TOKEN_CMD_SECONDS)


def _cluster_token_bounded(env, now, recovery, timeout):
    """The same mint as `cluster_token`, with `timeout` bounding each command of it (the status summary's bound; the launch entry passes the default)."""
    env = dict(os.environ if env is None else env)
    try:
        def capture(argv, env=None, feed=None):
            seconds = timeout() if callable(timeout) else timeout   # a callable gives what is LEFT of a whole-run deadline, read at each command
            return None if seconds <= 0 else _capture(argv, env=env, feed=feed, timeout=seconds)   # None is the route's "could not run or too slow"
        return _lib().cluster_token(env, duration=_duration(env), capture=capture, now=now, recovery=recovery)
    except _lib().ClusterLogin as err:
        raise D["Refusal"](_token_refusal(err)) from None


def private_root():
    """The per-user directory (0700, ours, not a link) under the temp dir that holds the token files of running commands. One that has another
    owner, another mode or is a link is REFUSED, never repaired."""
    root = Path(tempfile.gettempdir()) / ("cwtok-%d" % os.getuid())
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        found = os.lstat(str(root))
    except OSError as err:
        raise D["Refusal"]("the private directory for the cluster token cannot be made (%s); nothing was called" % (err.strerror or err)) from None
    if stat.S_ISLNK(found.st_mode) or not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid() or stat.S_IMODE(found.st_mode) != 0o700:
        raise D["Refusal"]("the private directory %s is not the user's own with mode 0700; refused, nothing was called" % root)
    return root


def _ps(pid, field):
    """One `ps -o FIELD= -p PID` value (the process's start time or full command line), or None when the process is gone or ps cannot say."""
    try:
        out = subprocess.run(["ps", "-o", field + "=", "-p", str(int(pid))], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}, timeout=10).stdout.decode("utf-8", "replace").strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return " ".join(out.split()) or None


def stop_recorded_tunnels(directory):
    """Stop an orphaned `kubectl port-forward` of a dead command: the pid and start time it recorded in its directory. A pid is signalled only when
    the process STILL has the recorded start time AND its command line is a kubectl port-forward that uses THIS directory's kubeconfig: a reused
    pid (another program, or another run's tunnel) is never touched."""
    stopped = 0
    config = os.path.join(directory, "config")
    for note in sorted(Path(directory).glob("tunnel-*.pid")):
        try:
            pid_text, _, started = D["_read_private"](note, 400).partition(" ")
            pid = int(pid_text)
        except (D["CloudError"], OSError, ValueError):
            continue
        command = _ps(pid, "command")
        if pid <= 1 or not started or _ps(pid, "lstart") != started or not command or "kubectl" not in command \
                or "port-forward" not in command or config not in command:
            continue
        gone = False
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if _ps(pid, "lstart") != started:   # re-verified right before each signal
                gone = True
                break
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, sig)
                else:
                    os.kill(pid, sig)
            except OSError:
                break
            time.sleep(0.2)
        gone = gone or _ps(pid, "lstart") != started
        stopped += 1 if gone else 0
    return stopped


def sweep_stale(root):
    """At the start of every command: remove what a command killed by SIGKILL left: a `cmd-<pid>-*` directory of ours whose process is gone, or
    that is older than the token's longest life, and STOP the `kubectl port-forward` that command left running (see stop_recorded_tunnels).
    Nothing else is touched. Says so once when it removed one (never what was in it)."""
    removed = 0
    tunnels = 0
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
            tunnels += stop_recorded_tunnels(str(entry))
            shutil.rmtree(str(entry), ignore_errors=True)
            removed += 1
    if removed:
        sys.stderr.write("%s: removed %d leftover cluster-token directory of an earlier command that was killed%s\n"
                         % (D["H"]["PROG"], removed, " and stopped %d tunnel it left" % tunnels if tunnels else ""))


class Cluster:
    """The login for ONE command. `with Cluster() as cluster:` mints a token, writes it to a 0600 O_EXCL file in a private 0700 directory (the only
    place it touches disk: the launcher's tokenFile mechanism needs a file), and removes the directory on every way out: normal end, failure,
    SIGINT, SIGTERM, SIGHUP and SIGQUIT. A stale directory of a killed command is swept at the start of every command. No token is kept between commands."""

    def __init__(self, recovery=None, timeout=TOKEN_CMD_SECONDS):   # `timeout`: seconds per command of the mint, or a callable (see _cluster_token_bounded)
        self.recovery = recovery
        self.timeout = timeout
        self.known = set()
        self.dir = None
        self.kubeconfig = None
        self.token_file = None
        self.server = None
        self.old = {}
        self.tunnels = []

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            # the SAME handler for every way a terminal or a person ends a command: a closed window (SIGHUP) and SIGQUIT take their default action
            # otherwise, which ends the process with no cleanup and leaves the tunnel (its own session gets no hangup) running
            self.old = {sig: signal.signal(sig, self._on_signal) for sig in HANDLED_SIGNALS}
        try:
            use_settings()   # idempotent: the same file gives the same values; send, read, retire and status log in from the file too
            server = (os.environ.get("CW_CLOUD_SERVER") or "").strip()
            if not server:
                raise D["Refusal"]("CW_CLOUD_SERVER is not set: no cluster to reach; nothing was changed")
            root = private_root()
            sweep_stale(root)
            token = _cluster_token_bounded(None, None, self.recovery, self.timeout)
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
            raise D["Refusal"]("the token file cannot be written safely (%s); nothing was called" % (err.strerror or err)) from None
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_):
        self.close()
        return False

    def _on_signal(self, signum, frame):
        for sig in HANDLED_SIGNALS:   # a second signal must not interrupt the cleanup
            signal.signal(sig, signal.SIG_IGN)
        for child in list(self.tunnels):   # stop the tunnels now, whatever the stack is doing; the `finally` blocks then remove the files
            _kill_group(child)
        raise KeyboardInterrupt

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


@contextlib.contextmanager
def tunnel(cluster, record):
    """A `kubectl port-forward` to the worker's pod that exists for the `with` block only. Yields the record with its url set to the loopback address
    chosen now (never stored). The child has its own process group and is killed on every way out, interrupts included."""
    found = POD_RE.fullmatch(record.get("runtime_id") or "")
    if not found:
        raise D["CloudError"]("the record of %s names no pod" % record.get("worker"))
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    argv = [os.environ.get("CW_KUBECTL", "kubectl"), "--kubeconfig", cluster.kubeconfig, "--context", "cw", "-n", os.environ.get("CW_CLOUD_NAMESPACE", "cw"),
            "port-forward", "pod/" + found.group(1), "%d:4096" % port, "--address", "127.0.0.1"]
    log_path = os.path.join(cluster.dir, "forward-%d.log" % port)
    # a signal that arrives between the fork and the registration would end the command with the tunnel unknown to the handler and to the `finally`
    # below: hold the handled signals back for the spawn and the registration; one that came meanwhile is delivered at the unblock, tunnel known
    masked = threading.current_thread() is threading.main_thread() and hasattr(signal, "pthread_sigmask")
    held = signal.pthread_sigmask(signal.SIG_BLOCK, HANDLED_SIGNALS) if masked else None
    try:
        with open(log_path, "wb") as log:
            try:
                child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=cluster.env(), start_new_session=True,
                                         preexec_fn=(lambda: signal.pthread_sigmask(signal.SIG_SETMASK, [])) if masked else None)   # the tunnel starts unblocked
            except OSError as err:
                raise D["CloudError"]("cannot open the tunnel: %s" % (err.strerror or err)) from None
        cluster.tunnels.append(child)
    finally:
        if masked:
            signal.pthread_sigmask(signal.SIG_SETMASK, held)
    try:
        # the pid and start time go in the command's private directory: if this command is killed outright (SIGKILL), the NEXT command stops
        # the orphaned port-forward (only while the pid still has this start time and is a port-forward of this directory)
        started = None
        for _ in range(5):   # ps can fail for a moment on a loaded machine
            started = _ps(child.pid, "lstart")
            if started:
                break
            time.sleep(0.2)
        if not started:
            sys.stderr.write("%s: warning: the tunnel could not be recorded; if this command is killed outright it may leave the tunnel running\n" % D["H"]["PROG"])
        else:
            fd = os.open(os.path.join(cluster.dir, "tunnel-%d.pid" % port), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as note:
                note.write("%d %s" % (child.pid, started))
        deadline = time.monotonic() + float(os.environ.get("CW_TUNNEL_TIMEOUT") or 30)
        ready = ("Forwarding from 127.0.0.1:%d" % port).encode()
        while True:
            try:
                with open(log_path, "rb") as log:
                    said = log.read(4000)
            except OSError:
                said = b""
            # ready only when OUR kubectl says it is forwarding on this port and is still alive: a port that merely answers might be another
            # process that took it first, and the worker's password must never go there
            if ready in said and child.poll() is None:
                break
            if child.poll() is not None or time.monotonic() >= deadline:
                why = said[:400].decode("utf-8", "replace")
                raise D["CloudError"]("the tunnel to %s did not open%s" % (record.get("worker"), (": " + D["_one"](cluster.clean(why), 160)) if why.strip() else ""))
            time.sleep(0.1)
        yield dict(record, url="http://127.0.0.1:%d" % port)
    finally:
        _kill_group(child)
        if child in cluster.tunnels:
            cluster.tunnels.remove(child)
