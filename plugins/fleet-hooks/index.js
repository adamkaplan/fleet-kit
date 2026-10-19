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
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import net from "node:net";
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

// #357: an approximate tool-call rate per session for the health view. Each permission evaluation is counted in memory (one
// Map update, no I/O on the tool-call path); a timer writes `tool-rate.json` in the switchboard's state directory at most every
// TALLY_FLUSH_MS (and every TALLY_HEARTBEAT_MS even when nothing moved, so a reader can tell a quiet agent from a plugin that is not
// running). It holds names and counts only, keeps TALLY_MINUTES minutes and TALLY_SESSIONS sessions, and a fault of any kind is ignored.
export const TALLY_FLUSH_MS = 15000;
export const TALLY_HEARTBEAT_MS = 60000;
export const TALLY_MINUTES = 20;
export const TALLY_SESSIONS = 200;
export const TALLY_FILE = "tool-rate.json";

export function stateDirOf(env = process.env, home = os.homedir()) {
  const base = env.XDG_STATE_HOME && path.isAbsolute(env.XDG_STATE_HOME) ? env.XDG_STATE_HOME : path.join(home, ".local", "state");
  return path.join(base, "fleet-switchboard");
}

export function makeTally({ dir, now = Date.now, writeFile = fs.promises.writeFile, rename = fs.promises.rename, unlink = fs.promises.unlink }) {
  const sessions = new Map(); // session -> Map(minute epoch -> count)
  let dirty = false;
  let lastWrite = 0;
  let writing = false;
  const prune = (minute) => {
    for (const [session, minutes] of sessions) {
      for (const m of minutes.keys()) if (m <= minute - TALLY_MINUTES) minutes.delete(m);
      if (minutes.size === 0) sessions.delete(session);
    }
  };
  const tally = {
    count(session) {
      try {
        const minute = Math.floor(now() / 60000);
        let minutes = sessions.get(session);
        if (!minutes) {
          if (sessions.size >= TALLY_SESSIONS) sessions.delete(sessions.keys().next().value);
          minutes = new Map();
          sessions.set(session, minutes);
        }
        minutes.set(minute, (minutes.get(minute) || 0) + 1);
        dirty = true;
      } catch { /* never in the way of a tool call */ }
    },
    body() {
      const t = now();
      prune(Math.floor(t / 60000));
      const out = {};
      for (const [session, minutes] of sessions) out[session] = Object.fromEntries(minutes);
      return { written_at: Math.floor(t / 1000), sessions: out };
    },
    async flush() {
      const t = now();
      if (writing || !dir || (!dirty && t - lastWrite < TALLY_HEARTBEAT_MS)) return false;
      writing = true;
      const file = path.join(dir, TALLY_FILE);
      const tmp = `${file}.${process.pid}.${randomBytes(6).toString("hex")}.tmp`; // a name nobody can predict, created exclusively
      try {
        await writeFile(tmp, JSON.stringify(tally.body()), { mode: 0o600, flag: "wx" });
        await rename(tmp, file);
        dirty = false;
        lastWrite = t;
        return true;
      } catch {
        try { await unlink(tmp); } catch { /* best effort: nothing to remove */ }
        return false; // the state directory may not exist yet: the view says "not recorded"
      } finally {
        writing = false;
      }
    },
  };
  return tally;
}

// #290 slice 6: tell the daemon that something happened in a session, so it reads that one session once instead of polling. One short
// JSON line per event on the daemon's own socket (mode 0600, $STATE/judge.sock), written with node `net` (no child process). The message
// holds a version, a kind, one of a few event names, a session id, a time and at most a message id, a person flag and a count of events
// dropped before it: NEVER prompt text, never anything from the environment. It is fire and forget: the caller returns at once, a bounded
// queue and a bounded number of open connections hold the rest, and a full queue or a daemon that is not there drops the event and counts it.
export const EVENT_SOCKET = "judge.sock";
export const EVENT_QUEUE_MAX = 20;
export const EVENT_INFLIGHT_MAX = 4;
export const EVENT_CONNECT_MS = 500;
export const EVENT_NAMES = ["prompt", "compaction", "idle", "activity"];
const SESSION_RE = /^ses_[A-Za-z0-9]{1,80}$/;
const MESSAGE_RE = /^msg_[A-Za-z0-9]{1,80}$/;

export function makeEventSender({ dir, now = Date.now, connect = net.createConnection, setTimer = setTimeout } = {}) {
  const queue = [];
  let inflight = 0;
  let dropped = 0; // events lost since the last message that carried the count
  const socketPath = dir ? path.join(dir, EVENT_SOCKET) : null;
  const pump = () => {
    while (inflight < EVENT_INFLIGHT_MAX && queue.length > 0) {
      const message = queue.shift();
      if (dropped > 0) { message.dropped = dropped; }
      const owed = message.dropped || 0;
      if (owed > 0) dropped -= owed;
      inflight += 1;
      let done = false;
      let socket = null;
      const finish = (ok) => {
        if (done) return;
        done = true;
        inflight -= 1;
        if (!ok) dropped += 1 + owed; // this event and the count it carried are lost: both are told to the next one
        try { socket && socket.destroy(); } catch { /* nothing to close */ }
        if (queue.length > 0) setImmediate(pump);
      };
      try {
        socket = connect(socketPath);
        const timer = setTimer(() => finish(false), EVENT_CONNECT_MS);
        timer.unref?.();
        socket.on("error", () => finish(false));
        socket.on("connect", () => { socket.end(JSON.stringify(message) + "\n", () => finish(true)); });
      } catch {
        finish(false);
      }
    }
  };
  return {
    // Never throws and never waits.
    push(event, sessionID, extra = {}) {
      try {
        if (!socketPath || !EVENT_NAMES.includes(event) || typeof sessionID !== "string" || !SESSION_RE.test(sessionID)) return false;
        if (queue.length >= EVENT_QUEUE_MAX) { dropped += 1; return false; }
        const message = { v: 1, kind: "event", event, sessionID, at: now() };
        if (typeof extra.person === "boolean") message.person = extra.person;
        if (typeof extra.id === "string" && MESSAGE_RE.test(extra.id)) message.id = extra.id;
        queue.push(message);
        setImmediate(pump);
        return true;
      } catch {
        return false;
      }
    },
    dropped: () => dropped,
  };
}

// The in-process facts that name a session. UNVERIFIED until the plugin is loaded in the service: the hook and event names below were read
// from the service binary and from another plugin, not exercised. A name or shape that does not match pushes nothing (the daemon's
// `session.list` gate stays the signal); a wrong guess can never push more than the four events above.
export function sessionOf(event) {
  const id = event && (event.sessionID ?? event.data?.sessionID ?? event.durable?.aggregateID);
  return typeof id === "string" ? id : null;
}

export function eventOf(event) {
  const type = event && event.type;
  if (type === "session.idle") return { name: "idle" };
  if (type === "message.created" && (event.data?.role === "user" || event.data?.message?.role === "user")) {
    const id = event.data?.id ?? event.data?.messageID ?? event.data?.message?.id;
    return { name: "prompt", id: typeof id === "string" ? id : undefined };
  }
  return null;
}

export function wireEvents(api, sender) {
  if (api.session && typeof api.session.hook === "function") {
    api.session.hook("compaction", (event) => { sender.push("compaction", sessionOf(event)); });
  }
  if (api.event && typeof api.event.subscribe === "function") {
    (async () => {
      try {
        for await (const event of api.event.subscribe({})) {
          const found = eventOf(event);
          if (found) sender.push(found.name, sessionOf(event), { id: found.id });
        }
      } catch { /* the subscription ended: the gate is the signal */ }
    })();
  }
}

export function makeHandler({ bin, timeoutMs = DEFAULT_TIMEOUT_MS, spawnFn = spawn, now = Date.now, state = null, tally = null }) {
  const mode = state || {};
  mode.inflight = mode.inflight || 0;
  mode.dropped = mode.dropped || 0;
  mode.modes = mode.modes || new Map();
  mode.flips = mode.flips || [];
  return async function evaluate(input) {
    try {
      if (!input || !OUTCOMES.includes(input.effect) || typeof input.sessionID !== "string") return;
      if (tally) { try { tally.count(input.sessionID); } catch { /* a counter fault never touches judging */ } } // every evaluation, judged or not: an approximate tool-call count
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

// #846: a launched HEADLESS worker (no pane, so no pane environment) gets its own private TMPDIR here, the same design as the pane's (#767):
// <profile>/tmp/<agent name>, mode 0700, the name validated by the same pattern, never the shared directory by fallback in silence. The session is
// told from the launch record the switchboard writes for a headless worker (state-dir/headless/<name>.json: {worker, session, kind: "local"}), read
// here the way agent_presence.py reads it (a real directory of ours, no symlink, a private regular file, 8 KB at most). The session id is the
// OPENCODE_SESSION_ID the service sets in the tool's environment (the id the gh shim already goes by): UNVERIFIED in the shell spec until the
// plugin is loaded. A session with no record (a terminal worker, the Chief of Staff, a person) gets no change.
export const AGENT_NAME_RE = /^[a-z][a-z0-9_-]{0,31}$/; // herdr's pattern, as NAME_RE in bin/fleet-switchboard
const RECORD_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/; // agent_presence.valid_name
const RECORD_MAX = 8192;
const MAX_RECORDS = 200;
const NEGATIVE_TTL_MS = 10000; // a session with no record is looked up again after this, so a command costs no scan
const LANE_PREFIX = "planrev-"; // the plan review lanes and the Chief of Staff keep the shared directory (agent_tmp_shared_name)

export function dataDirOf(env = process.env, home = os.homedir()) {
  const base = env.XDG_DATA_HOME && path.isAbsolute(env.XDG_DATA_HOME) ? env.XDG_DATA_HOME : path.join(home, ".local", "share");
  return path.join(base, "fleet-switchboard");
}

export function tmpRootOf(env = process.env, home = os.homedir()) {
  return path.join(dataDirOf(env, home), "profile", "tmp");
}

function oneLine(text) {
  return String(text).replace(/[\u0000-\u001f\u007f-\u009f]/g, " ").slice(0, 200);
}

// The agent's private directory, made now (0700) under root; returns its path. Throws when the name is not valid or the path is not our own
// directory. Never recursive below the root, never follows a symlink at the path.
export function ensureAgentTmp(root, name, fsm = fs) {
  if (typeof name !== "string" || !AGENT_NAME_RE.test(name) || name === "cos" || name.startsWith(LANE_PREFIX)) throw new Error("not a valid agent name");
  const dir = path.join(root, name);
  if (path.dirname(dir) !== root) throw new Error("outside the temp root");
  fsm.mkdirSync(root, { recursive: true });
  try {
    fsm.mkdirSync(dir, { mode: 0o700 });
  } catch (err) {
    if (!err || err.code !== "EEXIST") throw err;
  }
  const info = fsm.lstatSync(dir);
  if (!info.isDirectory() || info.uid !== process.getuid()) throw new Error("exists and is not a directory of ours");
  const fd = fsm.openSync(dir, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW); // no symlink swapped in after the check
  try {
    fsm.fchmodSync(fd, 0o700);
  } finally {
    fsm.closeSync(fd);
  }
  return dir;
}

function readRecord(base, file, fsm) {
  const name = file.slice(0, -5);
  const fd = fsm.openSync(path.join(base, file), fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const info = fsm.fstatSync(fd);
    if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077) !== 0 || info.size > RECORD_MAX) return null;
    const buf = Buffer.alloc(info.size);
    fsm.readSync(fd, buf, 0, info.size, 0);
    const record = JSON.parse(buf.toString("utf8"));
    if (!record || record.kind !== "local" || record.worker !== name || typeof record.session !== "string") return null;
    return record.session;
  } finally {
    fsm.closeSync(fd);
  }
}

// The launched headless worker's name for a session id, or null. Reads only.
export function headlessNameOf(stateDir, sessionID, fsm = fs) {
  if (typeof sessionID !== "string" || !SESSION_RE.test(sessionID)) return null;
  const base = path.join(stateDir, "headless");
  let info;
  try {
    info = fsm.lstatSync(base);
  } catch {
    return null; // no headless worker was ever launched
  }
  if (info.isSymbolicLink() || !info.isDirectory() || info.uid !== process.getuid()) return null;
  const files = fsm.readdirSync(base).filter((f) => f.endsWith(".json") && RECORD_NAME_RE.test(f.slice(0, -5))).sort().slice(0, MAX_RECORDS);
  for (const file of files) {
    try {
      if (readRecord(base, file, fsm) === sessionID) return file.slice(0, -5);
    } catch {
      // one unreadable record is not the session's
    }
  }
  return null;
}

// (env) -> the private TMPDIR for the session named in env, or null for "no change". Never throws: a directory that cannot be made says so once on
// stderr (per session and path) and the command runs on the shared directory.
export function makeTmpResolver({ stateDir, root, now = Date.now, fsm = fs, warn = (line) => process.stderr.write(line + "\n") }) {
  const known = new Map(); // session -> name (the record's file and the directory are re-checked on every use)
  const absent = new Map(); // session -> time until which "no record" stands
  const said = new Set();
  return function tmpFor(env) {
    try {
      if (!env || typeof env !== "object" || env.FLEET_AGENT_TMPDIR) return null; // a terminal worker has its own since #767
      const session = env.OPENCODE_SESSION_ID;
      if (typeof session !== "string" || !SESSION_RE.test(session)) return null;
      let name = known.get(session);
      if (name !== undefined) {
        try {
          fsm.lstatSync(path.join(stateDir, "headless", name + ".json")); // a retired worker's record is gone: its directory is not made again
        } catch {
          known.delete(session);
          name = undefined;
          absent.set(session, now() + NEGATIVE_TTL_MS);
          return null;
        }
      }
      if (name === undefined) {
        if (now() < (absent.get(session) || 0)) return null;
        name = headlessNameOf(stateDir, session, fsm);
        if (name === null) {
          absent.set(session, now() + NEGATIVE_TTL_MS);
          if (absent.size > 500) absent.delete(absent.keys().next().value);
          return null;
        }
        if (known.size >= 500) known.delete(known.keys().next().value);
        known.set(session, name);
      }
      if (name === "cos" || name.startsWith(LANE_PREFIX)) return null;
      try {
        return ensureAgentTmp(root, name, fsm);
      } catch (err) {
        const key = session + "|" + name;
        if (!said.has(key)) {
          said.add(key);
          try {
            warn(`fleet-hooks: no private TMPDIR for ${oneLine(name)} (${oneLine(err && (err.code || err.message) || "error")}): its commands run on the shared temp directory`);
          } catch {
            // stderr gone: nothing to say it on
          }
        }
        return null;
      }
    } catch {
      return null; // the command runs as it was
    }
  };
}

export function makeShellHook(dir, tmpFor = null) {
  return function createBefore(spec) {
    try {
      if (!spec || typeof spec !== "object") return;
      const env = Object.assign({}, spec.env);
      const rest = String(env.PATH || "").split(path.delimiter).filter((p) => p && p !== dir);
      env.PATH = [dir, ...rest].join(path.delimiter);
      if (tmpFor) {
        const tmp = tmpFor(spec.env);
        if (tmp) env.TMPDIR = tmp;
      }
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
    if (env.FLEET_SWITCHBOARD_PLUGIN_EVENTS !== "off") {
      const sender = deps.sender === undefined ? makeEventSender({ dir: stateDirOf(env, deps.home) }) : deps.sender;
      if (sender) wireEvents(api, sender); // each part is wired only if the service offers it; never any effect on a tool call
    }
    if (shims && api.shell && typeof api.shell.hook === "function") {
      const tmpFor = deps.tmpFor === undefined ? makeTmpResolver({ stateDir: stateDirOf(env, deps.home), root: tmpRootOf(env, deps.home) }) : deps.tmpFor;
      api.shell.hook("create.before", makeShellHook(shims, tmpFor));
    }
    if (!bin) return; // not configured: tool calls follow the configured rules alone (and none is counted)
    const tally = deps.tally === undefined ? makeTally({ dir: stateDirOf(env, deps.home) }) : deps.tally;
    if (tally) {
      const timer = setInterval(() => { tally.flush().catch(() => {}); }, TALLY_FLUSH_MS);
      timer.unref?.();
    }
    api.permission.hook("evaluate", makeHandler({ bin, timeoutMs, tally }));
  },
};
