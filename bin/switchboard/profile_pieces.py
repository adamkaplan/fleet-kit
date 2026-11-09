# ------------------------------------------------- the profile pieces (shared)
#
# What `install` puts into OpenCode v2's profile. bin/switchboard-trial calls these same functions with
# the trial's own profile, so there is one path. Each takes the directories it writes, and returns whether
# anything changed. Nothing here reads the environment.

class InstallError(Exception):
    """A step of `install` cannot go on, and says why and what to do."""


def current_uid():
    return os.getuid()


def check_writable_target(path):
    """Refuse a target this installer must not write: a symlink, anything but a regular file, a file another user owns or one
    another user can write. A missing file is fine. Raises InstallError naming the path and the reason."""
    path = Path(path)
    try:
        info = os.lstat(str(path))
    except FileNotFoundError:
        return
    except OSError as err:
        raise InstallError("cannot look at %s: %s" % (path, _one_line(str(err), 100))) from None
    if stat.S_ISLNK(info.st_mode):
        raise InstallError("%s is a symbolic link; the installer never writes through one (remove it first)" % path)
    if not stat.S_ISREG(info.st_mode):
        raise InstallError("%s is not a regular file; the installer leaves it alone" % path)
    if info.st_uid != current_uid():
        raise InstallError("%s is owned by another user; the installer leaves it alone" % path)
    if info.st_mode & 0o022:
        raise InstallError("%s can be written by its group or others (mode %o); fix it and run again" % (path, info.st_mode & 0o777))


def set_file_mode(path, mode):
    """The file's mode, set when it is not the one asked for. True when it changed it. The mode asked for is the
    one to set: a private file never gets a wider one."""
    try:
        info = os.lstat(str(path))
    except OSError:
        return False
    if stat.S_IMODE(info.st_mode) == mode:
        return False
    os.chmod(str(path), mode)
    return True


def write_if_changed(path, content, mode=None):
    """Write `content` unless the file already holds it. True when it wrote. The target is checked first
    (check_writable_target), and a write goes to a temporary file beside it and is renamed over it, so a file
    that appears in between is replaced, never written through.

    `mode` is the mode the file must have when it is written, and when it already holds the content (an identical
    file is given the mode too). None keeps today's behaviour: the mode of the umask, and the mode of a file that
    is replaced."""
    path = Path(path)
    check_writable_target(path)
    try:
        if path.read_text() == content:
            if mode is not None:
                set_file_mode(path, mode)
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    mask = os.umask(0)
    os.umask(mask)
    file_mode = (0o666 & ~mask) & ~0o022
    try:
        file_mode = stat.S_IMODE(os.lstat(str(path)).st_mode)   # a replaced file keeps its mode
    except OSError:
        pass
    if mode is not None:
        file_mode = mode
    fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(temp, file_mode)
        os.replace(temp, str(path))
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return True


def kit_agent_text(name, model, kit_root=KIT_ROOT):
    """A kit agent file with the model placeholder filled in."""
    folder = "reviewers" if name.startswith("reviewer-") else "opencode"   # the reviewer subagents are not fleet roles: they live in their own folder
    return (Path(kit_root) / "agents" / folder / (name + ".md")).read_text().replace("__PROVIDER__/__MODEL_ID__", model)


def install_agents(config_dir, model=STANDARD_MODEL, kit_root=KIT_ROOT):
    """The four agent definitions into <config_dir>/agents. True when any file changed."""
    changed = False
    for name in KIT_AGENTS:
        changed |= write_if_changed(Path(config_dir) / "agents" / (name + ".md"), kit_agent_text(name, model, kit_root))
    return changed


def kit_skill_names(kit_root=KIT_ROOT):
    root = Path(kit_root) / "skills"
    return sorted(p.name for p in root.iterdir() if (p / "SKILL.md").is_file())


def _kit_skill_text(name, kit_root):
    return (Path(kit_root) / "skills" / name / "SKILL.md").read_text()


def skill_locations(home=None, data_dir=None):
    """The folders agents read skills from that exist on this machine: the switchboard's own skills folder and the
    shared ~/.agents/skills (read by several harnesses). Only a folder that exists is a location."""
    home = Path(home) if home else Path.home()
    data = Path(data_dir) if data_dir else _xdg("XDG_DATA_HOME", ".local/share") / PROG
    return [p for p in (data / "skills", home / ".agents" / "skills") if p.is_dir()]


def skills_drift(locations, kit_root=KIT_ROOT):
    """[(location, skill, "differs" | "missing")] for every kit skill whose installed SKILL.md is not the kit's. Reads files only."""
    out = []
    names = kit_skill_names(kit_root)
    for location in locations:
        for name in names:
            path = Path(location) / name / "SKILL.md"
            try:
                same = path.read_text() == _kit_skill_text(name, kit_root)
            except OSError:
                out.append((location, name, "missing"))
                continue
            except ValueError:
                same = False
            if not same:
                out.append((location, name, "differs"))
    return out


def skills_status_line(locations=None, kit_root=KIT_ROOT):
    """(ok, text) for status and the doctor: in sync, or each skill that differs or is missing and the command that fixes it."""
    locations = skill_locations() if locations is None else locations
    if not locations:
        return True, "no installed skills folder found (`%s install` creates it)" % PROG
    drift = skills_drift(locations, kit_root)
    if not drift:
        return True, "in sync with the kit (%s)" % ", ".join(str(p) for p in locations)
    return False, "NOT in sync: %s; run `%s skills sync`" % (
        "; ".join("%s %s in %s" % (name, why, loc) for loc, name, why in drift), PROG)


def _replace_skill_file(path, text):
    """Replace `path` with `text`: keep the old file as a backup beside it, write a temporary file then rename, keep the mode."""
    path = Path(path)
    mode = 0o644
    if path.exists():
        mode = path.stat().st_mode & 0o7777
        stamp = time.strftime("%Y%m%d%H%M%S")
        backup = path.with_name("%s.bak-%s" % (path.name, stamp))
        n = 0
        while backup.exists():
            n += 1
            backup = path.with_name("%s.bak-%s-%d" % (path.name, stamp, n))
        shutil.copy2(str(path), str(backup))
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    try:
        with os.fdopen(handle, "w") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, mode)
        os.replace(temp, str(path))
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def sync_skills(skills_dir, kit_root=KIT_ROOT):
    """The one step: make `skills_dir` hold the kit's skills, one directory each, and nothing else changes (a skill that
    is not the kit's, a file beside the skills: untouched). A file already equal is not written. Returns the changed paths."""
    changed = []
    for name in kit_skill_names(kit_root):
        path = Path(skills_dir) / name / "SKILL.md"
        text = _kit_skill_text(name, kit_root)
        try:
            if path.read_text() == text:
                continue
        except (OSError, ValueError):
            pass
        _replace_skill_file(path, text)
        changed.append(str(path))
    return changed


def install_skills(skills_dir, kit_root=KIT_ROOT):
    """The kit's skills into `skills_dir` (the sync step). True when any file changed."""
    return bool(sync_skills(skills_dir, kit_root))


def cmd_skills(args, runner, now):
    """`skills check` (read only) and `skills sync` (the kit's skills into every installed folder that exists)."""
    locations = skill_locations()
    if args.skills_action == "check":
        ok, text = skills_status_line(locations, KIT_ROOT)
        print(text)
        return EXIT_OK if ok else EXIT_ERROR
    if not locations:
        sys.stderr.write("%s: skills: no installed skills folder found; run `%s install`\n" % (PROG, PROG))
        return EXIT_ERROR
    for location in locations:
        changed = sync_skills(location, KIT_ROOT)
        print("%s: %s" % (location, "%d file(s) replaced (old ones kept as SKILL.md.bak-*)" % len(changed) if changed else "already in sync"))
    return EXIT_OK


cmd_skills.direct = True   # files only: no daemon start


def add_skills_parsers(sub):
    skills = sub.add_parser("skills", help="Compare the installed skills with the kit's (`skills check`, read only) or copy the kit's into every installed folder (`skills sync`)")
    skills_sub = skills.add_subparsers(dest="skills_action", required=True)
    skills_sub.add_parser("check", help="Say whether the installed skills equal the kit's; exit 1 naming each that differs or is missing")
    skills_sub.add_parser("sync", help="Copy exactly the kit's skills into the installed folders, keeping a backup of each file replaced")


def _read_json_object(path, what):
    """The JSON object at `path` ({} when there is no file). Anything else is refused, never overwritten."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        body = None
    if not isinstance(body, dict):
        raise InstallError("%s is not a JSON object: fix it by hand (%s), then run again" % (path, what))
    return body


def ensure_skills_path(config_dir, skills_dir, model=None):
    """Name `skills_dir` in the profile's opencode.json under skills.paths, keeping every other key.

    v2 also scans the user's home for skills, and a skill of the same name there would win over one that is
    only copied into the profile; a configured path is read after those and wins. True when it wrote.
    """
    path = Path(config_dir) / "opencode.json"
    body = _read_json_object(path, "it holds the profile's settings")
    skills = body.get("skills")
    if skills is not None and not isinstance(skills, dict):
        raise InstallError("%s: `skills` is not an object: fix it by hand, then run again" % path)
    skills = dict(skills or {})
    paths = skills.get("paths", [])
    if not isinstance(paths, list):
        raise InstallError("%s: `skills.paths` is not a list: fix it by hand, then run again" % path)
    if str(skills_dir) in paths:
        return False
    skills["paths"] = list(paths) + [str(skills_dir)]
    if not body:
        body["$schema"] = "https://opencode.ai/config.json"
        if model:
            body["model"] = model
    body["skills"] = skills
    return write_if_changed(path, json.dumps(body, indent=2) + "\n")


def install_hooks_plugin(config_dir, kit_root=KIT_ROOT):
    return write_if_changed(Path(config_dir) / "plugins" / HOOKS_PLUGIN_FILE,
                            (Path(kit_root) / "plugins" / "fleet-hooks" / "index.js").read_text())


def read_cli_json(config_dir):
    """The profile's cli.json as an object ({} when there is none). A file that is not a JSON object with a
    `plugins` list is refused, never overwritten: it may hold plugins this code knows nothing about."""
    path = Path(config_dir) / "cli.json"
    body = _read_json_object(path, "it names the TUI plugins")
    if not isinstance(body.get("plugins", []), list):
        raise InstallError("%s is not a JSON object with a `plugins` list: fix it by hand, then run again" % path)
    return body


def install_decisions_plugin(config_dir, kit_root=KIT_ROOT):
    """Copy the Decisions TUI plugin into the profile and name its directory in cli.json, keeping every other
    plugin and key there. Idempotent; True when anything changed. Nothing is restarted."""
    source = Path(kit_root) / "plugins" / "fleet-decisions-tui"
    target = Path(config_dir) / DECISIONS_PLUGIN_NAME
    changed = False
    for name in DECISIONS_PLUGIN_FILES:
        changed |= write_if_changed(target / name, (source / name).read_text())
    body = read_cli_json(config_dir)
    wanted = "./" + DECISIONS_PLUGIN_NAME
    if wanted not in body.get("plugins", []):
        body["plugins"] = list(body.get("plugins", [])) + [wanted]
        changed |= write_if_changed(Path(config_dir) / "cli.json", json.dumps(body, indent=2) + "\n")
    return changed


def decisions_plugin_status(config_dir, kit_root=KIT_ROOT):
    """`installed`, `not installed`, or `out of date` (a file differs from the kit's)."""
    try:
        named = ("./" + DECISIONS_PLUGIN_NAME) in read_cli_json(config_dir).get("plugins", [])
    except InstallError:
        named = False
    source = Path(kit_root) / "plugins" / "fleet-decisions-tui"
    target = Path(config_dir) / DECISIONS_PLUGIN_NAME
    if not named or not all((target / name).is_file() for name in DECISIONS_PLUGIN_FILES):
        return "not installed"
    same = all((target / name).read_text() == (source / name).read_text() for name in DECISIONS_PLUGIN_FILES)
    return "installed" if same else "out of date"


def _todolist_files(kit_root):
    """{path relative to src/: bytes} for every file under the vendored plugin's src/."""
    src = Path(kit_root) / "plugins" / "opencode-todolist" / "src"
    return {str(p.relative_to(src)): p.read_bytes() for p in sorted(src.rglob("*")) if p.is_file()}


def _todolist_paths(config_dir):
    config = Path(config_dir)
    return config / TODOLIST_PLUGIN_NAME, config / "plugins" / TODOLIST_LOADER_FILE


def _same_bytes(path, content):
    try:
        return Path(path).read_bytes() == content
    except OSError:
        return False


def install_todolist_plugin(config_dir, kit_root=KIT_ROOT):
    """Copy the vendored todo plugin's src/ into <config>/opencode-todolist/ and write the loader
    <config>/plugins/opencode-todolist.ts that re-exports it (a `plugins` entry naming the directory does not
    load: its package.json exports an unbuilt ./dist). A loader this code did not write is refused, never
    overwritten; so is a differing src file when there is no loader of ours to say the directory is ours.
    Idempotent; True when anything changed."""
    files = _todolist_files(kit_root)
    target, loader = _todolist_paths(config_dir)
    loader_bytes = TODOLIST_LOADER_TEXT.encode()
    if loader.exists() and not _same_bytes(loader, loader_bytes):
        raise InstallError("%s exists and the kit did not write it: move it aside, then run again" % loader)
    ours = loader.exists()
    if not ours:
        for name, body in files.items():
            if (target / name).exists() and not _same_bytes(target / name, body):
                raise InstallError("%s exists and the kit did not write it: move it aside, then run again"
                                   % (target / name))
    changed = False
    for name, body in files.items():
        if not _same_bytes(target / name, body):
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            (target / name).write_bytes(body)
            changed = True
    if not ours:
        loader.parent.mkdir(parents=True, exist_ok=True)
        loader.write_bytes(loader_bytes)
        changed = True
    return changed


def todolist_plugin_status(config_dir, kit_root=KIT_ROOT):
    """`not installed`, `already installed`, or `differs from the kit`."""
    files = _todolist_files(kit_root)
    target, loader = _todolist_paths(config_dir)
    if not loader.exists() and not any((target / name).exists() for name in files):
        return "not installed"
    same = _same_bytes(loader, TODOLIST_LOADER_TEXT.encode()) and all(
        _same_bytes(target / name, body) for name, body in files.items())
    return "already installed" if same else "differs from the kit"


def uninstall_todolist_plugin(config_dir, kit_root=KIT_ROOT):
    """Remove the files install wrote and only those: a file edited since stays, and so does its directory."""
    target, loader = _todolist_paths(config_dir)
    removed = False
    if _same_bytes(loader, TODOLIST_LOADER_TEXT.encode()):
        loader.unlink()
        removed = True
    for name, body in _todolist_files(kit_root).items():
        if _same_bytes(target / name, body):
            (target / name).unlink()
            removed = True
    for path in sorted((p for p in target.rglob("*") if p.is_dir()), reverse=True) if target.is_dir() else []:
        try:
            path.rmdir()
        except OSError:
            pass
    try:
        target.rmdir()
    except OSError:
        pass
    return removed


def remove_if_same(path, content):
    """Delete `path` when it still holds exactly `content` (what the kit wrote). False when absent or edited."""
    path = Path(path)
    try:
        if path.read_text() != content:
            return False
    except OSError:
        return False
    path.unlink()
    return True


def uninstall_agents(config_dir, model=STANDARD_MODEL, kit_root=KIT_ROOT):
    removed = False
    for name in KIT_AGENTS:
        removed |= remove_if_same(Path(config_dir) / "agents" / (name + ".md"), kit_agent_text(name, model, kit_root))
    return removed


def uninstall_skills(skills_dir, kit_root=KIT_ROOT):
    removed = False
    for name in kit_skill_names(kit_root):
        target = Path(skills_dir) / name
        if remove_if_same(target / "SKILL.md", (Path(kit_root) / "skills" / name / "SKILL.md").read_text()):
            removed = True
            try:
                target.rmdir()
            except OSError:
                pass
    try:
        Path(skills_dir).rmdir()
    except OSError:
        pass
    return removed


def uninstall_skills_path(config_dir, skills_dir):
    """Take `skills_dir` out of skills.paths; drop what is then empty; remove the file if it held only that."""
    path = Path(config_dir) / "opencode.json"
    if not path.exists():
        return False
    body = _read_json_object(path, "it holds the profile's settings")
    skills = body.get("skills")
    if not isinstance(skills, dict) or str(skills_dir) not in (skills.get("paths") or []):
        return False
    skills["paths"] = [p for p in skills["paths"] if p != str(skills_dir)]
    if not skills["paths"]:
        del skills["paths"]
    if not skills:
        del body["skills"]
    if set(body) <= {"$schema", "model"}:
        path.unlink()
    else:
        path.write_text(json.dumps(body, indent=2) + "\n")
    return True


def uninstall_hooks_plugin(config_dir, kit_root=KIT_ROOT):
    return remove_if_same(Path(config_dir) / "plugins" / HOOKS_PLUGIN_FILE,
                          (Path(kit_root) / "plugins" / "fleet-hooks" / "index.js").read_text())


def uninstall_decisions_plugin(config_dir, kit_root=KIT_ROOT):
    source = Path(kit_root) / "plugins" / "fleet-decisions-tui"
    target = Path(config_dir) / DECISIONS_PLUGIN_NAME
    removed = False
    for name in DECISIONS_PLUGIN_FILES:
        removed |= remove_if_same(target / name, (source / name).read_text())
    try:
        target.rmdir()
    except OSError:
        pass
    path = Path(config_dir) / "cli.json"
    if path.exists():
        body = read_cli_json(config_dir)
        wanted = "./" + DECISIONS_PLUGIN_NAME
        if wanted in body.get("plugins", []):
            body["plugins"] = [p for p in body["plugins"] if p != wanted]
            if not body["plugins"]:
                del body["plugins"]
            if body:
                path.write_text(json.dumps(body, indent=2) + "\n")
            else:
                path.unlink()
            removed = True
    return removed
