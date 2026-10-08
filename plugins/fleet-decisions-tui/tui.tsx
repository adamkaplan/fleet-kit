/** @jsxImportSource @opentui/solid */
// The Decisions section of the TUI sidebar: what waits on you, read-only, and the Heads-up section (PR notices). It reads decisions.json, which the
// switchboard daemon keeps (docs/switchboard.md, "Decisions (PR 14)"), and draws it. It holds no credential,
// makes no network call and never spawns a process (a row's link is an OSC 8 link the terminal opens, or an OSC 52 copy). The file is replaced by rename, so the DIRECTORY is
// watched; one slow re-read covers a missed event, and one timer fires when a list would turn stale.
import { createSignal, For } from "solid-js"
import { watch } from "node:fs"
import { HEAVY, THIN, safeUrl, ROW_WIDTH, SAFETY_MS, buildView, concernsFile, hasNotices, loadSnapshot, msUntilStale, stateDirOf, viewOf } from "./decisions.mjs"

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
    // A click on a row copies its link (OSC 52, through the renderer: no process, no network); a terminal that
    // honours OSC 8 also opens the same link on its own click (Cmd-click in iTerm2, Ctrl-click in herdr).
    const copyLink = (raw: unknown, event: any) => {
      const url = safeUrl(raw)
      if (!url || event?.modifiers?.ctrl) return // no link, or a terminal link click: leave it to the terminal
      let copied = false
      try {
        copied = api.renderer?.copyToClipboardOSC52?.(url) === true
      } catch {
        copied = false
      }
      try {
        api.ui?.toast?.show?.({ message: copied ? `Copied ${url}` : url, variant: copied ? "success" : "info", duration: 4000 })
      } catch {
        // a toast is a nicety
      }
    }
    api.ui.slot({
      append: "sidebar.content",
      render: () => (
        <box flexDirection="column" paddingBottom={1}>
          <text>
            <b>{view().title}</b>
          </text>
          <For each={view().sections ?? []}>
            {(section) => (
              <box flexDirection="column">
                <text>{HEAVY(ROW_WIDTH)}</text>
                <text>
                  <b>{section.title}</b>
                </text>
                {section.groups.length === 0 && (view().groups.length > 0 || hasNotices(view())) ? <text>none</text> : null}
                <For each={section.groups}>
                  {(group, i) => (
                    <box flexDirection="column">
                      {i() > 0 ? <text>{HEAVY(ROW_WIDTH)}</text> : null}
                      <text>
                        <b>{group.repo}</b>
                      </text>
                      <For each={group.entries}>
                        {(entry, j) => (
                          <box flexDirection="column" onMouseUp={entry.url ? (event: any) => copyLink(entry.url, event) : undefined}>
                            {j() > 0 ? <text>{THIN(ROW_WIDTH)}</text> : null}
                            <For each={entry.lines}>{(line) => <text>{line}</text>}</For>
                            <For each={(entry.rows ?? []).flat()}>{(line) => <text>{line}</text>}</For>
                            <For each={entry.facts ?? []}>{(line) => <text>{line}</text>}</For>
                            {safeUrl(entry.url) ? (
                              <text>
                                <a href={safeUrl(entry.url) as string}>{entry.head}</a>
                              </text>
                            ) : (
                              <text>{entry.head}</text>
                            )}
                          </box>
                        )}
                      </For>
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
