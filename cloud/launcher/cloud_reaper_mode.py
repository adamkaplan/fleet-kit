"""The reaper in the cluster (ask #1582): cluster mode, the lease, the token client and the liveness reading.

Cluster mode logs in with the service account's own token file (Kube reads it by path) and runs no vault command and no personal-login
command: ClusterModeRefresher never mints. The git token comes from the token service (token_client.py, set as CW_GIT_TOKEN_CMD): a request
that names the repository and the worker role, carrying the service account's token as its bearer and no key. The lease is a Kubernetes
Lease object; whoever holds it acts, the other yields until it has lapsed. The liveness is the Lease's own renew time (no second object).
"""
import datetime as dt
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LEASE_NAME = "cw-reaper"
LEASE_SECONDS = 180          # a holder that has not renewed for this long has lapsed
LEASE_READ_SECONDS = 10      # the Mac's one lease read
BEAT_SECONDS = 30            # the lease is renewed during a pass whenever this long has passed (well inside the 60 s the plan asks)
MISSING_PASS_MINUTES = 10    # a renew time older than this is an alert
ROLE = "worker"


class ClusterModeRefresher:
    """Stands where ClusterTokenRefresher stands: it never reads a vault and never makes a login. The token file is the pod's projected one."""
    notices = []

    def __init__(self, env=None):
        self.kube = None

    def run(self, force=False):
        from cloud_worker import TokenRefresh
        return TokenRefresh("skipped")


class LeaseGate:
    """store.get() -> (holder, renew datetime, version) or None; store.write(holder, now, version) -> True, or False on a lost race (version None: create)."""

    def __init__(self, store, holder, clock, seconds=LEASE_SECONDS):
        self.store, self.holder, self.clock, self.seconds = store, holder, clock, seconds
        self.last = None   # when this holder last wrote the lease

    def __call__(self, _launcher=None):
        now = self.clock()
        cur = self.store.get()
        if cur is None:
            ok = self.store.write(self.holder, now, None)
        else:
            holder, renewed, version = cur
            ok = (holder == self.holder or (now - renewed).total_seconds() > self.seconds) and self.store.write(self.holder, now, version)
        self.last = now if ok else None   # a failed write, a conflict or another holder: this holder does not hold it
        return ok

    def holds(self):
        """True while this holder's last write is younger than the lease time."""
        return self.last is not None and (self.clock() - self.last).total_seconds() <= self.seconds

    def beat(self):
        """Called before each stop and each renewal of a pass: renews the lease when BEAT_SECONDS have passed since the last renewal.
        Returns whether this holder still holds it (a failed write, a conflict or a read showing another holder: False)."""
        if self.last is None or (self.clock() - self.last).total_seconds() >= BEAT_SECONDS:
            try:
                return bool(self())
            except Exception:
                self.last = None
                return False
        return True


class MacYieldGate:
    """The Mac's (non-cluster) pass: it reads the one Lease through its own cluster login and acts on nothing while another holder has it
    and it has not lapsed. FAIL OPEN: a Lease that cannot be read (no right, not found, the cluster unreachable, any error) leaves the
    Mac acting as it always did. It never writes the Lease (it holds no right to)."""

    def __init__(self, clock, holder="mac", seconds=LEASE_SECONDS):
        self.clock, self.holder, self.seconds = clock, holder, seconds

    renewal_allowed = None   # set below: the ticker's check, the same read

    def __call__(self, launcher):
        try:
            cur = KubectlLeaseStore(launcher.kube).get(timeout=LEASE_READ_SECONDS)   # a short bound: a hung read fails open in 10 s, not 120
            if cur is None:
                return True
            holder, renewed, _ = cur
            return holder == self.holder or (self.clock() - renewed).total_seconds() > self.seconds
        except Exception:
            return True


MacYieldGate.renewal_allowed = lambda self, launcher: self(launcher)  # noqa: E731


class KubectlLeaseStore:
    """The Lease through the launcher's own Kube door: get, create and replace (the rights get, create, update)."""

    def __init__(self, kube, name=LEASE_NAME):
        self.kube, self.name = kube, name

    def get(self, timeout=120):
        out = self.kube.run(["get", "lease", self.name, "-o", "json"], ok_missing=True, timeout=timeout)
        if not out:
            return None
        d = json.loads(out)
        spec = d.get("spec", {})
        t = dt.datetime.strptime(spec["renewTime"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
        return spec.get("holderIdentity"), t, d["metadata"].get("resourceVersion")

    def write(self, holder, now, version):
        body = {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": {"name": self.name},
                "spec": {"holderIdentity": holder, "leaseDurationSeconds": LEASE_SECONDS,
                         "renewTime": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")}}
        if version is not None:
            body["metadata"]["resourceVersion"] = version   # a replace of a stale version is refused by the cluster
        try:
            self.kube.run(["create" if version is None else "replace", "-f", "-"], stdin=json.dumps(body))
            return True
        except Exception:
            return False


def alert_line(renewed, now, minutes=MISSING_PASS_MINUTES):
    """ONE line naming the reaper and the minutes when the last pass is older than `minutes`; None for a fresh one."""
    gone = int((now - renewed).total_seconds() // 60)
    if gone <= minutes:
        return None
    return "cloud reaper: no pass for %d minutes (limit %d)" % (gone, minutes)


def status_line(renewed):
    return "cloud reaper last pass: %s" % renewed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def request_token(repo, url, bearer, opener=urllib.request.urlopen):
    """Ask the token service for one repository and the worker role. The only credential sent is the service account's own bearer."""
    req = urllib.request.Request(url, data=json.dumps({"repository": repo, "role": ROLE}).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + bearer, "Content-Type": "application/json"})
    with opener(req, timeout=30) as r:
        return json.loads(r.read(65536))["token"]


def main(env=None):
    """CW_GIT_TOKEN_CMD of the cluster reaper: prints the token for GH_REPO."""
    env = os.environ if env is None else env
    with open(env["CW_CLUSTER_TOKEN_FILE"]) as f:
        bearer = f.read(65536).strip()
    sys.stdout.write(request_token(env["GH_REPO"], env["CW_TOKEN_SERVICE_URL"], bearer))


def run_cluster_reaper(env=None, holder="cloud", loop=60, sleep=None, clock=None):
    """The in-cluster reaper's entry: the service account's own token file for the login, no vault command, the lease as its gate, the
    token service for git tokens (CW_GIT_TOKEN_CMD points at this file's main)."""
    import argparse
    import time
    env = dict(os.environ if env is None else env)
    env.setdefault("CW_GIT_TOKEN_CMD", "%s %s" % (sys.executable, os.path.abspath(__file__)))
    clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    lease = []

    def gate(launcher):
        if not lease:
            lease.append(LeaseGate(KubectlLeaseStore(launcher.kube), holder, clock))
        launcher.beat = lease[0].beat   # renewed again during the pass, before each stop and renewal; its result says whether the lease is still held
        return lease[0]()
    gate.renewal_allowed = lambda launcher: bool(lease) and lease[0].holds()   # the renewal thread yields: no renewal while this reaper does not hold the lease
    import cloud_worker
    return cloud_worker._reap_main(env, argparse.Namespace(loop=loop), sleep or time.sleep, clock=clock,
                                   refresher=ClusterModeRefresher(env), gate=gate)


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        sys.exit(run_cluster_reaper())
    main()
