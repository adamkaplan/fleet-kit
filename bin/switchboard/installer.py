# ----------------------------------------------------------------- the installer

def v2_version_of(runner, exe):
    """The version `binary --version` prints, as a tuple of ints, or None (it did not run or did not say)."""
    try:
        done = runner([exe, "--version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", (done.stdout or "") + " " + (done.stderr or "")) if done.returncode == 0 else None
    return tuple(int(part) for part in match.groups()) if match else None


OLD_JANITOR_COMMAND = "%(p)s disk prune; %(p)s disk janitor --apply --weekly"   # the weekly job the documentation used to show (#958): the janitor half is now the daemon's gated run
JANITOR_JOB_TAIL = "; %(p)s disk janitor --apply --weekly"


def weekly_job_scan(directory):
    """[(path, state, detail)] for each `*.plist` file of `directory` that mentions `disk janitor --apply`: state `old` (the job's ProgramArguments are EXACTLY
    `/bin/sh -c "<P> disk prune; <P> disk janitor --apply --weekly"`, one program path P twice, and the old command occurs once in the file's text: it may be
    rewritten) or `other` (anything else: only reported, never changed). A file that cannot be read or parsed is `other`. The Docker prune part is never read as
    anything but the text in front of the cut."""
    import plistlib
    found = []
    try:
        names = sorted(n for n in os.listdir(str(directory)) if n.endswith(".plist"))
    except OSError:
        return found
    for name in names:
        path = Path(directory) / name
        try:
            if path.is_symlink() or not path.is_file():
                continue
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if "disk janitor --apply" not in text:
            continue
        try:
            args = plistlib.loads(text.encode("utf-8")).get("ProgramArguments")
        except Exception:
            args = None
        program = None
        if isinstance(args, list) and len(args) == 3 and args[:2] == ["/bin/sh", "-c"] and isinstance(args[2], str):
            first = args[2].split(" disk prune; ", 1)[0]
            if first and " " not in first and args[2] == OLD_JANITOR_COMMAND % {"p": first}:
                program = first
        if program and text.count(OLD_JANITOR_COMMAND % {"p": program}) == 1:
            found.append((path, "old", program))
        else:
            found.append((path, "other", "its command is not exactly the old snippet's"))
    return found


class Installer:
    """`fleet-switchboard install`: idempotent steps, each reported `done`, `already done` or `skipped (why)`.

    `runner` runs the other programs (OpenCode, herdr, launchctl, systemctl): tests give it a fake and a
    temporary HOME, so nothing real is touched. `dry_run` reports what would change and writes nothing.
    """

    def __init__(self, runner=subprocess.run, spawn=subprocess.Popen, now=time.time, platform=None, kit_root=KIT_ROOT,
                 dry_run=False, out=None, shared_profile=False, profile=None, gh_user=None):
        self.runner, self.spawn, self.now = runner, spawn, now
        self.platform = platform
        self.kind = service_kind(platform)
        self.kit_root = Path(kit_root)
        self.dry = dry_run
        self.out = out or sys.stdout
        self.problems = []
        self.asked_shared = bool(shared_profile)
        # v2's profile is private unless the person asks for the shared one, by name. (The fault is the bug this
        # fixed: a default that writes where the ordinary OpenCode reads.)
        self.shared = self.asked_shared or fault("shared-profile-default")
        self.profile_arg, self.gh_user = profile, gh_user

    # -- reporting
    def say(self, step, status, detail=""):
        self.out.write("%-24s %s%s\n" % (step, status, " " + detail if detail else ""))

    def result(self, step, changed, detail="", unchanged_detail=None):
        status = ("would be done" if self.dry else "done") if changed else "already done"
        self.say(step, status, detail if changed else (unchanged_detail if unchanged_detail is not None else detail))

    # -- places
    def profile_root(self):
        """The private profile: v2's XDG_CONFIG_HOME, XDG_DATA_HOME, XDG_STATE_HOME and HOME (its wrapper sets them)."""
        if self.profile_arg:
            if not os.path.isabs(self.profile_arg):
                raise InstallError("--profile must be an absolute path, got %r" % self.profile_arg)
            return Path(self.profile_arg)
        return self.data_dir() / "profile"

    def config_dir(self, config):
        """The OpenCode config directory the pieces go into: the private profile's, or with --shared-profile the one
        the ordinary OpenCode reads."""
        if not self.shared:
            return self.profile_root() / "config" / "opencode"
        env = config.get("opencode_env") or {}
        if env.get("OPENCODE_CONFIG_DIR"):
            return Path(env["OPENCODE_CONFIG_DIR"])
        if env.get("XDG_CONFIG_HOME"):
            return Path(env["XDG_CONFIG_HOME"]) / "opencode"
        return _xdg("XDG_CONFIG_HOME", ".config") / "opencode"

    def data_dir(self):
        return _xdg("XDG_DATA_HOME", ".local/share") / PROG

    def skills_dir(self):
        return self.data_dir() / "skills"

    def wrapper_path(self):
        return self.data_dir() / "bin" / WRAPPER_NAME

    # -- 1: the OpenCode v2 binary
    def candidates(self, explicit, config):
        if explicit:
            return [explicit]
        found = []
        configured = config.get("opencode_executable") or config.get("opencode")
        if configured and configured != str(self.wrapper_path()):
            found.append(configured)
        found.append(str(self.data_dir() / "v2" / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"))
        found.append(str(Path.home() / ".local" / "share" / "fleet-v2" / "bin" / "opencode"))
        on_path = shutil.which("opencode")
        if on_path:
            found.append(on_path)
        return found

    def find_v2(self, explicit, config):
        """(binary, version) of an OpenCode v2 by absolute path. Refuses with how to get one."""
        tried = []
        for binary in self.candidates(explicit, config):
            if not os.path.isabs(binary):
                raise InstallError("--opencode must be an absolute path, got %r: a bare name is resolved through PATH, "
                                   "and PATH can find OpenCode v1" % binary)
            version = v2_version_of(self.runner, binary) if os.path.exists(binary) else None
            if version is not None and version[0] == 2:
                return binary, ".".join(str(part) for part in version)
            tried.append("%s (%s)" % (binary, "not there" if not os.path.exists(binary)
                                      else "version %s" % (".".join(map(str, version)) if version else "unreadable")))
        prefix = self.data_dir() / "v2"
        raise InstallError(
            "no OpenCode v2 found (looked at: %s). Your ordinary `opencode` may be v1; this never assumes it is not.\n"
            "  Get v2 into a private directory, never `npm -g` or the curl installer (they replace v1):\n"
            "    npm install --prefix %s --no-audit --no-fund --ignore-scripts %s@%s\n"
            "    (cd %s/node_modules/%s && node ./postinstall.mjs)\n"
            "  then run `%s install --opencode %s/node_modules/@opencode/cli/bin/opencode.exe`."
            % (", ".join(tried) or "nothing", prefix, V2_PACKAGE, V2_VERSION, prefix, V2_PACKAGE, PROG, prefix))

    # -- 2: config.json
    OWNED_KEYS = ("opencode", "opencode_executable")  # what a pane runs: install owns exactly these two

    def wanted_config(self, binary):
        # A shared profile has no wrapper: the binary is run as given (a stable link, not its resolved path, so a
        # package upgrade does not break it). A private one runs it through the wrapper.
        keys = {"opencode": binary if self.shared else str(self.wrapper_path()), "opencode_executable": binary}
        if not self.shared:  # the wrapper drops a pane's herdr identity (its plugin reports status) unless this is set
            keys["launch_env"] = {PANE_ENV: "1"}
        herdr = shutil.which("herdr")
        if herdr:
            keys["herdr"] = herdr
        keys["github_repos"] = []
        keys["jev"] = {"model": JEV_MODEL}
        keys["policy"] = {"enabled": True}
        return keys

    def step_config(self, binary):
        path = config_path()
        raw = _read_json_object(path, "it is the switchboard's config")
        wanted = self.wanted_config(binary)
        missing = {key: value for key, value in wanted.items() if key not in raw}
        stale = {key: wanted[key] for key in self.OWNED_KEYS if key in raw and raw[key] != wanted[key]}
        if not missing and not stale:
            return self.result("config", False, str(path))
        merged = dict(raw, **missing)  # the user's keys keep their values and their order; ours follow
        merged.update(stale)
        validate_config(merged, str(path))
        if not self.dry:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(merged, indent=2) + "\n")
        changes = []
        if missing:
            changes.append("added %s" % ", ".join(missing))
        if stale:
            changes.append("updated %s" % ", ".join("%s (was %s)" % (key, raw[key]) for key in stale))
        return self.result("config", True, "%s: %s" % (path, "; ".join(changes)))

    # -- 2b: the Chief of Staff's standing orders file (PR 18)
    def step_cos_orders(self, config):
        """A template when there is no file; never overwritten, never removed."""
        path = cos_orders_path(config)
        made = install_cos_orders_template(config, self.dry)
        self.result("standing orders", made, "%s (a template: yours to edit; install never overwrites it)" % path,
                    "%s (exists: left as it is)" % path)

    # -- 3: what goes into the profile
    def gh_config_dir(self):
        """Where gh keeps its accounts: read now, because the wrapper's private XDG_CONFIG_HOME would hide it."""
        return os.environ.get("GH_CONFIG_DIR") or str(_xdg("XDG_CONFIG_HOME", ".config") / "gh")

    def wrapper_text(self, binary, config):
        common = dict(fleet_bin=shlex.quote(str(self.kit_root / "bin" / PROG)), shims=shlex.quote(str(self.kit_root / "shims")),
                      state=shlex.quote(str(state_dir_path())), kit_bin=shlex.quote(str(self.kit_root / "bin")),
                      v2=shlex.quote(binary))
        return PRIVATE_WRAPPER_TEXT.format(
            profile=shlex.quote(str(self.profile_root())), gh=shlex.quote(shutil.which("gh") or "gh"),
            gh_config=shlex.quote(self.gh_config_dir()), key_pattern=KEY_NAME_PATTERN, pane_env=PANE_ENV,
            gh_user_flag="--user %s" % shlex.quote(self.gh_user) if self.gh_user else "", **common)

    # -- 3a: the profile itself
    SHARED_NOTICE = ("--shared-profile: the ordinary OpenCode will read the files below too ({dir}). They are v2-shaped "
                     "(a plugin, a decisions directory, skills.paths, cli.json) and can stop a v1 OpenCode that reads "
                     "them from starting. Leave the flag off to keep v2's profile private.")

    def step_profile(self, config):
        if self.shared:
            where = self.config_dir(config)
            if self.asked_shared:
                self.out.write("%s\n" % self.SHARED_NOTICE.format(dir=where))
            return self.say("profile", "shared", str(where))
        root = self.profile_root()
        missing = [name for name in PROFILE_DIRS if not (root / name).is_dir()]
        if missing and not self.dry:
            for name in PROFILE_DIRS:
                (root / name).mkdir(parents=True, exist_ok=True)
            os.chmod(str(root / "state"), 0o700)
        self.result("profile", bool(missing), "%s (private to v2; %s is not touched)" % (root, self.shared_dir()),
                    "%s (private to v2; %s is not touched)" % (root, self.shared_dir()))

    def shared_dir(self):
        return _xdg("XDG_CONFIG_HOME", ".config") / "opencode"

    def piece(self, step, action, detail):
        """Run one profile piece (`action()` returns whether it changed anything). In a dry run, ask without writing."""
        changed = action()
        self.result(step, changed, detail)

    def step_pieces(self, binary, config):
        profile = self.config_dir(config)
        wrapper = self.wrapper_path()
        if self.shared:  # config points at the binary itself: nothing would run a wrapper
            self.say("wrapper", "skipped (--shared-profile: the binary runs as it is)")
        else:
            text = self.wrapper_text(binary, config)
            current = wrapper.read_text() if wrapper.exists() else None
            if not self.dry and current != text:
                write_if_changed(wrapper, text)
                os.chmod(str(wrapper), 0o755)
            self.result("wrapper", current != text, str(wrapper))
        steps = (("agents", lambda: install_agents(profile, STANDARD_MODEL, self.kit_root), "%s/agents" % profile),
                 ("skills", lambda: install_skills(self.skills_dir(), self.kit_root), str(self.skills_dir())),
                 ("skills path", lambda: ensure_skills_path(profile, self.skills_dir(), STANDARD_MODEL),
                  "%s/opencode.json" % profile),
                 ("fleet-hooks plugin", lambda: install_hooks_plugin(profile, self.kit_root),
                  "%s/plugins/%s" % (profile, HOOKS_PLUGIN_FILE)),
                 ("decisions plugin", lambda: install_decisions_plugin(profile, self.kit_root),
                  "%s/%s and cli.json" % (profile, DECISIONS_PLUGIN_NAME)),
                 ("todolist plugin", lambda: install_todolist_plugin(profile, self.kit_root),
                  "%s/%s and plugins/%s" % (profile, TODOLIST_PLUGIN_NAME, TODOLIST_LOADER_FILE)))
        for step, action, detail in steps:
            if self.dry:
                self.dry_piece(step, profile)
            else:
                self.piece(step, action, detail)

    def dry_piece(self, step, profile):
        """What a piece would change, found by running it against a copy: nothing real is written."""
        with tempfile.TemporaryDirectory() as scratch:
            shadow = Path(scratch) / "profile"
            if profile.exists():
                shutil.copytree(str(profile), str(shadow), symlinks=True)
            skills = Path(scratch) / "skills"
            if self.skills_dir().exists():
                shutil.copytree(str(self.skills_dir()), str(skills))
            actions = {"agents": lambda: install_agents(shadow, STANDARD_MODEL, self.kit_root),
                       "skills": lambda: install_skills(skills, self.kit_root),
                       "skills path": lambda: ensure_skills_path(shadow, self.skills_dir(), STANDARD_MODEL),
                       "fleet-hooks plugin": lambda: install_hooks_plugin(shadow, self.kit_root),
                       "decisions plugin": lambda: install_decisions_plugin(shadow, self.kit_root),
                       "todolist plugin": lambda: install_todolist_plugin(shadow, self.kit_root)}
            self.result(step, actions[step](), "in %s" % profile)

    # -- 3b: herdr's OpenCode integration, in the private profile
    def integration_line(self, config):
        """(the `opencode:` line of herdr's integration status read with the private HOME, the path it names)."""
        root = self.profile_root()
        line = herdr_integration_status(self.runner, self.herdr_bin(config), private_env(root))
        named = re.search(r"\((/[^)]*)\)\s*$", line)
        return line, named.group(1) if named else None

    def inside_private_home(self, path):
        return bool(path) and (str(path) + os.sep).startswith(str(self.profile_root() / "home") + os.sep)

    def step_integration(self, config):
        """herdr installs its OpenCode integration (the plugin that reports an agent's status) into
        $HOME/.config/opencode and ignores XDG_CONFIG_HOME. Run with the private HOME, whose `.config/opencode` is a
        link to the private profile, it lands there and never in the ordinary OpenCode's directory. Refused when
        herdr says it would install anywhere else."""
        if self.shared:
            return self.say("herdr integration", "skipped (--shared-profile)")
        try:
            line, path = self.integration_line(config)
        except (OSError, subprocess.SubprocessError) as err:
            return self.say("herdr integration", "skipped (herdr cannot be asked: %s)" % _one_line(str(err), 100))
        if not self.inside_private_home(path):
            raise InstallError("herdr would install its OpenCode integration outside the private profile (%s): "
                               "refusing; nothing was installed" % (line or "no `opencode:` line in `herdr integration status`"))
        if "current" in line:
            return self.result("herdr integration", False, path)
        if self.dry:
            return self.result("herdr integration", True, "`herdr integration install opencode` into %s" % path)
        ensure_home_link(self.profile_root(), self.config_dir(config))
        root = self.profile_root()
        done = self.runner([self.herdr_bin(config), "integration", "install", "opencode"], capture_output=True, text=True,
                           timeout=60, env=private_env(root), stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            raise InstallError("`herdr integration install opencode` failed: %s" % _one_line(done.stderr or done.stdout, 200))
        self.result("herdr integration", True, "installed into %s" % path)

    # -- 4: the herdr plugin
    def herdr_bin(self, config):
        return config.get("herdr") or "herdr"

    def plugin_linked(self, config):
        return plugin_report(config, self.runner)

    def step_link(self, config):
        herdr, source = self.herdr_bin(config), self.kit_root / "herdr"
        state = self.plugin_linked(config)
        if state["state"] == "linked":
            return self.say("herdr plugin", "already done", "%s is linked" % HERDR_PLUGIN_ID)
        if state["state"] == "unknown":
            raise InstallError("cannot tell whether herdr's plugin is linked (%s): is herdr installed and running? "
                               "Run again, or pass --no-herdr-link" % state["detail"])
        self.say("herdr plugin", "would run" if self.dry else "will run", "`%s plugin link %s`" % (herdr, source))
        for line in ("herdr has one server, so a linked plugin's hooks run for every pane in it, including panes that "
                     "run something else:",
                     "  startup: `%s ensure` starts the switchboard daemon unless it runs;" % (self.kit_root / "bin" / PROG),
                     "  pane created, closed, exited, agent detected or its status changed: `%s poke`, which is "
                     "silent, returns at once, and only asks the daemon for a pass;" % (self.kit_root / "bin" / PROG),
                     "  nothing is typed into any pane, and nothing is sent anywhere. `install --uninstall` unlinks it "
                     "(`%s plugin unlink %s`)." % (herdr, HERDR_PLUGIN_ID)):
            self.out.write("    %s\n" % line)
        if self.dry:
            return self.say("herdr plugin", "would be done")
        done = self.runner([herdr, "plugin", "link", str(source)], capture_output=True, text=True,
                           timeout=HERDR_CALL_TIMEOUT * 2, stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            raise InstallError("`herdr plugin link` failed: %s" % _one_line(done.stderr or done.stdout, 200))
        self.say("herdr plugin", "done", "linked %s" % source)

    # -- 4b: the CLI on PATH
    CLI_NAME = PROG

    def cli_target(self):
        return self.kit_root / "bin" / PROG

    def path_dirs_under_home(self):
        """The directories of the current PATH that are under HOME, in order."""
        home = os.path.abspath(str(Path.home())) + os.sep
        dirs = [os.path.abspath(d) for d in os.environ.get("PATH", "").split(os.pathsep) if d]
        return [Path(d) for d in dirs if (d + os.sep).startswith(home)]

    def cli_candidates(self):
        """Where the link may go, best first: ~/.local/bin when it is on PATH, then the first directory of PATH under
        HOME that can be written, then ~/.local/bin anyway (with the line that puts it on PATH)."""
        local = Path.home() / ".local" / "bin"
        on_path = self.path_dirs_under_home()
        writable = [d for d in on_path if d != local and os.access(str(d), os.W_OK)]
        return ([local] if local in on_path else []) + writable + ([] if local in on_path else [local])

    def link_state(self, link):
        """`ours` (a link to this checkout's CLI), `stale` (a link to a checkout that is gone), `other` (anything else
        there: a file, or a link to something else), or `absent`."""
        if not os.path.lexists(str(link)):
            return "absent"
        if not os.path.islink(str(link)):
            return "other"
        target = os.readlink(str(link))
        if os.path.realpath(str(link)) == os.path.realpath(str(self.cli_target())):
            return "ours"
        if not os.path.exists(str(link)) and os.path.basename(target) == PROG:
            return "stale"
        return "other"

    def step_cli_link(self):
        target = self.cli_target()
        candidates = self.cli_candidates()
        for directory in candidates:
            if self.link_state(directory / self.CLI_NAME) == "ours":
                return self.cli_done(directory, False, target)
        directory = candidates[0]
        link = directory / self.CLI_NAME
        state = self.link_state(link)
        if state == "other":
            return self.say("cli link", "skipped (%s exists and is not a link to this checkout's %s; left alone: "
                                        "agents' shells will not find `%s` until it is moved)" % (link, target, PROG))
        if not self.dry:
            directory.mkdir(parents=True, exist_ok=True)
            if state == "stale":
                link.unlink()
            os.symlink(str(target), str(link))
        self.cli_done(directory, True, target)

    def cli_done(self, directory, changed, target):
        link = directory / self.CLI_NAME
        on_path = directory in self.path_dirs_under_home() or str(directory) in os.environ.get("PATH", "").split(os.pathsep)
        self.result("cli link", changed, "%s -> %s" % (link, target))
        if on_path:
            self.out.write("    %s is on your PATH, so a shell (a Chief of Staff's too) finds `%s`.\n" % (directory, PROG))
        else:
            where = str(directory)
            home = str(Path.home())
            if where.startswith(home + os.sep):
                where = "$HOME" + where[len(home):]
            self.out.write("    %s is not on your PATH. Add this line to your shell's startup file:\n"
                           "      export PATH=\"%s:$PATH\"\n" % (directory, where))
        self.out.write("    A moved or deleted checkout breaks this link; run `%s install` again from the new one to "
                       "repair it.\n" % PROG)

    # -- 5: the service
    def service_text(self, config):
        tools = [shutil.which("gh"), self.herdr_bin(config) if os.path.isabs(self.herdr_bin(config)) else shutil.which("herdr")]
        dirs = service_path_dirs(self.kit_root, tools)
        return render_service(self.kind, service_values(self.kit_root, dirs), self.kit_root)

    def loaded(self):
        uid = os.getuid()
        if self.kind == "launchd":
            done = self.runner(["launchctl", "print", "gui/%d/%s" % (uid, SERVICE_LABEL)], capture_output=True, text=True,
                               timeout=30, stdin=subprocess.DEVNULL)
            return done.returncode == 0
        done = self.runner(["systemctl", "--user", "is-enabled", SERVICE_UNIT], capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
        return done.returncode == 0

    def run_checked(self, argv, what):
        done = self.runner(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            raise InstallError("%s failed: %s" % (what, _one_line(done.stderr or done.stdout, 200)))
        return done

    def load_commands(self, path):
        uid = os.getuid()
        if self.kind == "launchd":
            return [["launchctl", "bootstrap", "gui/%d" % uid, str(path)]]
        return [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", SERVICE_UNIT]]

    def unload_commands(self):
        uid = os.getuid()
        if self.kind == "launchd":
            return [["launchctl", "bootout", "gui/%d/%s" % (uid, SERVICE_LABEL)]]
        return [["systemctl", "--user", "disable", "--now", SERVICE_UNIT]]

    def step_service(self, config):
        path, text = service_file_path(self.kind), self.service_text(config)
        check_writable_target(path)   # before any read or any load: a symlink or a foreign file stops the install here
        on_disk = path.read_text() if path.exists() else None
        loaded = self.loaded()
        if on_disk == text and loaded:
            return self.say("service", "already done", str(path))
        what = "load" if on_disk == text else "write and load"
        commands = " && ".join(" ".join(c) for c in self.load_commands(path))
        self.say("service", "will run" if not self.dry else "would run", "%s: %s" % (what, commands))
        if self.dry:
            return self.say("service", "would be done", str(path))
        ensure_state_dir()  # launchd opens its log files in the state directory, and will not make it
        if on_disk != text:
            if loaded:  # a changed definition is read only by a fresh load
                for command in self.unload_commands():
                    self.run_checked(command, " ".join(command))
            write_if_changed(path, text)
        for command in self.load_commands(path):
            self.run_checked(command, " ".join(command))
        self.say("service", "done", str(path))

    # -- 5c: the weekly disk job installed from the old documentation snippet (#958)
    def step_janitor_job(self):
        """The old snippet ran `disk janitor --apply --weekly` after `disk prune`: the daemon owns that run now, under a recorded yes, and the command refuses
        without the reviewed fingerprint. A job file that is EXACTLY the old snippet's command is backed up (beside it, never over another backup) and has the
        janitor part cut out (the Docker prune part is the text in front of the cut, byte for byte); any other file that runs the old command is only reported.
        Nothing is reloaded: the person runs the `launchctl` line it prints."""
        if self.kind != "launchd":
            return self.say("weekly disk job", "skipped (no launchd here)")
        directory = Path.home() / "Library" / "LaunchAgents"
        found = weekly_job_scan(directory)
        if not found:
            return self.say("weekly disk job", "already done", "no job runs `disk janitor --apply`")
        for path, state, detail in found:
            if state != "old":
                self.say("weekly disk job", "skipped", "%s runs `disk janitor --apply` but does not match the old snippet exactly (%s): change its command yourself to drop "
                         "`; <program> disk janitor --apply --weekly`; nothing was changed" % (path, detail))
                continue
            text = path.read_text()
            old = OLD_JANITOR_COMMAND % {"p": detail}
            new = old[:-len(JANITOR_JOB_TAIL % {"p": detail})]
            backup = path.with_name(path.name + ".bak-%d" % int(self.now()))
            if self.dry:
                self.say("weekly disk job", "would be done", "%s: back up to %s, then drop the janitor part" % (path, backup))
                continue
            check_writable_target(path)
            if os.path.lexists(str(backup)):
                self.say("weekly disk job", "skipped", "%s exists: nothing was changed" % backup)
                continue
            shutil.copy2(str(path), str(backup))
            write_if_changed(path, text.replace(old, new))
            self.say("weekly disk job", "done", "%s rewritten (backup %s); to load it: `launchctl bootout gui/$(id -u) %s; launchctl bootstrap gui/$(id -u) %s`" % (
                path, backup, path, path))

    # -- 5b: the cloud reaper's service (opt-in, like the heartbeat's: it needs a cluster login this machine may not have)
    def reaper_command_problem(self):
        """None when the file the reaper service runs and every parent above it are the user's own and not writable by others,
        else the reason. It is the launcher's one walk (bin/path_walk.py, loaded from THIS kit's bin, which the installer already
        runs from): the walk never executes the file it judges."""
        import importlib.util
        script = Path(self.kit_root) / "cloud" / "launcher" / "cloud_worker.py"
        if not script.is_file():
            return "is missing"
        spec = importlib.util.spec_from_file_location("fleet_kit_path_walk", str(Path(__file__).resolve().parent / "path_walk.py"))
        walk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(walk)
        return walk.parents_problem(str(script)) or walk.entry_problem(os.lstat(str(script)), "the reaper program")

    def reaper_text(self, config, table_from_file=False, login_file=None):
        """The reaper's service file. `table_from_file` is the reaper-only install: the launcher's own settings file
        is then the only source of the keys it holds, so the service carries none of them."""
        settings = reaper_service_settings(os.environ) if table_from_file else reaper_settings(os.environ)
        if login_file is not None:   # the default login file: the service environment only, never the owner's settings file
            settings = dict(settings, CW_CLOUD_TOKEN_FILE=login_file)
        return render_reaper(self.kind, service_values(self.kit_root, service_path_dirs(self.kit_root, [shutil.which("python3")])),
                             settings, self.kit_root)

    def reaper_loaded(self):
        uid = os.getuid()
        if self.kind == "launchd":
            argv = ["launchctl", "print", "gui/%d/%s" % (uid, REAPER_LABEL)]
        else:
            argv = ["systemctl", "--user", "is-enabled", REAPER_UNIT]
        return self.runner(argv, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL).returncode == 0

    def reaper_load_commands(self, path):
        if self.kind == "launchd":
            return [["launchctl", "bootstrap", "gui/%d" % os.getuid(), str(path)]]
        return [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", REAPER_UNIT]]

    def reaper_unload_commands(self):
        if self.kind == "launchd":
            return [["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), REAPER_LABEL)]]
        return [["systemctl", "--user", "disable", "--now", REAPER_UNIT]]

    def step_reaper(self, config, table_from_file=False, login_file=None):
        path, text = reaper_file_path(self.kind), self.reaper_text(config, table_from_file, login_file)
        check_writable_target(path)
        problem = self.reaper_command_problem()
        if problem:
            raise InstallError("the reaper service would run %s, which %s; fix that and run again"
                               % (Path(self.kit_root) / "cloud" / "launcher" / "cloud_worker.py", problem))
        on_disk = path.read_text() if path.exists() else None
        loaded = self.reaper_loaded()
        if on_disk == text and loaded:
            set_file_mode(path, REAPER_FILE_MODE)   # an identical file still gets the private mode
            self.say("cloud reaper", "already done", str(path))
            return False
        what = "load" if on_disk == text else "write and load"
        commands = " && ".join(" ".join(c) for c in self.reaper_load_commands(path))
        self.say("cloud reaper", "will run" if not self.dry else "would run", "%s: %s" % (what, commands))
        if self.dry:
            self.say("cloud reaper", "would be done", str(path))
            return True
        ensure_state_dir()
        if on_disk != text:
            if loaded:
                for command in self.reaper_unload_commands():
                    self.run_checked(command, " ".join(command))
        write_if_changed(path, text, mode=REAPER_FILE_MODE)
        for command in self.reaper_load_commands(path):
            self.run_checked(command, " ".join(command))
        self.say("cloud reaper", "done", str(path))
        return True

    def reaper_is_ours(self, path, text):
        """True when the file at `path` is a rendering of the reaper template: the CW_* settings lines are set aside on BOTH sides and
        blank lines are collapsed on both, so the settings the file was written with do not matter."""
        def bare(body):
            if self.kind == "launchd":
                body = re.sub(r"[ \t]*<key>CW_[A-Z_]+</key>\s*<string>[^\n]*</string>[ \t]*\n?", "", body)
            else:
                body = re.sub(r'(?m)^Environment="CW_[A-Z_]+=[^\n]*"[ \t]*\n?', "", body)
            return re.sub(r"\n\s*\n+", "\n", body)
        try:
            return bare(text) == bare(render_reaper(self.kind, service_values(self.kit_root, service_path_dirs(self.kit_root, [shutil.which("python3")])), {}, self.kit_root))
        except OSError:
            return False

    def remove_reaper(self):
        """Uninstall: unload the reaper's job (it is named by this installer's label, so it is this installer's to stop, whoever
        wrote the file) and delete its file, but only a file this installer's template makes. A file that is not ours (or a symlink)
        is named and left. Silent when it never was installed."""
        path = reaper_file_path(self.kind)
        note = None
        removable = True
        if os.path.lexists(str(path)):
            try:
                check_writable_target(path)
                removable = self.reaper_is_ours(path, path.read_text())
                if not removable:
                    note = "%s is not the file this installer writes; left alone" % path
            except InstallError as err:
                removable, note = False, str(err)
        loaded = self.reaper_loaded()
        if loaded and not os.path.lexists(str(path)) and not note:
            unload = " && ".join(" ".join(c) for c in self.reaper_unload_commands())
            if not self.dry:
                for command in self.reaper_unload_commands():
                    self.run_checked(command, " ".join(command))
            return self.say("cloud reaper", "would run" if self.dry else "done", "%s: the job was loaded and has no file to remove" % unload)
        if loaded:
            self.say("cloud reaper", "would run" if self.dry else "will run", " && ".join(" ".join(c) for c in self.reaper_unload_commands()))
            if not self.dry:
                for command in self.reaper_unload_commands():
                    self.run_checked(command, " ".join(command))
        if note:
            return self.say("cloud reaper", "skipped (%s%s)" % (note, "; the job is unloaded" if loaded and not self.dry else ""))
        had = os.path.lexists(str(path))
        if had and not self.dry:
            path.unlink()
            if self.kind == "systemd":
                self.run_checked(["systemctl", "--user", "daemon-reload"], "systemctl --user daemon-reload")
        if had or loaded:
            self.result("cloud reaper", had, "%s %s" % ("would remove" if self.dry else "removed", path), "not installed")

    # -- 6: the daemon, and what status says
    def step_ensure(self, ensure, status):
        if self.dry:
            return self.say("daemon", "skipped (dry run)")
        try:
            self.say("daemon", "done" if ensure() == "started" else "already done")
        except (LockError, ConfigError, OSError) as err:
            self.say("daemon", "FAILED", _one_line(str(err), 200))
        self.out.write("\n%s\n" % status())

    def read_config(self):
        """The config as it stands now (defaults when there is no file). A file that cannot be read stops the install."""
        try:
            return load_config() if config_path().exists() else validate_config({})
        except ConfigError as err:
            raise InstallError(str(err)) from None

    def install(self, explicit_opencode=None, with_service=True, with_link=True, ensure=None, status=None, with_reaper=False):
        config = self.read_config()
        binary, version = self.find_v2(explicit_opencode, config)
        self.result("opencode v2", binary != config.get("opencode_executable"), "%s (v%s)" % (binary, version),
                    "%s (v%s)" % (binary, version))
        self.step_config(binary)
        config = self.read_config()
        self.step_cos_orders(config)
        self.step_profile(config)
        self.step_pieces(binary, config)
        if with_link:
            self.step_integration(config)
        else:
            self.say("herdr integration", "skipped (--no-herdr-link)")
        if with_link:
            self.step_link(config)
        else:
            self.say("herdr plugin", "skipped (--no-herdr-link)")
        self.step_cli_link()
        if not with_service:
            self.say("service", "skipped (--no-service)")
        elif self.kind is None:
            self.say("service", "skipped (no service manager known for this platform)")
        else:
            self.step_service(config)
            if with_reaper:
                self.step_reaper(config)
        self.step_janitor_job()
        self.step_ensure(ensure or (lambda: ensure_daemon(ensure_state_dir(), self.spawn)),
                         status or (lambda: format_status(collect_status(self.runner, self.now, self.platform))))

    # -- the reaper's service and nothing else (ask #982)
    def install_cloud_reaper_only(self):
        """Install and load ONLY the cloud worker's reaper service. Nothing else on the machine is touched: no config
        key, no profile piece, no command link, no plugin link, no switchboard service and no daemon. The state
        directory (the reaper's log) is the one other thing made, and the service file is written with mode 0600.

        The launcher's own settings file, when it exists, is the only source of the settings it holds, so the service
        carries none of them: a key set differently in the file and in the service makes every start refuse."""
        if self.kind is None:
            return self.say("cloud reaper", "skipped (no service manager known for this platform)")
        problem = reaper_settings_problem()   # before anything is written: this path only (the full install does not check)
        if problem:
            raise InstallError(problem)
        login, is_default, problem = reaper_login_file(os.environ)
        if problem:
            raise InstallError(problem)
        if is_default:
            self.out.write("cloud reaper: login file %s (default; set CW_CLOUD_TOKEN_FILE to change it)\n" % login)
            if not self.dry:
                make_reaper_login_directory(login)
        changed = self.step_reaper({}, table_from_file=True, login_file=login if is_default else None)
        self.out.write("only the cloud reaper's own service %s: the config, the profile pieces, the command link, "
                       "the plugin link, the switchboard's own service and the daemon were left alone\n"
                       % (("would be installed" if self.dry else "was installed") if changed else "is already installed"))
        self.out.write("the reaper logs into the cluster by itself, from the settings the service carries (a token file "
                       "by path, or a context) and from the settings file it reads at every start; without a login it "
                       "writes `cluster login failed: ...` and the service restarts it every 30 seconds\n")

    def uninstall_cloud_reaper_only(self):
        """The way back of install_cloud_reaper_only: unload the reaper's job and remove ONLY its service file (the
        plist or the unit). Nothing else is touched: no plugin, agent, skill, wrapper, link, service, config or profile.
        Nothing installed: one line, and success."""
        if self.kind is None:
            return self.say("cloud reaper", "skipped (no service manager known for this platform)")
        if not os.path.lexists(str(reaper_file_path(self.kind))) and not self.reaper_loaded():
            self.out.write("cloud reaper: not installed\n")
            return
        self.remove_reaper()
        self.out.write("only the cloud reaper's own service %s: the plugins, the agents, the skills, the wrapper, the command link, "
                       "the switchboard's own service, the config and the profile were left alone\n"
                       % ("would be removed" if self.dry else "was removed"))
        login = reaper_default_login_file()
        self.out.write("the login file %s and its directory were left in place (a leftover token file when the reaper has "
                       "run): to remove it, run `rm %s` and, when nothing else is in it, `rmdir %s` "
                       "(a login file you set with CW_CLOUD_TOKEN_FILE is yours to remove)\n"
                       % (login, shlex.quote(str(login)), shlex.quote(str(login.parent))))

    # -- the reverse of 3 to 5
    def uninstall(self, with_service=True, with_link=True):
        config = self.read_config()
        profile = self.config_dir(config)
        if not with_service:
            self.say("service", "skipped (--no-service)")
        elif self.kind is None:
            self.say("service", "skipped (no service manager known for this platform)")
        else:
            path = service_file_path(self.kind)
            ours = True
            if os.path.lexists(str(path)):
                try:
                    check_writable_target(path)
                    ours = PROG in path.read_text()   # the file this installer wrote names the program
                except InstallError as err:
                    ours = False
                    self.say("service", "skipped (%s)" % err)
                else:
                    if not ours:
                        self.say("service", "skipped (%s is not the file this installer writes; left alone)" % path)
            had = ours and path.exists()
            if ours and self.loaded():
                self.say("service", "would run" if self.dry else "will run", " && ".join(" ".join(c) for c in self.unload_commands()))
                if not self.dry:
                    for command in self.unload_commands():
                        self.run_checked(command, " ".join(command))
            if had and not self.dry:
                path.unlink()
                if self.kind == "systemd":
                    self.run_checked(["systemctl", "--user", "daemon-reload"], "systemctl --user daemon-reload")
            if ours:
                self.result("service", had, "%s %s" % ("would remove" if self.dry else "removed", path), "not installed")
        if self.kind is not None:
            self.remove_reaper()   # whatever --no-service says: the reaper's service is its own opt-in
        if with_link:
            state = self.plugin_linked(config)
            if state["state"] == "linked":
                self.say("herdr plugin", "will run" if not self.dry else "would run",
                         "`%s plugin unlink %s`" % (self.herdr_bin(config), HERDR_PLUGIN_ID))
                if not self.dry:
                    self.run_checked([self.herdr_bin(config), "plugin", "unlink", HERDR_PLUGIN_ID], "`herdr plugin unlink`")
                self.result("herdr plugin", True, "unlinked")
            elif state["state"] == "unknown":
                self.say("herdr plugin", "skipped (cannot tell whether it is linked: %s)" % state["detail"])
            else:
                self.result("herdr plugin", False, "not linked")
        else:
            self.say("herdr plugin", "skipped (--no-herdr-link)")
        wrapper = self.wrapper_path()
        self.uninstall_integration(config, profile, with_link)
        for step, action in (("todolist plugin", lambda: uninstall_todolist_plugin(profile, self.kit_root)),
                             ("decisions plugin", lambda: uninstall_decisions_plugin(profile, self.kit_root)),
                             ("fleet-hooks plugin", lambda: uninstall_hooks_plugin(profile, self.kit_root)),
                             ("skills path", lambda: uninstall_skills_path(profile, self.skills_dir())),
                             ("skills", lambda: uninstall_skills(self.skills_dir(), self.kit_root)),
                             ("agents", lambda: uninstall_agents(profile, STANDARD_MODEL, self.kit_root))):
            self.result(step, self.would_remove(step, profile) if self.dry else action(), "removed" , "nothing to remove")
        had = wrapper.exists()
        if had and not self.dry:
            wrapper.unlink()
        self.result("wrapper", had, "removed %s" % wrapper, "nothing to remove")
        self.uninstall_cli_link()
        self.out.write("config, state, sessions and your standing orders file are left alone; a running daemon keeps "
                       "running until it exits\n")

    def uninstall_cli_link(self):
        """Remove the links `install` makes: only a symlink that points at this checkout's CLI."""
        places = [directory / self.CLI_NAME for directory in dict.fromkeys(
            self.cli_candidates() + [Path.home() / ".local" / "bin"])]
        ours = [link for link in places if self.link_state(link) == "ours"]
        for link in ours:
            if not self.dry:
                link.unlink()
        self.result("cli link", bool(ours), "removed %s" % ", ".join(str(link) for link in ours), "nothing to remove")

    def uninstall_integration(self, config, profile, with_link):
        """Remove herdr's OpenCode integration from the private profile, and the home link install made. Only ever
        the private HOME: a status that names any other path leaves it alone."""
        if self.shared:
            return self.say("herdr integration", "skipped (--shared-profile)")
        if not with_link:
            return self.say("herdr integration", "skipped (--no-herdr-link)")
        try:
            line, path = self.integration_line(config)
        except (OSError, subprocess.SubprocessError) as err:
            self.say("herdr integration", "skipped (herdr cannot be asked: %s)" % _one_line(str(err), 100))
            path, line = None, ""
        present = self.inside_private_home(path) and "not installed" not in line and bool(line)
        if present and not self.dry:
            done = self.runner([self.herdr_bin(config), "integration", "uninstall", "opencode"], capture_output=True,
                               text=True, timeout=60, env=private_env(self.profile_root()), stdin=subprocess.DEVNULL)
            if done.returncode != 0:
                raise InstallError("`herdr integration uninstall opencode` failed: %s" % _one_line(done.stderr or done.stdout, 200))
        if path or present:
            self.result("herdr integration", present, "removed %s" % path, "nothing to remove")
        ours = home_link_is_ours(self.profile_root(), profile)
        if ours and not self.dry:
            home_link_path(self.profile_root()).unlink()
        self.result("home link", ours, "removed %s" % home_link_path(self.profile_root()), "nothing to remove")

    def would_remove(self, step, profile):
        """For a dry run of --uninstall: whether the piece is there to remove (a file the user edited is not)."""
        with tempfile.TemporaryDirectory() as scratch:
            shadow, skills = Path(scratch) / "profile", Path(scratch) / "skills"
            if profile.exists():
                shutil.copytree(str(profile), str(shadow), symlinks=True)
            if self.skills_dir().exists():
                shutil.copytree(str(self.skills_dir()), str(skills))
            actions = {"todolist plugin": lambda: uninstall_todolist_plugin(shadow, self.kit_root),
                       "decisions plugin": lambda: uninstall_decisions_plugin(shadow, self.kit_root),
                       "fleet-hooks plugin": lambda: uninstall_hooks_plugin(shadow, self.kit_root),
                       "skills path": lambda: uninstall_skills_path(shadow, self.skills_dir()),
                       "skills": lambda: uninstall_skills(skills, self.kit_root),
                       "agents": lambda: uninstall_agents(shadow, STANDARD_MODEL, self.kit_root)}
            return actions[step]()


def cmd_install(args, runner, now, spawn=subprocess.Popen):
    conflict = reaper_only_conflict(args)
    if conflict:
        sys.stderr.write("%s: install refused: %s\n" % (PROG, conflict))
        return EXIT_USAGE
    installer = Installer(runner, spawn, now, dry_run=args.dry_run, shared_profile=args.shared_profile,
                          profile=args.profile, gh_user=args.gh_user)
    try:
        if args.uninstall and args.cloud_reaper_only:
            installer.uninstall_cloud_reaper_only()
        elif args.uninstall:
            installer.uninstall(not args.no_service, not args.no_herdr_link)
        elif args.cloud_reaper_only:
            installer.install_cloud_reaper_only()
        else:
            installer.install(args.opencode, not args.no_service, not args.no_herdr_link, with_reaper=args.with_cloud_reaper)
    except InstallError as err:
        sys.stderr.write("%s: install stopped: %s\n" % (PROG, err))
        return EXIT_ERROR
    return EXIT_OK


# The options `install --cloud-reaper-only` refuses, and why each of them is about the FULL install that the new
# option does not do. One table: the completeness test in bin/test-switchboard holds every option of the install
# parser to it, so a later option must be classified here.
REAPER_ONLY_OPTION = "--cloud-reaper-only"
REAPER_ONLY_REFUSALS = {
    "--with-cloud-reaper": ("with_cloud_reaper", "the full install installs the reaper with the rest of the kit"),
    "--no-service": ("no_service", "the reaper's service is the only service this option installs"),
    "--no-herdr-link": ("no_herdr_link", "the herdr plugin link belongs to the full install"),
    "--shared-profile": ("shared_profile", "the profile pieces belong to the full install"),
    "--profile": ("profile", "the profile belongs to the full install"),
    "--opencode": ("opencode", "the OpenCode binary belongs to the full install"),
    "--gh-user": ("gh_user", "the gh account belongs to the full install"),
}


def reaper_only_conflict(args):
    """The option `--cloud-reaper-only` refuses, or None. Nothing is written and no service is called when this
    answers: the check runs in the parser, before the installer does anything."""
    if not getattr(args, "cloud_reaper_only", False):
        return None
    for option, (dest, why) in REAPER_ONLY_REFUSALS.items():
        if getattr(args, dest, None):
            return "%s cannot be used with %s: %s" % (REAPER_ONLY_OPTION, option, why)
    return None


def add_install_parsers(sub):
    install = sub.add_parser(
        "install", help="Install the switchboard for this user: config, profile pieces, the herdr plugin, a service",
        description="Idempotent. Each step says done, already done, or skipped (why). Finds an OpenCode v2 binary "
                    "(never assumes `opencode` is v2), writes the config keys that are missing, puts the kit's agents, "
                    "skills and plugins into a private v2 profile (never the directory your ordinary OpenCode reads, unless "
                    "--shared-profile), links the herdr plugin, installs a service that keeps "
                    "the daemon alive, then starts the daemon and prints `status`. No secret is written anywhere.")
    install.add_argument("--opencode", help="The OpenCode v2 binary (absolute path); default: the configured one, "
                                           "then the known places")
    install.add_argument("--no-service", dest="no_service", action="store_true", help="Do not install the service")
    install.add_argument("--with-cloud-reaper", dest="with_cloud_reaper", action="store_true",
                         help="Also install and load the cloud worker reaper as a service (opt-in: it needs a cluster login; "
                              "its non-secret CW_* settings are copied from this environment)")
    install.add_argument("--cloud-reaper-only", dest="cloud_reaper_only", action="store_true",
                         help="Install and load ONLY the cloud worker reaper's own service (opt-in: it needs a cluster login). "
                              "It writes the reaper's service file (mode 0600) and the state directory (the reaper's log) "
                              "and loads the service; it leaves alone the config, the profile pieces, the command link, "
                              "the plugin link, the switchboard's own service and the daemon. It first checks the settings file "
                              "(cloud.env: mode 600, the cluster login and vault names) and refuses with one line. "
                              "With --uninstall it removes ONLY the reaper's service")
    install.add_argument("--no-herdr-link", dest="no_herdr_link", action="store_true",
                         help="Do not link herdr's plugin (it starts the daemon whenever herdr starts)")
    install.add_argument("--shared-profile", dest="shared_profile", action="store_true",
                         help="Put v2's pieces in the directory your ordinary OpenCode reads too (%s), which can break "
                              "a v1 OpenCode that reads them. Default: a private profile" % "~/.config/opencode")
    install.add_argument("--profile", metavar="DIR", help="The private profile's directory (absolute); default: "
                                                          "$XDG_DATA_HOME/%s/profile" % PROG)
    install.add_argument("--gh-user", dest="gh_user", metavar="NAME",
                         help="The gh account the agents' token comes from (default: gh's active one)")
    install.add_argument("--dry-run", dest="dry_run", action="store_true", help="Print every step; write nothing")
    install.add_argument("--uninstall", action="store_true",
                         help="Reverse the profile pieces, the herdr link and the service; config, state and sessions stay")
    key = sub.add_parser("key", help="Save or check the decision-model key (it is a private file, never an argument)")
    keys = key.add_subparsers(dest="key_action", required=True)
    keys.add_parser("set", help="Read the key from standard input (hidden at a terminal) into a private file")
    keys.add_parser("status", help="Say where the key comes from: file, environment or none")
    sub.add_parser("restart", help="Stop the daemon by its recorded pid (only if it is a switchboard daemon), then "
                                   "start it again")
    boot = sub.add_parser("bootstrap", help="Open the Chief of Staff in its own herdr workspace, or resume it")
    boot.add_argument("--workspace", metavar="LABEL", help="The workspace's label (default: %s)" % COS_WORKSPACE_LABEL)
    boot.add_argument("--dir", help="The Chief of Staff's working directory (default: here)")
    boot.add_argument("--no-focus", dest="stay_put", action="store_true",
                      help="Open the workspace without taking focus (for scripts and trials)")
