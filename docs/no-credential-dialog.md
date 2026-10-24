# No git call of the fleet opens a credential dialog

A credential helper such as Git Credential Manager can open a dialog on the person's screen. Git runs the helper
whenever a server answers `401`, for **any** URL: a code host, another host, or `http://127.0.0.1:<random port>` (a
local test server, a `kubectl port-forward`). The fleet's `git` shim routes only the code host
(`credential.https://github.com.helper`), so every other URL fell to the machine's own helper, and with it the dialog.

Two environment variables and one setting stop it, and they are set in every place the fleet starts git:

| Place | What it sets | Why |
|---|---|---|
| `shims/git` | `GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never` in the environment, and `-c credential.interactive=false` right before the verb, on every call it passes on (all verbs, all hosts) | The shim is first on an agent's `PATH`: every `git` an agent types goes through it. The setting comes after the caller's own `-c`, so interactivity is never turned back on; a caller's other `credential.*` settings stay. |
| Launch environments (`launch_base_env` in `bin/fleet-switchboard`: terminal launch and reattach; the OpenCode service client; a headless worker's tool processes start in the service's own environment, which this repository does not set: for them the shim's forced variables are the protection, and that start was not verified here) | `GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never` | A tool that calls the real git by its full path bypasses the shim, as does an alias that runs `git` again (git puts its own directory first on `PATH`, so a caller that writes `credential.interactive=true` there is believed); the environment still stops the prompt. A configured `opencode_env` or `launch_env` cannot turn them off. |
| Cloud pod entrypoint (`cloud/worker/entrypoint.sh`) | `GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never`, exported | Nobody can answer a dialog in a pod. |
| Heavy wrapper (`bin/heavy_slots.py`) and the test suites (`bin/test-switchboard`) | `GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never` | A test that runs real git against a local server must fail at once, never ask. Such a test also sets `GIT_CONFIG_NOSYSTEM=1` and a private home, so no machine helper is read. |

Nothing here reads, prints or writes a credential, and nothing writes the person's global git configuration
: cleaning an existing entry is the person's own decision.

A git call that relied on a prompt now fails at once with git's own error (`terminal prompts disabled`).

The check that nothing writes a git configuration is behavioural first: the suite runs the launch environment, the service client, the heavy wrapper and the shim in front of a real git with `HOME` and the global and XDG variables pointing at a scratch home, and asserts that home is byte-identical afterwards with no new file. The text scan of the diff is only a cheap first line; it cannot see a spelling built from pieces.
