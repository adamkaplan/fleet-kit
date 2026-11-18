// Admission: this file shows how many parallel test workers an area may start on a host, so a landing cannot take its host down. The cap is min(core share, free memory / the area's
// per-worker budget), never below 1; the budget is the project's one setting (`checks.workerBudgetMB`) with a default; the free memory is the host's or what the process's cgroup leaves; the peak
// the group's processes held is measured and reported with the MemoryMax in force. They would fail if the cap ignored memory or cores, if a setting did not override the default, if a cap went to 0,
// or if the report named a different ceiling than the one in force. The landing's own MemoryMax is a systemd user scope trusted only when it reports back the limit it was given.
// Oracle: the pure functions with fake core, memory, cgroup and /proc readers; one real child process for the /proc reader; the real run-steps in checks.test.mjs for the whole.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import test from "node:test";
import {
  DEFAULT_BUDGET_MB,
  availableBytes,
  budgetBytes,
  cpuCores,
  limitSource,
  hostShareBytes,
  memoryMaxBytes,
  scopeCommand,
  scopeWorks,
  groupCap,
  peakLine,
  planLine,
  treeRssBytes,
  uncappedLine,
  workerCap,
} from "./workers.mjs";

const MB = 1024 * 1024;
const GB = 1024 * MB;

test("the cap is the smaller of the core share and the memory the budget allows", () => {
  assert.equal(workerCap({ coreShare: 12, freeBytes: 16 * GB, budgetBytes: 1.5 * GB }).workers, 10, "memory binds: 16 GB / 1.5 GB");
  assert.equal(workerCap({ coreShare: 4, freeBytes: 64 * GB, budgetBytes: 1.5 * GB }).workers, 4, "cores bind");
  const c = workerCap({ coreShare: 12, freeBytes: 16 * GB, budgetBytes: 1.5 * GB });
  assert.deepEqual([c.byCores, c.byMemory], [12, 10]);
});

test("the cores and the free memory are shared between the test groups and the other steps: one share each, one left over", () => {
  // memory binds: 24 GB / (2 groups + 1) = 8 GB, at 1 GB a worker
  assert.equal(groupCap({ cores: 64, groups: 2, freeBytes: 24 * GB, budgetBytes: GB }).workers, 8);
  assert.equal(groupCap({ cores: 64, groups: 1, freeBytes: 24 * GB, budgetBytes: GB }).workers, 12, "one group: half the memory");
  // cores bind: 24 cores / (3 groups + 1) = 6
  assert.equal(groupCap({ cores: 24, groups: 3, freeBytes: 640 * GB, budgetBytes: GB }).workers, 6);
  assert.equal(groupCap({ cores: 2, groups: 4, freeBytes: 640 * GB, budgetBytes: GB }).workers, 2, "never below the floor of 2 cores");
  assert.equal(groupCap({ cores: 2, groups: 4, freeBytes: 0, budgetBytes: GB }).workers, 1, "nor below 1 worker");
});

test("a worker count is never below 1, whatever the host has", () => {
  assert.equal(workerCap({ coreShare: 12, freeBytes: 0, budgetBytes: GB }).workers, 1);
  assert.equal(workerCap({ coreShare: 12, freeBytes: 100 * MB, budgetBytes: 1.5 * GB }).workers, 1);
  assert.equal(workerCap({ coreShare: 0, freeBytes: 64 * GB, budgetBytes: GB }).workers, 1);
});

test("the project's setting overrides the default, per area or for all; a bad value is an error", () => {
  assert.equal(budgetBytes({}, "app"), DEFAULT_BUDGET_MB * MB, "no setting: the default");
  assert.equal(budgetBytes(undefined, "app"), DEFAULT_BUDGET_MB * MB);
  assert.equal(budgetBytes({ workerBudgetMB: 1536 }, "app"), 1.5 * GB, "one number: every area");
  const set = { workerBudgetMB: { app: 1536, server: 512 } };
  assert.equal(budgetBytes(set, "app"), 1.5 * GB);
  assert.equal(budgetBytes(set, "server"), 0.5 * GB);
  assert.equal(budgetBytes(set, "other"), DEFAULT_BUDGET_MB * MB, "an area it does not name: the default");
  // the setting moves the count
  const free = 8 * GB;
  assert.ok(workerCap({ coreShare: 100, freeBytes: free, budgetBytes: budgetBytes(set, "server") }).workers > workerCap({ coreShare: 100, freeBytes: free, budgetBytes: budgetBytes(set, "app") }).workers);
  for (const bad of [0, -1, "big", null, NaN]) assert.throws(() => budgetBytes({ workerBudgetMB: bad }, "app"), /workerBudgetMB/, String(bad));
  assert.throws(() => budgetBytes({ workerBudgetMB: { app: "x" } }, "app"), /workerBudgetMB\.app/);
});

const cgroups = (files) => ({ read: (p) => files[p] });

test("available memory is the host's free memory, or what is left under the tightest memory.max of the process's cgroup and its parents", () => {
  const at = "0::/user.slice/user-1000.slice/app.slice/land.scope\n";
  assert.equal(availableBytes({ free: 20 * GB, fs: cgroups({}) }), 20 * GB, "no cgroup files: the host's free memory");
  assert.equal(availableBytes({ free: 20 * GB, fs: cgroups({ "/proc/self/cgroup": at }) }), 20 * GB, "no limit files");
  const own = { "/proc/self/cgroup": at, "/sys/fs/cgroup/user.slice/user-1000.slice/app.slice/land.scope/memory.max": `${8 * GB}\n`, "/sys/fs/cgroup/user.slice/user-1000.slice/app.slice/land.scope/memory.current": `${2 * GB}\n` };
  assert.equal(availableBytes({ free: 20 * GB, fs: cgroups(own) }), 6 * GB, "limit minus use");
  assert.equal(availableBytes({ free: 4 * GB, fs: cgroups(own) }), 4 * GB, "the host is the tighter one");
  const parent = { ...own, "/sys/fs/cgroup/user.slice/memory.max": `${3 * GB}\n`, "/sys/fs/cgroup/user.slice/memory.current": `${1 * GB}\n` };
  assert.equal(availableBytes({ free: 20 * GB, fs: cgroups(parent) }), 2 * GB, "a parent's limit counts");
  const unlimited = { ...own, "/sys/fs/cgroup/user.slice/user-1000.slice/app.slice/land.scope/memory.max": "max\n" };
  assert.equal(availableBytes({ free: 20 * GB, fs: cgroups(unlimited) }), 20 * GB, "`max` is no limit");
});

test("the MemoryMax in force is the tightest one the process's cgroup and its parents hold, whatever the environment says", () => {
  const at = "0::/user.slice/land.scope\n";
  assert.equal(memoryMaxBytes({ fs: cgroups({ "/proc/self/cgroup": at, "/sys/fs/cgroup/user.slice/land.scope/memory.max": `${8 * GB}\n` }) }), 8 * GB);
  assert.equal(memoryMaxBytes({ fs: cgroups({ "/proc/self/cgroup": at, "/sys/fs/cgroup/user.slice/land.scope/memory.max": "max\n" }) }), 0, "`max`: no ceiling");
  assert.equal(memoryMaxBytes({ fs: cgroups({}) }), 0, "no cgroup v2: no ceiling");
  const parent = { "/proc/self/cgroup": at, "/sys/fs/cgroup/user.slice/land.scope/memory.max": `${8 * GB}\n`, "/sys/fs/cgroup/user.slice/memory.max": `${3 * GB}\n` };
  assert.equal(memoryMaxBytes({ fs: cgroups(parent) }), 3 * GB, "a parent's tighter limit is the one in force: the line names the ceiling that set the count");
});

test("the landing's ceiling is a share of what is free, in whole megabytes", () => {
  assert.equal(hostShareBytes(10 * GB), Math.floor((10 * GB * 0.9) / MB) * MB);
  assert.equal(hostShareBytes(10 * GB) % MB, 0);
  assert.ok(hostShareBytes(10 * GB) < 10 * GB);
});

test("a scope is trusted only when it reports back the limit it was given", () => {
  const argv = scopeCommand(2 * GB, ["node", "x.mjs"]);
  assert.deepEqual(argv.slice(0, 3), ["systemd-run", "--user", "--scope"]);
  assert.ok(argv.includes(`MemoryMax=${2 * GB}`));
  assert.ok(argv.includes("MemorySwapMax=0"));
  assert.deepEqual(argv.slice(-3), ["--", "node", "x.mjs"], "the command comes last, after --");
  const run = (status, stdout) => () => ({ status, stdout });
  assert.equal(scopeWorks(2 * GB, run(0, `${2 * GB}\n`)), true);
  assert.equal(scopeWorks(2 * GB, run(0, "max\n")), false, "a user manager that ignores the limit");
  assert.equal(scopeWorks(2 * GB, run(1, "")), false, "systemd-run failed");
  assert.equal(scopeWorks(2 * GB, run(null, "")), false, "no systemd-run on this host");
});

test("the resident memory of a process tree is summed from /proc (fake, then a real child)", () => {
  // pid 10 -> 11 -> 12; 20 is not of the family. statm: size resident ...
  const stat = (ppid) => `1 (a b) S ${ppid} 1 1 0`;
  const table = { 10: [stat(1), "100 5"], 11: [stat(10), "100 7"], 12: [stat(11), "100 11"], 20: [stat(1), "100 50"] };
  const proc = { dirs: () => Object.keys(table).map(Number), read: (p, f) => table[p]?.[f === "stat" ? 0 : 1] };
  assert.equal(treeRssBytes(10, proc, 1000), (5 + 7 + 11) * 1000);
  assert.equal(treeRssBytes(99, proc, 1000), 0, "a pid that is gone holds nothing");
  assert.equal(treeRssBytes(10, { dirs: () => { throw new Error("ENOENT: no /proc"); }, read: () => undefined }), 0, "a host with no /proc measures nothing, and does not fail");
  if (process.platform === "linux") {
    const c = spawn(process.execPath, ["-e", "setTimeout(() => {}, 5000)"], { stdio: "ignore" });
    try {
      assert.ok(treeRssBytes(c.pid) > 1 * MB, "a node process holds more than a megabyte");
    } finally {
      c.kill();
    }
  }
});

test("the lines name the workers and what bound them when a group starts, and the measured peak per worker when it ends", () => {
  const cap = { workers: 6 };
  const base = { area: "app", cap, cores: 24, shares: 2, freeBytes: 16 * GB, budget: 1.5 * GB };
  assert.equal(planLine({ ...base, limitBytes: 14 * GB, source: "host" }), "area app: 6 workers (24 cores/2, 16 GB free/2 / 1.5 GB budget); MemoryMax 14 GB; read host");
  assert.match(planLine({ ...base, shares: 4, limitBytes: 0, source: "host" }), /24 cores\/4, 16 GB free\/4 \/ 1.5 GB budget\); no MemoryMax here \(worker count only\); read host$/);
  assert.equal(peakLine({ area: "app", cap, peakBytes: 7.2 * GB }), "area app: peak measured 1.2 GB/worker (7.2 GB at the most, for 6 workers)");
  assert.equal(peakLine({ area: "app", cap, peakBytes: 0 }), "area app: peak not measured");
  assert.equal(uncappedLine({ area: "app", limitBytes: 14 * GB, source: "host" }), "area app: workers not capped (no checks.testConcurrencyFlag for this command: the runner chooses); MemoryMax 14 GB; read host");
  assert.match(uncappedLine({ area: "app", limitBytes: 0, source: "host" }), /no MemoryMax here either; read host$/);
});

const HUGE = "9223372036854771712\n"; // cgroup v1's "no limit"
const v2 = (extra = {}) => cgroups({ "/proc/self/cgroup": "0::/land.scope\n", ...extra });
const v1 = (extra = {}) => cgroups({ "/proc/self/cgroup": "12:cpu,cpuacct:/land\n4:memory:/land\n", ...extra });

test("cgroup v1 limits are read like v2, and the source is named", () => {
  const mem = { "/sys/fs/cgroup/memory/land/memory.limit_in_bytes": `${8 * GB}\n`, "/sys/fs/cgroup/memory/land/memory.usage_in_bytes": `${2 * GB}\n` };
  assert.equal(availableBytes({ free: 20 * GB, fs: v1(mem) }), 6 * GB);
  assert.equal(memoryMaxBytes({ fs: v1(mem) }), 8 * GB);
  assert.equal(limitSource({ fs: v1(mem) }), "cgroup v1");
  assert.equal(limitSource({ fs: v2({ "/sys/fs/cgroup/land.scope/memory.max": `${8 * GB}\n` }) }), "cgroup v2");
  assert.equal(limitSource({ fs: cgroups({}) }), "host");
  const parent = { ...mem, "/sys/fs/cgroup/memory/memory.limit_in_bytes": `${3 * GB}\n` };
  assert.equal(memoryMaxBytes({ fs: v1(parent) }), 3 * GB, "a v1 parent's tighter limit counts");
});

test("no limit means the host: v1 huge number, v2 max, in memory", () => {
  const f1 = v1({ "/sys/fs/cgroup/memory/land/memory.limit_in_bytes": HUGE });
  assert.equal(availableBytes({ free: 20 * GB, fs: f1 }), 20 * GB);
  assert.equal(memoryMaxBytes({ fs: f1 }), 0);
  assert.equal(limitSource({ fs: f1 }), "host");
  const f2 = v2({ "/sys/fs/cgroup/land.scope/memory.max": "max\n" });
  assert.equal(memoryMaxBytes({ fs: f2 }), 0);
  assert.equal(limitSource({ fs: f2 }), "host");
  assert.equal(memoryMaxBytes({ fs: v2({ "/sys/fs/cgroup/land.scope/memory.max": HUGE }) }), 0, "a huge v2 number is no limit either");
});

test("a hybrid host (a 0:: line and a v1 memory line) reads the v1 memory limit", () => {
  const f = cgroups({ "/proc/self/cgroup": "4:memory:/land\n0::/land\n", "/sys/fs/cgroup/memory/land/memory.limit_in_bytes": `${8 * GB}\n` });
  assert.equal(memoryMaxBytes({ fs: f }), 8 * GB);
  assert.equal(limitSource({ fs: f }), "cgroup v1");
});

test("the plan line says which source it read", () => {
  const base = { area: "app", cap: { workers: 2 }, cores: 4, shares: 2, freeBytes: 4 * GB, budget: GB, limitBytes: 0 };
  assert.match(planLine({ ...base, source: "cgroup v1" }), /; read cgroup v1$/);
  assert.match(planLine({ ...base, source: "host" }), /; read host$/);
  assert.match(uncappedLine({ area: "app", limitBytes: 0, source: "cgroup v2" }), /; read cgroup v2$/);
});

test("the CPU cores are the host's, or the tightest cgroup quota (v2 cpu.max, v1 cfs_quota/cfs_period), and the source is named", () => {
  const V2 = "/sys/fs/cgroup/land.scope", V1 = "/sys/fs/cgroup/cpu,cpuacct/land";
  const got = (fs) => cpuCores({ host: 16, fs });
  assert.deepEqual(got(cgroups({})), { cores: 16, source: "host" });
  assert.deepEqual(got(v2({ [`${V2}/cpu.max`]: "400000 100000\n" })), { cores: 4, source: "cgroup v2" });
  assert.deepEqual(got(v2({ [`${V2}/cpu.max`]: "max 100000\n" })), { cores: 16, source: "host" }, "max: the host's cores");
  assert.deepEqual(got(v2({ [`${V2}/cpu.max`]: "50000 100000\n" })), { cores: 1, source: "cgroup v2" }, "never below 1");
  assert.deepEqual(got(v2({ "/sys/fs/cgroup/cpu.max": "200000 100000\n", [`${V2}/cpu.max`]: "max 100000\n" })), { cores: 2, source: "cgroup v2" }, "a parent's quota counts");
  assert.deepEqual(got(v1({ [`${V1}/cpu.cfs_quota_us`]: "300000\n", [`${V1}/cpu.cfs_period_us`]: "100000\n" })), { cores: 3, source: "cgroup v1" });
  assert.deepEqual(got(v1({ "/sys/fs/cgroup/cpu/land/cpu.cfs_quota_us": "200000\n", "/sys/fs/cgroup/cpu/land/cpu.cfs_period_us": "100000\n" })), { cores: 2, source: "cgroup v1" }, "the cpu/ mount too");
  assert.deepEqual(got(v1({ [`${V1}/cpu.cfs_quota_us`]: "-1\n", [`${V1}/cpu.cfs_period_us`]: "100000\n" })), { cores: 16, source: "host" }, "-1: the host's cores");
  assert.deepEqual(got(v1({ [`${V1}/cpu.cfs_quota_us`]: "800000\n", [`${V1}/cpu.cfs_period_us`]: "100000\n" })), { cores: 8, source: "cgroup v1" });
  assert.deepEqual(got(v1({ [`${V1}/cpu.cfs_quota_us`]: "9600000\n", [`${V1}/cpu.cfs_period_us`]: "100000\n" })), { cores: 16, source: "host" }, "a quota above the host is no limit");
});

test("the plan line names the CPU source too", () => {
  const base = { area: "app", cap: { workers: 2 }, cores: 4, shares: 2, freeBytes: 4 * GB, budget: GB, limitBytes: 0, source: "host" };
  assert.match(planLine({ ...base, cpuSource: "cgroup v1" }), /4 cores\/2 cpu cgroup v1,/);
  assert.match(planLine({ ...base, cpuSource: "host" }), /4 cores\/2 cpu host,/);
});
