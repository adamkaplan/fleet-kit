// The health block of the right sidebar: the Caretakers and the Active Cloud Workers, one or two lines each, drawn above the Decisions rows. Pure: it reads files the switchboard daemon and its recorders already keep in the state directory
// (load.json from the load alert (#348), pane-health.json, decisions.json, floor-live.json) and turns them
// into rows. No process is started, nothing leaves this machine, and nothing is polled that was not already there.
//
// A file that is missing, unreadable, malformed or too old is a missing source: its section draws nothing. A Caretaker whose activity
// no source records says "state unknown", never a guess.
import { readFileSync, statSync } from "node:fs"
import { join } from "node:path"
import { wrapText } from "./decisions.mjs"

export const LOAD_FILE = "load.json"
export const PANE_FILE = "pane-health.json"
export const DECISIONS_FILE = "decisions.json"
export const FLOOR_LIVE_FILE = "floor-live.json"   // the live Floor sessions the cluster mirror keeps (bin/cloud_floor.py)
export const WATCHED = [LOAD_FILE, PANE_FILE, DECISIONS_FILE, FLOOR_LIVE_FILE]
export const ALARM_FILE = "owner-alarm.json"   // the Chief of Staff's blocked alarm (written by the daemon's blocked report)
export const ALARM_KEEP_MS = 3600_000          // an ended alarm's row is kept an hour
export const ALARM_STALE_MS = 24 * 3600_000  // a blocked record not updated for 24 hours is a block whose end was never written (the daemon stopped, the agent was removed)
WATCHED.push(ALARM_FILE)
export const FLOOR_LIVE_STALE_MS = 600_000   // the Floor sync rewrites floor-live.json on every pass
export const LOAD_STALE_MS = 3 * 3600_000 // load.json is rewritten only when its content changes: an hour or two quiet is normal
export const PANE_STALE_MS = 600_000
export const DECISIONS_STALE_MS = 180_000
export const MAX_BYTES = 1 << 20

export const concernsHealth = (filename) => typeof filename === "string" && WATCHED.includes(filename)

// A file as an object with its age, or null (missing, over MAX_BYTES, not JSON, not an object).
export function readState(dir, name, now) {
  if (!dir) return null
  try {
    const path = join(dir, name)
    const info = statSync(path)
    if (!info.isFile() || info.size > MAX_BYTES) return null
    const body = JSON.parse(readFileSync(path, "utf8"))
    return body && typeof body === "object" && !Array.isArray(body) ? { body, ageMs: Math.max(0, now - info.mtimeMs) } : null
  } catch {
    return null
  }
}

export function loadInputs(dir, now) {
  const read = (name, stale) => {
    const got = readState(dir, name, now)
    return got && got.ageMs <= stale ? got.body : null // a stale file is a missing source
  }
  return {
    load: read(LOAD_FILE, LOAD_STALE_MS),
    pane: read(PANE_FILE, PANE_STALE_MS),
    decisions: read(DECISIONS_FILE, DECISIONS_STALE_MS),
    floor: read(FLOOR_LIVE_FILE, FLOOR_LIVE_STALE_MS),
    alarm: read(ALARM_FILE, Infinity), // the record carries its own times: the row judges them
  }
}

const isNum = (v) => typeof v === "number" && Number.isFinite(v)
const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : {})
const clean = (v, max) => String(v ?? "").replace(/[^\x20-\x7e]/g, "").slice(0, max)

// floor-live.json carries live sessions only; this list is a second guard for a hand-made file.
const FINISHED = ["done", "failed", "cancelled", "retired"]
const isLead = (s) => s.mode === "orchestrator" || s.agent === "caretaker" || s.agent === "orchestrator"

function waitingByAgent(inputs) {
  const out = new Map()
  const listed = inputs.decisions && Array.isArray(inputs.decisions.decisions) ? inputs.decisions.decisions : []
  for (const d of listed) {
    if (!d || d.closed || d.kind === "notice" || (d.tier ?? "human") !== "human") continue
    out.set(d.agent, (out.get(d.agent) ?? 0) + 1)
  }
  return out
}

// Caretakers: {kind, name, repo, charter, mode, state, busy, rounds, open, waiting}, from the live Floor sessions and the pane agents.
export function caretakerBlocks(inputs, now) {
  const states = obj(obj(inputs.pane).states)
  const waits = waitingByAgent(inputs)
  const open = obj(obj(inputs.load).latest)
  const seen = new Map()
  const add = (name, repo, charter, mode, state, busy, rounds) => {
    const n = clean(name, 40)
    if (!n || seen.has(n)) return
    const fig = obj(open[name])
    seen.set(n, {
      kind: "caretaker", name: n, repo: clean(repo, 60), charter: clean(charter, 60), mode: clean(mode, 20) || "caretaker", state: clean(state, 12) || "running",
      busy: typeof busy === "boolean" ? busy : null, rounds: isNum(rounds) ? rounds : 1, open: isNum(fig.open_asks) ? fig.open_asks : null, waiting: waits.get(name) ?? 0,
    })
  }
  const sessions = Array.isArray(obj(inputs.floor).sessions) ? inputs.floor.sessions : []
  for (const s of sessions) {
    const x = obj(s)
    if (isLead(x) && !FINISHED.includes(x.state)) add(x.name, x.repo, x.charter, x.mode, x.state, x.busy, x.rounds)
  }
  for (const [name, a] of Object.entries(obj(obj(inputs.pane).agents))) {
    const x = obj(a)
    if (obj(states[name]).state === "dead") continue
    if (x.role === "caretaker" || x.role === "orchestrator" || (x.kind === "cloud" && (x.charter || x.repo))) add(name, x.repo, x.charter, x.role ?? "caretaker", obj(states[name]).state, null, 1)
  }
  return [...seen.values()].sort((a, b) => a.name.localeCompare(b.name))
}

// Active cloud workers: {kind, name, repo, issue, branch, state, rounds, elapsed, summary}; elapsed is seconds or null.
export function workerBlocks(inputs, now) {
  const sessions = Array.isArray(obj(inputs.floor).sessions) ? inputs.floor.sessions : []
  const out = []
  for (const s of sessions) {
    const x = obj(s)
    if (isLead(x) || FINISHED.includes(x.state)) continue
    const name = clean(x.name, 40)
    if (!name) continue
    const at = isNum(x.started_at) ? x.started_at : null
    // Guard for floor-live.json rows written before the writer (brief_summary in bin/cloud_floor.py) kept header lines out.
    const rawSum = clean(x.summary, 120)
    const summary = /^ISSUE\s*:/i.test(rawSum) ? "" : rawSum
    out.push({
      kind: "worker", name, repo: clean(x.repo, 60), issue: isNum(x.issue) ? x.issue : null, branch: clean(x.branch, 60), state: clean(x.state, 12) || "running",
      rounds: isNum(x.rounds) ? x.rounds : 1, elapsed: at === null ? null : Math.max(0, Math.floor(now / 1000 - at)), summary,
    })
  }
  return out.sort((a, b) => a.name.localeCompare(b.name))
}

export function abbrevRepo(repo) {
  const r = String(repo || "").split("/").pop() || ""
  return r ? r.split(/[-_]+/).filter(Boolean).map((w) => w.slice(0, 2)).join("-") : ""
}
const target = (repo, issue) => (repo ? abbrevRepo(repo) + (issue !== null && issue !== undefined ? `#${issue}` : "") : "")
export function elapsedText(sec) {
  if (sec === null) return ""
  return sec < 60 ? `${sec}s` : sec < 3600 ? `${Math.floor(sec / 60)}m` : `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m`
}

function cut(segs, width) {
  const out = []
  let left = width
  for (const [text, keys] of segs) {
    if (left <= 0) break
    if (text.length > left) {
      out.push([text.slice(0, Math.max(0, left - 1)) + "…", keys])
      break
    }
    out.push([text, keys])
    left -= text.length
  }
  return out
}

// One or two lines of a Caretaker as styled segments [[text, keys]]. Each line is cut to `width`.
export function blockLines(b, width) {
  const color = b.waiting > 0 ? "w" : b.busy ? "s" : "m"
  const act = b.busy === null ? "state unknown" : b.busy ? "work" : "sleep"
  const label = clean(b.repo || b.charter, 60)
  const status = `${b.mode} ${act} r${b.rounds}`
  const first = [["●", color], [" ", ""], [b.name, "b"], [" ", ""], [status, color], [" · ", ""], [label, ""]]
  const lines = [cut(first, width)]
  if (b.open !== null || b.waiting > 0) {
    const bits = []
    if (b.open !== null) bits.push(`q${b.open} open`)
    if (b.waiting > 0) bits.push(`d${b.waiting} waiting`)
    lines.push(cut([["  " + bits.join(" · "), "m"]], width))
  }
  return lines
}

// One or two lines of an active cloud worker: name, repo#issue, state, round, elapsed; then its one-line summary.
export function workerLines(w, width) {
  const tail = [w.state, `r${w.rounds}`, elapsedText(w.elapsed)].filter(Boolean).join(" ")
  const lines = [cut([["●", "s"], [" ", ""], [w.name, "b"], [" ", ""], [target(w.repo, w.issue), ""], [" ", ""], [tail, "m"]], width)]
  if (w.summary) lines.push(cut([["  " + clean(w.summary, Math.max(0, width - 2)), "m"]], width))
  return lines
}

// All rows of the health section: the owner's alarm, the Caretakers, then the Active Cloud Workers. [] when there is nothing to show.
export function healthRows(inputs, now, width) {
  const rows = [...alarmRows(inputs.alarm, now, width)]
  const care = caretakerBlocks(inputs, now)
  if (care.length) {
    rows.push({ segs: [["Caretakers", "b"], [` (${care.length})`, "m"]] })
    for (const b of care) for (const segs of blockLines(b, width)) rows.push({ segs })
    rows.push({ segs: [["", ""]] })
  }
  const workers = workerBlocks(inputs, now)
  if (workers.length) {
    rows.push({ segs: [["Active Cloud Workers", "b"], [` (${workers.length})`, "m"]] })
    for (const w of workers) for (const segs of workerLines(w, width)) rows.push({ segs })
    rows.push({ segs: [["", ""]] })
  }
  return rows
}

// The Chief of Staff's blocked alarm (owner-alarm.json): a headline, the line that says what it waits on, and the command that resumes it.
// A blocked record shows while the daemon still rewrites it (ALARM_STALE_MS: the daemon stopped, or the agent is gone, otherwise it would
// count up for ever); an ended one for an hour; a missing, malformed or older record shows nothing. The daemon's text is cleaned like every name.
export function alarmRows(file, now, width) {
  const rec = obj(obj(file).alarm)
  const agent = clean(rec.agent, 24)
  if (!agent || !isNum(rec.since)) return []
  const rows = []
  if (rec.ended === null || rec.ended === undefined) {
    if (!isNum(rec.at) || now - rec.at * 1000 > ALARM_STALE_MS) return [] // no time of the last write: nothing says it is still alive
    rows.push(`! ${agent} blocked ${Math.max(0, Math.floor((now - rec.since * 1000) / 60000))} minutes`)
    rows.push(...wrapText(clean(rec.text, 400), width, 6)) // wrapped, not cut: the wait and the command are the point of the row
    const resume = clean(rec.resume, 120)
    if (resume) rows.push(...wrapText(`resume: ${resume}`, width, 3))
  } else {
    if (!isNum(rec.ended) || now - rec.ended * 1000 > ALARM_KEEP_MS) return []
    rows.push(`${agent} block ended (${Math.max(0, Math.floor((rec.ended - rec.since) / 60))} minutes)`)
  }
  return [...rows.map((label, i) => ({ segs: [[label.slice(0, width), i === 0 ? "b" : "m"]] })), { segs: [["", ""]] }]
}
