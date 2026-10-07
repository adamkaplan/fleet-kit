/** @jsxImportSource @opentui/solid */
import { createEffect, createMemo, createSignal, For, onCleanup, Show } from "solid-js"
import type { Plugin } from "@opencode/plugin/tui"
import type { Todo, TodoStatus } from "./todos.js"
import { headerLabel, headerTimeLabel, todowritesFromMessages } from "./tui-data.js"
import { buildTiming, findItem, hasCompletedItem, itemTimeLabel } from "./timing.js"
import {
  normalizeSettings,
  openSettingsMenu,
  SETTINGS_KEY,
  type TodoDisplaySettings,
} from "./tui-settings.js"

/** V1 glyphs: ✓ completed, • in progress, blank otherwise. */
function mark(status: TodoStatus): string {
  if (status === "completed") return "✓"
  if (status === "in_progress") return "•"
  return " "
}

function TodoRow(props: { context: Plugin.Context; todo: Todo; time?: string }) {
  const color = () =>
    props.todo.status === "in_progress"
      ? props.context.theme.text.feedback.warning.base
      : props.context.theme.text.muted

  return (
    <box flexDirection="row" gap={0}>
      <text flexShrink={0} style={{ fg: color() }}>
        {`[${mark(props.todo.status)}] `}
      </text>
      <text flexGrow={1} wrapMode="word" style={{ fg: color() }}>
        {props.todo.content}
      </text>
      <Show when={props.time}>
        <text flexShrink={0} style={{ fg: props.context.theme.text.muted }}>
          {` · ${props.time}`}
        </text>
      </Show>
    </box>
  )
}

/** V1-parity todo list at the end of the sidebar, with configurable heading. */
function TodoStrip(props: { context: Plugin.Context; sessionID: string; settings: TodoDisplaySettings }) {
  const [open, setOpen] = createSignal(true)
  const [now, setNow] = createSignal(Date.now())
  const display = () => normalizeSettings(props.settings)

  const writes = createMemo(() => {
    try {
      return todowritesFromMessages(props.context.data.session.message.list(props.sessionID))
    } catch {
      return []
    }
  })
  const timing = createMemo(() => buildTiming(writes()))
  const todos = createMemo(() => {
    const list = writes()
    return list.length === 0 ? [] : list[list.length - 1].todos
  })
  const run = createMemo(() => timing().current ?? timing().last)
  const finished = createMemo(() => {
    const state = timing()
    return (
      state.current === undefined &&
      state.last !== undefined &&
      state.last.abandoned !== true &&
      hasCompletedItem(state.last)
    )
  })

  // Only tick while a run is open; the closed total is rendered from its end time.
  createEffect(() => {
    if (!display().timer || timing().current === undefined) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    onCleanup(() => clearInterval(id))
  })

  const show = createMemo(() => {
    const list = todos()
    if (list.length === 0) return false
    if (list.some((todo) => todo.status !== "completed")) return true
    return display().timer && finished()
  })
  const collapsible = () => todos().length > display().collapseThreshold

  const header = createMemo(() => headerLabel(todos(), display()))
  const headerTime = createMemo(() => headerTimeLabel(timing(), display().timer, now()))

  return (
    <Show when={show()}>
      <box
        flexDirection="column"
        gap={0}
        border={display().border}
        {...(display().border ? { borderColor: props.context.theme.text.muted } : {})}
        paddingLeft={display().border ? 1 : 0}
        paddingRight={display().border ? 1 : 0}
      >
        <box width="100%" flexDirection="row" gap={1} onMouseDown={() => collapsible() && setOpen((value) => !value)}>
          <Show when={collapsible()}>
            <text flexShrink={0} fg={props.context.theme.text.base}>{open() ? "▼" : "▶"}</text>
          </Show>
          <text flexGrow={1} minWidth={0} fg={props.context.theme.text.base}>
            <b>{header()}</b>
          </text>
          <Show when={headerTime()}>
            <text flexShrink={0} fg={props.context.theme.text.base}>
              <b>{headerTime()}</b>
            </text>
          </Show>
        </box>
        <Show when={display().gap > 0}>
          <box height={1} border={["top"]} borderColor={props.context.theme.text.muted} />
        </Show>
        <Show when={display().gap > 1}>
          <box height={display().gap - 1} />
        </Show>
        <Show when={!collapsible() || open()}>
          <For each={todos()}>
            {(todo) => {
              const time = () => (display().timer ? itemTimeLabel(findItem(run(), todo.content), now()) : undefined)
              return <TodoRow context={props.context} todo={todo} time={time()} />
            }}
          </For>
        </Show>
      </box>
    </Show>
  )
}

/** Settings command lives in the app slot so it stays available when the sidebar is hidden. */
function SettingsRoot(props: {
  context: Plugin.Context
  settings: TodoDisplaySettings
  update: (mutation: (draft: TodoDisplaySettings) => void) => Promise<void>
}) {
  props.context.keymap.layer(() => ({
    mode: "global" as const,
    commands: [
      {
        id: "aiev.todolist.settings",
        title: "Todolist: Settings",
        description: "Configure the sidebar todo list",
        slash: { name: "todo-sections" },
        palette: true,
        run: () => openSettingsMenu(props.context, props.settings, props.update),
      },
    ],
  }))
  return null
}

const mod: Plugin.Definition = {
  id: "aiev.todolist.tui",

  setup(context) {
    const [settings, updateSettings] = context.storage.store<TodoDisplaySettings>(SETTINGS_KEY, {
      initial: normalizeSettings(undefined),
    })

    context.ui.slot({
      append: "app",
      render: () => <SettingsRoot context={context} settings={settings} update={updateSettings} />,
    })

    context.ui.slot({
      append: "sidebar.content",
      render: (props) => <TodoStrip context={context} sessionID={props.sessionID} settings={settings} />,
    })
  },
}

export default mod
