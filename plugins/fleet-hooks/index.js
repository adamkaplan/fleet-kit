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
import path from "node:path";

export const OUTCOMES = ["allow", "ask", "deny"]; // least to most strict
export const DEFAULT_TIMEOUT_MS = 2500; // the CLI's own budget is 1500 ms; this is that plus slack
const REASON_LIMIT = 600;

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
export function runJudge(bin, request, timeoutMs, spawnFn = spawn) {
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
      child = spawnFn(bin, ["judge-tool"], { stdio: ["pipe", "pipe", "ignore"], env: childEnv(), shell: false });
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

export function makeHandler({ bin, timeoutMs = DEFAULT_TIMEOUT_MS, spawnFn = spawn }) {
  return async function evaluate(input) {
    try {
      if (!input || !OUTCOMES.includes(input.effect) || typeof input.sessionID !== "string") return;
      if (toolOf(input.action) === null) return; // not an action this plugin forwards: the configured outcome stands
      const reply = await runJudge(bin, requestOf(input), timeoutMs, spawnFn);
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

export default {
  id: "fleet.hooks",
  async setup(api, env = process.env) {
    const { bin, timeoutMs } = settingsOf(env);
    const shims = shimsOf(env);
    if (shims && api.shell && typeof api.shell.hook === "function") api.shell.hook("create.before", makeShellHook(shims));
    if (!bin) return; // not configured: tool calls follow the configured rules alone
    api.permission.hook("evaluate", makeHandler({ bin, timeoutMs }));
  },
};
