# The way back from the cloud to local

Plan: <https://github.com/adamkaplan/fleet-kit/issues/1059#issuecomment-6320367025> (section 3, "The way back"; scenario S6; slice 6a, issue #1079).

The cloud fleet can be left and the Mac fleet resumed. This page is the order of steps, and `bin/cloud_way_back_check.py` is the read-only check
that says what is needed and what is missing. The cloud daemon and the Mac daemon never run at once (constraint PC-4). The guarantee of one daemon
comes from the `run.lock` flock in the state directory (a second daemon refuses with "another run holds"), **not** from the check: the check only
reads what you wrote in the fixture.

## What the check reads

```sh
bin/cloud_way_back_check.py FIXTURE_DIR
```

`FIXTURE_DIR/way-back.json` is the captured state (the field list is in the check's docstring): the phase (`before-start`, `after-start`, `done`),
whether each daemon runs, the snapshot file with its checksum and `removed` flag, the cloud and the restored local agent names, each orchestrator's
session export (or `"export_fallback": "resume_brief"`), and the import directory with the working directories of live agents. The check writes
nothing, runs nothing and uses no network. It prints one `OK` or `MISSING` line per requirement and the verdict. Exit 0: nothing missing. Exit 1:
something is missing. Exit 2: the fixture cannot be read or is malformed (one line, no traceback). Names from the fixture are stripped of control
characters and cut to 60 characters before they are printed. The fixture holds names, paths, flags and a checksum, never a key or token.

Fill `running` from the real lock state, not from memory: `fleet-switchboard daemon status` (exit 0 only when a daemon runs and is healthy), or
whether `run.lock` in the state directory is held. The state directory is `$XDG_STATE_HOME/fleet-switchboard`, default `~/.local/state/fleet-switchboard`.

## Handling the snapshot and the exports (read first)

The state tarball and the session exports hold session data and may hold credentials.

- Work in a **private directory outside the repository** (never in a repository, a shared folder or a shared store): `umask 077; mkdir -p ~/wayback-private`.
  Create the tarball and the exports there, with `umask 077` in force.
- Record the tarball's **checksum at snapshot time** (`sha256sum`) and keep that record **apart from the fixture's directory** (for example in your notes).
  The checksum detects corruption, it does not detect tampering: whoever can edit the fixture can edit its checksum.
- **Delete** the tarball and the exports at the end (step 8). In phase `done` the check reports `snapshot_removed` MISSING until `snapshot.removed` is
  true and none of those files is left under the fixture directory.

## Steps

1. **Choose the dedicated import directory now**, before anything else (an empty directory that no agent works in, for example `~/wayback-import`). Step 5 needs it.
2. **Stop the cloud daemon and take its state snapshot to a tarball**, and record the checksum. This command does not exist yet: the cloud daemon, its
   stop-with-snapshot and its lease belong to slice 3 (#1072, #1073) and the orchestrator launch to #1068. Until they land, do not invent one; the
   cloud daemon is stopped by whatever the cloud deployment provides, confirmed by the operator.
3. **Restore the state directory** on the Mac from the tarball into `$XDG_STATE_HOME/fleet-switchboard` (default `~/.local/state/fleet-switchboard`), while the
   local daemon is not running. The restored registry must list every agent the cloud registry held (`fleet-switchboard status`).
4. **Export each cloud orchestrator's session** from its pod with OpenCode's session export (`opencode session export ... --server ...`; the exact calls
   worked in slice 1 and are on issue #1061). If an export cannot be made, name the fallback in the fixture: `"export_fallback": "resume_brief"`
   for that orchestrator, and resume it from GitHub with the resume brief (`bin/resume_brief.py`; `fleet-switchboard launch --resume SESSION --brief-file FILE`).
5. **Run the check with phase `before-start`**: it must exit 0 (no daemon alive: cloud and local both stopped, snapshot matches the recorded checksum,
   agents restored, an export or the fallback for each orchestrator, import directory dedicated).
6. **Import each export** into the dedicated directory (the matching `opencode session import`), then **start the local daemon**
   (`fleet-switchboard daemon start`). Check again with phase `after-start`: exactly the local daemon is alive.
7. **Resume the orchestrators**, then delete the imported copies (step below). Retire any agent that should not continue with `fleet-switchboard retire NAME`.
8. **Delete the imported copies, the tarball and the exports**, set `snapshot.removed` to true, and run the check with phase `done`.

## Import goes into a dedicated directory, and the copy is deleted after

An import lands in the Mac's shared OpenCode session store, and through the directory and project match it can appear in an unrelated terminal pane
(issue 1066 finding: a session reported for a pane can take the pane of a live agent). So:

- import into a **dedicated directory** that no live agent works in (and that is not inside or above one); the check reports `import_dir_dedicated` MISSING
  when the directory is the same as, inside, or above a live agent's directory (paths are resolved first), or when none is named;
- **delete the imported copy after** it has been used; in phase `done` the check reports `import_copy_deleted` MISSING while any copy remains.

The directory test is a **guardrail, not a boundary**: it compares the paths you wrote down, it does not stop OpenCode from matching a pane.

## Cost and limits

Nothing here creates or spends on Azure. The check is only as true as the fixture: it does not talk to the cluster, so the stop of the cloud daemon is
confirmed by the operator before the fixture is written.
