// The pure part of the Decisions plugin: read the file the switchboard daemon keeps, say how old it is, and
// turn it into the rows the sidebar draws. No process is spawned and nothing leaves this machine; the file
// is read from a state directory named by FLEET_SWITCHBOARD_STATE. See docs/switchboard.md, "Decisions (PR 14)".
import { readFileSync, statSync } from "node:fs"
import { join } from "node:path"

export const STATE_ENV = "FLEET_SWITCHBOARD_STATE"
export const DECISIONS_FILE = "decisions.json"
export const STALE_MS = 120_000       // older than this (by mtime, which every daemon pass refreshes): not current
export const SAFETY_MS = 60_000       // one slow re-read in case a file event was missed
export const ROW_WIDTH = 34           // characters of one row: the sidebar is about 36 wide (Lab, 160 columns); a row never wraps
export const NOT_UPDATING = "daemon not updating"

// The state directory from the environment, or null when it is unset or empty.
export function stateDirOf(env) {
  const value = env && env[STATE_ENV]
  return typeof value === "string" && value.trim() ? value : null
}

// What the file says now: {status, decisions, errors, ageMs}.
// status: "ok" | "stale" | "missing" | "unreadable" | "unset". Only "ok" is a current list.
export function loadSnapshot(dir, nowMs, fs = { statSync, readFileSync }) {
  const none = { decisions: [], errors: [], ageMs: null }
  if (!dir) return { status: "unset", ...none }
  const path = join(dir, DECISIONS_FILE)
  let info
  try {
    info = fs.statSync(path)
  } catch (err) {
    return { status: err && err.code === "ENOENT" ? "missing" : "unreadable", ...none }
  }
  let body
  try {
    body = JSON.parse(fs.readFileSync(path, "utf8"))
  } catch {
    return { status: "unreadable", ...none }
  }
  if (!body || !Array.isArray(body.decisions)) return { status: "unreadable", ...none }
  const ageMs = Math.max(0, nowMs - info.mtimeMs)
  return {
    status: ageMs > STALE_MS ? "stale" : "ok",
    decisions: body.decisions.filter((d) => d && typeof d.id === "string" && typeof d.title === "string"),
    errors: Array.isArray(body.errors) ? body.errors : [],
    ageMs,
  }
}

// `45s`, `12m`, `1h05m`, `3d`: the same text `fleet-switchboard decisions` prints.
export function formatAge(seconds) {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) {
    const minutes = Math.floor((s % 3600) / 60)
    return minutes ? `${Math.floor(s / 3600)}h${String(minutes).padStart(2, "0")}m` : `${Math.floor(s / 3600)}h`
  }
  return `${Math.floor(s / 86400)}d`
}

// Cut `text` to one line of at most `width` characters, ending in an ellipsis when it was cut.
export function oneLine(text, width) {
  const flat = String(text ?? "").replace(/\s+/g, " ").trim()
  return flat.length <= width ? flat : flat.slice(0, Math.max(0, width - 1)) + "\u2026"
}

// One row of the list: `<id> <age> <title>`, at most `width` characters.
export function rowOf(decision, nowMs, width = ROW_WIDTH) {
  const since = Date.parse(decision.since)
  const age = Number.isNaN(since) ? "?" : formatAge((nowMs - since) / 1000)
  const head = `${decision.id} ${age} `
  return head + oneLine(decision.title, Math.max(8, width - head.length))
}

// The section: a title and its rows. Never empty, never absent: an empty list says "none", and a list the
// daemon does not keep current says so instead of showing as current.
export function buildView(snapshot, nowMs, width = ROW_WIDTH) {
  if (snapshot.status !== "ok") {
    return { title: "Decisions (?)", rows: [{ key: "state", text: NOT_UPDATING }] }
  }
  const rows = snapshot.decisions.length
    ? snapshot.decisions.map((d) => ({ key: d.id, text: rowOf(d, nowMs, width) }))
    : [{ key: "none", text: "none" }]
  if (snapshot.errors.length) {
    rows.push({ key: "errors", text: oneLine(`${snapshot.errors.length} source(s) unreadable`, width) })
  }
  return { title: `Decisions (${snapshot.decisions.length})`, rows }
}

// Milliseconds until a list that is "ok" now turns stale if no pass touches the file again; null otherwise.
export function msUntilStale(snapshot) {
  return snapshot.status === "ok" ? Math.max(1000, STALE_MS - snapshot.ageMs + 1000) : null
}

// Whether a directory event is about the decisions file (a rename names it; some platforms give no name).
export function concernsFile(filename) {
  if (filename == null) return true
  const name = String(filename)
  return name === DECISIONS_FILE || name.startsWith(`${DECISIONS_FILE}.`)
}
