"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): process_and_ci."""
import ast
import collections
import io
import json
import os
import re
import zipfile
import subprocess
import threading
import time
from support.paths import BIN
from support.fake_hubs import sweep_issue
from support.loader import sb, switchboard_source
from support.paths import BIN


def s5_issue(number, body="", labels=(), pr=False, draft=None, **kw):
    """The shared sweep_issue plus a draft flag, kept out of the shared fake (#1044)."""
    record = sweep_issue(number, body, labels, pr=pr, **kw)
    if draft is not None:
        record["draft"] = draft
    return record


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def parse_plugin_manifest(text):
    """Just enough TOML for herdr/herdr-plugin.toml: `key = "string"`, `key = ["a", "b"]`,
    and `[[table]]` headers. Anything else raises, so the file cannot drift
    past what this reads."""
    manifest, table = {}, None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        header = re.match(r"^\[\[([a-z]+)\]\]$", line)
        if header:
            table = {}
            manifest.setdefault(header.group(1), []).append(table)
            continue
        pair = re.match(r'^([a-z_]+) = (.+)$', line)
        if not pair:
            raise ValueError("line %d is not understood: %r" % (number, raw))
        try:
            value = json.loads(pair.group(2))  # these strings and arrays are valid JSON too
        except ValueError:
            value = None
        if not (isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value))):
            raise ValueError("line %d: value is not a string or a list of strings: %r" % (number, raw))
        (table if table is not None else manifest)[pair.group(1)] = value
    return manifest


def classified_in_audit(entries, agent, text, choice):
    """True when the audit's last classification of that agent's message `text` chose `choice`."""
    found = [e for e in entries if e.get("event") == "classify"
             and (e.get("detail") or {}).get("agent") == agent
             and text in ((e["detail"].get("question") or {}).get("state") or {}).get("message", "")]
    got = found[-1]["detail"]["answer"]["choice"] if found else None
    return got == choice, "%d classification(s) of the message; the last chose %r" % (len(found), got)


def apply_edit_intent(run, step):
    """`edit-intent`: edit the fake issue, then do what PR 5's webhook handler
    will do: invalidate the cache, and turn the event into an item for the
    Chief of Staff and a toast for you, with the pure functions under test."""
    world = run.world
    fields = {name: step[name] for name in ("intent", "done_when") if name in step}
    old, new = run.ask_gh.edit(step["issue"], **fields)
    world.intents.invalidate(step["issue"])
    payload = {"action": "edited", "changes": {"body": {"from": old}}, "sender": {"login": "you"},
               "issue": {"number": step["issue"], "body": new, "updated_at": run.ask_gh.updated[step["issue"]]}}
    cos = next(name for name, member in sorted(run.scenario["cast"].items()) if member["role"] == "chief-of-staff")
    change = sb.intent_change_fact(payload, cos)
    if change is not None:
        with open(str(world.state / sb.LAB_FACTS), "a") as handle:
            handle.write(json.dumps(change.to_json()) + "\n")
    toast = sb.intent_change_toast(payload)
    if toast is not None:
        with world.engine.audit.external("daemon", "herdr.notification", "you", toast):
            world.engine.herdr.notification_show(toast["title"], toast["body"])


def apply_handoff_step(run, step):
    """PR 8, `handoff`: the cast member runs `fleet-switchboard handoff` for an ask, as the
    Chief of Staff's model would. The brief is posted on the fake GitHub, and the subagent is
    launched (into the fakes) with the cast member as the one it reports to."""
    world = run.world
    sb.handoff_ask(world.new_engine("cli"), step["actor"], step["issue"], step["brief"],
                   directory=str(run.work), workspace="w1", patience=5,
                   sleep=lambda seconds: setattr(world.clock, "t", world.clock.t + seconds))


SHELL_HOOK_CASES = r"""
const sep = ":";
const hook = hooks.makeShellHook("/kit/shims");
const spec = (env) => ({ command: "gh issue comment 2 --body hi", cwd: "/w", timeout: 0, shell: "/bin/sh", env });
let a = spec({ PATH: "/usr/bin:/bin", KEEP: "1" }); hook(a);
results["prepended"] = { path: a.env.PATH, keep: a.env.KEEP, command: a.command };
let b = spec({ PATH: "/usr/bin:/kit/shims:/bin" }); hook(b); hook(b);
results["once-only"] = b.env.PATH;
let c = spec({}); hook(c);
results["no-path"] = c.env.PATH;
let d = spec(undefined); hook(d);
results["no-env"] = d.env.PATH;
const before = { PATH: "/x" }; let e = spec(before); hook(e);
results["input-env-untouched"] = before.PATH;
let junk = [undefined, null, 5, "x"]; for (const j of junk) hook(j);
results["junk-ok"] = true;
let frozen = Object.freeze(spec({ PATH: "/x" })); hook(frozen);
results["frozen-ok"] = frozen.env.PATH;
results["shimsOf"] = [hooks.shimsOf({ FLEET_SWITCHBOARD_SHIMS: "/abs/shims" }), hooks.shimsOf({ FLEET_SWITCHBOARD_SHIMS: "rel/shims" }), hooks.shimsOf({})];
const reg = []; const noReg = [];
await plugin.setup({ shell: { hook: (n, f) => reg.push([n, typeof f]) }, permission: { hook: () => {} } }, { FLEET_SWITCHBOARD_SHIMS: "/abs/shims" });
await plugin.setup({ shell: { hook: (n) => noReg.push(n) } }, {});
await plugin.setup({ shell: { hook: (n) => noReg.push(n) } }, { FLEET_SWITCHBOARD_SHIMS: "relative" });
await plugin.setup({ permission: { hook: () => {} } }, { FLEET_SWITCHBOARD_SHIMS: "/abs/shims", FLEET_SWITCHBOARD_BIN: "/abs/x" }); // an api without a shell service
results["registration"] = { reg, noReg };
"""


USE_COMMANDS = {
    "send": ["send", "other", "hello"], "report": ["report", "done", "finished"], "launch": ["launch", "a"],
    "handoff": ["handoff", "--issue", "1", "--brief-file", "-"], "comment": ["comment", "--issue", "1", "--body-file", "-"],
    "remind": ["remind", "other", "1h", "later"], "decisions": ["decisions"], "intent": ["intent", "1"],
    "bootstrap": ["bootstrap"], "notice": ["notice", "pr", "acme/api#7", "--from", "coder"],
    "move": ["move", "1", "--to", "2", "--from", "coder"], "reassign": ["reassign", "coder", "--undo", "--from", "coder"],
    "charter": ["charter", "set", "coder", "1", "--from", "coder"],
    "resume": ["resume", "other"],
}


EXEC_CHILD = r"""
import importlib.machinery, importlib.util, os, sys, time
loader = importlib.machinery.SourceFileLoader("sbx", sys.argv[1])
spec = importlib.util.spec_from_loader("sbx", loader)
sbx = importlib.util.module_from_spec(spec)
loader.exec_module(sbx)
state, phase = sys.argv[2], sys.argv[3]
with sbx.single_instance(state):
    if phase == "first":
        open(os.path.join(state, "first.pid"), "w").write(str(os.getpid()))
        sbx.reexec_in_place([sys.executable, __file__, sys.argv[1], state, "second"])
        sys.exit(9)
    open(os.path.join(state, "second.pid"), "w").write(str(os.getpid()))
    deadline = time.time() + 20
    while not os.path.exists(os.path.join(state, "stop")) and time.time() < deadline:
        time.sleep(0.05)
"""


def light_guard_sites():
    """Every READ of `engine.light` in bin/fleet-switchboard, derived from the code: (qualified function, ordinal in it) -> (first line, last line)."""
    tree, found, counts = ast.parse(switchboard_source()), {}, collections.Counter()

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            inner = stack + [child.name] if isinstance(child, (ast.FunctionDef, ast.ClassDef)) else stack
            asked = isinstance(child, ast.Attribute) and child.attr == "light" and isinstance(child.ctx, ast.Load)
            asked = asked or (isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "getattr" and len(child.args) >= 2
                              and isinstance(child.args[1], ast.Constant) and child.args[1].value == "light")
            if asked:
                owner = ".".join(stack)
                counts[owner] += 1
                found[(owner, counts[owner])] = (child.lineno, child.end_lineno)
            walk(child, inner)
    walk(tree, [])
    return found


def watch_parent(getppid=os.getppid, exit_fn=os._exit, pause=time.sleep, every=2.0, rounds=None):
    """A daemon thread that ends this run (status 143) when its parent process is gone (the parent id changes, to 1 or to a subreaper), so no run outlives the shell
    that started it. Returns the thread, or None when the parent was already init (a container's pid 1: nothing to compare with). `rounds` bounds the loop (tests)."""
    start = getppid()
    if start == 1:
        return None

    def watch():
        left = rounds
        while left is None or left > 0:
            if getppid() != start:
                exit_fn(143)
                return
            pause(every)
            left = None if left is None else left - 1
    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread


def ci_run(workflow, name, conclusion, completed=None, started=None, status="COMPLETED"):
    """One entry of gh's statusCheckRollup, as `gh pr view --json statusCheckRollup` writes a check run."""
    entry = {"workflowName": workflow, "name": name, "status": status, "conclusion": conclusion,
             "startedAt": started or completed or "2026-10-15T15:00:00Z", "completedAt": completed or "0001-01-01T00:00:00Z"}
    return entry


def _zip_with(member, data):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(member, data)
    return stream.getvalue()


# THE RULE: an old token appears ONLY in the registry module (bin/registry.py, which folds and derives them) and in this DATED list of (file, function, token).
# Each entry names who removes it and by when; the test fails on an entry that no longer matches (shrink it) and on one past its date. The scan covers every
# tracked file that is not a test or a document: Python by AST (aliases and constant concatenations resolved), anything else by text.
OLD_SOURCE_ALLOWED = {
    ("bin/fleet-switchboard", "adopt_before_close", "RETIRED_FILE"): "2026-12-20, last slice of #903 (the adoption code owns it): only the key of the strict-record table, the read itself is the registry's",
    ("bin/fleet-switchboard", "adopt_workers", "RETIRED_FILE"): "2026-12-20, last slice of #903 (the adoption code owns it): only the key of the strict-record table, the read itself is the registry's",
    ("bin/fleet-switchboard", "repoint_workers", "RETIRED_FILE"): "2026-12-20, last slice of #903 (the adoption code owns it): only the key of the strict-record table, the read itself is the registry's",
    ("bin/fleet-switchboard", "write_boss_records", "RETIRED_FILE"): "2026-12-20, last slice of #903 (the adoption code owns it): only the key of the strict-record table, the read itself is the registry's",
    ("bin/fleet-switchboard", "<module>", "PANE_HEALTH_FILE"): "2026-12-20, last slice of #903 (slice 2 owns it): the constant of the derived file",
    ("bin/fleet-switchboard", "<module>", "RETIRED_FILE"): "2026-12-20, last slice of #903 (slice 2 owns it): the constant of the derived file",
    ("bin/fleet-switchboard", "read_pane_health", "PANE_HEALTH_FILE"): "2026-12-20, last slice of #903 (slice 2 owns it): the derived-file reader",
    ("bin/fleet-switchboard", "write_pane_health", "PANE_HEALTH_FILE"): "2026-12-20, last slice of #903 (slice 2 owns it): the derived-file writer the sidebar reads",
    ("bin/fleet-switchboard", "<module>", "pane-health.json"): "2026-12-20, last slice of #903 (slice 2 owns it): the file name; the derived output goes with the old files",
    ("bin/fleet-switchboard", "<module>", "retired.json"): "2026-12-20, last slice of #903 (slice 2 owns it): the file name; the derived output goes with the old files",
    ("bin/fleet-switchboard", "_reattach_locked", "pane-health.json"): "2026-12-20, last slice of #903 (slice 2 owns it): reattach's message about the derived file",
    ("bin/fleet-switchboard", "_reattach_locked", "read_pane_health"): "2026-12-20, last slice of #903 (slice 2 owns it): reattach reads the pane record it rewrites (derived)",
    ("bin/fleet-switchboard", "cmd_retire", "read_pane_health"): "2026-12-20, last slice of #903 (slice 2 owns it): retire prunes the derived pane record it rewrites",
    ("bin/fleet-switchboard", "pane_health_pass", "pane-health.json"): "2026-12-20, last slice of #903 (slice 2 owns it): the pane pass writes the derived file",
    ("bin/fleet-switchboard", "pane_health_pass", "read_pane_health"): "2026-12-20, last slice of #903 (slice 2 owns it): the pane pass reads the pane states it wrote (derived)",
    ("bin/fleet-switchboard", "janitor_agent_roots", "PANE_HEALTH_FILE"): "2026-12-20, last slice of #903 (the temp janitor, #936, owns it): it reads the live agents' working directories from the derived pane record",
    ("bin/fleet-switchboard", "janitor_agent_roots", "RETIRED_FILE"): "2026-12-20, last slice of #903 (the temp janitor, #936, owns it): only the name in its unreadable-record message, the read itself is read_retired_strict (the registry's)",
}


def tracked_files():
    """Every file git tracks (the repository's own list, never a directory walk), as repository-relative paths."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=str(BIN.parent), capture_output=True, check=True).stdout.decode()
    return [p for p in out.split("\0") if p and os.path.isfile(str(BIN.parent / p)) and not os.path.islink(str(BIN.parent / p))]


def load_ci_runs():
    return sb.load_module("ci_runs")
