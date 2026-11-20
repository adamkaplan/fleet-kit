// The policy front of the Copilot bridge (ask #2341). Node standard library only. The ONLY listener a pod can reach: the bridge
// (copilot-api 0.7.0) is bound to 127.0.0.1 and this front forwards to it. No secret is read, held or logged here.
// Forwards only POST /chat/completions and POST /v1/chat/completions; answers GET /usage; every other path or method is 404.
// Model policy: an id containing "opus" (any case) is refused 403; every other id becomes claude-haiku-5.5 (Fast class)
// Usage: the day's spend in credits is read from each answer's copilot_usage (total_nano_aiu, else token_details), see spend(); there is no daily cap.
// with the caller's effort (council default low; omitted when the caller sends none). The bridge passes the whole body on to Copilot unchanged, so BOTH
// reasoning.effort and reasoning_effort are set. Log: one count line per request, never a body, an answer, a header or a token.
import http from "node:http";
import fs from "node:fs";

const env = process.env;
const int = (v, d) => (/^[0-9]{1,9}$/.test(v || "") && Number(v) > 0 ? Number(v) : d);
const PORT = int(env.BRIDGE_FRONT_PORT, 8080);
const HOST = env.BRIDGE_FRONT_HOST || "0.0.0.0";
const UP_HOST = "127.0.0.1";
const UP_PORT = int(env.BRIDGE_UPSTREAM_PORT, 4141);
const num = (v, d) => (/^[0-9]{1,9}(\.[0-9]{1,9})?$/.test(v || "") && Number(v) > 0 ? Number(v) : d);
const FALLBACK = num(env.BRIDGE_FALLBACK_CREDITS, 1);     // counted for an answer that carries no usage at all
const NANO = 1e9;
const MAX_ANSWER = 16 * 1024 * 1024;
const MAX_BODY = int(env.BRIDGE_MAX_BODY, 400000);
const TIMEOUT_MS = 160000;
const CLOCK_FILE = env.BRIDGE_CLOCK_FILE || "";   // a test hook set only in the pod's own environment: a file holding the day (YYYY-MM-DD)
const SONNET = "claude-sonnet-5.5";
const HAIKU = "claude-haiku-5.5";
const BANNED_TEXT = "model banned from the council: opus";
// Only these fields of a request go on to the bridge (the bridge passes the body to Copilot unchanged): nothing else can name a model or a route.
const FIELDS = ["messages", "stream", "temperature", "top_p", "max_tokens", "tools", "tool_choice", "stop", "seed", "response_format", "presence_penalty", "frequency_penalty"];
const MAX_TOKENS = 64000;
const PATHS = new Set(["/chat/completions", "/v1/chat/completions"]);

const days = new Map();   // day -> { requests, refused, models: { id: count } }
const today = () => {
  if (CLOCK_FILE) { try { const d = fs.readFileSync(CLOCK_FILE, "utf8").trim(); if (/^\d{4}-\d{2}-\d{2}$/.test(d)) return d; } catch {} }
  return new Date().toISOString().slice(0, 10);
};
const dayRow = (d) => {
  if (!days.has(d)) { days.set(d, { requests: 0, refused: 0, models: {} , nano: 0 }); for (const k of [...days.keys()].sort().slice(0, -14)) days.delete(k); }
  return days.get(d);
};
// Nano credits of one answer: total_nano_aiu, else the sum of token_count * cost_per_batch / batch_size; null when neither is there.
const spendOf = (u) => {
  if (!u || typeof u !== "object") return null;
  if (typeof u.total_nano_aiu === "number" && u.total_nano_aiu >= 0) return u.total_nano_aiu;
  if (!Array.isArray(u.token_details) || !u.token_details.length) return null;
  let sum = 0;
  for (const t of u.token_details) {
    if (!t || typeof t.token_count !== "number" || typeof t.cost_per_batch !== "number" || typeof t.batch_size !== "number" || t.batch_size <= 0) return null;
    sum += t.token_count * t.cost_per_batch / t.batch_size;
  }
  return sum;
};
// The copilot_usage of an answer: a JSON body, or the last block of a stream of "data:" lines.
const usageOf = (text) => {
  try { const j = JSON.parse(text); return j && j.copilot_usage; } catch {}
  let found = null;
  for (const line of text.split("\n")) {
    if (!line.startsWith("data:") || !line.includes("copilot_usage")) continue;
    try { const j = JSON.parse(line.slice(5)); if (j && j.copilot_usage) found = j.copilot_usage; } catch {}
  }
  return found;
};
const logLine = (entry) => process.stdout.write(JSON.stringify(entry) + "\n");

const send = (res, status, obj) => {
  const text = JSON.stringify(obj);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(text) });
  res.end(text);
};

// The served model and the effort for a requested id. Returns { refuse } or { model, effort } (effort null: leave the caller's).
const policy = (id) => {
  const norm = id.toLowerCase();
  if (norm.includes("opus")) return { refuse: true };
  return { model: HAIKU, effort: null };
};

const server = http.createServer((req, res) => {
  const started = Date.now();
  const day = today();
  const path = (req.url || "").split("?")[0];
  let done = false;
  const finish = (status, served, bytes) => {
    if (done) return;
    done = true;
    const entry = { ts: new Date().toISOString(), day, method: req.method === "GET" || req.method === "POST" ? req.method : "other", path: PATHS.has(path) || path === "/usage" ? path : "other", status, model: served, bytes, ms: Date.now() - started };
    logLine(entry);
  };
  const answer = (status, obj, served) => { send(res, status, obj); finish(status, served || "-", 0); };

  if (req.method === "GET" && path === "/usage") {
    const out = { day, days: {} };
    for (const [d, row] of days) {
      const models = {};
      for (const [m, v] of Object.entries(row.models)) models[m] = { requests: v.requests, credits: Number((v.nano / NANO).toFixed(4)) };
      out.days[d] = { credits: Number((row.nano / NANO).toFixed(4)), requests: row.requests, refused: row.refused, models };
    }
    return answer(200, out);
  }
  if (req.method !== "POST" || !PATHS.has(path)) return answer(404, { error: "not found" });
  const declared = Number(req.headers["content-length"]);
  if (declared > MAX_BODY) { req.resume(); return answer(413, { error: "body over the size cap" }); }

  const chunks = [];
  let size = 0;
  let over = false;
  req.on("data", (c) => {
    size += c.length;
    if (over) return;
    if (size > MAX_BODY) { over = true; chunks.length = 0; return; }
    chunks.push(c);
  });
  req.on("error", () => finish(400, "-", 0));
  req.on("end", () => {
    if (over) return answer(413, { error: "body over the size cap" });
    let body;
    try { body = JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch { return answer(400, { error: "the body is not JSON" }); }
    if (!body || typeof body !== "object" || Array.isArray(body) || typeof body.model !== "string" || !Array.isArray(body.messages)) {
      return answer(400, { error: "the body needs a string model and a messages array" });
    }
    const verdict = policy(body.model);
    const row = dayRow(day);
    if (verdict.refuse) { row.refused += 1; return answer(403, { error: BANNED_TEXT }, "refused"); }
    row.requests += 1;
    const mrow = row.models[verdict.model] || (row.models[verdict.model] = { requests: 0, nano: 0 });
    mrow.requests += 1;
    let payload;
    try {   // a body that cannot be re-serialised (for example nested too deep) is a 400, never a crash
      const effort = verdict.effort || (body.reasoning && typeof body.reasoning.effort === "string" ? body.reasoning.effort : (typeof body.reasoning_effort === "string" ? body.reasoning_effort : null));
      const out = { model: verdict.model };
      for (const k of FIELDS) if (Object.prototype.hasOwnProperty.call(body, k)) out[k] = body[k];
      if (typeof out.max_tokens === "number" && out.max_tokens > MAX_TOKENS) out.max_tokens = MAX_TOKENS;
      if (effort) { out.reasoning = { effort }; out.reasoning_effort = effort; }
      const chk = (v, d) => { if (d > 64) throw new RangeError(); if (v && typeof v === "object") for (const x of (Array.isArray(v) ? v : Object.values(v))) chk(x, d + 1); };
      chk(out, 0);
      payload = Buffer.from(JSON.stringify(out));
    } catch { row.requests -= 1; mrow.requests -= 1; return answer(400, { error: "the body cannot be processed" }); }
    const up = http.request({ host: UP_HOST, port: UP_PORT, method: "POST", path: "/chat/completions", timeout: TIMEOUT_MS,
      headers: { "content-type": "application/json", "content-length": payload.length } }, (ur) => {   // no incoming header is forwarded: no Authorization
      res.writeHead(ur.statusCode || 502, { "content-type": ur.headers["content-type"] || "application/json" });
      ur.pipe(res);
      const parts = [];
      let got = 0;
      ur.on("data", (c) => { got += c.length; if (got <= MAX_ANSWER) parts.push(c); });
      ur.on("end", () => {
        if ((ur.statusCode || 502) < 300) {
          let nano = got <= MAX_ANSWER ? spendOf(usageOf(Buffer.concat(parts).toString("utf8"))) : null;
          if (nano === null) { nano = FALLBACK * NANO; logLine({ ts: new Date().toISOString(), day, event: "unpriced", model: verdict.model, count: 1 }); }
          row.nano += nano;
          mrow.nano += nano;
        }
        finish(ur.statusCode || 502, verdict.model, payload.length);
      });
      ur.on("error", () => { res.destroy(); finish(502, verdict.model, payload.length); });
    });
    res.on("close", () => up.destroy());   // a caller that goes away ends the upstream call
    up.on("timeout", () => { up.destroy(); if (!res.headersSent) send(res, 504, { error: "the bridge did not answer in time" }); finish(504, verdict.model, payload.length); });
    up.on("error", () => { if (!res.headersSent) send(res, 502, { error: "the bridge is not reachable" }); finish(502, verdict.model, payload.length); });
    up.end(payload);
  });
});
server.requestTimeout = 30000;   // the time to RECEIVE a request; the upstream wait has its own 160 s timeout
server.maxConnections = 64;
process.on("uncaughtException", () => logLine({ ts: new Date().toISOString(), event: "error" }));   // never the error text: it could hold a body
server.headersTimeout = 30000;
server.listen(PORT, HOST, () => logLine({ ts: new Date().toISOString(), event: "listening", port: PORT, cap: MAX_BODY }));
