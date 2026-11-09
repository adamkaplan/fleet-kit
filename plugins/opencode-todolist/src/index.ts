import type { Plugin } from "@opencode/plugin"
import {
  TODO_PRIORITIES,
  TODO_STATUSES,
  normalizeTodos,
  parseTodoRecord,
  renderTodos,
  storageKey,
} from "./todos.js"
import { applyTodoWrite, findItem, itemTimeLabel, timingSummary } from "./timing.js"

export * from "./todos.js"

const TODOS_INPUT_SCHEMA = {
  type: "object",
  properties: {
    todos: {
      type: "array",
      description: "The complete todo list. This replaces any previous list.",
      items: {
        type: "object",
        properties: {
          content: {
            type: "string",
            description: "Imperative description of the task.",
          },
          status: {
            type: "string",
            enum: [...TODO_STATUSES],
            description: "Current state of the task.",
          },
          priority: {
            type: "string",
            enum: [...TODO_PRIORITIES],
            description: "Optional priority of the task.",
          },
        },
        required: ["content", "status"],
        additionalProperties: false,
      },
    },
  },
  required: ["todos"],
  additionalProperties: false,
}

const TODO_READ_INPUT_SCHEMA = {
  type: "object",
  properties: {},
  additionalProperties: false,
}

const TODOWRITE_DESCRIPTION = [
  "Create or replace the session todo list.",
  "Pass the complete list every time; it replaces any previous one.",
  "Use it to plan multi-step work and keep status current: keep exactly one task in_progress while working on it,",
  "mark tasks completed as soon as they are done, and cancel tasks that are no longer needed.",
  "Prefer short, imperative task descriptions.",
].join(" ")

const TODOREAD_DESCRIPTION =
  "Read the current session todo list. Use it to recover the list after context compaction or to check progress before starting the next task. Call it in a step of its own: do not issue it together with todowrite."

// Tool calls that a model issues in one step run concurrently. A todoread started right after a todowrite of the
// same session must see that write, so a session's tool executions run one at a time, in the order they began
// (fleet-kit#38: cos called todowrite and todoread in one step and todoread read the list before it was stored).
const chains = new Map<string, Promise<unknown>>()
let warnedAboutWait = false

// A predecessor that never finishes (a hung storage call) must not block the session's todo tools for good: wait for it
// at most this long (5 s; OPENCODE_TODOLIST_QUEUE_WAIT_MS overrides it, for tests), then go ahead and say so once.
function queueWaitMs(): number {
  const wanted = Number(process.env.OPENCODE_TODOLIST_QUEUE_WAIT_MS)
  return Number.isFinite(wanted) && wanted >= 0 ? wanted : 5000
}

async function waitForPredecessor(before: Promise<unknown>, sessionID: string): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const late = new Promise<"late">((resolve) => {
    timer = setTimeout(() => resolve("late"), queueWaitMs())
  })
  const outcome = await Promise.race([before.then(() => "done" as const), late])
  if (timer !== undefined) clearTimeout(timer)
  if (outcome === "late" && !warnedAboutWait) {
    warnedAboutWait = true
    console.warn(`[aiev.todolist] a todo call of ${sessionID} did not finish in ${queueWaitMs()} ms; the next one goes ahead`)
  }
}

function serialized<T>(sessionID: string, work: () => Promise<T>): Promise<T> {
  const before = chains.get(sessionID) ?? Promise.resolve()
  const run = waitForPredecessor(before, sessionID).then(work)
  const tail = run.then(
    () => undefined,
    () => undefined,
  )
  chains.set(sessionID, tail)
  void tail.then(() => {
    if (chains.get(sessionID) === tail) chains.delete(sessionID)
  })
  return run
}

const plugin: Plugin.Plugin = {
  id: "aiev.todolist",

  async setup(ctx: Plugin.Context) {
    const tools = await ctx.tool.transform((editor) => {
      editor.add({
        name: "todowrite",
        description: TODOWRITE_DESCRIPTION,
        input: TODOS_INPUT_SCHEMA,
        options: { codemode: false },
        execute: (input, context) =>
          serialized(context.sessionID, async () => {
            const todos = normalizeTodos(input)
            const at = Date.now()
            const key = storageKey(context.sessionID)
            const previous = parseTodoRecord(await ctx.storage.get(key))
            const timing = applyTodoWrite(previous?.timing, todos, at)
            await ctx.storage.set(key, { todos, updatedAt: at, timing })
            return {
              content: `Todo list updated (${todos.length} ${todos.length === 1 ? "item" : "items"}):\n${renderTodos(todos)}`,
            }
          }),
      })

      editor.add({
        name: "todoread",
        description: TODOREAD_DESCRIPTION,
        input: TODO_READ_INPUT_SCHEMA,
        options: { codemode: false },
        execute: (_input, context) =>
          serialized(context.sessionID, async () => {
            const record = parseTodoRecord(await ctx.storage.get(storageKey(context.sessionID)))
            const todos = record?.todos ?? []
            const now = Date.now()
            const summary = timingSummary(record?.timing, now)
            const run = record?.timing?.current ?? record?.timing?.last
            const list = renderTodos(todos, (todo) => itemTimeLabel(findItem(run, todo.content), now))
            return { content: `Current todo list${summary ? ` (${summary})` : ""}:\n${list}` }
          }),
      })
    })

    const context = await ctx.session.hook("context", async (event) => {
      const record = parseTodoRecord(await ctx.storage.get(storageKey(event.sessionID)))
      const todos = record?.todos ?? []
      if (!todos.some((todo) => todo.status === "pending" || todo.status === "in_progress")) return
      event.system.push({
        type: "text",
        text: [
          "Current todo list for this session:",
          renderTodos(todos),
          "",
          "Keep it current with the todowrite tool as work progresses.",
        ].join("\n"),
      })
    })

    // Drop the stored list when its session is deleted. Records are tiny, so a
    // startup sweep is unnecessary; the event covers deletions while running.
    const deleted = new AbortController()
    const cleanup = (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: deleted.signal })) {
          if (event.type !== "session.deleted") continue
          const sessionID = event.data?.sessionID ?? event.durable?.aggregateID
          if (sessionID) await ctx.storage.remove(storageKey(sessionID))
        }
      } catch (error) {
        if (!deleted.signal.aborted) {
          console.warn("[aiev.todolist] session.deleted listener stopped:", error)
        }
      }
    })()

    return async () => {
      deleted.abort()
      await cleanup
      await context.dispose()
      await tools.dispose()
    }
  },
}

export default plugin
