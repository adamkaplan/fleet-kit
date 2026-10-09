// fleet-hooks: a thin OpenCode v2 plugin. It forwards each permission
// evaluation to `fleet-switchboard judge-tool` and applies the reply only
// when it is stricter than the outcome the configured rules already chose.
// It holds no policy: what is judged, and how, lives in the CLI and the
// switchboard daemon (docs/switchboard.md, "Tool-call policy (PR 9)"). The
// daemon, not v2, holds the OpenRouter key; this plugin never sees it.
//
// Two guards keep it from ever making a tool call looser or slower than the
// configured rules alone: a reply that is not stricter is ignored, and the
// child process has a hard timeout, after which the configured outcome stands.
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const OUTCOMES = ["allow", "ask", "deny"]; // least to most strict
export const DEFAULT_TIMEOUT_MS = 2500; // the CLI's own budget is 1500 ms; this is that plus slack
const REASON_LIMIT = 600;
const JUDGE_ARGV = ["judge-tool"]; // the one command this plugin runs

// The permission `action` names this plugin forwards, exactly as v2 reports
// them, mapped to the CLI's tool kinds. Measured in the Lab (v2 2.0.22): the
// shell tool's action is "shell". The other names are the expected ones and
// the Lab must confirm them; an action not listed here keeps the configured
// outcome and is never sent anywhere.
export const JUDGED_ACTIONS = {
  shell: "shell",
  edit: "edit",
  webfetch: "webfetch",
  subagent: "subagent",
  task: "subagent",
};

// Environment variables the CLI is given. It is a short allow-list on purpose:
// this plugin runs inside v2's service, whose environment every agent's shell
// tool shares, and the OpenRouter key is the daemon's, never reachable here.
export const CHILD_ENV = ["PATH", "HOME", "TMPDIR", "LANG", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"];

export function toolOf(action) {
  return Object.hasOwn(JUDGED_ACTIONS, action) ? JUDGED_ACTIONS[action] : null;
}

export function childEnv(env = process.env) {
  const out = {};
  for (const [name, value] of Object.entries(env)) {
    if (CHILD_ENV.includes(name) || name.startsWith("FLEET_SWITCHBOARD_")) out[name] = value;
  }
  return out;
}

export function isStricter(reply, configured) {
  const next = OUTCOMES.indexOf(reply && reply.outcome);
  const now = OUTCOMES.indexOf(configured);
  return next >= 0 && now >= 0 && next > now;
}

export function requestOf(input) {
  const resources = Array.isArray(input.resources) ? input.resources.filter((r) => typeof r === "string") : [];
  return {
    sessionID: input.sessionID,
    tool: toolOf(input.action),
    arguments: resources.join("\n"),
    outcome: input.effect,
  };
}

// The first complete JSON line the child prints, as an object, or null.
function parseReply(text) {
  const line = String(text).split("\n").find((l) => l.trim().startsWith("{"));
  if (!line) return null;
  try {
    const reply = JSON.parse(line);
    return reply && typeof reply === "object" && OUTCOMES.includes(reply.outcome) ? reply : null;
  } catch {
    return null;
  }
}

// Run the CLI once. Resolves with its reply, or null when it cannot answer in
// time (a timeout kills it) or at all. Never rejects.
export function runJudge(bin, request, timeoutMs, spawnFn = spawn, extraEnv = null) {
  return new Promise((resolve) => {
    let settled = false;
    let out = "";
    let child;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => {
      try { child?.kill("SIGKILL"); } catch { /* already gone */ }
      finish(null);
    }, timeoutMs);
    try {
      child = spawnFn(bin, JUDGE_ARGV, { stdio: ["pipe", "pipe", "ignore"], env: Object.assign(childEnv(), extraEnv || {}), shell: false });
      child.on("error", () => finish(null));
      child.on("close", () => finish(parseReply(out)));
      child.stdout.on("data", (chunk) => {
        out += chunk;
        const reply = out.includes("\n") ? parseReply(out) : null;
        if (reply) {
          // The reply is out; let the CLI go on warming its cache without us.
          try { child.stdout.destroy(); child.unref?.(); } catch { /* nothing to release */ }
          finish(reply);
        }
      });
      child.stdin.on("error", () => {});
      child.stdin.end(JSON.stringify(request));
    } catch {
      finish(null);
    }
  });
}

// #113: while the judge is in shadow its verdict decides nothing, so nothing should wait for it. The plugin learns
// the mode from the judge's own reply (`shadow: true`, only ever sent by a judge that is in shadow) and keeps it for
// MODE_TTL_MS. It never assumes: an unknown or expired mode, or any reply without `shadow`, means AWAIT AND ENFORCE,
// exactly as before. In shadow the call is fired detached (bounded in flight, never blocking, dropped and counted
// when over the cap) and the hook returns at once. One awaited call per TTL refreshes the mode.
export const MODE_TTL_MS = 30000;
export const SHADOW_MAX_INFLIGHT = 4;
export const SHADOW_HARD_TIMEOUT_MS = 15000;

export function fireDetached(bin, request, state, spawnFn = spawn) {
  if (state.inflight >= SHADOW_MAX_INFLIGHT) {
    state.dropped += 1;
    return false;
  }
  let child;
  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    state.inflight -= 1;
    clearTimeout(timer);
  };
  state.inflight += 1;
  const timer = setTimeout(() => {
    try { child?.kill("SIGKILL"); } catch { /* already gone */ }
    release();
  }, SHADOW_HARD_TIMEOUT_MS);
  timer.unref?.();
  try {
    const owed = state.dropped;
    const env = Object.assign(childEnv(), noticesFor(state));
    if (owed > 0) env.FLEET_SWITCHBOARD_DROPPED = String(owed);
    child = spawnFn(bin, JUDGE_ARGV, { stdio: ["pipe", "ignore", "ignore"], env, shell: false });
    if (owed > 0) state.dropped -= owed;
    child.on("error", release);
    child.on("close", release);
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(request));
    child.unref?.();
  } catch {
    release();
  }
  return true;
}

// The mode is kept PER SESSION: one agent's shadow reply never makes another agent's calls asynchronous, and a
// session with no cached mode awaits. A flip (shadow to enforcing or back) is told to the next judge-tool child in
// FLEET_SWITCHBOARD_MODE_FLIPS ("<session>:<from>><to>,..."), which audits it as `policy.mode`, so the up-to-30 s window
// is visible.
export const MAX_SESSIONS = 500;

export function noticesFor(state) {
  if (!state.flips || state.flips.length === 0) return {};
  const told = state.flips.splice(0, 20).join(",");
  return { FLEET_SWITCHBOARD_MODE_FLIPS: told };
}

function setMode(state, session, shadow, now) {
  const modes = state.modes;
  const before = modes.get(session);
  const was = before === undefined ? null : before.shadow ? "shadow" : "enforcing";
  const is = shadow ? "shadow" : "enforcing";
  if (was !== null && was !== is) state.flips.push(`${session}:${was}>${is}`);
  modes.delete(session);
  modes.set(session, { shadow, until: shadow ? now + MODE_TTL_MS : 0 });
  if (modes.size > MAX_SESSIONS) modes.delete(modes.keys().next().value);
}

export function makeHandler({ bin, timeoutMs = DEFAULT_TIMEOUT_MS, spawnFn = spawn, now = Date.now, state = null }) {
  const mode = state || {};
  mode.inflight = mode.inflight || 0;
  mode.dropped = mode.dropped || 0;
  mode.modes = mode.modes || new Map();
  mode.flips = mode.flips || [];
  return async function evaluate(input) {
    try {
      if (!input || !OUTCOMES.includes(input.effect) || typeof input.sessionID !== "string") return;
      if (toolOf(input.action) === null) return; // not an action this plugin forwards: the configured outcome stands
      const request = requestOf(input);
      const known = mode.modes.get(input.sessionID);
      if (known && known.shadow && now() < known.until) {
        fireDetached(bin, request, mode, spawnFn);
        return; // shadow: the tool call proceeds at once
      }
      const reply = await runJudge(bin, request, timeoutMs, spawnFn, noticesFor(mode));
      if (reply && reply.shadow === true) setMode(mode, input.sessionID, true, now());
      else if (reply && reply.judged === true) setMode(mode, input.sessionID, false, now()); // enforcing: never async
      if (reply && reply.shadow === true) return; // a shadow reply is the configured outcome anyway
      if (!isStricter(reply, input.effect)) return; // the second guard: never loosen, never repeat
      input.effect = reply.outcome;
      input.message = typeof reply.reason === "string" ? reply.reason.slice(0, REASON_LIMIT) : undefined;
    } catch {
      // Whatever goes wrong here, the configured outcome stands.
    }
  };
}

export function settingsOf(env = process.env) {
  const bin = env.FLEET_SWITCHBOARD_BIN;
  const given = Number(env.FLEET_HOOKS_TIMEOUT_MS);
  return {
    bin: bin && path.isAbsolute(bin) ? bin : null, // no lookup on PATH: an unset or relative path turns the plugin off
    timeoutMs: Number.isFinite(given) && given > 0 ? given : DEFAULT_TIMEOUT_MS,
  };
}

// Put the fleet's shims (a `gh` that signs what an agent says on GitHub) first on PATH of every shell
// command v2 runs. v2 triggers `shell.create.before` with a mutable spec {command, cwd, timeout, shell, env}
// and spawns with spec.env (measured in the Lab, v2 2.0.22). Nothing is parsed or rewritten: the command is
// exactly what the agent wrote, and only the environment it runs in changes.
export function shimsOf(env = process.env) {
  const dir = env.FLEET_SWITCHBOARD_SHIMS;
  return dir && path.isAbsolute(dir) ? dir : null; // an unset or relative path turns the shell hook off
}

export function makeShellHook(dir) {
  return function createBefore(spec) {
    try {
      if (!spec || typeof spec !== "object") return;
      const env = Object.assign({}, spec.env);
      const rest = String(env.PATH || "").split(path.delimiter).filter((p) => p && p !== dir);
      env.PATH = [dir, ...rest].join(path.delimiter);
      spec.env = env;
    } catch {
      // Whatever goes wrong, the command runs as it was.
    }
  };
}

// The service that runs v2 is often auto-started by a TUI, with none of the variables above. So when they are
// UNSET (an explicit value, even a bad one, always wins) the plugin finds the switchboard where `install` links it,
// ~/.local/bin/fleet-switchboard, and the shims beside the checkout that link resolves to: <checkout>/bin/..
// /shims. Nothing is looked up on PATH. `fleet-doctor` checks that this checkout is the main one.
export const DEFAULT_LINK = [".local", "bin", "fleet-switchboard"];

export function derivedPaths(home = os.homedir(), realpath = fs.realpathSync) {
  try {
    const bin = realpath(path.join(home, ...DEFAULT_LINK));
    const shims = path.join(path.dirname(path.dirname(bin)), "shims");
    return { bin, shims: realpath(shims) };
  } catch {
    return { bin: null, shims: null }; // no link, or a dangling one: the plugin stays off, as before
  }
}

export function resolveSettings(env = process.env, deps = {}) {
  const settings = settingsOf(env);
  let shims = shimsOf(env);
  const unsetBin = !env.FLEET_SWITCHBOARD_BIN;
  const unsetShims = !env.FLEET_SWITCHBOARD_SHIMS;
  if (unsetBin || unsetShims) {
    const derived = derivedPaths(deps.home, deps.realpath);
    if (unsetBin) settings.bin = derived.bin;
    if (unsetShims) shims = derived.shims;
  }
  return { ...settings, shims };
}

export default {
  id: "fleet.hooks",
  async setup(api, env = process.env, deps = {}) {
    const { bin, timeoutMs, shims } = resolveSettings(env, deps);
    if (shims && api.shell && typeof api.shell.hook === "function") api.shell.hook("create.before", makeShellHook(shims));
    if (!bin) return; // not configured: tool calls follow the configured rules alone
    api.permission.hook("evaluate", makeHandler({ bin, timeoutMs }));
  },
};
