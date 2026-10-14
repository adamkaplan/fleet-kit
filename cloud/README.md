# cloud/: worker image and Sysbox node setup

Part of the cloud-worker prototype (plan scenarios S10 and S12). Two pieces:

- `worker/`: the worker image (OpenCode headless, git, gh, Node, Python, Docker for use under Sysbox, non-root)
  and `scan-secrets.sh`, a build-failing scan for stored credentials.
- `k8s/`: the RuntimeClass and a DaemonSet that repairs the containerd registration of Sysbox on a node, repeatably.

The launcher contract (environment, endpoint, password handling) lives in `docs/cloud-worker-interface.md` once it
lands; this image takes everything it needs from the environment at start and stores nothing.

## What is in the image

Ubuntu 24.04, `node` 22 (copied from the official image), Python 3, git, `gh`, Docker Engine with the compose plugin,
`opencode`. It runs as user `worker` (uid 1000). `worker` may run one command as root (`start-dockerd`) through sudo;
the entrypoint uses it to start the nested `dockerd` and then execs the command. Default command:
`opencode serve --hostname 0.0.0.0 --port 4096`. Set `WORKER_DOCKER=0` to skip the Docker daemon. No credential is
built in: `OPENCODE_SERVER_PASSWORD`, `GH_TOKEN` and any cloud identity are supplied by the launcher at run time.

## The Sysbox node fix

The stock Sysbox installer writes the `sysbox-runc` runtime under the legacy plugin key
`io.containerd.grpc.v1.cri`. containerd 2.x (AKS node image with containerd 2.3.6) reads
`io.containerd.cri.v1.runtime`, so pods fail with `RuntimeClass handler "sysbox-runc" ... is not known`.
`k8s/containerd-fix.yaml` runs on every node labelled `sysbox-install=yes`: it waits for the installer's stanza, renames
the plugin key on the `sysbox-runc` tables, checks `containerd config dump`, restarts containerd only if it changed
the file (restoring a backup if the dump fails), then labels the node `sysbox-runtime=running`. A node already right is
left alone. `k8s/runtimeclass.yaml` schedules `sysbox-runc` pods only on nodes with that label, so no pod lands before
the fix. Evidence for the cause: issue #365, probe comment.

## Tests (no cluster, no Azure)

```sh
cloud/tests/test-cloud          # prints RESULT: PASS
```

It runs `kubectl apply --dry-run=client` on each manifest (an offline parse when no API server answers), runs the
manifest's own `sed` on a legacy-keyed config twice (second pass changes nothing), and checks the scan: 0 hits on a clean
directory and image, a failure on a planted token and on an image with one baked in, and that it never prints the value.

## Build and scan the image

```sh
docker build -t fleet-worker:dev -f cloud/worker/Dockerfile cloud     # image reference is yours to publish
cloud/worker/scan-secrets.sh fleet-worker:dev                        # must print "0 hits", exit 0; run it before every push
docker push <registry>/fleet-worker:<tag>                            # only after the scan passes
```

The scan covers every layer (including files deleted in a later layer) and the image config (env, history). It prints
`path: pattern-name`, never the value. A hit exits 1, so a build pipeline that runs it fails the build.

## Live proof (S10 runtime half and S12), run on a cluster

Needs a cluster with one Ubuntu node whose containerd is 2.x, and `kubectl` pointed at it. Replace `<image>` with the
published image. Nothing here needs a stored secret.

```sh
NODE=$(kubectl get nodes -o name | head -1 | cut -d/ -f2)

# 1. install Sysbox (upstream installer), then the fix and RuntimeClass
kubectl label node "$NODE" sysbox-install=yes
kubectl apply -f https://raw.githubusercontent.com/nestybox/sysbox/master/sysbox-k8s-manifests/sysbox-install.yaml
kubectl apply -f cloud/k8s/containerd-fix.yaml
kubectl apply -f cloud/k8s/runtimeclass.yaml      # if the installer already created it, this updates it
kubectl -n kube-system logs ds/sysbox-containerd-fix --tail=20
kubectl get node "$NODE" -L sysbox-runtime        # expect: running (no manual edit of config.toml)

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
