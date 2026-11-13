#!/usr/bin/env node
// versions.mjs: every command the tool runs uses the PROJECT'S pinned tool versions, never the host's defaults.
//
//   The pins are read from the project root: .node-version, .nvmrc, mise.toml / .mise.toml ([tools]), .tool-versions, package.json (volta.node, engines.node).
//   Each pinned tool is looked up among the versions installed on this host (mise's installs: $MISE_DATA_DIR or ~/.local/share/mise, installs/<tool>/<version>;
//   for node also the directories of PATH, each asked for its real version); the highest installed version that satisfies EVERY pin of the tool is put first
//   in PATH for every child process. A pin no installed version satisfies REFUSES, with the tool, the pin, what is installed and how to install it: there is
//   no fallback to the host's default, ever. A tool the project pins nothing for runs as the host has it (the startup line says so).
//
//   Every child process of the tool is started through the `spawn`, `spawnSync` and `execFileSync` exported here (versions.test.mjs fails on a raw
//   child_process import anywhere else): they put the pinned directories first in the child's own PATH, whatever env the caller gives, and they
//   resolve the project's pins first if nobody has. Shell entries source versions.sh (`spire_versions ROOT`), which runs this file's `env` command.
//
//   node versions.mjs env ROOT              shell text that pins the current shell (export PATH=...); the startup line goes to stderr
//   node versions.mjs exec ROOT -- CMD...   run CMD under the pins (exit status = CMD's): `versions.mjs exec . -- node --test FILE` runs a test as CI does
//   node versions.mjs show ROOT             print the startup line only
// Exit: 0 | 1 refused (the reason on stderr) | 2 usage.
import * as cp from "node:child_process";
import {
  accessSync,
  constants,
  existsSync,
  readdirSync,
  readFileSync,
  realpathSync,
  statSync,
} from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** A pin the host cannot honour: the message is the plain reason, printed as it is. */
export class PinError extends Error {}

const isVersion = (s) => /^v?\d+(\.\d+){0,2}$/.test(s);
const strip = (v) => v.replace(/^v/, "");
const cmp = (a, b) => {
  const x = strip(a).split(".").map(Number);
  const y = strip(b).split(".").map(Number);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const d = (x[i] ?? 0) - (y[i] ?? 0);
    if (d) {
      return d;
    }
  }
  return 0;
};

// one comparator of a range: [op]X[.Y[.Z]] with x, X or * as a wildcard
const COMPARATOR =
  /^(>=|<=|>|<|=|\^|~)?v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?$/;
function comparator(version, c) {
  const m = COMPARATOR.exec(c);
  if (!m) {
    throw new PinError(`"${c}" is not a version or a version range`);
  }
  const op = m[1] ?? "";
  const given = []; // the numeric leading parts: 22.x gives [22]
  for (const p of [m[2], m[3], m[4]]) {
    if (p === undefined || !/^\d+$/.test(p)) {
      break;
    }
    given.push(Number(p));
  }
  if (!given.length) {
    return true; // * or x: any
  }
  const full = [0, 1, 2].map((i) => given[i] ?? 0).join(".");
  const bumped = (n) => {
    // the version just past the n-th part (0 = major)
    const p = [0, 1, 2].map((i) => given[i] ?? 0);
    p[n] += 1;
    for (let i = n + 1; i < 3; i++) {
      p[i] = 0;
    }
    return p.join(".");
  };
  const last = given.length - 1;
  switch (op) {
    case "":
    case "=":
      return cmp(version, full) >= 0 && cmp(version, bumped(last)) < 0;
    case ">=":
      return cmp(version, full) >= 0;
    case "<":
      return cmp(version, full) < 0;
    case ">":
      return cmp(version, bumped(last)) >= 0;
    case "<=":
      return cmp(version, bumped(last)) < 0;
    case "~":
      return (
        cmp(version, full) >= 0 && cmp(version, bumped(given.length > 1 ? 1 : 0)) < 0
      );
    default: {
      // ^: nothing past the first non-zero part
      const at = given[0] > 0 ? 0 : given[1] > 0 || given.length < 3 ? 1 : 2;
      return cmp(version, full) >= 0 && cmp(version, bumped(at)) < 0;
    }
  }
}

/** Does this exact version (22.23.4) satisfy a pin: an exact version, a prefix (22, 22.23) or a range (>=22 <23 || ^24)? Throws PinError for text that is neither. */
export function satisfies(version, spec) {
  const s = spec.trim();
  if (isVersion(s)) {
    const want = strip(s).split(".");
    return (
      strip(version).split(".").slice(0, want.length).join(".") === want.join(".")
    );
  }
  return s.split("||").some((set) =>
    set
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .every((c) => comparator(strip(version), c)),
  );
}

const firstLine = (f) =>
  readFileSync(f, "utf8").split("\n")[0].replace(/#.*/, "").trim();

/** The project's pins: [{tool, spec, source}] in order of precedence. */
export function readPins(root) {
  const pins = [];
  const add = (tool, spec, source) => {
    const s = String(spec ?? "").trim();
    if (s) {
      pins.push({ tool, spec: s, source });
    }
  };
  for (const f of [".node-version", ".nvmrc"]) {
    if (existsSync(join(root, f))) {
      add("node", firstLine(join(root, f)), f);
    }
  }
  for (const f of ["mise.toml", ".mise.toml"]) {
    if (!existsSync(join(root, f))) {
      continue;
    }
    let inTools = false;
    for (const raw of readFileSync(join(root, f), "utf8").split("\n")) {
      const line = raw.replace(/\s#.*$/, "").trim();
      const sec = /^\[([^\]]+)\]$/.exec(line);
      if (sec) {
        inTools = sec[1].trim() === "tools";
        continue;
      }
      const kv = inTools && /^["']?([A-Za-z0-9_.-]+)["']?\s*=\s*(.+)$/.exec(line);
      if (!kv) {
        continue;
      }
      // "22.1" | ["22.1", "20"] | { version = "22.1" }: the first version named
      const v = /["']([^"']+)["']/.exec(kv[2]);
      add(kv[1], v ? v[1] : kv[2], f);
    }
  }
  if (existsSync(join(root, ".tool-versions"))) {
    for (const raw of readFileSync(join(root, ".tool-versions"), "utf8").split("\n")) {
      const [tool, spec] = raw.replace(/#.*/, "").trim().split(/\s+/);
      if (tool && spec) {
        add(tool, spec, ".tool-versions");
      }
    }
  }
  if (existsSync(join(root, "package.json"))) {
    let pkg = {};
    try {
      pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
    } catch {
      // not ours to judge
    }
    add("node", pkg.volta?.node, "package.json volta.node");
    add("node", pkg.engines?.node, "package.json engines.node");
  }
  return pins;
}

const miseInstalls = (env) =>
  join(
    env.MISE_DATA_DIR ??
      join(env.XDG_DATA_HOME ?? join(env.HOME ?? homedir(), ".local/share"), "mise"),
    "installs",
  );

const isExec = (p) => {
  try {
    accessSync(p, constants.X_OK);
    return statSync(p).isFile();
  } catch {
    return false;
  }
};
const isShims = (d) => /(^|\/)shims\/?$/.test(d); // a shim picks its version by the directory it runs in: not a version

/** The real version a node binary reports, or "" when it does not run. */
function nodeVersionOf(bin) {
  const r = cp.spawnSync(bin, ["--version"], { encoding: "utf8", timeout: 20_000 });
  const v = r.status === 0 ? strip(r.stdout.trim()) : "";
  return /^\d+\.\d+\.\d+/.test(v) ? v : "";
}

/** The versions of a tool installed on this host, highest first: [{version, binDir, via}], one per real bin directory. */
export function installed(tool, env = process.env) {
  const out = [];
  const seen = new Set();
  const take = (dir, via, named) => {
    let binDir;
    try {
      const real = realpathSync(dir);
      binDir = existsSync(join(real, "bin")) ? join(real, "bin") : real;
    } catch {
      return;
    }
    if (seen.has(binDir)) {
      return;
    }
    seen.add(binDir);
    // node: the binary's own word, not its directory's name
    const version = tool === "node" ? nodeVersionOf(join(binDir, "node")) : named;
    if (/^v?\d+(\.\d+)*$/.test(version)) {
      out.push({ version: strip(version), binDir, via });
    }
  };
  const base = join(miseInstalls(env), tool);
  if (/^[A-Za-z0-9_-]+$/.test(tool) && existsSync(base)) {
    for (const d of readdirSync(base)) {
      if (/^v?\d/.test(d)) {
        take(join(base, d), "mise", d);
      }
    }
  }
  if (tool === "node") {
    for (const d of (env.PATH ?? "").split(":").filter(Boolean)) {
      if (!isShims(d) && isExec(join(d, "node"))) {
        take(d, "PATH");
      }
    }
  }
  // The directories an earlier resolution pinned (SPIRE_PINNED_BIN, in step with SPIRE_PINNED "tool@version") stay candidates: the PATH strip must not drop a
  // tool that lives only there (node in /usr/local/bin on an engineer image). They are shared deliberately, across projects: a candidate still has to be
  // RUN and report the version (node: its own --version; another tool: the first version `TOOL --version` prints must equal the record), so a stale or forged pair is dropped, and each pin
  // is still matched by satisfies() like any other candidate.
  const prevBins = (env.SPIRE_PINNED_BIN ?? "").split(":").filter(Boolean);
  (env.SPIRE_PINNED ?? "")
    .split(" ")
    .filter(Boolean)
    .forEach((tv, i) => {
      const [t, v] = tv.split("@");
      if (t !== tool || !prevBins[i] || !isVersion(v ?? "")) {
        return;
      }
      if (tool === "node") {
        take(prevBins[i], "PATH");
      } else {
        // the version is READ from the probe (the first number of its output) and must equal the record
        const r = cp.spawnSync(join(prevBins[i], tool), ["--version"], { encoding: "utf8", timeout: 20_000 });
        const probed = /\d+(\.\d+)+/.exec(`${r.stdout}${r.stderr}`)?.[0];
        if (r.status === 0 && probed === strip(v)) {
          take(prevBins[i], "PATH", probed);
        }
      }
    });
  return out.sort((a, b) => cmp(b.version, a.version));
}

const hostDefault = (env) => {
  const d = (env.PATH ?? "")
    .split(":")
    .find((x) => x && !isShims(x) && isExec(join(x, "node")));
  return d ? nodeVersionOf(join(d, "node")) : "";
};

/** Resolve the project's pins against this host: {root, tools: [{tool, pin, source, version, binDir, via}], bins}. Throws PinError with the plain reason. */
export function resolvePins(root, env = process.env) {
  const byTool = new Map();
  for (const p of readPins(root)) {
    byTool.set(p.tool, [...(byTool.get(p.tool) ?? []), p]);
  }
  const tools = [];
  for (const [tool, pins] of byTool) {
    for (const p of pins) {
      try {
        satisfies("0.0.0", p.spec);
      } catch (e) {
        throw new PinError(
          `${tool}: the project pins "${p.spec}" (${p.source}), which is not a version or a range (${e.message}). Pin a version, like ${tool}@22.23.4, in ${p.source}: spire-land never chooses one itself.`,
        );
      }
    }
    const primary = pins.find((p) => isVersion(p.spec)) ?? pins[0];
    const have = installed(tool, env);
    const list = have.length
      ? have.map((h) => `${h.version}${h.via === "PATH" ? " (PATH)" : ""}`).join(", ")
      : "none";
    const hint = isVersion(primary.spec)
      ? `mise install ${tool}@${strip(primary.spec)}`
      : `mise install ${tool}@<a version satisfying ${primary.spec}>`;
    const first = have.filter((h) => satisfies(h.version, primary.spec));
    const note = `${primary.spec} (${primary.source})`;
    if (!first.length) {
      const dflt = tool === "node" ? hostDefault(env) : "";
      throw new PinError(
        `${tool}: the project pins ${note} and this host has no such version. Installed: ${list}. Install it: ${hint}. ` +
          `Refusing to run: spire-land never falls back to the host default${dflt ? ` (node ${dflt} on PATH)` : ""}.`,
      );
    }
    const all = first.filter((h) => pins.every((p) => satisfies(h.version, p.spec)));
    if (!all.length) {
      const bad = pins.find((p) => !satisfies(first[0].version, p.spec));
      throw new PinError(
        `${tool}: the project's pins disagree: ${note} is met by ${first.map((h) => h.version).join(", ")}, but ${bad.spec} (${bad.source}) is met by none of them. Make the pins agree, then install it: ${hint}.`,
      );
    }
    tools.push({ tool, pin: primary.spec, source: primary.source, ...all[0] });
  }
  return { root, tools, bins: tools.map((t) => t.binDir) };
}

/** The startup line: the versions every command of this run uses. */
export function startupLine(r) {
  if (!r.tools.length) {
    return "spire-land: pins: none declared by the project (tools as the host has them)";
  }
  return `spire-land: pins: ${r.tools
    .map((t) => `${t.tool} ${t.version} (pin ${t.pin} in ${t.source}${t.via === "PATH" ? ", from PATH" : ""})`)
    .join("; ")}`;
}

const dedupe = (path, bins) =>
  [...bins, ...path.split(":").filter((d) => d && !bins.includes(d))].join(":");
/** PATH without the directories an earlier resolution put first: pinning another project starts from the host's own PATH; see installed() for the carried directories. */
const unpinned = (env) => {
  const old = (env.SPIRE_PINNED_BIN ?? "").split(":").filter(Boolean);
  return (env.PATH ?? "").split(":").filter((d) => !old.includes(d)).join(":");
};

/** The variables that carry a resolution to every child process (and tell a nested entry it is already pinned). */
const marks = (r) => ({
  SPIRE_PINNED_ROOT: r.root,
  SPIRE_PINNED_BIN: r.bins.join(":"),
  SPIRE_PINNED: r.tools.map((t) => `${t.tool}@${t.version}`).join(" "),
});

/** `env` with the pinned directories first in its PATH. Resolves the project's pins first if nobody has (a refusal throws PinError). */
export function pinnedEnv(env = process.env) {
  if (process.env.SPIRE_PINNED_ROOT === undefined) {
    applyPins(undefined, { reexec: false, quiet: true, onRefuse: "throw" });
  }
  const bins = (process.env.SPIRE_PINNED_BIN ?? "").split(":").filter(Boolean);
  return {
    ...env,
    ...(bins.length ? { PATH: dedupe(env.PATH ?? process.env.PATH ?? "", bins) } : {}),
    SPIRE_PINNED_ROOT: process.env.SPIRE_PINNED_ROOT,
    SPIRE_PINNED_BIN: process.env.SPIRE_PINNED_BIN ?? "",
    SPIRE_PINNED: process.env.SPIRE_PINNED ?? "",
  };
}

const gitTop = (cwd) => {
  const r = cp.spawnSync("git", ["rev-parse", "--show-toplevel"], { cwd, encoding: "utf8" });
  return r.status === 0 ? r.stdout.trim() : cwd;
};

/**
 * Pin THIS process and every child it starts. Resolves the project's pins (root: the one given, else the one already in force, else the git
 * top of the current directory), puts them first in process.env.PATH and prints the startup line on stderr (once per root). A process that
 * runs another node than the pinned one is re-run under the pinned node (reexec) and exits with its status. A pin with no match prints the
 * plain reason and exits 1 (onRefuse "throw": throws the PinError instead).
 */
export function applyPins(
  root,
  { reexec = true, quiet = false, onRefuse = "exit", log = (l) => process.stderr.write(`${l}\n`) } = {},
) {
  const env = process.env;
  let r;
  if (root === undefined && env.SPIRE_PINNED_ROOT !== undefined) {
    r = {
      root: env.SPIRE_PINNED_ROOT,
      tools: (env.SPIRE_PINNED ?? "")
        .split(" ")
        .filter(Boolean)
        .map((s) => ({ tool: s.split("@")[0], version: s.split("@")[1] })),
      bins: (env.SPIRE_PINNED_BIN ?? "").split(":").filter(Boolean),
    };
  } else {
    const top = resolve(root ?? gitTop(process.cwd()));
    try {
      r = resolvePins(top, { ...env, PATH: unpinned(env) });
    } catch (e) {
      if (!(e instanceof PinError) || onRefuse === "throw") {
        throw e;
      }
      process.stderr.write(`spire-land: ${e.message}\n`);
      process.exit(1);
    }
    const again = env.SPIRE_PINNED_ROOT === top;
    env.PATH = unpinned(env);
    Object.assign(env, marks(r));
    env.PATH = dedupe(env.PATH, r.bins);
    if (!quiet && !again) {
      log(startupLine(r));
    }
  }
  const node = r.tools.find((t) => t.tool === "node");
  if (reexec && node && process.version !== `v${node.version}` && !env.SPIRE_REEXEC) {
    const c = cp.spawnSync(
      join(r.bins[r.tools.indexOf(node)], "node"),
      [...process.execArgv, ...process.argv.slice(1)],
      { stdio: "inherit", env: { ...env, SPIRE_REEXEC: "1" } },
    );
    process.exit(c.status ?? 1);
  }
  return r;
}

/** Child processes: the calls of node:child_process, started with the project's pins first in the child's PATH. */
export const spawn = (cmd, args, opts = {}) =>
  cp.spawn(cmd, args, { ...opts, env: pinnedEnv(opts.env) });
export const spawnSync = (cmd, args, opts = {}) =>
  cp.spawnSync(cmd, args, { ...opts, env: pinnedEnv(opts.env) });
export const execFileSync = (cmd, args, opts = {}) =>
  cp.execFileSync(cmd, args, { ...opts, env: pinnedEnv(opts.env) });

const shq = (s) => `'${String(s).replaceAll("'", `'\\''`)}'`;

function main(argv) {
  const [cmd, rootArg, ...rest] = argv;
  if (!["env", "exec", "show"].includes(cmd) || !rootArg) {
    process.stderr.write("usage: versions.mjs env ROOT | exec ROOT -- CMD... | show ROOT\n");
    return 2;
  }
  const root = resolve(rootArg);
  try {
    if (cmd === "env" && process.env.SPIRE_PINNED_ROOT === root) {
      return 0; // this shell is already pinned for this project: nothing to change, nothing to say twice
    }
    const r = resolvePins(root, { ...process.env, PATH: unpinned(process.env) });
    if (cmd === "show") {
      process.stdout.write(`${startupLine(r)}\n`);
      return 0;
    }
    process.stderr.write(`${startupLine(r)}\n`);
    const env = { ...process.env, PATH: unpinned(process.env), ...marks(r) };
    env.PATH = dedupe(env.PATH, r.bins);
    if (cmd === "env") {
      const keys = ["PATH", ...Object.keys(marks(r))];
      process.stdout.write(`${keys.map((k) => `export ${k}=${shq(env[k] ?? "")}`).join("\n")}\n`);
      return 0;
    }
    const run = rest.slice(rest[0] === "--" ? 1 : 0);
    if (!run.length) {
      process.stderr.write("usage: versions.mjs exec ROOT -- CMD...\n");
      return 2;
    }
    const c = cp.spawnSync(run[0], run.slice(1), { stdio: "inherit", env });
    return c.status ?? (c.error ? 127 : 1);
  } catch (e) {
    if (e instanceof PinError) {
      process.stderr.write(`spire-land: ${e.message}\n`);
      return 1;
    }
    throw e;
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  process.exitCode = main(process.argv.slice(2));
}
