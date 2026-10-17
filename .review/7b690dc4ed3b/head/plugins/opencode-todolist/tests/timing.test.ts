import assert from "node:assert/strict"
import test from "node:test"
import { applyTodoWrite, buildTiming, formatDuration, itemTimeLabel, timingSummary } from "../src/timing"
import { parseTimingState, type Todo, type TodoStatus } from "../src/todos"

const t = (content: string, status: TodoStatus): Todo => ({ content, status })

test("formatDuration renders seconds, minutes and hours", () => {
  assert.equal(formatDuration(0), "0s")
  assert.equal(formatDuration(42_000), "42s")
  assert.equal(formatDuration(252_000), "4m12s")
  assert.equal(formatDuration(3_780_000), "1h03m")
  assert.equal(formatDuration(Number.NaN), "0s")
})

test("applyTodoWrite starts a run on the first open list and closes it when nothing is open", () => {
  let state = applyTodoWrite(undefined, [t("A", "pending")], 1_000)
  assert.equal(state.current?.startedAt, 1_000)
  assert.equal(state.last, undefined)

  state = applyTodoWrite(state, [t("A", "completed")], 61_000)
  assert.equal(state.current, undefined)
  assert.equal(state.last?.startedAt, 1_000)
  assert.equal(state.last?.endedAt, 61_000)
  assert.equal(state.last?.abandoned, undefined)
  assert.equal(state.last?.items[0].startedAt, undefined)
  assert.equal(state.last?.items[0].completedAt, 61_000)
})

test("applyTodoWrite records in_progress starts and completed ends per task", () => {
  let state = applyTodoWrite(undefined, [t("A", "pending")], 0)
  state = applyTodoWrite(state, [t("A", "in_progress")], 10_000)
  assert.equal(state.current?.items[0].startedAt, 10_000)

  state = applyTodoWrite(state, [t("A", "completed")], 40_000)
  assert.equal(state.last?.items[0].startedAt, 10_000)
  assert.equal(state.last?.items[0].completedAt, 40_000)
})

test("applyTodoWrite keeps the first timestamp for a repeated status", () => {
  let state = applyTodoWrite(undefined, [t("A", "in_progress")], 5_000)
  state = applyTodoWrite(state, [t("A", "in_progress")], 9_000)
  assert.equal(state.current?.items[0].startedAt, 5_000)
})

test("applyTodoWrite treats cancellation as a closed task", () => {
  let state = applyTodoWrite(undefined, [t("A", "pending")], 0)
  state = applyTodoWrite(state, [t("A", "cancelled")], 30_000)
  assert.equal(state.current, undefined)
  assert.equal(state.last?.endedAt, 30_000)
})

test("applyTodoWrite abandons a run replaced by an unrelated open list", () => {
  let state = applyTodoWrite(undefined, [t("A", "pending")], 0)
  state = applyTodoWrite(state, [t("B", "pending")], 20_000)

  assert.equal(state.last?.abandoned, true)
  assert.equal(state.last?.endedAt, 20_000)
  assert.equal(state.current?.startedAt, 20_000)
  assert.deepEqual(
    state.current?.items.map((item) => item.content),
    ["B"],
  )
})

test("applyTodoWrite keeps the run when an open task survives the rewrite", () => {
  let state = applyTodoWrite(undefined, [t("A", "pending"), t("B", "pending")], 0)
  state = applyTodoWrite(state, [t("A", "completed"), t("C", "pending")], 10_000)

  assert.equal(state.current?.startedAt, 0)
  assert.equal(state.current?.items.find((item) => item.content === "A")?.completedAt, 10_000)
  assert.equal(state.current?.items.find((item) => item.content === "C")?.status, "pending")
})

test("applyTodoWrite closes a run as abandoned when the list is cleared", () => {
  let state = applyTodoWrite(undefined, [t("A", "pending")], 0)
  state = applyTodoWrite(state, [], 15_000)
  assert.equal(state.current, undefined)
  assert.equal(state.last?.abandoned, true)
  assert.equal(state.last?.endedAt, 15_000)
})

test("applyTodoWrite ignores a list that is already closed", () => {
  const state = applyTodoWrite(undefined, [t("A", "completed")], 0)
  assert.deepEqual(state, {})
})

test("buildTiming replays writes and skips untimed ones", () => {
  const state = buildTiming([
    { at: null, todos: [t("A", "pending")] },
    { at: 1_000, todos: [t("A", "pending")] },
    { at: 2_000, todos: [t("A", "completed")] },
  ])
  assert.equal(state.last?.startedAt, 1_000)
  assert.equal(state.last?.endedAt, 2_000)
})

test("timingSummary reports running, finished and abandoned states", () => {
  const running = applyTodoWrite(undefined, [t("A", "in_progress")], 0)
  assert.equal(timingSummary(running, 252_000), "running for 4m12s")

  const finished = applyTodoWrite(running, [t("A", "completed")], 252_000)
  assert.equal(timingSummary(finished, 999_999), "finished in 4m12s")

  const cancelled = applyTodoWrite(
    applyTodoWrite(undefined, [t("A", "pending")], 0),
    [t("A", "cancelled")],
    30_000,
  )
  assert.equal(timingSummary(cancelled, 30_000), undefined)

  const abandoned = applyTodoWrite(undefined, [t("A", "pending")], 0)
  const next = applyTodoWrite(abandoned, [t("B", "pending")], 60_000)
  assert.equal(timingSummary(next, 60_000), "running for 0s")

  const cleared = applyTodoWrite(next, [], 90_000)
  assert.equal(timingSummary(cleared, 90_000), "previous list abandoned after 30s")
})

test("itemTimeLabel shows live time in progress and fixed time when completed", () => {
  assert.equal(itemTimeLabel({ content: "A", status: "in_progress", startedAt: 0 }, 90_000), "1m30s")
  assert.equal(itemTimeLabel({ content: "A", status: "completed", startedAt: 0, completedAt: 45_000 }, 90_000), "45s")
  assert.equal(itemTimeLabel({ content: "A", status: "pending" }, 90_000), undefined)
  assert.equal(itemTimeLabel(undefined, 90_000), undefined)
})

test("parseTimingState keeps valid runs and drops garbage", () => {
  assert.equal(parseTimingState(undefined), undefined)
  assert.equal(parseTimingState({ current: { startedAt: "nope" } }), undefined)

  const state = parseTimingState({
    current: { startedAt: 1, items: [{ content: "A", status: "in_progress", startedAt: 2 }, "junk"] },
  })
  assert.deepEqual(state?.current?.items, [{ content: "A", status: "in_progress", startedAt: 2 }])
})
