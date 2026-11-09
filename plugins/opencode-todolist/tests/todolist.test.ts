import assert from "node:assert/strict"
import test from "node:test"
import { hasOpenTodos, normalizeTodos, parseTodoRecord, renderTodos, storageKey } from "../src/index"

test("storageKey scopes todos to the session", () => {
  assert.equal(storageKey("ses_abc"), "todos/ses_abc")
})

test("normalizeTodos accepts a valid list and trims content", () => {
  const todos = normalizeTodos({
    todos: [
      { content: "  Write tests  ", status: "pending", priority: "high" },
      { content: "Ship it", status: "in_progress" },
    ],
  })
  assert.deepEqual(todos, [
    { content: "Write tests", status: "pending", priority: "high" },
    { content: "Ship it", status: "in_progress" },
  ])
})

test("normalizeTodos rejects malformed input", () => {
  assert.throws(() => normalizeTodos(undefined), /todos/)
  assert.throws(() => normalizeTodos({}), /todos/)
  assert.throws(() => normalizeTodos({ todos: "nope" }), /array/)
  assert.throws(() => normalizeTodos({ todos: [{}] }), /content/)
  assert.throws(() => normalizeTodos({ todos: [{ content: "x", status: "done" }] }), /status/)
  assert.throws(() => normalizeTodos({ todos: [{ content: "x", status: "pending", priority: "urgent" }] }), /priority/)
})

test("renderTodos renders status marks and priorities", () => {
  const list = renderTodos([
    { content: "Pending", status: "pending" },
    { content: "Doing", status: "in_progress", priority: "high" },
    { content: "Done", status: "completed" },
    { content: "Dropped", status: "cancelled", priority: "low" },
  ])
  assert.equal(
    list,
    [
      "1. [ ] Pending",
      "2. [•] Doing — high priority",
      "3. [x] Done",
      "4. [-] Dropped — low priority",
    ].join("\n"),
  )
})

test("renderTodos reports an empty list", () => {
  assert.match(renderTodos([]), /empty/)
})

test("renderTodos appends an optional per-task suffix", () => {
  const list = renderTodos(
    [{ content: "Done", status: "completed", priority: "high" }],
    (todo) => (todo.status === "completed" ? "3m" : undefined),
  )
  assert.equal(list, "1. [x] Done — high priority — 3m")
})

test("hasOpenTodos only counts pending and in progress", () => {
  assert.equal(hasOpenTodos([]), false)
  assert.equal(hasOpenTodos([{ content: "a", status: "completed" }]), false)
  assert.equal(hasOpenTodos([{ content: "a", status: "cancelled" }]), false)
  assert.equal(hasOpenTodos([{ content: "a", status: "pending" }]), true)
  assert.equal(hasOpenTodos([{ content: "a", status: "in_progress" }]), true)
})

test("parseTodoRecord survives garbage and keeps valid entries", () => {
  assert.equal(parseTodoRecord(undefined), undefined)
  assert.equal(parseTodoRecord({ todos: "nope" }), undefined)
  const record = parseTodoRecord({
    todos: [
      { content: "ok", status: "pending", priority: "medium" },
      { content: "bad-status", status: "nope" },
      "junk",
    ],
    updatedAt: 123,
  })
  assert.deepEqual(record, {
    todos: [{ content: "ok", status: "pending", priority: "medium" }],
    updatedAt: 123,
  })
})

test("parseTodoRecord keeps a valid timing state", () => {
  const record = parseTodoRecord({
    todos: [{ content: "ok", status: "pending" }],
    updatedAt: 5,
    timing: { current: { startedAt: 1, items: [{ content: "ok", status: "pending", startedAt: 1 }] } },
  })
  assert.equal(record?.timing?.current?.startedAt, 1)
  assert.deepEqual(record?.timing?.current?.items, [{ content: "ok", status: "pending", startedAt: 1 }])
})

test("parseTodoRecord drops unusable timing", () => {
  const record = parseTodoRecord({ todos: [], updatedAt: 0, timing: { current: { startedAt: "x" } } })
  assert.equal(record?.timing, undefined)
})
