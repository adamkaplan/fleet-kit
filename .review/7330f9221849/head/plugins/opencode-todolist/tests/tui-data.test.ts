import assert from "node:assert/strict"
import test from "node:test"
import { headerLabel, headerTimeLabel, latestTodosFromMessages, todowritesFromMessages } from "../src/tui-data"
import { buildTiming } from "../src/timing"

function message(parts: unknown[]) {
  return { content: parts }
}

function tool(name: string, status: string, input: unknown, at?: number) {
  return {
    type: "tool",
    name,
    state: { status, input },
    ...(at === undefined ? {} : { time: { created: at } }),
  }
}

test("latestTodosFromMessages picks the most recent completed todowrite", () => {
  const messages = [
    message([tool("todowrite", "completed", { todos: [{ content: "Old", status: "pending" }] })]),
    message([tool("todoread", "completed", {})]),
    message([tool("todowrite", "completed", { todos: [{ content: "New", status: "in_progress", priority: "high" }] })]),
    message([tool("todowrite", "error", { todos: [{ content: "Failed", status: "pending" }] })]),
  ]
  assert.deepEqual(latestTodosFromMessages(messages), [{ content: "New", status: "in_progress", priority: "high" }])
})

test("latestTodosFromMessages ignores malformed history and empty input", () => {
  assert.equal(latestTodosFromMessages(undefined), undefined)
  assert.equal(latestTodosFromMessages([]), undefined)
  assert.equal(latestTodosFromMessages([message([tool("todowrite", "completed", {})])]), undefined)
  assert.equal(
    latestTodosFromMessages([
      message([tool("todowrite", "completed", { todos: [{ content: "X", status: "nope" }] })]),
    ]),
    undefined,
  )
})

test("latestTodosFromMessages keeps the last valid list when a later call is invalid", () => {
  const messages = [
    message([tool("todowrite", "completed", { todos: [{ content: "Keep", status: "pending" }] })]),
    message([tool("todowrite", "completed", { todos: "broken" })]),
  ]
  assert.deepEqual(latestTodosFromMessages(messages), [{ content: "Keep", status: "pending" }])
})

test("todowritesFromMessages orders writes by timestamp regardless of message order", () => {
  const older = message([tool("todowrite", "completed", { todos: [{ content: "Old", status: "pending" }] }, 1_000)])
  const newer = message([tool("todowrite", "completed", { todos: [{ content: "New", status: "pending" }] }, 2_000)])

  assert.deepEqual(
    todowritesFromMessages([newer, older]).map((write) => write.at),
    [1_000, 2_000],
  )
  assert.equal(latestTodosFromMessages([newer, older])?.[0].content, "New")
  assert.equal(latestTodosFromMessages([older, newer])?.[0].content, "New")
})

test("todowritesFromMessages falls back through part and state timestamps", () => {
  const fromState = message([
    {
      type: "tool",
      name: "todowrite",
      state: { status: "completed", input: { todos: [{ content: "S", status: "pending" }] }, time: { created: 5 } },
    },
  ])
  const fromCompleted = message([
    {
      type: "tool",
      name: "todowrite",
      state: { status: "completed", input: { todos: [{ content: "C", status: "pending" }] } },
      time: { completed: 7 },
    },
  ])

  assert.deepEqual(
    todowritesFromMessages([fromState, fromCompleted]).map((write) => write.at),
    [5, 7],
  )
})

test("todowritesFromMessages keeps untimed writes in order", () => {
  const writes = todowritesFromMessages([
    message([tool("todowrite", "completed", { todos: [{ content: "First", status: "pending" }] })]),
    message([tool("todowrite", "completed", { todos: [{ content: "Second", status: "pending" }] })]),
  ])
  assert.deepEqual(
    writes.map((write) => write.at),
    [null, null],
  )
  assert.deepEqual(
    writes.map((write) => write.todos[0].content),
    ["First", "Second"],
  )
})

const sample = [
  { content: "a", status: "completed" as const },
  { content: "b", status: "pending" as const },
  { content: "c", status: "in_progress" as const },
  { content: "d", status: "pending" as const },
]

test("headerLabel formats count and percentage", () => {
  assert.equal(headerLabel(sample, { count: true, percent: true }), "Todo [1/4] · 25%")
  assert.equal(headerLabel(sample, { count: true, percent: false }), "Todo [1/4]")
  assert.equal(headerLabel(sample, { count: false, percent: true }), "Todo 25%")
  assert.equal(headerLabel(sample, { count: false, percent: false }), "Todo")
})

test("headerLabel rounds and handles an empty list", () => {
  const thirds = [
    { content: "a", status: "completed" as const },
    { content: "b", status: "pending" as const },
    { content: "c", status: "pending" as const },
  ]
  assert.equal(headerLabel(thirds, { count: true, percent: true }), "Todo [1/3] · 33%")
  assert.equal(headerLabel([], { count: true, percent: true }), "Todo [0/0] · 0%")
})

test("headerTimeLabel keeps the live timer separate from the heading without a separator", () => {
  const state = buildTiming([{ at: 1_000, todos: sample }])
  assert.equal(headerTimeLabel(state, true, 253_000), "4m12s")
  assert.equal(headerLabel(sample, { count: true, percent: true }), "Todo [1/4] · 25%")
  assert.equal(headerTimeLabel(state, false, 253_000), undefined)
})

test("headerTimeLabel keeps the finished total fixed and without a separator", () => {
  const state = buildTiming([
    { at: 1_000, todos: [{ content: "A", status: "in_progress" }] },
    { at: 253_000, todos: [{ content: "A", status: "completed" }] },
  ])
  assert.equal(headerTimeLabel(state, true, 999_999), "done in 4m12s")
  assert.equal(headerTimeLabel(state, false, 999_999), undefined)
})

test("headerTimeLabel omits missing, abandoned and cancelled-only timing", () => {
  assert.equal(headerTimeLabel({}, true, 999_999), undefined)
  const abandoned = buildTiming([
    { at: 1_000, todos: [{ content: "A", status: "in_progress" }] },
    { at: 253_000, todos: [] },
  ])
  assert.equal(headerTimeLabel(abandoned, true, 999_999), undefined)
  const cancelled = buildTiming([
    { at: 1_000, todos: [{ content: "A", status: "in_progress" }] },
    { at: 253_000, todos: [{ content: "A", status: "cancelled" }] },
  ])
  assert.equal(headerTimeLabel(cancelled, true, 999_999), undefined)
})
