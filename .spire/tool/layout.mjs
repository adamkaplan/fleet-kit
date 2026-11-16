// layout.mjs: where everything is, resolved from where THIS FILE is (LAYOUT.md). The tool lives in <project>/.spire/tool/; the project's own files are its siblings in .spire/.
// Nothing here looks at the current directory, and no project path is named: the same code runs from any checkout or worktree that holds a copy of the tool.
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** The layout of the copy of the tool whose folder is `tool` (default: the folder this file is in). */
export function layoutOf(tool = dirname(fileURLToPath(import.meta.url))) {
  const t = resolve(tool);
  const spire = resolve(t, "..");
  return {
    tool: t, // .spire/tool: the vendored copy (never edited)
    spire, // .spire: the project's own files live here
    root: resolve(spire, ".."), // the project root: the checkout the tool runs from (not necessarily the tree under test)
    land: join(spire, "land.json"),
    box: join(spire, "box.json"),
    principles: join(spire, "principles.md"),
    review: join(spire, "review"),
  };
}

export const layout = layoutOf();
