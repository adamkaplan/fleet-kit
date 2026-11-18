// Admission: attest.mjs rewrites the Land-Checks of the commits of a push and nothing else: every tree, parent chain, author, committer and date is the one the commit had, the other trailers and the body are kept, and the
// worktree it runs in (the gate leaves installs in it) is not touched. It would fail if the stamp changed a tree, lost the council's record, or needed a clean worktree. Oracle: real git commits in a temp repository.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { attestedBy, partOf, report, summarize, trailerValue, withChecks } from "./attest.mjs";

const attest = join(fileURLToPath(import.meta.url), "../attest.mjs");
const env = { ...process.env, GIT_AUTHOR_NAME: "a", GIT_AUTHOR_EMAIL: "a@a", GIT_COMMITTER_NAME: "c", GIT_COMMITTER_EMAIL: "c@c" };
const git = (cwd, ...a) => execFileSync("git", a, { cwd, env, encoding: "utf8" });

test("restamp: the tip's Land-Checks is the line given and an earlier commit's adds part-of the tip's tree; trees, authors, dates, the council's record and the body are the commits' own; the dirty worktree stays dirty", () => {
  const dir = mkdtempSync(join(tmpdir(), "attest-test-"));
  try {
    git(dir, "init", "-q", "-b", "main");
    writeFileSync(join(dir, "f"), "0\n");
    git(dir, "add", "-A");
    git(dir, "commit", "-qm", "base");
    const base = git(dir, "rev-parse", "HEAD").trim();
    for (const [i, n] of ["one", "two"].entries()) {
      writeFileSync(join(dir, `f${n}`), `${n}\n`);
      git(dir, "add", "-A");
      env.GIT_AUTHOR_DATE = `170000000${i} +0100`;
      env.GIT_COMMITTER_DATE = `170000010${i} +0200`;
      git(dir, "commit", "-qm", `docs: ${n}`, "-m", "a body line", "-m", `Land-Checks: areas=old tree=000000000000\nLand-Council: ran: warden=approve`);
    }
    delete env.GIT_AUTHOR_DATE;
    delete env.GIT_COMMITTER_DATE;
    writeFileSync(join(dir, "left-by-the-gate"), "an install\n"); // untracked, as a gate leaves them
    writeFileSync(join(dir, "f"), "changed by the gate\n"); // a tracked file modified, as a gate leaves them
    const before = git(dir, "log", "--reverse", "--format=%T %an %ae %ad %cn %ce %cd %s", "--date=raw", `${base}..HEAD`);
    const tip = execFileSync("node", [attest, "restamp", base, "areas=new tree=abcdefabcdef"], { cwd: dir, env, encoding: "utf8" }).trim();
    assert.equal(git(dir, "log", "--reverse", "--format=%T %an %ae %ad %cn %ce %cd %s", "--date=raw", `${base}..${tip}`), before, "the trees, authors, dates and subjects are the commits' own");
    const tipTree = git(dir, "rev-parse", `${tip}^{tree}`).trim().slice(0, 12);
    const bodies = git(dir, "log", "--format=%B%x1e", `${base}..${tip}`).split("\x1e").map((x) => x.trim()).filter(Boolean); // newest first
    assert.match(bodies[0], /^Land-Checks: areas=new tree=abcdefabcdef$/m, "the tip carries the line as it is");
    assert.match(bodies[1], new RegExp(`^Land-Checks: areas=new tree=abcdefabcdef part-of=${tipTree}$`, "m"), "an earlier commit of the push says whose tree it was gated with");
    for (const b of bodies) {
      assert.doesNotMatch(b, /areas=old/);
      assert.match(b, /^Land-Council: ran: warden=approve$/m);
      assert.match(b, /^a body line$/m);
    }
    assert.equal(git(dir, "rev-list", "--count", `${base}..${tip}`).trim(), "2");
    assert.equal(git(dir, "rev-parse", "HEAD").trim() === tip, false, "HEAD is not moved: the caller does that");
    assert.match(git(dir, "status", "--porcelain"), /left-by-the-gate/);
    assert.match(git(dir, "status", "--porcelain"), /^ M f$/m);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// Admission: what counts as attested. A commit is attested when its Land-Checks names its own tree, or says it is part of a push whose tip is attested (the tip's tree is the one named); a record of another tree, none, and a push whose tip
// is not in the range are told apart. The limit is stated, not hidden. It would fail if an earlier commit of a push never counted, if any `part-of` counted without the tip, or if a stale tree counted. Oracle: commits as data.
test("attestedBy: a commit counts by its own tree or as part of an attested push; a stale record, no record and an open push do not", () => {
  const T = (c) => c.repeat(40);
  const c = (sha, tree, line) => ({ sha, tree: T(tree), body: `subject\n\n${line ? `Land-Checks: ${line}\nLand-Council: x\n` : ""}` });
  const ok = (tree) => `areas=a total=1s tree=${T(tree).slice(0, 12)}`;
  const rows = attestedBy([
    c("none", "1", null),
    c("stale", "2", ok("9")),
    c("own", "3", ok("3")),
    c("p1", "4", partOf(ok("6"), T("6"))), // the first of a push of three
    c("p2", "5", partOf(ok("6"), T("6"))),
    c("tip", "6", ok("6")),
    c("liar", "7", partOf(ok("8"), T("8"))), // part of a push whose tip has another tree
    c("tip2", "9", ok("9")),
    c("open", "a", partOf(ok("b"), T("b"))), // the range ends before its tip
  ]);
  assert.deepEqual(
    rows.map((r) => r.status),
    ["none", "stale", "tree", "push", "push", "tree", "stale", "tree", "open"],
  );
  assert.match(rows[1].reason, /names tree 999999999999, the commit's tree is 222222222222/);
  assert.equal(partOf(partOf("x tree=1", "ab".repeat(20)), "cd".repeat(20)), "x tree=1 part-of=cdcdcdcdcdcd", "one part-of, the last");
});

// Admission (F4): the audit says the number. From commits as data: the share, its split by own tree and by push, the push tips, the overrides (with and without a record) and the commits that are not attested, in words a person can act on.
// Then from a real repository through the CLI (`spire attest`'s mechanism): the tree of each commit is read from git, a record of a tree that never landed is "stale", a hand Override without a record is not attested. It would fail
// if a stale record counted, if the push group were ignored, or if the CLI read the message instead of the tree. Oracle: real git commits.
test("summarize and report: the share, the split, the tips, the overrides and the commits that are not attested", () => {
  const T = (c) => c.repeat(40);
  const c = (sha, tree, line, extra = "") => ({ sha: sha.padEnd(12, "0"), subject: `s ${sha}`, tree: T(tree), body: `s\n\n${line ? `Land-Checks: ${line}\nLand-Council: x\n` : ""}${extra}` });
  const ok = (tree) => `areas=a total=1s tree=${T(tree).slice(0, 12)}`;
  const sum = summarize([c("hand", "1", null, "Override: owner fix\n"), c("old", "2", ok("9")), c("a", "3", partOf(ok("4"), T("4"))), c("b", "4", ok("4")), c("o", "5", ok("5"), "Override: the owner: law\n")]);
  assert.deepEqual([sum.total, sum.attested, sum.byTree, sum.byPush, sum.tips, sum.tipsAttested], [5, 3, 2, 1, 4, 2]);
  assert.deepEqual(sum.overrides.map((o) => [o.sha, o.attested]), [["hand00000000", false], ["o00000000000", true]]);
  assert.deepEqual(sum.notAttested.map((r) => r.status), ["none", "stale"]);
  const text = report(sum, { range: "R" });
  assert.match(text, /^attest: 3\/5 commits of R carry a record of the tree that landed \(60\.0%\)$/m);
  assert.match(text, /2 by their own tree, 1 by their push's tip \(part-of\), 2 not attested; push tips 2\/4 \(50\.0%/);
  assert.match(text, /overrides: 2 \(hand00000000 \[no record\]: owner fix; o00000000000: the owner: law\)/);
  assert.match(text, /not attested hand00000000 none: no Land-Checks record \(Override: owner fix\) \| s hand/);
  assert.match(text, /not attested old000000000 stale: the record names tree 999999999999, the commit's tree is 222222222222/);
  assert.match(text, /limit: a commit inside a push is attested by the push's tip/);
  assert.doesNotMatch(report(sum, { brief: true }), /limit:/);
});

test("attest CLI: reads the tree from git; a record of another tree, none, and a hand Override are not attested; the push group counts", () => {
  const dir = mkdtempSync(join(tmpdir(), "attest-cli-"));
  const cli = (...a) => execFileSync("node", [attest, ...a], { cwd: dir, env, encoding: "utf8" });
  try {
    git(dir, "init", "-q", "-b", "main");
    const commit = (name, msg) => {
      writeFileSync(join(dir, name), `${name}\n`);
      git(dir, "add", "-A");
      git(dir, "commit", "-qm", msg);
      return git(dir, "rev-parse", "--short=12", "HEAD^{tree}").trim();
    };
    commit("a", "seed: no record");
    commit("b", "hand: override\n\nOverride: owner fix");
    git(dir, "commit", "-q", "--amend", "-m", "hand: override", "-m", "Override: owner fix");
    commit("c", "stale");
    git(dir, "commit", "-q", "--amend", "-m", "stale", "-m", "Land-Checks: areas=x total=1s tree=aaaaaaaaaaaa\nLand-Council: x");
    const base = git(dir, "rev-parse", "HEAD").trim();
    const pushTree = (() => {
      writeFileSync(join(dir, "d"), "d\n");
      git(dir, "add", "-A");
      git(dir, "commit", "-qm", "push, earlier");
      writeFileSync(join(dir, "e"), "e\n");
      git(dir, "add", "-A");
      git(dir, "commit", "-qm", "push, tip");
      return git(dir, "rev-parse", "--short=12", "HEAD^{tree}").trim();
    })();
    const line = `areas=x total=1s tree=${pushTree}`;
    const tip = execFileSync("node", [attest, "restamp", base, line], { cwd: dir, env, encoding: "utf8" }).trim();
    git(dir, "reset", "-q", "--soft", tip); // what push does: HEAD to the stamped commits
    const j = JSON.parse(cli("--json"));
    assert.deepEqual([j.total, j.attested, j.byTree, j.byPush], [5, 2, 1, 1]);
    assert.deepEqual(j.notAttested.map((r) => r.status), ["none", "none", "stale"]);
    assert.equal(j.overrides.length, 1);
    assert.match(cli("--brief"), /^attest: 2\/5 commits of HEAD \(last 300\) carry a record of the tree that landed \(40\.0%\)/);
    assert.match(cli(`${base}..HEAD`), /^attest: 2\/2 commits of .*\(100\.0%\)/);
    assert.throws(() => cli("--nope"), /usage/);
    assert.throws(() => cli("nonexistent..range"), /cannot read/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("the record is read, and rewritten, wherever it is in the message: a body that goes on after the trailers", () => {
  const body = "subject\n\nLand-Checks: areas=old tree=111111111111\nLand-Council: x\n\nAvoided: a later paragraph\n";
  assert.equal(trailerValue(body, "Land-Checks"), "areas=old tree=111111111111");
  const out = withChecks(body, "areas=new tree=222222222222");
  assert.equal(out, "subject\n\nLand-Checks: areas=new tree=222222222222\nLand-Council: x\n\nAvoided: a later paragraph\n");
  assert.equal(withChecks(`${body}Land-Checks: areas=dup tree=1\n`, "n"), "subject\n\nLand-Council: x\n\nAvoided: a later paragraph\nLand-Checks: n\n");
  assert.match(withChecks("subject\n", "n"), /^Land-Checks: n$/m);
});
