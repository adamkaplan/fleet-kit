"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): installer_tables."""
from pathlib import Path
from support.paths import BIN, REPO
from support.delivery_measure import REPO
from support.loader import sb


WORKER_STEPS = ("away", "permission", "close-pane", "delete-state", "timer-heartbeat")


# Every field of the reaper's two templates: field -> what it must hold. A field added to a template needs a row.
REAPER_PLIST_FIELDS = {
    "Label": lambda v, ctx: v == "com.fleet-kit.cloud-reaper",
    "ProgramArguments": lambda v, ctx: v[1:3] == ["-B", str(REPO / "cloud/launcher/cloud_worker.py")] and v[3:] == ["reap", "--loop", str(sb.REAPER_INTERVAL_SECONDS)],
    "EnvironmentVariables": lambda v, ctx: v["HOME"] == str(Path.home()) and "/usr/bin" in v["PATH"].split(":"),
    "RunAtLoad": lambda v, ctx: v is True,
    "KeepAlive": lambda v, ctx: v is True,
    "ThrottleInterval": lambda v, ctx: isinstance(v, int) and v >= 10,
    "StandardErrorPath": lambda v, ctx: v == str(sb.state_dir_path() / "cloud-reaper-stderr.log"),
    "StandardOutPath": lambda v, ctx: v == str(sb.state_dir_path() / "cloud-reaper-stdout.log"),
    "ProcessType": lambda v, ctx: v == "Background",
    "WorkingDirectory": lambda v, ctx: v == str(Path.home()),
}


REAPER_UNIT_FIELDS = {
    ("Unit", "Description"): lambda v: "reaper" in v,
    ("Service", "Type"): lambda v: v == "simple",
    ("Service", "ExecStart"): lambda v: v.endswith("cloud/launcher/cloud_worker.py\" reap --loop %d" % sb.REAPER_INTERVAL_SECONDS),
    ("Service", "WorkingDirectory"): lambda v: v == str(Path.home()),
    ("Service", "Environment"): lambda v: "=" in v,
    ("Service", "Restart"): lambda v: v == "always",
    ("Service", "RestartSec"): lambda v: int(v) >= 10,
    ("Service", "StandardOutput"): lambda v: v == "journal",
    ("Service", "StandardError"): lambda v: v == "journal",
    ("Install", "WantedBy"): lambda v: v == "default.target",
}


# Every step of the installer, and what the reaper-only path does with it. A step added to the installer must be
# added here: the completeness test below fails when one has no row.
REAPER_ONLY_STEPS = {
    "step_config": "not called: the config file is the full install's",
    "step_cos_orders": "not called: the standing orders file is the full install's",
    "step_profile": "not called: the profile is the full install's",
    "step_pieces": "not called: the profile pieces are the full install's",
    "step_integration": "not called: herdr's integration is the full install's",
    "step_link": "not called: the herdr plugin link is the full install's",
    "step_cli_link": "not called: the command link is the full install's",
    "step_service": "not called: the switchboard's own service is not installed here",
    "step_janitor_job": "not called: the weekly disk job belongs to the full install",
    "step_ensure": "not called: the daemon is not started here",
    "step_installed_tree": "not called: the installed tree's record, guard scripts and hooks belong to the full install",
    "step_reaper": "called: the reaper's own step, the one thing this path does",
}


# Every module-level function of bin/fleet-switchboard that the installer's steps call, and what it does. A function
# the installer starts to call must be classified here: the completeness test fails when one has no row.
INSTALLER_READERS = ("_one_line", "_read_json_object", "_xdg", "check_writable_target", "collect_status", "config_path",
                     "cos_orders_path", "fault", "format_status", "herdr_integration_status", "home_link_is_ours",
                     "home_link_path", "installed_guard_module", "load_config", "plugin_report", "private_env", "reaper_default_login_file", "reaper_file_path", "reaper_login_file", "reaper_settings_problem",
                     "reaper_service_settings", "reaper_settings", "render_reaper", "render_service", "service_file_path",
                     "service_kind", "service_path_dirs", "service_values", "set_file_mode", "state_dir_path",
                     "v2_version_of", "validate_config", "weekly_job_scan")


INSTALLER_WRITERS = ("ensure_home_link", "ensure_skills_path", "ensure_state_dir", "install_agents",
                     "install_cos_orders_template", "install_decisions_plugin", "install_hooks_plugin", "install_skills",
                     "install_todolist_plugin", "make_reaper_login_directory", "uninstall_agents", "uninstall_decisions_plugin", "uninstall_hooks_plugin",
                     "uninstall_skills", "uninstall_skills_path", "uninstall_todolist_plugin", "write_if_changed")


INSTALLER_STARTERS = ("ensure_daemon",)


# The writers the reaper-only path may call: the reaper's own service file (through write_if_changed) and the state
# directory it logs into. Every other writer, and the daemon, must stay untouched.
REAPER_ONLY_WRITERS = ("ensure_state_dir", "write_if_changed")


# The six things the reaper-only install leaves alone: the help and the quickstart must each name every one of them.
REAPER_ONLY_LEAVE_ALONE = (r"\bconfig\b", r"profile pieces", r"command link", r"plugin link",
                            r"switchboard'?s (own )?service", r"\bdaemon\b")


# The files the full install writes, and the mode each one has (the umask of the run is 022): the mode argument of
# write_if_changed is for the reaper's file alone, and every other writer keeps the mode it always had.
INSTALLER_FILE_MODES = {
    "xdg-config/fleet-switchboard/config.json": 0o644,
    "xdg-data/fleet-switchboard/profile/config/opencode/agents/coder.md": 0o644,
    "xdg-data/fleet-switchboard/profile/config/opencode/cli.json": 0o644,
    "xdg-data/fleet-switchboard/profile/config/opencode/opencode.json": 0o644,
    "xdg-data/fleet-switchboard/profile/config/opencode/plugins/fleet-hooks.js": 0o644,
    "xdg-data/fleet-switchboard/skills/fleet-review/SKILL.md": 0o644,
    "xdg-data/fleet-switchboard/bin/fleet-opencode": 0o755,
}


_CG = sb.load_cpu_guard()


ORIGINAL_MEMORY_READING, ORIGINAL_PROCESS_TABLE = _CG.memory_reading, _CG.process_table   # the real readers, kept before a test patches them


# #943: a work item reads its parent ask's Done-when, and a pull request's `CLAIMS:` line narrows what its proof gate counts.
PARENT_RULE = ("A work item with no Done-when of its own takes the Done-when of its parent ask (its native sub-issue parent): `plan check` and the structure gate (G0a) read it there, "
               "and a Done-when the work item carries itself must still match exactly.")
