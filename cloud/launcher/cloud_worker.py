#!/usr/bin/env python3
"""cloud-worker: launcher, reaper and audit for FleetKit cloud workers (slice B).

Built to docs/cloud-worker-interface.md. Commands: create, stop, extend, delete, status, reap.
The cloud is reached only through `kubectl` (the binary is FLEET_KUBECTL, so tests fake it). Nothing here
holds a credential: the cluster login is kubectl's own, and a worker's GitHub token is minted inside the pod.
"""
import argparse
import datetime as dt
import pwd
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")  # the switchboard's agent name rule
SHAPES = {"S": {"cpu": "2", "memory": "4Gi", "usd_per_hour": 0.10},
          "M": {"cpu": "4", "memory": "8Gi", "usd_per_hour": 0.20}}  # list-price estimates, not billed figures
MAX_TOKEN_SECONDS = 3600
DEFAULT_MAX_HARD = "8h"  # OPEN in the contract: value
LABEL = "fleet-worker"
EXIT_DONE, EXIT_HOOK, EXIT_USAGE, EXIT_CLOUD, EXIT_TIMEOUT, EXIT_CAP, EXIT_REFUSED = 0, 1, 2, 3, 4, 5, 6

CREDENTIAL_PATTERNS = [re.compile(p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"sk-[A-Za-z0-9]{20,}", r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY", r"Bearer [A-Za-z0-9._-]{20,}", r"(?i)(client_secret|api[_-]?key)\"\s*:\s*\"[^\"]+")]
SECRET_ENV_NAME = re.compile(r"(?i)(TOKEN|SECRET|PASSWORD|API_?KEY|CREDENTIAL|^PAT$|_PAT$)")
ALLOWED_ENV_NAMES = {"FLEET_TOKEN_ENDPOINT"}  # a URL, not a token


class CloudError(Exception):
    def __init__(self, msg, code=EXIT_CLOUD):
        super().__init__(msg)
        self.code = code


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
        self.bin = env.get("FLEET_KUBECTL", "kubectl")
        self.ns = env.get("FLEET_CLOUD_NAMESPACE", "fleet-workers")

    def run(self, args, stdin=None, ok_missing=False, keep_stdout=False):
        cmd = [self.bin, "-n", self.ns] + args
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120, env=self.env)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CloudError("kubectl failed: %s" % e)
        if p.returncode != 0:
            if keep_stdout and p.stdout.strip():
                return p.stdout  # the caller reads the failure from the tool's own JSON
            if ok_missing and "NotFound" in p.stderr:
                return None
            last = (p.stderr.strip().splitlines() or ["failed"])[-1]
            raise CloudError("kubectl %s: %s" % (args[0], re.sub(r"[^\x20-\x7e]", "?", last)[:300]))
        return p.stdout

    def apply(self, obj):
        self.run(["apply", "-f", "-"], stdin=json.dumps(obj))

    def get_pod(self, name):
        out = self.run(["get", "pod", name, "-o", "json"], ok_missing=True)
        return json.loads(out) if out else None

    def list_pods(self):
        return json.loads(self.run(["get", "pods", "-l", "app=" + LABEL, "-o", "json"]))["items"]

    def annotate(self, name, **kv):
        self.run(["annotate", "pod", name, "--overwrite"] + ["fleet/%s=%s" % (k.replace("_", "-"), v) for k, v in kv.items()])

    def delete(self, name):
        self.run(["delete", "pod", name, "--ignore-not-found", "--wait=false"])
        self.run(["delete", "secret", name + "-pw", "--ignore-not-found"])

    def exec(self, name, *cmd, keep_stdout=False):
        return self.run(["exec", name, "--"] + list(cmd), keep_stdout=keep_stdout)


def ann(pod, key, default=None):
    return pod["metadata"].get("annotations", {}).get("fleet/" + key, default)


def render_pod(a, worker, name, now, hard, env):
    image = a.image or env.get("FLEET_CLOUD_IMAGE")
    if not image:
        raise CloudError("no image: pass --image or set FLEET_CLOUD_IMAGE", EXIT_USAGE)
    endpoint = env.get("FLEET_TOKEN_ENDPOINT")
    if not endpoint:
        raise CloudError("no token endpoint: set FLEET_TOKEN_ENDPOINT", EXIT_USAGE)
    shape = SHAPES[a.shape]
    e = {"FLEET_WORKER": worker, "FLEET_REPO": a.repo, "FLEET_BRANCH": a.branch,
         "FLEET_IDLE_SECONDS": str(parse_duration(a.idle)), "FLEET_HARD_DEADLINE": iso(hard),
         "FLEET_TOKEN_ENDPOINT": endpoint, "FLEET_AUDIT_RUNTIME_ID": "pod/" + name}
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {
        "name": name, "labels": {"app": LABEL, "fleet/worker": worker, "azure.workload.identity/use": "true"},
        "annotations": {"fleet/worker": worker, "fleet/started": iso(now), "fleet/hard-deadline": iso(hard),
                        "fleet/idle-seconds": e["FLEET_IDLE_SECONDS"], "fleet/shape": a.shape,
                        "fleet/max-hard": iso(now + dt.timedelta(seconds=parse_duration(a.max_hard)))}},
        "spec": {"restartPolicy": "Never", "serviceAccountName": env.get("FLEET_CLOUD_SERVICE_ACCOUNT", "fleet-worker"),
                  "securityContext": {"runAsNonRoot": True},
                 "containers": [{"name": "worker", "image": image,
                                 "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}, "env": [{"name": k, "value": v} for k, v in e.items()],
                                 "resources": {"limits": {"cpu": shape["cpu"], "memory": shape["memory"]}},
                                 "readinessProbe": {"httpGet": {"path": "/health", "port": 4096}},
                                 "volumeMounts": [{"name": "pw", "mountPath": "/run/fleet", "readOnly": True},
                                                  {"name": "tokens", "mountPath": "/run/fleet-tmp"},
                                                  {"name": "scratch", "mountPath": "/work"}]}],
                 "volumes": [{"name": "pw", "secret": {"secretName": name + "-pw", "defaultMode": 0o600,
                                                       "items": [{"key": "password", "path": "server-password"}]}},
                             {"name": "tokens", "emptyDir": {"medium": "Memory"}},
                             {"name": "scratch", "emptyDir": {}}]}}


class Launcher:
    def __init__(self, kube, env, clock=None, sleep=time.sleep):
        self.kube, self.env, self.sleep = kube, env, sleep
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.audit_path = env.get("FLEET_CLOUD_AUDIT") or os.path.join(
            env.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "fleet-cloud", "audit.jsonl")

    # -- audit: one line per action, no secret, written before the action returns --
    def audit(self, worker, runtime_id, action, cost, ok, reason=""):
        os.makedirs(os.path.dirname(self.audit_path), exist_ok=True)
        line = {"ts": iso(self.clock()), "worker": worker, "runtime_id": runtime_id, "action": action,
                "cost_estimate_usd": cost, "ok": ok, "actor": pwd.getpwuid(os.getuid()).pw_name, "reason": reason}
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
        try:
            result = fn(info)
        except CloudError as e:
            self.audit(worker, info["rid"], action, info["cost"], False, reason or str(e))
            raise
        except (ValueError, TypeError, KeyError) as e:  # a bad annotation or reply still writes its ok:false line
            self.audit(worker, info["rid"], action, info["cost"], False, reason or str(e)[:300])
            raise CloudError("unexpected cloud data: %s" % str(e)[:200])
        self.audit(worker, info["rid"], action, info["cost"], True, reason)
        return result

    def _need(self, worker):
        if not NAME_RE.match(worker or ""):
            raise CloudError("bad worker name %r" % worker, EXIT_USAGE)
        pod = self.kube.get_pod(pod_name(worker))
        return pod

    # -- create --
    def create(self, a):
        worker, name = a.worker, pod_name(a.worker)

        def go(info):
            info["rid"] = "pod/" + name
            if not NAME_RE.match(worker):
                raise CloudError("bad worker name %r" % worker, EXIT_USAGE)
            if a.shape not in SHAPES:
                raise CloudError("shape must be S or M", EXIT_USAGE)
            now = self.clock()
            hard = now + dt.timedelta(seconds=parse_duration(a.hard))
            max_hard = now + dt.timedelta(seconds=parse_duration(a.max_hard))
            if hard > max_hard:
                raise CloudError("--hard is past --max-hard", EXIT_REFUSED)
            cap = self.env.get("FLEET_CLOUD_SPEND_CAP_USD")  # OPEN in the contract: who sets it; an env value for now
            if cap and sum(self.cost(p) for p in self.kube.list_pods()) >= float(cap):
                raise CloudError("spend cap reached", EXIT_CAP)
            spec = render_pod(a, worker, name, now, hard, self.env)
            hits = scan_credentials(spec)
            if hits:
                raise CloudError("refusing a pod spec with a stored credential: %s" % ", ".join(hits), EXIT_USAGE)
            if self.kube.get_pod(name):
                raise CloudError("worker %s already exists" % worker, EXIT_USAGE)
            password = secrets.token_urlsafe(32)
            # Single operator assumed: the check above and the applies below are not one atomic step.
            try:
                self.kube.apply({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name + "-pw", "labels": {"app": LABEL}},
                                 "type": "Opaque", "stringData": {"password": password}})
                self.kube.apply(spec)
                self._wait_ready(name, a.wait)
                if a.password_fd is not None:
                    with os.fdopen(os.dup(a.password_fd), "w") as f:
                        f.write(password)
            except (CloudError, OSError) as e:
                self._cleanup(name)  # nothing is left running (and costing) or holding a password
                raise e if isinstance(e, CloudError) else CloudError("could not hand over the password: %s" % e)
            domain = self.env.get("FLEET_CLOUD_DOMAIN", "example.internal")
            return {"worker": worker, "url": "https://worker-%s.%s:4096" % (name, domain),
                    "auth_ref": "file:/run/fleet/%s.password" % worker, "runtime_id": "pod/" + name,
                    "idle_deadline": iso(now + dt.timedelta(seconds=parse_duration(a.idle))), "hard_deadline": iso(hard),
                    "password_fd": a.password_fd}
        return self._audited(worker, "create", go)

    def _cleanup(self, name):
        try:
            self.kube.delete(name)
        except CloudError:
            pass  # the original failure is the one to report

    def _wait_ready(self, name, seconds):
        end = self.clock() + dt.timedelta(seconds=seconds)
        while True:
            pod = self.kube.get_pod(name)
            status = (pod or {}).get("status", {})
            if status.get("phase") == "Failed":
                raise CloudError("worker pod failed to start")
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])):
                return
            if self.clock() >= end:
                raise CloudError("timed out waiting for the worker", EXIT_TIMEOUT)
            self.sleep(2)

    # -- end hook, stop, delete --
    def run_hook(self, name):
        out = self.kube.exec(name, "fleet-end-hook", keep_stdout=True)
        try:
            res = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"pushed": False, "wip_branch": None, "error": "end hook gave no result"}
        if not isinstance(res, dict):
            return {"pushed": False, "wip_branch": None, "error": "end hook gave no result"}
        if res.get("error"):  # text from inside the pod and the remote: printable, short, before it is shown or audited
            res["error"] = re.sub(r"[^\x20-\x7e]", "?", str(res["error"]))[:300]
        return res

    def stop(self, a, reason=None):
        worker, name = a.worker, pod_name(a.worker)
        reason = reason if reason is not None else (a.reason or "")

        def go(info):
            pod = self._need(worker)
            if not pod:
                return {"worker": worker, "runtime_id": "", "pushed": False, "wip_branch": None}
            info["rid"], info["cost"] = "pod/" + name, self.cost(pod)
            try:
                res = self.run_hook(name)
            except CloudError as e:
                res = {"pushed": False, "wip_branch": None, "error": str(e)}
            if res.get("error"):
                self.kube.annotate(name, kept="true")  # the pod is kept: the commit is not safe yet
                raise CloudError("end hook could not push, pod kept: %s" % res["error"], EXIT_HOOK)
            self.kube.annotate(name, hook_ran="true")
            self.kube.delete(name)
            skipped = [re.sub(r"[^\x20-\x7e]", "?", str(x))[:200] for x in (res.get("skipped") or [])][:50]
            if skipped:  # these files were not pushed and went with the pod: say so, loudly
                print("cloud-worker: WARNING: not saved (secret-looking names): %s" % ", ".join(skipped), file=sys.stderr)
            return {"worker": worker, "runtime_id": "pod/" + name, "pushed": bool(res.get("pushed")),
                    "wip_branch": res.get("wip_branch"), "skipped": skipped}
        return self._audited(worker, "stop", go, reason)

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
            d = json.loads(self.kube.exec(name, "fleet-worker-activity"))
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
        rd = self.env.get("FLEET_CLOUD_RECORD_DIR")
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
    c.add_argument("--wait", type=int, default=300)
    c.add_argument("--password-fd", type=int, help="a file descriptor the caller holds; the password is written there once")
    s = cmd("stop")
    s.add_argument("--reason", default="")
    e = cmd("extend")
    e.add_argument("--hard")
    e.add_argument("--idle")
    e.add_argument("--reason", default="")
    d = cmd("delete")
    d.add_argument("--force", action="store_true")
    d.add_argument("--reason", default="")
    cmd("status")
    r = cmd("reap", worker=False)
    r.add_argument("--loop", type=int, help="seconds between passes (default: one pass)")
    return p


def main(argv=None, env=None, clock=None, sleep=time.sleep):
    env = os.environ if env is None else env
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else 0
    L = Launcher(Kube(env), env, clock, sleep)
    try:
        if a.cmd == "reap":
            while True:
                res = L.reap()
                print(json.dumps([{"worker": w, "reason": r, "exit": c} for w, r, c in res]))
                if not a.loop:
                    return EXIT_HOOK if any(c for _, _, c in res) else 0
                sleep(min(a.loop, 60))  # never slower than the contract's once a minute
        result = getattr(L, a.cmd)(a)
        print(json.dumps(result, sort_keys=True))
        return 0
    except CloudError as e:
        print("cloud-worker: %s" % e, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
