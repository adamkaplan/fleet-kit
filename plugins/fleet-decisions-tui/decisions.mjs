// The pure part of the Decisions plugin: read the file the switchboard daemon keeps, say how old it is, and
// turn it into the rows the sidebar draws. No process is spawned and nothing leaves this machine; the file
// is read from the switchboard's state directory (FLEET_SWITCHBOARD_STATE, else the daemon's default one).
// See docs/switchboard.md, "Decisions (PR 14)".
import { readFileSync, statSync } from "node:fs"
import { isAbsolute, join } from "node:path"

export const STATE_ENV = "FLEET_SWITCHBOARD_STATE"
export const DECISIONS_FILE = "decisions.json"
export const STALE_MS = 120_000       // older than this (by mtime, which every daemon pass refreshes): not current
export const SAFETY_MS = 60_000       // one slow re-read in case a file event was missed
export const ROW_WIDTH = 34           // characters of one row: the sidebar is about 36 wide (Lab, 160 columns); titles wrap to it
export const HEADLINE_LINES = 2       // lines of one entry's headline
export const WRAP_MAX_LINES = 40     // safety cap on one title's lines; no real title comes near it
export const NOT_UPDATING = "daemon not updating"
export const ROLE_ENV = "FLEET_SWITCHBOARD_ROLE"  // PR 18: set by `launch` and `bootstrap` in the pane's environment
export const REPO_ENV = "FLEET_SWITCHBOARD_REPO"
export const HUMAN_VIEW = { kind: "human", repo: null, label: "for you" }

// Whose panel this is. The Chief of Staff's shows every tier and repo; a pane that names a repo shows that repo
// only (every tier); any other, a TUI that is not a fleet agent, shows the human tier.
export function viewOf(env) {
  const role = env && env[ROLE_ENV]
  const repo = env && env[REPO_ENV]
  if (role === "chief-of-staff") return { kind: "all", repo: null, label: "all" }
  if (typeof repo === "string" && repo.trim()) return { kind: "repo", repo: repo.trim(), label: repo.trim() }
  return HUMAN_VIEW
}

// The decisions a view shows. A decision with no tier (a file an older daemon wrote) is the human's.
export function selectFor(decisions, view) {
  if (view.kind === "all") return decisions
  if (view.kind === "repo") return decisions.filter((d) => d.repo === view.repo)
  return decisions.filter((d) => (d.tier ?? "human") === "human")
}

// The state directory: FLEET_SWITCHBOARD_STATE when it is set, else the one the daemon writes to by default,
// $XDG_STATE_HOME/fleet-switchboard or $HOME/.local/state/fleet-switchboard (a relative XDG_STATE_HOME is
// ignored, as the CLI ignores it). Null only when nothing says where it is. A TUI on a real install has
// no FLEET_SWITCHBOARD_STATE, which only the trial's wrapper exported.
export function stateDirOf(env) {
  const given = env && env[STATE_ENV]
  if (typeof given === "string" && given.trim()) return given
  const xdg = env && env.XDG_STATE_HOME
  if (typeof xdg === "string" && isAbsolute(xdg)) return join(xdg, "fleet-switchboard")
  const home = env && env.HOME
  if (typeof home === "string" && isAbsolute(home)) return join(home, ".local", "state", "fleet-switchboard")
  return null
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

// Word-wrap `text` to lines of at most `width` characters. Whitespace is collapsed; a word longer than a
// line is broken hard. Nothing is dropped, except past `maxLines` (a safety cap no real title reaches),
// where the last line ends in an ellipsis.
export function wrapText(text, width, maxLines = WRAP_MAX_LINES) {
  const w = Math.max(1, width)
  const lines = []
  let line = ""
  for (let word of String(text ?? "").split(/\s+/).filter(Boolean)) {
    while (word.length > w) {
      if (line) { lines.push(line); line = "" }
      lines.push(word.slice(0, w))
      word = word.slice(w)
    }
    if (!line) line = word
    else if (line.length + 1 + word.length <= w) line += " " + word
    else { lines.push(line); line = word }
  }
  if (line) lines.push(line)
  if (lines.length > maxLines) {
    const kept = lines.slice(0, maxLines)
    kept[maxLines - 1] = kept[maxLines - 1].slice(0, w - 1) + "\u2026"
    return kept
  }
  return lines
}

// One decision: its first line `<id> <age>`, then the whole title wrapped to `width`.
export function entryOf(decision, nowMs, width = ROW_WIDTH) {
  const since = Date.parse(decision.since)
  const age = Number.isNaN(since) ? "?" : formatAge((nowMs - since) / 1000)
  const via = Array.isArray(decision.reported_by) && decision.reported_by.length ? ` ${decision.reported_by.join(",")}` : ""
  // the daemon's headline (an older file has none: its title), never more than HEADLINE_LINES lines
  const text = typeof decision.headline === "string" && decision.headline ? decision.headline : decision.title
  return { key: decision.id, head: oneLine(`${decision.id} ${age}${via}`, width), lines: wrapText(text, width, HEADLINE_LINES) }
}

// The section: a title, decisions grouped by repo (repos by name, oldest decision first within one), and
// `rows` for the states that are not decisions. Never empty, never absent: an empty list says "none", and
// a list the daemon does not keep current says so instead of showing as current.
export function buildView(snapshot, nowMs, width = ROW_WIDTH, view = HUMAN_VIEW) {
  if (snapshot.status !== "ok") {
    return { title: "Decisions (?)", groups: [], rows: [{ key: "state", text: NOT_UPDATING }] }
  }
  const sinceOf = (d) => { const t = Date.parse(d.since); return Number.isNaN(t) ? Infinity : t }
  const shown = selectFor(snapshot.decisions, view)
  const groupsOf = (list) => {
    const by = new Map()
    for (const d of list) {
      const repo = typeof d.repo === "string" && d.repo ? d.repo : "(no repo)"
      if (!by.has(repo)) by.set(repo, [])
      by.get(repo).push(d)
    }
    return [...by.keys()].sort().map((repo) => ({
      repo,
      entries: by.get(repo)
        .map((d, i) => ({ d, i }))
        .sort((x, y) => sinceOf(x.d) - sinceOf(y.d) || x.i - y.i)
        .map(({ d }) => entryOf(d, nowMs, width)),
    }))
  }
  const groups = groupsOf(shown)
  // Waits on you is the human tier (a decision with no tier is the human's), in every view; what waits on the
  // Chief of Staff follows. A human's own view selects only the first, so it has one section.
  const tierOf = (d) => d.tier ?? "human"
  const sections = [{ key: "human", label: "Waits on you", list: shown.filter((d) => tierOf(d) === "human") }]
  if (view.kind !== "human") {
    sections.push({ key: "cos", label: "Waits on cos", list: shown.filter((d) => tierOf(d) === "cos") })
    const other = shown.filter((d) => !["human", "cos"].includes(tierOf(d)))
    if (other.length) sections.push({ key: "orchestrator", label: "Waits on orchestrator", list: other })
  }
  const rows = []
  if (!groups.length) rows.push({ key: "none", text: "none" })
  if (snapshot.errors.length) {
    rows.push({ key: "errors", text: oneLine(`${snapshot.errors.length} source(s) unreadable`, width) })
  }
  return {
    title: `Decisions (${shown.length}) ${view.label}`, groups, rows,
    sections: sections.map((x) => ({ key: x.key, title: `${x.label} (${x.list.length})`, count: x.list.length, groups: groupsOf(x.list) })),
  }
}

// The rules of the layout: a thin one between entries, a heavy one between repos and above a section.
export const THIN = (width) => "\u2504".repeat(width)
export const HEAVY = (width) => "\u2501".repeat(width)

// The view as plain text lines: what the sidebar shows, for tests and captures.
export function viewLines(view, width = ROW_WIDTH) {
  const out = [view.title]
  for (const section of view.sections ?? []) {
    out.push(HEAVY(width), section.title)
    if (!section.groups.length && view.groups.length) out.push("none")
    section.groups.forEach((g, i) => {
      if (i) out.push(HEAVY(width))
      out.push(g.repo)
      g.entries.forEach((e, j) => {
        if (j) out.push(THIN(width))
        out.push(...e.lines, e.head)
      })
    })
  }
  for (const r of view.rows) out.push(r.text)
  return out
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
