// The Decisions panel's Caretakers and Active Cloud Workers sections.
import test from "node:test"
import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { blockLines, caretakerBlocks, workerBlocks, workerLines, healthRows, loadInputs, concernsHealth, FLOOR_LIVE_FILE } from "./health.mjs"
import "./alarm.test.mjs" // the owner-alarm panel tests run with this file (CI names health.test.mjs)

const NOW = Date.UTC(2026, 9, 14, 12, 30, 0)
const startedAt = NOW / 1000 - 240
const floor = {
  sessions: [
    { name: "alpha", repo: "acme/api", issue: 12, mode: "orchestrator", agent: "caretaker", state: "running", busy: false, rounds: 3 },
    { name: "api-42-auth", repo: "acme/api", issue: 42, mode: "author", agent: "coder", state: "running", busy: true, rounds: 1, started_at: startedAt, summary: "Fix the auth flow" },
    { name: "api-43-done", repo: "acme/api", issue: 43, mode: "author", agent: "coder", state: "done", rounds: 2 },
    { name: "api-44-bad", repo: "acme/api", issue: 44, mode: "author", agent: "coder", state: "failed", rounds: 2 },
  ],
}
const pane = {
  agents: {
    "care-beta": { role: "caretaker", repo: "acme/web" },
    "care-dead": { role: "caretaker", repo: "acme/ops" },
    cloudy: { kind: "cloud", charter: "ops" },
    plain: { role: "coder" },
  },
  states: { "care-beta": { state: "attached" }, "care-dead": { state: "dead" } },
}
const decisions = { decisions: [{ agent: "alpha", tier: "human", kind: "issue" }, { agent: "alpha", tier: "human", closed: true }] }
const text = (segs) => segs.map((s) => s[0]).join("")
const all = (rows) => rows.map((r) => text(r.segs))

test("Caretakers: name, repo, mode, state, waiting decisions; dead agents are excluded", () => {
  const rows = all(healthRows({ floor, pane, decisions }, NOW, 60))
  assert.ok(rows.some((r) => r.startsWith("Caretakers (")))
  assert.ok(rows.includes("● alpha orchestrator sleep r3 · acme/api"))
  assert.ok(rows.includes("● care-beta caretaker state unknown r1 · acme/web"))
  assert.ok(rows.includes("  d1 waiting"))
  assert.ok(rows.some((r) => r.includes("cloudy")))
  assert.ok(!rows.some((r) => r.includes("care-dead") || r.includes("plain")))
  assert.ok(!rows.some((r) => r.includes("Workspaces")))
})

test("each Caretaker is one or two lines and lines fit the width", () => {
  for (const b of caretakerBlocks({ floor, pane, decisions }, NOW)) {
    const lines = blockLines(b, 24)
    assert.ok(lines.length >= 1 && lines.length <= 2)
    for (const l of lines) assert.ok(text(l).length <= 24)
  }
})

test("Active Cloud Workers: one or two lines, finished workers excluded", () => {
  const rows = all(healthRows({ floor }, NOW, 60))
  const at = rows.indexOf("Active Cloud Workers (1)")
  assert.ok(at >= 0)
  assert.equal(rows[at + 1], "● api-42-auth acme/api#42 running r1 4m")
  assert.equal(rows[at + 2], "  Fix the auth flow")
  assert.ok(!rows.some((r) => r.includes("api-43") || r.includes("api-44")))
  assert.ok(!rows.includes("Active Cloud Workers (0)"))
})

test("a worker without a summary is one line; a long summary is cut to the width", () => {
  const [w] = workerBlocks({ floor: { sessions: [{ name: "w-1-x", repo: "acme/api", issue: 1, state: "running", summary: "x".repeat(200) }] } }, NOW)
  const lines = workerLines(w, 30)
  assert.equal(lines.length, 2)
  assert.ok(text(lines[1]).length <= 30)
  assert.equal(workerLines({ ...w, summary: "" }, 30).length, 1)
})

test("a worker whose summary is an ISSUE header line shows no summary", () => {
  const [w] = workerBlocks({ floor: { sessions: [{ name: "w-1-x", repo: "acme/api", issue: 1, state: "running", summary: "ISSUE: https://example.com/acme/api/issues/1" }] } }, NOW)
  assert.equal(w.summary, "")
  assert.equal(workerLines(w, 60).length, 1)
})

test("nothing to show draws nothing, and no legend or workspace text is drawn", () => {
  assert.deepEqual(healthRows({}, NOW, 40), [])
  const rows = all(healthRows({ floor, pane, decisions }, NOW, 60)).join("\n")
  assert.ok(!/Workspaces|stalled|pushes\/hour/.test(rows))
})

test("hostile names and summaries carry no control characters", () => {
  const evil = { sessions: [
    { name: "w-1-\x1b[31mred", repo: "acme/api\x07", issue: 1, state: "running", summary: "a\x1b]0;x\x07b\nc" },
    { name: "care\x1b[2J", mode: "orchestrator", repo: "acme/x", state: "running" },
  ] }
  for (const r of all(healthRows({ floor: evil }, NOW, 80))) assert.ok(!/[\x00-\x1f\x7f]/.test(r))
})

test("loadInputs reads floor-live, pane, decisions; stale, corrupt and oversize files are missing", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "health-"))
  fs.writeFileSync(path.join(dir, "floor-live.json"), JSON.stringify(floor))
  fs.writeFileSync(path.join(dir, "pane-health.json"), JSON.stringify(pane))
  fs.writeFileSync(path.join(dir, "decisions.json"), JSON.stringify(decisions))
  const got = loadInputs(dir, Date.now())
  assert.equal(got.floor.sessions.length, 4)
  assert.ok(got.pane.agents && got.decisions.decisions)
  const old = new Date(Date.now() - 3600_000)
  fs.utimesSync(path.join(dir, "floor-live.json"), old, old)
  fs.utimesSync(path.join(dir, "decisions.json"), old, old)
  fs.writeFileSync(path.join(dir, "pane-health.json"), "{not json")
  const stale = loadInputs(dir, Date.now())
  assert.equal(stale.floor, null)
  assert.equal(stale.decisions, null)
  assert.equal(stale.pane, null)
  assert.deepEqual(healthRows(stale, Date.now(), 40), [])
  assert.deepEqual(loadInputs(undefined, NOW).floor, null)
})

test("floor-live.json wakes a redraw, other files do not", () => {
  assert.ok(concernsHealth(FLOOR_LIVE_FILE))
  assert.ok(concernsHealth("decisions.json"))
  assert.ok(!concernsHealth("other.json"))
})

test("a narrow Caretaker line keeps the state word", () => {
  const [b] = caretakerBlocks({ floor }, NOW)
  assert.match(text(blockLines({ ...b, repo: "acme/a-very-long-repository-name" }, 34)[0]), /sleep/)
})
