// workers.mjs: how many parallel test workers an area may start on THIS host, and the memory ceiling a landing runs under, so a landing cannot take its host down.
//   workers = max(1, min(core share, free memory / the area's per-worker budget))
// The budget is the project's one setting, `checks.workerBudgetMB` of land.json: a number (every area) or {"<area>": number, ...} (an area it does not name gets DEFAULT_BUDGET_MB).
// The landing runs inside a systemd user scope with `MemoryMax` = HOST_SHARE of the memory free when it started, where the host can (a Linux host with a user manager that delegates the
// memory controller); where it cannot, the worker count alone is the cap, and the group's plan line says which. The free memory is the host's, or less where the process's cgroup holds a memory.max; the report line says which MemoryMax (if any) is in force. A run measures what the workers peaked at
// (resident memory of the step's process tree, sampled) and reports it in the group's peak line, so a project can set its budget from a measurement. Every reader is a parameter: the tests give it fake cores, memory and /proc.
import { readdirSync, readFileSync } from "node:fs";
import { availableParallelism, freemem } from "node:os";
import { spawnSync } from "./versions.mjs";

export const DEFAULT_BUDGET_MB = 1024;
export const HOST_SHARE = 0.9; // of the free memory at the start: the landing's MemoryMax
const MB = 1024 * 1024;
const GB = 1024 * MB;
const gb = (bytes) => (bytes >= GB ? `${Math.round((bytes / GB) * 10) / 10} GB` : `${Math.round(bytes / MB)} MB`);

/** The per-worker memory budget of an area, in bytes: the project's `checks.workerBudgetMB` (a number, or a map by area name), else DEFAULT_BUDGET_MB. A value that is not a positive number is an error, never a fallback. */
export function budgetBytes(checks, area) {
  const set = checks?.workerBudgetMB;
  const byArea = typeof set === "object" && set !== null;
  const mb = (byArea ? set[area] : set) === undefined ? DEFAULT_BUDGET_MB : byArea ? set[area] : set;
  if (typeof mb !== "number" || !Number.isFinite(mb) || mb <= 0) {
    throw new Error(`checks.workerBudgetMB${byArea ? `.${area}` : ""} must be a positive number of megabytes, not ${JSON.stringify(mb)}`);
  }
  return Math.round(mb * MB);
}

/** The cap: never below 1, never above the core share, never above what the free memory holds at the budget per worker. */
export function workerCap({ coreShare, freeBytes, budgetBytes: budget }) {
  const byCores = Math.max(1, Math.floor(coreShare));
  const byMemory = Math.floor(freeBytes / budget);
  return { workers: Math.max(1, Math.min(byCores, byMemory)), byCores, byMemory };
}

const realFs = { read: (p) => { try { return readFileSync(p, "utf8"); } catch { return undefined; } } };

/** The workers of ONE test group when `groups` test groups run at the same time: the cores and the free memory are each shared in groups + 1 (one share per group, one for the other steps: typecheck, build, lint). */
export function groupCap({ cores, groups, freeBytes, budgetBytes: budget }) {
  return workerCap({ coreShare: Math.max(2, Math.floor(cores / (groups + 1))), freeBytes: freeBytes / (groups + 1), budgetBytes: budget });
}

const NO_LIMIT = 2 ** 60; // cgroup v1 spells "no limit" as a huge number (9223372036854771712): anything this large is no limit

/** Which cgroup this process is in (read once): {version, mem, cpuVersion, cpu} or undefined (the host). A v1 "memory" controller line wins over a "0::" line (a hybrid host keeps memory in v1); else v2 = the "0::/path" line. `cpu` is the path of the v1 "cpu" controller line when there is one (cpuVersion "v1"), else the "0::" path (cpuVersion "v2"). */
function cgroupOwn(fs) {
  const table = (fs.read("/proc/self/cgroup") ?? "").split("\n").map((l) => l.split(":"));
  const v1 = (ctl) => table.find((f) => f.length >= 3 && f[1].split(",").includes(ctl))?.[2];
  const v2 = table.find((f) => f[0] === "0" && f[1] === "")?.slice(2).join(":");
  const [mem, cpu1] = [v1("memory"), v1("cpu")];
  const cpu = cpu1 ?? v2;
  if (mem === undefined && v2 === undefined && cpu === undefined) return undefined;
  return { version: mem !== undefined ? "v1" : "v2", mem: mem ?? v2, cpuVersion: cpu1 !== undefined ? "v1" : "v2", cpu };
}

/** Walk DIR and the cgroups above it, calling visit(dir) for each. */
function up(dir, visit) {
  for (let d = dir; ; d = d.slice(0, d.lastIndexOf("/"))) {
    visit(d);
    if (!d) break;
  }
}

/** The memory limits that hold this process: for its cgroup and each one above it that has a numeric limit, {max, used} in bytes ("max", a v1 huge number and a missing file are no limit and skipped). The one reader of the cgroup memory files. */
function cgroupLimits(fs) {
  const own = cgroupOwn(fs);
  const limits = [];
  if (!own) return limits;
  const [maxFile, usedFile, root] = own.version === "v2" ? ["memory.max", "memory.current", "/sys/fs/cgroup"] : ["memory.limit_in_bytes", "memory.usage_in_bytes", "/sys/fs/cgroup/memory"];
  up(own.mem, (dir) => {
    const max = Number(fs.read(`${root}${dir}/${maxFile}`));
    const used = Number(fs.read(`${root}${dir}/${usedFile}`));
    if (Number.isFinite(max) && max < NO_LIMIT) limits.push({ max, used }); // used: NaN when unreadable: the limit still names the ceiling, but leaves no headroom figure
  });
  return limits;
}

/** The CPU cores this process may use: the host's `host`, or less under the tightest CPU quota of its cgroup and the ones above it (v2 cpu.max "QUOTA PERIOD"; v1 cpu.cfs_quota_us / cpu.cfs_period_us under cpu or cpu,cpuacct, -1 = none; "max", -1 and a quota that is not below the host are no limit; never below 1). {cores, source}: "cgroup v1"/"cgroup v2" when a quota set it, else "host". */
export function cpuCores({ host = availableParallelism(), fs = realFs } = {}) {
  const own = cgroupOwn(fs);
  let best = host, source = "host";
  if (own?.cpu === undefined) return { cores: best, source };
  const take = (quota, period) => {
    if (!(quota > 0) || !(period > 0)) return;
    const c = Math.max(1, Math.floor(quota / period));
    if (c < best) { best = c; source = `cgroup ${own.cpuVersion}`; }
  };
  if (own.cpuVersion === "v1") {
    for (const root of ["/sys/fs/cgroup/cpu,cpuacct", "/sys/fs/cgroup/cpu"]) up(own.cpu, (dir) => take(Number(fs.read(`${root}${dir}/cpu.cfs_quota_us`)), Number(fs.read(`${root}${dir}/cpu.cfs_period_us`))));
  } else {
    up(own.cpu, (dir) => { const [q, p] = (fs.read(`/sys/fs/cgroup${dir}/cpu.max`) ?? "").trim().split(/\s+/); take(Number(q), Number(p ?? 100000)); });
  }
  return { cores: best, source };
}

/** Which source set the limits: "cgroup v2" / "cgroup v1" when this process's cgroup holds a memory limit of that version, else "host". */
export function limitSource({ fs = realFs } = {}) {
  const own = cgroupOwn(fs);
  return own && cgroupLimits(fs).length ? `cgroup ${own.version}` : "host";
}

/** The MemoryMax in force on this process, in bytes: the tightest one of its cgroup and the ones above it (0: none). The environment has no say. `fs` = {read(path) -> string|undefined}. */
export function memoryMaxBytes({ fs = realFs } = {}) {
  const maxes = cgroupLimits(fs).map((l) => l.max);
  return maxes.length ? Math.min(...maxes) : 0;
}

/** The memory this process may still use: the host's free memory, or less when its cgroup (or one above it) has a memory.max. */
export function availableBytes({ free = freemem(), fs = realFs } = {}) {
  return cgroupLimits(fs).filter((l) => Number.isFinite(l.used)).reduce((left, l) => Math.min(left, Math.max(0, l.max - l.used)), free);
}

/** The landing's own ceiling, in whole MB: HOST_SHARE of what is available. */
export const hostShareBytes = (available) => Math.floor((available * HOST_SHARE) / MB) * MB;

/** The command that runs ARGV in a systemd user scope with MemoryMax (no swap: a ceiling that swaps is a slow host, not a safe one). */
export const scopeCommand = (bytes, argv) => ["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", `MemoryMax=${bytes}`, "-p", "MemorySwapMax=0", "--", ...argv];

/** Can this host hold a landing under a MemoryMax? Asks it: a scope is made and its own memory.max read back (a user manager that does not delegate the memory controller starts the scope and ignores the limit). */
export function scopeWorks(bytes, run = spawnSync) {
  const [cmd, ...args] = scopeCommand(bytes, ["sh", "-c", 'cat "/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)/memory.max"']);
  const r = run(cmd, args, { encoding: "utf8", timeout: 20000 });
  return r.status === 0 && String(r.stdout).trim() === String(bytes);
}

/** The resident memory of PID and all its descendants, in bytes (Linux /proc; 0 where there is none). `proc` = {dirs() -> pids, read(pid, file) -> string|undefined}. */
export function treeRssBytes(pid, proc = { dirs: () => readdirSync("/proc").filter((d) => /^\d+$/.test(d)).map(Number), read: (p, f) => { try { return readFileSync(`/proc/${p}/${f}`, "utf8"); } catch { return undefined; } } }, pageSize = 4096) {
  let pids;
  try {
    pids = proc.dirs();
  } catch {
    return 0; // no /proc (macOS): nothing is measured, and the report says so
  }
  const parent = new Map();
  for (const p of pids) {
    const stat = proc.read(p, "stat");
    if (stat) parent.set(p, Number(stat.slice(stat.lastIndexOf(")") + 2).split(" ")[1])); // the fields after "(comm)": state, ppid, ...
  }
  const family = new Set([pid]);
  for (let grew = true; grew; ) {
    grew = false;
    for (const [p, pp] of parent) if (family.has(pp) && !family.has(p)) { family.add(p); grew = true; }
  }
  let pages = 0;
  for (const p of family) pages += Number((proc.read(p, "statm") ?? "").split(" ")[1]) || 0;
  return pages * pageSize;
}

/** Follow a child's memory: peak() is the largest tree size seen so far; stop() ends the sampling. */
export function watchPeak(pid, every = 500, measure = treeRssBytes) {
  let peak = 0;
  const sample = () => { peak = Math.max(peak, measure(pid)); };
  const timer = setInterval(sample, every);
  timer.unref();
  sample();
  return { peak: () => peak, stop: () => { sample(); clearInterval(timer); } };
}

/** The line of a test group whose workers the tool cannot cap (the project names no `checks.testConcurrencyFlag`, or the command has no `--test`): the runner chooses its own count; only a MemoryMax holds. */
export const uncappedLine = ({ area, limitBytes, source }) => `area ${area}: workers not capped (no checks.testConcurrencyFlag for this command: the runner chooses); ${limitBytes ? `MemoryMax ${gb(limitBytes)}` : "no MemoryMax here either"}; read ${source}`;

/** The line of a test group, printed when it starts (a run the host kills must still say what it planned): its workers, what bound them, the budget and the MemoryMax in force. The free memory is shared like the cores:
 *  one share per test group and one left for the other steps (typecheck, build, lint), so `shares` = groups + 1 and `freeBytes` is the whole free memory. */
export function planLine({ area, cap, cores, cpuSource, shares, freeBytes, budget, limitBytes, source }) {
  const ceiling = limitBytes ? `MemoryMax ${gb(limitBytes)}` : "no MemoryMax here (worker count only)";
  return `area ${area}: ${cap.workers} workers (${cores} cores/${shares}${cpuSource ? ` cpu ${cpuSource}` : ""}, ${gb(freeBytes)} free/${shares} / ${gb(budget)} budget); ${ceiling}; read ${source}`;
}

/** The line of a test group when it ends: the most its processes held at once (0: not measured), and that divided by the workers. */
export function peakLine({ area, cap, peakBytes }) {
  return `area ${area}: ${peakBytes > 0 ? `peak measured ${gb(peakBytes / cap.workers)}/worker (${gb(peakBytes)} at the most, for ${cap.workers} workers)` : "peak not measured"}`;
}
