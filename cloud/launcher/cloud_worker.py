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
import fcntl
import datetime as dt
import pwd
import hashlib
import json
import os
import re
import math
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_record  # noqa: E402  (the run record: one module, one function the stop calls)

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")  # the switchboard's agent name rule
NAME_OBJECT = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}")   # a Kubernetes object name (what an ownerReference may name)
SHAPES = {"XS": {"cpu": "1", "memory": "2Gi", "usd_per_hour": 0.05, "stack": False},   # the light shape: no nested Docker (the default of a non-stack worker)
          "S": {"cpu": "2", "memory": "4Gi", "usd_per_hour": 0.10, "stack": True},
          "M": {"cpu": "4", "memory": "8Gi", "usd_per_hour": 0.20, "stack": True}}  # list-price estimates, not billed figures
DEFAULT_SHAPE = "XS"        # a create that names no shape and is not a stack launch (S, M by name only)
DEFAULT_ORCHESTRATOR_SHAPE = "S"   # an orchestrator launch that names no shape keeps the size it had before XS existed (the role table carries no shape: this is the one place)
DEFAULT_STACK_SHAPE = "S"   # a stack launch that names no shape (its row must carry stack: true)
MAX_TOKEN_SECONDS = 3600

# The cluster login (ONE route, used by the launch wiring and by the reaper's own refresh; the numbers of the refresh live here).
CLUSTER_TOKEN_MAX_SECONDS = 3600       # the cap: a cluster token lives at most an hour
CLUSTER_TOKEN_DURATION = "60m"         # the reaper's requested life: the cap, so a pass that stops a worker never meets an expiry
CLUSTER_TOKEN_REFRESH_MARGIN_SECONDS = 1200   # the reaper's margin: the file is renewed when less than 20 minutes are left (about every 40 minutes)
CLUSTER_TOKEN_RETRY_SECONDS = 300      # after a failed refresh: the next attempt is no sooner than this (the vault is read at most every 5 minutes)
TOKEN_CMD_SECONDS = 60                 # a vault read or a mint: its own run limit (seconds; a test pins it)
MINT_ACCOUNT = "cw-operator"           # the ONE account the mint step makes a token for: fixed in code, no setting changes it
MINT_KUBECTL_PATHS = ("/opt/homebrew/bin/kubectl", "/usr/local/bin/kubectl", "/usr/bin/kubectl")   # the ONLY kubectl the strict (reaper) route runs, searched in order
MINT_AZ_PATHS = ("/opt/homebrew/bin/az", "/usr/local/bin/az", "/usr/bin/az")   # the ONLY az the strict (reaper) route runs, searched in order (never PATH)
MINT_PATH = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"   # the PATH of the mint step's children
_NAMESPACE_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")   # a DNS-1123 label: the namespace
# What a vault refusal FOR THE FIREWALL looks like: the text the vault's client prints for its data plane. Matched case-insensitively in the error
# text only (never printed). Any other vault error (no permission, no such secret, not signed in, unreachable) matches neither.
FIREWALL_MARKERS = ("client address is not authorized and caller is not a trusted service", "forbiddenbyfirewall")
REAPER_RUNTIME_ID = "reaper"           # the reaper's own runtime id in the audit (it acts on no worker)
REFRESH_ACTION = "refresh-cluster-token"   # the audit action of the reaper's own login refresh
IGNORED_SETTING_ACTION = "ignored-setting-notice"   # the audit action of the notice that a setting is ignored by the unattended refresh
IGNORED_SETTINGS = ("CW_KUBECTL", "CW_CLUSTER_ADMIN_CMD", "CW_CLUSTER_TOKEN_CMD")   # the names the strict route ignores
IGNORED_NOTICE_SECONDS = 3600          # each ignored name is named on the first pass and then at most once an hour
IGNORED_NOTICE = ("cluster login: %s is ignored in the unattended refresh (the reaper uses a fixed %s path); "
                  "use the launch path for a custom command")

# The git token a worker pushes with is a one-hour installation token (app_token.py caps it at 61 minutes). The launcher mints it once and
# stores it as the `git-token` key of the per-run Secret; the managed reaper renews it by the SAME mint route before it ends. All of the
# renewal's numbers and names are here.
GIT_TOKEN_KEY = "git-token"                      # the Secret key the pod's credential helper and gh wrapper read at every call
GIT_TOKEN_RENEW_AFTER_SECONDS = 1800             # renew when the token is this old: 30 minutes of its life are left (a failed pass or two, a kubelet delay, a short sleep)
GIT_TOKEN_MINTED = "cw/git-token-minted"         # the Secret annotation that says when the token was minted (the Secret's creation time stands in when it is absent)
GIT_TOKEN_MINT_SECONDS = 60                      # the mint child's run limit
GIT_TOKEN_MAX_CHARS = 4096                       # the most a token may be

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
# The fresh-node race retry rule, in ONE place (#1023): there is no attempt count. After a race the launcher pauses
# STACK_RACE_PAUSE_SECONDS, then recreates the pod; a recreate starts only if at least one pause plus one poll
# (WAIT_POLL_SECONDS) fits in what is left of STACK_RACE_WINDOW_SECONDS. A fresh node took ~3-5 minutes to be ready end to end.
# The window stays under the switchboard's own bound on the launcher call (LAUNCH_SECONDS, 900).
STACK_RACE_WINDOW_SECONDS = 600
STACK_RACE_PAUSE_SECONDS = 15
WAIT_POLL_SECONDS = 2   # the pause between two reads of a pending pod
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
STACK_SPEC_KEYS = {"restartPolicy", "activeDeadlineSeconds", "serviceAccountName", "automountServiceAccountToken", "securityContext", "containers", "volumes",
                   "imagePullSecrets", "runtimeClassName", "hostUsers", "nodeSelector", "hostname", "subdomain", "terminationGracePeriodSeconds"}
STACK_CONTAINER_KEYS = {"name", "image", "securityContext", "env", "resources", "readinessProbe", "volumeMounts", "lifecycle"}
STACK_VOLUMES = {"tokens": {"name": "tokens", "emptyDir": {"medium": "Memory"}}, "scratch": {"name": "scratch", "emptyDir": {}}}
STACK_MOUNT_PATHS = {"pw": "/run/cw", "tokens": "/run/cw-tmp", "scratch": "/work"}
# The stack registry puller (ask #1022): a stack pod made with the three registry settings uses its own service account and ONE extra volume, a projected
# service-account token (audience for the token exchange, the shortest life Kubernetes allows, mode 0400) plus three non-secret values, mounted read-only at
# ONE path whose parent is root-owned mode 0700 in the image. Every other shape of a token is refused by the "service-account token" row.
PULLER_SERVICE_ACCOUNT = "cw-stack-puller"
PULLER_VOLUME_NAME = "registry-token"
PULLER_MOUNT_PATH = "/opt/cw-registry/token"
PULLER_AUDIENCE = "api://AzureADTokenExchange"
PULLER_TOKEN_SECONDS = 600
PULLER_ANNOTATIONS = {"registry-host": "cw/registry-host", "client-id": "cw/pull-client-id", "tenant-id": "cw/pull-tenant-id"}
PULLER_VOLUME = {"name": PULLER_VOLUME_NAME, "projected": {"defaultMode": 0o400, "sources": [
    {"serviceAccountToken": {"audience": PULLER_AUDIENCE, "expirationSeconds": PULLER_TOKEN_SECONDS, "path": "token"}},
    {"downwardAPI": {"items": [{"path": k, "fieldRef": {"fieldPath": "metadata.annotations['%s']" % v}} for k, v in PULLER_ANNOTATIONS.items()]}}]}}
PULLER_LABEL = "azure.workload.identity/use"
STACK_REGISTRY_KEYS = ("CW_STACK_REGISTRY", "CW_STACK_PULL_CLIENT_ID", "CW_STACK_PULL_TENANT_ID")


def _token_sources(pod):
    return [s for v in pod.get("spec", {}).get("volumes", []) for s in (v.get("projected") or {}).get("sources", []) if "serviceAccountToken" in (s or {})]


def _service_account_token(pod):
    """The token row: automount stays false; a projected token is allowed ONLY as PULLER_VOLUME in a stack pod of the puller service account,
    mounted once, read-only, at PULLER_MOUNT_PATH, with the workload-identity label absent. Anything else (any pod) is refused."""
    spec = pod.get("spec", {})
    if spec.get("automountServiceAccountToken") is not False:
        return True
    if not _token_sources(pod):
        return False
    vols = spec.get("volumes", [])
    mounts = [m for k in _containers(pod) for m in k.get("volumeMounts", []) if m.get("name") == PULLER_VOLUME_NAME]
    return not (
        _is_stack(pod) and spec.get("serviceAccountName") == PULLER_SERVICE_ACCOUNT
        and PULLER_LABEL not in (pod.get("metadata", {}).get("labels") or {})
        and [v for v in vols if "projected" in v and _token_sources({"spec": {"volumes": [v]}})] == [PULLER_VOLUME]
        and vols.count(PULLER_VOLUME) == 1
        and mounts == [{"name": PULLER_VOLUME_NAME, "mountPath": PULLER_MOUNT_PATH, "readOnly": True}])


def _has_puller_volume(pod):
    return any(v.get("name") == PULLER_VOLUME_NAME for v in pod.get("spec", {}).get("volumes", []))


# The pre-stop hook (ask #1142): the pod pushes its own unpushed work before it ends. The budget is the hook's whole life and
# stays strictly below the grace, so the agent keeps about 15 s of its own SIGTERM time.
POD_TERMINATION_GRACE_SECONDS = 60
PRESTOP_BUDGET_SECONDS = 45
PRESTOP_LIFECYCLE = {"preStop": {"exec": {"command": ["/usr/local/bin/cw-prestop", str(PRESTOP_BUDGET_SECONDS)]}}}
# The one read-only recovery route for work a hook could not push (a constant string).
RECOVERY_ROUTE = ("recover read-only: the work is still in /work/repo of the kept pod; see the pod's own output line "
                  "(cw-prestop: not pushed) and the run record")


def _prestop_differs(pod):
    spec = pod.get("spec", {})
    return (spec.get("terminationGracePeriodSeconds") != POD_TERMINATION_GRACE_SECONDS
            or any(k.get("lifecycle") != PRESTOP_LIFECYCLE for k in spec.get("containers", [])))


def _stack_rest_differs(pod):
    if not _is_stack(pod):
        return False
    spec, ks = pod.get("spec", {}), pod.get("spec", {}).get("containers", [])
    vols = spec.get("volumes", [])
    pw = [v for v in vols if v.get("name") == "pw"]
    return (
        not set(pod.get("metadata", {})) <= STACK_METADATA_KEYS
        or not set(spec) <= STACK_SPEC_KEYS
        or (("hostname" in spec or "subdomain" in spec)   # exactly these two keys, together: the pod's own name and the one subdomain
            and (spec.get("subdomain") != HUB_SUBDOMAIN or spec.get("hostname") != pod.get("metadata", {}).get("name")))
        or spec.get("restartPolicy") != "Never" or not isinstance(spec.get("serviceAccountName"), str) or not spec.get("serviceAccountName")
        or len(ks) != 1 or [k.get("name") for k in ks] != ["worker"]   # one container, named worker: no sidecar, even one with the stack context
        or any(not set(k) <= STACK_CONTAINER_KEYS or not isinstance(k.get("image"), str) or not k.get("image") for k in ks)
        or any(set(e) != {"name", "value"} for k in ks for e in k.get("env", []))   # no valueFrom / secretKeyRef; no envFrom (key allow-list)
        or any(set((k.get("resources") or {})) != {"limits"} or set(k["resources"]["limits"]) != {"cpu", "memory"} for k in ks)   # limits stay
        or any(k.get("readinessProbe") != {"tcpSocket": {"port": 4096}} for k in ks)
        or any(set(m) - {"name", "mountPath", "readOnly"} or {**STACK_MOUNT_PATHS, PULLER_VOLUME_NAME: PULLER_MOUNT_PATH}.get(m.get("name")) != m.get("mountPath")
               for k in ks for m in k.get("volumeMounts", []))
        or sorted(v.get("name") for v in vols) not in (["pw", "scratch", "tokens"], sorted(["pw", "scratch", "tokens", PULLER_VOLUME_NAME]))   # no extra emptyDir, secret or configMap volume
        or (_has_puller_volume(pod) and vols[-1] != PULLER_VOLUME)
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
    "pre-stop lifecycle or termination grace differs from the launcher's": _prestop_differs,
    "service-account token": _service_account_token,
}


def forbidden_pod_fields(pod):
    return [name for name, has in FORBIDDEN_POD_FIELDS.items() if has(pod)]
DEFAULT_MAX_HARD = "8h"  # OPEN in the contract: value
# The cluster-side floor: every pod carries spec.activeDeadlineSeconds = the --max-hard cap + this grace, so the kubelet
# ends a pod even when no reaper runs. The cap, not the current hard deadline: Kubernetes lets that field be lowered on
# a running pod but never raised, so a value at the initial hard limit would make `extend` (within the cap) a lie. The
# grace lets the reaper (which writes the run record and runs the end hook) normally act first.
POD_DEADLINE_GRACE_SECONDS = 300
DEFAULT_SPEND_CAP_USD = 25.0  # the prototype's cap; applies when CW_CLOUD_SPEND_CAP_USD is unset or empty
LABEL = "cw-worker"
# The node labels the summary reads: the POOL a node belongs to and its INSTANCE SIZE, in that order of preference. A label that is not there
# reads "unknown" (never a guess). The size label is the Kubernetes one (`node.kubernetes.io/instance-type`); the pool label is the cloud's own
# (Azure's `agentpool`, GKE's nodepool) or the kit's (`cw/pool`).
NODE_POOL_LABELS = ("cw/pool", "agentpool", "kubernetes.azure.com/agentpool", "cloud.google.com/gke-nodepool", "nodepool")
NODE_SIZE_LABELS = ("node.kubernetes.io/instance-type", "instance-type", "cw/size")
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
ALLOWED_ENV_NAMES = {"CW_TOKEN_ENDPOINT", "FLEET_CLOUD_TOKEN_FILE"}  # a URL; and the PATH of the channel secret's file, not the secret
# #1157: the agent a pod runs. ONE value, two accepted; the five names an orchestrator pod adds to the worker's, and no others.
AGENTS = ("worker", "orchestrator")
ORCHESTRATOR_ENV_NAMES = ("CW_AGENT", "FLEET_AGENT_NAME", "FLEET_BOSS", "FLEET_CLOUD_CHANNEL_URL", "FLEET_CLOUD_TOKEN_FILE")
CHANNEL_SECRET_KEY = "channel-secret"                       # the one new key of the per-run Secret
CHANNEL_TOKEN_FILE = "/run/cw-tmp/channel-token"        # inside the memory-backed directory; the entrypoint installs the key there
DEFAULT_CHANNEL_URL = "http://cloud-hub.cw.svc:8443/message"
DEFAULT_REGISTRY_SECRET = "cloud-hub-registry"
HUB_PW_SECRET = "cloud-hub-pw"                  # one key per agent: the pod's server password, mounted by the hub at /etc/hub/pw
HUB_PW_DIR = "/etc/hub/pw"
CLUSTER_DOMAIN = "cluster.local"   # the one place the cluster domain is named
HUB_SUBDOMAIN = "cw-agents"   # the headless Service (made by the hub manifests) that gives a hub pod the DNS name <pod>.cw-agents.<namespace>.svc.cluster.local


PERSISTENT_ANNOTATION = "cw/persistent"   # on the pod template (so on the pod) and on the StatefulSet of a persistent orchestrator
PERSISTENT_SETTING = "CW_MAX_PERSISTENT"   # the settings maximum of persistent orchestrators in the namespace
PERSISTENT_DEFAULT_MAX = 2
PERSISTENT_MAX_LIMIT = 20
PERSISTENT_DAY_SECONDS = 24 * 3600          # the spend cap prices ONE DAY of the shape for a persistent launch
WORKLOAD_DELETE_SECONDS = 120               # the bounded wait of a StatefulSet delete (kubectl's own --timeout)
PERSISTENT_UNREADABLE = "persistent workload unreadable: left alone"   # the reaper's one report line


def hub_agent_url(pod_name, namespace):
    return "http://%s.%s.%s.svc.%s:4096" % (pod_name, HUB_SUBDOMAIN, namespace, CLUSTER_DOMAIN)


class ReaperLocked(Exception):
    """Another reaper holds the lock, or the lock cannot be taken safely. Raised before any pass."""


class CloudError(Exception):
    audited = False  # True when the failure already wrote its own audit line

    def __init__(self, msg, code=EXIT_CLOUD):
        super().__init__(msg)
        self.code = code


class WorkloadForbidden(CloudError):
    """The cluster refused the read of a StatefulSet: the login's Role lacks the rule for StatefulSets in the apps group."""


class PodChanged(Exception):
    """The pod holding the name is not the one that was read (another uid, or gone): nothing was changed."""


class _FreshNodeRace(Exception):
    """The pod's sandbox failed on the handler-not-known message: the node's containerd was not yet configured."""


class _NotRenewed(Exception):
    """A git-token renewal that is refused or failed; the text is the reason (never a value)."""


class _PodVanished(Exception):
    """The pending stack pod existed and then a get said not found (seen on a fresh node when its runtime restarted)."""


class Interrupted(BaseException):
    """SIGINT or SIGTERM during create: not an Exception, so no handler in between swallows it."""


EXIT_INTERRUPTED = 130


def _raise_interrupted(signum, frame):
    raise Interrupted("signal %d" % signum)


TOKEN_SHAPES = [re.compile(p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{6,}", r"github_pat_[A-Za-z0-9_]{6,}", r"eyJ[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]*)*",
    r"[A-Za-z0-9+/]{40,}={0,2}", r"[A-Za-z0-9_-]{40,}", r"[0-9a-fA-F]{40,}")]  # standard and URL-safe base64, hex
# Credentials by SHAPE (a push error that echoes a request): an Authorization header of any scheme (any case and spacing,
# also quoted as in JSON), and the user:password of a URL of any scheme (the password may be percent-encoded). Each is
# replaced whole; they run BEFORE the token shapes so the surrounding text survives.
CREDENTIAL_SHAPES = [(re.compile(p, re.I), r) for p, r in (
    # the rest of the header line goes, and its folded continuation lines (a line that starts with a space or tab): a
    # multi-parameter scheme (Digest user="u", response="...") is not cut at its first comma
    (r"""\bauthorization["']?[ \t]*[:=][ \t]*[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*""", "authorization: [redacted]"),
    # URL userinfo stays INSIDE ITS OWN PART: the user holds no / ? # @ :, and a password holds no / ? # (it may hold @ and : : the last @ before the part ends closes it), so
    # host/path/query is never userinfo (a scoped package, an @user path, ?email=a@b, a port). Every quantifier is bounded
    # (hostile text cannot make the scan quadratic).
    (r"""\b([a-z][a-z0-9+.-]{0,30}://)[^\s/?#@:'"<>]{0,200}(?::[^\s/?#'"<>]{0,500})?@""", r"\1[redacted]@"),
    # An UNENCODED reserved character in a password (user:p/a?b#c@host): a ':' straight after the user, a password that is
    # not just a port (digits then / ? # or the end), up to the last @ within the bound.
    (r"""\b([a-z][a-z0-9+.-]{0,30}://)[^\s/?#@:'"<>]{0,200}:(?!\d+(?:[/?#\s'"<>]|$))[^\s'"<>]{0,500}@""", r"\1[redacted]@"))]
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
    for pat, repl in CREDENTIAL_SHAPES:
        text = pat.sub(repl, text)
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


def allowed_repo_keys(raw):
    """The allow-list's entries as keys (a None for an entry that is not exactly OWNER/NAME). THE parse of CW_ALLOWED_REPOS: the check below and
    the settings file's validator both call it."""
    return [_repo_key(x.strip()) for x in raw.split(",")]


def check_repo_allowed(repo, env):
    """The one allow-list check (the launcher's create and the driver both call it). Raises CloudError(EXIT_USAGE) before anything is
    made. CW_ALLOWED_REPOS: comma-separated OWNER/NAME, exact and case-insensitive; unset, empty or any malformed entry refuses.
    CW_TOOLING_REPO: the tooling's own repository, refused even when listed; unset adds no check, malformed refuses."""
    raw = (env.get("CW_ALLOWED_REPOS") or "").strip()
    if not raw:
        raise CloudError("refused: %s is not on the cloud allow-list (CW_ALLOWED_REPOS is unset or empty)" % scrub(repo, (), 120), EXIT_USAGE)
    allowed = allowed_repo_keys(raw)
    if None in allowed:
        raise CloudError("refused: CW_ALLOWED_REPOS has an entry that is not OWNER/NAME; nothing was created", EXIT_USAGE)
    tooling = (env.get("CW_TOOLING_REPO") or "").strip()
    tkey = _repo_key(tooling) if tooling else None
    if tooling and tkey is None:
        raise CloudError("refused: CW_TOOLING_REPO is not OWNER/NAME; nothing was created", EXIT_USAGE)
    key = _repo_key(repo)
    if key is None or key not in allowed or key == tkey:
        raise CloudError("refused: %s is not on the cloud allow-list" % scrub(repo, (), 120), EXIT_USAGE)


def git_token_cmd(repo, env):
    """The command that mints the git token for ONE repository: CW_GIT_TOKEN_CMD, else app_token.py beside this file. The launch (run_task.py)
    and the renewal run the same command; the renewal builds it here and the driver's own line in run_task.py is pinned equal by a test."""
    raw = env.get("CW_GIT_TOKEN_CMD")
    return shlex.split(raw) if raw else [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_token.py"), repo]


def run_git_token_cmd(cmd, repo, env, register=None):
    """THE mint call (the launch and the renewal both come here): run cmd for repo in env, return the token it printed. Never prints, never puts
    the token in an argument or a file. register(text) hears the child's stdout before it is judged, so a caller's scrubber knows it.
    Raises CloudError(EXIT_USAGE) with no value in the text."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_TOKEN_MINT_SECONDS, stdin=subprocess.DEVNULL, env=dict(env, GH_REPO=repo))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        p = None
    if p and register:
        register(p.stdout.strip())
    token = (p.stdout if p and p.returncode == 0 else "").strip()
    if not token or len(token) > GIT_TOKEN_MAX_CHARS:
        why = ""
        if p and p.stderr.strip():
            why = ": " + scrub(p.stderr.strip().splitlines()[-1], (), 200)
        raise CloudError("the git token command gave no usable token%s" % why, EXIT_USAGE)
    return token


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
    "CW_APP_ID_CMD",           # optional commands that print the App id and key instead of the vault (app_token.py)
    "CW_APP_INSTALLATION_CMD",   # obsolete: accepted and ignored (the installation is looked up), so an old config still validates
    "CW_APP_KEY_CMD",
)
# Never taken from the caller: PATH (a fixed one is built here), HOME and TMPDIR (a private empty directory), PYTHON* (the child runs `python -I`),
# AZURE_CONFIG_DIR (kept only when an absolute existing directory, else the account's ~/.azure), CW_OPENSSL (kept only when it is an absolute executable file).
PREFLIGHT_PATH = "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"


def _abs_executable(path):
    return bool(path) and os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _scrubbed_env(env, home, names, openssl=True, real_home=False):
    """THE scrubbed child environment (the preflight and the vault access check both use it): the allow-listed `names` (and LC_*), a fixed PATH, an
    empty private HOME and TMPDIR, and where `az` keeps its sign-in (the caller's AZURE_CONFIG_DIR when it is an absolute existing directory, else the
    account's own ~/.azure from the password database, never $HOME)."""
    clean = {k: v for k, v in env.items() if k in names or k.startswith("LC_")}
    clean["PATH"] = PREFLIGHT_PATH
    clean["HOME"] = home
    clean["TMPDIR"] = home
    if real_home:   # the vault access check keeps its own log under the user's home: the PASSWORD DATABASE's home, never the HOME variable
        try:
            clean["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
        except (KeyError, OSError):
            pass
    cfg = env.get("AZURE_CONFIG_DIR") or ""
    if not (os.path.isabs(cfg) and os.path.isdir(cfg)):
        try:
            cfg = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".azure")
        except (KeyError, OSError):
            cfg = ""
    if cfg:
        clean["AZURE_CONFIG_DIR"] = cfg
    ossl = env.get("CW_OPENSSL") or ""
    if openssl and _abs_executable(ossl):
        clean["CW_OPENSSL"] = ossl
    return clean


def _preflight_env(env, home):
    clean = _scrubbed_env(env, home, PREFLIGHT_ENV_NAMES)
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


# ---------------------------------------------------------------- the vault access check (#876 slice 2)
# A program of another repository, run UNCHANGED from a configured checkout (CW_VAULT_CHECK_DIR), only after a vault read was refused by the vault's
# firewall. Contract (agreed with its owner, who announces any change first): the module below, run with `check` from the root of the checkout; no other
# argument, option, environment or file; limit 300 s; it adds at most ONE single-address rule, only this machine's own address; it prints a count line.
VAULT_CHECK_KEY = "CW_VAULT_CHECK_DIR"
VAULT_CHECK_MODULE = "scripts.v2.cw_vault_firewall"
VAULT_CHECK_FILE = "scripts/v2/cw_vault_firewall.py"
VAULT_CHECK_ARGS = ("check",)           # the whole argument list after the module: never an address, never an option
VAULT_CHECK_SECONDS = 300               # the check's own run limit
VAULT_CHECK_GRACE = 15                  # ours, on top, before the process group is killed
VAULT_CHECK_ENV_NAMES = ()              # nothing is passed from the caller: the child gets PATH, a private HOME and TMPDIR, AZURE_CONFIG_DIR and LC_*
VAULT_CHECK_PYTHON = None               # a test hook (a module attribute): None means python3 found on the fixed PATH
# The exit codes, one table: code -> (outcome, the launch continues, what it means). Any other code (or no code: killed, timed out, not run) is treated as 40.
VAULT_CHECK_CODES = {
    0: ("answered", True, "the vault answered; nothing was changed"),
    10: ("added", True, "an address was added and the vault now answers"),
    20: ("refused-by-its-rules", False, "the check refused by its own rules (its cap of 6 rules is reached, or an address it will not add)"),
    30: ("added-still-refused", False, "an address was added but the vault still refused after 90 seconds"),
    40: ("failed", False, "the check failed for another reason"),
}
VAULT_CHECK_FALLBACK = 40
_COUNT_LINE = re.compile(r"rules (\d{1,4}) -> (\d{1,4})(?:, (\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?))?")


class CheckResult:
    """What one run of the check came to: its exit code UNCHANGED (None when it was killed, timed out or never ran), the outcome name, and the
    counts of its count line (None, None when none was found). Never an address."""
    def __init__(self, code, outcome, before=None, after=None, line=None):
        self.code, self.outcome, self.before, self.after, self.line = code, outcome, before, after, line

    @property
    def effective(self):
        return self.code if self.code in VAULT_CHECK_CODES else VAULT_CHECK_FALLBACK


VAULT_CHECK_MAX_ENTRIES = 200000       # a checkout with more entries than this is refused (a bounded walk)
VAULT_CHECK_SKIP = (".git",)           # the top-level version-control directory is never loaded by the interpreter


sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bin"))   # LAST: the launcher's own modules win a name
from path_walk import entry_problem as _entry_problem, parents_problem  # noqa: E402  (the one walk, also used by the installer)


def vault_check_dir_problem(checkout):
    """None when `checkout` is a usable checkout, else ONE short reason (never the path's content). `python3 -m` loads anything under the checkout,
    so EVERY entry is judged (lstat, never followed): an absolute path to an existing directory that is not a symlink, owned by the user, with every
    entry (directories, files, links, every __init__.py) owned by the user, not group- or world-writable and not a symlink; every parent judged by
    parents_problem; and scripts/v2/cw_vault_firewall.py a regular file. The top-level .git directory is not walked."""
    if not isinstance(checkout, str) or not checkout.strip() or not os.path.isabs(checkout) or ".." in checkout.split("/"):
        return "is not an absolute path"
    checkout = checkout.rstrip("/") or "/"
    problem = parents_problem(checkout)
    if problem:
        return problem
    try:
        top = os.lstat(checkout)
    except OSError:
        return "has no checkout directory"
    problem = _entry_problem(top, "the checkout")
    if problem:
        return problem
    if not stat.S_ISDIR(top.st_mode):
        return "is not a directory"
    module = os.path.join(checkout, VAULT_CHECK_FILE)
    try:
        found = os.lstat(module)
    except OSError:
        return "has no module"
    if not stat.S_ISREG(found.st_mode):
        return "has a module that is not a regular file"
    count, stack = 0, [checkout]
    while stack:
        here = stack.pop()
        try:
            names = os.listdir(here)
        except OSError:
            return "has a directory that cannot be read"
        for name in names:
            if here == checkout and name in VAULT_CHECK_SKIP:
                continue
            count += 1
            if count > VAULT_CHECK_MAX_ENTRIES:
                return "has more than %d entries" % VAULT_CHECK_MAX_ENTRIES
            full = os.path.join(here, name)
            try:
                found = os.lstat(full)
            except OSError:
                return "has an entry that cannot be read"
            problem = _entry_problem(found, "an entry")
            if problem:
                return problem
            if stat.S_ISDIR(found.st_mode):
                stack.append(full)
    return None


def _check_python():
    if VAULT_CHECK_PYTHON:
        return VAULT_CHECK_PYTHON
    found = shutil.which("python3", path=PREFLIGHT_PATH)
    return os.path.abspath(found) if found else None


def _kill_group(child):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except OSError:
            pass
        try:
            child.wait(5)
            break
        except subprocess.TimeoutExpired:
            continue
    try:   # the whole group, also what the child left behind
        os.killpg(child.pid, signal.SIGKILL)
    except OSError:
        pass


def vault_check_ready(env):
    """(checkout, python3) when the check may run, else a CloudError (exit 2, naming CW_VAULT_CHECK_DIR, never the path) and NOTHING has run."""
    checkout = (env.get(VAULT_CHECK_KEY) or "").strip()
    if not checkout:
        raise CloudError("refused: %s is not set: the vault refused this machine and the owner of the vault access check provides the checkout it runs "
                         "from; nothing was changed" % VAULT_CHECK_KEY, EXIT_USAGE)
    problem = vault_check_dir_problem(checkout)
    if problem:
        raise CloudError("refused: %s %s: the owner of the vault access check provides the checkout; nothing was run" % (VAULT_CHECK_KEY, problem), EXIT_USAGE)
    python = _check_python()
    if not python:
        raise CloudError("refused: python3 was not found for the vault access check; nothing was run", EXIT_USAGE)
    return checkout, python


def run_vault_check(env):
    """THE one call of the vault access check: runs the module with the argument list [python3, -m, MODULE, check] from the checkout (after
    vault_check_ready), in a scrubbed environment, for at most VAULT_CHECK_SECONDS (+ grace; the process group is killed), and returns a CheckResult
    carrying the exit code unchanged. Its output is read for the count line only and is never printed or kept."""
    checkout, python = vault_check_ready(env)
    argv = [python, "-m", VAULT_CHECK_MODULE] + list(VAULT_CHECK_ARGS)
    out = ""
    try:
        with tempfile.TemporaryDirectory(prefix="cw-vcheck-") as home:
            out_path = os.path.join(home, "out")   # a file, not a pipe: a grandchild that keeps it open cannot hold us up
            with open(out_path, "wb") as sink:
                child = subprocess.Popen(argv, cwd=checkout, env=_scrubbed_env(env, home, VAULT_CHECK_ENV_NAMES, openssl=False, real_home=True),
                                         stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.DEVNULL, start_new_session=True)
                try:
                    child.wait(timeout=VAULT_CHECK_SECONDS + VAULT_CHECK_GRACE)
                except subprocess.TimeoutExpired:
                    _kill_group(child)
                    return CheckResult(None, "timed-out")
                except BaseException:
                    _kill_group(child)
                    raise
                try:   # whatever the check left running in its group ends with it
                    os.killpg(child.pid, signal.SIGKILL)
                except OSError:
                    pass
            with open(out_path, "rb") as source:
                out = source.read(100001).decode("utf-8", "replace")
    except OSError:
        return CheckResult(None, "not-run")
    code = child.returncode
    outcome = VAULT_CHECK_CODES[code][0] if code in VAULT_CHECK_CODES else "unknown-code"
    before = after = line = None
    for text in out[:100000].splitlines():
        found = _COUNT_LINE.search(text)
        if found:
            before, after = int(found.group(1)), int(found.group(2))
            line = "rules %d -> %d%s" % (before, after, ", " + found.group(3) if found.group(3) else "")   # rebuilt from the parts, never echoed
            break
    return CheckResult(code, outcome, before, after, line)


def token_mode(env):
    """True when either service-account setting is present: the token-file login is then the only one (S8, S10)."""
    return bool((env.get("CW_CLOUD_SERVER") or "").strip() or (env.get("CW_CLOUD_TOKEN_FILE") or "").strip())


# ---------------------------------------------------------------- the cluster login (ONE route: the launch wiring and the reaper)
# The administrator kubeconfig is read from the vault into memory and handed to the one `kubectl create token cw-operator` command over a pipe
# (never a file, never an argument); it is dropped when that command ends and nothing is cached. The token it makes is for the namespaced
# operator account only: it holds the operator's rights, never the administrator's.

class ClusterLogin(Exception):
    """The cluster token could not be made: `kind` names the step that failed (never a value)."""

    def __init__(self, kind, lifetime=0.0, firewall=False, recovered=False):
        super().__init__(kind)
        self.kind, self.lifetime, self.firewall, self.recovered = kind, lifetime, firewall, recovered


def capture_command(argv, env=None, feed=None, timeout=TOKEN_CMD_SECONDS, limit=1000000):
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
            os.close(writer[0])
            os.close(writer[1])
        return None
    if writer:
        os.close(writer[0])
        threading.Thread(target=_feed_pipe, args=(writer[1], feed), daemon=True).start()
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


def _feed_pipe(fd, text):
    """Write the fed text into the pipe and close it (the child's other end reads it)."""
    try:
        data = text.encode()
        while data:
            data = data[os.write(fd, data):]
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def firewall_refused(ran):
    """True when a vault read's result (exit code, stdout, stderr; None when it could not run) is the vault's FIREWALL refusing this machine."""
    if ran is None or ran[0] == 0:
        return False
    text = (ran[2] or "").lower()
    return any(marker in text for marker in FIREWALL_MARKERS)


def jwt_claims(token):
    """(exp, iat) of a JWT's claims, or None when the text is not a JWT with a readable exp. Never raises."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        body = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
        exp, iat = claims["exp"], claims.get("iat")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    if iat is not None and (isinstance(iat, bool) or not isinstance(iat, (int, float))):
        return None
    return float(exp), (float(iat) if iat is not None else None)


def jwt_lifetime(token, now):
    """Seconds the token lives, read from its exp (and iat) claim when it is a JWT; None when it is not one (the requested duration is trusted)."""
    claims = jwt_claims(token)
    if claims is None:
        return None
    exp, iat = claims
    return exp - (iat if iat is not None else now)


def token_expiry(token):
    """The token's expiry in epoch seconds, read from its exp claim; None when the text is not a token with a readable expiry."""
    claims = jwt_claims(token)
    return claims[0] if claims else None


def mint_env(env):
    """The environment of the mint step's children (az, kubectl, sh -c): minimal. A fixed PATH, HOME, LANG, the az settings directory only when
    the person set it, and KUBECONFIG=/dev/null. Nothing else: no token, no CW_ setting, no other variable of the reaper."""
    out = {"PATH": MINT_PATH, "HOME": env.get("HOME") or "", "LANG": env.get("LANG") or "C.UTF-8", "KUBECONFIG": os.devnull}
    if env.get("AZURE_CONFIG_DIR"):
        out["AZURE_CONFIG_DIR"] = env["AZURE_CONFIG_DIR"]
    return out


def mint_minutes(duration):
    """The requested minutes of the token, as an integer, from "Nm" (or "Nh"); ClusterLogin("duration-refused") otherwise."""
    m = re.fullmatch(r"([0-9]{1,3})([mh])", str(duration))
    if not m:
        raise ClusterLogin("duration-refused")
    minutes = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
    if not 1 <= minutes <= CLUSTER_TOKEN_MAX_SECONDS // 60:
        raise ClusterLogin("duration-refused")
    return minutes


def _kubectl_problem(path):
    """None when `path` is an absolute path to an existing regular executable file (after links) that is not group- or world-writable."""
    if not path or not os.path.isabs(path):
        return "is not an absolute path"
    real = os.path.realpath(path)
    try:
        info = os.stat(real)
    except OSError:
        return "does not exist"
    if not stat.S_ISREG(info.st_mode) or not os.access(real, os.X_OK):
        return "is not an executable file"
    if info.st_mode & 0o022:
        return "is group- or world-writable"
    if info.st_uid not in (0, os.geteuid()):
        return "is owned by another user"
    return None


def mint_kubectl(env=None):
    """The kubectl the strict mint step runs: the first of MINT_KUBECTL_PATHS that passes the check. CW_KUBECTL is IGNORED (environment and file).
    No passing program is refused (ClusterLogin("kubectl-refused")); nothing is read before."""
    for path in MINT_KUBECTL_PATHS:
        if _kubectl_problem(path) is None:
            return os.path.realpath(path)
    raise ClusterLogin("kubectl-refused")


def mint_az():
    """The az the strict vault read runs: the first of MINT_AZ_PATHS that passes the check; never found through PATH."""
    for path in MINT_AZ_PATHS:
        if _kubectl_problem(path) is None:
            return os.path.realpath(path)
    raise ClusterLogin("az-refused")


def settings_file_problem(env):
    """None when there is no settings file or it is private to this user (the launcher's own check: regular, owned by the user, mode 0600);
    else the reason. Used before a custom command or program from the settings is honoured."""
    try:
        _read_settings_file(settings_path(env))
    except CloudError as e:
        return str(e)
    return None


def cluster_token(env, duration, capture=None, now=None, recovery=None, strict=False):
    """THE cluster login: a token for the namespaced operator account, in memory, at most an hour.

    The route is the launch's own: CW_CLUSTER_TOKEN_CMD (a command that prints one), else the administrator kubeconfig read from the vault
    (CW_CLUSTER_ADMIN_CMD, else `az keyvault secret show` with the vault's subscription and name) and `kubectl create token cw-operator
    --duration=...` run with it on a pipe. `duration` is the requested life ("Nm"). `recovery` (the launch only) is the vault access check: it
    runs once, after a firewall refusal, and the vault is read once more. Nothing is cached or kept: the administrator value is dropped when the
    one command ends. A failure raises ClusterLogin: its kind says which step failed, never a value.

    `strict` (default False) is the UNATTENDED reaper's mode: a FIXED route (az and kubectl from fixed absolute lists, checked; CW_KUBECTL,
    CW_CLUSTER_ADMIN_CMD and CW_CLUSTER_TOKEN_CMD ignored), the fixed account and subcommand, a DNS-label namespace, and a minimal child environment. strict=False is the interactive launch path (a person's
    own shell): the full environment, the program found on PATH, no extra refusals."""
    env = dict(env)
    clock = time.time() if now is None else now
    capture = capture or capture_command
    custom = (env.get("CW_CLUSTER_TOKEN_CMD") or "").strip()
    admin = (env.get("CW_CLUSTER_ADMIN_CMD") or "").strip()
    if strict:
        custom = admin = ""   # the unattended route is FIXED: CW_CLUSTER_TOKEN_CMD, CW_CLUSTER_ADMIN_CMD and CW_KUBECTL are ignored
    child_env = mint_env(env) if strict else env
    if custom:
        ran = capture(["sh", "-c", custom], env=child_env)
        if ran is None or ran[0] != 0:
            raise ClusterLogin("token-command-failed")
        token = ran[1].strip()
    else:
        if strict:
            minutes = mint_minutes(duration)
            namespace = (env.get("CW_CLOUD_NAMESPACE") or "cw")
            if not _NAMESPACE_LABEL.fullmatch(namespace) or "\n" in namespace:
                raise ClusterLogin("namespace-refused")
            kubectl = mint_kubectl()   # every refusal is BEFORE the vault is read
            az_path = mint_az()
        else:
            namespace, kubectl = env.get("CW_CLOUD_NAMESPACE", "cw"), env.get("CW_KUBECTL", "kubectl")
        if admin:
            argv = ["sh", "-c", admin]
        else:
            sub, vault = (env.get("CW_VAULT_SUBSCRIPTION") or "").strip(), (env.get("CW_VAULT_NAME") or "").strip()
            if not sub or not vault:
                raise ClusterLogin("vault-settings-missing")
            az = az_path if strict else "az"
            argv = [az, "keyvault", "secret", "show", "--subscription", sub, "--vault-name", vault,
                    "--name", env.get("CW_CLUSTER_ADMIN_SECRET") or "cw-cluster-admin-kubeconfig", "--query", "value", "-o", "tsv"]
        def read():
            return capture(argv, env=child_env)
        ran = read()
        recovered = False
        if recovery is not None and not admin and firewall_refused(ran):   # only the default vault read, only a firewall refusal, once
            ran = recovery(read, env)
            recovered = True
        if ran is None or ran[0] != 0 or not ran[1].strip() or len(ran[1]) > 1000000:
            raise ClusterLogin("admin-unreadable", firewall=firewall_refused(ran), recovered=recovered)
        admin_config = ran[1]
        # The command is fixed in code: only the program (an absolute, checked path), the namespace (a DNS label) and the minutes come from outside.
        mint_argv = [kubectl, "--kubeconfig", "/dev/fd/{fd}", "-n", namespace, "create", "token", MINT_ACCOUNT,
                     "--duration=%dm" % minutes if strict else "--duration=" + duration]
        minted = capture(mint_argv, env=child_env if strict else dict(env, KUBECONFIG=os.devnull), feed=admin_config)
        del admin_config
        if minted is None or minted[0] != 0:
            raise ClusterLogin("mint-failed")
        token = minted[1].strip()
    if not token or len(token) > 8192 or re.search(r"\s", token) or re.search(r"[\x00-\x1f\x7f]", token):
        raise ClusterLogin("not-a-token")
    lifetime = jwt_lifetime(token, clock)
    if lifetime is not None and not 0 < lifetime <= CLUSTER_TOKEN_MAX_SECONDS:
        raise ClusterLogin("too-long", lifetime=lifetime)
    return token


# ---------------------------------------------------------------- the reaper's own login: the token file stays fresh
# The file holds a token for the namespaced operator account that lives at most an hour. Nothing else in this repository writes or refreshes it:
# an installed reaper would stop working within the hour unless a person refreshed it by hand. The reaper therefore refreshes it itself, by the
# launch's own route, at the start of every pass.

def token_file_life_left(path, now):
    """Seconds of life left in the token file, read from the token's own expiry claim; None when the file cannot be read as a token (a missing,
    empty, unreadable or unreadable-as-a-token file counts as expired)."""
    try:
        with open(path) as f:
            info = os.fstat(f.fileno())
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return None   # not the user's own private file: it counts as expired (and is replaced by a private one)
            body = f.read(65536)
    except OSError:
        return None
    text = body.strip()
    if not text:
        return None
    expiry = token_expiry(text)
    if expiry is None:
        return None   # a file that cannot be read as a token counts as expired
    return expiry - now


def write_token_file(path, token):
    """Write the token to its file: mode 0600, by an atomic replace (a temp file in the same directory, then os.replace). The old file stays whole
    on any failure and the temp file is removed. The file's directory must be the user's own and not group- or world-writable (the one walk:
    parents_problem and entry_problem): it is refused, never repaired."""
    directory = os.path.dirname(os.path.abspath(path))
    problem = parents_problem(directory)
    if not problem:
        try:
            found = os.lstat(directory)
        except OSError:
            problem = "has a directory that cannot be read"
        else:
            problem = _entry_problem(found, "the token file's directory")
    if problem:
        raise OSError("the token file's directory %s" % problem)
    temp = os.path.join(directory, ".%s.%d.%s.tmp" % (os.path.basename(path), os.getpid(), secrets.token_hex(4)))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)   # the mode is exactly 0600, whatever the umask is
        with os.fdopen(fd, "w") as f:
            f.write(token)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


# The reason of each failed step, in one line, naming the step and never a value.
REFRESH_REASONS = {
    "vault-settings-missing": "the vault settings are missing (CW_VAULT_SUBSCRIPTION and CW_VAULT_NAME)",
    "admin-unreadable": "the administrator credential could not be read from the vault",
    "mint-failed": "the token could not be minted",
    "not-a-token": "the answer is not a token",
    "too-long": "the token lives over the 60 minutes allowed",
    "token-command-failed": "the token command failed",
    "settings-untrusted": "the settings file is not private to this user (mode 0600, owned by the user): its custom commands are refused",
    "kubectl-refused": "no kubectl of the fixed list is a plain executable file that only its owner can write",
    "az-refused": "no az of the fixed list is a plain executable file that only its owner can write",
    "namespace-refused": "the namespace is not a DNS label (CW_CLOUD_NAMESPACE)",
    "duration-refused": "the requested token life is not a whole number of minutes",
}
FIREWALL_REASON = "the vault refused this machine"   # the firewall refused this machine: the launch path's own words


def refresh_reason(err):
    """The reason a refresh failed, in one line: which step, never a value."""
    if getattr(err, "firewall", False):
        return FIREWALL_REASON
    return REFRESH_REASONS.get(getattr(err, "kind", ""), "the cluster token could not be obtained")


def audit_path(env):
    """Where the audit file is: the setting, else the state directory."""
    return env.get("CW_CLOUD_AUDIT") or os.path.join(env.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "cw-cloud", "audit.jsonl")


def write_audit(path, line):
    """One audit line, appended (the file is 0600). It never raises: the action already ran, so a loud warning beats undoing it."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:   # one short O_APPEND write: atomic enough for one line, the chosen guarantee
            f.write(json.dumps(line, sort_keys=True) + "\n")
    except OSError as e:
        print("cloud-worker: WARNING: the action ran but its audit line could not be written: %s" % e, file=sys.stderr)


def last_audit_line(path, action, runtime_id=None):
    """(ok, reason) of the newest audit line with this action (and this runtime id), or None. Only the file's tail is read."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 262144))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if rec.get("action") == action and (runtime_id is None or rec.get("runtime_id") == runtime_id):
            return rec.get("ok"), rec.get("reason")
    return None


class TokenRefresh:
    """What one refresh attempt came to: the state, the reason (never a value) and the token that was written."""

    def __init__(self, state, reason="", token=""):
        self.state, self.reason, self.token = state, reason, token


class ClusterTokenRefresher:
    """The reaper's own login: the token file stays fresh without a person.

    When the file is missing, or has less than CLUSTER_TOKEN_REFRESH_MARGIN_SECONDS of life left, a fresh short-lived token for the namespaced
    operator account is minted by the launch's own route and written to the file with mode 0600, by an atomic replace. The old file stays whole
    on any failure; one audit line names the reason (one line per change of reason, the renewal's rule) and the next attempt is no sooner than
    CLUSTER_TOKEN_RETRY_SECONDS later, so a failing vault is read at most every five minutes. Only the token-file login is ever touched: a kube
    context or a kubeconfig is left alone, and no other credential is ever tried."""

    def __init__(self, env, clock, capture=None):
        self.env, self.clock, self.capture = dict(env), clock or (lambda: dt.datetime.now(dt.timezone.utc)), capture
        self.audit_file = audit_path(self.env)
        self.kube = None            # the Kube, when there is one: its scrubber knows this run's secrets
        self.last_failure = None    # (when, reason): the retry gap and the reason of the last failure
        self.noticed = {}           # ignored setting name -> when it was last named
        self.notices = []           # the notice lines due in the last run (names only, never a value)

    def ignored_settings(self):
        """The ignored names that are set (non-empty) in the process environment or in the settings file; names only. An unreadable file is
        not an error here: the strict route does not depend on it."""
        found = {n for n in IGNORED_SETTINGS if (self.env.get(n) or "").strip()}
        try:
            text = _read_settings_file(settings_path(self.env))
            values = {m.group(1): m.group(2) for m in re.finditer(r"^[ \t]*([A-Z0-9_]+)=(.*)$", text or "", re.M)}   # names only matter: no value is kept or shown
        except CloudError:
            values = {}
        found |= {n for n in IGNORED_SETTINGS if str(values.get(n) or "").strip()}
        return [n for n in IGNORED_SETTINGS if n in found]

    def note_ignored(self, now):
        """The notice for each ignored setting that is set, on the first pass and then at most once an hour: one line and one audit line each."""
        self.notices = []
        for name in self.ignored_settings():
            last = self.noticed.get(name)
            if last is not None and 0 <= now - last < IGNORED_NOTICE_SECONDS:
                continue
            self.noticed[name] = now
            line = IGNORED_NOTICE % (name, "az" if name == "CW_CLUSTER_ADMIN_CMD" else "kubectl")
            self.notices.append(line)
            write_audit(self.audit_file, {"ts": iso(self.clock()), "worker": "", "runtime_id": REAPER_RUNTIME_ID, "action": IGNORED_SETTING_ACTION,
                                          "cost_estimate_usd": 0.0, "ok": True, "actor": pwd.getpwuid(os.getuid()).pw_name, "reason": line})

    def scrub(self, text, limit=1000):
        return self.kube.scrub(text, limit) if self.kube else scrub(text, (), limit)

    def enabled(self):
        """True when the reaper's own login is the token file: a kube context or a kubeconfig is another login and is never touched."""
        if (self.env.get("CW_KUBE_CONTEXT") or "").strip() or (self.env.get("CW_KUBECONFIG") or "").strip():
            return False
        return bool((self.env.get("CW_CLOUD_TOKEN_FILE") or "").strip())

    def path(self):
        return (self.env.get("CW_CLOUD_TOKEN_FILE") or "").strip()

    def run(self):
        """One attempt: make the file fresh when it is due. Returns a TokenRefresh (never raises)."""
        if not self.enabled():
            return TokenRefresh("skipped")   # another login kind: nothing is read and nothing is written
        now = self.clock().timestamp()
        self.note_ignored(now)
        if self.last_failure is not None and 0 <= now - self.last_failure[0] < CLUSTER_TOKEN_RETRY_SECONDS:   # a negative gap (the clock went back) counts as expired
            return TokenRefresh("waiting", self.last_failure[1])   # the retry gap: no new attempt yet
        path = self.path()
        life = token_file_life_left(path, now)
        if life is not None and life >= CLUSTER_TOKEN_REFRESH_MARGIN_SECONDS:
            return TokenRefresh("fresh")   # the file has life left: it is left alone
        try:
            token = cluster_token(self.env, duration=CLUSTER_TOKEN_DURATION, capture=self.capture, now=now, strict=True)
            write_token_file(path, token)
        except ClusterLogin as e:
            reason = refresh_reason(e)
            self.last_failure = (now, reason)
            self.audit(reason, False)
            return TokenRefresh("failed", reason)
        except OSError as e:
            reason = "the token file could not be written: %s" % (e.strerror or e)
            self.last_failure = (now, reason)
            self.audit(reason, False)
            return TokenRefresh("failed", reason)
        self.last_failure = None
        self.audit("", True)
        return TokenRefresh("written", "", token)

    def audit(self, reason, ok):
        """One audit line per change of reason (the renewal's rule): a persistent failure is not written again every pass, and the success line
        is the one that clears it. The reason names the step, never a value."""
        text = self.scrub(reason, limit=1000)
        if not ok and last_audit_line(self.audit_file, REFRESH_ACTION, REAPER_RUNTIME_ID) == (False, text):
            return
        write_audit(self.audit_file, {"ts": iso(self.clock()), "worker": "", "runtime_id": REAPER_RUNTIME_ID, "action": REFRESH_ACTION,
                                      "cost_estimate_usd": 0.0, "ok": ok, "actor": pwd.getpwuid(os.getuid()).pw_name, "reason": text})


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


def max_persistent(env):
    """The settings maximum of persistent orchestrators in the namespace: the setting (an integer 1 to 20), else 2. A bad value refuses (exit 2)."""
    raw = (env.get(PERSISTENT_SETTING) or "").strip()
    if not raw:
        return PERSISTENT_DEFAULT_MAX
    if not re.fullmatch(r"[0-9]{1,2}", raw) or not 1 <= int(raw) <= PERSISTENT_MAX_LIMIT:
        raise CloudError("%s must be an integer from 1 to %d; nothing was created" % (PERSISTENT_SETTING, PERSISTENT_MAX_LIMIT), EXIT_USAGE)
    return int(raw)


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


REGISTRY_KEYS = ("secrets.json", "agents.json")


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
            self.dir = None

    def run(self, args, stdin=None, ok_missing=False, keep_stdout=False, timeout=120, missing_what=None):
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
            if ok_missing and "NotFound" in p.stderr and (missing_what is None or (missing_what in p.stderr and " not found" in p.stderr)):
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
        out = self.run(["get", "pod", name, "-o", "json"], ok_missing=True, missing_what='pods "%s"' % name)
        return json.loads(out) if out else None

    def list_pods(self):
        return json.loads(self.run(["get", "pods", "-l", "app=" + LABEL, "-o", "json"]))["items"]

    def _raw(self, args, timeout=120):
        """One kubectl call whose three results are returned whole: (exit status, stdout, stderr). A failure to run it raises CloudError."""
        if not self.context:
            raise CloudError("CW_KUBE_CONTEXT is not set; refusing to run kubectl against an unnamed context", EXIT_USAGE)
        cmd = [self.bin] + (["--kubeconfig", self.kubeconfig] if self.kubeconfig else []) + ["--context", self.context, "-n", self.ns] + args
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=self.env)   # bytes: a carriage return is kept, never folded into a newline
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CloudError("kubectl failed: %s" % self.scrub(e))
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def get_workload(self, name):
        """The StatefulSet of this name, or None when the cluster says, in these exact words and on stderr alone, that THIS one is not there:
        `Error from server (NotFound): statefulsets.apps "NAME" not found` (one trailing newline allowed). The Forbidden answer for this object
        raises a CloudError that names the Role's missing rule. EVERY other answer (another name, kind, wording, extra or mixed output, a server
        error, an empty answer, an answer for another namespace) raises an `unreadable` CloudError: never None."""
        code, out, err = self._raw(["get", "statefulset", name, "-o", "json"])
        quoted = re.escape(name)
        if code != 0 and not out and re.fullmatch(r'Error from server \(NotFound\): statefulsets\.apps "%s" not found\n?' % quoted, err):
            return None
        if code != 0 and not out and re.fullmatch(r'Error from server \(Forbidden\): statefulsets\.apps "%s" is forbidden: [^\r\n]*in the namespace "%s"\n?' % (quoted, re.escape(self.ns)), err):
            raise WorkloadForbidden("the cluster login's Role lacks the rule for StatefulSets in the apps group (the owner's step: the administrator applies it); "
                                    "nothing was created or judged")
        if code == 0:
            try:
                obj = json.loads(out)
            except ValueError:
                obj = None
            if isinstance(obj, dict) and obj.get("kind") == "StatefulSet" and (obj.get("metadata") or {}).get("name") == name:
                return obj
        raise CloudError("workload %s unreadable: the cluster's answer is not the StatefulSet asked for (%s)" % (name, self.scrub(" ".join(err.split()) or "no message", 120)))

    def list_workloads(self):
        """The StatefulSets of the launcher's label in the namespace (a failure, a forbidden list included, raises CloudError)."""
        return json.loads(self.run(["get", "statefulsets", "-l", "app=" + LABEL, "-o", "json"]))["items"]

    def delete_workload_and_wait(self, name):
        """Delete the StatefulSet and return only when it is gone, within WORKLOAD_DELETE_SECONDS (its controller would recreate a pod deleted alone)."""
        try:
            self.run(["delete", "statefulset", name, "--ignore-not-found", "--wait=true", "--timeout=%ds" % WORKLOAD_DELETE_SECONDS], timeout=WORKLOAD_DELETE_SECONDS + 30)
        except CloudError as e:
            raise CloudError("the StatefulSet %s was not deleted within %d seconds: %s" % (name, WORKLOAD_DELETE_SECONDS, e)) from None

    def list_pods_all(self):
        """EVERY pod of the namespace (not only the worker's): the status summary counts them by phase. One call, one read."""
        return json.loads(self.run(["get", "pods", "-o", "json"]))["items"]

    def list_nodes(self):
        """The cluster's nodes: their pool label, their instance size label and their creation time. Needs `list` on nodes (cluster-wide): a
        login without that right is refused by the API server, and the caller falls back to the pod placement."""
        return json.loads(self.run(["get", "nodes", "-o", "json"]))["items"]

    def annotate(self, name, uid, **kv):
        """Annotate the pod that was READ (its `uid`), never one of the same name that replaced it: a merge patch whose body carries
        metadata.uid, which the API server refuses on a mismatch. Raises PodChanged when the pod is not that one (or is gone)."""
        if not uid:
            raise PodChanged("the pod carries no uid to bind to")
        body = json.dumps({"metadata": {"uid": uid, "annotations": {"cw/%s" % k.replace("_", "-"): str(v) for k, v in kv.items()}}})
        try:
            self.run(["patch", "pod", name, "--type=merge", "--patch-file=/dev/stdin"], stdin=body)
        except CloudError as e:
            if any(w in str(e) for w in ("Conflict", "NotFound", "precondition", "Precondition", "immutable", "Invalid")):
                raise PodChanged("the pod changed or is gone") from None
            raise

    def delete_pod(self, name, uid=None):
        """With a uid the delete is bound to that pod (DeleteOptions preconditions.uid): PodChanged when another pod holds the name now."""
        if not uid:
            return self.run(["delete", "pod", name, "--ignore-not-found", "--wait=false"])
        body = json.dumps({"kind": "DeleteOptions", "apiVersion": "v1", "preconditions": {"uid": uid}})
        try:
            self.run(["delete", "--raw", "/api/v1/namespaces/%s/pods/%s" % (self.ns, name), "-f", "-"], stdin=body)
        except CloudError as e:
            if "NotFound" in str(e):
                return None
            if any(w in str(e) for w in ("Conflict", "recondition")):
                raise PodChanged("the pod changed since it was read") from None
            raise

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

    def delete(self, name, uid=None):
        """Delete the pod and both Secrets, each attempted even if another fails; names what REMAINS and how to delete it.
        With a uid the pod delete is bound to the pod that was read; a pod that changed leaves all three whole (PodChanged)."""
        left = []
        if uid:
            self.delete_pod(name, uid)   # PodChanged here: nothing at all is deleted
            pod_fn = lambda n: None
        else:
            pod_fn = self.delete_pod
        for kind, obj, fn in (("pod", name, pod_fn), ("secret", name + "-pw", self.delete_secret),
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

    def registry_update(self, secret, change):
        """Read-modify-write of the hub's registry Secret (keys secrets.json and agents.json, each one JSON object): `change(secrets, agents)`
        edits both; the patch carries the resourceVersion that was read, so a concurrent write is a Conflict and the pass is repeated."""
        for _ in range(5):
            obj = self.get_secret(secret)
            if not obj:
                raise CloudError("the hub registry Secret %s is not found" % secret)
            data = obj.get("data") or {}
            try:
                maps = [json.loads(base64.b64decode(data[k]).decode()) if data.get(k) else {} for k in REGISTRY_KEYS]
                if not all(isinstance(m, dict) for m in maps):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                raise CloudError("the hub registry Secret %s is not two JSON objects" % secret) from None
            change(*maps)
            body = {"metadata": {"resourceVersion": (obj.get("metadata") or {}).get("resourceVersion")},
                    "data": {k: base64.b64encode(json.dumps(m, sort_keys=True).encode()).decode() for k, m in zip(REGISTRY_KEYS, maps)}}
            try:
                self.run(["patch", "secret", secret, "--type=merge", "--patch-file=/dev/stdin"], stdin=json.dumps(body))
                return
            except CloudError as e:
                if "Conflict" not in str(e) and "modified" not in str(e):
                    raise
        raise CloudError("the hub registry Secret %s kept changing; not written" % secret)

    def hub_pw_set(self, secret, key, value):
        """Set (or, with None, remove) ONE key of the hub's password Secret by a merge patch on stdin; the value is never in an argument."""
        body = {"data": {key: None if value is None else base64.b64encode(value.encode()).decode()}}
        if not self.get_secret(secret):
            raise CloudError("the hub password Secret %s is not found" % secret)
        self.run(["patch", "secret", secret, "--type=merge", "--patch-file=/dev/stdin"], stdin=json.dumps(body))

    def exec(self, name, *cmd, keep_stdout=False, timeout=120):
        return self.run(["exec", name, "--"] + list(cmd), keep_stdout=keep_stdout, timeout=timeout)


def _pod_env(pod, key):
    try:
        for c in pod["spec"]["containers"]:
            for e in c.get("env") or []:
                if e.get("name") == key and isinstance(e.get("value"), str):
                    return e["value"][:200]
    except (KeyError, TypeError, AttributeError):
        pass
    return ""


def _pod_repo(pod):
    return _pod_env(pod, "CW_REPO")


def ann(pod, key, default=None):
    return pod["metadata"].get("annotations", {}).get("cw/" + key, default)


SAFE_VALUE = re.compile(r"[A-Za-z0-9._-]{1,63}", re.ASCII)


def safe_value(value):
    """A value the cluster supplies (a node's pool or size label, a pod phase) that is printed to a person: ONE rule. It is kept whole when it is
    1 to 63 ASCII letters, digits, `-`, `_` or `.` (what a Kubernetes label value or a phase may hold); anything else (an escape, a bell, a newline,
    another control or non-ASCII character, an over-long value) becomes the fixed token `?`. Nothing is stripped: a partly clean value is not trusted."""
    return value if isinstance(value, str) and SAFE_VALUE.fullmatch(value) else "?"


def _node_label(labels, keys):
    """The first of `keys` the node carries, or "unknown" (never a guess, never a value of another label). The value is `safe_value`."""
    for key in keys:
        value = (labels or {}).get(key)
        if isinstance(value, str) and value:
            return safe_value(value)
    return "unknown"


RUN_SECRET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RESERVED_RUN_NAMES = {"server-password"}
STACK_MODEL_KEY = "stack-model-key"   # the per-run Secret key of the stack-only model credential (a stack launch only); never the coder's own key
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


def pod_deadline_floor(max_hard):
    """The pod's own activeDeadlineSeconds: the cap (a duration text, never empty) plus the grace; always finite."""
    return parse_duration(max_hard or DEFAULT_MAX_HARD) + POD_DEADLINE_GRACE_SECONDS


def check_shape(a):
    """Set a.shape when it names none (XS, or S for a stack launch) and refuse an unknown name, or a row that cannot run a stack, with --stack."""
    if getattr(a, "shape", None) is None:
        a.shape = DEFAULT_STACK_SHAPE if getattr(a, "stack", False) else DEFAULT_ORCHESTRATOR_SHAPE if getattr(a, "agent", None) == "orchestrator" else DEFAULT_SHAPE
    if a.shape not in SHAPES:
        raise CloudError("shape must be one of %s" % ", ".join(SHAPES), EXIT_USAGE)
    if getattr(a, "stack", False) and not SHAPES[a.shape]["stack"]:
        raise CloudError("--stack needs a shape that can run a nested Docker stack (%s), not %s" % (", ".join(k for k, v in SHAPES.items() if v["stack"]), a.shape), EXIT_USAGE)


def hub_on(env):
    """The hub setting: the channel address or the registry Secret name is set. Unset, a worker launch is exactly what it was."""
    return bool((env.get("CW_HUB_CHANNEL_URL") or "").strip() or (env.get("CW_HUB_REGISTRY_SECRET") or "").strip())


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
    agent = getattr(a, "agent", None) or "worker"
    if agent not in AGENTS:
        raise CloudError("--agent must be one of %s, got %r; nothing was created" % (" or ".join(AGENTS), str(agent)[:40]), EXIT_USAGE)
    if agent == "orchestrator" or hub_on(env):   # with the hub setting unset a worker pod gets none of these: its spec is exactly what it was
        boss = getattr(a, "boss", None)
        if (agent == "orchestrator" and not boss) or (boss and not NAME_RE.match(boss)):
            raise CloudError("--agent orchestrator needs --boss NAME", EXIT_USAGE)
        if CHANNEL_SECRET_KEY not in run_names:
            raise CloudError("a hub pod needs its channel secret", EXIT_USAGE)
        e.update({"CW_AGENT": agent, "FLEET_AGENT_NAME": worker, **({"FLEET_BOSS": boss} if boss else {}),
                  "FLEET_CLOUD_CHANNEL_URL": env.get("CW_HUB_CHANNEL_URL") or DEFAULT_CHANNEL_URL, "FLEET_CLOUD_TOKEN_FILE": CHANNEL_TOKEN_FILE})
    pw_volume = {"name": "pw", "secret": {"secretName": name + "-pw", "defaultMode": 0o600,
                                          "items": [{"key": "password", "path": "server-password"}]}}
    if run_names:  # one read-only mount at /run/cw holding the password file and each run secret (0400)
        pw_volume = {"name": "pw", "projected": {"defaultMode": 0o600, "sources": [
            {"secret": {"name": name + "-pw", "items": [{"key": "password", "path": "server-password", "mode": 0o600}]}},
            {"secret": {"name": name + "-run", "items": [{"key": k, "path": k, "mode": 0o400} for k in run_names]}}]}}
    puller = None
    if stack:
        set_keys = [k for k in STACK_REGISTRY_KEYS if (env.get(k) or "").strip()]
        if set_keys:   # all three or none, each a valid value, BEFORE any object is made; a refusal names keys, never a value
            if len(set_keys) != len(STACK_REGISTRY_KEYS):
                raise CloudError("the stack registry settings are all three or none: set %s too; nothing was created" % ", ".join(k for k in STACK_REGISTRY_KEYS if k not in set_keys), EXIT_USAGE)
            for k in STACK_REGISTRY_KEYS:
                if not VALIDATORS[k][1](env[k].strip()):
                    raise CloudError("%s must be %s; nothing was created" % (k, VALIDATORS[k][0]), EXIT_USAGE)
            puller = {k: env[k].strip() for k in STACK_REGISTRY_KEYS}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {
        "name": name, "labels": {"app": LABEL, "cw/worker": worker, "azure.workload.identity/use": "true"},
        "annotations": {"cw/worker": worker, "cw/started": iso(now), "cw/hard-deadline": iso(hard),
                        "cw/idle-seconds": e["CW_IDLE_SECONDS"], "cw/shape": a.shape,
                        "cw/max-hard": iso(now + dt.timedelta(seconds=parse_duration(a.max_hard or DEFAULT_MAX_HARD))),
                        **({"cw/run-id": a.run_id} if getattr(a, "run_id", None) else {})}},
        "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": pod_deadline_floor(a.max_hard),
                 "terminationGracePeriodSeconds": POD_TERMINATION_GRACE_SECONDS,
                 "serviceAccountName": env.get("CW_CLOUD_SERVICE_ACCOUNT", "cw-worker"),
                  "automountServiceAccountToken": False,
                  "securityContext": copy.deepcopy(POD_SECURITY_CONTEXT),
                 "containers": [{"name": "worker", "image": image,
                                 "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}, "env": [{"name": k, "value": v} for k, v in e.items()],
                                 "resources": {"limits": {"cpu": shape["cpu"], "memory": shape["memory"]}},
                                 "readinessProbe": {"tcpSocket": {"port": 4096}},
                                 "lifecycle": copy.deepcopy(PRESTOP_LIFECYCLE),
                                 "volumeMounts": [{"name": "pw", "mountPath": "/run/cw", "readOnly": True},
                                                  {"name": "tokens", "mountPath": "/run/cw-tmp"},
                                                  {"name": "scratch", "mountPath": "/work"}]}],
                 "volumes": [pw_volume,
                             {"name": "tokens", "emptyDir": {"medium": "Memory"}},
                             {"name": "scratch", "emptyDir": {}}]}}
    if "CW_AGENT" in e:   # a hub pod: a stable in-cluster name (the headless Service cw-agents) instead of the pod address
        pod["spec"]["hostname"], pod["spec"]["subdomain"] = name, HUB_SUBDOMAIN
    pull = getattr(a, "pull_secret", None)
    if pull:
        pod["spec"]["imagePullSecrets"] = [{"name": pull}]
    if stack:
        pod["metadata"]["annotations"]["cw/stack"] = "true"
        pod["spec"]["runtimeClassName"] = STACK_RUNTIME_CLASS
        pod["spec"]["hostUsers"] = False
        pod["spec"]["nodeSelector"] = dict(STACK_NODE_SELECTOR)
        pod["spec"]["containers"][0]["securityContext"] = copy.deepcopy(STACK_SECURITY_CONTEXT)
        if puller:   # the registry puller: its own service account, no workload-identity label (the webhook would mount a token into every container), one pinned volume
            pod["spec"]["serviceAccountName"] = PULLER_SERVICE_ACCOUNT
            del pod["metadata"]["labels"][PULLER_LABEL]
            pod["metadata"]["annotations"].update({PULLER_ANNOTATIONS["registry-host"]: puller["CW_STACK_REGISTRY"],
                                                   PULLER_ANNOTATIONS["client-id"]: puller["CW_STACK_PULL_CLIENT_ID"],
                                                   PULLER_ANNOTATIONS["tenant-id"]: puller["CW_STACK_PULL_TENANT_ID"]})
            pod["spec"]["volumes"].append(copy.deepcopy(PULLER_VOLUME))
            pod["spec"]["containers"][0]["volumeMounts"].append({"name": PULLER_VOLUME_NAME, "mountPath": PULLER_MOUNT_PATH, "readOnly": True})
    bad = forbidden_pod_fields(pod)
    if bad:
        raise CloudError("refusing a pod spec with a forbidden field: %s" % ", ".join(bad), EXIT_USAGE)
    return pod


def render_workload(a, worker, name, now, env, run_names=()):
    """The persistent orchestrator: ONE StatefulSet (apps/v1, one replica, the headless agent service) around render_pod's own pod: restart
    Always, no active deadline, no hard-deadline, idle or max-hard annotation (and no deadline names in the environment), the annotation cw/persistent.
    The guards run on the WHOLE StatefulSet spec. Its pod is NAME-0 (the controller names it and sets its hostname and subdomain)."""
    pod = render_pod(a, worker, name, now, now, env, run_names)
    meta, spec = pod["metadata"], pod["spec"]
    spec["restartPolicy"] = "Always"
    for key in ("activeDeadlineSeconds", "hostname", "subdomain"):
        spec.pop(key, None)
    for container in spec["containers"]:
        container["env"] = [e for e in container["env"] if e["name"] not in ("CW_IDLE_SECONDS", "CW_HARD_DEADLINE")]
    annotations = {k: v for k, v in meta["annotations"].items() if k not in ("cw/hard-deadline", "cw/idle-seconds", "cw/max-hard")}
    annotations[PERSISTENT_ANNOTATION] = "true"
    labels = dict(meta["labels"])
    selector = {"app": LABEL, "cw/worker": worker}
    top = {"app": LABEL, "cw/worker": worker, **({"cw/run-id": a.run_id} if getattr(a, "run_id", None) else {})}
    workload = {"apiVersion": "apps/v1", "kind": "StatefulSet",
                "metadata": {"name": name, "labels": top, "annotations": dict(annotations)},
                "spec": {"replicas": 1, "serviceName": HUB_SUBDOMAIN, "selector": {"matchLabels": selector},
                         "template": {"metadata": {"labels": labels, "annotations": annotations}, "spec": spec}}}
    bad = workload_problems(workload)
    if bad:
        raise CloudError("refusing a pod spec with a forbidden field: %s" % ", ".join(bad), EXIT_USAGE)
    return workload


def _workload_views(workload):
    template = workload["spec"]["template"]
    return [{"metadata": template["metadata"], "spec": template["spec"]}, {"spec": workload["spec"]}]


def workload_problems(workload):
    """The forbidden-field names found anywhere in the whole StatefulSet spec: every row on the template as a pod, and the rows that look at
    text (docker.sock, hostPath) on the whole spec too (a volume claim or any other key of the StatefulSet could carry them)."""
    found = set(forbidden_pod_fields(_workload_views(workload)[0]))
    whole = {"spec": workload["spec"]}
    if FORBIDDEN_POD_FIELDS["docker.sock"](whole):
        found.add("docker.sock")
    if '"hostPath"' in json.dumps(workload["spec"]):
        found.add("hostPath")
    return sorted(found)


def scan_workload(workload):
    """Stored-credential patterns found in the whole StatefulSet (empty means clean)."""
    return sorted({hit for view in _workload_views(workload) + [workload] for hit in scan_credentials(view)})


class Launcher:
    def __init__(self, kube, env, clock=None, sleep=time.sleep):
        self.kube, self.env, self.sleep = kube, env, sleep
        self.run_id, self.foreign = None, []
        self.recorder = run_record.Recorder(kube, env)
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.audit_path = audit_path(env)

    # -- audit: one line per action, no secret, written before the action returns --
    def audit(self, worker, runtime_id, action, cost, ok, reason="", extra=None):
        write_audit(self.audit_path, dict({"ts": iso(self.clock()), "worker": worker, "runtime_id": runtime_id, "action": action,
                                           "cost_estimate_usd": cost, "ok": ok, "actor": pwd.getpwuid(os.getuid()).pw_name,
                                           "reason": self.kube.scrub(reason, limit=1000)}, **(extra or {})))

    def cost(self, pod):
        hours = (self.clock() - parse_iso(ann(pod, "started"))).total_seconds() / 3600
        return round(max(hours, 0) * SHAPES.get(ann(pod, "shape", "S"), SHAPES["S"])["usd_per_hour"], 4)

    def _audited(self, worker, action, fn, reason="", runtime_id=""):
        info = {"rid": runtime_id, "cost": 0.0}

        def why():   # a stop that wrote a run record names its directory once, here
            return "%s; run record %s" % (reason, info["record"]) if info.get("record") else reason

        def extra():   # a persistent orchestrator's line says so (no other line has the key)
            return {"persistent": True} if info.get("persistent") else None
        try:
            result = fn(info)
        except CloudError as e:
            if not e.audited:
                # the failure is never dropped: a caller's own reason (a stop reason, the run-secret names) comes first
                self.audit(worker, info["rid"], action, info["cost"], False, "%s; %s" % (why(), e) if why() else str(e), extra())
            raise
        except (ValueError, TypeError, KeyError) as e:  # a bad annotation or reply still writes its ok:false line
            self.audit(worker, info["rid"], action, info["cost"], False, why() or str(e)[:300], extra())
            raise CloudError("unexpected cloud data: %s" % self.kube.scrub(e, 200))
        self.audit(worker, info["rid"], action, info["cost"], True, why(), extra())
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

    def _persistent_refusals(self, a, agent, run_values):
        """--persistent is for an orchestrator launched with the per-run git-token Secret, never a worker or a stack launch: each refusal names
        itself, exits 2 and happens before any cluster call."""
        if agent != "orchestrator":
            raise CloudError("--persistent is only for --agent orchestrator, not a worker; nothing was created", EXIT_USAGE)
        if getattr(a, "stack", False):
            raise CloudError("--persistent does not combine with --stack (a stack launch is a one-shot pod); nothing was created", EXIT_USAGE)
        if GIT_TOKEN_KEY not in run_values:
            raise CloudError("--persistent needs the per-run token route: pass --run-secret-fd %s=N (the reaper renews that Secret); nothing was created" % GIT_TOKEN_KEY, EXIT_USAGE)

    def check_persistent_count(self):
        """A persistent create that would make the namespace's StatefulSets of the launcher's label exceed the settings maximum, or whose count
        cannot be read, is refused (exit 2) and nothing is created."""
        limit = max_persistent(self.env)
        try:
            count = len(self.kube.list_workloads())
        except (CloudError, ValueError, KeyError, TypeError) as e:
            raise CloudError("could not count the persistent orchestrators of the namespace (%s); nothing was created" % self.kube.scrub(e, 150), EXIT_USAGE)
        if count + 1 > limit:
            raise CloudError("the namespace already has %d persistent orchestrator(s); the maximum is %d (%s); nothing was created" % (count, limit, PERSISTENT_SETTING), EXIT_USAGE)

    # -- create --
    def create(self, a):
        worker, name = a.worker, pod_name(a.worker)
        self.run_id, self.foreign = getattr(a, "run_id", None), []   # what this call proved is another's is never deleted
        made = {"secret": False, "run": False, "pod": False, "reg": None, "sts": False}  # set before the call that may create it: an interrupt may land after
        persistent = bool(getattr(a, "persistent", False))

        def go(info):
            info["rid"] = "pod/" + name + ("-0" if persistent else "")
            if persistent:
                info["persistent"] = True
            check_repo_allowed(a.repo, self.env)  # before anything is read or made
            if not NAME_RE.match(worker):
                raise CloudError("bad worker name %r" % worker, EXIT_USAGE)
            check_shape(a)   # resolves an unnamed shape; before any cluster, vault or token call
            now = self.clock()
            hard = now + dt.timedelta(seconds=parse_duration(a.hard))
            max_hard = now + dt.timedelta(seconds=parse_duration(a.max_hard))
            if hard > max_hard and not persistent:
                raise CloudError("--hard is past --max-hard", EXIT_REFUSED)
            run_values = read_run_secret_values(parse_run_secret_fds(a.run_secret_fd))  # before anything is made
            stack_secret = getattr(a, "stack_model_secret", None)
            if stack_secret is not None:   # the name only; the value came over the pipe. Both or neither, and only for a stack pod
                if not getattr(a, "stack", False) or not _VAULT_SECRET_NAME.fullmatch(stack_secret) or STACK_MODEL_KEY not in run_values:
                    raise CloudError("--stack-model-secret needs --stack, a vault secret name and the %s run secret; nothing was created" % STACK_MODEL_KEY, EXIT_USAGE)
            elif STACK_MODEL_KEY in run_values:
                raise CloudError("the %s run secret needs --stack-model-secret NAME; nothing was created" % STACK_MODEL_KEY, EXIT_USAGE)
            agent = getattr(a, "agent", None) or "worker"
            if agent not in AGENTS:   # before any cluster call
                raise CloudError("--agent must be one of %s, got %r; nothing was created" % (" or ".join(AGENTS), str(agent)[:40]), EXIT_USAGE)
            if persistent:
                self._persistent_refusals(a, agent, run_values)
                max_persistent(self.env)   # a bad setting is refused before any cluster call
            hub = agent == "orchestrator" or hub_on(self.env)
            if hub:   # the per-pod channel secret: random, only its sha256 leaves the pod's Secret, never printed
                if CHANNEL_SECRET_KEY in run_values:
                    raise CloudError("%s is the launcher's own key" % CHANNEL_SECRET_KEY, EXIT_USAGE)
                run_values[CHANNEL_SECRET_KEY] = secrets.token_hex(32)
            self.kube.add_secrets(*run_values.values())  # known to scrub before the first kubectl call
            self.check_cap(a.shape, PERSISTENT_DAY_SECONDS if persistent else int((hard - now).total_seconds()))
            if persistent:
                self.check_persistent_count()
                spec = render_workload(a, worker, name, now, self.env, sorted(run_values))
                hits = scan_workload(spec)
            else:
                spec = render_pod(a, worker, name, now, hard, self.env, sorted(run_values))
                hits = scan_credentials(spec)
            if hits:
                raise CloudError("refusing a pod spec with a stored credential: %s" % ", ".join(hits), EXIT_USAGE)
            if self.kube.get_pod(name) or (persistent and (self.kube.get_workload(name) or self.kube.get_pod(name + "-0"))):
                raise CloudError("worker %s already exists" % worker, EXIT_EXISTS)
            password = secrets.token_urlsafe(32)
            self.kube.add_secrets(password)
            # The check above is not atomic with the creates below; each create is (AlreadyExists fails exactly one of two callers).
            try:
                self._make(made, "secret", self.kube.apply, {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name + "-pw", "labels": dict({"app": LABEL}, **({"cw/run-id": a.run_id} if getattr(a, "run_id", None) else {}))},
                                 "type": "Opaque", "stringData": {"password": password}})
                if run_values:
                    self._make(made, "run", self.kube.create_run_secret, name, name, run_values, *([a.run_id] if getattr(a, "run_id", None) else []))
                if hub:
                    made["reg"] = (self._registry_secret(), worker)   # set before the call: a half write is undone by the cleanup
                    self._register(worker, run_values[CHANNEL_SECRET_KEY], spec if not persistent else {"metadata": {"name": name + "-0"}},
                                   self.env.get("CW_CLOUD_DOMAIN", "example.internal"), password, persistent)
                self._make(made, "sts" if persistent else "pod", self.kube.apply, spec)
                if getattr(a, "stack", False):
                    self._start_stack_pod(worker, name, spec, made, a.wait)
                else:
                    self._wait_ready(name + "-0" if persistent else name, a.wait)
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
            if persistent:   # no deadline of either kind: the answer says so (never the defaults), and carries the field the end alert reads
                return {"worker": worker, "url": "https://worker-%s-0.%s:4096" % (name, domain),
                        "auth_ref": "file:/run/cw/%s.password" % worker, "runtime_id": "pod/" + name + "-0",
                        "idle_deadline": None, "hard_deadline": None, "persistent": True,
                        "password_fd": a.password_fd}
            return {"worker": worker, "url": "https://worker-%s.%s:4096" % (name, domain),
                    "auth_ref": "file:/run/cw/%s.password" % worker, "runtime_id": "pod/" + name,
                    "idle_deadline": iso(now + dt.timedelta(seconds=parse_duration(a.idle))), "hard_deadline": iso(hard),
                    "password_fd": a.password_fd}
        old = self._catch_signals()
        try:
            reason = ("run-secrets=" + ",".join(sorted(parse_run_secret_fds(a.run_secret_fd)))) if a.run_secret_fd else ""
            if getattr(a, "stack_model_secret", None) and _VAULT_SECRET_NAME.fullmatch(a.stack_model_secret):   # the name and that it was delivered, nothing else
                reason = (reason + " " if reason else "") + "stack-model-secret=%s delivered" % a.stack_model_secret
            return self._audited(worker, "create", go, reason)
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

    def _registry_secret(self):
        return self.env.get("CW_HUB_REGISTRY_SECRET") or DEFAULT_REGISTRY_SECRET

    def _register(self, agent, channel_secret, spec, domain, password, persistent=False):
        """The hub's registry: {agent: sha256 hex of the channel secret} and {agent: {url, session, password_file}}. Only the hash is stored."""
        digest = hashlib.sha256(channel_secret.encode()).hexdigest()
        name = spec["metadata"]["name"]
        entry = {"url": hub_agent_url(name, self.env.get("CW_CLOUD_NAMESPACE", "cw")), "session": "", "password_file": "%s/%s" % (HUB_PW_DIR, agent)}
        if persistent:   # only a persistent orchestrator's entry has the key
            entry["persistent"] = True

        def change(sec, agents):
            sec[agent], agents[agent] = digest, entry
        self.kube.registry_update(self._registry_secret(), change)
        self.kube.hub_pw_set(HUB_PW_SECRET, agent, password)   # the pod's server password: the hub reads it from the file

    def hub_session(self, a):
        """Write the session id into the agent's hub entry (a no-op when it has none). Called by the switchboard once the session exists."""
        if not NAME_RE.match(a.worker or "") or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", a.session or ""):
            raise CloudError("bad worker name or session id", EXIT_USAGE)
        found = []

        def change(sec, agents):
            if a.worker in agents:
                agents[a.worker]["session"] = a.session
                found.append(1)
        self.kube.registry_update(self._registry_secret(), change)
        return {"worker": a.worker, "registered": bool(found)}

    def _unregister(self, agent):
        """Remove both entries of the agent (a no-op when absent). A failure is said on stderr and never raised: the stop goes on."""
        def change(sec, agents):
            sec.pop(agent, None)
            agents.pop(agent, None)
        try:
            self.kube.registry_update(self._registry_secret(), change)
            self.kube.hub_pw_set(HUB_PW_SECRET, agent, None)
            return True
        except CloudError as e:
            print("cloud-worker: the hub registry entry of %s could not be removed: %s" % (agent, e), file=sys.stderr)
            return False

    def _mine(self, kind, name):
        """True: carries this call's run id; False: another's (leave whole); None: not there; raises CloudError: unreadable."""
        if not self.run_id:
            return True   # a caller that sent no --run-id gets the plain behaviour
        if kind == "pod":
            obj, where = self.kube.get_pod(name), "annotations"
        elif kind == "sts":
            obj, where = self.kube.get_workload(name), "annotations"
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
        if made.get("reg"):
            self._unregister(made["reg"][1])
        try:
            if made.get("sts"):   # the workload FIRST and waited for (its controller would recreate a pod deleted alone), then its pod
                try:
                    mine = self._mine("sts", name)
                    if mine is False:
                        self.foreign.append("statefulset/" + name)
                        print("cloud-worker: statefulset/%s is not this call's (its run id does not match); left whole" % name, file=sys.stderr)
                    elif mine:
                        self.kube.delete_workload_and_wait(name)
                        self.kube.delete_pod(name + "-0")
                except CloudError:
                    left.append("statefulset/" + name)
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
        cleaned = [l for k, l in (("sts", "statefulset/" + name), ("pod", "pod/" + name), ("secret", "secret/%s-pw" % name), ("run", "secret/%s-run" % name)) if made.get(k) and l not in left and l not in self.foreign]
        reason = "%s; cleaned: %s" % ("interrupted" if interrupted else "failed: %s" % str(exc)[:200], ", ".join(cleaned) or "nothing")
        if left:
            reason += "; REMAINS: %s; delete each with: %s" % (", ".join(left), "; ".join(self._delete_cmd(l) for l in left))
        note = self._run_secret_note(name, made, left)
        if note:
            reason += "; " + note
        self.audit(worker, "pod/" + name + ("-0" if made.get("sts") else ""), "create.interrupted" if interrupted else "create.failed", 0.0, False, reason,
                   {"persistent": True} if made.get("sts") else None)
        err = CloudError("create %s" % reason, EXIT_INTERRUPTED if interrupted else EXIT_CLOUD)
        err.audited = True
        raise err from None

    def _start_stack_pod(self, worker, name, spec, made, seconds):
        """Wait for a stack pod; on the fresh-node race (the runtime handler is not yet known to the node's containerd) delete
        THE POD ONLY and create it again with the same run id and nonce, after a pause of STACK_RACE_PAUSE_SECONDS, for as long
        as one more pause plus one poll fits in STACK_RACE_WINDOW_SECONDS (no attempt count). The password Secret and the per-run Secret are made once, before the first attempt, and are
        never touched here: the pod refers to them by name, so a second attempt reads the same objects. The same bounded retry also
        recreates a pod that VANISHES while pending (it was seen, then a get says not found); a pod never seen, and any other
        failure, are not retried."""
        began = self.clock()
        deadline = began + dt.timedelta(seconds=STACK_RACE_WINDOW_SECONDS)
        self._slept = 0.0   # the seconds this launcher itself slept: a second bound, so a clock that does not advance still ends the loop
        attempt = 1
        while True:
            seen = []   # this attempt's pod: set once a get returned it

            def watch(pod, seen=seen):
                if pod is None and seen:
                    raise _PodVanished("pod vanished")
                if pod is not None:
                    seen.append(1)
                    self._raise_on_race(name, pod)
            try:
                # a retry never waits past the window (the first attempt keeps the caller's --wait, as before)
                self._wait_ready(name, seconds if attempt == 1 else max(0, min(seconds, int((deadline - self.clock()).total_seconds()), int(STACK_RACE_WINDOW_SECONDS - self._slept))), watch)
            except (_FreshNodeRace, _PodVanished) as race:
                last = not self._retry_fits(deadline)
                spent = int((self.clock() - began).total_seconds())
                self.audit(worker, "pod/" + name, "create-attempt", 0.0, False,
                           "attempt %d: %s%s" % (attempt, race, "; giving up" if last else "; pausing %d s, then recreating the pod" % STACK_RACE_PAUSE_SECONDS))
                if last:
                    if isinstance(race, _PodVanished):
                        raise CloudError("the worker pod could not start: the pod vanished while pending (the fresh-node race: the node's "
                                         "runtime restarted); gave up after %d attempt(s) in %d s" % (attempt, spent)) from None
                    raise CloudError("the worker pod could not start: the node's runtime handler %s was not ready (the fresh-node race: %s); "
                                     "gave up after %d attempt(s) in %d s" % (STACK_RUNTIME_CLASS, race, attempt, spent)) from None
                if self._mine("pod", name) is False:
                    self.foreign.append("pod/" + name)
                    raise CloudError("the pod %s is not this call's (its run id does not match); left whole" % name) from None
                self._refuse_if_removed(name, made)   # a stop, the reaper or someone else removed this call's own objects: not the race
                self._nap(STACK_RACE_PAUSE_SECONDS)   # the pod is deleted and made again only after the pause, never in a tight loop
                self.kube.delete_pod_and_wait(name)
                attempt += 1
                self._make(made, "pod", self.kube.apply, spec)
                self._refuse_if_removed(name, made)   # a stop that deleted the pod, then its Secrets, may have landed between the check and here
                continue
            except CloudError as e:
                self.audit(worker, "pod/" + name, "create-attempt", 0.0, False, "attempt %d: %s" % (attempt, e))
                raise
            self._refuse_if_removed(name, made)   # the stop's Secret deletes may have come after the pod went: never report a pod whose Secrets are gone
            self.audit(worker, "pod/" + name, "create-attempt", 0.0, True, "attempt %d: ready" % attempt)
            return

    def _retry_fits(self, deadline):
        """True when one more pause plus one poll fits in the window that is left."""
        need = STACK_RACE_PAUSE_SECONDS + WAIT_POLL_SECONDS
        return (deadline - self.clock()).total_seconds() >= need and STACK_RACE_WINDOW_SECONDS - self._slept >= need

    def _nap(self, seconds):
        self.sleep(seconds)
        self._slept = getattr(self, "_slept", 0.0) + seconds

    def _refuse_if_removed(self, name, made):
        gone = self._own_objects_gone(name, made)
        if gone:
            raise CloudError("the worker pod was removed from outside (%s no longer exist(s) or are not this call's): not recreated, and not reported ready" % ", ".join(gone)) from None

    def _own_objects_gone(self, name, made):
        """The Secrets this call made that are no longer this call's in the cluster: missing, or (with a run id) carrying another's."""
        gone = []   # a get that errors (not "not found") raises CloudError: the create fails, it never reads as "still there"
        for kind, label, get in (("secret", "secret/%s-pw" % name, lambda: self.kube.get_secret(name + "-pw")),
                                 ("run", "secret/%s-run" % name, lambda: self.kube.get_run_secret(name))):
            if not made[kind]:
                continue
            obj = get()
            if not obj or (self.run_id and ((obj.get("metadata") or {}).get("labels") or {}).get("cw/run-id") != self.run_id):
                gone.append(label)
        return gone

    def _raise_on_race(self, name, pod):
        uid = (pod or {}).get("metadata", {}).get("uid")
        for ev in self.kube.pod_events(name):
            if uid and (ev.get("involvedObject") or {}).get("uid") not in (None, uid):
                continue   # an event of an earlier pod of the same name
            if ev.get("reason") == "FailedCreatePodSandBox" and STACK_RACE_RE.search(ev.get("message") or ""):
                raise _FreshNodeRace(STACK_RACE_RE.search(ev["message"]).group(0))

    def _wait_ready(self, name, seconds, on_pending=None):
        end = self.clock() + dt.timedelta(seconds=seconds)
        waited = 0.0   # the second bound: the seconds slept here, for a clock that does not advance
        while True:
            pod = self.kube.get_pod(name)
            status = (pod or {}).get("status", {})
            if status.get("phase") == "Failed":
                raise CloudError("worker pod failed to start")
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])):
                return
            if on_pending:
                on_pending(pod)
            if self.clock() >= end or waited >= seconds:
                raise CloudError("timed out waiting for the worker", EXIT_TIMEOUT)
            self._nap(WAIT_POLL_SECONDS)
            waited += WAIT_POLL_SECONDS

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
            persistent = False
            workload = None
            if pod is None and self._persistent_evidence(worker, getattr(a, "record_dir", None)):
                # EVIDENCE RULE: only a worker with evidence of persistence has its workload read; the strict read's every answer but the exact
                # not-found (the exact Forbidden line included) fails the stop with everything kept; a plain worker makes no workload read at all
                try:
                    workload = self.kube.get_workload(name)
                except CloudError as e:
                    raise CloudError("the persistent orchestrator %s has its workload unreadable; nothing was stopped or deleted: %s" % (worker, e), EXIT_HOOK) from None
                if workload is not None:
                    persistent = True
                    pod = self.kube.get_pod(name + "-0")
            self._persistent_stop = name if persistent else None
            target = name + "-0" if persistent else name   # the pod the hook, the record and the marks act on
            if persistent:
                info["persistent"] = True
            uid = ((pod or {}).get("metadata") or {}).get("uid") if not persistent else None   # a persistent pod is never deleted alone: no pod-bound delete
            if persistent:
                template = (((workload.get("spec") or {}).get("template")) or {})
                self._orchestrator = _pod_env({"spec": template.get("spec") or {}}, "FLEET_AGENT_NAME") or None
            else:
                self._orchestrator = _pod_env(pod, "FLEET_AGENT_NAME") if pod and _pod_env(pod, "FLEET_CLOUD_TOKEN_FILE") else None   # a hub pod, orchestrator or worker
            if nonce and (pod or persistent):
                info["rid"] = "pod/" + target
                have = ann(pod, "run-id") if pod else ((workload.get("metadata") or {}).get("annotations") or {}).get("cw/run-id")
                if have != nonce:   # another run's (or an unlabelled) worker of this name: nothing is changed
                    raise CloudError("worker %s does not match run id %s; nothing was stopped or deleted" % (worker, nonce), EXIT_REFUSED)
            if not pod and not persistent:
                if nonce:   # no pod: this run's own Secrets (carrying the nonce) may remain from a cut-off create; another's are left whole
                    self._remove(name, nonce)
                # The pod read said NotFound (an operator deleted it); any other read error raised in _need above and never reaches here.
                removed = []
                if not nonce:   # by name, pod NotFound: only this worker's own two Secrets (exact names) may be left over; nothing else is touched
                    for full, fn in ((name + "-pw", self.kube.delete_secret), (name + "-run", self.kube.delete_run_secret)):
                        if self.kube.get_secret(full):
                            fn(name)
                            removed.append(full)
                return {"worker": worker, "runtime_id": "", "pushed": False, "wip_branch": None, "pod_gone": True, "secrets_removed": removed}
            info["rid"] = "pod/" + target
            info["cost"] = self.cost(pod if pod else {"metadata": {"annotations": ((workload.get("spec") or {}).get("template") or {}).get("metadata", {}).get("annotations") or {}}})
            if not getattr(a, "no_record", False):   # BEFORE the hook and the delete; never raises, never holds the stop back
                where = self.recorder.record(worker, target, reason or "stop", repo=_pod_repo(pod) if pod else "", started=ann(pod, "started") or "" if pod else "",
                                             root=getattr(a, "record_dir", None), **({"persistent": True} if persistent else {}))
                if where:
                    info["record"] = where
                    print("cloud-worker: run record: %s" % where, file=sys.stderr)
            phase = (pod.get("status") or {}).get("phase") if pod else "Absent"
            if persistent and not pod:   # its controller has no pod now: nothing runs, no hook can run; the workload goes first
                self._remove(name, nonce, uid)
                print("cloud-worker: the persistent pod was absent; end hook not run; the StatefulSet was deleted", file=sys.stderr)
                self._hook_record(info, {"ran": False, "pushed": False, "wip_branch": None, "skipped": [], "note": "pod was absent; end hook not run"})
                return {"worker": worker, "runtime_id": "pod/" + target, "pushed": False, "wip_branch": None,
                        "skipped": [], "note": "pod was absent; end hook not run", "record": info.get("record", "")}
            if phase in ("Failed", "Succeeded") and ann(pod, "kept") != "true":  # a pod kept for unpushed work is never deleted here
                # No container is running, so no hook can run and a kept pod would only linger: delete it and its
                # Secrets, and say so.
                self._remove(name, nonce, uid)
                print("cloud-worker: pod was %s; end hook not run; deleted" % phase, file=sys.stderr)
                self._hook_record(info, {"ran": False, "pushed": False, "wip_branch": None, "skipped": [], "note": "pod was %s; end hook not run" % phase})
                return {"worker": worker, "runtime_id": "pod/" + target, "pushed": False, "wip_branch": None,
                        "skipped": [], "note": "pod was %s; end hook not run" % phase, "record": info.get("record", "")}
            try:
                res = self.run_hook(target)
            except CloudError as e:
                res = {"pushed": False, "wip_branch": None, "error": str(e)}
            view = self._hook_view(res)   # every field text from the pod: a string, scrubbed, short
            self.last_hook = {k: view[k] for k in ("pushed", "wip_branch", "error")}
            self._hook_record(info, dict(view, ran=True))
            if view["error"]:
                note = self._mark(target, pod["metadata"].get("uid"), kept="true")  # the pod is kept (and its workload): the commit is not safe yet
                raise CloudError("end hook could not push, pod kept: %s%s; %s" % (view["error"], "; " + note if note else "", RECOVERY_ROUTE), EXIT_HOOK)
            note = ""
            try:
                note = self._mark(target, pod["metadata"].get("uid"), hook_ran="true")
            finally:
                self._remove(name, nonce, uid)  # the hook succeeded: whatever happens next, the pod and its Secrets go (that pod only)
            if note:
                raise CloudError(note, EXIT_REFUSED)
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
                    "wip_branch": view["wip_branch"], "skipped": skipped, "record": info.get("record", "")}
        return self._audited(worker, "stop", go, audit_reason)

    def _persistent_workload(self, name):
        """The StatefulSet of a worker whose own pod is not there (the strict read), or None. The Role's missing rule (a Forbidden answer) means this
        launcher cannot have made one: None. Any other unreadable answer raises: a stop never reads an unknown answer as no workload."""
        try:
            return self.kube.get_workload(name)
        except WorkloadForbidden:
            return None

    def _persistent_evidence(self, worker, record_dir=None):
        """True when at least one of THREE sources says the worker is a persistent orchestrator: its audit create line, its hub registry entry,
        its run record (each carries persistent true). A missing source is no evidence; an unreadable source (None) counts only when another source
        says persistent, which is the same as any source saying it; when NO source is readable the worker is treated as plain."""
        sources = (lambda: self._evidence_audit(worker), lambda: self._evidence_registry(worker), lambda: self._evidence_record(worker, record_dir))
        answers = [read() for read in sources]
        return any(x is True for x in answers)

    def _evidence_audit(self, worker):
        try:
            with open(self.audit_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 1048576))
                lines = f.read().decode(errors="replace").splitlines()
        except FileNotFoundError:
            return False
        except OSError:
            return None
        for raw in lines:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("action") == "create" and rec.get("worker") == worker and rec.get("persistent") is True:
                return True
        return False

    def _evidence_registry(self, worker):
        try:
            obj = self.kube.get_secret(self._registry_secret())
            if not obj:
                return False
            raw = (obj.get("data") or {}).get(REGISTRY_KEYS[1])
            agents = json.loads(base64.b64decode(raw).decode()) if raw else {}
            entry = agents.get(worker) if isinstance(agents, dict) else None
            return isinstance(entry, dict) and entry.get("persistent") is True
        except (CloudError, ValueError, UnicodeDecodeError, AttributeError, TypeError):
            return None

    def _evidence_record(self, worker, record_dir=None):
        root = self.recorder.root(record_dir)
        try:
            names = sorted(os.listdir(root))
        except FileNotFoundError:
            return False
        except OSError:
            return None
        found = False
        for entry in names:
            if not entry.startswith(worker + "-"):
                continue
            try:
                with open(os.path.join(root, entry, "run.json")) as f:
                    rec = json.load(f)
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                found = None if found is False else found
                continue
            if isinstance(rec, dict) and rec.get("worker") == worker and rec.get("persistent") is True:
                return True
        return found

    def _remove(self, name, nonce, uid=None):
        """Delete the pod and both Secrets (as `delete` does); with a nonce, only those that carry it (the pod, when there, matched already).
        With a uid the pod delete carries it as a precondition: a pod that changed since it was read is left whole, with a refusal.
        A persistent orchestrator: the StatefulSet FIRST, waited for within its bound (its controller would recreate a pod deleted alone); a delete
        that does not finish raises here and NOTHING else is removed (the pod, both Secrets and the hub entries stay); then the pod, then the rest."""
        try:
            if getattr(self, "_persistent_stop", None) == name:
                self.kube.delete_workload_and_wait(name)
                self.kube.delete_pod(name + "-0")
                uid = None
            got = self._remove_bound(name, nonce, uid)
            if getattr(self, "_orchestrator", None):   # the pod and its Secrets are gone: so are its hub entries
                self._unregister(self._orchestrator)
            return got
        except PodChanged:
            raise CloudError("pod/%s changed since it was read (another pod holds the name now); left whole" % name, EXIT_REFUSED)

    def _remove_bound(self, name, nonce, uid):
        if not nonce:
            return self.kube.delete(name, uid)
        self.run_id = nonce
        left = []
        changed = False
        for kind, label, fn in (("pod", "pod/" + name, lambda n: self.kube.delete_pod(n, uid)), ("secret", "secret/%s-pw" % name, self.kube.delete_secret),
                                ("run", "secret/%s-run" % name, self.kube.delete_run_secret)):
            try:
                mine = self._mine(kind, name)
                if mine:
                    try:
                        fn(name)
                    except PodChanged:
                        changed = True
                elif mine is False:
                    print("cloud-worker: %s is not this run's (its run id does not match); left whole" % label, file=sys.stderr)
            except CloudError:
                left.append(label)
        if left:
            raise CloudError("delete incomplete; REMAINS: %s; delete each with: %s" % (", ".join(left), "; ".join(self._delete_cmd(l) for l in left)))
        if changed:
            raise PodChanged("pod changed")

    def _mark(self, name, uid, **kv):
        """Mark the pod that was read; a pod that changed is not marked (for hook-ran that leaves `delete` refusing: the safe side)."""
        try:
            self.kube.annotate(name, uid, **kv)
            return ""
        except PodChanged:
            return "pod/%s changed since it was read (another pod holds the name now); not marked %s" % (name, ", ".join(k.replace("_", "-") for k in kv))

    def _hook_view(self, res):
        """The end hook's answer as it may be shown or kept: pushed a bool, wip_branch a scrubbed string or None, the
        skipped names strings (at most 50), note and error scrubbed. Never raises."""
        def text(v, limit=300):
            return self.kube.scrub(v, limit) if isinstance(v, str) and v else ""
        try:
            raw = res.get("skipped")
            return {"pushed": bool(res.get("pushed")), "wip_branch": text(res.get("wip_branch"), 200) or None,
                    "skipped": [text(x, 200) for x in raw if isinstance(x, str)][:50] if isinstance(raw, list) else [],
                    "note": text(res.get("note")), "error": text(res.get("error")) or ("end hook gave an unreadable error" if res.get("error") else "")}
        except Exception:
            return {"pushed": False, "wip_branch": None, "skipped": [], "note": "", "error": "end hook answer unreadable"}

    def _hook_record(self, info, hook):
        if info.get("record"):
            try:
                self.recorder.add_hook(info["record"], hook)
            except Exception:   # add_hook never raises; a stop is never held back by the record
                pass

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
            if self._persistent_evidence(worker, getattr(a, "record_dir", None)):   # the same evidence rule as the stop; a plain worker is as before
                raise CloudError("worker %s is a persistent orchestrator: delete is refused (even with --force); use the stop command: cloud-worker stop %s"
                                 % (worker, worker), EXIT_USAGE)
            if not pod:
                return {"worker": worker, "runtime_id": "", "deleted": True}
            info["rid"], info["cost"] = "pod/" + name, self.cost(pod)
            if ann(pod, "hook-ran") != "true" and not a.force:
                raise CloudError("end hook has not run; use --force to delete anyway", EXIT_REFUSED)
            try:
                self.kube.delete(name, (pod.get("metadata") or {}).get("uid"))
            except PodChanged:
                raise CloudError("pod/%s changed since it was read; left whole (run delete again)" % name, EXIT_REFUSED)
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
            try:
                self.kube.annotate(name, (pod.get("metadata") or {}).get("uid"), hard_deadline=iso(hard), idle_seconds=idle)
            except PodChanged:
                raise CloudError("pod/%s changed since it was read; not extended (run extend again)" % name, EXIT_REFUSED)
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
        if ann(pod, "persistent") is not None:   # under the worker's name (NAME-0 is only its runtime id): persistent, no deadline, its age and accrued cost
            hours = max((self.clock() - parse_iso(ann(pod, "started"))).total_seconds() / 3600, 0)
            return {"worker": ann(pod, "worker"), "runtime_id": "pod/" + name, "phase": pod.get("status", {}).get("phase"), "persistent": True,
                    "idle_since": None, "deadlines": {"idle": None, "hard": None}, "age_hours": round(hours, 4), "cost_estimate_usd": self.cost(pod)}
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
        if a.worker and not pods[0] and self._persistent_workload(pod_name(a.worker)) is not None:
            pods = [self.kube.get_pod(pod_name(a.worker) + "-0")]   # a persistent orchestrator: shown under the worker's name
        if a.worker and not pods[0]:
            raise CloudError("unknown worker %s" % a.worker, EXIT_USAGE)
        return [self.record(p) for p in pods]

    def snapshot(self, a):
        """ONE read-only look at the cluster: the pods of the namespace (name, worker, phase, node, hard deadline) and, when the login may
        LIST the nodes, the nodes themselves (pool, size, creation time). The node list is optional: a login without `list` on nodes still
        reads the pods, and the answer says so (`nodes_read` false) instead of failing the whole read. No write call is made here."""
        pods = []
        for pod in self.kube.list_pods_all():
            meta = pod.get("metadata") or {}
            pods.append({"name": meta.get("name"), "worker": ann(pod, "worker"),
                         "phase": safe_value((pod.get("status") or {}).get("phase")) if (pod.get("status") or {}).get("phase") else None,
                         "node": (pod.get("spec") or {}).get("nodeName"), "hard_deadline": ann(pod, "hard-deadline")})
        nodes, nodes_read, nodes_error = [], True, None
        try:
            for node in self.kube.list_nodes():
                meta = node.get("metadata") or {}
                labels = meta.get("labels") or {}
                nodes.append({"name": meta.get("name"), "pool": _node_label(labels, NODE_POOL_LABELS), "size": _node_label(labels, NODE_SIZE_LABELS),
                              "created": meta.get("creationTimestamp")})
        except CloudError as e:
            nodes, nodes_read, nodes_error = [], False, self.kube.scrub(e, 200)
        return {"pods": pods, "nodes": nodes, "nodes_read": nodes_read, "nodes_error": nodes_error}

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

    def _persistent_owned(self, pod):
        """True only when the pod's own StatefulSet (named by its ownerReferences) is read strictly, exists, carries cw/persistent and owns this pod
        (an ownerReference of kind StatefulSet with the same name). Forbidden, unreadable, absent or any mismatch is False."""
        try:
            refs = [r for r in ((pod.get("metadata") or {}).get("ownerReferences") or []) if isinstance(r, dict) and r.get("kind") == "StatefulSet"]
            if len(refs) != 1 or not isinstance(refs[0].get("name"), str) or not NAME_OBJECT.fullmatch(refs[0]["name"]):
                return False
            workload = self.kube.get_workload(refs[0]["name"])
            meta = (workload or {}).get("metadata") or {}
            return bool(workload) and meta.get("name") == refs[0]["name"] and PERSISTENT_ANNOTATION in (meta.get("annotations") or {})
        except (CloudError, ValueError, TypeError, AttributeError):
            return False

    def reap(self):
        """One pass: stop every pod past its hard deadline or idle too long. Returns [(worker, reason, exit)]."""
        out, now, live = [], self.clock(), []
        self.reap_results, self.last_hook = {}, None
        for pod in self.kube.list_pods():
            worker = ann(pod, "worker")
            reason = None
            if PERSISTENT_ANNOTATION in ((pod.get("metadata") or {}).get("annotations") or {}):
                # A persistent orchestrator is judged ONLY through its workload: never by a deadline or by idleness, never stopped here. A workload
                # that cannot be read, or that does not own the pod, leaves the pod untouched (no stop, delete or patch) and is reported once.
                if self._persistent_owned(pod):
                    live.append(pod)   # the git token renewal still serves it
                else:
                    out.append((worker, PERSISTENT_UNREADABLE, 1))
                continue
            try:
                hard = self.hard_deadline(pod)
            except (ValueError, TypeError):
                out.append((worker, "unreadable deadline", 1))  # one bad pod never stops the pass
                continue
            if now >= hard:
                reason = "hard limit"
            else:
                live.append(pod)   # before its hard limit and not stopped below: the git token renewal's candidates
                act = self.activity(pod["metadata"]["name"])
                if act is None:
                    # Fail mode, decided: an unreadable activity is not a reason to kill work, so the pod runs until
                    # its hard limit; it is reported so it is not silent.
                    out.append((worker, "activity unreadable", 1))
                    continue
                if act and not act[0] and act[1] is not None and (now - act[1]).total_seconds() >= int(ann(pod, "idle-seconds", "600")):
                    reason = "idle"
            if reason:
                self.last_hook = None
                if pod in live:
                    live.remove(pod)
                try:
                    # by the run's own nonce where the pod carries one: a pod of another run under this name is left whole
                    res = self.stop(argparse.Namespace(worker=worker, reason=reason, run_id=ann(pod, "run-id") or None), reason)
                    self.reap_results[worker] = {k: res.get(k) for k in ("pushed", "wip_branch", "skipped", "record")}
                    out.append((worker, reason, 0))
                except CloudError as e:
                    self.reap_results[worker] = dict(self.last_hook or {})
                    out.append((worker, reason, e.code))
        out.extend(self.renew_git_tokens(live, now))
        return out

    # -- the git token's renewal (one more step of the reaper's pass) --
    def renew_git_tokens(self, pods, now):
        """For each running pod this launcher made, renew its git token when it is GIT_TOKEN_RENEW_AFTER_SECONDS old. Returns report entries:
        (worker, "git token renewed", 0) or (worker, "git token not renewed: WHY", 1). A pod that needs nothing (no per-run Secret, not due)
        adds nothing. One pod's trouble never stops the pass; the old token stays until a patch has landed."""
        out = []
        for pod in pods:
            worker = ann(pod, "worker")
            try:
                if ann(pod, "persistent") is not None and (pod.get("status") or {}).get("phase") == "Running" and NAME_RE.match(worker or "") \
                        and not self.kube.get_run_secret(pod_name(worker)):   # a persistent pod's Secret is the worker's: its absence is ONE report line
                    raise _NotRenewed("the per-run Secret of the persistent orchestrator was not found")
                done = self._renew_git_token(pod, now)
            except _NotRenewed as e:
                why = self.kube.scrub(str(e), 200)
                self._audit_renewal_failure(worker, pod, why)
                out.append((worker, "git token not renewed: " + why, 1))
                continue
            except (CloudError, ValueError, TypeError, KeyError, AttributeError, OSError) as e:   # unexpected data from the cluster: still named, never silent
                why = self.kube.scrub("unexpected: %s" % e, 200)
                self._audit_renewal_failure(worker, pod, why)
                out.append((worker, "git token not renewed: " + why, 1))
                continue
            if done:
                self.audit(worker, "pod/" + pod["metadata"]["name"], "renew-git-token", self._cost_or_zero(pod), True, "")
                out.append((worker, "git token renewed", 0))
        return out

    def _last_renewal_audit(self, rid):
        """(ok, reason) of the newest renew-git-token audit line of this pod, or None (reads the file's tail only)."""
        return last_audit_line(self.audit_path, "renew-git-token", rid)

    def _audit_renewal_failure(self, worker, pod, why):
        """One audit line per CHANGE of reason (a persistent failure is not written again every pass; the success line is the 'cleared' line)."""
        rid = "pod/" + pod["metadata"]["name"]
        if self._last_renewal_audit(rid) == (False, self.kube.scrub(why, limit=1000)):
            return
        self.audit(worker, rid, "renew-git-token", self._cost_or_zero(pod), False, why)

    def _cost_or_zero(self, pod):
        try:
            return self.cost(pod)
        except (ValueError, TypeError, KeyError, AttributeError):
            return 0.0

    def _worker_record_repo(self, worker):
        """The repository the switchboard's worker record names (read only, CW_CLOUD_RECORD_DIR): the one source the renewal trusts for what
        the worker was launched for. A missing or unreadable record stops the renewal."""
        rd = self.env.get("CW_CLOUD_RECORD_DIR")
        if not rd or not NAME_RE.match(worker or ""):
            raise _NotRenewed("no worker record to read (CW_CLOUD_RECORD_DIR is not set)" if not rd else "bad worker name")
        try:
            with open(os.path.join(rd, worker + ".json")) as f:
                repo = json.load(f).get("repo")
        except (OSError, ValueError, AttributeError):
            raise _NotRenewed("the worker record is missing or unreadable") from None
        if not isinstance(repo, str) or not repo:
            raise _NotRenewed("the worker record names no repository")
        return repo

    def _renew_git_token(self, pod, now):
        """True: renewed. False: nothing to do (quietly). Raises _NotRenewed(reason): refused or failed, the old token untouched."""
        name, worker = pod["metadata"]["name"], ann(pod, "worker")
        if (pod.get("status") or {}).get("phase") != "Running":
            return False
        # a persistent pod is NAME-0 but its per-run Secrets are named after the worker: found by the worker's name. Any annotation the renewal needs
        # (such as the token role) is read from the pod's own annotations, which are the pod template's.
        persistent = ann(pod, "persistent") is not None and bool(NAME_RE.match(worker or ""))
        if persistent:
            name = pod_name(worker)
        secret = self.kube.get_run_secret(name)
        meta = (secret or {}).get("metadata") or {}
        labels = meta.get("labels") or {}
        if not secret or labels.get("cw/pod") != name or ((ann(pod, "run-id") or "") != (labels.get("cw/run-id") or "")):
            return False   # no per-run Secret (the endpoint route), or one that is not this run's: left whole, as stop leaves it
        old = (secret.get("data") or {}).get(GIT_TOKEN_KEY)
        if not old:
            return False
        if not meta.get("uid") or not meta.get("resourceVersion"):
            raise _NotRenewed("the Secret carries no uid or resourceVersion to bind the patch to")
        stamp = (meta.get("annotations") or {}).get(GIT_TOKEN_MINTED) or meta.get("creationTimestamp")
        try:
            age = (now - parse_iso(stamp)).total_seconds() if stamp else None
        except (ValueError, TypeError, AttributeError):
            age = None   # an age that cannot be read is renewed, never trusted
        if age is not None and age < 0:
            age = None   # a stamp from the future (a clock step, or an edited annotation) is not trusted either
        if age is not None and age < GIT_TOKEN_RENEW_AFTER_SECONDS:
            return False
        self.kube.add_secrets(*[self._b64_text(v) for v in (secret.get("data") or {}).values() if isinstance(v, str)])   # every value of the Secret is known to the scrubber before anything can echo it
        repo = _pod_repo(pod)
        if _repo_key(repo) is None or _repo_key(repo) != _repo_key(self._worker_record_repo(worker)):
            raise _NotRenewed("repository mismatch: the worker record and the pod name different repositories")
        try:
            check_repo_allowed(repo, self.env)
        except CloudError:
            raise _NotRenewed("repository not on the allow-list") from None
        try:
            token = run_git_token_cmd(git_token_cmd(repo, self.env), repo, dict(os.environ, **self.env), register=self.kube.add_secrets)
        except CloudError as e:
            raise _NotRenewed("the mint failed: %s" % e) from None
        self.kube.add_secrets(token)
        if re.search(r"\s", token):
            raise _NotRenewed("the mint gave a token with whitespace in it")
        # The mint took up to a minute: the Secret may have been replaced by a relaunch of the same worker (same name, new run). The patch is
        # bound to the exact object that was checked (uid + resourceVersion in the patch body: the API server refuses it on a mismatch), and the
        # labels are read again as a second guard.
        now_secret = self.kube.get_run_secret(name)
        nmeta = (now_secret or {}).get("metadata") or {}
        if not now_secret or (nmeta.get("labels") or {}).get("cw/pod") != labels.get("cw/pod") \
                or (nmeta.get("labels") or {}).get("cw/run-id") != labels.get("cw/run-id"):
            raise _NotRenewed("the secret changed during the mint: not renewed")
        minted = iso(now)
        body = json.dumps({"metadata": {"annotations": {GIT_TOKEN_MINTED: minted}, "uid": meta.get("uid"),
                                        "resourceVersion": meta.get("resourceVersion")},
                           "data": {GIT_TOKEN_KEY: base64.b64encode(token.encode()).decode()}})
        try:
            self.kube.run(["patch", "secret", name + "-run", "--type=merge", "--patch-file=/dev/stdin"], stdin=body)   # the one place the token goes: stdin
        except CloudError as e:
            if "Conflict" in str(e) or "NotFound" in str(e) or "precondition" in str(e).lower():
                raise _NotRenewed("the secret changed during the mint: not renewed") from None
            raise _NotRenewed("the Secret patch failed: %s" % e) from None
        after = self.kube.get_run_secret(name)   # the cluster accepted it: the new data, the new stamp
        ameta = (after or {}).get("metadata") or {}
        if ((after or {}).get("data") or {}).get(GIT_TOKEN_KEY) != base64.b64encode(token.encode()).decode() \
                or (ameta.get("annotations") or {}).get(GIT_TOKEN_MINTED) != minted:
            raise _NotRenewed("the Secret patch was not confirmed on read-back")
        return True

    @staticmethod
    def _b64_text(value):
        try:
            return base64.b64decode(value, validate=True).decode()
        except (ValueError, UnicodeDecodeError):
            return ""

# ---------------------------------------------------------------- the settings file
# ONE private file holds the operator's settings. `apply_settings` is the only reader: `launch --cloud` (bin/cloud_worker.py) and this driver's
# `main` both call it. When the file exists it is the only source (the environment is read ONLY when there is no file); a key set in BOTH with a
# different value is refused, naming both sources and never a value. Nothing is run or expanded on read, and a value that looks like a secret is refused.
SETTINGS_FILE_NAME = "cloud.env"
CONFIG_DIR_NAME = "fl" + "eet-switchboard"   # the switchboard's own config directory (written in two parts: the names check keeps the retired prefix out of cloud/)
SETTINGS_PATH = None        # a test hook (a module attribute, no environment variable): None means the real path
SETTINGS_FILE_MAX = 65536
# The one table of settings: name, required (in the file), kind. Kinds: name (a plain name, list or address), path, command (run as given: a
# command, never a value), digest-ref (an image reference that ends in @sha256:<64 hex>), number, secret-name (a vault secret's name), duration (Nm or Nh).
SETTINGS = (
    ("CW_ALLOWED_REPOS", True, "name"),            # comma-separated OWNER/NAME
    ("CW_TOOLING_REPO", False, "name"),
    ("CW_CLOUD_IMAGE", True, "digest-ref"),
    ("CW_CLOUD_SERVER", True, "name"),             # the cluster API address
    ("CW_CLOUD_CA_FILE", False, "path"),
    ("CW_PULL_SECRET", False, "name"),             # the NAME of a Secret, never its content
    ("CW_VAULT_SUBSCRIPTION", True, "name"),
    ("CW_VAULT_NAME", True, "name"),
    ("CW_CLOUD_NAMESPACE", True, "name"),
    ("CW_KUBECTL", False, "path"),
    ("CW_OPENCODE_BIN", False, "path"),
    ("CW_RUN_RECORD_DIR", False, "path"),
    ("CW_CLOUD_SPEND_CAP_USD", False, "number"),
    ("CW_CLUSTER_TOKEN_CMD", False, "command"),
    ("CW_CLUSTER_ADMIN_CMD", False, "command"),
    ("CW_GIT_TOKEN_CMD", False, "command"),
    ("CW_APP_ID_CMD", False, "command"),
    ("CW_APP_INSTALLATION_CMD", False, "command"),
    ("CW_APP_KEY_CMD", False, "command"),
    ("CW_STACK_MODEL_SECRET", False, "secret-name"),      # the NAME of the vault secret holding the stack-only model credential (a stack launch only); the name, never the value
    ("CW_CLUSTER_ADMIN_SECRET", False, "secret-name"),   # the NAME of the vault secret that holds the cluster administrator route (default cw-cluster-admin-kubeconfig), never its content
    ("CW_CLUSTER_TOKEN_DURATION", False, "duration"),    # the cluster token's requested life (default 20m)
    ("CW_HUB_CHANNEL_URL", False, "name"),         # the channel's address an orchestrator pod is told (default http://cloud-hub.cw.svc:8443/message); not a secret
    ("CW_HUB_REGISTRY_SECRET", False, "name"),     # the NAME of the hub's registry Secret in the cloud namespace (default cloud-hub-registry), never its content
    ("CW_STACK_REGISTRY", False, "registry-host"),  # the stack registry's host name (pull-only for a stack pod); all three CW_STACK_ keys are set together or not at all
    ("CW_STACK_PULL_CLIENT_ID", False, "guid"),    # the client id of the pull identity (not a secret)
    ("CW_STACK_PULL_TENANT_ID", False, "guid"),    # the tenant id (not a secret)
    ("CW_VAULT_CHECK_DIR", False, "path"),         # the checkout the vault access check runs from; needed only after a vault firewall refusal (see vault_check_dir_problem)
    ("CW_MAX_PERSISTENT", False, "number"),        # the most persistent orchestrators (StatefulSets of the launcher's label) the namespace may hold (an integer 1 to 20; default 2)
)
SETTING_KINDS = {name: kind for name, _, kind in SETTINGS}
SETTING_REQUIRED = tuple(name for name, required, _ in SETTINGS if required)
# The secret shapes a value is refused for: (shape name, pattern, kinds it applies to; None = every kind). One row per shape.
_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "glpat-", "sk-", "xoxb-", "xoxp-", "xoxa-", "AKIA", "AIza", "Bearer ")
SECRET_SHAPES = tuple(
    ("token prefix " + prefix.strip(), re.compile(r"(?<![A-Za-z0-9])" + re.escape(prefix) + r"[A-Za-z0-9._~+/=-]{8,}"), None) for prefix in _PREFIXES
) + (
    ("a JWT", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}"), None),
    ("a PEM header", re.compile(r"-----BEGIN "), None),
    ("a URL with a password", re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]*:[^/\s@]+@"), None),
    ("a 40-character hex string", re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40}(?![0-9A-Fa-f])"), ("command",)),
)
_IMAGE_CHARS = re.compile(r"[a-z0-9._/:-]+")
_IMAGE_REF = re.compile(r"(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?/)?[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*@sha256:[0-9a-f]{64}")
_DNS_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = re.compile(r"(?:%s\.)+%s" % (_DNS_LABEL, _DNS_LABEL))   # at least two labels
_NETLOC = re.compile(r"([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?")
_NAMESPACE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SECRET_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
_VAULT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{1,22}[A-Za-z0-9]")
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_BARE_PROGRAM = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._+-]*")
_VAULT_SECRET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,126}")   # the vault secret-name grammar: a letter or digit first (the value reaches `az` as `--name <value>`, so it can never look like an option)
_DURATION = re.compile(r"([0-9]{1,3})([mh])")   # the grammar of the launch wiring's own parse of CW_CLUSTER_TOKEN_DURATION (bin/cloud_worker.py `_duration`)
_REGISTRY_SUFFIXES = (".azurecr.io",)   # a small table: a sovereign cloud is one more row
_REGISTRY_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_REFUSED_HOSTS = ("metadata.google.internal",)
_REFUSED_SUFFIX = ".localhost"
_MAX_HOST = 253
_SCHEME = "https://"
_PATHS = ("", "/")
_BAD_CHARS = re.compile(r"[\s\x00-\x1f\x7f%?#@\\]")   # urlsplit silently drops a tab, CR or LF: judge the string as written, so none may be there


def _norm(host):
    """The host as compared: lower case (a name is not case-sensitive, so METADATA.google.internal and 0X7F000001 are the same as their lower-case forms)."""
    return host.lower()


def _integer_host_label(label):
    """An all-digit label, or 0x and hex digits (the label is lower-cased first): the forms a resolver (inet_aton) reads as an integer address
    (2130706433, 127.1, 0x7f000001)."""
    return label.isdigit() or bool(re.fullmatch(r"0x[0-9a-f]*", label))


def _has_letter(label):
    return any(c.isalpha() for c in label)


def _port_ok(port):
    return port is None or 0 < int(port) < 65536


def _split(value):
    """urlsplit, or None when it raises ValueError (an unbalanced [ or ], for one)."""
    try:
        return urllib.parse.urlsplit(value)
    except ValueError:
        return None


def _shape_ok(parts):
    return parts.path in _PATHS and not parts.query and not parts.fragment


def _server_ok(value):
    """The cluster API address is always a DNS NAME, so every IP literal (IPv4, bracketed IPv6, integer, hex or octal forms) is refused by the one rule:
    https://NAME[:PORT], NAME with at least two labels of letters, digits and hyphens (1 to 63 characters, no leading or trailing hyphen), at most 253
    characters in all, no trailing dot, a last label that has a letter and is neither all digits nor 0x and hex digits; a port of digits only, 1 to 65535.
    The branches, each with a named test and a mutant: scheme, bad characters, an unparsable URL, the netloc grammar, path, query and fragment, the host
    length, the port, the label grammar, the refused names, the refused suffix, a letter in the last label, an integer last label."""
    if not value.startswith(_SCHEME) or _BAD_CHARS.search(value):
        return False
    parts = _split(value)
    if parts is None:
        return False
    m = _NETLOC.fullmatch(parts.netloc)
    if not m or not _shape_ok(parts):
        return False
    host, port = m.group(1), m.group(2)
    low = _norm(host)
    last = _norm(low.rsplit(".", 1)[-1])
    return (len(host) <= _MAX_HOST and _port_ok(port) and bool(_HOST.fullmatch(host)) and low not in _REFUSED_HOSTS
            and not low.endswith(_REFUSED_SUFFIX) and _has_letter(last) and not _integer_host_label(last))


def _registry_host_ok(value):
    """A lower-case host name of at least two labels that ends in a known registry suffix: no scheme, path, port, user, IP or integer host."""
    return (len(value) <= _MAX_HOST and bool(_REGISTRY_HOST.fullmatch(value)) and value.endswith(_REGISTRY_SUFFIXES)
            and len(value) > max(len(x) for x in _REGISTRY_SUFFIXES) and not _integer_host_label(value.rsplit(".", 1)[-1]))


def _allowed_ok(value):
    keys = allowed_repo_keys(value)
    return None not in keys   # the one function the launch's allow-list check uses


def _path_ok(value):
    return os.path.isabs(value) and ".." not in value.split("/")


def _number_ok(value):
    return bool(_NUMBER.fullmatch(value)) and 0 < float(value) <= 100000


def _duration_ok(value):
    """Stricter than or equal to the launch wiring's `_duration` (a test holds it: whatever this accepts, the wiring accepts; the wiring also strips spaces and takes non-ASCII digits, this does not): 1 to 3 ASCII digits and m or h, from 1 minute to 60 minutes."""
    m = _DURATION.fullmatch(value)
    return bool(m) and 1 <= int(m.group(1)) * (60 if m.group(2) == "h" else 1) <= 60


# THE table of value rules: one row per setting, the rule's words (shown in a refusal, never the value) and its test. DEFAULT-DENY: a setting with no
# row is refused, and a test fails when SETTINGS and this table differ. Every value also refuses control characters (checked before this table).
VALIDATORS = {
    "CW_ALLOWED_REPOS": ("a comma-separated list of OWNER/NAME entries, no wildcard", _allowed_ok),
    "CW_TOOLING_REPO": ("one OWNER/NAME", lambda v: _repo_key(v) is not None),
    "CW_CLOUD_IMAGE": ("an image by digest, [registry[:port]/]path@sha256:<64 lowercase hex>, no tag, nothing else",
                       lambda v: bool(_IMAGE_CHARS.fullmatch(v.split("@")[0])) and bool(_IMAGE_REF.fullmatch(v))),
    "CW_CLOUD_SERVER": ("an https URL whose host is a DNS name (no IP literal of any form, no user, query, fragment, path, file: or http:)", _server_ok),
    "CW_CLOUD_CA_FILE": ("an absolute path with no .. segment", _path_ok),
    "CW_PULL_SECRET": ("a Kubernetes name (lowercase letters, digits, - and .)", lambda v: bool(_SECRET_NAME.fullmatch(v))),
    "CW_VAULT_SUBSCRIPTION": ("a subscription GUID", lambda v: bool(_GUID.fullmatch(v))),
    "CW_VAULT_NAME": ("a vault name: 3 to 24 letters, digits and hyphens, starting with a letter", lambda v: bool(_VAULT_NAME.fullmatch(v))),
    "CW_CLOUD_NAMESPACE": ("a DNS label that is not default or kube-*", lambda v: bool(_NAMESPACE.fullmatch(v)) and v != "default" and not v.startswith("kube-")),
    "CW_KUBECTL": ("an absolute path with no .. segment, or a bare program name", lambda v: _path_ok(v) or bool(_BARE_PROGRAM.fullmatch(v))),
    "CW_OPENCODE_BIN": ("an absolute path with no .. segment, or a bare program name", lambda v: _path_ok(v) or bool(_BARE_PROGRAM.fullmatch(v))),
    "CW_RUN_RECORD_DIR": ("an absolute path with no .. segment", _path_ok),
    "CW_CLOUD_SPEND_CAP_USD": ("a number above 0 and at most 100000", _number_ok),
    "CW_CLUSTER_TOKEN_CMD": ("a command that does not start with -", lambda v: not v.startswith("-")),
    "CW_CLUSTER_ADMIN_CMD": ("a command that does not start with -", lambda v: not v.startswith("-")),
    "CW_GIT_TOKEN_CMD": ("a command that does not start with -", lambda v: not v.startswith("-")),
    "CW_APP_ID_CMD": ("a command that does not start with -", lambda v: not v.startswith("-")),
    "CW_APP_INSTALLATION_CMD": ("a command that does not start with -", lambda v: not v.startswith("-")),
    "CW_APP_KEY_CMD": ("a command that does not start with -", lambda v: not v.startswith("-")),
    "CW_STACK_MODEL_SECRET": ("a vault secret name: 1 to 127 letters, digits and hyphens, starting with a letter or digit", lambda v: bool(_VAULT_SECRET_NAME.fullmatch(v))),
    "CW_CLUSTER_ADMIN_SECRET": ("a vault secret name: 1 to 127 letters, digits and hyphens, starting with a letter or digit", lambda v: bool(_VAULT_SECRET_NAME.fullmatch(v))),
    "CW_CLUSTER_TOKEN_DURATION": ("a duration of 1 to 3 digits and m or h, from 1m to 60m (for example 20m or 1h)", _duration_ok),
    "CW_HUB_CHANNEL_URL": ("an http or https URL: a host, an optional port, an optional one-segment path (/message), no user, query or fragment", lambda v: bool(re.fullmatch(r"https?://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?(?:/[A-Za-z0-9._-]{0,64})?", v))),
    "CW_HUB_REGISTRY_SECRET": ("a Kubernetes name (lowercase letters, digits, - and .)", lambda v: bool(_SECRET_NAME.fullmatch(v))),
    "CW_STACK_REGISTRY": ("a lower-case registry host name ending in .azurecr.io: no scheme, path, port, user or IP address", _registry_host_ok),
    "CW_STACK_PULL_CLIENT_ID": ("a GUID", lambda v: bool(_GUID.fullmatch(v))),
    "CW_STACK_PULL_TENANT_ID": ("a GUID", lambda v: bool(_GUID.fullmatch(v))),
    "CW_VAULT_CHECK_DIR": ("an absolute path with no .. segment", _path_ok),
    "CW_MAX_PERSISTENT": ("an integer from 1 to 20", lambda v: bool(re.fullmatch(r"[1-9][0-9]?", v)) and 1 <= int(v) <= PERSISTENT_MAX_LIMIT),
}
_SETTING_LINE = re.compile(r"([A-Z][A-Z0-9_]*)=(.*)")


def settings_path(env=None):
    """Where the file is: $XDG_CONFIG_HOME (default ~/.config) / <the switchboard's config directory> / cloud.env, beside the switchboard's own config."""
    if SETTINGS_PATH is not None:
        return str(SETTINGS_PATH)
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")
    return os.path.join(base, CONFIG_DIR_NAME, SETTINGS_FILE_NAME)


def _bad(path, why):
    return CloudError("refused: the settings file %s %s; nothing was called" % (path, why), EXIT_USAGE)


def _read_settings_file(path):
    """The file's text, or None when there is none. Refuses (CloudError, exit 2) a symlink, a file that is not a regular file, one owned by
    another user, one with any group or other permission bit, and one in a directory that is group- or world-writable or owned by another user.
    The checks are on the opened descriptor, so the file cannot change between the check and the read."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as err:
        raise _bad(path, "cannot be examined (%s)" % type(err).__name__) from None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as err:
        raise _bad(path, "is a symlink or cannot be opened (%s): it must be a regular file" % type(err).__name__) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _bad(path, "is not a regular file")
        if st.st_nlink != 1:
            raise _bad(path, "has more than one hard link: it must be the only name of the file")
        if st.st_uid != os.geteuid():
            raise _bad(path, "is owned by another user")
        if stat.S_IMODE(st.st_mode) & 0o177:
            raise _bad(path, "has mode %04o: it must be 0600 (nobody else may read or write it)" % stat.S_IMODE(st.st_mode))
        parent = os.stat(os.path.dirname(os.path.abspath(path)))
        if parent.st_uid != os.geteuid():
            raise _bad(path, "is in a directory owned by another user")
        if stat.S_IMODE(parent.st_mode) & 0o022:
            raise _bad(path, "is in a directory that is group- or world-writable (mode %04o)" % stat.S_IMODE(parent.st_mode))
        if st.st_size > SETTINGS_FILE_MAX:
            raise _bad(path, "is over %d bytes" % SETTINGS_FILE_MAX)
        raw = b""
        with os.fdopen(fd, "rb", closefd=False) as f:
            raw = f.read(SETTINGS_FILE_MAX + 1)
    finally:
        os.close(fd)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _bad(path, "is not UTF-8 text") from None


def parse_settings(text, path="the settings file"):
    """KEY=VALUE lines into {key: value}. `#` starts a comment line and blank lines are skipped. The value is the rest of the line, verbatim:
    no quoting, no expansion, nothing is run. A bad line, an unknown or repeated key, a quoted value, and a secret-shaped value are refused naming the
    line number and the key, never the value. An empty value counts as not set."""
    values = {}
    for number, line in enumerate(text.split("\n"), 1):
        lead = line.lstrip(" \t")   # a tab is allowed in leading indentation and inside a comment line, nowhere else
        bad = [c for c in lead if (ord(c) < 32 and not (c == "\t" and lead.startswith("#"))) or ord(c) == 127]
        if bad:
            key = line.split("=", 1)[0].strip() if "=" in line and line.split("=", 1)[0].strip() in SETTING_KINDS else None
            raise _bad(path, "line %d%s has a control character (a NUL, a carriage return or another: use plain LF text)" % (number, " (%s)" % key if key else ""))
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _SETTING_LINE.fullmatch(line.strip())
        if not m:
            raise _bad(path, "line %d is not KEY=VALUE" % number)
        key, value = m.group(1), m.group(2).strip()
        kind = SETTING_KINDS.get(key)
        if kind is None:
            raise _bad(path, "line %d: %s is not a setting this launcher knows" % (number, key))
        if key in values:
            raise _bad(path, "line %d: %s is set twice" % (number, key))
        if value[:1] in "\"'" and value[-1:] == value[:1] and len(value) > 1:
            raise _bad(path, "line %d: %s is quoted (the value is taken as written, with no quotes)" % (number, key))
        for shape, pattern, kinds in SECRET_SHAPES:
            if (kinds is None or kind in kinds) and pattern.search(value):
                raise _bad(path, "line %d: the value of %s looks like a secret (%s); the file holds no secret" % (number, key, shape))
        if value:
            rule = VALIDATORS.get(key)
            if rule is None or not rule[1](value):
                raise _bad(path, "line %d: %s must be %s" % (number, key, rule[0] if rule else "a setting with a value rule (it has none: refused)"))
            values[key] = value
    missing = [name for name in SETTING_REQUIRED if name not in values]
    if missing:
        raise _bad(path, "lacks the required setting%s %s" % ("s" if len(missing) > 1 else "", ", ".join(missing)))
    return values


def apply_settings(env=None, path=None):
    """THE reader of the operator's settings. Returns the source used: the file's path when it exists (its values then replace every setting of
    the table in `env`, which defaults to the process environment), else "environment" (nothing changes). A setting that the file and the
    environment BOTH set to different values is refused, naming both sources and no value."""
    env = os.environ if env is None else env
    path = path or settings_path(env)
    text = _read_settings_file(path)
    if text is None:
        return "environment"
    values = parse_settings(text, path)
    part = [k for k in STACK_REGISTRY_KEYS if k in values]
    if part and len(part) != len(STACK_REGISTRY_KEYS):   # all three or none (parse_settings judges one value at a time)
        raise _bad(path, "sets %s but not %s: the stack registry settings are all three or none" % (", ".join(part), ", ".join(k for k in STACK_REGISTRY_KEYS if k not in values)))
    clash = [name for name, _, _ in SETTINGS if env.get(name) and name in values and env[name].strip() != values[name]]
    if clash:
        raise CloudError("refused: %s is set differently in the file %s and in the environment: unset one (the file is the one source when it exists); "
                         "nothing was called" % (", ".join(clash), path), EXIT_USAGE)
    before = {name: env[name] for name, _, _ in SETTINGS if name in env}
    try:   # everything was validated above; if the environment still refuses a value, put every setting back as it was
        for name, _, _ in SETTINGS:
            env.pop(name, None)
        env.update(values)
    except Exception:
        for name, _, _ in SETTINGS:
            env.pop(name, None)
        env.update(before)
        raise CloudError("refused: the settings file %s could not be applied; nothing was changed" % path, EXIT_USAGE) from None
    return path


# -- stack-registry-check (ask #1022): runs on the operator's machine with the operator's own `az` login, BEFORE any pod exists. It only READS
# (az ... show / list); it never creates or changes an identity, a federated credential or a role. It cannot call the token exchange (that needs a
# projected token): only the live pod proves the exchange. Exit 0 = OK, 1 = an owner step is missing, 2 = CANNOT TELL (never a pass).
PULLER_IDENTITY = "cw-stack-puller-id"
PULLER_FIC_AUDIENCE = PULLER_AUDIENCE
PULL_ONLY_ROLES = ("AcrPull", "Reader")   # a role at the registry that is neither of these is write-capable (or wider) and is reported
AZ_BIN = "az"   # a test hook (a module attribute): the program that is run


class _AzCannotTell(Exception):
    pass


class _AzNotFound(Exception):
    pass


def _az_read(argv, env):
    """One read call of az; returns parsed JSON. Only the verbs show, list and get are ever passed (a test records them)."""
    try:
        r = subprocess.run([AZ_BIN] + argv + ["-o", "json"], capture_output=True, text=True, timeout=120,
                           env=dict(env))   # the operator's own environment: az is the operator's own login
    except (OSError, subprocess.SubprocessError):
        raise _AzCannotTell("az could not be run")
    if r.returncode != 0:
        if "ResourceNotFound" in r.stderr or "was not found" in r.stderr:
            raise _AzNotFound()
        raise _AzCannotTell("az is not logged in or may not read this resource (az exit %d)" % r.returncode)
    try:
        return json.loads(r.stdout or "null")
    except ValueError:
        raise _AzCannotTell("az answered something that is not JSON")


def stack_registry_check(a, env, out):
    """Report whether the pull identity can pull from the stack registry; name each missing owner step. Returns the exit status."""
    registry, ns = (env.get("CW_STACK_REGISTRY") or "").strip(), (env.get("CW_CLOUD_NAMESPACE") or "").strip()
    if not registry or not VALIDATORS["CW_STACK_REGISTRY"][1](registry) or not ns or not a.resource_group or not a.issuer:
        print("CANNOT TELL: set CW_STACK_REGISTRY and CW_CLOUD_NAMESPACE and pass --resource-group and --issuer", file=out)
        return 2
    subject = "system:serviceaccount:%s:%s" % (ns, PULLER_SERVICE_ACCOUNT)
    missing = []
    try:
        try:
            ident = _az_read(["identity", "show", "--name", a.identity, "--resource-group", a.resource_group], env)
        except _AzNotFound:
            print("MISSING owner step 1: create the user-assigned identity %s in resource group %s" % (a.identity, a.resource_group), file=out)
            return 1
        principal, client = (ident or {}).get("principalId"), (ident or {}).get("clientId")
        if not principal or not client:
            raise _AzCannotTell("the identity shows no principal id")
        creds = _az_read(["identity", "federated-credential", "list", "--identity-name", a.identity, "--resource-group", a.resource_group], env) or []
        if not any(c.get("issuer") == a.issuer and c.get("subject") == subject and PULLER_FIC_AUDIENCE in (c.get("audiences") or []) for c in creds):
            missing.append("MISSING owner step 2: create a federated credential on the identity with the cluster issuer, subject %s and audience %s" % (subject, PULLER_FIC_AUDIENCE))
        acr = _az_read(["acr", "show", "--name", registry.split(".")[0]], env)
        scope = (acr or {}).get("id")
        if not scope:
            raise _AzCannotTell("the registry shows no resource id")
        roles = [r.get("roleDefinitionName") for r in (_az_read(["role", "assignment", "list", "--assignee", principal, "--scope", scope], env) or [])]
        if "AcrPull" not in roles:
            missing.append("MISSING owner step 3: the registry's owner grants AcrPull on %s to principal id %s (pull only)" % (registry, principal))
        wide = sorted(set(r for r in roles if r not in PULL_ONLY_ROLES))
        if wide:
            missing.append("TOO WIDE owner step 3: remove the write-capable role %s from principal id %s at the registry" % (", ".join(map(str, wide)), principal))
    except _AzCannotTell as e:
        print("CANNOT TELL: %s" % e, file=out)
        return 2
    for m in missing:
        print(m, file=out)
    if missing:
        print("principal id (the one value the registry owner needs): %s" % principal, file=out)
        return 1
    print("OK: principal id %s can pull from %s (the token exchange itself is proven only by a live stack pod)" % (principal, registry), file=out)
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="cloud-worker")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name, worker=True):
        s = sub.add_parser(name)
        if worker:
            s.add_argument("worker", nargs=None if name != "status" else "?")
        s.add_argument("--json", action="store_true")
        return s
    k = sub.add_parser("stack-registry-check", help="read-only: can the stack pull identity pull from the stack registry; names the missing owner step")
    k.add_argument("--resource-group", required=True)
    k.add_argument("--issuer", required=True, help="the cluster's OIDC issuer URL")
    k.add_argument("--identity", default=PULLER_IDENTITY)
    c = cmd("create")
    c.add_argument("--repo", required=True)
    c.add_argument("--branch", required=True)
    c.add_argument("--idle", default="10m")
    c.add_argument("--hard", default="2h")
    c.add_argument("--max-hard", default=DEFAULT_MAX_HARD)
    c.add_argument("--shape", default=None, help="the pod shape, one of %s; unset: %s (%s with --stack or --agent orchestrator)" % (", ".join(SHAPES), DEFAULT_SHAPE, DEFAULT_STACK_SHAPE))
    c.add_argument("--image")
    c.add_argument("--stack", action="store_true",
                   help="a pod for a worker that runs a nested Docker stack: the Sysbox runtime class, its own user namespace, the pool's node label; off by default (the default pod is unchanged)")
    c.add_argument("--stack-model-secret", metavar="NAME", help="with --stack: the NAME of the vault secret whose value arrives as the run secret %s (audit: the name and 'delivered'); never a value" % STACK_MODEL_KEY)
    c.add_argument("--agent", help="worker (default) or orchestrator: the agent the pod runs (CW_AGENT); any other value is refused before anything is made")
    c.add_argument("--boss", help="with --agent orchestrator: the name of its boss (FLEET_BOSS)")
    c.add_argument("--persistent", action="store_true",
                   help="with --agent orchestrator and the per-run git-token Secret: an always-on orchestrator as ONE StatefulSet (restart always, no deadline, no idle limit); ended only by an explicit stop; refused for a worker or a stack launch")
    c.add_argument("--wait", type=int, default=300)
    c.add_argument("--pull-secret", help="name of an existing image-pull Secret (pull-only) for imagePullSecrets")
    c.add_argument("--run-secret-fd", action="append", default=[], metavar="NAME=N",
                   help="repeatable; the value is read from inherited descriptor N and piped to kubectl's stdin into the run's one Secret, mounted read-only at /run/cw/NAME (0400), deleted with the pod")
    c.add_argument("--run-id", type=_run_id,
                   help="a caller's nonce (lowercase hex), recorded on the pod and Secrets it creates so the caller can prove they are its own")
    c.add_argument("--password-fd", type=int, help="a file descriptor the caller holds; the password is written there once")
    h = cmd("hub-session")
    h.add_argument("--session", required=True)
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
    cmd("snapshot", worker=False)   # ONE read-only look at the cluster: the pods of the namespace and, when the login may list them, the nodes
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


REAP_FLOOR_SECONDS = 60   # the contract's once a minute: a pass is never further apart than this, whatever --loop says


def _reap_exit_line(clock, why):
    """The ONE line a reaper process writes when it ends: a UTC time and the reason. The reason is built by the caller
    from a scrubbed message, never from a token or an environment value."""
    now = clock() if clock else dt.datetime.now(dt.timezone.utc)
    print("cloud-worker: %s reaper exiting: %s" % (iso(now), why), file=sys.stderr)


REAPER_LOCK_ROOT = None   # tests point it at a temporary directory; None means the user's own directory, found where it is used


def _lock_root():
    """The user's lock directory from the password database, never from the environment: where every reaper of one user must agree.
    Found when a reaper starts, not at import: a user with no password entry gets a reason from the reaper, not a broken launcher."""
    if REAPER_LOCK_ROOT:
        return REAPER_LOCK_ROOT
    try:
        return os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".local", "state", "cw-cloud", "locks")
    except KeyError:
        raise ReaperLocked("this user has no entry in the password database, so the reaper lock directory cannot be found")


def reaper_identity(namespace):
    """What makes two reapers rivals: the same user and the same namespace, and NOTHING about how either logs in (a token file, a kube
    context, a kubeconfig, a server spelled two ways all reach the same pods). The lock is per user, so the user is the directory."""
    return "namespace\n" + namespace


def reaper_lock(identity, root=None):
    """The one lock between a user's reapers of one namespace: an exclusive, non-blocking flock on a 0600 file named for a hash of
    `identity`, in a private per-user directory (from the password database), held for the process's life; the kernel drops it when
    the process dies, however it dies. Returns the open descriptor. Raises ReaperLocked for a second reaper, a directory or file that
    is not the user's own, or a link at the path."""
    directory = root or _lock_root()
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        dinfo = os.stat(directory)
        if dinfo.st_uid != os.getuid() or dinfo.st_mode & 0o022:
            raise ReaperLocked("the lock directory %s is not private to this user" % directory)
        name = "reaper-%s.lock" % hashlib.sha256(identity.encode()).hexdigest()[:16]
        fd = os.open(os.path.join(directory, name), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        raise ReaperLocked("the reaper lock cannot be opened: %s" % e.strerror)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ReaperLocked("the reaper lock file is not a private regular file of this user")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise ReaperLocked("another reaper holds the lock")
    return fd


def _reap_main(env, a, sleep, clock=None, refresher=None, kube=None):
    """The reaper's loop. Whatever ends it writes ONE line saying why (signal, error, or the end of the loop).

    The reaper's own login is the token file: it is made fresh at the start of every pass (before the cluster is reached), so an installed
    reaper keeps working for days without a person refreshing the file by hand. A login that is not ready (the file is missing and the
    refresh failed) is not fatal in a loop: the pass names the failure and the next attempt follows the retry gap.

    The reaper's own login note (the refresh failed, or the login is not ready) is named in the pass's report and in the audit, but it never
    fails the pass: the pass's exit code is the result of the pass's own work. The old token file still serves the login while it lasts, and a
    login that is not ready is fatal by itself (the token file missing, empty or unreadable: `token unavailable`). A real failure still shows
    up where it belongs: in the workers' own results."""
    old, why, lock = Launcher._catch_signals(), None, None
    clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    try:
        lock = reaper_lock(reaper_identity(env.get("CW_CLOUD_NAMESPACE", "cw")))   # BEFORE the first pass: two reapers would both run an end hook for one pod
        refresher = refresher or ClusterTokenRefresher(env, clock)
        L = Launcher(kube, env, clock, sleep) if kube is not None else None
        while True:
            refresh = refresher.run()
            for line in getattr(refresher, "notices", []):
                print(line, file=sys.stderr)   # the stdout line of a pass is the JSON report; the notice names a setting, never a value
            if kube is not None and refresh.token:
                kube.add_secrets(refresh.token)   # the new token is a secret from now on: the scrubber knows it
            note = "cluster token not refreshed: %s" % refresh.reason if refresh.state in ("failed", "waiting") else ""
            if kube is None:
                try:
                    kube = Kube(env)
                    refresher.kube = kube
                    L = Launcher(kube, env, clock, sleep)
                except CloudError as e:
                    note = note or ("cluster login failed: %s" % scrub(e, (), 120))
            work = L.reap() if L is not None else []   # the pass's own work: the workers' results
            res = work
            if note:
                res = res + [("", note, 1)]   # the reaper's own login note: named in the report, never a worker's result
            print(json.dumps([dict({"worker": w, "reason": r, "exit": c}, **(L.reap_results.get(w, {}) if L is not None else {})) for w, r, c in res]))
            if not a.loop:
                why = "end of its pass"
                return EXIT_HOOK if any(c for _, _, c in work) else 0
            sleep(min(a.loop, REAP_FLOOR_SECONDS))
    except (Interrupted, KeyboardInterrupt) as e:
        why = "stopped by %s" % (e if isinstance(e, Interrupted) else "interrupt")
        raise
    except ReaperLocked as e:
        why = "not started: %s" % e
        print("cloud-worker: %s" % e, file=sys.stderr)
        return EXIT_REFUSED
    except BaseException as e:
        why = "error: %s" % scrub(e, (), 200)
        raise
    finally:
        if lock is not None:
            os.close(lock)
        if kube is not None:
            kube.close()
        Launcher._restore_signals(old)
        _reap_exit_line(clock, why)   # reason and UTC time, once


def main(argv=None, env=None, clock=None, sleep=time.sleep):
    explicit = env is not None
    env = os.environ if env is None else env
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else 0
    if not explicit:   # the real process reads the settings file; a caller that passes its own environment has made its own choice
        try:
            apply_settings(env)
        except CloudError as e:
            print("cloud-worker: %s" % e, file=sys.stderr)
            return e.code
    if a.cmd == "stack-registry-check":   # before any cluster login: it needs only the operator's own az
        return stack_registry_check(a, env, sys.stdout)
    if not token_mode(env) and not (env.get("CW_KUBE_CONTEXT") or "").strip():
        if a.cmd == "reap":
            _reap_exit_line(clock, "cluster login failed: no login is set (CW_KUBE_CONTEXT or the token-file settings)")
        print("cloud-worker: CW_KUBE_CONTEXT is not set; refusing to run (the active kubectl context is never used)", file=sys.stderr)
        return EXIT_USAGE
    kube = None
    try:
        if a.cmd == "reap":
            # The reaper's own login: the token file is made fresh BEFORE the cluster is reached (a missing file is made, a stale one is
            # renewed), so an installed reaper needs no person to refresh it. A login that is not ready is fatal for one pass and not for a
            # loop: a loop waits out the retry gap and tries again.
            refresher = ClusterTokenRefresher(env, clock)
            refresher.run()
            try:
                kube = Kube(env)
                refresher.kube = kube
            except CloudError:
                if not a.loop:
                    raise
                kube = None
            return _reap_main(env, a, sleep, clock, refresher, kube)
        kube = Kube(env)
        L = Launcher(kube, env, clock, sleep)
        result = getattr(L, a.cmd.replace("-", "_"))(a)
        print(result["report"] if a.cmd == "report" and not a.json else json.dumps(result, sort_keys=True))
        return 0
    except CloudError as e:
        if a.cmd == "reap" and kube is None:   # the login itself failed (the token file, the server, a kubeconfig)
            _reap_exit_line(clock, "cluster login failed: %s" % scrub(e, (), 200))
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
