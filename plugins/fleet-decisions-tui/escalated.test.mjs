// node --test plugins/fleet-decisions-tui/escalated.test.mjs : the panel takes the tier from the escalation record at read time (ask #2491).
import test from "node:test"
import assert from "node:assert/strict"
import { mkdtempSync, writeFileSync, mkdirSync, readFileSync, statSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { loadSnapshot, readEscalated, escalationKeyOf, ESCALATED_READ_MAX } from "./decisions.mjs"

const HEAD = "a".repeat(40), OTHER = "b".repeat(40)
const row = (number, head, tier = "cos", more = {}) => ({ id: `pr:widget#${number}`, kind: "captain", tier, repo: "acme/widget", number, head, title: `T${number}`, since: new Date(0).toISOString(), ...more })

function world(rows) {
  const dir = mkdtempSync(join(tmpdir(), "decisions-"))
  writeFileSync(join(dir, "decisions.json"), JSON.stringify({ decisions: rows, errors: [] }))
  return dir
}
const record = (dir, ...keys) => writeFileSync(join(dir, "escalated-requests.json"), JSON.stringify({ requests: Object.fromEntries(keys.map((k) => [k, "2026-01-01T00:00:00+00:00"])) }))
const tiers = (snap) => Object.fromEntries(snap.decisions.map((d) => [d.number, d.tier]))
const now = () => Date.now()

test("control: without a record the daemon's tier stands", () => {
  const dir = world([row(7, HEAD)])
  assert.deepEqual(tiers(loadSnapshot(dir, now())), { 7: "cos" })
  rmSync(dir, { recursive: true })
})

test("an escalation recorded after the daemon's write shows as human at once, by the exact head", () => {
  const dir = world([row(7, HEAD), row(8, HEAD)])
  record(dir, `captain:acme/widget#7@${HEAD}`, `captain:acme/widget#8@${OTHER}`, `captain:acme/widget#9@${HEAD}`)
  assert.deepEqual(tiers(loadSnapshot(dir, now())), { 7: "human", 8: "cos" })
  rmSync(dir, { recursive: true })
})

test("a request row is moved by session and request; a human row is never lowered; a row without a head never matches", () => {
  const dir = world([{ id: "d1", kind: "permission", tier: "cos", session: "ses1", request: "req9", title: "x", number: 1, since: new Date(0).toISOString() }, row(2, null), row(3, HEAD, "human")])
  record(dir, "ses1:req9", "captain:acme/widget#2@null", `captain:acme/widget#3@${HEAD}`)
  assert.deepEqual(tiers(loadSnapshot(dir, now())), { 1: "human", 2: "cos", 3: "human" })
  rmSync(dir, { recursive: true })
})

test("an unreadable, malformed, oversized or non-regular record leaves the daemon's tier, and nothing is written", () => {
  const dir = world([row(7, HEAD)])
  const file = join(dir, "escalated-requests.json")
  const key = `captain:acme/widget#7@${HEAD}`
  for (const text of ["{not json", "[]", '{"requests": []}', `{"requests": {"${key}": "t"}, "pad": "${"x".repeat(ESCALATED_READ_MAX)}"}`, "[".repeat(200000)]) {
    writeFileSync(file, text)
    const before = [readFileSync(file, "utf8"), statSync(file).mtimeMs]
    assert.deepEqual(tiers(loadSnapshot(dir, now())), { 7: "cos" }, text.slice(0, 20))
    assert.deepEqual([readFileSync(file, "utf8"), statSync(file).mtimeMs], before)
  }
  rmSync(file)
  mkdirSync(file)   // a directory is not a regular file
  assert.equal(readEscalated(dir), null)
  assert.deepEqual(tiers(loadSnapshot(dir, now())), { 7: "cos" })
  rmSync(dir, { recursive: true })
})

test("the key is the daemon's key (decision_core.captain_escalation_key)", () => {
  assert.equal(escalationKeyOf(row(7, HEAD)), `captain:acme/widget#7@${HEAD}`)
  assert.equal(escalationKeyOf(row(7, null)), null)
  assert.equal(escalationKeyOf({ kind: "permission", session: "s", request: "r" }), "s:r")
})
