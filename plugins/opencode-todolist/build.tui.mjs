import * as esbuild from "esbuild"
import { solidPlugin } from "esbuild-plugin-solid"

// TUI bundle: Solid JSX compiled against @opentui/solid intrinsics.
await esbuild.build({
  entryPoints: ["src/tui.tsx"],
  outfile: "dist/tui.js",
  format: "esm",
  platform: "node",
  bundle: true,
  external: ["@opencode/*", "@opentui/*", "solid-js"],
  plugins: [solidPlugin({ solid: { moduleName: "@opentui/solid", generate: "universal" } })],
})
