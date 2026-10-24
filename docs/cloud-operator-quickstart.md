# Cloud worker quickstart (operator)

How to prepare and run ONE cloud worker with `fleet-switchboard launch --cloud`. The facts are in
[cloud-worker-interface.md](cloud-worker-interface.md) and [../cloud/README.md](../cloud/README.md); this page is the short path.
Names below are examples (`acme-org/widget-service`, `/home/user`).

## 1. Settings

The preferred place for every setting is ONE private file, `~/.config/fleet-switchboard/cloud.env` (it sits beside the
switchboard's own `config.json`; with `XDG_CONFIG_HOME` set, under that). `launch --cloud`, `send`, `read`, `retire` and the `cloud-worker`
driver all read it through the same function. The old way, `CW_*` variables set in the shell that runs `launch`, still works **when there is no file**.

How the two sources behave:

- When the file exists it is the only source: the environment is not read for any setting in the table below.
- A setting that the file and the environment BOTH set to different values refuses the launch, naming the setting and both sources (never a value). Unset one.
- With no file, nothing changes: the environment is read exactly as before.
- `launch` prints once which source it used: the file's path, or `environment`.
- The path has no environment override. A different path is a test hook inside the test suite only.

What the file must be (anything else refuses, naming the file and the problem, before any cluster or vault call):

- a regular file (not a symlink), owned by you, mode `0600`: any group or other permission bit refuses (so `0640`, `0604`, `0644` and `0666` do);
- in a directory owned by you that is not group- or world-writable;
- plain `KEY=VALUE` lines: `#` starts a comment line, blank lines are skipped, the value is the rest of the line as written. No quotes, no `$VAR` or `$(...)`
  expansion, and nothing is run when the file is read. A quoted value, an unknown key, a repeated key or a malformed line refuses;
- **every value is checked against its own rule** (the table below; a setting with no rule is refused), and a value with a control character (a NUL, a tab inside a value, a carriage return, so a CRLF file too) is refused naming the line;
- **no secret**: a value shaped like one (a token prefix such as `ghp_`, `github_pat_`, `sk-`, `AKIA`, `Bearer `; a JWT; a PEM header; a URL with a password;
  a 40-character hex string inside a command) refuses, naming the key and never printing the value. A command that prints a secret is fine; the secret itself is not. The shape check is a guardrail, not a boundary: other secret formats are not recognised, so keep secrets out of the file yourself;
- a file with more than one hard link refuses. Settings are applied all at once: a refused file leaves the environment untouched.

Known limits (not checked): only the file's own directory is checked for owner and write bits, so a group-writable grandparent such as `~/.config` at `0770`, and a symlink in the directory path (it is followed), are not caught; and with `CW_CLUSTER_TOKEN_CMD` set the two vault settings are still required, validated and then unused.

Create it so that nobody else can ever read it (the template is below; fill in your own values):

```sh
umask 077
mkdir -p ~/.config/fleet-switchboard
${EDITOR:-vi} ~/.config/fleet-switchboard/cloud.env
ls -l ~/.config/fleet-switchboard/cloud.env   # -rw-------
```

Each setting's name, whether the file requires it, and its kind (name, https URL, path, command, digest-ref, number, secret-name, duration):

| Setting | Required | Kind | What it is |
|---|---|---|---|
| `CW_ALLOWED_REPOS` | yes | name | comma-separated `OWNER/NAME` the launcher may use, e.g. `acme-org/widget-service`; no wildcard. Unset or empty refuses everything. |
| `CW_CLOUD_IMAGE` | yes | digest-ref | the worker image, by digest: `name@sha256:<64 hex>`; a tag refuses. (`--image` on the command line stays as it was.) |
| `CW_CLOUD_SERVER` | yes | https URL | an https URL whose host is a DNS name (`https://NAME[:PORT]`), where NAME is a DNS name with at least two labels (an IP address of any form, including `[::1]`, `127.1` and `0x7f000001`, is refused: a cluster reached by IP needs a DNS name), a last label that has a letter and is not all digits or `0x`, and a port from 1 to 65535; no user, query, fragment, path, `file:` or `http:` |
| `CW_VAULT_SUBSCRIPTION` | yes | name | the vault's subscription, a GUID |
| `CW_VAULT_NAME` | yes | name | the vault's name: 3 to 24 letters, digits and hyphens, starting with a letter |
| `CW_CLOUD_NAMESPACE` | yes | name | the namespace the workers live in: a DNS label; `default` and `kube-*` are refused |
| `CW_TOOLING_REPO` | no | name | the tooling's own repository: refused even when it is on the list |
| `CW_CLOUD_CA_FILE` | no | path | absolute path to the cluster's CA certificate |
| `CW_PULL_SECRET` | no | name | name of the image pull secret in the namespace (the name, never its content) |
| `CW_KUBECTL` | no | path | the `kubectl` program (default: `kubectl` on `PATH`) |
| `CW_MODEL_AUTH_FILE` | no | path | the OpenCode auth file; only its `github-copilot` entry travels to the worker |
| `CW_RUN_RECORD_DIR` | no | path | where the run record is written when a worker stops (default: `runs/` beside the audit file) |
| `CW_CLOUD_SPEND_CAP_USD` | no | number | spend cap in USD for one create (default 25; an invalid value refuses) |
| `CW_CLUSTER_TOKEN_CMD` | no | command | a command that prints a short-lived cluster token (instead of the vault route) |
| `CW_CLUSTER_ADMIN_CMD` | no | command | a command that prints the cluster administrator kubeconfig (instead of the vault read) |
| `CW_CLUSTER_ADMIN_SECRET` | no | secret-name | the name of the vault secret that holds the cluster administrator kubeconfig (default `cw-cluster-admin-kubeconfig`): 1 to 127 letters, digits and hyphens; the name, never its content |
| `CW_CLUSTER_TOKEN_DURATION` | no | duration | the cluster token's requested life: 1 to 3 digits and `m` or `h`, from `1m` to `60m` (default `20m`; `1h` is the same as `60m`) |
| `CW_GIT_TOKEN_CMD` | no | command | a command that prints the one-hour git token; default is the GitHub App mint beside the launcher (`app_token.py`) |
| `CW_APP_ID_CMD`, `CW_APP_KEY_CMD` | no | command | commands that print the GitHub App's id and key instead of the vault read (`app_token.py`) |
| `CW_APP_INSTALLATION_CMD` | no | command | obsolete: accepted and ignored, never run. The installation is looked up per repository (see the next paragraph); the vault no longer needs `cw-app-installation-id` |
| `CW_VAULT_CHECK_DIR` | no (needed once the vault refuses this machine) | path | the checkout of the vault access check, an absolute path (see section 2b) |

How the installation is found: the mint signs a short-lived App token and asks GitHub which installation covers the repository (`GET /repos/OWNER/REPO/installation`), then mints the one-repository token for that installation. The answer must be one installation whose account is the repository's owner. A repository the App is not installed on is refused before any pod is created, with a message that says so. To cover a new repository, install the App on the account that owns it (an App installed on two accounts serves both) and add the repository to the installation; nothing in the vault changes.

Names the code reads that are deliberately not settings (set by the launcher itself, the other login kind, tuning knobs with safe defaults, test hooks) are listed, each with its reason, in `cloud/launcher/tests/test_cloud_settings.py` (`NOT_SETTINGS`); a test fails when the code reads a `CW_*` name that is neither a row here nor on that list.

The template (neutral placeholders; copy, edit, keep only what you need, and set the file to `0600`):

```text
# cloud.env: the cloud launcher's settings. No quotes, no secrets, one KEY=VALUE per line.
# the repositories a worker may be made for (comma-separated OWNER/NAME)
CW_ALLOWED_REPOS=acme-org/widget-service
# the worker image, by digest
CW_CLOUD_IMAGE=registry.example.internal/acme/worker@sha256:0000000000000000000000000000000000000000000000000000000000000000
# the cluster: an https URL whose host is a DNS name (https://NAME[:PORT])
CW_CLOUD_SERVER=https://cluster.example.internal:6443
# the vault that holds the cluster login
CW_VAULT_SUBSCRIPTION=00000000-0000-0000-0000-000000000000
CW_VAULT_NAME=acme-vault
# the namespace the workers live in
CW_CLOUD_NAMESPACE=cw
# optional: the tooling's own repository (refused even when listed)
# CW_TOOLING_REPO=acme-org/tooling
# optional: the cluster CA certificate (absolute path)
# CW_CLOUD_CA_FILE=/home/user/cluster-ca.pem
# optional: the name of the image pull secret
# CW_PULL_SECRET=acme-pull
# optional: the kubectl program
# CW_KUBECTL=/usr/local/bin/kubectl
# optional: the OpenCode auth file
# CW_MODEL_AUTH_FILE=/home/user/.local/share/opencode/auth.json
# optional: where run records are written
# CW_RUN_RECORD_DIR=/home/user/cloud-runs
# optional: the spend cap in USD for one create
# CW_CLOUD_SPEND_CAP_USD=25
# optional commands that print a token (the command, never the token)
# CW_CLUSTER_TOKEN_CMD=/home/user/bin/print-cluster-token
# CW_CLUSTER_ADMIN_CMD=/home/user/bin/print-cluster-kubeconfig
# optional: the vault secret that holds the cluster administrator kubeconfig (the name), and the token's life
# CW_CLUSTER_ADMIN_SECRET=acme-cluster-admin
# CW_CLUSTER_TOKEN_DURATION=20m
# CW_GIT_TOKEN_CMD=/home/user/bin/print-git-token
# CW_APP_ID_CMD=/home/user/bin/print-app-id
# CW_APP_KEY_CMD=/home/user/bin/print-app-key
# optional, but needed when the vault refuses this machine: the vault access check's checkout
# CW_VAULT_CHECK_DIR=/home/user/vault-check
```

Before reading the vault, do whatever your deployment requires (for example a sign-in or an access request). This repository does not
do it for you.

## 1b. Where the launcher is found

Nothing needs to be on your `PATH`. `launch --cloud` (and `send`, `read`, `retire`) look for the launcher program in this order and use the first that fits:

1. `FLEET_CLOUD_WORKER_BIN`, when set: the program it names, unchanged. It is an environment variable only, not a key of the settings file.
2. The launcher of the switchboard's **own checkout**: `cloud/launcher/cloud-worker`, found relative to the switchboard's own `bin/cloud_worker.py`. It is used only when it is a regular executable file (the kit ships it as a link to the sibling `cloud_worker.py`, which is then the file judged) owned by you and not group- or world-writable, and every parent directory up to `/` passes the same walk as the vault access check (no symlink, owned by root or you, not group- or world-writable unless sticky).
   "Own checkout" means the checkout that holds the running `bin/cloud_worker.py`, never the working directory. A lane that runs the switchboard from a worktree of a branch under review therefore uses THAT branch's launcher: it is the same checkout as the running script, so the trust root is unchanged.
3. `cloud-worker` on the `PATH`, as before.
4. Otherwise `launch` refuses (exit status 2, before any vault, cluster or launcher call), naming `FLEET_CLOUD_WORKER_BIN` and the checkout path it looked in.

Limit: the checks judge the launcher file once, just before it runs. A process running as the same user can still replace the file in that window, or edit the switchboard itself: the checks guard against OTHER users, not against yourself.

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
- `LANG`/`LC_*` and the token route's settings (`CW_VAULT_SUBSCRIPTION`, `CW_VAULT_NAME`, `CW_APP_ID_CMD`, `CW_APP_KEY_CMD`, and the obsolete `CW_APP_INSTALLATION_CMD`, which is passed through and ignored), and `CW_OPENSSL` only when it is an absolute executable file.

`CW_GITHUB_API_BASE`, `CW_PREFLIGHT_TOKEN_CMD`, `CW_GIT_TOKEN_CMD`, proxy variables, `GH_`/`GITHUB_` token variables, `HOME` and `PATH` set in your shell are ignored by `launch --cloud` and `run-task`: nothing in the environment can point the check at another host, and your `HOME`, `PATH` and `PYTHON*` cannot run code inside it. The token route's own settings (`CW_APP_*_CMD`, `CW_OPENSSL`, `AZURE_CONFIG_DIR`) are trusted operator configuration: they are run or read as given.

## 2b. The vault access check

The vault only answers machines on its firewall's list. When a vault read is **refused for that reason** (and only then), `launch --cloud`
runs the vault access check once and reads the vault once more. Any other vault error (no permission, a missing secret, not signed in,
unreachable) is not retried and runs nothing. The check belongs to its owner and is run unchanged: it adds at most one single-address rule,
only this machine's own address, and never removes or widens a rule. `launch` gives it no address and no option.

Set `CW_VAULT_CHECK_DIR` to the checkout the owner of the vault access check provides. Because `python3 -m` loads anything under the checkout,
**every entry** of it is judged without following links: each directory and file (every `__init__.py` too) must be owned by you, not
group- or world-writable and not a symlink (the top-level `.git` directory is not walked). The module must be a regular file, and
**every parent** of the checkout up to `/` must be a real directory owned by root or you and not group- or world-writable (a sticky one is fine). The lock file's parents get the same check.
A missing key or a bad path refuses the launch (exit status 2), names `CW_VAULT_CHECK_DIR` and runs nothing; an unset key
does not matter while the vault already answers. The check runs from that checkout as you, with a cleaned environment (a fixed `PATH`, no `PYTHON*` and none of your variables, your `az` sign-in found as for the preflight) and
a limit of 300 seconds. Its `HOME` is your real home taken from the password database, never from the `HOME` variable, so the check's own log line goes under your home as its owner expects (ask its owner for the exact file); `TMPDIR` is a private directory removed afterwards.

Two launches never run it at once: one lock file (mode 0600, in the cloud state directory) is held around the check; a second launch waits,
then reads the vault again first, and runs the check only if the vault still refuses.

What `launch` does with the check's exit code (every run writes one audit line, `cloud.vault_check`, with the outcome and the code, never an address):

| Exit | Meaning | Launch |
|---|---|---|
| 0 | the vault answered; nothing was changed | reads again and continues |
| 10 | an address was added and the vault now answers | continues, and **prints the check's count line once** (`rules A -> B, date`): forward it to whoever records it; `launch` cannot message a lane |
| 20 | refused by the check's own rules (its cap of 6 rules is reached, or an address it will not add) | stops, exit 2: ask the owner of the vault access check; it will not add more, and only it prunes |
| 30 | added, but the vault still refused after 90 seconds | stops, exit 2: ask the owner of the vault access check |
| 40 or any other code, or no code (timed out) | the check failed | stops, exit 2: ask the owner of the vault access check |

Nothing is created when it stops. The check's own output is never printed.

## 3. Run one worker

```text
fleet-switchboard launch coder7 --cloud --agent coder --repo acme-org/widget-service --branch main \
  --role coder --reports-to orch --brief-file /home/user/brief.txt
fleet-switchboard send coder7 "one more instruction"
fleet-switchboard read coder7
fleet-switchboard retire coder7
```

`retire` stops the worker: the end hook pushes its work and the pod and its Secrets are deleted.

### A worker with a nested Docker stack

Add `--stack` (and, if you want, `--shape S|M`) to the same command for a worker that runs a Docker Compose stack inside its pod:

```text
fleet-switchboard launch coder7 --cloud --stack --agent coder --repo acme-org/widget-service --branch main \
  --role coder --reports-to orch --brief-file /home/user/brief.txt
```

- `--stack` is handed to the launcher's `create` and only then: a launch without it builds exactly the pod it always did.
- The pod runs on the Sysbox runtime class (`sysbox-runc`) in its own user namespace, as a non-root user. No capability is added, it is never privileged, and it has no host path, Docker socket or service-account token.
- `--shape` is `S` (2 CPU, 4 GiB) or `M` (4 CPU, 8 GiB). Leave it out for the launcher's own default, `S` (4 GiB): the measured stack lane peaked at 0.8 to 1.35 GiB. The memory limit stays; the pod is never unlimited.
- The first pod on a fresh node may wait for the node's runtime to be ready (a short delay, a few minutes at most); the launcher notices that case and creates the pod again, up to 3 attempts. You do not retry it.
- `--stack` needs `--cloud`, and an unknown `--shape` is refused with `--stack`. `--stack` without `--cloud` is refused first of all; an unknown shape is refused right after the allow-list check (step 1 of section 2). Both exit 2 before any token is minted or any cluster call is made, and the order of section 2 is unchanged. `--shape` without `--stack` is passed to the launcher as before.

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

Turn it off: `fleet-switchboard install --uninstall` unloads and removes it (with the switchboard's own service); `--uninstall --no-service` removes the reaper too and leaves the switchboard's own service. A file at that path that the installer did not write is named and left alone. Only one reaper runs at a time: a hand-started `cloud-worker reap --loop` beside the service exits with `not started: another reaper holds the lock`. Path settings (`CW_CLOUD_TOKEN_FILE` and the others) must be absolute paths of plain characters; a pasted token is refused by name.

Its log: `cloud-reaper-stderr.log` in the switchboard state directory (journalctl on Linux). Each time it ends it writes one line
`<UTC time> reaper exiting: WHY`, and the service then restarts it.

When the Mac sleeps, the reaper pauses. Workers keep their pod-side floor (the pod's own limit, the cap plus 300 s, PR #880 once it
merges), so a sleeping Mac delays a stop but cannot leave a worker running past that floor.

### The reaper alone (nothing else on the machine)

`fleet-switchboard install --cloud-reaper-only` installs and loads the reaper's own service and nothing else. It writes the reaper's service file (mode `0600`) and the state directory (the reaper's log), and loads the service; it leaves alone the config, the profile pieces, the command link, the plugin link, the switchboard's own service and the daemon. Use it on a machine that runs the reaper and not the switchboard: a dry run (`--dry-run`) prints what it would do and writes nothing.

Before it writes anything the reaper-only install reads your settings file, `~/.config/fleet-switchboard/cloud.env`, with the launcher's own reader: it is the same settings file a launch needs, and the installer refuses what a launch would refuse. So the file must exist, be a regular file of your own with one hard link (no symlink, no directory), mode `0600`, in a directory that only you can write; hold `CW_ALLOWED_REPOS`, `CW_CLOUD_IMAGE` (an image by digest), `CW_CLOUD_SERVER` (an https URL with a DNS host), `CW_CLOUD_NAMESPACE`, `CW_VAULT_NAME` and `CW_VAULT_SUBSCRIPTION` (a GUID) with valid values (`CW_CLUSTER_ADMIN_SECRET` has a default and is optional); and be plain `KEY=VALUE` lines. A quoted value, an unknown line (an `export ` line, for example) or a repeated key is refused. The token file name `CW_CLOUD_TOKEN_FILE` is not a key that file can hold. The install is ONE command: when `CW_CLOUD_TOKEN_FILE` is not set in the shell that runs it, the install uses a default login file, `~/.local/state/fleet-switchboard/cloud-reaper-token` (under your real home), and prints `cloud reaper: login file <path> (default; set CW_CLOUD_TOKEN_FILE to change it)`. It makes that directory with mode `0700` when it is missing (an existing directory that group or others can reach is refused with a named line, never changed), the reaper itself makes the file `0600` when it first writes it, and the path goes into the service's environment only: the install never edits `cloud.env`. A `CW_CLOUD_TOKEN_FILE` you set must be an absolute path (a relative one is refused) and is carried into the service instead. When anything is wrong it refuses with ONE line that carries the launcher's own wording, for example `cloud reaper not installed: the settings file ~/.config/fleet-switchboard/cloud.env has mode 0644: it must be 0600 (nobody else may read or write it): fix it as a launch needs, then run again`, exits non-zero and writes nothing; it never prints a value. The vault names are not carried into the service: the file is their only source and the reaper reads it at every start. The full install with `--with-cloud-reaper` does not make this check.

Prerequisites: the Mac must be awake (the reaper pauses when it sleeps), and the vault must be reachable from this machine (it is behind a source-address allowlist: from another network the refresh is refused and the reaper keeps its old token until it ends).

The way back is as small as the way in: `fleet-switchboard install --uninstall --cloud-reaper-only` unloads the reaper's job and removes ONLY the reaper's service file (the plist or the unit). It touches nothing else (no plugin, agent or skill piece, no wrapper, no link, no switchboard service, no config, no profile). Run again with nothing installed it prints `cloud reaper: not installed` and succeeds; with `--dry-run` it prints what it would do and does nothing.

Do not use plain `--uninstall` (or `--uninstall --no-service`) for that: on a full install it removes much more than the reaper. It also unlinks the herdr plugin (unless `--no-herdr-link`), and removes the plugin, agent and skill pieces, the wrapper and the command link; only the switchboard's own service is skipped by `--no-service`.

The settings of the service: when your own settings file (`~/.config/fleet-switchboard/cloud.env`) exists, the service carries none of the keys that file holds — the file is then the only source of them, and a key set differently in the file and in the service makes every start of the reaper refuse. Without that file the service copies the non-secret `CW_*` settings of the shell that runs the install, as `--with-cloud-reaper` always did.

## 5. Known gaps

- Stopping a worker by name alone: #682.
- A layer scan of the worker image: #691.
