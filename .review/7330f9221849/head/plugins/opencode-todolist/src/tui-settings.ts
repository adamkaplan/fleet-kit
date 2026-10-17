import type { Plugin } from "@opencode/plugin/tui"

export type TodoDisplaySettings = {
  count: boolean
  percent: boolean
  timer: boolean
  gap: 0 | 1 | 2
  collapseThreshold: 2 | 3 | 5
  border: boolean
}

export const DEFAULT_SETTINGS: TodoDisplaySettings = {
  count: true,
  percent: true,
  timer: true,
  gap: 1,
  collapseThreshold: 2,
  border: false,
}

export const SETTINGS_KEY = "aiev.todolist.settings"

const GAP_VALUES: ReadonlyArray<TodoDisplaySettings["gap"]> = [0, 1, 2]
const THRESHOLD_VALUES: ReadonlyArray<TodoDisplaySettings["collapseThreshold"]> = [2, 3, 5]

/** Coerces stored settings (which may predate a field or be corrupted) into valid values. */
export function normalizeSettings(value: unknown): TodoDisplaySettings {
  if (value === null || typeof value !== "object") return { ...DEFAULT_SETTINGS }
  const raw = value as Record<string, unknown>
  return {
    count: typeof raw.count === "boolean" ? raw.count : DEFAULT_SETTINGS.count,
    percent: typeof raw.percent === "boolean" ? raw.percent : DEFAULT_SETTINGS.percent,
    timer: typeof raw.timer === "boolean" ? raw.timer : DEFAULT_SETTINGS.timer,
    gap: GAP_VALUES.includes(raw.gap as TodoDisplaySettings["gap"])
      ? (raw.gap as TodoDisplaySettings["gap"])
      : DEFAULT_SETTINGS.gap,
    collapseThreshold: THRESHOLD_VALUES.includes(raw.collapseThreshold as TodoDisplaySettings["collapseThreshold"])
      ? (raw.collapseThreshold as TodoDisplaySettings["collapseThreshold"])
      : DEFAULT_SETTINGS.collapseThreshold,
    border: typeof raw.border === "boolean" ? raw.border : DEFAULT_SETTINGS.border,
  }
}

const nextTick = () => new Promise<void>((resolve) => setTimeout(resolve, 0))
const onOff = (value: boolean) => (value ? "on" : "off")

/** Human-readable label for the header separator setting. */
export function gapLabel(gap: TodoDisplaySettings["gap"]): string {
  if (gap === 0) return "none"
  if (gap === 1) return "line"
  return "line + blank"
}

/**
 * Native settings menu: each pick applies and persists immediately, then the
 * menu reopens (the host closes the dialog before the next select).
 */
export function openSettingsMenu(
  context: Plugin.Context,
  settings: TodoDisplaySettings,
  update: (mutation: (draft: TodoDisplaySettings) => void) => Promise<void>,
): void {
  void (async () => {
    while (true) {
      const choice = await context.ui.dialog.select<string>({
        title: "Todolist settings",
        options: [
          { title: `Show count: ${onOff(settings.count)}`, value: "count" },
          { title: `Show percentage: ${onOff(settings.percent)}`, value: "percent" },
          { title: `Show timer: ${onOff(settings.timer)}`, value: "timer" },
          { title: `Header separator: ${gapLabel(settings.gap)}`, value: "gap" },
          { title: `Collapse threshold: ${settings.collapseThreshold}`, value: "threshold" },
          { title: `Border: ${onOff(settings.border)}`, value: "border" },
        ],
      })
      if (choice === undefined) return

      if (choice === "count") {
        await update((draft) => {
          draft.count = !draft.count
        })
      } else if (choice === "percent") {
        await update((draft) => {
          draft.percent = !draft.percent
        })
      } else if (choice === "timer") {
        await update((draft) => {
          draft.timer = !draft.timer
        })
      } else if (choice === "gap") {
        const picked = await context.ui.dialog.select<TodoDisplaySettings["gap"]>({
          title: "Header separator",
          options: GAP_VALUES.map((value) => ({
            title: value === 0 ? "none" : value === 1 ? "────" : "──── + blank",
            value,
          })),
          current: settings.gap,
        })
        if (picked !== undefined) {
          await update((draft) => {
            draft.gap = picked
          })
        }
      } else if (choice === "threshold") {
        const picked = await context.ui.dialog.select<TodoDisplaySettings["collapseThreshold"]>({
          title: "Collapse threshold",
          options: THRESHOLD_VALUES.map((value) => ({ title: String(value), value })),
          current: settings.collapseThreshold,
        })
        if (picked !== undefined) {
          await update((draft) => {
            draft.collapseThreshold = picked
          })
        }
      } else if (choice === "border") {
        await update((draft) => {
          draft.border = !draft.border
        })
      }

      await nextTick()
    }
  })()
}
