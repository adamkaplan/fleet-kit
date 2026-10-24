// node --test plugins/fleet-decisions-tui/kind.test.mjs : the kind marker of each row (ask #1094). Synthetic names only.
import test from "node:test"
import assert from "node:assert/strict"
import { KIND_MARKS, kindOf, markOf, buildView, styledRows, styledText, entryOf, urlOf, ROW_WIDTH } from "./decisions.mjs"

const NOW = Date.UTC(2026, 9, 14, 12, 0, 0)
const since = new Date(NOW - 3600_000).toISOString()
const pr = { id: "pr:acme/api#5", kind: "notice", repo: "acme/api", number: 5, title: "pr five", since, tier: "human" }
const cap = { id: "pr:acme/api#6", kind: "captain", repo: "acme/api", number: 6, title: "pr six", since, tier: "human" }
const withPlan = { id: "issue:acme/api#7", kind: "issue", repo: "acme/api", ask: 7, title: "issue seven", since, tier: "human", plan_kind: "plan" }
const noPlan = { id: "issue:acme/api#8", kind: "issue", repo: "acme/api", ask: 8, title: "issue eight", since, tier: "human", plan_kind: "noplan" }
const unknown = { id: "issue:acme/api#9", kind: "issue", repo: "acme/api", ask: 9, title: "issue nine", since, tier: "human" }
const snap = (decisions) => ({ status: "ok", decisions, errors: [], ageMs: 0 })
const all = [pr, cap, withPlan, noPlan, unknown]
const expected = { [pr.id]: "pr", [cap.id]: "pr", [withPlan.id]: "plan", [noPlan.id]: "noplan", [unknown.id]: "unknown" }

test("S2 kinds: pull request, plan, no plan, unknown", () => {
  assert.deepEqual(all.map(kindOf), ["pr", "pr", "plan", "noplan", "unknown"])
  assert.equal(kindOf({ ...noPlan, plan_kind: "weird" }), "unknown")
  assert.equal(kindOf(null), "unknown")
  assert.notEqual(markOf(unknown), KIND_MARKS.noplan)
  assert.deepEqual(KIND_MARKS, { pr: "🔀", plan: "📋", noplan: "📝", unknown: "❔" })
})

test("S1 every rendered row starts with its kind's symbol (entries, plain and styled rows)", () => {
  for (const d of all) {
    assert.ok(entryOf(d, NOW).head.trimStart().startsWith(KIND_MARKS[expected[d.id]]), d.id)
  }
  for (const v3 of [false, true]) {
    const lines = styledText(styledRows(snap(all), NOW, ROW_WIDTH, undefined, { v3 }))
    for (const d of all) {
      const word = d.title.split(" ")[1]  // "five", "six", "seven", "eight", "nine"
      const first = lines.find((l) => l.includes(word))
      assert.ok(first && first.startsWith(KIND_MARKS[expected[d.id]] + " "), `${v3} ${d.id}: ${first}`)
    }
  }
})

test("S6 apart from the marker the rows and their order are unchanged; links unchanged", () => {
  const strip = (l) => l.replace(/^(🔀|📋|📝|❔) /, "")
  const marked = styledText(styledRows(snap(all), NOW, ROW_WIDTH, undefined, { v3: false })).map(strip)
  const plain = styledText(styledRows(snap(all.map((d) => ({ ...d, plan_kind: undefined }))), NOW, ROW_WIDTH, undefined, { v3: false })).map(strip)
  assert.equal(marked.length, plain.length)
  assert.deepEqual(marked.filter((l) => l.includes("pr ") || l.includes("issue ")), plain.filter((l) => l.includes("pr ") || l.includes("issue ")))
  assert.equal(urlOf(withPlan), "https://github.com/acme/api/issues/7")
  assert.equal(urlOf(pr), "https://github.com/acme/api/pull/5")
})
