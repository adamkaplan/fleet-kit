// Admission: this file shows what the council decides without a model (.spire/tool/council-lib.mjs) and what the real `council`
// command does with a real git change and no model call: a docs change sits no member and exits 0 with its trailer; a server change with no key
// stops at 15 and names no key. It would fail if the roster, the model pick (never the author's own), the verdict derivation (a block finding
// blocks whatever the member's first line says; a finding that cites no principle number is a note), the exit codes or the trailer broke.
// The council reads two things only: the project's principles and the reviewers' viewpoints (reviewers.mjs); the tests below pin that.
// Oracle: real git repositories in a temp dir and the real classify.mjs; the live model run is not here (a test would pay for it and a fixture
// cannot fail to answer): the real runs on three recent venue diffs, with the review bots' verdicts for the same diffs, are quoted in the PR.
import assert from "node:assert/strict";
import { execFile, execFileSync, spawnSync } from "node:child_process";
import { createServer } from "node:http";
import {
  cpSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { SKIPPED_PREFIXES } from "./fixtures/project/scripts/ci/code-scope.mjs";
import { runTool } from "./council-tools.mjs";
import { CONTRACT, memberPrompt } from "./council-prompt.mjs";
import { classify } from "./classify.mjs";
import { matcher } from "./config.mjs";
import { citeOf, loadReviewers, parseDefinition, rulesOf } from "./reviewers.mjs";
import {
  anchorOf,
  citedLines,
  dropClosed,
  exitFor,
  GATEWAY_BEARER,
  modelRoute,
  loopPlan,
  parseRebuttal,
  rebaseOf,
  reconcile,
  resolutionText,
  settle,
  parseVerdict,
  pickModel,
  stripFrontMatter,
  normalizeAnchor,
  trailerText,
  wholeFiles,
  withAnchors,
} from "./council-lib.mjs";

const DOCS = SKIPPED_PREFIXES[0].replace(/\/$/, "");
const repo = join(fileURLToPath(import.meta.url), "../..");
const classes = JSON.parse(
  readFileSync(join(repo, "tool/fixtures/model-registry.json"), "utf8"),
).classes;

test("a member runs on the fastest class whose model is not the author's", () => {
  const fast = pickModel(classes, "", undefined);
  assert.equal(fast.class, "fast");
  const other = pickModel(classes, fast.model.replace(/^.*\//, ""), undefined); // an author names itself without the provider
  assert.notEqual(other.model, fast.model);
  assert.ok(classes[other.class].model);
  assert.equal(pickModel({ fast: { model: "a/x" } }, "x", undefined), null);
});

test("a member's reasoning effort is the registry's reasoningEffort for its class, 'low' only when absent", () => {
  const reg = {
    fast: { model: "a/f" },
    everyday: { model: "a/e", reasoningEffort: "medium" },
  };
  assert.equal(pickModel(reg, "", undefined).effort, "low");
  assert.equal(pickModel(reg, "", "everyday").effort, "medium");
  assert.equal(pickModel(reg, "f", "fast").class, "everyday");
  assert.equal(pickModel(reg, "f", "fast").effort, "medium");
});

const ANSWER = `verdict: approve
head: 30efe39f6f58
findings:
- P1 | block | server/src/a.ts:12 | a second definition of "delivered" | use the existing fold
- P9 | block | server/src/b.ts:3 | a number the principles do not hold | none
- | block | server/src/c.ts:9 | no cite at all | none
- **Simplicity first** | fix | server/src/d.ts:1 | a layer nothing uses | delete it
- principle 3 (Inspector item 3) | fix | a.ts:2 | w | d
- P2 | note | app/src/e.tsx:4 | wording | shorten`;

test("the verdict is derived from the findings, not from the member's first line; a finding cites a principle by number, and one that cites none is a note", () => {
  const p =
    "### Principles\n1. **One owner, one path, one definition.** x\n2. **Calm.** w\n3. **Run it; do not reason about it.** z\n4. **Simplicity first.** y";
  const v = parseVerdict(ANSWER, p);
  assert.equal(v.verdict, "block"); // the member said approve; a block finding blocks
  assert.equal(v.findings.length, 6);
  assert.equal(v.findings[0].principle, "P1");
  assert.equal(v.findings[0].grounded, true);
  assert.equal(v.findings[0].severity, "block");
  assert.equal(v.findings[1].principle, ""); // P9 is not a principle of this project: it cites none
  assert.equal(v.findings[1].uncited, true);
  assert.equal(v.findings[1].severity, "note"); // a finding that cites none is a note: it stops nothing
  assert.match(v.findings[1].why, /rated block, cites no principle/);
  assert.equal(v.findings[2].uncited, true);
  assert.equal(v.findings[2].severity, "note");
  assert.equal(v.findings[3].principle, "P4"); // the rule's bold name resolves to its number
  assert.equal(v.findings[4].principle, "P3"); // "principle 3 (aside)": the number, not the member's aside
  assert.equal(
    parseVerdict(
      "verdict: request-changes\nfindings:\n- P9 | block | a.ts:1 | w | d",
      p,
    ).verdict,
    "comment", // the only finding cited nothing real: a note, so nothing stops
  );
  assert.equal(
    parseVerdict("verdict: approve\nhead: x\nfindings:\n", p).verdict,
    "approve",
  );
  assert.equal(
    parseVerdict(
      "verdict: comment\nfindings:\n- P4 | note | a.ts:1 | w | d",
      p,
    ).verdict,
    "comment",
  );
  assert.equal(
    parseVerdict(
      "verdict: comment\nfindings:\n- P4 | fix | a.ts:1 | w | d",
      p,
    ).verdict,
    "block", // a finding rated fix stands like a block: it closes by a fix or an accepted rebuttal, never by a round count
  );
  assert.equal(
    parseVerdict("I looked and it seems fine", p).verdict,
    "incomplete",
  );
});

test("a cite is a number of the project's principles: P3, 3, principle 3 and the rule's bold name are one; a number it does not hold is none", () => {
  const p = "Intro\n\n1. **Single source.** a\n2. **Smallest change.** b\n   continued text\n3. plain rule with no bold name\n\n## Timeless\n1. **Another list.** ignored: the first 1 wins";
  assert.deepEqual(rulesOf(p).map((r) => [r.n, r.name]), [[1, "Single source"], [2, "Smallest change"], [3, "plain rule with no bold name"]]);
  for (const c of ["P2", "p2", "2", "principle 2", "Rule 2", "**P2**", "P2 Smallest change", "Smallest change", "**Smallest change.**"]) assert.equal(citeOf(p, c), "P2", c);
  for (const c of ["P4", "0", "", "Made up", "no"]) assert.equal(citeOf(p, c), null, c);
});

test("exit codes and trailer: 0 clean, 18 blocked, 15 could not answer; the trailer names the roles and the diff", () => {
  const r = (role, verdict) => ({ role, verdict, class: "fast" });
  assert.equal(exitFor([r("inspector", "approve"), r("warden", "comment")]), 0);
  assert.equal(
    exitFor([r("inspector", "block"), r("warden", "incomplete")]),
    18,
  );
  assert.equal(
    exitFor([r("inspector", "approve"), r("warden", "incomplete")]),
    15,
  );
  // 18: a finding still open stops the landing, whatever the verdict words say and however many rounds it has been open
  assert.equal(
    exitFor([r("inspector", "block")], { round: 40, unresolved: [{}] }),
    18,
  );
  assert.equal(
    exitFor([r("inspector", "incomplete")], { unresolved: [{}] }),
    18,
  );
  assert.equal(
    trailerText([], ""),
    "none needed (no path calls for a member)",
  );
  assert.equal(
    trailerText([r("inspector", "approve"), r("warden", "comment")], "abc"),
    "ran: inspector=approve,warden=approve (models fast, diff abc)",
  );
  assert.equal(stripFrontMatter("---\nname: x\n---\nbody\n"), "body");
});

// `full`: the default council as shipped (all five on every change); else this project has removed all but the Warden (a file of each other default's name with `calls: []`)
function work({ full = false } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "council-test-"));
  for (const [from, to] of [
    ["examples/spire-venue/land.json", ".spire/land.json"],
    ["tool", ".spire/tool"], // the tool, vendored whole (LAYOUT.md): its defaults and fixtures too
    ["tool/fixtures/principles.md", ".spire/principles.md"], // the project's own law: numbered rules
    ["tool/fixtures/project/scripts/ci/code-scope.mjs", "scripts/ci/code-scope.mjs"],
    ["tool/fixtures/model-registry.json", "server/model-registry.json"],
  ]) {
    mkdirSync(join(dir, to, ".."), { recursive: true });
    cpSync(join(repo, from), join(dir, to), { recursive: true });
  }
  if (!full) {
    mkdirSync(join(dir, ".spire/review"), { recursive: true });
    for (const role of ["inspector", "steward", "architect", "designer"]) writeFileSync(join(dir, ".spire/review", `${role}.md`), `---\nrole: ${role}\ncalls: []\n---\nremoved by this project\n`);
  }
  const env = {
    ...process.env,
    GIT_AUTHOR_NAME: "t",
    GIT_AUTHOR_EMAIL: "t@t",
    GIT_COMMITTER_NAME: "t",
    GIT_COMMITTER_EMAIL: "t@t",
  };
  const git = (...a) =>
    execFileSync("git", a, { cwd: dir, env, encoding: "utf8" });
  git("init", "-q", "-b", "main");
  git("add", "-A");
  git("commit", "-qm", "seed");
  const change = (path, lines) => {
    mkdirSync(join(dir, path, ".."), { recursive: true });
    writeFileSync(
      join(dir, path),
      Array.from({ length: lines }, (_, i) => `l${i}`).join("\n") + "\n",
    );
    git("add", "-A");
    git("commit", "-qm", "change");
  };
  // no key and no gateway in the env: a member cannot answer
  const env0 = {
    PATH: `${dirname(process.execPath)}:/usr/bin:/bin`,
    HOME: dir,
  };
  const run = () =>
    spawnSync(
      process.execPath,
      [join(dir, ".spire/tool/council.mjs"), "--base", "HEAD~1"],
      { cwd: dir, encoding: "utf8", env: env0 },
    );
  // the same command with the workroom's environment: no key, a gateway named (async: the gateway is in this process)
  const runVia = (env, ...more) =>
    new Promise((resolve) =>
      execFile(
        process.execPath,
        [
          join(dir, ".spire/tool/council.mjs"),
          "--base",
          "HEAD~1",
          ...more,
        ],
        { cwd: dir, encoding: "utf8", env: { ...env0, ...env } },
        (e, stdout, stderr) =>
          resolve({ status: e ? e.code : 0, stdout, stderr }),
      ),
    );
  return { dir, change, run, runVia, git };
}
const last = (r) => JSON.parse(r.stdout.trim().split("\n").at(-1));

test("the command: the Warden reads a docs-only change too (no key stops at 15); a project that removes every member gets none needed (0)", () => {
  const w = work();
  const none = work();
  try {
    w.change(`${DOCS}/a.md`, 3);
    const docs = w.run();
    assert.equal(docs.status, 15, docs.stdout + docs.stderr);
    assert.match(docs.stdout, /no COUNCIL_MODEL_URL/);
    w.change("server/src/authority/x.ts", 40);
    const srv = w.run();
    assert.equal(srv.status, 15, srv.stdout + srv.stderr);
    assert.match(srv.stdout, /no COUNCIL_MODEL_URL/);
    writeFileSync(join(none.dir, ".spire/review/warden.md"), "---\nrole: warden\ncalls: []\n---\nremoved\n");
    none.git("add", "-A");
    none.git("commit", "-qm", "the project removes the Warden");
    none.change(`${DOCS}/a.md`, 3);
    const r = none.run();
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /none needed: no path of this change calls for a member/);
    assert.equal(last(r).trailer, "none needed (no path calls for a member)");
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
    rmSync(none.dir, { recursive: true, force: true });
  }
});

test("the model route comes only from the caller's environment: a named URL with a key sends the key; with none, the call is a gateway's; with no URL, no route", () => {
  const key = modelRoute({ COUNCIL_MODEL_URL: "http://127.0.0.1:7331/v1", COUNCIL_MODEL_KEY: "k1" });
  assert.deepEqual([key.via, key.bearer, key.url], ["key", "k1", "http://127.0.0.1:7331/v1/chat/completions"]);
  const gw = modelRoute({ COUNCIL_MODEL_URL: "http://127.0.0.1:7332/api/v1/" });
  assert.deepEqual([gw.via, gw.bearer, gw.url], ["gateway", GATEWAY_BEARER, "http://127.0.0.1:7332/api/v1/chat/completions"]);
  // a gateway that names its turn by a bearer is sent that bearer, still never a key
  const relay = modelRoute({ COUNCIL_MODEL_URL: "https://gateway.test/model", COUNCIL_MODEL_BEARER: "turn-token" });
  assert.deepEqual([relay.via, relay.bearer, relay.url], ["gateway", "turn-token", "https://gateway.test/model/chat/completions"]);
  // nothing is built in: a key alone, or an unusable URL, is no route
  assert.equal(modelRoute({ COUNCIL_MODEL_KEY: "k" }), null);
  assert.equal(modelRoute({ COUNCIL_MODEL_URL: "nonsense" }), null);
});

test("the council in a workroom: no key in its environment, every member's call goes to the named gateway on the registry's class, and the gateway alone holds one", async () => {
  const seen = [];
  const gateway = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const j = JSON.parse(body);
      seen.push({
        method: req.method,
        path: req.url,
        auth: req.headers.authorization,
        model: j.model,
      });
      res.setHeader("content-type", "application/json");
      res.end(
        JSON.stringify({
          choices: [
            {
              finish_reason: "stop",
              message: {
                content: "verdict: approve\nhead: x\nfindings:\n",
              },
            },
          ],
        }),
      );
    });
  });
  await new Promise((r) => gateway.listen(0, "127.0.0.1", r));
  const w = work();
  try {
    w.change("server/src/authority/x.ts", 40);
    const r = await w.runVia({
      COUNCIL_MODEL_URL: `http://127.0.0.1:${gateway.address().port}/api/v1`,
    });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /model route gateway/);
    assert.ok(
      seen.length >= 1,
      "the member its paths call for (the Warden, on a sensitive path) called the gateway",
    );
    const fast = new Set(Object.values(classes).map((c) => c.model));
    for (const c of seen) {
      assert.equal(c.method, "POST");
      assert.equal(c.path, "/api/v1/chat/completions");
      assert.equal(c.auth, `Bearer ${GATEWAY_BEARER}`); // a placeholder: no key was in the environment to send
      assert.ok(
        fast.has(c.model),
        "the model is a registry class's, never a literal",
      );
    }
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
    gateway.close();
  }
});

// Admission: the effort a member's model call carries is the registry's `reasoningEffort` for the class it runs on, "low" when the registry has none. It would fail if the call
// site hard-coded the effort again. Oracle: the real `council` process in a scratch project whose registry is rewritten, against a gateway that records the request body.
test("the council command sends the registry's reasoningEffort with the member's call, 'low' when absent", async () => {
  const seen = [];
  const gateway = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      seen.push(JSON.parse(body).reasoning.effort);
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ choices: [{ finish_reason: "stop", message: { content: "verdict: approve\nhead: x\nfindings:\n" } }] }));
    });
  });
  await new Promise((r) => gateway.listen(0, "127.0.0.1", r));
  const env = { COUNCIL_MODEL_URL: `http://127.0.0.1:${gateway.address().port}/api/v1` };
  const w = work();
  try {
    w.change("server/src/x.ts", 5);
    const regPath = join(w.dir, "server/model-registry.json");
    const reg = JSON.parse(readFileSync(regPath, "utf8"));
    await w.runVia(env, "--roles", "warden");
    for (const c of Object.values(reg.classes)) c.reasoningEffort = "medium";
    writeFileSync(regPath, JSON.stringify(reg));
    await w.runVia(env, "--roles", "warden");
    assert.deepEqual(seen, ["low", "medium"]);
  } finally {
    gateway.close();
  }
});

// Admission: this test shows the council command's loop end to end against a scripted model gateway: a seeded duplicate rated `fix` stops the landing (18) however often it is run,
// is never accepted by a round count, and closes only when the change fixes it and the member re-reviews it clean (the Land-Council
// trailer records `fixed`) or when the member reads a rebuttal and accepts it (`rebutted ... accepted by warden`); a member that keeps the finding after reading the
// rebuttal keeps it open. It would fail if a round count ever turned an open finding into a pass. Oracle: the real `council` process, real git, a real HTTP gateway whose
// answers are decided by what the member was given (the seeded line in the files, the rebuttal in the prompt): the member's answer, not a flag, decides.
test("the council loop against a gateway: a fix finding stops however often it is run, and closes only by a re-reviewed fix or an accepted rebuttal", async () => {
  const gateway = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const user = JSON.parse(body).messages.at(-1).content;
      const text = typeof user === "string" ? user : JSON.stringify(user);
      const seeded = /^\/\/ DUPLICATE_OF_FOLD$/m.test(text); // in the file whole, not in a diff line (which starts with + - or a space)
      const accepts = text.includes("ACCEPT-THIS");
      const answer =
        seeded && !accepts
          ? "verdict: request-changes\nhead: x\nfindings:\n- P1 | fix | server/src/x.ts:3 | a second definition of the fold | delete it"
          : "verdict: approve\nhead: x\nfindings:\n";
      res.setHeader("content-type", "application/json");
      res.end(
        JSON.stringify({
          choices: [{ finish_reason: "stop", message: { content: answer } }],
        }),
      );
    });
  });
  await new Promise((r) => gateway.listen(0, "127.0.0.1", r));
  const env = {
    COUNCIL_MODEL_URL: `http://127.0.0.1:${gateway.address().port}/api/v1`,
  };
  const go = (w, ...more) =>
    w.runVia(env, "--loop", "--roles", "warden", "--base", w.base, ...more);
  const seed = (w, dup) => {
    mkdirSync(join(w.dir, "server/src"), { recursive: true });
    writeFileSync(
      join(w.dir, "server/src/x.ts"),
      `${Array.from({ length: 40 }, (_, i) => `l${i}`).join("\n")}\n${dup ? "// DUPLICATE_OF_FOLD\n" : ""}`,
    );
    w.git("add", "-A");
    w.git("commit", "-qm", dup ? "seed a duplicate" : "repair it");
  };
  try {
    const w = work();
    try {
      w.change("server/src/x.ts", 40);
      w.base = w.git("rev-parse", "HEAD~1").trim(); // as land does: the merge base, so the change is the same one across rounds
      seed(w, true);
      // every round: the fix finding stops the landing (18); the same again: still stopped, never accepted
      const rounds = [];
      for (let i = 0; i < 4; i++) {
        const r = await go(w);
        rounds.push(r.status);
        assert.match(r.stdout, /to close/, r.stderr);
        assert.match(
          r.stdout,
          /warden: FIX P1 server\/src\/x\.ts:3/,
        );
      }
      assert.deepEqual(rounds, [18, 18, 18, 18]);
      assert.equal(last(await go(w)).result, "blocked");
      // a rebuttal the member reads and keeps the finding after closes nothing
      const kept = await go(
        w,
        "--rebut",
        "warden=P1: it is fine",
      );
      assert.equal(kept.status, 18);
      assert.match(kept.stdout, /read the rebuttal and kept it/);
      // the change fixes it and the member re-reviews it clean: it lands (0), the trailer records the resolution; no count of rounds mattered
      seed(w, false);
      const ok = await go(w);
      assert.equal(ok.status, 0, ok.stdout + ok.stderr);
      assert.match(
        ok.stdout,
        /closed  warden fix P1 server\/src\/x\.ts=fixed r/,
      );
      assert.match(last(ok).trailer, /^ran: warden=approve \(models fast, round \d+, diff [0-9a-f]+\)$/);
    } finally {
      rmSync(w.dir, { recursive: true, force: true });
    }
    // a rebuttal the member accepts closes it on the same diff, recorded with the member's name
    const v = work();
    try {
      v.change("server/src/x.ts", 40);
      v.base = v.git("rev-parse", "HEAD~1").trim();
      seed(v, true);
      assert.equal((await go(v)).status, 18);
      const acc = await go(
        v,
        "--rebut",
        "warden=P1: ACCEPT-THIS the second reader is the oracle",
      );
      assert.equal(acc.status, 0, acc.stdout + acc.stderr);
      assert.match(
        acc.stdout,
        /closed  warden fix P1 server\/src\/x\.ts=rebutted r2, accepted by warden \(P1: ACCEPT-THIS/,
      );
    } finally {
      rmSync(v.dir, { recursive: true, force: true });
    }
  } finally {
    gateway.close();
  }
});

test("the read-only tools read the head tree through git and nothing else", () => {
  const w = work();
  try {
    w.change("server/src/x.ts", 5);
    const head = execFileSync("git", ["rev-parse", "HEAD"], {
      cwd: w.dir,
      encoding: "utf8",
    }).trim();
    const at = process.cwd();
    process.chdir(w.dir);
    try {
      assert.match(
        runTool(head, "read_file", {
          path: "server/src/x.ts",
          start: 2,
          end: 3,
        }),
        /^2: l1\n3: l2$/,
      );
      assert.match(
        runTool(head, "grep", { pattern: "l3" }),
        /server\/src\/x\.ts:4:l3/,
      );
      assert.match(
        runTool(head, "list_files", { path: "server/src" }),
        /server\/src\/x\.ts/,
      );
      assert.match(
        runTool(head, "read_file", { path: "no/such/file" }),
        /^error:/,
      );
      assert.match(
        runTool(head, "grep", { pattern: `l1; touch ${"pwned"}` }),
        /nothing/,
      ); // the pattern is one argv entry, never a shell word
      assert.match(runTool(head, "rm_rf", {}), /no tool named/);
    } finally {
      process.chdir(at);
    }
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});

// Which reviewers a change's paths call: each viewpoint's own `calls`; every default calls `@all`, so the FULL council sits on every change; a project removes or narrows a member in its own file.
test("which paths call which reviewer: the whole default council on every change, the Warden on a docs-only one; a project removes or narrows a member", () => {
  const base = {
    sensitive: { always: ["SECURITY.md"], globs: ["auth/**"], nameGlobs: [] },
    isTest: matcher(["**/*.test.ts"]),
    isProse: matcher(["**/*.md"]),
    isNotProd: matcher(["**/*.md"]),
    reachesChecks: () => true,
    areas: [{ name: "all", paths: ["**"], steps: [] }],
    held: [],
  };
  const reviewers = loadReviewers(noFiles); // the tool's defaults
  const roles = (paths, rv = reviewers) => classify(base, paths, paths.map((path) => ({ path, lines: 5 })), [], rv).council.sort();
  const FULL = ["architect", "designer", "inspector", "steward", "warden"];
  assert.deepEqual([...reviewers.keys()].sort(), FULL, "the tool ships the five default viewpoints");
  assert.deepEqual(roles(["src/a.ts"]), FULL); // ordinary code: everyone
  assert.deepEqual(roles(["auth/a.ts"]), FULL);
  assert.deepEqual(roles(["docs/a.md"]), FULL); // docs only: the whole council, the Warden among them
  assert.deepEqual(roles(["auth/a.test.ts"]), FULL); // a test only: the same
  assert.ok(reviewers.get("warden").calls.includes("@all"), "the Warden is not path-limited");
  assert.deepEqual(reviewers.get("inspector").enforces, [3, 5, 8, 11], "the restored viewpoints keep their enforces");
  // a project removes a member (calls: []), narrows one, retargets another, or adds one of its own
  const mine = loadReviewers({
    read: (p) => ({
      ".spire/review/designer.md": "---\nrole: designer\ncalls: []\n---\nx\n",
      ".spire/review/steward.md": '---\nrole: steward\ncalls: ["lib/**"]\n---\nx\n',
      ".spire/review/baker.md": '---\nrole: baker\ncalls: ["menu/**", "@prod"]\n---\ny\n',
    })[p] ?? null,
    list: () => ["designer.md", "steward.md", "baker.md"],
  });
  assert.deepEqual(roles(["src/x.ts"], mine), ["architect", "baker", "inspector", "warden"]); // designer removed, steward narrowed away
  assert.deepEqual(roles(["lib/x.ts"], mine), ["architect", "baker", "inspector", "steward", "warden"]);
  assert.deepEqual(roles(["docs/a.md"], mine), ["architect", "inspector", "warden"]);
});

// What a member is given: ONE contract, its viewpoint (the project's file over the tool's default) and the project's principles, and nothing else.
const noFiles = { read: () => null, list: () => [] };
const principlesText = readFileSync(join(repo, "tool/fixtures/principles.md"), "utf8");

test("a member's prompt is the one contract, its viewpoint and the project's principles, and the numbers it enforces", () => {
  const defs = loadReviewers(noFiles);
  assert.deepEqual([...defs.keys()].sort(), ["architect", "designer", "inspector", "steward", "warden"]); // the tool's defaults: the full council
  assert.ok([...defs.values()].every((d) => d.source === "default" && d.body.length > 100 && d.body.length < 3000), "short viewpoints");
  const p = memberPrompt({ role: "warden", def: defs.get("warden"), principles: principlesText, head: "0123456789abcdef" });
  assert.ok(p.includes(CONTRACT("Warden", "0123456789ab")));
  assert.ok(p.includes("**Can this change be abused to reach a harm?**"));
  assert.ok(p.includes("# The principles (the project's .spire/principles.md at the base)"));
  assert.ok(p.includes("1. **Single source.**")); // the project's text, not a default
  assert.match(p, /You enforce principles P10, P11, P12\./);
  assert.doesNotMatch(p, /\$\{\{|review-base|pull_request_read/); // no bot machinery
  // a viewpoint that reads a file is given it (a project's rubric)
  const withReads = memberPrompt({ role: "warden", def: { ...defs.get("warden"), reads: ["docs/rubric.md", "docs/none.md"] }, principles: principlesText, head: "abc", readFile: (f) => (f === "docs/rubric.md" ? "RUBRIC-TEXT" : "") });
  assert.match(withReads, /<file path="docs\/rubric\.md">\nRUBRIC-TEXT\n<\/file>/);
  assert.doesNotMatch(withReads, /docs\/none\.md/);
});

test("a viewpoint: the project's file replaces the default of its name whole or adds one; a file that does not say `role: <its own name>` is not a viewpoint", () => {
  const files = {
    ".spire/review/inspector.md": '---\nrole: inspector\ncalls: ["src/**"]\nenforces: [1, 2]\nreads: ["docs/rubric.md"]\n---\n# Mine\n**Look hard.**\n',
    ".spire/review/baker.md": '---\nrole: baker\ncalls: ["menu/**"]\n---\nTaste it.\n',
    ".spire/review/models.json": "{}",
    ".spire/review/rubric.md": "not a viewpoint: no front matter\n",
    ".spire/review/liar.md": "---\nrole: someone-else\n---\nnot read: the name and the file disagree\n",
  };
  const defs = loadReviewers({ read: (p) => files[p] ?? null, list: () => Object.keys(files).map((f) => f.split("/").pop()) });
  assert.deepEqual([...defs.keys()].sort(), ["architect", "baker", "designer", "inspector", "steward", "warden"]);
  const i = defs.get("inspector");
  assert.deepEqual([i.source, i.calls, i.enforces, i.reads, i.body.split("\n")[0]], ["project", ["src/**"], [1, 2], ["docs/rubric.md"], "# Mine"]);
  assert.equal(defs.get("baker").source, "project");
  assert.equal(defs.get("warden").source, "default"); // the others stay the tool's
  assert.throws(() => parseDefinition("---\nrole: x\ncalls: not a list\n---\nb"), /JSON list/);
  assert.equal(parseDefinition("no front matter"), null);
});

test("every default viewpoint enforces principles the default principles hold, and the default principles are numbered from 1 without a gap", () => {
  const dp = readFileSync(join(repo, "tool/defaults/principles.md"), "utf8");
  const n = rulesOf(dp).map((r) => r.n);
  assert.deepEqual(n, Array.from({ length: n.length }, (_, i) => i + 1));
  for (const d of loadReviewers(noFiles).values()) {
    for (const k of d.enforces) assert.ok(n.includes(k), `${d.role} enforces P${k}`);
  }
});

// Admission: the council's own rule, the captain's design: a finding stops a landing only by CITING a numbered principle of the project's .spire/principles.md. One that cites none, or a number the project
// does not hold, or a line outside the contract (a free-text "BLOCK: ..."), is a NOTE: the verdict is comment, the exit is 0 and no round count is involved. A member that said request-changes and
// gave nothing at all has not answered (incomplete, exit 15), never approve. They would fail if an uncited line, however it was rated or worded, stopped a landing. Oracle: the real parsers and exitFor
// (council-lib.mjs) on answers shaped like the ones real members give; the command itself is run in the next test.
test("a finding with no citation is a note and never blocks; one that cites a principle of the project blocks; an empty request-changes is incomplete", () => {
  const p = "1. **Simplicity first.** y";
  const read = (text) => parseVerdict(text, p);
  const exit = (v) => exitFor([{ role: "warden", verdict: v.verdict }]);
  // BLOCK with no citation (in the contract's shape, with a number the project does not hold, and as free text): a note, exit 0
  for (const text of [
    "verdict: request-changes\nfindings:\n- | block | a.ts:1 | w | d",
    "verdict: request-changes\nfindings:\n- P9 | block | a.ts:1 | w | d",
    "verdict: comment\nfindings:\n- P1 | note | a.ts:1 | w | d\n- BLOCK: the cache is never invalidated",
    "verdict: approve\nfindings:\n- looks wrong at a.ts:9",
    "verdict: request-changes\nfindings:\n- the handler leaks the token on line 4\n",
  ]) {
    const v = read(text);
    assert.equal(v.verdict, "comment", text);
    assert.ok(v.findings.every((f) => f.severity === "note"), text);
    assert.equal(exit(v), 0, text);
  }
  // the same BLOCK with a citation of the project's principle stands: blocked (18)
  const cited = read("verdict: request-changes\nfindings:\n- P1 | block | a.ts:1 | w | d");
  assert.equal(cited.verdict, "block");
  assert.equal(exit(cited), 18);
  // nothing read at all from a member that asked for changes: it did not answer
  assert.equal(read("verdict: request-changes\nfindings:\n").verdict, "incomplete");
  assert.equal(exit(read("verdict: request-changes\nfindings:\n")), 15);
  // what stays what it was: none, a read finding, an elaboration under a read finding
  assert.equal(read("verdict: approve\nfindings:\n- none").verdict, "approve");
  assert.equal(read("verdict: approve\nfindings:\n").verdict, "approve");
  assert.equal(read("verdict: comment\nfindings:\n- P1 | note | a.ts:1 | w | d\n- see also the earlier note").verdict, "comment");
});

// the same rule through the real `council` command and a gateway: a member that answers BLOCK with no citation lands (0, said as a note); the same block citing P1 stops it (18).
test("the council command: a BLOCK that cites no principle exits 0 as a note; a BLOCK citing one exits 18", async () => {
  let answer = "";
  const gateway = createServer((req, res) => {
    req.on("data", () => {});
    req.on("end", () => {
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ choices: [{ finish_reason: "stop", message: { content: answer } }] }));
    });
  });
  await new Promise((r) => gateway.listen(0, "127.0.0.1", r));
  const w = work();
  try {
    w.change("server/src/authority/x.ts", 40);
    const via = { COUNCIL_MODEL_URL: `http://127.0.0.1:${gateway.address().port}/api/v1` };
    answer = "verdict: request-changes\nhead: x\nfindings:\n- | block | server/src/authority/x.ts:3 | a worry with no principle | do something";
    const note = await w.runVia(via, "--loop");
    assert.equal(note.status, 0, note.stdout + note.stderr);
    assert.match(note.stdout, /cites no principle of the project: a note/);
    assert.equal(last(note).result, "clean");
    answer = "verdict: request-changes\nhead: x\nfindings:\n- P1 | block | server/src/authority/x.ts:3 | a second definition | delete it";
    const blocked = await w.runVia(via); // no --loop: the first run's kept answer is not carried
    assert.equal(blocked.status, 18, blocked.stdout + blocked.stderr);
    assert.equal(last(blocked).result, "blocked");
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
    gateway.close();
  }
});

// Admission: these tests show the council over a REBASE onto a moved main. The change's own diff, by its patch-id, is what the members read: the same diff on another base is not read again (no model call, said so, and the
// trailer names the members as kept); a rebase that had to resolve a conflict in a changed file made another diff, and every member reads it, an approving one too. With main unmoved the loop is what it was (a fix by one member
// keeps the others' approvals). They would fail if a rebase over an untouched diff called a model, if a changed diff were carried, or if a fix round (main unmoved) re-asked the members that approved.
// Oracle: the real `council` command, real git (a real rebase, a real conflict) and a real HTTP gateway that counts the calls it is given.
test("a rebase that leaves the change's diff as it was does not run the council again; one that changed it (a conflict resolved) does, every member", async () => {
  assert.deepEqual(rebaseOf({ base: "b1", diff: "d1" }, { base: "b2", diff: "d1" }), { moved: true, sameDiff: true });
  assert.deepEqual(rebaseOf({ base: "b1", diff: "d1" }, { base: "b2", diff: "d2" }), { moved: true, sameDiff: false });
  assert.deepEqual(rebaseOf({ base: "b1", diff: "d1" }, { base: "b1", diff: "d2" }), { moved: false, sameDiff: false });
  assert.deepEqual(rebaseOf({ base: "b1", diff: "d1", exact: "e1" }, { base: "b2", diff: "d1", exact: "e2" }), { moved: true, sameDiff: false }); // a patch-id does not see white space; the exact lines do
  assert.deepEqual(rebaseOf({ base: "b1", diff: "d1", law: "l1" }, { base: "b2", diff: "d1", law: "l2" }), { moved: true, sameDiff: false }); // the same diff under a law main changed is another review
  assert.deepEqual(rebaseOf({ base: "b1", diff: "d1", law: "l1" }, { base: "b2", diff: "d1", law: "l1" }), { moved: true, sameDiff: true });
  assert.equal(rebaseOf({ diff: "d1" }, { base: "b2", diff: "d2" }).moved, false); // a state with no base was written before the base was kept: never read as a rebase
  const prior = { inspector: { role: "inspector", verdict: "approve" }, warden: { role: "warden", verdict: "comment" } };
  assert.deepEqual(loopPlan(["inspector", "warden"], prior, { moved: true, sameDiff: true }).run, []);
  assert.deepEqual(loopPlan(["inspector", "warden"], prior, { moved: true, sameDiff: false }).run, ["inspector", "warden"]);
  assert.deepEqual(loopPlan(["inspector", "warden"], prior, { moved: false, sameDiff: false }).run, []); // a fix round, main unmoved: the approvals stand, as they did

  let calls = 0;
  const gateway = createServer((req, res) => {
    req.on("data", () => {});
    req.on("end", () => {
      calls++;
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ choices: [{ finish_reason: "stop", message: { content: "verdict: approve\nhead: x\nfindings:\n" } }] }));
    });
  });
  await new Promise((r) => gateway.listen(0, "127.0.0.1", r));
  const w = work();
  try {
    const via = { COUNCIL_MODEL_URL: `http://127.0.0.1:${gateway.address().port}/api/v1` };
    const x = "server/src/authority/x.ts";
    const lines = (n, tag = "") => Array.from({ length: n }, (_, i) => `l${i}${tag}`).join("\n") + "\n";
    const mainMoves = (from, file, text) => {
      // main moves on a side branch from `from`; the change (on `main` here, as land's branch is) is rebased onto it
      w.git("checkout", "-q", "-B", "upstream", from);
      mkdirSync(join(w.dir, file, ".."), { recursive: true });
      writeFileSync(join(w.dir, file), text);
      w.git("add", "-A");
      w.git("commit", "-qm", "main moved");
      w.git("checkout", "-q", "main");
      return w.git("rev-parse", "upstream").trim();
    };
    w.change(x, 40);
    const go = (base) => w.runVia(via, "--loop", "--base", base);
    let base = w.git("rev-parse", "HEAD~1").trim();
    const first = await go(base);
    assert.equal(first.status, 0, first.stdout + first.stderr);
    assert.equal(calls, 1);
    const trailer1 = last(first).trailer;

    // (a) main moved in a file the change does not touch; the change is rebased clean: the diff is the same, no member is asked, and the trailer says they are kept
    base = mainMoves(base, "docs/other.md", "other\n");
    w.git("rebase", "-q", "upstream");
    const same = await go(base);
    assert.equal(same.status, 0, same.stdout + same.stderr);
    assert.equal(calls, 1, "the same diff on a moved main is not read again");
    assert.match(same.stdout, /main moved; the change's own diff is the same \(patch-id [0-9a-f]+\)/);
    assert.match(last(same).trailer, /^ran: warden=approve \(models fast, round 1, diff [0-9a-f]+, kept from an earlier round: warden\)$/);
    assert.equal(last(same).trailer.match(/diff ([0-9a-f]+)/)[1], trailer1.match(/diff ([0-9a-f]+)/)[1]);

    // (d) main moved the law (a principle) and the change's diff is the same: the verdicts were given under another law, so the member reads it again
    base = mainMoves(base, ".spire/principles.md", `${readFileSync(join(w.dir, ".spire/principles.md"), "utf8")}\n99. **An added rule.** a rule main added\n`);
    w.git("rebase", "-q", "upstream");
    const law = await go(base);
    assert.equal(law.status, 0, law.stdout + law.stderr);
    assert.equal(calls, 2, "the same diff under a law main changed is read again");
    assert.match(law.stdout, /or the law it is judged by changed with it/);

    // (c) main moved in the very file the change adds; the rebase conflicts, and the resolution is another diff: the council reads it, though the member had approved
    base = mainMoves(base, x, lines(40, "-theirs"));
    assert.throws(() => w.git("rebase", "upstream"), /./); // conflict
    writeFileSync(join(w.dir, x), lines(40, "-resolved"));
    w.git("add", "-A");
    execFileSync("git", ["rebase", "--continue"], { cwd: w.dir, env: { ...process.env, GIT_EDITOR: "true", GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t" } });
    const changed = await go(base);
    assert.equal(changed.status, 0, changed.stdout + changed.stderr);
    assert.equal(calls, 3, "a changed diff is read again");
    assert.match(changed.stdout, /main moved and the change's own diff \(patch-id [0-9a-f-]+ -> [0-9a-f-]+\) or the law it is judged by changed with it/);
    assert.doesNotMatch(last(changed).trailer, /kept from an earlier round/);

    // (e) the change differs only by white space inside a line after the rebase: its patch-id is the same, the lines are not, and the member reads it
    writeFileSync(join(w.dir, x), lines(40, "-resolved").replace("l5-resolved", "l5-resolved "));
    w.git("commit", "-qa", "--amend", "-m", "change");
    base = mainMoves(base, "docs/e.md", "e\n");
    w.git("rebase", "-q", "upstream");
    const blank = await go(base);
    assert.equal(blank.status, 0, blank.stdout + blank.stderr);
    assert.equal(calls, 4, "a white space change a patch-id does not see is read again");
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
    gateway.close();
  }
});

// Admission: these tests show the rule of the council's loop: a finding rated fix or block is closed only by a fix its member re-reviews clean or by a rebuttal the member
// accepts, and never by a round count. They would fail if a round count converted an open finding into acceptance (the old "capped, lands"),
// or if a rebuttal closed a finding the member kept. Oracle: the real ledger (council-lib.mjs) on answers shaped like the members'; the command itself is run below.
test("the loop: a member with a finding to close runs again; a rebuttal must cite a principle; only a fix re-reviewed clean or an accepted rebuttal closes it; no round count does", () => {
  const f = (principle, severity = "block", where = "a.ts:1") => [
    { principle, severity, where, why: "w", fix: "d" },
  ];
  const known = "1. **Single source.** a fact is written once\n2. **Calm.** one look-at";
  const r1 = [
    {
      role: "inspector",
      verdict: "block",
      class: "fast",
      findings: f("P1", "fix"),
    },
    { role: "warden", verdict: "approve", class: "fast", findings: [] },
  ];
  const ran = ["inspector", "warden"];
  const prior = Object.fromEntries(r1.map((r) => [r.role, r]));
  const plan = loopPlan(["inspector", "warden"], prior);
  assert.deepEqual(plan.run, ["inspector"]); // a fix finding runs its member again; the approving member is kept
  assert.equal(plan.carried.warden.carried, true);
  const b = parseRebuttal(
    "inspector=P1: the second reader is the test oracle",
  );
  assert.deepEqual(b, {
    role: "inspector",
    principle: "P1",
    reason: "the second reader is the test oracle",
  });
  assert.equal(parseRebuttal("inspector: no equals sign"), null);
  assert.throws(
    () =>
      settle(r1, {
        rebuttals: [{ ...b, principle: "Made up" }],
        round: 1,
        principles: known,
        ran,
        diff: "d1",
      }),
    /is no principle of the project/,
  );
  // round 1: the fix finding is on the ledger, open; a note never is
  const s1 = settle(
    [
      {
        ...r1[0],
        findings: [...r1[0].findings, ...f("P2", "note", "b.ts:2")],
      },
      r1[1],
    ],
    { round: 1, principles: known, ran, diff: "d1" },
  );
  assert.equal(s1.ledger.length, 1);
  assert.equal(s1.unresolved.length, 1);
  assert.equal(exitFor(s1.results, s1), 18);
  // the member re-answers on the SAME diff: a rebuttal it reads and still keeps closes nothing; one it drops (accepts) is recorded as rebutted, with the member's name
  const kept = settle([r1[0]], {
    prior: s1.ledger,
    rebuttals: [b],
    round: 2,
    principles: known,
    ran: ["inspector"],
    diff: "d1",
  });
  assert.equal(kept.unresolved.length, 1);
  assert.equal(kept.unresolved[0].refused, true);
  const accepted = settle(
    [{ role: "inspector", verdict: "approve", findings: [] }],
    {
      prior: s1.ledger,
      rebuttals: [b],
      round: 2,
      principles: known,
      ran: ["inspector"],
      diff: "d1",
    },
  );
  assert.equal(accepted.unresolved.length, 0);
  assert.match(
    resolutionText(accepted.ledger[0]),
    /^inspector fix P1 a\.ts=rebutted r2, accepted by inspector \(P1: the second reader is the test oracle\)/,
  );
  // a rebuttal that answers another principle closes nothing; a fix (the diff changed) the member re-reviews clean is `fixed`
  const other = parseRebuttal("inspector=P2: unrelated");
  const wrong = settle(
    [{ role: "inspector", verdict: "block", findings: f("P1") }],
    {
      prior: s1.ledger,
      rebuttals: [other],
      round: 2,
      principles: known,
      ran: ["inspector"],
      diff: "d1",
    },
  );
  assert.equal(wrong.unresolved.length, 1);
  const fixed = settle(
    [{ role: "inspector", verdict: "approve", findings: [] }],
    { prior: s1.ledger, round: 2, principles: known, ran: ["inspector"], diff: "d2" },
  );
  assert.equal(fixed.ledger[0].status, "fixed");
  assert.equal(
    resolutionText(fixed.ledger[0]),
    "inspector fix P1 a.ts=fixed r2",
  );
  // the same diff and no rebuttal, yet the member drops it: `withdrawn` (its own answer changed), said as such in the trailer
  const dropped = settle(
    [{ role: "inspector", verdict: "approve", findings: [] }],
    { prior: s1.ledger, round: 2, principles: known, ran: ["inspector"], diff: "d1" },
  );
  assert.equal(dropped.ledger[0].status, "withdrawn");
  assert.match(
    resolutionText(dropped.ledger[0]),
    /^inspector fix P1 a\.ts=withdrawn by inspector r2/,
  );
  // a member that did not answer (incomplete) closes nothing, whatever it says
  const silent = reconcile(
    s1.ledger,
    [{ role: "inspector", verdict: "incomplete", findings: [] }],
    { ran: [], rebuttals: [], round: 2, diff: "d2" },
  );
  assert.equal(silent[0].status, "open");
  // no cap: open at round 3, at round 40, and every later round that still has it open: never accepted, never refused by a count
  for (const round of [3, 40]) {
    const late = settle(r1, { round, principles: known, ran, diff: "d1" });
    assert.equal(late.unresolved.length, 1);
    assert.equal(exitFor(late.results, late), 18);
    assert.match(
      resolutionText(late.ledger[0]),
      /^inspector fix P1 a\.ts=OPEN/,
    );
  }
});

// Admission: the council remembers what an accepted rebuttal closed. A finding's fingerprint is (member, principle, file, the normalized text of the lines it quotes); a member that raises again a finding the
// ledger closed (rebutted), on quoted lines that have not changed, is dropped by the tool; one whose quoted lines changed, or a new one, stands. These tests would fail if a re-raise
// after a rebuttal came back to the builder, if a finding on changed lines were swallowed, if a new finding were dropped, if a finding closed any other way (withdrawn, fixed) were dropped, or if white space moved the fingerprint. Oracle: the real ledger functions
// (council-lib.mjs) over the real round shapes, then the real `council` command and a real HTTP gateway.
test("council memory: a re-raise of a finding closed by an accepted rebuttal, on unchanged quoted lines, is dropped; changed lines or a new finding are kept; white space does not move the fingerprint", () => {
  const known = "1. **Single source.** a fact is written once\n2. **Calm.** one look-at";
  const file = (...lines) => (name) => (name === "a.ts" ? lines.join("\n") : null);
  const finding = (principle, where, severity = "block") => ({ principle, severity, where, why: "w", fix: "d" });
  const answer = (...findings) => ({ role: "inspector", verdict: "block", findings });
  const ran = ["inspector"];
  const raise = (read, ...fs) => withAnchors([answer(...fs)], ran, read);
  // (d) the normalization: runs of white space, edges, blank lines and indentation do not move the anchor; a word does; a cited range is its lines
  assert.equal(normalizeAnchor("  a   b\t c \n\n   d  "), "a b c\nd");
  const base = file("x", "  const  a = 1;", "y");
  assert.ok(anchorOf("a.ts:2", base));
  assert.equal(anchorOf("a.ts:2", base), anchorOf("a.ts:5", file("p", "q", "r", "s", "const a = 1;\t")), "the same text on another line, with other white space, is the same anchor");
  assert.notEqual(anchorOf("a.ts:2", base), anchorOf("a.ts:2", file("x", "const a = 2;")));
  assert.equal(anchorOf("a.ts:1-2", base), anchorOf("a.ts:L1-L2", base));
  assert.notEqual(anchorOf("a.ts:1-2", base), anchorOf("a.ts:2", base));
  assert.equal(anchorOf("a.ts", base), "", "no line cited: no anchor");
  assert.equal(anchorOf("gone.ts:1", base), "", "no such file: no anchor");
  assert.equal(anchorOf("a.ts:9", base), "", "no such line: no anchor");
  assert.deepEqual(citedLines("`a/b.mjs`:12-18"), { file: "a/b.mjs", from: 12, to: 18 });
  assert.deepEqual(citedLines("a/b.mjs:12"), { file: "a/b.mjs", from: 12, to: 12 });
  assert.equal(citedLines("a/b.mjs"), null);

  // round 1: the finding is raised and open. Round 2, the member lets it go three ways: on the same diff with no rebuttal (withdrawn), on the same diff after a rebuttal it read (rebutted), on a changed diff (fixed).
  // The anchor it was raised on is kept in the ledger
  const text = file("l0", "the quoted line", "l2");
  const r1 = settle(raise(text, finding("P1", "a.ts:2")), { round: 1, principles: known, ran, diff: "d1" });
  assert.equal(r1.ledger[0].anchor, anchorOf("a.ts:2", text));
  const approve = [{ role: "inspector", verdict: "approve", findings: [] }];
  const close = (diff, rebuttals = []) => settle(approve, { prior: r1.ledger, rebuttals, round: 2, principles: known, ran, diff });
  const withdrawn = close("d1");
  assert.equal(withdrawn.ledger[0].status, "withdrawn");
  const rebutted = close("d1", [parseRebuttal("inspector=P1: because")]);
  assert.equal(rebutted.ledger[0].status, "rebutted");
  const fixed = close("d2");
  assert.equal(fixed.ledger[0].status, "fixed");
  // only an accepted rebuttal is remembered: a finding the member let go on its own (withdrawn), or in silence on a diff that changed (fixed, whether or not its lines were touched), is the member's again when it raises it
  for (const soft of [withdrawn, fixed]) {
    const back = dropClosed(raise(text, finding("P1", "a.ts:2")), soft.ledger, ran);
    assert.equal(back.dropped.length, 0, soft.ledger[0].status);
    assert.equal(back.results[0].verdict, "block");
  }

  for (const closed of [rebutted]) {
    // (a) the member raises it again on the same quoted text (its line has moved, its indentation changed): dropped, said with the round, and the member's verdict is derived again: approve
    const moved = file("new", "l0", "  the   quoted line ", "l2");
    const again = dropClosed(raise(moved, finding("P1", "a.ts:3")), closed.ledger, ran);
    assert.equal(again.results[0].findings.length, 0);
    assert.equal(again.results[0].verdict, "approve");
    assert.deepEqual(again.dropped.map((d) => [d.role, d.principle, d.where, d.closed, d.how]), [["inspector", "P1", "a.ts:3", 2, closed.ledger[0].status]]);
    // the settled round then has nothing to close
    const settled = settle(again.results, { prior: closed.ledger, round: 3, principles: known, ran, diff: "d3", read: moved });
    assert.equal(settled.unresolved.length, 0);
    assert.equal(exitFor(settled.results, settled), 0);
    // (b) the quoted lines changed: the same finding stands, and the ledger opens it again
    const edited = file("l0", "the quoted line, edited", "l2");
    const reopened = dropClosed(raise(edited, finding("P1", "a.ts:2")), closed.ledger, ran);
    assert.equal(reopened.dropped.length, 0);
    assert.equal(reopened.results[0].verdict, "block");
    const settledEdit = settle(reopened.results, { prior: closed.ledger, round: 3, principles: known, ran, diff: "d3", read: edited });
    assert.equal(settledEdit.unresolved.length, 1);
    // (c) a new finding is kept: another principle, another file, another place; a note is no finding to drop
    for (const other of [finding("P2", "a.ts:2"), finding("P1", "b.ts:2"), finding("P1", "a.ts:3")]) {
      const read = (n) => (n === "b.ts" ? "l0\nthe quoted line\n" : text(n));
      const kept = dropClosed(raise(read, other), closed.ledger, ran);
      assert.equal(kept.dropped.length, 0, other.where + other.principle);
      assert.equal(kept.results[0].verdict, "block");
    }
    // only a finding of the member that closed it: another member's finding of the same place stands
    const another = withAnchors([{ ...answer(finding("P1", "a.ts:2")), role: "warden" }], ["warden"], text);
    assert.equal(dropClosed(another, closed.ledger, ["warden"]).dropped.length, 0);
    // a finding beside a dropped one stays, and keeps the member's block
    const both = dropClosed(raise(text, finding("P1", "a.ts:2"), finding("P2", "a.ts:3", "fix")), closed.ledger, ran);
    assert.deepEqual(both.results[0].findings.map((x) => x.principle), ["P2"]);
    assert.equal(both.results[0].verdict, "block");
  }
  // a finding still OPEN is not closed: raised again it stays; so does a finding with no anchor, and an entry written before anchors were kept never matches
  assert.equal(dropClosed(raise(text, finding("P1", "a.ts:2")), r1.ledger, ran).dropped.length, 0);
  assert.equal(dropClosed(raise(text, finding("P1", "a.ts")), rebutted.ledger, ran).dropped.length, 0);
  assert.equal(dropClosed(raise(text, finding("P1", "a.ts:2")), rebutted.ledger.map(({ anchor, ...e }) => e), ran).dropped.length, 0);
  // a member that did not answer this round is not touched
  assert.equal(dropClosed(withAnchors([answer(finding("P1", "a.ts:2"))], [], text), rebutted.ledger, []).dropped.length, 0);
});

// the same through the real `council` command and a gateway: round 1 raises two findings; round 2 lets one go (a rebuttal it accepts); round 3 raises both again and the closed one is dropped, said as closed in round 2; once its quoted line is edited it stands.
test("council memory, through the command: a finding closed by an accepted rebuttal, raised again on unchanged lines is dropped and printed; after its lines change it is kept", async () => {
  let answer = "";
  const gateway = createServer((req, res) => {
    req.on("data", () => {});
    req.on("end", () => {
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ choices: [{ finish_reason: "stop", message: { content: answer } }] }));
    });
  });
  await new Promise((r) => gateway.listen(0, "127.0.0.1", r));
  const w = work();
  try {
    const x = "server/src/authority/x.ts";
    w.change(x, 40);
    const via = { COUNCIL_MODEL_URL: `http://127.0.0.1:${gateway.address().port}/api/v1` };
    const one = `- P1 | block | ${x}:3 | the first | delete it`;
    const two = `- P2 | block | ${x}:10 | the second | delete it`;
    const say = (...fs) => `verdict: request-changes\nhead: x\nfindings:\n${fs.join("\n")}`;
    answer = say(one, two);
    const r1 = await w.runVia(via, "--loop");
    assert.equal(r1.status, 18, r1.stdout + r1.stderr);
    answer = say(two);
    const r2 = await w.runVia(via, "--loop", "--rebut", "warden=P1: the first is as it should be");
    assert.equal(r2.status, 18, r2.stdout + r2.stderr);
    assert.match(r2.stdout, /council: closed  warden \w+ P1 .*=rebutted r2, accepted by warden/);
    answer = say(one, two);
    const r3 = await w.runVia(via, "--loop");
    assert.equal(r3.status, 18, r3.stdout + r3.stderr);
    assert.match(r3.stdout, new RegExp(`council: dropped  warden: P1 ${x}:3: closed in round 2`));
    assert.doesNotMatch(r3.stdout, /to close {2}warden: BLOCK P1/);
    assert.match(last(r3).trailer, /, dropped \(re-raised after an accepted rebuttal\): warden P1 r2\)$/, "the evidence on the commit says what was dropped");
    assert.deepEqual(last(r3).dropped.map((d) => [d.role, d.principle, d.where, d.closed, d.how]), [["warden", "P1", `${x}:3`, 2, "rebutted"]], "the JSON says what the tool dropped");
    assert.match(r3.stdout, /to close {2}warden: BLOCK P2/);
    // the quoted line changes: the same finding is the member's again
    const file = readFileSync(join(w.dir, x), "utf8").replace("l2\n", "l2 edited\n");
    writeFileSync(join(w.dir, x), file);
    w.git("commit", "-qa", "--amend", "-m", "change");
    answer = say(one, two);
    const r5 = await w.runVia(via, "--loop");
    assert.equal(r5.status, 18, r5.stdout + r5.stderr);
    assert.doesNotMatch(r5.stdout, /council: dropped/);
    assert.match(r5.stdout, /to close {2}warden: BLOCK P1/);
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
    gateway.close();
  }
});

test("a member is given every changed file whole, none of them special (a lock file is read like any other), each within the file cap and all within the total", () => {
  const files = { "a.txt": "A".repeat(10), "package-lock.json": "{}", "big.bin": "B".repeat(50), "gone.txt": null, "late.txt": "L" };
  const got = wholeFiles((p) => files[p] ?? null, ["a.txt", "package-lock.json", "gone.txt", "big.bin", "late.txt"], 20, 30);
  assert.match(got, /=== a\.txt \(whole, at the head\) ===\nAAAAAAAAAA/);
  assert.match(got, /=== package-lock\.json \(whole, at the head\) ===\n\{\}/);
  assert.doesNotMatch(got, /gone\.txt/, "a file that is not there is not given");
  assert.match(got, /B{20}\n\[\.\.\. cut at 20 characters\]/, "a long file is cut at the file cap");
  assert.doesNotMatch(got, /late\.txt/, "once the total is used, no more is given");
});

// Admission: one round reports EVERY finding of EVERY member in one block, so the builder fixes them all at once and does not meet the second member's findings a round later. It would fail if a
// member's findings were dropped once another member had blocked, or if the exit hid the second member. Oracle: the real memberLines and exitFor on two members, each with an open finding.
test("a round prints every member's findings together", async () => {
  const { memberLines, exitFor } = await import("./council-lib.mjs");
  const mk = (role, why) => ({
    role,
    verdict: "block",
    class: "fast",
    model: "m/x",
    seconds: 3,
    why: "",
    findings: [{ severity: "fix", principle: "P1", where: `server/${role}.ts:1`, why, fix: "delete it" }],
  });
  const results = [mk("inspector", "a second fold"), mk("warden", "a secret in argv"), mk("steward", "a law broken")];
  const text = results.flatMap(memberLines).join("\n");
  for (const w of ["a second fold", "a secret in argv", "a law broken"]) {
    assert.ok(text.includes(w), w);
  }
  assert.equal(exitFor(results, { unresolved: results, refused: false }), 18);
});

// Admission: the council reads exactly two things, the project's principles (.spire/principles.md) and the reviewers' viewpoints (its .spire/review/<role>.md over the tool's defaults), at the base commit, and nothing
// else. A scratch project with its own principles, an overriding and an added viewpoint, a viewpoint that `reads` a rubric, and files that are NOT law (an AGENTS.md, a docs/ file no viewpoint names, a file
// in .spire/review/ that is no viewpoint, a viewpoint file whose role does not match its name) gets a council whose prompt holds the project's law and the viewpoint, and none of the rest. It would fail if the council
// still read an AGENTS.md or an imports folder, took the default viewpoint over the project's, or read a third file. Oracle: the real `council` process, real git, a real HTTP gateway that records what the member was given.
test("the council reads the project's principles and the reviewers' viewpoints and nothing else: an override replaces the default, a third file is not read, a project without a viewpoint of its own sits the default", async () => {
  const seen = [];
  const gateway = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const msgs = JSON.parse(body).messages;
      const system = msgs.find((m) => m.role === "system").content;
      seen.push({ system, user: msgs.at(-1).content });
      // the member of this scratch project cites ITS principle 4 (one the default does not hold)
      const cite = /KIT-PRINCIPLE-FOUR/.test(system) ? "P4" : "P99";
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ choices: [{ finish_reason: "stop", message: { content: `verdict: request-changes\nhead: x\nfindings:\n- ${cite} | fix | src/a.ts:1 | a duplicate | delete it` } }] }));
    });
  });
  await new Promise((r) => gateway.listen(0, "127.0.0.1", r));
  const dir = mkdtempSync(join(tmpdir(), "council-kit-"));
  const env = {
    PATH: `${dirname(process.execPath)}:/usr/bin:/bin`,
    HOME: dir,
    GIT_AUTHOR_NAME: "t",
    GIT_AUTHOR_EMAIL: "t@t",
    GIT_COMMITTER_NAME: "t",
    GIT_COMMITTER_EMAIL: "t@t",
    COUNCIL_MODEL_URL: `http://127.0.0.1:${gateway.address().port}/api/v1`,
  };
  const git = (...a) => execFileSync("git", a, { cwd: dir, env, encoding: "utf8" });
  const w = (p, t) => {
    mkdirSync(join(dir, p, ".."), { recursive: true });
    writeFileSync(join(dir, p), t);
  };
  try {
    cpSync(join(repo, "tool"), join(dir, ".spire/tool"), { recursive: true });
    cpSync(join(repo, "tool/fixtures/project/scripts/ci/code-scope.mjs"), join(dir, "scripts/ci/code-scope.mjs"));
    mkdirSync(join(dir, "config"));
    cpSync(join(repo, "tool/fixtures/model-registry.json"), join(dir, "config/models.json"));
    w(".spire/land.json", JSON.stringify({
      version: 1,
      paths: { tests: ["**/*.test.ts"], prose: ["**/*.md"], notProd: ["**/*.md"] },
      sensitive: { globs: [], nameGlobs: [], always: [] },
      areas: [{ name: "all", paths: ["**"], steps: [{ id: "ok", dir: ".", cmd: ["true"] }] }],
      review: { models: "config/models.json" },
    }));
    w(".spire/principles.md", "KIT-PRINCIPLES-FILE\n\n1. **Kit one.** a\n2. **Kit two.** b\n3. **Kit three.** c\n4. **Kit four.** KIT-PRINCIPLE-FOUR\n");
    w(".spire/review/warden.md", '---\nrole: warden\ncalls: ["src/**"]\nenforces: [4]\nreads: ["docs/rubric.md"]\n---\n# KIT-WARDEN-VIEWPOINT\ncan this change be abused?\n');
    w("docs/rubric.md", "KIT-RUBRIC-TEXT\n");
    // NOT law: none of these may reach a prompt
    w("AGENTS.md", "KIT-AGENTS-MD-NOT-READ\n");
    w("docs/other-law.md", "KIT-OTHER-LAW-NOT-READ\n");
    w(".spire/review/extra.md", "KIT-THIRD-FILE-NOT-READ (no front matter: not a viewpoint)\n");
    w(".spire/review/shared.md", "---\nrole: not-shared\n---\nKIT-MISNAMED-NOT-READ\n");
    w("src/a.ts", "export const a = 1;\n");
    git("init", "-q", "-b", "main");
    git("add", "-A");
    git("commit", "-qm", "seed");
    w("src/a.ts", Array.from({ length: 12 }, (_, i) => `export const a${i} = ${i};`).join("\n") + "\n");
    git("add", "-A");
    git("commit", "-qm", "change");
    const go = (...more) =>
      new Promise((resolve) =>
        execFile(process.execPath, [join(dir, ".spire/tool/council.mjs"), "--base", "HEAD~1", ...more], { cwd: dir, encoding: "utf8", env }, (e, stdout, stderr) =>
          resolve({ status: e ? e.code : 0, stdout, stderr }),
        ),
      );
    const r = await go("--roles", "warden");
    assert.equal(r.status, 18, r.stdout + r.stderr); // the member cited P4, which only the PROJECT's principles hold: the finding is real and stops
    assert.match(r.stdout, /FIX P4 /);
    assert.equal(seen.length, 1);
    const prompt = seen[0].system;
    for (const t of ["KIT-PRINCIPLES-FILE", "KIT-WARDEN-VIEWPOINT", "KIT-RUBRIC-TEXT", "You enforce principles P4."]) {
      assert.ok(prompt.includes(t), `the member was given ${t}`);
    }
    for (const t of ["KIT-AGENTS-MD-NOT-READ", "KIT-OTHER-LAW-NOT-READ", "KIT-THIRD-FILE-NOT-READ", "KIT-MISNAMED-NOT-READ", "Can this change be abused to reach a harm?"]) {
      assert.ok(!prompt.includes(t), `the member was not given ${t} (not law; and the project's viewpoint replaced the default whole)`);
    }
    // a project with NO viewpoint of its own sits the tool's default, and the same principles
    git("rm", "-q", ".spire/review/warden.md");
    git("commit", "-qm", "drop the override");
    w("src/a.ts", "export const again = 1;\n");
    git("add", "-A");
    git("commit", "-qm", "a change after the override is gone");
    seen.length = 0;
    const d = await go("--base", "HEAD~1", "--roles", "warden");
    assert.ok(seen.length >= 1, d.stdout + d.stderr);
    assert.ok(seen[0].system.includes("**Can this change be abused to reach a harm?**"), "the default viewpoint");
    assert.ok(seen[0].system.includes("KIT-PRINCIPLES-FILE"));
    // a role nobody defines is refused, naming the reviewers there are
    const u = await go("--roles", "nobody");
    assert.equal(u.status, 2);
    assert.match(u.stdout, /no reviewer named nobody \(reviewers: .*warden/);
    // a project with no principles file is judged against the tool's named default principles: said plainly, and a finding that cites a number the default does not hold is a note (exit 0)
    git("rm", "-q", ".spire/principles.md");
    git("commit", "-qm", "no law");
    w("src/a.ts", "export const changed = true;\n");
    git("add", "-A");
    git("commit", "-qm", "a change with no law on main");
    seen.length = 0;
    const n = await go("--base", "HEAD~1", "--roles", "warden");
    assert.equal(n.status, 0, n.stdout + n.stderr); // the member cited P99, which the default principles (the defaults) do not hold: a note
    assert.match(n.stdout, /no \.spire\/principles\.md/);
    assert.match(n.stdout, /cites no principle of the project: a note/);
    assert.equal(seen.length, 1);
    assert.ok(seen[0].system.includes("One owner, one path, one definition."), "the default principles were given");
  } finally {
    rmSync(dir, { recursive: true, force: true });
    gateway.close();
  }
});
