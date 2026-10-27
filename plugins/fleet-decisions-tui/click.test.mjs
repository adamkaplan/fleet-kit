// node --test plugins/fleet-decisions-tui/click.test.mjs : a click copies the short code (ask #1096).
import test from "node:test"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { shortCodeOf, urlOf, entryOf } from "./decisions.mjs"

const since = new Date(0).toISOString()
const pr = { id: "pr:acme/widget-service#12", kind: "notice", repo: "acme/widget-service", number: 12, title: "t", since, tier: "human" }
const plan = { id: "issue:acme/fleet-kit#935", kind: "issue", repo: "acme/fleet-kit", ask: 935, title: "t", since, tier: "human", plan_kind: "plan" }
const noplan = { ...plan, ask: 936, plan_kind: "noplan" }
const tui = readFileSync(new URL("./tui.tsx", import.meta.url), "utf8")

for (const [name, d, code, url] of [
  ["pull request", pr, "widget-service:#12", "https://github.com/acme/widget-service/pull/12"],
  ["issue with plan", plan, "fleet-kit:#935", "https://github.com/acme/fleet-kit/issues/935"],
  ["issue without plan", noplan, "fleet-kit:#936", "https://github.com/acme/fleet-kit/issues/936"],
]) {
  test(`${name}: link unchanged, code copied, not the URL`, () => {
    assert.equal(urlOf(d), url)
    assert.equal(shortCodeOf(d), code)
    const e = entryOf(d, 1000)
    assert.equal(e.url, url)
    assert.equal(e.code, code)
    assert.ok(!code.includes("acme"))
  })
}

test("the click copies the code, only one function builds it", () => {
  assert.match(tui, /copyToClipboardOSC52\?\.\(code\)/)
  assert.doesNotMatch(tui, /copyToClipboardOSC52\?\.\(url\)/)
  assert.equal(shortCodeOf({ repo: "../x/y", number: 1, kind: "notice" }), null)
  const src = readFileSync(new URL("./decisions.mjs", import.meta.url), "utf8")
  assert.equal((src.match(/:#\$\{/g) || []).length, 1)
})
