// doctor.test.mjs: landingPolicies reads the rules that apply to the default branch and says, line by line, whether landing can work and history is safe.
// Oracle: a fake rules API with synthetic rulesets (names acme-*, ids 11, 12, 13; actor Integration:7 is the landing identity). It would fail if a missing rule passed, a disabled ruleset counted,
// an unreadable answer passed, a line lost its ruleset name, id or [org]/[repo] tag, or a ✗ did not make the exit status 1.
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import { test } from "node:test";
import { doctor, landingPolicies, slugOf } from "./doctor.mjs";

const ME = "Integration:7";
const bypassMe = { actor_type: "Integration", actor_id: 7, bypass_mode: "always" };
const TRAILERS = { type: "commit_message_pattern", ruleset_id: 11, ruleset_source_type: "Repository", parameters: { operator: "regex", pattern: "^Land-Checks: .*\\nLand-Council: ", negate: false } };
const NOFF = { type: "non_fast_forward", ruleset_id: 12, ruleset_source_type: "Organization", ruleset_source: "acme" };
const REVIEW = { type: "pull_request", ruleset_id: 11, ruleset_source_type: "Repository", parameters: { required_approving_review_count: 1 } };
const DEL = { type: "deletion", ruleset_id: 12, ruleset_source_type: "Organization", ruleset_source: "acme" };
const GOOD = [TRAILERS, NOFF, DEL, REVIEW];
const DETAILS = { 11: { name: "acme-land", enforcement: "active", bypass_actors: [bypassMe] }, 12: { name: "acme-history", enforcement: "active", bypass_actors: [] } };
const LIST = [{ id: 11, name: "acme-land", enforcement: "active", source_type: "Repository" }, { id: 12, name: "acme-history", enforcement: "active", source_type: "Organization" }];

/** A fake API: `rules`, `details` by id, `list`; a value that is an Error (or missing) is an unreadable answer. */
const fake = ({ rules = GOOD, details = DETAILS, list = LIST } = {}) => (path) => {
  const give = (v) => { if (v === undefined || v instanceof Error) throw v ?? new Error("HTTP 404"); return v; };
  if (path === "repos/o/r/rules/branches/main") return give(rules);
  if (path === "repos/o/r/rulesets?includes_parents=true") return give(list);
  const m = /rulesets\/(\d+)$/.exec(path);
  return give(details[m?.[1]]);
};
const run = (o = {}, actor = ME) => landingPolicies({ api: fake(o), slug: "o/r", actor });
const line = (f, re) => f.find((x) => re.test(x.text));
const levelOf = (f, re) => line(f, re)?.level;

test("a good setup passes every line, and each line names the ruleset, its id and [org] or [repo]", () => {
  const f = run();
  assert.deepEqual(f.map((x) => x.level), ["ok", "ok", "ok", "ok", "ok", "ok"], JSON.stringify(f, null, 1));
  assert.match(line(f, /never rewritten/).text, /ruleset "acme-history" #12 \[org\]/);
  assert.match(line(f, /trailers/).text, /ruleset "acme-land" #11 \[repo\]/);
  assert.match(line(f, /review is required/).text, /ruleset "acme-land" #11 \[repo\]/);
});

test("missing: no force-push block, no trailers rule, no review rule, a rule the identity does not bypass, a human bypass: each is named and fails or warns", () => {
  assert.equal(levelOf(run({ rules: [TRAILERS, DEL, REVIEW] }), /rewritten/), "fail");
  assert.equal(levelOf(run({ rules: [TRAILERS, NOFF, REVIEW] }), /rewritten/), "fail", "deletion not blocked");
  assert.equal(levelOf(run({ rules: [NOFF, DEL, REVIEW] }), /Land-Checks/), "fail");
  assert.equal(levelOf(run({ rules: [NOFF, DEL, TRAILERS] }), /review is not required/), "fail");
  // the pull_request rule is not bypassed by the identity: it must review, and it cannot push
  const nobypass = { ...DETAILS, 11: { ...DETAILS[11], bypass_actors: [] } };
  const f = run({ details: nobypass });
  assert.equal(levelOf(f, /must review like everyone/), "fail");
  assert.equal(levelOf(f, /cannot push/), "fail");
  assert.match(line(f, /cannot push/).text, /ruleset "acme-land" #11 \[repo\]/);
  // another blocking rule (required status checks) in a ruleset the identity does not bypass
  const checks = { type: "required_status_checks", ruleset_id: 13, ruleset_source_type: "Repository" };
  const f2 = run({ rules: [...GOOD, checks], details: { ...DETAILS, 13: { name: "acme-ci", bypass_actors: [] } } });
  assert.match(line(f2, /cannot push/).text, /required_status_checks of ruleset "acme-ci" #13 \[repo\]/);
  // a human may bypass: a warning, never a pass
  const human = run({ details: { ...DETAILS, 12: { ...DETAILS[12], bypass_actors: [{ actor_type: "RepositoryRole", actor_id: 5, bypass_mode: "always" }] } } });
  assert.equal(levelOf(human, /besides the landing identity/), "warn");
  assert.match(line(human, /besides/).text, /RepositoryRole:5 \(always\) in ruleset "acme-history" #12 \[org\]/);
});

test("disabled rulesets are ignored (their rules are not counted) and listed", () => {
  const list = [...LIST, { id: 13, name: "acme-old", enforcement: "disabled", source_type: "Organization" }];
  const f = run({ list });
  assert.match(line(f, /ignored, not active/).text, /ruleset "acme-old" #13 \[org\] \(disabled\)/);
  // the rules API never returns a disabled ruleset's rules: without them the line is a failure, not a pass
  assert.equal(levelOf(run({ rules: [REVIEW, NOFF, DEL], list }), /Land-Checks/), "fail");
});

test("unreadable: every answer that cannot be read is a warning 'cannot determine', never a pass", () => {
  const none = run({ rules: new Error("HTTP 403") });
  assert.equal(none.length, 5);
  assert.ok(none.every((x) => x.level === "warn" && /^cannot determine/.test(x.text)), JSON.stringify(none));
  assert.ok(run({ rules: { message: "odd" } }).every((x) => x.level === "warn" || /disabled/.test(x.text)), "an answer that is not a list of rules is unreadable");
  const nodetail = run({ details: {} });
  for (const re of [/review is required/, /can push/, /else can bypass/]) assert.equal(levelOf(nodetail, re), "warn", String(re));
  assert.equal(levelOf(nodetail, /Land-Checks/), "ok", "the rule text itself was readable");
  assert.equal(levelOf(run({ list: new Error("HTTP 403") }), /which rulesets are disabled/), "warn");
  const noActor = run({}, null);
  assert.equal(levelOf(noActor, /review is required/), "warn");
  // history needs no identity and no bypass list: the rules alone decide it
  // disabled: a disabled history ruleset contributes no rules, so history is not protected (a fail, never "cannot determine")
  const offList = [LIST[0], { ...LIST[1], enforcement: "disabled" }];
  const off = run({ rules: [TRAILERS, REVIEW], list: offList }, null);
  assert.equal(levelOf(off, /rewritten/), "fail");
  assert.doesNotMatch(line(off, /rewritten/).text, /not named|cannot determine/);
  assert.doesNotMatch(line(noActor, /rewritten/).text, /not named|cannot determine/);
  assert.equal(levelOf(noActor, /never rewritten/), "ok");
  assert.equal(levelOf(run({ details: {} }, null), /never rewritten/), "ok");
  assert.equal(levelOf(run({ rules: [TRAILERS, REVIEW] }, null), /rewritten/), "fail");
  assert.match(line(noActor, /review is required/).fix, /SPIRE_LAND_ACTOR/);
  const notGithub = landingPolicies({ api: fake(), slug: null, actor: ME });
  assert.ok(notGithub.length && notGithub.every((x) => x.level === "warn"));
});

test("slugOf reads https and ssh GitHub URLs only", () => {
  assert.equal(slugOf("https://github.com/o/r.git"), "o/r");
  assert.equal(slugOf(["git", "@", "github.com:o/r"].join("")), "o/r");
  assert.equal(slugOf("/tmp/remote.git"), null);
});

test("doctor prints plain OK/WARN/FAIL off a terminal and with NO_COLOR, boxed ✓ ⚠ ✗ on one; exit 1 on a ✗", () => {
  const go = (opts) => { const out = []; const code = doctor({ cwd: "/", log: (l) => out.push(l), api: fake({ rules: [] }), slug: "o/r", ...opts }); return { code, text: out.join("\n") }; };
  const plain = go({ tty: false });
  assert.equal(plain.code, 1);
  assert.match(plain.text, /== Landing policies ==/);
  assert.match(plain.text, /FAIL history can be rewritten/);
  assert.match(plain.text, /Found \d+ issues to address \(\d+ failing/);
  assert.doesNotMatch(plain.text, /[✓⚠✗◆┌\x1b]/);
  const noColor = go({ tty: true, env: { ...process.env, NO_COLOR: "1" } });
  assert.doesNotMatch(noColor.text, /[✓⚠✗◆┌\x1b]/);
  const fancy = go({ tty: true, env: { ...process.env, NO_COLOR: "" } });
  assert.match(fancy.text, /┌─+┐/);
  assert.match(fancy.text, /◆ Host/);
  assert.match(fancy.text, /✗/);
  assert.match(fancy.text, /→ /);
});

test("doctor reads the PROJECT of the current directory, not the folder of the tool it runs from", () => {
  const proj = mkdtempSync(join(tmpdir(), "doctor-proj-"));
  execFileSync("git", ["init", "-q", proj]);
  mkdirSync(join(proj, ".spire"));
  writeFileSync(join(proj, ".spire", "land.json"), JSON.stringify({ areas: [{ name: "zeta", paths: ["**"], steps: [{ name: "t", run: "true" }] }] }));
  const out = [];
  doctor({ cwd: proj, log: (l) => out.push(l), api: fake(), slug: "o/r", tty: false, columns: 1000 }); // wide: a long temp path must not wrap the line the test reads
  const text = out.join("\n");
  assert.doesNotMatch(text, /no land\.json|has no .*land\.json/);
  assert.match(text, /\.spire\/land\.json is valid: 1 area \(zeta\)/);
  const bare = mkdtempSync(join(tmpdir(), "doctor-bare-"));
  execFileSync("git", ["init", "-q", bare]);
  const out2 = [];
  doctor({ cwd: bare, log: (l) => out2.push(l), api: fake(), slug: "o/r", tty: false, columns: 1000 });
  assert.ok(out2.join("\n").includes(`the project has no ${join(bare, ".spire", "land.json")}`), out2.join("\n"));
});

const project = () => {
  const proj = mkdtempSync(join(tmpdir(), "doctor-p-"));
  execFileSync("git", ["init", "-q", proj]);
  return proj;
};
const text = (o) => { const out = []; const code = doctor({ log: (l) => out.push(l), api: fake(), slug: "o/r", tty: false, ...o }); return { code, out, text: out.join("\n") }; };

test("a rule line and a Summary header separate the checks from the summary", () => {
  const r = text({ cwd: project(), columns: 60 });
  const i = r.out.indexOf("== Summary ==");
  assert.ok(i > 1, r.text);
  assert.equal(r.out[i - 1], "-".repeat(60));
  assert.equal(r.out[i + 1], "-".repeat(60));
  assert.ok(r.out.findIndex((l) => l === "== Landing policies ==") < i);
  assert.ok(text({ cwd: project(), tty: true, env: { ...process.env, NO_COLOR: "" } }).text.includes("◆ Summary"));
});

test("the summary lists every FAIL first, then every WARN, numbered in that order", () => {
  const r = text({ cwd: project(), api: fake({ rules: [] }) });
  const sum = r.out.slice(r.out.indexOf("== Summary ==")).filter((l) => /^ {2}\d+\. /.test(l));
  assert.ok(sum.length >= 3);
  const levels = sum.map((l) => /^ {2}\d+\. (FAIL|WARN)/.exec(l)?.[1]);
  assert.ok(levels.includes("FAIL") && levels.includes("WARN"), r.text);
  assert.deepEqual(levels, [...levels].sort((a, b) => (a === "FAIL" ? 0 : 1) - (b === "FAIL" ? 0 : 1)));
  assert.deepEqual(sum.map((l) => Number(/^ {2}(\d+)\./.exec(l)[1])), sum.map((_, i) => i + 1));
});

test("long lines wrap to the width with a hanging indent; a short width wraps more, and no line passes it", () => {
  const humans = { actor_type: "Team", actor_id: 1, bypass_mode: "always" };
  const api = fake({ details: { ...DETAILS, 12: { ...DETAILS[12], bypass_actors: [humans, { ...humans, actor_id: 2 }] } } });
  for (const columns of [60, 100]) {
    const r = text({ cwd: project(), api, columns });
    // a single unbreakable word (this host's git email) stays whole by design: only lines with spaces must fit
    const tooLong = r.out.filter((l) => l.length > columns && /\S\s+\S/.test(l));
    assert.deepEqual(tooLong, []);
    const at = r.out.findIndex((l) => /^ {2}WARN someone besides/.test(l));
    assert.ok(at >= 0, r.text);
    if (columns === 60) assert.match(r.out[at + 1], /^ {7}\S/, "continuation hangs under the text");
  }
  assert.ok(text({ cwd: project(), api, columns: 60 }).out.length > text({ cwd: project(), api, columns: 200 }).out.length);
});

test("the same actor in the same ruleset is listed once", () => {
  const dup = { actor_type: "Team", actor_id: 18731918, bypass_mode: "always" };
  const f = run({ details: { ...DETAILS, 12: { ...DETAILS[12], bypass_actors: [dup, dup] } } });
  const t = line(f, /besides/).text;
  assert.equal(t.split("Team:18731918").length - 1, 1, t);
});

test("run from a clone against another project, the tool's own 'not an installed copy' warning is not printed; in the tool's own checkout it is", () => {
  const other = project();
  assert.doesNotMatch(text({ cwd: other, columns: 1000 }).text, /not an installed copy|belongs to/);
  const self = project();
  assert.match(text({ cwd: self, tool: join(self, "tool"), columns: 1000 }).text, /not an installed copy/);
});
