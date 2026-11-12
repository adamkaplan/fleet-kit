// Pod plugin: no shell call of a pod runs longer than 300000 ms (5 minutes). The pod's tool calls end at 5 minutes,
// so a longer call must go through cw-run. Only the spec's `timeout` changes; the command and env are untouched.
export const CAP_MS = 300000;
export const NOTE = `This call was limited to ${CAP_MS} ms (5 minutes); run anything longer with cw-run start and poll it with cw-run wait.`;

export function clampSpec(spec) {
  const t = spec.timeout;
  spec.timeout = typeof t === "number" && Number.isFinite(t) && t > 0 && t <= CAP_MS ? t : CAP_MS; // absent, zero, NaN, huge: the cap
  return spec;
}

// Appends the fixed sentence to a result text only when the call ended by the cap (the text says it timed out).
export function noteOf(text) {
  const s = typeof text === "string" ? text : "";
  return /time(d)? ?out/i.test(s) && !s.includes(NOTE) ? s + "\n" + NOTE : s;
}

const UNAVAILABLE = "cw-tool-cap: shell hooks unavailable, calls are NOT capped";

export default {
  id: "cw.tool-cap",
  async setup(api) {
    if (!(api && api.shell && typeof api.shell.hook === "function")) { console.log(UNAVAILABLE); return; }
    try {
      api.shell.hook("create.before", async (spec) => { clampSpec(spec); });
    } catch (_) { console.log(UNAVAILABLE); return; }
    try {
      api.shell.hook("run.after", async (res) => { if (res && typeof res.output === "string") res.output = noteOf(res.output); });
    } catch (_) { console.log("cw-tool-cap: run.after hook unavailable, the timeout note is not added"); }
  },
};
