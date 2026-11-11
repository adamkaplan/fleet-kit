// TUI entry point: the host reads the { id, setup } protocol; the
// implementation lives in dist/tui.js (esbuild output).
import mod from "../dist/tui.js"

export default {
  id: "aiev.todolist.tui",
  setup: mod.setup,
}
