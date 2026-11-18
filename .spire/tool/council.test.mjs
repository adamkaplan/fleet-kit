// Admission: this file shows what the council decides without a model (.spire/tool/council-lib.mjs) and what the real `council`
// command does with a real git change and no model call: a docs change sits no member and exits 0 with its trailer; a server change with no key
// exits 0 too, with each member recorded as unable to review. It would fail if the roster, the model pick (never the author's own), the verdict derivation (a block finding
// is advice whatever the member's first line says; a finding that cites no principle number is a note), the exit (never a stop) or the trailer broke.
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
  COUNCIL_EFFORT_DEFAULT,
  mergeClasses,
  GATEWAY_BEARER,
  hasAdvice,
  modelRoute,
  parseVerdict,
  pickModel,
  stripFrontMatter,
  trailerText,
  wholeFiles,
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

test("COUNCIL_EFFORT_DEFAULT is 'low', the effort kept for a class that names none", () => {
  assert.equal(COUNCIL_EFFORT_DEFAULT, "low");
  assert.equal(pickModel({ fast: { model: "a/f" } }, "", undefined).effort, COUNCIL_EFFORT_DEFAULT);
});

test("mergeClasses: a class's model and effort are overridden, the other classes and fields stay the registry's", () => {
  const reg = { fast: { model: "a/f" }, everyday: { model: "a/e", reasoningEffort: "low" } };
  const out = mergeClasses(reg, '{"everyday":{"model":"b/e2","reasoningEffort":"high"}}');
  assert.deepEqual(out.everyday, { model: "b/e2", reasoningEffort: "high" });
  assert.deepEqual(out.fast, reg.fast);
  assert.deepEqual(mergeClasses(reg, '{"fast":{"reasoningEffort":"medium"}}').fast, { model: "a/f", reasoningEffort: "medium" });
  assert.deepEqual(mergeClasses(reg, '{"extra":{"model":"c/x"}}').extra, { model: "c/x" });
  assert.deepEqual(reg.everyday, { model: "a/e", reasoningEffort: "low" }); // the registry object is not mutated
});

test("mergeClasses fails loudly with a named error on a malformed value", () => {
  const reg = { fast: { model: "a/f" } };
  for (const bad of ["not json", "[]", "null", '"x"', '{"fast":"x"}', '{"fast":{"model":3}}', '{"fast":{"reasoningEffort":""}}']) {
    assert.throws(() => mergeClasses(reg, bad), { name: "CouncilModelsError", message: /^COUNCIL_MODELS: / }, bad);
  }
});

// Admission: the host's COUNCIL_EFFORT and COUNCIL_MODELS reach the member's real call, and a malformed one stops the council with exit 2.
// Oracle: the real `council` process against a gateway that records the model and effort of each request.
test("the council command: COUNCIL_EFFORT overrides the class effort, COUNCIL_MODELS overrides per class, a malformed one exits 2", async () => {
  const seen = [];
  const gateway = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const j = JSON.parse(body);
      seen.push(`${j.model}:${j.reasoning.effort}`);
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ choices: [{ finish_reason: "stop", message: { content: "verdict: approve\nhead: x\nfindings:\n" } }] }));
    });
  });
  await new Promise((r) => gateway.listen(0, "127.0.0.1", r));
  const env = { COUNCIL_MODEL_URL: `http://127.0.0.1:${gateway.address().port}/api/v1` };
  const w = work();
  try {
    w.change("server/src/x.ts", 5);
    const go = (e) => w.runVia({ ...env, ...e }, "--roles", "warden");
    await go({});
    await go({ COUNCIL_MODELS: '{"fast":{"model":"host/m","reasoningEffort":"high"}}' });
    await go({ COUNCIL_MODELS: '{"fast":{"model":"host/m","reasoningEffort":"high"}}', COUNCIL_EFFORT: "minimal" });
    await go({ COUNCIL_MODELS: '{"everyday":{"model":"host/other"}}' }); // another class: fast stays the registry's
    await go({ COUNCIL_MODELS: '{"fast":{"model":"env/m"}}' }); // model only: the registry's effort stays
    assert.deepEqual(seen, [
      "vendor/fast-1:low",
      "host/m:high",
      "host/m:minimal",
      "vendor/fast-1:low",
      "env/m:low",
    ]);
    const bad = await go({ COUNCIL_MODELS: "{nope" });
    assert.equal(bad.status, 2);
    assert.match(bad.stderr + bad.stdout, /CouncilModelsError: COUNCIL_MODELS: not valid JSON/);
    assert.equal(seen.length, 5); // a malformed override made no model call: no fallback
    await go({ COUNCIL_MODELS: '{"fast":{"model":"host/m","reasoningEffort":"high"}}', COUNCIL_EFFORT: "  " }); // blank counts as unset
    assert.equal(seen.at(-1), "host/m:high");
  } finally {
    gateway.close();
  }
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
  assert.equal(v.verdict, "advice"); // the member said approve; a block finding is advice
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
    "advice", // a finding rated fix is advice like a block
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

test("the trailer records the advice (member, rating, principle number) and a member that could not answer as unable to review; nothing here stops a landing", () => {
  const f = (severity, principle) => ({ severity, principle });
  const r = (role, verdict, findings = [], why = "") => ({ role, verdict, class: "fast", findings, why });
  assert.equal(hasAdvice([r("inspector", "approve"), r("warden", "advice", [f("fix", "P1")])]), true);
  assert.equal(hasAdvice([r("inspector", "comment")]), false);
  assert.equal(trailerText([], ""), "none needed (no path calls for a member)");
  assert.equal(
    trailerText([r("inspector", "approve"), r("warden", "comment")], "abc"),
    "ran: inspector=approve,warden=approve (models fast, diff abc)",
  );
  assert.equal(
    trailerText([r("inspector", "advice", [f("fix", "P1"), f("block", "P3"), f("note", "P4")]), r("warden", "approve")], "abc"),
    "ran: inspector=advice(fix P1,block P3),warden=approve (models fast, diff abc)",
  );
  assert.equal(
    trailerText([r("inspector", "incomplete", [], "no answer in 150s"), r("warden", "incomplete", [], "no COUNCIL_MODEL_URL in the environment: whoever starts the command sets it")], "abc"),
    "ran: inspector=unable to review (no answer in 150s),warden=unable to review (no COUNCIL_MODEL_URL in the environment) (models fast, diff abc)",
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

test("the command: the Warden reads a docs-only change too (no key: unable to review, exit 0, recorded in the trailer); a project that removes every member gets none needed (0)", () => {
  const w = work();
  const none = work();
  try {
    w.change(`${DOCS}/a.md`, 3);
    const docs = w.run();
    assert.equal(docs.status, 0, docs.stdout + docs.stderr); // no route is a note, never a stop
    assert.match(docs.stdout, /no COUNCIL_MODEL_URL/);
    assert.match(docs.stdout, /UNABLE TO REVIEW/);
    assert.match(docs.stdout, /did not block/);
    assert.match(last(docs).trailer, /^ran: warden=unable to review \(no COUNCIL_MODEL_URL in the environment\)/);
    assert.equal(last(docs).result, "unable-to-review");
    assert.match(docs.stdout, /whoever starts the command sets it/, "the sentence names its cause");
    w.change("server/src/authority/x.ts", 40);
    const srv = w.run();
    assert.equal(srv.status, 0, srv.stdout + srv.stderr);
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

// Admission: this test shows the council command's advice end to end against a scripted model gateway: a seeded duplicate rated `fix` is ADVICE, never a gate. Every run prints it ONCE, says it did not
// block, exits 0 and records it in the trailer (member, rating, principle number); there is no state between runs and no second run is asked for. A member whose model call is refused is recorded as
// unable to review and the run exits 0. It would fail if a finding or a silent member ever stopped a run, or printed the advice twice.
// Oracle: the real `council` process, real git, a real HTTP gateway whose answers are decided by what the member was given.
test("the council advice against a gateway: printed once and recorded, never a gate; a refused member is unable to review, never a stop", async () => {
  let refuse = false;
  const gateway = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      if (refuse) {
        res.statusCode = 400;
        res.end("{}");
        return;
      }
      const user = JSON.parse(body).messages.at(-1).content;
      const text = typeof user === "string" ? user : JSON.stringify(user);
      const seeded = /^\/\/ DUPLICATE_OF_FOLD$/m.test(text); // in the file whole, not in a diff line (which starts with + - or a space)
      const answer = seeded
        ? "verdict: request-changes\nhead: x\nfindings:\n- P1 | fix | server/src/x.ts:3 | a second definition of the fold | delete it"
        : "verdict: approve\nhead: x\nfindings:\n";
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ choices: [{ finish_reason: "stop", message: { content: answer } }] }));
    });
  });
  await new Promise((r) => gateway.listen(0, "127.0.0.1", r));
  const env = { COUNCIL_MODEL_URL: `http://127.0.0.1:${gateway.address().port}/api/v1` };
  const go = (w, ...more) => w.runVia(env, "--roles", "warden", "--base", w.base, ...more);
  const seed = (w, dup) => {
    mkdirSync(join(w.dir, "server/src"), { recursive: true });
    writeFileSync(join(w.dir, "server/src/x.ts"), `${Array.from({ length: 40 }, (_, i) => `l${i}`).join("\n")}\n${dup ? "// DUPLICATE_OF_FOLD\n" : ""}`);
    w.git("add", "-A");
    w.git("commit", "-qm", dup ? "seed a duplicate" : "repair it");
  };
  try {
    const w = work();
    try {
      w.change("server/src/x.ts", 40);
      w.base = w.git("rev-parse", "HEAD~1").trim();
      seed(w, true);
      for (let i = 0; i < 3; i++) {
        const r = await go(w);
        assert.equal(r.status, 0, r.stdout + r.stderr); // however often it is run: never a gate
        assert.match(r.stdout, /ADVICE \(1 advice\)/);
        assert.equal(r.stdout.match(/FIX P1 \| server\/src\/x\.ts:3/g)?.length, 1, "the advice is printed once");
        assert.match(r.stdout, /did not block/);
        assert.doesNotMatch(r.stdout, /returned to you/);
        assert.equal(last(r).result, "advice");
        assert.match(last(r).trailer, /^ran: warden=advice\(fix P1\) \(models fast, diff [0-9a-f]+\)$/);
      }
      // changing the commit (the fix) is the other answer; it is clean
      seed(w, false);
      const ok = await go(w);
      assert.equal(ok.status, 0, ok.stdout + ok.stderr);
      assert.match(last(ok).trailer, /^ran: warden=approve \(models fast, diff [0-9a-f]+\)$/);
      // a member whose model call is refused: recorded, said not to block, exit 0
      refuse = true;
      const silent = await go(w);
      assert.equal(silent.status, 0, silent.stdout + silent.stderr);
      assert.match(silent.stdout, /UNABLE TO REVIEW/);
      assert.match(silent.stdout, /did not block/);
      assert.equal(last(silent).result, "unable-to-review");
      assert.match(last(silent).trailer, /^ran: warden=unable to review \(model call refused \(400\)\) \(models fast, diff [0-9a-f]+\)$/);
    } finally {
      rmSync(w.dir, { recursive: true, force: true });
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
// does not hold, or a line outside the contract (a free-text "BLOCK: ..."), is a NOTE: the verdict is comment, and no round count is involved. A member that said request-changes and
// gave nothing at all has not answered (incomplete: recorded as unable to review), never approve. They would fail if an uncited line, however it was rated or worded, stopped a landing. Oracle: the real parsers
// (council-lib.mjs) on answers shaped like the ones real members give; the command itself is run in the next test.
test("a finding with no citation is a note and is never advice; one that cites a principle of the project is advice; an empty request-changes is incomplete", () => {
  const p = "1. **Simplicity first.** y";
  const read = (text) => parseVerdict(text, p);
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
    assert.equal(hasAdvice([v]), false, text);
  }
  // the same BLOCK with a citation of the project's principle is advice
  const cited = read("verdict: request-changes\nfindings:\n- P1 | block | a.ts:1 | w | d");
  assert.equal(cited.verdict, "advice");
  assert.equal(hasAdvice([cited]), true);
  // nothing read at all from a member that asked for changes: it did not answer
  assert.equal(read("verdict: request-changes\nfindings:\n").verdict, "incomplete");
  // what stays what it was: none, a read finding, an elaboration under a read finding
  assert.equal(read("verdict: approve\nfindings:\n- none").verdict, "approve");
  assert.equal(read("verdict: approve\nfindings:\n").verdict, "approve");
  assert.equal(read("verdict: comment\nfindings:\n- P1 | note | a.ts:1 | w | d\n- see also the earlier note").verdict, "comment");
});

// the same rule through the real `council` command and a gateway: a member that answers BLOCK with no citation is a note; the same block citing P1 is advice, printed and recorded; both exit 0.
test("the council command: a BLOCK that cites no principle is a note; a BLOCK citing one is advice, recorded with its principle; neither stops (0)", async () => {
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
    const note = await w.runVia(via);
    assert.equal(note.status, 0, note.stdout + note.stderr);
    assert.match(note.stdout, /cites no principle of the project: a note/);
    assert.equal(last(note).result, "clean");
    answer = "verdict: request-changes\nhead: x\nfindings:\n- P1 | block | server/src/authority/x.ts:3 | a second definition | delete it";
    const advice = await w.runVia(via);
    assert.equal(advice.status, 0, advice.stdout + advice.stderr);
    assert.equal(last(advice).result, "advice");
    assert.match(last(advice).trailer, /warden=advice\(block P1\)/);
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

// Admission: one round reports EVERY finding of EVERY member in one block, so the builder sees them all at once and does not meet the second member's findings a round later. It would fail if a
// member's findings were dropped once another member had given advice, or if the exit hid the second member. Oracle: the real memberLines on three members, each with an open finding.
test("a round prints every member's findings together", async () => {
  const { memberLines } = await import("./council-lib.mjs");
  const mk = (role, why) => ({
    role,
    verdict: "advice",
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
    assert.equal(r.status, 0, r.stdout + r.stderr); // the member cited P4, which only the PROJECT's principles hold: the finding is real advice (printed; never a gate)
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

test("the no-route sentence names its cause: a value that is no URL, or none set by whoever started the command; never a value", async () => {
  const { noRouteCause } = await import("./council-lib.mjs");
  assert.match(noRouteCause({ COUNCIL_MODEL_URL: "https://u:secret@" }), /is not an http\(s\) base URL/);
  assert.doesNotMatch(noRouteCause({ COUNCIL_MODEL_URL: "relay-host" }), /relay-host/);
  assert.match(noRouteCause({}), /no COUNCIL_MODEL_URL in the environment/);
});

test("a COUNCIL_MODEL_URL that is set but is no URL is said so, not called absent, and the value is never printed", async () => {
  const w = work();
  try {
    w.change(`${DOCS}/a.md`, 3);
    const r = await w.runVia({ COUNCIL_MODEL_URL: "relay-host" });
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /COUNCIL_MODEL_URL is set but is not an http\(s\) base URL/);
    assert.doesNotMatch(r.stdout, /no COUNCIL_MODEL_URL in the environment|relay-host/);
  } finally {
    rmSync(w.dir, { recursive: true, force: true });
  }
});
