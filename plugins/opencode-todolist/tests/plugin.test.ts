import assert from "node:assert/strict"
import test, { mock } from "node:test"
import type { Plugin } from "@opencode/plugin"
import plugin from "../src/index"

type ToolDef = {
  name: string
  execute: (input: unknown, context: { sessionID: string }) => Promise<{ content?: string }>
}

type ContextHook = (event: { sessionID: string; system: Array<{ type: string; text: string }> }) => Promise<void> | void

function fakeEvents() {
  const queue: Array<unknown> = []
  let wake: (() => void) | undefined
  let closed = false
  const signalDone = () => {
    closed = true
    wake?.()
    wake = undefined
  }
  const subscribe = (options?: { signal?: AbortSignal }) => {
    options?.signal?.addEventListener("abort", signalDone)
    return {
      [Symbol.asyncIterator]() {
        return {
          async next() {
            while (queue.length === 0 && !closed) {
              await new Promise<void>((resolve) => {
                wake = resolve
              })
            }
            if (queue.length === 0) return { value: undefined, done: true }
            return { value: queue.shift(), done: false }
          },
        }
      },
    }
  }
  return {
    subscribe,
    emit(event: unknown) {
      queue.push(event)
      wake?.()
      wake = undefined
    },
  }
}

function fakeContext() {
  const tools = new Map<string, ToolDef>()
  const hooks = new Map<string, ContextHook>()
  const storage = new Map<string, unknown>()
  const events = fakeEvents()

  const editor = {
    add: (tool: ToolDef) => tools.set(tool.name, tool),
    update: () => {},
    remove: () => {},
    namespace: () => {},
    list: () => [],
    get: () => undefined,
  }

  const context = {
    tool: {
      transform: async (callback: (value: unknown) => void) => {
        callback(editor)
        return { dispose: async () => {} }
      },
    },
    session: {
      hook: async (name: string, callback: ContextHook) => {
        hooks.set(name, callback)
        return { dispose: async () => {} }
      },
    },
    event: {
      subscribe: events.subscribe,
    },
    storage: {
      get: async (key: string) => storage.get(key),
      set: async (key: string, value: unknown) => {
        storage.set(key, value)
      },
      remove: async (key: string) => {
        storage.delete(key)
      },
      scan: async () => ({ entries: [] }),
    },
  } as unknown as Plugin.Context

  return { context, tools, hooks, storage, events }
}

test("setup registers todowrite and todoread", async () => {
  const fake = fakeContext()
  await plugin.setup(fake.context)
  assert.deepEqual([...fake.tools.keys()].sort(), ["todoread", "todowrite"])
})

test("todos are stored and read per session", async () => {
  const fake = fakeContext()
  await plugin.setup(fake.context)

  const write = fake.tools.get("todowrite")!
  const written = await write.execute(
    { todos: [{ content: "A", status: "pending", priority: "high" }] },
    { sessionID: "ses_1" },
  )
  assert.match(written.content ?? "", /Todo list updated \(1 item\)/)

  const stored = fake.storage.get("todos/ses_1") as { todos: unknown[]; updatedAt: number }
  assert.deepEqual(stored.todos, [{ content: "A", status: "pending", priority: "high" }])
  assert.equal(typeof stored.updatedAt, "number")

  const read = fake.tools.get("todoread")!
  const mine = await read.execute({}, { sessionID: "ses_1" })
  assert.match(mine.content ?? "", /1\. \[ \] A — high priority/)
  const other = await read.execute({}, { sessionID: "ses_2" })
  assert.match(other.content ?? "", /empty/)
})

test("context hook injects the open todo list into the system prompt", async () => {
  const fake = fakeContext()
  await plugin.setup(fake.context)
  await fake.tools.get("todowrite")!.execute(
    { todos: [{ content: "Open task", status: "in_progress" }] },
    { sessionID: "ses_1" },
  )

  const event = { sessionID: "ses_1", system: [] as Array<{ type: string; text: string }> }
  await fake.hooks.get("context")!(event)

  assert.equal(event.system.length, 1)
  assert.match(event.system[0].text, /Open task/)
  assert.match(event.system[0].text, /todowrite/)
})

test("context hook stays quiet when nothing is open", async () => {
  const fake = fakeContext()
  await plugin.setup(fake.context)
  await fake.tools.get("todowrite")!.execute(
    { todos: [{ content: "Done", status: "completed" }] },
    { sessionID: "ses_1" },
  )

  const finished = { sessionID: "ses_1", system: [] as Array<{ type: string; text: string }> }
  await fake.hooks.get("context")!(finished)
  assert.equal(finished.system.length, 0)

  const unknown = { sessionID: "ses_unknown", system: [] as Array<{ type: string; text: string }> }
  await fake.hooks.get("context")!(unknown)
  assert.equal(unknown.system.length, 0)
})

test("todoread reports a running timer for the current run", async () => {
  mock.timers.enable({ apis: ["Date"], now: 1_000 })
  try {
    const fake = fakeContext()
    await plugin.setup(fake.context)
    const write = fake.tools.get("todowrite")!
    await write.execute({ todos: [{ content: "Work", status: "in_progress" }] }, { sessionID: "ses_1" })

    mock.timers.tick(252_000)
    const output = await fake.tools.get("todoread")!.execute({}, { sessionID: "ses_1" })
    assert.match(output.content ?? "", /running for 4m12s/)
    assert.match(output.content ?? "", /\[•\] Work — 4m12s/)
  } finally {
    mock.timers.reset()
  }
})

test("todoread reports the total when the run finishes", async () => {
  mock.timers.enable({ apis: ["Date"], now: 1_000 })
  try {
    const fake = fakeContext()
    await plugin.setup(fake.context)
    const write = fake.tools.get("todowrite")!
    await write.execute({ todos: [{ content: "A", status: "pending" }] }, { sessionID: "ses_1" })

    mock.timers.tick(120_000)
    await write.execute({ todos: [{ content: "A", status: "completed" }] }, { sessionID: "ses_1" })

    const output = await fake.tools.get("todoread")!.execute({}, { sessionID: "ses_1" })
    assert.match(output.content ?? "", /finished in 2m00s/)
    assert.match(output.content ?? "", /\[x\] A/)
  } finally {
    mock.timers.reset()
  }
})

test("session.deleted removes the stored list for that session", async () => {
  const fake = fakeContext()
  const dispose = (await plugin.setup(fake.context)) as () => Promise<void>
  try {
    await fake.tools.get("todowrite")!.execute({ todos: [{ content: "A", status: "pending" }] }, { sessionID: "ses_1" })
    assert.ok(fake.storage.has("todos/ses_1"))

    fake.events.emit({ type: "session.created", data: { sessionID: "ses_1" } })
    await new Promise((resolve) => setImmediate(resolve))
    assert.ok(fake.storage.has("todos/ses_1"))

    fake.events.emit({ type: "session.deleted", data: { sessionID: "ses_1" }, durable: { aggregateID: "ses_1" } })
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(fake.storage.has("todos/ses_1"), false)
  } finally {
    await dispose()
  }
})

test("a todoread issued together with a todowrite sees the write (fleet-kit#38)", async () => {
  const fake = fakeContext()
  const slow = fake.context.storage as unknown as { set: (key: string, value: unknown) => Promise<void> }
  const set = slow.set
  slow.set = async (key, value) => {
    await new Promise((resolve) => setTimeout(resolve, 20))
    await set(key, value)
  }
  await plugin.setup(fake.context)
  const ctx = { sessionID: "ses_cos" }
  const [written, read] = await Promise.all([
    fake.tools.get("todowrite")!.execute({ todos: [{ content: "A", status: "in_progress" }] }, ctx),
    fake.tools.get("todoread")!.execute({}, ctx),
  ])
  assert.match(written.content ?? "", /updated \(1 item\)/)
  assert.match(read.content ?? "", /1\. \[•\] A/)
  assert.doesNotMatch(read.content ?? "", /empty/)
})

test("two parallel todowrites of one session do not lose the timing of either", async () => {
  const fake = fakeContext()
  await plugin.setup(fake.context)
  const ctx = { sessionID: "ses_p" }
  await Promise.all([
    fake.tools.get("todowrite")!.execute({ todos: [{ content: "A", status: "pending" }] }, ctx),
    fake.tools.get("todowrite")!.execute({ todos: [{ content: "A", status: "completed" }] }, ctx),
  ])
  const stored = fake.storage.get("todos/ses_p") as { todos: Array<{ status: string }> }
  assert.equal(stored.todos[0].status, "completed", "the later write wins, in the order the calls began")
})
