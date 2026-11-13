# Wiring a remote worker to the switchboard: design note

1. **The route.** A launch chooses it with one option, `launch --remote` (a flag, no value). Everything else is configuration: one JSON file (path in
   `FLEET_REMOTE_CONFIG`) names the backend and the carrier settings (storage account, floor id, caller id, queue and container SAS file paths, git
   base), the land command, the token command and a state directory.
2. **The module is imported where it is used.** The code behind the route is `remote_worker/cli.py` (also runnable alone: `python3 -m remote_worker`).
   The switchboard script gets ONE call site, in `_cmd_launch` beside the `--cloud` branch (about 5 lines plus the option line, nothing else: no new module
   of `bin/`, no new subcommand). Until that diff is agreed the route is reachable only through `python3 -m remote_worker launch`.
3. **The repo token.** A configured command (an argument list, never a shell) writes a short-lived token to a pipe: one argument holds `{fd}`, which becomes the write end's number (the command prints nothing on stdout); it runs once per launch, what it wrote is
   checked for plain token characters and goes into the launch command's `env` under a configured name. It is never stored (only the queue message,
   in memory, holds it), never logged, never in an error or a repr (`WorkerSpec.env` is hidden from `repr`).
4. **Same module for the rest.** `status`, `last-message`, `send` and `retire` are the other commands of the same `cli.py`. A worker's record
   (name, session id, next command number: no secret) is one small file in the state directory, so a later process can number its commands.
5. **Names.** Every example name is generic; the land command is a configuration value with no default.
