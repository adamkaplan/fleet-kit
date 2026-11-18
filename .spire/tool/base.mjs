#!/usr/bin/env node
// base: the project's BASE DISK (layer 1 of the workroom), built from the manifest's `warm` section, and a workroom started from it in seconds.
//
//   base build [--force]       the base image: the toolchain (`warm.from` + `warm.install`) and each dependency set (`warm.deps`) installed once, tagged by the
//                              hash of what it was built from, so an unchanged manifest and lock files build nothing. Prints the tag.
//   base up [--cold | --image TAG]   a workroom container: the checkout's HEAD tree in /workspace, each dependency set in place; prints its name. A dependency set is
//                              the image's when the hash of its `files` in the tree equals the hash it was built from, else it is installed now, as the build
//                              does (so a changed lock file costs that set only). --cold starts from the bare `warm.from` image and installs everything: what a
//                              project without a base disk pays, the number the base disk is measured against. --image TAG uses an image built earlier (a branch's tree against main's base). The image is built from the files of the HEAD commit, the tree the workroom gets..
//   base exec NAME -- CMD...   CMD in the workroom's /workspace with the dependency sets' env (`warm.deps[].env`)
//   base run [--cold] -- CMD   up, CMD, down on EVERY exit path; exit status = CMD's
//   base status | base down NAME    the workroom containers this tool made / destroy one BY ITS OWN NAME (only a container that carries this tool's label)
//
// The MECHANISM names no project. The project's manifest (.spire/box.json or land.json: manifest.mjs) says what to bake:
//   warm: { from: IMAGE (pin it), install: ROOT SHELL at build (system packages), deps: [ { name, files: [repo paths], run: SHELL, env: {K: V}, link: {repo path: path under the set} } ] }
// A set is made in $WARM_DIR (/opt/warm/<name> in the image) with `files` copied into it at their repo paths and `run` run there; `env` values may use $WARM_DIR and $PATH;
// `link` makes repo/PATH a symlink to the set's path (a dependency directory). Not here yet: a warm repo clone (the workroom's tree is the caller's checkout; the clone through the host gateway is slice 5).
// DESTROYS WHAT IT CREATES, BY ITS OWN NAME: the container is --rm, labelled with the run, ends itself after BASE_TTL_MIN (120), and `down` removes it by name with -v.
// The image is persistent on purpose and keyed by content. DOCKER is the docker command (default `docker`). Exit: 0 ok | 1 failed | 2 usage.
import { createHash, randomBytes } from "node:crypto";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { loadManifest } from "./manifest.mjs";
import { applyPins, spawnSync } from "./versions.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const DOCKER = (process.env.DOCKER ?? "docker").split(" ");
const OWNER = 65532; // the workroom's user, as the SWE image's
const q = (s) => `'${String(s).replaceAll("'", `'\\''`)}'`;
const say = (m) => process.stderr.write(`base: ${m}\n`);
const die = (m, code = 1) => {
  say(m);
  process.exit(code);
};
const sh = (cmd, args, opts = {}) =>
  spawnSync(cmd, args, { encoding: "utf8", maxBuffer: 1 << 28, ...opts });
const docker = (args, opts) =>
  sh(DOCKER[0], [...DOCKER.slice(1), ...args], opts);
const git = (args, opts) => sh("git", args, opts);

/** The warm section of the project's manifest, checked: what the mechanism needs. */
export function warmOf(top) {
  const files = [
    process.env.BOX_CONFIG ?? join(top, ".spire/box.json"),
    process.env.LAND_CONFIG ?? join(top, ".spire/land.json"),
  ];
  const warm = loadManifest(files).warm;
  if (!/@sha256:[0-9a-f]{64}$/.test(warm?.from ?? "")) {
    throw new Error(
      "warm.from must be a toolchain image pinned by digest (name@sha256:...): a moving tag would be reused as built",
    );
  }
  const seen = new Set();
  for (const d of warm.deps ?? []) {
    if (
      !/^[a-z0-9][a-z0-9_-]*$/.test(d.name ?? "") ||
      seen.has(d.name) ||
      !d.run ||
      !d.files?.length
    ) {
      throw new Error(
        `warm.deps: ${JSON.stringify(d.name)} needs a unique lowercase name, files and a run command`,
      );
    }
    seen.add(d.name);
    if (d.files.some((f) => f.startsWith("/") || f.split("/").includes(".."))) {
      throw new Error(
        `warm.deps ${d.name}: files are paths inside the repository`,
      );
    }
  }
  return { ...warm, deps: warm.deps ?? [] };
}

/** A file as the commit has it (what `up` puts in the workroom): never the working tree, never a symlink's target. */
const atHead = (top, f) => {
  const r = git(["-C", top, "show", `HEAD:${f}`], { encoding: "buffer" });
  if (r.status !== 0) {
    throw new Error(`warm.deps: ${f} is not in the HEAD commit`);
  }
  return r.stdout;
};

/** The tag: a hash of the warm section and of every file a set is built from (the lock files), so it changes exactly when the image would. */
export function tagOf(top, warm) {
  const h = createHash("sha256").update(JSON.stringify(warm));
  for (const d of warm.deps) {
    for (const f of d.files) {
      h.update(f).update(atHead(top, f));
    }
  }
  return `workroom-base:${h.digest("hex").slice(0, 12)}`;
}

// What one dependency set does, in the image at build and in a workroom whose files differ: the same shell, so the two cannot drift.
const makeSet = (d, dir, from) =>
  [
    `mkdir -p ${q(dir)}`,
    ...d.files.map(
      (f) =>
        `mkdir -p ${q(`${dir}/${dirname(f)}`)} && cp ${q(`${from}/${f}`)} ${q(`${dir}/${f}`)}`,
    ),
    `cd ${q(dir)}`,
    `WARM_DIR=${q(dir)}`,
    "export WARM_DIR",
    d.run,
  ].join(" && ");
const fingerprint = (d, dir) =>
  `cat ${d.files.map((f) => q(`${dir}/${f}`)).join(" ")} | sha256sum | cut -d' ' -f1`;

function dockerfile(warm) {
  const run = (cmd) => `RUN ${JSON.stringify(["sh", "-c", cmd])}`;
  const out = [`FROM ${warm.from}`, "USER root", run(warm.install || "true")];
  for (const d of warm.deps) {
    const dir = `/opt/warm/${d.name}`;
    out.push(`COPY ${d.name}/ /ctx/${d.name}/`);
    out.push(
      run(
        `${makeSet(d, dir, `/ctx/${d.name}`)} && ${fingerprint(d, dir)} >${dir}/.fingerprint && chmod -R a+rX ${dir} && rm -rf /ctx`,
      ),
    );
  }
  out.push(
    run(`install -d -o ${OWNER} -g ${OWNER} /workspace`),
    "LABEL io.spire.workroom.base=1",
  );
  return `${out.join("\n")}\n`;
}

function build(top, force = false) {
  const warm = warmOf(top);
  const tag = tagOf(top, warm);
  if (!force && docker(["image", "inspect", tag]).status === 0) {
    say(`${tag} is built (nothing to do)`);
    return tag;
  }
  const t0 = Date.now();
  const ctx = mkdtempSync(join(tmpdir(), "base-ctx-"));
  try {
    for (const d of warm.deps) {
      for (const f of d.files) {
        mkdirSync(join(ctx, d.name, dirname(f)), { recursive: true });
        writeFileSync(join(ctx, d.name, f), atHead(top, f));
      }
    }
    // run-hygiene: the base image is one persistent, shared tag keyed by content, built on purpose; a workroom container is labelled below
    const r = docker(["build", "-q", "-t", tag, "-f", "-", ctx], {
      input: dockerfile(warm),
    });
    if (r.status !== 0) {
      die(
        `building ${tag} failed:\n${(r.stderr || r.stdout).split("\n").slice(-25).join("\n")}`,
      );
    }
  } finally {
    rmSync(ctx, { recursive: true, force: true });
  }
  say(`built ${tag} in ${Date.now() - t0} ms`);
  return tag;
}

/** Inside the workroom, as its user: each set that is not the image's is made now; the env file and the links are written. */
function startScript(warm, cold) {
  const out = ["set -eu", "mkdir -p /tmp/home", ": >/tmp/workroom.env"];
  for (const d of warm.deps) {
    const baked = `/opt/warm/${d.name}`;
    const own = `/tmp/warm/${d.name}`;
    const install = [
      `WARM_DIR=${q(own)}`,
      `echo "install ${d.name}" >&2`,
      `(${makeSet(d, own, "/workspace")}) >/tmp/warm-${d.name}.log 2>&1 || { tail -20 /tmp/warm-${d.name}.log >&2; exit 1; }`,
    ];
    out.push(
      cold
        ? install.join("\n")
        : [
            `if [ -f ${q(`${baked}/.fingerprint`)} ] && [ "$(${fingerprint(d, "/workspace")})" = "$(cat ${q(`${baked}/.fingerprint`)})" ]; then`,
            `WARM_DIR=${q(baked)}; echo "warm ${d.name}" >&2`,
            "else",
            ...install,
            "fi",
          ].join("\n"),
    );
    // the env file is sourced by `exec`: WARM_DIR is set per set, and a value may use it and $PATH
    out.push(
      'printf "WARM_DIR=%s\\n" "$WARM_DIR" >>/tmp/workroom.env',
      "cat >>/tmp/workroom.env <<'ENV'",
    );
    out.push(
      ...Object.entries(d.env ?? {}).map(
        ([k, v]) => `export ${k}="${v.replace(/[\\"`]/g, "\\$&")}"`,
      ),
      "ENV",
    );
    for (const [at, to] of Object.entries(d.link ?? {})) {
      out.push(`ln -sfn "$WARM_DIR/"${q(to)} ${q(`/workspace/${at}`)}`);
    }
  }
  return `${out.join("\n")}\n`;
}

function up(top, cold, given) {
  const warm = warmOf(top);
  const t0 = Date.now();
  const image = cold ? warm.from : (given ?? build(top));
  if (
    cold &&
    docker(["image", "inspect", image]).status !== 0 &&
    docker(["pull", "-q", image]).status !== 0
  ) {
    die(`cannot pull ${image}`);
  }
  const t1 = Date.now();
  const id = `workroom-${new Date().toISOString().replace(/\D/g, "").slice(0, 14)}-${randomBytes(2).toString("hex")}`;
  const ttl = Number(process.env.BASE_TTL_MIN ?? 120);
  const labels = [
    "io.spire.workroom=1",
    `spire.run=${id}`,
  ];
  const args = [
    "run",
    "-d",
    "--rm",
    "--name",
    id,
    "--user",
    `${OWNER}:${OWNER}`,
    "-e",
    "HOME=/tmp/home",
    "-w",
    "/workspace",
    "--entrypoint",
    "sleep",
  ];
  if (cold) {
    args.push("--tmpfs", `/workspace:exec,uid=${OWNER},gid=${OWNER}`);
  }
  const r = docker([
    ...args,
    ...labels.flatMap((l) => ["--label", l]),
    image,
    String(ttl * 60),
  ]);
  if (r.status !== 0) {
    die(`cannot start a workroom from ${image}: ${r.stderr.trim()}`);
  }
  try {
    if (cold && warm.install) {
      const i = docker(["exec", "-u", "0", id, "sh", "-c", warm.install], {});
      if (i.status !== 0) {
        throw new Error(
          `warm.install failed: ${i.stderr.trim().split("\n").slice(-10).join("\n")}`,
        );
      }
    }
    const tree = git(["-C", top, "archive", "HEAD"], {
      encoding: "buffer",
      maxBuffer: 1 << 30,
    });
    const put = docker(["exec", "-i", id, "tar", "-x", "-C", "/workspace"], {
      input: tree.stdout,
    });
    const s =
      put.status === 0
        ? docker(["exec", "-i", id, "sh", "-s"], {
            input: startScript(warm, cold),
          })
        : put;
    if (s.status !== 0) {
      throw new Error(
        `the workroom could not be made ready: ${s.stderr.trim()}`,
      );
    }
    say(`${s.stderr.trim().split("\n").join(", ")}`);
  } catch (e) {
    down(id);
    die(e.message);
  }
  say(
    `up ${id} (${cold ? "cold" : "warm"}) in ${Date.now() - t1} ms (the image: ${t1 - t0} ms)`,
  );
  return id;
}

function down(id) {
  const own = docker([
    "inspect",
    "-f",
    '{{index .Config.Labels "io.spire.workroom"}}',
    id,
  ]);
  if (own.status !== 0 || own.stdout.trim() !== "1") {
    return die(`${id} is not a workroom this tool made`, 2);
  }
  docker(["rm", "-f", "-v", id]); // by its name, and its anonymous volumes with it
  say(`down ${id}`);
  return 0;
}

const exec = (id, cmd) => {
  const run = '. /tmp/workroom.env; exec "$@"';
  return (
    spawnSync(
      DOCKER[0],
      [
        ...DOCKER.slice(1),
        "exec",
        "-i",
        "-w",
        "/workspace",
        id,
        "sh",
        "-c",
        run,
        "sh",
        ...cmd,
      ],
      { stdio: "inherit" },
    ).status ?? 1
  );
};

function main(argv) {
  const [c, ...rest] = argv;
  const cold = rest.includes("--cold");
  const sep = rest.indexOf("--");
  const cmd = sep < 0 ? [] : rest.slice(sep + 1);
  const top = () =>
    git(["rev-parse", "--show-toplevel"]).stdout.trim() ||
    die("not in a git worktree", 2);
  try {
    switch (c) {
      case "build":
        return console.log(build(top(), rest.includes("--force")));
      case "up":
        return console.log(
          up(
            top(),
            cold,
            rest.includes("--image")
              ? rest[rest.indexOf("--image") + 1]
              : undefined,
          ),
        );
      case "exec":
        return process.exit(
          sep === 1 && cmd.length
            ? exec(rest[0], cmd)
            : die("usage: base exec NAME -- CMD...", 2),
        );
      case "run": {
        if (!cmd.length) {
          return die("usage: base run [--cold] -- CMD...", 2);
        }
        const id = up(top(), cold);
        const done = () => down(id);
        process.on("SIGINT", () => (done(), process.exit(130)));
        process.on("SIGTERM", () => (done(), process.exit(143)));
        const rc = exec(id, cmd);
        done();
        return process.exit(rc);
      }
      case "down":
        return rest[0] ? down(rest[0]) : die("usage: base down NAME", 2);
      case "status":
        return process.stdout.write(
          docker([
            "ps",
            "--filter",
            "label=io.spire.workroom=1",
            "--format",
            "{{.Names}} {{.Image}} {{.Status}}",
          ]).stdout,
        );
      default:
        return die(
          readFileSync(join(here, "base.mjs"), "utf8")
            .split("\n")
            .slice(1, 13)
            .map((l) => l.replace(/^\/\/ ?/, ""))
            .join("\n"),
          c ? 2 : 0,
        );
    }
  } catch (e) {
    return die(e.message);
  }
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  applyPins();
  main(process.argv.slice(2));
}
