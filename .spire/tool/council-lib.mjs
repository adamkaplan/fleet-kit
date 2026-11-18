// council-lib.mjs: the pure half of `council` (.spire/tool/council): who sits, on which model, what a verdict is, what exit it makes.
// No I/O here, so a test needs no model and no git. The members' prompts are NOT here: council.mjs reads them at run time from the
// project's principles and each member's viewpoint (reviewers.mjs, council-prompt.mjs): the only two things the council reads.
import { citeOf } from "./reviewers.mjs";

// The model classes a member may use, fastest first (the project's registry: `review.models` of land.json).
// A class is never a vendor literal here: the id comes from the registry at run time.
export const CLASS_ORDER = ["fast", "everyday", "frontier"];

// The reasoning effort a member runs at when its class names none. "low" is today's behaviour, kept for repos that name none.
export const COUNCIL_EFFORT_DEFAULT = "low";

/** A caller's override of the registry's classes: `raw` is a JSON object {class: {model?, reasoningEffort?}} (env COUNCIL_MODELS); each class is merged over the registry's entry of the same name, the others stay as the registry has them. `source` names the
 *  value in an error. A value that is not that shape throws an Error named CouncilModelsError: there is no silent fallback to the registry. */
export function mergeClasses(classes, raw, source = "COUNCIL_MODELS") {
  const fail = (why) => {
    const e = new Error(`${source}: ${why}`);
    e.name = "CouncilModelsError";
    throw e;
  };
  let over;
  try {
    over = JSON.parse(raw);
  } catch {
    return fail("not valid JSON");
  }
  if (!over || typeof over !== "object" || Array.isArray(over)) return fail("must be a JSON object {class: {model?, reasoningEffort?}}");
  const out = { ...classes };
  for (const [c, v] of Object.entries(over)) {
    if (!v || typeof v !== "object" || Array.isArray(v)) return fail(`class "${c}" must be an object`);
    for (const k of ["model", "reasoningEffort"]) {
      if (k in v && (typeof v[k] !== "string" || !v[k].trim())) return fail(`class "${c}": ${k} must be a non-empty string`);
    }
    out[c] = { ...classes?.[c], ...v };
  }
  return out;
}

/** The bearer a keyless call sends: a gateway replaces the Authorization of every call it forwards, so this is only a placeholder. */
export const GATEWAY_BEARER = "host-gateway";

/** The provider's API base, the ONE built-in address of the council's model route: used when `COUNCIL_MODEL_URL` is unset (a provider needs a key, so `COUNCIL_MODEL_KEY` is then required). */
export const DEFAULT_MODEL_URL = "https://openrouter.ai/api/v1";

/** Where a member's model call goes and what it sends: it comes from the caller's environment; the one thing built in is the provider's address (`DEFAULT_MODEL_URL`), the one thing never built in is a key.
 *  `COUNCIL_MODEL_URL` is the base of a chat-completions API; unset, it is `DEFAULT_MODEL_URL`. With `COUNCIL_MODEL_KEY` the call carries that key; without it a URL that was NAMED is a
 *  gateway that holds the key itself, and the call carries `COUNCIL_MODEL_BEARER` (a per-turn token the gateway may use) or a placeholder; the default URL is no gateway, so with no key there is no route.
 *  Returns {url, bearer, via: "key"|"gateway"} or null when no member can answer (`routeProblem` says which name is wrong). */
export function modelRoute(env) {
  if (routeProblem(env)) return null;
  const named = String(env.COUNCIL_MODEL_URL ?? "")
    .trim()
    .replace(/\/+$/, "");
  const url = `${named || DEFAULT_MODEL_URL}/chat/completions`;
  const key = String(env.COUNCIL_MODEL_KEY ?? "").trim();
  if (key) return { url, bearer: key, via: "key" };
  return { url, bearer: String(env.COUNCIL_MODEL_BEARER ?? "").trim() || GATEWAY_BEARER, via: "gateway" };
}

/** What stops `modelRoute` from finding a route, or null when it has one: `{name, text}`, the exact variable that is wrong and a sentence that says who must act. The environment is the only input;
 *  never a value (it may carry a credential). */
export function routeProblem(env) {
  const named = String(env.COUNCIL_MODEL_URL ?? "").trim();
  if (named) return /^https?:\/\/[^\s/]+/.test(named) ? null : BAD_URL;
  return String(env.COUNCIL_MODEL_KEY ?? "").trim() ? null : KEY_MISSING;
}
const BAD_URL = { name: "COUNCIL_MODEL_URL", text: "COUNCIL_MODEL_URL is set but is not an http(s) base URL: the side that set it must give the model API's base" };
const KEY_MISSING = {
  name: "COUNCIL_MODEL_KEY",
  text: `COUNCIL_MODEL_KEY missing: COUNCIL_MODEL_URL is unset, so the provider's address ${DEFAULT_MODEL_URL} is used and it needs a key: whoever starts the command sets it, so run it in a shell that was given one`,
};

/** Why `modelRoute` found no route, as the headline of the council's refusal: it names the variable and which side must act (the problem `routeProblem` finds; the key's when there is none). */
export const noRouteCause = (env) => (routeProblem(env) ?? KEY_MISSING).text;

// A model id as the registry writes it ("vendor/model-name") and as an author names itself ("model-name") are one model.
export const bare = (id) =>
  String(id ?? "")
    .toLowerCase()
    .replace(/^.*\//, "")
    .replace(/:.*$/, "");

/** The class a member runs on: the first of CLASS_ORDER that has a model and is not the author's own. `classes` is the registry's
 *  `classes` object ({fast:{model}, ...}). A class named by `want` wins when it is usable. Returns {class, model, effort} or null; effort is the class's `reasoningEffort`, COUNCIL_EFFORT_DEFAULT when the class gives none. */
export function pickModel(classes, author, want) {
  const order = want ? [want, ...CLASS_ORDER] : CLASS_ORDER;
  for (const c of order) {
    const model = classes?.[c]?.model;
    if (model && (!author || bare(model) !== bare(author))) {
      return {
        class: c,
        model,
        effort: classes[c].reasoningEffort || COUNCIL_EFFORT_DEFAULT,
      };
    }
  }
  return null;
}

/** A review file's body: what follows its front matter (the gh-aw header is workflow machinery, not the reviewer's scope). */
export function stripFrontMatter(md) {
  const m = /^---\n[\s\S]*?\n---\n/.exec(md);
  return (m ? md.slice(m[0].length) : md).trim();
}

const SEV = new Set(["block", "fix", "note"]);

/** A finding the council gives the agent as advice: rated `block` or `fix` (a `note` is only printed). Advice is never a gate: it is printed once, recorded in the Land-Council trailer, and the landing goes on; the agent follows it or says in one line why not. */
export const isAdvice = (f) => f.severity === "block" || f.severity === "fix";

/** The verdict is the findings' own: any block or fix is advice (verdict `advice`), a note comments, none approves. */
const verdictOf = (findings) => {
  if (findings.some(isAdvice)) {
    return "advice";
  }
  return findings.length ? "comment" : "approve";
};
const citeMark = (f) => (f.uncited ? " [no principle cited: a note]" : "");

/** Parse a member's answer. The verdict is DERIVED from the findings, never trusted from the `verdict:` line: any block or fix finding
 *  is advice, a note is a comment, none approves. No `verdict:` line and no findings block means the member did not
 *  answer in the contract: incomplete. A finding cites a principle by NUMBER (`principles` is the project's text; `P5`, `5` and the rule's bold name all resolve to `P5`):
 *  a finding that cites none, or a number the principles do not hold, is a `note` (flagged `uncited`), whatever severity it was given: it is never advice. */
export function parseVerdict(text, principles = "") {
  const t = String(text ?? "");
  const said = /^\s*verdict:\s*(approve|request-changes|comment|incomplete)\b/im
    .exec(t)?.[1]
    ?.toLowerCase();
  const at = t.search(/^\s*findings:/im);
  if (!said || at < 0) {
    return {
      verdict: "incomplete",
      said: said ?? null,
      findings: [],
      why: "no verdict block in the answer",
    };
  }
  const findings = [];
  const lost = []; // bullet lines that are findings in all but the contract's shape: they cite no principle, so each is a note
  for (const line of t.slice(at).split("\n").slice(1)) {
    const m = /^\s*[-*]\s+(.*)$/.exec(line);
    if (!m) {
      continue;
    }
    const cells = m[1].split("|").map((s) => s.trim());
    const sev = cells[1]?.toLowerCase().replace(/[^a-z]/g, "");
    if (cells.length < 4 || !SEV.has(sev)) {
      if (!/^(none|n\/a|no findings)\b/i.test(m[1])) {
        lost.push(m[1]);
      }
      continue;
    }
    // a cite is a principle NUMBER of the project's principles (`P5`, `5`, or the rule's bold name, which resolves to its number). A finding that cites none is a note: it never stops a landing.
    const asWritten = (/\*\*(.+?)\*\*/.exec(cells[0])?.[1] ?? cells[0]).replace(/[.\s]+$/, "").trim();
    const principle = citeOf(principles, asWritten) ?? "";
    const why = cells[3];
    findings.push({
      principle,
      severity: principle ? sev : "note",
      where: cells[2],
      why: !principle && sev !== "note" ? `(rated ${sev}, cites no principle of the project: a note) ${why}` : why,
      fix: cells.slice(4).join(" | "),
      uncited: !principle,
      grounded: !!principle,
    });
  }
  // a line outside the contract cites no principle number of the project's: it is a note, whatever it says (BLOCK or not), and is never advice. A member that said request-changes and gave
  // nothing at all (no finding, no line) has not answered: incomplete, never approve.
  for (const l of lost) {
    findings.push({ principle: "", severity: "note", where: "-", why: `(outside the contract, cites no principle of the project: a note) ${l}`, fix: "", uncited: true, grounded: false });
  }
  if (said === "request-changes" && !findings.length) {
    return {
      verdict: "incomplete",
      said,
      findings,
      why: "the member said request-changes and gave no finding: never read as approve",
    };
  }
  const verdict = verdictOf(findings);
  const stated =
    said === "incomplete" && !findings.length ? "incomplete" : verdict;
  return {
    verdict: stated,
    said,
    findings,
    why: stated === "incomplete" ? "the member said incomplete" : "",
  };
}

/** One printable line for a member, and its finding lines. `r` = {role, class, model, seconds, verdict, findings, why}. */
export function memberLines(r) {
  const tag = r.model
    ? `${r.model.replace(/^.*\//, "")} [${r.class ?? "-"}] ${r.seconds}s`
    : `no model call [${r.class ?? "-"}]`;
  const head = {
    approve: "approve",
    comment: "approve (notes)",
    advice: "ADVICE",
    incomplete: "UNABLE TO REVIEW",
  }[r.verdict];
  const n = r.findings.filter(isAdvice).length;
  const lines = [
    `council: ${r.role.padEnd(9)} ${head}${n ? ` (${n} advice)` : ""}  ${tag}${r.why ? `  ${r.why}` : ""}`,
  ];
  for (const f of r.findings) {
    const mark = citeMark(f);
    lines.push(
      `  - ${f.severity.toUpperCase()} ${f.principle || "(none)"}${mark} | ${f.where} | ${f.why}${f.fix ? ` | do: ${f.fix}` : ""}`,
    );
  }
  return lines;
}

/** Whether the members' answers hold advice. */
export const hasAdvice = (results) => results.some((r) => r.verdict === "advice");

/** Why a member could not answer, short enough for a trailer: its reason up to the first colon, on one line, never more than 80 characters (the reasons are the tool's own words, never a model's text or a value). */
const shortWhy = (why) => String(why ?? "").split(":")[0].replace(/\s+/g, " ").trim().slice(0, 80);

/** The text `land` writes as the Land-Council trailer value: who ran and what each said. A member that gave advice is `role=advice(rating principle,...)` (the findings that are advice, each with its rating and the principle number it cites);
 *  a member that could not answer is `role=unable to review (reason)`: a note, never a stop. `diffId` ties it to the diff the members read (unsigned). */
export function trailerText(results, diffId) {
  if (!results.length) {
    return "none needed (no path calls for a member)";
  }
  const who = results
    .map((r) => {
      if (r.verdict === "incomplete") {
        const why = shortWhy(r.why);
        return `${r.role}=unable to review${why ? ` (${why})` : ""}`;
      }
      const advice = r.findings.filter(isAdvice).map((f) => `${f.severity} ${f.principle}`);
      return `${r.role}=${r.verdict === "comment" ? "approve" : r.verdict}${advice.length ? `(${advice.join(",")})` : ""}`;
    })
    .join(",");
  const models = [...new Set(results.map((r) => r.class).filter(Boolean))].join("+");
  return `ran: ${who} (models ${models}${diffId ? `, diff ${diffId}` : ""})`;
}

/** What a member is given to read: the changed files whole (`read(path)` gives a file's text, or null when it is not there), each within `fileCap` and all within `totalCap`. No path is special: a file is read because it changed, whatever it is. */
export function wholeFiles(read, paths, fileCap, totalCap) {
  let used = 0;
  const out = [];
  for (const p of paths) {
    const body = read(p);
    if (body === null || used >= totalCap) {
      continue;
    }
    const text = body.length > fileCap ? `${body.slice(0, fileCap)}\n[... cut at ${fileCap} characters]` : body;
    used += text.length;
    out.push(`=== ${p} (whole, at the head) ===\n${text}`);
  }
  return out.join("\n\n");
}
