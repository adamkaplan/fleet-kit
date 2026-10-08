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
export const LONG_WAIT_SECONDS = 3600  // a decision waiting longer than this is "over 1h" (the CLI's status line counts the same way)
export const HEADLINE_LINES = 2       // lines of one entry's headline
export const ROW_LINES = 2            // lines of one row of a batch (`short-ref: recommendation`)
export const FACTS_LINES = 3          // lines of a heads-up's facts line (it wraps at ROW_WIDTH)
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

// A PR notice (kind "notice") is information, not a decision: it is never counted or listed with decisions.
export const isNotice = (d) => d.kind === "notice"

// The decisions a view shows. A decision with no tier (a file an older daemon wrote) is the human's.
export function selectFor(decisions, view) {
  const real = decisions.filter((d) => !isNotice(d))
  if (view.kind === "all") return real
  if (view.kind === "repo") return real.filter((d) => d.repo === view.repo)
  return real.filter((d) => (d.tier ?? "human") === "human")
}

// The heads-ups a view shows: every notice for the Chief of Staff and for a human, those of its repo for a repo's pane.
export function noticesFor(decisions, view) {
  const all = decisions.filter(isNotice)
  return view.kind === "repo" ? all.filter((d) => d.repo === view.repo) : all
}

// ---- links. A row links to its issue or PR so a click can open it. The URL is built ONLY from a repo and a
// number that match a strict pattern, never from a title or any other text of the entry, so nothing a title
// says can reach a terminal escape. A row with no repo or no number has no link.
export const REPO_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9._-]+$/
const NUMBER_PATTERN = /^[1-9][0-9]{0,8}$/

// The issue (a decision) or PR (a heads-up) URL of an entry, or null.
export function urlOf(d) {
  if (!d || typeof d.repo !== "string" || !REPO_PATTERN.test(d.repo) || d.repo.split("/").some((p) => p === "." || p === "..")) return null
  const raw = isNotice(d) ? d.number : d.ask
  const n = typeof raw === "number" || typeof raw === "string" ? String(raw) : ""
  if (!NUMBER_PATTERN.test(n)) return null
  return `https://github.com/${d.repo}/${isNotice(d) ? "pull" : "issues"}/${n}`
}

// Text safe to put on a line the terminal draws: every control character except tab and line breaks (which wrap
// collapses to a space) removed: C0, DEL, C1: ESC, BEL, the 8-bit CSI/OSC introducers and string terminators.
export const stripControls = (text) => String(text ?? "").replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g, "")

// A URL that may be handed to the terminal: only one urlOf can build, re-checked here, or null.
export function safeUrl(url) {
  return typeof url === "string" && /^https:\/\/github\.com\/[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9._-]+\/(issues|pull)\/[1-9][0-9]{0,8}$/.test(url) ? url : null
}

// "14:05 UTC" from an ISO time: UTC, like every time the daemon writes, so the panel and the file agree.
export function clockOf(iso) {
  const t = Date.parse(iso)
  return Number.isNaN(t) ? null : new Date(t).toISOString().slice(11, 16) + " UTC"
}

// The facts of a heads-up as one line: `+120/-14, 5 files, CI green, approved`, then `merges after HH:MM UTC`
// when the daemon set a hold window, and `(stale)` when the last read of the PR failed.
export function factsOf(d) {
  const parts = []
  if (Number.isInteger(d.additions) && Number.isInteger(d.deletions)) parts.push(`+${d.additions}/-${d.deletions}`)
  if (Number.isInteger(d.files)) parts.push(`${d.files} file${d.files === 1 ? "" : "s"}`)
  if (d.ci) parts.push(d.ci === "none" ? "no CI" : `CI ${d.ci}`)
  if (d.review) parts.push(d.review === "none" ? "no review" : d.review)
  let text = parts.join(", ")
  const after = d.merge_after ? clockOf(d.merge_after) : null
  if (after) text += `${text ? ", " : ""}merges after ${after}`
  if (d.stale) text += `${text ? " " : ""}(stale)`
  return text || "facts not read yet"
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

// One decision: its first line `<id> <age>`, then the daemon's headline (the title, for an older file with none) wrapped to `width` and cut to HEADLINE_LINES lines, the last ending in an ellipsis if cut.
export function entryOf(decision, nowMs, width = ROW_WIDTH) {
  const since = Date.parse(decision.since)
  const age = Number.isNaN(since) ? "?" : formatAge((nowMs - since) / 1000)
  const via = Array.isArray(decision.reported_by) && decision.reported_by.length ? ` ${decision.reported_by.join(",")}` : ""
  // the daemon's headline (an older file has none: its title), never more than HEADLINE_LINES lines
  const text = stripControls(typeof decision.headline === "string" && decision.headline ? decision.headline : decision.title)
  const entry = { key: decision.id, head: oneLine(`${decision.id} ${age}${via}`, width), lines: wrapText(text, width, HEADLINE_LINES) }
  // a batch: one line (wrapped to ROW_LINES) per row, between the headline and the id; the daemon wrote each row's text
  const rows = Array.isArray(decision.rows) ? decision.rows.filter((r) => r && typeof r.text === "string") : []
  if (rows.length) entry.rows = rows.map((r) => wrapText(stripControls(r.text), width, ROW_LINES))
  const url = urlOf(decision)
  if (url) entry.url = url  // only when there is one: an entry without a link is exactly what it was
  if (isNotice(decision)) entry.facts = wrapText(factsOf(decision), width, FACTS_LINES)  // a heads-up: facts between headline and id
  return entry
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
  // Heads-up (PR notices) follows Waits on you, before Waits on cos; it is information and is never in a decision count.
  const notices = noticesFor(snapshot.decisions, view)
  if (notices.length) sections.splice(1, 0, { key: "notice", label: "Heads-up", list: notices })
  const rows = []
  if (!groups.length && !notices.length) rows.push({ key: "none", text: "none" })
  if (snapshot.errors.length) {
    rows.push({ key: "errors", text: oneLine(`${snapshot.errors.length} source(s) unreadable`, width) })
  }
  return {
    title: `Decisions (${shown.length}) ${view.label}`, groups, rows,
    sections: sections.map((x) => ({
      key: x.key, count: x.list.length, groups: groupsOf(x.list),
      // what waits on cos shows how many have waited over an hour: the queue that must not pile up
      title: x.key === "cos" ? `${x.label} (${x.list.length}, ${overLongWait(x.list, nowMs)} over 1h)` : `${x.label} (${x.list.length})`,
    })),
  }
}

// How many of `list` have waited longer than LONG_WAIT_SECONDS at `nowMs`; one with no readable time is not counted.
export function overLongWait(list, nowMs) {
  return list.filter((d) => { const t = Date.parse(d.since); return !Number.isNaN(t) && (nowMs - t) / 1000 > LONG_WAIT_SECONDS }).length
}

// Whether the view has a Heads-up section (then an empty "Waits on you" says none, not nothing).
export const hasNotices = (view) => (view.sections ?? []).some((x) => x.key === "notice")

// The rules of the layout: a thin one between entries, a heavy one between repos and above a section.
export const THIN = (width) => "\u2504".repeat(width)
export const HEAVY = (width) => "\u2501".repeat(width)

// The view as plain text lines: what the sidebar shows, for tests and captures.
export function viewLines(view, width = ROW_WIDTH) {
  const out = [view.title]
  for (const section of view.sections ?? []) {
    out.push(HEAVY(width), section.title)
    if (!section.groups.length && (view.groups.length || hasNotices(view))) out.push("none")
    section.groups.forEach((g, i) => {
      if (i) out.push(HEAVY(width))
      out.push(g.repo)
      g.entries.forEach((e, j) => {
        if (j) out.push(THIN(width))
        out.push(...e.lines, ...(e.rows ?? []).flat(), ...(e.facts ?? []), e.head)
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
