// council-tools.mjs: the read-only tools a council member may call to look beyond the diff (a caller, a callee, the test of a changed file): the
// reviewers' own rule is one hop from a changed file. Every tool reads the HEAD tree through git (never the working tree, so what is read is what lands),
// runs no shell (argv only) and writes nothing. A member that cannot run or edit anything is the reviewers' "No execution" rule.
import { execFileSync } from "./versions.mjs";

const CAP = 24_000;
const git = (args) => {
  try {
    return execFileSync("git", args, {
      encoding: "utf8",
      maxBuffer: 1 << 26,
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch (e) {
    return e.status === 1 && !e.stderr
      ? ""
      : `error: ${String(e.stderr || e.message).split("\n")[0]}`;
  }
};
const cut = (s) =>
  s.length > CAP
    ? `${s.slice(0, CAP)}\n[cut at ${CAP} characters; ask for a narrower range]`
    : s || "(nothing)";

export const TOOLS = [
  {
    name: "read_file",
    description:
      "Read a file of the changed tree (path from the repository root), optionally a line range.",
    properties: {
      path: { type: "string" },
      start: { type: "integer" },
      end: { type: "integer" },
    },
    required: ["path"],
  },
  {
    name: "grep",
    description:
      "Search the changed tree for an extended regular expression; optionally within one path. Returns path:line:text.",
    properties: { pattern: { type: "string" }, path: { type: "string" } },
    required: ["pattern"],
  },
  {
    name: "git_diff",
    description:
      "The change itself (base to head), optionally limited to one path.",
    properties: { path: { type: "string" } },
    required: [],
  },
  {
    name: "git_log",
    description: "The commits of the change (base to head), subject and body.",
    properties: {},
    required: [],
  },
  {
    name: "list_files",
    description:
      "List the files of a directory of the changed tree (path from the repository root; empty for the root).",
    properties: { path: { type: "string" } },
    required: [],
  },
].map((t) => ({
  type: "function",
  function: {
    name: t.name,
    description: t.description,
    parameters: {
      type: "object",
      properties: t.properties,
      required: t.required,
    },
  },
}));

/** Run one tool call against `head`; always returns text (an error is text the member can read). */
export function runTool(head, name, a, base) {
  const path = String(a?.path ?? "").replace(/^\/+/, "");
  if (name === "read_file") {
    const body = git(["show", `${head}:${path}`]);
    const from = Math.max(1, Number(a.start) || 1);
    const to = Number(a.end) || Infinity;
    return cut(
      body.startsWith("error:")
        ? body
        : body
            .split("\n")
            .slice(from - 1, to === Infinity ? undefined : to)
            .map((l, i) => `${from + i}: ${l}`)
            .join("\n"),
    );
  }
  if (name === "grep") {
    return cut(
      git([
        "grep",
        "-n",
        "-I",
        "-E",
        "-e",
        String(a?.pattern ?? ""),
        head,
        "--",
        ...(path ? [path] : []),
      ])
        .split("\n")
        .slice(0, 80)
        .map((l) => l.replace(`${head}:`, ""))
        .join("\n"),
    );
  }
  if (name === "git_diff" && base) {
    return cut(git(["diff", "-U8", base, head, ...(path ? ["--", path] : [])]));
  }
  if (name === "git_log" && base) {
    return cut(git(["log", "--format=%h %an%n%B", `${base}..${head}`]));
  }
  if (name === "list_files") {
    return cut(
      git([
        "ls-tree",
        "--name-only",
        head,
        path ? `${path.replace(/\/$/, "")}/` : ".",
      ]),
    );
  }
  return `error: no tool named ${name}`;
}
