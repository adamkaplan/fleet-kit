/** @jsxImportSource @opentui/solid */
// The Decisions section of the TUI sidebar: what waits on you, read-only. It reads decisions.json, which the
// switchboard daemon keeps (docs/switchboard.md, "Decisions (PR 14)"), and draws it. It holds no credential,
// makes no network call and never spawns a process. The file is replaced by rename, so the DIRECTORY is
// watched; one slow re-read covers a missed event, and one timer fires when a list would turn stale.
import { createSignal, For } from "solid-js"
import { watch } from "node:fs"
import { ROW_WIDTH, SAFETY_MS, buildView, concernsFile, loadSnapshot, msUntilStale, stateDirOf, viewOf } from "./decisions.mjs"

export default {
  id: "fleet.decisions",
  setup(api: any) {
    const dir = stateDirOf(process.env)
    const who = viewOf(process.env) // PR 18: from the pane's environment, set by `launch` and `bootstrap`
    const read = () => {
      const now = Date.now()
      const snapshot = loadSnapshot(dir, now)
      return { view: buildView(snapshot, now, ROW_WIDTH, who), next: msUntilStale(snapshot) }
    }
    const first = read()
    const [view, setView] = createSignal(first.view)
    let stale: ReturnType<typeof setTimeout> | undefined
    const refresh = () => {
      const { view: next, next: staleIn } = read()
      setView(next)
      if (stale) clearTimeout(stale)
      stale = staleIn == null ? undefined : setTimeout(refresh, staleIn)
    }
    let watcher: ReturnType<typeof watch> | undefined
    if (dir) {
      try {
        watcher = watch(dir, { persistent: false }, (_event, filename) => {
          if (concernsFile(filename)) refresh()
        })
        watcher.on("error", () => {})
      } catch {
        // no watcher: the safety re-read below still keeps the list within a minute of the file
      }
    }
    const safety = setInterval(refresh, SAFETY_MS)
    refresh()
    api.ui.slot({
      append: "sidebar.content",
      render: () => (
        <box flexDirection="column" paddingBottom={1}>
          <text>
            <b>{view().title}</b>
          </text>
          <For each={view().groups}>
            {(group, i) => (
              <box flexDirection="column">
                {i() > 0 ? <text>{"\u2500".repeat(ROW_WIDTH)}</text> : null}
                <text>
                  <b>{group.repo}</b>
                </text>
                <For each={group.entries}>
                  {(entry) => (
                    <box flexDirection="column">
                      <text>{entry.head}</text>
                      <For each={entry.lines}>{(line) => <text>{line}</text>}</For>
                    </box>
                  )}
                </For>
              </box>
            )}
          </For>
          <For each={view().rows}>{(row) => <text>{row.text}</text>}</For>
        </box>
      ),
    })
    return () => {
      watcher?.close()
      clearInterval(safety)
      if (stale) clearTimeout(stale)
    }
  },
}
