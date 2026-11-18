#!/usr/bin/env node
// council.mjs [--base REF] [--head REF] [--roles a,b] [--author-model ID] [--class fast|everyday|frontier] [--json-only]
// The council in a box: ONE synchronous command that runs the review members this change's paths call for, in parallel, and says
// what they found. `land` calls it; it runs alone too (`.spire/tool/council`). It never fixes anything, and it never refuses a landing: its findings are ADVICE
// (captain ruling, 15 Nov 2026, and again 17 Nov 2026: council findings are always advice and never gates; the agent decides and records why not). The advice is printed once and recorded in the trailer; a member that could not answer is recorded as unable to review.
//   paths (classify.mjs: the reviewers' `calls`)  ->  members (none when no path calls for one: docs, tests)  ->  each member: ONE contract (council-prompt.mjs)
//   + its viewpoint (reviewers.mjs: the project's .spire/review/<role>.md, else the tool's default) + the project's principles (.spire/principles.md), all read from BASE at run time, never
//   copied: the council reads those two things and nothing else  + the diff and the changed files whole  ->  one model call on a Model class (the registry's `classes`, never a vendor
//   literal), a model other than the author's own  ->  the verdict block  ->  one line per member. A finding cites a principle by number; one that cites none is a note.
// Exit: 0 whatever the members said or whether they answered | 2 bad input (not a worktree, an unknown reviewer, a bad COUNCIL_MODELS).
// Last stdout line is JSON {"result","exit","members":[...],"trailer":"..."}: `result` is advice | unable-to-review | clean; `trailer` is the Land-Council value `land` writes.
// The model route (council-lib `modelRoute`) comes only from the caller's environment: COUNCIL_MODEL_URL (the API base) and, unless that is a gateway that holds the key,
// COUNCIL_MODEL_KEY, sent only as the Authorization header.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { applyPins, execFileSync, spawnSync } from "./versions.mjs";
import { runTool, TOOLS } from "./council-tools.mjs";
import { loadConfig } from "./config.mjs";
import { patchId } from "./patch-id.mjs";
import { memberPrompt } from "./council-prompt.mjs";
import { DEFAULT_PRINCIPLES, loadReviewers, PRINCIPLES_PATH } from "./reviewers.mjs";
import {
  hasAdvice,
  mergeClasses,
  modelRoute,
  noRouteCause,
  memberLines,
  parseVerdict,
  pickModel,
  trailerText,
  wholeFiles,
} from "./council-lib.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const MEMBER_SECONDS = 150;
const FILE_CAP = 40_000;
const TOTAL_CAP = 160_000;

const git = (...a) =>
  execFileSync("git", a, { encoding: "utf8", maxBuffer: 1 << 28 });
const gitOr = (fallback, ...a) => {
  try {
    return git(...a);
  } catch {
    return fallback;
  }
};

function args(argv) {
  const o = {};
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (
      [
        "--base",
        "--head",
        "--roles",
        "--author-model",
        "--class",
      ].includes(k)
    ) {
      o[k.slice(2)] = argv[++i];
    } else if (k === "--json-only") {
      o.jsonOnly = true;
    } else {
      process.stderr.write(
        "usage: council [--base REF] [--head REF] [--roles inspector,warden] [--author-model ID] [--class fast]\n",
      );
      process.exit(2);
    }
  }
  return o;
}

const changedFiles = (head, paths) =>
  wholeFiles((p) => gitOr(null, "show", `${head}:${p}`), paths, FILE_CAP, TOTAL_CAP);

const MAX_TURNS = 40; // stop reading by about call 35
const MEMBER_TOTAL = 300_000;
/** One member's conversation: the model may call the read-only tools (council-tools.mjs) for up to MAX_TURNS answers, then must give the verdict block. */
async function ask(route, model, effort, system, user, head, base) {
  const t0 = Date.now();
  const messages = [
    { role: "system", content: system },
    { role: "user", content: user },
  ];
  let last = "";
  for (let turn = 1; turn <= MAX_TURNS; turn++) {
    const final = turn === MAX_TURNS || Date.now() - t0 > MEMBER_TOTAL;
    if (final) {
      messages.push({
        role: "user",
        content:
          "Stop reading. Answer now, in the output form your contract gives.",
      });
    }
    const body = JSON.stringify({
      model,
      temperature: 0,
      max_tokens: 24000,
      reasoning: { effort: process.env.COUNCIL_EFFORT?.trim() || effort },
      messages,
      ...(final ? {} : { tools: TOOLS }),
    });
    let msg;
    for (let attempt = 0; attempt < 2 && !msg; attempt++) {
      try {
        const res = await fetch(route.url, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${route.bearer}`,
            "content-type": "application/json",
          },
          body,
          signal: AbortSignal.timeout(MEMBER_SECONDS * 1000),
        });
        const j = await res.json().catch(() => ({}));
        msg = j?.choices?.[0]?.message;
        if (process.env.COUNCIL_TRACE) {
          process.stderr.write(
            `trace ${model} turn ${turn} ${Math.round((Date.now() - t0) / 1000)}s ${j?.choices?.[0]?.finish_reason} tools ${msg?.tool_calls?.map((c) => c.function.name).join(",") ?? "-"} out ${j?.usage?.completion_tokens}\n`,
          );
        }
        if (!res.ok || !msg || (!msg.content && !msg.tool_calls?.length)) {
          last = res.ok
            ? `empty answer (${j?.choices?.[0]?.finish_reason ?? j?.error?.message ?? "no reason"})`
            : `model call refused (${res.status})`;
          msg = undefined;
          if (!res.ok && res.status < 500 && res.status !== 429) {
            return { error: last };
          }
        }
      } catch (e) {
        last =
          e?.name === "TimeoutError"
            ? `no answer in ${MEMBER_SECONDS}s`
            : "model call failed";
      }
    }
    if (!msg) {
      return { error: last };
    }
    if (!msg.tool_calls?.length) {
      return { text: msg.content, turns: turn };
    }
    messages.push({
      role: "assistant",
      content: msg.content ?? "",
      tool_calls: msg.tool_calls,
    });
    for (const c of msg.tool_calls) {
      let args = {};
      try {
        args = JSON.parse(c.function.arguments || "{}");
      } catch {}
      messages.push({
        role: "tool",
        tool_call_id: c.id,
        content: runTool(head, c.function.name, args, base),
      });
    }
  }
  return { error: "no verdict after the last turn" };
}

async function main() {
  applyPins(); // the project's pinned tool versions first: the git calls, the classifier and everything else this run starts
  const o = args(process.argv.slice(2));
  const top = gitOr("", "rev-parse", "--show-toplevel").trim();
  if (!top) {
    process.stderr.write("council: not in a git worktree\n");
    process.exit(2);
  }
  process.chdir(top);
  const head = git("rev-parse", o.head ?? "HEAD").trim();
  const base = git("merge-base", o.base ?? "origin/main", head).trim();
  const info = JSON.parse(
    execFileSync(process.execPath, [join(here, "classify.mjs"), base, head], {
      encoding: "utf8",
    }),
  );
  const roles = o.roles ? o.roles.split(",").filter(Boolean) : info.council;
  const say = (s) => process.stdout.write(`${s}\n`);
  const patch = git("diff", base, head);
  const diffId = patchId(patch).slice(0, 12);
  const finish = (results) => {
    const trailer = trailerText(results, diffId);
    const unable = results.filter((r) => r.verdict === "incomplete");
    const result = hasAdvice(results) ? "advice" : unable.length ? "unable-to-review" : "clean";
    const members = results.map(
      ({ role, class: c, model, seconds, verdict, findings }) => ({
        role,
        class: c,
        model,
        seconds,
        verdict,
        findings,
      }),
    );
    say(
      JSON.stringify({
        result,
        exit: 0,
        head,
        members,
        trailer,
      }),
    );
    process.exit(0);
  };
  if (!roles.length) {
    say(
      `council: none needed: no path of this change calls for a member`,
    );
    finish([]);
  }
  const route = modelRoute(process.env); // none: every member is unable to review (said below), never a stop
  // the laws, from BASE: a change cannot rewrite the law it is judged by
  const law = (p) =>
    gitOr(null, "show", `${base}:${p}`) ?? gitOr("", "show", `${head}:${p}`);
  // the council reads exactly two things: the project's principles and the reviewers' viewpoints (the project's, over the tool's defaults)
  // a project with no principles file is judged against the tool's named default principles: a finding stands only by citing one of those numbers, every other finding is a note (spire doctor says to make the file)
  let principles = law(PRINCIPLES_PATH).trim();
  if (!principles) {
    principles = readFileSync(DEFAULT_PRINCIPLES, "utf8").trim();
    say(
      `council: the project has no ${PRINCIPLES_PATH} (a project owns it; spire install makes it from the default; run spire doctor): findings are judged against the tool's default principles, and one that cites none of them is a note`,
    );
  }
  const reviewers = loadReviewers({
    read: law,
    list: (d) =>
      (gitOr("", "ls-tree", "--name-only", base, `${d}/`) ||
        gitOr("", "ls-tree", "--name-only", head, `${d}/`))
        .split("\n")
        .filter(Boolean)
        .map((n) => n.slice(d.length + 1)),
  });
  const unknown = roles.filter((r) => !reviewers.has(r));
  if (unknown.length) {
    say(
      `council: no reviewer named ${unknown.join(",")} (reviewers: ${[...reviewers.keys()].join(", ")}; a viewpoint is .spire/review/<role>.md with \`role: <role>\` in its front matter)`,
    );
    process.exit(2);
  }
  const models = (await loadConfig()).review.models;
  const registry = JSON.parse(
    readFileSync(join(top, models), "utf8"),
  );
  // the repo's registry is the default; a host's COUNCIL_MODELS is merged over it class by class
  try {
    if (process.env.COUNCIL_MODELS) registry.classes = mergeClasses(registry.classes, process.env.COUNCIL_MODELS);
  } catch (e) {
    if (e.name !== "CouncilModelsError") throw e;
    say(`council: ${e.name}: ${e.message}`);
    process.exit(2);
  }
  const author =
    o["author-model"] ??
    process.env.COUNCIL_AUTHOR_MODEL ??
    process.env.PI_MODEL ??
    "";
  const paths = git("diff", "--name-only", base, head)
    .split("\n")
    .filter(Boolean);
  const diff = git("diff", "-U8", base, head).slice(0, TOTAL_CAP);
  const userBase = `Review this change. Head: ${head}\n\nFiles changed (${paths.length}):\n${paths.join("\n")}\n\n=== the diff (base ${base.slice(0, 12)} to head ${head.slice(0, 12)}) ===\n${diff}\n\n${changedFiles(head, paths)}`;
  say(
    `council: members ${roles.join(", ")}${author ? `; author model ${author}` : ""}; model route ${route?.via ?? "none"}`,
  );

  const ran = await Promise.all(
    roles.map(async (role) => {
      const t0 = Date.now();
      // every member runs the same contract on its viewpoint; the model is a Model class (Fast by default), never a vendor literal
      const pick = pickModel(registry.classes, author, o.class);
      const done = (r) => ({
        role,
        class: pick?.class,
        model: pick?.model,
        seconds: Math.round((Date.now() - t0) / 1000),
        findings: [],
        why: "",
        ...r,
      });
      if (!pick) {
        return done({
          verdict: "incomplete",
          why: "no model other than the author's",
        });
      }
      if (!route) {
        return done({
          verdict: "incomplete",
          model: undefined,
          why: noRouteCause(process.env),
        });
      }
      const def = reviewers.get(role);
      const system = memberPrompt({
        role,
        def,
        principles,
        head,
        readFile: (p) => law(p),
      });
      const a = await ask(route, pick.model, pick.effort, system, userBase, head, base);
      return a.error
        ? done({ verdict: "incomplete", why: a.error })
        : done(parseVerdict(a.text, principles));
    }),
  );
  const results = ran;
  if (!o.jsonOnly) {
    for (const r of results) {
      for (const l of memberLines(r)) {
        say(l);
      }
    }
  }
  // the advice is printed once, above, and recorded in the trailer; it never stops the landing (the agent follows it, or says in one line why not)
  const unable = results.filter((r) => r.verdict === "incomplete");
  if (hasAdvice(results)) {
    say(
      "council: the advice above did not block this landing: it is recorded in the Land-Council trailer. Follow it (in a follow-up commit) or say in ONE line why not (`land --why-not TEXT`, optional)",
    );
  }
  if (unable.length) {
    say(
      `council: ${unable.map((r) => r.role).join(", ")} unable to review (the reason is above): recorded in the Land-Council trailer; it did not block this landing`,
    );
  }
  finish(results);
}

main().catch((e) => {
  process.stderr.write(`council: ${e?.message ?? e}\n`);
  process.exit(2);
});
