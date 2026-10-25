#!/usr/bin/env python3
"""cloud_way_back_check: a READ-ONLY check of the way back from the cloud to local (slice 6a of the plan on #1059, scenario S6).

    bin/cloud_way_back_check.py FIXTURE_DIR

FIXTURE_DIR holds `way-back.json`, the state the operator (or a test) captured. The check reads it and the files it names
under FIXTURE_DIR; it writes nothing, runs nothing and calls no network. It prints one line per requirement, `OK` or
`MISSING`, then the verdict, and exits 0 when nothing is missing and 1 otherwise. Exit 2: the fixture cannot be read or is
malformed (one line, never a traceback).

way-back.json:
    {"phase": "before-start" | "after-start" | "done",
     "cloud_daemon":  {"running": bool},       # fill from the real lock state (`fleet-switchboard daemon status`, run.lock)
     "local_daemon":  {"running": bool},
     "snapshot":      {"file": "<relative path>", "sha256": "<hex>", "removed": bool},   # removed: tarball and exports deleted
     "cloud_agents":  ["name", ...],          # agents the cloud registry held
     "local_agents":  ["name", ...],          # agents the restored local registry holds
     "orchestrators": [{"name": ..., "export": "<relative path>", "export_fallback": "resume_brief"}],
     "import":        {"dir": "<dedicated directory>", "agent_dirs": ["<working directory of a live agent>", ...],
                       "copies_remaining": ["<session id>", ...]}}

Rules (the runbook is docs/cloud-way-back.md): the cloud daemon is stopped; before the local start no daemon is alive, after it
exactly the local one; a snapshot exists and its checksum matches the recorded one; the restored local registry lists every
cloud agent; every orchestrator has a session export or names the resume_brief fallback; the import goes into a dedicated
directory that no live agent works in (issue 1066: an import into a shared directory can appear in an unrelated pane); once
the way back is done no imported copy remains and the snapshot and exports are deleted (`snapshot_removed`; in that phase the
checksum and the exports are no longer checked). The dedicated-directory test is a guardrail, not a boundary. The guarantee of
one daemon is the run.lock flock, not this check. Every fixture-derived string is stripped of control characters and cut to
60 characters before it is printed. The fixture holds names, paths, flags and a checksum, never a key or token.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

PHASES = ("before-start", "after-start", "done")
MAX_SHOWN = 60


class FixtureError(ValueError):
    """A malformed fixture; the message names the field, never a value."""


def _clean(value):
    """A fixture-derived string made safe to print: no control characters, at most MAX_SHOWN characters."""
    text = "".join(c if c.isprintable() else "?" for c in str(value))
    return text[:MAX_SHOWN]


def _inside(root, relative):
    """The file `relative` under `root`, or None when it escapes the fixture directory."""
    try:
        path = (root / str(relative)).resolve()
        path.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return path


def _norm(path):
    try:
        return Path(os.path.normcase(str(Path(path).resolve())))
    except (ValueError, OSError):
        return None


def _overlap(import_dir, agent_dir):
    """Which way an agent's working directory meets the import directory: 'same', 'agent_inside', 'import_inside', 'unresolvable' (counts as shared) or None."""
    d, a = _norm(import_dir), _norm(agent_dir)
    if d is None or a is None:
        return "unresolvable"
    if a == d:
        return "same"
    if d in a.parents:
        return "agent_inside"
    if a in d.parents:
        return "import_inside"
    return None


def _need(ok, field):
    if not ok:
        raise FixtureError("field %s has the wrong type" % field)


def _strings(value, field):
    _need(isinstance(value, list) and all(isinstance(v, str) for v in value), field)


def _no_nul(value, field="(fixture)"):
    """Raise FixtureError when any string in the fixture, key or value, holds a NUL byte (it breaks path resolution)."""
    if isinstance(value, str):
        _need("\0" not in value, field + " (NUL byte)")
    elif isinstance(value, list):
        for item in value:
            _no_nul(item, field)
    elif isinstance(value, dict):
        for key, item in value.items():
            _no_nul(key, field)
            _no_nul(item, field)


def check_fixture_shape(state):
    """Raise FixtureError unless every field the check reads has its documented type and no string holds a NUL byte."""
    _no_nul(state)
    _need(isinstance(state, dict), "(root)")
    _need(state.get("phase") is None or isinstance(state.get("phase"), str), "phase")
    for key in ("cloud_daemon", "local_daemon", "snapshot", "import"):
        _need(state.get(key) is None or isinstance(state.get(key), dict), key)
    for key in ("cloud_daemon", "local_daemon"):
        running = (state.get(key) or {}).get("running")
        _need(running is None or isinstance(running, bool), key + ".running")
    snap = state.get("snapshot") or {}
    for key in ("file", "sha256"):
        _need(snap.get(key) is None or isinstance(snap.get(key), str), "snapshot." + key)
    _need(snap.get("removed") is None or isinstance(snap.get("removed"), bool), "snapshot.removed")
    for key in ("cloud_agents", "local_agents"):
        if state.get(key) is not None:
            _strings(state.get(key), key)
    orchestrators = state.get("orchestrators")
    _need(orchestrators is None or isinstance(orchestrators, list), "orchestrators")
    for item in orchestrators or []:
        _need(isinstance(item, dict), "orchestrators[]")
        for key in ("name", "export", "export_fallback"):
            _need(item.get(key) is None or isinstance(item.get(key), str), "orchestrators[]." + key)
    imp = state.get("import") or {}
    _need(imp.get("dir") is None or isinstance(imp.get("dir"), str), "import.dir")
    for key in ("agent_dirs", "copies_remaining"):
        if imp.get(key) is not None:
            _strings(imp.get(key), "import." + key)


def check(state, root):
    """Return [(name, ok, detail)] for a validated fixture; reads files under `root` only."""
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
        add("no_daemon_alive", local is False and cloud is False, "cloud %s, local %s" % (cloud, local))
    else:
        add("only_the_local_daemon", local is True and cloud is False, "cloud %s, local %s" % (cloud, local))

    snap = state.get("snapshot") or {}
    path = _inside(root, snap.get("file", "")) if snap.get("file") else None
    orchestrators = state.get("orchestrators") or []
    if phase != "done":
        digest = None
        try:
            if path is not None and path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        if digest is None:
            add("snapshot_matches", False, "snapshot file missing or unreadable")
        else:
            add("snapshot_matches", digest == snap.get("sha256"), "checksum %s" % ("equal" if digest == snap.get("sha256") else "differs"))
    else:
        exports = [_inside(root, i["export"]) for i in orchestrators if i.get("export")]
        escaped = (bool(snap.get("file")) and path is None) or any(p is None for p in exports)
        present = [p for p in [path] + exports if p is not None and p.exists()]
        done = snap.get("removed") is True and not present and not escaped
        add("snapshot_removed", done, "snapshot and exports deleted" if done else "a path escapes the fixture directory" if escaped else "snapshot or exports not deleted")

    cloud_agents = set(state.get("cloud_agents") or [])
    lost = sorted(cloud_agents - set(state.get("local_agents") or []))
    add("agents_restored", bool(cloud_agents) and not lost, "%d of %d agents missing locally: %s" % (len(lost), len(cloud_agents), ", ".join(_clean(n) for n in lost) or "none"))

    if phase != "done":
        absent = []
        for item in orchestrators:
            export = _inside(root, item["export"]) if item.get("export") else None
            if (export is None or not export.is_file()) and item.get("export_fallback") != "resume_brief":
                absent.append(_clean(item.get("name")))
        add("orchestrator_exports", not absent, "exports missing for: %s" % (", ".join(absent) or "none"))

    imp = state.get("import") or {}
    directory = imp.get("dir")
    shared = [d for d in imp.get("agent_dirs") or [] if directory and _overlap(directory, d)]
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
        check_fixture_shape(state)
    except FixtureError as err:
        out.write("cannot read way-back.json: %s\n" % _clean(err))
        return 2
    except (OSError, ValueError, RecursionError) as err:
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
