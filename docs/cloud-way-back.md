# The way back from FleetKit Cloud to local

Plan: <https://github.com/adamkaplan/fleet-kit/issues/1059#issuecomment-6320367025> (section 3, "The way back"; scenario S6; slice 6a, issue #1079).

The cloud fleet can be left and the Mac fleet resumed. This page is the order of steps, and `bin/cloud_way_back_check.py` is the read-only check
that says what is needed and what is missing. The cloud daemon and the Mac daemon never run at once (constraint PC-4): the cloud daemon is stopped
before the local one starts.

## What the check reads

```sh
bin/cloud_way_back_check.py FIXTURE_DIR
```

`FIXTURE_DIR/way-back.json` is the captured state (the field list is in the check's docstring): the phase (`before-start`, `after-start`, `done`),
whether each daemon runs, the state snapshot file and its checksum, the cloud and the restored local agent names, each orchestrator's session export,
and the import directory. The check writes nothing, runs nothing and uses no network. It prints one `OK` or `MISSING` line per requirement and the
verdict. Exit 0: nothing missing. Exit 1: something is missing. Exit 2: the fixture cannot be read. It never holds or prints a key or token.

## Steps

1. **Stop the cloud daemon** with its state snapshot to a tarball (the lease is released). Record its checksum.
2. **Restore the state directory** on the Mac from the tarball; the restored registry must list every agent the cloud registry held.
3. **Export each cloud orchestrator's session** (OpenCode v2 export from the pod). `resume_brief` from GitHub is the fallback if an export is missing.
4. Run the check with phase `before-start`: it must exit 0 (no daemon alive, snapshot matches, agents restored, exports present, import directory dedicated).
5. **Import** each export, then **start the local daemon**. Check again with phase `after-start`: exactly the local daemon is alive.
6. **Delete the imported copies** once the orchestrators are resumed, and run the check with phase `done`.

## Import goes into a dedicated directory, and the copy is deleted after

An import lands in the Mac's shared OpenCode session store, and through the directory and project match it can appear in an unrelated terminal pane
(issue 1066 finding: a session reported for a pane can take the pane of a live agent). So:

- import into a **dedicated directory** that no live agent works in (and that is not inside or above one); the check reports `import_dir_dedicated` MISSING otherwise;
- **delete the imported copy after** it has been used; in phase `done` the check reports `import_copy_deleted` MISSING while any copy remains.

## Cost and limits

Nothing here creates or spends on Azure. The check is only as true as the fixture: it does not talk to the cluster, so the stop of the cloud daemon is
confirmed by the operator before the fixture is written.
