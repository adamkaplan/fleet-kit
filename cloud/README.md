# cloud/: worker image and Sysbox node setup

Part of the cloud-worker prototype (plan scenarios S10 and S12). Two pieces:

- `worker/`: the worker image (OpenCode headless, git, gh, Python, Docker for use under Sysbox, non-root)
  and `scan-secrets.sh`, a build-failing scan for stored credentials.
- `k8s/`: the RuntimeClass and a DaemonSet that repairs the containerd registration of Sysbox on a node, repeatably.

This image takes everything it needs from the environment at start and stores nothing. The launcher, run secrets,
service account and driver are later pull requests; this one is only the image, the scan and the node fix.

Cloud workers serve the organisation's repositories only; no task, test or demo of the tooling that runs the workers runs in a cloud worker.

## What is in the image

Ubuntu 24.04, Python 3, git, `gh`, Docker Engine with the compose plugin, and `opencode` v2 (pinned,
`OPENCODE_VERSION=2.0.28`, a standalone binary checked against its sha256 before it is unpacked; no node or npm). The
launcher's end hook and token mint are copied in as `cw-end-hook` and `cw-token-mint`, and the activity reader as
`cw-worker-activity` (it reads the v2 object shape of `GET /api/session/active`). It runs as user `worker` (uid 1000). `worker` may run one command as root (`start-dockerd`) through sudo;
the entrypoint uses it to start the nested `dockerd` and then execs the command. Default command:
`opencode serve --hostname 0.0.0.0 --port 4096`. Set `WORKER_DOCKER=0` to skip the Docker daemon. No credential is
built in: `OPENCODE_SERVER_PASSWORD`, `GH_TOKEN` and any cloud identity are supplied by the launcher at run time.

## The Sysbox node fix

The stock Sysbox installer writes the `sysbox-runc` runtime under the legacy plugin key
`io.containerd.grpc.v1.cri`. containerd 2.x (AKS node image with containerd 2.3.6) reads
`io.containerd.cri.v1.runtime`, so pods fail with `RuntimeClass handler "sysbox-runc" ... is not known`.
`k8s/containerd-fix.yaml` runs on every node labelled `sysbox-install=yes`: it waits for the installer's stanza, renames
the plugin key on the `sysbox-runc` tables, checks `containerd config dump`, restarts containerd only if it changed
the file (restoring a backup if the dump fails), then labels the node `sysbox-runtime=running` (a record only: the installer sets the same label before containerd knows the runtime, so it is not a health signal and no pod may be selected or placed by it). A node already right is
left alone.

The node file shape it handles: table headers may be indented (2 spaces per level) and use either quote style. The
installer (v0.7.1-0) writes `[plugins."io.containerd.grpc.v1.cri"]`, its `.containerd` and `.containerd.runtimes`
parents and the `sysbox-runc` tables (and `.options`) under the legacy key, while the real runtimes (`runc`,
`untrusted`) sit under `io.containerd.cri.v1.runtime`. The fix renames the key on the `sysbox-runc` tables only, removes
the legacy parent headers left with no keys and no child tables (never one that still holds something), leaves every
other stanza byte-identical, and refuses a result with a duplicate table header. The older column-0 form is still
handled. `tests/fixtures/containerd-aks-installer-form.toml` is a copy of such a file.

`k8s/runtimeclass.yaml` has no scheduling block: an autoscaler's template node cannot carry a label that is set only
after install, so a worker pod selects the pool label `sysbox-install=yes` itself. Evidence for the cause: issue #365, probe comment.

## Tests (no cluster, no Azure)

```sh
cloud/tests/test-cloud          # prints RESULT: PASS
```

It runs `kubectl apply --dry-run=client` on each manifest (an offline parse when no API server answers), runs the
manifest's own `sed` on a legacy-keyed config twice (second pass changes nothing), and checks the scan: 0 hits on a clean
directory and image, a failure on a planted token and on an image with one baked in, and that it never prints the value.

## Build and scan the image

```sh
docker build -t cw-worker:dev -f cloud/worker/Dockerfile cloud     # image reference is yours to publish
# with Azure Container Registry, the working directory must be cloud/ (the build context), then use -f worker/Dockerfile:
(cd cloud && az acr build --registry <registry> --image cw-worker:<tag> -f worker/Dockerfile .)
cloud/worker/scan-secrets.sh cw-worker:dev                        # must print "0 hits", exit 0; run it before every push
docker push <registry>/cw-worker:<tag>                            # only after the scan passes
```

The scan covers every layer (including files deleted in a later layer) and the image config (env, history). It prints
`path: pattern-name`, never the value. A hit exits 1, so a build pipeline that runs it fails the build.

### Resetting a node the fix gave up on

When the fix cannot bring a node back (a failed restore, or containerd still unhealthy after the rollback) it sets its
failed-attempt marker to the cap and exits non-zero once; later pods then idle and restart nothing. After the node has
been repaired by a person, reset BOTH files on the node, then delete the fix pod:

```sh
rm -f /etc/containerd/config.toml.sysbox-fix.failed /etc/containerd/config.toml.sysbox-fix.inprogress
```

## Live proof (S10 runtime half and S12), run on a cluster

Needs a cluster with one Ubuntu node whose containerd is 2.x, and `kubectl` pointed at it. Replace `<image>` with the
published image. Nothing here needs a stored secret.

```sh
NODE=$(kubectl get nodes -o name | head -1 | cut -d/ -f2)

# 1. install Sysbox (upstream installer), then the fix and RuntimeClass
kubectl label node "$NODE" sysbox-install=yes
kubectl apply -f https://raw.githubusercontent.com/nestybox/sysbox/<pinned commit on master>/sysbox-k8s-manifests/sysbox-install.yaml
# the manifest at tag v0.7.1 pins sysbox-deploy-k8s:v0.7.0-0, which rejects Kubernetes 1.36: use the manifest from master
# (image v0.7.1-0), or set the image, pinned by digest (the digest seen on a working node):
kubectl -n kube-system set image ds/sysbox-deploy-k8s \
  sysbox-deploy-k8s=registry.nestybox.com/nestybox/sysbox-deploy-k8s@sha256:4237a2083c71bce9ad9d3c3917dd5334c2b20682f8593d625ed1a6efb22b118d   # tag v0.7.1-0
kubectl apply -f cloud/k8s/containerd-fix.yaml
kubectl apply -f cloud/k8s/runtimeclass.yaml      # if the installer already created it, this updates it
kubectl -n kube-system logs ds/sysbox-containerd-fix --tail=20
# The node label sysbox-runtime=running is NOT proof: the stock installer sets it before containerd knows the runtime.
# The proof is (a) the fix pod's log (above) showing containerd answered ('ctr version') after the restart, and
# (b) a nested pod on the sysbox-runc class running Docker (no manual edit of config.toml):
kubectl run sysbox-check --restart=Never --rm -i --image=<image> --overrides='{"spec":{"runtimeClassName":"sysbox-runc","hostUsers":false}}' \
  --command -- /bin/bash -c 'docker run --rm hello-world'     # expect: "Hello from Docker!", exit 0

# 2. S12: a compose stack of a database and an app, inside one worker, then gone
cat <<'YAML' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata: {name: worker-s12}
spec:
  runtimeClassName: sysbox-runc
  hostUsers: false
  restartPolicy: Never
  containers:
    - name: worker
      image: <image>
      command: ["/bin/bash", "-c"]
      args:
        - |
          set -e
          mkdir /tmp/s && cd /tmp/s
          cat > compose.yaml <<'C'
          services:
            db: {image: postgres:16-alpine, environment: {POSTGRES_PASSWORD: test}}
            app: {image: alpine:3.20, depends_on: [db], command: ["sh","-c","sleep 5 && echo app-up"]}
          C
          docker compose up -d && sleep 8 && docker compose logs app | grep app-up
          docker compose ps
YAML
kubectl wait pod/worker-s12 --for=jsonpath='{.status.phase}'=Succeeded --timeout=10m
kubectl logs worker-s12 | tail -5                  # expect app-up; compose up exit status 0
kubectl get pod worker-s12 -o jsonpath='{.spec.volumes[*].name} {.spec.containers[0].securityContext}'  # no hostPath, not privileged

# 3. after the worker ends: nothing of it remains on the host
kubectl delete pod worker-s12
kubectl debug node/"$NODE" -it --image=busybox -- chroot /host sh -c \
  'crictl ps -a | grep -c -E "postgres|alpine"; crictl pods | grep -c worker-s12'    # expect 0 and 0

# 4. S10 runtime half: no stored credential in the running worker's environment or files
kubectl run worker-s10 --image=<image> --restart=Never --overrides='{"spec":{"runtimeClassName":"sysbox-runc","hostUsers":false}}' -- sleep 600
kubectl wait pod/worker-s10 --for=condition=Ready
kubectl exec worker-s10 -- env | grep -c -E 'TOKEN|SECRET|PASSWORD|KEY'          # expect 0 (a launcher-set password is per worker, minted at start)
kubectl delete pod worker-s10
```

Not run by the author (no cluster available): everything in this section. The token-lifetime half of S10 (at most 60
minutes) belongs to the launcher.

## Trust boundaries and limits

- `containerd-fix.yaml` is a node-root component: privileged, `hostPID`, and a ClusterRole that can `get` and `patch`
  nodes. The label `sysbox-install=yes` is the only gate on where it runs; the real boundary is who may patch node labels (the DaemonSet's own token can patch any node), so restrict that RBAC.
- Sysbox is the boundary that makes the worker's `docker` group and `sudo start-dockerd` safe. Do not run the image
  outside a Sysbox pod with `WORKER_DOCKER` left at 1 on a shared host.
- The scan finds known credential shapes only; "0 hits" is not proof that no secret exists.
- The fixer image is pinned by tag, not digest: pin it for a release (the OpenCode binary is already pinned with its sha256).
- `cloud/tests/test-cloud` is not yet run by CI (this change is limited to `cloud/`); it needs Docker for the image cases
  (`CLOUD_TEST_SKIP_DOCKER=1` skips them explicitly).
