#!/usr/bin/env python3
"""cloud-worker: launcher, reaper and audit for cloud workers (slice B).

Built to docs/cloud-worker-interface.md. Commands: create, stop, extend, delete, status, reap.
The cloud is reached only through `kubectl` (the binary is CW_KUBECTL, so tests fake it). Nothing here
holds a credential. Two ways to log in, never mixed: kubectl's own explicit context (CW_KUBE_CONTEXT, CW_KUBECONFIG), or
the namespace service account's token FILE (CW_CLOUD_SERVER, CW_CLOUD_TOKEN_FILE, optional CW_CLOUD_CA_FILE), for which a
minimal 0600 kubeconfig holding only the file's path is generated in a private temp dir and removed at exit. A per-run
secret value is read once from an inherited descriptor and goes to kubectl's stdin only.
"""
import argparse
import base64
import copy
import datetime as dt
import pwd
import hashlib
import json
import os
import re
import math
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_record  # noqa: E402  (the run record: one module, one function the stop calls)

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")  # the switchboard's agent name rule
SHAPES = {"S": {"cpu": "2", "memory": "4Gi", "usd_per_hour": 0.10},
          "M": {"cpu": "4", "memory": "8Gi", "usd_per_hour": 0.20}}  # list-price estimates, not billed figures
MAX_TOKEN_SECONDS = 3600

# The pod shape of a worker that runs a nested Docker stack (`create --stack`, opt-in; the default pod is unchanged).
# Shape S (2 CPU, 4 GiB limits) is the smallest existing shape that holds the stack lane's measured peak (1.0-1.35 GiB,
# 1-2 CPUs) plus the worker and the daemon; the hard limits stay, never unlimited.
STACK_RUNTIME_CLASS = "sysbox-runc"
STACK_NODE_SELECTOR = {"sysbox-install": "yes"}   # the POOL label; never gate on sysbox-runtime=running (set before containerd knows the runtime)
# live-probed with the cluster default only; the smallest set that lets a non-root worker start the daemon on Sysbox is to be
# determined by the next live run, one variation at a time. Start: sudo needs allowPrivilegeEscalation (no_new_privs would
# stop `sudo start-dockerd`); every capability stays dropped and none is added; never privileged. The pod-level seccomp profile
# (RuntimeDefault) was not part of the probe either.
STACK_SECURITY_CONTEXT = {"allowPrivilegeEscalation": True, "capabilities": {"drop": ["ALL"]}}
STACK_RACE_WINDOW_SECONDS = 240   # a fresh node's containerd is configured ~30-60 s after the pod is scheduled; this bounds the retries
STACK_ATTEMPTS = 3
STACK_RACE_RE = re.compile(r"RuntimeClass handler \S+ does not support user namespaces: the handler \S+ is not known")


def _containers(pod):
    spec = pod.get("spec", {})
    return [k for key in ("containers", "initContainers", "ephemeralContainers") for k in spec.get(key, [])]


def _cap(name):
    """A capability name as the container runtime reads it: case-insensitive, with or without a leading CAP_."""
    n = str(name).upper()
    return n[4:] if n.startswith("CAP_") else n


def _caps_add(pod, wanted):
    return any(_cap(c) in wanted for k in _containers(pod) for c in ((k.get("securityContext") or {}).get("capabilities") or {}).get("add") or [])


def _is_stack(pod):
    return "cw/stack" in (pod.get("metadata", {}).get("annotations") or {}) or "runtimeClassName" in pod.get("spec", {})


def _stack_context_differs(pod):
    """Allow-list: on a stack pod EVERY container's securityContext (init and ephemeral too) must equal STACK_SECURITY_CONTEXT,
    so any other capability, a Unconfined profile, procMount, a root runAsUser... fails until the constant and its test change together."""
    return _is_stack(pod) and any((k.get("securityContext") or {}) != STACK_SECURITY_CONTEXT for k in _containers(pod))


def _stack_shape_differs(pod):
    spec = pod.get("spec", {})
    return _is_stack(pod) and not (
        spec.get("runtimeClassName") == STACK_RUNTIME_CLASS and spec.get("hostUsers") is False
        and spec.get("nodeSelector") == STACK_NODE_SELECTOR
        and (pod.get("metadata", {}).get("annotations") or {}).get("cw/stack") == "true"
        and spec.get("securityContext") == POD_SECURITY_CONTEXT)   # the WHOLE pod-level context: fsGroup, runAsGroup, supplementalGroups, sysctls, seLinuxOptions...


# The allow-list of a stack pod's REST: a key outside these sets fails, so nodeName, tolerations, hostAliases, shareProcessNamespace,
# dnsPolicy/dnsConfig, priorityClassName, hostname, an init or ephemeral container, a hostPort... are refused until a deliberate edit
# here (and to the frozen fixture tests/fixtures/stack_pod.json) allows them.
STACK_METADATA_KEYS = {"name", "labels", "annotations"}
STACK_SPEC_KEYS = {"restartPolicy", "serviceAccountName", "automountServiceAccountToken", "securityContext", "containers", "volumes",
                   "imagePullSecrets", "runtimeClassName", "hostUsers", "nodeSelector"}
STACK_CONTAINER_KEYS = {"name", "image", "securityContext", "env", "resources", "readinessProbe", "volumeMounts"}
STACK_VOLUMES = {"tokens": {"name": "tokens", "emptyDir": {"medium": "Memory"}}, "scratch": {"name": "scratch", "emptyDir": {}}}
STACK_MOUNT_PATHS = {"pw": "/run/cw", "tokens": "/run/cw-tmp", "scratch": "/work"}


def _stack_rest_differs(pod):
    if not _is_stack(pod):
        return False
    spec, ks = pod.get("spec", {}), pod.get("spec", {}).get("containers", [])
    vols = spec.get("volumes", [])
    pw = [v for v in vols if v.get("name") == "pw"]
    return (
        not set(pod.get("metadata", {})) <= STACK_METADATA_KEYS
        or not set(spec) <= STACK_SPEC_KEYS
        or spec.get("restartPolicy") != "Never" or not isinstance(spec.get("serviceAccountName"), str) or not spec.get("serviceAccountName")
        or len(ks) != 1 or [k.get("name") for k in ks] != ["worker"]   # one container, named worker: no sidecar, even one with the stack context
        or any(not set(k) <= STACK_CONTAINER_KEYS or not isinstance(k.get("image"), str) or not k.get("image") for k in ks)
        or any(set(e) != {"name", "value"} for k in ks for e in k.get("env", []))   # no valueFrom / secretKeyRef; no envFrom (key allow-list)
        or any(set((k.get("resources") or {})) != {"limits"} or set(k["resources"]["limits"]) != {"cpu", "memory"} for k in ks)   # limits stay
        or any(k.get("readinessProbe") != {"tcpSocket": {"port": 4096}} for k in ks)
        or any(set(m) - {"name", "mountPath", "readOnly"} or STACK_MOUNT_PATHS.get(m.get("name")) != m.get("mountPath")
               for k in ks for m in k.get("volumeMounts", []))
        or sorted(v.get("name") for v in vols) != ["pw", "scratch", "tokens"]   # no extra emptyDir, secret or configMap volume
        or any(v != STACK_VOLUMES[v["name"]] for v in vols if v.get("name") in STACK_VOLUMES)
        or len(pw) != 1 or len(set(pw[0]) - {"name"}) != 1 or not set(pw[0]) - {"name"} <= {"secret", "projected"})


# The forbidden pod fields: ONE table (name -> does this pod have it). render_pod refuses a pod that has any of them (every
# pod, stack or not); the tests parametrise over this table, so a member added here is tested without another edit.
FORBIDDEN_POD_FIELDS = {
    "hostPath": lambda p: any("hostPath" in v for v in p.get("spec", {}).get("volumes", [])),
    "docker.sock": lambda p: "docker.sock" in json.dumps(p.get("spec", {})).lower(),
    "privileged": lambda p: any((k.get("securityContext") or {}).get("privileged") for k in _containers(p)),
    "hostNetwork": lambda p: bool(p.get("spec", {}).get("hostNetwork")),
    "hostPID": lambda p: bool(p.get("spec", {}).get("hostPID")),
    "hostIPC": lambda p: bool(p.get("spec", {}).get("hostIPC")),
    "hostUsers true": lambda p: p.get("spec", {}).get("hostUsers") is True,
    "capabilities add SYS_ADMIN/ALL": lambda p: _caps_add(p, ("SYS_ADMIN", "ALL")),
    "stack security context differs from STACK_SECURITY_CONTEXT": _stack_context_differs,
    "stack pod shape differs": _stack_shape_differs,
    "stack pod has a field or object outside the stack allow-list": _stack_rest_differs,
    "service-account token": lambda p: p.get("spec", {}).get("automountServiceAccountToken") is not False
        or any("serviceAccountToken" in (s or {}) for v in p.get("spec", {}).get("volumes", []) for s in (v.get("projected") or {}).get("sources", [])),
}


def forbidden_pod_fields(pod):
    return [name for name, has in FORBIDDEN_POD_FIELDS.items() if has(pod)]
DEFAULT_MAX_HARD = "8h"  # OPEN in the contract: value
DEFAULT_SPEND_CAP_USD = 25.0  # the prototype's cap; applies when CW_CLOUD_SPEND_CAP_USD is unset or empty
LABEL = "cw-worker"
REPORT_PATH = "/home/worker/report"  # the worker's final one-line report, written here inside the pod (the image's workdir)
REPORT_MAX = 2000
# the worker owns the file, and `head` follows a link: refuse anything but a regular file BEFORE reading ($1 the path, $2 the byte limit)
REPORT_READ = 'if [ -L "$1" ] || { [ -e "$1" ] && [ ! -f "$1" ]; }; then echo "not a regular file" >&2; exit 3; fi; head -c "$2" "$1"'
EXIT_DONE, EXIT_HOOK, EXIT_USAGE, EXIT_CLOUD, EXIT_TIMEOUT, EXIT_CAP, EXIT_REFUSED = 0, 1, 2, 3, 4, 5, 6
EXIT_EXISTS = 7  # create found a worker of that name already there: this call made nothing and touched nothing

CREDENTIAL_PATTERNS = [re.compile(p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"sk-[A-Za-z0-9]{20,}", r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY", r"Bearer [A-Za-z0-9._-]{20,}", r"(?i)(client_secret|api[_-]?key)\"\s*:\s*\"[^\"]+")]
SECRET_ENV_NAME = re.compile(r"(?i)(TOKEN|SECRET|PASSWORD|API_?KEY|CREDENTIAL|^PAT$|_PAT$)")
ALLOWED_ENV_NAMES = {"CW_TOKEN_ENDPOINT"}  # a URL, not a token


class CloudError(Exception):
    audited = False  # True when the failure already wrote its own audit line

    def __init__(self, msg, code=EXIT_CLOUD):
        super().__init__(msg)
        self.code = code


class _FreshNodeRace(Exception):
    """The pod's sandbox failed on the handler-not-known message: the node's containerd was not yet configured."""


class Interrupted(BaseException):
    """SIGINT or SIGTERM during create: not an Exception, so no handler in between swallows it."""


EXIT_INTERRUPTED = 130


def _raise_interrupted(signum, frame):
    raise Interrupted("signal %d" % signum)


TOKEN_SHAPES = [re.compile(p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{6,}", r"github_pat_[A-Za-z0-9_]{6,}", r"eyJ[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]*)*",
    r"[A-Za-z0-9+/]{40,}={0,2}", r"[A-Za-z0-9_-]{40,}", r"[0-9a-fA-F]{40,}")]  # standard and URL-safe base64, hex
SCRUB_INPUT_MAX = 200000
SCRUB_OUTPUT_MAX = 2 * SCRUB_INPUT_MAX   # a limit above the input maximum: redaction lengthens text (8 characters become 10), and a cut there would be silent
MIN_FRAGMENT = 8


def scrub(text, secrets_known=(), limit=300):
    """THE one place kubectl output (and anything built from it) is made safe to show, log or audit: the exact values
    of this run's secrets and any 8+ character piece of one, token-shaped strings and long base64/hex runs are removed,
    then the text is cut to `limit`. Control characters become '?'."""
    text = str(text)[:SCRUB_INPUT_MAX]
    for sec in sorted({v for v in secrets_known if v and len(v.strip()) >= MIN_FRAGMENT}, key=len, reverse=True):
        for v in {sec, sec.strip()}:
            text = text.replace(v, "[redacted]")
        out, i = [], 0
        while i < len(text):  # any run of 8+ characters that is a piece of the secret
            head = text[i:i + MIN_FRAGMENT]
            if len(head) == MIN_FRAGMENT and head in sec:
                j = i + MIN_FRAGMENT
                while j < len(text) and text[i:j + 1] in sec:
                    j += 1
                out.append("[redacted]")
                i = j
            else:
                out.append(text[i])
                i += 1
        text = "".join(out)
    for pat in TOKEN_SHAPES:
        text = pat.sub("[redacted]", text)
    return re.sub(r"[^\x20-\x7e]", "?", text)[:limit]


def _json_strings(node, out):
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():  # key names are not secrets
            _json_strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _json_strings(v, out)


def secret_forms(value):
    """Every form in which a secret value can be printed: itself, base64 (standard and URL-safe, padded or not, at each of
    the three alignments of a stream: the pieces fixed by the value alone), hex, percent-encoded and JSON-escaped. A model
    entry that is a JSON object also yields the forms of each string value inside it. Scrubbing treats each form, and any
    8+ character piece of it, as it treats the plain value."""
    texts = {value, value.strip()}
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        found = []
        _json_strings(parsed, found)
        texts.update(s for s in found if s.strip())
    forms = set()
    for t in texts:
        if not t:
            continue
        forms.add(t)
        raw = t.encode("utf-8", "replace")
        for k in (0, 1, 2):
            enc = base64.b64encode(b"\0" * k + raw).decode()
            core = enc.rstrip("=")
            lead = (0, 2, 3)[k]  # leading characters that also hold filler bits
            tail = 0 if (k + len(raw)) % 3 == 0 else 1  # a last partial character also holds bits of what follows
            stable = core[lead:len(core) - tail]
            for s in (stable, stable.replace("+", "-").replace("/", "_")):
                if s:
                    forms.add(s)
            if k == 0:
                forms.update({enc, core, enc.replace("+", "-").replace("/", "_"), core.replace("+", "-").replace("/", "_")})
        hx = raw.hex()
        forms.update({hx, hx.upper(), urllib.parse.quote(t, safe=""), urllib.parse.quote_plus(t), urllib.parse.quote(t),
                      json.dumps(t)[1:-1], json.dumps(t, ensure_ascii=False)[1:-1]})
    return {f for f in forms if f}


_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def _repo_key(text):
    """OWNER/NAME lower-cased, or None when it is not exactly that shape (no wildcard, space, newline, dot segment, trailing .git or extra slash). It compares spellings: a renamed or transferred repository is not matched."""
    owner, sep, name = (text or "").partition("/")
    if not sep or not _OWNER_RE.fullmatch(owner) or not _NAME_RE.fullmatch(name) or set(name) == {"."} or name.lower().endswith(".git"):
        return None
    return (owner + "/" + name).lower()


def check_repo_allowed(repo, env):
    """The one allow-list check (the launcher's create and the driver both call it). Raises CloudError(EXIT_USAGE) before anything is
    made. CW_ALLOWED_REPOS: comma-separated OWNER/NAME, exact and case-insensitive; unset, empty or any malformed entry refuses.
    CW_TOOLING_REPO: the tooling's own repository, refused even when listed; unset adds no check, malformed refuses."""
    raw = (env.get("CW_ALLOWED_REPOS") or "").strip()
    if not raw:
        raise CloudError("refused: %s is not on the cloud allow-list (CW_ALLOWED_REPOS is unset or empty)" % scrub(repo, (), 120), EXIT_USAGE)
    allowed = [_repo_key(x.strip()) for x in raw.split(",")]
    if None in allowed:
        raise CloudError("refused: CW_ALLOWED_REPOS has an entry that is not OWNER/NAME; nothing was created", EXIT_USAGE)
    tooling = (env.get("CW_TOOLING_REPO") or "").strip()
    tkey = _repo_key(tooling) if tooling else None
    if tooling and tkey is None:
        raise CloudError("refused: CW_TOOLING_REPO is not OWNER/NAME; nothing was created", EXIT_USAGE)
    key = _repo_key(repo)
    if key is None or key not in allowed or key == tkey:
        raise CloudError("refused: %s is not on the cloud allow-list" % scrub(repo, (), 120), EXIT_USAGE)


# The repository preflight (#665): the program beside this file, found by this file's realpath exactly as repo-approvals.json is found by
# preflight.py. PREFLIGHT_ARGV is a test hook (a module attribute, no setting, no environment variable): None means the real program.
PREFLIGHT_PROGRAM = os.path.join(os.path.dirname(os.path.realpath(__file__)), "preflight.py")
PREFLIGHT_ARGV = None
PREFLIGHT_TEST_ENV = {}   # a test hook too: variables added to the child's environment (a test's loopback API base and token command). Never read from the environment.
PREFLIGHT_SECONDS = 120
# The preflight child gets ONLY these variables from the caller (an allow-list). It never gets CW_GITHUB_API_BASE or CW_PREFLIGHT_TOKEN_CMD (they
# point the guard at a fake GitHub), CW_GIT_TOKEN_CMD (that is the mint's), proxy variables, or GH_/GITHUB_ token variables.
PREFLIGHT_ENV_NAMES = (
    "LANG",                    # (LC_* too) text encoding
    "CW_VAULT_SUBSCRIPTION",   # the vault the GitHub App's id and key are read from (app_token.py)
    "CW_VAULT_NAME",           # the same
    "CW_APP_ID_CMD",           # optional commands that print the App id, installation id and key instead of the vault (app_token.py)
    "CW_APP_INSTALLATION_CMD",
    "CW_APP_KEY_CMD",
)
# Never taken from the caller: PATH (a fixed one is built here), HOME and TMPDIR (a private empty directory), PYTHON* (the child runs `python -I`),
# AZURE_CONFIG_DIR (kept only when an absolute existing directory, else the account's ~/.azure), CW_OPENSSL (kept only when it is an absolute executable file).
PREFLIGHT_PATH = "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"


def _abs_executable(path):
    return bool(path) and os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _preflight_env(env, home):
    """The child's whole environment: the allow-listed names, a fixed PATH, an empty private HOME and TMPDIR, and where `az` keeps its sign-in
    (the caller's AZURE_CONFIG_DIR when it is an absolute existing directory, else the account's own ~/.azure from the password database, never $HOME)."""
    clean = {k: v for k, v in env.items() if k in PREFLIGHT_ENV_NAMES or k.startswith("LC_")}
    clean["PATH"] = PREFLIGHT_PATH
    clean["HOME"] = home
    clean["TMPDIR"] = home
    cfg = env.get("AZURE_CONFIG_DIR") or ""
    if not (os.path.isabs(cfg) and os.path.isdir(cfg)):
        try:
            cfg = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".azure")
        except (KeyError, OSError):
            cfg = ""
    if cfg:
        clean["AZURE_CONFIG_DIR"] = cfg
    ossl = env.get("CW_OPENSSL") or ""
    if _abs_executable(ossl):
        clean["CW_OPENSSL"] = ossl
    clean.update(PREFLIGHT_TEST_ENV)
    return clean


def check_repo_preflight(repo, env):
    """The one preflight call (the operator driver and `launch --cloud` both call it), run AFTER the allow-list and BEFORE any token is minted.
    Returns the preflight's one result line. Raises CloudError(EXIT_USAGE) (fail closed) unless the program ran, exited 0 and printed something:
    a missing, killed, timed-out or silent program is a refusal, never a pass. The refusal carries the program's own message."""
    argv = list(PREFLIGHT_ARGV) + [repo] if PREFLIGHT_ARGV else [os.path.realpath(sys.executable), "-I", "-S", PREFLIGHT_PROGRAM, repo]
    try:
        with tempfile.TemporaryDirectory(prefix="cw-preflight-") as home:   # 0700, empty, removed when the child ends (also on failure)
            p = subprocess.run(argv, capture_output=True, text=True, timeout=PREFLIGHT_SECONDS, stdin=subprocess.DEVNULL, env=_preflight_env(env, home), cwd=home)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise CloudError("refused: the repository preflight for %s could not be run; nothing was created" % scrub(repo, (), 120), EXIT_USAGE) from None
    lines = [x.strip() for x in p.stdout.splitlines() if x.strip()]
    if p.returncode != 0:
        said = " | ".join(lines[-4:]) or "no message (exit %d)" % p.returncode
        raise CloudError("refused: the repository preflight for %s did not pass: %s; nothing was created" % (scrub(repo, (), 120), scrub(said, (), 400)), EXIT_USAGE)
    if not lines:
        raise CloudError("refused: the repository preflight for %s printed nothing; nothing was created" % scrub(repo, (), 120), EXIT_USAGE)
    return scrub(lines[-1], (), 300)


def token_mode(env):
    """True when either service-account setting is present: the token-file login is then the only one (S8, S10)."""
    return bool((env.get("CW_CLOUD_SERVER") or "").strip() or (env.get("CW_CLOUD_TOKEN_FILE") or "").strip())


def spend_cap(env):
    """The cap in USD: the setting, else the default. Zero, negative, non-numeric, nan or inf refuse (fail closed)."""
    raw = (env.get("CW_CLOUD_SPEND_CAP_USD") or "").strip()
    if not raw:
        return DEFAULT_SPEND_CAP_USD
    try:
        cap = float(raw)
    except ValueError:
        cap = float("nan")
    if not math.isfinite(cap) or cap <= 0:
        raise CloudError("CW_CLOUD_SPEND_CAP_USD must be a positive finite number of USD; refusing to create", EXIT_CAP)
    return cap


def as_cost(x):
    """A finite non-negative number, or refuse: a cost that cannot be read is not a cost of zero."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0:
        raise CloudError("cost estimate unreadable; refusing to create (the spend cap cannot be checked)", EXIT_CAP)
    return float(x)


def parse_duration(text):
    m = re.fullmatch(r"(\d+)([smh])", text or "")
    if not m:
        raise CloudError("bad duration %r (use 30s, 10m, 2h)" % text, EXIT_USAGE)
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def pod_name(worker):
    """A valid, collision-free Kubernetes name: the worker name (`_` as `-`) and a hash of the real name."""
    return "%s-%s" % (worker.replace("_", "-"), hashlib.sha1(worker.encode()).hexdigest()[:4])


def scan_credentials(spec):
    """Names of stored-credential patterns found in a rendered pod spec (empty means clean)."""
    hits = []
    for container in spec.get("spec", {}).get("containers", []):
        for env in container.get("env", []):
            if SECRET_ENV_NAME.search(env["name"]) and env["name"] not in ALLOWED_ENV_NAMES:
                hits.append("env:" + env["name"])
    text = json.dumps(spec)
    hits += ["pattern:" + p.pattern for p in CREDENTIAL_PATTERNS if p.search(text)]
    return hits


class Kube:
    """The only door to the cloud: one kubectl call per method. Never puts a secret on a command line."""

    def __init__(self, env):
        self.env = dict(os.environ, **env)
        self.bin = env.get("CW_KUBECTL", "kubectl")
        self.ns = env.get("CW_CLOUD_NAMESPACE", "cw")
        self.context = (env.get("CW_KUBE_CONTEXT") or "").strip()
        self.kubeconfig = (env.get("CW_KUBECONFIG") or "").strip()
        self.dir = None
        self.known = set()  # values of this run's secrets, known to scrub(); never logged
        if token_mode(env):
            self._generate_kubeconfig(env)

    def _generate_kubeconfig(self, env):
        """The service-account mode: a generated kubeconfig whose only credential is a tokenFile path. Nothing is made
        and no call is possible until the token file is readable and non-empty (S10): no other credential is tried."""
        if self.kubeconfig or self.context:
            raise CloudError("CW_CLOUD_TOKEN_FILE and CW_KUBECONFIG/CW_KUBE_CONTEXT are two different logins; set one kind only", EXIT_USAGE)
        token_file, server = (env.get("CW_CLOUD_TOKEN_FILE") or "").strip(), (env.get("CW_CLOUD_SERVER") or "").strip()
        try:
            with open(token_file) as f:
                body = f.read(65536)
            usable = bool(body.strip())
            self.add_secrets(body.strip())
        except OSError:
            usable = False
        if not usable:
            raise CloudError("token unavailable: the token file is missing, empty or unreadable", EXIT_CLOUD)
        if not server:
            raise CloudError("no server: set CW_CLOUD_SERVER", EXIT_USAGE)
        cluster = {"server": server}
        ca = (env.get("CW_CLOUD_CA_FILE") or "").strip()
        if ca:
            cluster["certificate-authority"] = os.path.abspath(ca)  # a path, never inline data
        # JSON is valid YAML. Only the path is written, never a token value; kubectl reads the file itself.
        self.dir = tempfile.mkdtemp(prefix="cw-kube-")  # 0700
        self.kubeconfig = os.path.join(self.dir, "config")
        self.context = "cw"
        cfg = {"apiVersion": "v1", "kind": "Config", "current-context": "cw",
               "clusters": [{"name": "cw", "cluster": cluster}],
               "users": [{"name": "cw", "user": {"tokenFile": os.path.abspath(token_file)}}],
               "contexts": [{"name": "cw", "context": {"cluster": "cw", "user": "cw"}}]}
        fd = os.open(self.kubeconfig, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        self.env["KUBECONFIG"] = os.devnull  # no default kubeconfig can add a login

    def scrub(self, text, limit=300):
        return scrub(text, self.known, limit)

    def add_secrets(self, *values):
        for v in values:
            if v:
                self.known.update(secret_forms(v))

    def delete_command(self, kind, name):
        """The exact command that removes one object by hand: the configured login flags, never a value."""
        if self.dir:
            login = ["--kubeconfig", "<a kubeconfig for the service account whose token file is %s>" % (self.env.get("CW_CLOUD_TOKEN_FILE") or "")]
        else:
            login = (["--kubeconfig", self.kubeconfig] if self.kubeconfig else []) + ["--context", self.context]
        return " ".join([self.bin] + login + ["-n", self.ns, "delete", kind, name])

    def close(self):
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)

    def run(self, args, stdin=None, ok_missing=False, keep_stdout=False, timeout=120):
        if not self.context:  # never fall back to the active context
            raise CloudError("CW_KUBE_CONTEXT is not set; refusing to run kubectl against an unnamed context", EXIT_USAGE)
        cmd = [self.bin] + (["--kubeconfig", self.kubeconfig] if self.kubeconfig else []) + \
              ["--context", self.context, "-n", self.ns] + args
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, env=self.env)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CloudError("kubectl failed: %s" % self.scrub(e))
        if p.returncode != 0:
            if keep_stdout and p.stdout.strip():
                return p.stdout  # the caller reads the failure from the tool's own JSON
            if ok_missing and "NotFound" in p.stderr:
                return None
            last = (self.scrub(p.stderr, limit=SCRUB_OUTPUT_MAX).strip().splitlines() or ["failed"])[-1]
            raise CloudError("kubectl %s: %s" % (args[0], self.scrub(last)))
        return p.stdout  # a caller that renders it passes it through scrub() first

    @staticmethod
    def _exists(e, what):
        """kubectl said AlreadyExists: the object is another caller's, this call made nothing."""
        if "AlreadyExists" in str(e):
            return CloudError("%s already exists" % what, EXIT_EXISTS)
        return e

    def apply(self, obj):
        """Make ONE object, exclusively (`kubectl create`, atomic: of two callers exactly one succeeds; `apply` would
        replace the other's). The name is kept for the callers that patch it."""
        try:
            self.run(["create", "-f", "-"], stdin=json.dumps(obj))
        except CloudError as e:
            raise self._exists(e, "%s %s" % (obj["kind"], obj["metadata"]["name"])) from None

    def get_secret(self, full_name):
        out = self.run(["get", "secret", full_name, "-o", "json"], ok_missing=True)
        return json.loads(out) if out else None

    def get_pod(self, name):
        out = self.run(["get", "pod", name, "-o", "json"], ok_missing=True)
        return json.loads(out) if out else None

    def list_pods(self):
        return json.loads(self.run(["get", "pods", "-l", "app=" + LABEL, "-o", "json"]))["items"]

    def annotate(self, name, **kv):
        self.run(["annotate", "pod", name, "--overwrite"] + ["cw/%s=%s" % (k.replace("_", "-"), v) for k, v in kv.items()])

    def delete_pod(self, name):
        self.run(["delete", "pod", name, "--ignore-not-found", "--wait=false"])

    def delete_pod_and_wait(self, name):
        """Delete the pod and return only when it is gone (a recreate under the same name would find it terminating)."""
        self.run(["delete", "pod", name, "--ignore-not-found", "--wait=true", "--timeout=120s"], timeout=150)

    def pod_events(self, name):
        out = self.run(["get", "events", "--field-selector", "involvedObject.kind=Pod,involvedObject.name=" + name, "-o", "json"])
        return json.loads(out)["items"]

    def delete_secret(self, name):
        self.run(["delete", "secret", name + "-pw", "--ignore-not-found"])

    def delete_run_secret(self, name):
        self.run(["delete", "secret", name + "-run", "--ignore-not-found"])

    def delete(self, name):
        """Delete the pod and both Secrets, each attempted even if another fails; names what REMAINS and how to delete it."""
        left = []
        for kind, obj, fn in (("pod", name, self.delete_pod), ("secret", name + "-pw", self.delete_secret),
                              ("secret", name + "-run", self.delete_run_secret)):
            try:
                fn(name)
            except CloudError:
                left.append((kind, obj))
        if left:
            raise CloudError("delete incomplete; REMAINS: %s; delete each with: %s" % (
                ", ".join("%s/%s" % x for x in left), "; ".join(self.delete_command(k, o) for k, o in left)))

    def get_run_secret(self, name):
        out = self.run(["get", "secret", name + "-run", "-o", "json"], ok_missing=True)
        return json.loads(out) if out else None

    def create_run_secret(self, name, pod, values, run_id=None):
        """One Secret per run. Each value goes to kubectl's stdin (--from-file=KEY=/dev/stdin, or a patch file on stdin
        for the keys after the first), never into an argument."""
        items = sorted(values.items())
        key, val = items[0]
        try:
            self.run(["create", "secret", "generic", name + "-run", "--from-file=%s=/dev/stdin" % key], stdin=val)
        except CloudError as e:
            raise self._exists(e, "Secret " + name + "-run") from None
        try:   # the Secret exists and is ours (the create is exclusive): it must never be left without its label
            for key, val in items[1:]:
                body = json.dumps({"data": {key: base64.b64encode(val.encode()).decode()}})
                self.run(["patch", "secret", name + "-run", "--type=merge", "--patch-file=/dev/stdin"], stdin=body)
            self.run(["label", "secret", name + "-run", "cw/pod=" + pod] + (["cw/run-id=" + run_id] if run_id else []))
        except BaseException:
            try:
                self.delete_run_secret(name)
            except CloudError:
                pass   # the launcher's cleanup finds it unlabelled and says it REMAINS
            raise

    def exec(self, name, *cmd, keep_stdout=False, timeout=120):
        return self.run(["exec", name, "--"] + list(cmd), keep_stdout=keep_stdout, timeout=timeout)


def _pod_repo(pod):
    try:
        for c in pod["spec"]["containers"]:
            for e in c.get("env") or []:
                if e.get("name") == "CW_REPO" and isinstance(e.get("value"), str):
                    return e["value"][:200]
    except (KeyError, TypeError, AttributeError):
        pass
    return ""


def ann(pod, key, default=None):
    return pod["metadata"].get("annotations", {}).get("cw/" + key, default)


RUN_SECRET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RESERVED_RUN_NAMES = {"server-password"}
SERVER_UID = 1000  # the image's non-root user
# The pod-level security context of EVERY worker pod (the builder copies it; a stack pod must EQUAL it, whole).
POD_SECURITY_CONTEXT = {"runAsNonRoot": True, "runAsUser": SERVER_UID, "runAsGroup": SERVER_UID, "fsGroup": SERVER_UID,
                        "seccompProfile": {"type": "RuntimeDefault"}}
MAX_RUN_SECRET_BYTES = 1024 * 1024  # a Kubernetes Secret holds about 1 MiB in all
MAX_RUN_SECRETS = 8


def parse_run_secret_fds(items):
    """['NAME=N', ...] -> {NAME: fd}. Checks names and fd numbers only; reads nothing."""
    fds = {}
    if len(items or []) > MAX_RUN_SECRETS:
        raise CloudError("too many --run-secret-fd (at most %d)" % MAX_RUN_SECRETS, EXIT_USAGE)
    for item in items or []:
        key, sep, num = item.partition("=")
        if not sep or not RUN_SECRET_NAME.match(key) or key in RESERVED_RUN_NAMES or key in fds or not num.isdigit() or int(num) < 3:
            raise CloudError("bad --run-secret-fd (use NAME=N; NAME is a unique lowercase name, N an inherited descriptor >= 3)", EXIT_USAGE)
        fds[key] = int(num)
    return fds


def read_run_secret_values(fds):
    """Read each value once from its inherited descriptor. An unreadable or empty value is refused before anything is made."""
    values = {}
    for key, fd in fds.items():
        try:
            with os.fdopen(os.dup(fd), "r") as f:
                val = f.read(MAX_RUN_SECRET_BYTES + 1)
        except (OSError, ValueError):
            raise CloudError("run secret %s: descriptor %d is not readable" % (key, fd), EXIT_USAGE)
        if len(val) > MAX_RUN_SECRET_BYTES:
            raise CloudError("run secret %s: the value is over the %d byte cap" % (key, MAX_RUN_SECRET_BYTES), EXIT_USAGE)
        if not val.strip():
            raise CloudError("run secret %s: the value is empty" % key, EXIT_USAGE)
        values[key] = val
    return values


def render_pod(a, worker, name, now, hard, env, run_names=()):
    image = a.image or env.get("CW_CLOUD_IMAGE")
    if not image:
        raise CloudError("no image: pass --image or set CW_CLOUD_IMAGE", EXIT_USAGE)
    stack = bool(getattr(a, "stack", False))
    endpoint = env.get("CW_TOKEN_ENDPOINT")
    if not endpoint and not run_names:  # git access then comes from a per-run secret instead
        raise CloudError("no token endpoint: set CW_TOKEN_ENDPOINT", EXIT_USAGE)
    shape = SHAPES[a.shape]
    e = {"CW_WORKER": worker, "CW_REPO": a.repo, "CW_BRANCH": a.branch,
         "CW_IDLE_SECONDS": str(parse_duration(a.idle)), "CW_HARD_DEADLINE": iso(hard),
         "CW_AUDIT_RUNTIME_ID": "pod/" + name}
    if endpoint:
        e["CW_TOKEN_ENDPOINT"] = endpoint
    pw_volume = {"name": "pw", "secret": {"secretName": name + "-pw", "defaultMode": 0o600,
                                          "items": [{"key": "password", "path": "server-password"}]}}
    if run_names:  # one read-only mount at /run/cw holding the password file and each run secret (0400)
        pw_volume = {"name": "pw", "projected": {"defaultMode": 0o600, "sources": [
            {"secret": {"name": name + "-pw", "items": [{"key": "password", "path": "server-password", "mode": 0o600}]}},
            {"secret": {"name": name + "-run", "items": [{"key": k, "path": k, "mode": 0o400} for k in run_names]}}]}}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {
        "name": name, "labels": {"app": LABEL, "cw/worker": worker, "azure.workload.identity/use": "true"},
        "annotations": {"cw/worker": worker, "cw/started": iso(now), "cw/hard-deadline": iso(hard),
                        "cw/idle-seconds": e["CW_IDLE_SECONDS"], "cw/shape": a.shape,
                        "cw/max-hard": iso(now + dt.timedelta(seconds=parse_duration(a.max_hard))),
                        **({"cw/run-id": a.run_id} if getattr(a, "run_id", None) else {})}},
        "spec": {"restartPolicy": "Never", "serviceAccountName": env.get("CW_CLOUD_SERVICE_ACCOUNT", "cw-worker"),
                  "automountServiceAccountToken": False,
                  "securityContext": copy.deepcopy(POD_SECURITY_CONTEXT),
                 "containers": [{"name": "worker", "image": image,
                                 "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}, "env": [{"name": k, "value": v} for k, v in e.items()],
                                 "resources": {"limits": {"cpu": shape["cpu"], "memory": shape["memory"]}},
                                 "readinessProbe": {"tcpSocket": {"port": 4096}},
                                 "volumeMounts": [{"name": "pw", "mountPath": "/run/cw", "readOnly": True},
                                                  {"name": "tokens", "mountPath": "/run/cw-tmp"},
                                                  {"name": "scratch", "mountPath": "/work"}]}],
                 "volumes": [pw_volume,
                             {"name": "tokens", "emptyDir": {"medium": "Memory"}},
                             {"name": "scratch", "emptyDir": {}}]}}
    pull = getattr(a, "pull_secret", None)
    if pull:
        pod["spec"]["imagePullSecrets"] = [{"name": pull}]
    if stack:
        pod["metadata"]["annotations"]["cw/stack"] = "true"
        pod["spec"]["runtimeClassName"] = STACK_RUNTIME_CLASS
        pod["spec"]["hostUsers"] = False
        pod["spec"]["nodeSelector"] = dict(STACK_NODE_SELECTOR)
        pod["spec"]["containers"][0]["securityContext"] = copy.deepcopy(STACK_SECURITY_CONTEXT)
    bad = forbidden_pod_fields(pod)
    if bad:
        raise CloudError("refusing a pod spec with a forbidden field: %s" % ", ".join(bad), EXIT_USAGE)
    return pod


class Launcher:
    def __init__(self, kube, env, clock=None, sleep=time.sleep):
        self.kube, self.env, self.sleep = kube, env, sleep
        self.run_id, self.foreign = None, []
        self.recorder = run_record.Recorder(kube, env)
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.audit_path = env.get("CW_CLOUD_AUDIT") or os.path.join(
            env.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "cw-cloud", "audit.jsonl")

    # -- audit: one line per action, no secret, written before the action returns --
    def audit(self, worker, runtime_id, action, cost, ok, reason=""):
        os.makedirs(os.path.dirname(self.audit_path), exist_ok=True)
        line = {"ts": iso(self.clock()), "worker": worker, "runtime_id": runtime_id, "action": action,
                "cost_estimate_usd": cost, "ok": ok, "actor": pwd.getpwuid(os.getuid()).pw_name, "reason": self.kube.scrub(reason, limit=1000)}
        try:
            fd = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as f:  # one short O_APPEND write: atomic enough for one line, the chosen guarantee
                f.write(json.dumps(line, sort_keys=True) + "\n")
        except OSError as e:  # fail-open, decided: the action already ran; a loud warning beats undoing it
            print("cloud-worker: WARNING: the action ran but its audit line could not be written: %s" % e, file=sys.stderr)

    def cost(self, pod):
        hours = (self.clock() - parse_iso(ann(pod, "started"))).total_seconds() / 3600
        return round(max(hours, 0) * SHAPES.get(ann(pod, "shape", "S"), SHAPES["S"])["usd_per_hour"], 4)

    def _audited(self, worker, action, fn, reason="", runtime_id=""):
        info = {"rid": runtime_id, "cost": 0.0}

        def why():   # a stop that wrote a run record names its directory once, here
            return "%s; run record %s" % (reason, info["record"]) if info.get("record") else reason
        try:
            result = fn(info)
        except CloudError as e:
            if not e.audited:
                # the failure is never dropped: a caller's own reason (a stop reason, the run-secret names) comes first
                self.audit(worker, info["rid"], action, info["cost"], False, "%s; %s" % (why(), e) if why() else str(e))
            raise
        except (ValueError, TypeError, KeyError) as e:  # a bad annotation or reply still writes its ok:false line
            self.audit(worker, info["rid"], action, info["cost"], False, why() or str(e)[:300])
            raise CloudError("unexpected cloud data: %s" % self.kube.scrub(e, 200))
        self.audit(worker, info["rid"], action, info["cost"], True, why())
        return result

    def _need(self, worker):
        if not NAME_RE.match(worker or ""):
            raise CloudError("bad worker name %r" % worker, EXIT_USAGE)
        pod = self.kube.get_pod(pod_name(worker))
        return pod

    def estimate(self, shape, seconds):
        return SHAPES[shape]["usd_per_hour"] * seconds / 3600

    def check_cap(self, shape, hard_seconds):
        cap = spend_cap(self.env)
        try:
            spent = sum(as_cost(self.cost(p)) for p in self.kube.list_pods())
            launch = as_cost(self.estimate(shape, hard_seconds))
        except CloudError as e:
            if e.code == EXIT_CAP:
                raise
            raise CloudError("could not read the running cost; refusing to create: %s" % e, EXIT_CAP)
        except Exception as e:
            raise CloudError("could not read the cost; refusing to create: %s" % str(e)[:200], EXIT_CAP)
        if spent >= cap or spent + launch > cap:
            raise CloudError("spend cap reached (cap USD %s, spent %.4f, this launch %.4f)" % (cap, spent, launch), EXIT_CAP)

    # -- create --
    def create(self, a):
        worker, name = a.worker, pod_name(a.worker)
        self.run_id, self.foreign = getattr(a, "run_id", None), []   # what this call proved is another's is never deleted
        made = {"secret": False, "run": False, "pod": False}  # set before the call that may create it: an interrupt may land after

        def go(info):
            info["rid"] = "pod/" + name
            check_repo_allowed(a.repo, self.env)  # before anything is read or made
            if not NAME_RE.match(worker):
                raise CloudError("bad worker name %r" % worker, EXIT_USAGE)
            if a.shape not in SHAPES:
                raise CloudError("shape must be S or M", EXIT_USAGE)
            now = self.clock()
            hard = now + dt.timedelta(seconds=parse_duration(a.hard))
            max_hard = now + dt.timedelta(seconds=parse_duration(a.max_hard))
            if hard > max_hard:
                raise CloudError("--hard is past --max-hard", EXIT_REFUSED)
            run_values = read_run_secret_values(parse_run_secret_fds(a.run_secret_fd))  # before anything is made
            self.kube.add_secrets(*run_values.values())  # known to scrub before the first kubectl call
            self.check_cap(a.shape, int((hard - now).total_seconds()))
            spec = render_pod(a, worker, name, now, hard, self.env, sorted(run_values))
            hits = scan_credentials(spec)
            if hits:
                raise CloudError("refusing a pod spec with a stored credential: %s" % ", ".join(hits), EXIT_USAGE)
            if self.kube.get_pod(name):
                raise CloudError("worker %s already exists" % worker, EXIT_EXISTS)
            password = secrets.token_urlsafe(32)
            self.kube.add_secrets(password)
            # The check above is not atomic with the creates below; each create is (AlreadyExists fails exactly one of two callers).
            try:
                self._make(made, "secret", self.kube.apply, {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name + "-pw", "labels": dict({"app": LABEL}, **({"cw/run-id": a.run_id} if getattr(a, "run_id", None) else {}))},
                                 "type": "Opaque", "stringData": {"password": password}})
                if run_values:
                    self._make(made, "run", self.kube.create_run_secret, name, name, run_values, *([a.run_id] if getattr(a, "run_id", None) else []))
                self._make(made, "pod", self.kube.apply, spec)
                if getattr(a, "stack", False):
                    self._start_stack_pod(worker, name, spec, made, a.wait)
                else:
                    self._wait_ready(name, a.wait)
                if a.password_fd is not None:
                    with os.fdopen(os.dup(a.password_fd), "w") as f:
                        f.write(password)
            except (CloudError, OSError) as e:
                left = self._cleanup(name, made)  # nothing is left running (and costing) or holding a password
                err = e if isinstance(e, CloudError) else CloudError("could not hand over the password: %s" % e)
                if left:
                    err = CloudError("%s; CLEANUP FAILED, REMAINS: %s; delete each with: %s" % (
                        err, ", ".join(left), "; ".join(self._delete_cmd(l) for l in left)), err.code)
                note = self._run_secret_note(name, made, left)
                if note:
                    err = CloudError("%s; %s" % (err, note), err.code)
                raise err
            except BaseException as e:  # interrupt or an unexpected exception: clean up, audit, say what was cleaned
                self._abort(worker, name, made, e)
            domain = self.env.get("CW_CLOUD_DOMAIN", "example.internal")
            return {"worker": worker, "url": "https://worker-%s.%s:4096" % (name, domain),
                    "auth_ref": "file:/run/cw/%s.password" % worker, "runtime_id": "pod/" + name,
                    "idle_deadline": iso(now + dt.timedelta(seconds=parse_duration(a.idle))), "hard_deadline": iso(hard),
                    "password_fd": a.password_fd}
        old = self._catch_signals()
        try:
            return self._audited(worker, "create", go, ("run-secrets=" + ",".join(sorted(parse_run_secret_fds(a.run_secret_fd)))) if a.run_secret_fd else "")
        finally:
            self._restore_signals(old)

    @staticmethod
    def _catch_signals():
        try:
            return {s: signal.signal(s, _raise_interrupted) for s in (signal.SIGINT, signal.SIGTERM)}
        except ValueError:  # not the main thread (tests): no handler, the try/except still runs
            return {}

    @staticmethod
    def _restore_signals(old):
        for s, h in old.items():
            signal.signal(s, h)

    def _make(self, made, kind, fn, *args):
        """The flag is set BEFORE the call (an interrupt may land after the object exists); AlreadyExists means it is not
        ours, so the flag is taken back and nothing of it is ever deleted by this call."""
        made[kind] = True
        try:
            fn(*args)
        except CloudError as e:
            if e.code == EXIT_EXISTS:
                made[kind] = False
            raise

    def _mine(self, kind, name):
        """True: carries this call's run id; False: another's (leave whole); None: not there; raises CloudError: unreadable."""
        if not self.run_id:
            return True   # a caller that sent no --run-id gets the plain behaviour
        if kind == "pod":
            obj, where = self.kube.get_pod(name), "annotations"
        else:
            obj, where = self.kube.get_secret(name + ("-pw" if kind == "secret" else "-run")), "labels"
        if not obj:
            return None
        return ((obj.get("metadata") or {}).get(where) or {}).get("cw/run-id") == self.run_id

    def _cleanup(self, name, made):
        """Delete what create made, each on its own so one failure does not skip the other. Returns what REMAINS."""
        old = {}
        try:
            old = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM)}  # a second Ctrl-C must not abandon it
        except ValueError:
            pass
        left = []
        try:
            for kind, fn, label in (("pod", self.kube.delete_pod, "pod/" + name), ("secret", self.kube.delete_secret, "secret/%s-pw" % name),
                                  ("run", self.kube.delete_run_secret, "secret/%s-run" % name)):
                if made[kind]:
                    try:
                        mine = self._mine(kind, name)
                        if mine is False:
                            self.foreign.append(label)
                            print("cloud-worker: %s is not this call's (its run id does not match); left whole" % label, file=sys.stderr)
                        elif mine:
                            fn(name)
                    except CloudError:
                        left.append(label)
        finally:
            self._restore_signals(old)
        if left:
            print("cloud-worker: CLEANUP FAILED, REMAINS in the cluster (delete by hand): %s" % ", ".join(left), file=sys.stderr)
        return left

    def _delete_cmd(self, label):
        kind, obj = label.split("/", 1)
        return self.kube.delete_command(kind, obj)

    def _run_secret_note(self, name, made, left):
        """What happened to the per-run Secret of a failed create, said once, with the delete command when it remains."""
        if not made["run"]:
            return ""
        label = "secret/%s-run" % name
        if label in self.foreign:
            return "create failed; per-run secret %s-run is not this call's and was left whole" % name
        if label in left:
            return "create failed; per-run secret %s-run REMAINS: delete it with: %s" % (name, self.kube.delete_command("secret", name + "-run"))
        return "create failed; per-run secret %s-run cleaned" % name

    def _abort(self, worker, name, made, exc):
        interrupted = isinstance(exc, (Interrupted, KeyboardInterrupt))
        left = self._cleanup(name, made)
        cleaned = [l for k, l in (("pod", "pod/" + name), ("secret", "secret/%s-pw" % name), ("run", "secret/%s-run" % name)) if made[k] and l not in left and l not in self.foreign]
        reason = "%s; cleaned: %s" % ("interrupted" if interrupted else "failed: %s" % str(exc)[:200], ", ".join(cleaned) or "nothing")
        if left:
            reason += "; REMAINS: %s; delete each with: %s" % (", ".join(left), "; ".join(self._delete_cmd(l) for l in left))
        note = self._run_secret_note(name, made, left)
        if note:
            reason += "; " + note
        self.audit(worker, "pod/" + name, "create.interrupted" if interrupted else "create.failed", 0.0, False, reason)
        err = CloudError("create %s" % reason, EXIT_INTERRUPTED if interrupted else EXIT_CLOUD)
        err.audited = True
        raise err from None

    def _start_stack_pod(self, worker, name, spec, made, seconds):
        """Wait for a stack pod; on the fresh-node race (the runtime handler is not yet known to the node's containerd) delete
        THE POD ONLY and create it again with the same run id and nonce, at most STACK_ATTEMPTS times inside
        STACK_RACE_WINDOW_SECONDS. The password Secret and the per-run Secret are made once, before the first attempt, and are
        never touched here: the pod refers to them by name, so a second attempt reads the same objects. Any other failure is not retried."""
        deadline = self.clock() + dt.timedelta(seconds=STACK_RACE_WINDOW_SECONDS)
        attempt = 1
        while True:
            try:
                self._wait_ready(name, seconds, lambda pod: self._raise_on_race(name, pod))
            except _FreshNodeRace as race:
                last = attempt >= STACK_ATTEMPTS or self.clock() >= deadline
                self.audit(worker, "pod/" + name, "create-attempt", 0.0, False,
                           "attempt %d of %d: %s%s" % (attempt, STACK_ATTEMPTS, race, "; giving up" if last else "; recreating the pod"))
                if last:
                    raise CloudError("the worker pod could not start: the node's runtime handler %s was not ready (the fresh-node race: %s); "
                                     "gave up after %d attempt(s)" % (STACK_RUNTIME_CLASS, race, attempt)) from None
                if self._mine("pod", name) is False:
                    self.foreign.append("pod/" + name)
                    raise CloudError("the pod %s is not this call's (its run id does not match); left whole" % name) from None
                self.kube.delete_pod_and_wait(name)
                attempt += 1
                self._make(made, "pod", self.kube.apply, spec)
                continue
            except CloudError as e:
                self.audit(worker, "pod/" + name, "create-attempt", 0.0, False, "attempt %d of %d: %s" % (attempt, STACK_ATTEMPTS, e))
                raise
            self.audit(worker, "pod/" + name, "create-attempt", 0.0, True, "attempt %d of %d: ready" % (attempt, STACK_ATTEMPTS))
            return

    def _raise_on_race(self, name, pod):
        uid = (pod or {}).get("metadata", {}).get("uid")
        for ev in self.kube.pod_events(name):
            if uid and (ev.get("involvedObject") or {}).get("uid") not in (None, uid):
                continue   # an event of an earlier pod of the same name
            if ev.get("reason") == "FailedCreatePodSandBox" and STACK_RACE_RE.search(ev.get("message") or ""):
                raise _FreshNodeRace(STACK_RACE_RE.search(ev["message"]).group(0))

    def _wait_ready(self, name, seconds, on_pending=None):
        end = self.clock() + dt.timedelta(seconds=seconds)
        while True:
            pod = self.kube.get_pod(name)
            status = (pod or {}).get("status", {})
            if status.get("phase") == "Failed":
                raise CloudError("worker pod failed to start")
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])):
                return
            if on_pending:
                on_pending(pod)
            if self.clock() >= end:
                raise CloudError("timed out waiting for the worker", EXIT_TIMEOUT)
            self.sleep(2)

    # -- end hook, stop, delete --
    def run_hook(self, name):
        out = self.kube.exec(name, "cw-end-hook", keep_stdout=True)
        try:
            res = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"pushed": False, "wip_branch": None, "error": "end hook gave no result"}
        if not isinstance(res, dict):
            return {"pushed": False, "wip_branch": None, "error": "end hook gave no result"}
        if res.get("error"):  # text from inside the pod and the remote: scrubbed and short before it is shown or audited
            res["error"] = self.kube.scrub(res["error"])
        return res

    def stop(self, a, reason=None):
        worker, name = a.worker, pod_name(a.worker)
        reason = reason if reason is not None else (a.reason or "")
        nonce = getattr(a, "run_id", None)   # the run's own nonce (what create recorded): acts only on what carries it; none: by name, as before
        audit_reason = ("%s; " % reason if reason else "") + "run-id=" + nonce if nonce else reason   # the record keeps the plain reason

        def go(info):
            pod = self._need(worker)
            if nonce and pod:
                info["rid"] = "pod/" + name
                if ann(pod, "run-id") != nonce:   # another run's (or an unlabelled) worker of this name: nothing is changed
                    raise CloudError("worker %s does not match run id %s; nothing was stopped or deleted" % (worker, nonce), EXIT_REFUSED)
            if not pod:
                if nonce:   # no pod: this run's own Secrets (carrying the nonce) may remain from a cut-off create; another's are left whole
                    self._remove(name, nonce)
                return {"worker": worker, "runtime_id": "", "pushed": False, "wip_branch": None}
            info["rid"], info["cost"] = "pod/" + name, self.cost(pod)
            if not getattr(a, "no_record", False):   # BEFORE the hook and the delete; never raises, never holds the stop back
                where = self.recorder.record(worker, name, reason or "stop", repo=_pod_repo(pod), started=ann(pod, "started") or "",
                                             root=getattr(a, "record_dir", None))
                if where:
                    info["record"] = where
                    print("cloud-worker: run record: %s" % where, file=sys.stderr)
            phase = (pod.get("status") or {}).get("phase")
            if phase in ("Failed", "Succeeded") and ann(pod, "kept") != "true":  # a pod kept for unpushed work is never deleted here
                # No container is running, so no hook can run and a kept pod would only linger: delete it and its
                # Secrets, and say so.
                self._remove(name, nonce)
                print("cloud-worker: pod was %s; end hook not run; deleted" % phase, file=sys.stderr)
                return {"worker": worker, "runtime_id": "pod/" + name, "pushed": False, "wip_branch": None,
                        "skipped": [], "note": "pod was %s; end hook not run" % phase, "record": info.get("record", "")}
            try:
                res = self.run_hook(name)
            except CloudError as e:
                res = {"pushed": False, "wip_branch": None, "error": str(e)}
            if res.get("error"):
                self.kube.annotate(name, kept="true")  # the pod is kept: the commit is not safe yet
                raise CloudError("end hook could not push, pod kept: %s" % res["error"], EXIT_HOOK)
            try:
                self.kube.annotate(name, hook_ran="true")
            finally:
                self._remove(name, nonce)  # the hook succeeded: whatever happens next, the pod and its Secrets go
            skipped, note = [], ""
            try:
                raw = res.get("skipped")
                skipped = [self.kube.scrub(x, 200) for x in raw][:50] if isinstance(raw, list) else []
                note = self.kube.scrub(res.get("note") or "")
            except Exception:  # output handling never undoes the stop
                pass
            if note:
                print("cloud-worker: end hook: %s; nothing to push" % note, file=sys.stderr)
            if skipped:  # these files were not pushed and went with the pod: say so, loudly
                print("cloud-worker: WARNING: not saved (secret-looking names): %s" % ", ".join(skipped), file=sys.stderr)
            return {"worker": worker, "runtime_id": "pod/" + name, "pushed": bool(res.get("pushed")),
                    "wip_branch": res.get("wip_branch"), "skipped": skipped, "record": info.get("record", "")}
        return self._audited(worker, "stop", go, audit_reason)

    def _remove(self, name, nonce):
        """Delete the pod and both Secrets (as `delete` does); with a nonce, only those that carry it (the pod, when there, matched already)."""
        if not nonce:
            return self.kube.delete(name)
        self.run_id = nonce
        left = []
        for kind, label, fn in (("pod", "pod/" + name, self.kube.delete_pod), ("secret", "secret/%s-pw" % name, self.kube.delete_secret),
                                ("run", "secret/%s-run" % name, self.kube.delete_run_secret)):
            try:
                mine = self._mine(kind, name)
                if mine:
                    fn(name)
                elif mine is False:
                    print("cloud-worker: %s is not this run's (its run id does not match); left whole" % label, file=sys.stderr)
            except CloudError:
                left.append(label)
        if left:
            raise CloudError("delete incomplete; REMAINS: %s; delete each with: %s" % (", ".join(left), "; ".join(self._delete_cmd(l) for l in left)))

    def prune_records(self, a):
        """Remove run records older than --days (default CW_RECORD_KEEP_DAYS, else 30): only this tool's per-run directories."""
        days = a.days if a.days is not None else int(self.env.get("CW_RECORD_KEEP_DAYS") or run_record.KEEP_DAYS)
        if days < 1:
            raise CloudError("--days must be at least 1", EXIT_USAGE)
        root = self.recorder.root(a.record_dir)
        try:
            removed = run_record.prune(root, days)
        except OSError as e:
            raise CloudError("prune-records: %s" % self.kube.scrub(e, 200), EXIT_USAGE)
        return {"root": root, "days": days, "removed": removed}

    def report(self, a):
        """The worker's one-line report: the content of REPORT_PATH read with `kubectl exec head`, so nothing of the
        switchboard has to be inside the pod. Scrubbed, one line, at most REPORT_MAX characters."""
        worker, name = a.worker, pod_name(a.worker)

        def go(info):
            pod = self._need(worker)
            if not pod:
                raise CloudError("unknown worker %s" % worker, EXIT_USAGE)
            info["rid"], info["cost"] = "pod/" + name, self.cost(pod)
            try:
                raw = self.kube.exec(name, "sh", "-c", REPORT_READ, "sh", REPORT_PATH, "%d" % (REPORT_MAX * 4))   # not keep_stdout: a failed read must be an error, not a "report"
            except CloudError as e:
                if "not a regular file" in str(e):   # a link or a device the worker put there could name a secret mount
                    raise CloudError("report refused: %s is not a regular file in %s" % (REPORT_PATH, worker))
                if "No such file" in str(e) or "cannot open" in str(e):
                    raise CloudError("no report yet: %s is missing in %s" % (REPORT_PATH, worker))
                raise
            line = self.kube.scrub(" ".join(raw.split()), limit=REPORT_MAX)
            if not line:
                raise CloudError("no report yet: %s is empty in %s" % (REPORT_PATH, worker))
            return {"worker": worker, "runtime_id": "pod/" + name, "report": line}
        return self._audited(worker, "report", go)

    def delete(self, a):
        worker, name = a.worker, pod_name(a.worker)

        def go(info):
            pod = self._need(worker)
            if not pod:
                return {"worker": worker, "runtime_id": "", "deleted": True}
            info["rid"], info["cost"] = "pod/" + name, self.cost(pod)
            if ann(pod, "hook-ran") != "true" and not a.force:
                raise CloudError("end hook has not run; use --force to delete anyway", EXIT_REFUSED)
            self.kube.delete(name)
            return {"worker": worker, "runtime_id": "pod/" + name, "deleted": True}
        return self._audited(worker, "delete", go, a.reason or ("force" if a.force else ""))

    # -- extend --
    def extend(self, a):
        worker, name = a.worker, pod_name(a.worker)

        def go(info):
            pod = self._need(worker)
            if not pod:
                raise CloudError("unknown worker %s" % worker, EXIT_USAGE)
            info["rid"], info["cost"] = "pod/" + name, self.cost(pod)
            hard = parse_iso(ann(pod, "hard-deadline"))
            if a.hard:
                hard = hard + dt.timedelta(seconds=parse_duration(a.hard[1:])) if a.hard.startswith("+") else self._iso_arg(a.hard)
            if hard > parse_iso(ann(pod, "max-hard")):
                raise CloudError("extension is past --max-hard", EXIT_REFUSED)
            idle = parse_duration(a.idle) if a.idle else int(ann(pod, "idle-seconds"))
            self.kube.annotate(name, hard_deadline=iso(hard), idle_seconds=idle)
            return {"worker": worker, "runtime_id": "pod/" + name, "hard_deadline": iso(hard), "idle_seconds": idle}
        return self._audited(worker, "extend", go, a.reason or "")

    @staticmethod
    def _iso_arg(text):
        try:
            return parse_iso(text)
        except ValueError:
            raise CloudError("bad time %r (use YYYY-MM-DDTHH:MM:SSZ or +1h)" % text, EXIT_USAGE)

    # -- status and reaper --
    def activity(self, name):
        """(busy, last_activity or None) from the worker's own session state; None when unreadable."""
        try:
            d = json.loads(self.kube.exec(name, "cw-worker-activity"))
            return bool(d.get("busy")), (parse_iso(d["last_activity"]) if d.get("last_activity") else None)
        except (CloudError, ValueError, KeyError):
            return None

    def record(self, pod):
        name = pod["metadata"]["name"]
        act = self.activity(name)
        idle_for = int(ann(pod, "idle-seconds", "600"))
        since = act[1] if act and not act[0] else None
        return {"worker": ann(pod, "worker"), "runtime_id": "pod/" + name, "phase": pod.get("status", {}).get("phase"),
                "idle_since": iso(since) if since else None,
                "deadlines": {"idle": iso(since + dt.timedelta(seconds=idle_for)) if since else None, "hard": ann(pod, "hard-deadline")},
                "cost_estimate_usd": self.cost(pod)}

    def status(self, a):
        if a.worker and not NAME_RE.match(a.worker):
            raise CloudError("bad worker name %r" % a.worker, EXIT_USAGE)
        pods = [self.kube.get_pod(pod_name(a.worker))] if a.worker else self.kube.list_pods()
        if a.worker and not pods[0]:
            raise CloudError("unknown worker %s" % a.worker, EXIT_USAGE)
        return [self.record(p) for p in pods]

    def hard_deadline(self, pod):
        """The pod's copy, unless the switchboard record (read only, --record-dir) names a later or earlier one."""
        hard = parse_iso(ann(pod, "hard-deadline"))
        rd = self.env.get("CW_CLOUD_RECORD_DIR")
        if rd and NAME_RE.match(ann(pod, "worker", "")):
            try:
                with open(os.path.join(rd, ann(pod, "worker") + ".json")) as f:
                    hard = parse_iso(json.load(f)["hard_deadline"])
            except (OSError, ValueError, KeyError):
                print("cloud-worker: WARNING: no usable record for %s; using the pod's own deadline" % ann(pod, "worker"), file=sys.stderr)
        return min(hard, parse_iso(ann(pod, "max-hard", iso(hard))))  # a record never lengthens a life past the cap

    def reap(self):
        """One pass: stop every pod past its hard deadline or idle too long. Returns [(worker, reason, exit)]."""
        out, now = [], self.clock()
        for pod in self.kube.list_pods():
            worker = ann(pod, "worker")
            reason = None
            try:
                hard = self.hard_deadline(pod)
            except (ValueError, TypeError):
                out.append((worker, "unreadable deadline", 1))  # one bad pod never stops the pass
                continue
            if now >= hard:
                reason = "hard limit"
            else:
                act = self.activity(pod["metadata"]["name"])
                if act is None:
                    # Fail mode, decided: an unreadable activity is not a reason to kill work, so the pod runs until
                    # its hard limit; it is reported so it is not silent.
                    out.append((worker, "activity unreadable", 1))
                    continue
                if act and not act[0] and act[1] is not None and (now - act[1]).total_seconds() >= int(ann(pod, "idle-seconds", "600")):
                    reason = "idle"
            if reason:
                try:
                    self.stop(argparse.Namespace(worker=worker, reason=reason), reason)
                    out.append((worker, reason, 0))
                except CloudError as e:
                    out.append((worker, reason, e.code))
        return out


def build_parser():
    p = argparse.ArgumentParser(prog="cloud-worker")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name, worker=True):
        s = sub.add_parser(name)
        if worker:
            s.add_argument("worker", nargs=None if name != "status" else "?")
        s.add_argument("--json", action="store_true")
        return s
    c = cmd("create")
    c.add_argument("--repo", required=True)
    c.add_argument("--branch", required=True)
    c.add_argument("--idle", default="10m")
    c.add_argument("--hard", default="2h")
    c.add_argument("--max-hard", default=DEFAULT_MAX_HARD)
    c.add_argument("--shape", default="S")
    c.add_argument("--image")
    c.add_argument("--stack", action="store_true",
                   help="a pod for a worker that runs a nested Docker stack: the Sysbox runtime class, its own user namespace, the pool's node label; off by default (the default pod is unchanged)")
    c.add_argument("--wait", type=int, default=300)
    c.add_argument("--pull-secret", help="name of an existing image-pull Secret (pull-only) for imagePullSecrets")
    c.add_argument("--run-secret-fd", action="append", default=[], metavar="NAME=N",
                   help="repeatable; the value is read from inherited descriptor N and piped to kubectl's stdin into the run's one Secret, mounted read-only at /run/cw/NAME (0400), deleted with the pod")
    c.add_argument("--run-id", type=_run_id,
                   help="a caller's nonce (lowercase hex), recorded on the pod and Secrets it creates so the caller can prove they are its own")
    c.add_argument("--password-fd", type=int, help="a file descriptor the caller holds; the password is written there once")
    s = cmd("stop")
    s.add_argument("--reason", default="")
    s.add_argument("--run-id", type=_run_id,
                   help="the nonce create recorded: stop acts only when the pod carries it (another run's worker of the same name is left whole, exit 6); without it stop acts by name")
    s.add_argument("--record-dir", help="where the run record goes (default: $CW_RUN_RECORD_DIR, else runs/ beside the audit file)")
    s.add_argument("--no-record", action="store_true", help="write no run record (a record is the default: every stop makes one)")
    e = cmd("extend")
    e.add_argument("--hard")
    e.add_argument("--idle")
    e.add_argument("--reason", default="")
    d = cmd("delete")
    d.add_argument("--force", action="store_true")
    d.add_argument("--reason", default="")
    cmd("status")
    cmd("report")
    pr = cmd("prune-records", worker=False)
    pr.add_argument("--days", type=int, help="remove run records older than this many days (default: $CW_RECORD_KEEP_DAYS, else 30)")
    pr.add_argument("--record-dir")
    r = cmd("reap", worker=False)
    r.add_argument("--loop", type=int, help="seconds between passes (default: one pass)")
    return p


def _run_id(v):
    if not re.fullmatch(r"[0-9a-f]{8,64}", v):
        raise argparse.ArgumentTypeError("--run-id is 8 to 64 lowercase hex characters")
    return v


def main(argv=None, env=None, clock=None, sleep=time.sleep):
    env = os.environ if env is None else env
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else 0
    if not token_mode(env) and not (env.get("CW_KUBE_CONTEXT") or "").strip():
        print("cloud-worker: CW_KUBE_CONTEXT is not set; refusing to run (the active kubectl context is never used)", file=sys.stderr)
        return EXIT_USAGE
    kube = None
    try:
        kube = Kube(env)
        L = Launcher(kube, env, clock, sleep)
        if a.cmd == "reap":
            while True:
                res = L.reap()
                print(json.dumps([{"worker": w, "reason": r, "exit": c} for w, r, c in res]))
                if not a.loop:
                    return EXIT_HOOK if any(c for _, _, c in res) else 0
                sleep(min(a.loop, 60))  # never slower than the contract's once a minute
        result = getattr(L, a.cmd.replace("-", "_"))(a)
        print(result["report"] if a.cmd == "report" and not a.json else json.dumps(result, sort_keys=True))
        return 0
    except CloudError as e:
        print("cloud-worker: %s" % (kube.scrub(e, 1500) if kube else scrub(e, (), 1500)), file=sys.stderr)
        return e.code
    except (Interrupted, KeyboardInterrupt):
        print("cloud-worker: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    finally:
        if kube:
            kube.close()


if __name__ == "__main__":
    sys.exit(main())
