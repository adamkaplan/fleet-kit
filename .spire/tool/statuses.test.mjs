// Admission: this file shows that the lander writes the attestation twice while the commit statuses are rolled out (janus-infra/janus-platform#1009): the Land-Checks / Land-Council trailers stay in the commit message AND
// the same record is posted as commit statuses (land/checks, land/council) by the fleet's App on the exact commit that becomes main's tip, in this order: the commits are pushed to a temporary ref refs/land/<run>, the
// statuses are posted, main is fast-forwarded, the temporary ref is deleted (on every way out). A ruleset that requires the statuses (the stub's `requireStatuses`) therefore sees them on the pushed sha.
// It shows that an app that may not post statuses is SAID once ("statuses not permitted: trailer only") and the landing goes on by the trailers, unless FLEET_LAND_STATUSES=required, which stops it before main moves;
// that FLEET_LAND_STATUSES=off writes none; and that `spire attest --statuses` reads the statuses back and says, per commit, trailer / status / both.
// It would fail if the order changed, if the status went to another sha than main's tip, if a refusal were skipped in silence, if a temporary ref were left behind, if the statuses token could push or the pushing token could post,
// or if a secret were printed. Oracle: the real `push` and `attest.mjs` of a copy of this tool against the stub of GitHub (fixtures/app-stub.mjs: statuses API, the ruleset, the order of events).
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { API_URL_ENV, APP_ID_ENV, APP_KEY_ENV } from "./app-env.mjs";
import { summarize } from "./attest.mjs";
import { statusesAttest, statusesOf } from "./statuses.mjs";
import { git, leaks, run, world } from "./fixtures/app-world.mjs";

const attestJs = fileURLToPath(new URL("attest.mjs", import.meta.url));
const COUNCIL = "ran: warden=approve";
/** Put the record `land` writes on the tip of the clone: Land-Checks of the tip's own tree, Land-Council. */
const attested = (w) => {
  const tree = git(w.clone, "rev-parse", "HEAD^{tree}").slice(0, 12);
  const msg = git(w.clone, "log", "-1", "--format=%B");
  const line = `areas=server,app,docs tests=363 files=35 checks=tests:server(31):140s,tests:app(2):kept,self(1):kept,tests:docs(2):kept,typecheck(1):12s total=140s tree=${tree}`; // the gate's own shape: the tree is LAST and the line is longer than a description may be
  execGit(w.clone, ["commit", "-q", "--allow-empty", "--amend", "-F", "-"], `${msg}\n\nLand-Checks: ${line}\nLand-Council: ${COUNCIL}\n`);
  return { line, tree };
};
const execGit = (cwd, a, input) => execFileSync("git", a, { cwd, input, env: { ...process.env, GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t" } });
const seq = (w) => w.stub.calls.events.filter((e) => e.type !== "mint");
const brief = (e) => (e.type === "ref" ? `${/^refs\/land\//.test(e.ref) ? "refs/land/*" : e.ref} ${/^0+$/.test(e.new) ? "deleted" : /^0+$/.test(e.old) ? "created" : "updated"}` : e.type === "status" ? `status ${e.context} ${e.state}` : e.type);
const landRefs = (w) => git(w.bare, "for-each-ref", "--format=%(refname)", "refs/land");
const RULESET = ["land/checks", "land/council"];

test("dual write: the commits go to refs/land/<run>, then land/checks and land/council on the tip, then main is fast-forwarded, then the temporary ref is deleted; the trailers stay in the message; the ruleset's view of main's tip holds the statuses", async () => {
  const w = await world({ stubOptions: { requireStatuses: RULESET } });
  try {
    const { line } = attested(w);
    const tip = git(w.clone, "rev-parse", "HEAD");
    const r = await w.push();
    const out = `${r.stdout}\n${r.stderr}`;
    assert.equal(r.status, 0, out);
    assert.equal(w.main_(), tip);
    assert.deepEqual(seq(w).map(brief), ["refs/land/* created", "status land/checks success", "status land/council success", "refs/heads/main updated", "refs/land/* deleted"]);
    const ev = seq(w);
    assert.match(ev[0].ref, /^refs\/land\/run-[\w-]+$/);
    assert.equal(ev[4].ref, ev[0].ref, "the temporary ref that was made is the one deleted");
    assert.equal(ev[0].new, tip);
    assert.deepEqual([ev[1].sha, ev[2].sha, ev[3].new], [tip, tip, tip], "the statuses are on the sha that becomes main's tip");
    assert.ok(line.length > 140, "the test's line must not fit");
    assert.match(ev[1].description, /^tree=[0-9a-f]{12} areas=server,app,docs /, "the tree is at the front, so the cut of a long line cannot take it");
    assert.match(ev[1].description, /\(override: none\)$/);
    assert.ok(ev[1].description.includes(`tree=${git(w.clone, "rev-parse", "HEAD^{tree}").slice(0, 12)}`));
    assert.ok(ev[1].description.length <= 140 && ev[2].description.length <= 140);
    assert.ok(ev[2].description.includes(COUNCIL));
    assert.equal(landRefs(w), "", "no temporary ref is left");
    const body = git(w.bare, "log", "-1", "--format=%B", "main");
    assert.match(body, /^Land-Checks: .*tree=[0-9a-f]{12}/m, "the trailers stay (dual write)");
    assert.match(body, new RegExp(`^Land-Council: ${COUNCIL}$`, "m"));
    assert.match(r.stdout, /push: statuses: land\/checks and land\/council posted on [0-9a-f]{12} by app stub-app/);
    // two tokens, each for one thing: the one that pushes cannot post, the one that posts cannot push
    const asked = w.stub.calls.events.filter((e) => e.type === "mint").map((e) => e.permissions);
    assert.ok(asked.some((p) => JSON.stringify(p) === '{"contents":"write"}'));
    assert.ok(asked.some((p) => JSON.stringify(p) === '{"statuses":"write"}'));
    assert.ok(!asked.some((p) => p.contents && p.statuses));
    assert.deepEqual(leaks(out), []);
  } finally { await w.done(); }
});

test("a push of several commits: the statuses are on the tip that becomes main (after the stamp), once, and an earlier commit is part-of the tip's record", async () => {
  const w = await world({ stubOptions: { requireStatuses: RULESET } });
  try {
    execGit(w.clone, ["commit", "-q", "--allow-empty", "-m", "second"]);
    attested(w);
    const r = await w.push();
    assert.equal(r.status, 0, `${r.stdout}${r.stderr}`);
    const tip = w.main_();
    const posted = seq(w).filter((e) => e.type === "status");
    assert.deepEqual(posted.map((e) => [e.context, e.sha]), [["land/checks", tip], ["land/council", tip]]);
    assert.match(git(w.bare, "log", "-2", "--format=%B", "main"), /part-of=[0-9a-f]{12}/, "the earlier commit says it is part of the push");
    assert.equal(landRefs(w), "");
  } finally { await w.done(); }
});

for (const [name, mode] of [["the token is refused the status (403)", "forbidden"], ["the app has no statuses permission at all (the token cannot be made)", "none"]]) {
  test(`${name}: 'statuses not permitted: trailer only' is said ONCE, the commit lands by its trailers, the temporary ref is gone`, async () => {
    const w = await world({ stubOptions: { statuses: mode } });
    try {
      attested(w);
      const tip = git(w.clone, "rev-parse", "HEAD");
      const r = await w.push();
      const out = `${r.stdout}\n${r.stderr}`;
      assert.equal(r.status, 0, out);
      assert.equal(out.split("statuses not permitted: trailer only").length - 1, 1, out);
      assert.equal(w.main_(), tip);
      assert.deepEqual(seq(w).map(brief).filter((b) => b !== "refs/heads/main updated"), ["refs/land/* created", "refs/land/* deleted"]);
      assert.equal(seq(w).filter((e) => e.type === "status").length, 0);
      assert.equal(landRefs(w), "");
      assert.match(git(w.bare, "log", "-1", "--format=%B", "main"), /^Land-Checks: /m);
      assert.deepEqual(leaks(out), []);
    } finally { await w.done(); }
  });

  test(`${name}, FLEET_LAND_STATUSES=required: the landing STOPS (exit 1) before main moves, and says what to do; the temporary ref is gone`, async () => {
    const w = await world({ stubOptions: { statuses: mode } });
    try {
      attested(w);
      const before = w.main_();
      const r = await w.push({ FLEET_LAND_STATUSES: "required" });
      const out = `${r.stdout}\n${r.stderr}`;
      assert.equal(r.status, 1, out);
      assert.match(out, /statuses not permitted: FLEET_LAND_STATUSES=required, so nothing lands/);
      assert.equal(w.main_(), before);
      assert.equal(seq(w).filter((e) => e.type === "ref" && e.ref === "refs/heads/main").length, 0);
      assert.equal(landRefs(w), "");
    } finally { await w.done(); }
  });
}

test("FLEET_LAND_STATUSES=off writes no status and makes no temporary ref: the trailer path as before", async () => {
  const w = await world();
  try {
    attested(w);
    const r = await w.push({ FLEET_LAND_STATUSES: "off" });
    assert.equal(r.status, 0, `${r.stdout}${r.stderr}`);
    assert.deepEqual(seq(w).map(brief), ["refs/heads/main updated"]);
    assert.doesNotMatch(`${r.stdout}${r.stderr}`, /push: (no )?statuses|statuses not permitted/);
  } finally { await w.done(); }
});

test("a tip with no Land-Checks / Land-Council (nothing to attest) posts nothing, and says so; with FLEET_LAND_STATUSES=required it stops", async () => {
  const w = await world();
  try {
    const r = await w.push();
    assert.equal(r.status, 0, `${r.stdout}${r.stderr}`);
    assert.match(`${r.stdout}${r.stderr}`, /push: no statuses: the tip has no Land-Checks and Land-Council to post/);
    assert.deepEqual(seq(w).map(brief), ["refs/heads/main updated"]);
  } finally { await w.done(); }
  const s = await world();
  try {
    const before = s.main_();
    const r = await s.push({ FLEET_LAND_STATUSES: "required" });
    assert.equal(r.status, 1);
    assert.equal(s.main_(), before);
  } finally { await s.done(); }
});

test("the ruleset refuses main: the landing stops with the remote's refusal, and the temporary ref is deleted all the same", async () => {
  const w = await world({ stubOptions: { requireStatuses: ["land/checks", "land/council", "land/other"] } });
  try {
    attested(w);
    const before = w.main_();
    const r = await w.push();
    assert.equal(r.status, 1, `${r.stdout}${r.stderr}`);
    assert.equal(w.main_(), before);
    assert.ok(seq(w).some((e) => e.type === "refused"));
    assert.equal(landRefs(w), "", "no temporary ref is left behind");
    assert.equal(seq(w).filter((e) => e.type === "ref" && e.ref.startsWith("refs/land/") && /^0+$/.test(e.new)).length, 1);
  } finally { await w.done(); }
});

test("statusesOf (pure): the tree= of the gate's line goes first; the line is cut to 140 characters, the override stays; the council's line is cut too", () => {
  const long = `areas=${"a,".repeat(60)} total=9s tree=4b825dc642cb`;
  const [c, k] = statusesOf({ checks: long, council: "x".repeat(300), override: "the owner of the path\nsays so" });
  assert.ok(c.description.startsWith("tree=4b825dc642cb areas=") && c.description.length <= 140 && c.description.endsWith("(override: the owner of the path says so)"), c.description);
  assert.equal(k.description.length, 140);
  assert.equal(statusesOf({ checks: "tree=4b825dc642cb 5/5", council: "ran", override: "" })[0].description, "tree=4b825dc642cb 5/5 (override: none)");
  assert.equal(statusesAttest(statusesOf({ checks: long, council: "ran" }), "4b825dc642cb6eb9a060e54bf8d69288fbee4904").ok, true, "what the status says, attest reads");
});

test("statusesAttest (pure): a status attests a commit when land/checks is success and its tree= is the commit's tree, and land/council is success", () => {
  const tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";
  const ok = [{ context: "land/checks", state: "success", description: "tree=4b825dc642cb 5/5 green in 18s (override: none)" }, { context: "land/council", state: "success", description: "ran: warden=approve" }];
  assert.deepEqual(statusesAttest(ok, tree), { ok: true });
  assert.equal(statusesAttest(ok.slice(0, 1), tree).ok, false, "the council's is missing");
  assert.match(statusesAttest(ok.slice(1), tree).reason, /land\/checks/);
  assert.match(statusesAttest([{ ...ok[0], description: "tree=000000000000 x" }, ok[1]], tree).reason, /tree/);
  assert.match(statusesAttest([{ ...ok[0], state: "failure" }, ok[1]], tree).reason, /failure/);
  assert.equal(statusesAttest(undefined, tree).ok, false);
});

test("summarize with statuses (pure): a commit is both, trailer only, status only or neither; a commit of a push is judged by its tip's status", () => {
  const T1 = "1".repeat(40), T2 = "2".repeat(40), T3 = "3".repeat(40), T4 = "4".repeat(40);
  const rec = (t) => `Land-Checks: tree=${t.slice(0, 12)} ok\nLand-Council: ran: x`;
  const part = (t) => `Land-Checks: tree=${"9".repeat(12)} ok part-of=${t.slice(0, 12)}\nLand-Council: ran: x`;
  const commits = [
    { sha: "a".repeat(40), tree: T1, subject: "both", body: `s\n\n${rec(T1)}` },
    { sha: "b".repeat(40), tree: T2, subject: "trailer only", body: `s\n\n${rec(T2)}` },
    { sha: "c".repeat(40), tree: "5".repeat(40), subject: "status only", body: "s\n" },
    { sha: "d".repeat(40), tree: "6".repeat(40), subject: "neither", body: "s\n" },
    { sha: "e".repeat(40), tree: "7".repeat(40), subject: "inside a push", body: `s\n\n${part(T4)}` },
    { sha: "f".repeat(40), tree: T4, subject: "tip of that push", body: `s\n\n${rec(T4)}` },
  ];
  const good = (tree) => [{ context: "land/checks", state: "success", description: `tree=${tree.slice(0, 12)} ok` }, { context: "land/council", state: "success", description: "ran" }];
  const byTip = { [commits[0].sha]: good(T1), [commits[2].sha]: good("5".repeat(40)), [commits[5].sha]: good(T4) };
  const sum = summarize(commits, byTip);
  assert.deepEqual(sum.statuses, { read: true, both: 3, trailerOnly: 1, statusOnly: 1, neither: 1 });
  assert.deepEqual(sum.byCommit.map((r) => r.record), ["both", "trailer", "status", "neither", "both", "both"]);
  assert.equal(summarize(commits).statuses, undefined, "not asked: nothing about statuses");
});

test("attest --statuses (the real command against the stub): reads the statuses of each push tip with the App's read token and says per commit trailer / status / both; without a token it says so and still audits the trailers", async () => {
  const w = await world();
  try {
    attested(w);
    let r = await w.push();
    assert.equal(r.status, 0, `${r.stdout}${r.stderr}`);
    const land1 = w.main_();
    // a second landing by the trailer path alone
    await run("git", ["fetch", "-q", "origin", "main"], { cwd: w.clone, env: w.env() }); // (async: the stub is in this process)
    execGit(w.clone, ["reset", "-q", "--hard", "origin/main"]);
    execGit(w.clone, ["commit", "-q", "--allow-empty", "-m", "second landing"]);
    attested(w);
    r = await w.push({ FLEET_LAND_STATUSES: "off" });
    assert.equal(r.status, 0, `${r.stdout}${r.stderr}`);
    const land2 = w.main_();
    await run("git", ["fetch", "-q", "origin", "main"], { cwd: w.clone, env: w.env() }); // (async: the stub is in this process)
    const read = await run("node", [attestJs, "--statuses", "--json", "origin/main"], { cwd: w.clone, env: w.env() });
    assert.equal(read.status, 0, read.stderr);
    const sum = JSON.parse(read.stdout);
    assert.deepEqual(sum.statuses, { read: true, both: 1, trailerOnly: 1, statusOnly: 0, neither: 1 }, "the seed has neither; land 1 both; land 2 its trailers only");
    assert.deepEqual(sum.byCommit.map((c) => [c.sha, c.record]), [[git(w.clone, "rev-list", "--max-parents=0", "origin/main"), "neither"], [land1, "both"], [land2, "trailer"]]);
    assert.ok(w.stub.calls.api.some((c) => c.path.endsWith(`/commits/${land1}/status`)));
    assert.deepEqual(leaks(`${read.stdout}${read.stderr}`), []);
    const text = await run("node", [attestJs, "--statuses", "origin/main"], { cwd: w.clone, env: w.env() });
    assert.match(text.stdout, /statuses: 1 both, 1 trailer only, 0 status only, 1 neither/);
    // no token (no app, no GITHUB_TOKEN): said, not failed
    const bare = await run("node", [attestJs, "--statuses", "origin/main"], { cwd: w.clone, env: { ...w.env(), [APP_ID_ENV]: "", [APP_KEY_ENV]: "", GITHUB_TOKEN: "", GH_TOKEN: "" } });
    assert.equal(bare.status, 0, bare.stderr);
    assert.match(bare.stdout + bare.stderr, /statuses not read: no token/);
    assert.match(bare.stdout, /^attest: 2\/3 commits/m, "the trailer audit is still printed");
    // the app has no statuses permission: said
  } finally { await w.done(); }
  const n = await world({ stubOptions: { statuses: "none" } });
  try {
    const out = await run("node", [attestJs, "--statuses", "HEAD"], { cwd: n.clone, env: n.env() });
    assert.equal(out.status, 0, out.stderr);
    assert.match(out.stdout + out.stderr, /statuses not read: .*statuses permission/);
  } finally { await n.done(); }
});
