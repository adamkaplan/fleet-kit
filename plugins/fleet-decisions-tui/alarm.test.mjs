// The Chief of Staff's blocked alarm row (ask #2059, S8): a blocked record, an ended one within the hour, an old one, a missing and a malformed file.
import test from "node:test"
import assert from "node:assert/strict"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { alarmRows, healthRows, loadInputs, concernsHealth } from "./health.mjs"

const NOW = 1_800_000_000_000
const rec = (extra) => ({ alarm: { agent: "cos", since: NOW / 1000 - 25 * 60, text: "cos blocked", resume: "x resume cos", at: NOW / 1000, ended: null, ...extra } })
const rows = (file) => alarmRows(file, NOW, 34).filter((r) => r.segs[0][0] !== "")
const text = (r) => r.segs.map((s) => s[0]).join("")

test("a blocked record draws one row naming the Chief of Staff and the minutes", () => {
  const got = rows(rec({}))
  assert.equal(got.length, 1)
  assert.match(text(got[0]), /cos blocked 25 minutes/)
})
test("an ended record 30 minutes old still shows, one 90 minutes old shows nothing", () => {
  assert.equal(rows(rec({ ended: NOW / 1000 - 30 * 60 })).length, 1)
  assert.match(text(rows(rec({ ended: NOW / 1000 - 30 * 60 }))[0]), /ended/)
  assert.equal(rows(rec({ ended: NOW / 1000 - 90 * 60 })).length, 0)
})
test("a missing or malformed file shows nothing", () => {
  assert.equal(rows(null).length, 0)
  assert.equal(rows({}).length, 0)
  assert.equal(rows({ alarm: "x" }).length, 0)
  assert.equal(rows(rec({ agent: "" })).length, 0)
  assert.equal(rows(rec({ since: "soon" })).length, 0)
})
test("loadInputs reads the file and healthRows draws the row; a missing or malformed file draws none", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "alarm-panel-"))
  assert.equal(healthRows(loadInputs(dir, NOW), NOW, 34).length, 0)
  fs.writeFileSync(path.join(dir, "owner-alarm.json"), "not json")
  assert.equal(healthRows(loadInputs(dir, NOW), NOW, 34).length, 0)
  fs.writeFileSync(path.join(dir, "owner-alarm.json"), JSON.stringify(rec({})))
  const got = healthRows(loadInputs(dir, NOW), NOW, 34)
  assert.equal(got.length, 2)
  assert.match(text(got[0]), /25 minutes/)
  assert.equal(concernsHealth("owner-alarm.json"), true)
})
