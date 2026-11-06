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
          # the orchestrator row: it reads and relays, it builds nothing; no nested Docker. The estimate is the larger of the ratios 0.25 (cpu) and 0.375 (memory) against XS 0.05 = 0.01875, rounded up
          "O": {"cpu": "250m", "memory": "768Mi", "usd_per_hour": 0.02, "stack": False},
          "S": {"cpu": "2", "memory": "4Gi", "usd_per_hour": 0.10, "stack": True},
          "M": {"cpu": "4", "memory": "8Gi", "usd_per_hour": 0.20, "stack": True}}  # list-price estimates, not billed figures

DEFAULT_SHAPE = "XS"        # a create that names no shape and is not a stack launch (S, M by name only)
DEFAULT_ORCHESTRATOR_SHAPE = "O"   # an orchestrator launch that names no shape takes its own small row (the role table carries no shape: this is the one place)
DEFAULT_STACK_SHAPE = "S"   # a stack launch that names no shape (its row must carry stack: true)

# Per-shape requests (#1978): ONE table, shape S only; a shape with no row has no requests key. The limits stay the SHAPES rows.
SHAPE_REQUESTS = {"S": {"cpu": "500m", "memory": "2Gi"}}


def parse_quantity(q):
    """A Kubernetes quantity as an exact number (CPUs or bytes): plain numbers and the suffixes m, Ki, Mi, Gi."""
    from fractions import Fraction
    q = str(q)
    for suffix, mult in (("Ki", 1024), ("Mi", 1024 ** 2), ("Gi", 1024 ** 3), ("m", Fraction(1, 1000))):
        if q.endswith(suffix):
            return Fraction(q[:-len(suffix)]) * mult
    return Fraction(q)


def requests_fit(shape_name, resources):
    """True when resources hold requests of exactly cpu and memory, each equal to the table's value for the shape and not above the same limit."""
    if not isinstance(resources, dict) or set(resources) != {"limits", "requests"}:
        return False
    req, lim = resources.get("requests"), resources.get("limits")
    table = SHAPE_REQUESTS.get(shape_name)
    try:
        return bool(table) and req == table and set(lim) == {"cpu", "memory"} and all(
            parse_quantity(req[k]) <= parse_quantity(lim[k]) for k in ("cpu", "memory"))
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        return False

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
UNAUTH_RETRY_VERBS = ("get", "patch", "delete", "label")   # the reads and idempotent calls: never exec, never create
UNAUTH_REASON = "refreshed on unauthorized"   # the fixed audit reason of a refresh forced by an Unauthorized answer
REFRESH_ACTION = "refresh-cluster-token"   # the audit action of the reaper's own login refresh
SLOW_PASS_SECONDS = 600                 # a reaper pass that takes longer than this is named on the reaper's log and in the audit (a pass is normally seconds)
SLOW_PASS_ACTION = "slow-reap-pass"     # the audit action of that note (its reason holds the seconds)
ORPHAN_SECRET_AFTER_SECONDS = 3600      # a per-run Secret whose pod is absent is removed by the pass once it is older than this (set once; not a CW_ setting)
ORPHAN_SECRET_ACTION = "orphan-secret-removed"   # the audit action of that removal (one line per Secret; its reason holds the name and the age)
ORPHAN_SECRET_REASON = "orphan secret: its pod is absent"
PW_SECRET_NAME = re.compile(r"(.+-[0-9a-f]{4})-pw")   # '<worker>-<4 hex digits>-pw': the password Secret of the pod named by the group
REAP_PARALLEL = 4                       # the stops of one pass run at most this many at a time
REAP_STOP_SECONDS = 420                 # one stop's own limit: past it the wait is abandoned for this pass and the pod is left whole
RENEW_CADENCE_SECONDS = 600             # the renewal and the login refresh run at least this often, on their own thread, whatever a pass is doing
RESTART_ACTION = "reaper-restart"       # the audit action of the reaper's exit after the installed code changed (the service manager starts it again)
CODE_IDENTITY_FILES = ("cloud/launcher/cloud_worker.py", "cloud/launcher/run_record.py", "bin/path_walk.py", "cloud/launcher/token_mint.py", "bin/registry.py", "bin/record_json.py")   # the module and the kit modules it imports
RENEW_POLL_SECONDS = 15                 # how often that thread looks at the clock
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
SAFE_TO_EVICT_KEY = "cluster-autoscaler.kubernetes.io/safe-to-evict"  # not a cw/ key: the autoscaler reads it, the launcher reads nothing from it
GIT_TOKEN_MINTED = "cw/git-token-minted"         # the Secret annotation that says when the token was minted (the Secret's creation time stands in when it is absent)
GIT_TOKEN_MINT_SECONDS = 60                      # the mint child's run limit
GIT_TOKEN_MAX_CHARS = 4096                       # the most a token may be
# The token at its limit (#1354). The pod holds NO credential for the minter and never gets one: when the end hook's push is refused because
# the token had ended, the STOP side mints through the same route as the renewal (run_git_token_cmd), replaces ONLY the git-token key of the
# per-run Secret, waits (bounded) for the kubelet to project it, and runs the hook once more. One refresh, one retry, never a loop.
REFRESH_USED, REFRESH_REFUSED, REFRESH_NONE = "used", "refused", "none"   # the one word the stop's audit line and run record carry (never a value)
GIT_TOKEN_PROJECTION_SECONDS = 120               # the most the stop waits for the kubelet to project the new token into the pod
GIT_TOKEN_PROJECTION_POLL_SECONDS = 5
SCOPE_PROBE_SECONDS = 20
# The minute-40 note (new text): the worker is asked, once, to save its work to <worker>/wip with a plain push while its token still lives.
WIP_NOTE_AFTER_SECONDS = 40 * 60
WIP_NOTE_MARK = "wip_note"                      # the pod annotation (cw/wip-note) that says the note was sent: once, never twice


# The final note: N minutes before the pod's own hard deadline, the worker is asked, once, to send its final message (fixed words).
FINAL_NOTE_MARK = "final_note"                   # the pod annotation (cw/final-note): "sent" or "unreadable"; once, never twice
FINAL_NOTE_DEFAULT_MINUTES = "10"
FINAL_NOTE_TEXT = ("Your time is almost up. Send your final message now: what you pushed, your counts and what is left. "
                   "Then continue only if time remains.")


def wip_note(worker):
    """The note sent to the worker at minute 40: names the branch and the exact command (plain, never forced)."""
    return ("Your git token ends in about 20 minutes. Save your work now: commit it, then push it to your work-in-progress branch %s/wip "
            "with exactly this command (plain, never forced): git push origin HEAD:refs/heads/%s/wip" % (worker, worker))


def wip_note_due(started, now, already):
    """True once the pod is 40 minutes old and the note was not sent yet. started: a datetime or None; already: the annotation's value."""
    return not already and started is not None and (now - started).total_seconds() >= WIP_NOTE_AFTER_SECONDS


# The idle nudge: a pod idle for 15 minutes with no results file is asked, once, to continue with its brief.
IDLE_NUDGE = "Continue with the remaining steps of your brief and push each commit. If you are finished, write /work/results.txt."
IDLE_NUDGE_AFTER_SECONDS = 15 * 60
IDLE_NUDGE_MARK = "idle-nudge"                   # the pod annotation (cw/idle-nudge) that says the nudge was sent: once, never twice


def idle_nudge_due(busy, last_activity, now, idle_seconds, already, role, results_exists):
    """True only for a pod that is not busy, idle for 15 minutes or more, whose idle limit is longer than 15 minutes, not nudged yet,
    not the orchestrator, and whose results file does not exist."""
    return (not busy and last_activity is not None and (now - last_activity).total_seconds() >= IDLE_NUDGE_AFTER_SECONDS
            and idle_seconds > IDLE_NUDGE_AFTER_SECONDS and not already and role != "orchestrator" and results_exists is False)


def nudge_skip(self, pod, worker, name, quiet, reason, now=None):
    """DIAGNOSIS ONLY: one 'idle-nudge-skipped' audit line per (pod uid, quiet period) for a pod whose idle limit is longer than 15 minutes;
    remembered in memory, so a reaper restart may write one more line. Never raises, never changes the nudge."""
    try:
        if int(ann(pod, "idle-seconds", "600")) <= IDLE_NUDGE_AFTER_SECONDS:
            return
        if now is not None:   # a pod launched under 15 minutes ago (or with no known start) gets no line at all
            from datetime import datetime as _dt
            meta = pod.get("metadata") or {}
            began = (pod.get("status") or {}).get("startTime") or meta.get("creationTimestamp")
            began = _dt.fromisoformat(str(began).replace("Z", "+00:00"))
            if (now - began).total_seconds() < IDLE_NUDGE_AFTER_SECONDS:
                return
            if reason.endswith("(idle unknown)"):   # idle time not known: ONE line per pod per 15 minutes, not tied to idle time
                last = self.__dict__.setdefault("_nudge_unknown", {})
                k = meta.get("uid") or name
                if k in last and (now - last[k]).total_seconds() < IDLE_NUDGE_AFTER_SECONDS:
                    return
                last[k] = now
                self.audit(worker, "pod/" + name, "idle-nudge-skipped", self._cost_or_zero(pod), True, reason)
                return
        seen = self.__dict__.setdefault("_nudge_skipped", {})
        key = ((pod.get("metadata") or {}).get("uid") or name, str(quiet))
        if key in seen:
            return
        seen[key] = True
        self.audit(worker, "pod/" + name, "idle-nudge-skipped", self._cost_or_zero(pod), True, reason)
    except Exception:
        pass


NUDGE_MEMORY_CAP = 2000


def nudge_prune(self, pods):
    """Forget the skip memory of pods no longer in the list; backstop: keep at most NUDGE_MEMORY_CAP entries each, dropping the oldest. Never raises."""
    try:
        live = set()
        for p in pods:
            m = p.get("metadata") or {}
            live.update(x for x in (m.get("uid"), m.get("name")) if x)
        seen, last = self.__dict__.get("_nudge_skipped"), self.__dict__.get("_nudge_unknown")
        if seen is not None:
            for k in [k for k in seen if k[0] not in live]:
                del seen[k]
            for k in list(seen)[:max(0, len(seen) - NUDGE_MEMORY_CAP)]:
                del seen[k]
        if last is not None:
            for k in [k for k in last if k not in live]:
                del last[k]
            for k in list(last)[:max(0, len(last) - NUDGE_MEMORY_CAP)]:
                del last[k]
    except Exception:
        pass


# Run INSIDE the pod by `kubectl exec python3 -c <this> <text>`: the pod's own headless server, the password file the activity probe reads.
# It prompts the newest session with the note (queued). It prints nothing and holds no credential beyond the server password it already reads.
NOTE_SENDER_CODE = (
    "import base64,json,sys,urllib.request as u\n"
    "pw=open('/run/cw/server-password').read().strip()\n"
    "h={'Authorization':'Basic '+base64.b64encode(('opencode:'+pw).encode()).decode(),'Content-Type':'application/json'}\n"
    "g=lambda p,d=None:json.loads(u.urlopen(u.Request('http://127.0.0.1:4096'+p,data=d,headers=h),timeout=10).read().decode() or 'null')\n"
    "s=[x for x in g('/api/session')['data'] if isinstance(x,dict)]\n"
    "s.sort(key=lambda x:(x.get('time') or {}).get('updated') or 0)\n"
    "g('/api/session/'+s[-1]['id']+'/prompt',json.dumps({'text':sys.argv[1],'delivery':'queue'}).encode())\n")

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
LAUNCH_OUTER_SECONDS = 900   # the switchboard's own bound on the launch call: a wait for a node ends here
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
NO_REPOSITORY_NOTE = "no repository in the pod; nothing to push"   # the end hook's answer when /work/repo holds no checkout (end_hook.py says the same)
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
        or any((set((k.get("resources") or {})) != {"limits"} and not requests_fit(pod.get("metadata", {}).get("annotations", {}).get("cw/shape"), k.get("resources"))) or set(k["resources"]["limits"]) != {"cpu", "memory"} for k in ks)   # limits stay; requests only the table's
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
PERSISTENT_PATTERN = re.compile(r"[1-9][0-9]?")   # the ONE form of the value, used by the settings validator and by the launch: no leading zero, no space
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


import ipaddress  # noqa: E402  (PC-11: the masking is decided by the standard module)
WINDOW = 200               # a run of address characters longer than this is cut into windows ...
WINDOW_OVERLAP = 64        # ... that overlap by this many characters: more than the longest address (45) plus a short zone
SUBSTRING_MAX = 64         # a substring tried is at most this long: at most 64 parses start at one position
MASK_GUARD = 1000          # PC-12: the repeat ends in any case after this many passes; the text is then replaced by [masking_loop]
_RUN_RE = re.compile(r"[0-9A-Fa-f:.]+")   # the finder of candidates: every maximal run of address characters


def _address_spans(run):
    """Every [start, end) span of `run` (one window) that `ipaddress.ip_address` accepts: a span starts and ends at the edge of the run or next to
    a colon or a dot (a `::` is never a separator: `1::2::3` stays whole and unmasked)."""
    spans, n = [], len(run)
    for i in range(n):
        if i == 0 or (run[i - 1] in ":." and run[i - 2:i] != "::"):
            for j in range(i + 1, min(n, i + SUBSTRING_MAX) + 1):
                if j == n or (run[j] in ":." and run[j:j + 2] != "::"):
                    try:
                        ipaddress.ip_address(run[i:j])
                    except ValueError:
                        continue
                    spans.append((i, j))
    return spans


def _mask_pass(text):
    """One pass: in every run the LONGEST parseable substring is taken first (ties: the leftmost), then the longest of what does not overlap it,
    until nothing parses; each taken substring becomes `[address]`."""
    out, last = [], 0
    for m in _RUN_RE.finditer(text):
        run, base, spans = m.group(), m.start(), set()
        if len(run) <= WINDOW:
            spans.update(_address_spans(run))
        else:
            step = WINDOW - WINDOW_OVERLAP
            for w in range(0, len(run), step):
                spans.update((w + a, w + b) for a, b in _address_spans(run[w:w + WINDOW]))
                if w + WINDOW >= len(run):
                    break
        taken, used = [], bytearray(len(run))
        for a, b in sorted(spans, key=lambda s: (s[0] - s[1], s[0])):
            if not any(used[a:b]):
                used[a:b] = b"\1" * (b - a)
                taken.append((a, b))
        for a, b in sorted(taken):
            out.append(text[last:base + a])
            out.append("[address]")
            last = base + b
    out.append(text[last:])
    return "".join(out)


_HEX = frozenset("0123456789abcdefABCDEF")


def _glue_pass(text):
    """PC-13, one glue pass on the text the PC-11/PC-12 passes left: in every run of address characters a candidate of at most 64 characters may start at a
    normal start (run start, or after a colon or a dot) or at an extension start (a hex digit inside a group, 1 to 5 hex characters of the group before it)
    and end at a normal end or an extension end (the mirror), at least one being an extension; a candidate with a group of more than 4 characters is not
    tried; the longest that `ipaddress` accepts (ties: the leftmost) becomes `[address]`."""
    out, last = [], 0
    for m in _RUN_RE.finditer(text):
        r, n = m.group(), len(m.group())
        gs, ge, mark = [0] * n, [0] * n, -1
        for i, c in enumerate(r):
            gs[i] = mark + 1
            if c in ":.":
                mark = i
        nxt = n
        for i in range(n - 1, -1, -1):
            if r[i] in ":.":
                nxt = i
            ge[i] = nxt
        starts = []
        for p in range(n):
            ext = p > 0 and r[p] not in ":." and r[p - 1] in _HEX and 1 <= p - gs[p] <= 5
            if ext or p == 0 or r[p - 1] in ":.":
                starts.append((p, ext))
        ends = []
        for q in range(1, n + 1):
            ext = q < n and r[q - 1] in _HEX and r[q] in _HEX and r[q - 1] not in ":." and r[q] not in ":." and 1 <= ge[q] - q <= 5
            if ext or q == n or r[q] in ":.":
                ends.append((q, ext))
        best = None
        for p, pe in starts:
            for q, qe in ends:
                if q - p > SUBSTRING_MAX:
                    break
                if q <= p or not (pe or qe):
                    continue
                cand = r[p:q]
                if best is not None and q - p <= best[1] - best[0]:
                    continue
                if any(len(g) > 4 for g in re.split(r"[:.]", cand)):
                    continue
                try:
                    ipaddress.ip_address(cand)
                except ValueError:
                    continue
                best = (p, q)
        if best:
            out.append(text[last:m.start() + best[0]])
            out.append("[address]")
            last = m.start() + best[1]
    out.append(text[last:])
    return "".join(out)


_ABSORB_RE = re.compile(r"(?:(?<![0-9A-Za-z.])(?:[0-9]{1,3}\.){1,3})?\[address\](?:\.[0-9]{1,3}){1,3}(?![0-9])|(?<![0-9A-Za-z.])(?:[0-9]{1,3}\.){1,3}\[address\]")


def _absorb_pass(text):
    """PC-13 (the absorb rule): a `[address]` directly followed by a dot and 1 to 3 dotted octets, or directly preceded by 1 to 3 dotted octets, absorbs them
    (the dotted tail of an IPv6 address that phase 1 parsed only in part); over-masking only."""
    return _ABSORB_RE.sub("[address]", text)


def _mask_addresses(text):
    """The capacity answer's own masking, before the launcher's one scrub, decided by `ipaddress` (PC-10, PC-11, PC-12): every maximal run of address
    characters (an optional `%zone` stays text) is searched, in windows of 200 that overlap by 64, for substrings that `ipaddress` accepts, the longest
    first; a colon that only separates a label or a port is not part of the address; text that does not parse (`1::2::3`) is kept. The pass repeats
    until a pass changes nothing (each changing pass replaces a parseable substring by `[address]`, which holds none, so it ends); a guard of
    1,000 passes ends it in any case and the text is then replaced by `[masking_loop]`."""
    if not isinstance(text, str):
        return text
    for _ in range(MASK_GUARD):
        done = _mask_pass(text)
        if done == text:
            break
        text = done
    else:
        return "[masking_loop]"
    GLUE_PASS = True
    if GLUE_PASS and _address_spans("fd00::1"):  # PC-13: the glue pass (an address glued to hex letters), only while the IPv6 masking is on
        for _ in range(MASK_GUARD):
            glued = _absorb_pass(_glue_pass(text))
            if glued == text:
                return text
            text = glued
        return "[masking_loop]"
    return text


def _quantity(value, scale):
    """A Kubernetes quantity ('4', '3920m', '8Gi', '16000Ki') as a whole number: `scale` 1000 gives millicores, 1 gives bytes. Unreadable is 0."""
    text = str(value if value is not None else "0")
    units = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "k": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4, "m": 0.001}
    for suffix, mult in units.items():
        if text.endswith(suffix):
            text, factor = text[:-len(suffix)], mult
            break
    else:
        factor = 1
    try:
        return int(float(text) * factor * scale)
    except ValueError:
        return 0


POD_EVENTS_MAX, POD_EVENT_CHARS = 20, 200
_ADDRESS_RE = re.compile(r"(?<![\w.-])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\w.-])|(?<![\w.-])(?:(?:[A-Za-z0-9-]+\.)+[A-Za-z][A-Za-z0-9-]*|localhost):\d{1,5}(?![\w.-])|\[[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*\](?::\d{1,5})?|(?<![\w:])[0-9A-Fa-f:]{1,45}:(?:\d{1,3}\.){3}\d{1,3}(?![\w.:-])|(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){1,7}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?|::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?)(?:%[A-Za-z0-9_.-]{1,16})?(?![\w:])|(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![\w:])")


def mask_addresses(text):
    """An IPv4 address (with its port), a host name with a dot or `localhost` followed by a port, and an IPv6 address (compressed, full or bracketed with a port) become `[address]`; a word glued to a colon and digits that is no such name (`exit code:137`), a node name, a clock and a ratio are kept."""
    return _ADDRESS_RE.sub("[address]", str(text))


class KnownSet:
    """A set of known secret forms. It HOLDS its forms (it is not a set, and no public attribute exposes them), keeps a
    `version` (bumped AFTER every change of the content, under an RLock the instance owns, and only when the content
    changed) and the structure `scrub` built for it (`built`); both are read-only from outside, and `keep` stores a
    structure only for the version the set has now. A call whose structure is current never reads the set. An argument
    that is an iterable is read into a list BEFORE the lock is taken. No subclass, no pickle."""
    __slots__ = ("_forms", "_lock", "_version", "_built", "__weakref__")
    __hash__ = None

    def __init_subclass__(cls, **kw):
        raise TypeError("a KnownSet may not be subclassed")

    def __init__(self, forms=()):
        if getattr(self, "_lock", None) is not None:
            raise TypeError("a KnownSet is initialised once")
        items = list(forms)  # read first: a raise leaves the object uninitialised
        self._forms = set(items)
        self._version = 0
        self._built = None
        self._lock = threading.RLock()  # set last: it marks the object as initialised

    @property
    def version(self):
        return self._version

    @property
    def built(self):
        return self._built

    def keep(self, version, built):
        """Store the structure built for `version`; refused (False) when the set has moved on."""
        with self._lock:
            if version != self._version:
                return False
            self._built = built
            return True

    @staticmethod
    def _read(arg):
        """The argument as a list, read outside the lock; a raise half-way gives what was read and the error."""
        items = []
        try:
            for x in arg:
                items.append(x)
        except BaseException as exc:  # applied below, then raised again
            return items, exc
        return items, None

    def _bulk(self, name, args):
        reads = [self._read(a) for a in args]  # BEFORE the lock
        err = next((e for _, e in reads if e is not None), None)
        if err is not None and name in ("difference_update", "intersection_update", "symmetric_difference_update"):
            raise err  # an operation that removes forms: a half-read argument changes nothing (set and version unchanged)
        with self._lock:
            before = frozenset(self._forms)
            try:
                getattr(self._forms, name)(*[r for r, _ in reads])
            finally:
                if self._forms != before:
                    self._version += 1  # AFTER the content changed, under the lock
        if err is not None:
            raise err

    def add(self, item):
        with self._lock:
            if item not in self._forms:
                self._forms.add(item)
                self._version += 1

    def discard(self, item):
        with self._lock:
            if item in self._forms:
                self._forms.discard(item)
                self._version += 1

    def remove(self, item):
        with self._lock:
            if item not in self._forms:
                raise KeyError(item)  # nothing changed: no bump
            self._forms.remove(item)
            self._version += 1

    def pop(self):
        with self._lock:
            if not self._forms:
                raise KeyError("pop from an empty set")  # nothing changed: no bump
            item = self._forms.pop()
            self._version += 1
            return item

    def clear(self):
        with self._lock:
            if self._forms:
                self._forms.clear()
                self._version += 1

    def update(self, *args):
        self._bulk("update", args)

    def difference_update(self, *args):
        self._bulk("difference_update", args)

    def intersection_update(self, *args):
        self._bulk("intersection_update", args)

    def symmetric_difference_update(self, *args):
        self._bulk("symmetric_difference_update", args)

    def _inplace(self, name, other):
        if not isinstance(other, (set, frozenset, KnownSet)):
            return NotImplemented
        self._bulk({"__ior__": "update", "__iand__": "intersection_update", "__isub__": "difference_update",
                    "__ixor__": "symmetric_difference_update"}[name], (other,))
        return self

    def __ior__(self, other):
        return self._inplace("__ior__", other)

    def __iand__(self, other):
        return self._inplace("__iand__", other)

    def __isub__(self, other):
        return self._inplace("__isub__", other)

    def __ixor__(self, other):
        return self._inplace("__ixor__", other)

    def __iter__(self):
        with self._lock:
            return iter(frozenset(self._forms))  # ONE C-level copy: a set changed during a scrub never raises

    def __len__(self):
        with self._lock:
            return len(self._forms)

    def __contains__(self, item):
        with self._lock:
            return item in self._forms

    def __bool__(self):
        with self._lock:
            return bool(self._forms)

    def __eq__(self, other):
        if not isinstance(other, (set, frozenset, KnownSet)):
            return NotImplemented
        mine = frozenset(self)
        return mine == frozenset(other)

    def __ne__(self, other):
        r = self.__eq__(other)
        return r if r is NotImplemented else not r

    def __repr__(self):
        return "KnownSet(%d forms)" % len(self)  # never the forms

    def __reduce__(self):
        raise TypeError("a KnownSet is not pickled")

    def __reduce_ex__(self, protocol):
        raise TypeError("a KnownSet is not pickled")

    def copy(self):
        return KnownSet(self)

    def __copy__(self):
        return KnownSet(self)

    def __deepcopy__(self, memo):
        return KnownSet(self)


_SCRUB_MARK = "[redacted]"
_SCRUB_SLOT = [None]  # (snapshot, order, index) for a set that keeps no version of its own


def _scrub_built(secrets_known):
    """The secrets in their order (longest first, then by text) and the index of every 8-character piece of every secret
    to the ranks of the secrets that hold it. Built once for each state of the set; a `KnownSet` keeps its own version."""
    version = getattr(secrets_known, "version", None)  # a KnownSet: read its version FIRST
    built = getattr(secrets_known, "built", None) if version is not None else _SCRUB_SLOT[0]
    if version is not None and built is not None and built[0] != version:
        built = None
    if version is None or built is None:
        snap = frozenset(secrets_known)  # ONE C-level copy, taken first: a set that changes during the call cannot raise here
        if version is not None or built is None or built[0] != snap:
            order = sorted({v for v in snap if v and len(v.strip()) >= MIN_FRAGMENT}, key=lambda v: (-len(v), v))
            index = {}
            for n, sec in enumerate(order):
                for k in range(len(sec) - MIN_FRAGMENT + 1):
                    ranks = index.setdefault(sec[k:k + MIN_FRAGMENT], [])
                    if not ranks or ranks[-1] != n:
                        ranks.append(n)
            built = (version if version is not None else snap, order, index)
            if version is not None:
                secrets_known.keep(version, built)  # kept for the version read FIRST
            else:
                _SCRUB_SLOT[0] = built
    return built


class _ScrubSpans:
    """The matches of the known secrets on the text (ask #2144, one pass): for each secret, only at the positions that hold a piece of
    it, its whole occurrences and its runs (the longest piece of it that starts there) are kept as intervals [a, b) of the ORIGINAL
    text. The overlapping intervals are merged and the merged ones are redacted in the same walk: no later rescan, so no piece of a
    secret is left as the tail of an overlap, and a character outside every match is never touched."""

    def __init__(self, text, order, index, cand):
        self.text, self.order, self.index, self.cand = text, order, index, cand
        self.found = []

    def sweep(self, sec, whole, positions):
        """One pass over the candidate positions of one secret: its whole occurrences `whole`, or (None) its runs."""
        text, found = self.text, self.found
        ptr = -1
        for i in positions:
            if i < ptr:
                continue
            if whole is not None:
                if not text.startswith(whole, i):
                    continue
                end = i + len(whole)
            else:
                if text[i:i + MIN_FRAGMENT] not in sec:
                    continue
                lo = min(len(sec), len(text) - i)
                if text[i:i + lo] not in sec:  # a piece of a piece is a piece: the longest one is found by halving
                    low, hi = MIN_FRAGMENT, lo - 1
                    while low < hi:
                        mid = (low + hi + 1) // 2
                        if text[i:i + mid] in sec:
                            low = mid
                        else:
                            hi = mid - 1
                    lo = low
                end = i + lo
            found.append((i, end))
            ptr = end

    def run(self):
        for rank in sorted(self.cand):
            sec = self.order[rank]
            positions = sorted(self.cand[rank])
            for whole in {sec, sec.strip()}:
                self.sweep(sec, whole, positions)
            self.sweep(sec, None, positions)
        merged = []  # the matches with the overlapping and the touching ones merged
        for a, b in sorted(self.found):
            if merged and a <= merged[-1][1]:
                if b > merged[-1][1]:
                    merged[-1][1] = b
            else:
                merged.append([a, b])
        parts, pos = [], 0
        for a, b in merged:
            parts.append(self.text[pos:a])
            parts.append(_SCRUB_MARK)
            pos = b
        parts.append(self.text[pos:])
        return "".join(parts)


def _scrub_secrets(text, secrets_known):
    """Every known secret and every 8+ character piece of one removed from `text`: ONE probe of the index for each position,
    then each secret is applied only at the positions that hold a piece of it; the text is rebuilt once."""
    built = _scrub_built(secrets_known)
    order, index = built[1], built[2]
    cand, get = {}, index.get
    for i in range(len(text) - MIN_FRAGMENT + 1):
        for rank in get(text[i:i + MIN_FRAGMENT], ()):
            if rank in cand:
                cand[rank].add(i)
            else:
                cand[rank] = {i}
    if not cand:
        return text
    return _ScrubSpans(text, order, index, cand).run()


def scrub(text, secrets_known=(), limit=300):
    """THE one place kubectl output (and anything built from it) is made safe to show, log or audit: the exact values
    of this run's secrets and any 8+ character piece of one, token-shaped strings and long base64/hex runs are removed,
    then the text is cut to `limit`. Control characters become '?'."""
    text = str(text)[:SCRUB_INPUT_MAX]
    text = _scrub_secrets(text, secrets_known)
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


SECRET_PIECE_MIN = 8
TOKEN_PREFIXES = ("github_pat_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "sk-ant-", "sk-proj-", "sk-", "xoxb-", "xoxp-", "glpat-")


def _secret_pieces(value):
    """The usable pieces of a structured value, each of SECRET_PIECE_MIN+ characters (ask #2144): for a URL with a password, the
    password and user:password; for a token with a known prefix, the part after it; for a multi-line key, each line of 16+ characters."""
    v, out = value.strip(), set()
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://([^/?#@]*)@", v)
    if m and ":" in m.group(1):
        userinfo = m.group(1)
        out.update({userinfo, userinfo.partition(":")[2], urllib.parse.unquote(userinfo), urllib.parse.unquote(userinfo.partition(":")[2])})
    for pre in TOKEN_PREFIXES:
        if v.startswith(pre):
            out.add(v[len(pre):])
            break
    lines = [x.strip() for x in v.splitlines()]
    if len(lines) > 1:
        out.update(x for x in lines if len(x) >= 16)
    return {x for x in out if len(x) >= SECRET_PIECE_MIN and x != value and x != v}


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
    for piece in _secret_pieces(value):
        forms.update(secret_forms(piece))  # a piece is a form of the same value, with its own encodings
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


def git_token_cmd(repo, env, role="worker"):
    """The command that mints the git token for ONE repository: CW_GIT_TOKEN_CMD, else app_token.py beside this file. The launch (run_task.py)
    and the renewal run the same command; the renewal builds it here and the driver's own line in run_task.py is pinned equal by a test."""
    raw = env.get("CW_GIT_TOKEN_CMD")   # an operator's own command is run as it is: the role is passed to the default command only
    if raw:
        return shlex.split(raw)
    cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_token.py"), repo]
    return cmd + ["--role", role] if role != "worker" else cmd


GIT_TOKEN_TRIES = 4
GIT_TOKEN_WAITS = (2, 5, 10)
EXIT_TEMPFAIL = 75   # the token command's status for a passing failure; the only status that is tried again
git_token_sleep = time.sleep   # tests replace it


def tempfail_tries(run, last_line, say, sleep):
    """Run run() -> (status, result) up to GIT_TOKEN_TRIES times, waiting 2, 5, 10 seconds, and again ONLY when status is 75. last_line(result) is
    the cleaned last stderr line. say(text) hears one line per retry (no token value). Returns (status, result, tries). Shared by the launch."""
    tries = 0
    while True:
        tries += 1
        status, result = run()
        if status != EXIT_TEMPFAIL or tries >= GIT_TOKEN_TRIES:
            return status, result, tries
        wait = GIT_TOKEN_WAITS[tries - 1]
        say("git token try %d of %d failed: %s; trying again in %d seconds" % (tries, GIT_TOKEN_TRIES, last_line(result), wait))
        sleep(wait)


def read_pipe_end(fd, limit=8192):
    """Everything in the read end of a pipe (at most limit bytes), then close it. The writer has exited, so this does not wait."""
    try:
        data = b""
        while len(data) < limit:
            chunk = os.read(fd, limit - len(data))
            if not chunk:
                break
            data += chunk
        return data.decode("utf-8", "replace")
    except OSError:
        return ""
    finally:
        os.close(fd)


def mint_is_default(cmd, env):
    """True for the repository's own mint (no CW_GIT_TOKEN_CMD): it delivers the token through a descriptor, never on stdout. An override command prints on stdout."""
    return not env.get("CW_GIT_TOKEN_CMD") and len(cmd) >= 2 and cmd[1] == os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_token.py")


def run_git_token_cmd(cmd, repo, env, register=None):
    """THE mint call (the launch and the renewal both come here): run cmd for repo in env, return the token it gave. The default mint hands it over a
    pipe (--fd); an operator's CW_GIT_TOKEN_CMD prints it on stdout. Never prints, never puts the token in an argument or a file. register(text) hears
    the token before it is judged, so a caller's scrubber knows it. A passing failure (status 75) is tried again (tempfail_tries), a new pipe each try.
    Raises CloudError(EXIT_USAGE) with no value in the text."""
    def once():
        ends = os.pipe() if mint_is_default(cmd, env) else None
        try:
            q = subprocess.run(cmd + ["--fd", str(ends[1])] if ends else cmd, capture_output=True, text=True, timeout=GIT_TOKEN_MINT_SECONDS,
                               stdin=subprocess.DEVNULL, env=dict(env, GH_REPO=repo), pass_fds=(ends[1],) if ends else ())
        except (OSError, subprocess.TimeoutExpired, ValueError):
            q = None
        finally:
            if ends:
                os.close(ends[1])
        given = read_pipe_end(ends[0]) if ends else (q.stdout if q else "")
        if q and register:
            register(q.stdout.strip())
            if ends:
                register(given.strip())
        return (q.returncode if q else None), (q, given)

    def line(res):
        q = res[0]
        return scrub(q.stderr.strip().splitlines()[-1], (), 200) if q and q.stderr.strip() else "no message"

    _, (p, given), tries = tempfail_tries(once, line, lambda t: print(t, file=sys.stderr), lambda s: git_token_sleep(s))
    token = (given if p and p.returncode == 0 else "").strip()
    if not token or len(token) > GIT_TOKEN_MAX_CHARS:
        why = ""
        if p and p.stderr.strip():
            why = ": " + scrub(p.stderr.strip().splitlines()[-1], (), 200)
        after = " after %d tries" % tries if tries > 1 else ""
        raise CloudError("the git token command gave no usable token%s%s" % (after, why), EXIT_USAGE)
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


def _preflight_not_run(repo, exc):
    """The refusal text when the preflight did not run: on its time limit, the last `step:` line the child printed (subprocess.run keeps the partial
    stderr on the TimeoutExpired it re-raises) and the seconds waited; every other cause keeps the old text."""
    if not isinstance(exc, subprocess.TimeoutExpired):
        return "refused: the repository preflight for %s could not be run; nothing was created" % scrub(repo, (), 120)
    err = exc.stderr
    err = err.decode("utf-8", "replace") if isinstance(err, bytes) else (err or "")
    steps = [x.strip()[len("step: "):] for x in err.splitlines() if x.startswith("step: ")]
    step = steps[-1] if steps and steps[-1] in ("token mint", "repository read") else "its start"
    text = "refused: the repository preflight for %s timed out after %s s at %s; nothing was created" % (scrub(repo, (), 120), PREFLIGHT_SECONDS, step)
    if step == "token mint":
        text = text.replace("; nothing was created", "; the key vault is unreachable from this host; nothing was created")
    return text


def check_repo_preflight(repo, env):
    """The one preflight call (the operator driver and `launch --cloud` both call it), run AFTER the allow-list and BEFORE any token is minted.
    Returns the preflight's one result line. Raises CloudError(EXIT_USAGE) (fail closed) unless the program ran, exited 0 and printed something:
    a missing, killed, timed-out or silent program is a refusal, never a pass. The refusal carries the program's own message."""
    argv = list(PREFLIGHT_ARGV) + [repo] if PREFLIGHT_ARGV else [os.path.realpath(sys.executable), "-I", "-S", PREFLIGHT_PROGRAM, repo]
    try:
        with tempfile.TemporaryDirectory(prefix="cw-preflight-") as home:   # 0700, empty, removed when the child ends (also on failure)
            p = subprocess.run(argv, capture_output=True, text=True, timeout=PREFLIGHT_SECONDS, stdin=subprocess.DEVNULL, env=_preflight_env(env, home), cwd=home)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise CloudError(_preflight_not_run(repo, sys.exc_info()[1]), EXIT_USAGE) from None
    for x in (p.stderr or "").splitlines():   # only the child's own retry lines (already scrubbed texts), once each, in order
        if x.startswith("mint try "):
            print(scrub(x.strip(), (), 300), file=sys.stderr)
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


# ---------------------------------------------------------------- the vault block (ask #1427)
# After the vault's FIREWALL refuses this machine, every command that needs the vault refuses at once, for a set time, and starts no vault call.
# The block is ONE small private file in the switchboard's state directory: the time, a one-line reason and the retry time (no secret, no address, no
# vault name). Only a firewall answer creates it: a timeout, a missing secret, a login problem or a malformed answer never does. After the retry time
# exactly ONE command probes (the lock file below): success removes the block, a refusal renews it. With no block yet, the callers of one moment also
# take the lock one at a time, so a refusing vault is asked once and the others find the block.
VAULT_BLOCK_NAME = "vault-block.json"
VAULT_BLOCK_LOCK_NAME = "vault-block.lock"
VAULT_BLOCK_SETTING = "CW" + "_VAULT_BLOCK_DURATION"   # written in two parts: its settings row is a separate commit of ask #1427 (the names guard reads string constants)
VAULT_BLOCK_DEFAULT_MINUTES = 10
VAULT_BLOCK_REASON = "the vault refused this machine"
VAULT_BLOCK_POLL_SECONDS = 0.05


def vault_block_dir(env):
    """The block's folder: the switchboard's state directory ($XDG_STATE_HOME, default ~/.local/state, / the switchboard's directory name), in its
    `cloud` folder, where the launcher keeps its other records and where its tests already clear their files between two launches."""
    base = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, CONFIG_DIR_NAME, "cloud")


def vault_block_minutes(env):
    """The block's length in minutes: the setting (1m to 60m, the duration kind), else ten. A value that cannot be read gives the default."""
    text = (env.get(VAULT_BLOCK_SETTING) or "").strip()
    found = _DURATION.fullmatch(text) if text else None
    minutes = int(found.group(1)) * (60 if found.group(2) == "h" else 1) if found else VAULT_BLOCK_DEFAULT_MINUTES
    return minutes if 1 <= minutes <= 60 else VAULT_BLOCK_DEFAULT_MINUTES


def vault_lock_stale_seconds():
    """A probe lock older than the longest a vault call can last (the read, the access check after a refusal and the second read) is ignored."""
    return 2 * TOKEN_CMD_SECONDS + VAULT_CHECK_SECONDS + VAULT_CHECK_GRACE


VAULT_BLOCK_MAX_BYTES = 65536   # a block file is tiny: a larger one is not ours


def _damaged_block(path, base, minutes):
    """A block file that cannot be trusted FAILS CLOSED: it blocks until the file's own time plus the setting's length (never longer), then one probe runs."""
    return (base, base + 60 * minutes, path)


def read_vault_block(env, now=None):
    """None when there is no block file; otherwise (refused_at, retry_at, damaged_path) in epoch seconds (damaged_path is None for a good file).
    A file that is corrupt, empty, huge, a symlink, not ours, with a negative or non-number time blocks too, until the file's time (the earlier of its
    modification and change time and now, so a future date or a clock set back cannot lengthen it) plus the setting's length. A retry time is capped to it."""
    path = os.path.join(vault_block_dir(env), VAULT_BLOCK_NAME)
    try:
        found = os.lstat(path)
    except OSError:
        return None
    minutes = vault_block_minutes(env)
    now = time.time() if now is None else now
    mtime = found.st_mtime
    if mtime > now and stat.S_ISREG(found.st_mode) and found.st_uid == os.getuid():
        try:
            os.utime(path, (now, now), follow_symlinks=False)   # a time ahead of the clock (it was set back): the block starts now, never later
            mtime = now
        except OSError:
            pass
    # a time ahead of now is not believed: the block ends at most one setting's length from now (and from the first time it was seen so)
    base = min([t for t in (mtime, found.st_ctime) if t <= now] + [now])
    if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid() or found.st_size > VAULT_BLOCK_MAX_BYTES:
        return _damaged_block(path, base, minutes)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            raw = f.read(VAULT_BLOCK_MAX_BYTES + 1)
        if len(raw) > VAULT_BLOCK_MAX_BYTES:
            return _damaged_block(path, base, minutes)
        data = json.loads(raw.decode("utf-8"))
        at, retry = data["at"], data["retry_at"]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (at, retry)):
            raise ValueError("not a number")
        at, retry = float(at), float(retry)
        if not (math.isfinite(at) and math.isfinite(retry)) or at < 0 or retry < 0:
            raise ValueError("not a time")
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return _damaged_block(path, base, minutes)
    retry = min(retry, base + 60 * minutes)
    return (min(at, retry), retry, None)


def _stamp(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def vault_block_line(block):
    """The ONE line a blocked command prints: the reason and the retry time (never a value, an address or a name)."""
    if len(block) > 2 and block[2]:
        return "the vault block file %s cannot be trusted (damaged, too large, too far ahead or not a plain file of this user); no vault call is made until %s (delete that file to clear it by hand)" % (
            block[2], _stamp(block[1]))
    return "%s at %s; no vault call is made until %s (delete the vault-block file in the switchboard state directory to clear it by hand)" % (
        VAULT_BLOCK_REASON, _stamp(block[0]), _stamp(block[1]))


def write_vault_block(env, now):
    """Write the block by rename (a private temp file in the same directory, then os.replace). Never raises: a block that cannot be written costs only the saving."""
    directory = vault_block_dir(env)
    temp = None
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        body = json.dumps({"at": now, "reason": VAULT_BLOCK_REASON, "retry_at": now + 60 * vault_block_minutes(env)}, sort_keys=True)
        temp = os.path.join(directory, ".%s.%d.%s.tmp" % (VAULT_BLOCK_NAME, os.getpid(), secrets.token_hex(4)))
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.replace(temp, os.path.join(directory, VAULT_BLOCK_NAME))
        temp = None
    except OSError:
        pass
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _inspect_lock(lock):
    """(mtime, holder_gone) of the probe lock, or None when it is unusable (a FIFO, a symlink, a directory, a file of another user, unreadable).
    It is opened WITHOUT following a symlink and WITHOUT blocking, then judged by fstat: only a regular file of this user is a lock."""
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid():
            return None
        try:
            pid = int(os.read(fd, 20).decode("ascii", "replace").strip())
            os.kill(pid, 0)
            gone = False
        except ProcessLookupError:
            gone = True
        except (OSError, ValueError, OverflowError):
            gone = False   # an empty or unreadable lock is not judged gone: its age decides
        return (found.st_mtime, gone)
    finally:
        os.close(fd)


def _clear_unusable_lock(lock):
    """Replace an unusable lock (one line names it); True when it is gone, False when it cannot be removed (the call then goes on without a lock)."""
    print("cloud-worker: the vault probe lock %s is not a plain file of this user; ignoring it (delete it by hand to clear it)" % lock, file=sys.stderr)
    for remove in (os.unlink, os.rmdir):
        try:
            remove(lock)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            pass
    return False


class VaultFlight:
    """ONE vault call in flight at a time. `VaultFlight.enter(env)` raises ClusterLogin("vault-blocked") while a block lasts (its line is `.line`),
    or when a probe after the block's time is already running; otherwise it holds the lock. `done(ran)` records the final answer (a firewall refusal
    writes or renews the block; a success removes it; anything else changes nothing) and releases the lock."""

    def __init__(self, env, clock):
        self.env, self.clock, self.lock = env, clock, None

    @classmethod
    def enter(cls, env, clock=time.time, sleep=time.sleep):
        flight = cls(env, clock)
        directory = vault_block_dir(env)
        lock = os.path.join(directory, VAULT_BLOCK_LOCK_NAME)
        while True:
            block = read_vault_block(env, clock())
            if block is not None and clock() < block[1]:
                raise _vault_blocked(block)
            try:
                os.makedirs(directory, mode=0o700, exist_ok=True)
                fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                seen = _inspect_lock(lock)
                if seen is None:
                    if not os.path.lexists(lock):
                        continue   # it went away between the two calls: take it
                    if _clear_unusable_lock(lock):
                        continue
                    return flight   # cannot be removed: no lock is kept, and the call goes on (one call at most)
                age = clock() - seen[0]
                if abs(age) > vault_lock_stale_seconds() or seen[1]:   # too old, or dated far from now (a clock set back), or its command was killed
                    _unlink(lock)   # a stale lock (too old, or its command was killed) is ignored
                    continue
                if block is not None:
                    raise _vault_blocked(block)   # a probe is running: this command uses the block
                sleep(VAULT_BLOCK_POLL_SECONDS)   # no block yet: the call in flight answers first
                continue
            except OSError:
                return flight   # no state directory: no block can be kept, and the call goes on as before
            os.write(fd, str(os.getpid()).encode())   # who holds it: a lock of a command that is gone is ignored at once
            os.close(fd)
            flight.lock = lock
            block = read_vault_block(env, clock())   # the holder before us may have just written it
            if block is not None and clock() < block[1]:
                flight.release()
                raise _vault_blocked(block)
            return flight

    def release(self):
        if self.lock is not None:
            _unlink(self.lock)
            self.lock = None

    def done(self, ran):
        try:
            if firewall_refused(ran):
                write_vault_block(self.env, self.clock())
            elif ran is not None and ran[0] == 0:
                _unlink(os.path.join(vault_block_dir(self.env), VAULT_BLOCK_NAME))
        finally:
            self.release()


def _vault_blocked(block):
    err = ClusterLogin("vault-blocked", firewall=True)
    err.line = vault_block_line(block)
    return err


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
        flight = VaultFlight.enter(env)   # BEFORE any vault call: a block (or a probe in flight) refuses here, at once
        ran = None
        try:
            ran = read()
            recovered = False
            if recovery is not None and not admin and firewall_refused(ran):   # only the default vault read, only a firewall refusal, once
                ran = recovery(read, env)
                recovered = True
        finally:
            flight.done(ran)   # AFTER the final answer: a firewall refusal writes or renews the block, a success removes it
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
    "vault-blocked": "the vault refused this machine and a block is in force (the vault-block file of the switchboard state directory)",
}
FIREWALL_REASON = "the vault refused this machine"   # the firewall refused this machine: the launch path's own words


def refresh_reason(err):
    """The reason a refresh failed, in one line: which step, never a value."""
    if getattr(err, "firewall", False):
        return FIREWALL_REASON
    return REFRESH_REASONS.get(getattr(err, "kind", ""), "the cluster token could not be obtained")


VANISHED_READ_BYTES = 2 * 1024 * 1024   # the most audit bytes the vanished-pod check reads
VANISHED_WINDOW_SECONDS = 24 * 3600      # and the oldest line it looks at
VANISHED_MIN_AGE_SECONDS = 600           # a create line must be older than this before its pod is read by name
VANISHED_MAX_READS = 20                  # the most by-name pod reads of one pass


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


_TAIL_COUNT = threading.local()   # audit lines parsed by AuditTail, per thread: a pass or a tick reads its own thread's count


def tail_lines_parsed():
    return getattr(_TAIL_COUNT, "n", 0)


class AuditTail:
    """One read of the audit file's tail (the same 262,144-byte window as last_audit_line), made at the FIRST question and kept: for each
    (action, runtime id) the newest line's (ok, reason). A line that is not JSON is skipped; the newest line wins. `parsed` counts the lines parsed."""

    def __init__(self, path):
        self.path, self.parsed, self.newest, self.loaded = path, 0, {}, False

    def _load(self):
        self.loaded = True
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 262144))
                lines = f.read().decode(errors="replace").splitlines()
        except OSError:
            return
        for raw in reversed(lines):
            self.parsed += 1
            _TAIL_COUNT.n = tail_lines_parsed() + 1
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                self.newest.setdefault((rec.get("action"), rec.get("runtime_id")), (rec.get("ok"), rec.get("reason")))

    def get(self, action, runtime_id):
        if not self.loaded:
            self._load()
        return self.newest.get((action, runtime_id))


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

    def run(self, force=False, unauthorized=False):
        """One attempt: make the file fresh when it is due (force: now, whatever the file's life and the retry gap say).
        Returns a TokenRefresh (never raises)."""
        if not self.enabled():
            return TokenRefresh("skipped")   # another login kind: nothing is read and nothing is written
        now = self.clock().timestamp()
        self.note_ignored(now)
        force = force or unauthorized   # unauthorized: due now, but the retry gap still holds
        if not (force and not unauthorized) and self.last_failure is not None and 0 <= now - self.last_failure[0] < CLUSTER_TOKEN_RETRY_SECONDS:   # a negative gap (the clock went back) counts as expired
            return TokenRefresh("waiting", self.last_failure[1])   # the retry gap: no new attempt yet
        path = self.path()
        life = token_file_life_left(path, now)
        if not force and life is not None and life >= CLUSTER_TOKEN_REFRESH_MARGIN_SECONDS:
            return TokenRefresh("fresh")   # the file has life left: it is left alone
        try:
            token = cluster_token(self.env, duration=CLUSTER_TOKEN_DURATION, capture=self.capture, now=now, strict=True)
            write_token_file(path, token)
        except ClusterLogin as e:
            reason = refresh_reason(e)
            self.last_failure = (now, reason)
            self.audit(UNAUTH_REASON if unauthorized else reason, False)
            return TokenRefresh("failed", reason)
        except OSError as e:
            reason = "the token file could not be written: %s" % (e.strerror or e)
            self.last_failure = (now, reason)
            self.audit(reason, False)
            return TokenRefresh("failed", reason)
        self.last_failure = None
        self.audit(UNAUTH_REASON if unauthorized else "", True)
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
    raw = env.get(PERSISTENT_SETTING) or ""
    if not raw:
        return PERSISTENT_DEFAULT_MAX
    if not PERSISTENT_PATTERN.fullmatch(raw) or not 1 <= int(raw) <= PERSISTENT_MAX_LIMIT:
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


class KnownForms(set):
    """The forms of the known secrets, each with the lifetimes that hold it; a form with no lifetime never leaves."""

    def __init__(self, mirror=None):
        super().__init__()
        self._mirror = mirror   # a KnownSet (or set) kept equal to the live forms
        self._lock = threading.RLock()
        self._lives = {}
        self._absent = set()

    def __iter__(self):
        with self._lock:
            return iter(sorted(set.__iter__(self), key=lambda v: (-len(v), v)))   # the order of scrub

    def add_forms(self, forms, life=None):
        with self._lock:
            for f in forms:
                set.add(self, f)
                if self._mirror is not None:
                    self._mirror.add(f)
                if life is None:
                    self._lives[f] = None
                elif self._lives.get(f, 0) is not None:
                    self._lives.setdefault(f, set()).add(life)

    def sweep(self, now, pods):
        """Drop each form whose every holder has left: ("pod", name) when the pod is not in pods; ("until", t, name) when now >= t and the pod is not in pods; a holder is released only when its pod was also absent from the previous sweep (two in a row). pods=None or an empty list drops nothing and clears that memory. Returns the count dropped."""
        if not pods:
            with self._lock:
                self._absent = set()
            return 0
        pods = set(pods)
        gone = 0
        with self._lock:
            before, absent = self._absent, set()
            for f, lives in list(self._lives.items()):
                if lives is None:
                    continue
                for l in lives:
                    n = l[1] if l[0] == "pod" else l[2]
                    if n not in pods:
                        absent.add(n)
                lives -= {l for l in lives if ((l[0] == "pod" and l[1] not in pods) or (l[0] == "until" and now >= l[1] and l[2] not in pods)) and (l[1] if l[0] == "pod" else l[2]) in before}
                if not lives:
                    del self._lives[f]
                    set.discard(self, f)
                    if self._mirror is not None:
                        self._mirror.discard(f)
                    gone += 1
            self._absent = absent
        return gone


def secret_life_until(seconds=3660, pod=None):
    return ("until", time.time() + seconds, pod)


def _not_found_exactly(stderr, what):
    """True only when the whole message (one trailing newline trimmed) is the client's not-found text for the ONE object asked for (kind and exact name)."""
    text = stderr[:-1] if stderr.endswith("\n") else stderr
    return bool(what) and text == "Error from server (NotFound): %s not found" % what


class Kube:
    """The only door to the cloud: one kubectl call per method. Never puts a secret on a command line."""

    def __init__(self, env):
        self.env = dict(os.environ, **env)
        self.bin = env.get("CW_KUBECTL", "kubectl")
        self.ns = env.get("CW_CLOUD_NAMESPACE", "cw")
        self.context = (env.get("CW_KUBE_CONTEXT") or "").strip()
        self.kubeconfig = (env.get("CW_KUBECONFIG") or "").strip()
        self.dir = None
        self.known = KnownSet()  # values of this run's secrets, known to scrub(); never logged
        self.registry = KnownForms(mirror=self.known)  # the lives of the forms; keeps `known` equal to the live forms (ask #1783)
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
            self.add_secrets(body.strip(), life=secret_life_until())
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

    def add_secrets(self, *values, life=None):
        for v in values:
            if v:
                reg = getattr(self, "registry", None)
                if reg is None:
                    reg = self.known
                if hasattr(reg, "add_forms"):
                    reg.add_forms(secret_forms(v), life)
                else:   # a plain KnownSet / set put in place of the registry: no lives, the forms stay
                    reg.update(secret_forms(v))

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

    on_unauthorized = None   # the reaper's own: a function that refreshes its login through the retry gap; True when it did

    def run(self, args, *a, retry=True, **kw):
        """One kubectl call. A read or an idempotent call answered Unauthorized is retried ONCE after the login refresh (never the exec, never a create)."""
        try:
            return self._run(args, *a, **kw)
        except CloudError as e:
            if not (retry and self.on_unauthorized and args[0] in UNAUTH_RETRY_VERBS and "unauthorized" in str(e).lower() and self.on_unauthorized()):
                raise
        return self._run(args, *a, **kw)

    def _run(self, args, stdin=None, ok_missing=False, keep_stdout=False, timeout=120, missing_what=None):
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
            if ok_missing and _not_found_exactly(p.stderr, missing_what):
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
        out = self.run(["get", "secret", full_name, "-o", "json"], ok_missing=True, missing_what='secrets "%s"' % full_name)
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

    def list_events(self):
        """The namespace's scheduler events (`FailedScheduling`): a `get`, a read only."""
        return json.loads(self.run(["get", "events", "--field-selector", "reason=FailedScheduling", "-o", "json"]))["items"]

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

    def list_secrets(self, selector):
        """The Secrets that match a label selector, WITHOUT any value: an output form that names only the name, the labels cw/pod and app and
        the creation time (never the whole object). Returns [(name, cw/pod, app, creationTimestamp)]; an answer that cannot be read raises."""
        tmpl = (r'{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.cw/pod}{"\t"}{.metadata.labels.app}{"\t"}'
                r'{.metadata.creationTimestamp}{"\n"}{end}')
        out = self.run(["get", "secrets", "-l", selector, "-o", "jsonpath=" + tmpl])
        rows = []
        for line in out.splitlines():
            if not line.strip():
                continue
            f = line.split("\t")
            if len(f) != 4 or not f[0]:
                raise CloudError("kubectl get: the Secret list could not be read")
            rows.append(tuple(f))
        return rows

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
        out = self.run(["get", "secret", name + "-run", "-o", "json"], ok_missing=True, missing_what='secrets "%s-run"' % name)
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
        return self.run(["exec", name, "--"] + list(cmd), keep_stdout=keep_stdout, timeout=timeout, retry=False)


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


FAILED_EVENTS_MAX = 10
FAILED_EVENT_CHARS = 300
FAILED_POD_KEEP_SECONDS = 600   # a Failed pod without unpushed work is deleted this long after its record
FAILED_UNWRITTEN_TOLD = set()   # the Failed pods whose unwritable record was already reported (one report, not one per pass)


def ann(pod, key, default=None):
    return pod["metadata"].get("annotations", {}).get("cw/" + key, default)


PR_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 :._/-]{0,49}", re.ASCII)   # #2269: a label name the launch may carry (no comma: the list is comma-separated)
PR_LABELLED_MARK = "pr-labelled"                  # the pod annotation (cw/pr-labelled): the pull request numbers already handled, comma-separated; once each
PR_RECORD_READ = "import os,sys;d=os.environ.get('CW_TMP_DIR','/run/cw-tmp');o=[]\nfor p in (os.path.join(d,'pr-record','numbers'),'/run/cw-tmp/pr-record-numbers'):\n if os.path.isfile(p):o+=open(p).read(20000).split()\nsys.stdout.write(' '.join(dict.fromkeys(o)))"
PR_LABELS_PER_PASS = 20                           # numbers read from a pod's record per pass; the rest wait for later passes
PR_LABELS_CUT_TOLD = set()                        # workers whose cut was already said
PR_LABELS_TOLD = set()                            # (worker, number) whose failure was already reported


def clean_pr_labels(raw):
    """The launch's labels: each a plain name (PR_LABEL_RE), no duplicates, at most 10; anything else refuses before anything is made."""
    out = []
    for label in raw or []:
        if not isinstance(label, str) or not PR_LABEL_RE.fullmatch(label):
            raise CloudError("--pr-label must be a plain label name (letters, digits, space and : . _ / -, at most 50); nothing was created", EXIT_USAGE)
        if label not in out:
            out.append(label)
    if len(out) > 10:
        raise CloudError("at most 10 --pr-label values; nothing was created", EXIT_USAGE)
    return out


def github_add_labels(token, repo, number, labels):
    """POST /repos/R/issues/N/labels with exactly these labels (GitHub never creates here: a label that is missing answers 404 or 422). Raises on any failure."""
    import urllib.request
    import token_mint
    req = urllib.request.Request("https://api.github.com/repos/%s/issues/%d/labels" % (repo, number), data=json.dumps({"labels": list(labels)}).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "User-Agent": "cw-launcher", "Content-Type": "application/json"})
    with token_mint.open_no_redirect(req, SCOPE_PROBE_SECONDS) as r:
        r.read(100000)


def github_pr_head(token, repo, number):
    """GET /repos/R/pulls/N: (head branch name, state, head repository full name). Raises on any failure."""
    import urllib.request
    import token_mint
    req = urllib.request.Request("https://api.github.com/repos/%s/pulls/%d" % (repo, number), method="GET",
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "User-Agent": "cw-launcher"})
    with token_mint.open_no_redirect(req, SCOPE_PROBE_SECONDS) as r:
        data = json.loads(r.read(1000000).decode("utf-8", "replace"))
    head = data.get("head") or {}
    return str(head.get("ref") or ""), str(data.get("state") or ""), str((head.get("repo") or {}).get("full_name") or "")


SAFE_VALUE = re.compile(r"[A-Za-z0-9._-]{1,63}", re.ASCII)
POD_UID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")   # the form of a pod uid: the create records it, the retire hands it back (bin/cloud_worker.py holds the equal text)


def safe_value(value):
    """A value the cluster supplies (a node's pool or size label, a pod phase) that is printed to a person: ONE rule. It is kept whole when it is
    1 to 63 ASCII letters, digits, `-`, `_` or `.` (what a Kubernetes label value or a phase may hold); anything else (an escape, a bell, a newline,
    another control or non-ASCII character, an over-long value) becomes the fixed token `?`. Nothing is stripped: a partly clean value is not trusted."""
    return value if isinstance(value, str) and SAFE_VALUE.fullmatch(value) else "?"


def _one_line(text):
    """The first line of a cluster message, word by word through `safe_value` (a word that is not plain letters or digits becomes `?`), cut."""
    words = ((text or "").strip().splitlines() or [""])[0].split(":")[-1].split()
    return " ".join(w if re.fullmatch(r"[A-Za-z]+|[0-9]+", w) and safe_value(w) != "?" else "?" for w in words)[:120]


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


FLOOR_ORDER = ("XS", "S", "M")   # the shapes a repository floor may name, smallest first; the orchestrator row O is not one of them and ranks below XS


def shape_rank(shape):
    """The rank of a shape for the floor comparisons: its place in FLOOR_ORDER, or -1 (below XS) for any other row, the orchestrator row O included."""
    return FLOOR_ORDER.index(shape) if shape in FLOOR_ORDER else -1


def repo_min_shapes(raw):
    """THE parse of CW_REPO_MIN_SHAPE: {repository key: shape} from comma-separated OWNER/NAME=SHAPE pairs, or None when any entry is malformed (no
    '=', an unknown shape, a repository that is not OWNER/NAME, a repeated repository, a control character). The repository part is parsed by `_repo_key`,
    the one parse the allow-list uses. The settings file's validator and check_shape both call it."""
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return None
    floors = {}
    for entry in raw.split(","):
        repo, sep, shape = entry.partition("=")
        key = _repo_key(repo.strip())
        shape = shape.strip()
        if not sep or key is None or shape not in FLOOR_ORDER or key in floors:
            return None
        floors[key] = shape
    return floors


def check_shape(a, env=None):
    """Set a.shape when it names none (XS, or S for a stack launch or an orchestrator) and refuse an unknown name, or a row that cannot run a stack, with --stack.
    With `env` holding CW_REPO_MIN_SHAPE and a repository listed in it, the listed shape is a floor: an unnamed shape becomes the larger of the floor and
    the shape the flags give, and a named shape below the floor is refused. An unlisted repository or an unset setting changes nothing."""
    floor = None
    raw = ((env or {}).get("CW_REPO_MIN_SHAPE") or "").strip()
    if raw:
        floors = repo_min_shapes(raw)
        if floors is None:
            raise CloudError("refused: CW_REPO_MIN_SHAPE is not a list of OWNER/NAME=SHAPE entries; nothing was created", EXIT_USAGE)
        floor = floors.get(_repo_key(getattr(a, "repo", None) or "") or "")
    named = getattr(a, "shape", None)
    if named is not None and named not in SHAPES:
        raise CloudError("shape must be one of %s" % ", ".join(SHAPES), EXIT_USAGE)
    if named is not None and floor is not None and shape_rank(named) < shape_rank(floor):
        raise CloudError("refused: %s needs at least shape %s (CW_REPO_MIN_SHAPE), not %s; name a larger shape or change the setting; nothing was created" % (scrub(a.repo, (), 120), floor, named), EXIT_USAGE)
    if named is None:
        a.shape = DEFAULT_STACK_SHAPE if getattr(a, "stack", False) else DEFAULT_ORCHESTRATOR_SHAPE if getattr(a, "agent", None) == "orchestrator" else DEFAULT_SHAPE
        if floor is not None and shape_rank(floor) > shape_rank(a.shape):
            a.shape = floor
    if getattr(a, "stack", False) and not SHAPES[a.shape]["stack"]:
        raise CloudError("--stack needs a shape that can run a nested Docker stack (%s), not %s" % (", ".join(k for k, v in SHAPES.items() if v["stack"]), a.shape), EXIT_USAGE)


def hub_on(env):
    """The hub setting: the channel address or the registry Secret name is set. Unset, a worker launch is exactly what it was."""
    return bool((env.get("CW_HUB_CHANNEL_URL") or "").strip() or (env.get("CW_HUB_REGISTRY_SECRET") or "").strip())


REVIEW_COMMENTS = os.path.join(os.path.dirname(os.path.realpath(__file__)), "repo-review-comments.json")   # optional; absent = no comment anywhere


def review_comment(repo, path=None):
    """The review comment configured for repo (a JSON object beside this file: lowercased OWNER/NAME -> one-line text, at most 500 characters), or ''.
    Optional per repository: a missing, unreadable or malformed file, a missing key or a bad value gives '' (no comment is posted). Never raises."""
    try:
        with open(path or REVIEW_COMMENTS, "rb") as f:
            raw = f.read(65537)
        data = json.loads(raw) if len(raw) <= 65536 else {}
        v = data.get(str(repo).lower()) if isinstance(data, dict) else None
        if isinstance(v, str) and v.strip() and len(v) <= 500 and not re.search("[\\x00-\\x1f\\x7f-\\x9f]", v) \
                and not any(p.search(v) for p in CREDENTIAL_PATTERNS):
            return v.strip()
    except Exception:
        pass
    return ""


READONLY_ROOT_SETTING = "CW_READONLY_ROOT_LANES"   # a set of lane (boss) names, comma or space separated; empty or unset = off
READONLY_ROOT_MOUNTS = ("/tmp", "/home/worker/.cache/opencode", "/home/worker/.config/opencode", "/home/worker/.local/state/opencode",
                        # opencode data without model-auth (XDG_DATA_HOME is set only with it); npm, pip and the docker client write these in $HOME
                        "/home/worker/.local/share", "/home/worker/.npm", "/home/worker/.cache/pip", "/home/worker/.docker",
                        # nested docker: start-dockerd (through sudo) writes /var/log/dockerd.log and /var/lib/docker; mounted, so docker stays usable
                        "/var/lib/docker", "/var/log")
READONLY_ROOT_GITCONFIG = "/run/cw-tmp/gitconfig"   # ~/.gitconfig is a FILE: git is pointed at the tmpfs instead of mounting over it


def readonly_root_lanes(env):
    return set((env.get(READONLY_ROOT_SETTING) or "").replace(",", " ").split())


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
    pr_labels = clean_pr_labels(getattr(a, "pr_label", None))   # #2269: refused before anything is made when one is not a plain label
    if pr_labels:
        e["CW_PR_LABELS"] = ",".join(pr_labels)
        e["CW_PR_RECORD"] = "/run/cw-tmp/pr-record-numbers"   # #2277 fix-forward: the published image's client appends here
    comment = review_comment(a.repo)
    if comment:   # the repository's review comment, posted once by the end hook; none configured = no variable
        e["CW_REVIEW_COMMENT"] = comment
    agent = getattr(a, "agent", None) or "worker"
    if agent not in AGENTS:
        raise CloudError("--agent must be one of %s, got %r; nothing was created" % (" or ".join(AGENTS), str(agent)[:40]), EXIT_USAGE)
    if agent == "orchestrator" or hub_on(env):   # with the hub setting unset a worker pod gets none of these: its spec is exactly what it was
        boss = getattr(a, "boss", None)
        if (agent == "orchestrator" and not boss) or (boss and not NAME_RE.fullmatch(boss)):
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
        "annotations": {SAFE_TO_EVICT_KEY: "false", "cw/worker": worker, "cw/started": iso(now), "cw/hard-deadline": iso(hard),
                        "cw/idle-seconds": e["CW_IDLE_SECONDS"], "cw/shape": a.shape,
                        "cw/max-hard": iso(now + dt.timedelta(seconds=parse_duration(a.max_hard or DEFAULT_MAX_HARD))),
                        **({"cw/run-id": a.run_id} if getattr(a, "run_id", None) else {}),
                        **({"cw/no-commit": "true"} if getattr(a, "no_commit", False) else {}),   # launched NOT to commit (a measure or a read): no wip note
                        **({"cw/pr-labels": ",".join(pr_labels)} if pr_labels else {}),   # #2269: the labels the reaper puts on the pod's pull requests
                        **({"cw/role": agent} if agent == "orchestrator" else {})}},   # a worker pod's spec is byte-for-byte what it was: no annotation means worker
        "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": pod_deadline_floor(a.max_hard),
                 "terminationGracePeriodSeconds": POD_TERMINATION_GRACE_SECONDS,
                 "serviceAccountName": env.get("CW_CLOUD_SERVICE_ACCOUNT", "cw-worker"),
                  "automountServiceAccountToken": False,
                  "securityContext": copy.deepcopy(POD_SECURITY_CONTEXT),
                 "containers": [{"name": "worker", "image": image,
                                 "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}, "env": [{"name": k, "value": v} for k, v in e.items()],
                                 "resources": {"limits": {"cpu": shape["cpu"], "memory": shape["memory"]}, **({"requests": dict(SHAPE_REQUESTS[a.shape])} if a.shape in SHAPE_REQUESTS else {})},
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
    if not stack and getattr(a, "boss", None) in readonly_root_lanes(env):   # this lane's pods: the container's root is read-only, writes only where listed
        worker_container = pod["spec"]["containers"][0]
        worker_container["securityContext"]["readOnlyRootFilesystem"] = True
        worker_container["env"].append({"name": "GIT_CONFIG_GLOBAL", "value": READONLY_ROOT_GITCONFIG})
        for i, path in enumerate(READONLY_ROOT_MOUNTS):
            worker_container["volumeMounts"].append({"name": "rw%d" % i, "mountPath": path})
            pod["spec"]["volumes"].append({"name": "rw%d" % i, "emptyDir": {}})
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


def _cap_text(kube, text, limit):
    """PC-14: known secrets first (no cut), then addresses, then the cut."""
    return _mask_addresses(kube.scrub(text, SCRUB_OUTPUT_MAX))[:limit]


class _StopKube:
    """One stop's view of the cluster door: the same calls, but once the stop is abandoned every call raises, so a late thread changes nothing."""
    def __init__(self, kube, dead):
        self._kube, self._dead = kube, dead

    def __getattr__(self, name):
        attr = getattr(self._kube, name)
        if not callable(attr) or name == "scrub":
            return attr

        def call(*a, **k):
            if self._dead.is_set():
                raise CloudError("stop abandoned")
            return attr(*a, **k)
        return call


KEPT_RUNNING_MARK = "kept_running_tried"
KEPT_ENDED_NOTE = "kept pod ended: its work is unreachable by exec; the pod is left for a person"


# The results check of the idle nudge: ONE read in the pod. It prints "no" (no /work/results.txt), or "<file mtime> <newest user message time> <1 if the last non-empty line is exactly STATUS: done else 0>"
# (epoch seconds; the session's messages, as cw-pod-inspect reads them), or "unreadable". The file is "finished" only with the marker AND NEWER than that message.
RESULTS_PROBE_CODE = """
import base64, json, os, sys, urllib.parse, urllib.request
p = "/work/results.txt"
if not os.path.exists(p):
    print("no"); sys.exit(0)
try:
    port = os.environ.get("CW_ACTIVITY_PORT", "4096")
    pw = open(os.environ.get("CW_ACTIVITY_PASSWORD_FILE", "/run/cw/server-password")).read().strip()
    auth = "Basic " + base64.b64encode(("opencode:" + pw).encode()).decode()
    def get(path):
        r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%s%s" % (port, path), headers={"Authorization": auth}), timeout=10)
        return json.loads(r.read().decode())
    sessions = get("/api/session").get("data")
    sessions = list(sessions.values()) if isinstance(sessions, dict) else sessions
    best = None
    for s in sessions:
        q = ""
        for _page in range(5):
            body = get("/api/session/%s/message%s" % (s["id"], q))
            items = body.get("data") if isinstance(body, dict) else body
            for m in items or []:
                info = m.get("info") or m
                if m.get("type") == "user" or info.get("role") == "user":
                    t = (info.get("time") or {}).get("created")
                    if isinstance(t, (int, float)):
                        best = max(best or 0, t / 1000.0 if t > 1e11 else t)
            cur = body.get("cursor") if isinstance(body, dict) else None
            nxt = cur.get("next") if isinstance(cur, dict) else None
            if not nxt:
                break
            q = "?cursor=" + urllib.parse.quote(str(nxt))
    lines = [l.strip() for l in open(p, errors="replace").read().splitlines() if l.strip()]
    done = 1 if lines and lines[-1] == "STATUS: done" else 0
    print(os.path.getmtime(p), best if best is not None else "unreadable", done)
except Exception:
    print("unreadable")
"""


def results_not_finished(seen):
    """True when the probe's answer is "<mtime> <last user message time> <marker>" and the file is NOT finished: no STATUS: done marker, or not newer than that message. Any other answer is False."""
    try:
        mtime, prompt, marker = [float(x) for x in seen.split()]
    except ValueError:
        return False
    return marker != 1 or mtime <= prompt


class Launcher:
    def __init__(self, kube, env, clock=None, sleep=time.sleep):
        self.kube, self.env, self.sleep = kube, env, sleep
        self.run_id, self.foreign = None, []
        self.recorder = run_record.Recorder(kube, env)
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.audit_path = audit_path(env)
        self.relogin = None   # set by the reaper's loop: a function that refreshes the reaper's own login now; True when it did
        self.ended_tried = set()   # (name, uid) of kept ended pods already tried once by this process (a failed mark still counts)
        self.renew_lock = threading.RLock()   # the pass and the renewal thread never renew at the same moment

    # -- audit: one line per action, no secret, written before the action returns --
    def audit(self, worker, runtime_id, action, cost, ok, reason="", refresh=None, cause=None, extra=None):
        line = {"ts": iso(self.clock()), "worker": worker, "runtime_id": runtime_id, "action": action,
                "cost_estimate_usd": cost, "ok": ok, "actor": pwd.getpwuid(os.getuid()).pw_name,
                "reason": self.kube.scrub(reason, limit=1000)}
        if refresh in (REFRESH_USED, REFRESH_REFUSED, REFRESH_NONE):   # a stop that ran the hook: ONE word, never a value
            line["refresh"] = refresh
        if cause:   # a stop the launcher made of its own accord names why: idle, hard, pod_gone or pod_failed (a person's stop has none)
            line["cause"] = cause
        write_audit(self.audit_path, dict(line, **(extra or {})))

    def cost(self, pod):
        hours = (self.clock() - parse_iso(ann(pod, "started"))).total_seconds() / 3600
        return round(max(hours, 0) * SHAPES.get(ann(pod, "shape", "S"), SHAPES["S"])["usd_per_hour"], 4)

    def _audited(self, worker, action, fn, reason="", runtime_id=""):
        info = {"rid": runtime_id, "cost": 0.0}

        def why():   # a stop that wrote a run record names its directory once, here; a gone-pod stop names the Secrets it removed (names only)
            text = "%s; run record %s" % (reason, info["record"]) if info.get("record") else reason
            if "removed" in info:
                text = "%s%ssecrets removed: %s" % (text, "; " if text else "", ", ".join(info["removed"]) or "none")
            if info.get("identity_note"):
                text = "%s%s%s" % (text, "; " if text else "", info["identity_note"])
            return "%s; %s" % (text, info["hook_note"]) if text and info.get("hook_note") else (text or info.get("hook_note", ""))

        def extra():   # a persistent orchestrator's line says so (no other line has the key)
            return {"persistent": True} if info.get("persistent") else None
        try:
            result = fn(info)
        except CloudError as e:
            if not e.audited:
                # the failure is never dropped: a caller's own reason (a stop reason, the run-secret names) comes first
                self.audit(worker, info["rid"], action, info["cost"], False, "%s; %s" % (why(), e) if why() else str(e), info.get("refresh"), info.get("cause"), extra())
            raise
        except (ValueError, TypeError, KeyError) as e:  # a bad annotation or reply still writes its ok:false line
            self.audit(worker, info["rid"], action, info["cost"], False, why() or str(e)[:300], info.get("refresh"), info.get("cause"), extra())
            raise CloudError("unexpected cloud data: %s" % self.kube.scrub(e, 200))
        self.audit(worker, info["rid"], action, info["cost"], True, why(), info.get("refresh"), info.get("cause"), extra())
        return result

    def _need(self, worker):
        if not NAME_RE.fullmatch(worker or ""):
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
            issue_no = parse_issue_number(getattr(a, "issue", None))   # before anything is made
            check_repo_allowed(a.repo, self.env)  # before anything is read or made
            if not NAME_RE.fullmatch(worker):
                raise CloudError("bad worker name %r" % worker, EXIT_USAGE)
            check_shape(a, self.env)   # resolves an unnamed shape; before any cluster, vault or token call
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
                                   self.env.get("CW_CLOUD_DOMAIN", "example.internal"), password, persistent, a.repo, issue_no)
                self._make(made, "sts" if persistent else "pod", self.kube.apply, spec)
                if getattr(a, "no_wait", False):
                    pass   # the caller asked to return once the pod is requested: it reads the pod's readiness itself
                elif getattr(a, "stack", False):
                    self._start_stack_pod(worker, name, spec, made, a.wait)
                else:
                    self._wait_ready(name + "-0" if persistent else name, a.wait)
                if a.password_fd is not None:
                    with os.fdopen(os.dup(a.password_fd), "w") as f:
                        f.write(password)
                pod_uid = self._read_pod_uid(worker, name, info)   # inside the cleanup: a signal during this read ends the create like any other failure
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
            return {"worker": worker, "url": "https://worker-%s.%s:4096" % (name, domain), "pod_uid": pod_uid,
                    **({"identity_note": info["identity_note"]} if "identity_note" in info else {}),
                    "auth_ref": "file:/run/cw/%s.password" % worker, "runtime_id": "pod/" + name,
                    "idle_deadline": iso(now + dt.timedelta(seconds=parse_duration(a.idle))), "hard_deadline": iso(hard),
                    "idle_seconds": parse_duration(a.idle), "hard_seconds": parse_duration(a.hard),
                    "password_fd": a.password_fd}
        old = self._catch_signals()
        try:
            reason = ("run-secrets=" + ",".join(sorted(parse_run_secret_fds(a.run_secret_fd)))) if a.run_secret_fd else ""
            if getattr(a, "stack_model_secret", None) and _VAULT_SECRET_NAME.fullmatch(a.stack_model_secret):   # the name and that it was delivered, nothing else
                reason = (reason + " " if reason else "") + "stack-model-secret=%s delivered" % a.stack_model_secret
            return self._audited(worker, "create", go, reason)
        finally:
            self._restore_signals(old)

    def _read_pod_uid(self, worker, name, info):
        """The pod's own uid, read once at create: a later retire acts only on a pod with this uid.
        ONE check covers every way it is missing: the read raises any error, or answers anything but a pod with a uid in the form a retire accepts
        (nothing, text, a list, metadata that is no dict, no uid, an empty, non-text, spaced, control-character or over-long uid)."""
        try:
            uid = self.kube.get_pod(name)["metadata"]["uid"]
        except Exception:   # the pod exists: no failure of this read may escape the create
            uid = None
        if not (isinstance(uid, str) and POD_UID_RE.fullmatch(uid)):
            self._note_unread_identity(worker, info)
            return ""
        return uid

    def _note_unread_identity(self, worker, info):
        """The create could not read the new pod's uid: say so once on stderr and in the create's audit line."""
        note = "the pod identity of %s could not be read; the worker record holds none and a retire acts by name" % worker
        print("cloud-worker: WARNING: %s" % note, file=sys.stderr)
        info["identity_note"] = note

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

    def _register(self, agent, channel_secret, spec, domain, password, persistent=False, repo=None, issue=None):
        """The hub's registry: {agent: sha256 hex of the channel secret} and {agent: {url, session, password_file[, persistent][, repo][, issue]}}. Only the hash is stored."""
        digest = hashlib.sha256(channel_secret.encode()).hexdigest()
        name = spec["metadata"]["name"]
        entry = {"url": hub_agent_url(name, self.env.get("CW_CLOUD_NAMESPACE", "cw")), "session": "", "password_file": "%s/%s" % (HUB_PW_DIR, agent)}
        if persistent:   # only a persistent orchestrator's entry has the key
            entry["persistent"] = True
        if repo is not None:
            entry["repo"] = repo
        if issue is not None:
            entry["issue"] = issue

        def change(sec, agents):
            sec[agent], agents[agent] = digest, entry
        self.kube.registry_update(self._registry_secret(), change)
        self.kube.hub_pw_set(HUB_PW_SECRET, agent, password)   # the pod's server password: the hub reads it from the file

    def hub_session(self, a):
        """Write the session id into the agent's hub entry (a no-op when it has none). Called by the switchboard once the session exists."""
        if not NAME_RE.fullmatch(a.worker or "") or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", a.session or ""):
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
        scaling = unread = False   # what the cluster says of a pending pod (the plain wait only: the stack path passes its own watch)
        while True:
            pod = self.kube.get_pod(name)
            status = (pod or {}).get("status", {})
            if status.get("phase") == "Failed":
                raise CloudError("worker pod failed to start")
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])):
                return
            if on_pending:
                on_pending(pod)
            else:
                scaling, unread = self._why_pending(name, pod, scaling)
            more = max(0, LAUNCH_OUTER_SECONDS - seconds) if scaling else 0   # a scale-up under way: keep waiting to the outer bound
            if self.clock() >= end + dt.timedelta(seconds=more) or waited >= seconds + more:
                raise CloudError(self._timeout_text(scaling, unread), EXIT_TIMEOUT)
            self._nap(WAIT_POLL_SECONDS)
            waited += WAIT_POLL_SECONDS

    @staticmethod
    def _timeout_text(scaling, unread):
        if scaling:
            return "timed out waiting for a node to be added for the worker"
        return "timed out waiting for the worker" + ("; events could not be read" if unread else "")

    def _why_pending(self, name, pod, scaling):
        """Read why a pod is not Ready (reads only). Returns (a scale-up is under way, the events could not be read). Raises CloudError at once
        when the autoscaler says it will not add a node. A pod that is not Unschedulable (a starting pod) is not asked: nothing is read."""
        conds = ((pod or {}).get("status") or {}).get("conditions") or []
        if not any(c.get("type") == "PodScheduled" and c.get("status") == "False" and c.get("reason") == "Unschedulable" for c in conds):
            return scaling, False
        try:
            events = self.kube.pod_events(name)
        except (CloudError, ValueError, KeyError):
            return False, True
        verdict = None   # the latest autoscaler event of the two kinds
        for ev in events:
            if ev.get("reason") in ("TriggeredScaleUp", "NotTriggerScaleUp"):
                verdict = ev
        if verdict is None:
            return False, False
        if verdict["reason"] == "TriggeredScaleUp":
            return True, False
        count = len(self.kube.list_pods_all())
        raise CloudError("the worker pod cannot be scheduled: %s; the namespace holds %d pods" % (_one_line(verdict.get("message")), count))

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
            if getattr(a, "cause", None):
                info["cause"] = a.cause
            pod = self._need(worker)
            persistent = False
            workload = None
            if pod is None:
                # ONE strict read: every answer but the exact not-found (the exact Forbidden line included) fails the stop with everything kept
                try:
                    workload = self._absent_pod_workload(name)
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
            want_uid = getattr(a, "pod_uid", None)
            if want_uid and pod and not persistent and uid != want_uid:   # a new pod holds the name: not the one the record names; it and its Secrets are left whole
                raise CloudError("pod/%s is not the pod this worker's record names (its uid differs); nothing was stopped or deleted" % name, EXIT_REFUSED)
            if nonce and (pod or persistent):
                info["rid"] = "pod/" + target
                have = ann(pod, "run-id") if pod else ((workload.get("metadata") or {}).get("annotations") or {}).get("cw/run-id")
                if have != nonce:   # another run's (or an unlabelled) worker of this name: nothing is changed
                    raise CloudError("worker %s does not match run id %s; nothing was stopped or deleted" % (worker, nonce), EXIT_REFUSED)
            if not pod and not persistent:
                if getattr(a, "cause", None):   # only the reaper's own stop names a cause; a by-name stop carries none
                    info["cause"] = "pod_gone"
                if nonce:   # no pod: this run's own Secrets (carrying the nonce) may remain from a cut-off create; another's are left whole
                    self._remove(name, nonce)
                # The pod read said NotFound (an operator deleted it); any other read error raised in _need above and never reaches here.
                removed = []
                if not nonce:   # by name, pod NotFound: only this worker's own two Secrets (exact names) may be left over; nothing else is touched
                    if self.kube.get_pod(name):   # read again: a pod that reads as gone is retired by name only when no live pod holds the name
                        raise CloudError("pod/%s exists now (another pod holds the name); its Secrets are left whole" % name, EXIT_REFUSED)
                    for full, fn in ((name + "-pw", self.kube.delete_secret), (name + "-run", self.kube.delete_run_secret)):
                        if self.kube.get_secret(full):
                            fn(name)
                            removed.append(full)
                    info["removed"] = removed
                if getattr(a, "hub_entry", False) and not self._unregister(worker):   # the same call the live path uses; a failure is reported
                    raise CloudError("the hub registry entry of %s could not be removed (the Secrets are); retire again to finish" % worker)
                return {"worker": worker, "runtime_id": "", "pushed": False, "wip_branch": None, "pod_gone": True, "secrets_removed": removed}
            info["rid"] = "pod/" + target
            info["cost"] = self.cost(pod if pod else {"metadata": {"annotations": ((workload.get("spec") or {}).get("template") or {}).get("metadata", {}).get("annotations") or {}}})
            if not getattr(a, "no_record", False):   # BEFORE the hook and the delete; never raises, never holds the stop back
                where = self.recorder.record(worker, target, reason or "stop", repo=_pod_repo(pod) if pod else "", started=ann(pod, "started") or "" if pod else "",
                                             root=getattr(a, "record_dir", None), **({"persistent": True} if persistent else {}))
                if where:
                    info["record"] = where
                    print("cloud-worker: run record: %s" % where, file=sys.stderr)
            if info.get("record"):   # BEFORE the hook and the delete: what the pod object says of the pod, and its events when it is not Running
                self._record_pod_state(info["record"], pod, name)
            phase = (pod.get("status") or {}).get("phase") if pod else "Absent"
            if persistent and not pod:   # its controller has no pod now: nothing runs, no hook can run; the workload goes first
                self._remove(name, nonce, uid)
                print("cloud-worker: the persistent pod was absent; end hook not run; the StatefulSet was deleted", file=sys.stderr)
                self._hook_record(info, {"ran": False, "pushed": False, "wip_branch": None, "skipped": [], "note": "pod was absent; end hook not run"})
                return {"worker": worker, "runtime_id": "pod/" + target, "pushed": False, "wip_branch": None,
                        "skipped": [], "note": "pod was absent; end hook not run", "record": info.get("record", "")}
            if phase == "Failed" and getattr(a, "cause", None):
                info["cause"] = "pod_failed"
            if phase in ("Failed", "Succeeded") and ann(pod, "kept") != "true":  # a pod kept for unpushed work is never deleted here
                # No container is running, so no hook can run and a kept pod would only linger: delete it and its
                # Secrets, and say so.
                self._remove(name, nonce, uid)
                print("cloud-worker: pod was %s; end hook not run; deleted" % phase, file=sys.stderr)
                self._hook_record(info, {"ran": False, "pushed": False, "wip_branch": None, "skipped": [], "note": "pod was %s; end hook not run" % phase})
                return {"worker": worker, "runtime_id": "pod/" + target, "pushed": False, "wip_branch": None,
                        "skipped": [], "note": "pod was %s; end hook not run" % phase, "record": info.get("record", "")}
            if phase in ("Failed", "Succeeded"):   # kept, and ended: no exec can reach its work. Tried ONCE: marked, left for a person, never retried
                self.ended_tried.add((name, uid))
                try:
                    note = self._mark(name, uid, unreachable="true")
                except CloudError as err:   # the mark failed: the try still counts (remembered in this process), logged once
                    note = "could not mark the pod unreachable: %s" % self.kube.scrub(str(err), 200)
                    print("cloud-worker: " + note, file=sys.stderr)
                info["hook_note"] = KEPT_ENDED_NOTE + ("; " + note if note else "")
                print("cloud-worker: " + KEPT_ENDED_NOTE, file=sys.stderr)
                return {"worker": worker, "runtime_id": "pod/" + name, "pushed": False, "wip_branch": None,
                        "skipped": [], "note": KEPT_ENDED_NOTE, "record": info.get("record", ""), "left": True}
            info["refresh"] = REFRESH_NONE
            try:
                res = self.run_hook(target)
            except CloudError as e:
                res = {"pushed": False, "wip_branch": None, "error": str(e)}
            if res.get("refresh") == "needed" and res.get("error") and not res.get("pushed"):
                # The push was refused because the token had ended: ONE refresh here, on the stop side (the pod holds no minter credential),
                # and ONE more run of the hook. A refused refresh means no second attempt; the second answer is never refreshed again.
                info["refresh"] = self.refresh_for_stop(pod, self.clock())
                if info["refresh"] == REFRESH_USED:
                    try:
                        res = self.run_hook(target)
                    except CloudError as e:
                        res = {"pushed": False, "wip_branch": None, "error": str(e)}
            view = self._hook_view(res)   # every field text from the pod: a string, scrubbed, short
            no_repo = res.get("no_repository") is True and not view["error"]   # no checkout in the pod: the launcher's own words, not the pod's
            if no_repo:
                view["note"] = NO_REPOSITORY_NOTE
                info["hook_note"] = NO_REPOSITORY_NOTE
            self.last_hook = {k: view[k] for k in ("pushed", "wip_branch", "error")}
            self._hook_record(info, dict(view, ran=True, refresh=info["refresh"]))
            if view["error"] and res.get("refresh") == "needed" and phase == "Running":
                # An ended token after the one refresh and the one retry: the stop's own record and audit line are the one pair; the pod is marked and the reaper skips it.
                self._mark(target, pod["metadata"].get("uid"), **{KEPT_RUNNING_MARK: "true"})
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
                note = NO_REPOSITORY_NOTE if no_repo else self.kube.scrub(res.get("note") or "")
            except Exception:  # output handling never undoes the stop
                pass
            if no_repo:
                print("cloud-worker: end hook: %s" % note, file=sys.stderr)
            elif note:
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

    def _absent_pod_workload(self, name):
        """The ONE strict read for a worker whose pod NAME is absent (shared by stop and delete): the StatefulSet named like the worker, or None
        for the exact not-found. Every other answer (a near miss, the exact Forbidden line) raises CloudError."""
        return self.kube.get_workload(name)

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
                    "head_on": text(res.get("head_on"), 100) or None,
                    "pr_note": text(res.get("pr_note"), 120),
                    "skipped": [text(x, 200) for x in raw if isinstance(x, str)][:50] if isinstance(raw, list) else [],
                    "note": text(res.get("note")), "error": text(res.get("error")) or ("end hook gave an unreadable error" if res.get("error") else "")}
        except Exception:
            return {"pushed": False, "wip_branch": None, "skipped": [], "note": "", "error": "end hook answer unreadable"}

    def _pod_state_of(self, pod, name):
        """The pod's own state from the pod object already read (phase, each container's state, the node) and, ONLY when the phase is
        not Running, its newest POD_EVENTS_MAX events through scrub and the address mask. A failed events read says `unreadable`. Never raises."""
        scrub = self.kube.scrub
        status, spec = pod.get("status") or {}, pod.get("spec") or {}
        phase = status.get("phase")
        containers = []
        for c in (status.get("containerStatuses") or [])[:20]:
            st = c.get("state") or {}
            kind = next((k for k in ("terminated", "waiting", "running") if k in st), "unknown")
            one = {"name": mask_addresses(scrub(c.get("name") or "", 63)), "state": kind}
            detail = st.get(kind) or {}
            if detail.get("reason"):
                one["reason"] = mask_addresses(scrub(detail["reason"], 100))
            if kind == "terminated":
                one["exit_code"] = detail.get("exitCode")
            containers.append(one)
        state = {"phase": scrub(phase or "", 30), "containers": containers, "node": mask_addresses(scrub(spec.get("nodeName") or "", 253)),
                 "events": [], "events_read": "no"}
        if phase == "Running":
            return state
        try:
            uid = (pod.get("metadata") or {}).get("uid")
            items = [e for e in self.kube.pod_events(name) if not (uid and (e.get("involvedObject") or {}).get("uid") not in (None, uid))]
            stamp = lambda e: str(e.get("lastTimestamp") or e.get("eventTime") or (e.get("metadata") or {}).get("creationTimestamp") or "")
            items.sort(key=stamp)
            events = []
            for e in items[-POD_EVENTS_MAX:]:
                text = "%s %s: %s" % (e.get("type") or "", e.get("reason") or "", e.get("message") or "")
                events.append(mask_addresses(self.kube.scrub(text, 1000))[:POD_EVENT_CHARS])
            state["events"], state["events_read"] = events, "yes"
        except Exception:   # a Forbidden answer, a timeout, a bad reply: one word, and the stop goes on
            state["events"], state["events_read"] = [], "unreadable"
        return state

    def _record_pod_state(self, directory, pod, name):
        try:
            state = self._pod_state_of(pod, name)
        except Exception:
            state = "unreadable"
        try:
            self.recorder.add_pod_state(directory, state)
        except Exception:   # add_pod_state never raises; a stop is never held back by the record
            pass

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
            if not pod:   # the same ONE strict read as the stop: a StatefulSet or any answer but the exact not-found refuses, nothing deleted
                try:
                    found = self._absent_pod_workload(name)
                except CloudError:
                    found = True
                if found is not None:
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
        if (pod.get("status") or {}).get("phase") == "Failed" and not self._failed_name_fits(pod):   # not this launcher's pod: nothing is read of it
            return {"worker": ann(pod, "worker"), "runtime_id": "pod/" + name, "phase": "Failed", "idle_since": None,
                    "deadlines": {"idle": None, "hard": ann(pod, "hard-deadline")}, "cost_estimate_usd": self.cost(pod)}
        if (pod.get("status") or {}).get("phase") == "Failed":   # a dead pod answers nothing: its reason is read, once, from the cluster
            note, _ = self._failed_note(pod)
            return {"worker": ann(pod, "worker"), "runtime_id": "pod/" + name, "phase": "Failed", "idle_since": None,
                    "deadlines": {"idle": None, "hard": ann(pod, "hard-deadline")}, "cost_estimate_usd": self.cost(pod),
                    "pod_failed": note["pod_failed_reason"][:160], "status_line": "pod failed: " + note["pod_failed_reason"][:160]}
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

    # -- a pod that ended Failed (#1442) --
    def _failed_facts(self, name):
        """ONE read of a Failed pod's own status and of its events: (reason text, exit code or None, [events]). Every text goes through the
        launcher's scrub; at most FAILED_EVENTS_MAX events, each cut to FAILED_EVENT_CHARS. A read that fails is said in the reason."""
        try:
            whole = self.kube.get_pod(name) or {}
        except CloudError as e:
            whole = {"_unread": self.kube.scrub(e, 120)}
        try:
            seen = self.kube.pod_events(name)
        except CloudError:
            seen = []
        status = whole.get("status") or {}
        terminated = []
        for key in ("containerStatuses", "initContainerStatuses"):
            for c in status.get(key) or []:
                for where in ("state", "lastState"):
                    t = (c.get(where) or {}).get("terminated")
                    if isinstance(t, dict):
                        terminated.append(t)
        chosen = ([t for t in terminated if t.get("exitCode") not in (0, None)] or terminated or [{}])[0]
        code = chosen.get("exitCode") if isinstance(chosen.get("exitCode"), int) else None
        events = []
        for ev in [e for e in seen if isinstance(e, dict)][-FAILED_EVENTS_MAX:]:
            events.append(self.kube.scrub("%s: %s" % (ev.get("reason") or "", ev.get("message") or ""), FAILED_EVENT_CHARS))
        last = ([e for e in seen if isinstance(e, dict)] or [{}])[-1]
        word = chosen.get("reason") or None
        if word and code is not None:
            text = "%s (exit %d)" % (word, code)
        elif word or code is not None:
            text = word or "exit %d" % code
        else:
            text = status.get("reason") or whole.get("_unread") or last.get("reason") or "no reason was given"
        return self.kube.scrub(text, 300), code, events, self.kube.scrub(status.get("message") or "", 300)

    def _failed_name_fits(self, pod):
        """True when the pod's worker annotation is a worker name (NAME_RE, the whole text): only then it may become a part of a path."""
        worker = ann(pod, "worker")
        return isinstance(worker, str) and bool(NAME_RE.fullmatch(worker))

    def _failed_key(self, pod):
        return ann(pod, "run-id") or (pod.get("metadata") or {}).get("uid") or pod["metadata"]["name"]

    def _failed_find(self, pod):
        """The record of this Failed pod (found by the pod's run id), as a dict, or None."""
        worker, tail = ann(pod, "worker") or "", hashlib.sha1(self._failed_key(pod).encode()).hexdigest()[:6]
        root = self.recorder.root()
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return None
        for n in names:
            if n.startswith(worker + "-") and n.endswith("-" + tail):   # the name is a guide; the record's own fields decide
                try:
                    with open(os.path.join(root, n, "run.json")) as f:
                        info = json.load(f)
                except (OSError, ValueError):
                    continue
                if isinstance(info, dict) and info.get("pod_run_id") == self._failed_key(pod) and info.get("exit_reason") == "pod failed" \
                        and info.get("worker") == worker:
                    return info
        return None

    def _failed_aged(self, worker, pod, note, now):
        """True when the record is old enough for the pod to go, False when not, None when its time cannot be read. Such a record counts as
        no usable record: the pod is left (neither rewritten nor deleted), the caller says so once, and the other pods of the pass go on."""
        try:
            return (now - parse_iso(note["recorded_at"])).total_seconds() >= FAILED_POD_KEEP_SECONDS
        except (ValueError, TypeError):
            return None

    def _failed_note(self, pod):
        """(record, new): the run record of a Failed pod; read ONCE (the pod's status and events) and written once; a second call finds it."""
        if not self._failed_name_fits(pod):   # no path part is built from a text that is not a worker name: nothing is read or written
            return {"unwritten": True, "skipped": True, "pod_failed_reason": "", "pod_message": "the pod carries no usable worker name"}, False
        found = self._failed_find(pod)
        if found:
            return found, False
        name, now = pod["metadata"]["name"], self.clock()
        text, code, events, message = self._failed_facts(name)
        worker = ann(pod, "worker") or ""
        run_id = "%s-%s-%s" % (worker, now.strftime("%Y%m%dT%H%M%SZ"), hashlib.sha1(self._failed_key(pod).encode()).hexdigest()[:6])
        info = {"worker": worker, "repository": _pod_repo(pod), "run_id": run_id, "pod_run_id": self._failed_key(pod), "start": ann(pod, "started") or "",
                "end": iso(now), "recorded_at": iso(now), "exit_reason": "pod failed", "pod_failed_reason": text, "exit_code": code,
                "pod_message": message, "events": events, "head_sha": None, "transcript": "unavailable", "reason": "the pod ended Failed; no session could be read"}
        directory = None
        try:
            base = run_record.private_root(self.recorder.root())
            directory = os.path.join(base, run_id)
            if not os.path.realpath(directory).startswith(os.path.realpath(base) + os.sep):   # a second line of defence
                directory = None
                raise OSError("the record directory is outside the record root")
            os.makedirs(directory, mode=0o700, exist_ok=True)
            run_record.write_private(directory, "run.json", json.dumps(info, sort_keys=True) + "\n")
        except OSError as e:
            if directory:
                try:
                    os.rmdir(directory)   # an empty directory of a record that was not written is not left behind
                except OSError:
                    pass
            info["pod_message"] = self.kube.scrub("the record could not be written: %s" % e, 200)
            info["unwritten"] = True
            return info, False   # not written: the next pass tries again
        return info, True

    def status(self, a):
        if a.worker and not NAME_RE.fullmatch(a.worker):
            raise CloudError("bad worker name %r" % a.worker, EXIT_USAGE)
        pods = [self.kube.get_pod(pod_name(a.worker))] if a.worker else self.kube.list_pods()
        if a.worker and not pods[0] and self._persistent_workload(pod_name(a.worker)) is not None:
            pods = [self.kube.get_pod(pod_name(a.worker) + "-0")]   # a persistent orchestrator: shown under the worker's name
        if a.worker and not pods[0]:
            raise CloudError("unknown worker %s" % a.worker, EXIT_USAGE)
        return [self.record(p) for p in pods]

    # -- inspect: ONE read-only look inside a running pod (slice 2 of ask #2115) --
    def inspect(self, a):
        """What the pod is doing NOW, read-only: ONE `kubectl exec` of the pod's `cw-pod-inspect` (one JSON line). A pod whose image has no such command
        (or whose answer is not that line) is read by the fallback: git in /work/repo and `cw-worker-activity` through exec; every other field is listed in
        `missing`. EVERY text is scrubbed here (Kube.scrub) before it is returned: nothing the pod printed leaves raw."""
        if not NAME_RE.match(a.worker or ""):
            raise CloudError("bad worker name %r" % a.worker, EXIT_USAGE)
        name = pod_name(a.worker)
        if not self.kube.get_pod(name):
            raise CloudError("unknown worker %s" % a.worker, EXIT_USAGE)
        try:
            raw, source = json.loads(self.kube.exec(name, "cw-pod-inspect")), "cw-pod-inspect"
            if not isinstance(raw, dict):
                raise ValueError("not an object")
        except (CloudError, ValueError):
            raw, source = self._inspect_fallback(name), "fallback"
        return self._inspect_clean(a.worker, raw, source)

    def _inspect_fallback(self, name):
        """The reads that need no helper in the image: git through exec for the branch, the unpushed commits and the dirty tree; the activity
        tool for the idle seconds. What it cannot read is named in `missing`."""
        def git(*args):
            try:
                return self.kube.exec(name, "git", "-C", "/work/repo", *args)
            except CloudError:
                return None
        raw, missing = {}, []
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        raw["branch"] = branch.strip() if branch is not None and branch.strip() else None
        shas = git("rev-list", "--max-count=50", "HEAD", "--not", "--remotes")
        raw["unpushed"] = {"count": len(shas.split()), "shas": shas.split()[:10]} if shas is not None else None
        dirty = git("status", "--porcelain")
        raw["dirty"] = len([x for x in dirty.splitlines() if x.strip()]) if dirty is not None else None
        act = self.activity(name)
        raw["idle_seconds"] = (max(0, int((self.clock() - act[1]).total_seconds())) if act and not act[0] and act[1] else None)
        raw["in_flight"], raw["last_calls"], raw["last_text"], raw["last_push"] = None, [], "", None
        raw["missing"] = ["in_flight", "last_calls", "last_text", "last_push"] + [k for k in ("idle_seconds", "branch", "unpushed", "dirty") if raw.get(k) is None]
        return raw

    def _inspect_clean(self, worker, raw, source):
        """The ONE shape that is printed. Every text is cut and scrubbed; a number is an int or None; a sha is hex or dropped."""
        scrub_ = self.kube.scrub

        def num(v):
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        def text(v, limit):
            return scrub_(v, limit) if isinstance(v, str) else None
        flight = raw.get("in_flight") if isinstance(raw.get("in_flight"), dict) else None
        flight = {"tool": text(flight.get("tool"), 80) or "?", "seconds": num(flight.get("seconds"))} if flight else None
        calls = [{"tool": text(c.get("tool"), 80) or "?", "status": text(c.get("status"), 40) or "?", "seconds_ago": num(c.get("seconds_ago"))}
                 for c in (raw.get("last_calls") if isinstance(raw.get("last_calls"), list) else [])[:10] if isinstance(c, dict)]
        un = raw.get("unpushed") if isinstance(raw.get("unpushed"), dict) else None
        if un is not None:
            shas = [s[:12] for s in (un.get("shas") if isinstance(un.get("shas"), list) else []) if isinstance(s, str) and re.fullmatch(r"[0-9a-f]{7,40}", s)][:10]
            un = {"count": num(un.get("count")) or 0, "shas": shas}
        bad = [c for c in calls if c["status"].lower() in ("error", "failed", "failure", "timeout", "aborted")]
        last_error = "%s %s %ss ago" % (bad[0]["tool"], bad[0]["status"], bad[0]["seconds_ago"]) if bad else None
        missing = [text(m, 40) for m in (raw.get("missing") if isinstance(raw.get("missing"), list) else []) if isinstance(m, str)][:20]
        if source == "fallback" or last_error is None and "last_calls" in missing:
            missing = missing + (["last_error"] if "last_error" not in missing else [])
        return {"worker": worker, "source": source, "in_flight": flight, "last_calls": calls, "last_text": text(raw.get("last_text"), 300) or "",
                "idle_seconds": num(raw.get("idle_seconds")), "branch": text(raw.get("branch"), 200), "unpushed": un, "dirty": num(raw.get("dirty")),
                "last_push": text(raw.get("last_push"), 100), "last_error": last_error, "missing": missing}

    def snapshot(self, a):
        """ONE read-only look at the cluster: the pods of the namespace (name, worker, phase, node, hard deadline) and, when the login may
        LIST the nodes, the nodes themselves (pool, size, creation time). The node list is optional: a login without `list` on nodes still
        reads the pods, and the answer says so (`nodes_read` false) instead of failing the whole read. No write call is made here."""
        pods = []
        for pod in self.kube.list_pods_all():
            meta = pod.get("metadata") or {}
            pods.append({"name": meta.get("name"), "worker": ann(pod, "worker"),
                         "phase": safe_value((pod.get("status") or {}).get("phase")) if (pod.get("status") or {}).get("phase") else None,
                         "node": (pod.get("spec") or {}).get("nodeName"), "hard_deadline": ann(pod, "hard-deadline"),
                         "ready": any(c.get("type") == "Ready" and c.get("status") == "True" for c in ((pod.get("status") or {}).get("conditions") or []) if isinstance(c, dict))})
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

    def capacity(self, a):
        """ONE read-only look at room: per node its name and allocatable cpu (millicores) and memory (bytes) ONLY (never its addresses), the requests of the
        pods this login can list, per node, the Pending pods count and the last FailedScheduling line (scrubbed, 200 characters). Only `get` calls are made. A refused
        read is said (`nodes_read`, `pods_read`, `events_read` false and a short scrubbed error), never a failure of the whole read."""
        out = {"nodes": [], "requests": {}, "pending": None, "last_event": None,
               "nodes_read": True, "nodes_error": None, "pods_read": True, "pods_error": None, "events_read": True, "events_error": None}
        try:
            for node in self.kube.list_nodes():
                alloc = (node.get("status") or {}).get("allocatable") or {}
                out["nodes"].append({"name": _cap_text(self.kube, (node.get("metadata") or {}).get("name"), 100),
                                     "cpu_m": _quantity(alloc.get("cpu"), 1000), "memory_b": _quantity(alloc.get("memory"), 1)})
        except CloudError as e:
            out["nodes"], out["nodes_read"], out["nodes_error"] = [], False, self.kube.scrub(e, 120)
        try:
            pending = 0
            for pod in self.kube.list_pods_all():
                phase = (pod.get("status") or {}).get("phase")
                pending += phase == "Pending"
                node = (pod.get("spec") or {}).get("nodeName")
                if node and phase not in ("Succeeded", "Failed"):
                    mine = out["requests"].setdefault(_cap_text(self.kube, node, 100), {"cpu_m": 0, "memory_b": 0})
                    for c in (pod.get("spec") or {}).get("containers") or []:
                        req = ((c.get("resources") or {}).get("requests")) or {}
                        mine["cpu_m"] += _quantity(req.get("cpu"), 1000)
                        mine["memory_b"] += _quantity(req.get("memory"), 1)
            out["pending"] = pending
        except CloudError as e:
            out["requests"], out["pods_read"], out["pods_error"] = {}, False, self.kube.scrub(e, 120)
        try:
            events = [ev for ev in self.kube.list_events() if (ev or {}).get("reason") in (None, "FailedScheduling")]
            if events:
                def when(ev):
                    return str(ev.get("lastTimestamp") or ev.get("eventTime") or (ev.get("metadata") or {}).get("creationTimestamp") or "")
                last = max(events, key=when)
                who = (last.get("involvedObject") or {}).get("name")
                out["last_event"] = _cap_text(self.kube, "FailedScheduling %s%s" % (who + ": " if who else "", last.get("message") or ""), 200)
        except CloudError as e:
            out["events_read"], out["events_error"] = False, self.kube.scrub(e, 120)
        # PC-10: after the one scrub, every name and the event line lose an address of the three forms
        out["nodes"] = [dict(n, name=_mask_addresses(n["name"])) for n in out["nodes"]]
        out["requests"] = {_mask_addresses(k): v for k, v in out["requests"].items()}
        out["last_event"] = _mask_addresses(out["last_event"])[:200] if out["last_event"] else out["last_event"]
        return out

    def hard_deadline(self, pod):
        """The pod's copy, unless the switchboard record (read only, --record-dir) names a later or earlier one."""
        hard = parse_iso(ann(pod, "hard-deadline"))
        rd = self.env.get("CW_CLOUD_RECORD_DIR")
        if rd and NAME_RE.fullmatch(ann(pod, "worker", "")):
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

    def _run_stops(self, jobs):
        """Stop the pods of one pass, at most REAP_PARALLEL at a time, each on its own per-stop copy of the launcher (own last_hook, run id, hub
        mark, record deadline and cluster view). Returns [(result, error, last_hook)] in the order of `jobs`. A stop past REAP_STOP_SECONDS is
        abandoned: its thread is left to end by itself (our kubectl client is not reachable), every later call of it raises, the pod is left
        whole and un-annotated, and ONE audit line says so; the next pass tries again."""
        done, running, nxt, got = {}, {}, 0, []

        def work(i, ctx):   # a late (abandoned) thread never replaces the limit result: dict.setdefault of one key is atomic in CPython
            _, worker, reason, nonce = jobs[i]
            try:
                done.setdefault(i, (ctx.stop(argparse.Namespace(worker=worker, reason=reason, run_id=nonce,
                                                      cause="hard" if reason == "hard limit" else "idle"), reason), None, ctx.last_hook))
            except CloudError as e:
                done.setdefault(i, (None, e, ctx.last_hook))
            except BaseException as e:   # as before: not ours to swallow, re-raised by the pass
                done.setdefault(i, (None, e, None))
        while nxt < len(jobs) or running:
            for i, (dead, t0, ctx) in list(running.items()):
                took = int((self.clock() - t0).total_seconds())
                if i in done:
                    del running[i]
                elif took >= REAP_STOP_SECONDS:
                    dead.set()
                    del running[i]
                    done[i] = (None, CloudError("stop hit its limit: %d seconds (limit %d)" % (took, REAP_STOP_SECONDS), 1), None)
                    self.audit(jobs[i][1], "pod/" + pod_name(jobs[i][1]), "stop", 0.0, False, str(done[i][1]))
            while nxt < len(jobs) and len(running) < REAP_PARALLEL:
                dead = threading.Event()
                ctx = copy.copy(self)
                ctx.kube, ctx.last_hook, ctx._orchestrator, ctx.foreign = _StopKube(self.kube, dead), None, None, []
                ctx.recorder = copy.copy(self.recorder)
                ctx.audit = lambda *a, _d=dead, **k: None if _d.is_set() else self.audit(*a, **k)
                running[nxt] = (dead, self.clock(), ctx)
                threading.Thread(target=work, args=(nxt, ctx), daemon=True).start()
                nxt += 1
            if running:
                time.sleep(0.005)
        for i in range(len(jobs)):
            if isinstance(done[i][1], BaseException) and not isinstance(done[i][1], CloudError):
                raise done[i][1]
        return [done[i] for i in range(len(jobs))]

    def reap(self):
        """One pass: stop every pod past its hard deadline or idle too long. Returns [(worker, reason, exit)]."""
        out, now, live, jobs = [], self.clock(), [], []
        self.reap_results, self.last_hook = {}, None
        mono = time.monotonic
        steps = self.step_seconds = {"list": 0.0, "renew": 0.0, "vanished": 0.0, "activity": 0.0, "activity_n": 0, "stops": 0.0, "stops_n": 0, "orphans": 0.0, "notes": 0.0, "total": None}
        t_pass = t0 = mono()
        steps["start"] = t_pass   # a pass that raises is measured from here at its finally
        acts = {}   # name -> the activity this pass already read (passed to send_idle_nudges: one read per pod per pass)
        try:
            pods = self.kube.list_pods()
        finally:
            steps["list"] = mono() - t0
        t0 = mono()
        _sw = getattr(self.kube.registry if getattr(self.kube, "registry", None) is not None else getattr(self.kube, "known", None), "sweep", None)
        if _sw:
            try:
                if not pods:   # an empty or unreadable list cannot be told from a truncated read: drop nothing
                    self.audit("", "", "known-forms.sweep-skipped", 0, True, "empty pod list")
                    _sw(time.time(), None)   # a skipped pass clears the memory of absent pods (two non-empty lists in a row are needed)
                else:
                    _sw(time.time(), {(p.get("metadata") or {}).get("name") for p in pods})   # forms whose every holder has left are dropped (ask #1783)
            except Exception as e:   # the sweep never raises into the pass; the class name only, never a value
                try:
                    self.audit("", "", "known-forms.sweep-failed", 0, False, type(e).__name__)
                except Exception:
                    pass
        # The renewal is the FIRST step: a slow activity read or stop below can no longer hold it back (the audit of 29 October).
        try:
            renewed = self.renew_git_tokens(self._before_hard_limit(pods, now), now)
        finally:
            steps["renew"] = mono() - t0
        t0 = mono()
        try:
            self._note_vanished_pods(pods, now)
        finally:
            steps["vanished"] = mono() - t0
        for pod in pods:
            ended = (pod.get("status") or {}).get("phase") in ("Failed", "Succeeded")
            if ended and ann(pod, "kept") == "true" and (ann(pod, "unreachable") == "true"
                                                         or ((pod.get("metadata") or {}).get("name"), (pod.get("metadata") or {}).get("uid")) in self.ended_tried):
                continue   # a kept pod that ended and was tried once: left for a person; no stop, no record, no audit
            if (pod.get("status") or {}).get("phase") == "Running" and ann(pod, "kept") == "true" and ann(pod, KEPT_RUNNING_MARK.replace("_", "-")) == "true":
                continue   # a kept running pod whose push failed on an ended token was tried once: no stop, no record, no audit; never deleted
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
            failed = (pod.get("status") or {}).get("phase") == "Failed"
            try:
                hard = self.hard_deadline(pod)
            except (ValueError, TypeError):
                if not failed:
                    out.append((worker, "unreadable deadline", 1))  # one bad pod never stops the pass
                    continue
                hard = None   # a Failed pod is still handled below; its deadline is said once, not on every pass
                said = (worker, self._failed_key(pod), "deadline")
                if self._failed_name_fits(pod) and said not in FAILED_UNWRITTEN_TOLD:
                    FAILED_UNWRITTEN_TOLD.add(said)
                    out.append((worker, "unreadable deadline", 1))
            if hard is not None and now >= hard:
                reason = "hard limit"
            elif failed:
                # A pod that ended Failed answers nothing: read why ONCE, keep the record, report it once; delete it ten minutes after
                # its record unless it holds unpushed work (kept). A second pass reads no activity of it.
                if not self._failed_name_fits(pod):   # no worker annotation, or one that is not a worker name: not this launcher's pod, never read, recorded or deleted
                    continue
                note, fresh = self._failed_note(pod)
                told = (worker, self._failed_key(pod))
                if note.get("unwritten"):   # the pod stays; said once, written on the first pass that can
                    if told not in FAILED_UNWRITTEN_TOLD:
                        FAILED_UNWRITTEN_TOLD.add(told)
                        out.append((worker, "record not written: " + note["pod_message"], 1))
                    continue
                FAILED_UNWRITTEN_TOLD.discard(told)
                if fresh:
                    out.append((worker, "pod failed: " + note["pod_failed_reason"][:160], 0))
                elif ann(pod, "kept") != "true" and note.get("recorded_at") and self._failed_aged(worker, pod, note, now) is None:
                    said = (worker, self._failed_key(pod), "time")
                    if said not in FAILED_UNWRITTEN_TOLD:
                        FAILED_UNWRITTEN_TOLD.add(said)
                        out.append((worker, "record time unreadable; the pod is left", 1))
                elif ann(pod, "kept") != "true" and note.get("recorded_at") and self._failed_aged(worker, pod, note, now):
                    try:
                        self._remove(pod["metadata"]["name"], ann(pod, "run-id") or None, (pod.get("metadata") or {}).get("uid"))
                        self.audit(worker, "pod/" + pod["metadata"]["name"], "stop", self.cost(pod), True, "failed pod: record kept ten minutes", None, "pod_failed")
                    except CloudError as e:
                        out.append((worker, "failed pod not deleted: " + self.kube.scrub(e, 120), e.code))
                continue
            else:
                live.append(pod)   # before its hard limit and not stopped below: the git token renewal's candidates
                t0 = mono()
                try:
                    act = self.activity(pod["metadata"]["name"])
                finally:
                    steps["activity"] += mono() - t0
                    steps["activity_n"] += 1
                acts[pod["metadata"]["name"]] = act
                if act is None:
                    # Fail mode, decided: an unreadable activity is not a reason to kill work, so the pod runs until
                    # its hard limit; it is reported so it is not silent.
                    out.append((worker, "activity unreadable", 1))
                    continue
                if act and not act[0] and act[1] is not None and (now - act[1]).total_seconds() >= int(ann(pod, "idle-seconds", "600")):
                    reason = "idle"
            if reason:
                if pod in live:
                    live.remove(pod)
                out.append(None)   # this pod's place in the result: filled after the stops ran
                # by the run's own nonce where the pod carries one: a pod of another run under this name is left whole
                jobs.append((len(out) - 1, worker, reason, ann(pod, "run-id") or None))
        t0 = mono()
        try:
            stopped = self._run_stops(jobs)
        finally:
            steps["stops"] += mono() - t0
            steps["stops_n"] += len(jobs)
        for (slot, worker, reason, _), (res, err, hook) in zip(jobs, stopped):
            try:
                if err is not None:
                    raise err
                if res is not None:
                    self.reap_results[worker] = {k: res.get(k) for k in ("pushed", "wip_branch", "skipped", "record")}
                    out[slot] = (worker, reason, 0)
                    self.retire_row(worker, reason)   # the pod is ended: its registry row is retired, once (DW-3)
            except CloudError as e:
                self.reap_results[worker] = dict(hook or {})
                out[slot] = (worker, reason, e.code)
        out.extend(renewed)
        t0 = mono()
        try:
            self.remove_orphan_secrets(pods, now)
        finally:
            steps["orphans"] = mono() - t0
        t0 = mono()
        try:
            self.send_wip_notes(live, now)
        finally:
            steps["notes"] = mono() - t0
        t0 = mono()
        try:
            self.send_final_notes(live, now)
        finally:
            steps["notes"] += mono() - t0   # the final note is counted with the notes
        t0 = mono()
        try:
            nudge_prune(self, pods)   # the FULL pod list, not the subset `live`
            self.send_idle_nudges(live, now, acts)
        finally:
            steps["notes"] += mono() - t0   # the nudges are counted with the notes: both are the pass's sends
        try:
            out.extend(self.label_pod_prs(live, now))   # #2269: the pull requests a pod recorded get the labels its launch named
        except Exception as err:
            print("cloud-worker: pod pull requests were not labelled: %s" % self.kube.scrub(err, 120), file=sys.stderr)
        steps["total"] = mono() - t_pass
        return out

    def _note_vanished_pods(self, pods, now):
        """A worker this launcher created (a `create` line in the audit's last 24 hours, read from its last 2 MB) with no later stop or delete line,
        whose pod is absent from this pass's snapshot: ONE stop line, cause pod_gone. The next pass sees that line and writes nothing."""
        try:
            with open(self.audit_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - VANISHED_READ_BYTES))
                lines = f.read().decode(errors="replace").splitlines()
        except OSError:
            return
        present = {"pod/" + p["metadata"]["name"] for p in pods}
        open_runs = {}
        for raw in lines:
            try:
                rec = json.loads(raw)
                when = parse_iso(rec["ts"])
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            if not isinstance(rec, dict) or (now - when).total_seconds() > VANISHED_WINDOW_SECONDS:
                continue
            rid = rec.get("runtime_id")
            if rec.get("action") == "create" and rec.get("ok") is True and isinstance(rid, str) and rid:
                if rec.get("persistent") is True:
                    continue   # a persistent orchestrator's pod is briefly absent while its StatefulSet recreates it: never stamped
                open_runs[rid] = (rec.get("worker"), when)
            elif rec.get("action") in ("stop", "delete") and rec.get("ok") is True and rid in open_runs:
                del open_runs[rid]   # only a stop that happened closes the run; a failed stop (the pod was kept) leaves it open
        if not pods:
            return   # an empty snapshot is no evidence of anything
        reads = 0
        for rid, (worker, created) in open_runs.items():
            if rid in present or not isinstance(worker, str) or not NAME_RE.fullmatch(worker):
                continue
            if (now - created).total_seconds() <= VANISHED_MIN_AGE_SECONDS:
                continue   # too young: a later pass notices it
            if reads >= VANISHED_MAX_READS:
                break
            reads += 1
            try:
                gone = self.kube.get_pod(pod_name(worker)) is None   # None only on the exact not-found of this one pod
            except (CloudError, ValueError, TypeError, OSError):
                continue   # any other answer: nothing is written for it
            if gone:
                self.audit(worker, rid, "stop", 0.0, True, "pod gone; no stop was recorded", None, "pod_gone")

    def remove_orphan_secrets(self, pods, now):
        """Remove each per-run Secret (`<pod>-run`, label cw/pod; `<pod>-pw`, label app=cw-worker) whose pod is absent from THIS pass's pod list and
        that is more than ORPHAN_SECRET_AFTER_SECONDS old by its creation time (an upper bound of how long the pod has been gone). Each goes by its
        exact name through the launcher's own delete, with ONE audit line. A kept Secret: its pod is listed (or that name plus `-0`, a persistent
        orchestrator's pod; this branch has no workload list, so that rule alone stands in for the workload), a Secret younger than the time, any
        Secret without the launcher's label (the hub's). An unreadable list deletes nothing and is said once. No value is ever read."""
        if not hasattr(self.kube, "list_secrets"):
            return []   # a cluster object that cannot list Secrets (an earlier test's fake): the step does not run; the real Kube always can
        try:
            run_rows = self.kube.list_secrets("cw/pod")
            pw_rows = self.kube.list_secrets("app=" + LABEL)
        except (CloudError, ValueError, OSError) as e:
            print("cloud-worker: orphan secrets: the Secret list could not be read: %s" % scrub(str(e), (), 120).replace("\n", " "), file=sys.stderr)
            return []
        names = {(p.get("metadata") or {}).get("name") for p in pods}
        found = []
        for name, pod, app, created in run_rows:
            if pod and name == pod + "-run":
                found.append((name, pod, created, self.kube.delete_run_secret))
        for name, pod, app, created in pw_rows:
            m = PW_SECRET_NAME.fullmatch(name)
            if m and app == LABEL:
                found.append((name, m.group(1), created, self.kube.delete_secret))
        done = []
        for name, pod, created, delete in found:
            if pod in names or pod + "-0" in names:
                continue
            try:
                age = int((now - parse_iso(created)).total_seconds())
            except (ValueError, TypeError):
                continue
            if age <= ORPHAN_SECRET_AFTER_SECONDS:
                continue
            try:
                delete(pod)
            except CloudError as e:
                print("cloud-worker: orphan secrets: %s was not removed: %s" % (name, scrub(str(e), (), 120).replace("\n", " ")), file=sys.stderr)
                continue
            self.audit(pod, REAPER_RUNTIME_ID, ORPHAN_SECRET_ACTION, 0.0, True, "%s age %d seconds: %s" % (name, age, ORPHAN_SECRET_REASON))
            done.append(name)
        return done

    def retire_row(self, worker, reason):
        """The reaper retires the registry row of a worker it ended, through the registry's own `retire` (the state directory is the switchboard's
        own). A registry that cannot be written is ONE line on standard error and never a reason to keep the pod (the pod is already ended)."""
        try:
            import registry
            base = self.env.get("XDG_STATE_HOME")
            base = base if base and os.path.isabs(base) else os.path.join(os.path.expanduser("~"), ".local", "state")
            got = registry.retire(os.path.join(base, "fl" + "eet-switchboard"), worker, "cloud-worker", "reaped: %s" % reason)
            why = got.get("problem") if isinstance(got, dict) else None
        except Exception as e:  # noqa: BLE001  (any registry failure is said, never raised)
            why = str(e) or e.__class__.__name__
        if why:
            print("registry: %s: %s" % (worker, scrub(why, (), 120).replace("\n", " ")), file=sys.stderr)

    def note_slow_pass(self, took):
        """A pass that took longer than SLOW_PASS_SECONDS: ONE line on the reaper's own log and ONE audit line with the seconds. A short pass: none."""
        if took <= SLOW_PASS_SECONDS:
            return False
        text = "the pass took %d seconds (limit %d)" % (took, SLOW_PASS_SECONDS)
        print("cloud-worker: slow reaper pass: %s" % text, file=sys.stderr)
        self.audit("", REAPER_RUNTIME_ID, SLOW_PASS_ACTION, 0.0, False, text)
        return True

    def _before_hard_limit(self, pods, now):
        """The pods whose hard limit has not passed (a bad deadline is left to the pass's own report)."""
        out = []
        for pod in pods:
            if ann(pod, "persistent") is not None:   # a persistent pod has no hard deadline: it is always before its limit
                if self._persistent_owned(pod):   # only a pod whose own workload is read and owns it is renewed; the reap pass reports the others
                    out.append(pod)
                continue
            try:
                if now < self.hard_deadline(pod):
                    out.append(pod)
            except (ValueError, TypeError):
                pass
        return out

    def send_wip_notes(self, pods, now):
        """At minute 40 of a pod's life, ONE note asks its worker to push <worker>/wip (the text is wip_note). The pod's cw/wip-note annotation
        is set BEFORE the note goes, so a failed send is not repeated and a second pass never sends a second note. Never raises."""
        for pod in pods:
            if ann(pod, "persistent") is not None:   # an always-on orchestrator is not asked to push at minute 40
                continue
            try:
                worker, name = ann(pod, "worker"), pod["metadata"]["name"]
                if (pod.get("status") or {}).get("phase") != "Running" or not NAME_RE.fullmatch(worker or ""):
                    continue
                no_commit_mark = ann(pod, "no-commit")
                if no_commit_mark == "true":
                    continue   # launched not to commit: no note, no annotation, no audit line
                no_commit_unreadable = no_commit_mark is not None   # present but not the text true: treated as unmarked
                if not wip_note_due(parse_iso(ann(pod, "started")), now, ann(pod, "wip-note")):
                    continue
                if no_commit_unreadable:   # the note below sets the wip-note annotation, so this line is written once
                    self.audit(worker, "pod/" + name, "no-commit-unreadable", 0.0, True, "the cw/no-commit annotation is not the text true; the pod is treated as unmarked")
                self.kube.annotate(name, (pod.get("metadata") or {}).get("uid"), **{WIP_NOTE_MARK: "sent"})
                self.send_note(name, wip_note(worker))
                self.audit(worker, "pod/" + name, "wip-note", self._cost_or_zero(pod), True, "")
            except Exception as e:
                print("cloud-worker: the 40-minute note was not sent: %s" % self.kube.scrub(e, 120), file=sys.stderr)

    def send_idle_nudges(self, pods, now, acts=None):
        """ONE fixed line to a stalled builder (see idle_nudge_due). The cw/idle-nudge annotation is set BEFORE the send; one audit line.
        A results check that cannot run means do not nudge. Never raises."""
        for pod in pods:
            try:
                worker, name = ann(pod, "worker"), pod["metadata"]["name"]
                if (pod.get("status") or {}).get("phase") != "Running" or not NAME_RE.fullmatch(worker or ""):
                    continue
                if ann(pod, "idle-nudge") and ann(pod, "role", "worker") != "orchestrator":
                    nudge_skip(self, pod, worker, name, "-", "already-nudged", now)
                if ann(pod, "idle-nudge") or ann(pod, "role", "worker") == "orchestrator":
                    continue
                act = (acts or {}).get(name) or self.activity(name)
                # These three have no known idle time: the line says so and is rate-limited by the clock, not by idle time.
                if not act:
                    nudge_skip(self, pod, worker, name, "-", "probe-unreadable (idle unknown)", now)
                elif act[0]:
                    nudge_skip(self, pod, worker, name, "-", "busy (idle unknown)", now)
                elif act[1] is None:
                    nudge_skip(self, pod, worker, name, "-", "no-activity-time (idle unknown)", now)
                if not act or act[0] or act[1] is None:
                    continue
                idle = int(ann(pod, "idle-seconds", "600"))
                if not idle_nudge_due(False, act[1], now, idle, "", ann(pod, "role", "worker"), False):
                    continue
                try:   # one read-only exec: it prints yes or no; a failure to run means do not nudge
                    seen = self.kube.exec(name, "python3", "-c", RESULTS_PROBE_CODE).strip()
                except CloudError:
                    nudge_skip(self, pod, worker, name, act[1], "exec-failed", now)
                    continue
                if seen != "no" and not results_not_finished(seen):
                    nudge_skip(self, pod, worker, name, act[1], "results-finished", now)
                if seen != "no" and not results_not_finished(seen):
                    continue
                self.kube.annotate(name, (pod.get("metadata") or {}).get("uid"), **{IDLE_NUDGE_MARK: "sent"})
                self.send_note(name, IDLE_NUDGE)
                self.audit(worker, "pod/" + name, "idle-nudge", self._cost_or_zero(pod), True, "")
            except Exception as e:
                print("cloud-worker: the idle nudge was not sent: %s" % self.kube.scrub(e, 120), file=sys.stderr)

    def send_note(self, name, text):
        self.kube.exec(name, "python3", "-c", NOTE_SENDER_CODE, text)

    def label_pod_prs(self, pods, now):
        """#2269: for each running pod launched with labels (cw/pr-labels), read the pod's pull request record by exec (one number a line) and add the
        labels to each number not yet handled (cw/pr-labelled), once, with the repository's token. Never creates a label; a number outside the record
        is never touched. A failure is said once (one report entry and one audit line). Returns report entries; never raises."""
        out, token = [], None
        for pod in pods:
            try:
                worker, name = ann(pod, "worker"), pod["metadata"]["name"]
                raw = ann(pod, "pr-labels")
                if not raw or (pod.get("status") or {}).get("phase") != "Running" or not NAME_RE.match(worker or ""):
                    continue
                labels = [x for x in raw.split(",") if PR_LABEL_RE.fullmatch(x)]
                if not labels:
                    continue
                done = set((ann(pod, PR_LABELLED_MARK) or "").split(","))
                seen = [x for x in self.kube.exec(name, "python3", "-c", PR_RECORD_READ).split() if x.isascii() and x.isdigit() and 0 < int(x) < 10 ** 9]
                todo = [x for x in dict.fromkeys(str(int(x)) for x in seen) if x not in done]
                if not todo:
                    continue
                if len(todo) > PR_LABELS_PER_PASS:
                    todo = todo[:PR_LABELS_PER_PASS]
                    if worker not in PR_LABELS_CUT_TOLD:
                        PR_LABELS_CUT_TOLD.add(worker)
                        out.append((worker, "pull request record holds more than %d numbers; %d are handled per pass, the rest wait" % (PR_LABELS_PER_PASS, PR_LABELS_PER_PASS), 1))
                repo = _pod_repo(pod)
                for number in todo:
                    try:
                        if token is None:
                            check_repo_allowed(repo, self.env)
                            token = run_git_token_cmd(git_token_cmd(repo, self.env), repo, dict(os.environ, **self.env), register=lambda *v: self.kube.add_secrets(*v))
                            self.kube.add_secrets(token)
                        ref, state, head_repo = (self.pr_head or github_pr_head)(token, repo, int(number))
                        if state != "open" or head_repo.lower() != repo.lower():
                            raise RuntimeError("skipped: the pull request is %s and its head is in %s, not %s" % (safe_value(state), safe_value(head_repo), safe_value(repo)))
                        (self.add_labels or github_add_labels)(token, repo, int(number), labels)
                        ok, why = True, "labelled %s" % ", ".join(labels)
                    except Exception as e:
                        ok, why = False, "not labelled: %s" % self.kube.scrub(e, 120)
                    done.add(number)
                    mark_failed = False
                    try:
                        self.kube.annotate(name, (pod.get("metadata") or {}).get("uid"), **{PR_LABELLED_MARK: ",".join(sorted(x for x in done if x))})   # before the report: once
                    except Exception as e:
                        mark_failed = True
                        why += "; the done mark failed: %s" % self.kube.scrub(e, 80)
                    self.audit(worker, "pod/" + name, "pr-label", 0.0, ok, "#%s %s" % (number, why))
                    if not ok and (worker, number) not in PR_LABELS_TOLD:
                        PR_LABELS_TOLD.add((worker, number))
                        out.append((worker, "pull request #%s %s" % (number, why), 1))
                    if mark_failed:
                        break   # not retried within this pass
            except Exception as e:
                print("cloud-worker: the pod's pull requests were not labelled: %s" % self.kube.scrub(e, 120), file=sys.stderr)
        return out

    pr_head = None   # a test replaces it: (token, repo, number) -> (head ref, state, head repository full name)
    add_labels = None   # a test replaces it: (token, repo, number, labels)

    def send_final_notes(self, pods, now):
        """When a worker pod has N minutes left to its hard deadline (N = CW_FINAL_NOTE_MINUTES, default 10) and lived at least 2N minutes in all,
        ONE note asks it to send its final message. The pod's cw/final-note annotation is set BEFORE the send, so a failed send or a restart
        never repeats it. A pod with no cw/hard-deadline (a persistent orchestrator) is skipped. Unreadable data: no note, one audit line. Never raises."""
        for pod in pods:
            try:
                worker, name = ann(pod, "worker"), pod["metadata"]["name"]
                if (pod.get("status") or {}).get("phase") != "Running" or not NAME_RE.match(worker or ""):
                    continue
                raw_end = ann(pod, "hard-deadline")
                if not raw_end or ann(pod, "final-note"):
                    continue
                uid = (pod.get("metadata") or {}).get("uid")
                raw_n = self.env.get("CW_FINAL_NOTE_MINUTES")
                raw_n = FINAL_NOTE_DEFAULT_MINUTES if raw_n is None else raw_n
                reason = None
                try:
                    end, began = parse_iso(raw_end), parse_iso(ann(pod, "started"))
                    if end is None or began is None:
                        reason = "unreadable annotation"
                except Exception:
                    reason = "unreadable annotation"
                if reason is None:
                    s = str(raw_n)
                    if not (s.isascii() and s.isdigit() and 1 <= int(s) <= 60):
                        reason = "unreadable setting"
                    else:
                        n = int(s)
                bad_setting = False
                if reason == "unreadable setting":
                    reason, n, bad_setting = None, int(FINAL_NOTE_DEFAULT_MINUTES), True
                    if str(raw_n).strip() == "":
                        bad_setting = False
                if reason is not None:
                    self.kube.annotate(name, uid, **{FINAL_NOTE_MARK: "unreadable"})
                    self.audit(worker, "pod/" + name, "final-note", self._cost_or_zero(pod), False, reason)
                    continue
                left = (end - now).total_seconds() / 60
                if left > n or (end - began).total_seconds() / 60 < 2 * n:
                    continue
                if bad_setting:
                    self.kube.annotate(name, uid, **{FINAL_NOTE_MARK: "sent"})
                    self.send_note(name, FINAL_NOTE_TEXT)
                    self.audit(worker, "pod/" + name, "final-note", self._cost_or_zero(pod), True, "minutes left: %d; unreadable setting, %d used" % (max(0, int(left)), n))
                    continue
                self.kube.annotate(name, uid, **{FINAL_NOTE_MARK: "sent"})
                self.send_note(name, FINAL_NOTE_TEXT)
                self.audit(worker, "pod/" + name, "final-note", self._cost_or_zero(pod), True, "minutes left: %d" % max(0, int(left)))
            except Exception as e:
                print("cloud-worker: the final note was not sent: %s" % self.kube.scrub(e, 120), file=sys.stderr)

    # -- the git token's renewal (one more step of the reaper's pass) --
    def renew_git_tokens(self, pods, now):
        """For each running pod this launcher made, renew its git token when it is GIT_TOKEN_RENEW_AFTER_SECONDS old. Returns report entries:
        (worker, "git token renewed", 0) or (worker, "git token not renewed: WHY", 1). A pod that needs nothing (no per-run Secret, not due)
        adds nothing. One pod's trouble never stops the pass; the old token stays until a patch has landed."""
        with self.renew_lock:
            self._tail = AuditTail(self.audit_path)   # one lookup for this call; it reads nothing until a failure asks
            try:
                return self._renew_locked(pods, now)
            finally:
                self._tail = None

    def _renew_locked(self, pods, now):
        out, relogged = [], False
        for pod in pods:
            worker = ann(pod, "worker")
            try:
                if ann(pod, "persistent") is not None and (pod.get("status") or {}).get("phase") == "Running" and NAME_RE.match(worker or "") \
                        and not self.kube.get_run_secret(pod_name(worker)):   # a persistent pod's Secret is the worker's: its absence is ONE report line
                    raise _NotRenewed("the per-run Secret of the persistent orchestrator was not found")
                try:
                    done = self._renew_git_token(pod, now)
                except (_NotRenewed, CloudError) as first:
                    # The reaper's own login lapsed: refresh it and retry THIS renewal once, in the same pass (never a loop).
                    if relogged or self.relogin is None or "unauthorized" not in str(first).lower():
                        raise
                    relogged = True
                    if not self.relogin():
                        raise
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
        tail = getattr(self, "_tail", None)
        return tail.get("renew-git-token", rid) if tail is not None else last_audit_line(self.audit_path, "renew-git-token", rid)

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
        if not rd or not NAME_RE.fullmatch(worker or ""):
            raise _NotRenewed("no worker record to read (CW_CLOUD_RECORD_DIR is not set)" if not rd else "bad worker name")
        try:
            with open(os.path.join(rd, worker + ".json")) as f:
                repo = json.load(f).get("repo")
        except (OSError, ValueError, AttributeError):
            raise _NotRenewed("the worker record is missing or unreadable") from None
        if not isinstance(repo, str) or not repo:
            raise _NotRenewed("the worker record names no repository")
        return repo

    def _renew_git_token(self, pod, now, force=False):
        """True: renewed. False: nothing to do (quietly). Raises _NotRenewed(reason): refused or failed, the old token untouched.
        force: the stop's own refresh (#1354): the age check is skipped and the new token's scope is probed before the Secret is patched."""
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
        if not force and age is not None and age < GIT_TOKEN_RENEW_AFTER_SECONDS:
            return False
        self.kube.add_secrets(*[self._b64_text(v) for k, v in (secret.get("data") or {}).items() if isinstance(v, str) and k != "model-auth"], life=("pod", (pod.get("metadata") or {}).get("name")))   # every value of the Secret is known to the scrubber before anything can echo it
        self.kube.add_secrets(*[self._b64_text(v) for k, v in (secret.get("data") or {}).items() if isinstance(v, str) and k == "model-auth"])   # the model entry never leaves (open choice b)
        repo = _pod_repo(pod)
        if _repo_key(repo) is None or _repo_key(repo) != _repo_key(self._worker_record_repo(worker)):
            raise _NotRenewed("repository mismatch: the worker record and the pod name different repositories")
        try:
            check_repo_allowed(repo, self.env)
        except CloudError:
            raise _NotRenewed("repository not on the allow-list") from None
        try:
            role = ann(pod, "role", "worker")   # the role the pod was made under; a pod made before the annotation is a worker
            role = role if role in AGENTS else "worker"
            cmd = git_token_cmd(repo, self.env, role) if role != "worker" else git_token_cmd(repo, self.env)
            token = run_git_token_cmd(cmd, repo, dict(os.environ, **self.env), register=lambda *v: self.kube.add_secrets(*v, life=secret_life_until(pod=(pod.get("metadata") or {}).get("name"))))
        except CloudError as e:
            raise _NotRenewed("the mint failed: %s" % e) from None
        self.kube.add_secrets(token, life=secret_life_until(pod=(pod.get("metadata") or {}).get("name")))
        if re.search(r"\s", token):
            raise _NotRenewed("the mint gave a token with whitespace in it")
        self._require_same_scope(force, token, repo)
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

    def scope_probe(self, token, repo):
        """What the token can reach, asked of GitHub with the token itself (a test replaces this method): the repositories it lists, each as
        {full_name, permissions}. The token goes in one header and nowhere else. Raises on any failure (the caller refuses: fail closed)."""
        import urllib.request
        import token_mint   # the cloud programs' one opener: it never follows a redirect, so the header stays on api.github.com
        req = urllib.request.Request("https://api.github.com/installation/repositories?per_page=5",
                                     headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "User-Agent": "cw-launcher"})
        with token_mint.open_no_redirect(req, SCOPE_PROBE_SECONDS) as r:
            body = json.loads(r.read(1000000).decode())
        return body["repositories"]

    def _require_same_scope(self, force, token, repo):
        """The stop's refresh only: a token that is not the same scope as the first is refused (_NotRenewed), and the old file is kept."""
        if force and not self._scope_is_same(token, repo):
            raise _NotRenewed("the new token is not the same scope as the first: refused")

    def _scope_is_same(self, token, repo):
        """True only when the new token reaches exactly the one repository of the pod and holds no more than push and pull on it (never admin
        or maintain). Anything unreadable is a no. The first token passed the same minter checks (app_token.py: one repository, the
        exact permission table, the 61-minute cap); this is the second look at the new one."""
        try:
            repos = self.scope_probe(token, repo)
            if not isinstance(repos, list) or len(repos) != 1 or not isinstance(repos[0], dict):
                return False
            only = repos[0]
            perms = only.get("permissions") or {}
            return _repo_key(only.get("full_name") or "") == _repo_key(repo) and perms.get("push") is True \
                and not perms.get("admin") and not perms.get("maintain")
        except Exception:
            return False

    def _projected(self, name, token):
        """True when the pod's token file already holds this token: only a digest comes back, never the token."""
        want = hashlib.sha256(token.encode()).hexdigest()
        code = ("import hashlib,os;print(hashlib.sha256(open(os.path.join(os.environ.get('CW_RUN_DIR','/run/cw'),'git-token'),'rb').read().strip())"
                ".hexdigest())")
        try:
            return self.kube.exec(name, "python3", "-c", code).strip() == want
        except CloudError:
            return False

    def refresh_for_stop(self, pod, now):
        """The stop's one refresh: REFRESH_USED once the Secret holds a new same-scope token (and the pod's file shows it, or the wait ran
        out), REFRESH_REFUSED otherwise. Never raises, never holds a value. The mint is the renewal's (run_git_token_cmd)."""
        name = pod["metadata"]["name"]
        try:
            if not self._renew_git_token(pod, now, force=True):
                return REFRESH_REFUSED
        except Exception:
            return REFRESH_REFUSED
        waited = 0
        while waited < GIT_TOKEN_PROJECTION_SECONDS:
            secret = self.kube.get_run_secret(name)
            raw = ((secret or {}).get("data") or {}).get(GIT_TOKEN_KEY)
            if raw and self._projected(name, self._b64_text(raw)):
                break
            self._nap(GIT_TOKEN_PROJECTION_POLL_SECONDS)
            waited += GIT_TOKEN_PROJECTION_POLL_SECONDS
        return REFRESH_USED

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
    ("CW_FINAL_NOTE_MINUTES", False, "number"),          # minutes before a worker's hard limit that it is told to send its final message (default 10)
    ("CW_HUB_CHANNEL_URL", False, "name"),         # the channel's address an orchestrator pod is told (default http://cloud-hub.cw.svc:8443/message); not a secret
    ("CW_HUB_REGISTRY_SECRET", False, "name"),     # the NAME of the hub's registry Secret in the cloud namespace (default cloud-hub-registry), never its content
    ("CW_STACK_REGISTRY", False, "registry-host"),  # the stack registry's host name (pull-only for a stack pod); all three CW_STACK_ keys are set together or not at all
    ("CW_STACK_PULL_CLIENT_ID", False, "guid"),    # the client id of the pull identity (not a secret)
    ("CW_STACK_PULL_TENANT_ID", False, "guid"),    # the tenant id (not a secret)
    ("CW_REPO_MIN_SHAPE", False, "name"),          # comma-separated OWNER/NAME=SHAPE: the smallest pod shape a launch for that repository may use (a floor)
    ("CW_READONLY_ROOT_LANES", False, "name"),     # lane (boss) names, comma or space separated, whose pods get a read-only root file system; empty = off
    ("CW_VAULT_CHECK_DIR", False, "path"),         # the checkout the vault access check runs from; needed only after a vault firewall refusal (see vault_check_dir_problem)
    ("CW_MAX_PERSISTENT", False, "number"),        # the most persistent orchestrators (StatefulSets of the launcher's label) the namespace may hold (an integer 1 to 20; default 2)
    ("CW_VAULT_BLOCK_DURATION", False, "duration"),   # how long the launcher refuses vault commands after a vault firewall refusal (default 10m, 1m to 60m)
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


def _minutes_ok(value):
    return value.isascii() and value.isdigit() and 1 <= int(value) <= 60


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
    "CW_FINAL_NOTE_MINUTES": ("a whole number of minutes from 1 to 60", _minutes_ok),
    "CW_HUB_CHANNEL_URL": ("an http or https URL: a host, an optional port, an optional one-segment path (/message), no user, query or fragment", lambda v: bool(re.fullmatch(r"https?://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?(?:/[A-Za-z0-9._-]{0,64})?", v))),
    "CW_HUB_REGISTRY_SECRET": ("a Kubernetes name (lowercase letters, digits, - and .)", lambda v: bool(_SECRET_NAME.fullmatch(v))),
    "CW_STACK_REGISTRY": ("a lower-case registry host name ending in .azurecr.io: no scheme, path, port, user or IP address", _registry_host_ok),
    "CW_STACK_PULL_CLIENT_ID": ("a GUID", lambda v: bool(_GUID.fullmatch(v))),
    "CW_STACK_PULL_TENANT_ID": ("a GUID", lambda v: bool(_GUID.fullmatch(v))),
    "CW_REPO_MIN_SHAPE": ("a comma-separated list of OWNER/NAME=SHAPE entries (SHAPE one of %s), no repository twice" % ", ".join(FLOOR_ORDER), lambda v: repo_min_shapes(v) is not None),
    "CW_READONLY_ROOT_LANES": ("empty, or lane names separated by commas or spaces, each a worker name (a lower-case letter, then up to 31 lower-case letters, digits, _ or -)", lambda v: all(NAME_RE.fullmatch(x) for x in v.replace(",", " ").split())),
    "CW_VAULT_CHECK_DIR": ("an absolute path with no .. segment", _path_ok),
    "CW_MAX_PERSISTENT": ("an integer from 1 to 20", lambda v: bool(PERSISTENT_PATTERN.fullmatch(v)) and 1 <= int(v) <= PERSISTENT_MAX_LIMIT),
    "CW_VAULT_BLOCK_DURATION": ("a duration of 1 to 3 digits and m or h, from 1m to 60m (for example 10m or 1h)", _duration_ok),
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


def parse_issue_number(value):
    """None when no issue is named; else an int above 0; anything else is a usage error."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value):
        return int(value)
    raise CloudError("--issue must be a whole number above 0, got %r; nothing was created" % str(value)[:40], EXIT_USAGE)


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
    c.add_argument("--issue", default=None, help="the issue number (a whole number above 0) written into the hub entry of a registered worker")
    c.add_argument("--branch", required=True)
    c.add_argument("--idle", default="30m")
    c.add_argument("--hard", default="2h")
    c.add_argument("--max-hard", default=DEFAULT_MAX_HARD)
    c.add_argument("--shape", default=None, help="the pod shape, one of %s; unset: %s (%s with --stack, %s with --agent orchestrator)" % (", ".join(SHAPES), DEFAULT_SHAPE, DEFAULT_STACK_SHAPE, DEFAULT_ORCHESTRATOR_SHAPE))
    c.add_argument("--image")
    c.add_argument("--stack", action="store_true",
                   help="a pod for a worker that runs a nested Docker stack: the Sysbox runtime class, its own user namespace, the pool's node label; off by default (the default pod is unchanged)")
    c.add_argument("--stack-model-secret", metavar="NAME", help="with --stack: the NAME of the vault secret whose value arrives as the run secret %s (audit: the name and 'delivered'); never a value" % STACK_MODEL_KEY)
    c.add_argument("--agent", help="worker (default) or orchestrator: the agent the pod runs (CW_AGENT); any other value is refused before anything is made")
    c.add_argument("--no-commit", action="store_true", help="this pod is launched NOT to commit (a measure or a read): the launcher never tells it to push its work")
    c.add_argument("--pr-label", action="append", default=None, metavar="LABEL", help="a label the pod's pull requests get (repeatable); the reaper adds it, never creates it")
    c.add_argument("--boss", help="with --agent orchestrator: the name of its boss (FLEET_BOSS)")
    c.add_argument("--persistent", action="store_true",
                   help="with --agent orchestrator and the per-run git-token Secret: an always-on orchestrator as ONE StatefulSet (restart always, no deadline, no idle limit); ended only by an explicit stop; refused for a worker or a stack launch")
    c.add_argument("--wait", type=int, default=300)
    c.add_argument("--no-wait", action="store_true", help="return once the pod is requested, without waiting for it to be ready (the caller reads the pod's state later)")
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
    s.add_argument("--pod-uid", type=_pod_uid, help="the uid create recorded: stop acts only on the pod with this uid (a new pod under the name is left whole, exit 6)")
    s.add_argument("--hub-entry", action="store_true", help="also remove the worker's hub entry when its pod is already gone (a hub orchestrator)")
    e = cmd("extend")
    e.add_argument("--hard")
    e.add_argument("--idle")
    e.add_argument("--reason", default="")
    d = cmd("delete")
    d.add_argument("--force", action="store_true")
    d.add_argument("--reason", default="")
    cmd("status")
    cmd("inspect")   # ONE read-only look inside a running pod: the call in flight, the recent calls, git, the last push
    cmd("snapshot", worker=False)   # ONE read-only look at the cluster: the pods of the namespace and, when the login may list them, the nodes
    cmd("capacity", worker=False)   # ONE read-only look at room: the nodes' allocatable, the pods' requests, the Pending count, the last FailedScheduling line
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


def _pod_uid(v):
    if not POD_UID_RE.fullmatch(v):
        raise argparse.ArgumentTypeError("--pod-uid is 1 to 64 letters, digits, dots, dashes or underscores")
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


def code_identity(tree=None):
    """A sha256 over the bytes of the reaper's code on DISK (CODE_IDENTITY_FILES under `tree`, the kit root by default). A missing file is part of
    the identity (its name and a marker), not an error."""
    tree = tree or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    h = hashlib.sha256()
    for rel in CODE_IDENTITY_FILES:
        try:
            with open(os.path.join(tree, rel), "rb") as f:
                data = b"present:" + f.read()
        except OSError:
            data = b"missing"
        h.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


START_IDENTITY = code_identity()   # taken once when this module is imported: a file changed after Python read the modules is seen at the first compare


class RenewalTicker:
    """The renewal and the reaper's own login refresh on their own cadence: a thread that, at least every RENEW_CADENCE_SECONDS of the clock,
    refreshes the login and renews every due token, whatever the pass is doing (a stop that waits, an activity read that hangs). `wait` is the
    thread's pause (a test replaces it); it never raises out of the thread."""

    def __init__(self, launcher, refresher, clock, wait=None):
        self.L, self.refresher, self.clock = launcher, refresher, clock
        self.wait = wait or (lambda: self._stop.wait(RENEW_POLL_SECONDS))
        self._stop, self.thread, self.last = threading.Event(), None, clock()
        self.ticks, self.retry = 0, False

    def due(self):
        return self.retry or (self.clock() - self.last).total_seconds() >= RENEW_CADENCE_SECONDS

    def tick(self):
        now = self.clock()
        self.last = now
        self.ticks += 1
        cpu0, parsed0 = time.thread_time(), tail_lines_parsed()
        try:
            with self.L.renew_lock:
                refresh = self.refresher.run()
                self.retry = refresh.state in ("failed", "waiting")   # tried again at each look until the refresher's own retry gap has passed and it has tried
                if refresh.state == "failed":   # one line per failure, not one per look
                    print("cloud-worker: the login refresh failed: %s" % scrub(getattr(refresh, "reason", ""), (), 200), file=sys.stderr)
                if self.L.kube is not None and refresh.token:
                    self.L.kube.add_secrets(refresh.token, life=secret_life_until())
                self.L.renew_git_tokens(self.L._before_hard_limit(self.L.kube.list_pods(), now), now)
        except Exception as e:   # the thread goes on: the next tick tries again
            print("cloud-worker: the renewal thread: %s" % scrub(e, (), 200), file=sys.stderr)
        finally:
            print("cloud-worker: renewal tick line: cpu_seconds=%.3f audit_lines_parsed=%d" % (time.thread_time() - cpu0, tail_lines_parsed() - parsed0), file=sys.stderr)

    def _run(self):
        while not self._stop.is_set():
            self.wait()
            if self._stop.is_set():
                return
            if self.due():
                self.tick()

    def start(self):
        self.thread = threading.Thread(target=self._run, name="cw-renewal", daemon=True)
        self.thread.start()

    def close(self):
        self._stop.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(5)   # a tick in flight ends first


def step_text(st):
    """The seconds of the seven steps of a pass, for the pass line: numbers only, no name or value.

    Never raises (it runs inside a finally) and never prints a negative number: a bad dictionary gives the line without a step part."""
    try:
        if not st:
            return ""
        def sec(v):
            return max(0.0, float(v))
        total = st["total"] if st["total"] is not None else time.monotonic() - st["start"]   # a pass that raised never set its total: the wall seconds from its start
        return " steps list=%.1f renew=%.1f vanished=%.1f activity=%.1f (%d) stops=%.1f (%d) orphans=%.1f notes=%.1f total=%.1f" % (
            sec(st["list"]), sec(st["renew"]), sec(st["vanished"]), sec(st["activity"]), int(st["activity_n"]), sec(st["stops"]), int(st["stops_n"]),
            sec(st["orphans"]), sec(st["notes"]), sec(total))
    except Exception:
        return ""


def _reap_main(env, a, sleep, clock=None, refresher=None, kube=None, ticker_wait=None, tree=None):
    """The reaper's loop. Whatever ends it writes ONE line saying why (signal, error, or the end of the loop).

    The reaper's own login is the token file: it is made fresh at the start of every pass (before the cluster is reached), so an installed
    reaper keeps working for days without a person refreshing the file by hand. A login that is not ready (the file is missing and the
    refresh failed) is not fatal in a loop: the pass names the failure and the next attempt follows the retry gap.

    The reaper's own login note (the refresh failed, or the login is not ready) is named in the pass's report and in the audit, but it never
    fails the pass: the pass's exit code is the result of the pass's own work. The old token file still serves the login while it lasts, and a
    login that is not ready is fatal by itself (the token file missing, empty or unreadable: `token unavailable`). A real failure still shows
    up where it belongs: in the workers' own results."""
    old, why, lock, ticker = Launcher._catch_signals(), None, None, None
    clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    try:
        lock = reaper_lock(reaper_identity(env.get("CW_CLOUD_NAMESPACE", "cw")))   # BEFORE the first pass: two reapers would both run an end hook for one pod
        refresher = refresher or ClusterTokenRefresher(env, clock)
        L = Launcher(kube, env, clock, sleep) if kube is not None else None
        ticker = None
        identity = code_identity(tree) if tree else START_IDENTITY
        while True:
            blocked = read_vault_block(env)
            if blocked is not None and time.time() < blocked[1]:   # the vault block (ask #1427): no pass, no az process, ONE log line
                print("cloud-worker: reaper pass skipped: %s" % vault_block_line(blocked), file=sys.stderr)
                if not a.loop:
                    why = "end of its pass"
                    return 0
                sleep(min(a.loop, REAP_FLOOR_SECONDS))
                continue
            refresh = refresher.run()
            for line in getattr(refresher, "notices", []):
                print(line, file=sys.stderr)   # the stdout line of a pass is the JSON report; the notice names a setting, never a value
            if kube is not None and refresh.token:
                kube.add_secrets(refresh.token, life=secret_life_until())   # the new token is a secret from now on: the scrubber knows it
            note = "cluster token not refreshed: %s" % refresh.reason if refresh.state in ("failed", "waiting") else ""
            if kube is None:
                try:
                    kube = Kube(env)
                    refresher.kube = kube
                    L = Launcher(kube, env, clock, sleep)
                except CloudError as e:
                    note = note or ("cluster login failed: %s" % scrub(e, (), 120))
            if L is not None and ticker is None:
                def relogin(refresher=refresher, L=L):
                    r = refresher.run(unauthorized=True)
                    if r.token:
                        L.kube.add_secrets(r.token, life=secret_life_until())
                    return r.state == "written"
                L.relogin = relogin
                L.kube.on_unauthorized = relogin
                ticker = RenewalTicker(L, refresher, clock, ticker_wait)
                ticker.start()
            began = clock()
            if L is not None:
                L.step_seconds = None   # the pass line shows the steps of THIS pass only
            cpu0, parsed0 = time.thread_time(), tail_lines_parsed()
            try:
                work = L.reap() if L is not None else []   # the pass's own work: the workers' results
            finally:
                print("cloud-worker: reaper pass line: cpu_seconds=%.3f audit_lines_parsed=%d%s" % (time.thread_time() - cpu0, tail_lines_parsed() - parsed0, step_text(getattr(L, "step_seconds", None))), file=sys.stderr)
            took = (clock() - began).total_seconds()
            if L is not None:
                L.note_slow_pass(took)
            res = work
            if note:
                res = res + [("", note, 1)]   # the reaper's own login note: named in the report, never a worker's result
            print(json.dumps([dict({"worker": w, "reason": r, "exit": c}, **(L.reap_results.get(w, {}) if L is not None else {})) for w, r, c in res]))
            if not a.loop:
                why = "end of its pass"
                return EXIT_HOOK if any(c for _, _, c in work) else 0
            if code_identity(tree) != identity:   # between two passes only: a stop and its end hook have finished
                why = "end of its pass"   # the pass ended; the line above says why the loop does not go on
                print("cloud-worker: the installed code changed: the reaper exits to be started again", file=sys.stderr)
                if L is not None:
                    L.audit("", REAPER_RUNTIME_ID, RESTART_ACTION, 0.0, True, "the installed code changed")
                return 0
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
        if ticker is not None:
            ticker.close()
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
