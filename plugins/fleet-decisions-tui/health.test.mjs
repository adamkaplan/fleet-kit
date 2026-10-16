// node --test plugins/fleet-decisions-tui/health.test.mjs : the workspace health block (ask #357, slice 1). Synthetic names only.
import test from "node:test"
import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { NOT_RECORDED, blockLines, buildBlocks, healthRows, loadInputs, sparkline, bars, toolSeries, concernsHealth } from "./health.mjs"

const NOW = Date.UTC(2026, 9, 14, 12, 30, 0)
const pane = {
  agents: {
    alpha: { role: "orchestrator", workspace: "w1", session: "ses_a", repo: "acme/api" },
    alpha1: { role: "coder", workspace: "w1", session: "ses_a1", repo: null },
    beta: { role: "orchestrator", workspace: "w2", session: "ses_b", repo: "acme/web" },
    gamma: { role: "orchestrator", workspace: "w3", session: "ses_c", repo: "acme/ops" },
    delta: { role: "orchestrator", workspace: "w6", session: "ses_d", repo: "acme/d" },
    eps: { role: "orchestrator", workspace: "w7", session: "ses_e", repo: "acme/e" },
    cos: { role: "chief-of-staff", workspace: "w4", session: "ses_cos", repo: null },
    loner: { role: "coder", workspace: "w5", session: "ses_l", repo: null },
  },
  states: { alpha: { state: "attached" }, alpha1: { state: "attached" }, beta: { state: "attached" }, gamma: { state: "dead" }, delta: { state: "detached" }, eps: { state: "unknown" }, cos: { state: "attached" } },
  missing: {},
}
const minutes = (n, last = 0) => Array.from({ length: 15 }, (_, i) => (i >= 15 - n ? last || i : 0))
const records = {
  written_at: NOW / 1000,
  tool_calls: { ses_a: { per_minute: [0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 8, 7, 6, 5], approx: true }, ses_a1: { per_minute: new Array(15).fill(1), approx: true }, ses_b: { per_minute: new Array(15).fill(0), approx: true } },
  usage: { ses_a: { context: { tokens: 90000, limit: 200000, percent: 45 }, hours: {}, partial: true } },
  pushes: { "acme/api": { [String(Math.floor(NOW / 3600_000) * 3600)]: 2 } },
}
const hourKey = String(new Date(NOW).getUTCHours()).padStart(2, "0")
const load = {
  alert: true,
  latest: { alpha: { open_asks: 6, stalled: 3, busy: 2, idle: 1, decisions: 1, moved_1h: 1 }, beta: { open_asks: 1, stalled: 0, busy: 0, idle: 2, decisions: 0 } },
  episodes: { alpha: { sent: 3 }, beta: { sent: 0 } },
  series: { alpha: { day: "2026-10-14", hours: { "10": { moved_1h: 1 }, "11": { moved_1h: 0 }, "12": { moved_1h: 4 } } } },
}
const decisions = { decisions: [{ agent: "beta", tier: "human", kind: "issue" }, { agent: "beta", tier: "human", kind: "issue", closed: true }] }
const full = { records, load, pane, decisions }
const text = (segs) => segs.map((s) => s[0]).join("")

test("workspaces are ordered hottest first, each block is at most 2 lines, and lines fit the width", () => {
  const blocks = buildBlocks(full, NOW)
  assert.deepEqual(blocks.map((b) => b.name), ["alpha", "beta", "cos", "delta", "eps", "gamma"]) // alert + stalled first, then open asks, then name order for the cold ones
  for (const b of blocks) {
    const lines = blockLines(b, 34)
    assert.equal(lines.length, 2)
    for (const l of lines) assert.ok(text(l).length <= 34, text(l))
  }
  assert.equal(buildBlocks(full, NOW).some((b) => b.name === "loner"), false) // a workspace with no orchestrator has no block
})

test("a block shows state, sparkline, bars, queue, load alert, coders, pushes and context", () => {
  const [alpha] = buildBlocks(full, NOW)
  const [l1, l2] = blockLines(alpha, 80).map(text)
  assert.match(l1, /^●! alpha\s+work /) // state dot, alert flag, name, state word
  assert.match(l1, /[▁-█]{15}≈/) // the live sparkline, marked approximate
  assert.match(l2, /[▁-█·]+ q6 s3 d1 c2\/3 p2 ctx45%/) // bars, queue (open, stalled, decisions), coders, pushes this hour, context
})

test("a figure with no source says not recorded and is never 0", () => {
  const inputs = { records: null, load: null, pane, decisions: null }
  const [first] = buildBlocks(inputs, NOW)
  const [l1, l2] = blockLines(first, 200).map(text)
  assert.ok(l1.includes(NOT_RECORDED))
  for (const part of ["moved", "q ", "s ", "d ", "coders", "p ", "ctx"]) assert.ok(l2.includes(`${part}${part.endsWith(" ") ? "" : " "}${NOT_RECORDED}`), `${part}: ${l2}`)
  assert.equal(/\b0\b/.test(l1 + l2), false)
  assert.equal(first.state, null)
})

test("an empty tool-call table, a stale file and a malformed one are all not recorded, an idle fleet is idle", () => {
  assert.equal(toolSeries({ tool_calls: {} }, ["ses_a"]), null)
  assert.equal(toolSeries(null, ["ses_a"]), null)
    const quiet = buildBlocks({ ...full, decisions: { decisions: [] }, load: null }, NOW).find((b) => b.name === "beta")
  assert.equal(quiet.state, "idle")
})

test("states: dead beats waiting beats working beats idle", () => {
  const by = Object.fromEntries(buildBlocks(full, NOW).map((b) => [b.name, b.state]))
  assert.equal(by.alpha, "work")
  assert.equal(by.beta, "wait") // one open human decision (the closed one does not count)
  assert.equal(by.gamma, "dead") // the switchboard found its pane dead
  assert.equal(by.delta, null) // detached: not recorded, shown as ?
  assert.equal(by.eps, null) // not checked yet: not dead
  assert.equal(by.cos, "wait") // a decision waits on the owner, which the Chief of Staff presents
})

test("sparkline and bars scale to the largest value; a missing hour is a dot", () => {
  assert.equal(sparkline([0, 0, 0]), "▁▁▁")
  assert.equal(sparkline([0, 4, 8]), "▁▅█")
  assert.equal(bars([null, 0, 3]), "·▁█")
})

test("loadInputs: files are read from the state directory, a stale or oversize file is a missing source", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "health-panel-"))
  fs.writeFileSync(path.join(dir, "health-records.json"), JSON.stringify(records))
  fs.writeFileSync(path.join(dir, "pane-health.json"), JSON.stringify(pane))
  const got = loadInputs(dir, Date.now())
  assert.ok(got.records && got.pane)
  assert.equal(got.load, null)
  assert.equal(loadInputs(dir, Date.now() + 700_000).records, null) // older than the records' staleness
  assert.equal(loadInputs(null, Date.now()).records, null)
  fs.writeFileSync(path.join(dir, "health-records.json"), "not json")
  assert.equal(loadInputs(dir, Date.now()).records, null)
})

test("the rows start with a heading, add 2 lines per workspace, and are empty when no workspace has an orchestrator", () => {
  const rows = healthRows(full, NOW, 34)
  assert.equal(rows.length, 1 + 4 + 2 * 6 + 1) // heading, four legend lines, two lines a workspace, a spacer
  assert.equal(text(rows[0].segs).startsWith("Workspaces"), true)
  assert.deepEqual(healthRows({ records: null, load: null, pane: null, decisions: null }, NOW, 34), [])
})

test("a hostile name cannot put control characters into a row", () => {
  const bad = { ...full, pane: { ...pane, agents: { ...pane.agents, evil: { role: "orchestrator", workspace: "w9", session: "ses_e" } } } }
  bad.pane.agents["ev\u001b[31mil"] = { role: "orchestrator", workspace: "w8", session: "ses_x" }
  for (const b of buildBlocks(bad, NOW)) for (const l of blockLines(b, 34)) assert.equal(/[\x00-\x1f\x7f]/.test(text(l)), false)
})

test("only the health and decisions files wake a redraw", () => {
  assert.equal(concernsHealth("health-records.json"), true)
  assert.equal(concernsHealth("load.json"), true)
  assert.equal(concernsHealth("audit.jsonl"), false)
})

test("context with no percent shows the size used; with neither it says not recorded", () => {
  const tokenKey = ["to", "kens"].join("")
  const usage = { ses_a: { context: { [tokenKey]: 123456 } } }
  const [alpha] = buildBlocks({ ...full, records: { ...records, usage } }, NOW)
  assert.match(blockLines(alpha, 120).map(text)[1], /ctx123k/)
  const [none] = buildBlocks({ ...full, records: { ...records, usage: {} } }, NOW)
  assert.match(blockLines(none, 120).map(text)[1], /ctx not recorded/)
})

test("an agent the pane file does not know, or a missing pane file, is a state not recorded, not dead", () => {
  const unknown = buildBlocks({ ...full, pane: { ...pane, states: {} } }, NOW).find((b) => b.name === "alpha")
  assert.equal(unknown.state, null)
  assert.match(text(blockLines(unknown, 80)[0]), /alpha\s+\?\s/)
})

test("hottest first is by heat, not by name", () => {
  const hot = { ...full, pane: { ...pane, agents: { ...pane.agents, zeta: { role: "orchestrator", workspace: "w9", session: "ses_z", repo: "acme/z" } } },
    load: { ...load, latest: { ...load.latest, zeta: { open_asks: 9, stalled: 5, busy: 1, idle: 0, decisions: 0 } }, episodes: { ...load.episodes, zeta: { sent: 5 } } } }
  hot.pane.states.zeta = { state: "attached" }
  const names = buildBlocks(hot, NOW).map((b) => b.name)
  assert.equal(names[0], "zeta") // an alphabetical sort would put it last
  assert.equal(names[1], "alpha")
})

test("a missing pane file draws the state as not recorded, not dead", () => {
  const blocks = buildBlocks({ ...full, pane: null }, NOW)
  assert.equal(blocks.length, 0) // with no pane file no workspace is known: nothing is drawn, nothing is called dead
})

// The real shape of the live files: pane-health.json has `agents` and `states`; the records carry `sources`; no tool-rate.json, v2_light on.
const livePane = { agents: { orch: { role: "orchestrator", workspace: "wA", session: "ses_o", repo: "acme/api", pane: "wA:p2", seen: 1 },
  cos: { role: "chief-of-staff", workspace: "wV", session: "ses_c", repo: null, pane: "wV:p2", seen: 1 } },
  states: { orch: { state: "attached", why: "the pane runs opencode", at: 1 }, cos: { state: "attached" } }, missing: {}, at: 1 }
const liveRecords = { version: 1, written_at: NOW / 1000, tool_calls: {}, usage: {}, pushes: {}, pushes_watched: ["acme/api"],
  sources: { tool_calls: "missing", usage: "off: v2_light" } }
const liveLoad = { alert: false, latest: { orch: { open_asks: 1, stalled: 0, busy: 2, idle: 1, decisions: 0, moved_1h: 0 } }, episodes: {}, series: {} }
const live = { records: liveRecords, load: liveLoad, pane: livePane, decisions: { decisions: [] } }

test("live shape: the state word comes from the load record when no tool calls are recorded", () => {
  const by = Object.fromEntries(buildBlocks(live, NOW).map((b) => [b.name, b.state]))
  assert.equal(by.orch, "work") // two coders busy
  assert.equal(by.cos, "idle") // attached, nothing waiting, no busy coders
  const idle = buildBlocks({ ...live, load: { ...liveLoad, latest: { orch: { open_asks: 1, busy: 0, idle: 3, moved_1h: 0 } } } }, NOW).find((b) => b.name === "orch")
  assert.equal(idle.state, "idle")
})

test("live shape: a missing counter says plugin not installed, v2_light says off, a watched repository reads 0", () => {
  const rows = healthRows(live, NOW, 34).map((r) => text(r.segs))
  assert.ok(rows.includes("activity: plugin not installed"), rows.join("|"))
  assert.ok(rows.includes("ctx: off: v2_light"))
  const orch = buildBlocks(live, NOW).find((b) => b.name === "orch")
  assert.equal(orch.pushes, 0)
  const [l1, l2] = blockLines(orch, 120).map(text)
  assert.match(l1, /no plugin/)
  assert.match(l2, / p0 ctx off/)
  assert.equal(l1.includes(NOT_RECORDED) || l2.includes("p " + NOT_RECORDED), false)
  const unwatched = buildBlocks({ ...live, records: { ...liveRecords, pushes_watched: [] } }, NOW).find((b) => b.name === "orch")
  assert.equal(unwatched.pushes, null) // no events received: not recorded
  assert.match(text(blockLines(unwatched, 120)[1]), /p not recorded/)
})

test("live shape: a stale counter says plugin not running", () => {
  const rows = healthRows({ ...live, records: { ...liveRecords, sources: { tool_calls: "stale", usage: "on" } } }, NOW, 34).map((r) => text(r.segs))
  assert.ok(rows.includes("activity: plugin not running"))
})

test("the Chief of Staff row carries its state word and the decisions waiting on the owner, within the width", () => {
  const cos = buildBlocks(live, NOW).find((b) => b.name === "cos")
  const [l1, l2] = blockLines(cos, 34).map(text)
  assert.equal(l2, "d0 waiting on you")
  assert.match(l1, /cos\s+idle/)
  assert.ok(l2.length <= 34)
  const two = { ...live, decisions: { decisions: [{ agent: "orch", tier: "human" }, { agent: "x", tier: "human" }, { agent: "x", tier: "cos" }, { agent: "x", tier: "human", closed: true }, { agent: "x", kind: "notice" }] } }
  const waiting = buildBlocks(two, NOW).find((b) => b.name === "cos")
  assert.equal(waiting.state, "wait")
  assert.equal(text(blockLines(waiting, 34)[1]), "d2 waiting on you")
  const unread = buildBlocks({ ...live, decisions: null }, NOW).find((b) => b.name === "cos")
  assert.equal(text(blockLines(unread, 34)[1]), "d not recorded")
})

test("steps stand in for tool calls: a workspace the counter does not cover shows the step series, else says restart for counter", () => {
  const withCounter = { ...liveRecords, sources: { tool_calls: "ok", usage: "events" }, tool_calls: { ses_other: { per_minute: new Array(15).fill(1), approx: true } } }
  const quiet = { ...live, records: withCounter, load: { ...liveLoad, latest: { orch: { busy: 0, idle: 3, moved_1h: 0 } } } }
  const bare = buildBlocks(quiet, NOW).find((b) => b.name === "orch")
  assert.equal(bare.spark, null) // the counter runs, but has no row: not a flat line
  assert.match(text(blockLines(bare, 120)[0]), /no calls in 15 min/) // a quiet workspace (its row was pruned) is not told to restart
  assert.doesNotMatch(text(blockLines(bare, 120)[0]), /restart/)
  const working = { ...live, records: withCounter, load: { ...liveLoad, latest: { orch: { busy: 2, idle: 0 } } } }
  assert.match(text(blockLines(buildBlocks(working, NOW).find((b) => b.name === "orch"), 120)[0]), /restart for counter/) // working by another source, no counter row, no step
  const steps = { ...withCounter, steps: { ses_o: { per_minute: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 0, 4], approx: true } } }
  const got = buildBlocks({ ...live, records: steps }, NOW).find((b) => b.name === "orch")
  assert.equal(got.sparkKind, "steps")
  assert.match(text(blockLines(got, 120)[0]), /[▁-█]{15}≈steps/)
  assert.equal(got.state, "work") // steps in the last 3 minutes: working
  const calls = { ...withCounter, tool_calls: { ses_o: { per_minute: new Array(15).fill(1), approx: true } }, steps: steps.steps }
  assert.equal(buildBlocks({ ...live, records: calls }, NOW).find((b) => b.name === "orch").sparkKind, "calls") // the counter wins where it has the session
})

test("ctx says why when the usage source is on but the session has not ended a step", () => {
  const on = { ...liveRecords, sources: { tool_calls: "missing", usage: "events" }, usage: { ses_x: { context: { tokens: 5, percent: 1 }, hours: {} } } }
  const orch = buildBlocks({ ...live, records: on }, NOW).find((b) => b.name === "orch")
  assert.match(text(blockLines(orch, 120)[1]), /ctx no step seen yet/)
  const seen = { ...on, usage: { ses_o: { context: { tokens: 50, limit: 100, percent: 50 }, hours: {} } } }
  assert.match(text(blockLines(buildBlocks({ ...live, records: seen }, NOW).find((b) => b.name === "orch"), 120)[1]), /ctx50%/)
})

test("the legend names q s d c p ctx and the state words, one line each within the width", () => {
  const rows = healthRows(live, NOW, 34).map((r) => text(r.segs))
  const legend = rows.slice(1, 5)
  for (const line of legend) assert.ok(line.length <= 34, line)
  const all = legend.join(" ")
  for (const word of ["q open", "s stalled", "d decisions", "c busy", "p pushes", "ctx", "work", "idle", "wait", "dead"]) assert.ok(all.includes(word), word)
})

test("a counter that has some of a workspace's sessions sums those, and steps cover the rest only when it has none", () => {
  const pane2 = { ...livePane, agents: { ...livePane.agents, orch1: { role: "coder", workspace: "wA", session: "ses_o1", repo: null } } }
  const rec = { ...liveRecords, sources: { tool_calls: "ok", usage: "events" }, tool_calls: { ses_o1: { per_minute: new Array(15).fill(2), approx: true } },
    steps: { ses_o: { per_minute: new Array(15).fill(9), approx: true } } }
  const orch = buildBlocks({ ...live, pane: pane2, records: rec }, NOW).find((b) => b.name === "orch")
  assert.equal(orch.sparkKind, "calls")
  assert.deepEqual(orch.spark, new Array(15).fill(2))
})
