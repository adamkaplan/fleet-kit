#!/usr/bin/env python3
"""cloud_way_back_check: a READ-ONLY check of the way back from FleetKit Cloud to local (slice 6a of the plan on #1059, scenario S6).

    bin/cloud_way_back_check.py FIXTURE_DIR

FIXTURE_DIR holds `way-back.json`, the state the operator (or a test) captured. The check reads it and the files it names
under FIXTURE_DIR; it writes nothing, runs nothing and calls no network. It prints one line per requirement, `OK` or
`MISSING`, then the verdict, and exits 0 when nothing is missing and 1 otherwise (2 when the fixture cannot be read).

way-back.json:
    {"phase": "before-start" | "after-start" | "done",
     "cloud_daemon":  {"running": bool},
     "local_daemon":  {"running": bool},
     "snapshot":      {"file": "<relative path>", "sha256": "<hex>"},
     "cloud_agents":  ["name", ...],          # agents the cloud registry held
     "local_agents":  ["name", ...],          # agents the restored local registry holds
     "orchestrators": [{"name": ..., "export": "<relative path>"}],
     "import":        {"dir": "<dedicated directory>", "agent_dirs": ["<working directory of a live agent>", ...],
                       "copies_remaining": ["<session id>", ...]}}

Rules (the runbook is docs/cloud-way-back.md): the cloud daemon is stopped; a snapshot exists and its checksum matches; the
restored local registry lists every cloud agent; at most one daemon is alive (after the local start exactly the local one);
every orchestrator has a session export; the import goes into a dedicated directory that no live agent works in (issue
1066: an import into a shared directory can appear in an unrelated pane), and once the way back is done no imported copy remains.
The fixture holds names, paths, flags and a checksum, never a key or token.
"""
import hashlib
import json
import sys
from pathlib import Path

PHASES = ("before-start", "after-start", "done")


def _inside(root, relative):
    """The file `relative` under `root`, or None when it escapes the fixture directory."""
    path = (root / str(relative)).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    return path


def _same_dir(a, b):
    return str(a).rstrip("/") == str(b).rstrip("/")


def check(state, root):
    """Return [(name, ok, detail)] for a parsed fixture; reads files under `root` only."""
    root = Path(root)
    results = []

    def add(name, ok, detail):
        results.append((name, bool(ok), detail))

    phase = state.get("phase")
    add("phase_known", phase in PHASES, "phase %s" % (phase if phase in PHASES else "unknown"))
    cloud = (state.get("cloud_daemon") or {}).get("running")
    local = (state.get("local_daemon") or {}).get("running")
    add("cloud_daemon_stopped", cloud is False, "cloud daemon running" if cloud else "cloud daemon state unknown" if cloud is None else "cloud daemon stopped")
    if phase == "before-start":
        add("one_daemon_at_most", not (cloud and local), "cloud %s, local %s" % (cloud, local))
    else:
        add("only_the_local_daemon", local is True and cloud is False, "cloud %s, local %s" % (cloud, local))

    snap = state.get("snapshot") or {}
    path = _inside(root, snap.get("file", "")) if snap.get("file") else None
    if path is None or not path.is_file():
        add("snapshot_matches", False, "snapshot file missing")
    else:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        add("snapshot_matches", digest == snap.get("sha256"), "checksum %s" % ("equal" if digest == snap.get("sha256") else "differs"))

    cloud_agents = set(state.get("cloud_agents") or [])
    lost = sorted(cloud_agents - set(state.get("local_agents") or []))
    add("agents_restored", bool(cloud_agents) and not lost, "%d of %d agents missing locally: %s" % (len(lost), len(cloud_agents), ", ".join(lost) or "none"))

    absent = []
    for item in state.get("orchestrators") or []:
        export = _inside(root, item.get("export", "")) if item.get("export") else None
        if export is None or not export.is_file():
            absent.append(str(item.get("name")))
    add("orchestrator_exports", not absent, "exports missing for: %s" % (", ".join(absent) or "none"))

    imp = state.get("import") or {}
    directory = imp.get("dir")
    shared = [d for d in imp.get("agent_dirs") or [] if directory and (_same_dir(d, directory) or str(d).startswith(str(directory).rstrip("/") + "/") or str(directory).startswith(str(d).rstrip("/") + "/"))]
    add("import_dir_dedicated", bool(directory) and not shared, "dedicated directory" if directory and not shared else "no import directory named" if not directory else "shared with %d live agent directories" % len(shared))
    if phase == "done":
        left = imp.get("copies_remaining") or []
        add("import_copy_deleted", not left, "%d imported copies remain" % len(left))
    return results


def main(argv, out=sys.stdout):
    if len(argv) != 1:
        out.write("usage: cloud_way_back_check.py FIXTURE_DIR\n")
        return 2
    root = Path(argv[0])
    try:
        state = json.loads((root / "way-back.json").read_text())
        if not isinstance(state, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as err:
        out.write("cannot read way-back.json: %s\n" % type(err).__name__)
        return 2
    results = check(state, root)
    for name, ok, detail in results:
        out.write("%-7s %s: %s\n" % ("OK" if ok else "MISSING", name, detail))
    missing = [n for n, ok, _ in results if not ok]
    out.write("needed: %d, missing: %d\n" % (len(results), len(missing)))
    out.write("WAY BACK READY\n" if not missing else "WAY BACK NOT READY: %s\n" % ", ".join(missing))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
