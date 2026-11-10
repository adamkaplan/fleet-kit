import { normalizeTodos, type Todo } from "./todos.js"
import type { TodoDisplaySettings } from "./tui-settings.js"
import { formatDuration, hasCompletedItem, runElapsed, type TimingState } from "./timing.js"

type PartTime = {
  created?: unknown
  ran?: unknown
  completed?: unknown
}

type ToolPart = {
  type?: unknown
  name?: unknown
  time?: PartTime
  state?: { status?: unknown; input?: unknown; time?: PartTime } | undefined
}

type MessageLike = {
  content?: unknown
  time?: unknown
}

/** One completed `todowrite` recovered from the session log, in write order. */
export type TodoWrite = {
  /** Write timestamp in ms, or null when the part carries no time. */
  at: number | null
  todos: Array<Todo>
}

/**
 * Replays every completed `todowrite` in the session messages into a
 * chronological timeline. Parts without a timestamp are ordered last, keeping
 * their original order so legacy logs behave like the previous last-wins scan.
 */
export function todowritesFromMessages(messages: ReadonlyArray<unknown> | undefined): Array<TodoWrite> {
  if (!Array.isArray(messages)) return []
  const writes: Array<{ at: number | null; todos: Array<Todo>; order: number }> = []
  let order = 0
  for (const message of messages) {
    if (message === null || typeof message !== "object") continue
    const content = (message as MessageLike).content
    if (!Array.isArray(content)) continue
    for (const part of content) {
      if (part === null || typeof part !== "object") continue
      const tool = part as ToolPart
      if (tool.type !== "tool" || tool.name !== "todowrite") continue
      if (tool.state?.status !== "completed") continue
      let todos: Array<Todo>
      try {
        todos = normalizeTodos(tool.state.input)
      } catch {
        // Skip malformed historical calls; a later valid write still wins.
        continue
      }
      writes.push({ at: writeTimestamp(tool, message as MessageLike) ?? null, todos, order: order++ })
    }
  }
  writes.sort((left, right) => {
    if (left.at === null && right.at === null) return left.order - right.order
    if (left.at === null) return 1
    if (right.at === null) return -1
    if (left.at !== right.at) return left.at - right.at
    return left.order - right.order
  })
  return writes.map(({ at, todos }) => ({ at, todos }))
}

/**
 * Returns the most recent completed `todowrite` list. The strip relies on this
 * because the TUI cannot read the server plugin storage.
 */
export function latestTodosFromMessages(messages: ReadonlyArray<unknown> | undefined): Array<Todo> | undefined {
  const writes = todowritesFromMessages(messages)
  return writes.length === 0 ? undefined : writes[writes.length - 1].todos
}

function writeTimestamp(part: ToolPart, message: MessageLike): number | undefined {
  const candidates = [
    part.time?.created,
    part.time?.ran,
    part.time?.completed,
    part.state?.time?.created,
    part.state?.time?.ran,
    part.state?.time?.completed,
  ]
  for (const value of candidates) {
    if (typeof value === "number") return value
  }
  if (typeof message.time === "number") return message.time
  if (message.time !== null && typeof message.time === "object") {
    const created = (message.time as { created?: unknown }).created
    if (typeof created === "number") return created
  }
  return undefined
}

/**
 * Builds the strip heading: `Todo [done/total] · NN%`, dropping either part
 * according to the display settings and falling back to plain `Todo`.
 */
export function headerLabel(
  todos: ReadonlyArray<Todo>,
  display: Pick<TodoDisplaySettings, "count" | "percent">,
): string {
  const total = todos.length
  const done = todos.filter((todo) => todo.status === "completed").length
  const percent = total === 0 ? 0 : Math.round((done / total) * 100)
  const parts: Array<string> = []
  if (display.count) parts.push(`[${done}/${total}]`)
  if (display.percent) parts.push(`${percent}%`)
  return parts.length === 0 ? "Todo" : `Todo ${parts.join(" · ")}`
}

/** Standalone timer label for the right side of the heading, without a separator. */
export function headerTimeLabel(state: TimingState, timer: boolean, now: number): string | undefined {
  if (!timer) return undefined
  if (state.current) return formatDuration(runElapsed(state.current, now))
  const last = state.last
  if (last && !last.abandoned && hasCompletedItem(last)) {
    return `done in ${formatDuration(runElapsed(last, now))}`
  }
  return undefined
}
