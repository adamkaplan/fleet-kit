# Cloud worker quickstart (operator)

How to prepare and run ONE cloud worker with `fleet-switchboard launch --cloud`. The facts are in
[cloud-worker-interface.md](cloud-worker-interface.md) and [../cloud/README.md](../cloud/README.md); this page is the short path.
Names below are examples (`acme-org/widget-service`, `/home/user`).

## 1. Settings

Set these in the shell that runs `launch` (never in a file the worker can read):

| Setting | What it is |
|---|---|
| `CW_ALLOWED_REPOS` | comma-separated `OWNER/NAME` the launcher may use, e.g. `acme-org/widget-service`. Unset or empty refuses everything. |
| `CW_TOOLING_REPO` | the tooling's own repository: refused even when it is on the list |
| `CW_CLOUD_IMAGE` | the worker image (or pass `--image`) |
| `CW_CLOUD_SERVER` | the cluster API address, for the service-account login |
| `CW_CLOUD_CA_FILE` | optional path to the cluster's CA certificate |
| `CW_PULL_SECRET` | optional name of the image pull secret in the namespace |
| cluster token | either `CW_CLUSTER_TOKEN_CMD` (a command that prints a short-lived token) or the vault settings `CW_VAULT_SUBSCRIPTION` and `CW_VAULT_NAME` |
| `CW_GIT_TOKEN_CMD` | optional command that prints the one-hour git token; default is the GitHub App mint beside the launcher (`app_token.py`) |
| `CW_MODEL_AUTH_FILE` | the OpenCode auth file; only its `github-copilot` entry travels to the worker |
| `CW_RUN_RECORD_DIR` | where the run record is written when a worker stops (default: `runs/` beside the audit file) |
| `CW_CLOUD_SPEND_CAP_USD` | spend cap in USD for one create (default 25; an invalid value refuses) |

Before reading the vault, do whatever your deployment requires (for example a sign-in or an access request). This repository does not
do it for you.

## 2. Order of checks

`launch --cloud` and the `run-task` driver run the same checks in this order, and each stops the whole launch when it refuses:

1. the allow-list (`CW_ALLOWED_REPOS`, `CW_TOOLING_REPO`);
2. the repository preflight: the target repository's default branch must refuse direct pushes, force-pushes and deletion
   (its one-line result is printed once). A missing, killed or silent preflight program refuses; silence never passes;
3. the git token is minted;
4. the worker is created.

A refusal at step 1 or 2 means no token was minted and nothing was created.

The preflight runs as `python -I -S` (no user site, no `PYTHON*` variable) by the launcher's own absolute interpreter, in a cleaned environment.
The child sees only:

- a fixed `PATH` (`/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin`), never yours;
- an empty private `HOME` and `TMPDIR`, removed when the check ends;
- `AZURE_CONFIG_DIR`: yours when it is an absolute existing directory, else `~/.azure` of your account (read from the password database, not `$HOME`);
- `LANG`/`LC_*` and the token route's settings (`CW_VAULT_SUBSCRIPTION`, `CW_VAULT_NAME`, `CW_APP_ID_CMD`, `CW_APP_INSTALLATION_CMD`, `CW_APP_KEY_CMD`), and `CW_OPENSSL` only when it is an absolute executable file.

`CW_GITHUB_API_BASE`, `CW_PREFLIGHT_TOKEN_CMD`, `CW_GIT_TOKEN_CMD`, proxy variables, `GH_`/`GITHUB_` token variables, `HOME` and `PATH` set in your shell are ignored by `launch --cloud` and `run-task`: nothing in the environment can point the check at another host, and your `HOME`, `PATH` and `PYTHON*` cannot run code inside it. The token route's own settings (`CW_APP_*_CMD`, `CW_OPENSSL`, `AZURE_CONFIG_DIR`) are trusted operator configuration: they are run or read as given.

## 3. Run one worker

```text
fleet-switchboard launch coder7 --cloud --agent coder --repo acme-org/widget-service --branch main \
  --role coder --reports-to orch --brief-file /home/user/brief.txt
fleet-switchboard send coder7 "one more instruction"
fleet-switchboard read coder7
fleet-switchboard retire coder7
```

`retire` stops the worker: the end hook pushes its work and the pod and its Secrets are deleted.

## 4. What a worker can and cannot do

- It works on one repository and one branch with a one-hour git token and the model entry it was given, nothing else.
- Protection on the default branch (pull request or merge queue, no force-push, no deletion) is what stops it merging or rewriting
  history; the preflight only checks that this protection exists.
- It holds no cluster credential and no other model provider entry.
- It ends at its idle or hard deadline whether or not anyone retires it.

## 4b. The reaper runs by itself (opt-in)

Turn it on: set the cluster settings in your shell (`CW_KUBE_CONTEXT`, or `CW_CLOUD_SERVER` and `CW_CLOUD_TOKEN_FILE`, plus
`CW_CLOUD_NAMESPACE`), then run `fleet-switchboard install --with-cloud-reaper`. It writes a LaunchAgent (macOS) or a systemd
user unit (Linux), loads it, and starts it at login. Nothing secret is written: only names and file paths.

Turn it off: `fleet-switchboard install --uninstall` unloads and removes it (with the switchboard's own service).

Its log: `cloud-reaper-stderr.log` in the switchboard state directory (journalctl on Linux). Each time it ends it writes one line
`<UTC time> reaper exiting: WHY`, and the service then restarts it.

When the Mac sleeps, the reaper pauses. Workers keep their pod-side floor (the pod's own limit, the cap plus 300 s, PR #880 once it
merges), so a sleeping Mac delays a stop but cannot leave a worker running past that floor.

## 5. Known gaps

- Stopping a worker by name alone: #682.
- A layer scan of the worker image: #691.
