# cloud/: worker image and Sysbox node setup

Part of the cloud-worker prototype (plan scenarios S10 and S12). Two pieces:

- `worker/`: the worker image (OpenCode headless, git, gh, Python, Docker for use under Sysbox, non-root)
  and `scan-secrets.sh`, a build-failing scan for stored credentials.
- `k8s/`: the RuntimeClass and a DaemonSet that repairs the containerd registration of Sysbox on a node, repeatably.

This image takes everything it needs from the environment and mounted files at start and stores nothing. The launcher
(`cloud/launcher/`) supplies the per-run files; the entrypoint installs them (next section) and the operator driver
(`cloud/launcher/run_task.py`) runs one task end to end.

Cloud workers serve the organisation's repositories only; no task, test or demo of the tooling that runs the workers runs in a cloud worker.

## What is in the image

Ubuntu 24.04, Python 3, git, `gh`, Docker Engine with the compose plugin, and `opencode` v2 (pinned,
`OPENCODE_VERSION=2.0.28`, a standalone binary checked against its sha256 before it is unpacked; no node or npm). The
launcher's end hook and token mint are copied in as `cw-end-hook` and `cw-token-mint`, and the activity reader as
`cw-worker-activity` (it reads the v2 object shape of `GET /api/session/active`). It runs as user `worker` (uid 1000). `worker` may run one command as root (`start-dockerd`) through sudo;
the entrypoint uses it to start the nested `dockerd` and then execs the command. Default command:
`opencode serve --hostname 0.0.0.0 --port 4096`. Set `WORKER_DOCKER=0` to skip the Docker daemon. No credential is
built in: `OPENCODE_SERVER_PASSWORD`, `GH_TOKEN` and any cloud identity are supplied by the launcher at run time.

## Per-run secrets in the image

The launcher mounts per-run files read-only under `/run/cw` (`CW_RUN_DIR`). The entrypoint, which never prints a value:

- `server-password`: read into `OPENCODE_SERVER_PASSWORD` for the server it starts (OpenCode reads the password only from its environment).
- `model-auth`: the model provider's access entry (JSON), copied with mode 600 to `<tmpfs>/data/opencode/auth.json` (`XDG_DATA_HOME`), so the server refreshes its short-lived token in the copy and never in the Secret. A model config naming `CW_MODEL` (default `github-copilot/claude-sonnet-5.5`) is written beside it (`XDG_CONFIG_HOME`); it holds no secret.
- `git-token`: never copied. A git credential helper (`git config --global`, so every process of the worker user sees it, including a fresh-environment `kubectl exec` such as the end hook) and a `gh` wrapper read the file at call time. The helper answers only for `https://github.com`; the token is never in git config, argv or the environment. The directory is the path baked in when the files were generated (a caller's `CW_RUN_DIR` changes nothing), and at every call an unreadable or empty token file makes both fail with one line and a non-zero exit (never an empty password, never a `gh` with an empty token). The `gh` wrapper passes every subcommand: the guard is the token's own permissions (a per-run, one-repository App token), not the wrapper. The server password is in the server's environment, so every process the agent starts can read it; that is accepted by design.

A file that is absent is skipped (a pod started without per-run secrets). A file that is present but unreadable or empty
refuses the start with one line naming it; there is no fallback to another source. The tmpfs is `CW_TMP_DIR` (default `/run/cw-tmp`).

## The worker's rules in the pod (a convenience, not the guard)

A cloud worker holds the same rules a local agent has. The entrypoint writes an OpenCode config (on tmpfs, no secret in it)
that defines a `worker` agent (`worker.md`, with the STOP rules in prose) and denies, for the shell tool and for every agent, six
kinds of command: merging a pull request (`gh pr merge`, the API merge call, the GraphQL merge mutations), force-pushing
(`--force`, `-f`, `--force-with-lease`, `+ref`), pushing to the default branch, writing issues, writing labels, and
repository settings calls (`gh repo edit/delete/archive`, `gh api` with PATCH/PUT/DELETE, branch protection, rulesets,
webhooks, secrets). The `gh` and `git` wrappers first on the worker's PATH (installed with the token file) refuse the same
six with exit status 3 and one line, `refused: <what>: the repository's rules are the guard; this wrapper is only a
convenience`; the line names a fixed phrase and never an argument value. Everything else passes unchanged (the `gh` wrapper
still reads the token file at call time).

What this is NOT: the guard. Both layers are defence in depth and can be bypassed: calling the real binary by its path,
another HTTP client or a script skips the wrapper, and a pattern is only text matching. The guard is the repository's own
rules (branch protection or a ruleset that forbids merge by the worker, pushes to the default branch and force-pushes) and
the token's scope (the preflight, #545). Known limits: the `permission.bash` block was checked to load and convert in a local OpenCode 2.0.28 server (it becomes `shell` deny rules), but that server did not list the `worker` agent in isolation, so the agent definition is not proven to register or to be the default (no `default_agent` is set); the global deny block applies to every agent either way. the config's patterns name `main` and `master` only (the wrapper
also reads `CW_DEFAULT_BRANCH`, space or comma separated, and `origin/HEAD`), `git push` from a checkout of the default
branch is refused by the wrapper only, and the config's patterns can over-refuse (a branch named `fix-main-menu`). A
missing guard file refuses the start. Tests: `cloud/worker/tests/test_guard.py`.

### What else the wrapper refuses, and what it leaves (review round)

Beyond the six, the wrapper (and, where a text pattern can say it, the OpenCode config) refuses:

- **Aliases are covered.** `gh alias set|import|delete`, `git config` of any `alias.*` key and `git -c alias.*` (also `--config-env`) are refused, and a first word of `gh` it does not know is refused as a possible alias (`gh m 5`). A built-in command passes. The config cannot name an unknown word, so that part is the wrapper's only. Extensions are refused too (`gh extension install`, and any `gh extension` except `list|search|browse`); an installed extension's own name is an unknown word.
- **`gh api` endpoints** are read after a scheme, host, port, userinfo, query, fragment and leading or trailing slash are stripped (`https://api.github.com/repos/o/r/pulls/5/merge` is the merge call). An endpoint it cannot parse (`%` escapes, `..`, a bare scheme) is refused. GraphQL passes only when the query is a plain `-f query=...` on the command line; a query from a file, `-F query=@...`, `--input` or stdin is refused, because the wrapper cannot read it.
- **Short-flag clusters** of `gh api` are read letter by letter (`-iX PUT` is `-i` and `-X PUT`; a value-taking letter `X H q t p f F` takes the rest of the cluster or the next word); a cluster with any other letter, or a second endpoint word, is refused. The `gh api` path is folded to lower case before matching.
- **Git's environment configuration is refused**: any `git` command when `GIT_CONFIG_COUNT`, `GIT_CONFIG_PARAMETERS`, any `GIT_CONFIG_KEY_*`, `GIT_CONFIG_GLOBAL` or `GIT_CONFIG_SYSTEM` is set and not empty, because they carry the `alias.*` and `push.default` overrides the `-c` refusals cover. The pod's own credential helper is written to `~/.gitconfig` (a file) and the entrypoint exports none of these, so the worker's own git is not affected. `GH_CONFIG_DIR` is not refused: a `gh` alias only expands for a first word `gh` does not know, and the wrapper refuses every such word. `git config` passes as a read only in the exact shape one read mode (`--get`, `--list`, `-l` ...), scope flags, at most one key; any other shape is judged word by word, so an `alias.*` or `push.default` is refused. `gh config set` is refused.
- **`git push`**: a refspec with `*`, any `push.default` override (`-c`, `git config`), and the `--repo=`, `--receive-pack=` and `--exec=` forms are handled by name.
- **Approving or dismissing a review is refused** (`gh pr review` in any form, and `gh api` writes under `repos/*/pulls/*/reviews`), because a bot approval can count toward a required-reviews rule on another pull request. `gh pr close`, `ready`, `reopen` and `comment` are NOT refused: they act on the worker's own pull request and grant nothing another one relies on; the repository's ruleset (who may approve, who may merge) is what stops an approval counting.
- Also refused: `gh pr edit --add-assignee|--remove-assignee`, `gh workflow run|enable|disable`, `gh release create|edit|delete|upload`, `gh run rerun|cancel`, `gh api` writes to `repos/*/dispatches`, `.../git/refs` (any write), `.../releases` and `orgs/*`. `gh ruleset` is only reads (`list`, `view`, `check`), so it is no longer refused.
- Known over-refusals: the config's patterns refuse a read of `.../reviews`, `.../dispatches`, `.../git/refs`, `.../releases` or `orgs/*` through `gh api` (the wrapper lets a GET through), and a branch such as `fix-main-menu` (a `main` pattern); an unknown first word of `gh` is refused (so a new `gh` command is refused too until the list in `guard.sh` is updated).
- Known passes: when `origin/HEAD` cannot be read the default branch is only `main`, `master` and `CW_DEFAULT_BRANCH`; the config cannot say a literal `*` refspec, so that is the wrapper's only; an option the parser cannot read passes.

The OpenCode config is written under the worker's own tmpfs, so the worker user can edit it, and a repository's own `opencode.json` is merged over it by OpenCode (not run here: whether a later file can lift a `deny` is unproven). The deny rules are a convenience. `"*": "allow"` followed by the denies is allow-by-default for the shell tool: it grants nothing the pod could not already do, and narrows nothing by itself. The guard is the repository's rules and the token's scope.

## The operator driver

```sh
export CW_KUBE_CONTEXT=<context>      # or CW_CLOUD_SERVER + CW_CLOUD_TOKEN_FILE, as the launcher
cloud/launcher/run_task.py WORKER OWNER/NAME BRANCH BRIEF_FILE [--pull-secret NAME] [--image IMAGE]
```

The target repository is an argument and has no default. The driver reads the model entry (the `github-copilot` entry of
`CW_MODEL_AUTH_FILE`, default `~/.local/share/opencode/auth.json`) and a git token (the output of `CW_GIT_TOKEN_CMD`;
default `app_token.py OWNER/NAME` beside the driver, the one-hour GitHub App token) in memory, creates the pod through
the launcher with both handed over inherited pipes (`--run-secret-fd`), port-forwards, opens a session, sends the brief,
waits for idle, then stops the worker (the end hook pushes the work; the pod and its Secrets are deleted).

- A normal end, a failure and an interrupt (SIGINT, SIGTERM) all clean up, and it says exactly once
  `per-run secret NAME-run cleaned` or `per-run secret NAME-run REMAINS: delete it with: <command>`.
- A worker whose end hook cannot push is kept so the work is not lost; its per-run Secret is still deleted and the
  pod's name and delete command are printed.
- Every line it prints passes through the launcher's one `scrub()`. No secret is in an argument, an environment
  variable of a child, a log or a file; the only file is a private temporary directory (`CW_TMPDIR`) holding the
  port-forward's own log, removed at exit. The server password comes back on a pipe and stays in memory.
- A missing or unreadable input (auth file, entry, token command, brief) refuses before anything is created. A second run
  with a worker name that exists refuses and changes nothing. So does a run that LOSES a race for the name (another run made
  the worker while this one minted its token): the launcher exits with its own code (7, "already exists") and the driver
  returns without an end hook and without deleting anything. The driver acts on a worker only when it can prove it is
  its own: a nonce it chose before the create is set (`--run-id`) on the pod and Secrets as `cw/run-id` and read back;
  no match, or no answer, leaves it whole. The launcher makes the password Secret, the per-run Secret and the pod with `kubectl create` (exclusive: of two simultaneous creates exactly one wins, the other exits 7 having made nothing), and its own cleanup deletes only objects that carry its `--run-id`. A per-run Secret with no run id (a crash before it was labelled) is never guessed at: it is reported with the exact delete command (and says the delete command when ownership could not be read).
- A kill with SIGKILL cannot be cleaned by the driver: the launcher's `delete` or the reaper removes what remains.

Tests: `bin/test-cloud-launcher` (driver, fake kubectl), `cloud/tests/test-cloud` (entrypoint, stub server).

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

The build itself scans the image. `worker/Dockerfile` has a `scan` stage that runs `scan-secrets.sh --dir /` as root over
the whole filesystem of the finished `worker` stage (skipping only the top-level `proc`, `sys`, `dev` and the script), and the
`final` stage, the one that is published, depends on it: a hit (exit 1) or a file the scan cannot read (exit 2) fails the build,
so no image exists to push. The scan runs where the build runs, so a remote `linux/amd64` build reads the layers that
`docker save` cannot export from an arm64 workstation. The marker is only bind-mounted into `final`, never copied: the shipped
image holds no scan file.

```sh
# the real build: the working directory must be cloud/ (the build context). It fails when the scan finds a secret.
(cd cloud && az acr build --registry acme-registry --image cw-worker:<tag> --platform linux/amd64 -f worker/Dockerfile .)
# the same build on a workstation (BuildKit; pass the platform on an arm64 machine)
docker build --platform linux/amd64 -t cw-worker:dev -f cloud/worker/Dockerfile cloud
# the configuration and history ONLY, with the same patterns, on any workstation, after a pull or a local build
docker pull acme-registry.azurecr.io/cw-worker:<tag>
cloud/worker/scan-secrets.sh --meta acme-registry.azurecr.io/cw-worker:<tag>   # prints "0 hits (META SCAN ONLY: ... layer FILES NOT SCANNED)", exit 0
```

`--meta <image>` scans `docker image inspect` Config (env, labels, command) and `docker history --no-trunc`. It reads no
layer, and its output says so (`META SCAN ONLY: image configuration and history; layer FILES NOT SCANNED`, on success and on
failure): it never prints the plain `0 hits` of a file scan. `scan-secrets.sh <image>` (a `docker save` scan that extracts every
layer separately) still works where the engine can export the layers (an image of the machine's own architecture); it is not run
by the build or CI.

What the build scan covers and does NOT cover:

- It covers every regular file of the MERGED filesystem of the finished `worker` stage (minus `proc`, `sys`, `dev` and the
  scan files), binary files included. Files are read as bytes in streamed 4 MiB chunks with an overlap, so a token across a chunk
  edge is found and there is no file-size limit; each file is searched as it is and with its NUL bytes removed, which finds
  UTF-16LE, UTF-16BE and NUL-padded text. A CRLF line end does not hide a private key.
- A file it cannot read fails the build (exit 2); nothing is skipped.
- It does NOT see a secret written in one layer and deleted in a later one (the layers are already merged), so no step of the
  `worker` stage may take a credential. A per-layer scan was judged not cheap: it needs a full scan after every layer-creating step.
- It does NOT detect base64, compressed (gzip, zip, tar) or otherwise encoded forms, or a token split by other bytes: the
  patterns are text patterns for known credential shapes.

Output is `path: pattern-name`, never the value. Exit codes: 0 no hits, 1 hits, 2 not readable or a usage error.

## Resetting a node the fix gave up on

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
