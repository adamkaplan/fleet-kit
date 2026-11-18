// A world for the tests of the base disk's own cleanup (base-prune.test.mjs, base-run.test.mjs): a fake `docker` (a node script, the
// tool's DOCKER) keeps images and containers as files of a state directory, so the real base.mjs runs against it on any host.
import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const base = join(fileURLToPath(import.meta.url), "../../base.mjs");
export const scratch = [];

const FAKE = String.raw`
import { appendFileSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
const state = process.env.FAKE_STATE;
for (const k of ["image", "container"]) mkdirSync(join(state, k), { recursive: true });
const argv = process.argv.slice(2);
appendFileSync(join(state, "calls"), JSON.stringify(argv) + "\n");
const read = (k, n) => JSON.parse(readFileSync(join(state, k, n), "utf8"));
const names = (k) => readdirSync(join(state, k));
const out = (t) => process.stdout.write(t);
const fail = (t) => { process.stderr.write(t + "\n"); process.exit(1); };
const [a, b] = argv;
if (a === "image" && b === "inspect") {
  const ref = argv.at(-1);
  const hit = names("image").map((n) => read("image", n)).find((i) => i.ref === ref);
  if (!hit) process.exit(1);
  out(hit.created + "\n");
} else if (a === "build") {
  const ref = argv[argv.indexOf("-t") + 1];
  const dockerfile = readFileSync(0, "utf8");
  const id = "img" + String(names("image").length + Number(existsSync(join(state, "counter")) ? readFileSync(join(state, "counter"), "utf8") : 0) + 1).padStart(4, "0");
  writeFileSync(join(state, "counter"), String(names("image").length + 1));
  const labels = [...dockerfile.matchAll(/^LABEL (\S+)=(\S+)$/gm)].map((m) => m[1] + "=" + m[2]);
  const created = new Date(Date.UTC(2026, 10, 13, 0, 0, Number(id.slice(3)))).toISOString();
  writeFileSync(join(state, "image", id), JSON.stringify({ id, ref, labels, created }));
  out(id + "\n");
} else if (a === "image" && b === "ls" && existsSync(join(state, "ls-fails"))) {
  fail("Cannot connect to the Docker daemon");
} else if (a === "image" && b === "ls") {
  const want = argv.flatMap((x, k) => (argv[k - 1] === "--filter" ? [x.replace(/^label=/, "")] : []));
  for (const n of names("image")) { const i = read("image", n); if (want.every((w) => i.labels.includes(w))) out(i.id + "\t" + i.ref + "\n"); }
} else if (a === "rmi") {
  const id = argv.at(-1);
  if (argv.includes("-f") || argv.includes("--force")) fail("forced");
  const held = names("container").some((n) => read("container", n).image === id);
  if (held) fail("conflict: unable to delete " + id + " (cannot be forced) - image is being used by running container");
  rmSync(join(state, "image", id), { force: true });
} else if (a === "run") {
  const name = argv[argv.indexOf("--name") + 1];
  const ref = argv.filter((x) => x.startsWith("workroom-base:") || x.startsWith("other:"))[0] ?? "";
  const image = names("image").map((n) => read("image", n)).find((i) => i.ref === ref)?.id ?? "";
  writeFileSync(join(state, "container", name), JSON.stringify({ image, labels: argv.filter((x) => x.startsWith("io.spire")) }));
} else if (a === "inspect") {
  if (!existsSync(join(state, "container", argv.at(-1)))) process.exit(1);
  out("1\n");
} else if (a === "rm") {
  rmSync(join(state, "container", argv.at(-1)), { force: true });
} else if (a === "exec" && argv.includes(". /tmp/workroom.env; exec \"$@\"")) {
  writeFileSync(join(state, "at-exec"), "");
  await new Promise((r) => setTimeout(r, 60000));
} else {
  // FAKE_VANISH: the docker command is gone once the workroom is up (a run whose command cannot even be started)
  if (process.env.FAKE_VANISH && argv.at(-2) === "sh" && argv.at(-1) === "-s") rmSync(process.env.FAKE_DOCKER_BIN, { force: true });
  process.stdin.resume(); process.stdin.on("data", () => {}); process.stdin.on("end", () => process.exit(0));
}
`;

export function world() {
  const dir = mkdtempSync(join(tmpdir(), "base-prune-"));
  scratch.push(dir);
  const state = join(dir, "state");
  mkdirSync(state);
  writeFileSync(join(dir, "docker.mjs"), FAKE);
  const bin = join(dir, "docker");
  writeFileSync(bin, `#!/bin/sh\nexec ${process.execPath} ${join(dir, "docker.mjs")} "$@"\n`, { mode: 0o755 });
  const project = join(dir, "project");
  mkdirSync(join(project, ".spire"), { recursive: true });
  writeFileSync(
    join(project, ".spire/box.json"),
    JSON.stringify({ box: 1, warm: { from: `x@sha256:${"0".repeat(64)}`, install: "true", deps: [{ name: "py", files: ["req.txt"], run: "true" }] } }),
  );
  const git = (...a) => execFileSync("git", ["-C", project, "-c", "user.name=t", "-c", "user.email=t@t", ...a], { stdio: "pipe" });
  git("init", "-q");
  const env = {
    ...Object.fromEntries(Object.entries(process.env).filter(([k]) => k !== "NODE_TEST_CONTEXT")),
    DOCKER: bin,
    FAKE_DOCKER_BIN: bin,
    FAKE_STATE: state,
  };
  const cli = (...a) => spawnSync(process.execPath, [base, ...a], { cwd: project, env, encoding: "utf8" });
  const lock = (n) => {
    writeFileSync(join(project, "req.txt"), `lock ${n}\n`);
    git("add", "-A");
    git("commit", "-q", "-m", `lock ${n}`);
  };
  const images = () => readdirSync(join(state, "image")).map((n) => JSON.parse(readFileSync(join(state, "image", n), "utf8")));
  const calls = () => (existsSync(join(state, "calls")) ? readFileSync(join(state, "calls"), "utf8").trim().split("\n").map((l) => JSON.parse(l)) : []);
  // the project's own mark on its base images: its root commit (once it has one)
  const mark = () => `io.spire.workroom.base.project=${execFileSync("git", ["-C", project, "rev-list", "--max-parents=0", "HEAD"], { encoding: "utf8" }).trim().slice(0, 12)}`;
  return { dir, state, project, env, cli, lock, images, calls, mark };
}

