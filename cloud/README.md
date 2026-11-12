# cloud/: worker image and Sysbox node setup

Part of the cloud-worker prototype (plan scenarios S10 and S12). Two pieces:

- `worker/`: the worker image (OpenCode headless, git, `cw-gh`, Python, Docker for use under Sysbox, non-root)
  and `scan-secrets.sh`, a build-failing scan for stored credentials.
- `k8s/`: the RuntimeClass and a DaemonSet that repairs the containerd registration of Sysbox on a node, repeatably.

This image takes everything it needs from the environment and mounted files at start and stores nothing. The launcher
(`cloud/launcher/`) supplies the per-run files; the entrypoint installs them (next section) and the operator driver
(`cloud/launcher/run_task.py`) runs one task end to end.
The launcher's settings file may set `CW_AZURE_CONFIG_DIR` (an absolute path of an existing directory): the launcher then uses it as `AZURE_CONFIG_DIR` for every vault read of a launch, replacing the caller's own value; unset, nothing changes.

The settings file may set `CW_SECRET_DIR` (an absolute path of a directory under the user's home: a real directory, not a link, owned by the running user, mode 0700): the launcher then reads the vault's secrets from files of that directory instead of `az`, in the interactive and the unattended route, and runs no program for it. One regular file per secret, named exactly as the vault's secret: `cw-app-id`, `cw-app-key` (used by `app_token.py`), the cluster login's kubeconfig (the file named by `CW_CLUSTER_ADMIN_SECRET` when that is set, else `cw-minter-kubeconfig`; `fk-cloud-workers-admin-kubeconfig` works by name) and the stack model secret (`cw-stack-model-key` or the name given). Each file is mode 0600, owned by the running user, not a link, at most 64 KiB, and holds the raw secret text (no encoding). A wrong mode, owner, link, size, a missing or empty file is refused as a vault error with a one-line reason naming the file, never its content. `CW_APP_ID_CMD`, `CW_APP_KEY_CMD` and `CW_CLUSTER_ADMIN_CMD` still win where they are honoured; unset, every read is as before. The stack read still needs `CW_VAULT_SUBSCRIPTION` and `CW_VAULT_NAME` in the settings file (its check is not changed), and the strict route still checks that a fixed `az` path exists (it never runs it). The image publish (`worker/publish-image.sh`) with `CW_SECRET_DIR` set in its environment logs in with `crane` using the file `acr-token` (one line `username:password`, the token of a scope map that allows `content/read` and `content/write` on `cw/worker` and `cw/worker-candidate` only), the password on standard input into a private temporary docker config, and runs no `az` after the build. The one-time creation of the token (with the owner's Azure session; give the token an expiry of at most one year):

The reaper reads the settings file again at the start of each pass (when its size, modification time or content changed): a setting changed or removed in the file takes effect at the next pass with no restart, a value the caller set itself stays, a file that cannot be read keeps the old values and writes one line (the reason, no value), and a change of `CW_CLOUD_NAMESPACE` or `CW_CLOUD_SERVER` is reported on one line and waits for a restart.

```sh
az acr scope-map create -r acme-registry -n cw-publish --repository cw/worker content/read content/write --repository cw/worker-candidate content/read content/write
az acr token create -r acme-registry -n cw-publish-token --scope-map cw-publish --expiration-in-days 365   # prints the password once: write it into the file `acr-token` with `umask 077`, never an argument
```

Cloud workers serve the organisation's repositories only; no task, test or demo of the tooling that runs the workers runs in a cloud worker.

## What is in the image

Ubuntu 24.04, Python 3, git, `cw-gh` (our own small GitHub client; the real `gh` is not installed: see "The client that replaces the real `gh`"), Docker Engine with the compose plugin, and `opencode` v2 (pinned,
`OPENCODE_VERSION=2.1.4`, a standalone binary checked against its sha256 before it is unpacked; a standalone binary, not a Node package). The agent release is pinned by version and tarball sha256; the pin is a security floor: the agent release built on Bun 1.4.4 or later (the earlier runtime embeds trust roots that are being withdrawn); floor 2.1.3 (Bun 1.4.4). To bump: update `OPENCODE_VERSION` and `OPENCODE_SHA256` in `cloud/worker/Dockerfile`, run `python3 cloud/worker/tests/test_dockerfile_agent.py` (it refuses a version below the floor), then run `CW_ROOT_LIST=<file the person provides> python3 cloud/worker/scan-embedded-roots.py <unpacked binary>` (an operator tool, not in the image; the list is never kept in the repository) and put its output in the PR (it prints list indexes and 8-hex hash prefixes, never labels; exit 2 when nothing was examined). No match is not proof of absence outside the forms listed; not detected: DER with a length form other than 0x81, 0x82 or 0x83, a certificate shorter than 300 or longer than 2500 bytes (0x81 form: outside 128..258), indefinite length, PEM with a split or compressed body, a compressed section of the binary, a certificate split in pieces, UTF-16 strings, base64 outside PEM markers. The previous image digest stays the rollback. The
launcher's end hook and token mint are copied in as `cw-end-hook` and `cw-token-mint`, and the activity reader as
`cw-worker-activity` (it reads the v2 object shape of `GET /api/session/active`). `cw-plan-check` and `plan_check.py` (a byte-identical copy of `bin/plan_check.py`, pinned by a CI test) let a worker check its own plan with `cw-plan-check --file PLAN --done-when TEXT`: a pure local read, same output and exit codes (0 pass, 1 fail, 2 unreadable) as `plan check --file`; if the plan has an `## Intent` section with its own `Done when:` line, the pod compares it with `--done-when` exactly as the local check does. Slice B: a retire of a worker whose pod is already gone clears its name record and removes its own left-over `NAME-pw` and `NAME-run` Secrets (audited as `cloud.name_freed`, which names them) so the name can be launched again; see `docs/cloud-worker-interface.md`; the image must be rebuilt and published by an operator before pods have the command. It runs as user `worker` (uid 1000). `worker` may run one command as root (`start-dockerd`) through sudo;
the entrypoint uses it to start the nested `dockerd` and then execs the command. The image also carries `make` (the one package, no compiler toolchain). The image carries the default identity in ONE root-owned read-only file, `/etc/cw-default-identity` (mode 0444, two lines: name, then email), written at build from the one constant pair `CW_DEFAULT_IDENT_NAME` and `CW_DEFAULT_IDENT_EMAIL` (required build arguments with no default, in their own block of the Dockerfile; a build without both fails at a guard step that names the missing one, so the operator passes both with `--build-arg`); it writes no `/etc/gitconfig`. At start, beside the credential helper, the entrypoint sets `user.name` and `user.email` in the worker user's global git configuration from that file, each only when that key is unset there. The entrypoint exports no identity variable. Git's own order applies (system, then global, then local), so a repository's configured identity and a local `git config user.name` or `user.email` win over the default. The permission file no longer holds the text patterns that refused a branch name containing the word main or master (ask #2402); the pre-push hook reads refspecs. The image changes only by an operator rebuild after merge. The default command is
`opencode serve --hostname 0.0.0.0 --port 4096`. Set `WORKER_DOCKER=0` to skip the Docker daemon. No credential is
built in: `OPENCODE_SERVER_PASSWORD`, `GH_TOKEN` and any cloud identity are supplied by the launcher at run time.

### The toolchain the image carries

Ubuntu 24.04, Python 3 with pip and venv, git, Docker Engine with compose and buildx, `jq`, `opencode` v2, and Node 22 with npm and npx
(`/opt/node`, root-owned, linked into `/usr/local/bin`). The recorded case: a lane's first database-lane test in a stack pod needed Node and
npm, the image had neither, and the coder downloaded a Node tarball into its home directory at every launch. Node is now built in:
the Dockerfile pins the sha256 of the release tarball (`.tar.xz`) per architecture (x64, arm64) as constants beside `NODE_VERSION`, downloads
the release for the image's architecture and checks it with `sha256sum -c` against the pinned value BEFORE it unpacks; a mismatch fails the build.
No checksum list is downloaded and no checksum address can be overridden. The added image size was not measured. A repository's own prettier and eslint are its dev dependencies and run through `npx`; the image
does not install them.

Go 1.27.4 is also built in (`/usr/local/go`, root-owned, `go` and `gofmt` linked into `/usr/local/bin`, `GOTOOLCHAIN=local` so no other toolchain is downloaded at run time). Its pin lives in `cloud/worker/Dockerfile`: `GO_VERSION` and `GO_SHA256` (the sha256 of `go<version>.linux-amd64.tar.gz`), checked with `sha256sum -c` before the unpack; a bump updates both and the pin test `cloud/worker/tests/test_dockerfile_go.py`.

`mise` v2026.11.6 is built in too (`/usr/local/bin/mise`, root-owned, not writable by others): `MISE_VERSION` and `MISE_SHA256` (the sha256 of `mise-<version>-linux-x64.tar.gz`) in `cloud/worker/Dockerfile`, checked with `sha256sum -c` before the unpack; a bump updates both and the pin test `cloud/worker/tests/test_dockerfile_mise.py`. Other toolchain versions (for example `mise install python@3.11`) install at run time into the run user's home, once per pod; `python3` stays the system Python.

The pinned version is a security floor: Node >= 22.23.4 (the older 22.23.2 carries trust roots that are being withdrawn).

To bump Node: update `NODE_VERSION` (a full 22.x.y release; the only place the version is written) AND both sha256 constants (`NODE_SHA256_X64`,
`NODE_SHA256_ARM64`) at the top of the Node block in `cloud/worker/Dockerfile`, keep the version equal to the product repository's `.node-version`, then rebuild and publish the image
(an operator step) and record the previous digest as the rollback.

### PostgreSQL 16 and two extensions in the image (ask #1353)

**Cause.** The recorded case: measurement pods of a product repository spend about 20 of 60 minutes compiling PostgreSQL 16 and two extensions (vector, message queue) from source on every run. The image now carries them, built once.

**What is built.** A separate build stage (`pgbuild`, with the compilers) builds PostgreSQL, the vector extension and the message-queue extension into `/opt/pg`; the `final` stage copies only that directory, root-owned. No compiler is in the final image. PostgreSQL has a version constant and a 64-hex sha256 constant at the top of the block in `cloud/worker/Dockerfile` (`PG_VERSION` / `PG_SHA256`: the project's release tarball, 16.x), checked with `sha256sum -c` BEFORE it is unpacked. Each extension is pinned to an immutable 40-hex git COMMIT, not a tag (a tag can be moved): `PGVECTOR_VERSION` / `PGVECTOR_COMMIT` and `PGMQ_VERSION` / `PGMQ_COMMIT`; the version is only the directory and label. The build clones the publisher's repository, checks out the commit and fails unless `git rev-parse HEAD` equals it, BEFORE any build step (`git` is in the `buildpack-deps` base). To bump PostgreSQL: edit `PG_VERSION` AND `PG_SHA256`. To bump an extension: edit its version AND its commit; find the commit with `git ls-remote https://github.com/pgvector/pgvector refs/tags/v<version>` (for an annotated tag, the commit is the `^{}` line), and confirm it twice (a second `ls-remote`, and the tag's page on GitHub). Then run `python3 cloud/worker/tests/test_dockerfile_postgres.py` and rebuild. The build-time secret scan also reads `/opt/pg`: the `scan` stage copies the built PostgreSQL tree in before it scans. The `final` stage also builds the bundle from `/opt/pg` with copies (`usr/lib/postgresql/16/{bin,lib,share}`, the vector, pgmq and `pg_trgm` files under `usr/share/postgresql/16/extension`, `usr/share/postgresql/postgresql.conf.sample`; root-owned, not writable by others) in `/home/worker/.cache/<name>`, the layout `scripts/dev/ephemeral-pg.sh` reads when no variable is set; `<name>` is the REQUIRED build argument `CW_BUNDLE_CACHE_NAME` (no default; the build fails when it is empty; the operator passes `--build-arg CW_BUNDLE_CACHE_NAME=<name>`), and `/home/worker/.cache` is owned by the run user. The image sets no variable that starts with `PG` (the repository's queue code refuses such an ambient override). `pg_trgm` is built in the `pgbuild` stage from the PostgreSQL source's `contrib/pg_trgm`. Nothing starts a server at boot. The relocatable build resolves `share` beside `bin`, which is why `share` is copied under `usr/lib/postgresql/16` too.

Versions: PostgreSQL 16.15, vector 0.8.7, message queue (pgmq) 1.13.0 (the version the repository's schema file creates). The server is built without readline, ICU and zlib and with OpenSSL ON (`--with-openssl`; `show ssl` can be turned on by a lane); the OpenSSL run-time library (libssl3t64) comes with the base image's curl. Size cost: measured by the operator at the first real build (before and after, and the pod start time). Not measured here for the image; the installed tree `/opt/pg` was 35 MB in a local arm64 build of the build stage (a developer measurement, not the operator's amd64 build).

**Nothing starts at pod start.** The entrypoint script is unchanged; the pod's first process is `tini -g` (installed from the distribution archive), which runs the script and then the server beneath it, reaps orphans and forwards signals. No server runs and no port listens until a lane starts it. It runs only as the unprivileged `worker` user, with its data directory on the scratch volume (`/run/cw-tmp`), so the data ends with the pod. The one-line start (initdb, then pg_ctl on a local socket only):

```sh
/opt/pg/bin/initdb -D /run/cw-tmp/pgdata -U worker --locale=C.UTF-8 -E UTF8 && /opt/pg/bin/pg_ctl -D /run/cw-tmp/pgdata -o "-c listen_addresses= -c unix_socket_directories=/run/cw-tmp" -l /run/cw-tmp/pg.log start
```

Then `/opt/pg/bin/psql -h /run/cw-tmp -U worker -d postgres -c "create extension vector"` (and `pgmq`). The network policy, the guard and the permission files are unchanged.

**Rollback.** Point the workers at the previous image digest (kept in the registry until the new image has run live).

## Per-run secrets in the image

The launcher mounts per-run files read-only under `/run/cw` (`CW_RUN_DIR`). The entrypoint, which never prints a value:

- `server-password`: read into `OPENCODE_SERVER_PASSWORD` for the server it starts (OpenCode reads the password only from its environment).
- `model-auth`: the model provider's access entry (JSON), the one-entry array `opencode auth export` prints, IMPORTED with `opencode auth import --standalone` (read from the mounted file on stdin) into OpenCode's own store under `<tmpfs>/data` (`XDG_DATA_HOME`) BEFORE the server starts, because the server reads only that store and ignores an `auth.json`. A failed import refuses the start with `worker: the model credential import failed; refusing to start`; the Secret file is never written. A model config naming `CW_MODEL` (default `github-copilot/claude-sonnet-5.5`) is written beside it (`XDG_CONFIG_HOME`); it holds no secret.
  A worker can read this credential (the one-entry array, refresh token included, and the store under `XDG_DATA_HOME`) for the life of its pod; the pod's rules do not guard it. The pod holds a launch-time snapshot: a revoked or rotated credential ends that pod's turns, and the operator's rotated token is not pushed to a running pod. A stale `CW_MODEL_AUTH_FILE` (retired) is refused as an unknown setting in a settings file and silently ignored in the process environment. An old image with the new launcher answers on the free model; the old launcher with the new image refuses at the import.
- `git-token`: never copied. A git credential helper (set in the user's own git file, so every process of the worker user sees it, including a fresh-environment `kubectl exec` such as the end hook) and a `gh` wrapper read the file at call time. The helper answers only for `https://github.com`; the token is never in git config, argv or the environment. The directory is the path baked in when the files were generated (a caller's `CW_RUN_DIR` changes nothing), and at every call an unreadable or empty token file makes both fail with one line and a non-zero exit (never an empty password, never a `gh` with an empty token). The `gh` script runs `cw-gh`, which has only the verbs listed under "The client that replaces the real `gh`"; the boundary is the token's own permissions (a per-run, one-repository App token) and the repository's rules, not the script or the client. The managed reaper replaces the Secret's token about every 30 minutes (see `launcher/README.md`, "The git token and its renewal"); the kubelet refreshes the whole-directory mount, so the next call reads the new token. The server password is in the server's environment, so every process the agent starts can read it; that is accepted by design.
- The `git-token` also carries `issues: read` (one table in `cloud/launcher/app_token.py`), so the worker can read its repository's issues and their comments. No pod-side layer refuses a write to an issue, a label or a merge any more (the permission file holds no shell deny row, ask #2402): the token's scope (no issues write) and the repository's rules decide.

A file that is absent is skipped (a pod started without per-run secrets). A file that is present but unreadable or empty
refuses the start with one line naming it; there is no fallback to another source. The tmpfs is `CW_TMP_DIR` (default `/run/cw-tmp`).

## The worker's rules in the pod (a convenience, not the guard)

A cloud worker holds the same rules a local agent has. The entrypoint writes an OpenCode config (on tmpfs, no secret in it)
that defines a `worker` agent (`worker.md`, with the STOP rules in prose) and denies the question tool (since ask #2402 it
denies no shell command; the shell tool is the catch-all allow). No pod-side layer refuses the six kinds of command a local guard would: merging a pull request (`gh pr merge`, the API merge call, the GraphQL merge mutations), force-pushing
(`--force`, `-f`, `--force-with-lease`, `+ref`), pushing to the default branch, writing issues, writing labels, and
repository settings calls (`gh repo edit/delete/archive`, `gh api` with PATCH/PUT/DELETE, branch protection, rulesets,
webhooks, secrets). There is no wrapper before `git` or `gh` any more (the guard layer, `guard.sh` and its table, was removed): `git` is the real binary, and `gh` is a small script that gives the client `cw-gh` the token for one call.

What this is NOT: the guard. The real `gh` is no longer in the image; the pod's own client and the missing wrapper refuse nothing by policy, and another HTTP client (`curl`, `python3`) or a script reaches the API the same way. The guard is the repository's own
rules (branch protection or a ruleset that forbids merge by the worker, pushes to the default branch and force-pushes) and
the token's scope (the preflight, #545). Known limits: the `permission.bash` block was checked to load and convert in a local OpenCode 2.0.28 server (it was then a list of `shell` deny rules; since ask #2402 it is the single row `"*": "allow"`), but that server did not list the `worker` agent in isolation, so the agent definition is not proven to register or to be the default (no `default_agent` is set); the global block applies to every agent either way. The permission file holds no shell deny row (its bash map is `"*": "allow"`, plus the operator's long-run rows the entrypoint adds, and `question: deny`).

### What else the pod keeps (review round)

- **`git push`** (a hook of the image; there is no wrapper before `git`): `cloud/worker/pod-hooks/pre-push` is installed as the worker user's global `core.hooksPath` (`/usr/local/lib/cw-hooks`, root-owned; the entrypoint sets it beside the credential helper). Git calls it after it has resolved the remote, with the remote name and the URL (after `insteadOf` and `pushurl` rewriting) as arguments and one line per ref on standard input (`<local ref> <local sha> <remote ref> <remote sha>`, zeros for a deletion or a new ref), so no command-line form is parsed. For a NETWORK URL (https, http, ssh, git or scp-like; a plain path or `file://` is local) and a remote ref that is the default branch (`CW_DEFAULT_BRANCH`, else the target of `refs/remotes/origin/HEAD`, else `main` and `master`) it requires a local object that is a full 40-hex commit whose message, read with the real git (`git interpret-trailers --parse`), has a `Land-Checks` and a `Land-Council` trailer, each non-empty and not the text `not recorded` (no empty duplicate), and a remote object that is an ancestor of it (a fast-forward; zeros, a branch that does not exist yet, skip only that check); a deletion is refused. A refusal is exit 3 and one line. Every other push passes, and then the repository's own `pre-push` hook (if executable and not this file) runs with the same arguments and input and its status is returned. Git reads the remote's refs before it calls the hook, so a URL that cannot be reached ends with git's connection error before the hook runs. The permission file holds no rows since ask #2402, so a shell command that pushes with `--no-verify`, or that sets `core.hooksPath`, is not refused by the pod: it skips the hook. It is a convenience, not the guard: a `--no-verify`, a changed hooks path and a different home directory skip it; the repository's rules and the token's scope are the guard. The trailers are unsigned, so this is an accident guard, not proof. Tests: `cloud/worker/tests/test_pre_push_hook.py`.
- **The pod's notes file (ask #2426, log only).** Cause: the pod's convenience rules refused a push to the default branch, a force-push, a merge and an issue or ref write, and by refusing made them visible; with the rules deleted (ask #2402) nothing records those attempts, and the repository's rulesets alone stop them. Now the pre-push hook above appends one line `<UTC time> <kind> <remote name> <ref> <short sha>` to `/work/.pod-notes.log` (`CW_NOTES_LOG` moves it, for tests) for a push whose remote ref is a default branch (`default-branch-push`; the same default-branch test as the rule) and for a push whose remote object is not an ancestor of the pushed one (`non-fast-forward`; a push that is both writes both lines), and `cw-gh` appends the same shape (remote `github`, sha `-`) for `pr merge` and for a non-GET `api` call to `.../pulls/N/merge` (`api-merge`), `.../git/refs...` (`ref-write`) or `.../issues/...` (`issue-write`). It only records: no push or call is refused or changed because of it, and a failed write is silent; a link or a FIFO at the path is left alone (nothing is recorded, nothing blocks). `cw-gh` records a call when it is asked, before it is validated or answered, so an attempt the client does not implement is recorded too. A remote that is not a plain name (a URL may carry a credential) is written as `-`, a ref is cut to `[A-Za-z0-9._/-]` and 100 characters, no body is read. The file is capped: past `NOTES_MAX_BYTES` (65,536) it is cut to its last `NOTES_KEEP_LINES` (200) lines. At the pod's end the launcher's run record keeps the last 20 lines scrubbed (`pod_notes` in `run.json`, `Recorder.notes_file`), and `cloud_retired.pod_notes_tail` (the one reader: the last 20 lines, at most 2,000 characters, redacted again, no backtick) adds them to the pod's final message (`last_message`). Tests: `cloud/worker/tests/test_pod_notes_log.py`, `bin/test-cloud-retired` (`TestPodNotesInTheFinalMessage`, `TestRecorderKeepsThePodNotes`).

The OpenCode config is written under the worker's own tmpfs, so the worker user can edit it, and a repository's own `opencode.json` is merged over it by OpenCode (not run here: whether a later file can lift the `question: deny` is unproven). The file holds no shell deny row (ask #2402): `"*": "allow"` alone (plus the operator's long-run rows) is allow-by-default for the shell tool; it grants nothing the pod could not already do. The guard is the repository's rules and the token's scope. `question` stays `deny`.

## The root cause of the recorded case

A cloud worker called the real `gh` binary by its path instead of the `gh` wrapper that was first on its PATH, and ran a GraphQL mutation that removes a pull request from a merge queue. The wrapper refuses merge-class commands and any GraphQL `mutation`, but it only runs when the caller goes through it: the real `gh` binary skipped it, and the token itself allowed the mutation. The wrapper worked as designed; it was never a boundary, and a wrapper, a removed binary or a file mode inside the pod can never be one, because the worker's unix user can read the token file, runs `curl` and `python3`, and is in the `docker` group.

## Where the boundary is

There are two real boundaries, and everything else in the pod is a convenience.

- **Boundary 1, the token's scope.** The installation token has exactly contents write, pull requests write, issues read and metadata read (`app_token.py` refuses any other set). What those permissions allow is the table below.
- **Boundary 2, the repository's rules.** Rulesets, the merge queue and review requirements decide what the permitted actions can reach: a merge, a push to the default branch, a force-push. The preflight reads them before a worker is launched (`cloud/launcher/preflight.py`).

The client and the text rules are conveniences, never a boundary (the `gh` and `git` wrappers and `guard.sh` that once stood here were removed, and the permission file holds no shell deny row): the worker prompt, and `cw-gh` (the client that replaces the real `gh`). They catch accidents and make conduct rules visible. A worker that wants to act around them can, by design, with the routes listed under "What remains reachable by design".

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

The image no longer installs `gh`. `cloud/worker/cw-gh` is a small client of our own (Python, standard library, no extra dependency) that the pod's `gh` script runs (with the token for that one call). It covers only the verbs a worker's task needs:

- `gh pr create|view|list|comment|checks|diff|ready`, and `gh pr edit N --body-file F` (the body only; no check of whose pull request it is or what the body holds)
- `gh repo view`
- `gh issue view|list` (read only)
- `gh run view|list|logs` (read only; `gh run view --log` and `--log-failed` print the CI logs)
- `gh api` with GET only

Its output differs from the real `gh`: `--json` prints the raw REST object and `--jq` and `--template` are not supported.

### Verbs that no longer work

Everything else exits 2 with `cw-gh: '<verb>' is not available in this pod` (the client has no code for it; nothing refuses first):

- `gh issue create`, `gh issue edit`, `gh issue close`, `gh issue comment` and the rest of `gh issue` writes: the token has no issues permission (comments on a pull request are `gh pr comment`).
- `gh pr merge`, `gh pr review`, `gh pr close`, `gh pr reopen`, `gh pr update-branch`: not covered by the client. `gh pr edit` covers the body only (`gh pr edit N --body-file F`); the title, base, labels, reviewers and assignees are not covered.
- `gh label ...`, `gh release ...`, `gh workflow ...`, `gh run rerun` and `gh run cancel`.
- `gh repo edit`, `gh repo clone`, `gh repo create` and every other `gh repo` verb except `view`; `gh secret ...`, `gh variable ...`, `gh ruleset ...`, `gh deploy-key ...`.
- `gh extension ...`, `gh alias ...`, `gh config ...`, `gh auth ...`, `gh codespace ...`, `gh gist ...`, `gh search ...`, `gh project ...` and the rest of the real `gh`.
- `gh api` with any method but GET, any body field or `--input`: not implemented (the client has no code for them). A full URL or `//host` is refused so the token goes only to the API host (the host check), and the client never follows a redirect. GraphQL as a plain GET path and issue paths are no longer refused by policy.

A task that needs one of these is a task for a person or for the repository's own automation. Removing a verb narrows what a worker does by habit or accident; it does not stop a worker that sends the same request with `curl`.

### What remains reachable by design

`cloud/worker/probe-reach.sh` (installed as `cw-probe-reach`) shows it: the real `gh` at its absolute path exits 127, and exactly three routes remain.

- `curl` with the token file: any HTTP call the token allows.
- `python3` with the token file: any HTTP call the token allows.
- The `git` credential helper: `git push` and `git fetch` to `github.com` as the token. Git needs the token, and the worker's user can read the file, so this cannot be removed.

Through them, the actions of the table marked allowed remain possible: bounded by the repository's rules, not by the pod.

## The orchestrator agent (#1157)

**Cause.** Before this change the pod's agent was fixed: `entrypoint.sh` wrote only `worker.md`, and `cmd_launch` only recorded `--agent`, so no pod could run an orchestrator.

The entrypoint now picks the agent from ONE value, `CW_AGENT`: `worker` (also when unset) or `orchestrator`; any other value exits 1 with a message naming `CW_AGENT` before the server starts. `launch --cloud --agent orchestrator --reports-to BOSS` sets it (a worker launch sets nothing new and renders the same pod as before; any other `--agent` on `launch --cloud` stays the local role name, only recorded; the launcher's own `create --agent` refuses any value but `worker` or `orchestrator` with exit 2 before any cluster call).

An orchestrator pod gets exactly five more environment names, all non-secret: `CW_AGENT`, `FLEET_AGENT_NAME` (the launch's name), `FLEET_BOSS` (`--reports-to`), `FLEET_CLOUD_CHANNEL_URL` (settings row `CW_HUB_CHANNEL_URL`, default `http://cloud-hub.cw.svc:8443/message`) and `FLEET_CLOUD_TOKEN_FILE` (`/run/cw-tmp/channel-token`, the path of the channel secret). The volume list and the token set are the worker's; the token set is chosen by role (see "Token roles" below).

**The hub's address for a pod.** A hub pod (an orchestrator, or any launch while the hub setting is on) gets exactly two more spec keys, `spec.hostname` (the pod's name) and `spec.subdomain` (`cw-agents`); the stack-pod allow-list admits exactly these two, together, with that subdomain. The launcher registers `http://<pod name>.cw-agents.<namespace>.svc.cluster.local:4096` (the cluster domain is the constant `CLUSTER_DOMAIN`) (namespace from `CW_CLOUD_NAMESPACE`, default `cw`), a name the hub accepts over http; no pod address is read. A launch with the hub setting needs the headless Service `cw-agents` (clusterIP None, selector `app=cw-worker`, port 4096) in that namespace; the hub manifests create it, the launcher does not.

**An orchestrator launch needs no hub setting for its entry to get its session.** Cause: the launch set its hub flag only from the two settings below, while the launcher registers a hub entry for every orchestrator launch, so without a setting the entry's `session` stayed empty, the hub refused the record as a bad agent record and every message to it answers 503. The flag is now also true when `--agent` is `orchestrator`, so the session step runs for every orchestrator launch; a worker launch without a setting is unchanged.

**The hub settings (launcher side).** `CW_HUB_CHANNEL_URL`: the channel address a hub pod is told, not a secret, default `http://cloud-hub.cw.svc:8443/message`. `CW_HUB_REGISTRY_SECRET`: the name of the hub's registry Secret in the cloud namespace, default `cloud-hub-registry`. Either one set means the hub setting is on: every launch registers, and each launch needs the headless Service `cw-agents`; both unset, a worker launch is exactly what it was. `CW_AGENT` is not a setting: the launch's `--agent` (`worker` default, or `orchestrator` with `--boss`) chooses it. Both rows are in the launcher's settings table (`cloud/launcher/README.md`) and in the code's one table, which `test_cloud_settings.py` checks.

**Channel credential (tightening-only).** The launcher draws a random 32-byte secret per pod and adds it to the pod's per-run Secret as ONE new key, `channel-secret`; the entrypoint copies it (mode 0400) into the memory-backed directory `/run/cw-tmp`. It expires with the pod and is never printed. The launcher registers the pod in the hub's registry Secret (`CW_HUB_REGISTRY_SECRET`, default `cloud-hub-registry`; keys `secrets.json` and `agents.json`): only the sha256 of the secret is stored; the pod's server password goes to a key named for the agent in the Secret `cloud-hub-pw` (mounted by the hub at `/etc/hub/pw`); `stop` removes all three entries. A failed registration refuses the launch and removes what it made. The registry is the launcher's only new cluster write.

**The image changes: an operator rebuild after merge.** The image now carries `orchestrator.md` beside `worker.md` and the cloud command and hub files (`cloud/worker/bin/`, byte-identical copies of `bin/` files, checked by `cloud/launcher/tests/test_orchestrator_agent.py`). Rebuild and publish it once after merge (see "Build and scan the image").

**Branch names.** No pod-side rule refuses a command line that merely contains the word `main` or `master`; the repository's rules decide what a push to the default branch can reach.

**Token roles.** The mint takes a role (`app_token.py OWNER/REPO --role ROLE`, default `worker`; an unknown role is refused before any network call) from one table of permission sets, `PERMISSIONS_BY_ROLE` in `cloud/launcher/app_token.py`. The worker role asks for, and accepts, exactly `contents: write, pull_requests: write, issues: read, metadata: read`. The orchestrator role asks for, and accepts, exactly `issues: write, pull_requests: read, checks: read, metadata: read` (no `contents` key at all). A launch with `--agent orchestrator` mints under the orchestrator role and records it on the pod as the annotation `cw/role: orchestrator` (a worker pod carries no annotation: its spec is unchanged); the reaper's renewal mints under the role the annotation names, and a pod made before the annotation renews as a worker. A custom mint command (`CW_GIT_TOKEN_CMD`) ignores the role, so the guarantee that an orchestrator never holds contents write holds only for the default mint command. An orchestrator pod made before this change has no role annotation and renews as a worker until it ends, so it is relaunched to take the narrower set. The orchestrator pod holds no App key, App id, vault setting or mint command.

The App already holds checks: read (the owner confirmed it on 29 October), so the orchestrator set needs no App change today; if a later set needs a permission the App lacks, the owner adds it and accepts it on each installation, never this change.

**What the orchestrator's token can and cannot do.** It can read issues, pull requests, check results and repository metadata, and it can write to issues only. It cannot push, create a branch, read a file, open, edit, approve or merge a pull request, change a workflow, a setting, a secret or a ruleset, or mint a token.


## The operator driver

```sh
export CW_KUBE_CONTEXT=<context>      # or CW_CLOUD_SERVER + CW_CLOUD_TOKEN_FILE, as the launcher
cloud/launcher/run_task.py WORKER OWNER/NAME BRANCH BRIEF_FILE [--pull-secret NAME] [--image IMAGE]
```

The target repository is an argument and has no default. The driver reads the model entry (the one `github-copilot` entry of OpenCode's own
credential store, read with `opencode auth export` in a scrubbed environment through a pipe, in memory; `CW_OPENCODE_BIN` names the program) and a git token (the stdout of `CW_GIT_TOKEN_CMD`;
default `app_token.py OWNER/NAME --fd N` beside the driver, which hands the one-hour GitHub App token over a pipe of its own, never standard output or standard error, and never prints it) in memory, creates the pod through
the launcher with both handed over inherited pipes (`--run-secret-fd`), port-forwards, opens a session, sends the brief,
waits for idle, then stops the worker (the end hook pushes the work; the pod and its Secrets are deleted).

- A normal end, a failure and an interrupt (SIGINT, SIGTERM) all clean up, and it says exactly once
  `per-run secret NAME-run cleaned` or `per-run secret NAME-run REMAINS: delete it with: <command>`.
- A worker whose end hook cannot push is kept so the work is not lost; its per-run Secret is still deleted and the
  pod's name and delete command are printed.
  A kept pod that has ended (Failed or Succeeded) is deleted with its Secrets `KEPT_GRACE_SECONDS` (24 hours) after its hard deadline, after one best-effort save to `<worker>/wip` and one `kept-expired` audit line; an unkept pod still there ten minutes after its hard deadline is deleted whatever its phase (live at the next reaper restart; the status shows the ended pods).
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

## Shape `L`, for a land (ask #2457)

`L` is a pod shape for a land: limit and request both 6 CPU and 16 GiB, nested Docker allowed like `M`, chosen by name only (`--shape L`; a launch with no shape is unchanged). Its request equals its limit, so one `L` pod takes 6 of the 7 or so CPU a node has after system pods (the plan assumed about 7; not read on a live node here): two `L` pods never share a node. The autoscaler may add a node for it, and a node is a cost.

Cost (list-price estimate, not a billed figure): 0.34 USD per hour (6 of 8 vCPU of a 0.452 USD node). One land of 45 minutes costs about 0.26 USD (0.34 x 0.75); one of 90 minutes about 0.51 USD (0.34 x 1.5).

A lane may hold at most 2 live shape-`L` cloud workers at once (the launch check in `bin/cloud_worker.py`, beside `MAX_M_PER_LANE`; a lower limit than the 10 of `M`, because one `L` pod takes a node and the autoscaler would add one each); the refusal reads `lane LANE already holds 2 shape-L cloud workers (...)`, exits 2 before any token or cluster call, and counts only `L` records. Test: `cloud/launcher/tests/test_m_cap.py` (`test_l_...`, with a mutant that never stops).

A dedicated pool of a larger SKU is an option, not part of this change: it needs the owner's word, and no Azure resource is created by it.

Why: `tests:pg` on our pods was measured at 2520 to 2618 s, while a reference host ran the same step in 759 s (shape `M` has a 4 CPU limit and a 2 CPU request). The first real land on `L` is to be compared with both numbers; that run is not recorded here yet.

| Input or outside call | Test | Mutant |
|---|---|---|
| the `L` row and its request | `cloud/launcher/tests/test_land_shape.py::Row` | request left at 2 CPU: `the L request is not 6 CPU` |
| the capacity count (3 nodes, then 2; 0 nodes; `not read`) | `::Capacity` | request of 3 CPU counts 3 nodes after one pod |
| the other rows and the default | `::Row.test_s4_...` | an edited `M` request fails the byte-equal check |
| the diff has no Azure create, node pool or scale command | `::NoAzureCreate` | a planted command is found by the scan |

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

## The pre-push check (`bin/cloud-prepush`)

The cause: cloud workers push, and then continuous integration finds a failure the worker could have found itself (a test that reads an edited file, a repository-wide scan of added lines). A pod cannot run the whole suite in reasonable time, and each brief used to re-describe by hand which tests to pick. `bin/cloud-prepush <base>` replaces the hand-written rule.

It is the last step before every push, and the only command a worker names for it: it runs `bin/cloud-pr-preflight` as its FIRST step (found next to itself by path, never through PATH; run with the same python and no shell; the base is passed in `CLOUD_PREFLIGHT_BASE`, never as an option). A preflight that fails ends the run with exit 1, its lines printed, and no test is run; a preflight that cannot run (missing, crash, timeout) is "could not run", exit 1, never clean. Then it lists the files changed since the merge base, selects every test file whose text names a changed file (basename match; false positives are possible, for example a changed `README.md` selects every test that names any `README.md`; none is missed) plus the scan tests listed in the script, runs each on the branch and on a clean worktree of the base (under the temporary directory, removed at the end), one test and one side at a time (no parallelism: a pod has a hard memory limit), prints one progress line per test on stderr (`[7/25] <path> branch`), and prints only the differences with the counts.

- Exit 0: no difference; 1: a difference, a test that could not run, or no test selected for a non-empty change; 2: a refused input (unknown base or a base starting with a dash, no merge base or a shallow clone, a dirty tree, an invalid bound).
- Bounds: `CLOUD_PREPUSH_TIMEOUT` seconds per test and side (default 120) and `CLOUD_PREPUSH_TOTAL` seconds overall (default 1500); each must be a positive whole number up to 86400, else exit 2. A test that times out, or is not reached when the overall deadline expires, is listed and counts as "could not run" (exit 1).
- The preflight runs the repository's whole check on both sides: slow in a pod; if it times out you get exit 1 and CI stays the gate. It has its own bound, `CLOUD_PREPUSH_PREFLIGHT_TIMEOUT` seconds (default 900, a positive whole number, else exit 2), counted inside `CLOUD_PREPUSH_TOTAL` (its bound is the smaller of the two, the total still bounds the run); the per-test bound does not apply to it. It runs serially and prints `preflight running (<N> s of <M>)` on stderr at start and every 60 seconds.
- Identical means the same exit code and the same normalised last 4000 characters. A new failure inside a test that already fails on the base with the same tail looks identical, so the last line says `identical: N (of which failing on both sides: M)` and those tests are listed by name. Exit status is decided by the rules above, so read that number: it is not a clean result.
- The child environment is an allow-list: `PATH`, `LANG`, `LC_*`, `TERM`, `PYTHONHASHSEED`, `CLOUD_PREPUSH_*`, with `HOME` and `TMPDIR` replaced by a temporary directory and `PYTHONDONTWRITEBYTECODE=1`. Every other variable is dropped, and so is any name containing TOKEN, KEY, SECRET, PASSWORD or CRED or starting with `GH_`, `GITHUB_`, the retired product prefix, `CW_`, `OPENCODE_`, `AZURE_` or `AWS_`.
- Hardening: every child (the preflight and each test run) starts in its own session, and on a timeout, SIGTERM or interrupt the WHOLE process group is killed and waited for, so no grandchild outlives the run. Each side has its own scratch directory (its own `HOME` and `TMPDIR`). `HOME` is replaced, so the tests see no git identity. A bound must match `[0-9]+` exactly (no space, no newline) and be at most 86400, else exit 2. An EMPTY change exits 0 before the preflight runs.
- Memory-heavy tests: the literal tuple `HEAVY` in the script (now `cloud/worker/tests/test_root_scanner.py`) names tests that need a lot of memory (a pod has a hard memory limit). A selected test on that list is NOT run: it is printed as `skipped: memory-heavy <path>`, counts as "could not run" (exit 1, never clean), and the last line gives the count. Continuous integration runs those tests.
- The base worktree is removed without git's force flag; if git refuses the plain removal, the script deletes only its own temporary tree and runs `git worktree prune`. A SIGTERM or SIGKILL leaves the temporary directory and the `.git/worktrees` metadata; `git worktree prune` cleans the metadata later.

<!-- preflight-modes:begin -->
### The preflight has two modes (ask #1299)

Cause: in a cloud pod the full preflight of the pre-push command (`bin/cloud-pr-preflight`) runs the repository's whole check on both the branch and the clean base, and it did not finish within 300 seconds, so the one pre-push command ended with "could not run the preflight".

- full mode (the default of `bin/cloud-pr-preflight`): every test file of `cloud/launcher/tests` and `cloud/worker/tests`, on the branch and the clean base. Time measured (a builder's measurement in a pod, not a CI figure): did not finish within 300 seconds.
- quick mode (`bin/cloud-pr-preflight --quick`, and the default of `bin/cloud-prepush`): only the test files that read a changed file plus the named scans of `bin/cloud_quick.py` (each a file and one selector, run through the same test code CI runs). The match is on the file's basename, so a doc change can select many files; the mode line says "N checks that read the K files (basename match)". Same three sections and exit codes. A reader is one rule for both commands (`is_test` of `bin/cloud_quick.py`: a `test_*.py` under a `tests` directory, or a `bin/test-*` script). A non-empty change that no check reads exits 1 with `NOT RUN: no check reads <files>` (the scans alone never make it clean); if the only readers are whole `bin/test-*` suites that cannot finish in a quick run, the line is `NOT RUN: no check that can run in quick reads <files> (cannot run in quick: <tests>)`, with one `NOT RUN: <test> cannot run in quick` line per such test, never silently skipped. `bin/cloud-prepush` gives the same verdict and reason, and starts nothing. A memory-heavy test (the one list `HEAVY` of `bin/cloud_quick.py`) is reported `skipped: memory-heavy <path>` and counts as could not run (exit 1). The quick run has a total time bound: `CLOUD_PREFLIGHT_QUICK_TOTAL` seconds (default 240, so the pre-push command's own tests still fit in a 300 s pod; each file's timeout is clamped to the time left, so the run never overshoots by a whole per-file timeout); `CLOUD_PREFLIGHT_QUICK_TOTAL` must be a positive whole number (else exit 2 with one line; it is read only by a quick run); when it is exceeded the run prints `NOT RUN: quick run exceeded <N> s (selected <S> files, ran <M>; stopped before <file>)` and exits 1. A change to a widely-named file (for example `cloud/launcher/cloud_worker.py`) can select dozens of files and so ends NOT RUN within the bound: use the full check, or name the test you need, never clean (a cap, chosen over limiting basename selection). Time (all figures are builder measurements in a pod, not CI figures, with no kept log): a six-file change took 194 seconds for both sides; a documentation-only change did not finish within 110 seconds (a builder measurement, the worst case measured, no log; hence the cap); the full mode did not finish in 300 seconds; the TestEverySite scan alone took about 17 seconds and bin/test-cloud-neutral-names about 4 seconds. A non-Python scan entry is run with `sh`. A change no check reads is decided before any run (`NOT RUN` at once, exit 1). `bin/cloud-prepush` keeps a second selector only as a fallback for a lone copy without the helper. Its missing-helper check is a text match of the sibling script: a convenience, not a guard.
- quick run budget: the whole quick run has a budget of 240 seconds by default, changed by `CLOUD_PREFLIGHT_QUICK_TOTAL` (a whole number of seconds). The quick run prints the seconds of each file on the branch and on the clean copy, and runs the clean copy only for a file that did not pass on the branch. A spent budget means that some selected files were not run, not that a test failed: the run ends with status 3 and the final line `NOT RUN (budget): ...` naming each file not run. A failing test still ends with status 1 and names the file as the failure, never as the budget.

<!-- scan-table:begin -->
| scan | in quick | why |
| --- | --- | --- |
| `bin/test-switchboard::TestGitShim.test_s7_u1_no_file_of_this_change_writes_the_global_git_configuration` | yes | the added-lines scan of the whole diff; one method, seconds |
| `cloud/launcher/tests/test_stub_cleanup.py::TestScanFindsTheShape` | yes | the shape finder of the string-of-code scan |
| `cloud/launcher/tests/test_stub_cleanup.py::TestEverySite` | yes | the repository-wide string-of-code and stub-cleanup scan; measured about 17 s in a pod, so a pod can run it |
| `bin/test-cloud-neutral-names` | yes | the neutral-names scan; about 4 s. The preflight's own full mode does not run it (it is not under the test directories); the full check reaches it only from CI's workflow, so the quick set is a superset of the preflight's full mode there |
| `bin/test-switchboard` | no | every other method: a pod cannot finish it; CI runs it |
<!-- scan-table:end -->
- A `bin/test-switchboard` selector is looked up by class and method name in the script and in the `tests/switchboard/test_cases_NN.py` files it imports its classes from, so it survives a move between them; a class found in none of them does not resolve, and the tests that check the quick list (`test_cloud_quick_guards.py`, `test_cloud_preflight_quick.py`, `test_cloud_quick_selector.py`) fail. The check is the tests', not a run-time one.
- `CLOUD_PREPUSH_PREFLIGHT=full` asks `bin/cloud-prepush` for the full mode; `quick` (the default) is printed as `preflight mode: quick`; any other value is refused with exit 2. Continuous integration stays the full check.
<!-- preflight-modes:end -->

### The background run (ask #1616)

Cause: the foreground command bounds each test file at 120 seconds and the whole run at 1500 seconds, and nothing of it survives the end of its starting command; on a pod the slow files of the full check run longer than that and are reported as "could not run", so no pod reported a full-suite result.

- Start: `CLOUD_PREPUSH_PREFLIGHT=full bin/cloud-prepush --background <base>` starts the same run (same selection, same preflight, same base comparison) detached (its own session, no stdin, output in `run.log`), prints the result file's path and the log's path, and exits 0 at once. The run has its own default bounds, 300 seconds per file and 7200 seconds in all; `CLOUD_PREPUSH_TIMEOUT` and `CLOUD_PREPUSH_TOTAL` override them. `CLOUD_PREPUSH_RESULT` names the result directory (default a fresh private directory under the temporary directory); `result.json` and `run.log` survive the end of the run.
- Result file: one JSON document, replaced atomically at the start and after every file: `version`, `started`, `finished` (null while running), `status` (`running` or `finished`), `selection`, and per file the `branch` and `base` result (`pass`, `fail`, `could not run`, `running`, `not started`) with seconds.
- Read: `bin/cloud-prepush --read FILE` (the result file or its directory) prints the lines the foreground run prints, plus a `RUNNING <file> (branch|base)` line for a file in progress and `cloud-prepush: still running: N of M files done`. Exit 0: finished, no difference; 1: finished with a difference or a file that could not run; 4: still running (never 0: a file not yet run is `not started`, never passed); 2: the file is missing or unreadable.
- Polling: a background process does not count as activity for the idle limit (only a busy session does), so a builder that starts the run and ends its turn is stopped as idle. A builder keeps polling with short foreground calls, one command at a time: `sleep 60; bin/cloud-prepush --read DIR`, until the exit status is not 4. A launch of a builder that will run the full check asks for an idle limit above the longest poll.

It needs git and python3 only, makes no network call and writes nothing outside its temporary directory. It is advice, never the gate: continuous integration stays the gate and its result is the only evidence. Tests: `cloud/worker/tests/test_cloud_prepush.py` and `cloud/worker/tests/test_cloud_prepush_preflight.py`.

## Saving unpushed work when the pod ends

A pod ended by its deadline or by a retire lost its unpushed work unless a fresh token was minted and the work pushed from outside, and that adds a token-issuance path; the pod's own token is renewed about every 30 minutes while the reaper runs (`GIT_TOKEN_RENEW_AFTER_SECONDS`), so the pod can push with it before it ends. Every worker container therefore has a `preStop` hook, `cw-prestop 45`, and the pod's `terminationGracePeriodSeconds` is 60 (`POD_TERMINATION_GRACE_SECONDS`; the hook budget is `PRESTOP_BUDGET_SECONDS`, kept below the grace).

- **What is saved:** uncommitted files (secret-looking names left out) are committed, and when the commit is on no remote branch it is pushed to `<worker>/wip` with a plain push (never forced) through the same credential helper the worker uses. A clean tree or a tree already on a remote branch: nothing happens and nothing is printed. Success prints `cw-prestop: saved work to <worker>/wip` to the pod's output, then `; ` and one JSON object with the untracked files it saved (below).
- **Which untracked files a hook saved (ask #1289).** Cause: the status list is staged whole (every untracked file except the secret-looking ones), and the hook's line names only the skipped secret-looking paths, so a stray untracked file (a test's leftover, a scratch file) went to the wip branch without a word. Both hooks (`cw-end-hook` and its `--prestop` path) now say which. The end hook's one JSON line keeps `pushed`, `wip_branch`, `skipped` (and `error` on a failure) and gains `untracked` (the untracked paths it saved, sorted, at most the first ten) and `untracked_more` (the count of the rest, 0 when none); with no untracked file the line is today's line plus `"untracked": []` and `"untracked_more": 0`. The pre-stop line is `cw-prestop: saved work to <worker>/wip; {"untracked": [...], "untracked_more": N}`. A pod cannot know a declared set of files (nothing in it records which files a worker meant to keep), so the line names all untracked paths saved and a deliberate new file is listed too: the reader judges. A path is JSON-escaped (a space or a newline is carried by the string, the line stays one line) and the line names paths, never content. A secret-looking path is never saved and never listed in `untracked`: only its name appears, under `skipped`. What is saved is unchanged.
- **The run files in the answer (ask #2432).** The end hook's one JSON line gains `runs` (at most 20 entries, newest first, each `{name, exit, woke, log_tail}` read from `/work/runs`: the exit number or null, whether `NAME.woke` exists, the last 20 lines of the log redacted and cut to 3000 characters) and `wake_lines` (the last 10 progress lines that start with `run ` and contain ` exited `, or equal `wake not delivered`, at most 200 characters each), the whole addition at most 20000 characters; the launcher keeps both in the run record's `end_hook.run_logs`, never in the wip branch, and a failure to read or store them never blocks the stop. Test: `test_end_hook_run_files.py`.
- **When it fails:** an expired token, no network, a branch that has moved on (non-fast-forward) or a push that hangs past the budget. The hook then exits 0 within the grace and writes one line, `cw-prestop: not pushed (<reason>); work left in /work/repo and is LOST when the pod ends; unpushed files: <names>` (to stdout and stderr, at most 50 names, names only). **The work is LOST when the pod ends**: the pod and its volume are deleted after the hook, so a refusal or an early return is data loss, not retention. The line is the only trace of a refused run; it lives in the pod's log and ends with the pod unless the cluster ships logs elsewhere (nothing in the repository keeps it). The launcher does not collect the hook's output. The remote url is compared after userinfo is stripped, and the push goes to the URL built from CW_REPO whatever the remote says; so a person who sees it while the pod still exists can ask for the listed files. The same line is printed for every early return: a bad worker name or repository name, no git repository, `.git` that is a file (a linked worktree or a separate git directory is not supported), no credential helper, a refused configuration, an origin elsewhere, a timeout or a push failure. A pod whose token expired loses its unpushed work unless the reaper kept the token renewed. The launcher's `stop` refusal text names the read-only recovery route: the work stays in `/work/repo` of the kept pod, and the pod's own output line and the run record say why it was not pushed.
- **Trust:** The hook does not trust the repository's configuration: it refuses a repository whose local configuration has any key outside a short allow-list (it names the key and leaves the work in /work/repo), ignores every global/system configuration, and pushes only to the explicit address of the worker's own repository with the pod's own credential helper; the honest limit: the pod token lives 60 minutes (until the reaper renews tokens) and the hook runs only while the token is valid. The allow-list is the plain keys a clone writes (`core.repositoryformatversion`, `filemode`, `bare`, `logallrefupdates`, `ignorecase`, `precomposeunicode`, `symlinks`; `user.name`, `user.email`; `remote.origin.url`, `pushurl` and `fetch`; `branch.*.remote` and `.merge`; `init.defaultbranch`; `gc.*`; `pack.*`; `receive.denycurrentbranch`); keys from an included file, `include.*`, `url.*`, `credential.*` and `http.*` are all refusals. If the repository's configuration is odd or hostile the hook pushes nothing and the unpushed work is LOST when the pod ends (the trade-off for not trusting that configuration); the one output line names the key only, never a value.
- **Limits:** the agent keeps running while the hook runs, so a file written mid-push can be left out. The hook mints and reads no new token or file. The worker image changes (`cw-prestop`, the `--prestop` mode of `cw-end-hook`): rebuild the image first, then the launcher. Whether the kubelet runs the hook at `activeDeadlineSeconds` is decided by a live check after merge.

**The token at its limit (#1354).** **Cause.** Workers ended with unpushed work because the one-hour git token had ended before the push: the reaper's renewal (30 minutes left) can miss, when the reaper sleeps, the vault refuses or the kubelet projects late, and the end hook's push is then refused. Two changes, both on the launcher side; the pod gets no new credential (it holds none for the minter and never will).

- **No commit yet (ask #2381).** A running save writes and pushes nothing (`saved: false`, note `no commit yet: nothing saved`) while the pod has no readable `HEAD` and `<worker>/wip` has no earlier save, so no save is ever a parentless commit; existing branches that hold a root are left as they are.
- **A save is the pod's own history (ask #2437).** A running save commits with the pod's `HEAD` as its one parent (never the previous save) and names the previous save in a `Previous-Save: <full object name>` line of its message, so `<worker>/wip` is the pod's commits with one commit on top and a rebase of the branch replays each commit once; the push is plain when the remote tip is an ancestor of the new commit, else one `--force-with-lease` push naming the tip fetched in the same call, to `refs/heads/<worker>/wip` only (a lease failure prints one line and is not retried). Existing branches that hold merges are left as they are. The running save is live at the reaper's next restart; the pre-stop save is baked into the image and reaches pods with the next image publish. Test: `test_wip_rebasable.py`.
- **The note at minute 40.** The reaper's pass sends each worker ONE note when its pod is 40 minutes old (the `cw/wip-note` pod annotation is set before the send, so a second pass sends none): it names the branch `<worker>/wip` and the exact command `git push origin HEAD:refs/heads/<worker>/wip` (plain, never forced). It reaches the worker's own session through `kubectl exec` of a short program that reads the same server password file the activity probe reads; no image change is needed.
- **One refresh and one retry.** The rule is exactly one refresh and one retry. When the end hook's `fetch` or `push` is refused because the token had ended, the hook answers `refresh: needed` (a fixed word; nothing of the remote's text) and the STOP side, not the pod, mints through the renewal's route (`run_git_token_cmd`), replaces ONLY the `git-token` key of the per-run Secret, waits (at most 120 seconds) until the pod's file shows the new token (a digest comes back, never the token), and runs the hook once more. A refused refresh means no second attempt and no loop; a refusal for any other reason (a rejected ref, a hostile configuration, the network) asks for no refresh. A second token end during the retry loses the work: the pod is kept as for any hook failure.
- **Scope.** The new token is for the same repository and the same permissions, with the same one-hour life (the minter's own checks and its 61-minute refusal are unchanged) and is checked once more with GitHub before the Secret is patched: exactly the pod's one repository, push and pull, never admin or maintain; anything unreadable is a refusal and the old file is kept. The token is never printed or written to a log, record or audit line. The pod's token file is replaced by a temporary file and a rename: that is the kubelet's own projection of the Secret; nothing added here writes the file.
- **The word.** The stop's audit line carries one word, refresh: used, refused or none (a word, never a value), and so does the `end_hook` part of the run record.

Not run in a cluster: the live check (a real pod with an ended token, and the note reaching a real session) is the operator's, after merge.

## The stall watch (ask #2399)

**Cause.** A worker pod wrote that every shell command was refused and then sat idle for 75 minutes: nothing read a live pod's progress file, run logs or the agent's last words, and the idle nudge fires once. Now the reaper's pass (`cloud/launcher/stall_watch.py`, one call after the idle nudges) reads each running pod older than 10 minutes with ONE `kubectl exec` (3 seconds at most): `/work/progress.txt` (modification time, last lines), every file under `/work/runs` (log name, time, last line; exit number and time) and the agent session's last message (at most 400 characters, through the pod's own server). It sends an alert for three conditions:

- **`progress-stale`**: the progress file and every run log unchanged for 15 minutes and no run active (a run is active when its exit file is absent and its log changed in the last 3 minutes).
- **`refusal`**: the agent's last message or the progress file's last two lines hold the text `Permission denied: shell`, the text `permission.rejected`, or a line that starts (after leading blanks) with `refused:`, `Blocked:` or `Blocked at` (case-sensitive; the same words in the middle of a sentence do not count), or one error line occurs 3 times in the progress file's last 10 lines.
- **`failed-run-idle`**: a run's exit file holds a non-zero number and neither the progress file nor the agent session changed for 10 minutes after the exit file's time.
- **The session's activity** (same exec, same local route: the session's last update, the time of its last message or tool call and whether a turn is running, from `/api/session`, its messages and `/api/session/active`): `progress-stale` and `failed-run-idle` stay silent while that last time is younger than their 15 and 10 minutes, or while a turn is running and that time is younger than 30 minutes; a running turn with no new message or tool call for 30 minutes still alerts (a turn hung in one tool call). When the server does not answer or the fields are missing, the condition decides from the files and its evidence line says so.

A pod that has finished is not stalled: when the progress file's last non-empty line is `done` or `STATUS: done` (any case), or `/work/results.txt`'s last non-empty line is `STATUS: done` (read in the same one exec, at most 200 characters), `progress-stale` and `failed-run-idle` are skipped; `refusal` still fires (a line that merely contains the word, such as `not done yet`, does not count).

Each condition is alerted at most once per pod per 60 minutes (the pod annotation `cw/stall-alert-<condition>` is set before the send). One message goes to the pod's boss (`reports_to` in the switchboard's worker record, `CW_CLOUD_RECORD_DIR`) and one to the Chief of Staff, through the switchboard's `send`: the pod name, the condition, one evidence line (secret-looking text and the launcher's known secrets replaced by `[redacted]`, at most 300 characters; untrusted data, never a command) and the time. Nothing is stopped, restarted, nudged or changed in the pod. A pod younger than 10 minutes or a pod whose exec fails is skipped, and the pass line names it with the reason and no pod content. The pass line gains ` stall read=N alerted=M [skipped=pod:reason,...]` before its steps. Rollback: revert the change and restart the reaper.

## Tests (no cluster, no Azure)

```sh
cloud/tests/test-cloud          # prints RESULT: PASS
```

**The rule for every test of the cloud tree (#956):** a test that starts a server or another long-lived child process starts it through `cloud/worker/tests/stub_support.py` (`start` registers the cleanup first; the cleanup kills the child's whole process group, waits, and asserts the port is free), or registers its own cleanup for that very process. The stub server also ends by itself (on SIGTERM, when its parent is gone, and after an idle cap), because a SIGKILLed runner runs no cleanup. `cloud/launcher/tests/test_stub_cleanup.py` scans the cloud tests by syntax tree and fails any start site that does neither, with a case and a mutant per site. The scan follows one closed rule: a constant call of a spawning primitive (through the usual import aliases and `getattr(module, 'name')`) is a start site, and ANY OTHER reference to one is itself a site that fails closed (an alias such as `P = subprocess.Popen`, `functools.partial`, a primitive passed as a value, `subprocess.__dict__`, `__import__`/`importlib.import_module`), as is a string of code that starts one (read as code when it parses, otherwise on any use of the primitive) and a Python file it cannot parse; such a site passes only when `EMBEDDED_OK` lists its exact text with a reason. Not followed (stated in the test file's `LIMITS`): a star import, a name assembled at run time, and a spawn through a callable of product code. The port check counts only a refused connect as free; the stub names its parent through `STUB_PARENT_PID` (set by `stub_support`), so a parent that dies during startup still ends it.

**The three relay suites in CI (ask #1376).** The `test-cloud` job of `.github/workflows/test.yml` runs `bin/test-cloud-relay`, `bin/test-cloud-hub-relay` and `bin/test-cloud-relay-daemon` as steps of their own, in this order, after the other suite steps. Measured in the first pull request run at `e4d27788` (a CI measurement; seconds, Ubuntu / macOS): relay 28 / 70, hub-relay 36 / 69, relay-daemon 67 / 117. Each step has `timeout-minutes: 10`, more than twice the longest time (117 s). All three run on both systems.

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

`bin/cloud_image_roll.py SOURCE@sha256:DIGEST REGISTRY TAG [--dry-run]` (`cw-image-roll`, the operator's machine only) copies a published image with `crane copy`, checks the destination digest and only then changes the one `CW_CLOUD_IMAGE=` line of the settings file (backup first); a failed copy or a different digest changes nothing.

Images are built only from integration's tip after merging, never from a merge of branch heads, and the build passes `--build-arg CW_IMAGE_REVISION=<the commit the tree was taken from>` (`git rev-parse HEAD`), which the image carries as its `org.opencontainers.image.revision` label (ask #2418). `bin/cloud_image_proof.py DIGEST` (cw-image-proof) launches a proof pod on exactly that digest, runs the proof set of `cloud/worker/proof-set.md`, retires the pod and writes a receipt (mode 0600) in the operator's state directory; `cw-image-roll` refuses, before any copy and with no bypass flag, an image with an empty revision label, a revision not reachable from `origin/integration`, or no passing receipt for exactly its digest younger than 6 hours.
The proof command launches its pod with the switchboard's `launch --cloud` (the one command that makes the git token and the cluster login), naming the candidate image for that one launch in a scratch configuration directory (`XDG_CONFIG_HOME` of the launch only: a copy of `cloud.env` with the candidate on the `CW_CLOUD_IMAGE` line, every other entry linked), removes that directory at the end whatever the verdict, and retires the pod with the switchboard's `retire NAME --close`; the real settings file is never written, the brief is `cloud/worker/proof-brief.md` and the proof set's commands are ones the pod's shell rules allow (ask #2440).

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

### What is and is not scanned (unmodified distro files are skipped)

The scan reads every file WE put in the image, and skips only the files the distro's package manager put there and that are
still exactly as the package shipped them. A file is NOT scanned only when BOTH hold: (a) it is named in an installed
package's file list (`/var/lib/dpkg/info/<package>.list`), and (b) its md5 equals the entry for it in the SAME package's
`/var/lib/dpkg/info/<package>.md5sums`. Why: distro binaries contain chance matches (an `ASIA` plus sixteen capitals in a
table of instruction names in `libLLVM`, in `libgnutls`, `libunistring`), and one allowlist line per binary and per
distro update is the wrong fix. Scanned as before: our `COPY`'d files and scripts, configuration, the downloaded Node and
opencode binaries, `/opt/pg` (built by us), a file dpkg owns whose md5 DIFFERS (a modified distro file still fails),
a file no package owns, the dpkg database files themselves, a credential file by NAME (`.netrc`, `.ssh/id_*`, ...: the name
check is never skipped), and the image configuration (env, labels) and history. The allowlist and its format are unchanged
and still apply to everything that is scanned.

Rules of the skip set: a path with a `..` is never in it; a directory symlink in a listed path is followed inside the root
(so `/lib/x/f` finds `/usr/lib/x/f` on a usr-merged image), but a `..` that would climb out of the root, a loop, or a
symlink as the file itself is not resolvable and not skipped; a package with a list but no md5sums skips nothing; a name in
md5sums but not in the same package's list skips nothing. FAIL CLOSED: if the dpkg database is missing, unreadable, has no
package list, or any list or md5sums file is malformed, the skip set is EMPTY, every file is scanned and the scan says so on
stderr (`dpkg database not usable (...): skip set EMPTY, every file is scanned`). Output: `dpkg: N unmodified files of installed
distro packages not scanned`.

`--dir` reads the database inside the root it scans. `--archive` (and so `publish-image.sh`) keeps the per-layer scan as it
is (every layer once, a deleted file still seen, an unreadable layer exit 2, the digest scanned and the tag re-resolved by
`publish-image.sh` unchanged) and, in the same pass, records each regular file's md5 and the dpkg files, symlinks,
directories and whiteouts of each layer. After the last layer it builds the MERGED view of the database (layers in order; a
`.wh.<name>` whiteout or an opaque directory deletes what is below; a later layer's file replaces the earlier one) and only
then decides, per file, whether it is an unmodified distro file; a file whose md5 differs from the final md5sums is a hit in
the layer that holds it, also when a later layer replaces it. Hits and layer lines are printed after the last layer.

Why this is safe: the scan guards against a credential written by accident. Whoever can alter the Dockerfile or the package
database controls the build and the image anyway (they could drop the scan stage); the rule gives them nothing they did not
have. A file that is not exactly what its package shipped is scanned. The md5 comparison is not a defence against a
deliberate collision, which would need a second preimage of a distro file. Accepted gap: the rule trusts ANY package dpkg has installed, not only distro ones: a `.deb` we build ourselves and install with `dpkg -i` gets its own list and md5sums and is then not scanned, so do not package files that may hold credentials. Also: a credential that a distro package
itself ships unmodified (including Docker's third-party apt packages) is not found; those packages come from signed
repositories, and the pinned download checks (sha256) cover what we fetch directly.

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

Shape requests: the limits are the `SHAPES` rows (S 2 CPU / 4 GiB, M 4 CPU / 8 GiB); the pod's requests are the `SHAPE_REQUESTS` row when the shape has one (S 500m / 2Gi, M 2 CPU / 8Gi: the CPU request is half the limit, the memory request stays at the limit) and equal to the limits otherwise (XS, O). The capacity read counts a node's room for a shape by that request, else by its limit.

The stack pod is pinned whole, like the default pod: `cloud/launcher/tests/fixtures/stack_pod.json` is the pod the stack create
builds, and `stack_create_objects.json` is every other object the create makes (the password Secret, the run Secret and its
labels; the run record is written by `stop`, not by `create`). At render time a stack pod must also have exactly one container
named `worker`, the whole pod-level security context, only the allow-listed pod, container and volume fields, plain `name`/`value`
env entries and the three known volumes. Any added container, field, volume or env therefore fails CI; a deliberate change edits the
constant and the fixture together (regenerate with `CW_WRITE_STACK_FIXTURE=1 python3 cloud/launcher/tests/test_cloud_stack.py`, then
read the diff). An image by tag is accepted by design: the operator picks the image, and no digest is required here.
Details: `docs/cloud-worker-interface.md`. The live runs are in
"Live proof" below.

## A stack-only model credential

The pod holds only the coder's model entry (`model-auth`); the containers a stack starts hold none, and giving them `model-auth` would put the coder's key into the stack. With `launch --cloud --stack` and the setting `CW_STACK_MODEL_SECRET` (a vault secret name), the switchboard reads that secret before any pod exists and hands it over as the per-run secret `stack-model-key`; the entrypoint copies it to `/run/cw-tmp/stack/model-key` (0600). `cw-stack-up` (use it instead of `docker compose`) gives the compose child the variable `CW_STACK_MODEL_KEY`; a service lists `CW_STACK_MODEL_KEY` under `environment:`. `cw-stack-up down` removes the copy. The audit and run record carry the name and `delivered`. The coder can read this credential (same user, docker group); the protection is purpose-scoping, a spend cap and revocation at the provider. Owner steps: `docs/cloud-operator-quickstart.md`, "A stack-only model credential".

## Pulling prebuilt stack images (ask #1022)

**The cause.** A worker pod sets `automountServiceAccountToken: false` and the guard refuses any pod with a service-account token, so no cloud identity works in a pod today: the label `azure.workload.identity/use` has nothing to exchange. (Whether the cluster's webhook injects a token for the default account is read live by the tester in the live slice; this text changes if it does.) A stack therefore built its images inside the pod, and peaked at 95 percent of its memory limit.

**What a stack pod gets when the three settings are set.** With `CW_STACK_REGISTRY` (a registry host ending in `.azurecr.io`), `CW_STACK_PULL_CLIENT_ID` and `CW_STACK_PULL_TENANT_ID` (GUIDs, not secrets) all set, `create --stack` makes a pod that uses the service account `cw-stack-puller` (manifest: `cloud/k8s/stack-puller-sa.yaml`, token not auto-mounted), has no workload-identity label, and carries one extra volume: a projected service-account token (audience `api://AzureADTokenExchange`, 600 seconds, mode 0400) plus the three values as files, mounted read-only at ONE path, `/opt/cw-registry/token`, whose parent `/opt/cw-registry` is root-owned mode 0700 in the image. With none of the keys set the pod is exactly as before; one or two of them are refused, naming keys and never values.

**The step.** `sudo cw-stack-pull IMAGE...` (a root-run script; the sudoers rule names it and `start-dockerd` only, with `env_reset`) trades the token for an Entra access token, trades that for a short-lived registry credential, logs the nested Docker in with `--password-stdin` into a 0700 directory on the memory tmpfs (files 0600), pulls each image (which must be on the configured registry host, read from the root-only file, never from the caller), and removes the directory on success, failure and SIGTERM. The credential is never an argument, an environment variable, a log line or part of the run record. A pod that never pulls never holds a credential.

**Honest residual risk.** The coder is in the `docker` group, which inside the pod is root-equivalent: a nested container can bind-mount `/` and read the token file. So the isolation covers an accidental or prompt-injected shell (`cat`, `printenv`, a copied log) and any container the stack starts by default; it does not stop a coder who deliberately builds a container to read it. The bound: a 10 minute token, valid only for the exchange for one identity whose only right is to pull from one small registry.

**Owner steps, in order** (the launcher never creates or changes an identity, a credential or a role):

1. Create a user-assigned managed identity (`cw-stack-puller-id`) and give the launcher's operator its **principal id** (the one value the registry's owner needs).
2. Create a federated credential on it: the cluster's OIDC issuer, subject `system:serviceaccount:<ns>:cw-stack-puller`, audience `api://AzureADTokenExchange`.
3. The registry's owner grants `AcrPull` to the principal id, at the registry only (no write-capable role).
4. Apply `cloud/k8s/stack-puller-sa.yaml` with the identity's client id and tenant id as annotations, then set the three keys, then publish the rebuilt image (new files: `cloud/worker/cw-stack-pull`, the Dockerfile lines).

**The check, before any pod exists.** `cloud-worker stack-registry-check --resource-group RG --issuer ISSUER_URL` (needs `CW_STACK_REGISTRY` and `CW_CLOUD_NAMESPACE`; runs with your own `az` login; read-only) prints the principal id and `OK`, or each missing owner step (exit 1, also for a write-capable role), or `CANNOT TELL: <reason>` (exit 2, never a pass). It cannot call the token exchange: only a live stack pod proves that.

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

## Long commands: `cw-run` and `CW_LONG_RUN_DENY` (ask #2369)

The shell tool moves a foreground call that outlives its time limit to a background shell and promises a notification; none wakes the agent, so the pod sits silent until its limits end it. `cw-run` (root-owned, in the image) runs such a command detached: `cw-run start [--name NAME] -- COMMAND...` (own session, stdin closed, output to `/work/runs/NAME.log`, status to `/work/runs/NAME.exit`; returns within 2 seconds), `cw-run wait NAME [SECONDS]` (at most the smaller of SECONDS and 240, default 120; prints `running Ns` or `exited CODE` and the last 20 log lines), `cw-run tail NAME [LINES]`, `cw-run list`. While a run lives it appends a line to `/work/progress.txt` every 5 minutes and one when it ends. A name is letters, digits, dot, dash, underscore; a second start under a running name is refused.

The operator's setting `CW_LONG_RUN_DENY` (settings file only; 1 to 8 comma-separated patterns of letters, digits, `. - / _ * ?`, each at most 80 characters, none starting with a hyphen) is passed to every worker pod. When it is set the entrypoint appends to both `permission.bash` maps of the pod's copy of `opencode-worker.json`, after every existing row, one `deny` row per pattern and then the row `cw-run start*` `allow`. The matching order: the LAST matching row decides (the repository's evaluator `cw-shell-check` applies it, and `test_entrypoint_long_run.py` proves it with that evaluator); the runtime splits a command into its parts and every part is matched, so `cw-run start x; <denied command>` is still refused. `cw-run start` itself holds the command against the template's rules (the list without the operator's patterns) and refuses one they deny, so the allow row does not widen them. Unset, the file is the template's, byte for byte. The pod's config directory is private to the entrypoint's user (0700, on tmpfs).

When a `cw-run` command ends, the helper wakes the agent, once per run (a marker file `NAME.woke` is created before the first attempt, so a second helper process for the same name sends nothing). After the exit file is written it adds one user message to the agent's own session through the pod's local server (`POST /api/session/<id>/prompt` with `{"text": ...}`, the session being the most recently updated one of `GET /api/session`), with the text `run NAME exited CODE after Ns; last 20 lines:` and the redacted tail (at most 3000 characters); it appends the first line to the progress file; and it sends the first line with the last 5 log lines to the boss through the pod's client (`cw-cloud send`). A busy or silent server is retried every 10 seconds for up to 5 minutes (`CW_RUN_WAKE_GAP`, `CW_RUN_WAKE_LIMIT`), then `wake not delivered` is written to the progress file and the log. A failure of one of the three never blocks the others and never changes the run's exit number. The wake cannot reach an agent whose turn is stuck inside a tool call, nor a pod whose server is down. Test: `test_cw_run_wake.py`.

What the rows cannot catch: a long command wrapped in a script file the patterns do not name, or one started by a program other than the shell tool. Rollback: unset the setting, or revert the change.

Tests: `test_cw_run.py`, `test_entrypoint_long_run.py` (with a mutant that puts the allow row first) in `cloud/worker/tests/`, `test_long_run_setting.py` in `cloud/launcher/tests/`.

## Trust boundaries and limits

- `containerd-fix.yaml` is a node-root component: privileged, `hostPID`, and a ClusterRole that can `get` and `patch`
  nodes. The label `sysbox-install=yes` is the only gate on where it runs; the real boundary is who may patch node labels (the DaemonSet's own token can patch any node), so restrict that RBAC.
- Sysbox is the boundary that makes the worker's `docker` group and `sudo start-dockerd` safe. Do not run the image
  outside a Sysbox pod with `WORKER_DOCKER` left at 1 on a shared host.
- The scan finds known credential shapes only; "0 hits" is not proof that no secret exists.
- The fixer image is pinned by tag, not digest: pin it for a release (the OpenCode binary is already pinned with its sha256).
- `cloud/tests/test-cloud` is not yet run by CI (this change is limited to `cloud/`); it needs Docker for the image cases
  (`CLOUD_TEST_SKIP_DOCKER=1` skips them explicitly).

**The reaper pass line and the renewal tick line (#1742).** The reaper writes one `reaper pass line` for each pass and one `renewal tick line` for each tick of its renewal thread, on its own log (standard error), not in the audit. Each line holds its numbers: `cpu_seconds`, the CPU seconds of the thread that ran the pass or the tick, and `audit_lines_parsed`, the audit lines read and parsed in it for the renewal's failure lookup; the pass line holds those two numbers and the seconds and counts of the seven steps of the pass (`list`, `renew`, `vanished`, `activity`, `stops`, `orphans` and `notes`, with the number of pods for `activity` and `stops`, and the `total`), and nothing else. They are counts only: no pod name, no worker name, no address, no value. To see whether the reaper spends CPU between passes, read the renewal tick lines: a tick with a high `cpu_seconds` or a high `audit_lines_parsed` is where the CPU went; a tail is read once per tick, so `audit_lines_parsed` is at most the lines of the audit tail (about 262,144 bytes). The step with the most seconds in the pass line is where a slow pass spent its time.

## The token service (ask #2002)

A small service in the cluster (`cloud/launcher/token_service.py`, manifest `cloud/k8s/token-mint.yaml`) holds the GitHub App's id and key
and mints the pod's git token, so the machine that launches pods no longer reads the key. The operator account calls it through the cluster
API's service proxy; the service reviews the caller's token and serves the operator account only. It accepts one repository on the
launcher's allowed list and one role, and answers with GitHub's own token, repositories, permissions and expiry.

- The setting `CW_TOKEN_SERVICE_URL` (`https://<server>/api/v1/namespaces/cw/services/token-mint:8080/proxy/mint`) makes `app_token.py` ask
  the service; the answer gets the signing route's checks on token characters, repository, expiry and permissions (not the installation owner id, which only the signer can compare). With it unset the signing route runs as before: that is the rollback.
  `CW_TOKEN_SERVICE_CALLER_TOKEN_CMD` may name a command that prints the cluster token; unset, the launcher's own cluster login is used.
  Preflight note: the repository preflight's child gets `CW_CLOUD_SERVER`, `CW_CLOUD_NAMESPACE` and `CW_CLOUD_CA_FILE` (no secrets) with the two service settings,
  but not the launcher's cluster-login settings: it reaches the service only if `CW_TOKEN_SERVICE_CALLER_TOKEN_CMD` prints a cluster token that the launcher holds.
  The client sends the cluster token in `Authorization` and in `X-Caller-Token` (the API server removes `Authorization` of a proxied request;
  the service reads and reviews only `X-Caller-Token`), verifies TLS against `CW_CLOUD_CA_FILE` when it is set, and refuses a service URL that is
  not exactly the one above (the host of `CW_CLOUD_SERVER`, the namespace `CW_CLOUD_NAMESPACE`) before it sends the token.
- A passing failure of the service (502, a timeout, no connection) exits 75 and the launcher tries again; a refusal exits 1.
- The owner applies the manifests (`token-mint-review.yaml` holds the one cluster-wide permission), creates the Secret `token-mint-app`
  (keys `app-id`, `app-key`) and publishes the image.
- Tests: `cloud/launcher/tests/test_token_service.py` and `cloud/launcher/tests/test_app_token_service.py` (fakes only).

## The Copilot bridge (ask #2341)

A council model endpoint for the worker pods, built from the open-source bridge `copilot-api` 0.7.0 (npm tarball sha1
`cdc6b4973c8c014f90c3df8fdbe1e9f5c4d00b9d`, tag commit `36847c22741f2b397ecb29a001105085fe14b545`; `cloud/copilot-bridge/package-lock.json` pins every
dependency by integrity and the image installs with `npm ci --ignore-scripts --omit=dev`). The bridge keeps the owner's GitHub token in a file
`github_token` (mode 0600) under the data directory and prints a token only with `--show-token`, which is never passed. It has no bind option and listens on
every interface by default; its HTTP server reads the `HOST` variable, so the entrypoint sets `HOST=127.0.0.1` for it. The policy front
(`cloud/copilot-bridge/bridge-front.mjs`, Node standard library only) is the only listener a pod can reach: it forwards `POST /chat/completions` and
`POST /v1/chat/completions` only, answers `GET /usage`, refuses a body over `BRIDGE_MAX_BODY` (400000 bytes), refuses an id containing `opus` (any case) with
403 and `model banned from the council: opus`, maps every other model to `claude-sonnet-5.5` at `medium` (`claude-haiku-5.5` passes with the caller's effort),
answers 429 past `BRIDGE_DAILY_BUDGET` requests a day (default 500), and logs one count line per request: never a body, an answer, a header or a token.
The bridge forwards the whole body to Copilot unchanged, so the front sets both `reasoning.effort` and `reasoning_effort`.

The owner's steps (nothing here is applied by the repository):

1. Create the Secret from the vault: `kubectl -n cw create secret generic copilot-github-token --from-file=github_token=<the file>` (the key is `github_token`).
2. Build and push the image from `cloud/copilot-bridge/`, and put its digest in place of `IMAGE_PLACEHOLDER` in `cloud/k8s/copilot-bridge.yaml`.
3. Apply `cloud/k8s/copilot-bridge.yaml` (Deployment, Service `copilot-bridge`, NetworkPolicy admitting only pods labelled `app=cw-worker`).
4. Set `CW_COUNCIL_URL=http://copilot-bridge.cw.svc:8080` in the launcher's settings file: a worker pod then carries `COUNCIL_MODEL_URL` with that value and no key.
Rollback: unset the setting, delete the Deployment (`kubectl -n cw delete deployment,service,networkpolicy -l app.kubernetes.io/part-of=copilot-bridge`), revoke the sign-in at GitHub.

Tests: `test_copilot_front.py`, `test_copilot_manifest.py`, `test_council_url_render.py` in `cloud/launcher/tests/` (fakes only).

## Shell call cap (ask #2447)

Second layer: the worker image sets `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS=300000` (`cloud/worker/Dockerfile`; a test holds it within 1..the plugin's `CAP_MS`), so a call that passes no timeout is capped even when the plugin is not loaded. This layer does NOT bound a call that passes its own longer timeout; only the plugin does. Whether the plugin's hook works on the pod's OpenCode build is decided by the pod proof (S5/S6, the operator's tester run after the next image). A hook that throws at call time leaves the spec as it was and prints one line.

A pod plugin (`cloud/worker/plugins/cw-tool-cap`, named in `opencode-worker.json`) sets the `timeout` of every shell spec in `shell.create.before` to at most 300000 ms (absent, zero or larger becomes 300000), and adds one fixed sentence (matched loosely on the words "timeout"/"timed out" of the result, so a result that merely mentions them may carry the sentence too) naming the limit and `cw-run` to a timed-out result. Anything longer goes through `cw-run start`. The note is best-effort, and the clamp fails open: when the build offers no shell hook API or registering throws, the plugin prints one plain line (`cw-tool-cap: shell hooks unavailable, calls are NOT capped`) and calls are not capped; a failing `run.after` registration is reported the same way without echoing any error text, and only the note is lost.

Read before building: OpenCode's shell default is 120000 ms and the call takes a per-call `timeout`; the environment variable sets the default only; `mcp.timeout` bounds MCP servers and no key clamps a shell call. UNVERIFIED here (the local build is 2.1.4, no pod): whether 2.2.7 honours a changed spec `timeout`, and the exact timeout text and hook name for the result; the sentence hook is wired only if the build offers it. The tester's pod proof (S5, S6) settles both.

| Input / outside call | Test | Mutant (control first, failure text) |
|---|---|---|
| spec `timeout` 60000/300000/300001/3600000/absent/background | index.test.mjs "S2: six specs" | clamp only a present value: assert.equal failure (undefined !== 300000) on the absent spec |
| installed `plugin` entry | test_tool_cap test_s3_plugin_listed_once | remove entry: list mismatch |
| config timeouts | test_s3_no_timeout_above_cap | add `mcp.timeout` 600000: list shows 600000 |
| result text | index.test.mjs "S4: note" | drop sentence or the run.after hook line: endsWith(NOTE) fails |
| image copy | test_dockerfile_copies_plugin | remove COPY: count 0 != 1 |

## The daily land report (ask #2448)

`cloud/launcher/land_report.py` is READ-ONLY. The reaper's pass (`_land_report` in `cloud_worker.py`, one inserted call) runs `daily_pass`: at the first pass at or after 13:00 UTC of a day whose report is not complete, it reads the last 24 hours of run records and of landed commits, sends ONE message (the switchboard command `send cos --issue <the setting>`) and adds ONE comment line (`gh issue comment`) to the pinned issue. It edits no issue body. The last-sent day and each part's state (`message`, `comment`) are in `<run record dir>/.land-report.json` (0600, written atomically after each part); a part that failed is tried at the next pass, a part that went is not sent again. Rollback: remove the call.

**What the records hold (read on 12 November 2026).** `run_logs` is `{runs: [{name, exit, woke, log_tail}], wake_lines}`: it has NO time field, so a per-step time of `tests:server` or `tests:pg` is not recorded. The report reads one only from a line `step=<tests:server|tests:pg> seconds=<n>` in a land run's log tail; without it the run is counted as `no step data` (never as 0). Follow-up: the land tool prints that line. Land wall time is the record's `end` minus `start` (the pod's). A landing's size and rounds come from the commit's trailer `Land-Checks: files=<n> lines=<n> rounds=<n>` (`rounds` may be in `Land-Council`); a commit without a key counts as unread. A land run is a run named `land` or `land-*`; its exit 0 is landed, 18 council, 13 tests, 3 environment, 124 or 137 time limit, any other other. A pod counts as landed when its record's `head_sha` is a landed commit of the window (a squashed landing with another sha shows as unlanded: known limit). A lane is the worker name without its trailing `-<digits>`. Windows are by the record's `end`. Median of an even count is the mean of the middle two; p90 is the nearest rank; a lane with fewer than 3 landings prints `n/a`.

Settings (the settings file; all optional): `CW_LAND_REPORT_ISSUE` (`owner/repo#N`; unset: nothing is sent, one stderr line), `CW_LAND_REPORT_SMALL_LINES` (the small bound, default 50, named in the report), `CW_LAND_REPORT_GIT_DIR` (a checkout) and `CW_LAND_REPORT_BRANCH` (default `origin/main`); without a checkout the report says `landed commits: not read`.

| Input or outside call | Failure handled | Test | Mutant (control first, anchored text) |
|---|---|---|---|
| run records directory, 24 h window | records of other days are not counted | `S1.test_control` | `S1.test_mutant_counts_every_record` (`S1 lane a`) |
| a record that is corrupt, a link, or 5 MB | skipped and counted `N records not read`; the rest counted | `S7.test_control` | `S7.test_mutant_stops_at_corrupt` (`S7`) |
| a secret-shaped value in a log tail or an outside error | one `scrub` on every output; log lines carry the class name only | `S7.test_scrub`, `S8.test_raising_sender_and_missing_issue` | none: the scrub is pinned by value |
| the land wall times (median, p90, trend) | fewer than 3: `n/a` | `S2.test_control` | `S2.test_mutant_mean` (`S2 numbers`) |
| step times in the log tail | a missing time is `no step data`, not 0 | `S3.test_control` | `S3.test_mutant_missing_is_zero` (`S3 server`) |
| `git log` of the default branch (one call, 60 s, no shell) | non-zero, a bad branch or no directory: `None`, the report says so | `S8.test_git_reads` | none: the failure is the test |
| trailers `Land-Checks`, `Land-Council` (untrusted text) | only `files`, `lines`, `rounds` as digits are read | `S5.test_trailer_parse` | none |
| the small bound setting | not a whole number 1 to 999999: 50 | `S4.test_small_bound_setting`, `S4.test_control` | `S4.test_mutant_at_least` (`S4 bound`) |
| council rounds per landing | median and max, not a sum | `S5.test_control` | `S5.test_mutant_sum` (`S5 rounds`) |
| pods ended unlanded | only pods with no landed head | `S6.test_control` | `S6.test_mutant_every_pod` (`S6 count`) |
| the schedule and the state file | one per day, none repeated in the hour; a bad state file means due | `S8.test_control` | `S8.test_mutant_no_last_day` (`S8 repeat`) |
| the switchboard `send cos` (60 s, argv list) | non-zero or an exception: the part stays unsent and is retried alone | `S8.test_failed_part_is_retried_alone`, `S8.test_sender_calls_are_argv_lists` | none |
| `gh issue comment` (60 s, argv list; issue checked against `owner/repo#N`) | as above; a bad issue is refused | `S8.test_comment_alone_retried`, `S8.test_sender_calls_are_argv_lists` | none |

A crash between a send and the state write can send that part twice (the state is written right after each part). Tests: `cloud/launcher/tests/test_land_report.py`. S9 (the hand check against the real records) is a tester's.
