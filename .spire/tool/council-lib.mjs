// council-lib.mjs: the pure half of `council` (.spire/tool/council): who sits, on which model, what a verdict is, what exit it makes.
// No I/O here, so a test needs no model and no git. The members' prompts are NOT here: council.mjs reads them at run time from the
// project's principles and each member's viewpoint (reviewers.mjs, council-prompt.mjs): the only two things the council reads.
import { createHash } from "node:crypto";
import { citeOf } from "./reviewers.mjs";

// The model classes a member may use, fastest first (the project's registry: `review.models` of land.json).
// A class is never a vendor literal here: the id comes from the registry at run time.
export const CLASS_ORDER = ["fast", "everyday", "frontier"];

/** The bearer a keyless call sends: a gateway replaces the Authorization of every call it forwards, so this is only a placeholder. */
export const GATEWAY_BEARER = "host-gateway";

/** Where a member's model call goes and what it sends: everything comes from the caller's environment, nothing is built in.
 *  `COUNCIL_MODEL_URL` is the base of a chat-completions API (required). With `COUNCIL_MODEL_KEY` the call carries that key; without it the URL is a
 *  gateway that holds the key itself, and the call carries `COUNCIL_MODEL_BEARER` (a per-turn token the gateway may use) or a placeholder.
 *  Returns {url, bearer, via: "key"|"gateway"} or null when no member can answer. */
export function modelRoute(env) {
  const named = String(env.COUNCIL_MODEL_URL ?? "")
    .trim()
    .replace(/\/+$/, "");
  if (!/^https?:\/\/[^\s/]+/.test(named)) return null;
  const url = `${named}/chat/completions`;
  const key = String(env.COUNCIL_MODEL_KEY ?? "").trim();
  if (key) return { url, bearer: key, via: "key" };
  return { url, bearer: String(env.COUNCIL_MODEL_BEARER ?? "").trim() || GATEWAY_BEARER, via: "gateway" };
}

// A model id as the registry writes it ("vendor/model-name") and as an author names itself ("model-name") are one model.
export const bare = (id) =>
  String(id ?? "")
    .toLowerCase()
    .replace(/^.*\//, "")
    .replace(/:.*$/, "");

/** The class a member runs on: the first of CLASS_ORDER that has a model and is not the author's own. `classes` is the registry's
 *  `classes` object ({fast:{model}, ...}). A class named by `want` wins when it is usable. Returns {class, model, effort} or null; effort is the class's `reasoningEffort`, "low" when the registry gives none. */
export function pickModel(classes, author, want) {
  const order = want ? [want, ...CLASS_ORDER] : CLASS_ORDER;
  for (const c of order) {
    const model = classes?.[c]?.model;
    if (model && (!author || bare(model) !== bare(author))) {
      return {
        class: c,
        model,
        effort: classes[c].reasoningEffort || "low",
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

/** A finding that must be closed before the landing: rated `block` or `fix` (a `note` stays a note). */
export const isOpen = (f) => f.severity === "block" || f.severity === "fix";

/** The verdict is the findings' own: any block or fix stands (verdict `block`: the landing stops until it is closed), a note comments, none approves. */
const verdictOf = (findings) => {
  if (findings.some(isOpen)) {
    return "block";
  }
  return findings.length ? "comment" : "approve";
};
const citeMark = (f) => (f.uncited ? " [no principle cited: a note]" : "");

/** Parse a member's answer. The verdict is DERIVED from the findings, never trusted from the `verdict:` line: any block or fix finding
 *  stands, a note is a comment, none approves. No `verdict:` line and no findings block means the member did not
 *  answer in the contract: incomplete. A finding cites a principle by NUMBER (`principles` is the project's text; `P5`, `5` and the rule's bold name all resolve to `P5`):
 *  a finding that cites none, or a number the principles do not hold, is a `note` (flagged `uncited`), whatever severity it was given: it stops nothing. */
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
  // a line outside the contract cites no principle number of the project's: it is a note, whatever it says (BLOCK or not), and stops nothing. A member that said request-changes and gave
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
    block: "BLOCK",
    incomplete: "INCOMPLETE",
  }[r.verdict];
  const n = r.findings.filter(isOpen).length;
  const lines = [
    `council: ${r.role.padEnd(9)} ${head}${n ? ` (${n} to close)` : ""}  ${tag}${r.why ? `  ${r.why}` : ""}${r.carried ? "  (kept from an earlier round)" : ""}`,
  ];
  for (const f of r.findings) {
    const mark = citeMark(f);
    lines.push(
      `  - ${f.severity.toUpperCase()} ${f.principle || "(none)"}${mark} | ${f.where} | ${f.why}${f.fix ? ` | do: ${f.fix}` : ""}`,
    );
  }
  return lines;
}

/** Exit: 0 clean (every member approved or only commented, every fix and block closed) | 18 a member has a finding rated fix or block to close (however many rounds it has been open:
 *  nothing is accepted by a count) | 15 nothing to close but a member could not answer. `loop` = {unresolved} from `settle`. */
export function exitFor(results, loop) {
  if (results.some((r) => r.verdict === "block") || loop?.unresolved?.length) {
    return 18;
  }
  return results.some((r) => r.verdict === "incomplete") ? 15 : 0;
}

/** `--rebut "ROLE=PRINCIPLE: REASON"`: the builder's answer to a member's block. Null when it does not have the three parts. */
export function parseRebuttal(text) {
  const m = /^\s*(\w+)\s*=\s*([^:]+?)\s*:\s*(\S[\s\S]*)$/.exec(
    String(text ?? ""),
  );
  return m
    ? { role: m[1].toLowerCase(), principle: m[2].trim(), reason: m[3].trim() }
    : null;
}

/** What a loop round knows of the rebase since the last one: `state` = the loop's state ({base, diff, exact, law}: the merge base, the patch-id of the diff, a hash of its lines with their white space, and the id of the law (the principles, the viewpoints, the tool's defaults) the last round read), `now` = the same of this one.
 *  `moved`: main moved (another base); `sameDiff`: the change's own diff is the same (patch-id equal, and the same lines to the white space: a patch-id does not see it) and so is the law it is judged by (a main that moved the law is another review). A state with no base (or no law) was written before it was kept: it never reads as moved (or as another law). */
export function rebaseOf(state, now) {
  return {
    moved: !!state?.base && state.base !== now.base,
    sameDiff: !!state?.diff && state.diff === now.diff && (state.exact === undefined || state.exact === now.exact) && (state.law === undefined || state.law === now.law),
  };
}

/** The loop's step: which members run on this diff and which are carried. Only a member with a finding to close (or that never answered) runs again; one that
 *  approved or only commented keeps its verdict from the round that gave it. `prior` = {role: result} from the loop's state.
 *  A rebase onto a moved main does not change what the council read: with the change's own diff the same (`sameDiff`) the verdicts are kept as they are. When the rebase changed
 *  that diff (a conflict resolved in a changed file) nothing is kept: the diff is another one, and every member reads it (`rebase` = rebaseOf). */
export function loopPlan(roles, prior, rebase = {}) {
  const carried = {};
  const run = [];
  const changed = rebase.moved && !rebase.sameDiff;
  for (const role of roles) {
    const p = changed ? undefined : prior?.[role];
    if (p && ["approve", "comment"].includes(p.verdict)) {
      carried[role] = { ...p, carried: true };
    } else {
      run.push(role);
    }
  }
  return { run, carried, changed: !!changed };
}

/** The lines a finding quotes, in a form white space cannot change: each line's runs of white space are one space, edges trimmed, blank lines gone. */
export const normalizeAnchor = (text) =>
  String(text ?? "")
    .split("\n")
    .map((l) => l.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .join("\n");

const ANCHOR_LINES = 20; // a finding that cites a longer range is anchored by its first lines

/** The file and the lines a finding's `where` cites: `a/b.mjs:12`, `a/b.mjs:12-18`, `a/b.mjs:L12-L18`. Null when it names no line. */
export function citedLines(where) {
  const m = /^\s*`?(.+?)`?\s*:\s*L?(\d+)(?:\s*[-\u2013]\s*L?(\d+))?/.exec(String(where ?? ""));
  if (!m) {
    return null;
  }
  const from = Number(m[2]);
  const to = Math.min(Math.max(Number(m[3] ?? from), from), from + ANCHOR_LINES - 1);
  return from >= 1 ? { file: m[1].trim(), from, to } : null;
};

/** A finding's anchor: a hash of the normalized text of the lines it cites, as the head has them (`read(file)` = the file's text at the head, or null). Empty when the finding cites no
 *  line, the file is not there, or the lines are blank: such a finding has no anchor and is never dropped by the council's memory. */
export function anchorOf(where, read) {
  const c = citedLines(where);
  const body = c ? read(c.file) : null;
  if (body === null || body === undefined) {
    return "";
  }
  const text = normalizeAnchor(String(body).split("\n").slice(c.from - 1, c.to).join("\n"));
  return text ? createHash("sha1").update(text).digest("hex").slice(0, 16) : "";
}

/** Put its anchor on every finding rated fix or block of a member that answered this round (`ran`). */
export function withAnchors(results, ran, read) {
  return results.map((r) =>
    ran.includes(r.role) ? { ...r, findings: r.findings.map((f) => (isOpen(f) ? { ...f, anchor: anchorOf(f.where, read) } : f)) } : r,
  );
}

const same = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();
const fileOf = (where) =>
  citedLines(where)?.file ??
  String(where ?? "")
    .replace(/:\d.*$/, "")
    .trim();
/** A finding's identity across rounds: the member, the principle it cites and the file (not the line, which a fix moves). */
const keyOf = (role, f) =>
  `${role}|${String(f.principle ?? "").toLowerCase()}|${fileOf(f.where)}`;
/** A rebuttal answers a finding that cites its principle; a finding that cites none is answered by any rebuttal of its member. */
const answers = (b, role, f) =>
  b.role === role && (!f.principle || same(b.principle, f.principle));

/** The council's memory. A finding's fingerprint is (member, principle, file, anchor). A finding of a member that answered this round, rated fix or block, whose fingerprint is that of a ledger entry
 *  CLOSED BY AN ACCEPTED REBUTTAL (`rebutted`: the builder answered it and the member, having read the answer, stopped raising it) is dropped: the member raised again what it had accepted, on lines that have not
 *  changed. Nothing else is remembered: a `fixed` finding is one whose lines the fix changed (a re-raise there has another anchor, so it stands), and a `withdrawn` or merely silent one is the member's own answer
 *  changing, which closes nothing the tool may keep closed. A finding is reopened by its quoted lines changing (another anchor is another fingerprint). A finding with no anchor, and an entry written
 *  before anchors were kept, never match. The member's verdict is derived again from what is left.
 *  Returns {results, dropped: [{role, principle, where, closed, how}]}. */
export function dropClosed(results, prior, ran) {
  const closed = new Map();
  for (const e of prior ?? []) {
    if (e.status === "rebutted" && e.anchor) {
      closed.set(`${keyOf(e.role, e)}|${e.anchor}`, e);
    }
  }
  const dropped = [];
  const out = results.map((r) => {
    if (!ran.includes(r.role) || !closed.size) {
      return r;
    }
    const kept = r.findings.filter((f) => {
      const e = isOpen(f) && f.anchor ? closed.get(`${keyOf(r.role, f)}|${f.anchor}`) : undefined;
      if (e) {
        dropped.push({ role: r.role, principle: f.principle, where: f.where, closed: e.closed, how: e.status });
      }
      return !e;
    });
    return kept.length === r.findings.length ? r : { ...r, findings: kept, verdict: verdictOf(kept) };
  });
  return { results: out, dropped };
}

/** The ledger of the change's findings rated `fix` or `block` (a `note` is never on it): one entry per finding, `open` until it is closed, and how it closed. Only a
 *  member that answered this round (`ran`) can close its entries, and only by not raising the finding again on the new diff: it is `fixed` when the diff changed since the
 *  finding was raised (the member re-reviewed it clean), `rebutted` when the builder's rebuttal was read by the member (it is in the member's prompt) and the member
 *  did not raise the finding again (the member ACCEPTED it), else `withdrawn` (the same diff and no rebuttal: the member's own answer changed). A finding the member
 *  raises again stays open, whatever the rebuttal said (`refused` is then true). Never closed by a round count. */
export function reconcile(prior, results, { ran, rebuttals, round, diff }) {
  const ledger = (prior ?? []).map((e) => ({ ...e }));
  for (const r of results) {
    if (!ran.includes(r.role)) {
      continue;
    }
    const now = new Map(
      r.findings.filter(isOpen).map((f) => [keyOf(r.role, f), f]),
    );
    for (const e of ledger) {
      if (e.role !== r.role || e.status !== "open") {
        continue;
      }
      const mine = rebuttals.filter((b) => answers(b, e.role, e));
      const f = now.get(e.key);
      if (f) {
        now.delete(e.key);
        Object.assign(e, {
          severity: f.severity,
          where: f.where,
          why: f.why,
          anchor: f.anchor ?? "",
          refused: mine.length > 0,
          diff,
        });
      } else if (e.diff !== diff) {
        Object.assign(e, { status: "fixed", closed: round });
      } else if (mine.length) {
        Object.assign(e, {
          status: "rebutted",
          closed: round,
          rebuttal: mine.map((b) => `${b.principle}: ${b.reason}`).join("; "),
        });
      } else {
        Object.assign(e, { status: "withdrawn", closed: round });
      }
    }
    for (const [key, f] of now) {
      ledger.push({
        role: r.role,
        key,
        severity: f.severity,
        principle: f.principle,
        where: f.where,
        why: f.why,
        anchor: f.anchor ?? "",
        status: "open",
        raised: round,
        diff,
      });
    }
  }
  return ledger;
}

/** After the members answered: a rebuttal that cites nothing real is refused (an Error: a rebuttal cites a principle in force). Then the ledger moves (`reconcile`), and the
 *  No round count closes a finding or refuses a landing: only a fix the member re-reviews clean, or a rebuttal it accepts, closes one.
 *  `prior` = the loop's ledger; `ran` = the roles that answered this round; `principles` = the project's principles text (a rebuttal's cite must be one of its numbers). */
export function settle(
  results,
  {
    prior = [],
    rebuttals = [],
    round,
    principles = "",
    ran = [],
    diff = "",
  },
) {
  const cited = rebuttals.map((b) => {
    const cite = citeOf(principles, b.principle);
    if (!cite) {
      throw new Error(
        `the rebuttal of ${b.role} cites "${b.principle}", which is no principle of the project (.spire/principles.md): a rebuttal cites one by its number, as P3`,
      );
    }
    return { ...b, principle: cite };
  });
  const ledger = reconcile(prior, results, { ran, rebuttals: cited, round, diff });
  const roles = results.map((r) => r.role);
  const unresolved = ledger.filter(
    (e) => e.status === "open" && roles.includes(e.role),
  );
  return { results, ledger, unresolved };
}

const HOW = {
  fixed: (e) => `fixed r${e.closed}`,
  rebutted: (e) =>
    `rebutted r${e.closed}, accepted by ${e.role} (${e.rebuttal.slice(0, 90)})`,
  withdrawn: (e) => `withdrawn by ${e.role} r${e.closed}`,
  open: (e) => (e.refused ? "OPEN, rebuttal not accepted" : "OPEN"),
};
/** One ledger entry as the text the trailer carries: `role severity principle file=how it closed`. */
export const resolutionText = (e) =>
  `${e.role} ${e.severity} ${e.principle || "none"} ${fileOf(e.where) || "-"}=${HOW[e.status](e)}`;

/** The text `land` writes as the Land-Council trailer value: who ran and their verdicts (a member kept from an earlier round is named as kept: its answer is not this round's). `diffId` ties it to the diff the members read (unsigned). `loop` = {round, dropped}: what the council's memory dropped is named, so the evidence says it. */
export function trailerText(results, diffId, loop) {
  if (!results.length) {
    return "none needed (no path calls for a member)";
  }
  const who = results
    .map((r) => `${r.role}=${r.verdict === "comment" ? "approve" : r.verdict}`)
    .join(",");
  const models = [...new Set(results.map((r) => r.class).filter(Boolean))].join(
    "+",
  );
  const kept = results.filter((r) => r.carried).map((r) => r.role);
  return `ran: ${who} (models ${models}${loop?.round ? `, round ${loop.round}` : ""}${diffId ? `, diff ${diffId}` : ""}${kept.length ? `, kept from an earlier round: ${kept.join(",")}` : ""}${loop?.dropped?.length ? `, dropped (re-raised after an accepted rebuttal): ${loop.dropped.map((d) => `${d.role} ${d.principle} r${d.closed}`).join(",")}` : ""})`;
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
