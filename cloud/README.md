# cloud/: worker image and Sysbox node setup

Part of the cloud-worker prototype (plan scenarios S10 and S12). Two pieces:

- `worker/`: the worker image (OpenCode headless, git, `cw-gh`, Python, Docker for use under Sysbox, non-root)
  and `scan-secrets.sh`, a build-failing scan for stored credentials.
- `k8s/`: the RuntimeClass and a DaemonSet that repairs the containerd registration of Sysbox on a node, repeatably.

This image takes everything it needs from the environment and mounted files at start and stores nothing. The launcher
(`cloud/launcher/`) supplies the per-run files; the entrypoint installs them (next section) and the operator driver
(`cloud/launcher/run_task.py`) runs one task end to end.

Cloud workers serve the organisation's repositories only; no task, test or demo of the tooling that runs the workers runs in a cloud worker.

## What is in the image

Ubuntu 24.04, Python 3, git, `cw-gh` (our own small GitHub client; the real `gh` is not installed: see "The client that replaces the real `gh`"), Docker Engine with the compose plugin, and `opencode` v2 (pinned,
`OPENCODE_VERSION=2.0.28`, a standalone binary checked against its sha256 before it is unpacked; no node or npm). The
launcher's end hook and token mint are copied in as `cw-end-hook` and `cw-token-mint`, and the activity reader as
`cw-worker-activity` (it reads the v2 object shape of `GET /api/session/active`). It runs as user `worker` (uid 1000). `worker` may run one command as root (`start-dockerd`) through sudo;
the entrypoint uses it to start the nested `dockerd` and then execs the command. Default command:
`opencode serve --hostname 0.0.0.0 --port 4096`. Set `WORKER_DOCKER=0` to skip the Docker daemon. No credential is
built in: `OPENCODE_SERVER_PASSWORD`, `GH_TOKEN` and any cloud identity are supplied by the launcher at run time.

## Per-run secrets in the image

The launcher mounts per-run files read-only under `/run/cw` (`CW_RUN_DIR`). The entrypoint, which never prints a value:

- `server-password`: read into `OPENCODE_SERVER_PASSWORD` for the server it starts (OpenCode reads the password only from its environment).
- `model-auth`: the model provider's access entry (JSON), the one-entry array `opencode auth export` prints, IMPORTED with `opencode auth import --standalone` (read from the mounted file on stdin) into OpenCode's own store under `<tmpfs>/data` (`XDG_DATA_HOME`) BEFORE the server starts, because the server reads only that store and ignores an `auth.json`. A failed import refuses the start with `worker: the model credential import failed; refusing to start`; the Secret file is never written. A model config naming `CW_MODEL` (default `github-copilot/claude-sonnet-5.5`) is written beside it (`XDG_CONFIG_HOME`); it holds no secret.
  A worker can read this credential (the one-entry array, refresh token included, and the store under `XDG_DATA_HOME`) for the life of its pod; the shell deny rules do not guard it. The pod holds a launch-time snapshot: a revoked or rotated credential ends that pod's turns, and the operator's rotated token is not pushed to a running pod. A stale `CW_MODEL_AUTH_FILE` (retired) is refused as an unknown setting in a settings file and silently ignored in the process environment. An old image with the new launcher answers on the free model; the old launcher with the new image refuses at the import.
- `git-token`: never copied. A git credential helper (`git config --global`, so every process of the worker user sees it, including a fresh-environment `kubectl exec` such as the end hook) and a `gh` wrapper read the file at call time. The helper answers only for `https://github.com`; the token is never in git config, argv or the environment. The directory is the path baked in when the files were generated (a caller's `CW_RUN_DIR` changes nothing), and at every call an unreadable or empty token file makes both fail with one line and a non-zero exit (never an empty password, never a `gh` with an empty token). The `gh` wrapper runs the guard and then `cw-gh`, which has only the verbs listed under "The client that replaces the real `gh`"; the boundary is the token's own permissions (a per-run, one-repository App token) and the repository's rules, not the wrapper or the client. The managed reaper replaces the Secret's token about every 30 minutes (see `launcher/README.md`, "The git token and its renewal"); the kubelet refreshes the whole-directory mount, so the next call reads the new token. The server password is in the server's environment, so every process the agent starts can read it; that is accepted by design.
- The `git-token` also carries `issues: read` (one table in `cloud/launcher/app_token.py`), so the worker can read its repository's issues and their comments. The wrapper still refuses every write to an issue, a label or a merge.

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

What this is NOT: the guard. Both layers are defence in depth and can be bypassed: the real `gh` is no longer in the image,
but another HTTP client (`curl`, `python3`) or a script skips the wrapper, and a pattern is only text matching. The guard is the repository's own
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
- **Git's environment configuration is refused**: any `git` command when `GIT_CONFIG_COUNT`, `GIT_CONFIG_PARAMETERS`, any `GIT_CONFIG_KEY_*`, `GIT_CONFIG_GLOBAL` or `GIT_CONFIG_SYSTEM` is set and not empty, because they carry the `alias.*` and `push.default` overrides the `-c` refusals cover. The pod's own credential helper is written to `~/.gitconfig` (a file) and the entrypoint exports none of these, so the worker's own git is not affected. `GH_CONFIG_DIR` is not refused: a `gh` alias only expands for a first word `gh` does not know, and the wrapper refuses every such word. `gh config set` is refused.
- **`git config` writes are refused** (the table of forms is at the top of `guard.sh`; this list names exactly its refused forms): `--rename-section`, `--remove-section`, `-e`, `--edit`, `--add`, `--unset`, `--unset-all`, `--replace-all`, `--file`, `-f`, `--blob`, `an unknown flag or abbreviation` (any flag not listed as a read or display flag; git takes a unique prefix, so --uns is --unset; this also refuses an abbreviated read flag, by design), `a value word after a read mode's key` and `an option after the get or list subcommand` (refused although git takes them as reads: --get K REGEX, --get-all K REGEX, --get-regexp P VALREGEX, config list --show-origin, config get --all K; the key count must match the mode and options come before the key), `a value that looks like an option` (git stops reading options at the first non-option word, so everything after the key is a value; a word after the key that starts with a dash is refused whatever it is, so a value cannot pose as a read modifier: for example core.pager with the value --default=x;touch), the subcommand forms `edit`, `set`, `unset`, `rename-section` and `remove-section`, the plain `KEY VALUE` form, and a key named `include.path` or `includeIf.<cond>.path` (a value that makes git read another file). The one exception is the plain form for exactly `user.name` or `user.email` (identity, needed to commit; `user.name.x` is not it). A write flag anywhere in the command makes it a write, whatever read mode or key it comes with (`--get --unset k` is refused). The wrapper reads the command like git does and denies by default: the options are only the words before the first non-option word, and a read passes only as exactly one read mode (`--get`, `--get-all`, `--get-regexp` take one key, `--list` and `-l` none, `--get-urlmatch`, `--get-color` and `--get-colorbool` one or two) or no mode and one key, with scope and display flags and `--type=`, `--default=` and the other read modifiers before the key. Any other shape is judged word by word and refused, so an `alias.*` or `push.default` keeps its own phrase. The deny globs in the OpenCode configuration and `worker.md` are belt and braces, not a second guard: a text pattern cannot read a command's shape. Like the rest of this wrapper it is a convenience, not the guard: the repository's rules and the token's scope are.
- **`git push`**: a refspec with `*`, any `push.default` override (`-c`, `git config`), and the `--repo=`, `--receive-pack=` and `--exec=` forms are handled by name.
- **Approving or dismissing a review is refused** (`gh pr review` in any form, and `gh api` writes under `repos/*/pulls/*/reviews`), because a bot approval can count toward a required-reviews rule on another pull request. `gh pr ready` and `comment` are NOT refused (and `gh pr close` and `reopen` are not refused by the wrapper, though the client has no such verb): they act on the worker's own pull request and grant nothing another one relies on; the repository's ruleset (who may approve, who may merge) is what stops an approval counting.
- Also refused: `gh pr edit --add-assignee|--remove-assignee`, `gh workflow run|enable|disable`, `gh release create|edit|delete|upload`, `gh run rerun|cancel`, `gh api` writes to `repos/*/dispatches`, `.../git/refs` (any write), `.../releases` and `orgs/*`. `gh ruleset` is only reads (`list`, `view`, `check`), so it is no longer refused.
- Known over-refusals: the config's patterns refuse a read of `.../reviews`, `.../dispatches`, `.../git/refs`, `.../releases` or `orgs/*` through `gh api` (the wrapper lets a GET through), and a branch such as `fix-main-menu` (a `main` pattern); an unknown first word of `gh` is refused (so a new `gh` command is refused too until the list in `guard.sh` is updated).
- Known passes: when `origin/HEAD` cannot be read the default branch is only `main`, `master` and `CW_DEFAULT_BRANCH`; the config cannot say a literal `*` refspec, so that is the wrapper's only; an option the parser cannot read passes.

The OpenCode config is written under the worker's own tmpfs, so the worker user can edit it, and a repository's own `opencode.json` is merged over it by OpenCode (not run here: whether a later file can lift a `deny` is unproven). The deny rules are a convenience. `"*": "allow"` followed by the denies is allow-by-default for the shell tool: it grants nothing the pod could not already do, and narrows nothing by itself. The guard is the repository's rules and the token's scope.

## The root cause of the recorded case

A cloud worker called the real `gh` binary by its path instead of the `gh` wrapper first on its PATH, and ran a GraphQL mutation that removes a pull request from a merge queue. The wrapper refuses merge-class commands and any GraphQL `mutation`, but it only runs when the caller goes through it: the real `gh` binary skipped it, and the token itself allowed the mutation. The wrapper worked as designed; it was never a boundary, and a wrapper, a removed binary or a file mode inside the pod can never be one, because the worker's unix user can read the token file, runs `curl` and `python3`, and is in the `docker` group.

## Where the boundary is

There are two real boundaries, and everything else in the pod is a convenience.

- **Boundary 1, the token's scope.** The installation token has exactly contents write, pull requests write, issues read and metadata read (`app_token.py` refuses any other set). What those permissions allow is the table below.
- **Boundary 2, the repository's rules.** Rulesets, the merge queue and review requirements decide what the permitted actions can reach: a merge, a push to the default branch, a force-push. The preflight reads them before a worker is launched (`cloud/launcher/preflight.py`).

The wrappers and the client are conveniences, never a boundary: the `gh` and `git` wrappers (`guard.sh`), the OpenCode deny rules, the worker prompt, and `cw-gh` (the client that replaces the real `gh`). They catch accidents and make conduct rules visible. A worker that wants to act around them can, by design, with the routes listed under "What remains reachable by design".

Owner decisions that remain (the plan names them; the answers are recorded on the pull request, not assumed):

- Whether to accept, as allowed by design, the actions the table leaves open to the token: PR comments, labels on a PR, ready and draft, close and reopen, reviewer requests, tags, releases and branch deletes.
- Whether the preflight's new checks (an App bypass actor, auto-merge, no required status check, a merge queue) block instead of warn. They warn first; one table, `CHECK_MODES` in `preflight.py`, is switched after a week of clean warnings.
- Whether the live probes of the "to be probed live" rows pass on the throwaway repository (a tester step after merge).

## What the token allows

One row per action, derived from GitHub's own "Permissions required for GitHub Apps" page. The rows live in `cloud/token-table.txt`; this rendering is checked against it by `cloud/worker/tests/test_token_table.py` (regenerate with `python3 cloud/worker/tests/test_token_table.py --write`). A status of "to be probed live" means no live token was used to confirm it: the probes run on the throwaway repository after merge (`cloud/worker/probe-token.py`), never against a product repository.

<!-- token-table:begin -->
| Action | Token | Decided by | Status |
|---|---|---|---|
| PR create, edit title/body/base, close, reopen | allowed | pull requests: write | documented |
| PR comment, review comment, reply | allowed | pull requests: write | documented |
| Issue comment on a PR, labels on a PR, assignees | allowed | pull requests: write (the shared endpoints accept issues or pull requests) | documented, labels to be probed live |
| Request or remove reviewers | allowed | pull requests: write | documented |
| Ready for review, convert to draft | allowed | pull requests: write (GraphQL, not in the REST table) | to be probed live |
| Submit review, approve, request changes, dismiss | allowed | pull requests: write (GitHub refuses a self-approval; whether a bot approval counts is the ruleset's) | documented, counting to be probed live |
| Update PR branch | allowed | pull requests: write | documented |
| Merge a PR (REST) | allowed | contents: write (bounded only by the ruleset) | documented |
| Enable or disable auto-merge | allowed | pull requests: write (GraphQL; only if the repository allows auto-merge) | to be probed live |
| Merge queue enqueue and dequeue | unknown | not in the documented table | to be probed live |
| Branch create, push, delete (non-default) | allowed | contents: write | documented |
| Push or delete the default branch, force-push | allowed | contents: write (refused only by the ruleset) | documented |
| Tag create and delete | allowed | contents: write | documented |
| Releases and release assets | allowed | contents: write | documented |
| Workflow file changes | refused | no workflows permission | documented |
| Repository settings, rulesets, branch protection, webhooks, secrets | refused | administration, secrets and webhooks not granted | documented |
| Issue and issue-comment reads (view, list) | allowed | issues: read | documented, to be probed live |
| Issue create, edit, close | refused | issues: write not granted, only read (comments, labels and assignees on a PR are the PR rows above) | documented, to be probed live |
<!-- token-table:end -->

### The client that replaces the real `gh`

The image no longer installs `gh`. `cloud/worker/cw-gh` is a small client of our own (Python, standard library, no extra dependency) that the `gh` wrapper runs after the guard. It covers only the verbs a worker's task needs:

- `gh pr create|view|list|comment|checks|diff|ready`
- `gh repo view`
- `gh issue view|list` (read only)
- `gh run view|list|logs` (read only; `gh run view --log` and `--log-failed` print the CI logs)
- `gh api` with GET only

Its output differs from the real `gh`: `--json` prints the raw REST object and `--jq` and `--template` are not supported. The wrapper's refusals are unchanged and run first.

### Verbs that no longer work

Everything else exits 2 with `cw-gh: '<verb>' is not available in this pod`, and the wrapper refuses a subset of them first (exit 3) as before:

- `gh issue create`, `gh issue edit`, `gh issue close`, `gh issue comment` and the rest of `gh issue` writes: the token has no issues permission (comments on a pull request are `gh pr comment`).
- `gh pr merge`, `gh pr review`, `gh pr edit`, `gh pr close`, `gh pr reopen`, `gh pr update-branch`: not covered by the client (merge and review are also refused by the wrapper).
- `gh label ...`, `gh release ...`, `gh workflow ...`, `gh run rerun` and `gh run cancel`.
- `gh repo edit`, `gh repo clone`, `gh repo create` and every other `gh repo` verb except `view`; `gh secret ...`, `gh variable ...`, `gh ruleset ...`, `gh deploy-key ...`.
- `gh extension ...`, `gh alias ...`, `gh config ...`, `gh auth ...`, `gh codespace ...`, `gh gist ...`, `gh search ...`, `gh project ...` and the rest of the real `gh`.
- `gh api` with any method but GET, any body field, `--input`, GraphQL, or a full URL.

A task that needs one of these is a task for a person or for the repository's own automation. Removing a verb narrows what a worker does by habit or accident; it does not stop a worker that sends the same request with `curl`.

### What remains reachable by design

`cloud/worker/probe-reach.sh` (installed as `cw-probe-reach`) shows it: the real `gh` at its absolute path exits 127, and exactly three routes remain.

- `curl` with the token file: any HTTP call the token allows.
- `python3` with the token file: any HTTP call the token allows.
- The `git` credential helper: `git push` and `git fetch` to `github.com` as the token. Git needs the token, and the worker's user can read the file, so this cannot be removed.

Through them, the actions of the table marked allowed remain possible: bounded by the repository's rules, not by the pod.

## The orchestrator agent (#1157)

**Cause.** Before this change the pod's agent was fixed: `entrypoint.sh` wrote only `worker.md`, and `cmd_launch` only recorded `--agent`, so no pod could run an orchestrator.

The entrypoint now picks the agent from ONE value, `CW_AGENT`: `worker` (also when unset) or `orchestrator`; any other value exits 1 with a message naming `CW_AGENT` before the server starts. `launch --cloud --agent orchestrator --reports-to BOSS` sets it (a worker launch sets nothing new and renders the same pod as before; any `--agent` other than `worker` or `orchestrator` is refused with exit 2 before any cluster, vault or token call).

An orchestrator pod gets exactly five more environment names, all non-secret: `CW_AGENT`, `FLEET_AGENT_NAME` (the launch's name), `FLEET_BOSS` (`--reports-to`), `FLEET_CLOUD_CHANNEL_URL` (settings row `CW_HUB_CHANNEL_URL`, default `http://cloud-hub.cw.svc:8443`) and `FLEET_CLOUD_TOKEN_FILE` (`/run/cw-tmp/fleet-switchboard`, the path of the channel secret). The volume list and the token set are the worker's; there is no new token role.

**Channel credential (tightening-only).** The launcher draws a random 32-byte secret per pod and adds it to the pod's per-run Secret as ONE new key, `channel-secret`; the entrypoint copies it (mode 0400) into the memory-backed directory `/run/cw-tmp`. It expires with the pod and is never printed. The launcher registers the pod in the hub's registry Secret (`CW_HUB_REGISTRY_SECRET`, default `cloud-hub-registry`; keys `secrets.json` and `agents.json`): only the sha256 of the secret is stored, and `stop` removes both entries. A failed registration refuses the launch and removes what it made. The registry is the launcher's only new cluster write.

**The image changes: an operator rebuild after merge.** The image now carries `orchestrator.md` beside `worker.md` and the cloud command and hub files (`cloud/worker/bin/`, byte-identical copies of `bin/` files, checked by `cloud/launcher/tests/test_orchestrator_agent.py`). Rebuild and publish it once after merge (see "Build and scan the image").

## The operator driver

```sh
export CW_KUBE_CONTEXT=<context>      # or CW_CLOUD_SERVER + CW_CLOUD_TOKEN_FILE, as the launcher
cloud/launcher/run_task.py WORKER OWNER/NAME BRANCH BRIEF_FILE [--pull-secret NAME] [--image IMAGE]
```

The target repository is an argument and has no default. The driver reads the model entry (the one `github-copilot` entry of OpenCode's own
credential store, read with `opencode auth export` in a scrubbed environment through a pipe, in memory; `CW_OPENCODE_BIN` names the program) and a git token (the output of `CW_GIT_TOKEN_CMD`;
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
- A missing or unreadable input (the export command, its entry, token command, brief) refuses before anything is created. A second run
  with a worker name that exists refuses and changes nothing. So does a run that LOSES a race for the name (another run made
  the worker while this one minted its token): the launcher exits with its own code (7, "already exists") and the driver
  returns without an end hook and without deleting anything. The driver acts on a worker only when it can prove it is
  its own: a nonce it chose before the create is set (`--run-id`) on the pod and Secrets as `cw/run-id` and read back;
  no match, or no answer, leaves it whole. The launcher makes the password Secret, the per-run Secret and the pod with `kubectl create` (exclusive: of two simultaneous creates exactly one wins, the other exits 7 having made nothing), and its own cleanup deletes only objects that carry its `--run-id`. `stop --run-id NONCE` does the same for a stop: it acts only on a pod and Secrets that carry the nonce (a different nonce changes nothing, says `does not match`, exits 6); the driver's stop, and the launch path's stop after a failure (`bin/cloud_worker.py` `_stop_after_failure`), pass their own nonce. A `stop` with no `--run-id` (the switchboard's `retire NAME`, a person, the reaper) acts by name, as before: it is the operator's explicit act. A per-run Secret with no run id (a crash before it was labelled) is never guessed at: it is reported with the exact delete command (and says the delete command when ownership could not be read).
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
`docker save` cannot export from an arm64 workstation. `final` copies a 3-byte marker (`/usr/local/share/cw/scan-ok`, root-owned, mode 0644, never
executed) from `scan`; that plain `COPY --from` is what makes the registry's legacy builder (`az acr build`) run the scan first,
because it has no `RUN --mount`. The Dockerfile uses no BuildKit-only syntax, and `test-cloud` fails if one appears. The
shipped image holds that marker and no scan script.

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
failure): it never prints the plain `0 hits` of a file scan.

### The per-layer scan is THE gate

`scan-secrets.sh --archive <file.tar>` reads an image archive as a stream (python `tarfile`, nothing extracted to disk),
every layer ONCE, so the work is the sum of the layer sizes. It scans every regular file of every layer, including a file
that a later layer deletes (the deletion is only a `.wh.<name>` entry in the later layer), plus the image configuration
(environment, labels) and history. Formats: `docker save` output (`manifest.json` + layer tarballs) and an OCI image layout
(`index.json` -> manifest -> blobs; plain or gzip layers; zstd only where python can read it). It prints
`layer <n> <short digest>: <files> files, <MiB> MiB, <hits> hits` per layer, `layer <n>: <path>: <pattern>` per hit (never a
value), a `total:` line and, only when every layer was read and nothing hit, `all N layers scanned (files + configuration +
history)`. Exit 0 / 1 hits / 2 a layer, a member or the archive could not be read (`not scanned`, never a pass; a layer that
is zstd where python has no zstd is exit 2). `scan-secrets.sh <image>` is `docker save` + `--archive`.

The in-build stage (`--dir /` over the merged filesystem) stays as a cheap early gate: it stops a bad build before anything
is exported. It cannot see a file deleted in a later layer, so it is not the gate.

How to get the archive (the layers of an amd64 image cannot come out of `docker save` on an arm64 machine with the containerd
image store: it writes only the index, and the scanner then exits 2 for the missing blobs):

```sh
# 1. a local, registry-free export that holds the amd64 layers (tested)
docker buildx build --platform linux/amd64 --output type=oci,dest=/tmp/worker.tar -f cloud/worker/Dockerfile cloud
cloud/worker/scan-secrets.sh --archive /tmp/worker.tar
# 2. an image in the registry, without a docker daemon (command shapes; NOT tested here)
crane pull --platform linux/amd64 --format oci acme-registry.azurecr.io/cw/worker-candidate:<sha> /tmp/worker-oci   # an OCI layout directory: tar it (tar -C /tmp/worker-oci -cf /tmp/worker.tar .)
skopeo copy --override-arch amd64 docker://acme-registry.azurecr.io/cw/worker-candidate:<sha> oci-archive:/tmp/worker.tar
# 3. an amd64 host
docker save acme-registry.azurecr.io/cw/worker-candidate:<sha> -o /tmp/worker.tar   # or: scan-secrets.sh <image>
```

Publishing is ONE command, `worker/publish-image.sh`. Build to a STAGING tag first (nothing is published under the real
name), then:

```sh
(cd cloud && az acr build --registry acme-registry -t cw/worker-candidate:<sha> --platform linux/amd64 -f worker/Dockerfile .)
cloud/worker/publish-image.sh --tool crane \
  --staging acme-registry.example/cw/worker-candidate:<sha> --release acme-registry.example/cw/worker:<tag>
```

It resolves the staging tag to a digest, exports exactly `<staging>@<digest>` (`crane pull --platform linux/amd64 --format oci`,
or `skopeo copy --override-arch amd64 ... oci-archive:`), runs the per-layer scan on that export, resolves the staging tag
AGAIN and refuses if it no longer points at the scanned digest, and only then copies `<staging>@<digest>` to the release tag
(`crane copy` / `skopeo copy`), by digest, never by tag. Exit 0 = published; 1 = a hit; 2 = a layer could not be read; 3 = a
tool failed, a digest could not be resolved, the tag moved, or the arguments were wrong. In every non-zero case nothing is
published. The staging digest must be a single-image (amd64) manifest: an index or manifest list is REFUSED (exit 3) before anything is exported. The tool comes from `--tool` (`crane` or `skopeo` only) and
runs from an absolute path (`--tool-dir`, else a fixed list of system directories), never from the caller's environment. A registry login must
already exist: this script never reads or prints a credential. The command shapes for the registry tools are untested against a
real registry here; the script's logic is tested with stub tools.

What the per-layer scan covers and does NOT cover:

- It covers every regular file of every layer, binary files included (hardlinks are covered by the layer that holds the data),
  searched as bytes in streamed 4 MiB chunks with an overlap, as they are and with NUL bytes removed (UTF-16LE/BE, NUL-padded
  text), a CRLF line end not hiding a key; the image configuration and history; credential file names (ssh keys,
  `.git-credentials`, `.netrc`, `gh/hosts.yml`, `kubeconfig`).
- It does NOT detect base64, compressed (gzip, zip, tar) or otherwise encoded copies of a secret inside a file, nor a secret
  split across two files or by other bytes: the patterns are text patterns for known credential shapes.
- It does NOT read a layer it cannot decompress: that is exit 2 (fail), not a skip.

### The allowlist (known false positives)

Scanning packaged binaries as bytes finds strings that only look like credentials (the `gh` binary, a locale table in
`libunistring`, format strings in `libgnutls`). `worker/scan-allowlist.txt` lists them as exact triples, one per line, each
with a reason:

```text
<path in the image> <sha256 of THAT file> <pattern name> <reason: what the false positive is>
```

A hit is suppressed ONLY when the path, the sha256 of that file (in a layer scan: of that layer's copy) and the pattern name all
match. The same file with any other pattern still fails; a changed binary (a new sha256, for example after a base-image or `gh`
update) fails the build until a reviewed PR updates its line. That is the maintenance cost, and it is deliberate: the gate fails
closed. A missing, unreadable or malformed allowlist is exit 2, never "no allowlist, pass everything". Every suppressed hit is
printed (`allowlisted: <path>: <pattern>`) and counted in the totals line, never silent. The file is tracked and COPYed into the
scan stage only (never into the shipped image); a change to it is a change to the gate and needs a strict review.

The allowlist applies to pattern hits (and to a credential file by name in the archive scan); a credential file by name in the `--dir` scan is never allowlisted. Paths are printed with control characters escaped (`\x1b`), so an image cannot forge output lines. `SCAN_ALLOWLIST` overrides the list path for tests only; the build never sets it.

For a new hit, print its triple (no value is ever printed) and review the file before adding a line:

```sh
SCAN_TRIPLES=1 cloud/worker/scan-secrets.sh --archive /tmp/worker.tar     # or --dir <dir>: prints "TRIPLE<TAB><path> <sha256> <pattern>"
```

What the in-build stage covers: the merged filesystem of the finished `worker` stage (minus `proc`, `sys`, `dev` and the scan
files), nothing deleted in a layer, same byte scan. A file it cannot read fails the build (exit 2).

Output is `path: pattern-name`, never the value. Exit codes: 0 no hits, 1 hits, 2 not readable or a usage error.

## Resetting a node the fix gave up on

When the fix cannot bring a node back (a failed restore, or containerd still unhealthy after the rollback) it sets its
failed-attempt marker to the cap and exits non-zero once; later pods then idle and restart nothing. After the node has
been repaired by a person, reset BOTH files on the node, then delete the fix pod:

```sh
rm -f /etc/containerd/config.toml.sysbox-fix.failed /etc/containerd/config.toml.sysbox-fix.inprogress
```

## A pod for a nested Docker stack (`create --stack`)

`cloud-worker create ... --stack` builds the pod a worker needs to run a Docker stack of its own (for example a database
and an app): the `sysbox-runc` runtime class, its own user namespace (`hostUsers: false`), the pool's `sysbox-install=yes`
node selector, the non-root `worker` user, shape `S` (2 CPU, 4 GiB limits, never unlimited), and no privileged flag, host
path, Docker socket or service-account token. Without `--stack` the pod is byte-for-byte what it was (a stored fixture
pins it). The container's security context (`STACK_SECURITY_CONTEXT` in `cloud/launcher/cloud_worker.py`) was live-probed
with the cluster default only: the smallest set that lets the daemon start is decided by the next live run, one variation
at a time. A create that lands on a fresh node can hit the "handler sysbox-runc is not known" race; the launcher recreates the
pod (not the Secrets) after a pause of 15 seconds (`STACK_RACE_PAUSE_SECONDS`), for as long as one more pause fits in a
window of 600 seconds (`STACK_RACE_WINDOW_SECONDS`); there is no attempt count.

The stack pod is pinned whole, like the default pod: `cloud/launcher/tests/fixtures/stack_pod.json` is the pod the stack create
builds, and `stack_create_objects.json` is every other object the create makes (the password Secret, the run Secret and its
labels; the run record is written by `stop`, not by `create`). At render time a stack pod must also have exactly one container
named `worker`, the whole pod-level security context, only the allow-listed pod, container and volume fields, plain `name`/`value`
env entries and the three known volumes. Any added container, field, volume or env therefore fails CI; a deliberate change edits the
constant and the fixture together (regenerate with `CW_WRITE_STACK_FIXTURE=1 python3 cloud/launcher/tests/test_cloud_stack.py`, then
read the diff). An image by tag is accepted by design: the operator picks the image, and no digest is required here.
Details: `docs/cloud-worker-interface.md`. The live runs are in
"Live proof" below.

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
