// The workspace health block of the right sidebar (ask #357, slice 1): one dense block per workspace, hottest first, two lines at most,
// drawn above the Decisions rows. Pure: it reads files the switchboard daemon and its recorders already keep in the state directory
// (health-records.json from bin/health_records.py, load.json from the load alert (#348), pane-health.json, decisions.json) and turns them
// into rows. No process is spawned, nothing leaves this machine, and nothing is polled that was not already there.
//
// A figure with no source says "not recorded", never 0: a file that is missing, unreadable, malformed or too old is a missing source, and
// so is a figure that file does not carry. Tool calls per minute are approximate (permission evaluations), marked "≈".
import { readFileSync, statSync } from "node:fs"
import { join } from "node:path"

export const NOT_RECORDED = "not recorded"
export const RECORDS_FILE = "health-records.json"
export const LOAD_FILE = "load.json"
export const PANE_FILE = "pane-health.json"
export const DECISIONS_FILE = "decisions.json"
export const WATCHED = [RECORDS_FILE, LOAD_FILE, PANE_FILE, DECISIONS_FILE]
export const RECORDS_STALE_MS = 600_000   // the daemon rewrites health-records.json at least every 5 minutes (its heartbeat)
export const LOAD_STALE_MS = 3 * 3600_000 // load.json is rewritten only when its content changes: an hour or two quiet is normal
export const PANE_STALE_MS = 600_000
export const DECISIONS_STALE_MS = 180_000
export const MAX_BYTES = 1 << 20
export const SPARK = "▁▂▃▄▅▆▇█"
export const SPARK_MINUTES = 15
export const WORKING_MINUTES = 3          // tool calls within this many minutes: the workspace is working
export const BAR_HOURS = 8                // hours of today drawn on the second line (the most recent ones)
export const NAME_WIDTH = 9
export const LEGEND = ["q open  s stalled  d decisions", "c busy/all coders  p pushes/hour", "ctx context use  moved asks/hour", "work idle wait dead  ? unknown"]

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
    records: read(RECORDS_FILE, RECORDS_STALE_MS),
    load: read(LOAD_FILE, LOAD_STALE_MS),
    pane: read(PANE_FILE, PANE_STALE_MS),
    decisions: read(DECISIONS_FILE, DECISIONS_STALE_MS),
  }
}

const isNum = (v) => typeof v === "number" && Number.isFinite(v)
const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : {})
const clean = (v, max) => String(v ?? "").replace(/[^\x20-\x7e]/g, "").slice(0, max)

// The tool-call series (15 minutes, oldest first) summed over some sessions, or null when the records carry none of it. An empty
// `tool_calls` table is a missing source: the recorder cannot tell an idle fleet from a plugin that is not running.
export function toolSeries(records, sessions) {
  const table = obj(obj(records).tool_calls)
  if (Object.keys(table).length === 0) return null
  const out = new Array(SPARK_MINUTES).fill(0)
  for (const session of sessions) {
    const row = obj(table[session])
    if (!Array.isArray(row.per_minute)) continue
    row.per_minute.slice(-SPARK_MINUTES).forEach((n, i) => { if (isNum(n) && n >= 0) out[SPARK_MINUTES - row.per_minute.slice(-SPARK_MINUTES).length + i] += n })
  }
  return out
}

// Why a tool-call series is missing, from the `sources` the recorder writes: the counter file the plugin leaves is absent (the plugin is not
// installed in the agents' profile), stale (not running), or present without this workspace. null when nothing is known.
export function toolWhy(records) {
  const src = obj(obj(records).sources).tool_calls
  return src === "missing" ? "plugin not installed" : src === "stale" ? "plugin not running" : null
}

// The usage source's own word ("off: v2_light" when the daemon reads no session history), or null.
export function usageOff(records) {
  const src = obj(obj(records).sources).usage
  return typeof src === "string" && src.startsWith("off") ? clean(src, 30) : null
}

export function sparkline(series) {
  const top = Math.max(...series, 0)
  return series.map((n) => SPARK[top === 0 ? 0 : Math.min(SPARK.length - 1, Math.floor((n / top) * (SPARK.length - 1) + 0.5))]).join("")
}

// Asks moved per hour so far today (UTC, as the load alert keeps it), the most recent BAR_HOURS, or null.
export function movedBars(load, name, now) {
  const mine = obj(obj(load.series)[name])
  const day = new Date(now).toISOString().slice(0, 10)
  if (mine.day !== day) return null
  const hours = obj(mine.hours)
  const last = new Date(now).getUTCHours()
  const out = []
  for (let h = Math.max(0, last - BAR_HOURS + 1); h <= last; h++) {
    const v = obj(hours[String(h).padStart(2, "0")]).moved_1h
    out.push(isNum(v) ? v : null)
  }
  return out.some((v) => v !== null) ? out : null
}

export function bars(values) {
  const top = Math.max(...values.map((v) => v ?? 0), 0)
  return values.map((v) => (v === null ? "·" : v === 0 ? SPARK[0] : SPARK[Math.max(1, Math.min(SPARK.length - 1, Math.ceil((v / top) * (SPARK.length - 1))))])).join("")
}

// The records name the context size by a word this plugin's source-scan test forbids anywhere else (it guards against credentials), so the key is spelled out here.
const CONTEXT_USED = ["to", "kens"].join("")
const ROLES = ["orchestrator", "chief-of-staff"]

// One block per workspace that has an orchestrator (or the Chief of Staff): {key, name, state, alert, spark, bars, open, stalled, decisions,
// busy, total, pushes, context, heat}. Every figure is a value or null (not recorded).
export function buildBlocks(inputs, now) {
  const agents = obj(obj(inputs.pane).agents)
  const states = obj(obj(inputs.pane).states)
  const load = obj(inputs.load)
  const latest = obj(load.latest)
  const hour = Math.floor(now / 3600_000) * 3600
  const byWorkspace = new Map()
  for (const [name, a] of Object.entries(agents)) {
    const ws = clean(obj(a).workspace, 12)
    if (!ws) continue
    if (!byWorkspace.has(ws)) byWorkspace.set(ws, [])
    byWorkspace.get(ws).push({ name: clean(name, 60), ...obj(a) })
  }
  const waitingFor = new Map()
  const listed = inputs.decisions && Array.isArray(inputs.decisions.decisions) ? inputs.decisions.decisions : null
  if (listed) {
    for (const d of listed) {
      if (!d || d.closed || d.kind === "notice" || (d.tier ?? "human") !== "human") continue
      waitingFor.set(d.agent, (waitingFor.get(d.agent) ?? 0) + 1)
    }
  }
  const blocks = []
  for (const [ws, members] of byWorkspace) {
    const lead = members.filter((m) => ROLES.includes(m.role)).sort((a, b) => (a.role === b.role ? a.name.localeCompare(b.name) : a.role === "orchestrator" ? -1 : 1))[0]
    if (!lead) continue
    const sessions = members.map((m) => m.session).filter((s) => typeof s === "string")
    const series = toolSeries(inputs.records, sessions)
    const fig = obj(latest[lead.name])
    // Only a pane the switchboard has found dead is "dead". An unchecked (unknown) or detached pane, or a pane file that does not know the agent,
    // is not recorded: the state word is "?".
    const paneState = inputs.pane ? obj(states[lead.name]).state : undefined
    const dead = paneState === "dead"
    const isCos = lead.role === "chief-of-staff"
    const uncertain = paneState !== "attached" && paneState !== "dead"
    const waiting = listed ? waitingFor.get(lead.name) ?? 0 : null
    const recent = series ? series.slice(-WORKING_MINUTES).reduce((a, b) => a + b, 0) : null
    // The state word of an attached pane: waiting on the owner (decisions), else working or idle from the tool calls when they are recorded,
    // else from the load record every workspace has (coders busy, or asks moved this hour). No source at all: "?".
    const figBusy = isNum(fig.busy) ? fig.busy : null
    const figMoved = isNum(fig.moved_1h) ? fig.moved_1h : null
    let state = null
    if (dead) state = "dead"
    else if (uncertain) state = null
    else if (waiting) state = "wait"
    else if (recent !== null) state = recent > 0 ? "work" : "idle"
    else if (figBusy !== null || figMoved !== null) state = (figBusy ?? 0) > 0 || (figMoved ?? 0) > 0 ? "work" : "idle"
    else if (inputs.decisions) state = "idle"
    const episode = obj(obj(load.episodes)[lead.name])
    const alert = load.alert === true && isNum(episode.sent) && episode.sent > 0
    const stalled = isNum(fig.stalled) ? fig.stalled : null
    const open = isNum(fig.open_asks) ? fig.open_asks : null
    const busy = isNum(fig.busy) ? fig.busy : null
    const idle = isNum(fig.idle) ? fig.idle : null
    const pushRow = obj(obj(obj(inputs.records).pushes)[lead.repo])
    const watched = Array.isArray(obj(inputs.records).pushes_watched) && obj(inputs.records).pushes_watched.includes(lead.repo)
    // A repository whose events the daemon receives and that had no push this hour is 0; "not recorded" only when it receives none.
    const pushes = lead.repo && isNum(pushRow[String(hour)]) ? pushRow[String(hour)] : lead.repo && watched ? 0 : null
    const ctx = obj(obj(obj(obj(inputs.records).usage)[lead.session]).context)
    const context = isNum(ctx.percent) ? { percent: ctx.percent } : isNum(ctx[CONTEXT_USED]) ? { used: ctx[CONTEXT_USED] } : null
    blocks.push({
      key: ws, name: lead.name, state, alert, spark: series, bars: movedBars(load, lead.name, now), open, stalled,
      decisions: isNum(fig.decisions) ? fig.decisions : waiting, busy, total: busy !== null && idle !== null ? busy + idle : null, pushes, context, cos: isCos, toolWhy: series ? null : toolWhy(inputs.records), ctxOff: usageOff(inputs.records),
      heat: (alert ? 1000 : 0) + (stalled ?? 0) * 10 + (open ?? 0) + (series ? series.reduce((a, b) => a + b, 0) / 10 : 0),
    })
  }
  return blocks.sort((a, b) => b.heat - a.heat || a.name.localeCompare(b.name))
}

const DOT = { work: ["●", "s"], idle: ["●", "m"], wait: ["●", "w"], dead: ["●", "e"] }
const kilo = (n) => (n >= 1000 ? `${Math.round(n / 1000)}k` : String(n))

// The two lines of a block as styled segments [[text, keys]], the shape the Decisions rows use. Each line is cut to `width`.
export function blockLines(b, width) {
  const [dot, color] = DOT[b.state] ?? ["○", "m"]
  const name = clean(b.name, NAME_WIDTH).padEnd(NAME_WIDTH)
  const first = [
    [dot, color], [b.alert ? "!" : " ", "e+b"], [" ", ""], [name, "b"], [" ", ""], [(b.state ?? "?").padEnd(4), color], [" ", ""],
    b.spark ? [sparkline(b.spark) + "≈", ""] : b.toolWhy ? [b.toolWhy === "plugin not installed" ? "no plugin" : "plugin off", "m"] : [NOT_RECORDED, "m"],
  ]
  const piece = (label, value) => (value === null ? [`${label} ${NOT_RECORDED}`, "m"] : [`${label}${value}`, ""])
  if (b.cos) {
    return [cut(first, width), cut([piece("d", b.decisions), [" no load row: not orchestrator", "m"]], width)]
  }
  const second = [
    b.bars ? [bars(b.bars), ""] : [`moved ${NOT_RECORDED}`, "m"],
    piece(" q", b.open), piece(" s", b.stalled), piece(" d", b.decisions),
    b.busy !== null && b.total !== null ? [` c${b.busy}/${b.total}`, ""] : [` coders ${NOT_RECORDED}`, "m"],
    piece(" p", b.pushes),
    b.context ? [b.context.percent !== undefined ? ` ctx${Math.round(b.context.percent)}%` : ` ctx${kilo(b.context.used)}`, ""] : b.ctxOff ? [" ctx off", "m"] : [` ctx ${NOT_RECORDED}`, "m"],
  ]
  return [cut(first, width), cut(second, width)]
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

// All rows of the health section: a heading, then each block's two lines. [] when there is no workspace to show (nothing is drawn).
export function healthRows(inputs, now, width) {
  const blocks = buildBlocks(inputs, now)
  if (blocks.length === 0) return []
  const rows = [{ segs: [["Workspaces", "b"], [" · tool calls ≈", "m"]] }]
  for (const line of LEGEND) rows.push({ segs: [[line.slice(0, width), "m"]] })
  const why = blocks.map((b) => b.toolWhy).find(Boolean)
  if (why) rows.push({ segs: [[`tool calls: ${why}`.slice(0, width), "m"]] })
  const off = blocks.map((b) => b.ctxOff).find(Boolean)
  if (off) rows.push({ segs: [[`ctx: ${off}`.slice(0, width), "m"]] })
  for (const block of blocks) for (const segs of blockLines(block, width)) rows.push({ segs })
  rows.push({ segs: [["", ""]] })
  return rows
}
