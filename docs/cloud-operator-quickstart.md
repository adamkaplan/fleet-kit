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

Each setting's name, whether the file requires it, and its kind (name, https URL, path, command, digest-ref, number, secret-name, duration, registry-host, guid):

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
| `CW_OPENCODE_BIN` | no | path | the `opencode` program the launcher runs as `opencode auth export` (default: `opencode` on `PATH`); only the `github-copilot` entry of its output travels to the worker. The old `CW_MODEL_AUTH_FILE` is retired: a settings file that still has it is refused as an unknown setting |
| `CW_RUN_RECORD_DIR` | no | path | where the run record is written when a worker stops (default: `runs/` beside the audit file) |
| `CW_CLOUD_SPEND_CAP_USD` | no | number | spend cap in USD for one create (default 25; an invalid value refuses) |
| `CW_CLUSTER_TOKEN_CMD` | no | command | a command that prints a short-lived cluster token (instead of the vault route) |
| `CW_CLUSTER_ADMIN_CMD` | no | command | a command that prints the cluster administrator kubeconfig (instead of the vault read) |
| `CW_CLUSTER_ADMIN_SECRET` | no | secret-name | the name of the vault secret that holds the cluster administrator kubeconfig (default `cw-cluster-admin-kubeconfig`): 1 to 127 letters, digits and hyphens; the name, never its content |
| `CW_STACK_MODEL_SECRET` | no | secret-name | with `launch --cloud --stack` only: the name of the vault secret (same vault as the others) that holds the stack-only model credential: 1 to 127 letters, digits and hyphens; the name, never its value. Ignored without `--stack`. See "A stack-only model credential" |
| `CW_CLUSTER_TOKEN_DURATION` | no | duration | the cluster token's requested life: 1 to 3 digits and `m` or `h`, from `1m` to `60m` (default `20m`; `1h` is the same as `60m`) |
| `CW_GIT_TOKEN_CMD` | no | command | a command that prints the one-hour git token; default is the GitHub App mint beside the launcher (`app_token.py`) |
| `CW_APP_ID_CMD`, `CW_APP_KEY_CMD` | no | command | commands that print the GitHub App's id and key instead of the vault read (`app_token.py`) |
| `CW_APP_INSTALLATION_CMD` | no | command | obsolete: accepted and ignored, never run. The installation is looked up per repository (see the next paragraph); the vault no longer needs `cw-app-installation-id` |
| `CW_STACK_REGISTRY` | no | registry-host | the stack registry (lower case, ending `.azurecr.io`; no scheme, path, port or IP); with the next two, a stack pod pulls prebuilt images (see `cloud/README.md`); all three or none |
| `CW_STACK_PULL_CLIENT_ID`, `CW_STACK_PULL_TENANT_ID` | no | guid | the pull identity's client id and tenant id (GUIDs, not secrets); set together with `CW_STACK_REGISTRY` |
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
# optional: the opencode program (default: opencode on PATH)
# CW_OPENCODE_BIN=/usr/local/bin/opencode
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

### The light shape (`XS`) and the other shapes

A worker started with no `--shape` and no `--stack` gets `XS` (1 CPU, 2 GiB), the light shape and the default (requests equal limits, no nested Docker). Seven of them are meant to fit on one 8-CPU node (a live check reads the node's requested CPU back; if the system pods ask for more, the number is six and this section says so). `S` (2 CPU, 4 GiB) and `M` (4 CPU, 8 GiB) are chosen only by name: `--shape S`. A worker gets `XS` and an orchestrator (`--agent orchestrator`) gets `S` when `--shape` is not given.

- Belongs in `XS`: a reader (code reading and review), a planner, a tester running short tests, a small edit and commit.
- Does not belong in `XS`: a nested Docker stack (`--stack` refuses `XS`), an image build, a long full test suite or a build with a large memory peak: name `--shape S` or `M` for those.
- A pod killed for memory is fixed afterwards: raise the shape for that work with `--shape S`.

### A worker with a nested Docker stack

Add `--stack` (and, if you want, `--shape S|M`; XS cannot run a stack) to the same command for a worker that runs a Docker Compose stack inside its pod:

```text
fleet-switchboard launch coder7 --cloud --stack --agent coder --repo acme-org/widget-service --branch main \
  --role coder --reports-to orch --brief-file /home/user/brief.txt
```

- `--stack` is handed to the launcher's `create` and only then: a launch without it builds exactly the pod it always did.
- The pod runs on the Sysbox runtime class (`sysbox-runc`) in its own user namespace, as a non-root user. No capability is added, it is never privileged, and it has no host path, Docker socket or service-account token.
- With `--stack`, `--shape` is `S` (2 CPU, 4 GiB) or `M` (4 CPU, 8 GiB). Leave it out for `S` (4 GiB): the measured stack lane peaked at 0.8 to 1.35 GiB. The memory limit stays; the pod is never unlimited.
- The first pod on a fresh node may wait for the node's runtime to be ready (a short delay, a few minutes at most); the launcher notices that case and creates the pod again after a pause of 15 seconds (`STACK_RACE_PAUSE_SECONDS`), for as long as a retry fits in a window of 600 seconds (`STACK_RACE_WINDOW_SECONDS`); a fresh node can take 3 to 5 minutes. If the window ends, the launcher says so once (the attempts and the seconds spent) and leaves no pod. You do not retry it.
- `--stack` needs `--cloud`, and an unknown `--shape`, and `XS`, are refused with `--stack`. `--stack` without `--cloud` is refused first of all; an unknown shape is refused right after the allow-list check (step 1 of section 2). Both exit 2 before any token is minted or any cluster call is made, and the order of section 2 is unchanged. `--shape` without `--stack` is passed to the launcher as before.

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

### The reaper is unattended: it keeps its own cluster login fresh

The reaper needs a cluster login. In the token-file login (the one the service file names: `CW_CLOUD_TOKEN_FILE`
for the token file, `CW_CLOUD_SERVER` for the cluster and `CW_CLOUD_NAMESPACE` for the namespace), the token in
that file lives at most sixty minutes. The reaper therefore refreshes it itself, at the start of every pass: it
reads the cluster administrator kubeconfig from the vault (`CW_VAULT_SUBSCRIPTION` and `CW_VAULT_NAME` for the
vault, `CW_CLUSTER_ADMIN_SECRET` for the name of the secret inside it), hands that value to one
`kubectl create token cw-operator` command over a pipe, and writes the token it gets back to the token file with
mode `0600`, by an atomic replace. The old file stays whole if anything fails.

The unattended reaper uses a fixed route: it ignores `CW_KUBECTL`, `CW_CLUSTER_ADMIN_CMD` and `CW_CLUSTER_TOKEN_CMD`, and runs `az` and
`kubectl` only from fixed absolute paths (a custom command belongs to the launch path). When the vault holds the minter kubeconfig, that
token is long-lived (not cluster admin, but whoever reads it can mint `cw-operator` tokens until its Secret is deleted): see the launcher
README for who holds it and how to rotate it.

What this means for you: **the reaper runs unattended** for days, and no one has to refresh the token file. The
administrator value is never in a file, never in an argument and never in the service file (the service file
holds names and paths only, as before). The token that lands in the file holds the operator account's own rights
in the workers namespace and nothing more; the administrator login is used for that one command and dropped.

**The manual install comment on the installer ask is superseded**: it recorded that a person had to refresh the
token file by hand. The reaper does that now, by itself.

If a refresh fails (the vault, the login or the token request), the old file stays as it is, one audit line
names the reason (`refresh-cluster-token`), the pass report names the failure, and the reaper tries again five
minutes later. A vault that refuses this machine is named as such; the reaper never runs the vault access check
(that check belongs to the launch path alone).

When the Mac sleeps, the reaper pauses. Workers keep their pod-side floor (the pod's own limit, the cap plus 300 s, PR #880 once it
merges), so a sleeping Mac delays a stop but cannot leave a worker running past that floor.

### The reaper alone (nothing else on the machine)

`fleet-switchboard install --cloud-reaper-only` installs and loads the reaper's own service and nothing else. It writes the reaper's service file (mode `0600`) and the state directory (the reaper's log), and loads the service; it leaves alone the config, the profile pieces, the command link, the plugin link, the switchboard's own service and the daemon. Use it on a machine that runs the reaper and not the switchboard: a dry run (`--dry-run`) prints what it would do and writes nothing.

Before it writes anything the reaper-only install reads your settings file, `~/.config/fleet-switchboard/cloud.env`, with the launcher's own reader: it is the same settings file a launch needs, and the installer refuses what a launch would refuse. So the file must exist, be a regular file of your own with one hard link (no symlink, no directory), mode `0600`, in a directory that only you can write; hold `CW_ALLOWED_REPOS`, `CW_CLOUD_IMAGE` (an image by digest), `CW_CLOUD_SERVER` (an https URL with a DNS host), `CW_CLOUD_NAMESPACE`, `CW_VAULT_NAME` and `CW_VAULT_SUBSCRIPTION` (a GUID) with valid values (`CW_CLUSTER_ADMIN_SECRET` has a default and is optional); and be plain `KEY=VALUE` lines. A quoted value, an unknown line (an `export ` line, for example) or a repeated key is refused. The token file name `CW_CLOUD_TOKEN_FILE` is not a key that file can hold. The install is ONE command: when `CW_CLOUD_TOKEN_FILE` is not set in the shell that runs it, the install uses a default login file, `~/.local/state/fleet-switchboard/cloud-reaper-token` (under your real home; it does not follow `XDG_STATE_HOME`: set `CW_CLOUD_TOKEN_FILE` to put the file elsewhere), and prints `cloud reaper: login file <path> (default; set CW_CLOUD_TOKEN_FILE to change it)`. It makes that directory with mode `0700` when it is missing (an existing directory that group or others can reach is refused with a named line, never changed), the reaper itself makes the file `0600` when it first writes it, and the path goes into the service's environment only: the install never edits `cloud.env`. A `CW_CLOUD_TOKEN_FILE` you set must be an absolute path (a relative one is refused) and is carried into the service instead. When anything is wrong it refuses with ONE line that carries the launcher's own wording, for example `cloud reaper not installed: the settings file ~/.config/fleet-switchboard/cloud.env has mode 0644: it must be 0600 (nobody else may read or write it): fix it as a launch needs, then run again`, exits non-zero and writes nothing; it never prints a value. The vault names are not carried into the service: the file is their only source and the reaper reads it at every start. The full install with `--with-cloud-reaper` does not make this check.

Prerequisites: the Mac must be awake (the reaper pauses when it sleeps), and the vault must be reachable from this machine (it is behind a source-address allowlist: from another network the refresh is refused and the reaper keeps its old token until it ends).

The way back is as small as the way in: `fleet-switchboard install --uninstall --cloud-reaper-only` unloads the reaper's job and removes ONLY the reaper's service file (the plist or the unit). It leaves the login file and its directory in place (a leftover token file once the reaper has run): the uninstall prints the `rm` and `rmdir` lines that remove them, and a login file you set with `CW_CLOUD_TOKEN_FILE` is yours to remove. It touches nothing else (no plugin, agent or skill piece, no wrapper, no link, no switchboard service, no config, no profile). Run again with nothing installed it prints `cloud reaper: not installed` and succeeds; with `--dry-run` it prints what it would do and does nothing.

Do not use plain `--uninstall` (or `--uninstall --no-service`) for that: on a full install it removes much more than the reaper. It also unlinks the herdr plugin (unless `--no-herdr-link`), and removes the plugin, agent and skill pieces, the wrapper and the command link; only the switchboard's own service is skipped by `--no-service`.

The settings of the service: when your own settings file (`~/.config/fleet-switchboard/cloud.env`) exists, the service carries none of the keys that file holds — the file is then the only source of them, and a key set differently in the file and in the service makes every start of the reaper refuse. Without that file the service copies the non-secret `CW_*` settings of the shell that runs the install, as `--with-cloud-reaper` always did.

## 4c. The cloud hub (cloud/k8s/cloud-hub.yaml)

Cause: the hub (the channel server) exists as code, but nothing started it in the cluster, and the launcher's per-pod registration writes to a registry Secret that did not exist. One file now defines, in the `cw` namespace only: Deployment `cloud-hub` (one replica), Service `cloud-hub` (ClusterIP, 8443), the headless Service `cw-agents` (selector `app=cw-worker`, port 4096, publishes not-ready addresses: pods register a name under this Service, `http://<pod name>.cw-agents.cw.svc.cluster.local:4096`, so the hub can reach them) and Secrets `cloud-hub-registry` (keys `secrets.json` and `agents.json`, each `{}`, mounted read-only at `/etc/hub`) and `cloud-hub-pw` (empty: the launcher adds one key per agent name later; mounted read-only at `/etc/hub/pw`, so an agent's `password_file` is `/etc/hub/pw/<name>`; nested under the Secret mount: this works on the cluster's runtime; if a runtime refuses it use `/etc/hub-pw` and `/etc/hub-pw/<name>`). The container command is `python3 /usr/local/bin/cloud_hub.py` (`cloud/worker/Dockerfile` copies the hub and its modules to `/usr/local/bin`; the manifest, these docs and the test use that real path). The environment is exactly `FLEET_CLOUD_CHANNEL_LISTEN`, `FLEET_CLOUD_HUB_AGENTS`, `FLEET_CLOUD_HUB_SECRETS` (its presence selects the secrets mode), `FLEET_CLOUD_HUB_DECISIONS` and `FLEET_CLOUD_AGENT_NAMESPACE` (value `cw`, the namespace of agent accounts; required, the hub stops at start without it). The container runs non-root (uid 1000, fsGroup 1000, seccomp RuntimeDefault, CPU limit 500m) with a read-only root filesystem; Secret volumes use defaultMode 0440 (JSON form 288: not world readable; 0400 would leave root-owned files unreadable to uid 1000, so group read plus fsGroup); only `/var/hub` and `/tmp` (emptyDirs) are writable. Every object carries the label `app.kubernetes.io/part-of=cloud-hub`. The names live in `cloud/launcher/hub_registry.py` and a test compares them with the manifest.

Before you apply: replace the placeholder `IMAGE_DIGEST` with the settings' `CW_CLOUD_IMAGE` (a `name@sha256:...` reference; the file holds no real digest). The substituted value must carry `@sha256:` (a digest, never a floating tag). The pod runs only from an image built from the Dockerfile that holds the hub files.

- Order in the file: Secrets, Services, Deployment.
- Network boundary: the network boundary of the hub (which pods may reach it, which hosts it may reach) is enforced by the namespace's network policies, which are written in a separate manifest file under ask #1256 (cloud/k8s/cloud-hub-network.yaml); until that file is merged the policies exist only as applied in the cluster.
- Pull secret: the Deployment pulls its image with the secret `cw-pull`, which must exist in the namespace (the launcher's `CW_PULL_SECRET`).
- Check (server dry run, first install): `kubectl create --dry-run=server -f cloud/k8s/cloud-hub.yaml`. Expected result lines: five lines ending `created (server dry run)`.
- First install: `kubectl create -f cloud/k8s/cloud-hub.yaml`. Create refuses to overwrite an existing object: a second run fails with AlreadyExists on the Secrets, so it cannot reset the registry.
- Verify: `kubectl -n cw get deployment,service,secret -l app.kubernetes.io/part-of=cloud-hub` shows the five objects.
- Changing the hub later: applying the whole file again resets the registry Secret to empty objects and deregisters every live agent until the launcher registers them again. So never re-apply the whole file; patch in place. For the image: `kubectl -n cw set image deployment/cloud-hub hub=<registry>/<repository>@sha256:<digest>` (the container is named `hub`). For other fields: `kubectl -n cw patch deployment cloud-hub ...`. To apply the Deployment and Services only, apply a copy of the file with the two documents of kind Secret removed.
- Rolling out the hub namespace setting: the hub refuses to start without `FLEET_CLOUD_AGENT_NAMESPACE`, and the old hub image ignores that variable, so set the variable FIRST, then change the image. Both are in-place patches, never a manifest re-apply (a re-apply resets the registry Secret and deregisters live agents). The operator of the hub (the cloud-hub lane) runs them, together with the image rebuild:

  ```sh
  kubectl -n cw get deployment cloud-hub -o jsonpath='{.spec.template.spec.containers[?(@.name=="hub")].image}'   # record the previous image
  kubectl -n cw set env deployment/cloud-hub FLEET_CLOUD_AGENT_NAMESPACE=cw   # equals the namespace the hub runs in
  kubectl -n cw rollout status deployment/cloud-hub
  kubectl -n cw set image deployment/cloud-hub hub=<registry>/cw/worker@sha256:<digest>
  kubectl -n cw rollout status deployment/cloud-hub
  ```

  Check: the hub pod is Ready, a token review of a real account in the hub's namespace is accepted, and an account of another namespace is refused (how the review works: `docs/cloud-channel.md`, the TokenReview mode and the namespace setting). Rollback: `kubectl -n cw set image deployment/cloud-hub hub=<previous image>`; the environment entry may stay (the old image ignores it).
- The empty `cloud-hub-pw` Secret: a later apply may leave the launcher-added keys alone but does not guarantee it: do not re-apply.
- Delay: kubelet refreshes mounted Secret contents within about a minute, so a newly launched agent can need up to ~60 s before it can message through the hub.
- Smoke check: the hub has no unauthenticated health path (`GET /decisions` needs a token), so readiness is a TCP check on 8443. From a pod in `cw`, `nc -z cloud-hub 8443` must succeed.
- Rollback that keeps the Secrets: `kubectl -n cw delete deployment,service -l app.kubernetes.io/part-of=cloud-hub`. Do not add `secret` to that list: `kubectl delete deployment,service,secret -l <label>` also deletes BOTH Secrets (the registry and the per-agent passwords), and running agents cannot message after such a rollback until they are relaunched.

State: the decisions file is on an emptyDir at `/var/hub`; it is lost when the pod restarts. Tests (offline, no cluster): `python3 cloud/launcher/tests/test_cloud_hub_manifest.py`.

## 4d. The namespace's network policies

Cause: an independent read of the hub's manifest set found no network policy in the repository, while the cluster carried six applied by hand. None was in a file. They are now in `cloud/k8s/cloud-hub-network.yaml`, exactly as applied (a test fails when a rule is widened). The six policies:

- `allow-egress`: every pod may reach name resolution (port 53) and the public internet on ports 80 and 443, with the private ranges excluded. It selects EVERY pod of the namespace, so the hub itself may also reach the internet on ports 80 and 443 outside the excepted private ranges. No cluster or pod CIDR is excepted: acceptable only because the cluster's ranges lie inside the private ranges that are excepted (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10).
- `cloud-agents-ingress-from-hub`: worker pods accept the hub on port 4096 only.
- `cloud-agents-to-hub`: worker pods may send to the hub on port 8443 only.
- `cloud-hub-ingress`: the hub accepts worker pods of the same namespace on port 8443 only.
- `cloud-hub-to-agents`: the hub may send to worker pods on port 4096 only.
- `default-deny`: every pod is denied in both directions unless a policy above allows it.

Apply: run a server dry run first, `kubectl -n cw apply --dry-run=server -f cloud/k8s/cloud-hub-network.yaml` (all six should report unchanged), then apply the whole file in the order of its documents. Apply the allow policies before the default deny; a default deny applied first would cut every pod off. First check afterwards: a worker pod can still resolve names and the hub answers its health path.

Roll back: `kubectl -n cw delete networkpolicy -l app.kubernetes.io/part-of=cloud-hub` removes the four hub policies; `kubectl -n cw delete networkpolicy allow-egress default-deny` removes the two namespace-wide ones. Deleting default-deny without allow-egress, or the reverse, changes the whole namespace's reachability.

## 5. Known gaps

- Stopping a worker by name alone: #682.
- A layer scan of the worker image: #691.

## The GitHub App's permissions

The token the launcher mints asks for, and accepts, exactly `contents: write`, `pull_requests: write`, `issues: read`, `metadata: read`. The worker can read issues and their comments of its own repository and cannot write issues, labels, merges or settings.

The owner step, done only after the change that adds `issues: read` is merged: add `issues: read` to the App's permissions, then accept it on each installation. Until both are done a launch is refused before any pod exists, with a line that names `issues` and this step. See `docs/agent-credentials.md`.

## The model credential

The launcher reads the live model credential from OpenCode's own store with `opencode auth export` (in memory, never a file, never printed) and copies only the one `github-copilot` entry into the per-run Secret. The pod imports it with `opencode auth import --standalone` before its server starts. The legacy `auth.json` is not read: the server ignores it.

What to know before you run it:

- **A worker can read the credential.** For the life of its pod, the worker can read the one-entry array (the access entry and its refresh token) at `/run/cw/model-auth` and the store under `XDG_DATA_HOME`. The worker's shell deny rules are not a guard against that. Whether it could send the value out depends on the pod's network egress, which this change does not restrict.
- **The pod holds a snapshot taken at launch.** Nothing re-reads your store. A credential that is revoked or rotated ends that pod's turns; a refresh token you rotate afterwards is not pushed to a running pod. Launch a new run to pick it up.
- **A stale `CW_MODEL_AUTH_FILE` is retired, and what happens depends on where it is.** In a settings file it is refused as an unknown setting. In the process environment it is silently ignored (the launcher drops it), so the credential that is active in your store is used with no message.
- **Image and launcher must match.** An old image with the new launcher answers on the free model (it never imports the credential). The old launcher with the new image refuses at the import (`worker: the model credential import failed; refusing to start`).

To verify it:

- On the launcher's machine, `opencode auth list` shows the `github-copilot` entry as active.
- On a worker, its server's `/api/provider` lists `github-copilot` (a pod that lists only the built-in `opencode` provider answers on the free fallback model and is rate limited).

The worker image carries `cloud/worker/entrypoint.sh`, so the order is: merge, an operator rebuilds the image and switches the digest in the settings file, then run the check above on one no-op worker.

## A stack-only model credential

Cause: a cloud worker pod holds only the coder's own model entry (the per-run file `model-auth`, copied into the coder's data directory) and no credential for the containers that the stack starts inside the pod's nested Docker. A stack whose agents need model calls therefore has none, and handing the stack the coder's `model-auth` would put the coder's key into the containers the stack starts. The fix is a second, separate credential that exists only for the stack. The coder's own key is never copied into the stack.

How it travels (only with `launch --cloud --stack` and `CW_STACK_MODEL_SECRET` set; a launch without them is exactly as before): the switchboard reads the named vault secret before any cluster call, hands it to the launcher over an inherited pipe as the per-run secret `stack-model-key` (never an argument, an environment variable or a spec field), the kubelet mounts it read-only at `/run/cw/stack-model-key` (0400), and the entrypoint copies it to a 0600 file on the memory-backed tmpfs (`/run/cw-tmp/stack/model-key`). The stack's start step is `cw-stack-up` (a `docker compose` front end): it gives the compose child, and only that, the variable `CW_STACK_MODEL_KEY`. A service reads it by listing `CW_STACK_MODEL_KEY` under `environment:` in its compose file (no value in the file). `cw-stack-up down` (`down` as the first argument) deletes the tmpfs copy; the coder's shell can read this credential file (same user, docker group): the protection is the key's own scope, a spend cap and revocation; the tmpfs ends with the pod, and the launcher deletes the per-run Secret as it does the others. The kubelet's mount of the Secret stays visible until the pod ends. The audit and the run record carry the secret's name and `delivered`, nothing else. A secret that is missing, empty, over 16384 bytes or unreadable ends the launch with exit 2 before any pod, naming the secret's name.

The honest bound: the coder's shell is the same user and is in the docker group, so it can read the tmpfs file, read the services' environment with `docker inspect` and `docker exec` into a service. The coder CAN read this credential. The protection is not secrecy from the coder: it is that the credential is for one purpose, has a spend cap, can be revoked, is kept out of everything that persists or is shared, and differs from the coder's own key. A repository that wants a narrower exposure may mount the same file with compose `secrets:` (the path is the tmpfs file above); that is an option for the repository, not built here.

The owner's steps (the launcher never creates or changes a credential):

### Step 1: create a dedicated credential

At the model provider, create a credential for stacks only.

### Step 2: give it the narrowest scope

Allow only the model calls the stack makes.

### Step 3: set a spend cap

Set the spend cap at the provider.

### Step 4: store it in the vault

Store it in the cloud vault under a name of your choice (the suggested name is `cw-stack-model-key`) and set `CW_STACK_MODEL_SECRET` to that name in the settings file. To copy an existing key from another vault once, use a pipe so the value is never printed, never a file and never an argument (the value is never in `--value`):

```text
az keyvault secret show --subscription <src-sub> --vault-name <src-vault> --name <src-secret> --query value -o tsv \
  | az keyvault secret set --subscription <cloud-sub> --vault-name <cloud-vault> --name cw-stack-model-key --file /dev/stdin
```

The copy is not run or tested by the launcher; whether the installed `az` accepts `--file /dev/stdin` is checked once in the live run. It makes a second copy of the key; revoking at the provider revokes both.

### Step 5: rotate or revoke at the provider

Revoke or rotate at the provider. The next stack launch then fails at the provider, not in the launcher.

## Briefing a cloud operator

Do not assemble an operator's brief by hand from older ones: a pinned checkout hash goes stale within a day, and a rule said to be "in an earlier brief" is not in the brief the operator reads. Start every operator brief from the standard header, which the switchboard prints from one source file (`skills/fleet-coordination/operator-brief-header.txt`):

```
fleet-switchboard brief header --role operator
```

It names the cluster admin route by its settings key (`CW_CLUSTER_ADMIN_CMD`, `CW_CLUSTER_ADMIN_SECRET`), never a value; pins no checkout hash; and carries the secret rules, the rule to send the token mint's diagnostics to the null device, the rule to delete by exact name and the rule to stop and report at the first surprise. Paste its output at the top of the brief, then add the task.

`launch` also refuses a `--reports-to` name that is not a registered identity (a running, recorded or retired agent, a cloud worker, or the role alias `chief-of-staff`), before anything is created: it lists the nearest registered names. `--boss-unregistered REASON` goes on anyway and writes one `launch.boss_unregistered` audit line. If the registry cannot be read, the launch warns and goes on. The override --boss-unregistered REASON is not restricted to a person: an agent may pass it. It writes one launch.boss_unregistered audit line (the actor, the worker, the boss text and the reason). A worker launched so has a boss nobody answers to: only the operator or the Chief of Staff can end it. The reasons of both overrides, --boss-unregistered and --memory-ok, are scrubbed of secret shapes, made one line and cut to 160 characters before they are audited.

One temporary rule of `launch --cloud` (a named constant in `bin/cloud_worker.py`): a brief that links an issue of the repository prints one warning until a worker can read issues (ask #1019). A brief that names docker or buildx without `--stack` also prints one warning (the worker would have no nested Docker). The one-line output of a cloud launch now has a `shape` field.

## Checking a cloud pull request before the push

Over one day a cloud pull request took nine pushes and others two to four, because each cloud coder learned the repository's checks from a red run: a new test file not chained in `bin/test-cloud-launcher`, an existing test of another file broken by the change, or an existing pinned test edited to make a new behaviour pass; and a cloud pod cannot run the whole suite. Run `bin/cloud-pr-preflight` at the repository root before any push (offline; it copies the default branch to a clean temporary directory and removes it on exit). It prints three sections: the completeness scan of the test list, a per-file table of every test under `cloud/launcher/tests` and `cloud/worker/tests` run one at a time on the branch and on the default branch (`CLOUD_PREFLIGHT_TIMEOUT`, default 120 seconds each), and every existing test file the branch changes, which you must justify in the pull request (it only lists them: an edited existing test does not change the exit status). A file failing on the branch only is named and fails the command; one failing the same on both is listed as environment-only; a timeout is "not run" and fails unless the file is on the short allow-list in the script. Paste the table into the pull request before pushing. Exit 0 means the scan found nothing unchained, no test fails on the branch only and none timed out unallowed, and (with `--expect`) no added file is stray; the edited-test list in section 3 never fails the run by itself, you justify it in the pull request. `--expect` takes path PREFIXES (`cloud/` allows anything under it); a rename shows as one added and one deleted file (a known limit); "environment-only" (the identical failure on both sides) can also hide a test that fails for a real reason on both sides, and the output line says so. The terminate, hangup and interrupt signals also remove the temporary copy and stop any running test; `CLOUD_PREFLIGHT_TIMEOUT` must be a whole number of seconds or the command refuses (exit 2).

### A rule for every cloud coder brief

Before any push, run bin/cloud-pr-preflight at the repository root and paste its three sections in the pull request comment; a branch-only failure, a not-run file or an unchained test file must be fixed first, and every existing test file it lists as edited must be justified there.

## Pre-push preflight modes

<!-- preflight-modes:begin -->
### The preflight has two modes (ask #1299)

Cause: in a cloud pod the full preflight of the pre-push command (`bin/cloud-pr-preflight`) runs the repository's whole check on both the branch and the clean base, and it did not finish within 300 seconds, so the one pre-push command ended with "could not run the preflight".

- full mode (the default of `bin/cloud-pr-preflight`): every test file of `cloud/launcher/tests` and `cloud/worker/tests`, on the branch and the clean base. Time measured (a builder's measurement in a pod, not a CI figure): did not finish within 300 seconds.
- quick mode (`bin/cloud-pr-preflight --quick`, and the default of `bin/cloud-prepush`): only the test files that read a changed file plus the named scans of `bin/cloud_quick.py` (each a file and one selector, run through the same test code CI runs). Same three sections and exit codes. Time measured (a builder's measurement in a pod, not a CI figure): 194 seconds on a six-file change for both sides. The match is on the basename, so a doc change can select many files; a change no check reads exits 1 with `NOT RUN: no check reads <files>`; a memory-heavy test is skipped and counts as could not run. The scan table is in `cloud/README.md`. A quick run that selects nothing for a non-empty change fails, never clean.
- `CLOUD_PREPUSH_PREFLIGHT=full` asks `bin/cloud-prepush` for the full mode; `quick` (the default) is printed as `preflight mode: quick`; any other value is refused with exit 2. Continuous integration stays the full check.
<!-- preflight-modes:end -->
