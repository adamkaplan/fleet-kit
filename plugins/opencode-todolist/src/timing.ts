import { hasOpenTodos, type Todo, type TodoStatus } from "./todos.js"

/** Per-task timing collected while a todo run is active. */
export type TodoItemTiming = {
  content: string
  status: TodoStatus
  startedAt?: number
  completedAt?: number
}

/**
 * A single pass over a todo list: from the write that first introduced open
 * tasks until the write where nothing is open anymore. `abandoned` marks a run
 * that was replaced by a different list (or cleared) instead of completing.
 */
export type TodoRunTiming = {
  startedAt: number
  endedAt?: number
  abandoned?: boolean
  items: Array<TodoItemTiming>
}

export type TimingState = {
  current?: TodoRunTiming
  last?: TodoRunTiming
}

/**
 * Folds one `todowrite` into the timing state. Pure so the server plugin
 * (persisted incrementally) and the TUI (replayed from the message log) agree.
 */
export function applyTodoWrite(state: TimingState | undefined, todos: Array<Todo>, at: number): TimingState {
  const previous: TimingState = state ?? {}
  const current = previous.current
  const hasOpen = hasOpenTodos(todos)

  if (current) {
    const openBefore = current.items.filter((item) => item.status === "pending" || item.status === "in_progress")
    const continues = todos.some((todo) => openBefore.some((item) => item.content === todo.content))
    if (openBefore.length > 0 && hasOpen && !continues) {
      // A different list took over while this one still had open tasks.
      return { current: startRun(todos, at), last: closeRun(current, at, true) }
    }
  }

  if (!current) {
    if (!hasOpen) return previous
    return { ...previous, current: startRun(todos, at) }
  }

  const run: TodoRunTiming = { ...current, items: mergeItems(current.items, todos, at) }
  if (todos.length === 0) {
    return { ...previous, current: undefined, last: closeRun(run, at, true) }
  }
  if (!hasOpen) {
    return { ...previous, current: undefined, last: closeRun(run, at, false) }
  }
  return { ...previous, current: run }
}

/** Replays a chronological list of writes into a timing state. */
export function buildTiming(writes: ReadonlyArray<{ at: number | null; todos: Array<Todo> }>): TimingState {
  let state: TimingState = {}
  for (const write of writes) {
    if (typeof write.at !== "number") continue
    state = applyTodoWrite(state, write.todos, write.at)
  }
  return state
}

/** Elapsed time of a run: final duration when closed, live age otherwise. */
export function runElapsed(run: TodoRunTiming, now: number): number {
  return (run.endedAt ?? now) - run.startedAt
}

/** Whether a run reached the end with at least one task actually completed. */
export function hasCompletedItem(run: TodoRunTiming): boolean {
  return run.items.some((item) => item.completedAt !== undefined)
}

/** Short label for a task's own time: fixed when completed, live while running. */
export function itemTimeLabel(item: TodoItemTiming | undefined, now: number): string | undefined {
  if (!item || item.startedAt === undefined) return undefined
  if (item.status === "completed" && item.completedAt !== undefined) {
    return formatDuration(item.completedAt - item.startedAt)
  }
  if (item.status === "in_progress") return formatDuration(now - item.startedAt)
  return undefined
}

export function findItem(run: TodoRunTiming | undefined, content: string): TodoItemTiming | undefined {
  return run?.items.find((item) => item.content === content)
}

/** One-line timing summary for tool output, e.g. `running for 4m12s`. */
export function timingSummary(state: TimingState | undefined, now: number): string | undefined {
  const current = state?.current
  if (current) return `running for ${formatDuration(runElapsed(current, now))}`
  const last = state?.last
  if (!last || last.endedAt === undefined) return undefined
  if (!last.abandoned && !hasCompletedItem(last)) return undefined
  const label = last.abandoned ? "previous list abandoned after" : "finished in"
  return `${label} ${formatDuration(runElapsed(last, now))}`
}

/** `42s`, `4m12s`, `1h03m`; anything unmeasurable collapses to `0s`. */
export function formatDuration(ms: number): string {
  const total = Number.isFinite(ms) && ms > 0 ? Math.floor(ms / 1000) : 0
  const seconds = total % 60
  const minutes = Math.floor(total / 60) % 60
  const hours = Math.floor(total / 3600)
  if (hours > 0) return `${hours}h${String(minutes).padStart(2, "0")}m`
  if (minutes > 0) return `${minutes}m${String(seconds).padStart(2, "0")}s`
  return `${seconds}s`
}

function startRun(todos: Array<Todo>, at: number): TodoRunTiming {
  return { startedAt: at, items: todos.map((todo) => initialItem(todo, at)) }
}

function initialItem(todo: Todo, at: number): TodoItemTiming {
  const item: TodoItemTiming = { content: todo.content, status: todo.status }
  if (todo.status === "in_progress") item.startedAt = at
  if (todo.status === "completed") item.completedAt = at
  return item
}

function mergeItems(
  existing: Array<TodoItemTiming>,
  todos: Array<Todo>,
  at: number,
): Array<TodoItemTiming> {
  const byContent = new Map(existing.map((item) => [item.content, item]))
  return todos.map((todo) => {
    const previous = byContent.get(todo.content)
    const item: TodoItemTiming = previous
      ? { ...previous, status: todo.status }
      : { content: todo.content, status: todo.status }
    if (todo.status === "in_progress" && item.startedAt === undefined) item.startedAt = at
    if (todo.status === "completed" && item.completedAt === undefined) item.completedAt = at
    return item
  })
}

function closeRun(run: TodoRunTiming, at: number, abandoned: boolean): TodoRunTiming {
  return { ...run, endedAt: at, ...(abandoned ? { abandoned: true } : {}) }
}
